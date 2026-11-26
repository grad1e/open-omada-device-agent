"""Reference access-point profile backed by generic Linux observations."""
from collections.abc import Callable
from typing import Any

from ....application.contracts import PlatformCapabilities
from ....application.settings import AgentSettings
from ....capabilities import ap_components_v2
from ....contexts.device.domain import DeviceIdentity
from ....contexts.wireless.domain import RadioBand, RadioChannel, RadioHardware
from ....shared.domain import MacAddress
from ....system_tools import get_cpu_utilization, get_memory_utilization, get_system_uptime


RADIO_ID_BY_BAND: dict[RadioBand, int] = {
    RadioBand.TWO_G: 0,
    RadioBand.FIVE_G: 1,
    RadioBand.FIVE_G2: 2,
    RadioBand.SIX_G: 3,
}

BAND_LABEL_BY_BAND: dict[RadioBand, str] = {
    RadioBand.TWO_G: "2.4G",
    RadioBand.FIVE_G: "5G",
    RadioBand.FIVE_G2: "5G2",
    RadioBand.SIX_G: "6G",
}

# Omada stores channel width support as regulatory-domain bit flags. These
# values are the same constants used by Controller 6.2.14.11's channel code.
# 0x10000 is the base 20 MHz bit: a real EAP650 (fw 1.3.13) reports it on every
# 2.4 GHz and 5 GHz channel. Without it the controller offers no channel for
# 20 MHz ("No available channels").
WIDTH_FLAG_20 = 0x10000
WIDTH_FLAG_2G_HT40 = WIDTH_FLAG_20 | 0x20000 | 0x40000
WIDTH_FLAG_5G_HT40_VHT80 = WIDTH_FLAG_20 | 0x20000 | 0x40000 | 0x800100
WIDTH_FLAG_6G_20_40_80_160 = WIDTH_FLAG_20 | 0x100 | 0x20000 | 0x40000 | 0x800000 | 0x1000000

# Band bits. They match the Qualcomm/net80211 channel flags (CCK 0x20,
# OFDM 0x40, 2GHZ 0x80, 5GHZ 0x100, PASSIVE 0x200) and are set on every channel
# of every EAP model template in Controller 6.3.0.111, Wi-Fi 4 to Wi-Fi 7:
# 2.4 GHz 0xe0, 5 GHz 0x140 (0x340 on radar channels), 6 GHz 0x300.
FLAG_CCK = 0x20
FLAG_OFDM = 0x40
FLAG_2GHZ = 0x80
FLAG_PASSIVE = 0x200
BAND_FLAGS: dict[RadioBand, int] = {
    RadioBand.TWO_G: FLAG_CCK | FLAG_OFDM | FLAG_2GHZ,
    RadioBand.FIVE_G: FLAG_OFDM | 0x100,
    RadioBand.FIVE_G2: FLAG_OFDM | 0x100,
    RadioBand.SIX_G: 0x100 | FLAG_PASSIVE,
}

# Per-bit meanings observed on a real EAP650 (fw 1.3.13) channel list.
FLAG_HT40_UP = 0x20000
FLAG_HT40_DOWN = 0x40000
FLAG_5G_COMMON = 0x100  # present on every 5 GHz channel of a real EAP650
FLAG_VHT20 = 0x100000
FLAG_VHT40_UP = 0x200000
FLAG_VHT40_DOWN = 0x400000
FLAG_80 = 0x800000
FLAG_160 = 0x1000000
FLAG_DFS = FLAG_PASSIVE  # radar channel; a real EAP650 sets it on 52-140

# First channel of each 80 MHz block in the 5 GHz band.
FIVE_G_80_BLOCKS = (36, 52, 100, 116, 132, 149)
FIVE_G_160_BLOCKS = (36, 100, 149)

DEFAULT_MAX_POWER: dict[RadioBand, int] = {RadioBand.TWO_G: 20}
DEFAULT_MAX_POWER_OTHER = 23

