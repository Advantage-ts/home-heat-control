"""Behaviour tests for Home Heat Control running inside Home Assistant."""
from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.home_heat_control.const import DOMAIN, DATA_CONTROLLER

BOILER = "switch.central_heating_relay_l1"
HW_RELAY = "switch.central_heating_relay_l2"


def room(label, sensor, thermostat="", **extra):
    return {
        "label": label,
        "area_id": label.lower(),
        "sensor": sensor,
        "thermostat": thermostat,
        "target_day": 21.0,
        "target_night": 21.0,
        "day_start": "",
        "night_start": "",
        "enabled": True,
        "room_heating_mode": "adaptive",
        **extra,
    }


def base_config(**overrides) -> dict[str, Any]:
    cfg = {
        "enabled": True,
        "main_control_type": "switch",
        "main_switch": BOILER,
        "heating_mode": "adaptive",
        "watch_only": False,
        "boiler_min_on_minutes": 0,
        "boiler_min_off_minutes": 0,
        "rooms": {
            "millie": room("Millie", "sensor.millie_temp", "climate.millie_radiator"),
        },
    }
    cfg.update(overrides)
    return cfg


@pytest.fixture
async def calls(hass: HomeAssistant):
    """Record switch and climate service calls instead of running them."""
    from homeassistant.setup import async_setup_component

    # Load the real components first so our mocks replace their services.
    for component in ("switch", "climate", "persistent_notification"):
        assert await async_setup_component(hass, component, {})
    recorded: list[ServiceCall] = []

    async def _record(call: ServiceCall) -> None:
        recorded.append(call)
        entity = call.data.get("entity_id")
        if call.domain == "switch" and isinstance(entity, str):
            hass.states.async_set(entity, "on" if call.service == "turn_on" else "off")

    for domain, service in (
        ("switch", "turn_on"),
        ("switch", "turn_off"),
        ("climate", "set_temperature"),
        ("climate", "set_hvac_mode"),
        ("persistent_notification", "create"),
    ):
        hass.services.async_register(domain, service, _record)
    return recorded


def set_world(hass, millie=19.0, tank=None, schedule="off"):
    hass.states.async_set("sensor.millie_temp", str(millie))
    hass.states.async_set(
        "climate.millie_radiator", "auto",
        {"current_temperature": 18.0, "temperature": 5.0, "min_temp": 5.0, "target_temp_step": 0.5},
    )
    hass.states.async_set(BOILER, "off")
    hass.states.async_set(HW_RELAY, "off")
    if tank is not None:
        hass.states.async_set("sensor.hot_water_tank_temperature", str(tank))
    hass.states.async_set("schedule.hot_water_schedule", schedule)


async def setup(hass, cfg) -> Any:
    entry = MockConfigEntry(domain=DOMAIN, data=cfg, title="Home Heat Control")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return hass.data[DOMAIN][entry.entry_id][DATA_CONTROLLER]


def boiler_calls(calls):
    return [c.service for c in calls if c.domain == "switch" and c.data.get("entity_id") == BOILER]


async def test_cold_room_turns_boiler_on(hass, calls):
    set_world(hass, millie=19.0)
    ctrl = await setup(hass, base_config())
    assert ctrl.boiler_demand is True
    assert boiler_calls(calls) == ["turn_on"]
    assert hass.states.get("binary_sensor.home_heat_boiler_demand").state == "on"


async def test_workshop_cannot_call_boiler(hass, calls):
    """The 27 Oct fault: a valve-less room at 5 C kept the boiler on."""
    set_world(hass, millie=22.0)
    hass.states.async_set("sensor.workshop_temp", "5.5")
    cfg = base_config()
    cfg["rooms"]["workshop"] = room("Workshop", "sensor.workshop_temp")
    ctrl = await setup(hass, cfg)
    assert ctrl.boiler_demand is False
    assert "turn_on" not in boiler_calls(calls)
    attrs = hass.states.get("binary_sensor.home_heat_boiler_demand").attributes
    assert attrs["rooms_blocked"] == {"Workshop": "no radiator valve"}


async def test_implausible_reading_cannot_call_boiler(hass, calls):
    set_world(hass, millie=2.0)
    ctrl = await setup(hass, base_config())
    assert ctrl.boiler_demand is False
    assert ctrl.boiler_blocked_rooms == {"Millie": "implausible reading 2.0"}


async def test_watch_only_sends_nothing(hass, calls):
    set_world(hass, millie=19.0)
    ctrl = await setup(hass, base_config(watch_only=True))
    assert ctrl.boiler_demand is True
    assert calls == []
    log = hass.states.get("sensor.home_heat_decisions").attributes["recent"]
    assert any(e["kind"] == "would_send" and BOILER in e["message"] for e in log)


async def test_fresh_install_defaults_to_watch_only(hass, calls):
    set_world(hass, millie=19.0)
    cfg = base_config()
    del cfg["watch_only"]
    ctrl = await setup(hass, cfg)
    assert ctrl.is_watch_only()
    assert calls == []


async def test_target_ceiling(hass, calls):
    set_world(hass, millie=19.0)
    cfg = base_config(max_room_target=22.0)
    cfg["rooms"]["millie"]["target_day"] = 30.0
    cfg["rooms"]["millie"]["target_night"] = 30.0
    ctrl = await setup(hass, cfg)
    assert ctrl._effective_target_for_room(ctrl.config["rooms"]["millie"]) == 22.0


async def test_reload_never_adds_rooms(hass, calls):
    set_world(hass, millie=22.0)
    hass.states.async_set("sensor.workshop_temp", "5.5", {"device_class": "temperature", "unit_of_measurement": "°C"})
    ctrl = await setup(hass, base_config())
    await hass.services.async_call(DOMAIN, "reload", {}, blocking=True)
    await hass.async_block_till_done()
    assert list(ctrl.config["rooms"]) == ["millie"]


