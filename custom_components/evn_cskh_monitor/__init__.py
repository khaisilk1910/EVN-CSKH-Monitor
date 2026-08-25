"""EVN CSKH Monitor integration."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path
from typing import Any

from homeassistant.components import frontend
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv

from .const import (
    CONF_CUSTOMER_ID,
    CONF_PASSWORD,
    CONF_REGION,
    CONF_USERNAME,
    DATA_DIR_NAME,
    DB_FILENAME,
    DOMAIN,
    NAME,
    PANEL_URL_PATH,
    PLATFORMS,
    WEBUI_URL_PREFIX,
)
from .coordinator import EVNDataUpdateCoordinator
from .database import EVNDatabase
from .evn_api import EVNAPI
from .views import async_register_views

_LOGGER = logging.getLogger(__name__)
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


@dataclass(slots=True)
class EVNCSKHRuntimeData:
    """Runtime objects owned by one EVN CSKH Monitor config entry."""

    api: EVNAPI
    coordinator: EVNDataUpdateCoordinator
    database: EVNDatabase
    data_dir: Path


type EVNCSKHConfigEntry = ConfigEntry[EVNCSKHRuntimeData]


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Set up domain-level HTTP resources and the dashboard panel once."""
    webui_path = Path(__file__).parent / "webui"
    await hass.http.async_register_static_paths(
        [StaticPathConfig(WEBUI_URL_PREFIX, str(webui_path), False)]
    )
    async_register_views(hass)

    if not frontend.async_panel_exists(hass, PANEL_URL_PATH):
        frontend.async_register_built_in_panel(
            hass,
            component_name="iframe",
            sidebar_title=NAME,
            sidebar_icon="mdi:transmission-tower",
            frontend_url_path=PANEL_URL_PATH,
            config={"url": f"{WEBUI_URL_PREFIX}/index.html"},
            require_admin=False,
        )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: EVNCSKHConfigEntry) -> bool:
    """Set up one EVN account without waiting for the EVN cloud."""
    customer_id = str(entry.data[CONF_CUSTOMER_ID]).strip().upper()
    data_dir = Path(hass.config.path(DATA_DIR_NAME))
    database = EVNDatabase(data_dir / DB_FILENAME)
    api = EVNAPI(
        hass,
        str(entry.data[CONF_REGION]),
        str(entry.data[CONF_USERNAME]),
        str(entry.data[CONF_PASSWORD]),
        customer_id,
    )
    coordinator = EVNDataUpdateCoordinator(
        hass,
        entry,
        api,
        database,
        data_dir,
        customer_id,
    )

    # Only local SQLite initialization/cache loading happens during setup. All
    # potentially slow EVN network work starts after entities are registered.
    await coordinator.async_initialize()
    entry.runtime_data = EVNCSKHRuntimeData(api, coordinator, database, data_dir)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # A background task explicitly does not hold Home Assistant startup. The
    # coordinator still provides hourly updates after entities subscribe.
    entry.async_create_background_task(
        hass,
        coordinator.async_refresh(),
        name=f"{DOMAIN} initial refresh {entry.entry_id}",
    )
    entry.async_create_background_task(
        hass,
        coordinator.async_backfill_history(),
        name=f"{DOMAIN} history backfill {entry.entry_id}",
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: EVNCSKHConfigEntry) -> bool:
    """Unload a config entry and its platforms."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        await entry.runtime_data.api.close()
    return unload_ok


async def _async_update_listener(
    hass: HomeAssistant, entry: EVNCSKHConfigEntry
) -> None:
    """Reload once when options or connection data changes."""
    await hass.config_entries.async_reload(entry.entry_id)
