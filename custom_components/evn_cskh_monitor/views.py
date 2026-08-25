"""Authenticated HTTP API used by the EVN CSKH Monitor WebUI."""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .calculations import future_outages, parse_iso_date
from .const import (
    API_URL_PREFIX,
    CONF_CUSTOMER_ID,
    CONF_NGAYDAUKY,
    CONF_WEBUI_SUBTITLE,
    CONF_WEBUI_TITLE,
    DEFAULT_NGAYDAUKY,
    DEFAULT_WEBUI_SUBTITLE,
    DEFAULT_WEBUI_TITLE,
    DOMAIN,
    NAME,
)
from .invoice import detect_invoice_type
from .naming import device_display_name


def _runtime_for_account(hass: HomeAssistant, account: str):
    normalized = account.strip().upper()
    for entry in hass.config_entries.async_entries(DOMAIN):
        if str(entry.data.get(CONF_CUSTOMER_ID, "")).strip().upper() != normalized:
            continue
        runtime = getattr(entry, "runtime_data", None)
        if runtime is not None:
            return entry, runtime
    return None, None


def _json_number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _invoice_files(
    data_dir: Path,
    customer_id: str,
    monthly: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """List validated invoice files directly from the private data folder.

    Do not depend on a monthly DB row being present: some EVN regions can expose
    the official attachment through notifications before the billing endpoint is
    populated. The function runs in Home Assistant's executor.
    """
    del monthly  # Kept in the signature for compatibility with older callers.
    pattern = re.compile(
        rf"^{re.escape(customer_id)}_(0?[1-9]|1[0-2])_(20\d{{2}})\.(pdf|png)$",
        re.IGNORECASE,
    )
    files: list[dict[str, Any]] = []
    if not data_dir.is_dir():
        return files
    for path in data_dir.iterdir():
        if not path.is_file():
            continue
        match = pattern.fullmatch(path.name)
        if not match:
            continue
        month = int(match.group(1))
        year = int(match.group(2))
        ext = match.group(3).lower()
        try:
            stat = path.stat()
            if stat.st_size <= 0:
                continue
            with path.open("rb") as handle:
                if detect_invoice_type(handle.read(64)) != ext:
                    continue
        except OSError:
            continue
        files.append(
            {
                "month": month,
                "year": year,
                "type": ext,
                "name": path.name,
                "size": stat.st_size,
            }
        )
    files.sort(key=lambda item: (item["year"], item["month"], item["type"]), reverse=True)
    return files


def _webui_settings(entry) -> tuple[str, str]:
    title = str(entry.options.get(CONF_WEBUI_TITLE, DEFAULT_WEBUI_TITLE)).strip()
    subtitle = str(entry.options.get(CONF_WEBUI_SUBTITLE, DEFAULT_WEBUI_SUBTITLE)).strip()
    return title or DEFAULT_WEBUI_TITLE, subtitle


class EVNPingView(HomeAssistantView):
    url = f"{API_URL_PREFIX}/ping"
    name = "api:evn_cskh_monitor:ping"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "integration": NAME})


class EVNOptionsView(HomeAssistantView):
    url = f"{API_URL_PREFIX}/options"
    name = "api:evn_cskh_monitor:options"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        accounts: list[dict[str, Any]] = []
        for entry in hass.config_entries.async_entries(DOMAIN):
            customer_id = str(entry.data.get(CONF_CUSTOMER_ID, "")).strip().upper()
            if not customer_id:
                continue
            runtime = getattr(entry, "runtime_data", None)
            customer = runtime.coordinator.data.get("customer", {}) if runtime else {}
            webui_title, webui_subtitle = _webui_settings(entry)
            accounts.append(
                {
                    "id": customer_id,
                    "customer_id": customer_id,
                    "userevn": customer_id,
                    "name": device_display_name(hass, entry, customer_id),
                    "evn_customer_name": customer.get("name"),
                    "region": customer.get("region") or entry.data.get("region"),
                    "billing_start": int(
                        entry.options.get(
                            CONF_NGAYDAUKY,
                            entry.data.get(CONF_NGAYDAUKY, DEFAULT_NGAYDAUKY),
                        )
                    ),
                    "webui_title": webui_title,
                    "webui_subtitle": webui_subtitle,
                }
            )
        accounts.sort(key=lambda item: str(item["name"]).casefold())
        return web.json_response(
            {
                "accounts": accounts,
                # Kept for backwards compatibility with the prerelease WebUI.
                "accounts_json": json.dumps(accounts, ensure_ascii=False),
            }
        )


