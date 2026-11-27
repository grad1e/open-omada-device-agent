"""MAC filtering from the SSID editor -> hostapd macfilter/maclist."""
from open_omada_device_agent.adapters.outbound.openwrt.uci import OpenWrtUciAdapter
from open_omada_device_agent.ap_config import parse_config_body
from open_omada_device_agent.contexts.wireless.domain import (
    MacFilterAssocRule, MacFilterChange, MacFilterListRule, MacFilterState,
)
from open_omada_device_agent.domain import RadioBand
from open_omada_device_agent.openwrt import CommandResult, build_uci_batch
from open_omada_device_agent.platform_capabilities import PlatformCapabilities

SSID_ID = 1001
LIST_ID = 2002
MAC1, MAC2 = "02-AA-BB-CC-DD-01", "02-AA-BB-CC-DD-02"


def _caps():
    return PlatformCapabilities(
        platform="openwrt",
        radio_bands=(RadioBand.TWO_G, RadioBand.FIVE_G),
        max_ssids=4,
        supports_wlan_config=True,
        supports_wpa2_psk=True,
    )


def _group(*macs, op=1):
    rules = [{"operation": op, "listId": LIST_ID}]
    rules += [{"operation": op, "ruleId": i, "mac": mac, "listId": LIST_ID} for i, mac in enumerate(macs, 1)]
    return {"rule": rules}


def _assoc(action=0, op=1, bands=(0, 1), list_id=LIST_ID, type_=0):
    return {
        "rule": [
            {"operation": op, "ssidId": SSID_ID, "band": band, "listId": list_id, "action": action, "type": type_}
            for band in bands
        ]
    }


def _ssid_body(**extra):
    item = {"id": SSID_ID, "index": 1, "ssidName": "lab", "pskKey": "network-passphrase", "pskVer": 2}
    return {"ssid_2G": {"radioId": 0, "ssid": [item]}, **extra}


def _filter_lines(batch, section="openomada_2g_1"):
    return [line for line in batch if f".{section}.mac" in line]


# --- state --------------------------------------------------------------

def _state(**body):
    change = parse_config_body(body).mac_filter
    return MacFilterState().apply(change, full_sync=True)


def test_deny_list_policy():
    state = _state(macFilterGlobal={"enable": True}, macFilterList=_group(MAC1, MAC2), macFilterAssoc=_assoc())

    policy = state.policy(SSID_ID, 0)

    assert policy is not None and policy.allow is False
    assert policy.macs == (MAC1, MAC2)


def test_global_switch_off_means_no_filter():
    state = _state(macFilterGlobal={"enable": False}, macFilterList=_group(MAC1), macFilterAssoc=_assoc())

    assert state.policy(SSID_ID, 0) is None


def test_incremental_changes_follow_the_controller():
    state = _state(macFilterGlobal={"enable": True}, macFilterList=_group(MAC1, MAC2), macFilterAssoc=_assoc())

    # Deny -> Allow: delete all links for the SSID, add the new one.
    change = MacFilterChange(
        assoc_rules=(
            MacFilterAssocRule(operation=2, ssid_id=SSID_ID, band=0, list_id=-1, action=1),
            MacFilterAssocRule(operation=1, ssid_id=SSID_ID, band=0, list_id=LIST_ID, action=1),
        )
    )
    state = state.apply(change, full_sync=False)
    assert state.policy(SSID_ID, 0).allow is True

    # Remove one MAC (operation 2), keep the other (operation 3).
    change = MacFilterChange(
        list_rules=(
            MacFilterListRule(operation=2, list_id=LIST_ID, rule_id=2, mac=MAC2),
            MacFilterListRule(operation=3, list_id=LIST_ID, rule_id=1, mac=MAC1),
        )
    )
    state = state.apply(change, full_sync=False)
    assert state.policy(SSID_ID, 0).macs == (MAC1,)

    # Filter off for the SSID.
    change = MacFilterChange(
        assoc_rules=(MacFilterAssocRule(operation=2, ssid_id=SSID_ID, band=0, list_id=-1, action=0),)
    )
    state = state.apply(change, full_sync=False)
    assert state.policy(SSID_ID, 0) is None
    assert state.policy(SSID_ID, 1) is not None


