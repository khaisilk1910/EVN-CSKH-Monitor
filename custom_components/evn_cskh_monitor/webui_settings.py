"""Domain-wide persistent settings for the EVN CSKH Monitor WebUI."""

from __future__ import annotations

import asyncio
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import (
    CONF_WEBUI_SUBTITLE,
    CONF_WEBUI_THEME,
    CONF_WEBUI_TITLE,
    DEFAULT_WEBUI_SUBTITLE,
    DEFAULT_WEBUI_THEME,
    DEFAULT_WEBUI_TITLE,
    DOMAIN,
    WEBUI_SETTINGS_DATA_KEY,
    WEBUI_STORAGE_KEY,
    WEBUI_STORAGE_VERSION,
    WEBUI_THEMES,
)

WEBUI_TITLE_MAX_LENGTH = 80
WEBUI_SUBTITLE_MAX_LENGTH = 180


class EVNWebUISettingsManager:
    """Own one WebUI configuration shared by every EVN config entry."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._store = Store[dict[str, str]](
            hass,
            WEBUI_STORAGE_VERSION,
            WEBUI_STORAGE_KEY,
            # This payload is tiny, but keeping JSON serialization out of the
            # event loop follows the Home Assistant 2026.x storage guidance.
            serialize_in_event_loop=False,
        )
        self._lock = asyncio.Lock()
        self._settings = self._defaults()

    @staticmethod
    def _defaults() -> dict[str, str]:
        return {
            CONF_WEBUI_TITLE: DEFAULT_WEBUI_TITLE,
            CONF_WEBUI_SUBTITLE: DEFAULT_WEBUI_SUBTITLE,
            CONF_WEBUI_THEME: DEFAULT_WEBUI_THEME,
        }

    @staticmethod
    def _normalize(data: dict[str, Any] | None) -> dict[str, str]:
        source = data or {}
        title = str(source.get(CONF_WEBUI_TITLE, DEFAULT_WEBUI_TITLE)).strip()
        subtitle = str(
            source.get(CONF_WEBUI_SUBTITLE, DEFAULT_WEBUI_SUBTITLE)
        ).strip()
        theme = str(source.get(CONF_WEBUI_THEME, DEFAULT_WEBUI_THEME)).strip()
        if not title:
            title = DEFAULT_WEBUI_TITLE
        if theme not in WEBUI_THEMES:
            theme = DEFAULT_WEBUI_THEME
        return {
            CONF_WEBUI_TITLE: title[:WEBUI_TITLE_MAX_LENGTH],
            CONF_WEBUI_SUBTITLE: subtitle[:WEBUI_SUBTITLE_MAX_LENGTH],
            CONF_WEBUI_THEME: theme,
        }

    def _legacy_entry_settings(self) -> dict[str, str] | None:
        """Pick one old per-entry WebUI value when upgrading from 2026.8.26.x.

        Older builds stored title/subtitle on every meter. The panel is global,
        so only one value can survive migration. Prefer a genuinely customized
        entry; otherwise use the first legacy entry deterministically.
        """
        candidates: list[dict[str, str]] = []
        entries = sorted(
            self._hass.config_entries.async_entries(DOMAIN),
            key=lambda entry: entry.entry_id,
        )
        for entry in entries:
            options = dict(entry.options)
            if (
                CONF_WEBUI_TITLE not in options
                and CONF_WEBUI_SUBTITLE not in options
            ):
                continue
            candidate = self._normalize(options)
            candidates.append(candidate)
            if (
                candidate[CONF_WEBUI_TITLE] != DEFAULT_WEBUI_TITLE
                or candidate[CONF_WEBUI_SUBTITLE] != DEFAULT_WEBUI_SUBTITLE
            ):
                return candidate
        return candidates[0] if candidates else None

    async def async_load(self) -> None:
        """Load global WebUI settings without any cloud/network access."""
        stored = await self._store.async_load()
        if stored is not None:
            self._settings = self._normalize(stored)
            return

        legacy = self._legacy_entry_settings()
        if legacy is None:
            self._settings = self._defaults()
            return

        self._settings = legacy
        # Persist the one-time migration so future entry removals cannot change
        # the global title/subtitle that the user already had configured.
        await self._store.async_save(dict(self._settings))

    def as_dict(self) -> dict[str, str]:
        """Return a detached JSON-serializable snapshot."""
        return dict(self._settings)

    async def async_update(self, data: dict[str, Any]) -> dict[str, str]:
        """Validate and persist a complete settings update."""
        normalized = self._normalize(data)
        async with self._lock:
            if normalized != self._settings:
                self._settings = normalized
                await self._store.async_save(dict(self._settings))
        return self.as_dict()


def webui_settings_manager(hass: HomeAssistant) -> EVNWebUISettingsManager:
    """Return the domain-level WebUI settings manager."""
    return hass.data[DOMAIN][WEBUI_SETTINGS_DATA_KEY]
