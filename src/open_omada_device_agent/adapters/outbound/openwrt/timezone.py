"""Site time zone from the controller -> OpenWrt system time zone.

The controller sends ``timeSetting`` and ``dst`` in SYSTEM_NEGOTIATION at every
connect. ``timeSetting.timeZone`` is the site's standard UTC offset in minutes
plus 720; the zone name is not sent. The agent writes the offset as a POSIX
TZ string to ``system.@system[0].timezone`` (for example ``<+0530>-5:30``;
POSIX counts west of UTC as positive) and removes ``zonename``, which would
otherwise name a different zone in LuCI. ``/etc/init.d/system reload`` applies
it. NTP and the clock itself are left alone.

Daylight saving: ``dst`` carries ``enable``, ``offset`` (seconds) and the next
``start``/``end`` as Unix times (Observed: a London site sent the last Sundays
of March and October 2027, 01:00 UTC). They are turned into POSIX
``Mm.w.d/time`` rules ("week 5" = last), so the zone keeps working after those
dates even if the controller does not resend them.
"""
from __future__ import annotations

import calendar
import logging
from datetime import datetime, timedelta, timezone

from ....application.commands import ApplyDeviceConfigurationCommand
from ....application.contracts import PlatformCapabilities
from ....contexts.device.domain import DaylightSaving
from .uci import CommandRunner, ReconciliationResult, SubprocessRunner

log = logging.getLogger("open_omada.time")

SECTION = "system.@system[0]"


def posix_time_zone(utc_offset_minutes: int, dst: DaylightSaving | None = None) -> str:
    """POSIX TZ: +330 -> ``<+0530>-5:30``, 0 -> ``UTC0``, London with DST ->
    ``<+00>0<+01>-1,M3.5.0/1,M10.5.0``."""
    rules = _dst_rules(utc_offset_minutes, dst) if dst is not None else None
    if rules is None:
        if utc_offset_minutes == 0:
            return "UTC0"
        return _name(utc_offset_minutes) + _posix_offset(utc_offset_minutes)
    summer = utc_offset_minutes + dst.offset // 60
    return (
        f"{_name(utc_offset_minutes)}{_posix_offset(utc_offset_minutes)}"
        f"{_name(summer)}{_posix_offset(summer)},{rules[0]},{rules[1]}"
    )


def _name(offset: int) -> str:
    sign = "+" if offset >= 0 else "-"
    hours, minutes = divmod(abs(offset), 60)
    return f"<{sign}{hours:02d}{minutes:02d}>" if minutes else f"<{sign}{hours:02d}>"


def _posix_offset(offset: int) -> str:
    # POSIX counts hours west of UTC: UTC+05:30 is "-5:30".
    sign = "-" if offset > 0 else ""
    hours, minutes = divmod(abs(offset), 60)
    return f"{sign}{hours}:{minutes:02d}" if minutes else f"{sign}{hours}"


def _dst_rules(standard: int, dst: DaylightSaving) -> tuple[str, str] | None:
    """``Mm.w.d/time`` for the start and end, or None if ``dst`` is unusable."""
    if not dst.enabled or dst.start <= 0 or dst.end <= 0 or dst.offset <= 0 or dst.offset % 900:
        return None
    # A transition is given in the local time in force just before it.
    start = datetime.fromtimestamp(dst.start, timezone.utc) + timedelta(minutes=standard)
    end = datetime.fromtimestamp(dst.end, timezone.utc) + timedelta(minutes=standard, seconds=dst.offset)
    return _rule(start), _rule(end)


def _rule(moment: datetime) -> str:
    last_day = calendar.monthrange(moment.year, moment.month)[1]
    week = 5 if moment.day + 7 > last_day else (moment.day - 1) // 7 + 1
    weekday = (moment.weekday() + 1) % 7  # POSIX: 0 = Sunday
    rule = f"M{moment.month}.{week}.{weekday}"
    seconds = moment.hour * 3600 + moment.minute * 60
    if seconds != 7200:  # POSIX default is 02:00
        hours, rest = divmod(seconds, 3600)
        rule += f"/{hours}" if not rest else f"/{hours}:{rest // 60:02d}"
    return rule


class OpenWrtTimeZoneAdapter:
    """Command port for the site time zone (``timeSetting`` / ``dst``)."""

    def __init__(
        self,
        *,
        runner: CommandRunner | None = None,
        init_script: str = "/etc/init.d/system",
    ) -> None:
        self._runner = runner or SubprocessRunner()
        self._init_script = init_script

    def reconcile(
        self,
        update: ApplyDeviceConfigurationCommand,
        capabilities: PlatformCapabilities,
    ) -> ReconciliationResult:
        setting = update.time_zone
        if setting is None:
            return ReconciliationResult(applied=True, changed=False)
        if not capabilities.supports_time_zone:
            log.info("Site time zone received; OMADA_CAP_TIMEZONE is off, not applied")
            return ReconciliationResult(applied=True, changed=False)
        wanted = posix_time_zone(setting.utc_offset_minutes, setting.daylight_saving)
        if setting.daylight_saving.enabled and "," not in wanted:
            log.warning(
                "Site daylight saving is on but unusable (%r); only the standard offset is applied",
                setting.daylight_saving,
            )
        current = self._get("timezone")
        zonename = self._get("zonename")
        if current == wanted and zonename is None:
            return ReconciliationResult(applied=True, changed=False)

        lines = [f"set {SECTION}.timezone='{wanted}'"]
        if zonename is not None:
            lines.append(f"delete {SECTION}.zonename")
        result = self._runner.run(
            ["uci", "-q", "batch"], input_text="\n".join([*lines, "commit system"]) + "\n"
        )
        if result.returncode != 0:
            self._runner.run(["uci", "revert", "system"])
            return ReconciliationResult(applied=False, changed=False, error="uci batch for system failed")
        self._runner.run([self._init_script, "reload"])
        log.info("Time zone set to %s (was %s, zonename %s)", wanted, current, zonename)
        return ReconciliationResult(applied=True, changed=True, command_count=len(lines))

    def _get(self, option: str) -> str | None:
        result = self._runner.run(["uci", "-q", "get", f"{SECTION}.{option}"])
        return result.stdout.strip() if result.returncode == 0 else None
