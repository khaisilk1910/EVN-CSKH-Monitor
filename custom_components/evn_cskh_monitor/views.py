"""Authenticated HTTP API used by the EVN CSKH Monitor WebUI."""

from __future__ import annotations

import json
import math
from typing import Any

from aiohttp import web

from homeassistant.components.http import HomeAssistantView, require_admin
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .calculations import future_outages, parse_iso_date
from .const import (
    API_URL_PREFIX,
    CONF_CUSTOMER_ID,
    CONF_NGAYDAUKY,
    CONF_WEBUI_AVERAGE_MIN_KWH,
    CONF_WEBUI_SUBTITLE,
    CONF_WEBUI_THEME,
    CONF_WEBUI_TITLE,
    DEFAULT_NGAYDAUKY,
    DOMAIN,
    NAME,
    WEBUI_THEMES,
)
from .naming import device_display_name
from .webui_settings import (
    WEBUI_SUBTITLE_MAX_LENGTH,
    WEBUI_TITLE_MAX_LENGTH,
    webui_settings_manager,
)


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
        settings = webui_settings_manager(hass).as_dict()
        user = request["hass_user"]
        accounts: list[dict[str, Any]] = []
        for entry in hass.config_entries.async_entries(DOMAIN):
            customer_id = str(entry.data.get(CONF_CUSTOMER_ID, "")).strip().upper()
            if not customer_id:
                continue
            runtime = getattr(entry, "runtime_data", None)
            customer = runtime.coordinator.data.get("customer", {}) if runtime else {}
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
                    # Kept on each account for backwards compatibility with
                    # older copies of panel.js; values are now domain-wide.
                    "webui_title": settings[CONF_WEBUI_TITLE],
                    "webui_subtitle": settings[CONF_WEBUI_SUBTITLE],
                    "webui_theme": settings[CONF_WEBUI_THEME],
                }
            )
        accounts.sort(key=lambda item: str(item["name"]).casefold())
        return web.json_response(
            {
                "accounts": accounts,
                "webui": settings,
                "webui_themes": list(WEBUI_THEMES),
                "can_edit_webui": bool(user.is_admin),
                # Kept for backwards compatibility with the prerelease WebUI.
                "accounts_json": json.dumps(accounts, ensure_ascii=False),
            }
        )


class EVNWebUISettingsView(HomeAssistantView):
    """Read/update one global WebUI configuration for the whole integration."""

    url = f"{API_URL_PREFIX}/webui-settings"
    name = "api:evn_cskh_monitor:webui_settings"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        user = request["hass_user"]
        return web.json_response(
            {
                "settings": webui_settings_manager(hass).as_dict(),
                "themes": list(WEBUI_THEMES),
                "can_edit": bool(user.is_admin),
            }
        )

    @require_admin
    async def post(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]

        try:
            payload = await request.json()
        except (json.JSONDecodeError, ValueError):
            return web.json_response({"error": "invalid_json"}, status=400)
        if not isinstance(payload, dict):
            return web.json_response({"error": "invalid_payload"}, status=400)

        manager = webui_settings_manager(hass)
        merged: dict[str, Any] = manager.as_dict()
        for key in (CONF_WEBUI_TITLE, CONF_WEBUI_SUBTITLE, CONF_WEBUI_THEME):
            if key not in payload:
                continue
            if not isinstance(payload[key], str):
                return web.json_response({"error": f"invalid_{key}"}, status=400)
            merged[key] = payload[key]

        if CONF_WEBUI_AVERAGE_MIN_KWH in payload:
            try:
                average_min_kwh = float(payload[CONF_WEBUI_AVERAGE_MIN_KWH])
            except (TypeError, ValueError):
                return web.json_response({"error": "invalid_average_min_kwh"}, status=400)
            if not math.isfinite(average_min_kwh) or not 0 <= average_min_kwh <= 100000:
                return web.json_response({"error": "invalid_average_min_kwh"}, status=400)
            merged[CONF_WEBUI_AVERAGE_MIN_KWH] = average_min_kwh

        title = str(merged.get(CONF_WEBUI_TITLE, "")).strip()
        subtitle = str(merged.get(CONF_WEBUI_SUBTITLE, "")).strip()
        theme = str(merged.get(CONF_WEBUI_THEME, "")).strip()
        if len(title) > WEBUI_TITLE_MAX_LENGTH:
            return web.json_response({"error": "title_too_long"}, status=400)
        if len(subtitle) > WEBUI_SUBTITLE_MAX_LENGTH:
            return web.json_response({"error": "subtitle_too_long"}, status=400)
        if theme not in WEBUI_THEMES:
            return web.json_response({"error": "invalid_theme"}, status=400)

        settings = await manager.async_update(merged)
        return web.json_response({"settings": settings, "can_edit": True})


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
        settings = webui_settings_manager(hass).as_dict()
        customer.update(
            {
                "device_name": device_display_name(hass, entry, account),
                "webui_title": settings[CONF_WEBUI_TITLE],
                "webui_subtitle": settings[CONF_WEBUI_SUBTITLE],
                "webui_theme": settings[CONF_WEBUI_THEME],
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
            }
        )


def async_register_views(hass: HomeAssistant) -> None:
    """Register integration API views once during domain setup."""
    hass.http.register_view(EVNPingView)
    hass.http.register_view(EVNOptionsView)
    hass.http.register_view(EVNWebUISettingsView)
    hass.http.register_view(EVNMonthlyDataView)
    hass.http.register_view(EVNDailyDataView)
    hass.http.register_view(EVNSummaryView)
