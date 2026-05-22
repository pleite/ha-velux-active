"""DataUpdateCoordinator for the Velux ACTIVE integration."""
from __future__ import annotations

import logging
import time
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import VeluxActiveApi, VeluxActiveAuthError, VeluxActiveConnectionError
from .const import DOMAIN, UPDATE_INTERVAL

_LOGGER = logging.getLogger(__name__)

# Refresh homesdata (module<->bridge<->room mapping) at least this often.
# A re-pairing on the KIX 300 can rotate the bridge id; if we cache the old
# one forever, setstate commands silently target a stale bridge and never
# reach the actuator.
HOMES_DATA_REFRESH_SECONDS = 30 * 60


class VeluxActiveCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Manages fetching data from the Velux ACTIVE cloud."""

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        api: VeluxActiveApi,
        home_id: str,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=UPDATE_INTERVAL),
        )
        self.api = api
        self.home_id = home_id
        self.module_names: dict[str, str] = {}
        self.room_names: dict[str, str] = {}
        self.module_rooms: dict[str, str] = {}
        self.module_bridges: dict[str, str] = {}
        self._names_fetched_at: float = 0.0

    def _extract_names(self, data: Any) -> None:
        """Recursively extract all 'id' -> 'name' and 'room_id' mappings."""
        if isinstance(data, dict):
            item_id = data.get("id")
            if isinstance(item_id, str):
                if item_name := data.get("name"):
                    if isinstance(item_name, str):
                        self.module_names[item_id] = item_name
                        self.room_names[item_id] = item_name
                if room_id := data.get("room_id"):
                    if isinstance(room_id, str):
                        self.module_rooms[item_id] = room_id
                if bridge_id := data.get("bridge"):
                    if isinstance(bridge_id, str):
                        self.module_bridges[item_id] = bridge_id
            for value in data.values():
                self._extract_names(value)
        elif isinstance(data, list):
            for item in data:
                self._extract_names(item)

    async def _async_fetch_names(self, *, force: bool = False) -> None:
        """Fetch human-readable names + bridge mapping from homesdata.

        Re-fetched periodically because the KIX 300 bridge id can change after
        a re-pairing; a stale bridge id causes setstate to silently no-op.
        """
        now = time.time()
        if (
            not force
            and self._names_fetched_at
            and now - self._names_fetched_at < HOMES_DATA_REFRESH_SECONDS
        ):
            return

        try:
            data = await self.api.async_get_homes_data()
        except (VeluxActiveAuthError, VeluxActiveConnectionError) as err:
            _LOGGER.warning("Failed to fetch homes data for names: %s", err)
            return

        homes = data.get("body", {}).get("homes", [])
        for home in homes:
            if home.get("id") == self.home_id:
                self._extract_names(home)

        self._names_fetched_at = now

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from the Velux ACTIVE API."""
        await self._async_fetch_names()

        try:
            status = await self.api.async_get_home_status(self.home_id)
        except VeluxActiveAuthError as err:
            raise ConfigEntryAuthFailed(err) from err
        except VeluxActiveConnectionError as err:
            raise UpdateFailed(err) from err

        home: dict[str, Any] = status.get("body", {}).get("home", {})

        # Inject human-readable names, bridge id, and room relationships
        for module in home.get("modules", []):
            mod_id = module.get("id")
            if mod_id in self.module_names:
                module["name"] = self.module_names[mod_id]
            if mod_id in self.module_rooms:
                module["room_id"] = self.module_rooms[mod_id]
            # Ensure the bridge id stays fresh — see HOMES_DATA_REFRESH_SECONDS
            if mod_id in self.module_bridges and not module.get("bridge"):
                module["bridge"] = self.module_bridges[mod_id]
                
        for room in home.get("rooms", []):
            if room.get("id") in self.room_names:
                room["name"] = self.room_names[room["id"]]

        return home
