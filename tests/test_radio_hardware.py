from pathlib import Path

from open_omada_device_agent.adapters.outbound.openwrt.capabilities import (
    detect_platform_capabilities,
)
from open_omada_device_agent.adapters.outbound.openwrt.device_profile import (
    GenericOpenWrtAccessPointProfile,
)
from open_omada_device_agent.adapters.outbound.openwrt.iw_phy import (
    parse_dfs_region,
    parse_iw_list,
)
from open_omada_device_agent.application.contracts import PlatformCapabilities
from open_omada_device_agent.bootstrap import AgentSettings
from open_omada_device_agent.contexts.wireless.domain import RadioBand

JIO_AX3000 = (Path(__file__).parent / "fixtures" / "iw_list_jio_6101_ax3000.txt").read_text()

HT_ONLY_5G = """Wiphy phy0
\tBand 2:
\t\tCapabilities: 0x1ef
\t\t\tHT20/HT40
\t\tHT TX/RX MCS rate indexes supported: 0-15
\t\tFrequencies:
\t\t\t* 5180.0 MHz [36] (23.0 dBm)
\t\t\t* 5200.0 MHz [40] (23.0 dBm)
\t\t\t* 5220.0 MHz [44] (23.0 dBm)
\t\t\t* 5240.0 MHz [48] (23.0 dBm)
\tSupported commands:
"""

VHT_ONLY_5G = """Wiphy phy0
\tBand 2:
\t\tCapabilities: 0x1ef
\t\t\tHT20/HT40
\t\tHT TX/RX MCS rate indexes supported: 0-15
\t\tVHT Capabilities (0x338001b2):
\t\t\tSupported Channel Width: neither 160 nor 80+80
\t\tVHT RX MCS set:
\t\t\t1 streams: MCS 0-9
\t\t\t2 streams: MCS 0-9
\t\t\t3 streams: not supported
\t\tFrequencies:
\t\t\t* 5180.0 MHz [36] (23.0 dBm)
\t\t\t* 5200.0 MHz [40] (23.0 dBm)
\t\t\t* 5220.0 MHz [44] (23.0 dBm)
\t\t\t* 5240.0 MHz [48] (23.0 dBm)
\t\t\t* 5260.0 MHz [52] (20.0 dBm) (radar detection)
\t\t\t* 5745.0 MHz [149] (20.0 dBm) (no IR)
\tSupported commands:
"""


def _profile(radio_hardware, bands=(RadioBand.TWO_G, RadioBand.FIVE_G)):
    return GenericOpenWrtAccessPointProfile(
        AgentSettings.from_environment(),
        ip_address=lambda: "192.0.2.10",
        capabilities=PlatformCapabilities(
            platform="openwrt",
            radio_bands=bands,
            max_ssids=4,
            supports_wlan_config=True,
            radio_hardware=tuple(radio_hardware),
        ),
    )


def _channels(profile, band_label):
    radio = next(r for r in profile.channel_info() if r["band"] == band_label)
    return {c["vl"]: c for c in radio["channelList"]}


def test_parses_ax_radio_capabilities_from_iw_list():
    radios = {hw.band: hw for hw in parse_iw_list(JIO_AX3000)}

    five = radios[RadioBand.FIVE_G]
    assert (five.ht40, five.vht, five.vht160, five.he, five.he160) == (
        True, True, True, True, True,
    )
    assert five.streams == 4
    by_channel = {ch.channel: ch for ch in five.channels}
    assert by_channel[36].radar is False and by_channel[36].max_power == 30
    assert by_channel[52].radar is True
    assert 177 not in by_channel  # disabled under the regulatory domain

    two = radios[RadioBand.TWO_G]
    assert (two.ht40, two.vht, two.he, two.he160) == (True, False, True, False)
    assert two.streams == 4
    assert 14 not in {ch.channel for ch in two.channels}


