"""SSID Security (securityMode): only None and WPA-Personal are applied.

Anything else gets no key from OpenWrt's point of view, so writing it would
create an open network; those SSIDs are refused instead.
"""
import pytest

from open_omada_device_agent.ap_config import parse_config_body
from open_omada_device_agent.domain import RadioBand
from open_omada_device_agent.openwrt import CommandResult, OpenWrtUciAdapter, build_uci_batch, validate_update
from open_omada_device_agent.platform_capabilities import PlatformCapabilities

KEY = "network-passphrase"  # made up


def _caps(**overrides):
    values = {
        "platform": "openwrt",
        "radio_bands": (RadioBand.TWO_G, RadioBand.FIVE_G),
        "max_ssids": 4,
        "supports_wlan_config": True,
        "supports_wpa2_psk": True,
        "supports_radius": True,
    }
    values.update(overrides)
    return PlatformCapabilities(**values)


def _update(**ssid):
    item = {"index": 1, "ssidName": "lab", **ssid}
    return parse_config_body({"ssid_2G": {"radioId": 0, "ssid": [item]}})


def _encryption(batch):
    return next(line for line in batch if ".openomada_2g_1.encryption=" in line).split("=", 1)[1]


def test_wpa_personal_with_a_key_is_applied():
    update = _update(securityMode=3, pskVer=2, pskCipher=3, pskKey=KEY)

    assert validate_update(update, _caps()) == ()
    assert _encryption(build_uci_batch(update, _caps())) == "'psk2+ccmp'"


@pytest.mark.parametrize("ssid", [{}, {"pskKey": ""}, {"pskKey": None}])
def test_wpa_personal_without_a_key_is_refused(ssid):
    errors = validate_update(_update(securityMode=3, pskVer=2, **ssid), _caps())

    assert any("WPA-Personal without a Wi-Fi key" in error for error in errors)


def test_none_is_open_even_with_a_leftover_key():
    # Switching an SSID from WPA-Personal to None may still carry the old key.
    update = _update(securityMode=0, pskKey=KEY)

    assert validate_update(update, _caps()) == ()
    batch = build_uci_batch(update, _caps())
    assert _encryption(batch) == "'none'"
    assert not any(".openomada_2g_1.key=" in line for line in batch)


@pytest.mark.parametrize(
    ("ssid", "message"),
    [
        ({"securityMode": 2, "radiusAuth": {"servers": []}}, "WPA-Enterprise is not supported"),
        ({"securityMode": 4}, "PPSK is not supported"),
        ({"securityMode": 5}, "PPSK is not supported"),
        ({"securityMode": 0, "oweEnable": True}, "OWE (Enhanced Open) is not supported"),
        ({"securityMode": 9}, "unsupported security mode code 9"),
    ],
)
def test_other_security_modes_are_refused_not_opened(ssid, message):
    errors = validate_update(_update(**ssid), _caps())

    assert any(message in error for error in errors)


def test_without_a_security_mode_the_key_decides():
    assert _encryption(build_uci_batch(_update(pskKey=KEY, pskVer=2, pskCipher=3), _caps())) == "'psk2+ccmp'"
    assert _encryption(build_uci_batch(_update(), _caps())) == "'none'"


class _Runner:
    def __init__(self, show=""):
        self.calls = []
        self.show = show

    def run(self, args, *, input_text=None):
        self.calls.append((tuple(args), input_text))
        if args[:3] == ["uci", "-q", "show"]:
            return CommandResult(0, self.show, "")
        return CommandResult(0, "", "")


def _show(**options):
    lines = ["wireless.openomada_2g_1=wifi-iface", "wireless.openomada_2g_1.openomada_managed='1'"]
    lines += [f"wireless.openomada_2g_1.{name}='{value}'" for name, value in options.items()]
    return "\n".join(lines) + "\n"


def _batches(runner):
    return [text for args, text in runner.calls if args == ("uci", "-q", "batch")]


