"""Data coordinator for EVN CSKH Monitor."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import logging
from pathlib import Path
import re
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    DAILY_BATCH_DAYS,
    DOMAIN,
    FAILED_REFRESH_RETRY_SECONDS,
    HISTORY_BATCH_PAUSE_SECONDS,
    HISTORY_BOOTSTRAP_DELAY_SECONDS,
    HISTORY_MONTH_PAUSE_SECONDS,
    HISTORY_PREVIOUS_YEARS,
    INVOICES_DIR_NAME,
    MAX_CONCURRENT_EVN_REQUESTS,
    NETWORK_SEMAPHORE_DATA_KEY,
    RECENT_BOOTSTRAP_DAYS,
    REFRESH_WINDOW_DAYS,
    UPDATE_INTERVAL,
)
from .database import EVNDatabase, parse_number
from .evn_api import EVNAPI
from .invoice import (
    decode_base64_payload,
    detect_invoice_type,
    infer_invoice_period,
    is_invoice_notification,
    iter_attachment_candidates,
)
from .zalo import ZaloNotifier

_LOGGER = logging.getLogger(__name__)

_INVOICE_RESCAN_STATE_KEY = "invoice_attachment_rescan_v3"
_INVOICE_HISTORY_STATE_KEY = "invoice_history_period_scan_v2"
_INVOICE_PERIOD_STATE_PREFIX = "invoice_history_period_v2_"
_INVOICE_HISTORY_REGIONS = {"HN", "NPC", "CPC"}


class EVNDataUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinate EVN cloud requests and keep entities on an in-memory snapshot."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        api: EVNAPI,
        database: EVNDatabase,
        data_dir: Path,
        customer_id: str,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"EVN CSKH Monitor {customer_id}",
            update_interval=UPDATE_INTERVAL,
        )
        self.entry = entry
        self.api = api
        self.database = database
        self.data_dir = data_dir
        self.invoice_dir = data_dir / INVOICES_DIR_NAME
        self.customer_id = customer_id
        self.cache_loaded = False
        self._api_lock = asyncio.Lock()
        domain_data = hass.data.setdefault(DOMAIN, {})
        semaphore = domain_data.get(NETWORK_SEMAPHORE_DATA_KEY)
        if semaphore is None:
            semaphore = asyncio.Semaphore(MAX_CONCURRENT_EVN_REQUESTS)
            domain_data[NETWORK_SEMAPHORE_DATA_KEY] = semaphore
        self._network_semaphore: asyncio.Semaphore = semaphore
        self._backfill_lock = asyncio.Lock()
        self._invoice_lock = asyncio.Lock()
        self._invoice_rescan_lock = asyncio.Lock()
        self._invoice_history_lock = asyncio.Lock()
        self._invoice_rescan_complete = False
        self._invoice_history_complete = self.api.region not in _INVOICE_HISTORY_REGIONS
        self._history_backfill_complete = False
        self._zalo_baseline_ready = False
        self.zalo = ZaloNotifier(hass, entry, database, self.invoice_dir)

    async def _async_api_call(self, method: Any, /, *args: Any, **kwargs: Any) -> Any:
        if self.hass.is_stopping:
            raise asyncio.CancelledError
        async with self._network_semaphore:
            return await method(*args, **kwargs)

    async def _async_login_with_retry(self) -> bool:
        for attempt in range(2):
            if await self._async_api_call(self.api.login):
                return True
            if self.api.last_login_auth_failed or self.hass.is_stopping:
                return False
            if attempt == 0:
                await asyncio.sleep(0.75)
        return False

    async def async_initialize(self) -> None:
        """Prepare local storage/cache without making a cloud request."""
        await self.hass.async_add_executor_job(self.database.initialize)
        await self.hass.async_add_executor_job(
            _prepare_invoice_directory, self.data_dir, self.invoice_dir
        )
        self.data = await self.hass.async_add_executor_job(
            self.database.load_snapshot, self.customer_id
        )
        self.data = self._decorate_snapshot(self.data)
        year = dt_util.now().year
        marker = await self.hass.async_add_executor_job(
            self.database.get_state, self.customer_id, "history_bootstrap_year"
        )
        self._history_backfill_complete = marker == str(year)
        self._zalo_baseline_ready = (
            await self.hass.async_add_executor_job(
                self.database.get_state, self.customer_id, "zalo_baseline_ready"
            )
            == "1"
        )
        self._invoice_rescan_complete = (
            await self.hass.async_add_executor_job(
                self.database.get_state, self.customer_id, _INVOICE_RESCAN_STATE_KEY
            )
            == "1"
        )
        if self.api.region in _INVOICE_HISTORY_REGIONS:
            marker = await self.hass.async_add_executor_job(
                self.database.get_state, self.customer_id, _INVOICE_HISTORY_STATE_KEY
            )
            self._invoice_history_complete = marker == _invoice_history_target_marker(dt_util.now())
        self.cache_loaded = True

    async def _async_update_data(self) -> dict[str, Any]:
        async with self._api_lock:
            if not self.api.access_token and not await self._async_login_with_retry():
                if self.api.last_login_auth_failed:
                    raise ConfigEntryAuthFailed("EVN authentication failed")
                raise UpdateFailed(
                    f"EVN login service unavailable: {self.api.last_login_error or 'unknown error'}"
                )

        errors: list[str] = []
        now = dt_util.now()
        daily_ok = False
        try:
            daily_ok = await self._async_refresh_daily(now)
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001
            errors.append(f"daily: {err}")
            _LOGGER.warning("Daily refresh failed for %s: %s", self.customer_id, err)

        current = (now.month, now.year)
        previous = (12, now.year - 1) if now.month == 1 else (now.month - 1, now.year)
        outage_start = now - timedelta(days=30)
        outage_end = now + timedelta(days=60)
        async with self._api_lock:
            results = await asyncio.gather(
                self._async_api_call(self.api.get_chisothang, *current),
                self._async_api_call(self.api.get_chisothang, *previous),
                self._async_api_call(self.api.get_hoadon),
                self._async_api_call(
                    self.api.get_ngungcapdien,
                    outage_start.strftime("%d/%m/%Y"),
                    outage_end.strftime("%d/%m/%Y"),
                ),
                self._async_api_call(self.api.get_thongbao),
                return_exceptions=True,
            )
        if self.api.last_login_auth_failed:
            raise ConfigEntryAuthFailed("EVN authentication failed while refreshing data")
        if not daily_ok and not any(
            item is not None and not isinstance(item, Exception) for item in results
        ):
            raise UpdateFailed(
                "All EVN data endpoints were unavailable",
                retry_after=FAILED_REFRESH_RETRY_SECONDS,
            )

        await self._async_process_monthly_result("monthly_current", results[0], *current, errors)
        await self._async_process_monthly_result("monthly_previous", results[1], *previous, errors)
        # Live/current get_hoadon is the only bill response allowed to update debt.
        await self._async_process_bill_result(results[2], errors, update_debt=True)
        outage_authoritative = await self._async_process_outage_result(
            results[3], errors, outage_start, outage_end
        )
        await self._async_process_notifications_result(
            results[4], errors, allow_outage_fallback=not outage_authoritative
        )

        await self.hass.async_add_executor_job(
            self.database.set_state,
            self.customer_id,
            "last_sync",
            now.isoformat(),
        )
        snapshot = await self.hass.async_add_executor_job(
            self.database.load_snapshot, self.customer_id
        )
        snapshot = self._decorate_snapshot(snapshot)
        snapshot["partial_errors"] = errors

        # All slow recovery work stays in entry-owned background tasks.
        if not self._invoice_rescan_complete and not self._invoice_rescan_lock.locked():
            self.entry.async_create_background_task(
                self.hass,
                self.async_rescan_invoice_attachments(),
                name=f"evn_cskh_monitor invoice rescan {self.customer_id}",
                eager_start=False,
            )
        elif not self._invoice_history_complete and not self._invoice_history_lock.locked():
            self.entry.async_create_background_task(
                self.hass,
                self.async_backfill_invoice_history(),
                name=f"evn_cskh_monitor invoice history {self.customer_id}",
                eager_start=False,
            )
        if not self._history_backfill_complete and not self._backfill_lock.locked():
            self.entry.async_create_background_task(
                self.hass,
                self.async_backfill_history(),
                name=f"evn_cskh_monitor history {self.customer_id}",
                eager_start=False,
            )
        if self._zalo_baseline_ready:
            self.entry.async_create_background_task(
                self.hass,
                self._async_process_zalo(snapshot),
                name=f"evn_cskh_monitor zalo {self.customer_id}",
                eager_start=False,
            )
        return snapshot

    async def _async_process_zalo(self, snapshot: dict[str, Any]) -> None:
        try:
            await self.zalo.async_process(snapshot)
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Zalo processing failed for %s: %s", self.customer_id, err)

    async def _async_refresh_daily(self, now: datetime) -> bool:
        last = await self.hass.async_add_executor_job(
            self.database.get_last_daily_date, self.customer_id
        )
        if last is None:
            fetch_start = now - timedelta(days=RECENT_BOOTSTRAP_DAYS)
        else:
            # Refetch a small overlap because EVN can revise the newest reading.
            fetch_start = max(last - timedelta(days=2), now - timedelta(days=REFRESH_WINDOW_DAYS))
        records: list[dict[str, Any]] = []
        received = False
        current_start = fetch_start
        while current_start.date() <= now.date():
            current_end = min(current_start + timedelta(days=DAILY_BATCH_DAYS - 1), now)
            async with self._api_lock:
                response = await self._async_api_call(
                    self.api.get_chisongay,
                    current_start.strftime("%d/%m/%Y"),
                    current_end.strftime("%d/%m/%Y"),
                )
            if response is not None:
                received = True
                await self._async_save_raw("daily", response)
                payload = response.get("data") if isinstance(response, dict) else None
                if isinstance(payload, list):
                    records.extend(x for x in payload if isinstance(x, dict))
            current_start = current_end + timedelta(days=1)
            await asyncio.sleep(0)
        if records:
            parsed = self._build_daily_rows(records)
            if parsed:
                await self.hass.async_add_executor_job(
                    self.database.save_daily_records, self.customer_id, parsed
                )
                await self.hass.async_add_executor_job(
                    self.database.aggregate_monthly_from_daily, self.customer_id
                )
        return received

    async def async_rescan_invoice_attachments(self) -> None:
        if self._invoice_rescan_complete or self._invoice_rescan_lock.locked():
            return
        async with self._invoice_rescan_lock:
            records = await self.hass.async_add_executor_job(
                self.database.load_invoice_source_records, self.customer_id
            )
            recovered = 0
            for source, payload in records:
                if self.hass.is_stopping:
                    return
                try:
                    fallback = None
                    source_hint = "stored response"
                    resource_base = None
                    match = re.fullmatch(r"history_month_(20\d{2})(0[1-9]|1[0-2])", source)
                    if match:
                        fallback = (int(match.group(2)), int(match.group(1)))
                        resource_base = self.api.monthly_resource_base_url
                    match = re.fullmatch(r"invoice_history_(20\d{2})(0[1-9]|1[0-2])", source)
                    if match:
                        fallback = (int(match.group(2)), int(match.group(1)))
                        source_hint = "stored historical bill"
                        resource_base = self.api.invoice_resource_base_url
                    recovered += await self._async_extract_invoice_files(
                        [payload] if isinstance(payload, dict) else [],
                        fallback_period=fallback,
                        source_hint=source_hint,
                        resource_base_url=resource_base,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as err:  # noqa: BLE001
                    _LOGGER.debug("Stored invoice recovery skipped %s: %s", source, err)
                await asyncio.sleep(0.05)
            await self.hass.async_add_executor_job(
                self.database.set_state,
                self.customer_id,
                _INVOICE_RESCAN_STATE_KEY,
                "1",
            )
            self._invoice_rescan_complete = True
            if not self._invoice_history_complete and not self.hass.is_stopping:
                self.entry.async_create_background_task(
                    self.hass,
                    self.async_backfill_invoice_history(),
                    name=f"evn_cskh_monitor invoice history {self.customer_id}",
                    eager_start=False,
                )
            if recovered:
                _LOGGER.info("Recovered %s invoice file(s) for %s", recovered, self.customer_id)

    async def async_backfill_invoice_history(self) -> None:
        """Fetch paid/old invoices without allowing them to rewrite live debt."""
        if self._invoice_history_complete or self.api.region not in _INVOICE_HISTORY_REGIONS:
            self._invoice_history_complete = True
            return
        if self._invoice_history_lock.locked():
            return
        async with self._invoice_history_lock:
            now = dt_util.now()
            target_marker = _invoice_history_target_marker(now)
            async with self._api_lock:
                if not self.api.access_token and not await self._async_login_with_retry():
                    return
            errors: list[str] = []
            all_done = True
            for month, year in _invoice_history_periods(now):
                if self.hass.is_stopping:
                    return
                key = f"{_INVOICE_PERIOD_STATE_PREFIX}{year}{month:02d}"
                state = await self.hass.async_add_executor_job(
                    self.database.get_state, self.customer_id, key
                )
                if state == "1" and not _invoice_period_is_recent(month, year, now):
                    continue
                try:
                    async with self._api_lock:
                        result = await self._async_api_call(self.api.get_hoadon, month, year)
                    if result is None:
                        all_done = False
                        continue
                    # CRITICAL DEBT FIX: historical invoice archive lookups persist
                    # bills/files only. They must never update current outstanding debt.
                    recovered = await self._async_process_bill_result(
                        result,
                        errors,
                        source=f"invoice_history_{year}{month:02d}",
                        fallback_period=(month, year),
                        update_debt=False,
                    )
                    rows = result.get("data") if isinstance(result, dict) else None
                    if recovered or (isinstance(rows, list) and rows) or not _invoice_period_is_recent(month, year, now):
                        await self.hass.async_add_executor_job(
                            self.database.set_state, self.customer_id, key, "1"
                        )
                    else:
                        all_done = False
                except asyncio.CancelledError:
                    raise
                except Exception as err:  # noqa: BLE001
                    all_done = False
                    _LOGGER.debug("Invoice history %02d/%s failed: %s", month, year, err)
                await asyncio.sleep(HISTORY_MONTH_PAUSE_SECONDS)
            # Mark the overall range only when every recent period either yielded
            # data/file or has safely aged out. This permits late EVN publication.
            if all_done:
                await self.hass.async_add_executor_job(
                    self.database.set_state,
                    self.customer_id,
                    _INVOICE_HISTORY_STATE_KEY,
                    target_marker,
                )
                self._invoice_history_complete = True

    async def async_backfill_history(self) -> None:
        if self._history_backfill_complete:
            if not self._zalo_baseline_ready:
                await self._async_ensure_zalo_baseline()
            return
        if self._backfill_lock.locked():
            return
        async with self._backfill_lock:
            try:
                await self._async_backfill_history()
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("History bootstrap paused for %s: %s", self.customer_id, err)
            finally:
                if not self._zalo_baseline_ready:
                    await self._async_ensure_zalo_baseline()

    async def _async_ensure_zalo_baseline(self) -> None:
        snapshot = await self.hass.async_add_executor_job(
            self.database.load_snapshot, self.customer_id
        )
        snapshot = self._decorate_snapshot(snapshot)
        await self.zalo.async_seed_all(snapshot)
        await self.hass.async_add_executor_job(
            self.database.set_state, self.customer_id, "zalo_baseline_ready", "1"
        )
        self._zalo_baseline_ready = True

    async def _async_backfill_history(self) -> None:
        await asyncio.sleep(HISTORY_BOOTSTRAP_DELAY_SECONDS)
        now = dt_util.now()
        year = now.year
        marker = await self.hass.async_add_executor_job(
            self.database.get_state, self.customer_id, "history_bootstrap_year"
        )
        if marker == str(year):
            self._history_backfill_complete = True
            return
        start = datetime(year - HISTORY_PREVIOUS_YEARS, 1, 1, tzinfo=now.tzinfo)
        cursor_key = f"history_daily_cursor_{year}"
        cursor = await self.hass.async_add_executor_job(
            self.database.get_state, self.customer_id, cursor_key
        )
        current = start
        if cursor:
            try:
                current = datetime.fromisoformat(cursor).replace(tzinfo=now.tzinfo) + timedelta(days=1)
            except ValueError:
                pass
        async with self._api_lock:
            if not self.api.access_token and not await self._async_login_with_retry():
                return
        while current.date() <= now.date():
            end = min(current + timedelta(days=DAILY_BATCH_DAYS - 1), now)
            async with self._api_lock:
                response = await self._async_api_call(
                    self.api.get_chisongay,
                    current.strftime("%d/%m/%Y"),
                    end.strftime("%d/%m/%Y"),
                )
            if response is None:
                return
            await self._async_save_raw(
                f"daily_history_{current:%Y%m%d}_{end:%Y%m%d}", response
            )
            payload = response.get("data") if isinstance(response, dict) else None
            if isinstance(payload, list):
                parsed = self._build_daily_rows([x for x in payload if isinstance(x, dict)])
                parsed = [
                    row
                    for row in parsed
                    if current.date() <= datetime.strptime(row[0], "%d-%m-%Y").date() <= end.date()
                ]
                if parsed:
                    await self.hass.async_add_executor_job(
                        self.database.save_daily_records, self.customer_id, parsed
                    )
            await self.hass.async_add_executor_job(
                self.database.set_state,
                self.customer_id,
                cursor_key,
                end.date().isoformat(),
            )
            current = end + timedelta(days=1)
            await asyncio.sleep(HISTORY_BATCH_PAUSE_SECONDS)
        await self.hass.async_add_executor_job(
            self.database.aggregate_monthly_from_daily, self.customer_id
        )

        monthly_complete = True
        if self.api.region in {"HN", "NPC", "CPC"}:
            month_cursor = datetime(start.year, start.month, 1, tzinfo=now.tzinfo)
            while month_cursor <= now:
                m, y = month_cursor.month, month_cursor.year
                try:
                    async with self._api_lock:
                        result = await self._async_api_call(self.api.get_chisothang, m, y)
                    errors: list[str] = []
                    await self._async_process_monthly_result(
                        f"history_month_{y}{m:02d}", result, m, y, errors
                    )
                    if result is None:
                        monthly_complete = False
                except asyncio.CancelledError:
                    raise
                except Exception:
                    monthly_complete = False
                if m == 12:
                    month_cursor = month_cursor.replace(year=y + 1, month=1)
                else:
                    month_cursor = month_cursor.replace(month=m + 1)
                await asyncio.sleep(HISTORY_MONTH_PAUSE_SECONDS)

        snapshot = await self.hass.async_add_executor_job(
            self.database.load_snapshot, self.customer_id
        )
        snapshot = self._decorate_snapshot(snapshot)
        if not self._zalo_baseline_ready:
            await self.zalo.async_seed_all(snapshot)
            await self.hass.async_add_executor_job(
                self.database.set_state, self.customer_id, "zalo_baseline_ready", "1"
            )
            self._zalo_baseline_ready = True
        if monthly_complete:
            await self.hass.async_add_executor_job(
                self.database.set_state, self.customer_id, "history_bootstrap_year", str(year)
            )
            self._history_backfill_complete = True
        self.async_set_updated_data(snapshot)

    async def _async_process_monthly_result(
        self,
        source: str,
        result: Any,
        month: int,
        year: int,
        errors: list[str],
    ) -> None:
        if isinstance(result, Exception):
            errors.append(f"{source}: {result}")
            return
        if result is None:
            errors.append(f"{source}: no response")
            return
        await self._async_save_raw(source, result)
        payload = result.get("data") if isinstance(result, dict) else None
        if not isinstance(payload, list) or not payload:
            return
        record = payload[0] if isinstance(payload[0], dict) else {}
        consumption = parse_number(
            _first_value(record, "DIEN_TTHU", "dien_tthu", "SAN_LUONG", "san_luong")
        )
        if consumption is None:
            new = parse_number(_first_value(record, "CHISO_MOI", "chi_so_moi"))
            old = parse_number(_first_value(record, "CHISO_CU", "chi_so_cu"))
            if new is not None and old is not None and new >= old:
                consumption = new - old
        await self.hass.async_add_executor_job(
            self.database.save_monthly_reading,
            self.customer_id,
            month,
            year,
            consumption,
        )
        await self._async_extract_invoice_files(
            [result],
            fallback_period=(month, year),
            source_hint="monthly",
            resource_base_url=self.api.monthly_resource_base_url,
        )

    async def _async_process_bill_result(
        self,
        result: Any,
        errors: list[str],
        *,
        source: str = "bill",
        fallback_period: tuple[int, int] | None = None,
        update_debt: bool = True,
    ) -> int:
        """Persist bills; only live lookup may update outstanding debt."""
        if isinstance(result, Exception):
            errors.append(f"{source}: {result}")
            return 0
        if result is None:
            errors.append(f"{source}: no response")
            return 0
        await self._async_save_raw(source, result)
        bills = result.get("data") if isinstance(result, dict) else result if isinstance(result, list) else None
        clean_bills = [x for x in bills if isinstance(x, dict)] if isinstance(bills, list) else []
        if clean_bills:
            await self.hass.async_add_executor_job(
                self.database.save_bills,
                self.customer_id,
                clean_bills,
                update_debt,
            )
        scan_records = list(clean_bills)
        if isinstance(result, dict):
            scan_records.append(result)
        return await self._async_extract_invoice_files(
            scan_records,
            fallback_period=fallback_period,
            source_hint=source,
            resource_base_url=self.api.invoice_resource_base_url,
        )

    async def _async_process_outage_result(
        self,
        result: Any,
        errors: list[str],
        window_start: datetime,
        window_end: datetime,
    ) -> bool:
        if isinstance(result, Exception):
            errors.append(f"outage: {result}")
            return False
        if result is None:
            errors.append("outage: no response")
            return False
        await self._async_save_raw("outage", result)
        payload = result.get("data") if isinstance(result, dict) else None
        if not isinstance(payload, list):
            errors.append("outage: invalid response")
            return False
        rows = [x for item in payload if isinstance(item, dict) if (x := self._normalize_outage(item))]
        if self.api.region in {"SPC", "CPC", "HCMC"}:
            await self.hass.async_add_executor_job(
                self.database.sync_outages,
                self.customer_id,
                rows,
                window_start,
                window_end,
            )
            return True
        if rows:
            await self.hass.async_add_executor_job(
                self.database.save_outages, self.customer_id, rows
            )
        return False

    async def _async_process_notifications_result(
        self,
        result: Any,
        errors: list[str],
        *,
        allow_outage_fallback: bool = True,
    ) -> None:
        if isinstance(result, Exception):
            errors.append(f"notifications: {result}")
            return
        if result is None:
            if self.api.region != "SPC":
                errors.append("notifications: no response")
            return
        if not isinstance(result, list):
            errors.append("notifications: invalid response")
            return
        await self._async_save_raw("notifications", result)
        selected: list[dict[str, Any]] = []
        outage_rows: list[dict[str, Any]] = []
        for note in result:
            if not isinstance(note, dict):
                continue
            summary = str(note.get("summary") or note.get("noiDung") or note.get("strNoiDung") or "")
            if re.search(r"[PS][A-Z]\d{6,}", summary) and self.customer_id not in summary:
                continue
            normalized = dict(note)
            normalized["loai"] = self._notification_category(note.get("notificationType"))
            selected.append(normalized)
            if normalized["loai"] == "NGUNGCAP_DIEN" and self.customer_id in summary:
                parsed = self._parse_outage_notification(summary)
                if parsed:
                    outage_rows.append(parsed)
        if selected:
            await self.hass.async_add_executor_job(
                self.database.save_notifications, self.customer_id, selected
            )
            invoice_notes = [x for x in selected if is_invoice_notification(x)]
            if invoice_notes:
                await self._async_extract_invoice_files(
                    invoice_notes,
                    source_hint="notification",
                    allow_generic_period=False,
                    resource_base_url=self.api.notification_resource_base_url,
                )
        if outage_rows and allow_outage_fallback:
            await self.hass.async_add_executor_job(
                self.database.save_outages, self.customer_id, outage_rows
            )

    async def _async_save_raw(self, source: str, payload: Any) -> None:
        await self.hass.async_add_executor_job(
            self.database.save_raw_response, self.customer_id, source, payload
        )

    def _build_daily_rows(
        self, records: list[dict[str, Any]]
    ) -> list[tuple[str, float | None, float | None]]:
        parsed: list[tuple[datetime, dict[str, Any]]] = []
        for record in records:
            date_value = self._parse_date(record)
            if date_value is not None:
                parsed.append((date_value, record))
        parsed.sort(key=lambda item: item[0])
        rows: list[tuple[str, float | None, float | None]] = []
        previous_reading: float | None = None
        previous_date: datetime | None = None
        for row_date, record in parsed:
            reading = parse_number(
                _first_value(
                    record,
                    "CHISO_MOI",
                    "chi_so_moi",
                    "CHISO",
                    "chi_so",
                    "CHI_SO",
                    "chiSo",
                    "dGiaoBT",
                )
            )
            consumption = parse_number(
                _first_value(
                    record,
                    "dien_tieu_thu",
                    "DIEN_TIEU_THU",
                    "SAN_LUONG",
                    "san_luong",
                    "DIEN_TIEU_THU_KWH",
                    "dSanLuongBT",
                    "DIEN_TTHU",
                )
            )
            if (
                consumption is None
                and reading is not None
                and previous_reading is not None
                and previous_date is not None
                and (row_date.date() - previous_date.date()).days == 1
                and reading >= previous_reading
            ):
                consumption = reading - previous_reading
            rows.append(
                (
                    row_date.strftime("%d-%m-%Y"),
                    reading,
                    round(consumption, 6) if consumption is not None else None,
                )
            )
            if reading is not None:
                previous_reading = reading
                previous_date = row_date
        deduped: dict[str, tuple[str, float | None, float | None]] = {}
        for row in rows:
            old = deduped.get(row[0])
            if old is None:
                deduped[row[0]] = row
            else:
                deduped[row[0]] = (
                    row[0],
                    row[1] if row[1] is not None else old[1],
                    row[2] if row[2] is not None else old[2],
                )
        return list(deduped.values())

    @staticmethod
    def _parse_date(record: dict[str, Any]) -> datetime | None:
        for key in (
            "NGAY",
            "ngay",
            "NGAY_DO",
            "ngay_do",
            "NGAY_DO_CS",
            "ngay_do_cs",
            "THOI_DIEM",
            "thoi_diem",
            "THOI_GIAN",
            "thoi_gian",
            "strTime",
            "ngayFull",
        ):
            raw = record.get(key)
            if raw is None:
                continue
            text = str(raw).strip()
            if not text or text.lower() in {"none", "null"}:
                continue
            if " " in text:
                text = text.split(" ", 1)[0]
            if re.fullmatch(r"\d{1,2}/\d{1,2}/\d{4}-\d{1,2}/\d{1,2}/\d{4}", text):
                text = text.split("-")[-1]
            for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%Y%m%d", "%d%m%Y"):
                try:
                    return datetime.strptime(text, fmt)
                except ValueError:
                    continue
        return None

    def _normalize_outage(self, outage: dict[str, Any]) -> dict[str, Any] | None:
        start_raw = _first_value(outage, "NGAY_BAT_DAU", "ngay_bat_dau", "NGAY", "ngay")
        if not start_raw:
            return None
        end_raw = _first_value(outage, "NGAY_KET_THUC", "ngay_ket_thuc", "NGAY", "ngay") or start_raw
        start_date = self._parse_date({"NGAY": start_raw})
        end_date = self._parse_date({"NGAY": end_raw})
        if start_date is None:
            return None
        return {
            "ngay_bat_dau": start_date.strftime("%d-%m-%Y"),
            "ngay_ket_thuc": (end_date or start_date).strftime("%d-%m-%Y"),
            "thoi_gian_bat_dau": str(_first_value(outage, "THOI_GIAN_BAT_DAU", "thoi_gian_bat_dau", "THOI_GIAN", "thoi_gian", "THOI_DIEM", "thoi_diem") or ""),
            "thoi_gian_ket_thuc": str(_first_value(outage, "THOI_GIAN_KET_THUC", "thoi_gian_ket_thuc") or ""),
            "ly_do": str(_first_value(outage, "LY_DO", "ly_do", "NOI_DUNG", "noi_dung") or ""),
            "khu_vuc": str(_first_value(outage, "KHU_VUC", "khu_vuc", "DIA_CHI", "dia_chi") or ""),
        }

    @staticmethod
    def _notification_category(value: Any) -> str:
        text = str(value or "").upper()
        if text.startswith("NGUNGCAP_DIEN"):
            return "NGUNGCAP_DIEN"
        if text.startswith("HOADON"):
            return "HOADON"
        if text.startswith("TRUYEN_THONG"):
            return "TRUYEN_THONG"
        return text or "KHAC"

    def _parse_outage_notification(self, summary: str) -> dict[str, Any] | None:
        date_match = re.search(r"ngày\s+(\d{1,2})/(\d{1,2})/(\d{4})", summary, re.I)
        time_match = re.search(r"từ\s+(\d{1,2})h(\d{2})\s+đến\s+(\d{1,2})h(\d{2})", summary, re.I)
        if not date_match or not time_match:
            return None
        area_match = re.search(r"thuộc\s+(.+?)\s+thời điểm", summary, re.I)
        reason_match = re.search(r"để\s+(.+?)(?:\s*\.\.\.|\s*$)", summary, re.I)
        display_date = f"{int(date_match.group(1)):02d}-{int(date_match.group(2)):02d}-{date_match.group(3)}"
        return {
            "ngay_bat_dau": display_date,
            "ngay_ket_thuc": display_date,
            "thoi_gian_bat_dau": f"{int(time_match.group(1)):02d}:{time_match.group(2)}",
            "thoi_gian_ket_thuc": f"{int(time_match.group(3)):02d}:{time_match.group(4)}",
            "khu_vuc": area_match.group(1).strip() if area_match else "",
            "ly_do": reason_match.group(1).strip() if reason_match else summary.strip(),
        }

    async def _async_extract_invoice_files(
        self,
        records: list[dict[str, Any]],
        *,
        fallback_period: tuple[int, int] | None = None,
        source_hint: str = "bill",
        allow_generic_period: bool = True,
        resource_base_url: str | None = None,
    ) -> int:
        saved = 0
        async with self._invoice_lock:
            self.invoice_dir.mkdir(parents=True, exist_ok=True)
            for record in records:
                if not isinstance(record, dict):
                    continue
                period = infer_invoice_period(record, allow_generic=allow_generic_period) or fallback_period
                candidates = list(iter_attachment_candidates(record))
                if not candidates:
                    continue
                if period is None:
                    for _, value in candidates:
                        period = infer_invoice_period(value, allow_generic=True)
                        if period is not None:
                            break
                if period is None:
                    continue
                month, year = period
                if not (1 <= month <= 12 and 2000 <= year <= 2100):
                    continue
                already_valid: set[str] = set()
                for ext in ("pdf", "png"):
                    path = self.invoice_dir / f"{self.customer_id}_{month}_{year}.{ext}"
                    if await self.hass.async_add_executor_job(_valid_invoice_file, path, ext):
                        already_valid.add(ext)
                if len(already_valid) == 2:
                    continue
                for kind, value in candidates:
                    content: bytes | None
                    if kind == "url":
                        content = await self._async_api_call(
                            self.api.download_file, value, base_url=resource_base_url
                        )
                    elif kind == "base64":
                        content = decode_base64_payload(value)
                    else:
                        continue
                    detected = detect_invoice_type(content)
                    if detected is None or detected in already_valid or content is None:
                        continue
                    path = self.invoice_dir / f"{self.customer_id}_{month}_{year}.{detected}"
                    await self.hass.async_add_executor_job(_write_bytes_atomic, path, content)
                    already_valid.add(detected)
                    saved += 1
                    if len(already_valid) == 2:
                        break
        return saved

    def _decorate_snapshot(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        result = dict(snapshot)
        result["customer"] = {
            "id": self.customer_id,
            "name": self.api.ten_khang,
            "phone": self.api.dien_thoai,
            "address": self.api.dia_chi,
            "region": self.api.region,
            "management_unit": self.api.ma_dviqly,
        }
        result.setdefault("partial_errors", [])
        return result


def _first_value(data: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = data.get(key)
        if value not in (None, ""):
            return value
    return None


def _previous_month(month: int, year: int) -> tuple[int, int]:
    return (12, year - 1) if month == 1 else (month - 1, year)


def _invoice_history_periods(now: datetime) -> list[tuple[int, int]]:
    target_month, target_year = _previous_month(now.month, now.year)
    first_year = now.year - HISTORY_PREVIOUS_YEARS
    periods: list[tuple[int, int]] = []
    for year in range(first_year, target_year + 1):
        last_month = target_month if year == target_year else 12
        for month in range(1, last_month + 1):
            periods.append((month, year))
    periods.reverse()
    return periods


def _invoice_period_is_recent(
    month: int, year: int, now: datetime, *, retry_months: int = 3
) -> bool:
    target_month, target_year = _previous_month(now.month, now.year)
    target_index = target_year * 12 + target_month - 1
    period_index = year * 12 + month - 1
    age = target_index - period_index
    return 0 <= age < retry_months


def _invoice_history_target_marker(now: datetime) -> str:
    month, year = _previous_month(now.month, now.year)
    return f"{year}{month:02d}"


def _prepare_invoice_directory(data_dir: Path, invoice_dir: Path) -> int:
    invoice_dir.mkdir(parents=True, exist_ok=True)
    pattern = re.compile(r"^.+_(0?[1-9]|1[0-2])_(20\d{2})\.(pdf|png)$", re.IGNORECASE)
    moved = 0
    if not data_dir.is_dir():
        return moved
    for source in data_dir.iterdir():
        if not source.is_file():
            continue
        match = pattern.fullmatch(source.name)
        if not match:
            continue
        ext = match.group(3).lower()
        if not _valid_invoice_file(source, ext):
            continue
        destination = invoice_dir / source.name
        if destination.exists() and _valid_invoice_file(destination, ext):
            try:
                source.unlink()
            except OSError:
                pass
            else:
                moved += 1
            continue
        try:
            if destination.exists():
                destination.unlink()
            source.replace(destination)
            moved += 1
        except OSError:
            continue
    return moved


def _valid_invoice_file(path: Path, ext: str) -> bool:
    if not path.is_file():
        return False
    try:
        with path.open("rb") as handle:
            head = handle.read(16)
    except OSError:
        return False
    if ext == "pdf":
        return head.startswith(b"%PDF-")
    if ext == "png":
        return head.startswith(b"\x89PNG\r\n\x1a\n")
    return False


def _write_bytes_atomic(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_bytes(content)
    temp.replace(path)
