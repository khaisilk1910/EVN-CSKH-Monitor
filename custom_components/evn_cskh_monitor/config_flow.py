"""Config and options flows for EVN CSKH Monitor."""

from __future__ import annotations

import asyncio
from datetime import timedelta
import logging
import re
from typing import Any, override
from uuid import uuid4

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.util import dt as dt_util

from .const import (
    CONF_CONFIRM_DELETE,
    CONF_CUSTOMER_ID,
    CONF_NGAYDAUKY,
    CONF_PASSWORD,
    CONF_REGION,
    CONF_USERNAME,
    CONF_WEBUI_SUBTITLE,
    CONF_WEBUI_TITLE,
    CONF_ZALO_ACCOUNT_SELECTION,
    CONF_ZALO_ACTION,
    CONF_ZALO_RECIPIENT_ENABLED,
    CONF_ZALO_RECIPIENT_ID,
    CONF_ZALO_RECIPIENT_NAME,
    CONF_ZALO_RECIPIENTS,
    CONF_ZALO_SEND_DAILY,
    CONF_ZALO_SEND_INVOICE,
    CONF_ZALO_SEND_OUTAGE,
    CONF_ZALO_THREAD_ID,
    CONF_ZALO_TYPE,
    CUSTOMER_ID_PREFIX_REGION,
    DEFAULT_NGAYDAUKY,
    DEFAULT_ZALO_SEND_DAILY,
    DEFAULT_ZALO_SEND_INVOICE,
    DEFAULT_ZALO_SEND_OUTAGE,
    DEFAULT_ZALO_TYPE,
    DOMAIN,
    MAX_CONCURRENT_EVN_REQUESTS,
    NETWORK_SEMAPHORE_DATA_KEY,
    REGION_CPC,
    REGION_HCMC,
    REGION_HN,
    REGION_NPC,
    REGION_SPC,
)
from .evn_api import EVNAPI
from .zalo_config import (
    form_defaults,
    normalize_zalo_recipients,
    recipient_from_form,
    without_legacy_zalo_options,
)

_LOGGER = logging.getLogger(__name__)

REGION_OPTIONS = [
    {"value": REGION_HN, "label": "Hà Nội (HN)"},
    {"value": REGION_NPC, "label": "Miền Bắc (NPC)"},
    {"value": REGION_CPC, "label": "Miền Trung (CPC)"},
    {"value": REGION_SPC, "label": "Miền Nam (SPC)"},
    {"value": REGION_HCMC, "label": "TP. Hồ Chí Minh (HCMC)"},
]
ZALO_TYPE_OPTIONS = [
    {"value": "0", "label": "0 - User"},
    {"value": "1", "label": "1 - Group"},
]
ZALO_ACTION_OPTIONS = [
    {"value": "add", "label": "Thêm tài khoản / nơi nhận"},
    {"value": "edit", "label": "Sửa tài khoản / nơi nhận"},
    {"value": "delete", "label": "Xóa tài khoản / nơi nhận"},
]


def _account_schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(
                CONF_REGION, default=defaults.get(CONF_REGION, REGION_NPC)
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=REGION_OPTIONS,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Required(
                CONF_USERNAME, default=defaults.get(CONF_USERNAME, "")
            ): selector.TextSelector(
                selector.TextSelectorConfig(
                    type=selector.TextSelectorType.TEXT, autocomplete="username"
                )
            ),
            vol.Required(
                CONF_PASSWORD, default=defaults.get(CONF_PASSWORD, "")
            ): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
            ),
            vol.Required(
                CONF_CUSTOMER_ID, default=defaults.get(CONF_CUSTOMER_ID, "")
            ): selector.TextSelector(selector.TextSelectorConfig()),
        }
    )


def _general_options_schema(current: dict[str, Any], data: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(
                CONF_NGAYDAUKY,
                default=int(
                    current.get(
                        CONF_NGAYDAUKY,
                        data.get(CONF_NGAYDAUKY, DEFAULT_NGAYDAUKY),
                    )
                ),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=1,
                    max=31,
                    step=1,
                    mode=selector.NumberSelectorMode.SLIDER,
                )
            ),
        }
    )


