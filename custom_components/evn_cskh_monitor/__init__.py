"""EVN CSKH Monitor integration."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path
from typing import Any

from homeassistant.components import frontend, panel_custom
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.start import async_at_started

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
    VERSION,
    WEBUI_DIR_NAME,
    WEBUI_URL_PREFIX,
)
from .coordinator import EVNDataUpdateCoordinator
from .database import EVNDatabase
from .evn_api import EVNAPI
from .views import async_register_views
from .webui_deployer import ensure_webui_assets, prepare_webui_directory

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
    """Set up domain-level authenticated WebUI resources once."""
    data_dir = Path(hass.config.path(DATA_DIR_NAME))
    webui_dir = data_dir / WEBUI_DIR_NAME

    # StaticPathConfig expects a real directory on first install. Creating the
    # empty runtime directory is the only filesystem operation awaited here and
    # it runs in the executor. Decompression/writes are deliberately background
    # work so they never hold up Home Assistant startup.
    await hass.async_add_executor_job(prepare_webui_directory, webui_dir)

    # Deploy/update the runtime WebUI without blocking startup. On ordinary
    # restarts this performs only a marker read + three stat checks and writes
    # nothing. A version change (including a HACS update) or missing asset causes
    # an atomic redeploy into /config/evncskh/webui only.
    async def _async_ensure_webui() -> None:
        try:
            await hass.async_add_executor_job(ensure_webui_assets, webui_dir, VERSION)
        except Exception:  # noqa: BLE001 - background deployment must not break HA
            _LOGGER.exception("Unable to deploy EVN CSKH Monitor WebUI runtime")

    hass.async_create_background_task(
        _async_ensure_webui(),
        f"{DOMAIN} webui deployment",
    )

    # panel.js is cache-busted by VERSION in module_url, so static caching is
    # safe and avoids repeatedly transferring unchanged frontend assets.
    await hass.http.async_register_static_paths(
        [StaticPathConfig(WEBUI_URL_PREFIX, str(webui_dir), True)]
    )
    async_register_views(hass)

    # Use Home Assistant's custom-panel bridge instead of an ordinary iframe.
    # The bridge passes the authenticated `hass` object to panel.js, allowing
    # hass.callApi() to access our requires_auth=True endpoints without scraping
    # tokens from browser storage.
    if not frontend.async_panel_exists(hass, PANEL_URL_PATH):
        await panel_custom.async_register_panel(
            hass,
            frontend_url_path=PANEL_URL_PATH,
            webcomponent_name="evn-cskh-monitor-panel",
            module_url=f"{WEBUI_URL_PREFIX}/panel.js?v={VERSION}",
            sidebar_title=NAME,
            sidebar_icon="mdi:transmission-tower",
            embed_iframe=True,
            require_admin=False,
            config={},
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

    # Do not start EVN network traffic until Home Assistant has fully started.
    # If an entry is added/reloaded while HA is already running, async_at_started
    # invokes this callback immediately. The refresh itself remains an
    # entry-owned background task and is cancelled automatically on unload.
    def _schedule_initial_refresh(_: HomeAssistant) -> None:
        entry.async_create_background_task(
            hass,
            coordinator.async_refresh(),
            name=f"{DOMAIN} initial refresh {entry.entry_id}",
        )

    entry.async_on_unload(async_at_started(hass, _schedule_initial_refresh))
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
    """Apply option changes cheaply and reload only connection changes.

    Billing/Zalo/WebUI options are read dynamically, so reloading the whole
    config entry would unnecessarily restart the EVN client and launch another
    cloud refresh. Credential or region changes still require a full reload.
    """
    runtime = getattr(entry, "runtime_data", None)
    if runtime is None:
        return

    api = runtime.api
    connection_changed = (
        str(entry.data.get(CONF_REGION, "")) != api.region
        or str(entry.data.get(CONF_USERNAME, "")) != api.username
        or str(entry.data.get(CONF_PASSWORD, "")) != api.password
        or str(entry.data.get(CONF_CUSTOMER_ID, "")).strip().upper()
        != api.customer_id
    )
    if connection_changed:
        await hass.config_entries.async_reload(entry.entry_id)
        return

    # Re-render cached sensors immediately for billing-period option changes.
    # ZaloNotifier and WebUI endpoints read entry.options on demand as well.
    runtime.coordinator.async_update_listeners()
