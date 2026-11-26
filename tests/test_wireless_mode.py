import pytest

from open_omada_device_agent.adapters.inbound.ecsp.mappers.configuration import (
    parse_config_body,
)
from open_omada_device_agent.adapters.outbound.openwrt.uci import (
    OpenWrtUciAdapter,
    _radio_htmode,
    build_uci_batch,
)
from open_omada_device_agent.application.contracts import PlatformCapabilities
from open_omada_device_agent.contexts.wireless.domain import (
    RadioBand,
    RadioConfig,
    RadioHardware,
)
from open_omada_device_agent.openwrt import CommandResult

AX_2G = RadioHardware(band=RadioBand.TWO_G, ht40=True, he=True, streams=4)
AX_5G = RadioHardware(band=RadioBand.FIVE_G, ht40=True, vht=True, vht160=True, he=True, he160=True, streams=4)
AC_5G = RadioHardware(band=RadioBand.FIVE_G, ht40=True, vht=True, streams=2)


def _radio(band, *, mode=None, width=None):
    return RadioConfig(band=band, radio_id=0 if band is RadioBand.TWO_G else 1, wireless_mode=mode, channel_width=width)


@pytest.mark.parametrize(
    ("band", "mode", "width", "hardware", "expected"),
    [
        (RadioBand.TWO_G, 3, 4, AX_2G, "NOHT"),  # b/g: legacy rates only
        (RadioBand.TWO_G, 4, 4, AX_2G, "HT40"),  # b/g/n
        (RadioBand.TWO_G, 13, 2, AX_2G, "HE20"),  # b/g/n/ax
        (RadioBand.FIVE_G, 7, 6, AX_5G, "HT40"),  # a/n: 802.11n stops at 40 MHz
        (RadioBand.FIVE_G, 10, 5, AX_5G, "VHT80"),  # a/n/ac
        (RadioBand.FIVE_G, 16, 7, AX_5G, "HE160"),  # a/n/ac/ax
        (RadioBand.FIVE_G, 18, 5, AX_5G, "HE80"),  # be capped at what the radio has
        (RadioBand.FIVE_G, 16, 5, AC_5G, "VHT80"),  # ax capped on a Wi-Fi 5 radio
    ],
)
def test_wireless_mode_selects_htmode_family(band, mode, width, hardware, expected):
    assert _radio_htmode(_radio(band, mode=mode, width=width), "HE40", hardware) == expected


def test_auto_mode_restores_the_hardware_family():
    # Auto arrives without wirelessMode; after b/g the radio is on NOHT.
    assert _radio_htmode(_radio(RadioBand.TWO_G, width=4), "NOHT", AX_2G) == "HE40"
    assert _radio_htmode(_radio(RadioBand.FIVE_G, width=6), "HT40", AX_5G) == "HE80"


def test_mode_without_width_keeps_the_current_width():
    assert _radio_htmode(_radio(RadioBand.FIVE_G, mode=10), "HE80", AX_5G) == "VHT80"
    assert _radio_htmode(_radio(RadioBand.TWO_G, mode=13), "NOHT", AX_2G) == "HE20"


def test_without_hardware_details_the_current_family_is_kept():
    assert _radio_htmode(_radio(RadioBand.FIVE_G, width=5), "HE40", None) == "HE80"
    assert _radio_htmode(_radio(RadioBand.FIVE_G, width=5), None, None) == "VHT80"


def _caps():
    return PlatformCapabilities(
        platform="openwrt",
        has_uci=True,
        radio_bands=(RadioBand.TWO_G, RadioBand.FIVE_G),
        radio_hardware=(AX_2G, AX_5G),
        supports_wlan_config=True,
    )


def test_batch_writes_htmode_from_the_controller_mode():
    update = parse_config_body(
        {"wirelessBasic_2G": {"radioId": 0, "wirelessMode": 3, "chanWidth": 4, "channel": 6}}
    )

    batch = build_uci_batch(update, _caps(), current_htmodes={"radio0": "HE40"})

    assert "set wireless.radio0.htmode='NOHT'" in batch


class _Runner:
    def __init__(self):
        self.calls = []

    def run(self, args, *, input_text=None):
        self.calls.append((tuple(args), input_text))
        if tuple(args) == ("uci", "-q", "get", "wireless.radio1.htmode"):
            return CommandResult(0, "HE80\n")
        return CommandResult(0)


def test_adapter_reads_current_width_when_only_the_mode_changes():
    runner = _Runner()
    update = parse_config_body({"wirelessBasic_5G": {"radioId": 1, "wirelessMode": 10}})

    result = OpenWrtUciAdapter(runner).reconcile(update, _caps())

    assert result.applied
    batch = next(text for args, text in runner.calls if args == ("uci", "-q", "batch"))
    assert "set wireless.radio1.htmode='VHT80'" in batch
