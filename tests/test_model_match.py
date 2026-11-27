import json
import re
from dataclasses import replace
from pathlib import Path

import pytest

from open_omada_device_agent.adapters.outbound.openwrt.iw_phy import parse_iw_list
from open_omada_device_agent.adapters.outbound.openwrt.model_select import (
    generation,
    radio_facts,
    resolve_model,
)
from open_omada_device_agent.application.contracts import PlatformCapabilities
from open_omada_device_agent.contexts.device.model_match import RadioFacts, best_model
from open_omada_device_agent.contexts.device.omada_models_data import MODELS
from open_omada_device_agent.contexts.wireless.domain import RadioBand, RadioHardware

FIXTURE = Path(__file__).parent / "fixtures" / "iw_list_jio_6101_ax3000.txt"
SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _row(model, gen, *radios, hw="1.0"):
    return {
        "model": model, "model_version": hw, "hardware_version": hw, "gen": gen, "dfs": True, "regions": [],
        "radios": [
            {"band": b, "nss": n, "max_width": w, "min_tx": 5, "max_tx": p} for b, n, w, p in radios
        ],
    }


TABLE = (
    _row("AP-20", 4, ("2g", 2, 40, 20), hw="4.0"),
    _row("AP-AC", 5, ("2g", 2, 40, 20), ("5g", 2, 80, 23)),
    _row("AP-AX2", 6, ("2g", 2, 40, 20), ("5g", 2, 160, 27)),
    _row("AP-AX4", 6, ("2g", 4, 40, 26), ("5g", 4, 160, 26)),
    _row("AP-AX4 HP", 6, ("2g", 4, 40, 28), ("5g", 4, 160, 28)),
    _row("AP-AX4-Outdoor", 6, ("2g", 4, 40, 26), ("5g", 4, 160, 26)),
    _row("AP-BE", 7, ("2g", 2, 40, 20), ("5g", 2, 160, 25), ("6g", 2, 320, 25)),
)


def test_exact_match_and_power_breaks_ties():
    radios = [RadioFacts("2g", 4, 40, 26), RadioFacts("5g", 4, 160, 26)]
    match = best_model(6, radios, TABLE)
    assert match.model == "AP-AX4" and match.exact
    radios = [RadioFacts("2g", 4, 40, 28), RadioFacts("5g", 4, 160, 28)]
    assert best_model(6, radios, TABLE).model == "AP-AX4 HP"


def test_never_claims_more_than_the_hardware():
    # A 2x2 Wi-Fi 6 radio limited to 80 MHz: AP-AX2 would claim 160 MHz,
    # so the Wi-Fi 5 model (one generation below) fits better.
    match = best_model(6, [RadioFacts("2g", 2, 40), RadioFacts("5g", 2, 80)], TABLE)
    assert match.model == "AP-AC" and match.overclaim == 0 and match.shortfall == 1


def test_bands_must_match_and_variants_lose_ties():
    assert best_model(4, [RadioFacts("2g", 2, 40)], TABLE).model == "AP-20"
    assert best_model(6, [RadioFacts("6g", 2, 160)], TABLE) is None
    radios = [RadioFacts("2g", 4, 40), RadioFacts("5g", 4, 160)]
    assert best_model(6, radios, TABLE).model != "AP-AX4-Outdoor"


def test_jio_hardware_matches_eap683_ur_in_the_real_table():
    hardware = parse_iw_list(FIXTURE.read_text())
    facts = [radio_facts(h) for h in hardware]
    assert generation(hardware) == 6
    assert sorted((f.band, f.nss, f.max_width) for f in facts) == [("2g", 4, 40), ("5g", 4, 160)]
    match = best_model(generation(hardware), facts)
    assert (match.model, match.model_version, match.hardware_version, match.exact) == (
        "EAP683 UR", "1.0", "1.0", True)


def _settings(model, version="4.0"):
    from dataclasses import fields

    from open_omada_device_agent.application.settings import AgentSettings

    values = {f.name: None for f in fields(AgentSettings)}
    return AgentSettings(**{**values, "model": model, "model_version": version})


def _caps(hardware=None):
    return PlatformCapabilities(platform="openwrt", radio_hardware=hardware or ())


def test_auto_setting_uses_the_match_and_others_are_left_alone(caplog):
    caplog.set_level("INFO", logger="open_omada.model")
    caps = _caps(parse_iw_list(FIXTURE.read_text()))
    chosen = resolve_model(_settings("auto"), caps)
    assert (chosen.model, chosen.model_version, chosen.hardware_version) == ("EAP683 UR", "1.0", "1.0")
    fixed = _settings("EAP650", "2.0")
    assert resolve_model(fixed, caps) is fixed
    assert "Closest Omada model for these radios: EAP683 UR" in caplog.text
    assert resolve_model(_settings("auto"), _caps()).model == "EAP110"


