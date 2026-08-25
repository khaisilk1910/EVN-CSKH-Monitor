"""Constants for EVN CSKH Monitor."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.const import Platform

DOMAIN = "evn_cskh_monitor"
NAME = "EVN CSKH Monitor"
VERSION = "2026.8.25"
PLATFORMS: list[Platform] = [Platform.SENSOR]

# Connection/config-entry data.
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_CUSTOMER_ID = "customer_id"
CONF_REGION = "region"

# User-adjustable options.
CONF_NGAYDAUKY = "ngaydauky"
CONF_ZALO_TYPE = "zalo_type"
CONF_ZALO_ACCOUNT_SELECTION = "zalo_account_selection"
CONF_ZALO_THREAD_ID = "zalo_thread_id"
CONF_ZALO_SEND_INVOICE = "zalo_send_invoice"
CONF_ZALO_SEND_DAILY = "zalo_send_daily"
CONF_ZALO_SEND_OUTAGE = "zalo_send_outage"

DEFAULT_NGAYDAUKY = 1
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

# Polling cadence. EVN daily readings normally change once per day, while bills
# and outage notifications may change independently. One-hour polling is a good
# compromise without hammering the upstream services.
UPDATE_INTERVAL = timedelta(hours=1)
REFRESH_WINDOW_DAYS = 10
HISTORY_START_YEAR = 2020
DAILY_BATCH_DAYS = 15
RECENT_BOOTSTRAP_DAYS = 45
BACKFILL_DELAY_SECONDS = 30
BACKFILL_PAUSE_SECONDS = 0.25

# HTTP/network behavior.
REQUEST_TIMEOUT_SECONDS = 30

# Web panel/API paths are unique to this new integration.
PANEL_URL_PATH = "evn_cskh_monitor"
WEBUI_URL_PREFIX = "/evncskh-monitor"
API_URL_PREFIX = "/api/evncskh"
