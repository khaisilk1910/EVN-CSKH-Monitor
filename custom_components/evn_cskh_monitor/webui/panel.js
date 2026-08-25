/* EVN CSKH Monitor authenticated Home Assistant custom panel. */

const AUTO_REFRESH_MS = 5 * 60 * 1000;

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
      // Refreshes only the authenticated local integration API. It does not
      // force extra EVN cloud polling, so an open dashboard remains current
      // without increasing load on EVN servers.
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
      <div class="app">
        <header class="topbar">
          <div class="brand">
            <img src="/evncskh-monitor/icon.png" alt="EVN CSKH Monitor">
            <div class="brand-text">
              <h1 id="webuiTitle">EVN CSKH Monitor</h1>
              <p id="webuiSubtitle">Dữ liệu EVN, hóa đơn, sản lượng và lịch cắt điện</p>
            </div>
          </div>
          <div class="toolbar">
            <select id="accountSelect" aria-label="Tài khoản EVN"></select>
            <select id="yearSelect" aria-label="Năm"><option value="all">Tất cả năm</option></select>
            <button id="reloadBtn" type="button">Làm mới màn hình</button>
          </div>
        </header>

        <main>
          <section id="notice" class="notice hidden"></section>
          <section class="status-row">
            <span id="syncStatus" class="pill">Chưa đồng bộ</span>
            <span id="customerMeta" class="muted"></span>
          </section>

          <section class="metric-grid">
            <article class="metric"><span>Tổng sản lượng</span><strong id="totalKwh">—</strong><small>kWh trong dữ liệu đã lưu</small></article>
            <article class="metric"><span>Tiền hóa đơn EVN</span><strong id="officialCost">—</strong><small>chỉ số tiền chính thức từ hóa đơn</small></article>
            <article class="metric"><span>Trung bình/ngày</span><strong id="avgDaily">—</strong><small>kWh/ngày có dữ liệu</small></article>
            <article class="metric"><span>Độ phủ dữ liệu</span><strong id="coverage">—</strong><small id="coverageDetail">—</small></article>
            <article class="metric"><span>Hóa đơn chính thức</span><strong id="invoiceCount">—</strong><small>tháng có số tiền EVN</small></article>
            <article class="metric"><span>Dữ liệu server gốc</span><strong id="rawCount">—</strong><small>response khác nhau đã lưu nguyên bản</small></article>
            <article class="metric"><span>Tiền nợ</span><strong id="debt">—</strong><small>theo trạng thái hóa đơn EVN</small></article>
            <article class="metric"><span>File hóa đơn</span><strong id="fileCount">—</strong><small>PNG/PDF thực trong /config/evncskh</small></article>
          </section>

          <section class="panel two-col">
            <div>
              <div class="panel-head"><div><h2>Sản lượng theo tháng</h2><p>Không trộn tiền ước tính với hóa đơn EVN.</p></div></div>
              <div id="monthlyBars" class="bars"></div>
            </div>
            <div>
              <div class="panel-head"><div><h2>Chất lượng dữ liệu</h2><p>Giúp phát hiện ngày thiếu hoặc đồng bộ chưa hoàn chỉnh.</p></div></div>
              <dl class="details" id="qualityDetails"></dl>
            </div>
          </section>

          <section class="panel">
            <div class="panel-head"><div><h2>Hóa đơn theo tháng</h2><p>Sản lượng, tiền, trạng thái và nguồn dữ liệu.</p></div></div>
            <div class="table-wrap"><table><thead><tr><th>Tháng</th><th>Sản lượng</th><th>Tiền điện</th><th>Trạng thái</th><th>Nguồn</th></tr></thead><tbody id="monthlyTable"></tbody></table></div>
          </section>

          <section class="panel">
            <div class="panel-head daily-head">
              <div><h2>Sản lượng hằng ngày</h2><p>Tiền điện theo ngày để trống vì biểu giá bậc thang không cho phép phân bổ chính xác từng ngày.</p></div>
              <div class="range"><input id="startDate" type="date"><input id="endDate" type="date"><button id="applyRange" type="button">Lọc</button><button id="clearRange" type="button" class="secondary">Xóa lọc</button></div>
            </div>
            <div class="table-wrap tall"><table><thead><tr><th>Ngày</th><th>Chỉ số công tơ</th><th>Sản lượng</th></tr></thead><tbody id="dailyTable"></tbody></table></div>
          </section>

          <section class="panel two-col">
            <div><div class="panel-head"><div><h2>Lịch cắt điện</h2><p>Lịch tương lai lấy từ dữ liệu EVN đã đồng bộ.</p></div></div><div id="outages" class="list"></div></div>
            <div><div class="panel-head"><div><h2>File & trạng thái đồng bộ</h2><p>File hóa đơn và lỗi từng phần gần nhất.</p></div></div><div id="invoiceFiles" class="list"></div><div id="partialErrors" class="errors"></div></div>
          </section>
        </main>
      </div>`;

    this.$("accountSelect").addEventListener("change", (event) => {
      void this._loadAccount(event.target.value);
    });
    this.$("yearSelect").addEventListener("change", () => this._renderMonthly());
    this.$("reloadBtn").addEventListener("click", () => void this._reloadAll());
    this.$("applyRange").addEventListener("click", () => this._renderDaily());
    this.$("clearRange").addEventListener("click", () => {
      this.$("startDate").value = "";
      this.$("endDate").value = "";
      this._renderDaily();
    });
  }

  $(id) {
    return this.shadowRoot.getElementById(id);
  }

  async _apiGet(path) {
    if (!this._hass?.callApi) {
      throw new Error("Home Assistant API chưa sẵn sàng");
    }
    return this._hass.callApi("GET", path);
  }

  async _boot() {
    await this._reloadOptions();
    if (!this._accounts.length) {
      this._showNotice("Chưa có tài khoản EVN CSKH Monitor được cấu hình.");
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
      const select = this.$("accountSelect");
      select.innerHTML = this._accounts
        .map((account) => `<option value="${this._escape(account.customer_id)}">${this._escape(account.name || account.customer_id)}</option>`)
        .join("");
    } catch (error) {
      console.error(error);
      this._showNotice(`Không đọc được API EVN CSKH Monitor: ${error.message || error}`);
      this._accounts = [];
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
    this.$("reloadBtn").disabled = true;
    try {
      const encoded = encodeURIComponent(account);
      [this._monthly, this._daily, this._summary] = await Promise.all([
        this._apiGet(`evncskh/monthly/${encoded}`),
        this._apiGet(`evncskh/daily/${encoded}`),
        this._apiGet(`evncskh/summary/${encoded}`),
      ]);
      const summaryCustomer = this._summary.customer || {};
      if (summaryCustomer.webui_title) this.$("webuiTitle").textContent = summaryCustomer.webui_title;
      if (summaryCustomer.webui_subtitle !== undefined) this.$("webuiSubtitle").textContent = summaryCustomer.webui_subtitle || "";
      this._buildYears();
      this._renderAll();
    } catch (error) {
      console.error(error);
      this._showNotice(`Lỗi đọc dữ liệu: ${error.message || error}`);
    } finally {
      this._loading = false;
      this.$("reloadBtn").disabled = false;
    }
  }

  _showNotice(text) {
    const node = this.$("notice");
    if (!node) return;
    node.textContent = text;
    node.classList.toggle("hidden", !text);
  }

  _buildYears() {
    const years = [...new Set((this._monthly.SanLuong || [])
      .map((row) => Number(row["Năm"]))
      .filter(Boolean))].sort((a, b) => b - a);
    const select = this.$("yearSelect");
    const current = select.value;
    select.innerHTML = `<option value="all">Tất cả năm</option>${years.map((year) => `<option value="${year}">${year}</option>`).join("")}`;
    if (["all", ...years.map(String)].includes(current)) select.value = current;
  }

  _renderAll() {
    this._renderHeader();
    this._renderMetrics();
    this._renderQuality();
    this._renderMonthly();
    this._renderDaily();
    this._renderOutages();
    this._renderFilesAndErrors();
  }

  _renderHeader() {
    const customer = this._summary.customer || {};
    this.$("syncStatus").textContent = this._summary.last_sync
      ? `Đồng bộ EVN: ${new Date(this._summary.last_sync).toLocaleString("vi-VN")}`
      : "Đang chờ lần đồng bộ EVN đầu tiên";
    this.$("customerMeta").textContent = [
      customer.device_name,
      customer.id,
      customer.name && customer.name !== customer.device_name ? customer.name : null,
      customer.region,
      customer.management_unit,
    ].filter(Boolean).join(" • ");
  }

  _renderMetrics() {
    const daily = this._summary.daily || {};
    const monthly = this._summary.monthly || {};
    this.$("totalKwh").textContent = this._fmt(daily.total_kwh, 3);
    this.$("officialCost").textContent = this._money(monthly.official_cost_total);
    this.$("avgDaily").textContent = this._fmt(daily.average_kwh, 3);
    this.$("coverage").textContent = daily.coverage_percent == null ? "—" : `${this._fmt(daily.coverage_percent, 2)}%`;
    this.$("coverageDetail").textContent = `${daily.valid_records || 0}/${daily.expected_days || 0} ngày`;
    this.$("invoiceCount").textContent = this._fmt(monthly.official_invoice_count);
    this.$("rawCount").textContent = this._fmt(this._summary.raw_server_record_count);
    this.$("debt").textContent = this._money(this._summary.debt?.amount);
    this.$("fileCount").textContent = this._fmt((this._summary.invoice_files || []).length);
  }

  _renderQuality() {
    const daily = this._summary.daily || {};
    const peak = daily.peak;
    const low = daily.lowest;
    const rows = [
      ["Ngày đầu tiên", daily.first_date || "—"],
      ["Ngày gần nhất", daily.last_date || "—"],
      ["Bản ghi ngày", daily.records ?? "—"],
      ["Bản ghi có sản lượng", daily.valid_records ?? "—"],
      ["Ngày cao nhất", peak ? `${peak.date_display}: ${this._kwh(peak.consumption)}` : "—"],
      ["Ngày thấp nhất", low ? `${low.date_display}: ${this._kwh(low.consumption)}` : "—"],
      ["Thông báo EVN", this._summary.notification_count ?? 0],
      ["Lịch cắt điện đã lưu", this._summary.outage_count ?? 0],
    ];
    this.$("qualityDetails").innerHTML = rows
      .map(([label, value]) => `<dt>${this._escape(label)}</dt><dd>${this._escape(String(value))}</dd>`)
      .join("");
  }

  _mergedMonthly() {
    const map = new Map();
    for (const row of this._monthly.SanLuong || []) {
      const key = `${row["Năm"]}-${row["Tháng"]}`;
      map.set(key, {
        year: Number(row["Năm"]),
        month: Number(row["Tháng"]),
        kwh: row["Điện tiêu thụ (KWh)"],
        source: row["Nguồn"],
      });
    }
    for (const row of this._monthly.TienDien || []) {
      const key = `${row["Năm"]}-${row["Tháng"]}`;
      const item = map.get(key) || { year: Number(row["Năm"]), month: Number(row["Tháng"]) };
      Object.assign(item, {
        cost: row["Tiền Điện"],
        status: row["Trạng thái"],
        source: row["Nguồn"] || item.source,
      });
      map.set(key, item);
    }
    return [...map.values()].sort((a, b) => a.year - b.year || a.month - b.month);
  }

  _renderMonthly() {
    const selected = this.$("yearSelect").value;
    const rows = this._mergedMonthly().filter((row) => selected === "all" || String(row.year) === selected);
    const max = Math.max(1, ...rows.map((row) => Number(row.kwh) || 0));
    this.$("monthlyBars").innerHTML = rows.length
      ? rows.map((row) => `<div class="bar-col"><span class="bar-value">${this._fmt(row.kwh, 1)}</span><div class="bar" style="height:${Math.max(2, (Number(row.kwh) || 0) / max * 155)}px"></div><span class="bar-label">${String(row.month).padStart(2, "0")}/${row.year}</span></div>`).join("")
      : this._empty();
    this.$("monthlyTable").innerHTML = rows.slice().reverse().map((row) => `
      <tr>
        <td>${String(row.month).padStart(2, "0")}/${row.year}</td>
        <td>${this._kwh(row.kwh)}</td>
        <td>${this._money(row.cost)}</td>
        <td>${this._escape(row.status || "—")}</td>
        <td class="${row.source === "invoice" ? "source-official" : ""}">${this._escape(row.source || "—")}</td>
      </tr>`).join("") || `<tr><td colspan="5">Chưa có dữ liệu.</td></tr>`;
  }

  _renderDaily() {
    const start = this.$("startDate").value;
    const end = this.$("endDate").value;
    let rows = (this._daily || []).filter((row) =>
      (!start || row["Ngày ISO"] >= start) && (!end || row["Ngày ISO"] <= end));
    rows = rows.slice().sort((a, b) => (b["Ngày ISO"] || "").localeCompare(a["Ngày ISO"] || ""));
    this.$("dailyTable").innerHTML = rows.map((row) => `
      <tr>
        <td>${this._escape(row["Ngày"] || row["Ngày ISO"] || "—")}</td>
        <td>${this._fmt(row.CHISO, 3)}</td>
        <td>${this._kwh(row["Điện tiêu thụ (kWh)"])}</td>
      </tr>`).join("") || `<tr><td colspan="3">Chưa có dữ liệu trong khoảng chọn.</td></tr>`;
  }

  _renderOutages() {
    const rows = this._summary.outages || [];
    this.$("outages").innerHTML = rows.length
      ? rows.map((row) => `<div class="item"><strong>${this._escape(row.start_date || "")}: ${this._escape(row.start_time || "")} - ${this._escape(row.end_time || "")}</strong><div>${this._escape(row.area || "Không có khu vực")}</div><small class="muted">${this._escape(row.reason || "Không có lý do")}</small></div>`).join("")
      : this._empty();
  }

  _renderFilesAndErrors() {
    const files = this._summary.invoice_files || [];
    this.$("invoiceFiles").innerHTML = files.length
      ? files.map((file) => `<div class="item"><strong>${this._escape(file.name)}</strong><span class="muted">${this._fmt(file.size / 1024, 1)} KB • ${this._escape(String(file.type || "").toUpperCase())}</span></div>`).join("")
      : this._empty();
    const errors = this._summary.partial_errors || [];
    this.$("partialErrors").innerHTML = errors.length
      ? `<strong>Lỗi từng phần lần đồng bộ gần nhất:</strong>${errors.map((error) => `<div>• ${this._escape(error)}</div>`).join("")}`
      : "";
  }

  _fmt(value, digits = 0) {
    if (value == null || Number.isNaN(Number(value))) return "—";
    return Number(value).toLocaleString("vi-VN", { maximumFractionDigits: digits });
  }

  _money(value) {
    return value == null ? "—" : `${this._fmt(value)} ₫`;
  }

  _kwh(value) {
    return value == null ? "—" : `${this._fmt(value, 3)} kWh`;
  }

  _empty() {
    return `<div class="empty">Chưa có dữ liệu.</div>`;
  }

  _escape(value) {
    return String(value ?? "").replace(/[&<>'"]/g, (char) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      "'": "&#39;",
      '"': "&quot;",
    }[char]));
  }

  _styles() {
    return `
      :host{display:block;min-height:100vh;color-scheme:dark;--bg:#07101f;--panel:#101a2b;--panel2:#152238;--line:#263752;--text:#f4f7fb;--muted:#9fb0c8;--good:#34d399;--bad:#fb7185;--shadow:0 12px 35px rgba(0,0,0,.22);font:14px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;color:var(--text);background:radial-gradient(circle at 8% -10%,#173760 0,transparent 30%),var(--bg)}
      *{box-sizing:border-box}.app{min-height:100vh}.topbar{position:sticky;top:0;z-index:10;display:flex;justify-content:space-between;gap:18px;align-items:center;padding:14px 20px;background:rgba(7,16,31,.94);backdrop-filter:blur(15px);border-bottom:1px solid var(--line)}
      .brand{display:flex;gap:12px;align-items:center;min-width:0}.brand img{width:50px;height:50px;border-radius:14px;flex:0 0 auto}.brand-text{min-width:0}.brand h1{margin:0;font-size:20px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.brand p,.panel-head p{margin:2px 0 0;color:var(--muted)}
      .toolbar,.range{display:flex;gap:8px;flex-wrap:wrap}select,input,button{border:1px solid var(--line);border-radius:9px;background:var(--panel2);color:var(--text);padding:9px 11px}button{cursor:pointer;background:#1d4ed8;border-color:#2563eb;font-weight:650}button:disabled{opacity:.55;cursor:wait}button.secondary{background:var(--panel2);border-color:var(--line)}
      main{max-width:1500px;margin:auto;padding:18px}.status-row{display:flex;gap:10px;align-items:center;margin-bottom:12px;flex-wrap:wrap}.pill{border:1px solid var(--line);padding:5px 9px;border-radius:999px;background:var(--panel)}.muted{color:var(--muted)}.notice{padding:10px 12px;border:1px solid var(--bad);background:rgba(251,113,133,.08);border-radius:10px;margin-bottom:12px}.hidden{display:none}
      .metric-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px}.metric,.panel{background:linear-gradient(180deg,var(--panel2),var(--panel));border:1px solid var(--line);border-radius:14px;box-shadow:var(--shadow)}.metric{padding:14px}.metric span{color:var(--muted)}.metric strong{display:block;font-size:23px;margin:6px 0}.metric small{color:var(--muted)}
      .panel{margin-top:12px;padding:15px}.two-col{display:grid;grid-template-columns:1.5fr 1fr;gap:18px}.panel-head{display:flex;justify-content:space-between;gap:12px;align-items:flex-start;margin-bottom:12px}.panel h2{font-size:17px;margin:0}.bars{display:flex;align-items:end;gap:7px;min-height:210px;overflow-x:auto;padding:12px 3px 2px}.bar-col{min-width:42px;display:flex;flex-direction:column;align-items:center;gap:5px}.bar-value{font-size:10px;color:var(--muted)}.bar{width:24px;min-height:2px;border-radius:6px 6px 2px 2px;background:linear-gradient(#60a5fa,#2563eb)}.bar-label{font-size:10px;white-space:nowrap;color:var(--muted)}
      .details{display:grid;grid-template-columns:1fr auto;gap:9px 12px;margin:0}.details dt{color:var(--muted)}.details dd{margin:0;font-weight:650;text-align:right}.table-wrap{overflow:auto;max-height:430px;border:1px solid var(--line);border-radius:10px}.table-wrap.tall{max-height:620px}table{width:100%;border-collapse:collapse;min-width:660px}th,td{padding:9px 10px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}th{position:sticky;top:0;background:#132138;z-index:1;color:#d8e5f6}th:first-child,td:first-child{text-align:left}tbody tr:hover{background:rgba(96,165,250,.06)}
      .list{display:grid;gap:8px}.item{padding:10px;border:1px solid var(--line);border-radius:9px;background:rgba(0,0,0,.08)}.item strong{display:block;margin-bottom:2px}.empty{color:var(--muted);padding:12px 0}.errors{margin-top:10px;color:#fecdd3}.errors div{margin-top:5px}.source-official{color:var(--good)}
      @media(max-width:1000px){.metric-grid{grid-template-columns:repeat(2,1fr)}.two-col{grid-template-columns:1fr}.topbar{align-items:flex-start;flex-direction:column}.toolbar{width:100%}.toolbar select,.toolbar button{flex:1}.daily-head{flex-direction:column}}
      @media(max-width:560px){main{padding:10px}.metric-grid{grid-template-columns:1fr 1fr}.metric strong{font-size:19px}.brand p{display:none}.brand h1{font-size:18px}.range>*{flex:1}.panel{padding:11px}}
    `;
  }
}

if (!customElements.get("evn-cskh-monitor-panel")) {
  customElements.define("evn-cskh-monitor-panel", EVNCSKHMonitorPanel);
}
