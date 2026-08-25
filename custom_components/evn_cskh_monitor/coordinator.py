"""Data coordinator for EVN CSKH Monitor."""

from __future__ import annotations

import asyncio
import base64
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
    BACKFILL_DELAY_SECONDS,
    BACKFILL_PAUSE_SECONDS,
    DAILY_BATCH_DAYS,
    HISTORY_START_YEAR,
    RECENT_BOOTSTRAP_DAYS,
    REFRESH_WINDOW_DAYS,
    UPDATE_INTERVAL,
)
from .database import EVNDatabase, parse_number
from .evn_api import EVNAPI
from .zalo import ZaloNotifier

_LOGGER = logging.getLogger(__name__)


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
        self.zalo = ZaloNotifier(hass, entry, database, data_dir)

    async def async_initialize(self) -> None:
        """Prepare local storage and load cache without any network request."""
        await self.hass.async_add_executor_job(self.database.initialize)
        self.data = await self.hass.async_add_executor_job(
            self.database.load_snapshot, self.customer_id
        )
        self.data = self._decorate_snapshot(self.data)
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
        self.entry.async_create_background_task(
            self.hass,
            self._async_process_zalo(snapshot),
            name=f"evn_cskh_monitor zalo {self.customer_id}",
        )
        self.entry.async_create_background_task(
            self.hass,
            self.async_backfill_history(),
            name=f"evn_cskh_monitor history retry {self.customer_id}",
        )

        return snapshot

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
        history_start = datetime(HISTORY_START_YEAR, 1, 1, tzinfo=now.tzinfo)
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
        """Run one background backfill worker at a time."""
        if self._backfill_lock.locked():
            return
        async with self._backfill_lock:
            await self._async_backfill_history()

    async def _async_backfill_history(self) -> None:
        """Backfill older EVN history incrementally in the background.

        A persistent cursor is advanced only after a server batch was received
        and saved. Failed batches therefore cannot create silent holes or mark
        an incomplete history as complete.
        """
        await asyncio.sleep(BACKFILL_DELAY_SECONDS)
        complete = await self.hass.async_add_executor_job(
            self.database.get_state, self.customer_id, "history_backfill_complete"
        )
        if complete == "1":
            return

        now = dt_util.now()
        history_start = datetime(HISTORY_START_YEAR, 1, 1, tzinfo=now.tzinfo)
        recent_cutoff = now - timedelta(days=RECENT_BOOTSTRAP_DAYS + 1)
        if recent_cutoff <= history_start:
            return

        cursor = await self.hass.async_add_executor_job(
            self.database.get_state, self.customer_id, "history_backfill_cursor"
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
                current_start = max(history_start, candidate)
            except ValueError:
                _LOGGER.warning(
                    "Ignoring invalid history cursor for %s: %s",
                    self.customer_id,
                    cursor,
                )

        _LOGGER.info(
            "Starting background EVN history backfill for %s from %s",
            self.customer_id,
            current_start.date(),
        )
        saved = 0
        while current_start.date() <= recent_cutoff.date():
            current_end = min(
                current_start + timedelta(days=DAILY_BATCH_DAYS - 1), recent_cutoff
            )
            try:
                async with self._api_lock:
                    if not self.api.access_token and not await self.api.login():
                        if self.api.last_login_auth_failed:
                            _LOGGER.warning(
                                "Stopping history backfill for %s because EVN authentication failed",
                                self.customer_id,
                            )
                        else:
                            _LOGGER.warning(
                                "Pausing history backfill for %s because EVN login is unavailable: %s",
                                self.customer_id,
                                self.api.last_login_error or "unknown error",
                            )
                        return
                    response = await self.api.get_chisongay(
                        current_start.strftime("%d/%m/%Y"),
                        current_end.strftime("%d/%m/%Y"),
                    )

                if self.api.last_login_auth_failed:
                    _LOGGER.warning(
                        "Stopping history backfill for %s because EVN authentication expired",
                        self.customer_id,
                    )
                    return
                if response is None:
                    _LOGGER.warning(
                        "Pausing history backfill for %s: no response for %s..%s",
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
                    if parsed:
                        await self.hass.async_add_executor_job(
                            self.database.save_daily_records, self.customer_id, parsed
                        )
                        saved += len(parsed)

                # Advance only after the complete server response has been persisted.
                await self.hass.async_add_executor_job(
                    self.database.set_state,
                    self.customer_id,
                    "history_backfill_cursor",
                    current_end.date().isoformat(),
                )
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning(
                    "Pausing history backfill batch %s..%s for %s after error: %s",
                    current_start.date(),
                    current_end.date(),
                    self.customer_id,
                    err,
                )
                return

            current_start = current_end + timedelta(days=1)
            await asyncio.sleep(BACKFILL_PAUSE_SECONDS)

        await self.hass.async_add_executor_job(
            self.database.aggregate_monthly_from_daily, self.customer_id
        )
        await self.hass.async_add_executor_job(
            self.database.set_state,
            self.customer_id,
            "history_backfill_complete",
            "1",
        )
        snapshot = await self.hass.async_add_executor_job(
            self.database.load_snapshot, self.customer_id
        )
        snapshot = self._decorate_snapshot(snapshot)
        self.async_set_updated_data(snapshot)
        _LOGGER.info(
            "Completed background EVN history backfill for %s (%s parsed rows)",
            self.customer_id,
            saved,
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
        await self._async_extract_invoice_files(clean_bills)

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

    async def _async_extract_invoice_files(self, bills: list[dict[str, Any]]) -> None:
        """Persist direct PDF/PNG attachments exposed by an EVN bill response.

        EVN regions do not all expose the same invoice attachment fields. This
        routine intentionally accepts direct URLs, data URLs, and base64 payloads
        from any clearly named field. If a region exposes no attachment in the
        bill response, no synthetic document is created.
        """
        for bill in bills:
            month = _to_int(_first_value(bill, "THANG", "thang", "month"))
            year = _to_int(_first_value(bill, "NAM", "nam", "year"))
            if month is None or year is None:
                continue
            for ext in ("png", "pdf"):
                path = self.data_dir / f"{self.customer_id}_{month}_{year}.{ext}"
                exists = await self.hass.async_add_executor_job(_valid_file, path)
                if exists:
                    continue
                candidate = _find_attachment_candidate(bill, ext)
                if candidate is None:
                    continue
                kind, value = candidate
                content: bytes | None = None
                if kind == "url":
                    content = await self.api.download_file(value)
                elif kind == "base64":
                    content = _decode_base64(value)
                if not content or not _looks_like_file(content, ext):
                    continue
                await self.hass.async_add_executor_job(_write_bytes_atomic, path, content)
                _LOGGER.info("Saved EVN invoice attachment %s", path)


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


def _valid_file(path: Path) -> bool:
    return path.is_file() and path.stat().st_size > 0


def _write_bytes_atomic(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_bytes(content)
    temp.replace(path)


def _decode_base64(value: str) -> bytes | None:
    text = value.strip()
    if text.startswith("data:") and "," in text:
        text = text.split(",", 1)[1]
    try:
        return base64.b64decode(text, validate=False)
    except Exception:  # noqa: BLE001
        return None


def _looks_like_file(content: bytes, ext: str) -> bool:
    if ext == "pdf":
        return content.startswith(b"%PDF-")
    if ext == "png":
        return content.startswith(b"\x89PNG\r\n\x1a\n")
    return False


def _find_attachment_candidate(data: Any, ext: str) -> tuple[str, str] | None:
    """Recursively find an attachment value whose field name clearly matches."""
    if isinstance(data, dict):
        # Prefer fields explicitly mentioning the requested extension/invoice file.
        items = list(data.items())
        items.sort(
            key=lambda item: 0
            if ext in str(item[0]).lower()
            else 1
        )
        for key, value in items:
            key_lower = str(key).lower()
            if isinstance(value, str):
                text = value.strip()
                field_is_relevant = (
                    ext in key_lower
                    or "file" in key_lower
                    or "url" in key_lower
                    or "link" in key_lower
                    or "hoa_don" in key_lower
                    or "hoadon" in key_lower
                )
                if not field_is_relevant:
                    continue
                lower = text.lower()
                if lower.startswith(("http://", "https://")) and (
                    f".{ext}" in lower or ext in key_lower
                ):
                    return ("url", text)
                if lower.startswith("data:") and f"/{ext}" in lower[:80]:
                    return ("base64", text)
                if len(text) > 256 and ("base64" in key_lower or ext in key_lower):
                    decoded = _decode_base64(text)
                    if decoded and _looks_like_file(decoded, ext):
                        return ("base64", text)
            nested = _find_attachment_candidate(value, ext)
            if nested:
                return nested
    elif isinstance(data, list):
        for value in data:
            nested = _find_attachment_candidate(value, ext)
            if nested:
                return nested
    return None