def test_refused_ssid_left_open_by_an_earlier_apply_is_removed():
    # An older agent wrote this PPSK SSID as an open network.
    runner = _Runner(_show(ssid="lab", encryption="none"))
    result = OpenWrtUciAdapter(runner).reconcile(_update(securityMode=4), _caps())

    assert not result.applied
    assert result.changed
    assert "PPSK is not supported" in result.error
    assert "removed the open SSID" in result.error
    assert _batches(runner) == ["delete wireless.openomada_2g_1\ncommit wireless\n"]
    assert ("wifi", "reload") in [args for args, _ in runner.calls]


@pytest.mark.parametrize(
    "options",
    [
        {"disabled": "1"},  # turned off by hand or an older fix
        {"disabled": "1", "openomada_schedule_off": "1"},  # off by schedule: the tick would turn it back on
    ],
)
def test_refused_open_ssid_that_is_off_is_removed_too(options):
    runner = _Runner(_show(encryption="none", **options))
    OpenWrtUciAdapter(runner).reconcile(_update(securityMode=2), _caps())

    assert _batches(runner) == ["delete wireless.openomada_2g_1\ncommit wireless\n"]


def test_refused_ssid_that_is_secured_is_left_alone():
    runner = _Runner(_show(encryption="psk2+ccmp"))
    result = OpenWrtUciAdapter(runner).reconcile(_update(securityMode=2), _caps())

    assert not result.applied
    assert not result.changed
    assert _batches(runner) == []


def test_ssids_past_the_limit_are_not_checked():
    # The SET is refused for the first SSID's VLAN; the PPSK one is past
    # max_ssids, so it is never applied or checked.
    show = "wireless.openomada_2g_2=wifi-iface\nwireless.openomada_2g_2.openomada_managed='1'\n"
    runner = _Runner(show)
    items = [
        {"index": 1, "ssidName": "lab", "securityMode": 0, "vlanId": 10},
        {"index": 2, "ssidName": "extra", "securityMode": 4},
    ]
    update = parse_config_body({"ssid_2G": {"radioId": 0, "ssid": items}})
    result = OpenWrtUciAdapter(runner).reconcile(update, _caps(max_ssids=1, supports_ssid_vlan=False))

    assert not result.applied
    assert _batches(runner) == []


class _FailingRunner(_Runner):
    def __init__(self, show, fail):
        super().__init__(show)
        self.fail = fail

    def run(self, args, *, input_text=None):
        if tuple(args[: len(self.fail)]) == self.fail:
            self.calls.append((tuple(args), input_text))
            return CommandResult(1, "", "failed")
        return super().run(args, input_text=input_text)


@pytest.mark.parametrize(
    ("fail", "changed"),
    [
        (("uci", "-q", "show"), False),
        (("uci", "-q", "batch"), False),
        (("wifi", "reload"), True),
    ],
)
def test_failures_still_refuse_the_set(fail, changed):
    runner = _FailingRunner(_show(encryption="none"), fail)
    result = OpenWrtUciAdapter(runner).reconcile(_update(securityMode=4), _caps())

    assert not result.applied
    assert result.changed is changed
    assert "PPSK is not supported" in result.error


def test_other_refusals_do_not_touch_open_ssids():
    # None is a valid open SSID; the SET is refused for another reason.
    runner = _Runner(_show(encryption="none"))
    update = _update(securityMode=0, vlanEnable=True, vlanId=10)
    result = OpenWrtUciAdapter(runner).reconcile(update, _caps(supports_ssid_vlan=False))

    assert not result.applied
    assert _batches(runner) == []
    assert not any(args[:3] == ("uci", "-q", "show") for args, _ in runner.calls)


@pytest.mark.parametrize(
    ("ssid", "message"),
    [
        ({"securityMode": "4"}, "PPSK is not supported"),
        ({"securityMode": 0, "oweEnable": "true"}, "OWE (Enhanced Open) is not supported"),
    ],
)
def test_string_values_from_the_controller_are_read(ssid, message):
    errors = validate_update(_update(**ssid), _caps())

    assert any(message in error for error in errors)


def test_owe_flag_false_as_a_string_stays_open():
    update = _update(securityMode=0, oweEnable="false")

    assert validate_update(update, _caps()) == ()
