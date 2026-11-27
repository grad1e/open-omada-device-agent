"""WLAN schedule (Profiles > Time Range) -> SSID ``disabled`` in UCI."""
import json
from datetime import datetime

from open_omada_device_agent.adapters.outbound.openwrt.uci import OpenWrtUciAdapter
from open_omada_device_agent.ap_config import parse_config_body
from open_omada_device_agent.contexts.wireless.schedule import (
    ScheduleChange, ScheduleListRule, SchedulePeriod, ScheduleState,
)
from open_omada_device_agent.domain import RadioBand
from open_omada_device_agent.openwrt import CommandResult, build_uci_batch
from open_omada_device_agent.platform_capabilities import PlatformCapabilities

SSID_ID = 1001
PROFILE_ID = 3003
# 2026-11-28 is a Saturday, 2026-11-30 a Monday.
SATURDAY_NOON = datetime(2026, 11, 28, 12, 0)
SATURDAY_NIGHT = datetime(2026, 11, 28, 20, 0)
MONDAY_NOON = datetime(2026, 11, 30, 12, 0)


def _caps():
    return PlatformCapabilities(
        platform="openwrt",
        radio_bands=(RadioBand.TWO_G, RadioBand.FIVE_G),
        max_ssids=4,
        supports_wlan_config=True,
        supports_wpa2_psk=True,
    )


def _range(type_="Weekend", day=0, start=(8, 0), end=(18, 0), op=1):
    return {
        "rule": [
            {"operation": op, "profileId": PROFILE_ID},
            {"operation": op, "ruleId": 1, "profileId": PROFILE_ID, "type": type_, "day": day,
             "startH": start[0], "startM": start[1], "endH": end[0], "endM": end[1]},
        ]
    }


def _assoc(action=0, profile=PROFILE_ID, bands=(0, 1)):
    rules = []
    for band in bands:
        rule = {"ssidId": SSID_ID, "band": band, "action": action}
        if profile is not None:
            rule["profileId"] = profile
        rules.append(rule)
    return {"rule": rules}


def _ssid_body():
    item = {"id": SSID_ID, "index": 1, "ssidName": "lab", "pskKey": "network-passphrase", "pskVer": 2}
    return {"ssid_2G": {"radioId": 0, "ssid": [item]}}


def _state(**body):
    return ScheduleState().apply(parse_config_body(body).schedule, full_sync=True)


# --- periods and state --------------------------------------------------

def test_radio_off_during_the_range():
    state = _state(schedulerGlobal={"enable": True, "mode": 0}, schedulerList=_range(), schedulerAssoc=_assoc(0))

    assert state.ssid_off(SSID_ID, 0, SATURDAY_NOON)
    assert not state.ssid_off(SSID_ID, 0, SATURDAY_NIGHT)
    assert not state.ssid_off(SSID_ID, 0, MONDAY_NOON)


def test_radio_on_only_during_the_range():
    state = _state(schedulerGlobal={"enable": True}, schedulerList=_range(), schedulerAssoc=_assoc(1))

    assert not state.ssid_off(SSID_ID, 0, SATURDAY_NOON)
    assert state.ssid_off(SSID_ID, 0, SATURDAY_NIGHT)
    assert state.ssid_off(SSID_ID, 0, MONDAY_NOON)


def test_day_types():
    everyday = SchedulePeriod("Everyday", 0, 0, 60)
    weekday = SchedulePeriod("Weekday", 0, 600, 900)
    monday = SchedulePeriod("Select Days", 1, 600, 900)
    sunday = SchedulePeriod("Select Days", 7, 600, 900)

    assert everyday.covers(datetime(2026, 11, 28, 0, 30))
    assert weekday.covers(MONDAY_NOON) and not weekday.covers(SATURDAY_NOON)
    assert monday.covers(MONDAY_NOON) and not monday.covers(SATURDAY_NOON)
    assert sunday.covers(datetime(2026, 11, 29, 12, 0))


def test_overnight_range_belongs_to_the_day_it_starts():
    friday_night = SchedulePeriod("Select Days", 5, 22 * 60, 6 * 60)

    assert friday_night.covers(datetime(2026, 11, 27, 23, 0))  # Friday 23:00
    assert friday_night.covers(datetime(2026, 11, 28, 5, 59))  # Saturday 05:59
    assert not friday_night.covers(datetime(2026, 11, 28, 6, 0))
    assert not friday_night.covers(datetime(2026, 11, 27, 5, 0))  # Friday morning


def test_global_switch_off_and_no_profile_mean_no_schedule():
    off = _state(schedulerGlobal={"enable": False}, schedulerList=_range(), schedulerAssoc=_assoc(0))
    unlinked = _state(schedulerGlobal={"enable": True}, schedulerList=_range(), schedulerAssoc=_assoc(0, profile=None))

    assert not off.ssid_off(SSID_ID, 0, SATURDAY_NOON)
    assert not unlinked.ssid_off(SSID_ID, 0, SATURDAY_NOON)


