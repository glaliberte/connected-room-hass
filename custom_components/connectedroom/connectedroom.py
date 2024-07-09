import asyncio
import logging
import random
from threading import Timer

import httpx
import socketio
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import event
from socketio.exceptions import ConnectionError

from .const import API_URL
from .const import SOCKETIO_PATH
from .const import VERSION
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
        self.sio = None
        self.reconnect_timer = None
        self.namespace_connected = False
        self.do_not_reconnect = False
        self.reconnect_attempts = 0

    def login(hass: HomeAssistant, api_key):
        headers = {"Authorization": "Bearer " + api_key}

        payload = {
            "home_assistant_id": hass.data["core.uuid"],
            "home_assistant_integration_version": VERSION,
        }

        try:
            request = httpx.post(
                API_URL + "/auth/user", data=payload, headers=headers, verify=False
            )
        except Exception:
            return ConnectionError

        try:
            json_data = request.json()
        except Exception:
            raise InvalidAuth

        if not json_data["success"]:
            raise InvalidAuth

        return {"unique_id": json_data["unique_id"], "api_key": api_key}

    async def reconnect(self, api_key, unique_id):
        if self.do_not_reconnect:
            return

        self.reconnect_attempts += 1

        await asyncio.sleep(
            5.0 + random.uniform(0.0, 5.0) + (self.reconnect_attempts * 1.0)
        )
        await self.connectedroom_websocket_connect(api_key, unique_id)

    async def stop(self):
        self.do_not_reconnect = True

        if self.sio:
            await self.sio.disconnect()

        if self.reconnect_timer:
            self.reconnect_timer.cancel()

    # connect to websocket to get updates
    async def connectedroom_websocket_connect(
        self,
        api_key,
        unique_id,
    ):
        if self.do_not_reconnect:
            return

        login = await self.hass.async_add_executor_job(
            ConnectedRoom.login, self.hass, api_key
        )

        api_url_web_websocket = WSS_URL

        if self.sio is not None:
            if self.sio.connected:
                await self.sio.disconnect()

            self.sio = None

        self.sio = socketio.AsyncClient(
            ssl_verify=False, logger=True, engineio_logger=True, reconnection=True
        )

        sio = self.sio

        sio.register_namespace(
            ConnectedRoomUserNamespace(
                connected_room=self, api_key=api_key, unique_id=login["unique_id"]
            )
        )

        try:
            await sio.connect(
                api_url_web_websocket,
                transports=["websocket"],
                socketio_path=SOCKETIO_PATH,
            )

            if not self.namespace_connected:
                raise ConnectionError

            self.reconnect_attempts = 0

        except ConnectionError:
            self.reconnect_timer = self.hass.async_create_task(
                self.reconnect(api_key, login["unique_id"])
            )

            return

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


