"""Naming helpers for EVN CSKH Monitor."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN, NAME


def device_display_name(
    hass: HomeAssistant,
    entry: ConfigEntry,
    customer_id: str,
) -> str:
    """Return the current Home Assistant device name for a meter.
    Home Assistant Core 2026.8 restricts a device to one config entry and
    deprecates the ambiguous ``async_get_device`` lookup. Scope the identifier
    lookup to the owning config entry so user renames (``name_by_user``) are
    reflected immediately in notifications and the WebUI.
    """
    registry = dr.async_get(hass)
    device = registry.async_get_device_by_identifier(
        (DOMAIN, customer_id), entry.entry_id
    )
    if device is not None:
        for candidate in (device.name_by_user, device.name):
            if candidate and str(candidate).strip():
                return str(candidate).strip()
    if entry.title and entry.title.strip():
        return entry.title.strip()
    return f"{NAME} {customer_id}"
