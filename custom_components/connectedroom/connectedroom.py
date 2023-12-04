import logging

import httpx
import socketio
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr

from .const import API_URL
from .const import SOCKETIO_PATH
from .const import WSS_URL

_LOGGER = logging.getLogger(__name__)


class ConnectedRoom:
    def __init__(
        self,
        hass: HomeAssistant,
        coordinator,
    ):
        self.hass = hass
        self.coordinator = coordinator

    def login(api_key):
        headers = {"Authorization": "Bearer " + api_key}

        try:
            request = httpx.post(API_URL + "/auth/user", headers=headers, verify=False)
        except Exception:
            return ConnectionError

        try:
            json_data = request.json()
        except Exception:
            raise InvalidAuth

        if not json_data["success"]:
            raise InvalidAuth

        return {"unique_id": json_data["unique_id"], "api_key": api_key}

    # connect to websocket to get updates
    async def connectedroom_websocket_connect(
        self,
        api_key,
        unique_id,
    ):
        api_url_web_websocket = WSS_URL

        sio = socketio.AsyncClient(ssl_verify=False, logger=True, engineio_logger=True)

        sio = socketio.AsyncClient()

        await sio.connect(
            api_url_web_websocket,
            namespaces=["/" + unique_id],
            transports=["websocket"],
            socketio_path=SOCKETIO_PATH,
        )

        @sio.on("goal", namespace="/" + unique_id)
        async def goal(data):
            registry = dr.async_get(self.hass)

            devices = dr.async_entries_for_config_entry(
                registry, self.coordinator.config_entry.entry_id
            )

            for device in devices:
                event_data = {
                    "type": "goal",
                    "device_id": device.id,
                    "entity_id": self.coordinator.config_entry.entry_id,
                    "payload": data,
                }

                self.hass.bus.async_fire("connectedroom_event", event_data)

            if data["team"] is not None and data["team"]["options"] is not None:
                colors = {}

                if data["team"]["options"]["primary_color_rgb"] is not None:
                    colors["primary"] = data["team"]["options"]["primary_color_rgb"]

                if data["team"]["options"]["secondary_color_rgb"] is not None:
                    colors["secondary"] = data["team"]["options"]["secondary_color_rgb"]

                if data["team"]["options"]["alternate_color_rgb"] is not None:
                    colors["alternate"] = data["team"]["options"]["alternate_color_rgb"]

                await self.sync_lights(colors)

            if data["natural_text"] is not None:
                await self.tts(data["natural_text"])

        @sio.on("period_start", namespace="/" + unique_id)
        async def period_start(data):
            registry = dr.async_get(self.hass)

            devices = dr.async_entries_for_config_entry(
                registry, self.coordinator.config_entry.entry_id
            )

            for device in devices:
                event_data = {
                    "type": "period_start",
                    "device_id": device.id,
                    "entity_id": self.coordinator.config_entry.entry_id,
                    "payload": data,
                }

                self.hass.bus.async_fire("connectedroom_event", event_data)

            if data["natural_text"] is not None:
                await self.tts(data["natural_text"])

        @sio.on("period_end", namespace="/" + unique_id)
        async def period_end(data):
            registry = dr.async_get(self.hass)

            devices = dr.async_entries_for_config_entry(
                registry, self.coordinator.config_entry.entry_id
            )

            for device in devices:
                event_data = {
                    "type": "period_end",
                    "device_id": device.id,
                    "entity_id": self.coordinator.config_entry.entry_id,
                    "payload": data,
                }

                self.hass.bus.async_fire("connectedroom_event", event_data)

            if data["natural_text"] is not None:
                await self.tts(data["natural_text"])

        @sio.on("game_start", namespace="/" + unique_id)
        async def game_start(data):
            registry = dr.async_get(self.hass)

            devices = dr.async_entries_for_config_entry(
                registry, self.coordinator.config_entry.entry_id
            )

            for device in devices:
                event_data = {
                    "type": "game_start",
                    "device_id": device.id,
                    "entity_id": self.coordinator.config_entry.entry_id,
                    "payload": data,
                }

                self.hass.bus.async_fire("connectedroom_event", event_data)

            if data["natural_text"] is not None:
                await self.tts(data["natural_text"])

        @sio.on("game_end", namespace="/" + unique_id)
        async def game_end(data):
            registry = dr.async_get(self.hass)

            devices = dr.async_entries_for_config_entry(
                registry, self.coordinator.config_entry.entry_id
            )

            for device in devices:
                event_data = {
                    "type": "game_end",
                    "device_id": device.id,
                    "entity_id": self.coordinator.config_entry.entry_id,
                    "payload": data,
                }

                self.hass.bus.async_fire("connectedroom_event", event_data)

            if data["natural_text"] is not None:
                await self.tts(data["natural_text"])

        self.hass.async_create_task(sio.wait())

        return sio

    async def sync_lights(self, colors: dict):
        for color in colors:
            lights = self.coordinator.config_entry.options.get(color + "_lights")

            if lights is not None:
                await self.hass.services.async_call(
                    domain="light",
                    service="turn_on",
                    target={"device_id": lights},
                    service_data={
                        "rgb_color": [
                            colors[color]["r"],
                            colors[color]["g"],
                            colors[color]["b"],
                        ]
                    },
                )

    async def tts(self, message):
        tts_devices = self.coordinator.config_entry.options.get("tts_devices")

        tts_provider = self.coordinator.config_entry.options.get("tts_provider")

        if tts_devices is not None and tts_provider is not None:
            for tts_device in tts_devices:
                await self.hass.services.async_call(
                    domain="tts",
                    service="speak",
                    service_data={
                        "cache": True,
                        "media_player_entity_id": tts_device,
                        "entity_id": tts_provider,
                        "message": message,
                        "language": "en-us",
                    },
                )


class InvalidAuth(HomeAssistantError):
    """Error to indicate there is invalid auth."""


class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect."""
