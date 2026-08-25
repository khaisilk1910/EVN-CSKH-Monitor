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
    HISTORY_BATCH_PAUSE_SECONDS,
    HISTORY_BOOTSTRAP_DELAY_SECONDS,
    HISTORY_MONTH_PAUSE_SECONDS,
    HISTORY_PREVIOUS_YEARS,
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

_INVOICE_RESCAN_STATE_KEY = "invoice_attachment_rescan_v2"


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
        self.customer_id = customer_id
        self.cache_loaded = False
        self._api_lock = asyncio.Lock()
        self._backfill_lock = asyncio.Lock()
        self._invoice_lock = asyncio.Lock()
        self._invoice_rescan_lock = asyncio.Lock()
        self._invoice_rescan_complete = False
        self._history_backfill_complete = False
        self._zalo_baseline_ready = False
        self.zalo = ZaloNotifier(hass, entry, database, data_dir)

    async def async_initialize(self) -> None:
        """Prepare local storage and load cache without any network request."""
        await self.hass.async_add_executor_job(self.database.initialize)
        self.data = await self.hass.async_add_executor_job(
            self.database.load_snapshot, self.customer_id
        )
        self.data = self._decorate_snapshot(self.data)
        bootstrap_year = await self.hass.async_add_executor_job(
            self.database.get_state, self.customer_id, "history_bootstrap_year"
        )
        self._history_backfill_complete = bootstrap_year == str(dt_util.now().year)
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
        self.cache_loaded = True

    async def _async_update_data(self) -> dict[str, Any]:
        """Refresh EVN data.

        This method is only launched as a config-entry background task for the
        first refresh, so a slow EVN server cannot delay Home Assistant startup.
        SQLite work is always sent to the executor.
        """
        async with self._api_lock:
            if not self.api.access_token and not await self.api.login():
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
        except Exception as err:  # noqa: BLE001 - partial refresh should continue
            errors.append(f"daily: {err}")
            _LOGGER.warning("Daily refresh failed for %s: %s", self.customer_id, err)

        # These endpoints are independent and can be fetched concurrently. Using
        # gather shortens refresh time without doing blocking work on the event loop.
        current_month = (now.month, now.year)
        if now.month == 1:
            previous_month = (12, now.year - 1)
        else:
            previous_month = (now.month - 1, now.year)

        async with self._api_lock:
            results = await asyncio.gather(
                self.api.get_chisothang(*current_month),
                self.api.get_chisothang(*previous_month),
                self.api.get_hoadon(),
                self.api.get_ngungcapdien(
                    (now - timedelta(days=30)).strftime("%d/%m/%Y"),
                    (now + timedelta(days=60)).strftime("%d/%m/%Y"),
                ),
                self.api.get_thongbao(),
                return_exceptions=True,
            )
        current_month_data, previous_month_data, bill_data, outage_data, notifications = results

        if self.api.last_login_auth_failed:
            raise ConfigEntryAuthFailed("EVN authentication failed while refreshing data")

        cloud_results_ok = any(
            item is not None and not isinstance(item, Exception) for item in results
        )
        if not daily_ok and not cloud_results_ok:
            raise UpdateFailed("All EVN data endpoints were unavailable")

        await self._async_process_monthly_result(
            "monthly_current", current_month_data, *current_month, errors
        )
        await self._async_process_monthly_result(
            "monthly_previous", previous_month_data, *previous_month, errors
        )
        await self._async_process_bill_result(bill_data, errors)
        await self._async_process_outage_result(outage_data, errors)
        await self._async_process_notifications_result(notifications, errors)

        sync_time = dt_util.now().isoformat()
        await self.hass.async_add_executor_job(
            self.database.set_state, self.customer_id, "last_sync", sync_time
        )
        snapshot = await self.hass.async_add_executor_job(
            self.database.load_snapshot, self.customer_id
        )
        snapshot = self._decorate_snapshot(snapshot)
        snapshot["partial_errors"] = errors

        # Zalo is optional and deliberately detached from the coordinator. The
        # EVN snapshot becomes available immediately even if a third-party Zalo
        # service is slow or temporarily unavailable. Config-entry background
        # tasks are cancelled automatically on unload and do not block startup.
        # Do not emit Zalo notifications while the initial historical import is
        # running. The importer seeds all current fingerprints when it finishes,
        # so old bills/outages/production are treated as baseline rather than new.
        if self._zalo_baseline_ready and self._invoice_rescan_complete:
            self.entry.async_create_background_task(
                self.hass,
                self._async_process_zalo(snapshot),
                name=f"evn_cskh_monitor zalo {self.customer_id}",
            )
        if not self._invoice_rescan_complete:
            self.entry.async_create_background_task(
                self.hass,
                self.async_rescan_stored_invoice_attachments(),
                name=f"evn_cskh_monitor invoice recovery {self.customer_id}",
            )
        if not self._history_backfill_complete or not self._zalo_baseline_ready:
            self.entry.async_create_background_task(
                self.hass,
                self.async_backfill_history(),
                name=f"evn_cskh_monitor history retry {self.customer_id}",
            )

        return snapshot

    async def async_rescan_stored_invoice_attachments(self) -> None:
        """Recover invoice files from already stored EVN payloads in background.

        Older builds persisted the full EVN responses but only recognized URLs
        ending in ``.pdf``/``.png``.  Newer regional gateways often expose an
        opaque viewer/download URL.  A one-time recovery pass lets upgrades find
        those resources without forcing a historical data re-download.
        """
        if self._invoice_rescan_complete or self._invoice_rescan_lock.locked():
            return
        async with self._invoice_rescan_lock:
            marker = await self.hass.async_add_executor_job(
                self.database.get_state, self.customer_id, _INVOICE_RESCAN_STATE_KEY
            )
            if marker == "1":
                self._invoice_rescan_complete = True
                return

            stored = await self.hass.async_add_executor_job(
                self.database.load_invoice_source_records, self.customer_id
            )
            recovered = 0
            for source, payload in stored:
                try:
                    if source == "bill":
                        rows = payload.get("data") if isinstance(payload, dict) else payload
                        if isinstance(rows, list):
                            recovered += await self._async_extract_invoice_files(
                                [row for row in rows if isinstance(row, dict)],
                                source_hint="stored bill",
                            )
                    elif source == "notifications":
                        rows = payload.get("data") if isinstance(payload, dict) else payload
                        if isinstance(rows, list):
                            invoices = [
                                row
                                for row in rows
                                if isinstance(row, dict) and is_invoice_notification(row)
                            ]
                            if invoices:
                                recovered += await self._async_extract_invoice_files(
                                    invoices,
                                    source_hint="stored notification",
                                    allow_generic_period=False,
                                )
                    elif source.startswith(("monthly_", "history_month_")):
                        fallback: tuple[int, int] | None = None
                        match = re.fullmatch(r"history_month_(20\d{2})(0[1-9]|1[0-2])", source)
                        if match:
                            fallback = (int(match.group(2)), int(match.group(1)))
                        recovered += await self._async_extract_invoice_files(
                            [payload] if isinstance(payload, dict) else [],
                            fallback_period=fallback,
                            source_hint="stored monthly",
                        )
                except asyncio.CancelledError:
                    raise
                except Exception as err:  # noqa: BLE001 - best-effort recovery
                    _LOGGER.debug(
                        "Stored invoice recovery skipped %s for %s: %s",
                        source,
                        self.customer_id,
                        err,
                    )
                # Cooperate with the event loop and avoid hammering EVN if many
                # historic payloads contain viewer links.
                await asyncio.sleep(0.05)

            # This is an upgrade/historical recovery pass. Seed only invoice
            # fingerprints before enabling the normal Zalo loop, otherwise old
            # attachments newly discovered from raw responses would look new.
            current_snapshot: dict[str, Any] | None = None
            if recovered and self._zalo_baseline_ready:
                current_snapshot = await self.hass.async_add_executor_job(
                    self.database.load_snapshot, self.customer_id
                )
                current_snapshot = self._decorate_snapshot(current_snapshot)
                await self.zalo.async_seed_invoice_files(current_snapshot)

            await self.hass.async_add_executor_job(
                self.database.set_state,
                self.customer_id,
                _INVOICE_RESCAN_STATE_KEY,
                "1",
            )
            self._invoice_rescan_complete = True

            # The normal Zalo task was intentionally skipped while recovery was
            # pending. Process the current snapshot now so daily/outage alerts are
            # not delayed by a full polling interval. Historical invoice files
            # have already been baseline-seeded above.
            if self._zalo_baseline_ready:
                if current_snapshot is None:
                    current_snapshot = await self.hass.async_add_executor_job(
                        self.database.load_snapshot, self.customer_id
                    )
                    current_snapshot = self._decorate_snapshot(current_snapshot)
                await self._async_process_zalo(current_snapshot)

            if recovered:
                _LOGGER.info(
                    "Recovered %s official invoice file(s) for %s from stored EVN responses",
                    recovered,
                    self.customer_id,
                )

    async def _async_process_zalo(self, snapshot: dict[str, Any]) -> None:
        """Run optional Zalo delivery without delaying EVN state refreshes."""
        try:
            await self.zalo.async_process(snapshot)
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Zalo notification processing failed: %s", err)

    def _decorate_snapshot(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        snapshot["customer"] = {
            "id": self.customer_id,
            "name": self.api.ten_khang,
            "phone": self.api.dien_thoai,
            "address": self.api.dia_chi,
            "region": self.api.region,
            "management_unit": self.api.ma_dviqly,
        }
        return snapshot

    async def _async_refresh_daily(self, now: datetime) -> bool:
        last_saved = await self.hass.async_add_executor_job(
            self.database.get_last_daily_date, self.customer_id
        )
        history_start = datetime(
            now.year - HISTORY_PREVIOUS_YEARS, 1, 1, tzinfo=now.tzinfo
        )
        if last_saved is not None:
            if now.tzinfo is not None and last_saved.tzinfo is None:
                last_saved = last_saved.replace(tzinfo=now.tzinfo)
            fetch_start = max(last_saved - timedelta(days=REFRESH_WINDOW_DAYS), history_start)
        else:
            fetch_start = max(now - timedelta(days=RECENT_BOOTSTRAP_DAYS), history_start)
            _LOGGER.info(
                "%s has no local history; loading the most recent %s days first",
                self.customer_id,
                RECENT_BOOTSTRAP_DAYS,
            )

        records: list[dict[str, Any]] = []
        received_response = False
        current_start = fetch_start
        while current_start.date() <= now.date():
            current_end = min(
                current_start + timedelta(days=DAILY_BATCH_DAYS - 1), now
            )
            async with self._api_lock:
                response = await self.api.get_chisongay(
                    current_start.strftime("%d/%m/%Y"),
                    current_end.strftime("%d/%m/%Y"),
                )
            if response is not None:
                received_response = True
                await self._async_save_raw("daily", response)
                payload = response.get("data") if isinstance(response, dict) else None
                if isinstance(payload, list):
                    records.extend(item for item in payload if isinstance(item, dict))
            current_start = current_end + timedelta(days=1)
            await asyncio.sleep(0)

        if not records:
            return received_response
        parsed = self._build_daily_rows(records)
        if parsed:
            await self.hass.async_add_executor_job(
                self.database.save_daily_records, self.customer_id, parsed
            )
            await self.hass.async_add_executor_job(
                self.database.aggregate_monthly_from_daily, self.customer_id
            )
            _LOGGER.debug("Saved %s daily records for %s", len(parsed), self.customer_id)
        return received_response

    async def async_backfill_history(self) -> None:
        """Run one background history worker and establish a no-send baseline."""
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
            else:
                # Even if an upstream batch temporarily paused the importer, the
                # initial current-state refresh has already populated recent data.
                # Seed that snapshot so notifications can start without ever
                # replaying the imported baseline. Older retries remain silent.
                if not self._zalo_baseline_ready:
                    await self._async_ensure_zalo_baseline()

    async def _async_ensure_zalo_baseline(self) -> None:
        """Seed all configured Zalo routes from the current local snapshot."""
        snapshot = await self.hass.async_add_executor_job(
            self.database.load_snapshot, self.customer_id
        )
        snapshot = self._decorate_snapshot(snapshot)
        await self.zalo.async_seed_all(snapshot)
        await self.hass.async_add_executor_job(
            self.database.set_state,
            self.customer_id,
            "zalo_baseline_ready",
            "1",
        )
        self._zalo_baseline_ready = True

    async def _async_backfill_history(self) -> None:
        """Load the previous calendar year and current year in the background.

        A new meter gets January..December of the previous year plus January
        through the current date/year, when EVN has those records. Daily data is
        downloaded in small batches with a persistent cursor. HN/NPC/CPC also
        query the dedicated monthly endpoint because it can be more authoritative
        than summing daily readings. HCMC/SPC monthly data is derived from the same
        daily endpoint, so duplicate monthly network calls are intentionally
        avoided. Historical batches never call Zalo; the current snapshot is
        baseline-seeded before notification delivery is enabled.
        """
        await asyncio.sleep(HISTORY_BOOTSTRAP_DELAY_SECONDS)

        now = dt_util.now()
        target_year = now.year
        completed_year = await self.hass.async_add_executor_job(
            self.database.get_state, self.customer_id, "history_bootstrap_year"
        )
        if completed_year == str(target_year):
            self._history_backfill_complete = True
            return

        history_start = datetime(
            target_year - HISTORY_PREVIOUS_YEARS, 1, 1, tzinfo=now.tzinfo
        )
        history_end = now
        daily_cursor_key = f"history_daily_cursor_{target_year}"
        monthly_done_key = f"history_monthly_done_{target_year}"

        # Authenticate once before the slower import. Every API method still
        # handles token expiry itself, and all cloud requests share _api_lock.
        async with self._api_lock:
            if not self.api.access_token and not await self.api.login():
                _LOGGER.warning(
                    "History bootstrap paused for %s: EVN login unavailable (%s)",
                    self.customer_id,
                    self.api.last_login_error or "unknown error",
                )
                return

        monthly_done = await self.hass.async_add_executor_job(
            self.database.get_state, self.customer_id, monthly_done_key
        )
        monthly_saved = 0
        monthly_complete = monthly_done == "1" or self.api.region in {"HCMC", "SPC"}
        if monthly_done != "1" and self.api.region not in {"HCMC", "SPC"}:
            monthly_complete = True
            _LOGGER.info(
                "Loading monthly EVN history for %s from %s through %s/%s",
                self.customer_id,
                history_start.year,
                now.month,
                now.year,
            )
            month_errors: list[str] = []
            for year in range(history_start.year, target_year + 1):
                last_month = now.month if year == target_year else 12
                for month in range(1, last_month + 1):
                    month_done_state_key = f"history_month_done_{year}{month:02d}"
                    month_done_state = await self.hass.async_add_executor_job(
                        self.database.get_state,
                        self.customer_id,
                        month_done_state_key,
                    )
                    if month_done_state == "1":
                        continue
                    try:
                        async with self._api_lock:
                            result = await self.api.get_chisothang(month, year)
                        if self.api.last_login_auth_failed:
                            _LOGGER.warning(
                                "History bootstrap stopped for %s because EVN authentication expired",
                                self.customer_id,
                            )
                            return
                        if result is None:
                            # ``None`` means transport/auth/server failure in the
                            # client. A valid month with no data returns an empty
                            # response object instead. Do not mark history complete
                            # or a transient EVN outage would create a permanent gap.
                            monthly_complete = False
                            _LOGGER.warning(
                                "Monthly EVN history unavailable for %s %02d/%s; will retry later",
                                self.customer_id,
                                month,
                                year,
                            )
                        else:
                            before = len(month_errors)
                            await self._async_process_monthly_result(
                                f"history_month_{year}{month:02d}",
                                result,
                                month,
                                year,
                                month_errors,
                            )
                            if len(month_errors) == before:
                                monthly_saved += 1
                            await self.hass.async_add_executor_job(
                                self.database.set_state,
                                self.customer_id,
                                month_done_state_key,
                                "1",
                            )
                    except asyncio.CancelledError:
                        raise
                    except Exception as err:  # noqa: BLE001
                        monthly_complete = False
                        _LOGGER.warning(
                            "Monthly history %02d/%s failed for %s: %s",
                            month,
                            year,
                            self.customer_id,
                            err,
                        )
                    await asyncio.sleep(HISTORY_MONTH_PAUSE_SECONDS)
            if monthly_complete:
                await self.hass.async_add_executor_job(
                    self.database.set_state,
                    self.customer_id,
                    monthly_done_key,
                    "1",
                )

        cursor = await self.hass.async_add_executor_job(
            self.database.get_state, self.customer_id, daily_cursor_key
        )
        current_start = history_start
        if cursor:
            try:
                cursor_date = datetime.fromisoformat(cursor).date()
                candidate = datetime.combine(
                    cursor_date + timedelta(days=1),
                    datetime.min.time(),
                    tzinfo=now.tzinfo,
                )
                if cursor_date >= history_end.date():
                    current_start = history_end + timedelta(days=1)
                elif history_start.date() <= candidate.date() <= history_end.date():
                    current_start = candidate
            except ValueError:
                _LOGGER.warning(
                    "Ignoring invalid history cursor for %s: %s",
                    self.customer_id,
                    cursor,
                )

        _LOGGER.info(
            "Loading daily EVN history for %s from %s through %s",
            self.customer_id,
            current_start.date(),
            history_end.date(),
        )
        saved = 0
        while current_start.date() <= history_end.date():
            current_end = min(
                current_start + timedelta(days=DAILY_BATCH_DAYS - 1), history_end
            )
            try:
                # Include the previous day in each historical request. Some EVN
                # regions expose only cumulative meter readings; the overlap lets
                # _build_daily_rows calculate the first day's delta in every batch
                # without creating a missing-consumption gap at batch boundaries.
                request_start = current_start - timedelta(days=1)
                async with self._api_lock:
                    response = await self.api.get_chisongay(
                        request_start.strftime("%d/%m/%Y"),
                        current_end.strftime("%d/%m/%Y"),
                    )

                if self.api.last_login_auth_failed:
                    _LOGGER.warning(
                        "History bootstrap stopped for %s because EVN authentication expired",
                        self.customer_id,
                    )
                    return
                if response is None:
                    # Do not advance the cursor on a transport/server failure. The
                    # next scheduled refresh/startup can resume without a data hole.
                    _LOGGER.warning(
                        "History bootstrap paused for %s: no response for %s..%s",
                        self.customer_id,
                        current_start.date(),
                        current_end.date(),
                    )
                    return

                await self._async_save_raw(
                    f"daily_history_{current_start:%Y%m%d}_{current_end:%Y%m%d}",
                    response,
                )
                payload = response.get("data") if isinstance(response, dict) else None
                if isinstance(payload, list):
                    parsed = self._build_daily_rows(
                        [item for item in payload if isinstance(item, dict)]
                    )
                    # Keep the overlap row only as a calculation aid; persist the
                    # intended batch window so the history range stays exact.
                    parsed = [
                        row
                        for row in parsed
                        if current_start.date()
                        <= datetime.strptime(row[0], "%d-%m-%Y").date()
                        <= current_end.date()
                    ]
                    if parsed:
                        await self.hass.async_add_executor_job(
                            self.database.save_daily_records, self.customer_id, parsed
                        )
                        saved += len(parsed)

                # An empty but valid server response still advances the cursor:
                # it means EVN has no data for that batch.
                await self.hass.async_add_executor_job(
                    self.database.set_state,
                    self.customer_id,
                    daily_cursor_key,
                    current_end.date().isoformat(),
                )
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning(
                    "History bootstrap batch %s..%s paused for %s after error: %s",
                    current_start.date(),
                    current_end.date(),
                    self.customer_id,
                    err,
                )
                return

            current_start = current_end + timedelta(days=1)
            await asyncio.sleep(HISTORY_BATCH_PAUSE_SECONDS)

        await self.hass.async_add_executor_job(
            self.database.aggregate_monthly_from_daily, self.customer_id
        )
        if self.api.region in {"HCMC", "SPC"}:
            await self.hass.async_add_executor_job(
                self.database.set_state, self.customer_id, monthly_done_key, "1"
            )

        snapshot = await self.hass.async_add_executor_job(
            self.database.load_snapshot, self.customer_id
        )
        snapshot = self._decorate_snapshot(snapshot)

        # Seed all currently known data BEFORE marking the import complete. This
        # guarantees the historical import itself never emits Zalo messages.
        if not self._zalo_baseline_ready:
            await self.zalo.async_seed_all(snapshot)
            await self.hass.async_add_executor_job(
                self.database.set_state,
                self.customer_id,
                "zalo_baseline_ready",
                "1",
            )
            self._zalo_baseline_ready = True
        if monthly_complete:
            await self.hass.async_add_executor_job(
                self.database.set_state,
                self.customer_id,
                "history_bootstrap_year",
                str(target_year),
            )
            self._history_backfill_complete = True
        else:
            _LOGGER.info(
                "Daily history is complete for %s, but one or more monthly EVN calls will retry later",
                self.customer_id,
            )
        self.async_set_updated_data(snapshot)
        _LOGGER.info(
            "Completed EVN history bootstrap for %s (%s daily rows, %s monthly responses)",
            self.customer_id,
            saved,
            monthly_saved,
        )

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
        # Some regional monthly responses include a direct official invoice
        # viewer/file link. The period is already known from the request, so this
        # is safer than trying to infer it from a generic response timestamp.
        await self._async_extract_invoice_files(
            [result], fallback_period=(month, year), source_hint="monthly"
        )

    async def _async_process_bill_result(self, result: Any, errors: list[str]) -> None:
        if isinstance(result, Exception):
            errors.append(f"bill: {result}")
            return
        if result is None:
            errors.append("bill: no response")
            return
        await self._async_save_raw("bill", result)
        bills = result.get("data") if isinstance(result, dict) else None
        if not isinstance(bills, list):
            return
        clean_bills = [item for item in bills if isinstance(item, dict)]
        await self.hass.async_add_executor_job(
            self.database.save_bills, self.customer_id, clean_bills
        )
        await self._async_extract_invoice_files(clean_bills, source_hint="bill")

    async def _async_process_outage_result(self, result: Any, errors: list[str]) -> None:
        if isinstance(result, Exception):
            errors.append(f"outage: {result}")
            return
        if result is None:
            errors.append("outage: no response")
            return
        await self._async_save_raw("outage", result)
        payload = result.get("data") if isinstance(result, dict) else None
        if not isinstance(payload, list):
            return
        rows = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            normalized = self._normalize_outage(item)
            if normalized:
                rows.append(normalized)
        if rows:
            await self.hass.async_add_executor_job(
                self.database.save_outages, self.customer_id, rows
            )

    async def _async_process_notifications_result(
        self, result: Any, errors: list[str]
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
            summary = str(note.get("summary") or "")
            # Shared EVN accounts may return notifications for several customer IDs.
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
            invoice_notifications = [
                item for item in selected if is_invoice_notification(item)
            ]
            if invoice_notifications:
                await self._async_extract_invoice_files(
                    invoice_notifications,
                    source_hint="notification",
                    allow_generic_period=False,
                )
        if outage_rows:
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
        # Dedupe repeated dates returned by overlapping/region-specific endpoints.
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
            # Some SPC rows are ranges such as 08/10/2025-09/10/2025.
            if re.fullmatch(r"\d{1,2}/\d{1,2}/\d{4}-\d{1,2}/\d{1,2}/\d{4}", text):
                text = text.split("-")[-1]
            for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%Y%m%d", "%d%m%Y"):
                try:
                    return datetime.strptime(text, fmt)
                except ValueError:
                    continue
        _LOGGER.debug("Skipping EVN record with unparseable date: %s", record)
        return None

    def _normalize_outage(self, outage: dict[str, Any]) -> dict[str, Any] | None:
        start_raw = _first_value(
            outage, "NGAY_BAT_DAU", "ngay_bat_dau", "NGAY", "ngay"
        )
        if not start_raw:
            return None
        end_raw = _first_value(
            outage, "NGAY_KET_THUC", "ngay_ket_thuc", "NGAY", "ngay"
        ) or start_raw
        start_date = self._parse_date({"NGAY": start_raw})
        end_date = self._parse_date({"NGAY": end_raw})
        if start_date is None:
            return None
        return {
            "ngay_bat_dau": start_date.strftime("%d-%m-%Y"),
            "ngay_ket_thuc": (end_date or start_date).strftime("%d-%m-%Y"),
            "thoi_gian_bat_dau": str(
                _first_value(
                    outage,
                    "THOI_GIAN_BAT_DAU",
                    "thoi_gian_bat_dau",
                    "THOI_GIAN",
                    "thoi_gian",
                    "THOI_DIEM",
                    "thoi_diem",
                )
                or ""
            ),
            "thoi_gian_ket_thuc": str(
                _first_value(outage, "THOI_GIAN_KET_THUC", "thoi_gian_ket_thuc") or ""
            ),
            "ly_do": str(
                _first_value(outage, "LY_DO", "ly_do", "NOI_DUNG", "noi_dung") or ""
            ),
            "khu_vuc": str(
                _first_value(outage, "KHU_VUC", "khu_vuc", "DIA_CHI", "dia_chi") or ""
            ),
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
        time_match = re.search(
            r"từ\s+(\d{1,2})h(\d{2})\s+đến\s+(\d{1,2})h(\d{2})",
            summary,
            re.I,
        )
        if not date_match or not time_match:
            return None
        area_match = re.search(r"thuộc\s+(.+?)\s+thời điểm", summary, re.I)
        reason_match = re.search(r"để\s+(.+?)(?:\s*\.\.\.|\s*$)", summary, re.I)
        display_date = (
            f"{int(date_match.group(1)):02d}-{int(date_match.group(2)):02d}-{date_match.group(3)}"
        )
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
    ) -> int:
        """Discover and persist official PDF/PNG files exposed by EVN.

        Regional gateways use different field names and frequently return an
        opaque viewer/download URL without a file extension. Candidates are
        therefore discovered by schema hints but accepted only after the bytes
        match the PDF/PNG file signature. Nothing synthetic is generated.
        """
        saved = 0
        async with self._invoice_lock:
            for record in records:
                if not isinstance(record, dict):
                    continue
                period = infer_invoice_period(
                    record, allow_generic=allow_generic_period
                ) or fallback_period
                candidates = list(iter_attachment_candidates(record))
                if not candidates:
                    continue

                # When a bill object has no explicit period, a filename/URL such
                # as HoaDon_07_2026.pdf can still provide an unambiguous period.
                if period is None:
                    for _, value in candidates:
                        period = infer_invoice_period(
                            value, allow_generic=True
                        )
                        if period is not None:
                            break
                if period is None:
                    _LOGGER.debug(
                        "Skipping EVN %s attachment with unknown bill period for %s",
                        source_hint,
                        self.customer_id,
                    )
                    continue

                month, year = period
                if not (1 <= month <= 12 and 2000 <= year <= 2100):
                    continue

                already_valid: set[str] = set()
                for ext in ("pdf", "png"):
                    path = self.data_dir / f"{self.customer_id}_{month}_{year}.{ext}"
                    if await self.hass.async_add_executor_job(
                        _valid_invoice_file, path, ext
                    ):
                        already_valid.add(ext)
                if len(already_valid) == 2:
                    continue

                for kind, value in candidates:
                    content: bytes | None
                    if kind == "url":
                        content = await self.api.download_file(value)
                    elif kind == "base64":
                        content = decode_base64_payload(value)
                    else:
                        continue
                    detected = detect_invoice_type(content)
                    if detected is None or detected in already_valid or content is None:
                        continue

                    path = self.data_dir / f"{self.customer_id}_{month}_{year}.{detected}"
                    await self.hass.async_add_executor_job(
                        _write_bytes_atomic, path, content
                    )
                    already_valid.add(detected)
                    saved += 1
                    _LOGGER.info(
                        "Saved official EVN %s invoice attachment %s",
                        source_hint,
                        path,
                    )
                    if len(already_valid) == 2:
                        break
        return saved


def _first_value(data: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in data and data[key] is not None and data[key] != "":
            return data[key]
    return None


def _to_int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _valid_invoice_file(path: Path, ext: str) -> bool:
    """Validate an existing invoice by magic bytes, not only file size."""
    if not path.is_file() or path.stat().st_size <= 0:
        return False
    try:
        with path.open("rb") as handle:
            return detect_invoice_type(handle.read(64)) == ext
    except OSError:
        return False


def _write_bytes_atomic(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_bytes(content)
    temp.replace(path)

