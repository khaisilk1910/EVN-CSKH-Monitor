const API = "/api/evncskh";
let monthly = {SanLuong: [], TienDien: []};
let daily = [];
let summary = {};
let currentAccount = "";

function token() {
  try {
    const parentTokens = window.parent && window.parent.__tokenCache && window.parent.__tokenCache.tokens;
    if (parentTokens?.access_token) return parentTokens.access_token;
  } catch (_) {}
  try {
    const raw = localStorage.getItem("hassTokens");
    if (raw) return JSON.parse(raw)?.access_token || "";
  } catch (_) {}
  return "";
}
async function apiFetch(path) {
  const accessToken = token();
  const headers = accessToken ? {Authorization: `Bearer ${accessToken}`} : {};
  const response = await fetch(path, {headers, cache: "no-store"});
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
  return response.json();
}
const $ = (id) => document.getElementById(id);
const fmt = (v, digits=0) => v == null || Number.isNaN(Number(v)) ? "—" : Number(v).toLocaleString("vi-VN", {maximumFractionDigits:digits});
const money = (v) => v == null ? "—" : `${fmt(v)} ₫`;
const kwh = (v) => v == null ? "—" : `${fmt(v,3)} kWh`;
function showNotice(text) { $("notice").textContent = text; $("notice").classList.toggle("hidden", !text); }

async function boot() {
  try {
    const options = await apiFetch(`${API}/options`);
    const accounts = options.accounts || (options.accounts_json ? JSON.parse(options.accounts_json) : []);
    if (!accounts.length) { showNotice("Chưa có tài khoản EVN CSKH Monitor được cấu hình."); return; }
    $("accountSelect").innerHTML = accounts.map(a => `<option value="${escapeHtml(a.customer_id)}">${escapeHtml(a.name || a.customer_id)}</option>`).join("");
    $("accountSelect").addEventListener("change", () => loadAccount($("accountSelect").value));
    $("yearSelect").addEventListener("change", renderMonthly);
    $("reloadBtn").addEventListener("click", () => loadAccount(currentAccount));
    $("applyRange").addEventListener("click", renderDaily);
    $("clearRange").addEventListener("click", () => { $("startDate").value=""; $("endDate").value=""; renderDaily(); });
    await loadAccount(accounts[0].customer_id);
  } catch (err) {
    console.error(err);
    showNotice("Không đọc được API EVN CSKH Monitor. Nếu Home Assistant vừa khởi động, hãy tải lại trang. Chi tiết: " + err.message);
  }
}

async function loadAccount(account) {
  currentAccount = account;
  showNotice("");
  $("reloadBtn").disabled = true;
  try {
    [monthly, daily, summary] = await Promise.all([
      apiFetch(`${API}/monthly/${encodeURIComponent(account)}`),
      apiFetch(`${API}/daily/${encodeURIComponent(account)}`),
      apiFetch(`${API}/summary/${encodeURIComponent(account)}`),
    ]);
    buildYears();
    renderAll();
  } catch (err) {
    console.error(err); showNotice("Lỗi đọc dữ liệu: " + err.message);
  } finally { $("reloadBtn").disabled = false; }
}

