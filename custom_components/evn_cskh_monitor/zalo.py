"""Optional Zalo Bot notifications for EVN CSKH Monitor."""

from __future__ import annotations

import asyncio
from datetime import timedelta
import hashlib
import logging
from pathlib import Path
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .calculations import (
    consumption_on,
    format_number,
    future_outages,
    period_chain,
    period_consumption,
    period_cost,
)
from .const import (
    CONF_CUSTOMER_ID,
    CONF_NGAYDAUKY,
    CONF_ZALO_ACCOUNT_SELECTION,
    CONF_ZALO_SEND_DAILY,
    CONF_ZALO_SEND_INVOICE,
    CONF_ZALO_SEND_OUTAGE,
    CONF_ZALO_THREAD_ID,
    CONF_ZALO_TYPE,
    DEFAULT_NGAYDAUKY,
    DEFAULT_ZALO_TYPE,
)
from .database import EVNDatabase

_LOGGER = logging.getLogger(__name__)
ZALO_DOMAIN = "zalo_bot"


class ZaloNotifier:
    """Send deduplicated Zalo notifications without blocking HA startup."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        database: EVNDatabase,
        data_dir: Path,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.database = database
        self.data_dir = data_dir
        self.customer_id = str(entry.data[CONF_CUSTOMER_ID])
        self._process_lock = asyncio.Lock()

    @property
    def _options(self) -> dict[str, Any]:
        return dict(self.entry.options)

    def _configured(self) -> bool:
        options = self._options
        return bool(
            str(options.get(CONF_ZALO_ACCOUNT_SELECTION, "")).strip()
            and str(options.get(CONF_ZALO_THREAD_ID, "")).strip()
        )

    def _base_data(self) -> dict[str, Any]:
        options = self._options
        return {
            "type": int(options.get(CONF_ZALO_TYPE, DEFAULT_ZALO_TYPE)),
            "account_selection": str(options.get(CONF_ZALO_ACCOUNT_SELECTION, "")).strip(),
            "thread_id": str(options.get(CONF_ZALO_THREAD_ID, "")).strip(),
        }

    async def async_process(self, snapshot: dict[str, Any]) -> None:
        """Process enabled notification types without overlapping service calls."""
        if not self._configured():
            return
        async with self._process_lock:
            if self._options.get(CONF_ZALO_SEND_INVOICE, False):
                await self._async_send_invoice_files(snapshot)
            if self._options.get(CONF_ZALO_SEND_DAILY, False):
                await self._async_send_daily(snapshot)
            if self._options.get(CONF_ZALO_SEND_OUTAGE, False):
                await self._async_send_outage(snapshot)

    async def _async_service_call(self, service: str, data: dict[str, Any]) -> bool:
        if not self.hass.services.has_service(ZALO_DOMAIN, service):
            _LOGGER.debug("Zalo Bot service %s.%s is not available", ZALO_DOMAIN, service)
            return False
        try:
            async with asyncio.timeout(45):
                await self.hass.services.async_call(
                    ZALO_DOMAIN,
                    service,
                    data,
                    blocking=True,
                )
            return True
        except Exception as err:  # noqa: BLE001 - third-party service errors vary
            _LOGGER.warning("Could not call %s.%s: %s", ZALO_DOMAIN, service, err)
            return False

    async def _get_state(self, key: str) -> str | None:
        return await self.hass.async_add_executor_job(
            self.database.get_state, self.customer_id, key
        )

    async def _set_state(self, key: str, value: str) -> None:
        await self.hass.async_add_executor_job(
            self.database.set_state, self.customer_id, key, value
        )

    async def _async_send_invoice_files(self, snapshot: dict[str, Any]) -> None:
        files = await self.hass.async_add_executor_job(self._scan_invoice_files, snapshot)
        for kind, month, year, path in files:
            key = f"zalo_invoice_{kind}_{month}_{year}"
            fingerprint = await self.hass.async_add_executor_job(_file_fingerprint, path)
            if await self._get_state(key) == fingerprint:
                continue

            if kind == "png":
                data = {
                    **self._base_data(),
                    "image_path": str(path),
                    "message": f"Hóa đơn tháng {month}/{year} của công tơ {self.customer_id}",
                }
                success = await self._async_service_call("send_image", data)
            else:
                data = {
                    **self._base_data(),
                    "file_path_or_url": str(path),
                    "message": f"Chi tiết tiền điện tháng {month}/{year} công tơ {self.customer_id}",
                }
                success = await self._async_service_call("send_file", data)

            if success:
                await self._set_state(key, fingerprint)

    def _scan_invoice_files(self, snapshot: dict[str, Any]) -> list[tuple[str, int, int, Path]]:
        found: list[tuple[str, int, int, Path]] = []
        # Newest first so recent invoices are sent before historical files.
        months = sorted(
            snapshot.get("monthly", []),
            key=lambda row: (int(row.get("year") or 0), int(row.get("month") or 0)),
            reverse=True,
        )
        for row in months:
            month = int(row.get("month") or 0)
            year = int(row.get("year") or 0)
            if not (1 <= month <= 12 and year > 2000):
                continue
            for kind in ("png", "pdf"):
                path = self.data_dir / f"{self.customer_id}_{month}_{year}.{kind}"
                if path.is_file() and path.stat().st_size > 0:
                    found.append((kind, month, year, path))
        return found

    async def _async_send_daily(self, snapshot: dict[str, Any]) -> None:
        now = dt_util.now()
        yesterday = now.date() - timedelta(days=1)
        yesterday_value = consumption_on(snapshot, yesterday)
        if yesterday_value is None:
            return

        fingerprint = f"{yesterday.isoformat()}:{round(yesterday_value, 3)}"
        if await self._get_state("zalo_daily") == fingerprint:
            return

        day_before = yesterday - timedelta(days=1)
        day_before_value = consumption_on(snapshot, day_before)
        billing_start = int(
            self._options.get(
                CONF_NGAYDAUKY,
                self.entry.data.get(CONF_NGAYDAUKY, DEFAULT_NGAYDAUKY),
            )
        )
        periods = period_chain(billing_start, now.date())
        current_start, _ = periods[0]
        prev_start, prev_end = periods[1]
        prev2_start, prev2_end = periods[2]

        current_kwh = period_consumption(snapshot, current_start, now.date())
        current_cost, current_cost_meta = period_cost(
            snapshot, current_start, now.date(), billing_start
        )
        prev_kwh = period_consumption(snapshot, prev_start, prev_end)
        prev_cost, prev_cost_meta = period_cost(snapshot, prev_start, prev_end, billing_start)
        prev2_kwh = period_consumption(snapshot, prev2_start, prev2_end)
        prev2_cost, prev2_cost_meta = period_cost(
            snapshot, prev2_start, prev2_end, billing_start
        )

        message = (
            f"🚨 Công tơ {self.customer_id}:\n\n"
            f"📈 Sản lượng hôm qua: {_format_kwh(yesterday_value)}.\n"
            f"📈 Sản lượng hôm kia: {_format_kwh(day_before_value)}.\n\n"
            "━━━━━━━━━━━━\n"
            f"📊 Sản lượng kỳ này: {_format_kwh(current_kwh)}.\n"
            f"💸 Tiền điện kỳ này{_estimate_suffix(current_cost_meta)}: {_format_vnd(current_cost)}.\n\n"
            "━━━━━━━━━━━━\n"
            f"📊 Sản lượng kỳ trước: {_format_kwh(prev_kwh)}.\n"
            f"💸 Tiền điện kỳ trước{_estimate_suffix(prev_cost_meta)}: {_format_vnd(prev_cost)}.\n\n"
            "━━━━━━━━━━━━\n"
            f"📊 Sản lượng kỳ trước nữa: {_format_kwh(prev2_kwh)}.\n"
            f"💸 Tiền điện kỳ trước nữa{_estimate_suffix(prev2_cost_meta)}: {_format_vnd(prev2_cost)}.\n"
            f"🕒 {now.strftime('%H:%M:%S %A %d-%b-%Y')}."
        )
        success = await self._async_service_call(
            "send_message", {**self._base_data(), "message": message}
        )
        if success:
            await self._set_state("zalo_daily", fingerprint)

    async def _async_send_outage(self, snapshot: dict[str, Any]) -> None:
        events = future_outages(snapshot, dt_util.now().date())
        if not events:
            return
        event = events[0]
        raw = "|".join(
            str(event.get(key) or "")
            for key in ("start_date", "start_time", "end_time", "area", "reason")
        )
        fingerprint = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        if await self._get_state("zalo_outage") == fingerprint:
            return

        message = (
            f"⚡ Lịch cắt điện - {self.customer_id}\n\n"
            f"📅 Ngày: {event.get('start_date') or ''}\n"
            f"🕒 Thời gian: {event.get('start_time') or ''} - {event.get('end_time') or ''}\n"
            f"📍 Khu vực: {event.get('area') or 'Không có thông tin'}\n"
            f"📝 Lý do: {event.get('reason') or 'Không có thông tin'}"
        )
        success = await self._async_service_call(
            "send_message", {**self._base_data(), "message": message}
        )
        if success:
            await self._set_state("zalo_outage", fingerprint)


def _file_fingerprint(path: Path) -> str:
    stat = path.stat()
    return f"{stat.st_size}:{stat.st_mtime_ns}"


def _format_kwh(value: float | int | None) -> str:
    """Format energy without turning missing server data into a fake zero."""
    if value is None:
        return "chưa có dữ liệu"
    return f"{format_number(value)} kWh"


def _format_vnd(value: float | int | None) -> str:
    """Format money without turning missing server data into a fake zero."""
    if value is None:
        return "chưa có dữ liệu"
    return f"{int(round(float(value))):,} đ"


def _estimate_suffix(meta: dict[str, Any]) -> str:
    """Label local tariff calculations separately from official EVN invoice money."""
    return " (ước tính)" if meta.get("estimated") else ""