class EVNMonthlyDataView(HomeAssistantView):
    url = f"{API_URL_PREFIX}/monthly/{{account}}"
    name = "api:evn_cskh_monitor:monthly"
    requires_auth = True

    async def get(self, request: web.Request, account: str) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        _, runtime = _runtime_for_account(hass, account)
        if runtime is None:
            return web.json_response({"error": "account_not_found"}, status=404)
        rows = list(runtime.coordinator.data.get("monthly", []))
        return web.json_response(
            {
                "SanLuong": [
                    {
                        "Tháng": int(row.get("month") or 0),
                        "Năm": int(row.get("year") or 0),
                        "Điện tiêu thụ (KWh)": _json_number(row.get("consumption")),
                        "Nguồn": row.get("source"),
                    }
                    for row in rows
                ],
                "TienDien": [
                    {
                        "Tháng": int(row.get("month") or 0),
                        "Năm": int(row.get("year") or 0),
                        "Tiền Điện": _json_number(row.get("cost")),
                        "Trạng thái": row.get("status"),
                        "Nguồn": row.get("source"),
                    }
                    for row in rows
                ],
            }
        )


class EVNDailyDataView(HomeAssistantView):
    url = f"{API_URL_PREFIX}/daily/{{account}}"
    name = "api:evn_cskh_monitor:daily"
    requires_auth = True

    async def get(self, request: web.Request, account: str) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        _, runtime = _runtime_for_account(hass, account)
        if runtime is None:
            return web.json_response({"error": "account_not_found"}, status=404)
        return web.json_response(
            [
                {
                    "Ngày": row.get("date_display"),
                    "Ngày ISO": row.get("date"),
                    "Điện tiêu thụ (kWh)": _json_number(row.get("consumption")),
                    "CHISO": _json_number(row.get("reading")),
                    # Tiered tariffs do not permit an exact per-day allocation.
                    "Tiền điện (VND)": None,
                }
                for row in runtime.coordinator.data.get("daily", [])
            ]
        )


class EVNSummaryView(HomeAssistantView):
    url = f"{API_URL_PREFIX}/summary/{{account}}"
    name = "api:evn_cskh_monitor:summary"
    requires_auth = True

    async def get(self, request: web.Request, account: str) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        entry, runtime = _runtime_for_account(hass, account)
        if runtime is None or entry is None:
            return web.json_response({"error": "account_not_found"}, status=404)

        snapshot = runtime.coordinator.data
        customer = dict(snapshot.get("customer", {}))
        webui_title, webui_subtitle = _webui_settings(entry)
        customer.update(
            {
                "device_name": device_display_name(hass, entry, account),
                "webui_title": webui_title,
                "webui_subtitle": webui_subtitle,
            }
        )

        daily = list(snapshot.get("daily", []))
        monthly = list(snapshot.get("monthly", []))
        valid_daily = [
            row
            for row in daily
            if row.get("consumption") is not None and parse_iso_date(row.get("date"))
        ]
        total_kwh = sum(float(row["consumption"]) for row in valid_daily)
        official = [
            row
            for row in monthly
            if row.get("cost") is not None and row.get("source") == "invoice"
        ]
        official_costs = [float(row["cost"]) for row in official]
        dates = sorted(
            item
            for item in (parse_iso_date(row.get("date")) for row in valid_daily)
            if item is not None
        )
        expected_days = (dates[-1] - dates[0]).days + 1 if dates else 0
        coverage = (len(valid_daily) / expected_days * 100) if expected_days else 0
        peak = max(valid_daily, key=lambda row: float(row["consumption"]), default=None)
        low = min(valid_daily, key=lambda row: float(row["consumption"]), default=None)
        files = await hass.async_add_executor_job(
            _invoice_files, runtime.data_dir, account, monthly
        )

        return web.json_response(
            {
                "customer": customer,
                "last_sync": snapshot.get("last_sync"),
                "partial_errors": snapshot.get("partial_errors", []),
                "daily": {
                    "records": len(daily),
                    "valid_records": len(valid_daily),
                    "first_date": dates[0].isoformat() if dates else None,
                    "last_date": dates[-1].isoformat() if dates else None,
                    "expected_days": expected_days,
                    "coverage_percent": round(coverage, 2),
                    "total_kwh": round(total_kwh, 3),
                    "average_kwh": (
                        round(total_kwh / len(valid_daily), 3) if valid_daily else None
                    ),
                    "peak": peak,
                    "lowest": low,
                },
                "monthly": {
                    "records": len(monthly),
                    "official_invoice_count": len(official),
                    "official_cost_total": (
                        round(sum(official_costs), 2) if official_costs else None
                    ),
                },
                "debt": snapshot.get("debt", {}),
                "outage_count": len(snapshot.get("outages", [])),
                "outages": [
                    {key: value for key, value in item.items() if key != "_date"}
                    for item in future_outages(snapshot, dt_util.now().date())
                ],
                "notification_count": len(snapshot.get("notifications", [])),
                "raw_server_record_count": int(snapshot.get("raw_record_count", 0)),
                "invoice_files": files,
            }
        )


def async_register_views(hass: HomeAssistant) -> None:
    """Register integration API views once during domain setup."""
    hass.http.register_view(EVNPingView)
    hass.http.register_view(EVNOptionsView)
    hass.http.register_view(EVNMonthlyDataView)
    hass.http.register_view(EVNDailyDataView)
    hass.http.register_view(EVNSummaryView)
