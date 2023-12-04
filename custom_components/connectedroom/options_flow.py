from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.config_entries import OptionsFlow
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.selector import EntitySelector
from homeassistant.helpers.selector import EntitySelectorConfig

DATA_SCHEMA = vol.Schema(
    {
        vol.Optional("primary_lights"): EntitySelector(
            EntitySelectorConfig(domain="light", multiple=True)
        )
    }
)


class OptionsFlowHandler(OptionsFlow):
    def __init__(self, config_entry: ConfigEntry) -> None:
        """Initialize options flow."""
        self.config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        return self.async_show_form(step_id="init", data_schema=DATA_SCHEMA)
