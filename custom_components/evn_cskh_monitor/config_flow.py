"""Config and options flows for EVN CSKH Monitor."""

from __future__ import annotations

from datetime import timedelta
import logging
from typing import Any, override

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.util import dt as dt_util

from .const import (
    CONF_CUSTOMER_ID,
    CONF_NGAYDAUKY,
    CONF_PASSWORD,
    CONF_REGION,
    CONF_USERNAME,
    CONF_ZALO_ACCOUNT_SELECTION,
    CONF_ZALO_SEND_DAILY,
    CONF_ZALO_SEND_INVOICE,
    CONF_ZALO_SEND_OUTAGE,
    CONF_ZALO_THREAD_ID,
    CONF_ZALO_TYPE,
    CUSTOMER_ID_PREFIX_REGION,
    DEFAULT_NGAYDAUKY,
    DEFAULT_ZALO_ACCOUNT_SELECTION,
    DEFAULT_ZALO_SEND_DAILY,
    DEFAULT_ZALO_SEND_INVOICE,
    DEFAULT_ZALO_SEND_OUTAGE,
    DEFAULT_ZALO_THREAD_ID,
    DEFAULT_ZALO_TYPE,
    DOMAIN,
    REGION_CPC,
    REGION_HCMC,
    REGION_HN,
    REGION_NPC,
    REGION_SPC,
)
from .evn_api import EVNAPI

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
        if len(customer_id) < 8 or not customer_id.startswith(("P", "S")):
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
            if not await api.login():
                return "invalid_auth" if api.last_login_auth_failed else "cannot_connect"
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
    """Options for billing-cycle calculations and Zalo Bot delivery."""

    @override
    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current = dict(self.config_entry.options)
        data = self.config_entry.data
        schema = vol.Schema(
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
                vol.Required(
                    CONF_ZALO_TYPE,
                    default=str(current.get(CONF_ZALO_TYPE, DEFAULT_ZALO_TYPE)),
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=ZALO_TYPE_OPTIONS,
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Optional(
                    CONF_ZALO_ACCOUNT_SELECTION,
                    default=str(
                        current.get(
                            CONF_ZALO_ACCOUNT_SELECTION,
                            DEFAULT_ZALO_ACCOUNT_SELECTION,
                        )
                    ),
                ): selector.TextSelector(selector.TextSelectorConfig()),
                vol.Optional(
                    CONF_ZALO_THREAD_ID,
                    default=str(
                        current.get(CONF_ZALO_THREAD_ID, DEFAULT_ZALO_THREAD_ID)
                    ),
                ): selector.TextSelector(selector.TextSelectorConfig()),
                vol.Required(
                    CONF_ZALO_SEND_INVOICE,
                    default=bool(
                        current.get(CONF_ZALO_SEND_INVOICE, DEFAULT_ZALO_SEND_INVOICE)
                    ),
                ): selector.BooleanSelector(),
                vol.Required(
                    CONF_ZALO_SEND_DAILY,
                    default=bool(
                        current.get(CONF_ZALO_SEND_DAILY, DEFAULT_ZALO_SEND_DAILY)
                    ),
                ): selector.BooleanSelector(),
                vol.Required(
                    CONF_ZALO_SEND_OUTAGE,
                    default=bool(
                        current.get(CONF_ZALO_SEND_OUTAGE, DEFAULT_ZALO_SEND_OUTAGE)
                    ),
                ): selector.BooleanSelector(),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
