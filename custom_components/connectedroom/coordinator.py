"""DataUpdateCoordinator for WLED."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import callback
from homeassistant.core import Event
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .connectedroom import ConnectedRoom
from .const import DOMAIN

LOGGER = logging.getLogger(__name__)


class ConnectedRoomCoordinator(DataUpdateCoordinator):
    """Class to manage fetching WLED data from single endpoint."""

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(hass, LOGGER, name=DOMAIN)

        self.config_entry = entry
        self._api_key = entry.data["api_key"]
        self._unique_id = entry.data["unique_id"]
        self.connectedroom = ConnectedRoom(hass, self)
        self.hass = hass
        self.socket = None

    @callback
    def _use_websocket(self) -> None:
        """Use WebSocket for updates, instead of polling."""

        async def listen() -> None:
            """Listen for state changes via WebSocket."""
            self.socket = await self.connectedroom.connectedroom_websocket_connect(
                self._api_key, self._unique_id
            )

        async def close_websocket(_: Event) -> None:
            """Close WebSocket connection."""
            if self.socket is not None:
                await self.socket.disconnect()

        # Clean disconnect WebSocket on Home Assistant shutdown
        self.unsub = self.hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STOP, close_websocket
        )

        # Start listening
        self.config_entry.async_create_background_task(
            self.hass, listen(), "connectedroom-listen"
        )

    async def _async_update_data(self):
        """Fetch data from WLED."""

        self._use_websocket()