def test_deleted_time_range_schedules_nothing():
    state = _state(schedulerGlobal={"enable": True}, schedulerList=_range(), schedulerAssoc=_assoc(1))
    gone = state.apply(
        ScheduleChange(list_rules=(ScheduleListRule(operation=2, profile_id=PROFILE_ID),)), full_sync=False
    )

    assert not gone.ssid_off(SSID_ID, 0, MONDAY_NOON)


def test_state_round_trips_through_json():
    state = _state(schedulerGlobal={"enable": True}, schedulerList=_range(), schedulerAssoc=_assoc(1))

    assert ScheduleState.from_dict(json.loads(json.dumps(state.to_dict()))) == state


# --- UCI ------------------------------------------------------------------

def _managed(**extra):
    return {".type": "wifi-iface", "openomada_managed": "1", "openomada_ssid_id": str(SSID_ID), **extra}


def test_rebuilt_ssid_starts_off_inside_the_range():
    state = _state(schedulerGlobal={"enable": True}, schedulerList=_range(), schedulerAssoc=_assoc(0))
    update = parse_config_body(_ssid_body())

    batch = build_uci_batch(update, _caps(), schedule=state, now=SATURDAY_NOON)

    assert "set wireless.openomada_2g_1.disabled='1'" in batch
    assert "set wireless.openomada_2g_1.openomada_schedule_off='1'" in batch


def test_schedule_change_turns_other_ssids_back_on():
    wireless_state = {"openomada_5g_1": _managed(disabled="1", openomada_schedule_off="1")}
    update = parse_config_body({"schedulerAssoc": _assoc(0, profile=None)})
    state = ScheduleState().apply(update.schedule, full_sync=False)

    batch = build_uci_batch(
        update, _caps(), schedule=state, schedule_changed=True, wireless_state=wireless_state, now=SATURDAY_NOON
    )

    assert batch == (
        "delete wireless.openomada_5g_1.disabled",
        "delete wireless.openomada_5g_1.openomada_schedule_off",
        "commit wireless",
    )


def test_ssid_the_agent_did_not_turn_off_is_left_alone():
    wireless_state = {"openomada_5g_1": _managed(disabled="1")}
    update = parse_config_body({"schedulerAssoc": _assoc(0, profile=None)})

    batch = build_uci_batch(
        update, _caps(), schedule=ScheduleState(), schedule_changed=True, wireless_state=wireless_state
    )

    assert batch == ()


# --- adapter --------------------------------------------------------------

class _Runner:
    def __init__(self, show=""):
        self.calls = []
        self.show = show

    def run(self, args, *, input_text=None):
        self.calls.append((tuple(args), input_text))
        if args[:3] == ["uci", "-q", "show"]:
            return CommandResult(0, self.show, "")
        return CommandResult(0, "", "")


def _batches(runner):
    return [text for args, text in runner.calls if args == ("uci", "-q", "batch")]


def _full_sync(action=0):
    return parse_config_body(
        {"configVersion": 5, "schedulerGlobal": {"enable": True, "mode": 0}, "schedulerList": _range(),
         "schedulerAssoc": _assoc(action), **_ssid_body()}
    )


def test_tick_turns_the_ssid_off_and_back_on():
    clock = [SATURDAY_NIGHT]
    runner = _Runner(show="wireless.openomada_2g_1=wifi-iface\nwireless.openomada_2g_1.openomada_managed='1'\n"
                          f"wireless.openomada_2g_1.openomada_ssid_id='{SSID_ID}'\n")
    adapter = OpenWrtUciAdapter(runner, clock=lambda: clock[0])
    assert adapter.reconcile(_full_sync(0), _caps()).applied
    assert adapter.tick() is None  # outside the range: nothing to do

    clock[0] = SATURDAY_NOON
    result = adapter.tick()
    assert result is not None and result.applied
    assert "set wireless.openomada_2g_1.disabled='1'" in _batches(runner)[-1]
    assert ("wifi", "reload") in [args for args, _ in runner.calls]

    runner.show += "wireless.openomada_2g_1.disabled='1'\nwireless.openomada_2g_1.openomada_schedule_off='1'\n"
    assert adapter.tick() is None  # already off

    clock[0] = SATURDAY_NIGHT
    assert adapter.tick().applied
    assert "delete wireless.openomada_2g_1.disabled" in _batches(runner)[-1]


def test_tick_without_a_schedule_reads_uci_only_once():
    runner = _Runner()
    adapter = OpenWrtUciAdapter(runner)

    adapter.tick()
    adapter.tick()

    assert [args for args, _ in runner.calls] == [("uci", "-q", "show", "wireless")]


def test_schedule_survives_an_agent_restart(tmp_path):
    state_file = tmp_path / ".omada-wireless-state-020000000001.json"
    OpenWrtUciAdapter(_Runner(), state_file=state_file, clock=lambda: SATURDAY_NIGHT).reconcile(
        _full_sync(0), _caps()
    )

    runner = _Runner()
    restarted = OpenWrtUciAdapter(runner, state_file=state_file, clock=lambda: SATURDAY_NOON)
    restarted.reconcile(parse_config_body({"configVersionInc": 1, **_ssid_body()}), _caps())

    assert "disabled='1'" in _batches(runner)[-1]


