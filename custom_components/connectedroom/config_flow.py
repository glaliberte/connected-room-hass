"""Config flow for Hello World integration."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.config_entries import ConfigFlow
from homeassistant.config_entries import OptionsFlow
from homeassistant.core import callback
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.selector import TargetSelector
from homeassistant.helpers.selector import TargetSelectorConfig
from homeassistant.helpers.selector import EntitySelector
from homeassistant.helpers.selector import EntitySelectorConfig
from homeassistant.helpers.selector import TextSelector

from .connectedroom import CannotConnect
from .connectedroom import ConnectedRoom
from .connectedroom import InvalidAuth
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# This is the schema that used to display the UI to the user. This simple
# schema has a single required host field, but it could include a number of fields
# such as username, password etc. See other components in the HA core code for
# further examples.
# Note the input displayed to the user will be translated. See the
# translations/<lang>.json file and strings.json. See here for further information:
# https://developers.home-assistant.io/docs/config_entries_config_flow_handler/#translations
# At the time of writing I found the translations created by the scaffold didn't
# quite work as documented and always gave me the "Lokalise key references" string
# (in square brackets), rather than the actual translated value. I did not attempt to
# figure this out or look further into it.


async def validate_api_key(hass: HomeAssistant, data: dict) -> dict[str, Any]:
    """Validate the user input allows us to connect.

    Data has the keys from DATA_SCHEMA with values provided by the user.
    """

    login = await hass.async_add_executor_job(ConnectedRoom.login, data["api_key"])

    # Return info that you want to store in the config entry.
    return {"api_key": login["api_key"], "unique_id": login["unique_id"]}


class ConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for pixie_plus."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step."""

        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")

        schema = vol.Schema({vol.Required("api_key"): str})

        if user_input is None:
            return self.async_show_form(step_id="user", data_schema=schema)

        errors = {}

        try:
            info = await validate_api_key(self.hass, user_input)
        except CannotConnect:
            errors["base"] = "cannot_connect"
        except InvalidAuth:
            errors["base"] = "invalid_auth"
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("Unexpected exception")
            errors["base"] = "unknown"
        else:
            return self.async_create_entry(title="ConnectedRoom", data=info)

        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> OptionsFlow:
        """Create the options flow."""
        return OptionsFlowHandler(config_entry)


class OptionsFlowHandler(OptionsFlow):

    VERSION=2

    def __init__(self, config_entry: ConfigEntry) -> None:
        """Initialize options flow."""
        self.config_entry = config_entry
        self.options = dict(config_entry.options)

    async def async_step_init(self, user_input=None):
        """Manage the options."""
        return self.async_show_menu(
            step_id="init",
            menu_options=["user", "lighting", "tts", "goal_horn"],
            description_placeholders={
                "model": "Example model",
            },
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage the options."""

        old_api_key = self.config_entry.options.get(
            "api_key", self.config_entry.data.get("api_key", "")
        )

        errors = {}

        if user_input is not None:
            try:
                api_key = user_input["api_key"]

                if old_api_key != api_key:
                    user_input = await validate_api_key(self.hass, user_input)

            except CannotConnect:
                errors["base"] = "cannot_connect"
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"

            if not errors:
                # update options flow values
                self.options.update(user_input)
                return await self._update_options()
                # for later - extend with options you don't want in config but option flow
                # return await self.async_step_options_2()

        schema = vol.Schema(
            {
                vol.Required("api_key", default=old_api_key): str,
            }
        )

        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_lighting(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage the options."""

        errors = {}

        if user_input is not None:
            # update options flow values

            if "primary_lights" not in user_input:
                user_input["primary_lights"] = dict()
            if "secondary_lights" not in user_input:
                user_input["secondary_lights"] = dict()

            self.options.update(user_input)
            return await self._update_options()
        
            # for later - extend with options you don't want in config but option flow
            # return await self.async_step_options_2()

        updated_to_target_selector = self.config_entry.options.get("update_to_target_selector", False)

        primary_lights = self.config_entry.options.get("primary_lights", dict())
        secondary_lights = self.config_entry.options.get("secondary_lights", dict())

        if updated_to_target_selector is False:
            primary_lights = dict()
            secondary_lights = dict()
            self.options.update( {
                "update_to_target_selector": True
            } )

        schema = vol.Schema(
            {
                vol.Optional(
                    "primary_lights",
                    description={"suggested_value": primary_lights},
                ): TargetSelector(
                    TargetSelectorConfig(
                        entity=EntitySelectorConfig(domain="light")
                    )
                ),
                vol.Optional(
                    "secondary_lights",
                    description={"suggested_value": secondary_lights},
                ): TargetSelector(
                    TargetSelectorConfig(
                        entity=EntitySelectorConfig(domain="light")
                    )
                ),
            }
        )

        return self.async_show_form(
            step_id="lighting", data_schema=schema, errors=errors
        )

    async def async_step_tts(
        self, user_input: dict[str, Any|None] | None = None
    ) -> FlowResult:
        """Manage the options."""

        errors = {}

        if user_input is not None:

            if "tts_provider" not in user_input:
                user_input["tts_provider"] = None
            if "tts_service" not in user_input:
                user_input["tts_service"] = None
            if "tts_devices" not in user_input:
                user_input["tts_devices"] = None

            # update options flow values
            self.options.update(user_input)
            return await self._update_options()
            # for later - extend with options you don't want in config but option flow
            # return await self.async_step_options_2()

        schema = vol.Schema(
            {
                vol.Optional(
                    "tts_provider",
                    description={"suggested_value": self.config_entry.options.get( "tts_provider", None )}
                ): EntitySelector(EntitySelectorConfig(domain="tts")),
                vol.Optional(
                    "tts_service",
                    description={"suggested_value": self.config_entry.options.get( "tts_service", "" )}
                ): TextSelector(),
                vol.Optional(
                    "tts_devices",
                    description={"suggested_value": self.config_entry.options.get( "tts_devices", [] )},
                ): EntitySelector(EntitySelectorConfig(domain="media_player", multiple=True))
            }
        )

        return self.async_show_form(step_id="tts", data_schema=schema, errors=errors)
    

    async def async_step_goal_horn(
        self, user_input: dict[str, Any|None] | None = None
    ) -> FlowResult:
        """Manage the options."""

        errors = {}

        if user_input is not None:

            if "goal_horn_devices" not in user_input:
                user_input["goal_horn_devices"] = None

            # update options flow values
            self.options.update(user_input)
            return await self._update_options()
            # for later - extend with options you don't want in config but option flow
            # return await self.async_step_options_2()

        schema = vol.Schema(
            {
                vol.Optional(
                    "goal_horn_devices",
                    description={"suggested_value": self.config_entry.options.get( "goal_horn_devices", [] )},
                ): EntitySelector(EntitySelectorConfig(domain="media_player", multiple=True))
            }
        )

        return self.async_show_form(step_id="goal_horn", data_schema=schema, errors=errors)
    
    async def _update_options(self):
        return self.async_create_entry( title="ConnectedRoom", data=self.options ) 