CHANNELS_BY_BAND: dict[RadioBand, tuple[tuple[int, int, int, int], ...]] = {
    RadioBand.TWO_G: tuple(
        (channel, 2407 + channel * 5, 20, WIDTH_FLAG_2G_HT40)
        for channel in range(1, 14)
    ),
    RadioBand.FIVE_G: (
        (36, 5180, 23, WIDTH_FLAG_5G_HT40_VHT80),
        (40, 5200, 23, WIDTH_FLAG_5G_HT40_VHT80),
        (44, 5220, 23, WIDTH_FLAG_5G_HT40_VHT80),
        (48, 5240, 23, WIDTH_FLAG_5G_HT40_VHT80),
        (149, 5745, 23, WIDTH_FLAG_5G_HT40_VHT80),
        (153, 5765, 23, WIDTH_FLAG_5G_HT40_VHT80),
        (157, 5785, 23, WIDTH_FLAG_5G_HT40_VHT80),
        (161, 5805, 23, WIDTH_FLAG_5G_HT40_VHT80),
        (165, 5825, 23, WIDTH_FLAG_5G_HT40_VHT80),
    ),
    RadioBand.FIVE_G2: (
        (149, 5745, 23, WIDTH_FLAG_5G_HT40_VHT80),
        (153, 5765, 23, WIDTH_FLAG_5G_HT40_VHT80),
        (157, 5785, 23, WIDTH_FLAG_5G_HT40_VHT80),
        (161, 5805, 23, WIDTH_FLAG_5G_HT40_VHT80),
        (165, 5825, 23, WIDTH_FLAG_5G_HT40_VHT80),
    ),
    RadioBand.SIX_G: (
        (5, 5975, 23, WIDTH_FLAG_6G_20_40_80_160),
        (21, 6055, 23, WIDTH_FLAG_6G_20_40_80_160),
        (37, 6135, 23, WIDTH_FLAG_6G_20_40_80_160),
        (53, 6215, 23, WIDTH_FLAG_6G_20_40_80_160),
        (69, 6295, 23, WIDTH_FLAG_6G_20_40_80_160),
    ),
}