def test_radio_facts_widths():
    two = RadioHardware(RadioBand.TWO_G, ht40=True, he=True, streams=2)
    five = RadioHardware(RadioBand.FIVE_G, ht40=True, vht=True, he=True, streams=2)
    assert radio_facts(two).max_width == 40
    assert radio_facts(five).max_width == 80
    assert radio_facts(replace(five, he160=True)).max_width == 160
    assert radio_facts(RadioHardware(RadioBand.TWO_G)).max_width == 20


def test_table_holds_only_the_allowed_facts():
    allowed_row = {"model", "model_version", "hardware_version", "regions", "gen", "dfs", "radios"}
    allowed_radio = {"band", "nss", "min_tx", "max_tx", "max_width"}
    assert len(MODELS) > 50
    for row in MODELS:
        assert set(row) == allowed_row
        assert all(set(radio) == allowed_radio for radio in row["radios"])
    text = (Path(__file__).resolve().parents[1] / "src/open_omada_device_agent/contexts/device/omada_models_data.py").read_text()
    text = text.split("MODELS", 1)[1]  # the header names the controller version
    assert not re.search(r"hwId|oemId|specialModel|firmware|\d+\.\d+\.\d+\.\d+|([0-9A-F]{2}-){5}", text)


def test_generator_reduces_a_template(tmp_path, monkeypatch):
    import importlib.util

    spec = importlib.util.spec_from_file_location("gen", SCRIPTS / "gen_omada_models.py")
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)
    adopt = {
        "deviceInfo": {"hwId": "X", "ip": "192.0.2.5", "modelVersion": "2.0", "hardwareVersion": "2.0"},
        "devCap": {"supportRadioMode2G": 180, "supportRadioMode5G": 368, "mimo2G": 2},
        "deviceMisc": {"supportDfs": 7},
        "radioCap": [{"radioId": 0, "mimo": 2, "minPow": 7, "maxPow": 20},
                     {"radioId": 1, "mimo": 2, "minPow": 6, "maxPow": 27}],
        "channelInfo": [
            {"radioId": 0, "band": "2.4G", "channelList": [{"cFlag": 0x700E0}]},
            {"radioId": 1, "band": "5G", "channelList": [{"cFlag": 0x1F70340}]},
        ],
    }
    record = {"model": "EAP-X(EU)", "hw_version": "2.0", "type": "ap", "adopt_resp": json.dumps(adopt)}
    other = dict(record, model="EAP-X(US)")
    rows = gen.build([record, other, {"type": "switch"}])
    assert rows == [{
        "model": "EAP-X", "model_version": "2.0", "hardware_version": "2.0", "regions": ["EU", "US"], "gen": 6, "dfs": True,
        "radios": [
            {"band": "2g", "nss": 2, "min_tx": 7, "max_tx": 20, "max_width": 40},
            {"band": "5g", "nss": 2, "min_tx": 6, "max_tx": 27, "max_width": 160},
        ],
    }]
    out = tmp_path / "data.py"
    monkeypatch.setattr(gen, "OUTPUT", out)
    (tmp_path / "in.jsonl").write_text(json.dumps(record) + "\n")
    assert gen.main(["gen", str(tmp_path / "in.jsonl"), "1.2.3"]) == 0
    namespace = {}
    exec(out.read_text(), namespace)
    assert namespace["MODELS"][0]["model"] == "EAP-X"


@pytest.mark.parametrize("value", ["auto", "Auto", " AUTO "])
def test_auto_is_case_insensitive(value):
    caps = _caps(parse_iw_list(FIXTURE.read_text()))
    assert resolve_model(_settings(value), caps).model == "EAP683 UR"


def _gen():
    import importlib.util

    spec = importlib.util.spec_from_file_location("gen", SCRIPTS / "gen_omada_models.py")
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)
    return gen


def test_generator_orders_versions_numerically_and_keeps_device_values():
    gen = _gen()
    assert gen.version_key("v1.0") < gen.version_key("1.20") < gen.version_key("2.0") < gen.version_key("10.0")

    def record(region, model_version, hardware_version, streams):
        adopt = {
            "deviceInfo": {"modelVersion": model_version, "hardwareVersion": hardware_version},
            "devCap": {"supportRadioMode2G": 180},
            "radioCap": [{"radioId": 0, "mimo": streams, "minPow": 1, "maxPow": 20}],
            "channelInfo": [{"radioId": 0, "band": "2.4G", "channelList": []}],
        }
        # hw_version is the template's own field; devices never send values like 1.6.7.
        return {"model": f"EAP-Y({region})", "hw_version": "1.6.7", "type": "ap",
                "adopt_resp": json.dumps(adopt)}

    rows = gen.build([record("EU", "2.0", "2.0", 4), record("US", "10.0", "10.0", 2),
                      record("JP", 1, 1, 1)])
    assert len(rows) == 1
    row = rows[0]
    assert (row["model_version"], row["hardware_version"]) == ("10.0", "10.0")
    assert row["radios"][0]["nss"] == 2  # facts of the newest version
    assert row["regions"] == ["EU", "JP", "US"]
    assert gen.reduce_template(record("JP", 1, 1, 1))["model_version"] == "1"
