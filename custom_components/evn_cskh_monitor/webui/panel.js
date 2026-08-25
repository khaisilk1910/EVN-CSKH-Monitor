/* EVN CSKH Monitor authenticated Home Assistant custom panel. */

const AUTO_REFRESH_MS = 5 * 60 * 1000;
const MONTH_LABELS = ["T1", "T2", "T3", "T4", "T5", "T6", "T7", "T8", "T9", "T10", "T11", "T12"];

class EVNCSKHMonitorPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._hass = null;
    this._booted = false;
    this._loading = false;
    this._accounts = [];
    this._currentAccount = "";
    this._monthly = { SanLuong: [], TienDien: [] };
    this._daily = [];
    this._summary = {};
    this._refreshTimer = null;
  }

  set hass(value) {
    this._hass = value;
    if (!this._booted && this.isConnected && value) {
      this._booted = true;
      void this._boot();
    }
  }

  get hass() {
    return this._hass;
  }

  set panel(value) {
    this._panel = value;
  }

  set narrow(value) {
    this.toggleAttribute("narrow", Boolean(value));
  }

  connectedCallback() {
    this._renderShell();
    if (!this._booted && this._hass) {
      this._booted = true;
      void this._boot();
    }
    if (!this._refreshTimer) {
      this._refreshTimer = window.setInterval(() => {
        if (document.visibilityState === "visible") void this._reloadAll();
      }, AUTO_REFRESH_MS);
    }
  }

  disconnectedCallback() {
    if (this._refreshTimer) {
      window.clearInterval(this._refreshTimer);
      this._refreshTimer = null;
    }
  }

  _renderShell() {
    this.shadowRoot.innerHTML = `
      <style>${this._styles()}</style>
      <div class="app" id="app">
        <header class="topbar">
          <div class="topbar-inner">
            <div class="brand">
              <img src="/evncskh-monitor/icon.png" alt="EVN CSKH Monitor">
              <div class="brand-text">
                <h1 id="webuiTitle">EVN CSKH Monitor</h1>
                <p id="webuiSubtitle">Dữ liệu EVN, hóa đơn, sản lượng và lịch cắt điện</p>
              </div>
            </div>
            <div class="toolbar">
              <label class="control">
                <span>Công tơ</span>
                <select id="accountSelect" aria-label="Công tơ EVN"></select>
              </label>
              <label class="control">
                <span>Năm thống kê</span>
                <select id="yearSelect" aria-label="Năm thống kê"></select>
              </label>
              <button id="reloadBtn" type="button" class="reload-btn" aria-label="Làm mới màn hình">
                <span class="reload-icon">↻</span><span>Làm mới</span>
              </button>
            </div>
          </div>
        </header>

        <main>
          <section id="notice" class="notice hidden"></section>
          <section class="status-row">
            <span id="syncStatus" class="pill"><span class="status-dot"></span>Chưa đồng bộ</span>
            <span id="customerMeta" class="muted customer-meta"></span>
          </section>

          <section class="metric-grid">
            <article class="metric metric-energy">
              <div class="metric-icon">⚡</div><div><span>Tổng sản lượng</span><strong id="totalKwh">—</strong><small>kWh đã lưu</small></div>
            </article>
            <article class="metric metric-money">
              <div class="metric-icon">₫</div><div><span>Tiền hóa đơn EVN</span><strong id="officialCost">—</strong><small>tiền chính thức đã có</small></div>
            </article>
            <article class="metric metric-average">
              <div class="metric-icon">∅</div><div><span>Trung bình/ngày</span><strong id="avgDaily">—</strong><small>kWh/ngày có dữ liệu</small></div>
            </article>
            <article class="metric metric-debt">
              <div class="metric-icon">⌁</div><div><span>Tiền nợ</span><strong id="debt">—</strong><small>theo EVN</small></div>
            </article>
            <article class="metric metric-files">
              <div class="metric-icon">▣</div><div><span>File hóa đơn</span><strong id="fileCount">—</strong><small>PDF/PNG đã tải</small></div>
            </article>
          </section>

          <section class="panel chart-panel">
            <div class="panel-head">
              <div><h2>Sản lượng theo tháng</h2><p id="monthlyCaption">12 tháng của năm đã chọn.</p></div>
              <span class="year-badge" id="chartYearBadge">—</span>
            </div>
            <div class="chart-frame">
              <div id="monthlyBars" class="bars" role="img" aria-label="Biểu đồ sản lượng theo tháng"></div>
            </div>
          </section>

          <section class="panel">
            <div class="panel-head">
              <div><h2>Hóa đơn theo tháng</h2><p>Sản lượng và số tiền hóa đơn EVN theo năm.</p></div>
            </div>
            <div class="table-wrap compact-table">
              <table class="monthly-table">
                <thead><tr><th>Tháng</th><th>Sản lượng</th><th>Tiền điện</th></tr></thead>
                <tbody id="monthlyTable"></tbody>
              </table>
            </div>
          </section>

          <section class="panel">
            <div class="panel-head daily-head">
              <div><h2>Sản lượng hằng ngày</h2><p id="dailyCaption">Hiển thị từng ngày của tháng đang chọn.</p></div>
              <div class="period-controls">
                <label class="mini-control"><span>Tháng</span><select id="dailyMonthSelect" aria-label="Tháng dữ liệu ngày"></select></label>
                <label class="mini-control"><span>Năm</span><select id="dailyYearSelect" aria-label="Năm dữ liệu ngày"></select></label>
              </div>
            </div>
            <div class="table-wrap daily-table-wrap">
              <table class="daily-table">
                <thead><tr><th>Ngày</th><th>Chỉ số công tơ</th><th>Sản lượng</th></tr></thead>
                <tbody id="dailyTable"></tbody>
              </table>
            </div>
          </section>

          <section class="bottom-grid">
            <section class="panel bottom-panel">
              <div class="panel-head"><div><h2>Lịch cắt điện</h2><p>Lịch sắp tới từ dữ liệu EVN đã đồng bộ.</p></div></div>
              <div id="outages" class="list"></div>
            </section>
            <section class="panel bottom-panel">
              <div class="panel-head"><div><h2>File hóa đơn & đồng bộ</h2><p>PDF/PNG chính thức đã tải về /config/evncskh.</p></div></div>
              <div id="invoiceFiles" class="list"></div>
              <div id="partialErrors" class="errors"></div>
            </section>
          </section>
        </main>
      </div>`;

    this.$("accountSelect").addEventListener("change", (event) => {
      void this._loadAccount(event.target.value);
    });
    this.$("yearSelect").addEventListener("change", () => this._renderMonthly());
    this.$("dailyMonthSelect").addEventListener("change", () => this._renderDaily());
    this.$("dailyYearSelect").addEventListener("change", () => {
      this._rebuildDailyMonths();
      this._renderDaily();
    });
    this.$("reloadBtn").addEventListener("click", () => void this._reloadAll());
  }

  $(id) {
    return this.shadowRoot.getElementById(id);
  }

  async _apiGet(path) {
    if (!this._hass?.callApi) throw new Error("Home Assistant API chưa sẵn sàng");
    return this._hass.callApi("GET", path);
  }

  async _boot() {
    await this._reloadOptions();
    if (!this._accounts.length) {
      this._showNotice("Chưa có công tơ EVN CSKH Monitor được cấu hình.");
      return;
    }
    await this._loadAccount(this._currentAccount || this._accounts[0].customer_id);
  }

  async _reloadAll() {
    if (this._loading) return;
    const previous = this._currentAccount;
    await this._reloadOptions();
    const target = this._accounts.some((item) => item.customer_id === previous)
      ? previous
      : this._accounts[0]?.customer_id;
    if (target) await this._loadAccount(target);
  }

  async _reloadOptions() {
    try {
      const options = await this._apiGet("evncskh/options");
      this._accounts = options.accounts || [];
      this.$("accountSelect").innerHTML = this._accounts
        .map((account) => `<option value="${this._escape(account.customer_id)}">${this._escape(account.name || account.customer_id)}</option>`)
        .join("");
    } catch (error) {
      console.error(error);
      this._accounts = [];
      this._showNotice(`Không đọc được API EVN CSKH Monitor: ${error.message || error}`);
    }
  }

  _applyAccountHeader(accountId) {
    const account = this._accounts.find((item) => item.customer_id === accountId) || {};
    this.$("webuiTitle").textContent = account.webui_title || "EVN CSKH Monitor";
    this.$("webuiSubtitle").textContent = account.webui_subtitle || "";
    this.$("accountSelect").value = accountId;
  }

  async _loadAccount(account) {
    if (!account || this._loading) return;
    this._loading = true;
    this._currentAccount = account;
    this._applyAccountHeader(account);
    this._showNotice("");
    this.$("app").classList.add("loading");
    this.$("reloadBtn").disabled = true;
    try {
      const encoded = encodeURIComponent(account);
      [this._monthly, this._daily, this._summary] = await Promise.all([
        this._apiGet(`evncskh/monthly/${encoded}`),
        this._apiGet(`evncskh/daily/${encoded}`),
        this._apiGet(`evncskh/summary/${encoded}`),
      ]);
      const customer = this._summary.customer || {};
      if (customer.webui_title) this.$("webuiTitle").textContent = customer.webui_title;
      if (customer.webui_subtitle !== undefined) this.$("webuiSubtitle").textContent = customer.webui_subtitle || "";
      this._buildMonthlyYears();
      this._buildDailyFilters();
      this._renderAll();
    } catch (error) {
      console.error(error);
      this._showNotice(`Lỗi đọc dữ liệu: ${error.message || error}`);
    } finally {
      this._loading = false;
      this.$("app").classList.remove("loading");
      this.$("reloadBtn").disabled = false;
    }
  }

  _showNotice(text) {
    const node = this.$("notice");
    if (!node) return;
    node.textContent = text;
    node.classList.toggle("hidden", !text);
  }

  _mergedMonthly() {
    const map = new Map();
    for (const row of this._monthly.SanLuong || []) {
      const year = Number(row["Năm"]);
      const month = Number(row["Tháng"]);
      if (!year || !month) continue;
      map.set(`${year}-${month}`, { year, month, kwh: row["Điện tiêu thụ (KWh)"] });
    }
    for (const row of this._monthly.TienDien || []) {
      const year = Number(row["Năm"]);
      const month = Number(row["Tháng"]);
      if (!year || !month) continue;
      const key = `${year}-${month}`;
      const item = map.get(key) || { year, month };
      item.cost = row["Tiền Điện"];
      map.set(key, item);
    }
    return [...map.values()].sort((a, b) => a.year - b.year || a.month - b.month);
  }

  _buildMonthlyYears() {
    const currentYear = new Date().getFullYear();
    const yearSet = new Set(this._mergedMonthly().map((row) => row.year).filter(Boolean));
    yearSet.add(currentYear);
    const years = [...yearSet].sort((a, b) => b - a);
    const select = this.$("yearSelect");
    const previous = Number(select.value);
    select.innerHTML = years.map((year) => `<option value="${year}">${year}</option>`).join("");
    const preferred = years.includes(previous) ? previous : currentYear;
    select.value = String(preferred);
  }

  _buildDailyFilters() {
    const valid = (this._daily || [])
      .map((row) => this._parseIso(row["Ngày ISO"]))
      .filter(Boolean);
    const now = new Date();
    const yearSet = new Set(valid.map((date) => date.getFullYear()));
    yearSet.add(now.getFullYear());
    const years = [...yearSet].sort((a, b) => b - a);
    const yearSelect = this.$("dailyYearSelect");
    const previousYear = Number(yearSelect.value);
    yearSelect.innerHTML = years.map((year) => `<option value="${year}">${year}</option>`).join("");
    const preferredYear = years.includes(previousYear) ? previousYear : now.getFullYear();
    yearSelect.value = String(preferredYear);
    this._rebuildDailyMonths(true);
  }

  _rebuildDailyMonths(initial = false) {
    const year = Number(this.$("dailyYearSelect").value);
    const monthSelect = this.$("dailyMonthSelect");
    const previousMonth = Number(monthSelect.value);
    const available = new Set(
      (this._daily || [])
        .map((row) => this._parseIso(row["Ngày ISO"]))
        .filter((date) => date && date.getFullYear() === year)
        .map((date) => date.getMonth() + 1)
    );
    const months = Array.from({ length: 12 }, (_, index) => index + 1);
    monthSelect.innerHTML = months.map((month) =>
      `<option value="${month}">Tháng ${month}${available.has(month) ? "" : " · chưa có dữ liệu"}</option>`
    ).join("");
    const now = new Date();
    let preferred = previousMonth;
    if (initial || !(preferred >= 1 && preferred <= 12)) {
      preferred = year === now.getFullYear()
        ? now.getMonth() + 1
        : ([...available].sort((a, b) => b - a)[0] || 12);
    }
    monthSelect.value = String(preferred);
  }

  _renderAll() {
    this._renderHeader();
    this._renderMetrics();
    this._renderMonthly();
    this._renderDaily();
    this._renderOutages();
    this._renderFilesAndErrors();
  }

  _renderHeader() {
    const customer = this._summary.customer || {};
    this.$("syncStatus").innerHTML = this._summary.last_sync
      ? `<span class="status-dot"></span>Đồng bộ ${this._escape(new Date(this._summary.last_sync).toLocaleString("vi-VN"))}`
      : `<span class="status-dot pending"></span>Đang chờ đồng bộ EVN`;
    this.$("customerMeta").textContent = [
      customer.device_name,
      customer.id && customer.id !== customer.device_name ? customer.id : null,
      customer.region,
    ].filter(Boolean).join(" • ");
  }

  _renderMetrics() {
    const daily = this._summary.daily || {};
    const monthly = this._summary.monthly || {};
    this.$("totalKwh").textContent = this._kwhNumber(daily.total_kwh);
    this.$("officialCost").textContent = this._money(monthly.official_cost_total);
    this.$("avgDaily").textContent = this._kwhNumber(daily.average_kwh);
    this.$("debt").textContent = this._money(this._summary.debt?.amount);
    this.$("fileCount").textContent = this._fmt((this._summary.invoice_files || []).length);
  }

  _renderMonthly() {
    const year = Number(this.$("yearSelect").value);
    this.$("chartYearBadge").textContent = year || "—";
    this.$("monthlyCaption").textContent = year ? `Sản lượng điện 12 tháng năm ${year}.` : "Chưa có dữ liệu theo năm.";
    const sourceRows = this._mergedMonthly().filter((row) => row.year === year);
    const byMonth = new Map(sourceRows.map((row) => [row.month, row]));
    const values = Array.from({ length: 12 }, (_, index) => Number(byMonth.get(index + 1)?.kwh) || 0);
    const max = Math.max(1, ...values);

    this.$("monthlyBars").innerHTML = Array.from({ length: 12 }, (_, index) => {
      const month = index + 1;
      const row = byMonth.get(month);
      const value = Number(row?.kwh);
      const hasValue = Number.isFinite(value) && row?.kwh != null;
      const pct = hasValue ? Math.max(3, value / max * 100) : 1.5;
      return `
        <div class="bar-col ${hasValue ? "" : "missing"}" title="${this._escape(`Tháng ${month}/${year}: ${hasValue ? this._kwh(value) : "Chưa có dữ liệu"}`)}">
          <span class="bar-value">${hasValue ? this._fmt(value, 1) : ""}</span>
          <div class="bar-track"><div class="bar" style="--bar-height:${pct}%"></div></div>
          <span class="bar-label">${MONTH_LABELS[index]}</span>
        </div>`;
    }).join("");

    const tableRows = sourceRows.slice().sort((a, b) => b.month - a.month);
    this.$("monthlyTable").innerHTML = tableRows.length
      ? tableRows.map((row) => `
          <tr>
            <td><strong>Tháng ${row.month}</strong><small>${row.year}</small></td>
            <td>${this._kwh(row.kwh)}</td>
            <td>${this._money(row.cost)}</td>
          </tr>`).join("")
      : `<tr><td colspan="3" class="empty-cell">Chưa có dữ liệu năm ${year || "đã chọn"}.</td></tr>`;
  }

  _renderDaily() {
    const year = Number(this.$("dailyYearSelect").value);
    const month = Number(this.$("dailyMonthSelect").value);
    this.$("dailyCaption").textContent = year && month
      ? `Các ngày có dữ liệu trong tháng ${month}/${year}.`
      : "Chưa có dữ liệu ngày để chọn.";
    const rows = (this._daily || [])
      .filter((row) => {
        const date = this._parseIso(row["Ngày ISO"]);
        return date && date.getFullYear() === year && date.getMonth() + 1 === month;
      })
      .sort((a, b) => (a["Ngày ISO"] || "").localeCompare(b["Ngày ISO"] || ""));

    this.$("dailyTable").innerHTML = rows.length
      ? rows.map((row) => `
          <tr>
            <td><strong>${this._escape(this._dayLabel(row["Ngày ISO"], row["Ngày"]))}</strong></td>
            <td>${this._fmt(row.CHISO, 3)}</td>
            <td>${this._kwh(row["Điện tiêu thụ (kWh)"])}</td>
          </tr>`).join("")
      : `<tr><td colspan="3" class="empty-cell">Chưa có dữ liệu trong tháng đã chọn.</td></tr>`;
  }

  _renderOutages() {
    const rows = this._summary.outages || [];
    this.$("outages").innerHTML = rows.length
      ? rows.map((row) => `
          <article class="list-item outage-item">
            <div class="item-icon">⚠</div>
            <div><strong>${this._escape(row.start_date || "")} ${this._escape(row.start_time || "")}${row.end_time ? ` – ${this._escape(row.end_time)}` : ""}</strong>
            <p>${this._escape(row.area || "Khu vực chưa xác định")}</p>
            <small>${this._escape(row.reason || "EVN chưa cung cấp lý do")}</small></div>
          </article>`).join("")
      : `<div class="empty">Không có lịch cắt điện sắp tới.</div>`;
  }

  _renderFilesAndErrors() {
    const files = this._summary.invoice_files || [];
    this.$("invoiceFiles").innerHTML = files.length
      ? files.slice(0, 12).map((file) => `
          <article class="list-item file-item">
            <div class="item-icon">${String(file.type).toLowerCase() === "pdf" ? "PDF" : "IMG"}</div>
            <div><strong>${this._escape(file.name)}</strong><small>${this._fmt(file.size / 1024, 1)} KB • ${this._escape(String(file.type || "").toUpperCase())}</small></div>
          </article>`).join("")
      : `<div class="empty">Chưa tải được file hóa đơn PDF/PNG.</div>`;
    const errors = this._summary.partial_errors || [];
    this.$("partialErrors").innerHTML = errors.length
      ? `<details><summary>Chi tiết lỗi đồng bộ gần nhất (${errors.length})</summary>${errors.map((error) => `<div>• ${this._escape(error)}</div>`).join("")}</details>`
      : "";
  }

  _parseIso(value) {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(String(value || ""))) return null;
    const [year, month, day] = String(value).split("-").map(Number);
    const date = new Date(year, month - 1, day);
    return Number.isNaN(date.getTime()) ? null : date;
  }

  _dayLabel(iso, fallback) {
    const date = this._parseIso(iso);
    return date ? String(date.getDate()).padStart(2, "0") : (fallback || iso || "—");
  }

  _fmt(value, digits = 0) {
    if (value == null || value === "" || Number.isNaN(Number(value))) return "—";
    return Number(value).toLocaleString("vi-VN", { maximumFractionDigits: digits });
  }

  _money(value) {
    return value == null ? "—" : `${this._fmt(value)} ₫`;
  }

  _kwh(value) {
    return value == null ? "—" : `${this._fmt(value, 3)} kWh`;
  }

  _kwhNumber(value) {
    return value == null ? "—" : `${this._fmt(value, 2)} kWh`;
  }

  _escape(value) {
    return String(value ?? "").replace(/[&<>'"]/g, (char) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
    }[char]));
  }

  _styles() {
    return `
      :host{display:block;width:100%;max-width:100vw;min-height:100vh;overflow-x:hidden;color-scheme:dark;--bg:#07111f;--surface:#0e1b2e;--surface2:#13233b;--surface3:#192c49;--line:rgba(148,173,208,.18);--text:#f5f8ff;--muted:#9fb0c8;--blue:#4c8dff;--blue2:#2457e6;--cyan:#43d7ff;--green:#39d98a;--amber:#ffbf47;--red:#ff6b7a;--shadow:0 18px 50px rgba(0,0,0,.28);font:clamp(13px,1.2vw,15px)/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;color:var(--text);background:var(--bg)}
      *{box-sizing:border-box;min-width:0}.app{min-height:100vh;width:100%;overflow:hidden;background:radial-gradient(900px 460px at 10% -8%,rgba(55,115,255,.23),transparent 60%),radial-gradient(760px 460px at 100% 18%,rgba(49,208,255,.10),transparent 64%),var(--bg)}
      .topbar{position:sticky;top:0;z-index:20;border-bottom:1px solid var(--line);background:rgba(7,17,31,.88);backdrop-filter:blur(20px) saturate(140%)}.topbar-inner{width:min(1500px,100%);margin:auto;padding:clamp(12px,2vw,20px);display:flex;align-items:center;justify-content:space-between;gap:18px}
      .brand{display:flex;align-items:center;gap:12px;min-width:0}.brand img{width:clamp(48px,6vw,62px);height:clamp(48px,6vw,62px);border-radius:18px;box-shadow:0 10px 28px rgba(22,83,190,.3);flex:0 0 auto}.brand-text{min-width:0}.brand h1{margin:0;font-size:clamp(20px,2.2vw,30px);line-height:1.15;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;letter-spacing:-.02em}.brand p{margin:5px 0 0;color:var(--muted);font-size:clamp(11px,1vw,14px);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
      .toolbar{display:grid;grid-template-columns:minmax(150px,1fr) minmax(112px,.65fr) auto;gap:8px;align-items:end}.control,.mini-control{display:grid;gap:4px}.control>span,.mini-control>span{font-size:11px;color:var(--muted);padding-left:4px}.control select,.mini-control select,button{width:100%;height:42px;border:1px solid var(--line);border-radius:12px;background:var(--surface2);color:var(--text);padding:0 12px;font:inherit;outline:none}.control select:focus,.mini-control select:focus,button:focus-visible{border-color:var(--blue);box-shadow:0 0 0 3px rgba(76,141,255,.16)}button{cursor:pointer}.reload-btn{display:flex;align-items:center;justify-content:center;gap:7px;background:linear-gradient(135deg,var(--blue),var(--blue2));border-color:transparent;font-weight:700;box-shadow:0 10px 24px rgba(36,87,230,.22)}.reload-btn:hover{filter:brightness(1.08);transform:translateY(-1px)}.reload-btn:disabled{opacity:.55;cursor:wait;transform:none}.reload-icon{font-size:20px;line-height:1}
      main{width:min(1500px,100%);margin:auto;padding:clamp(10px,2vw,22px)}.notice{padding:11px 13px;border:1px solid rgba(255,107,122,.55);background:rgba(255,107,122,.09);border-radius:12px;margin-bottom:12px;color:#ffd3d8}.hidden{display:none}.status-row{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:12px}.pill{display:inline-flex;align-items:center;gap:7px;padding:6px 10px;border:1px solid var(--line);border-radius:999px;background:rgba(14,27,46,.8);font-size:12px}.status-dot{width:7px;height:7px;border-radius:50%;background:var(--green);box-shadow:0 0 12px rgba(57,217,138,.7)}.status-dot.pending{background:var(--amber);box-shadow:0 0 12px rgba(255,191,71,.7)}.muted{color:var(--muted)}.customer-meta{font-size:12px;overflow-wrap:anywhere}
      .metric-grid{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:10px}.metric,.panel{border:1px solid var(--line);background:linear-gradient(145deg,rgba(23,40,66,.94),rgba(12,25,43,.94));box-shadow:var(--shadow)}.metric{position:relative;overflow:hidden;border-radius:16px;padding:14px;display:flex;align-items:center;gap:12px;min-height:105px;transition:transform .2s ease,border-color .2s ease}.metric:after{content:"";position:absolute;width:110px;height:110px;border-radius:50%;right:-45px;top:-55px;background:radial-gradient(circle,rgba(76,141,255,.18),transparent 67%)}.metric:hover{transform:translateY(-2px);border-color:rgba(110,158,231,.38)}.metric-icon{width:40px;height:40px;border-radius:13px;display:grid;place-items:center;flex:0 0 auto;background:rgba(76,141,255,.12);color:#a9c8ff;font-weight:800}.metric span{display:block;color:var(--muted);font-size:12px}.metric strong{display:block;margin:2px 0 1px;font-size:clamp(18px,2vw,26px);line-height:1.2;letter-spacing:-.02em;white-space:normal;overflow-wrap:anywhere}.metric small{color:var(--muted);font-size:11px}.metric-money .metric-icon{color:#8df0c4;background:rgba(57,217,138,.1)}.metric-debt .metric-icon{color:#ffd88b;background:rgba(255,191,71,.1)}.metric-files .metric-icon{color:#aebcff;background:rgba(134,126,255,.1)}
      .panel{border-radius:18px;margin-top:12px;padding:clamp(12px,2vw,18px)}.panel-head{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;margin-bottom:12px}.panel-head h2{margin:0;font-size:clamp(17px,1.6vw,22px);letter-spacing:-.015em}.panel-head p{margin:3px 0 0;color:var(--muted);font-size:12px}.year-badge{display:inline-grid;place-items:center;min-width:60px;padding:6px 10px;border-radius:999px;background:rgba(76,141,255,.12);border:1px solid rgba(76,141,255,.25);color:#b9d0ff;font-weight:750}
      .chart-frame{height:clamp(220px,32vw,360px);padding:8px 2px 0}.bars{height:100%;display:grid;grid-template-columns:repeat(12,minmax(0,1fr));gap:clamp(3px,1vw,12px);align-items:stretch}.bar-col{height:100%;display:grid;grid-template-rows:22px 1fr 22px;gap:4px;align-items:end;text-align:center}.bar-value{font-size:clamp(8px,1vw,12px);color:#b8c7dc;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.bar-track{position:relative;height:100%;min-height:130px;display:flex;align-items:flex-end;justify-content:center;border-bottom:1px solid var(--line)}.bar-track:before{content:"";position:absolute;inset:0 0 auto;height:1px;background:linear-gradient(90deg,transparent,var(--line),transparent);opacity:.45}.bar{height:var(--bar-height);width:min(68%,46px);min-width:5px;border-radius:9px 9px 3px 3px;background:linear-gradient(180deg,#65b0ff 0%,#4388ff 38%,#2457e6 100%);box-shadow:0 8px 20px rgba(37,87,230,.28),inset 0 1px 0 rgba(255,255,255,.24);animation:growbar .65s cubic-bezier(.2,.8,.2,1) both;transform-origin:bottom}.bar-col.missing .bar{background:rgba(116,141,176,.12);box-shadow:none}.bar-label{font-size:clamp(9px,1vw,12px);color:var(--muted);align-self:start}.bar-col:hover .bar{filter:brightness(1.12)}
      .table-wrap{width:100%;overflow:hidden;border:1px solid var(--line);border-radius:13px;background:rgba(5,14,27,.18)}table{width:100%;border-collapse:collapse;table-layout:fixed}th,td{padding:10px 12px;border-bottom:1px solid var(--line);text-align:right;vertical-align:middle;overflow-wrap:anywhere}th{font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:#b8c7dc;background:rgba(21,39,64,.86)}th:first-child,td:first-child{text-align:left}tbody tr:last-child td{border-bottom:0}tbody tr:hover{background:rgba(76,141,255,.05)}td small{display:block;color:var(--muted);font-size:10px}.monthly-table th:nth-child(1){width:24%}.monthly-table th:nth-child(2){width:34%}.monthly-table th:nth-child(3){width:42%}.daily-table th:nth-child(1){width:23%}.daily-table th:nth-child(2){width:39%}.daily-table th:nth-child(3){width:38%}.empty-cell{text-align:center!important;color:var(--muted);padding:22px}
      .daily-head{align-items:end}.period-controls{display:grid;grid-template-columns:repeat(2,minmax(100px,145px));gap:8px}.daily-table-wrap{max-height:min(62vh,720px);overflow:auto}.daily-table thead th{position:sticky;top:0;z-index:2}.daily-table td:first-child strong{display:inline-grid;place-items:center;min-width:34px;height:30px;border-radius:9px;background:rgba(76,141,255,.1);color:#c6d9ff}
      .bottom-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.bottom-panel{margin-top:12px}.list{display:grid;gap:8px}.list-item{display:flex;align-items:flex-start;gap:10px;padding:11px;border:1px solid var(--line);border-radius:12px;background:rgba(3,11,23,.18)}.list-item strong{display:block;font-size:13px}.list-item p{margin:3px 0;color:#cbd7e8}.list-item small{display:block;color:var(--muted);font-size:11px}.item-icon{width:37px;height:37px;border-radius:11px;display:grid;place-items:center;flex:0 0 auto;background:rgba(76,141,255,.1);color:#bcd3ff;font-size:10px;font-weight:800}.outage-item .item-icon{background:rgba(255,191,71,.1);color:#ffd27b}.empty{color:var(--muted);padding:14px;border:1px dashed var(--line);border-radius:12px;text-align:center}.errors{margin-top:9px;color:#ffc3ca;font-size:12px}.errors details{padding:9px 10px;border:1px solid rgba(255,107,122,.2);border-radius:10px;background:rgba(255,107,122,.05)}.errors summary{cursor:pointer;font-weight:650}
      .loading .reload-icon{animation:spin .8s linear infinite}.loading .panel,.loading .metric{transition:opacity .2s ease;opacity:.82}
      @keyframes growbar{from{transform:scaleY(.05);opacity:.35}to{transform:scaleY(1);opacity:1}}@keyframes spin{to{transform:rotate(360deg)}}
      @media(max-width:1100px){.topbar-inner{align-items:flex-start;flex-direction:column}.toolbar{width:100%;grid-template-columns:1.3fr .8fr auto}.metric-grid{grid-template-columns:repeat(3,minmax(0,1fr))}}
      @media(max-width:760px){.brand p{white-space:normal}.metric-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.metric{min-height:100px}.bottom-grid{grid-template-columns:1fr}.daily-head{align-items:flex-start;flex-direction:column}.period-controls{width:100%;grid-template-columns:repeat(2,minmax(0,1fr))}.panel-head p{max-width:68ch}}
      @media(max-width:520px){.topbar-inner{padding:12px 10px;gap:12px}.brand{gap:10px}.brand img{width:50px;height:50px;border-radius:15px}.brand h1{font-size:21px}.brand p{font-size:11px;line-height:1.35}.toolbar{grid-template-columns:1fr 1fr}.reload-btn{grid-column:1/-1;height:40px}.control select{height:40px;padding:0 9px}.control>span{font-size:10px}main{padding:10px}.status-row{gap:7px}.customer-meta{width:100%}.metric-grid{gap:8px}.metric{padding:11px;gap:8px;min-height:94px}.metric-icon{width:34px;height:34px;border-radius:10px}.metric strong{font-size:17px}.metric span{font-size:11px}.metric small{font-size:10px}.panel{padding:11px;border-radius:15px}.chart-frame{height:250px}.bars{gap:3px}.bar-col{grid-template-rows:18px 1fr 18px}.bar-value{font-size:8px}.bar-label{font-size:9px}.bar{width:72%;min-width:4px;border-radius:6px 6px 2px 2px}th,td{padding:9px 6px;font-size:11px}.monthly-table th:nth-child(1){width:25%}.monthly-table th:nth-child(2){width:34%}.monthly-table th:nth-child(3){width:41%}.daily-table th:nth-child(1){width:18%}.daily-table th:nth-child(2){width:42%}.daily-table th:nth-child(3){width:40%}.daily-table td:first-child strong{min-width:28px;height:26px}.year-badge{min-width:52px;padding:5px 8px}.panel-head h2{font-size:17px}.panel-head p{font-size:11px}.period-controls{gap:6px}.mini-control select{height:38px;padding:0 8px}.list-item{padding:9px}}
      @media(max-width:360px){.brand h1{font-size:19px}.metric-grid{grid-template-columns:1fr}.metric{min-height:82px}.chart-frame{height:225px}.bar-value{display:none}th,td{font-size:10.5px;padding:8px 4px}}
      @media(prefers-reduced-motion:reduce){*,*:before,*:after{animation:none!important;transition:none!important;scroll-behavior:auto!important}}
    `;
  }
}

if (!customElements.get("evn-cskh-monitor-panel")) {
  customElements.define("evn-cskh-monitor-panel", EVNCSKHMonitorPanel);
}
