"""Immersion heater control for Home Heat Control.

Replaces three automations (on at 11:59 if the tank is below 55 C, off at
13:00, off at 59 C) with one controller that owns the immersion relay.

Rules, in order of priority:
1. Immersion disabled in config: the relay is not touched at all.
2. No tank reading: relay off.
3. Tank at or above stop_at: relay off.
4. Run limit reached: relay off for the rest period, with an alert.
5. Boost active: heat until stop_at or the boost time runs out.
6. Inside the daily window: start if the tank is below start_below, keep
   heating until stop_at or the window ends.
7. Solar surplus (off by default, needs export and import sensors): start
   when export has been at least solar_min_export_w for solar_on_minutes,
   the battery is at least solar_battery_min (if a battery sensor is set) and
   the tank is below solar_start_below. Stop when grid import goes above
   solar_max_import_w for solar_off_minutes.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import time as dt_time
from typing import TYPE_CHECKING, Any

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_change,
)
from homeassistant.util import dt as dt_util

from .const import SIGNAL_DECISIONS_UPDATED

if TYPE_CHECKING:
    from .controller import SmartHeatingController

_LOGGER = logging.getLogger(__name__)

CONF_IMMERSION = "immersion"

# Defaults match the automations in use on 1 Nov 2026.
IMMERSION_DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "switch": "switch.immersion_heater",
    "power_sensor": "sensor.immersion_heater_power",
    "tank_sensor": "sensor.hot_water_tank_temperature",
    "window_start": "11:59",
    "window_end": "13:00",
    "start_below": 55.0,
    "stop_at": 59.0,
    "boost_minutes": 60,
    "max_run_minutes": 120,
    "rest_minutes": 30,
    "min_switch_seconds": 120,
    # Solar surplus, off until the inverter is in Home Assistant.
    "solar_enabled": False,
    "solar_export_sensor": "",
    "solar_import_sensor": "",
    "solar_battery_sensor": "",
    "solar_battery_min": 95.0,
    "solar_min_export_w": 3000.0,
    "solar_max_import_w": 200.0,
    "solar_start_below": 50.0,
    "solar_on_minutes": 5,
    "solar_off_minutes": 2,
}


def _parse_time(value: Any, fallback: str) -> dt_time:
    for candidate in (value, fallback):
        try:
            parts = str(candidate).split(":")
            return dt_time(int(parts[0]), int(parts[1]))
        except (TypeError, ValueError, IndexError):
            continue
    return dt_time(0, 0)


class ImmersionController:
    """Owns the immersion heater relay."""

    def __init__(self, hass: HomeAssistant, main: SmartHeatingController) -> None:
        self.hass = hass
        self.main = main
        self._unsub: list[Callable[[], None]] = []
        self.demand: bool = False
        self.reason: str = "not started"
        self.boost_until: float = 0.0
        self._heating: bool = False
        self._window_started: bool = False
        self._solar_active: bool = False
        self._export_ok_since: float | None = None
        self._import_high_since: float | None = None
        self._on_since: float | None = None
        self._rest_until: float = 0.0
        self._changed_at: float = 0.0
        self._last_sent_at: float = 0.0
        self._last_sent_cmd: bool | None = None

    # ------------------------------------------------------------------
    # config
    # ------------------------------------------------------------------

    @property
    def cfg(self) -> dict[str, Any]:
        raw = self.main.config.get(CONF_IMMERSION) or {}
        merged = dict(IMMERSION_DEFAULTS)
        if isinstance(raw, dict):
            merged.update({k: v for k, v in raw.items() if v is not None})
        return merged

    def _num(self, key: str) -> float:
        try:
            return float(self.cfg[key])
        except (TypeError, ValueError):
            return float(IMMERSION_DEFAULTS[key])

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("enabled"))

    def _read(self, key: str) -> float | None:
        entity_id = self.cfg.get(key)
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return None
        try:
            return float(state.state)
        except (TypeError, ValueError):
            return None

    def tank_temp(self) -> float | None:
        return self._read("tank_sensor")

    def power(self) -> float | None:
        return self._read("power_sensor")

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    async def async_start(self) -> None:
        self.async_stop_listeners()
        watch = [
            e for e in (
                self.cfg.get("tank_sensor"),
                self.cfg.get("solar_export_sensor"),
                self.cfg.get("solar_import_sensor"),
                self.cfg.get("power_sensor"),
            )
            if isinstance(e, str) and e
        ]
        if watch:
            self._unsub.append(
                async_track_state_change_event(self.hass, watch, self._on_state_change)
            )
        self._unsub.append(async_track_time_change(self.hass, self._on_tick, second=15))
        self.evaluate()

    def async_stop_listeners(self) -> None:
        for unsub in self._unsub:
            try:
                unsub()
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Error removing immersion listener")
        self._unsub.clear()

    def reconfigure(self) -> None:
        self.hass.async_create_task(self.async_start())

    @callback
    def _on_state_change(self, event: Event) -> None:
        if event.data.get("entity_id") == self.cfg.get("power_sensor"):
            # Power only changes what the panel shows.
            async_dispatcher_send(self.hass, SIGNAL_DECISIONS_UPDATED)
            return
        self.evaluate()

    @callback
    def _on_tick(self, now) -> None:
        self.evaluate()

    # ------------------------------------------------------------------
    # boost
    # ------------------------------------------------------------------

    def start_boost(self, minutes: float | None = None) -> None:
        mins = self._num("boost_minutes") if minutes is None else float(minutes)
        self.boost_until = dt_util.now().timestamp() + mins * 60
        self.main._record("immersion", f"Immersion boost started for {int(mins)} minutes")
        self.evaluate()

    def cancel_boost(self) -> None:
        if self.boost_until:
            self.boost_until = 0.0
            self.main._record("immersion", "Immersion boost cancelled")
        self.evaluate()

    def boost_minutes_left(self) -> int:
        if not self.boost_until:
            return 0
        return max(0, int((self.boost_until - dt_util.now().timestamp() + 59) // 60))

    # ------------------------------------------------------------------
    # decision
    # ------------------------------------------------------------------

    def _in_window(self) -> bool:
        start = _parse_time(self.cfg.get("window_start"), IMMERSION_DEFAULTS["window_start"])
        end = _parse_time(self.cfg.get("window_end"), IMMERSION_DEFAULTS["window_end"])
        now = dt_util.now().time()
        if start <= end:
            return start <= now < end
        return now >= start or now < end  # window crosses midnight

    def _solar_decision(self, now_ts: float, temp: float) -> bool:
        cfg = self.cfg
        if not cfg.get("solar_enabled") or not cfg.get("solar_export_sensor"):
            self._solar_active = False
            self._export_ok_since = self._import_high_since = None
            return False
        export = self._read("solar_export_sensor") or 0.0
        imported = self._read("solar_import_sensor") or 0.0
        if self._solar_active:
            if temp >= self._num("stop_at"):
                self._solar_active = False
            elif imported > self._num("solar_max_import_w"):
                self._import_high_since = self._import_high_since or now_ts
                if now_ts - self._import_high_since >= self._num("solar_off_minutes") * 60:
                    self._solar_active = False
            else:
                self._import_high_since = None
        else:
            self._import_high_since = None
            battery_ok = True
            if cfg.get("solar_battery_sensor"):
                battery = self._read("solar_battery_sensor")
                battery_ok = battery is not None and battery >= self._num("solar_battery_min")
            if (
                export >= self._num("solar_min_export_w")
                and battery_ok
                and temp < self._num("solar_start_below")
            ):
                self._export_ok_since = self._export_ok_since or now_ts
                if now_ts - self._export_ok_since >= self._num("solar_on_minutes") * 60:
                    self._solar_active = True
                    self._export_ok_since = None
            else:
                self._export_ok_since = None
        return self._solar_active

    def _decide(self, now_ts: float) -> tuple[bool, str]:
        temp = self.tank_temp()
        if temp is None:
            return False, "no tank reading"
        stop_at = self._num("stop_at")
        if temp >= stop_at:
            self._window_started = False
            self.boost_until = 0.0
            self._solar_active = False
            return False, f"tank at {stop_at:g}"
        if now_ts < self._rest_until:
            return False, "resting after run limit"

        if self.boost_until and now_ts < self.boost_until:
            return True, "boost"
        if self.boost_until:
            self.boost_until = 0.0
            self.main._record("immersion", "Immersion boost finished")

        if self._in_window():
            if self._window_started or temp < self._num("start_below"):
                self._window_started = True
                return True, "daily window"
            return False, "daily window, tank warm enough"
        self._window_started = False

        if self._solar_decision(now_ts, temp):
            return True, "solar surplus"
        return False, "idle"

    def evaluate(self) -> None:
        if not self.enabled:
            if self.reason != "disabled":
                self.demand, self.reason = False, "disabled"
                async_dispatcher_send(self.hass, SIGNAL_DECISIONS_UPDATED)
            return

        now_ts = dt_util.now().timestamp()
        want, reason = self._decide(now_ts)

        max_run = self._num("max_run_minutes")
        if want and max_run > 0 and self._on_since is not None:
            if now_ts - self._on_since >= max_run * 60:
                self._rest_until = now_ts + self._num("rest_minutes") * 60
                self.boost_until = 0.0
                self._window_started = False
                self._solar_active = False
                want, reason = False, "run limit reached"
                self.main._notify(
                    "Immersion run limit",
                    f"The immersion heater has been on for {int(max_run)} minutes. "
                    f"It is resting for {int(self._num('rest_minutes'))} minutes.",
                )

        safety = reason.startswith(("no tank", "tank at", "run limit", "resting"))
        if (
            want != self._heating
            and not safety
            and now_ts - self._changed_at < self._num("min_switch_seconds")
        ):
            want, reason = self._heating, reason + " (holding)"

        if want != self._heating:
            self._heating = want
            self._changed_at = now_ts
            self.main._record(
                "immersion", f"Immersion {'ON' if want else 'OFF'} ({reason})",
                tank=self.tank_temp(),
            )
        if want:
            self._on_since = self._on_since or now_ts
        else:
            self._on_since = None

        changed = want != self.demand or reason != self.reason
        self.demand, self.reason = want, reason
        self._drive_relay(want, now_ts)
        if changed:
            async_dispatcher_send(self.hass, SIGNAL_DECISIONS_UPDATED)

    def _drive_relay(self, turn_on: bool, now_ts: float) -> None:
        relay = self.cfg.get("switch")
        if not relay:
            return
        if self.main.is_watch_only():
            last = self.main._desired_targets.get(relay)
            if last is None or (last >= 0.5) != turn_on:
                self.main._desired_targets[relay] = 1.0 if turn_on else 0.0
                self.main._call(
                    relay.split(".", 1)[0], "turn_on" if turn_on else "turn_off",
                    {"entity_id": relay},
                )
            return
        state = self.hass.states.get(relay)
        if state is not None and state.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            if (state.state == "on") == turn_on:
                return
        if self._last_sent_cmd == turn_on and now_ts - self._last_sent_at < 30:
            return
        self._last_sent_at = now_ts
        self._last_sent_cmd = turn_on
        self.main._call(
            relay.split(".", 1)[0], "turn_on" if turn_on else "turn_off",
            {"entity_id": relay},
        )
