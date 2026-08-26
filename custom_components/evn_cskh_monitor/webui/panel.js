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
    this._pendingAccount = "";
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
          </section>

          <section id="outagePanel" class="outage-alert hidden" aria-live="polite">
            <div class="outage-alert-head">
              <div class="outage-title-wrap">
                <div class="outage-bolt">⚠</div>
                <div><span class="outage-kicker">THÔNG BÁO EVN</span><h2>Lịch cắt điện</h2></div>
              </div>
              <span id="outageCount" class="outage-count">0 lịch</span>
            </div>
            <div id="outages" class="outage-list"></div>
          </section>

          <section class="panel chart-panel yearly-panel">
            <div class="panel-head">
              <div><h2 id="yearlyTitle">Sản lượng năm —</h2><p id="monthlyCaption">Sản lượng và hóa đơn theo từng tháng.</p></div>
              <span class="year-badge" id="chartYearBadge">—</span>
            </div>
            <div class="stats-grid stats-grid-year">
              <article class="stat-card"><span>Tổng sản lượng</span><strong id="yearTotalKwh">—</strong><small>trong năm đã chọn</small></article>
              <article class="stat-card"><span>Tổng tiền</span><strong id="yearTotalCost">—</strong><small>hóa đơn EVN có dữ liệu</small></article>
              <article class="stat-card stat-average"><span>Trung bình</span><strong id="yearAvgKwh">—</strong><small id="yearAvgCost">—</small></article>
            </div>
            <div class="year-chart-legend" aria-label="Chú giải biểu đồ năm">
              <span><i class="legend-swatch legend-kwh"></i>Sản lượng (kWh)</span>
              <span><i class="legend-swatch legend-cost"></i>Tiền điện (₫)</span>
            </div>
            <div class="chart-frame yearly-chart-frame">
              <div id="monthlyBars" class="bars monthly-bars" role="img" aria-label="Biểu đồ cột đôi sản lượng và tiền điện từng tháng trong năm"></div>
            </div>
          </section>

          <section class="panel chart-panel month-panel">
            <div class="panel-head month-head">
              <div><h2 id="selectedMonthTitle">Sản lượng hằng tháng</h2><p id="selectedMonthCaption">Biểu đồ sản lượng từng ngày của tháng.</p></div>
              <div class="period-controls">
                <label class="mini-control"><span>Tháng</span><select id="dailyMonthSelect" aria-label="Tháng dữ liệu ngày"></select></label>
                <label class="mini-control"><span>Năm</span><select id="dailyYearSelect" aria-label="Năm dữ liệu ngày"></select></label>
              </div>
            </div>
            <div class="stats-grid stats-grid-month">
              <article class="stat-card"><span>Tổng sản lượng</span><strong id="monthTotalKwh">—</strong><small id="monthDaysCount">—</small></article>
              <article class="stat-card"><span>Tổng tiền</span><strong id="monthTotalCost">—</strong><small>hóa đơn EVN của tháng</small></article>
              <article class="stat-card stat-average"><span>Trung bình</span><strong id="monthAvgKwh">—</strong><small id="monthAvgCost">—</small></article>
            </div>
            <div class="chart-frame daily-chart-frame">
              <div id="dailyBars" class="bars daily-bars" role="img" aria-label="Biểu đồ sản lượng từng ngày trong tháng"></div>
            </div>
          </section>

          <section class="panel daily-panel">
            <div class="panel-head daily-head">
              <div><h2>Sản lượng hằng ngày</h2><p id="dailyCaption">Ngày mới nhất được hiển thị ở trên.</p></div>
            </div>
            <div class="table-wrap daily-table-wrap">
              <table class="daily-table">
                <thead><tr><th>Ngày</th><th>Chỉ số công tơ</th><th>Sản lượng</th></tr></thead>
                <tbody id="dailyTable"></tbody>
              </table>
            </div>
          </section>
        </main>
        <div id="chartTooltip" class="chart-tooltip hidden" role="status" aria-live="polite" aria-hidden="true"></div>
      </div>`;

    this.$("accountSelect").addEventListener("change", (event) => {
      void this._loadAccount(event.target.value);
    });
    this.$("yearSelect").addEventListener("change", () => this._renderYearly());
    this.$("dailyMonthSelect").addEventListener("change", () => this._renderSelectedMonth());
    this.$("dailyYearSelect").addEventListener("change", () => {
      this._rebuildDailyMonths();
      this._renderSelectedMonth();
    });
    this.$("reloadBtn").addEventListener("click", () => void this._reloadAll());

    const monthlyBars = this.$("monthlyBars");
    monthlyBars.addEventListener("click", (event) => {
      const group = event.target.closest?.(".month-bar-group");
      if (group) this._toggleChartTooltip(group);
    });
    monthlyBars.addEventListener("keydown", (event) => {
      if (event.key !== "Enter" && event.key !== " ") return;
      const group = event.target.closest?.(".month-bar-group");
      if (!group) return;
      event.preventDefault();
      this._toggleChartTooltip(group);
    });
    this.shadowRoot.addEventListener("click", (event) => {
      if (event.target.closest?.(".month-bar-group")) return;
      this._hideChartTooltip();
    });
  }

  _toggleChartTooltip(anchor) {
    const tooltip = this.$("chartTooltip");
    if (!tooltip) return;
    if (this._tooltipAnchor === anchor && !tooltip.classList.contains("hidden")) {
      this._hideChartTooltip();
      return;
    }
    this._showChartTooltip(anchor);
  }

  _showChartTooltip(anchor) {
    const tooltip = this.$("chartTooltip");
    if (!tooltip || !anchor) return;
    const raw = anchor.dataset.tooltip || anchor.getAttribute("title") || "";
    if (!raw) return;

    this._tooltipAnchor?.classList.remove("tooltip-active");
    this._tooltipAnchor = anchor;
    anchor.classList.add("tooltip-active");
    tooltip.textContent = raw.replaceAll(" • ", "\n");
    tooltip.classList.remove("hidden");
    tooltip.setAttribute("aria-hidden", "false");
    tooltip.style.left = "0px";
    tooltip.style.top = "0px";

    const anchorRect = anchor.getBoundingClientRect();
    const tipRect = tooltip.getBoundingClientRect();
    const viewport = window.visualViewport;
    const viewLeft = viewport?.offsetLeft ?? 0;
    const viewTop = viewport?.offsetTop ?? 0;
    const viewWidth = viewport?.width ?? window.innerWidth;
    const viewHeight = viewport?.height ?? window.innerHeight;
    const margin = 8;
    const gap = 10;
    const minLeft = viewLeft + margin;
    const maxLeft = Math.max(minLeft, viewLeft + viewWidth - tipRect.width - margin);
    const minTop = viewTop + margin;
    const maxTop = Math.max(minTop, viewTop + viewHeight - tipRect.height - margin);
    const centeredLeft = anchorRect.left + anchorRect.width / 2 - tipRect.width / 2;
    const above = anchorRect.top - tipRect.height - gap;
    const below = anchorRect.bottom + gap;

    let top;
    if (above >= minTop) top = above;
    else if (below <= maxTop) top = below;
    else top = Math.min(Math.max(anchorRect.top + anchorRect.height / 2 - tipRect.height / 2, minTop), maxTop);

    tooltip.style.left = `${Math.min(Math.max(centeredLeft, minLeft), maxLeft)}px`;
    tooltip.style.top = `${Math.min(Math.max(top, minTop), maxTop)}px`;
  }

  _hideChartTooltip() {
    const tooltip = this.$("chartTooltip");
    this._tooltipAnchor?.classList.remove("tooltip-active");
    this._tooltipAnchor = null;
    if (!tooltip) return;
    tooltip.classList.add("hidden");
    tooltip.setAttribute("aria-hidden", "true");
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
    if (!account) return;
    if (this._loading) {
      this._pendingAccount = account;
      return;
    }

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
      const pending = this._pendingAccount;
      this._pendingAccount = "";
      if (pending && pending !== this._currentAccount) void this._loadAccount(pending);
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
      map.set(`${year}-${month}`, {
        year,
        month,
        kwh: row["Điện tiêu thụ (KWh)"],
        kwhSource: row["Nguồn"],
      });
    }
    for (const row of this._monthly.TienDien || []) {
      const year = Number(row["Năm"]);
      const month = Number(row["Tháng"]);
      if (!year || !month) continue;
      const key = `${year}-${month}`;
      const item = map.get(key) || { year, month };
      item.cost = row["Tiền Điện"];
      item.costSource = row["Nguồn"];
      item.status = row["Trạng thái"];
      map.set(key, item);
    }

    // A few EVN regions intermittently omit monthly consumption even while the
    // daily series is complete. Fill only missing monthly kWh from daily data;
    // official monthly values always win and money is never fabricated per day.
    const dailyTotals = new Map();
    for (const row of this._daily || []) {
      const date = this._parseIso(row["Ngày ISO"]);
      const raw = row["Điện tiêu thụ (kWh)"];
      if (!date || raw == null || !Number.isFinite(Number(raw))) continue;
      const key = `${date.getFullYear()}-${date.getMonth() + 1}`;
      dailyTotals.set(key, (dailyTotals.get(key) || 0) + Number(raw));
    }
    for (const [key, kwh] of dailyTotals) {
      const [year, month] = key.split("-").map(Number);
      const item = map.get(key) || { year, month };
      if (item.kwh == null || !Number.isFinite(Number(item.kwh))) {
        item.kwh = kwh;
        item.kwhSource = "daily";
      }
      map.set(key, item);
    }

    return [...map.values()].sort((a, b) => a.year - b.year || a.month - b.month);
  }

  _buildMonthlyYears() {
    const currentYear = new Date().getFullYear();
    const yearSet = new Set(this._mergedMonthly().map((row) => row.year).filter(Boolean));
    for (const row of this._daily || []) {
      const date = this._parseIso(row["Ngày ISO"]);
      if (date) yearSet.add(date.getFullYear());
    }
    if (!yearSet.size) yearSet.add(currentYear);

    const years = [...yearSet].sort((a, b) => b - a);
    const select = this.$("yearSelect");
    const previous = Number(select.value);
    select.innerHTML = years.map((year) => `<option value="${year}">${year}</option>`).join("");
    const preferred = years.includes(previous)
      ? previous
      : (years.includes(currentYear) ? currentYear : years[0]);
    select.value = String(preferred);
  }

  _buildDailyFilters() {
    const valid = (this._daily || [])
      .map((row) => this._parseIso(row["Ngày ISO"]))
      .filter(Boolean);
    const now = new Date();
    const yearSet = new Set(valid.map((date) => date.getFullYear()));
    if (!yearSet.size) yearSet.add(now.getFullYear());
    const years = [...yearSet].sort((a, b) => b - a);
    const yearSelect = this.$("dailyYearSelect");
    const previousYear = Number(yearSelect.value);
    yearSelect.innerHTML = years.map((year) => `<option value="${year}">${year}</option>`).join("");
    const preferredYear = years.includes(previousYear)
      ? previousYear
      : (years.includes(now.getFullYear()) ? now.getFullYear() : years[0]);
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
    } else if (available.size && !available.has(preferred)) {
      preferred = [...available].sort((a, b) => b - a)[0];
    }
    monthSelect.value = String(preferred);
  }

  _renderAll() {
    this._renderHeader();
    this._renderMetrics();
    this._renderOutages();
    this._renderYearly();
    this._renderSelectedMonth();
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
  }

  _renderYearly() {
    this._hideChartTooltip();
    const year = Number(this.$("yearSelect").value);
    this.$("chartYearBadge").textContent = year || "—";
    this.$("yearlyTitle").textContent = year ? `Sản lượng năm ${year}` : "Sản lượng năm —";
    this.$("monthlyCaption").textContent = year
      ? `Sản lượng và tiền hóa đơn của các tháng có dữ liệu trong năm ${year}.`
      : "Chưa có dữ liệu theo năm.";

    const sourceRows = this._mergedMonthly().filter((row) => row.year === year);
    const byMonth = new Map(sourceRows.map((row) => [row.month, row]));
    const kwhByMonth = Array.from({ length: 12 }, (_, index) => {
      const raw = byMonth.get(index + 1)?.kwh;
      return raw == null || !Number.isFinite(Number(raw)) ? null : Number(raw);
    });
    const costByMonth = Array.from({ length: 12 }, (_, index) => {
      const raw = byMonth.get(index + 1)?.cost;
      return raw == null || !Number.isFinite(Number(raw)) ? null : Number(raw);
    });
    const maxKwh = Math.max(1, ...kwhByMonth.filter((value) => value != null));
    const maxCost = Math.max(1, ...costByMonth.filter((value) => value != null));

    this.$("monthlyBars").innerHTML = Array.from({ length: 12 }, (_, index) => {
      const month = index + 1;
      const kwh = kwhByMonth[index];
      const cost = costByMonth[index];
      const hasKwh = kwh != null;
      const hasCost = cost != null;
      const kwhPct = !hasKwh || kwh <= 0 ? 0 : Math.max(3, kwh / maxKwh * 100);
      const costPct = !hasCost || cost <= 0 ? 0 : Math.max(3, cost / maxCost * 100);
      const title = `Tháng ${month}/${year} • Sản lượng: ${hasKwh ? this._kwh(kwh) : "Chưa có dữ liệu"} • Tiền điện: ${hasCost ? this._money(cost) : "Chưa có dữ liệu"}`;
      return `
        <div class="month-bar-group ${hasKwh || hasCost ? "" : "missing"}" tabindex="0" role="button" aria-label="${this._escape(title)}" title="${this._escape(title)}" data-tooltip="${this._escape(title)}">
          <div class="month-bar-values" aria-hidden="true">
            <span class="month-value kwh-value">${hasKwh ? this._fmt(kwh, 1) : "—"}</span>
            <span class="month-value cost-value">${hasCost ? this._compactMoney(cost) : "—"}</span>
          </div>
          <div class="month-bar-tracks">
            <div class="series-track"><div class="series-bar kwh-bar ${hasKwh ? "" : "missing-series"}" style="--bar-height:${kwhPct}%"></div></div>
            <div class="series-track"><div class="series-bar cost-bar ${hasCost ? "" : "missing-series"}" style="--bar-height:${costPct}%"></div></div>
          </div>
          <span class="bar-label">${MONTH_LABELS[index]}</span>
        </div>`;
    }).join("");

    const kwhValues = sourceRows
      .map((row) => row.kwh)
      .filter((value) => value != null && Number.isFinite(Number(value)))
      .map(Number);
    const costValues = sourceRows
      .map((row) => row.cost)
      .filter((value) => value != null && Number.isFinite(Number(value)))
      .map(Number);
    const totalKwh = this._sum(kwhValues);
    const totalCost = this._sum(costValues);
    const avgKwh = kwhValues.length ? totalKwh / kwhValues.length : null;
    const avgCost = costValues.length ? totalCost / costValues.length : null;

    this.$("yearTotalKwh").textContent = kwhValues.length ? this._kwh(totalKwh) : "—";
    this.$("yearTotalCost").textContent = costValues.length ? this._money(totalCost) : "—";
    this.$("yearAvgKwh").textContent = avgKwh == null ? "—" : `${this._fmt(avgKwh, 2)} kWh/tháng`;
    this.$("yearAvgCost").textContent = avgCost == null ? "Tiền: —" : `Tiền: ${this._money(avgCost)}/tháng`;
  }

  _selectedDailyRows() {
    const year = Number(this.$("dailyYearSelect").value);
    const month = Number(this.$("dailyMonthSelect").value);
    return (this._daily || [])
      .filter((row) => {
        const date = this._parseIso(row["Ngày ISO"]);
        return date && date.getFullYear() === year && date.getMonth() + 1 === month;
      });
  }

  _renderSelectedMonth() {
    const year = Number(this.$("dailyYearSelect").value);
    const month = Number(this.$("dailyMonthSelect").value);
    const rows = this._selectedDailyRows();
    this.$("selectedMonthTitle").textContent = year && month
      ? `Sản lượng hằng tháng · ${month}/${year}`
      : "Sản lượng hằng tháng";
    this.$("selectedMonthCaption").textContent = year && month
      ? `Biểu đồ sản lượng từng ngày trong tháng ${month}/${year}.`
      : "Chưa có dữ liệu ngày để chọn.";
    this.$("dailyCaption").textContent = year && month
      ? `Tháng ${month}/${year} · ngày mới nhất hiển thị ở trên.`
      : "Ngày mới nhất được hiển thị ở trên.";

    this._renderDailyChart(rows, year, month);
    this._renderMonthStats(rows, year, month);
    this._renderDailyTable(rows);
  }

  _renderDailyChart(rows, year, month) {
    const byDay = new Map();
    for (const row of rows) {
      const date = this._parseIso(row["Ngày ISO"]);
      if (!date) continue;
      byDay.set(date.getDate(), row);
    }

    const daysInMonth = year && month ? new Date(year, month, 0).getDate() : 31;
    const values = Array.from({ length: daysInMonth }, (_, index) => {
      const raw = byDay.get(index + 1)?.["Điện tiêu thụ (kWh)"];
      return raw == null || !Number.isFinite(Number(raw)) ? null : Number(raw);
    });
    const max = Math.max(1, ...values.filter((value) => value != null));
    const bars = this.$("dailyBars");
    bars.style.setProperty("--day-count", String(daysInMonth));
    bars.innerHTML = Array.from({ length: daysInMonth }, (_, index) => {
      const day = index + 1;
      const value = values[index];
      const hasValue = value != null;
      const pct = hasValue ? Math.max(4, value / max * 100) : 1.5;
      const showLabel = day === 1 || day === daysInMonth || day % 5 === 0;
      const title = `${String(day).padStart(2, "0")}/${String(month || "").padStart(2, "0")}/${year || ""}: ${hasValue ? this._kwh(value) : "Chưa có dữ liệu"}`;
      return `
        <div class="bar-col day-bar-col ${hasValue ? "" : "missing"}" title="${this._escape(title)}">
          <span class="bar-value">${hasValue ? this._fmt(value, 1) : ""}</span>
          <div class="bar-track"><div class="bar" style="--bar-height:${pct}%"></div></div>
          <span class="bar-label ${showLabel ? "" : "quiet-label"}">${showLabel ? day : "·"}</span>
        </div>`;
    }).join("");
  }

  _renderMonthStats(rows, year, month) {
    const values = rows
      .map((row) => row["Điện tiêu thụ (kWh)"])
      .filter((value) => value != null && Number.isFinite(Number(value)))
      .map(Number);
    const totalKwh = this._sum(values);
    const avgKwh = values.length ? totalKwh / values.length : null;
    const monthRow = this._mergedMonthly().find((row) => row.year === year && row.month === month);
    const cost = monthRow?.cost != null && Number.isFinite(Number(monthRow.cost)) ? Number(monthRow.cost) : null;
    const avgCost = cost != null && values.length ? cost / values.length : null;

    this.$("monthTotalKwh").textContent = values.length ? this._kwh(totalKwh) : "—";
    this.$("monthDaysCount").textContent = values.length ? `${values.length} ngày có dữ liệu` : "Chưa có ngày có dữ liệu";
    this.$("monthTotalCost").textContent = this._money(cost);
    this.$("monthAvgKwh").textContent = avgKwh == null ? "—" : `${this._fmt(avgKwh, 2)} kWh/ngày`;
    this.$("monthAvgCost").textContent = avgCost == null ? "Tiền: —" : `Tiền: ${this._money(avgCost)}/ngày`;
  }

  _renderDailyTable(rows) {
    const sortedRows = rows.slice().sort((a, b) => (b["Ngày ISO"] || "").localeCompare(a["Ngày ISO"] || ""));
    this.$("dailyTable").innerHTML = sortedRows.length
      ? sortedRows.map((row) => `
          <tr>
            <td><strong>${this._escape(this._dayLabel(row["Ngày ISO"], row["Ngày"]))}</strong><small>${this._escape(row["Ngày ISO"] || "")}</small></td>
            <td>${this._fmt(row.CHISO, 3)}</td>
            <td>${this._kwh(row["Điện tiêu thụ (kWh)"])}</td>
          </tr>`).join("")
      : `<tr><td colspan="3" class="empty-cell">Chưa có dữ liệu trong tháng đã chọn.</td></tr>`;
  }

  _renderOutages() {
    const rows = this._summary.outages || [];
    const panel = this.$("outagePanel");
    panel.classList.toggle("hidden", !rows.length);
    if (!rows.length) {
      this.$("outages").innerHTML = "";
      this.$("outageCount").textContent = "0 lịch";
      return;
    }

    this.$("outageCount").textContent = `${rows.length} lịch`;
    this.$("outages").innerHTML = rows.map((row) => `
      <article class="outage-item">
        <div class="outage-item-icon">⚡</div>
        <div class="outage-item-body">
          <strong>${this._escape(row.start_date || "")}${row.start_time ? ` · ${this._escape(row.start_time)}` : ""}${row.end_time ? ` – ${this._escape(row.end_time)}` : ""}</strong>
          <p>${this._escape(row.area || "Khu vực chưa xác định")}</p>
          <small>${this._escape(row.reason || "EVN chưa cung cấp lý do")}</small>
        </div>
      </article>`).join("");
  }

  _parseIso(value) {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(String(value || ""))) return null;
    const [year, month, day] = String(value).split("-").map(Number);
    const date = new Date(year, month - 1, day);
    if (Number.isNaN(date.getTime())) return null;
    if (date.getFullYear() !== year || date.getMonth() + 1 !== month || date.getDate() !== day) return null;
    return date;
  }

  _dayLabel(iso, fallback) {
    const date = this._parseIso(iso);
    return date ? String(date.getDate()).padStart(2, "0") : (fallback || iso || "—");
  }

  _sum(values) {
    return values.reduce((total, value) => total + Number(value || 0), 0);
  }

  _fmt(value, digits = 0) {
    if (value == null || value === "" || Number.isNaN(Number(value))) return "—";
    return Number(value).toLocaleString("vi-VN", { maximumFractionDigits: digits });
  }

  _money(value) {
    return value == null || !Number.isFinite(Number(value)) ? "—" : `${this._fmt(value)} ₫`;
  }

  _compactMoney(value) {
    if (value == null || !Number.isFinite(Number(value))) return "—";
    const amount = Number(value);
    const abs = Math.abs(amount);
    if (abs >= 1_000_000_000) return `${this._fmt(amount / 1_000_000_000, 2)}tỷ`;
    if (abs >= 1_000_000) return `${this._fmt(amount / 1_000_000, 2)}tr`;
    if (abs >= 1_000) return `${this._fmt(amount / 1_000, 0)}k`;
    return this._fmt(amount);
  }

  _kwh(value) {
    return value == null || !Number.isFinite(Number(value)) ? "—" : `${this._fmt(value, 3)} kWh`;
  }

  _kwhNumber(value) {
    return value == null || !Number.isFinite(Number(value)) ? "—" : `${this._fmt(value, 2)} kWh`;
  }

  _escape(value) {
    return String(value ?? "").replace(/[&<>'"]/g, (char) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
    }[char]));
  }

  _styles() {
    return `
      :host{display:block;min-height:100%;color-scheme:dark;--bg:var(--primary-background-color,#07101f);--surface:var(--card-background-color,#101d31);--surface2:color-mix(in srgb,var(--surface) 88%,#213957);--text:var(--primary-text-color,#f4f7fb);--muted:var(--secondary-text-color,#94a7c2);--line:color-mix(in srgb,var(--text) 12%,transparent);--blue:var(--primary-color,#4c8dff);--blue2:#2457e6;--green:#39d98a;--amber:#ffbf47;--red:#ff6b7a;--shadow:0 16px 42px rgba(0,0,0,.18);font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
      *{box-sizing:border-box}button,select{font:inherit}.app{min-height:100vh;min-height:100dvh;background:radial-gradient(circle at 75% -15%,rgba(49,104,204,.16),transparent 30%),var(--bg);color:var(--text)}
      .topbar{position:sticky;top:0;z-index:10;background:color-mix(in srgb,var(--bg) 88%,transparent);backdrop-filter:blur(18px);border-bottom:1px solid var(--line)}.topbar-inner{width:min(1500px,100%);margin:auto;padding:clamp(10px,1.6vw,18px) clamp(10px,2vw,22px);display:flex;align-items:center;justify-content:space-between;gap:16px}.brand{display:flex;align-items:center;gap:12px;min-width:0}.brand img{width:56px;height:56px;border-radius:17px;box-shadow:0 10px 26px rgba(0,0,0,.24);flex:0 0 auto}.brand-text{min-width:0}.brand h1{margin:0;font-size:clamp(20px,2.2vw,30px);line-height:1.05;letter-spacing:-.025em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.brand p{margin:4px 0 0;color:var(--muted);font-size:clamp(11px,1vw,14px);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
      .toolbar{display:grid;grid-template-columns:minmax(150px,1fr) minmax(112px,.65fr) auto;gap:8px;align-items:end}.control,.mini-control{display:grid;gap:4px}.control>span,.mini-control>span{font-size:11px;color:var(--muted);padding-left:4px}.control select,.mini-control select,button{width:100%;height:42px;border:1px solid var(--line);border-radius:12px;background:var(--surface2);color:var(--text);padding:0 12px;outline:none}.control select:focus,.mini-control select:focus,button:focus-visible{border-color:var(--blue);box-shadow:0 0 0 3px color-mix(in srgb,var(--blue) 18%,transparent)}button{cursor:pointer}.reload-btn{display:flex;align-items:center;justify-content:center;gap:7px;background:linear-gradient(135deg,var(--blue),var(--blue2));border-color:transparent;color:#fff;font-weight:700;box-shadow:0 10px 24px rgba(36,87,230,.22)}.reload-btn:hover{filter:brightness(1.08);transform:translateY(-1px)}.reload-btn:disabled{opacity:.55;cursor:wait;transform:none}.reload-icon{font-size:20px;line-height:1}
      main{width:min(1500px,100%);margin:auto;padding:clamp(10px,2vw,22px)}.notice{padding:11px 13px;border:1px solid color-mix(in srgb,var(--red) 55%,transparent);background:color-mix(in srgb,var(--red) 9%,transparent);border-radius:12px;margin-bottom:12px;color:#ffd3d8}.hidden{display:none!important}.status-row{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:12px}.pill{display:inline-flex;align-items:center;gap:7px;padding:6px 10px;border:1px solid var(--line);border-radius:999px;background:color-mix(in srgb,var(--surface) 78%,transparent);font-size:12px}.status-dot{width:7px;height:7px;border-radius:50%;background:var(--green);box-shadow:0 0 12px color-mix(in srgb,var(--green) 70%,transparent)}.status-dot.pending{background:var(--amber);box-shadow:0 0 12px color-mix(in srgb,var(--amber) 70%,transparent)}.muted{color:var(--muted)}.customer-meta{font-size:12px;overflow-wrap:anywhere}
      .metric-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,220px),1fr));gap:10px}.metric,.panel{border:1px solid var(--line);background:linear-gradient(145deg,color-mix(in srgb,var(--surface) 96%,#152d50),color-mix(in srgb,var(--surface) 94%,#07101f));box-shadow:var(--shadow)}.metric{position:relative;overflow:hidden;border-radius:16px;padding:14px;display:flex;align-items:center;gap:12px;min-height:105px;transition:transform .2s ease,border-color .2s ease}.metric:after{content:"";position:absolute;width:110px;height:110px;border-radius:50%;right:-45px;top:-55px;background:radial-gradient(circle,color-mix(in srgb,var(--blue) 20%,transparent),transparent 67%)}.metric:hover{transform:translateY(-2px);border-color:color-mix(in srgb,var(--blue) 38%,var(--line))}.metric-icon{width:40px;height:40px;border-radius:13px;display:grid;place-items:center;flex:0 0 auto;background:color-mix(in srgb,var(--blue) 12%,transparent);color:#a9c8ff;font-weight:800}.metric span{display:block;color:var(--muted);font-size:12px}.metric strong{display:block;margin:2px 0 1px;font-size:clamp(18px,2vw,26px);line-height:1.2;letter-spacing:-.02em;overflow-wrap:anywhere}.metric small{color:var(--muted);font-size:11px}.metric-money .metric-icon{color:#8df0c4;background:rgba(57,217,138,.1)}.metric-debt .metric-icon{color:#ffd88b;background:rgba(255,191,71,.1)}
      .outage-alert{margin-top:12px;border:1px solid rgba(255,191,71,.58);border-radius:19px;padding:clamp(13px,2vw,18px);background:linear-gradient(135deg,rgba(255,191,71,.14),rgba(255,107,122,.08) 55%,color-mix(in srgb,var(--surface) 94%,#24180a));box-shadow:0 18px 48px rgba(255,160,48,.13),inset 0 1px 0 rgba(255,255,255,.05);position:relative;overflow:hidden}.outage-alert:before{content:"";position:absolute;inset:-100px auto auto -80px;width:220px;height:220px;border-radius:50%;background:radial-gradient(circle,rgba(255,191,71,.17),transparent 70%);pointer-events:none}.outage-alert-head{position:relative;display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:12px}.outage-title-wrap{display:flex;align-items:center;gap:11px}.outage-bolt{width:46px;height:46px;border-radius:14px;display:grid;place-items:center;background:linear-gradient(145deg,#ffd36d,#ff9f43);color:#231400;font-size:22px;box-shadow:0 10px 28px rgba(255,174,48,.26);animation:outagePulse 2.2s ease-in-out infinite}.outage-kicker{display:block;color:#ffd98a;font-size:10px;font-weight:800;letter-spacing:.12em}.outage-alert h2{margin:1px 0 0;font-size:clamp(18px,1.8vw,24px)}.outage-count{padding:6px 10px;border-radius:999px;background:rgba(255,191,71,.13);border:1px solid rgba(255,191,71,.32);color:#ffe1a3;font-size:11px;font-weight:750;white-space:nowrap}.outage-list{position:relative;display:grid;gap:8px}.outage-item{display:flex;align-items:flex-start;gap:10px;padding:11px 12px;border:1px solid rgba(255,213,128,.2);border-radius:13px;background:rgba(7,16,31,.32)}.outage-item-icon{width:34px;height:34px;border-radius:10px;display:grid;place-items:center;flex:0 0 auto;background:rgba(255,191,71,.13);color:#ffd27b}.outage-item-body{min-width:0}.outage-item strong{display:block;font-size:13px;color:#fff3d2}.outage-item p{margin:3px 0;color:var(--text);overflow-wrap:anywhere}.outage-item small{display:block;color:color-mix(in srgb,var(--muted) 90%,#ffd88b);font-size:11px;overflow-wrap:anywhere}
      .panel{border-radius:18px;margin-top:12px;padding:clamp(12px,2vw,18px)}.panel-head{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;margin-bottom:12px}.panel-head h2{margin:0;font-size:clamp(17px,1.6vw,22px);letter-spacing:-.015em}.panel-head p{margin:3px 0 0;color:var(--muted);font-size:12px}.year-badge{display:inline-grid;place-items:center;min-width:60px;padding:6px 10px;border-radius:999px;background:color-mix(in srgb,var(--blue) 12%,transparent);border:1px solid color-mix(in srgb,var(--blue) 25%,transparent);color:#b9d0ff;font-weight:750}
      .stats-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;margin-bottom:12px}.stat-card{min-width:0;border:1px solid var(--line);border-radius:13px;padding:11px 12px;background:color-mix(in srgb,var(--bg) 22%,transparent)}.stat-card span{display:block;color:var(--muted);font-size:11px}.stat-card strong{display:block;margin-top:3px;font-size:clamp(15px,1.5vw,20px);line-height:1.25;overflow-wrap:anywhere}.stat-card small{display:block;margin-top:3px;color:var(--muted);font-size:10.5px;overflow-wrap:anywhere}.stat-average strong{color:#bcd3ff}
      .year-chart-legend{display:flex;align-items:center;flex-wrap:wrap;gap:8px 16px;margin:1px 1px 2px;color:var(--muted);font-size:11px}.year-chart-legend>span{display:inline-flex;align-items:center;gap:6px;white-space:nowrap}.legend-swatch{display:inline-block;width:10px;height:10px;border-radius:3px;box-shadow:inset 0 1px 0 rgba(255,255,255,.2)}.legend-kwh{background:linear-gradient(180deg,#65b0ff,#2457e6)}.legend-cost{background:linear-gradient(180deg,#ffd477,#e88a18)}.chart-tooltip{position:fixed;z-index:10000;max-width:min(340px,calc(100vw - 16px));padding:9px 11px;border:1px solid color-mix(in srgb,var(--blue) 45%,var(--line));border-radius:10px;background:color-mix(in srgb,var(--surface2) 96%,#000);box-shadow:0 14px 38px rgba(0,0,0,.42);color:var(--text);font-size:12px;line-height:1.5;font-weight:600;white-space:pre-line;overflow-wrap:anywhere;pointer-events:none;backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px)}
      .chart-frame{height:clamp(220px,32vw,355px);padding:8px 2px 0;overflow:hidden}.bars{height:100%;display:grid;align-items:stretch}.monthly-bars{grid-template-columns:repeat(12,minmax(0,1fr));gap:clamp(2px,.7vw,10px);min-width:0}.month-bar-group{height:100%;display:grid;grid-template-rows:34px 1fr 22px;gap:4px;align-items:end;text-align:center;min-width:0;cursor:pointer;touch-action:manipulation;border-radius:8px;outline:none}.month-bar-group:focus-visible,.month-bar-group.tooltip-active{box-shadow:0 0 0 2px color-mix(in srgb,var(--blue) 55%,transparent)}.month-bar-values{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:2px;align-items:end;min-width:0;height:100%}.month-value{font-size:clamp(7px,.72vw,11px);font-weight:650;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0;align-self:end}.kwh-value{color:#9dc8ff}.cost-value{color:#ffd18a;overflow:visible;text-overflow:clip;transform:rotate(-34deg);transform-origin:50% 100%;justify-self:center}.month-bar-tracks{position:relative;height:100%;min-height:125px;display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:clamp(2px,.32vw,6px);align-items:stretch;border-bottom:1px solid var(--line);min-width:0}.month-bar-tracks:before{content:"";position:absolute;inset:0 0 auto;height:1px;background:linear-gradient(90deg,transparent,var(--line),transparent);opacity:.45}.series-track{height:100%;min-width:0;display:flex;align-items:flex-end;justify-content:center}.series-bar{height:var(--bar-height);width:min(82%,32px);min-width:3px;border-radius:8px 8px 3px 3px;animation:growbar .55s cubic-bezier(.2,.8,.2,1) both;transform-origin:bottom;box-shadow:inset 0 1px 0 rgba(255,255,255,.22)}.kwh-bar{background:linear-gradient(180deg,#65b0ff 0%,#4388ff 38%,#2457e6 100%);box-shadow:0 8px 18px rgba(37,87,230,.22),inset 0 1px 0 rgba(255,255,255,.24)}.cost-bar{background:linear-gradient(180deg,#ffd477 0%,#ffb342 42%,#e88a18 100%);box-shadow:0 8px 18px rgba(232,138,24,.2),inset 0 1px 0 rgba(255,255,255,.24)}.series-bar.missing-series{height:0;min-height:0;background:transparent;box-shadow:none}.month-bar-group:hover .series-bar{filter:brightness(1.1)}.daily-bars{grid-template-columns:repeat(var(--day-count,31),minmax(3px,1fr));gap:clamp(1px,.35vw,5px);min-width:0}.bar-col{height:100%;display:grid;grid-template-rows:22px 1fr 22px;gap:4px;align-items:end;text-align:center;min-width:0}.bar-value{font-size:clamp(8px,1vw,12px);color:#b8c7dc;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0}.bar-track{position:relative;height:100%;min-height:125px;display:flex;align-items:flex-end;justify-content:center;border-bottom:1px solid var(--line)}.bar-track:before{content:"";position:absolute;inset:0 0 auto;height:1px;background:linear-gradient(90deg,transparent,var(--line),transparent);opacity:.45}.bar{height:var(--bar-height);width:min(68%,46px);min-width:4px;border-radius:9px 9px 3px 3px;background:linear-gradient(180deg,#65b0ff 0%,#4388ff 38%,#2457e6 100%);box-shadow:0 8px 20px rgba(37,87,230,.25),inset 0 1px 0 rgba(255,255,255,.24);animation:growbar .55s cubic-bezier(.2,.8,.2,1) both;transform-origin:bottom}.day-bar-col .bar{width:72%;border-radius:6px 6px 2px 2px}.bar-col.missing .bar{background:color-mix(in srgb,var(--muted) 12%,transparent);box-shadow:none}.bar-label{font-size:clamp(8px,.9vw,11px);color:var(--muted);align-self:start}.quiet-label{opacity:.3}.bar-col:hover .bar{filter:brightness(1.12)}.daily-chart-frame{height:clamp(225px,29vw,330px)}
      .table-wrap{width:100%;overflow:hidden;border:1px solid var(--line);border-radius:13px;background:color-mix(in srgb,var(--bg) 20%,transparent)}table{width:100%;border-collapse:collapse;table-layout:fixed}th,td{padding:10px 12px;border-bottom:1px solid var(--line);text-align:right;vertical-align:middle;overflow-wrap:anywhere}th{font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:#b8c7dc;background:color-mix(in srgb,var(--surface2) 86%,transparent)}th:first-child,td:first-child{text-align:left}tbody tr:last-child td{border-bottom:0}tbody tr:hover{background:color-mix(in srgb,var(--blue) 5%,transparent)}td small{display:block;color:var(--muted);font-size:10px}.daily-table th:nth-child(1){width:25%}.daily-table th:nth-child(2){width:38%}.daily-table th:nth-child(3){width:37%}.empty-cell{text-align:center!important;color:var(--muted);padding:22px}
      .month-head{align-items:end}.period-controls{display:grid;grid-template-columns:repeat(2,minmax(100px,145px));gap:8px}.daily-table-wrap{max-height:min(62vh,720px);overflow:auto}.daily-table thead th{position:sticky;top:0;z-index:2}.daily-table td:first-child strong{display:inline-grid;place-items:center;min-width:34px;height:30px;border-radius:9px;background:color-mix(in srgb,var(--blue) 10%,transparent);color:#c6d9ff}.daily-table td:first-child small{margin-top:3px}
      .loading .reload-icon{animation:spin .8s linear infinite}.loading .panel,.loading .metric,.loading .outage-alert{transition:opacity .2s ease;opacity:.82}
      @keyframes growbar{from{transform:scaleY(.05);opacity:.35}to{transform:scaleY(1);opacity:1}}@keyframes spin{to{transform:rotate(360deg)}}@keyframes outagePulse{0%,100%{transform:scale(1)}50%{transform:scale(1.05);box-shadow:0 10px 34px rgba(255,174,48,.36)}}
      @media(max-width:1100px){.topbar-inner{align-items:flex-start;flex-direction:column}.toolbar{width:100%;grid-template-columns:1.3fr .8fr auto}}
      @media(max-width:760px){.brand p{white-space:normal}.month-head{align-items:flex-start;flex-direction:column}.period-controls{width:100%;grid-template-columns:repeat(2,minmax(0,1fr))}.panel-head p{max-width:68ch}.stats-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.stat-average{grid-column:1/-1}.outage-alert-head{align-items:flex-start}.month-bar-group{grid-template-rows:36px 1fr 20px}.cost-value{transform:rotate(-48deg);font-size:7px}.series-bar{width:86%}.bar-col{grid-template-rows:34px 1fr 22px}.bar-value{font-size:7px;overflow:visible;text-overflow:clip;transform:rotate(-58deg);transform-origin:50% 100%;justify-self:center;align-self:end}}
      @media(max-width:520px){.topbar-inner{padding:12px 10px;gap:12px}.brand{gap:10px}.brand img{width:50px;height:50px;border-radius:15px}.brand h1{font-size:21px}.brand p{font-size:11px;line-height:1.35}.toolbar{grid-template-columns:1fr 1fr}.reload-btn{grid-column:1/-1;height:40px}.control select{height:40px;padding:0 9px}.control>span{font-size:10px}main{padding:10px}.status-row{gap:7px}.customer-meta{width:100%}.metric-grid{gap:8px}.metric{padding:11px;gap:8px;min-height:92px}.metric-icon{width:34px;height:34px;border-radius:10px}.metric strong{font-size:17px}.metric span{font-size:11px}.metric small{font-size:10px}.panel{padding:11px;border-radius:15px}.outage-alert{border-radius:15px;padding:11px}.outage-bolt{width:40px;height:40px}.outage-alert-head{margin-bottom:9px}.outage-item{padding:9px}.stats-grid{gap:6px}.stat-card{padding:9px}.chart-frame{height:260px}.daily-chart-frame{height:250px}.year-chart-legend{gap:6px 10px;font-size:10px}.monthly-bars{gap:2px}.month-bar-group{grid-template-rows:38px 1fr 18px;gap:3px}.month-value{font-size:6.7px}.cost-value{transform:rotate(-55deg)}.month-bar-tracks{gap:2px;min-height:110px}.series-bar{width:90%;min-width:2px;border-radius:5px 5px 2px 2px}.daily-bars{gap:1px}.bar-col{grid-template-rows:38px 1fr 18px}.bar-value{font-size:6.4px;transform:rotate(-64deg)}.bar-label{font-size:8px}.bar{width:72%;min-width:3px;border-radius:6px 6px 2px 2px}th,td{padding:9px 6px;font-size:11px}.daily-table th:nth-child(1){width:25%}.daily-table th:nth-child(2){width:38%}.daily-table th:nth-child(3){width:37%}.daily-table td:first-child strong{min-width:28px;height:26px}.year-badge{min-width:52px;padding:5px 8px}.panel-head h2{font-size:17px}.panel-head p{font-size:11px}.period-controls{gap:6px}.mini-control select{height:38px;padding:0 8px}.chart-tooltip{font-size:11.5px;padding:8px 10px}}
      @media(max-width:360px){.brand h1{font-size:19px}.metric-grid{grid-template-columns:1fr}.metric{min-height:80px}.stats-grid{grid-template-columns:1fr}.stat-average{grid-column:auto}.chart-frame{height:245px}.daily-chart-frame{height:235px}.month-bar-group{grid-template-rows:36px 1fr 18px}.month-value{font-size:6px}.cost-value{transform:rotate(-62deg)}.month-bar-tracks{min-height:105px}.bar-col{grid-template-rows:40px 1fr 18px}.bar-value{font-size:5.7px;transform:rotate(-70deg)}th,td{font-size:10.5px;padding:8px 4px}.outage-count{display:none}}
      @media(prefers-reduced-motion:reduce){*,*:before,*:after{animation:none!important;transition:none!important;scroll-behavior:auto!important}}
    `;
  }
}

if (!customElements.get("evn-cskh-monitor-panel")) {
  customElements.define("evn-cskh-monitor-panel", EVNCSKHMonitorPanel);
}
