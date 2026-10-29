"""One Boost button per room."""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_ROOM_LABEL, CONF_ROOMS, DATA_CONTROLLER, DOMAIN
from .helpers import hhc_device_info


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    cfg = hass.data.get(DOMAIN, {}).get(entry.entry_id, {}).get("config", {})
    async_add_entities(
        [
            RoomBoostButton(hass, entry, room_id, str(room.get(CONF_ROOM_LABEL, room_id)))
            for room_id, room in (cfg.get(CONF_ROOMS) or {}).items()
            if isinstance(room, dict)
        ]
    )


class RoomBoostButton(ButtonEntity):
    """Press to boost the room by the Boost amount for the Boost time.
    Press again while it is running to cancel."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:radiator"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, room_id: str, label: str) -> None:
        self.hass = hass
        self._entry = entry
        self._room_id = room_id
        self._attr_name = f"{label} boost"
        self._attr_unique_id = f"{entry.entry_id}_room_{room_id}_boost"
        self._attr_device_info = hhc_device_info(entry)

    async def async_press(self) -> None:
        controller = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {}).get(DATA_CONTROLLER)
        if controller is not None:
            controller.toggle_room_boost(self._room_id)