def test_parses_ht_and_vht_only_radios():
    (ht,) = parse_iw_list(HT_ONLY_5G)
    assert (ht.band, ht.ht40, ht.vht, ht.he, ht.streams) == (
        RadioBand.FIVE_G, True, False, False, 2,
    )

    (vht,) = parse_iw_list(VHT_ONLY_5G)
    assert (vht.vht, vht.vht160, vht.he, vht.streams) == (True, False, False, 2)
    assert 149 not in {ch.channel for ch in vht.channels}  # no IR: AP may not transmit


def test_ax_hardware_advertises_80_mhz_and_wifi6_radio_caps():
    profile = _profile(parse_iw_list(JIO_AX3000))

    assert profile.device_misc()["support_11ac"] is True
    five_cap = next(c for c in profile.radio_cap() if c["radioId"] == 1)
    assert (five_cap["mimo"], five_cap["ofdma"], five_cap["mcsLevel"]) == (4, 1, 11)

    five = _channels(profile, "5G")
    assert five[36]["cFlag"] & 0x800000
    assert five[36]["cFlag"] & (0x10000 | 0x20000 | 0x100000 | 0x200000) == (
        0x10000 | 0x20000 | 0x100000 | 0x200000
    )
    assert five[40]["cFlag"] & 0x400000
    assert five[161]["cFlag"] & 0x800000
    assert not five[165]["cFlag"] & 0x800000
    assert not any(ch in five for ch in (52, 100, 144))  # no DFS region: radar channels hidden
    # Per-channel limit from iw (30 dBm under IN), and maxPow follows it so the
    # controller's Tx Power range reaches what the radio can do.
    assert all(c["mPow"] == 30 for c in five.values())
    assert (five_cap["minPow"], five_cap["maxPow"]) == (1, 30)

    two = _channels(profile, "2.4G")
    assert all(not c["cFlag"] & (0x100000 | 0x800000) for c in two.values())
    # Same values as real Wi-Fi 6 EAPs (EAP650-Wall template: 0x300e0 / 0x500e0).
    assert two[1]["cFlag"] == 0x10000 | 0x20000 | 0xE0
    assert two[13]["cFlag"] == 0x10000 | 0x40000 | 0xE0


def test_ht_only_hardware_does_not_advertise_11ac_or_80_mhz():
    profile = _profile(parse_iw_list(HT_ONLY_5G), bands=(RadioBand.FIVE_G,))

    assert profile.device_misc()["support_11ac"] is False
    cap = profile.radio_cap()[0]
    assert (cap["mimo"], cap["ofdma"], cap["mcsLevel"]) == (2, 0, 7)
    assert all(not c["cFlag"] & 0x800000 for c in _channels(profile, "5G").values())


def test_detect_platform_capabilities_collects_hardware_for_configured_bands():
    caps = detect_platform_capabilities(
        env={"OMADA_PLATFORM": "openwrt", "OMADA_RADIO_BANDS": "5g"},
        command_exists=lambda name: f"/usr/sbin/{name}",
        command_output=lambda args: JIO_AX3000 if args[-1] == "list" else "",
    )

    assert [hw.band for hw in caps.radio_hardware] == [RadioBand.FIVE_G]
    assert caps.hardware_for(RadioBand.FIVE_G).he160 is True
    assert caps.hardware_for(RadioBand.TWO_G) is None


REG_FCC = """global
country PA: DFS-FCC
\t(2402 - 2472 @ 40), (N/A, 30), (N/A)
"""

REG_IN = """global
country IN: DFS-UNSET
\t(5470 - 5725 @ 160), (N/A, 24), (0 ms), DFS
"""


def _detect(reg_output):
    outputs = {"list": JIO_AX3000, "get": reg_output}
    return detect_platform_capabilities(
        env={"OMADA_PLATFORM": "openwrt", "OMADA_RADIO_BANDS": "2g,5g"},
        command_exists=lambda name: f"/usr/sbin/{name}",
        command_output=lambda args: outputs.get(args[-1], ""),
    )


def test_dfs_follows_the_regulatory_dfs_region():
    assert parse_dfs_region(REG_FCC) == "DFS-FCC"
    assert parse_dfs_region(REG_IN) == "DFS-UNSET"
    assert all(hw.dfs for hw in _detect(REG_FCC).radio_hardware)
    assert not any(hw.dfs for hw in _detect(REG_IN).radio_hardware)


