import asyncio
import json
import logging
from threading import Timer

import httpx
import pysher
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import event
from homeassistant.loader import async_get_integration
from homeassistant.util import color as color_util

from .const import API_URL
from .const import VERSION
from .const import WSS_HOST
from .const import WSS_KEY


_LOGGER = logging.getLogger(__name__)


SUPPORTED_DOMAINS = ("light", "switch")

CAPABILITY_ATTRIBUTES = (
    "supported_color_modes",
    "effect_list",
    "min_color_temp_kelvin",
    "max_color_temp_kelvin",
    "min_mireds",
    "max_mireds",
)

COLOR_MODES = ("hs", "rgb", "xy", "rgbw", "rgbww")

FLASH_SLOWEST_INTERVAL = 1.0
FLASH_FASTEST_LOCAL_INTERVAL = 0.05
FLASH_FASTEST_CLOUD_INTERVAL = 0.5
FLASH_MAX_DURATION = 300.0
FLASH_DEFAULT_DURATION = 3.0
FLASH_DEFAULT_SPEED = 5

ALTERNATE_MAX_COLORS = 3
COLOR_ALIKE_HUE_GAP = 30.0
COLOR_ALIKE_SATURATION_GAP = 25.0
COLOR_WASHED_OUT_SATURATION = 15.0


async def flash_interval(hass, entity_id, speed):
    """Half cycle of a flash, tightened when the device is driven locally."""

    try:
        speed = int(speed)
    except (TypeError, ValueError):
        speed = FLASH_DEFAULT_SPEED

    speed = min(10, max(1, speed))

    fastest = FLASH_FASTEST_CLOUD_INTERVAL

    entry = er.async_get(hass).async_get(entity_id)

    if entry is not None:
        try:
            integration = await async_get_integration(hass, entry.platform)
        except Exception:
            integration = None

        if integration is not None and (integration.iot_class or "").startswith(
            "local"
        ):
            fastest = FLASH_FASTEST_LOCAL_INTERVAL

    return FLASH_SLOWEST_INTERVAL - (speed - 1) * (FLASH_SLOWEST_INTERVAL - fastest) / 9


def as_list(value):
    if value is None:
        return []

    if isinstance(value, str):
        return [value]

    return list(value)


def resolve_target_entities(hass, target):
    """Expand a target selection into the entity ids it actually refers to."""

    if not target:
        return []

    entity_registry = er.async_get(hass)
    device_registry = dr.async_get(hass)
    area_registry = ar.async_get(hass)

    entity_ids = []

    def add(entity_id):
        if entity_id.split(".")[0] in SUPPORTED_DOMAINS and entity_id not in entity_ids:
            entity_ids.append(entity_id)

    def add_entry(entry):
        if entry.disabled_by is None:
            add(entry.entity_id)

    def add_device(device_id):
        for entry in er.async_entries_for_device(entity_registry, device_id):
            add_entry(entry)

    for entity_id in as_list(target.get("entity_id")):
        add(entity_id)

    for device_id in as_list(target.get("device_id")):
        add_device(device_id)

    area_ids = as_list(target.get("area_id"))

    for floor_id in as_list(target.get("floor_id")):
        for area in ar.async_entries_for_floor(area_registry, floor_id):
            area_ids.append(area.id)

    for label_id in as_list(target.get("label_id")):
        for entry in er.async_entries_for_label(entity_registry, label_id):
            add_entry(entry)

        for device in dr.async_entries_for_label(device_registry, label_id):
            add_device(device.id)

        for area in ar.async_entries_for_label(area_registry, label_id):
            area_ids.append(area.id)

    for area_id in area_ids:
        for entry in er.async_entries_for_area(entity_registry, area_id):
            add_entry(entry)

        for device in dr.async_entries_for_area(device_registry, area_id):
            for entry in er.async_entries_for_device(entity_registry, device.id):
                if entry.area_id is None:
                    add_entry(entry)

    return entity_ids


def resolve_entity_name(hass, entity_id, entity, device):
    """Find the name the user sees in Home Assistant for this entity."""

    state = hass.states.get(entity_id)

    if state is not None:
        friendly_name = state.attributes.get("friendly_name")

        if friendly_name:
            return friendly_name

    if entity is not None:
        if entity.name:
            return entity.name

        if entity.original_name:
            return entity.original_name

    if device is not None:
        if device.name_by_user:
            return device.name_by_user

        if device.name:
            return device.name

    return entity_id


def resolve_capabilities(entity, state):
    if entity is not None and entity.capabilities:
        return entity.capabilities

    if state is None:
        return None

    return {
        key: value
        for key, value in state.attributes.items()
        if key in CAPABILITY_ATTRIBUTES and value is not None
    }


