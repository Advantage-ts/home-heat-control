"""Time boxes for the immersion daily window."""
from __future__ import annotations

from datetime import time as dt_time

from homeassistant.components.time import TimeEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DATA_CONTROLLER, DATA_ENABLED, DEFAULT_ENABLED, DOMAIN
from .helpers import hhc_device_info
from .immersion import CONF_IMMERSION, IMMERSION_DEFAULTS, _parse_time


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities(
        [
            ImmersionTime(hass, entry, "window_start", "Immersion window start", "mdi:clock-start"),
            ImmersionTime(hass, entry, "window_end", "Immersion window end", "mdi:clock-end"),
        ]
    )


class ImmersionTime(TimeEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, hass, entry, key, name, icon) -> None:
        self.hass = hass
        self._entry = entry
        self._key = key
        self._attr_name = name
        self._attr_icon = icon
        self._attr_unique_id = f"{entry.entry_id}_immersion_{key}"
        self._attr_device_info = hhc_device_info(entry)

    def _entry_data(self):
        return self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id)

    @property
    def native_value(self) -> dt_time:
        data = self._entry_data() or {}
        section = (data.get("config") or {}).get(CONF_IMMERSION) or {}
        return _parse_time(section.get(self._key), IMMERSION_DEFAULTS[self._key])

    async def async_set_value(self, value: dt_time) -> None:
        data = self._entry_data()
        if data is None:
            return
        config = dict(data.get("config") or {})
        section = dict(config.get(CONF_IMMERSION) or {})
        section[self._key] = value.strftime("%H:%M")
        config[CONF_IMMERSION] = section
        data["config"] = config
        self.hass.config_entries.async_update_entry(self._entry, data=config)
        controller = data.get(DATA_CONTROLLER)
        if controller is not None:
            controller.config[CONF_IMMERSION] = section
            controller.immersion.evaluate()
        self.hass.states.async_set(
            f"{DOMAIN}.config",
            "active" if config.get(DATA_ENABLED, DEFAULT_ENABLED) else "disabled",
            attributes=config,
        )
        self.async_write_ha_state()