def test_unknown_codes_are_refused():
    adapter = OpenWrtUciAdapter(_Runner())
    for body in (
        {"schedulerList": _range(type_="Holidays")},
        {"schedulerList": _range(type_="Select Days", day=8)},
        {"schedulerList": _range(end=(25, 0))},
        {"schedulerList": _range(op=7)},
        {"schedulerAssoc": _assoc(action=2)},
        {"schedulerAssoc": _assoc(bands=(3,))},
    ):
        result = adapter.reconcile(parse_config_body({"configVersionInc": 1, **body}), _caps())
        assert not result.applied, body


def test_scheduler_keys_are_no_longer_unsupported():
    update = parse_config_body({"schedulerList": _range()})

    assert update.unhandled_keys == () and update.passive_keys == ()
    assert update.schedule.list_rules[1].period == SchedulePeriod("Weekend", 0, 480, 1080)


def test_use_case_tick_reaches_platform_ports():
    from open_omada_device_agent.application.configuration import ApplyDeviceConfiguration

    ticks = []

    class _Port:
        def tick(self):
            ticks.append(1)

    ApplyDeviceConfiguration(
        capability_detector=_caps, platform_ports=(_Port(), object()), command_ports=()
    ).tick()

    assert ticks == [1]


def test_period_delete_for_an_unknown_range_does_not_turn_the_ssid_off():
    # E.g. the state file was lost: only the assoc and a period delete are known.
    state = _state(schedulerGlobal={"enable": True}, schedulerAssoc=_assoc(1))
    state = state.apply(
        ScheduleChange(list_rules=(ScheduleListRule(operation=2, profile_id=PROFILE_ID, rule_id=9),)),
        full_sync=False,
    )

    assert PROFILE_ID not in state.profiles
    assert not state.ssid_off(SSID_ID, 0, MONDAY_NOON)


def test_add_before_delete_with_the_same_rule_id_keeps_the_new_period():
    state = _state(schedulerGlobal={"enable": True}, schedulerList=_range(), schedulerAssoc=_assoc(0))
    friday = SchedulePeriod("Select Days", 5, 480, 1080)
    state = state.apply(
        ScheduleChange(
            list_rules=(
                ScheduleListRule(operation=1, profile_id=PROFILE_ID, rule_id=1, period=friday),
                ScheduleListRule(operation=2, profile_id=PROFILE_ID, rule_id=1),
            )
        ),
        full_sync=False,
    )

    assert state.profiles[PROFILE_ID] == {1: friday}


def test_overnight_sunday_range_runs_into_monday():
    sunday_night = SchedulePeriod("Select Days", 7, 22 * 60, 6 * 60)

    assert sunday_night.covers(datetime(2026, 11, 29, 23, 0))  # Sunday 23:00
    assert sunday_night.covers(datetime(2026, 11, 30, 5, 0))  # Monday 05:00
    assert not sunday_night.covers(datetime(2026, 11, 30, 23, 0))  # Monday 23:00


class _FailingRunner(_Runner):
    def __init__(self, show="", fail=()):
        super().__init__(show)
        self.fail = fail

    def run(self, args, *, input_text=None):
        if self.fail and tuple(args[: len(self.fail)]) == self.fail:
            self.calls.append((tuple(args), input_text))
            return CommandResult(1, "", "failed")
        return super().run(args, input_text=input_text)


_SHOW = (
    "wireless.openomada_2g_1=wifi-iface\nwireless.openomada_2g_1.openomada_managed='1'\n"
    f"wireless.openomada_2g_1.openomada_ssid_id='{SSID_ID}'\n"
)


def test_tick_reports_a_failed_batch_or_reload():
    for fail, changed in ((("uci", "-q", "batch"), False), (("wifi", "reload"), True)):
        runner = _FailingRunner(show=_SHOW)
        adapter = OpenWrtUciAdapter(runner, clock=lambda: SATURDAY_NIGHT)
        adapter.reconcile(_full_sync(0), _caps())
        runner.fail = fail

        result = OpenWrtUciAdapter(runner, clock=lambda: SATURDAY_NOON)
        result._schedule = adapter._schedule
        outcome = result.tick()

        assert outcome is not None and not outcome.applied and outcome.changed is changed


def test_failed_first_uci_show_is_retried():
    runner = _FailingRunner(fail=("uci", "-q", "show"))
    adapter = OpenWrtUciAdapter(runner)

    adapter.tick()
    runner.fail = ()
    adapter.tick()
    adapter.tick()

    shows = [args for args, _ in runner.calls if args[:3] == ("uci", "-q", "show")]
    assert len(shows) == 2


def test_session_tick_survives_an_exception(caplog):
    from types import SimpleNamespace

    from open_omada_device_agent.adoption import _tick

    def boom():
        raise RuntimeError("uci crashed")

    _tick(SimpleNamespace(configuration=SimpleNamespace(tick=boom)))

    assert "WLAN schedule check failed" in caplog.text