class GenericOpenWrtAccessPointProfile:
    def __init__(
        self,
        settings: AgentSettings,
        *,
        ip_address: Callable[[], str],
        capabilities: PlatformCapabilities,
        lan_setting: Callable[[], dict[str, Any] | None] | None = None,
    ) -> None:
        self._settings = settings
        self._ip_address = ip_address
        self._capabilities = capabilities
        self._lan_setting = lan_setting

    def lan_setting(self) -> dict[str, Any] | None:
        """Current IP Settings for DEVICE_NEGOTIATION, when the agent manages them."""
        return self._lan_setting() if self._lan_setting is not None else None

    def identity(self) -> DeviceIdentity:
        return DeviceIdentity(
            mac=MacAddress(self._settings.mac),
            name=self._settings.device_name,
            model=self._settings.model,
            model_version=self._settings.model_version,
            hardware_version=self._settings.hardware_version,
            firmware_version=self._settings.firmware_version,
        )

    def device_info(self) -> dict[str, Any]:
        identity = self.identity()
        return {
            "ip": self._ip_address(),
            "isFactory": True,
            "name": identity.name,
            "model": identity.model,
            "modelVersion": identity.model_version,
            "firmwareVersion": identity.firmware_version,
            "hardwareVersion": identity.hardware_version,
            "upTime": get_system_uptime(),
            "cpuUti": get_cpu_utilization(),
            "memUti": get_memory_utilization(),
            "wirelessLinked": False,
            "p2p": False,
            "supportBridge": 0,
            "mainMac": identity.mac.omada,
        }

    def device_misc(self) -> dict[str, Any]:
        return {
            "modelType": "NORMAL",
            "support_11ac": self._supports_11ac(),
            "support_lag": False,
            "supportMesh": 0,
            "customizeRegion": self._settings.customize_region,
            "support_channelLimit": False,
            "channelLimit_mode": 0,
            # A real EAP650 reports 7; 0 when radar detection is unavailable.
            "supportDfs": 7 if self._supports_dfs() else 0,
            "lanPortsNum": 1,
            "lanVlanPorts": [],
            "lanPoePorts": [],
            "supportRoaming": 0,
        }

    def components_v2(self) -> dict[str, str]:
        return ap_components_v2(self._capabilities)

    def channel_info(self) -> tuple[dict[str, Any], ...]:
        if not self._capabilities.supports_wlan_config:
            return ()
        return tuple(
            {
                "radioId": RADIO_ID_BY_BAND[band],
                "band": BAND_LABEL_BY_BAND[band],
                "channelList": self._channel_list(band),
            }
            for band in self._capabilities.radio_bands
            if band in CHANNELS_BY_BAND
        )

    def _channel_list(self, band: RadioBand) -> list[dict[str, int]]:
        hardware = self._capabilities.hardware_for(band)
        if hardware is not None and band in (RadioBand.TWO_G, RadioBand.FIVE_G):
            channels = _usable_channels(hardware)
            if channels:
                return [
                    _channel_detail(
                        ch.channel,
                        ch.freq,
                        _channel_max_power(band, ch),
                        _width_flags(hardware, ch.channel, {c.channel for c in channels})
                        | BAND_FLAGS.get(band, 0)
                        | (FLAG_DFS if ch.radar else 0),
                    )
                    for ch in channels
                ]
        return [
            _channel_detail(channel, freq, max_power, width_flag | BAND_FLAGS.get(band, 0))
            for channel, freq, max_power, width_flag in CHANNELS_BY_BAND[band]
        ]

    def dev_cap(self) -> dict[str, Any]:
        """Device capabilities in the shape real EAPs send at adoption.

        Only what the hardware has: antenna count, supported wireless modes and
        OFDMA per band, plus the features the agent implements (LLDP). Features
        it does not implement (PPSK, 802.11r, mesh, ...) are left out, so the
        controller does not offer them.
        """
        cap: dict[str, Any] = {"meshChainNum": 0}
        if self._capabilities.supports_lldp:
            cap["supportLldp"] = 1
        ofdma = 0
        for band in self._capabilities.radio_bands:
            suffix = DEV_CAP_SUFFIX_BY_BAND.get(band)
            hardware = self._capabilities.hardware_for(band)
            if suffix is None or hardware is None:
                continue
            cap[f"mimo{suffix}"] = max(1, hardware.streams)
            mode = _support_radio_mode(band, hardware)
            if mode:
                cap[f"supportRadioMode{suffix}"] = mode
            if hardware.he:
                ofdma |= OFDMA_BIT_BY_BAND.get(band, 0)
        if ofdma:
            cap["supportOFDMA"] = ofdma
        return cap

    def radio_cap(self) -> tuple[dict[str, Any], ...]:
        if not self._capabilities.supports_wlan_config:
            return ()
        max_ssids = max(1, self._capabilities.max_ssids)
        return tuple(
            _radio_cap_item(
                RADIO_ID_BY_BAND[band],
                max_ssids=max_ssids,
                hardware=self._capabilities.hardware_for(band),
                max_power=self._max_power(band),
            )
            for band in self._capabilities.radio_bands
            if band in RADIO_ID_BY_BAND
        )

    def _max_power(self, band: RadioBand) -> int:
        # Real EAPs advertise maxPow equal to their highest per-channel mPow;
        # the controller builds the Tx Power range (Custom 1..maxPow, High) from it.
        powers = [channel["mPow"] for channel in self._channel_list(band)] if band in CHANNELS_BY_BAND else []
        return max(powers, default=DEFAULT_MAX_POWER.get(band, DEFAULT_MAX_POWER_OTHER))

    def _supports_dfs(self) -> bool:
        return any(hw.dfs for hw in self._capabilities.radio_hardware)

    def _supports_11ac(self) -> bool:
        return any(
            hw.vht or hw.he
            for hw in self._capabilities.radio_hardware
            if hw.band is not RadioBand.TWO_G
        )


DEV_CAP_SUFFIX_BY_BAND = {
    RadioBand.TWO_G: "2G",
    RadioBand.FIVE_G: "5G",
    RadioBand.FIVE_G2: "5G2",
    RadioBand.SIX_G: "6G",
}
# devCap.supportOFDMA bits seen in Controller 6.3.0.111 model templates:
# dual-band Wi-Fi 6/7 EAPs report 3, tri-band 2.4/5/6 GHz EAPs report 11.
OFDMA_BIT_BY_BAND = {
    RadioBand.TWO_G: 1,
    RadioBand.FIVE_G: 2,
    RadioBand.FIVE_G2: 4,
    RadioBand.SIX_G: 8,
}
# devCap.supportRadioMode* bitmasks seen in Controller 6.3.0.111 model
# templates, by the newest standard the radio supports: 2.4 GHz b/g/n EAPs
# (EAP110, EAP225) report 52 and Wi-Fi 6 EAPs (EAP650, EAP683 UR) 180; 5 GHz
# a/n EAPs (EAP115-Bridge) report 19, Wi-Fi 5 EAPs (EAP223, EAP235-Wall) 96 and
# Wi-Fi 6 EAPs (EAP683 UR, EAP655-WE) 368.
SUPPORT_RADIO_MODE = {
    (RadioBand.TWO_G, "ht"): 52,
    (RadioBand.TWO_G, "he"): 180,
    (RadioBand.FIVE_G, "ht"): 19,
    (RadioBand.FIVE_G, "vht"): 96,
    (RadioBand.FIVE_G, "he"): 368,
}


