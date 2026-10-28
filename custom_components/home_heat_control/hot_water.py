"""Hot water control for Home Heat Control.

Replaces the old setup of a generic thermostat plus three automations
(follow schedule, boost button, top-up below 49 C) with one controller that
owns the hot water relay, so nothing else can leave it switched on.

Rules, in order of priority:
1. Hot water disabled in config: the relay is not touched at all.
2. No tank reading: relay off.
3. Tank at or above the hard maximum: relay off.
4. Run limit reached: relay off for the rest period, with an alert.
5. Boost active: heat until the boost target or the boost time runs out.
6. Schedule on: heat below restart_below, stop at target.
7. Schedule off: heat only if the tank falls below topup_below, and stop at
   topup_until.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
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

CONF_HOT_WATER = "hot_water"

# Defaults match the automations in use on 28 Oct 2026.
HOT_WATER_DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "relay": "switch.central_heating_relay_l2",
    "sensor": "sensor.hot_water_tank_temperature",
    "schedule": "schedule.hot_water_schedule",
    "boost_button": "input_button.hot_water_boost",
    "target": 58.0,
    "restart_below": 57.0,
    "boost_target": 60.0,
    "boost_minutes": 60,
    "topup_below": 49.0,
    "topup_until": 55.0,
    "max_temp": 62.0,
    "max_run_minutes": 120,
    "rest_minutes": 20,
    "min_switch_seconds": 120,
}


class HotWaterController:
    """Owns the hot water relay."""

    def __init__(self, hass: HomeAssistant, main: SmartHeatingController) -> None:
        self.hass = hass
        self.main = main
        self._unsub: list[Callable[[], None]] = []
        self.demand: bool = False
        self.reason: str = "not started"
        self.boost_until: float = 0.0
        self._heating: bool = False
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
        raw = self.main.config.get(CONF_HOT_WATER) or {}
        merged = dict(HOT_WATER_DEFAULTS)
        if isinstance(raw, dict):
            merged.update({k: v for k, v in raw.items() if v is not None and v != ""})
        return merged

    def _num(self, key: str) -> float:
        try:
            return float(self.cfg[key])
        except (TypeError, ValueError):
            return float(HOT_WATER_DEFAULTS[key])

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("enabled"))

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    async def async_start(self) -> None:
        self.async_stop_listeners()
        cfg = self.cfg
        watch = [
            e for e in (cfg.get("sensor"), cfg.get("schedule"), cfg.get("boost_button"))
            if isinstance(e, str) and e
        ]
        if watch:
            self._unsub.append(
                async_track_state_change_event(self.hass, watch, self._on_state_change)
            )
        self._unsub.append(
            async_track_time_change(self.hass, self._on_tick, second=30)
        )
        self.evaluate()

    def async_stop_listeners(self) -> None:
        for unsub in self._unsub:
            try:
                unsub()
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Error removing hot water listener")
        self._unsub.clear()

    def reconfigure(self) -> None:
        self.hass.async_create_task(self.async_start())

    @callback
    def _on_state_change(self, event: Event) -> None:
        entity_id = event.data.get("entity_id")
        if entity_id and entity_id == self.cfg.get("boost_button"):
            new = event.data.get("new_state")
            old = event.data.get("old_state")
            if (
                new is not None
                and old is not None
                and new.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN)
                and old.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN)
                and new.state != old.state
            ):
                self.start_boost()
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
        self.main._record("hot_water", f"Hot water boost started for {int(mins)} minutes")
        self.evaluate()

    def cancel_boost(self) -> None:
        if self.boost_until:
            self.boost_until = 0.0
            self.main._record("hot_water", "Hot water boost cancelled")
        self.evaluate()

    # ------------------------------------------------------------------
    # decision
    # ------------------------------------------------------------------

    def _tank_temp(self) -> float | None:
        entity_id = self.cfg.get("sensor")
        state = self.hass.states.get(entity_id) if entity_id else None
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return None
        try:
            return float(state.state)
        except (TypeError, ValueError):
            return None

    def _schedule_on(self) -> bool:
        entity_id = self.cfg.get("schedule")
        if not entity_id:
            return False
        state = self.hass.states.get(entity_id)
        return state is not None and state.state == "on"

    def _decide(self, now_ts: float) -> tuple[bool, str]:
        temp = self._tank_temp()
        if temp is None:
            return False, "no tank reading"
        if temp >= self._num("max_temp"):
            return False, f"tank at hard maximum ({temp:.1f})"
        if now_ts < self._rest_until:
            return False, "resting after run limit"

        if self.boost_until and now_ts < self.boost_until:
            if temp >= self._num("boost_target"):
                self.boost_until = 0.0
                self.main._record("hot_water", f"Hot water boost finished at {temp:.1f} C")
            else:
                return True, "boost"
        elif self.boost_until:
            self.boost_until = 0.0
            self.main._record("hot_water", "Hot water boost timed out")

        if self._schedule_on():
            stop, start = self._num("target"), self._num("restart_below")
            label = "schedule"
        else:
            stop, start = self._num("topup_until"), self._num("topup_below")
            label = "top-up"

        if self._heating:
            if temp >= stop:
                return False, f"{label} reached {stop:g}"
            return True, label
        if temp < start:
            return True, label
        return False, f"{label} satisfied"

    def evaluate(self) -> None:
        if not self.enabled:
            if self.reason != "disabled":
                self.demand, self.reason = False, "disabled"
                async_dispatcher_send(self.hass, SIGNAL_DECISIONS_UPDATED)
            return

        now_ts = dt_util.now().timestamp()
        want, reason = self._decide(now_ts)

        # Run limit.
        max_run = self._num("max_run_minutes")
        if want and max_run > 0 and self._on_since is not None:
            if now_ts - self._on_since >= max_run * 60:
                self._rest_until = now_ts + self._num("rest_minutes") * 60
                self.boost_until = 0.0
                want, reason = False, "run limit reached"
                self.main._notify(
                    "Hot water run limit",
                    f"Hot water has been heating for {int(max_run)} minutes. "
                    f"It is resting for {int(self._num('rest_minutes'))} minutes. "
                    f"Tank: {self._tank_temp()}",
                )

        # Avoid rapid relay switching, except for safety stops.
        safety = reason.startswith(("no tank", "tank at hard", "run limit", "resting"))
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
                "hot_water", f"Hot water {'ON' if want else 'OFF'} ({reason})",
                tank=self._tank_temp(),
            )
        if want:
            if self._on_since is None:
                self._on_since = now_ts
        else:
            self._on_since = None

        changed = want != self.demand or reason != self.reason
        self.demand, self.reason = want, reason
        self._drive_relay(want, now_ts)
        if changed:
            async_dispatcher_send(self.hass, SIGNAL_DECISIONS_UPDATED)

    def _drive_relay(self, turn_on: bool, now_ts: float) -> None:
        relay = self.cfg.get("relay")
        if not relay:
            return
        if self.main.is_watch_only():
            # Only record changes in our decision, never the relay's real state.
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
        # Re-send the same command at most every 30 s while the relay catches
        # up. A change of direction is always sent straight away.
        if self._last_sent_cmd == turn_on and now_ts - self._last_sent_at < 30:
            return
        self._last_sent_at = now_ts
        self._last_sent_cmd = turn_on
        self.main._call(
            relay.split(".", 1)[0], "turn_on" if turn_on else "turn_off",
            {"entity_id": relay},
        )
