"""Constants for EVN CSKH Monitor."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.const import Platform

DOMAIN = "evn_cskh_monitor"
NAME = "EVN CSKH Monitor"
VERSION = "2026.8.25.3"
PLATFORMS: list[Platform] = [Platform.SENSOR]

# Connection/config-entry data.
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_CUSTOMER_ID = "customer_id"
CONF_REGION = "region"

# User-adjustable options.
CONF_NGAYDAUKY = "ngaydauky"
CONF_WEBUI_TITLE = "webui_title"
CONF_WEBUI_SUBTITLE = "webui_subtitle"

# Multi-recipient Zalo configuration. Each item in CONF_ZALO_RECIPIENTS is a
# serializable dict so Home Assistant can persist it in the config entry options.
CONF_ZALO_RECIPIENTS = "zalo_recipients"
CONF_ZALO_RECIPIENT_NAME = "zalo_recipient_name"
CONF_ZALO_RECIPIENT_ENABLED = "zalo_recipient_enabled"
CONF_ZALO_TYPE = "zalo_type"
CONF_ZALO_ACCOUNT_SELECTION = "zalo_account_selection"
CONF_ZALO_THREAD_ID = "zalo_thread_id"
CONF_ZALO_SEND_INVOICE = "zalo_send_invoice"
CONF_ZALO_SEND_DAILY = "zalo_send_daily"
CONF_ZALO_SEND_OUTAGE = "zalo_send_outage"
CONF_ZALO_ACTION = "zalo_action"
CONF_ZALO_RECIPIENT_ID = "zalo_recipient_id"
CONF_CONFIRM_DELETE = "confirm_delete"

DEFAULT_NGAYDAUKY = 1
DEFAULT_WEBUI_TITLE = NAME
DEFAULT_WEBUI_SUBTITLE = "Dữ liệu EVN, hóa đơn, sản lượng và lịch cắt điện"
DEFAULT_ZALO_TYPE = 0
DEFAULT_ZALO_ACCOUNT_SELECTION = ""
DEFAULT_ZALO_THREAD_ID = ""
DEFAULT_ZALO_SEND_INVOICE = False
DEFAULT_ZALO_SEND_DAILY = False
DEFAULT_ZALO_SEND_OUTAGE = False

# Regional backends. NPC is the official Northern Power Corporation region
# identifier used by EVN Northern Power Corporation services.
REGION_HN = "HN"
REGION_NPC = "NPC"
REGION_CPC = "CPC"
REGION_SPC = "SPC"
REGION_HCMC = "HCMC"

CUSTOMER_ID_PREFIX_REGION = {
    "PD": REGION_HN,
    "PE": REGION_HCMC,
    "PA": REGION_NPC,
    "PH": REGION_NPC,
    "PM": REGION_NPC,
    "PN": REGION_NPC,
    "PC": REGION_CPC,
    "PP": REGION_CPC,
    "PQ": REGION_CPC,
    "PB": REGION_SPC,
    "PK": REGION_SPC,
}

# Local storage. The path resolves to /config/evncskh on a normal HA install.
DATA_DIR_NAME = "evncskh"
DB_FILENAME = "evncskh.db"
WEBUI_DIR_NAME = "webui"

# Polling cadence. EVN daily readings normally change once per day, while bills
# and outage notifications may change independently. One-hour polling is a good
# compromise without hammering the upstream services.
UPDATE_INTERVAL = timedelta(hours=1)
REFRESH_WINDOW_DAYS = 10
RECENT_BOOTSTRAP_DAYS = 45
DAILY_BATCH_DAYS = 15

# Initial history bootstrap. A newly added meter loads the whole previous
# calendar year plus the current year through today, if EVN has data. This is a
# background task and never blocks Home Assistant startup.
HISTORY_PREVIOUS_YEARS = 1
HISTORY_BOOTSTRAP_DELAY_SECONDS = 15
HISTORY_BATCH_PAUSE_SECONDS = 0.25
HISTORY_MONTH_PAUSE_SECONDS = 0.20

# HTTP/network behavior.
REQUEST_TIMEOUT_SECONDS = 30

# Web panel/API paths are unique to this integration. Only the webui subfolder is
# exposed as static content; the SQLite database and invoice files remain private.
PANEL_URL_PATH = "evn_cskh_monitor"
WEBUI_URL_PREFIX = "/evncskh-monitor"
API_URL_PREFIX = "/api/evncskh"
