"""WLAN schedule: turn an SSID off (or on) during time ranges.

Observed (lab, Controller 6.3.0.111), from Profiles > Time Range and the
SSID editor's WLAN Schedule:

- ``schedulerGlobal`` {``enable``, ``mode``}: the site switch. Only ``mode``
  0 has been seen; its meaning is Unknown.
- ``schedulerList`` rules {``operation``, ``profileId``, ``ruleId``,
  ``type``, ``day``, ``startH``, ``startM``, ``endH``, ``endM``}: a rule
  without ``ruleId`` is the time range (profile) itself, the others are its
  periods. ``operation`` 1 = add, 2 = delete (Observed); 3 = update is
  Inferred from the MAC filter: an edit was seen as delete plus add.
- ``type`` "Everyday", "Weekday" or "Weekend" with ``day`` 0, or
  "Select Days" with ``day`` 1 = Monday ... 7 = Sunday; the UI's Customized
  mode sends one "Select Days" period per ticked day (Observed: 5 = Friday,
  6 = Saturday). A delete may carry ``day`` -2 (Observed), so deletes are
  not checked. Weekday = Monday to Friday and Weekend = Saturday and Sunday
  are Inferred.
- ``schedulerAssoc`` rules {``ssidId``, ``band``, ``profileId``,
  ``action``}, with no ``operation``: each rule is the whole schedule of one
  SSID on one band, and a rule without ``profileId`` means no schedule.
  ``band`` 0 = 2.4 GHz, 1 = 5 GHz. ``action`` 0 = radio off during the
  range, 1 = radio on during the range (only).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping

SCHEDULE_ADD = 1
SCHEDULE_DELETE = 2
SCHEDULE_UPDATE = 3
SCHEDULE_OPERATIONS = (SCHEDULE_ADD, SCHEDULE_DELETE, SCHEDULE_UPDATE)

SCHEDULE_OFF_IN_RANGE = 0
SCHEDULE_ON_IN_RANGE = 1

# Weekdays as datetime.weekday(): 0 = Monday ... 6 = Sunday.
SCHEDULE_TYPES = {
    "Everyday": frozenset(range(7)),
    "Weekday": frozenset(range(5)),
    "Weekend": frozenset({5, 6}),
}
SCHEDULE_SELECT_DAYS = "Select Days"
MINUTES_PER_DAY = 24 * 60


@dataclass(frozen=True)
class SchedulePeriod:
    """One ``schedulerList`` period: days plus a start and end time."""
    type: str
    day: int
    start: int  # minutes after midnight
    end: int

    def days(self) -> frozenset[int]:
        if self.type == SCHEDULE_SELECT_DAYS:
            return frozenset({self.day - 1}) if 1 <= self.day <= 7 else frozenset()
        return SCHEDULE_TYPES.get(self.type, frozenset())

    def covers(self, moment: datetime) -> bool:
        minute = moment.hour * 60 + moment.minute
        today = moment.weekday()
        days = self.days()
        if self.start == self.end:
            # The whole day (Inferred: the UI has no 24-hour option otherwise).
            return today in days
        if self.start < self.end:
            return today in days and self.start <= minute < self.end
        # Overnight, e.g. 22:00-06:00: the morning part belongs to the
        # previous day's period (Inferred).
        return (today in days and minute >= self.start) or (
            (today - 1) % 7 in days and minute < self.end
        )


@dataclass(frozen=True)
class ScheduleListRule:
    operation: int
    profile_id: int
    rule_id: int | None = None
    period: SchedulePeriod | None = None


@dataclass(frozen=True)
class ScheduleAssocRule:
    ssid_id: int
    band: int
    profile_id: int | None
    action: int


@dataclass(frozen=True)
class ScheduleChange:
    enabled: bool | None = None
    mode: int | None = None
    list_rules: tuple[ScheduleListRule, ...] = ()
    assoc_rules: tuple[ScheduleAssocRule, ...] = ()


@dataclass(frozen=True)
class ScheduleState:
    """The controller's WLAN schedules as the agent last applied them."""
    enabled: bool = False
    # profile_id -> {rule_id: period}
    profiles: Mapping[int, Mapping[int, SchedulePeriod]] = field(default_factory=dict)
    # (ssid_id, band) -> (profile_id, action)
    assoc: Mapping[tuple[int, int], tuple[int, int]] = field(default_factory=dict)

    def apply(self, change: ScheduleChange, *, full_sync: bool) -> "ScheduleState":
        enabled = False if full_sync else self.enabled
        profiles = {} if full_sync else {key: dict(value) for key, value in self.profiles.items()}
        assoc = {} if full_sync else dict(self.assoc)
        if change.enabled is not None:
            enabled = change.enabled
        for rule in change.list_rules:
            if rule.rule_id is None:
                if rule.operation == SCHEDULE_DELETE:
                    profiles.pop(rule.profile_id, None)
                else:
                    profiles.setdefault(rule.profile_id, {})
                continue
            periods = profiles.setdefault(rule.profile_id, {})
            if rule.operation == SCHEDULE_DELETE:
                periods.pop(rule.rule_id, None)
            elif rule.period is not None:
                periods[rule.rule_id] = rule.period
        for rule in change.assoc_rules:
            key = (rule.ssid_id, rule.band)
            if rule.profile_id is None:
                assoc.pop(key, None)
            else:
                assoc[key] = (rule.profile_id, rule.action)
        return ScheduleState(enabled=enabled, profiles=profiles, assoc=assoc)

    def ssid_off(self, ssid_id: int | None, band: int, moment: datetime) -> bool:
        """True when the schedule wants this SSID off at ``moment``."""
        if not self.enabled or ssid_id is None or (ssid_id, band) not in self.assoc:
            return False
        profile_id, action = self.assoc[(ssid_id, band)]
        if profile_id not in self.profiles:
            # A deleted time range schedules nothing.
            return False
        in_range = any(period.covers(moment) for period in self.profiles[profile_id].values())
        return in_range if action == SCHEDULE_OFF_IN_RANGE else not in_range

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "profiles": {
                str(profile_id): {
                    str(rule_id): {"type": p.type, "day": p.day, "start": p.start, "end": p.end}
                    for rule_id, p in periods.items()
                }
                for profile_id, periods in self.profiles.items()
            },
            "assoc": [
                {"ssidId": ssid_id, "band": band, "profileId": profile_id, "action": action}
                for (ssid_id, band), (profile_id, action) in self.assoc.items()
            ],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ScheduleState":
        profiles = {
            int(profile_id): {
                int(rule_id): SchedulePeriod(str(p["type"]), int(p["day"]), int(p["start"]), int(p["end"]))
                for rule_id, p in periods.items()
            }
            for profile_id, periods in data.get("profiles", {}).items()
        }
        assoc = {
            (int(link["ssidId"]), int(link["band"])): (int(link["profileId"]), int(link["action"]))
            for link in data.get("assoc", [])
        }
        return cls(enabled=bool(data.get("enabled", False)), profiles=profiles, assoc=assoc)
