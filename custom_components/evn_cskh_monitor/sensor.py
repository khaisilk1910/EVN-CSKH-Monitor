"""Sensor platform for EVN CSKH Monitor.

Entities are calculated from the coordinator's in-memory snapshot. No entity
property opens SQLite or performs network I/O, which keeps the Home Assistant
event loop responsive even with many state renders.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import UnitOfEnergy
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import EVNCSKHConfigEntry
from .calculations import (
    consumption_on,
    future_outages,
    nearest_reading,
    period_chain,
    period_consumption,
    period_cost,
    period_rows,
)
from .const import CONF_NGAYDAUKY, DEFAULT_NGAYDAUKY, DOMAIN, NAME, VERSION

SENSORS: dict[str, tuple[str, str]] = {
    "chi_so_cuoi_ky": ("Chỉ số cuối kỳ trước", "mdi:counter"),
    "chi_so_tam_chot": ("Chỉ số tạm chốt", "mdi:counter"),
    "tieu_thu_ky_nay": ("Tiêu thụ kỳ này", "mdi:lightning-bolt"),
    "tien_dien_ky_nay": ("Tiền điện kỳ này", "mdi:cash"),
    "tieu_thu_ky_truoc": ("Tiêu thụ kỳ trước", "mdi:lightning-bolt"),
    "tien_dien_ky_truoc": ("Tiền điện kỳ trước", "mdi:cash"),
    "tieu_thu_ky_truoc_nua": ("Tiêu thụ kỳ trước nữa", "mdi:lightning-bolt"),
    "tien_dien_ky_truoc_nua": ("Tiền điện kỳ trước nữa", "mdi:cash"),
    "tieu_thu_hom_nay": ("Tiêu thụ hôm nay", "mdi:calendar-today"),
    "tieu_thu_hom_qua": ("Tiêu thụ hôm qua", "mdi:calendar-arrow-left"),
    "tieu_thu_hom_kia": ("Tiêu thụ hôm kia", "mdi:calendar-minus"),
    "chi_tiet_dien_tieu_thu_ky_nay": ("Chi tiết kỳ này", "mdi:table-large"),
    "tien_dien_san_luong_nam_nay": ("Hóa đơn năm nay", "mdi:receipt-text"),
    "lich_cat_dien": ("Lịch cắt điện", "mdi:calendar-alert"),
    "lan_cap_nhat_cuoi": ("Lần cập nhật cuối", "mdi:update"),
    "tien_no": ("Tiền nợ", "mdi:cash-clock"),
    "thong_tin_khach_hang": ("Thông tin khách hàng", "mdi:account-box"),
    "thong_bao_ngung_dien": ("Thông báo ngừng điện", "mdi:transmission-tower-off"),
    "thong_bao_hoa_don": ("Thông báo hóa đơn", "mdi:receipt"),
    "thong_bao_truyen_thong": ("Thông báo truyền thông", "mdi:bullhorn"),
}

# Coordinator centralizes all inbound API updates for this read-only platform.
PARALLEL_UPDATES = 0


NOTIFICATION_CATEGORIES = {
    "thong_bao_ngung_dien": "NGUNGCAP_DIEN",
    "thong_bao_hoa_don": "HOADON",
    "thong_bao_truyen_thong": "TRUYEN_THONG",
}


async def async_setup_entry(
    hass,
    entry: EVNCSKHConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up EVN CSKH Monitor sensors from one config entry."""
    coordinator = entry.runtime_data.coordinator
    async_add_entities(
        EVNCSKHSensor(entry, sensor_key, name, icon)
        for sensor_key, (name, icon) in SENSORS.items()
    )


