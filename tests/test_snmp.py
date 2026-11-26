from open_omada_device_agent.adapters.inbound.ecsp.mappers.configuration import (
    parse_config_body,
)
from open_omada_device_agent.adapters.outbound.openwrt.capabilities import (
    detect_platform_capabilities,
)
from open_omada_device_agent.adapters.outbound.openwrt.snmp import (
    OpenWrtSnmpAdapter,
    build_snmp_batch,
    parse_snmpd_show,
)
from open_omada_device_agent.adapters.outbound.openwrt.uci import CommandResult
from open_omada_device_agent.application.contracts import PlatformCapabilities
from open_omada_device_agent.capabilities import ap_components_v2

# Trimmed from the OpenWrt snmpd package defaults.
DEFAULT_SHOW = """\
snmpd.@agent[0]=agent
snmpd.public=com2sec
snmpd.public.secname='ro'
snmpd.public.source='default'
snmpd.public.community='public'
snmpd.public6=com2sec6
snmpd.public6.secname='ro'
snmpd.public6.source='default'
snmpd.public6.community='public'
snmpd.@system[0]=system
snmpd.@system[0].sysLocation='office'
snmpd.@system[0].sysContact='bofh@example.com'
snmpd.@system[0].sysName='HeartOfGold'
snmpd.general=snmpd
snmpd.general.enabled='1'
"""


def _snmp(**fields):
    body = {
        "location": "Lab rack",
        "contact": "ops@example.com",
        "v1v2cEnable": True,
        "community": "s3cret",
        "v3Enable": False,
        "v3Username": "",
        "v3Password": "",
        "v3SecurityLevel": 1,
        "v3AuthMode": 1,
        "v3PrivacyMode": 1,
        "v3PrivacyPassword": "",
    }
    body.update(fields)
    return parse_config_body({"snmp": body})


def _caps(**overrides):
    values = {"platform": "openwrt", "supports_snmp": True}
    values.update(overrides)
    return PlatformCapabilities(**values)


class _Runner:
    def __init__(self, show=DEFAULT_SHOW, enabled=False, batch_rc=0):
        self.show = show
        self.enabled = enabled
        self.batch_rc = batch_rc
        self.calls = []
        self.batch = None

    def run(self, args, *, input_text=None):
        self.calls.append(list(args))
        if args[:3] == ["uci", "-q", "show"]:
            return CommandResult(0, self.show)
        if args[:3] == ["uci", "-q", "batch"]:
            self.batch = input_text
            return CommandResult(self.batch_rc)
        if args[-1] == "enabled":
            return CommandResult(0 if self.enabled else 1)
        return CommandResult(0)


def test_mapper_parses_snmp_and_hides_secrets():
    update = _snmp(v3Enable=True, v3Username="mon", v3Password="authpass1")

    assert update.unhandled_keys == () and "snmp" not in update.passive_keys
    assert update.snmp.location == "Lab rack"
    assert update.snmp.community.reveal() == "s3cret"
    assert update.snmp.v3_password.reveal() == "authpass1"
    assert "s3cret" not in repr(update.snmp) and "authpass1" not in repr(update.snmp)


def test_v1v2c_batch_sets_community_location_and_enables():
    update = _snmp()
    lines = build_snmp_batch(update.snmp, parse_snmpd_show(DEFAULT_SHOW))

    assert "set snmpd.public.community='s3cret'" in lines
    assert "set snmpd.public6.community='s3cret'" in lines
    assert "set snmpd.@system[0].sysLocation='Lab rack'" in lines
    assert "set snmpd.@system[0].sysContact='ops@example.com'" in lines
    assert "delete snmpd.@system[0].sysName" in lines
    assert "set snmpd.general.snmp_version='v1/v2c'" in lines
    assert not any("openomada_v3" in line for line in lines)
    assert not any("general.enabled" in line for line in lines)  # already 1


def test_v3_auth_priv_section_and_v1v2c_removed():
    update = _snmp(
        v1v2cEnable=False,
        v3Enable=True,
        v3Username="mon",
        v3SecurityLevel=2,
        v3AuthMode=2,
        v3Password="authpass1",
        v3PrivacyMode=2,
        v3PrivacyPassword="privpass1",
    )
    lines = build_snmp_batch(update.snmp, parse_snmpd_show(DEFAULT_SHOW))

    assert "delete snmpd.public" in lines and "delete snmpd.public6" in lines
    assert "set snmpd.openomada_v3=v3" in lines
    assert "set snmpd.openomada_v3.auth_type='SHA'" in lines
    assert "set snmpd.openomada_v3.privacy_type='AES'" in lines
    assert "set snmpd.openomada_v3.allow_write='0'" in lines
    assert "set snmpd.general.snmp_version='v1/v2c/v3'" in lines