def _support_radio_mode(band: RadioBand, hardware: RadioHardware) -> int:
    if hardware.he:
        family = "he"
    elif hardware.vht and band is not RadioBand.TWO_G:
        family = "vht"
    else:
        family = "ht"
    return SUPPORT_RADIO_MODE.get((band, family), 0)


def _radio_cap_item(
    radio_id: int,
    *,
    max_ssids: int,
    hardware: RadioHardware | None = None,
    max_power: int = 20,
) -> dict[str, Any]:
    mimo, ofdma_on, mcs_level = 1, 0, 7
    if hardware is not None:
        mimo = max(1, hardware.streams)
        ofdma_on = 1 if hardware.he else 0
        mcs_level = 11 if hardware.he else 9 if hardware.vht else 7
    return {
        "radioId": radio_id,
        "minPow": 1,
        "maxPow": max_power,
        "maxPowOd": max_power,
        "limitMaxPow": max_power,
        "mimo": mimo,
        # ``ofdma`` = 1 makes the controller show the OFDMA checkbox (it sets
        # supportOFDMA<band> from this field). OpenWrt has no switch to turn
        # OFDMA off on Wi-Fi 6 radios, so hide the checkbox and report the
        # state the driver actually runs: on for 802.11ax, off otherwise.
        "ofdma": 0,
        "ofdmaEnable": ofdma_on,
        "mcsLevel": mcs_level,
        "supportSsidNum": max_ssids,
        "supportAntennaDirection": [],
        "supportMaxClient": 64,
        "supportMlo": False,
        "supportMaxAssocClient": 64,
        "supportAnteGainSetting": 0,
        "supportCSA": 0,
        "supportCSAWidth": 0,
        "supportMgtSsid": 0,
    }


def _usable_channels(hardware: RadioHardware) -> tuple[RadioChannel, ...]:
    # Radar channels only when the regulatory domain allows CAC; otherwise
    # hostapd disables the radio as soon as the controller picks one.
    return tuple(ch for ch in hardware.channels if hardware.dfs or not ch.radar)


def _channel_max_power(band: RadioBand, channel: RadioChannel) -> int:
    # ``iw phy`` reports the per-channel limit the driver will actually apply
    # (regulatory domain and hardware); fall back to a conservative default.
    if channel.max_power is None:
        return DEFAULT_MAX_POWER.get(band, DEFAULT_MAX_POWER_OTHER)
    return channel.max_power


def _width_flags(hardware: RadioHardware, channel: int, available: set[int]) -> int:
    flags = WIDTH_FLAG_20
    if hardware.band is RadioBand.TWO_G:
        if hardware.ht40:
            if channel + 4 in available:
                flags |= FLAG_HT40_UP
            if channel - 4 in available:
                flags |= FLAG_HT40_DOWN
        return flags

    # 5 GHz: 40 MHz pairs are (36,40), (44,48), ... and (149,153), (157,161), ...
    base = 36 if channel < 149 else 149
    upper = ((channel - base) // 4) % 2 == 0
    partner = channel + 4 if upper else channel - 4
    pair_ok = hardware.ht40 and partner in available
    if pair_ok:
        flags |= FLAG_HT40_UP if upper else FLAG_HT40_DOWN
    flags |= FLAG_5G_COMMON
    if hardware.vht or hardware.he:
        flags |= FLAG_VHT20
        if pair_ok:
            flags |= FLAG_VHT40_UP if upper else FLAG_VHT40_DOWN
        block = next(
            (start for start in FIVE_G_80_BLOCKS if start <= channel < start + 16),
            None,
        )
        if block is not None and all(block + 4 * i in available for i in range(4)):
            flags |= FLAG_80
        if hardware.vht160 or hardware.he160:
            wide = next(
                (start for start in FIVE_G_160_BLOCKS if start <= channel < start + 32),
                None,
            )
            if wide is not None and all(wide + 4 * i in available for i in range(8)):
                flags |= FLAG_160
    return flags


def _channel_detail(
    channel: int,
    freq: int,
    max_power: int,
    width_flag: int,
) -> dict[str, int]:
    return {
        "fr": freq,
        "vl": channel,
        "mPow": max_power,
        "cFlag": width_flag,
        "dFlag": 0,
        "lm": 1,
        "mPowOd": 0,
        "cFlagOd": 0,
    }
