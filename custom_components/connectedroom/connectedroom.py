import asyncio
import json
import logging
from threading import Timer

import httpx
import pysher
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import event

from .const import API_URL
from .const import VERSION
from .const import WSS_HOST
from .const import WSS_KEY


_LOGGER = logging.getLogger(__name__)


class ConnectedRoom:
    def __init__(
        self,
        hass: HomeAssistant,
        coordinator,
    ):
        self.hass = hass
        self.coordinator = coordinator
        self.auth = None
        self.last_goal_horn_unsub = None
        self.tts_after_goal_horn = None
        self.goal_horn_timer = None
        self.stay_on_goal_horn = False
        self.pusher = None
        self.reconnect_timer = None
        self.namespace_connected = False
        self.do_not_reconnect = False
        self.reconnect_attempts = 0
        self.is_playing_horn = False

    def login_request(hass, api_key):
        headers = {"Authorization": "Bearer " + api_key, "Accept": "application/json"}

        payload = {
            "home_assistant_id": hass.data["core.uuid"],
            "home_assistant_integration_version": VERSION,
        }

        try:
            request = httpx.post(
                API_URL + "/integrations/home-assistant/link",
                data=payload,
                headers=headers,
                verify=False,
            )
        except Exception:
            raise ConnectionError

        try:
            json_data = request.json()
        except Exception:
            raise InvalidAuth

        if not json_data["success"]:
            raise InvalidAuth

        return {
            "unique_id": json_data["unique_id"],
            "api_key": api_key,
            "websocket_key": json_data["websocket_key"],
            "integration_key": json_data["integration_key"],
        }

    async def login(self, api_key):
        self.auth = None

        self.auth = await self.hass.async_add_executor_job(
            ConnectedRoom.login_request, self.hass, api_key
        )

    def stop(self):
        self.do_not_reconnect = True

        if (
            self.pusher
            and self.pusher.connection
            and self.pusher.connection.state != "disconnected"
        ):
            self.pusher.disconnect()

    # init connectedroom
    async def setup(
        self,
        api_key,
        unique_id,
    ):
        await self.login(api_key)

        await self.setup_websockets()

        await self.setup_devices()

    async def setup_websockets(self):
        if self.do_not_reconnect:
            return

        if self.pusher is not None:
            return

        if self.auth is None:
            return

        self.do_not_reconnect = False

        self.pusher = pysher.Pusher(
            key=WSS_KEY,
            custom_host=WSS_HOST,
            auth_endpoint=API_URL + "/auth/websockets",
            auth_endpoint_headers={"x-websocket-key": self.auth["websocket_key"]},
            reconnect_interval=15,
            log_level=logging.CRITICAL,
        )

        def connect_handler(data):
            ConnectedRoomEvents(self, self.pusher, self.auth["unique_id"])
            ConnectedRoomDeviceEvents(self, self.pusher, self.auth["integration_key"])

        def error_handler(data):
            if "code" in data:
                try:
                    error_code = int(data["code"])
                except ValueError:
                    error_code = None

                if error_code is not None:
                    self.pusher.connection.logger.error(
                        "Connection: Received error %s" % error_code
                    )

                    if (error_code >= 4200) and (error_code <= 4299):
                        # The connection SHOULD be re-established immediately
                        self.pusher.connection.reconnect(5)
                    else:
                        self.pusher.connection.reconnect()
                else:
                    self.pusher.connection.logger.error(
                        "Connection: Unknown error code"
                    )
            else:
                self.pusher.connection.logger.error(
                    "Connection: No error code supplied"
                )

        self.pusher.connection.ping_interval = 15

        self.pusher.connection.bind("pusher:connection_established", connect_handler)

        self.pusher.connection.event_callbacks.pop("pusher:error")

        self.pusher.connection.bind("pusher:error", error_handler)

        self.pusher.connect()

        return self.pusher

    async def setup_devices(self):
        devices = self.coordinator.config_entry.options.get("devices")

        if devices is None:
            return

        entity_registry = er.async_get(self.hass)
        device_registry = dr.async_get(self.hass)

        to_sync = []

        for entity_id in devices["entity_id"]:
            entity = entity_registry.async_get(entity_id)

            device = device_registry.async_get(entity.device_id)

            name = entity.original_name

            if name is None:
                name = device.name

            to_sync.append(
                {
                    "entity_id": entity.entity_id,
                    "capabilities": entity.capabilities,
                    "device_class": entity.domain,
                    "name": name,
                }
            )

        headers = {
            "Authorization": "Bearer " + self.auth["api_key"],
            "Accept": "application/json",
        }

        payload = {
            "devices": to_sync,
        }

        try:
            request = httpx.post(
                API_URL + "/integrations/home-assistant/devices/sync",
                json=payload,
                headers=headers,
                verify=False,
            )
        except Exception:
            raise ConnectionError

        try:
            json_data = request.json()
        except Exception:
            raise InvalidAuth

        if not json_data["success"]:
            raise InvalidAuth

        return True

    async def sync_lights(self, colors: dict):
        for color in colors:
            lights = self.coordinator.config_entry.options.get(color + "_lights")

            if lights:
                self.hass.services.call(
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
        if self.is_playing_horn:
            return

        tts_devices = self.coordinator.config_entry.options.get("tts_devices")

        tts_provider = self.coordinator.config_entry.options.get("tts_provider")

        tts_service = self.coordinator.config_entry.options.get("tts_service")

        if self.last_goal_horn_unsub:
            self.last_goal_horn_unsub()
            self.last_goal_horn_unsub = None

        if self.goal_horn_timer:
            self.goal_horn_timer.cancel()
            self.goal_horn_timer = None

        self.tts_after_goal_horn = None

        if tts_devices:
            if tts_service:
                for tts_device in tts_devices:
                    self.hass.services.call(
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
                    self.hass.services.call(
                        domain="tts",
                        service="speak",
                        service_data={
                            "cache": True,
                            "media_player_entity_id": tts_device,
                            "entity_id": tts_provider,
                            "message": message,
                        },
                    )


class ConnectedRoomEvents:
    def __init__(
        self, connected_room: ConnectedRoom, pusher: pysher.Pusher, unique_id: str
    ):
        self.connected_room = connected_room
        self.pusher = pusher
        self.unique_id = unique_id

        self.channel = pusher.subscribe("private-" + unique_id)

        self.channel.bind("goal", lambda data, **kargs: asyncio.run(self.on_goal(data)))
        self.channel.bind(
            "goal_horn", lambda data, **kargs: asyncio.run(self.on_goal_horn(data))
        )
        self.channel.bind(
            "period_start",
            lambda data, **kargs: asyncio.run(self.on_period_start(data)),
        )
        self.channel.bind(
            "period_end", lambda data, **kargs: asyncio.run(self.on_period_end(data))
        )
        self.channel.bind(
            "game_start", lambda data, **kargs: asyncio.run(self.on_game_start(data))
        )
        self.channel.bind(
            "game_end", lambda data, **kargs: asyncio.run(self.on_game_end(data))
        )

    async def on_goal(self, data):
        data = json.loads(data)

        registry = dr.async_get(self.connected_room.hass)

        goal_horn_devices = self.connected_room.coordinator.config_entry.options.get(
            "goal_horn_devices"
        )

        if (
            "already_triggered_from_score_change" not in data
            or data.already_triggered_from_score_change is not True
        ):
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

                try:
                    self.connected_room.hass.bus.fire("connectedroom_event", event_data)
                except Exception:
                    _LOGGER.error("Error while running automation")

            if data["team"] is not None and data["team"]["options"] is not None:
                colors = {}

                if data["team"]["options"]["primary_color_rgb"] is not None:
                    colors["primary"] = data["team"]["options"]["primary_color_rgb"]

                if data["team"]["options"]["secondary_color_rgb"] is not None:
                    colors["secondary"] = data["team"]["options"]["secondary_color_rgb"]

                if data["team"]["options"]["alternate_color_rgb"] is not None:
                    colors["alternate"] = data["team"]["options"]["alternate_color_rgb"]

                await self.connected_room.sync_lights(colors)

            self.connected_room.tts_after_goal_horn = None

            if "natural_text" in data and data["natural_text"] is not None:
                if not goal_horn_devices:
                    await self.connected_room.tts(data["natural_text"])
                else:
                    self.connected_room.tts_after_goal_horn = data["natural_text"]

                    if self.connected_room.goal_horn_timer:
                        self.connected_room.goal_horn_timer.cancel()
                        self.connected_room.goal_horn_timer = None

                    if self.connected_room.last_goal_horn_unsub:
                        self.connected_room.last_goal_horn_unsub()
                        self.connected_room.last_goal_horn_unsub = None

                    self.connected_room.goal_horn_timer = Timer(
                        3.0,
                        lambda: asyncio.run(
                            self.connected_room.tts(
                                message=self.connected_room.tts_after_goal_horn
                            )
                        ),
                    )
                    self.connected_room.goal_horn_timer.start()

    async def on_goal_horn(self, data):
        data = json.loads(data)

        goal_horn_devices = self.connected_room.coordinator.config_entry.options.get(
            "goal_horn_devices"
        )

        if self.connected_room.last_goal_horn_unsub:
            self.connected_room.last_goal_horn_unsub()
            self.connected_room.last_goal_horn_unsub = None

        if self.connected_room.goal_horn_timer:
            self.connected_room.goal_horn_timer.cancel()
            self.connected_room.goal_horn_timer = None

        goal_horn = data["audioFile"]

        if goal_horn_devices and goal_horn is not None:
            if self.connected_room.is_playing_horn:
                self.connected_room.tts_after_goal_horn = None

            self.connected_room.is_playing_horn = True

            for goal_horn_device in goal_horn_devices:
                self.connected_room.hass.services.call(
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

        self.connected_room.is_playing_horn = False

    async def play_tts_when_goal_horn_is_done(self, event):
        if not self.connected_room.is_playing_horn:
            return

        if event.data.get("old_state") is None:
            return

        old_state = event.data.get("old_state")

        if old_state.state != "playing":
            return

        if event.data.get("new_state") is not None:
            new_state = event.data.get("new_state")

            if new_state.state == "idle" and (
                not old_state.attributes["media_content_id"]
                or not new_state.attributes["media_content_id"]
                or new_state.attributes["media_content_id"]
                == old_state.attributes["media_content_id"]
            ):
                self.connected_room.is_playing_horn = False

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
                    message = self.connected_room.tts_after_goal_horn

                    self.connected_room.goal_horn_timer = Timer(
                        1.0,
                        lambda: asyncio.run(self.connected_room.tts(message=message)),
                    )
                    self.connected_room.goal_horn_timer.start()
                    self.connected_room.tts_after_goal_horn = None

    async def on_period_start(self, data):
        data = json.loads(data)

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

            self.connected_room.hass.bus.fire("connectedroom_event", event_data)

        if "natural_text" in data and data["natural_text"] is not None:
            await self.connected_room.tts(data["natural_text"])

    async def on_period_end(self, data):
        data = json.loads(data)

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

            self.connected_room.hass.bus.fire("connectedroom_event", event_data)

        if "natural_text" in data and data["natural_text"] is not None:
            await self.connected_room.tts(data["natural_text"])

    async def on_game_start(self, data):
        data = json.loads(data)

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

            self.connected_room.hass.bus.fire("connectedroom_event", event_data)

        if "natural_text" in data and data["natural_text"] is not None:
            await self.connected_room.tts(data["natural_text"])

    async def on_game_end(self, data):
        data = json.loads(data)

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

            self.connected_room.hass.bus.fire("connectedroom_event", event_data)

        if "natural_text" in data and data["natural_text"] is not None:
            await self.connected_room.tts(data["natural_text"])


class ConnectedRoomDeviceEvents:
    def __init__(
        self, connected_room: ConnectedRoom, pusher: pysher.Pusher, integration_key: str
    ):
        self.connected_room = connected_room
        self.pusher = pusher
        self.integration_key = integration_key

        self.channel = pusher.subscribe("private-home-assistant." + integration_key)

        devices = self.connected_room.coordinator.config_entry.options.get("devices")

        if devices is not None and devices["entity_id"] is not None:
            for entity_id in devices["entity_id"]:
                if ("execute." + entity_id) not in self.channel.event_callbacks:
                    self.channel.bind(
                        "execute." + entity_id,
                        lambda data, entity_id_local=entity_id, **kargs: asyncio.run(
                            self.on_execute(data, entity_id_local)
                        ),
                    )

                if ("get_state." + entity_id) not in self.channel.event_callbacks:
                    self.channel.bind(
                        "get_state." + entity_id,
                        lambda data, entity_id_local=entity_id, **kargs: asyncio.run(
                            self.on_get_state(data, entity_id_local)
                        ),
                    )

    async def on_execute(self, data, entity_id):
        data = json.loads(data)

        if data["action"] == "set_color":
            self.connected_room.hass.services.call(
                domain="light",
                service="turn_on",
                target={"entity_id": [entity_id]},
                service_data={"xy_color": {data["color"][0], data["color"][1]}},
            )

        elif data["action"] == "set_effect":
            self.connected_room.hass.services.call(
                domain="light",
                service="turn_on",
                target={"entity_id": [entity_id]},
                service_data={"effect": data["effect"]},
            )

        elif data["action"] == "set_brightness":
            self.connected_room.hass.services.call(
                domain="light",
                service="turn_on",
                target={"entity_id": [entity_id]},
                service_data={"brightness": data["brightness"]},
            )

        elif data["action"] == "turn_off":
            self.connected_room.hass.services.call(
                domain="light", service="turn_off", target={"entity_id": [entity_id]}
            )

        elif data["action"] == "turn_on":
            self.connected_room.hass.services.call(
                domain="light", service="turn_on", target={"entity_id": [entity_id]}
            )

        elif data["action"] == "restore":
            self.connected_room.hass.services.call(
                domain="light",
                service="turn_on",
                target={"entity_id": [entity_id]},
                service_data=data["state"],
            )

    async def on_get_state(self, data, entity_id):
        data = json.loads(data)

        if data["request_id"] is None:
            return

        _LOGGER.info(self.connected_room.hass.states.get(entity_id))

        state = self.connected_room.hass.states.get(entity_id)

        headers = {"Authorization": "Bearer " + self.connected_room.auth["api_key"]}

        payload = {
            "scope": "home-assistant." + self.connected_room.auth["integration_key"],
            "payload": json.dumps(state.attributes),
            "request_id": data["request_id"],
        }

        try:
            httpx.post(
                API_URL + "/requests/execute",
                json=payload,
                headers=headers,
                verify=False,
            )
        except Exception:
            raise ConnectionError


class InvalidAuth(HomeAssistantError):
    """Error to indicate there is invalid auth."""


class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect."""