class ConnectedRoomUserNamespace(socketio.AsyncClientNamespace):
    def __init__(self, connected_room: ConnectedRoom, api_key: str, unique_id: str):
        self.connected_room = connected_room
        self.api_key = api_key
        self.unique_id = unique_id
        self.namespace = "/" + unique_id

        super(socketio.AsyncClientNamespace, self).__init__(namespace="/" + unique_id)

    def on_connect(self):
        _LOGGER.info("Connected")

        self.connected_room.namespace_connected = True

        pass

    def on_disconnect(self):
        self.connected_room.namespace_connected = False

        self.connected_room.reconnect_timer = self.connected_room.hass.create_task(
            self.connected_room.reconnect(self.api_key, self.unique_id)
        )

        pass

    async def on_goal(self, data):
        registry = dr.async_get(self.connected_room.hass)

        devices = dr.async_entries_for_config_entry(
            registry, self.connected_room.coordinator.config_entry.entry_id
        )

        for device in devices:
            event_data = {
                "type": "goal",
                "device_id": device.id,
                "entity_id": self.connected_room.coordinator.config_entry.entry_id,
                "payload": data,
            }

            self.connected_room.hass.bus.async_fire("connectedroom_event", event_data)

        goal_horn = None
        goal_horn_devices = self.connected_room.coordinator.config_entry.options.get(
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

            await self.connected_room.sync_lights(colors)

            if (
                data["team"]["options"]["goal_horn"] is not None
                or data["team"]["options"]["goal_horn_with_music"] is not None
                and goal_horn_devices is not None
            ):
                goal_horn = True

        tts_after_goal_horn = None

        if data["natural_text"] is not None:
            if goal_horn is False:
                await self.connected_room.tts(data["natural_text"])
            else:
                tts_after_goal_horn = data["natural_text"]

        if goal_horn is True:
            if tts_after_goal_horn is not None:
                self.connected_room.tts_after_goal_horn = tts_after_goal_horn

    async def on_goal_horn(self, data):
        goal_horn_devices = self.connected_room.coordinator.config_entry.options.get(
            "goal_horn_devices"
        )

        if self.connected_room.last_goal_horn_unsub:
            self.connected_room.stay_on_goal_horn = True
            self.connected_room.last_goal_horn_unsub()
            self.connected_room.last_goal_horn_unsub = None

        if self.connected_room.goal_horn_timer:
            self.connected_room.goal_horn_timer.cancel()
            self.connected_room.goal_horn_timer = None

        goal_horn = data["audioFile"]
        max_duration = data["maxDuration"]

        if goal_horn_devices and goal_horn is not None:
            for goal_horn_device in goal_horn_devices:
                await self.connected_room.hass.services.async_call(
                    domain="media_player",
                    service="play_media",
                    service_data={
                        "media_content_type": "music",
                        "media_content_id": goal_horn,
                        "entity_id": goal_horn_device,
                    },
                )

            self.connected_room.last_goal_horn_unsub = (
                event.async_track_state_change_event(
                    self.connected_room.hass,
                    goal_horn_devices,
                    self.play_tts_when_goal_horn_is_done,
                )
            )

            if max_duration > 0:
                self.connected_room.goal_horn_timer = Timer(
                    max_duration * 1.0, self.stop_goal_horn
                )
                self.connected_room.goal_horn_timer.start()

    def stop_goal_horn(self):
        if self.connected_room.goal_horn_timer:
            self.connected_room.goal_horn_timer.cancel()
            self.connected_room.goal_horn_timer = None

        goal_horn_devices = self.connected_room.coordinator.config_entry.options.get(
            "goal_horn_devices"
        )

        for goal_horn_device in goal_horn_devices:
            self.connected_room.hass.services.call(
                domain="media_player",
                service="media_stop",
                service_data={"entity_id": goal_horn_device},
            )

    async def play_tts_when_goal_horn_is_done(self, event):
        if event.data.get("old_state") is None:
            return

        old_state = event.data.get("old_state")

        if old_state.state != "playing":
            return

        if event.data.get("new_state") is not None:
            new_state = event.data.get("new_state")

            if new_state.state == "idle":
                if self.connected_room.stay_on_goal_horn:
                    self.connected_room.stay_on_goal_horn = False
                    return

                if self.connected_room.goal_horn_timer:
                    self.connected_room.goal_horn_timer.cancel()
                    self.connected_room.goal_horn_timer = None

                if self.connected_room.last_goal_horn_unsub:
                    self.connected_room.last_goal_horn_unsub()
                    self.connected_room.last_goal_horn_unsub = None

                if self.connected_room.tts_after_goal_horn is not None:
                    await self.connected_room.tts(
                        self.connected_room.tts_after_goal_horn
                    )

                self.connected_room.tts_after_goal_horn = None

    async def on_period_start(self, data):
        registry = dr.async_get(self.connected_room.hass)

        devices = dr.async_entries_for_config_entry(
            registry, self.connected_room.coordinator.config_entry.entry_id
        )

        for device in devices:
            event_data = {
                "type": "period_start",
                "device_id": device.id,
                "entity_id": self.connected_room.coordinator.config_entry.entry_id,
                "payload": data,
            }

            self.connected_room.hass.bus.async_fire("connectedroom_event", event_data)

        if data["natural_text"] is not None:
            await self.connected_room.tts(data["natural_text"])

    async def on_period_end(self, data):
        registry = dr.async_get(self.connected_room.hass)

        devices = dr.async_entries_for_config_entry(
            registry, self.connected_room.coordinator.config_entry.entry_id
        )

        for device in devices:
            event_data = {
                "type": "period_end",
                "device_id": device.id,
                "entity_id": self.connected_room.coordinator.config_entry.entry_id,
                "payload": data,
            }

            self.connected_room.hass.bus.async_fire("connectedroom_event", event_data)

        if data["natural_text"] is not None:
            await self.connected_room.tts(data["natural_text"])

    async def on_game_start(self, data):
        registry = dr.async_get(self.connected_room.hass)

        devices = dr.async_entries_for_config_entry(
            registry, self.connected_room.coordinator.config_entry.entry_id
        )

        for device in devices:
            event_data = {
                "type": "game_start",
                "device_id": device.id,
                "entity_id": self.connected_room.coordinator.config_entry.entry_id,
                "payload": data,
            }

            self.connected_room.hass.bus.async_fire("connectedroom_event", event_data)

        if data["natural_text"] is not None:
            await self.connected_room.tts(data["natural_text"])

    async def on_game_end(self, data):
        registry = dr.async_get(self.connected_room.hass)

        devices = dr.async_entries_for_config_entry(
            registry, self.connected_room.coordinator.config_entry.entry_id
        )

        for device in devices:
            event_data = {
                "type": "game_end",
                "device_id": device.id,
                "entity_id": self.connected_room.coordinator.config_entry.entry_id,
                "payload": data,
            }

            self.connected_room.hass.bus.async_fire("connectedroom_event", event_data)

        if data["natural_text"] is not None:
            await self.connected_room.tts(data["natural_text"])


class InvalidAuth(HomeAssistantError):
    """Error to indicate there is invalid auth."""


class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect."""
