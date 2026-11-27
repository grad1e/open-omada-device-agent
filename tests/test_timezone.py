from types import SimpleNamespace

import pytest

from open_omada_device_agent import adoption
from open_omada_device_agent.adapters.inbound.ecsp.mappers.configuration import (
    parse_config_body,
    parse_time_zone,
)
from open_omada_device_agent.adapters.outbound.openwrt.capabilities import (
    detect_platform_capabilities,
)
from open_omada_device_agent.adapters.outbound.openwrt.timezone import (
    OpenWrtTimeZoneAdapter,
    posix_time_zone,
)
from open_omada_device_agent.adapters.outbound.openwrt.uci import CommandResult
from open_omada_device_agent.application.configuration import ApplyConfigurationResult
from open_omada_device_agent.application.contracts import PlatformCapabilities


def _body(code=1050, dst=None):
    body = {"timeSetting": {"timeZone": code, "date": "2026/01/02", "time": "03:04:05", "ntpEnable": False}}
    if dst is not None:
        body["dst"] = dst
    return body


class _Runner:
    def __init__(self, timezone="GMT0", zonename="UTC", batch_rc=0):
        self.options = {"timezone": timezone, "zonename": zonename}
        self.batch_rc = batch_rc
        self.calls = []
        self.batch = None

    def run(self, args, *, input_text=None):
        self.calls.append(list(args))
        if args[:3] == ["uci", "-q", "get"]:
            value = self.options.get(args[3].rsplit(".", 1)[1])
            return CommandResult(0, value + "\n") if value is not None else CommandResult(1)
        if args[:3] == ["uci", "-q", "batch"]:
            self.batch = input_text
            return CommandResult(self.batch_rc)
        return CommandResult(0)


def _caps(**overrides):
    values = {"platform": "openwrt", "supports_time_zone": True}
    values.update(overrides)
    return PlatformCapabilities(**values)


@pytest.mark.parametrize(
    "offset, expected",
    [
        (330, "<+0530>-5:30"),
        (0, "UTC0"),
        (-300, "<-05>5"),
        (345, "<+0545>-5:45"),
        (-570, "<-0930>9:30"),
        (840, "<+14>-14"),
    ],
)
def test_posix_time_zone(offset, expected):
    assert posix_time_zone(offset) == expected


# Next DST period as a London site sent it (Controller 6.3.0.111): last Sundays
# of March and October 2027, 01:00 UTC, offset in seconds.
LONDON_DST = {"enable": 1, "start": 1806195600, "end": 1824944400, "offset": 3600}


def test_daylight_saving_becomes_yearly_posix_rules():
    from open_omada_device_agent.contexts.device.domain import DaylightSaving

    london = parse_time_zone(_body(720, LONDON_DST))
    assert posix_time_zone(0, london.daylight_saving) == "<+00>0<+01>-1,M3.5.0/1,M10.5.0"
    # Sydney 2027: starts first Sunday of October 02:00, ends first Sunday of April 03:00.
    sydney = DaylightSaving(True, 1822492800, 1806768000, 3600)
    assert posix_time_zone(600, sydney) == "<+10>-10<+11>-11,M10.1.0,M4.1.0/3"
    # Off, or unusable values: standard offset only.
    assert posix_time_zone(330, DaylightSaving(False, 1, 2, 3600)) == "<+0530>-5:30"
    assert posix_time_zone(0, DaylightSaving(True, 0, 0, 3600)) == "UTC0"
    assert posix_time_zone(0, DaylightSaving(True, 1806195600, 1824944400, 100)) == "UTC0"


def test_adapter_writes_daylight_saving():
    runner = _Runner()
    update = parse_config_body(_body(720, LONDON_DST))
    OpenWrtTimeZoneAdapter(runner=runner).reconcile(update, _caps())
    assert "timezone='<+00>0<+01>-1,M3.5.0/1,M10.5.0'" in runner.batch


def test_time_setting_is_offset_plus_720():
    setting = parse_time_zone(_body(1050, {"enable": 0, "start": 0, "end": 0, "offset": 0}))
    assert setting.utc_offset_minutes == 330
    assert not setting.daylight_saving.enabled
    assert parse_time_zone(_body(720)).utc_offset_minutes == 0
    assert parse_time_zone(_body(420)).utc_offset_minutes == -300
    # A malformed dst block keeps the standard offset.
    assert parse_time_zone(_body(1050, {"enable": 1, "start": "x"})).utc_offset_minutes == 330


@pytest.mark.parametrize("code", [None, "x", -1, 720 + 15 * 60, 1051])
def test_unusable_time_zone_is_ignored(code):
    assert parse_time_zone(_body(code)) is None
    assert parse_time_zone({}) is None


def test_set_keys_are_known():
    update = parse_config_body(_body(1050, {"enable": 0}))
    assert update.unhandled_keys == ()
    assert update.time_zone.utc_offset_minutes == 330


