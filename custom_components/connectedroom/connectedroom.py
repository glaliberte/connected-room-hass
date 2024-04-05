import logging
from threading import Timer

import httpx
import socketio
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import event

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
        self.last_goal_horn_unsub = None
        self.tts_after_goal_horn = None
        self.goal_horn_timer = None
        self.stay_on_goal_horn = False

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

            goal_horn = None
            goal_horn_devices = self.coordinator.config_entry.options.get(
                "goal_horn_devices"
            )

            if data["team"] is not None and data["team"]["options"] is not None:
                colors = {}

                if data["team"]["options"]["primary_color_rgb"] is not None:
                    colors["primary"] = data["team"]["options"]["primary_color_rgb"]

                if data["team"]["options"]["secondary_color_rgb"] is not None:
                    colors["secondary"] = data["team"]["options"]["secondary_color_rgb"]

                if data["team"]["options"]["alternate_color_rgb"] is not None:
                    colors["alternate"] = data["team"]["options"]["alternate_color_rgb"]

                await self.sync_lights(colors)

                if (
                    data["team"]["options"]["goal_horn"] is not None
                    or data["team"]["options"]["goal_horn_with_music"] is not None
                    and goal_horn_devices is not None
                ):
                    goal_horn = True

            tts_after_goal_horn = None

            if data["natural_text"] is not None:
                if goal_horn is False:
                    await self.tts(data["natural_text"])
                else:
                    tts_after_goal_horn = data["natural_text"]

            if goal_horn is True:
                if tts_after_goal_horn is not None:
                    self.tts_after_goal_horn = tts_after_goal_horn

        @sio.on("goal_horn", namespace="/" + unique_id)
        async def goal_horn(data):
            goal_horn_devices = self.coordinator.config_entry.options.get(
                "goal_horn_devices"
            )

            if self.last_goal_horn_unsub:
                self.stay_on_goal_horn = True
                self.last_goal_horn_unsub()
                self.last_goal_horn_unsub = None

            if self.goal_horn_timer:
                self.goal_horn_timer.cancel()
                self.goal_horn_timer = None

            goal_horn = data["audioFile"]
            max_duration = data["maxDuration"]

            if goal_horn_devices and goal_horn is not None:
                for goal_horn_device in goal_horn_devices:
                    await self.hass.services.async_call(
                        domain="media_player",
                        service="play_media",
                        service_data={
                            "media_content_type": "music",
                            "media_content_id": goal_horn,
                            "entity_id": goal_horn_device,
                        },
                    )

                self.last_goal_horn_unsub = event.async_track_state_change_event(
                    self.hass, goal_horn_devices, play_tts_when_goal_horn_is_done
                )

                if max_duration > 0:
                    self.goal_horn_timer = Timer(max_duration * 1.0, stop_goal_horn)
                    self.goal_horn_timer.start()

        def stop_goal_horn():
            if self.goal_horn_timer:
                self.goal_horn_timer.cancel()
                self.goal_horn_timer = None

            goal_horn_devices = self.coordinator.config_entry.options.get(
                "goal_horn_devices"
            )

            for goal_horn_device in goal_horn_devices:
                self.hass.services.call(
                    domain="media_player",
                    service="media_stop",
                    service_data={"entity_id": goal_horn_device},
                )

        async def play_tts_when_goal_horn_is_done(event):
            if event.data.get("old_state") is None:
                return

            old_state = event.data.get("old_state")

            if old_state.state != "playing":
                return

            if event.data.get("new_state") is not None:
                new_state = event.data.get("new_state")

                if new_state.state == "idle":
                    if self.stay_on_goal_horn:
                        self.stay_on_goal_horn = False
                        return

                    if self.goal_horn_timer:
                        self.goal_horn_timer.cancel()
                        self.goal_horn_timer = None

                    if self.last_goal_horn_unsub:
                        self.last_goal_horn_unsub()
                        self.last_goal_horn_unsub = None

                    if self.tts_after_goal_horn is not None:
                        await self.tts(self.tts_after_goal_horn)

                    self.tts_after_goal_horn = None

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

            if lights:
                await self.hass.services.async_call(
                    domain="light",
                    service="turn_on",
                    target=lights,
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

        tts_service = self.coordinator.config_entry.options.get("tts_service")

        if self.last_goal_horn_unsub:
            self.last_goal_horn_unsub()
            self.last_goal_horn_unsub = None

        if self.goal_horn_timer:
            self.goal_horn_timer.cancel()
            self.goal_horn_timer = None

        if tts_devices:
            if tts_service:
                for tts_device in tts_devices:
                    await self.hass.services.async_call(
                        domain="tts",
                        service=tts_service,
                        service_data={
                            "cache": True,
                            "entity_id": tts_device,
                            "message": message,
                        },
                    )
            elif tts_provider:
                for tts_device in tts_devices:
                    await self.hass.services.async_call(
                        domain="tts",
                        service="speak",
                        service_data={
                            "cache": True,
                            "media_player_entity_id": tts_device,
                            "entity_id": tts_provider,
                            "message": message,
                        },
                    )


class InvalidAuth(HomeAssistantError):
    """Error to indicate there is invalid auth."""


class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect."""