def _zalo_recipient_schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(
                CONF_ZALO_RECIPIENT_NAME,
                default=str(defaults.get(CONF_ZALO_RECIPIENT_NAME, "Zalo")),
            ): selector.TextSelector(selector.TextSelectorConfig()),
            vol.Required(
                CONF_ZALO_RECIPIENT_ENABLED,
                default=bool(defaults.get(CONF_ZALO_RECIPIENT_ENABLED, True)),
            ): selector.BooleanSelector(),
            vol.Required(
                CONF_ZALO_TYPE,
                default=str(defaults.get(CONF_ZALO_TYPE, DEFAULT_ZALO_TYPE)),
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=ZALO_TYPE_OPTIONS,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Required(
                CONF_ZALO_ACCOUNT_SELECTION,
                default=str(defaults.get(CONF_ZALO_ACCOUNT_SELECTION, "")),
            ): selector.TextSelector(selector.TextSelectorConfig()),
            vol.Required(
                CONF_ZALO_THREAD_ID,
                default=str(defaults.get(CONF_ZALO_THREAD_ID, "")),
            ): selector.TextSelector(selector.TextSelectorConfig()),
            vol.Required(
                CONF_ZALO_SEND_INVOICE,
                default=bool(
                    defaults.get(CONF_ZALO_SEND_INVOICE, DEFAULT_ZALO_SEND_INVOICE)
                ),
            ): selector.BooleanSelector(),
            vol.Required(
                CONF_ZALO_SEND_DAILY,
                default=bool(
                    defaults.get(CONF_ZALO_SEND_DAILY, DEFAULT_ZALO_SEND_DAILY)
                ),
            ): selector.BooleanSelector(),
            vol.Required(
                CONF_ZALO_SEND_OUTAGE,
                default=bool(
                    defaults.get(CONF_ZALO_SEND_OUTAGE, DEFAULT_ZALO_SEND_OUTAGE)
                ),
            ): selector.BooleanSelector(),
        }
    )


def _validate_zalo_form(user_input: dict[str, Any]) -> dict[str, str]:
    errors: dict[str, str] = {}
    if not str(user_input.get(CONF_ZALO_RECIPIENT_NAME, "")).strip():
        errors[CONF_ZALO_RECIPIENT_NAME] = "required_value"
    if not str(user_input.get(CONF_ZALO_ACCOUNT_SELECTION, "")).strip():
        errors[CONF_ZALO_ACCOUNT_SELECTION] = "required_value"
    if not str(user_input.get(CONF_ZALO_THREAD_ID, "")).strip():
        errors[CONF_ZALO_THREAD_ID] = "required_value"
    return errors


class EVNCSKHConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a completely independent EVN CSKH Monitor config entry."""

    VERSION = 1

    @override
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            normalized = dict(user_input)
            normalized[CONF_CUSTOMER_ID] = str(user_input[CONF_CUSTOMER_ID]).strip().upper()
            error = await self._async_validate(normalized)
            if error is None:
                customer_id = normalized[CONF_CUSTOMER_ID]
                await self.async_set_unique_id(customer_id)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"EVN CSKH Monitor {customer_id}", data=normalized
                )
            errors["base"] = error
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                _account_schema({}), user_input
            ),
            errors=errors,
        )

    @override
    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
        """Start reauthentication after EVN rejects stored credentials."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            updated = {**entry.data, **user_input}
            error = await self._async_validate(updated)
            if error is None:
                return self.async_update_and_abort(
                    entry,
                    data_updates={
                        CONF_USERNAME: str(user_input[CONF_USERNAME]),
                        CONF_PASSWORD: str(user_input[CONF_PASSWORD]),
                    },
                )
            errors["base"] = error
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_USERNAME, default=entry.data.get(CONF_USERNAME, "")
                ): selector.TextSelector(
                    selector.TextSelectorConfig(
                        type=selector.TextSelectorType.TEXT, autocomplete="username"
                    )
                ),
                vol.Required(CONF_PASSWORD): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                ),
            }
        )
        return self.async_show_form(
            step_id="reauth_confirm", data_schema=schema, errors=errors
        )

    @override
    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            normalized = dict(user_input)
            normalized[CONF_CUSTOMER_ID] = str(user_input[CONF_CUSTOMER_ID]).strip().upper()
            error = await self._async_validate(normalized)
            if error is None:
                await self.async_set_unique_id(normalized[CONF_CUSTOMER_ID])
                self._abort_if_unique_id_mismatch()
                # The config-entry update listener performs the one required reload.
                return self.async_update_and_abort(entry, data_updates=normalized)
            errors["base"] = error
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                _account_schema(entry.data), user_input or entry.data
            ),
            errors=errors,
        )

    async def _async_validate(self, data: dict[str, Any]) -> str | None:
        customer_id = str(data[CONF_CUSTOMER_ID]).strip().upper()
        region = str(data[CONF_REGION])
        # Customer IDs are used in database keys and invoice filenames. Accept
        # only the alphanumeric EVN format so malformed input cannot create
        # unintended filesystem paths.
        if not re.fullmatch(r"[PS][A-Z][A-Z0-9]{6,30}", customer_id):
            return "invalid_customer_id"
        expected_region = CUSTOMER_ID_PREFIX_REGION.get(customer_id[:2])
        if expected_region and expected_region != region:
            return "wrong_region"

        api = EVNAPI(
            self.hass,
            region,
            str(data[CONF_USERNAME]),
            str(data[CONF_PASSWORD]),
            customer_id,
        )
        try:
            domain_data = self.hass.data.setdefault(DOMAIN, {})
            network_semaphore = domain_data.get(NETWORK_SEMAPHORE_DATA_KEY)
            if network_semaphore is None:
                network_semaphore = asyncio.Semaphore(MAX_CONCURRENT_EVN_REQUESTS)
                domain_data[NETWORK_SEMAPHORE_DATA_KEY] = network_semaphore
            async with network_semaphore:
                if not await api.login():
                    return (
                        "invalid_auth"
                        if api.last_login_auth_failed
                        else "cannot_connect"
                    )
                now = dt_util.now()
                start = now - timedelta(days=7)
                response = await api.get_chisongay(
                    start.strftime("%d/%m/%Y"), now.strftime("%d/%m/%Y")
                )
            if response is None:
                return "cannot_connect"
            if isinstance(response, dict) and response.get("data") is None:
                return "no_data"
        except Exception:  # noqa: BLE001 - regional EVN clients return varied errors
            _LOGGER.exception("EVN account validation failed")
            return "cannot_connect"
        finally:
            await api.close()
        return None

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        return EVNCSKHOptionsFlow()