def test_adapter_writes_the_offset_and_drops_zonename():
    runner = _Runner()
    update = parse_config_body(_body(1050))
    result = OpenWrtTimeZoneAdapter(runner=runner).reconcile(update, _caps())

    assert result.applied and result.changed
    assert runner.batch == (
        "set system.@system[0].timezone='<+0530>-5:30'\n"
        "delete system.@system[0].zonename\n"
        "commit system\n"
    )
    assert ["/etc/init.d/system", "reload"] in runner.calls


def test_adapter_leaves_a_matching_zone_alone():
    runner = _Runner(timezone="<+0530>-5:30", zonename=None)
    result = OpenWrtTimeZoneAdapter(runner=runner).reconcile(parse_config_body(_body(1050)), _caps())

    assert result.applied and not result.changed
    assert runner.batch is None


def test_adapter_reverts_a_failed_batch():
    runner = _Runner(batch_rc=1)
    result = OpenWrtTimeZoneAdapter(runner=runner).reconcile(parse_config_body(_body(1050)), _caps())

    assert not result.applied
    assert ["uci", "revert", "system"] in runner.calls


def test_switched_off_or_absent_does_nothing():
    runner = _Runner()
    adapter = OpenWrtTimeZoneAdapter(runner=runner)
    assert not adapter.reconcile(parse_config_body(_body(1050)), _caps(supports_time_zone=False)).changed
    assert not adapter.reconcile(parse_config_body({}), _caps()).changed
    assert runner.calls == []


def test_capability_defaults_on_with_uci_and_can_be_switched_off():
    def which(name):
        return f"/usr/bin/{name}" if name in {"uci", "ubus"} else None

    assert detect_platform_capabilities(env={}, command_exists=which).supports_time_zone
    off = detect_platform_capabilities(env={"OMADA_CAP_TIMEZONE": "false"}, command_exists=which)
    assert not off.supports_time_zone


def test_negotiation_applies_the_time_zone_and_never_raises(caplog):
    seen = []

    def execute(update):
        seen.append(update.time_zone)
        return ApplyConfigurationResult(False, error="boom")

    services = SimpleNamespace(configuration=SimpleNamespace(execute=execute))
    adoption._apply_site_time_zone(_body(1050), services=services)
    assert seen[0].utc_offset_minutes == 330
    assert "boom" in caplog.text

    def crash(update):
        raise RuntimeError("uci missing")

    services.configuration.execute = crash
    adoption._apply_site_time_zone(_body(1050), services=services)  # logged, not raised
    adoption._apply_site_time_zone({}, services=services)  # nothing to apply


def test_out_of_range_dst_is_ignored_not_raised():
    from open_omada_device_agent.contexts.device.domain import DaylightSaving

    huge = {"enable": 1, "start": 10**20, "end": 10**20, "offset": 3600}
    setting = parse_time_zone(_body(720, huge))
    assert setting.daylight_saving == DaylightSaving()
    # A live SET with it applies the standard offset and does not crash.
    runner = _Runner()
    result = OpenWrtTimeZoneAdapter(runner=runner).reconcile(parse_config_body(_body(720, huge)), _caps())
    assert result.applied and "timezone='UTC0'" in runner.batch
    # Even if such values reach the rules directly, they are dropped.
    assert posix_time_zone(0, DaylightSaving(True, 10**20, 10**20, 3600)) == "UTC0"
    assert parse_time_zone(_body(720, {"enable": 1, "start": -5, "end": 1, "offset": 3600})).daylight_saving \
        == DaylightSaving()


def test_southern_and_half_hour_zones_parse_end_to_end():
    # Sydney 2027/28: DST starts 2027-10-03 02:00 AEST, ends 2028-04-02 03:00 AEDT.
    sydney = {"enable": 1, "start": 1822492800, "end": 1838217600, "offset": 3600}
    setting = parse_time_zone(_body(720 + 600, sydney))
    assert posix_time_zone(600, setting.daylight_saving) == "<+10>-10<+11>-11,M10.1.0,M4.1.0/3"
    # Adelaide (UTC+9:30): same Sunday rules, half-hour names.
    adelaide = {"enable": 1, "start": 1822494600, "end": 1838219400, "offset": 3600}
    setting = parse_time_zone(_body(720 + 570, adelaide))
    assert posix_time_zone(570, setting.daylight_saving) == "<+0930>-9:30<+1030>-10:30,M10.1.0,M4.1.0/3"


def test_missing_dst_keeps_the_rules_on_the_device():
    london = "<+00>0<+01>-1,M3.5.0/1,M10.5.0"
    setting = parse_time_zone(_body(720))
    assert setting.daylight_saving is None

    runner = _Runner(timezone=london, zonename=None)
    result = OpenWrtTimeZoneAdapter(runner=runner).reconcile(parse_config_body(_body(720)), _caps())
    assert result.applied and not result.changed and runner.batch is None

    # A different standard offset still replaces the zone.
    runner = _Runner(timezone=london, zonename=None)
    OpenWrtTimeZoneAdapter(runner=runner).reconcile(parse_config_body(_body(1050)), _caps())
    assert "timezone='<+0530>-5:30'" in runner.batch