def normalize_color(data):
    """Read the colour out of a payload whichever format it was sent in."""

    if data.get("hs_color") is not None:
        hs_color = data["hs_color"]
        return float(hs_color[0]), float(hs_color[1])

    if data.get("rgb_color") is not None:
        rgb_color = data["rgb_color"]
        return color_util.color_RGB_to_hs(
            int(rgb_color[0]), int(rgb_color[1]), int(rgb_color[2])
        )

    color = data.get("xy_color")

    if color is None:
        color = data.get("color")

    if color is None or len(color) < 2:
        return None

    if len(color) >= 3:
        return color_util.color_RGB_to_hs(int(color[0]), int(color[1]), int(color[2]))

    return color_util.color_xy_to_hs(float(color[0]), float(color[1]))


def parse_color(value):
    """Read one colour of an alternate sequence. None means off."""

    if value is None:
        return None

    if isinstance(value, dict):
        if "r" in value and "g" in value and "b" in value:
            red, green, blue = int(value["r"]), int(value["g"]), int(value["b"])

            if red == green == blue == 0:
                return None

            return color_util.color_RGB_to_hs(red, green, blue)

        return normalize_color(value)

    if isinstance(value, (list, tuple)):
        values = list(value)

        if len(values) >= 3 and not any(values[:3]):
            return None

        return normalize_color({"color": values})

    return None


def colors_alike(first, second):
    """Whether two steps would read as the same colour on a wall."""

    if first is None or second is None:
        return first is None and second is None

    if (
        first[1] < COLOR_WASHED_OUT_SATURATION
        and second[1] < COLOR_WASHED_OUT_SATURATION
    ):
        # both washed out to white, whatever their hue claims
        return True

    hue_gap = abs(first[0] - second[0]) % 360
    hue_gap = min(hue_gap, 360 - hue_gap)

    return (
        hue_gap < COLOR_ALIKE_HUE_GAP
        and abs(first[1] - second[1]) < COLOR_ALIKE_SATURATION_GAP
    )


def collapse_alike(steps):
    """Drop neighbours that look the same, the sequence being a loop."""

    collapsed = []

    for step in steps:
        if collapsed and colors_alike(collapsed[-1], step):
            continue

        collapsed.append(step)

    while len(collapsed) > 1 and colors_alike(collapsed[0], collapsed[-1]):
        collapsed.pop()

    return collapsed


def drop_stranded_off(steps):
    """An off between two different colours just interrupts the alternation."""

    if len(steps) != 3:
        return steps

    for index, step in enumerate(steps):
        if step is not None:
            continue

        before = steps[index - 1]
        after = steps[(index + 1) % 3]

        if not colors_alike(before, after):
            return [before, after]

    return steps


def alternate_steps(colors):
    """Build the colours to cycle through, off standing in for a blink."""

    steps = [parse_color(color) for color in as_list(colors)][:ALTERNATE_MAX_COLORS]

    if not any(step is not None for step in steps):
        return []

    steps = collapse_alike(steps)

    if len(steps) == 1:
        # one colour on its own cannot alternate, so blink it
        steps.append(None)

    return drop_stranded_off(steps)