class EVNCSKHOptionsFlow(config_entries.OptionsFlow):
    """Options for general settings and multiple Zalo destinations."""

    def __init__(self) -> None:
        self._selected_recipient_id: str | None = None

    @override
    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show a small manager menu instead of one oversized form."""
        return self.async_show_menu(
            step_id="init",
            menu_options=["general", "zalo_accounts"],
        )

    async def async_step_general(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit per-meter billing-period settings."""
        current = dict(self.config_entry.options)
        if user_input is not None:
            merged = dict(current)
            merged.update(user_input)
            # WebUI title/subtitle moved to one domain-wide Store in 2026.8.27.x.
            # Remove stale per-meter values the next time this form is saved.
            merged.pop(CONF_WEBUI_TITLE, None)
            merged.pop(CONF_WEBUI_SUBTITLE, None)
            return self.async_create_entry(title="", data=merged)
        return self.async_show_form(
            step_id="general",
            data_schema=_general_options_schema(current, dict(self.config_entry.data)),
        )

    async def async_step_zalo_accounts(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose add/edit/delete for one Zalo route."""
        recipients = normalize_zalo_recipients(dict(self.config_entry.options))
        errors: dict[str, str] = {}
        if user_input is not None:
            action = str(user_input[CONF_ZALO_ACTION])
            selected = str(user_input.get(CONF_ZALO_RECIPIENT_ID, "")).strip()
            if action == "add":
                return await self.async_step_zalo_add()
            if not selected or not any(item["id"] == selected for item in recipients):
                errors[CONF_ZALO_RECIPIENT_ID] = "select_recipient"
            else:
                self._selected_recipient_id = selected
                if action == "edit":
                    return await self.async_step_zalo_edit()
                if action == "delete":
                    return await self.async_step_zalo_delete()

        schema_fields: dict[Any, Any] = {
            vol.Required(CONF_ZALO_ACTION, default="add"): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=(ZALO_ACTION_OPTIONS if recipients else ZALO_ACTION_OPTIONS[:1]),
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            )
        }
        if recipients:
            options = [
                {
                    "value": item["id"],
                    "label": f"{item['name']} · {'Group' if item['type'] == 1 else 'User'}",
                }
                for item in recipients
            ]
            schema_fields[
                vol.Optional(CONF_ZALO_RECIPIENT_ID, default=recipients[0]["id"])
            ] = selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=options,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            )

        names = ", ".join(item["name"] for item in recipients) or "Chưa có"
        return self.async_show_form(
            step_id="zalo_accounts",
            data_schema=vol.Schema(schema_fields),
            errors=errors,
            description_placeholders={
                "count": str(len(recipients)),
                "accounts": names,
            },
        )

    async def async_step_zalo_add(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Add one Zalo sender/destination pair."""
        recipients = normalize_zalo_recipients(dict(self.config_entry.options))
        defaults = {
            CONF_ZALO_RECIPIENT_NAME: f"Zalo {len(recipients) + 1}",
            CONF_ZALO_RECIPIENT_ENABLED: True,
            CONF_ZALO_TYPE: str(DEFAULT_ZALO_TYPE),
            CONF_ZALO_ACCOUNT_SELECTION: "",
            CONF_ZALO_THREAD_ID: "",
            CONF_ZALO_SEND_INVOICE: DEFAULT_ZALO_SEND_INVOICE,
            CONF_ZALO_SEND_DAILY: DEFAULT_ZALO_SEND_DAILY,
            CONF_ZALO_SEND_OUTAGE: DEFAULT_ZALO_SEND_OUTAGE,
        }
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate_zalo_form(user_input)
            if not errors:
                recipient = recipient_from_form(
                    user_input,
                    uuid4().hex[:16],
                    default_name=f"Zalo {len(recipients) + 1}",
                )
                recipients.append(recipient)
                return self._save_recipients(recipients)
            defaults.update(user_input)
        return self.async_show_form(
            step_id="zalo_add",
            data_schema=_zalo_recipient_schema(defaults),
            errors=errors,
        )

    async def async_step_zalo_edit(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit one existing Zalo route without changing its dedupe identity."""
        recipients = normalize_zalo_recipients(dict(self.config_entry.options))
        selected = next(
            (item for item in recipients if item["id"] == self._selected_recipient_id),
            None,
        )
        if selected is None:
            return await self.async_step_zalo_accounts()
        defaults = form_defaults(selected)
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate_zalo_form(user_input)
            if not errors:
                replacement = recipient_from_form(
                    user_input,
                    selected["id"],
                    default_name=selected["name"],
                )
                updated = [
                    replacement if item["id"] == selected["id"] else item
                    for item in recipients
                ]
                return self._save_recipients(updated)
            defaults.update(user_input)
        return self.async_show_form(
            step_id="zalo_edit",
            data_schema=_zalo_recipient_schema(defaults),
            errors=errors,
            description_placeholders={"name": selected["name"]},
        )

    async def async_step_zalo_delete(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Delete one configured Zalo route with explicit confirmation."""
        recipients = normalize_zalo_recipients(dict(self.config_entry.options))
        selected = next(
            (item for item in recipients if item["id"] == self._selected_recipient_id),
            None,
        )
        if selected is None:
            return await self.async_step_zalo_accounts()
        errors: dict[str, str] = {}
        if user_input is not None:
            if not bool(user_input.get(CONF_CONFIRM_DELETE, False)):
                errors[CONF_CONFIRM_DELETE] = "confirm_delete"
            else:
                updated = [item for item in recipients if item["id"] != selected["id"]]
                return self._save_recipients(updated)
        return self.async_show_form(
            step_id="zalo_delete",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_CONFIRM_DELETE, default=False): selector.BooleanSelector()
                }
            ),
            errors=errors,
            description_placeholders={"name": selected["name"]},
        )

    def _save_recipients(self, recipients: list[dict[str, Any]]) -> ConfigFlowResult:
        options = without_legacy_zalo_options(dict(self.config_entry.options))
        options[CONF_ZALO_RECIPIENTS] = recipients
        return self.async_create_entry(title="", data=options)