class EVNCSKHSensor(CoordinatorEntity, SensorEntity):
    """A lightweight sensor calculated from the coordinator cache."""

    _attr_has_entity_name = True

    def __init__(
        self,
        entry: EVNCSKHConfigEntry,
        sensor_key: str,
        name: str,
        icon: str,
    ) -> None:
        super().__init__(entry.runtime_data.coordinator)
        self.entry = entry
        self.sensor_key = sensor_key
        self.customer_id = entry.runtime_data.coordinator.customer_id
        self._attr_name = name
        self._attr_icon = icon
        self._attr_unique_id = f"{self.customer_id}_{sensor_key}"

        if (
            sensor_key.startswith("chi_so_")
            or sensor_key.startswith("tieu_thu_")
            or sensor_key == "chi_tiet_dien_tieu_thu_ky_nay"
        ):
            self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
            self._attr_device_class = SensorDeviceClass.ENERGY
        elif (
            sensor_key.startswith("tien_dien_")
            and sensor_key != "tien_dien_san_luong_nam_nay"
        ) or sensor_key == "tien_no":
            self._attr_native_unit_of_measurement = "VND"
            self._attr_device_class = SensorDeviceClass.MONETARY
        elif sensor_key == "lan_cap_nhat_cuoi":
            self._attr_device_class = SensorDeviceClass.TIMESTAMP

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self.customer_id)},
            name=f"{NAME} {self.customer_id}",
            manufacturer="EVN",
            model=NAME,
            sw_version=VERSION,
        )

    @property
    def available(self) -> bool:
        # Cached local data remains usable during an EVN outage. The coordinator
        # exposes partial_errors and last_sync so stale data is transparent.
        return bool(self.coordinator.cache_loaded)

    @property
    def native_value(self) -> Any:
        value, _ = self._calculate()
        return value

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        _, attributes = self._calculate()
        return attributes or None

    @property
    def _billing_start(self) -> int:
        return int(
            self.entry.options.get(
                CONF_NGAYDAUKY,
                self.entry.data.get(CONF_NGAYDAUKY, DEFAULT_NGAYDAUKY),
            )
        )

    def _calculate(self) -> tuple[Any, dict[str, Any]]:
        snapshot = self.coordinator.data or {}
        today = dt_util.now().date()
        periods = period_chain(self._billing_start, today)
        current_start, current_end = periods[0]
        prev_start, prev_end = periods[1]
        prev2_start, prev2_end = periods[2]
        key = self.sensor_key

        if key == "chi_so_cuoi_ky":
            value, reading_date = nearest_reading(
                snapshot, current_start - timedelta(days=1), direction="before"
            )
            return _round(value), {"ngay_chot": reading_date.isoformat() if reading_date else None}

        if key == "chi_so_tam_chot":
            value, reading_date = nearest_reading(snapshot, today, direction="before")
            return _round(value), {"ngay_chot": reading_date.isoformat() if reading_date else None}

        if key in {
            "tieu_thu_ky_nay",
            "tieu_thu_ky_truoc",
            "tieu_thu_ky_truoc_nua",
        }:
            start, end = {
                "tieu_thu_ky_nay": (current_start, today),
                "tieu_thu_ky_truoc": (prev_start, prev_end),
                "tieu_thu_ky_truoc_nua": (prev2_start, prev2_end),
            }[key]
            return _round(period_consumption(snapshot, start, end)), {
                "bat_dau": start.isoformat(),
                "ket_thuc": end.isoformat(),
            }

        if key in {"tien_dien_ky_nay", "tien_dien_ky_truoc", "tien_dien_ky_truoc_nua"}:
            start, end = {
                "tien_dien_ky_nay": (current_start, today),
                "tien_dien_ky_truoc": (prev_start, prev_end),
                "tien_dien_ky_truoc_nua": (prev2_start, prev2_end),
            }[key]
            cost, details = period_cost(snapshot, start, end, self._billing_start)
            return (round(cost) if cost is not None else None), {
                "bat_dau": start.isoformat(),
                "ket_thuc": end.isoformat(),
                **details,
            }

        if key in {"tieu_thu_hom_nay", "tieu_thu_hom_qua", "tieu_thu_hom_kia"}:
            offset = {
                "tieu_thu_hom_nay": 0,
                "tieu_thu_hom_qua": 1,
                "tieu_thu_hom_kia": 2,
            }[key]
            target = today - timedelta(days=offset)
            return _round(consumption_on(snapshot, target)), {"ngay": target.isoformat()}

        if key == "chi_tiet_dien_tieu_thu_ky_nay":
            rows = period_rows(snapshot, current_start, today)
            details = [
                {
                    "ngay": row.get("date_display"),
                    "chi_so": row.get("reading"),
                    "dien_tieu_thu_kwh": row.get("consumption"),
                }
                for row in rows
            ]
            total = period_consumption(snapshot, current_start, today)
            return _round(total), {
                "bat_dau": current_start.isoformat(),
                "ket_thuc": today.isoformat(),
                "so_ngay_co_du_lieu": sum(1 for row in rows if row.get("consumption") is not None),
                "chi_tiet": details,
            }

        if key == "tien_dien_san_luong_nam_nay":
            rows = [
                row for row in snapshot.get("monthly", []) if int(row.get("year") or 0) == today.year
            ]
            rows.sort(key=lambda row: int(row.get("month") or 0), reverse=True)
            return today.year, {
                "hoa_don": [
                    {
                        "thang": row.get("month"),
                        "nam": row.get("year"),
                        "tien_dien": row.get("cost"),
                        "san_luong_kwh": row.get("consumption"),
                        "trang_thai": row.get("status"),
                        "nguon": row.get("source"),
                    }
                    for row in rows
                ],
                "tong_tien_hoa_don": round(
                    sum(float(row["cost"]) for row in rows if row.get("cost") is not None)
                ),
                "tong_san_luong_kwh": _round(
                    sum(float(row["consumption"]) for row in rows if row.get("consumption") is not None)
                ),
            }

        if key == "lich_cat_dien":
            future = future_outages(snapshot, today)
            future_clean = [{k: v for k, v in row.items() if k != "_date"} for row in future]
            if future_clean:
                first = future_clean[0]
                return "Có lịch cắt điện", {"gan_nhat": first, "tuong_lai": future_clean}
            return "Không có lịch cắt điện", {"tuong_lai": []}

        if key == "lan_cap_nhat_cuoi":
            raw = snapshot.get("last_sync")
            if not raw:
                return None, {"partial_errors": snapshot.get("partial_errors", [])}
            try:
                value = datetime.fromisoformat(str(raw))
            except ValueError:
                return None, {"raw": raw, "partial_errors": snapshot.get("partial_errors", [])}
            return value, {"partial_errors": snapshot.get("partial_errors", [])}

        if key == "tien_no":
            debt = snapshot.get("debt", {})
            amount = debt.get("amount")
            if amount is None:
                return None, {"cap_nhat": debt.get("updated_at")}
            return round(float(amount)), {"cap_nhat": debt.get("updated_at")}

        if key == "thong_tin_khach_hang":
            customer = snapshot.get("customer", {})
            return customer.get("name") or self.customer_id, {
                "ma_khach_hang": self.customer_id,
                "so_dien_thoai": customer.get("phone"),
                "dia_chi": customer.get("address"),
                "khu_vuc": customer.get("region"),
                "don_vi_quan_ly": customer.get("management_unit"),
                "raw_server_record_count": snapshot.get("raw_record_count", 0),
            }

        if key in NOTIFICATION_CATEGORIES:
            category = NOTIFICATION_CATEGORIES[key]
            items = [
                item for item in snapshot.get("notifications", []) if item.get("category") == category
            ][:5]
            if not items:
                return "Chưa có thông báo", {"gan_day": []}
            latest = items[0]
            text = str(latest.get("content") or latest.get("title") or "")
            return text[:250] or "Có thông báo", {
                "tieu_de": latest.get("title"),
                "thoi_gian": latest.get("time"),
                "da_doc": latest.get("read"),
                "noi_dung": latest.get("content"),
                "gan_day": items,
            }

        return None, {}


def _round(value: float | int | None, digits: int = 3) -> float | int | None:
    if value is None:
        return None
    result = round(float(value), digits)
    return int(result) if result.is_integer() else result
