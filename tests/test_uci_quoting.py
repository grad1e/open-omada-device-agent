"""Values in ``uci batch`` lines read back unchanged by libuci."""
import pytest

from open_omada_device_agent.adapters.inbound.ecsp.mappers.configuration import parse_config_body
from open_omada_device_agent.adapters.outbound.openwrt.snmp import build_snmp_batch, parse_snmpd_show
from open_omada_device_agent.adapters.outbound.openwrt.uci import build_uci_batch, uci_quote
from open_omada_device_agent.application.contracts import PlatformCapabilities
from open_omada_device_agent.domain import RadioBand

# Made-up values with a backslash and a single quote.
TRICKY = "lab\\net's"
FT_KEY = "00112233445566778899aabbccdd\\'ff"  # not hex: only the quoting is tested


def libuci_value(line: str) -> str:
    """Value of a ``set``/``add_list`` batch line as libuci parses it.

    Mimics uci/file.c: inside '...' everything is literal; inside "..." and
    outside quotes a backslash escapes the next character.
    """
    raw = line.split("=", 1)[1]
    value: list[str] = []
    quote = None
    i = 0
    while i < len(raw):
        char = raw[i]
        if quote == "'":
            if char == "'":
                quote = None
            else:
                value.append(char)
        elif char == "\\" and i + 1 < len(raw):
            i += 1
            value.append(raw[i])
        elif char == quote:
            quote = None
        elif quote is None and char in "'\"":
            quote = char
        else:
            value.append(char)
        i += 1
    assert quote is None, f"unterminated quote in {line!r}"
    return "".join(value)


def _line(lines, prefix):
    matches = [line for line in lines if line.startswith(prefix)]
    assert len(matches) == 1, (prefix, lines)
    return matches[0]


def _caps():
    return PlatformCapabilities(
        platform="openwrt",
        radio_bands=(RadioBand.TWO_G, RadioBand.FIVE_G),
        max_ssids=4,
        supports_wlan_config=True,
        supports_wpa2_psk=True,
        supports_wpa3_psk=True,
        supports_80211r=True,
    )


@pytest.mark.parametrize("value", ["plain", "a'b", "a\\b", "\\'", "'", "\\", "x\\\\'y'z"])
def test_uci_quote_round_trips(value):
    assert libuci_value(f"set wireless.s.o='{uci_quote(value)}'") == value


def test_uci_quote_leaves_backslashes_alone():
    assert uci_quote("pa\\ss") == "pa\\ss"


def test_ssid_wpa_key_and_80211r_key_read_back_unchanged():
    update = parse_config_body(
        {
            "ssid_2G": {
                "radioId": 0,
                "ssid": [
                    {
                        "index": 1,
                        "ssidName": TRICKY,
                        "pskKey": TRICKY,
                        "pskVer": 4,
                        "pskCipher": 3,
                        "fastTransition": {"enable11r": True, "mobilityDomain": "A4CB", "keyRrb": FT_KEY},
                    }
                ],
            }
        }
    )

    batch = build_uci_batch(update, _caps())

    assert libuci_value(_line(batch, "set wireless.openomada_2g_1.ssid=")) == TRICKY
    assert libuci_value(_line(batch, "set wireless.openomada_2g_1.key=")) == TRICKY
    assert libuci_value(_line(batch, "add_list wireless.openomada_2g_1.r0kh=")) == f"ff:ff:ff:ff:ff:ff,*,{FT_KEY}"
    assert (
        libuci_value(_line(batch, "add_list wireless.openomada_2g_1.r1kh="))
        == f"00:00:00:00:00:00,00:00:00:00:00:00,{FT_KEY}"
    )


def test_snmp_community_reads_back_unchanged():
    update = parse_config_body(
        {
            "snmp": {
                "location": "Lab rack",
                "contact": "ops@example.com",
                "v1v2cEnable": True,
                "community": TRICKY,
                "v3Enable": False,
            }
        }
    )
    current = parse_snmpd_show("snmpd.public=com2sec\nsnmpd.public.community='public'\n")

    batch = build_snmp_batch(update.snmp, current)

    assert libuci_value(_line(batch, "set snmpd.public.community=")) == TRICKY