def test_full_sync_replaces_the_old_state():
    state = _state(macFilterGlobal={"enable": True}, macFilterList=_group(MAC1), macFilterAssoc=_assoc())

    state = state.apply(parse_config_body({"macFilterGlobal": {"enable": True}}).mac_filter, full_sync=True)

    assert state.policy(SSID_ID, 0) is None
    assert state.lists == {}


# --- UCI ----------------------------------------------------------------

def test_ssid_gets_macfilter_and_maclist():
    state = _state(macFilterGlobal={"enable": True}, macFilterList=_group(MAC1, MAC2), macFilterAssoc=_assoc(action=1))

    batch = build_uci_batch(parse_config_body(_ssid_body()), _caps(), mac_filter=state)

    assert _filter_lines(batch) == [
        "set wireless.openomada_2g_1.macfilter='allow'",
        "add_list wireless.openomada_2g_1.maclist='02:aa:bb:cc:dd:01'",
        "add_list wireless.openomada_2g_1.maclist='02:aa:bb:cc:dd:02'",
    ]


def test_ssid_without_a_filter_writes_nothing():
    batch = build_uci_batch(parse_config_body(_ssid_body()), _caps(), mac_filter=MacFilterState())

    assert _filter_lines(batch) == []


def test_filter_change_rewrites_ssids_not_in_the_set():
    state = _state(macFilterGlobal={"enable": True}, macFilterList=_group(MAC1), macFilterAssoc=_assoc())
    wireless_state = {
        "openomada_5g_1": {".type": "wifi-iface", "openomada_managed": "1", "openomada_ssid_id": str(SSID_ID)},
        "default_radio0": {".type": "wifi-iface"},
    }
    update = parse_config_body({"macFilterList": _group(MAC1)})

    batch = build_uci_batch(
        update, _caps(), mac_filter=state, mac_filter_changed=True, wireless_state=wireless_state
    )

    assert _filter_lines(batch, "openomada_5g_1") == [
        "delete wireless.openomada_5g_1.macfilter",
        "delete wireless.openomada_5g_1.maclist",
        "set wireless.openomada_5g_1.macfilter='deny'",
        "add_list wireless.openomada_5g_1.maclist='02:aa:bb:cc:dd:01'",
    ]
    assert not any("default_radio0" in line for line in batch)
    assert batch[-1] == "commit wireless"


# --- adapter ------------------------------------------------------------

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


def test_adapter_keeps_state_between_sets():
    runner = _Runner()
    adapter = OpenWrtUciAdapter(runner)
    full = parse_config_body(
        {
            "configVersion": 5,
            "macFilterGlobal": {"enable": True},
            "macFilterList": _group(MAC1),
            "macFilterAssoc": _assoc(),
            **_ssid_body(),
        }
    )
    assert adapter.reconcile(full, _caps()).applied

    # A later SET only touches the SSID: the filter stays.
    later = parse_config_body({"configVersionInc": 1, **_ssid_body()})
    assert adapter.reconcile(later, _caps()).applied

    assert "macfilter='deny'" in _batches(runner)[-1]


def test_oui_profiles_and_unknown_codes_are_refused():
    adapter = OpenWrtUciAdapter(_Runner())
    for body in (
        {"macFilterAssoc": _assoc(type_=1)},
        {"macFilterAssoc": _assoc(op=7)},
        {"macFilterAssoc": _assoc(bands=(3,))},
        {"macFilterList": {"rule": [{"operation": 1, "ruleId": 1, "mac": "not-a-mac", "listId": 1}]}},
    ):
        result = adapter.reconcile(parse_config_body({"configVersionInc": 1, **body}), _caps())
        assert not result.applied


def test_refused_change_does_not_alter_state():
    runner = _Runner()
    adapter = OpenWrtUciAdapter(runner)
    adapter.reconcile(
        parse_config_body(
            {"configVersion": 5, "macFilterGlobal": {"enable": True}, "macFilterList": _group(MAC1),
             "macFilterAssoc": _assoc(), **_ssid_body()}
        ),
        _caps(),
    )
    adapter.reconcile(parse_config_body({"configVersionInc": 1, "macFilterAssoc": _assoc(type_=1, op=1)}), _caps())
    adapter.reconcile(parse_config_body({"configVersionInc": 1, **_ssid_body()}), _caps())

    assert "macfilter='deny'" in _batches(runner)[-1]