def test_dfs_region_advertises_radar_channels_and_160_mhz():
    caps = _detect(REG_FCC)
    profile = _profile(caps.radio_hardware)

    assert profile.device_misc()["supportDfs"] == 7
    five = _channels(profile, "5G")
    assert five[100]["cFlag"] & 0x200 and five[52]["cFlag"] & 0x200
    assert not five[36]["cFlag"] & 0x200
    for channel in (36, 64, 100, 128):
        assert five[channel]["cFlag"] & 0x1000000, channel
    assert not five[132]["cFlag"] & 0x1000000
    assert not five[149]["cFlag"] & 0x1000000  # 177 missing: no full 160 block


def test_dfs_unset_keeps_radar_channels_and_160_mhz_hidden():
    profile = _profile(_detect(REG_IN).radio_hardware)

    assert profile.device_misc()["supportDfs"] == 0
    five = _channels(profile, "5G")
    assert not any(ch in five for ch in (52, 100, 128))
    assert all(not c["cFlag"] & 0x1000000 for c in five.values())


def test_dev_cap_reports_antennas_modes_and_ofdma_like_a_real_eap683():
    from types import SimpleNamespace

    from open_omada_device_agent.adapters.outbound.openwrt.device_profile import (
        GenericOpenWrtAccessPointProfile,
    )
    from open_omada_device_agent.application.contracts import PlatformCapabilities
    from open_omada_device_agent.contexts.wireless.domain import RadioBand, RadioHardware

    caps = PlatformCapabilities(
        platform="openwrt",
        radio_bands=(RadioBand.TWO_G, RadioBand.FIVE_G),
        radio_hardware=(
            RadioHardware(band=RadioBand.TWO_G, ht40=True, he=True, streams=4),
            RadioHardware(band=RadioBand.FIVE_G, vht=True, he=True, he160=True, streams=4),
        ),
    )
    profile = GenericOpenWrtAccessPointProfile.__new__(GenericOpenWrtAccessPointProfile)
    profile._capabilities = caps
    profile._settings = SimpleNamespace()

    # Values from Controller 6.3.0.111's EAP683 UR model template.
    assert profile.dev_cap() == {
        "meshChainNum": 0,
        "mimo2G": 4,
        "supportRadioMode2G": 180,
        "mimo5G": 4,
        "supportRadioMode5G": 368,
        "supportOFDMA": 3,
    }


def test_dev_cap_for_a_wifi5_router_matches_ac_eaps():
    from open_omada_device_agent.adapters.outbound.openwrt.device_profile import (
        GenericOpenWrtAccessPointProfile,
    )
    from open_omada_device_agent.application.contracts import PlatformCapabilities
    from open_omada_device_agent.contexts.wireless.domain import RadioBand, RadioHardware

    caps = PlatformCapabilities(
        platform="openwrt",
        radio_bands=(RadioBand.TWO_G, RadioBand.FIVE_G),
        radio_hardware=(
            RadioHardware(band=RadioBand.TWO_G, ht40=True, streams=2),
            RadioHardware(band=RadioBand.FIVE_G, vht=True, streams=2),
        ),
    )
    profile = GenericOpenWrtAccessPointProfile.__new__(GenericOpenWrtAccessPointProfile)
    profile._capabilities = caps

    cap = profile.dev_cap()

    assert cap["supportRadioMode2G"] == 52  # EAP225 / EAP235-Wall
    assert cap["supportRadioMode5G"] == 96
    assert "supportOFDMA" not in cap


def test_every_channel_carries_the_band_bits_real_eaps_send():
    profile = _profile(parse_iw_list(JIO_AX3000))

    assert all(c["cFlag"] & 0xFF == 0xE0 for c in _channels(profile, "2.4G").values())
    assert all(c["cFlag"] & 0x1FF == 0x140 for c in _channels(profile, "5G").values())