function buildYears() {
  const years = [...new Set((monthly.SanLuong || []).map(r => Number(r["Năm"])).filter(Boolean))].sort((a,b)=>b-a);
  const current = $("yearSelect").value;
  $("yearSelect").innerHTML = `<option value="all">Tất cả năm</option>` + years.map(y => `<option value="${y}">${y}</option>`).join("");
  if (["all", ...years.map(String)].includes(current)) $("yearSelect").value = current;
}
function renderAll() { renderHeader(); renderMetrics(); renderQuality(); renderMonthly(); renderDaily(); renderOutages(); renderFilesAndErrors(); }
function renderHeader() {
  const c = summary.customer || {};
  $("syncStatus").textContent = summary.last_sync ? `Đồng bộ EVN: ${new Date(summary.last_sync).toLocaleString("vi-VN")}` : "Đang chờ lần đồng bộ EVN đầu tiên";
  $("customerMeta").textContent = [c.id, c.name, c.region, c.management_unit].filter(Boolean).join(" • ");
}
function renderMetrics() {
  const d = summary.daily || {}, m = summary.monthly || {};
  $("totalKwh").textContent = fmt(d.total_kwh,3);
  $("officialCost").textContent = money(m.official_cost_total);
  $("avgDaily").textContent = fmt(d.average_kwh,3);
  $("coverage").textContent = `${fmt(d.coverage_percent,2)}%`;
  $("coverageDetail").textContent = `${d.valid_records || 0}/${d.expected_days || 0} ngày`;
  $("invoiceCount").textContent = fmt(m.official_invoice_count);
  $("rawCount").textContent = fmt(summary.raw_server_record_count);
  $("debt").textContent = money(summary.debt?.amount);
  $("fileCount").textContent = fmt((summary.invoice_files || []).length);
}
function renderQuality() {
  const d = summary.daily || {};
  const peak = d.peak, low = d.lowest;
  const rows = [
    ["Ngày đầu tiên", d.first_date || "—"], ["Ngày gần nhất", d.last_date || "—"],
    ["Bản ghi ngày", d.records ?? "—"], ["Bản ghi có sản lượng", d.valid_records ?? "—"],
    ["Ngày cao nhất", peak ? `${peak["date_display"]}: ${kwh(peak.consumption)}` : "—"],
    ["Ngày thấp nhất", low ? `${low["date_display"]}: ${kwh(low.consumption)}` : "—"],
    ["Thông báo EVN", summary.notification_count ?? 0], ["Lịch cắt điện đã lưu", summary.outage_count ?? 0],
  ];
  $("qualityDetails").innerHTML = rows.map(([a,b]) => `<dt>${escapeHtml(a)}</dt><dd>${escapeHtml(String(b))}</dd>`).join("");
}
function mergedMonthly() {
  const map = new Map();
  for (const row of monthly.SanLuong || []) { const key=`${row["Năm"]}-${row["Tháng"]}`; map.set(key,{year:Number(row["Năm"]),month:Number(row["Tháng"]),kwh:row["Điện tiêu thụ (KWh)"],source:row["Nguồn"]}); }
  for (const row of monthly.TienDien || []) { const key=`${row["Năm"]}-${row["Tháng"]}`; const item=map.get(key)||{year:Number(row["Năm"]),month:Number(row["Tháng"])}; Object.assign(item,{cost:row["Tiền Điện"],status:row["Trạng thái"],source:row["Nguồn"]||item.source}); map.set(key,item); }
  return [...map.values()].sort((a,b)=>a.year-b.year||a.month-b.month);
}
function renderMonthly() {
  const selected = $("yearSelect").value;
  const rows = mergedMonthly().filter(r => selected === "all" || String(r.year) === selected);
  const max = Math.max(1, ...rows.map(r => Number(r.kwh)||0));
  $("monthlyBars").innerHTML = rows.length ? rows.map(r => `<div class="bar-col"><span class="bar-value">${fmt(r.kwh,1)}</span><div class="bar" style="height:${Math.max(2,(Number(r.kwh)||0)/max*155)}px"></div><span class="bar-label">${String(r.month).padStart(2,"0")}/${r.year}</span></div>`).join("") : empty();
  $("monthlyTable").innerHTML = rows.slice().reverse().map(r => `<tr><td>${String(r.month).padStart(2,"0")}/${r.year}</td><td>${kwh(r.kwh)}</td><td>${money(r.cost)}</td><td>${escapeHtml(r.status || "—")}</td><td class="${r.source === "invoice" ? "source-official":""}">${escapeHtml(r.source || "—")}</td></tr>`).join("") || `<tr><td colspan="5">Chưa có dữ liệu.</td></tr>`;
}
function renderDaily() {
  const start = $("startDate").value, end = $("endDate").value;
  let rows = daily.filter(r => (!start || r["Ngày ISO"] >= start) && (!end || r["Ngày ISO"] <= end));
  rows = rows.slice().sort((a,b)=>(b["Ngày ISO"]||"").localeCompare(a["Ngày ISO"]||""));
  $("dailyTable").innerHTML = rows.map(r => `<tr><td>${escapeHtml(r["Ngày"] || r["Ngày ISO"] || "—")}</td><td>${fmt(r.CHISO,3)}</td><td>${kwh(r["Điện tiêu thụ (kWh)"])}</td></tr>`).join("") || `<tr><td colspan="3">Chưa có dữ liệu trong khoảng chọn.</td></tr>`;
}
function renderOutages() {
  const rows = summary.outages || [];
  $("outages").innerHTML = rows.length ? rows.map(r => `<div class="item"><strong>${escapeHtml(r.start_date || "")}: ${escapeHtml(r.start_time || "")} - ${escapeHtml(r.end_time || "")}</strong><div>${escapeHtml(r.area || "Không có khu vực")}</div><small class="muted">${escapeHtml(r.reason || "Không có lý do")}</small></div>`).join("") : empty();
}
function renderFilesAndErrors() {
  const files = summary.invoice_files || [];
  $("invoiceFiles").innerHTML = files.length ? files.map(f => `<div class="item"><strong>${escapeHtml(f.name)}</strong><span class="muted">${fmt(f.size/1024,1)} KB • ${f.type.toUpperCase()}</span></div>`).join("") : empty();
  const errors = summary.partial_errors || [];
  $("partialErrors").innerHTML = errors.length ? `<strong>Lỗi từng phần lần đồng bộ gần nhất:</strong>${errors.map(e=>`<div>• ${escapeHtml(e)}</div>`).join("")}` : "";
}
function empty(){ return `<div class="empty">Chưa có dữ liệu.</div>`; }
function escapeHtml(value){ return String(value ?? "").replace(/[&<>'"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[c])); }
boot();
