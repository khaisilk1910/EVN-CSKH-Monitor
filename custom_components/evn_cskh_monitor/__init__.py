"""EVN CSKH Monitor integration."""

from __future__ import annotations

from dataclasses import dataclass
import logging

from pathlib import Path
import shutil
import tempfile
from homeassistant.components import frontend, panel_custom

from homeassistant.components.http import StaticPathConfig

from homeassistant.config_entries import ConfigEntry

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.typing import ConfigType
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
    WEBUI_SETTINGS_DATA_KEY,
    WEBUI_URL_PREFIX,
)
from .coordinator import EVNDataUpdateCoordinator
from .database import EVNDatabase
from .evn_api import EVNAPI
from .frontend_assets import decode_frontend_assets
from .views import async_register_views
from .webui_settings import EVNWebUISettingsManager
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

def _sync_webui_assets(destination: Path, icon_source: Path, version: str) -> None:
    """Materialize WebUI assets only under /config/evncskh/webui.
    The custom component deliberately ships with no ``webui`` directory. The
    JS/HTML source is stored compactly in :mod:`frontend_assets` and decoded
    here in Home Assistant's executor. The existing integration brand icon is
    copied into the runtime WebUI directory. Only this generated directory is
    exposed by the static route; database and invoice files remain private.
    """
    marker = destination / ".version"
    required = ("panel.js", "index.html", "icon.png")
    if (
        marker.is_file()
        and marker.read_text(encoding="utf-8").strip() == version
        and all((destination / name).is_file() for name in required)
    ):
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(
        tempfile.mkdtemp(prefix=".webui-stage-", dir=str(destination.parent))
    )
    backup = destination.with_name(f".{destination.name}.previous")
    moved_old = False
    try:
        for filename, content in decode_frontend_assets().items():
            (stage / filename).write_bytes(content)
        shutil.copyfile(icon_source, stage / "icon.png")
        (stage / ".version").write_text(version, encoding="utf-8")
        if backup.exists():
            shutil.rmtree(backup)
        if destination.exists():
            destination.replace(backup)
            moved_old = True
        stage.replace(destination)
        if backup.exists():
            shutil.rmtree(backup)
    except Exception:
        # Keep the previous known-good WebUI if replacement fails halfway.
        if moved_old and not destination.exists() and backup.exists():
            backup.replace(destination)
        raise
    finally:
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)

async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up domain-level authenticated WebUI resources once."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    if WEBUI_SETTINGS_DATA_KEY not in domain_data:
        settings_manager = EVNWebUISettingsManager(hass)
        await settings_manager.async_load()
        domain_data[WEBUI_SETTINGS_DATA_KEY] = settings_manager
    component_dir = Path(__file__).parent
    icon_source = component_dir / "brand" / "icon.png"
    data_dir = Path(hass.config.path(DATA_DIR_NAME))
    webui_dir = data_dir / WEBUI_DIR_NAME

    # Decode/write the runtime frontend only when the integration version
    # changes. All compression and filesystem work stays off the event loop.
    await hass.async_add_executor_job(
        _sync_webui_assets, webui_dir, icon_source, VERSION
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
    @callback
    def _schedule_initial_refresh(started_hass: HomeAssistant) -> None:
        """Schedule the first cloud refresh from Home Assistant's event loop.
        ``async_at_started`` wraps synchronous callables in ``HassJob``.  A
        plain synchronous function is treated as an executor job, which means
        calling ``ConfigEntry.async_create_background_task`` from it is unsafe:
        that API must run on Home Assistant's event-loop thread.  Marking this
        function with ``@callback`` keeps it on the loop.
        ``eager_start=False`` is equally intentional.  When a config entry is
        added/reloaded while Home Assistant is already running,
        ``async_at_started`` invokes the callback during ``async_setup_entry``.
        Deferring the coroutine until the next loop iteration lets Home
        Assistant mark the entry LOADED first and avoids re-entrant cloud work
        during config-entry setup.
        """
        if started_hass.is_stopping:
            return
        entry.async_create_background_task(
            hass,
            coordinator.async_refresh(),
            name=f"{DOMAIN} initial refresh {entry.entry_id}",
            eager_start=False,
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
    Billing/Zalo options are read dynamically, so reloading the whole config
    entry would unnecessarily restart the EVN client and launch another cloud
    refresh. Domain-wide WebUI settings are stored separately and never reload
    a meter. Credential or region changes still require a full reload.
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
    # ZaloNotifier reads its per-meter entry.options on demand.
    runtime.coordinator.async_update_listeners()
