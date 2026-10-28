# Home Heat Control

Room heating, boiler and hot water control for one house, built from
[Smartdome Heat Control](https://github.com/19DMO89/smartdome_heat_control) v3.9.0
(MIT licence, see `LICENSE.smartdome`).

It only changes when this repository changes. Smartdome updates never reach it.

## What is different from Smartdome

- **No room discovery.** Rooms are only ever added on purpose. The Reload rooms
  button and the discovery steps are gone, and the reload service just
  re-applies the saved settings.
- **Watch only mode.** `switch.home_heat_watch_only` (on by default). It works out
  every decision but sends nothing. Each command it would have sent is listed on
  `sensor.home_heat_decisions`.
- **Boiler safety rules.**
  - A room can only call the boiler if it has a radiator valve and its
    thermometer reads between 5 and 35 °C with no fault.
  - Room targets are capped at `max_room_target` (default 24 °C).
  - After `boiler_max_run_minutes` of non-stop demand (default 240), the boiler
    rests for `boiler_rest_minutes` (default 20) and an alert is raised.
  - Minimum boiler on and off times (default 3 minutes each).
  - No boiler demand while cooling is on.
  - The relay is checked against the decision every minute, so a restart or a
    manual change cannot leave it on.
- **Hot water control.** One controller owns the hot water relay: schedule, boost,
  top-up and a hard maximum (see below).
- **English only**, and entities all start with `home_heat_`.
- Cooling and air conditioning support is kept for when the units are fitted.

## Entities

| Entity | What it shows |
| --- | --- |
| `binary_sensor.home_heat_boiler_demand` | Whether the boiler should be on, with the rooms calling and any rooms blocked |
| `binary_sensor.home_heat_hot_water_demand` | Whether the hot water should be on, why, tank temperature, boost time left |
| `sensor.home_heat_decisions` | Latest decision, with the last 40 as an attribute |
| `switch.home_heat_watch_only` | On: decide but do not act |
| `climate.home_heat_<room>_heating` | One heating card per room |

## Hot water

Set under `hot_water` in the configuration (`update_config` service). Defaults
match the automations it replaces:

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | false | Hot water is left alone until this is true |
| `relay` | `switch.central_heating_relay_l2` | Hot water relay |
| `sensor` | `sensor.hot_water_tank_temperature` | Tank thermometer |
| `schedule` | `schedule.hot_water_schedule` | Heating windows |
| `boost_button` | `input_button.hot_water_boost` | Pressing it starts a boost |
| `target` / `restart_below` | 58 / 57 | Inside a schedule window |
| `topup_below` / `topup_until` | 49 / 55 | Outside a schedule window |
| `boost_target` / `boost_minutes` | 60 / 60 | Boost |
| `max_temp` | 62 | Never heats at or above this |
| `max_run_minutes` / `rest_minutes` | 120 / 20 | Run limit |

Services: `home_heat_control.hot_water_boost` (optional `minutes`) and
`home_heat_control.hot_water_cancel_boost`.

The relay is switched off if the tank thermometer stops reporting.

## Install

HACS, Custom repositories, add this repository as an Integration, install, restart
Home Assistant, then Settings, Devices and services, Add integration, Home Heat Control.
It starts with no rooms and in watch-only mode.

## Tests

```
pip install pytest-homeassistant-custom-component
pytest
```