def test_v3_auth_no_priv_drops_old_privacy_options():
    show = DEFAULT_SHOW + (
        "snmpd.openomada_v3=v3\n"
        "snmpd.openomada_v3.username='mon'\n"
        "snmpd.openomada_v3.allow_write='0'\n"
        "snmpd.openomada_v3.auth_type='MD5'\n"
        "snmpd.openomada_v3.auth_pass='authpass1'\n"
        "snmpd.openomada_v3.privacy_type='DES'\n"
        "snmpd.openomada_v3.privacy_pass='privpass1'\n"
    )
    update = _snmp(v3Enable=True, v3Username="mon", v3Password="authpass1")
    lines = build_snmp_batch(update.snmp, parse_snmpd_show(show))

    v3_lines = [line for line in lines if "openomada_v3" in line]
    assert v3_lines == [
        "delete snmpd.openomada_v3.privacy_type",
        "delete snmpd.openomada_v3.privacy_pass",
    ]


def test_reconcile_writes_commits_and_starts_snmpd():
    runner = _Runner(enabled=False)
    result = OpenWrtSnmpAdapter(runner=runner).reconcile(_snmp(), _caps())

    assert result.applied and result.changed
    assert runner.batch.rstrip().endswith("commit snmpd")
    assert ["/etc/init.d/snmpd", "enable"] in runner.calls
    assert ["/etc/init.d/snmpd", "restart"] in runner.calls


def test_reconcile_turning_everything_off_stops_and_disables():
    runner = _Runner(enabled=True)
    result = OpenWrtSnmpAdapter(runner=runner).reconcile(_snmp(v1v2cEnable=False), _caps())

    assert result.applied
    assert "set snmpd.general.enabled='0'" in runner.batch
    assert ["/etc/init.d/snmpd", "stop"] in runner.calls
    assert ["/etc/init.d/snmpd", "disable"] in runner.calls
    assert ["/etc/init.d/snmpd", "restart"] not in runner.calls


def test_reconcile_is_idempotent_when_config_matches():
    runner = _Runner(enabled=True)
    adapter = OpenWrtSnmpAdapter(runner=runner)
    adapter.reconcile(_snmp(), _caps())
    applied_show = (
        DEFAULT_SHOW.replace("'public'", "'s3cret'")
        .replace("'office'", "'Lab rack'")
        .replace("'bofh@example.com'", "'ops@example.com'")
        .replace("snmpd.@system[0].sysName='HeartOfGold'\n", "")
        + "snmpd.general.snmp_version='v1/v2c'\n"
    )

    again = _Runner(show=applied_show, enabled=True)
    result = OpenWrtSnmpAdapter(runner=again).reconcile(_snmp(), _caps())

    assert result.applied and not result.changed
    assert again.batch is None
    assert ["/etc/init.d/snmpd", "restart"] not in again.calls


def test_short_v3_password_is_rejected_before_any_change():
    runner = _Runner()
    result = OpenWrtSnmpAdapter(runner=runner).reconcile(
        _snmp(v3Enable=True, v3Username="mon", v3Password="short"), _caps()
    )

    assert not result.applied and "8 characters" in result.error
    assert runner.calls == []


def test_without_snmpd_the_setting_is_accepted_but_not_applied():
    runner = _Runner()
    result = OpenWrtSnmpAdapter(runner=runner).reconcile(_snmp(), _caps(supports_snmp=False))

    assert result.applied and not result.changed and runner.calls == []


def test_quoted_values_round_trip():
    show = "snmpd.@system[0]=system\nsnmpd.@system[0].sysLocation='Bob'\\''s desk'\n"
    assert parse_snmpd_show(show)["@system[0]"]["sysLocation"] == "Bob's desk"


def test_capability_follows_snmpd_binary_and_component_is_advertised():
    def which(found):
        return lambda name: f"/usr/bin/{name}" if name in found else None

    base = {"uci", "ubus"}
    caps = detect_platform_capabilities(env={}, command_exists=which(base | {"snmpd"}))
    assert caps.supports_snmp
    components = ap_components_v2(caps)
    assert components["snmp"] == "1.0"
    # service >= 1.1 makes the controller send the full SNMPv3 settings.
    assert components["service"] == "1.1"

    without = detect_platform_capabilities(env={}, command_exists=which(base))
    assert not without.supports_snmp
    assert "snmp" not in ap_components_v2(without)
    assert "service" not in ap_components_v2(without)


def test_v3_without_level_fields_defaults_to_auth_no_priv_md5():
    # What the controller sends when the AP lacks service >= 1.1.
    update = parse_config_body(
        {"snmp": {"v1v2cEnable": 0, "v3Enable": 1, "v3Username": "mon", "v3Password": "authpass1"}}
    )
    lines = build_snmp_batch(update.snmp, parse_snmpd_show(DEFAULT_SHOW))

    assert "set snmpd.openomada_v3.auth_type='MD5'" in lines
    assert not any("privacy" in line for line in lines)
