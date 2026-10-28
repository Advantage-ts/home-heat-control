"""Boiler demand as seen by Home Heat Control."""
from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DATA_CONTROLLER, DOMAIN, SIGNAL_DECISIONS_UPDATED
from .helpers import hhc_device_info


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities([BoilerDemandSensor(hass, entry)], True)


class BoilerDemandSensor(BinarySensorEntity):
    """On when Home Heat Control wants the central heating on.

    In watch-only mode this is what it would do, so it can be compared with
    the real boiler relay before taking over.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_name = "Boiler demand"
    _attr_device_class = BinarySensorDeviceClass.HEAT
    _attr_icon = "mdi:fire"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_boiler_demand"
        self._attr_device_info = hhc_device_info(entry)

    @property
    def _controller(self):
        return self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {}).get(DATA_CONTROLLER)

    @property
    def is_on(self) -> bool | None:
        controller = self._controller
        return None if controller is None else controller.boiler_demand

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        controller = self._controller
        if controller is None:
            return {}
        return {
            "reason": controller.boiler_reason,
            "rooms_calling": controller.boiler_calling_rooms,
            "rooms_blocked": controller.boiler_blocked_rooms,
            "watch_only": controller.is_watch_only(),
        }

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_DECISIONS_UPDATED, self._refresh)
        )

    @callback
    def _refresh(self) -> None:
        self.async_write_ha_state()
