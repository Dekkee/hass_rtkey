import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback

from . import CONF_TOKEN, DATA_SCHEMA, DOMAIN, OPTIONS_SCHEMA


class RTKeyOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input):
        if user_input is not None:
            return self.async_create_entry(
                title=self.config_entry.data["name"],
                data=user_input,
            )

        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(OPTIONS_SCHEMA), self.config_entry.options
            ),
        )


class RTKeyConfigFlow(ConfigFlow, domain=DOMAIN):
    # The schema version of the entries that it creates
    # Home Assistant will call your migrate method if the version changes
    VERSION = 1
    MINOR_VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> RTKeyOptionsFlow:
        return RTKeyOptionsFlow()

    async def async_step_user(self, user_input):
        if user_input is not None:
            return self.async_create_entry(
                title=user_input["name"], data=user_input, options=user_input
            )

        return self.async_show_form(
            step_id="user", data_schema=vol.Schema(DATA_SCHEMA).extend(OPTIONS_SCHEMA)
        )

    async def async_step_reauth(self, entry_data) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input=None) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        if user_input is not None:
            new_options = {**entry.options, CONF_TOKEN: user_input[CONF_TOKEN]}
            self.hass.config_entries.async_update_entry(entry, options=new_options)
            await self.hass.config_entries.async_reload(entry.entry_id)
            return self.async_abort(reason="reauth_successful")

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_TOKEN): str}),
        )
