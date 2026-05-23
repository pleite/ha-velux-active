"""Cover platform for Velux ACTIVE (NXO roller shutters and windows)."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.cover import (
    ATTR_POSITION,
    CoverDeviceClass,
    CoverEntity,
    CoverEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MODULE_TYPE_BRIDGE, MODULE_TYPE_ROLLER_SHUTTER, MODEL_MAP
from .coordinator import VeluxActiveCoordinator

_LOGGER = logging.getLogger(__name__)

VELUX_TYPE_TO_DEVICE_CLASS = {
    "window": CoverDeviceClass.WINDOW,
    "shutter": CoverDeviceClass.SHUTTER,
    "blind": CoverDeviceClass.BLIND,
    "awning": CoverDeviceClass.AWNING,
    "curtain": CoverDeviceClass.CURTAIN,
    "shade": CoverDeviceClass.SHADE,
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Velux ACTIVE cover entities from a config entry."""
    coordinator: VeluxActiveCoordinator = hass.data[DOMAIN][entry.entry_id]

    modules: list[dict[str, Any]] = coordinator.data.get("modules", [])
    entities = [
        VeluxActiveCover(coordinator, module)
        for module in modules
        if module.get("type") == MODULE_TYPE_ROLLER_SHUTTER
    ]
    async_add_entities(entities)


class VeluxActiveCover(CoordinatorEntity[VeluxActiveCoordinator], CoverEntity):
    """Representation of a Velux ACTIVE cover (roller shutter / window)."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_supported_features = (
        CoverEntityFeature.OPEN
        | CoverEntityFeature.CLOSE
        | CoverEntityFeature.STOP
        | CoverEntityFeature.SET_POSITION
    )

    def __init__(
        self,
        coordinator: VeluxActiveCoordinator,
        module: dict[str, Any],
    ) -> None:
        """Initialize the cover entity."""
        super().__init__(coordinator)
        self._module_id: str = module["id"]
        self._bridge_id: str = module.get("bridge", "")
        self._attr_unique_id = self._module_id
        velux_type: str = module.get("velux_type", "shutter")
        self._attr_device_class = VELUX_TYPE_TO_DEVICE_CLASS.get(
            velux_type, CoverDeviceClass.SHUTTER
        )
        
        device_name = module.get("name")
        if not device_name or device_name == self._module_id:
            # Fallback: Room Name + Device Type
            room_id = module.get("room_id")
            room_name = coordinator.room_names.get(room_id) if room_id else None
            type_name = velux_type.replace("_", " ").capitalize()
            if room_name:
                device_name = f"{room_name} {type_name}"
            else:
                device_name = f"{type_name} {self._module_id}"

        fw_ver = str(module.get("firmware_revision", ""))
        hw_ver = str(module.get("hardware_version", ""))

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._module_id)},
            name=device_name,
            manufacturer=module.get("manufacturer", "Velux"),
            model=MODEL_MAP.get(module.get("velux_type", MODULE_TYPE_ROLLER_SHUTTER), module.get("velux_type", MODULE_TYPE_ROLLER_SHUTTER)),
            via_device=(DOMAIN, self._bridge_id) if self._bridge_id else None,
            sw_version=fw_ver if fw_ver else None,
            hw_version=hw_ver if hw_ver else None,
            connections=set(),
        )
        # Initialise cached position from the first coordinator payload
        self._attr_current_cover_position: int | None = module.get("current_position")

    @property
    def _module(self) -> dict[str, Any]:
        """Return the current module status data."""
        for mod in self.coordinator.data.get("modules", []):
            if mod.get("id") == self._module_id:
                return mod
        return {}

    @property
    def _current_bridge_id(self) -> str:
        """Return the freshest known bridge id for this module.

        Prefer the bridge id from the latest coordinator payload (which is
        re-synced from homesdata every ~30 minutes). Fall back to the value
        cached at entity construction. This avoids silently sending setstate
        to a stale bridge id after a KIX 300 re-pairing.
        """
        mod = self._module
        if isinstance(mod, dict):
            bridge = mod.get("bridge")
            if isinstance(bridge, str) and bridge:
                return bridge
        from_cache = self.coordinator.module_bridges.get(self._module_id)
        if from_cache:
            return from_cache
        return self._bridge_id

    @property
    def _current_velux_type(self) -> str:
        """Return the current Velux module type."""
        mod = self._module
        velux_type = mod.get("velux_type")
        if isinstance(velux_type, str) and velux_type:
            return velux_type
        return "shutter"

    @callback
    def _handle_coordinator_update(self) -> None:
        """Update cached state from the coordinator and write to HA."""
        mod = self._module
        self._attr_current_cover_position = mod.get("current_position")
        super()._handle_coordinator_update()

    @property
    def is_closed(self) -> bool | None:
        """Return True if the cover is fully closed."""
        pos = self._attr_current_cover_position
        if pos is None:
            return None
        return pos == 0

    @property
    def is_opening(self) -> bool:
        """Return True if the cover is opening."""
        mod = self._module
        cur = mod.get("current_position")
        tgt = mod.get("target_position")
        if cur is None or tgt is None:
            return False
        return tgt > cur

    @property
    def is_closing(self) -> bool:
        """Return True if the cover is closing."""
        mod = self._module
        cur = mod.get("current_position")
        tgt = mod.get("target_position")
        if cur is None or tgt is None:
            return False
        return tgt < cur

    @property
    def available(self) -> bool:
        """Return True if the module is reachable."""
        return self.coordinator.last_update_success and self._module.get(
            "reachable", True
        )

    async def async_open_cover(self, **kwargs: Any) -> None:
        """Open the cover fully (100%)."""
        await self.coordinator.api.async_set_cover_position(
            self.coordinator.home_id,
            self._current_bridge_id,
            self._module_id,
            100,
            velux_type=self._current_velux_type,
        )
        await self.coordinator.async_request_refresh()

    async def async_close_cover(self, **kwargs: Any) -> None:
        """Close the cover fully (0%)."""
        await self.coordinator.api.async_set_cover_position(
            self.coordinator.home_id,
            self._current_bridge_id,
            self._module_id,
            0,
            velux_type=self._current_velux_type,
        )
        await self.coordinator.async_request_refresh()

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        """Set the cover to a specific position."""
        position: int = kwargs[ATTR_POSITION]
        await self.coordinator.api.async_set_cover_position(
            self.coordinator.home_id,
            self._current_bridge_id,
            self._module_id,
            position,
            velux_type=self._current_velux_type,
        )
        await self.coordinator.async_request_refresh()

    async def async_stop_cover(self, **kwargs: Any) -> None:
        """Stop any cover movement."""
        await self.coordinator.api.async_stop_movements(
            self.coordinator.home_id, self._current_bridge_id
        )
        await self.coordinator.async_request_refresh()