async def test_boiler_forced_off_after_manual_on(hass, calls):
    set_world(hass, millie=22.0)
    ctrl = await setup(hass, base_config())
    assert ctrl.boiler_demand is False
    hass.states.async_set(BOILER, "on")  # someone or something turns it on
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=2))
    await hass.async_block_till_done()
    assert "turn_off" in boiler_calls(calls)


async def test_boiler_run_limit(hass, calls):
    set_world(hass, millie=19.0)
    ctrl = await setup(hass, base_config(boiler_max_run_minutes=60, boiler_rest_minutes=20))
    assert ctrl.boiler_demand is True
    ctrl._boiler_on_since -= 61 * 60
    ctrl._evaluate()
    await hass.async_block_till_done()
    assert ctrl.boiler_demand is False
    assert ctrl.boiler_reason == "run limit reached"
    assert any(c.domain == "persistent_notification" for c in calls)


# ---------------------------------------------------------------- hot water

def hw_cfg(**hw):
    return base_config(hot_water={"enabled": True, **hw})


def hw_calls(calls):
    return [c.service for c in calls if c.domain == "switch" and c.data.get("entity_id") == HW_RELAY]


async def test_hot_water_disabled_does_nothing(hass, calls):
    set_world(hass, millie=22.0, tank=30, schedule="on")
    await setup(hass, base_config())
    assert hw_calls(calls) == []


async def test_hot_water_schedule_heats_and_stops(hass, calls):
    set_world(hass, millie=22.0, tank=50, schedule="on")
    ctrl = await setup(hass, hw_cfg(min_switch_seconds=0))
    assert ctrl.hot_water.demand is True
    assert hw_calls(calls) == ["turn_on"]
    hass.states.async_set("sensor.hot_water_tank_temperature", "58.2")
    await hass.async_block_till_done()
    assert ctrl.hot_water.demand is False
    assert hw_calls(calls)[-1] == "turn_off"


async def test_hot_water_topup_stops(hass, calls):
    """The old 49 C automation switched the relay on and never off."""
    set_world(hass, millie=22.0, tank=48, schedule="off")
    ctrl = await setup(hass, hw_cfg(min_switch_seconds=0))
    assert ctrl.hot_water.demand is True
    assert ctrl.hot_water.reason == "top-up"
    hass.states.async_set("sensor.hot_water_tank_temperature", "55.1")
    await hass.async_block_till_done()
    assert ctrl.hot_water.demand is False
    assert hw_calls(calls)[-1] == "turn_off"


async def test_hot_water_no_topup_above_threshold(hass, calls):
    set_world(hass, millie=22.0, tank=52, schedule="off")
    ctrl = await setup(hass, hw_cfg())
    assert ctrl.hot_water.demand is False


async def test_hot_water_boost_and_hard_max(hass, calls):
    set_world(hass, millie=22.0, tank=56, schedule="off")
    ctrl = await setup(hass, hw_cfg(min_switch_seconds=0))
    await hass.services.async_call(DOMAIN, "hot_water_boost", {}, blocking=True)
    await hass.async_block_till_done()
    assert ctrl.hot_water.demand is True and ctrl.hot_water.reason == "boost"
    hass.states.async_set("sensor.hot_water_tank_temperature", "62.5")
    await hass.async_block_till_done()
    assert ctrl.hot_water.demand is False
    assert ctrl.hot_water.reason.startswith("tank at hard maximum")


async def test_hot_water_button_starts_boost(hass, calls):
    set_world(hass, millie=22.0, tank=56, schedule="off")
    hass.states.async_set("input_button.hot_water_boost", "2026-10-28T06:00:00+00:00")
    ctrl = await setup(hass, hw_cfg(min_switch_seconds=0))
    hass.states.async_set("input_button.hot_water_boost", "2026-10-28T07:00:00+00:00")
    await hass.async_block_till_done()
    assert ctrl.hot_water.reason == "boost"


async def test_hot_water_sensor_lost_turns_off(hass, calls):
    set_world(hass, millie=22.0, tank=50, schedule="on")
    ctrl = await setup(hass, hw_cfg(min_switch_seconds=0))
    assert ctrl.hot_water.demand is True
    hass.states.async_set("sensor.hot_water_tank_temperature", "unavailable")
    await hass.async_block_till_done()
    assert ctrl.hot_water.demand is False
    assert hw_calls(calls)[-1] == "turn_off"


async def test_update_room_keeps_other_rooms(hass, calls):
    set_world(hass, millie=22.0)
    cfg = base_config()
    cfg["rooms"]["office"] = room("Office", "sensor.office_temp", "climate.office_radiator")
    ctrl = await setup(hass, cfg)
    await hass.services.async_call(
        DOMAIN, "update_room", {"room_id": "millie", "settings": {"thermostat_offset": 1.5}}, blocking=True
    )
    await hass.async_block_till_done()
    rooms = ctrl.config["rooms"]
    assert set(rooms) == {"millie", "office"}
    assert rooms["millie"]["thermostat_offset"] == 1.5
    assert rooms["millie"]["sensor"] == "sensor.millie_temp"


async def test_update_room_rejects_unknown_room(hass, calls):
    set_world(hass, millie=22.0)
    ctrl = await setup(hass, base_config())
    with pytest.raises(Exception):
        await hass.services.async_call(
            DOMAIN, "update_room", {"room_id": "workshop", "settings": {"enabled": True}}, blocking=True
        )
    assert list(ctrl.config["rooms"]) == ["millie"]
