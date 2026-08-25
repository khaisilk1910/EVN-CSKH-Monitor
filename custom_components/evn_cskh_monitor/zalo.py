"""Optional Zalo Bot notifications for EVN CSKH Monitor."""

from __future__ import annotations

import asyncio
from datetime import timedelta
import hashlib
import logging
from pathlib import Path
import re
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
from .const import CONF_CUSTOMER_ID, CONF_NGAYDAUKY, DEFAULT_NGAYDAUKY
from .database import EVNDatabase
from .invoice import detect_invoice_type
from .naming import device_display_name
from .zalo_config import normalize_zalo_recipients

_LOGGER = logging.getLogger(__name__)
ZALO_DOMAIN = "zalo_bot"


class ZaloNotifier:
    """Send deduplicated notifications to any number of Zalo destinations."""

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

    def _recipients(self) -> list[dict[str, Any]]:
        return [
            item
            for item in normalize_zalo_recipients(self._options)
            if item.get("enabled", True)
        ]

    @staticmethod
    def _base_data(recipient: dict[str, Any]) -> dict[str, Any]:
        return {
            "type": int(recipient["type"]),
            "account_selection": str(recipient["account_selection"]),
            "thread_id": str(recipient["thread_id"]),
        }

    def _display_name(self) -> str:
        """Return the latest user-visible Home Assistant device name."""
        return device_display_name(self.hass, self.entry, self.customer_id)

    async def async_seed_all(self, snapshot: dict[str, Any]) -> None:
        """Mark the current snapshot as seen without sending any Zalo message.

        This is used after the initial historical import, and also guarantees a
        newly configured destination never receives a flood of old invoices,
        old outages, or yesterday's already-known production value.
        """
        recipients = self._recipients()
        if not recipients:
            return
        async with self._process_lock:
            for recipient in recipients:
                await self._async_seed_recipient(recipient, snapshot, force=True)

    async def async_seed_invoice_files(self, snapshot: dict[str, Any]) -> None:
        """Baseline only invoice file fingerprints for all enabled routes.

        Used by historical/raw-response recovery so an upgrade can recover old
        PDF/PNG files without replaying them as new Zalo notifications.
        """
        recipients = self._recipients()
        if not recipients:
            return
        async with self._process_lock:
            files = await self.hass.async_add_executor_job(
                self._scan_invoice_files, snapshot
            )
            for recipient in recipients:
                for kind, month, year, path in files:
                    fingerprint = await self.hass.async_add_executor_job(
                        _file_fingerprint, path
                    )
                    await self._set_state(
                        self._recipient_key(
                            recipient, f"invoice_{kind}_{month}_{year}"
                        ),
                        fingerprint,
                    )

    async def async_process(self, snapshot: dict[str, Any]) -> None:
        """Process enabled notification types without overlapping service calls."""
        recipients = self._recipients()
        if not recipients:
            return
        async with self._process_lock:
            for recipient in recipients:
                initialized = await self._async_seed_recipient(
                    recipient, snapshot, force=False
                )
                if not initialized:
                    # First observation is baseline-only by design.
                    continue
                if recipient.get("send_invoice", False):
                    await self._async_send_invoice_files(recipient, snapshot)
                if recipient.get("send_daily", False):
                    await self._async_send_daily(recipient, snapshot)
                if recipient.get("send_outage", False):
                    await self._async_send_outage(recipient, snapshot)

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
        except asyncio.CancelledError:
            raise
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

    @staticmethod
    def _recipient_key(recipient: dict[str, Any], suffix: str) -> str:
        recipient_id = str(recipient["id"])
        return f"zalo_{recipient_id}_{suffix}"

    async def _async_seed_recipient(
        self,
        recipient: dict[str, Any],
        snapshot: dict[str, Any],
        *,
        force: bool,
    ) -> bool:
        """Seed dedupe state; return True only if it had already been initialized."""
        init_key = self._recipient_key(recipient, "initialized")
        already_initialized = await self._get_state(init_key) == "1"
        if already_initialized and not force:
            return True

        files = await self.hass.async_add_executor_job(self._scan_invoice_files, snapshot)
        for kind, month, year, path in files:
            fingerprint = await self.hass.async_add_executor_job(_file_fingerprint, path)
            await self._set_state(
                self._recipient_key(recipient, f"invoice_{kind}_{month}_{year}"),
                fingerprint,
            )

        daily_fingerprint = self._daily_fingerprint(snapshot)
        if daily_fingerprint is not None:
            await self._set_state(
                self._recipient_key(recipient, "daily"), daily_fingerprint
            )

        outage_fingerprint = self._outage_fingerprint(snapshot)
        if outage_fingerprint is not None:
            await self._set_state(
                self._recipient_key(recipient, "outage"), outage_fingerprint
            )

        await self._set_state(init_key, "1")
        return already_initialized

    async def _async_send_invoice_files(
        self, recipient: dict[str, Any], snapshot: dict[str, Any]
    ) -> None:
        files = await self.hass.async_add_executor_job(self._scan_invoice_files, snapshot)
        for kind, month, year, path in files:
            key = self._recipient_key(recipient, f"invoice_{kind}_{month}_{year}")
            fingerprint = await self.hass.async_add_executor_job(_file_fingerprint, path)
            if await self._get_state(key) == fingerprint:
                continue

            if kind == "png":
                data = {
                    **self._base_data(recipient),
                    "image_path": str(path),
                    "message": f"Hóa đơn tháng {month}/{year} của {self._display_name()}",
                }
                success = await self._async_service_call("send_image", data)
            else:
                data = {
                    **self._base_data(recipient),
                    "file_path_or_url": str(path),
                    "message": f"Chi tiết tiền điện tháng {month}/{year} của {self._display_name()}",
                }
                success = await self._async_service_call("send_file", data)

            if success:
                await self._set_state(key, fingerprint)

    def _scan_invoice_files(
        self, snapshot: dict[str, Any]
    ) -> list[tuple[str, int, int, Path]]:
        """Return validated invoice files, newest first.

        Files are discovered from /config/evncskh directly rather than from the
        monthly snapshot. This handles regions where an attachment arrives in a
        notification before the corresponding bill row is available.
        """
        del snapshot  # Signature retained for the existing executor call sites.
        pattern = re.compile(
            rf"^{re.escape(self.customer_id)}_(0?[1-9]|1[0-2])_(20\d{{2}})\.(pdf|png)$",
            re.IGNORECASE,
        )
        found: list[tuple[str, int, int, Path]] = []
        if not self.data_dir.is_dir():
            return found
        for path in self.data_dir.iterdir():
            if not path.is_file():
                continue
            match = pattern.fullmatch(path.name)
            if not match:
                continue
            month = int(match.group(1))
            year = int(match.group(2))
            kind = match.group(3).lower()
            try:
                if path.stat().st_size <= 0:
                    continue
                with path.open("rb") as handle:
                    if detect_invoice_type(handle.read(64)) != kind:
                        continue
            except OSError:
                continue
            found.append((kind, month, year, path))
        found.sort(key=lambda item: (item[2], item[1], item[0]), reverse=True)
        return found

    @staticmethod
    def _daily_fingerprint(snapshot: dict[str, Any]) -> str | None:
        now = dt_util.now()
        yesterday = now.date() - timedelta(days=1)
        value = consumption_on(snapshot, yesterday)
        if value is None:
            return None
        return f"{yesterday.isoformat()}:{round(value, 3)}"

    @staticmethod
    def _outage_fingerprint(snapshot: dict[str, Any]) -> str | None:
        events = future_outages(snapshot, dt_util.now().date())
        if not events:
            return None
        event = events[0]
        raw = "|".join(
            str(event.get(key) or "")
            for key in ("start_date", "start_time", "end_time", "area", "reason")
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    async def _async_send_daily(
        self, recipient: dict[str, Any], snapshot: dict[str, Any]
    ) -> None:
        now = dt_util.now()
        yesterday = now.date() - timedelta(days=1)
        yesterday_value = consumption_on(snapshot, yesterday)
        if yesterday_value is None:
            return

        fingerprint = f"{yesterday.isoformat()}:{round(yesterday_value, 3)}"
        key = self._recipient_key(recipient, "daily")
        if await self._get_state(key) == fingerprint:
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
            f"🚨 {self._display_name()}:\n\n"
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
            "send_message", {**self._base_data(recipient), "message": message}
        )
        if success:
            await self._set_state(key, fingerprint)

    async def _async_send_outage(
        self, recipient: dict[str, Any], snapshot: dict[str, Any]
    ) -> None:
        events = future_outages(snapshot, dt_util.now().date())
        if not events:
            return
        event = events[0]
        fingerprint = self._outage_fingerprint(snapshot)
        if fingerprint is None:
            return
        key = self._recipient_key(recipient, "outage")
        if await self._get_state(key) == fingerprint:
            return

        message = (
            f"⚡ Lịch cắt điện - {self._display_name()}\n\n"
            f"📅 Ngày: {event.get('start_date') or ''}\n"
            f"🕒 Thời gian: {event.get('start_time') or ''} - {event.get('end_time') or ''}\n"
            f"📍 Khu vực: {event.get('area') or 'Không có thông tin'}\n"
            f"📝 Lý do: {event.get('reason') or 'Không có thông tin'}"
        )
        success = await self._async_service_call(
            "send_message", {**self._base_data(recipient), "message": message}
        )
        if success:
            await self._set_state(key, fingerprint)


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