def restorable_state(state):
    """Keep only the attributes light.turn_on accepts, with one colour key."""

    if not state:
        return {}

    payload = {}

    for key in ("brightness", "effect"):
        if state.get(key) is not None:
            payload[key] = state[key]

    color_mode = state.get("color_mode")

    if color_mode == "color_temp" and state.get("color_temp_kelvin") is not None:
        payload["color_temp_kelvin"] = state["color_temp_kelvin"]
        return payload

    color_keys = [color_mode + "_color"] if color_mode in COLOR_MODES else []

    for key in COLOR_MODES:
        color_keys.append(key + "_color")

    for key in color_keys:
        if state.get(key) is not None:
            payload[key] = list(state[key])
            break

    return payload


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
        self.effect_generations = {}

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

        for entity_id in list(self.effect_generations):
            self.effect_generations[entity_id] += 1

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

        entity_ids = resolve_target_entities(self.hass, devices)

        entity_registry = er.async_get(self.hass)
        device_registry = dr.async_get(self.hass)

        to_sync = []

        for entity_id in entity_ids:
            entity = entity_registry.async_get(entity_id)

            device = None

            if entity is not None and entity.device_id is not None:
                device = device_registry.async_get(entity.device_id)

            state = self.hass.states.get(entity_id)

            to_sync.append(
                {
                    "entity_id": entity_id,
                    "capabilities": resolve_capabilities(entity, state),
                    "device_class": entity_id.split(".")[0],
                    "name": resolve_entity_name(self.hass, entity_id, entity, device),
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

    def start_effect(self, entity_id, build):
        """Hand an effect to the event loop so the socket thread stays free."""

        generation = self.effect_generations.get(entity_id, 0) + 1
        self.effect_generations[entity_id] = generation

        self.hass.loop.call_soon_threadsafe(
            lambda: self.coordinator.config_entry.async_create_background_task(
                self.hass, build(generation), "connectedroom-effect"
            )
        )

    def start_flash(self, entity_id, duration, speed):
        self.start_effect(
            entity_id,
            lambda generation: self.flash(entity_id, generation, duration, speed),
        )

    def start_alternate(self, entity_id, duration, speed, colors):
        self.start_effect(
            entity_id,
            lambda generation: self.alternate(
                entity_id, generation, duration, speed, colors
            ),
        )

    def effect_superseded(self, entity_id, generation):
        return self.effect_generations.get(entity_id, 0) != generation

    async def effect_call(self, domain, service, entity_id, service_data):
        await self.hass.services.async_call(
            domain=domain,
            service=service,
            target={"entity_id": [entity_id]},
            service_data=service_data,
            blocking=False,
        )

    async def flash(self, entity_id, generation, duration, speed):
        """Blink a device from here so the cluster only sends one command."""

        domain = entity_id.split(".")[0]

        if domain not in SUPPORTED_DOMAINS:
            return

        try:
            duration = float(duration)
        except (TypeError, ValueError):
            duration = FLASH_DEFAULT_DURATION

        duration = min(FLASH_MAX_DURATION, max(0.0, duration))

        interval = await flash_interval(self.hass, entity_id, speed)

        # a fade would smear the blink, and lights that cannot transition drop it
        step = {"transition": 0} if domain == "light" else {}

        restore = step

        state = self.hass.states.get(entity_id)

        if domain == "light" and state is not None and state.state == "on":
            restore = dict(step, **restorable_state(state.attributes))

        # counted up front, so accumulated float error cannot eat a blink
        cycles = int(duration * 1000) // int(interval * 2000)

        for _ in range(cycles):
            if self.effect_superseded(entity_id, generation):
                return

            await self.effect_call(domain, "turn_off", entity_id, step)
            await asyncio.sleep(interval)

            if self.effect_superseded(entity_id, generation):
                return

            await self.effect_call(domain, "turn_on", entity_id, step)
            await asyncio.sleep(interval)

        if self.effect_superseded(entity_id, generation):
            return

        # always land on, back the way we found it
        await self.effect_call(domain, "turn_on", entity_id, restore)

    async def alternate(self, entity_id, generation, duration, speed, colors):
        """Cycle a light through 2 or 3 colours from here, in one command."""

        domain = entity_id.split(".")[0]

        if domain == "switch":
            # no colour to give it, so the nearest thing is a blink
            await self.flash(entity_id, generation, duration, speed)
            return

        if domain != "light":
            return

        steps = alternate_steps(colors)

        if len(steps) < 2:
            return

        try:
            duration = float(duration)
        except (TypeError, ValueError):
            duration = FLASH_DEFAULT_DURATION

        duration = min(FLASH_MAX_DURATION, max(0.0, duration))

        interval = await flash_interval(self.hass, entity_id, speed)

        cycles = int(duration * 1000) // int(interval * 1000 * len(steps))

        for _ in range(cycles):
            for step in steps:
                if self.effect_superseded(entity_id, generation):
                    return

                await self.show_color(entity_id, step)
                await asyncio.sleep(interval)

        if self.effect_superseded(entity_id, generation):
            return

        # always land lit, on the first colour of the sequence
        await self.show_color(entity_id, next(s for s in steps if s is not None))

    async def show_color(self, entity_id, step):
        if step is None:
            await self.effect_call("light", "turn_off", entity_id, {"transition": 0})
            return

        await self.effect_call(
            "light",
            "turn_on",
            entity_id,
            {"transition": 0, "hs_color": [step[0], step[1]]},
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

        for entity_id in resolve_target_entities(self.connected_room.hass, devices):
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

        domain = entity_id.split(".")[0]

        if domain != "light" and data["action"] in (
            "set_color",
            "set_effect",
            "set_brightness",
        ):
            return

        if data["action"] == "set_color":
            color = normalize_color(data)

            if color is None:
                return

            self.connected_room.hass.services.call(
                domain="light",
                service="turn_on",
                target={"entity_id": [entity_id]},
                service_data={"hs_color": [color[0], color[1]]},
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
                domain=domain, service="turn_off", target={"entity_id": [entity_id]}
            )

        elif data["action"] == "turn_on":
            self.connected_room.hass.services.call(
                domain=domain, service="turn_on", target={"entity_id": [entity_id]}
            )

        elif data["action"] == "flash":
            self.connected_room.start_flash(
                entity_id, data.get("duration"), data.get("speed")
            )

        elif data["action"] == "alternate":
            self.connected_room.start_alternate(
                entity_id,
                data.get("duration"),
                data.get("speed"),
                data.get("colors"),
            )

        elif data["action"] == "restore":
            self.connected_room.hass.services.call(
                domain=domain,
                service="turn_on",
                target={"entity_id": [entity_id]},
                service_data=restorable_state(data["state"])
                if domain == "light"
                else {},
            )

    async def on_get_state(self, data, entity_id):
        data = json.loads(data)

        if data["request_id"] is None:
            return

        state = self.connected_room.hass.states.get(entity_id)

        if state is None:
            return

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
