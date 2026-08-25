"""Diagnostics support for EVN CSKH Monitor."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import EVNCSKHConfigEntry
from .const import (
    CONF_CUSTOMER_ID,
    CONF_PASSWORD,
    CONF_USERNAME,
    CONF_ZALO_ACCOUNT_SELECTION,
    CONF_ZALO_RECIPIENTS,
    CONF_ZALO_THREAD_ID,
)

_REDACT_ENTRY = {CONF_USERNAME, CONF_PASSWORD, CONF_CUSTOMER_ID}
_REDACT_OPTIONS = {CONF_ZALO_ACCOUNT_SELECTION, CONF_ZALO_THREAD_ID, CONF_ZALO_RECIPIENTS}
_REDACT_CUSTOMER = {"id", "name", "phone", "address", "management_unit"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: EVNCSKHConfigEntry
) -> dict[str, Any]:
    """Return useful diagnostics without credentials or raw EVN payloads."""
    snapshot = entry.runtime_data.coordinator.data or {}
    customer = async_redact_data(dict(snapshot.get("customer", {})), _REDACT_CUSTOMER)
    return {
        "entry": async_redact_data(dict(entry.data), _REDACT_ENTRY),
        "options": async_redact_data(dict(entry.options), _REDACT_OPTIONS),
        "customer": customer,
        "last_sync": snapshot.get("last_sync"),
        "partial_errors": snapshot.get("partial_errors", []),
        "daily_record_count": len(snapshot.get("daily", [])),
        "monthly_record_count": len(snapshot.get("monthly", [])),
        "outage_record_count": len(snapshot.get("outages", [])),
        "notification_record_count": len(snapshot.get("notifications", [])),
        "raw_server_record_count": snapshot.get("raw_record_count", 0),
        "cache_loaded": entry.runtime_data.coordinator.cache_loaded,
        "last_update_success": entry.runtime_data.coordinator.last_update_success,
    }
