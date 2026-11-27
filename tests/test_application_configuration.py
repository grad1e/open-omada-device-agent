from dataclasses import dataclass

from open_omada_device_agent.application.configuration import ApplyDeviceConfiguration
from open_omada_device_agent.domain import AccessPointConfigUpdate, LedConfig, RadioBand, RadioConfig


@dataclass
class Result:
    applied: bool = True
    changed: bool = False
    error: str = ""


class RecordingPort:
    def __init__(self, result=Result()):
        self.result = result
        self.calls = []

    def reconcile(self, update, capabilities):
        self.calls.append((update, capabilities))
        return self.result


def test_apply_configuration_selects_ports_by_configuration_intent():
    platform = RecordingPort(Result(changed=True))
    commands = RecordingPort()
    use_case = ApplyDeviceConfiguration(
        capability_detector=lambda: "host capabilities",
        platform_ports=(platform,),
        command_ports=(commands,),
    )

    result = use_case.execute(
        AccessPointConfigUpdate(
            sequence_id=1,
            config_version=2,
            config_version_inc=None,
            radios=(RadioConfig(RadioBand.TWO_G),),
        )
    )

    assert result.applied is True
    assert result.changed is True
    assert len(platform.calls) == 1
    assert commands.calls == []


def test_apply_configuration_stops_after_failed_adapter():
    failing = RecordingPort(Result(applied=False, error="platform rejected update"))
    later = RecordingPort()
    use_case = ApplyDeviceConfiguration(
        capability_detector=object,
        platform_ports=(),
        command_ports=(failing, later),
    )

    result = use_case.execute(
        AccessPointConfigUpdate(
            sequence_id=1,
            config_version=2,
            config_version_inc=None,
            led=LedConfig(enabled=True),
        )
    )

    assert result.applied is False
    assert result.error == "platform rejected update"
    assert later.calls == []


def test_apply_configuration_requires_lab_flag_for_ack_only_keys():
    use_case = ApplyDeviceConfiguration(
        capability_detector=object,
        platform_ports=(),
        command_ports=(),
    )

    result = use_case.execute(
        AccessPointConfigUpdate(
            sequence_id=1,
            config_version=2,
            config_version_inc=None,
            ack_only_keys=("ssh",),
        )
    )

    assert result.applied is False
    assert "OMADA_LAB_ACK_CONTROL_PLANE_CONFIG" in result.error


def test_apply_configuration_accepts_passive_keys_without_lab_flag():
    use_case = ApplyDeviceConfiguration(
        capability_detector=object,
        platform_ports=(),
        command_ports=(),
    )

    result = use_case.execute(
        AccessPointConfigUpdate(
            sequence_id=1,
            config_version=2,
            config_version_inc=None,
            passive_keys=("wirelessAdv_2G",),
        )
    )

    assert result.applied is True


def test_apply_configuration_applies_supported_domains_before_ack_only_failure():
    platform = RecordingPort(Result(changed=True))
    use_case = ApplyDeviceConfiguration(
        capability_detector=lambda: "host capabilities",
        platform_ports=(platform,),
        command_ports=(),
    )

    result = use_case.execute(
        AccessPointConfigUpdate(
            sequence_id=1,
            config_version=2,
            config_version_inc=None,
            radios=(RadioConfig(RadioBand.TWO_G),),
            ack_only_keys=("ssh",),
        )
    )

    assert result.applied is False
    assert result.changed is True
    assert len(platform.calls) == 1
    assert "supported domains were applied first" in result.error


def test_apply_configuration_propagates_successful_partial_adapter_warning():
    platform = RecordingPort(Result(changed=True, error="partial platform apply"))
    use_case = ApplyDeviceConfiguration(
        capability_detector=lambda: "host capabilities",
        platform_ports=(platform,),
        command_ports=(),
    )

    result = use_case.execute(
        AccessPointConfigUpdate(
            sequence_id=1,
            config_version=2,
            config_version_inc=None,
            radios=(RadioConfig(RadioBand.TWO_G),),
        )
    )

    assert result.applied is False
    assert result.changed is True
    assert result.error == "partial platform apply"


def test_apply_configuration_accepts_ack_only_keys_when_lab_flag_is_enabled():
    use_case = ApplyDeviceConfiguration(
        capability_detector=object,
        platform_ports=(),
        command_ports=(),
        allow_ack_only_config=True,
    )

    result = use_case.execute(
        AccessPointConfigUpdate(
            sequence_id=1,
            config_version=2,
            config_version_inc=None,
            ack_only_keys=("ssh",),
        )
    )

    assert result.applied is True


def test_apply_configuration_applies_supported_domains_before_unsupported_key_failure():
    # Removing a MAC filter sends macFilterAssoc in the same SET as the SSID
    # change; the SSID change must still be applied.
    platform = RecordingPort(Result(changed=True))
    use_case = ApplyDeviceConfiguration(
        capability_detector=lambda: "host capabilities",
        platform_ports=(platform,),
        command_ports=(),
    )

    result = use_case.execute(
        AccessPointConfigUpdate(
            sequence_id=1,
            config_version=2,
            config_version_inc=None,
            radios=(RadioConfig(RadioBand.TWO_G),),
            unhandled_keys=("macFilterAssoc",),
        )
    )

    assert result.applied is False
    assert result.changed is True
    assert len(platform.calls) == 1
    assert result.error == "unsupported keys: macFilterAssoc; supported domains were applied first"


def test_apply_configuration_reports_unsupported_keys_alone():
    use_case = ApplyDeviceConfiguration(
        capability_detector=object,
        platform_ports=(),
        command_ports=(),
    )

    result = use_case.execute(
        AccessPointConfigUpdate(
            sequence_id=1,
            config_version=2,
            config_version_inc=None,
            unhandled_keys=("schedulerList", "macFilterList"),
        )
    )

    assert result.applied is False
    assert result.changed is False
    assert result.error == "unsupported keys: schedulerList,macFilterList"


def test_apply_configuration_reports_adapter_failure_and_unsupported_keys():
    failing = RecordingPort(Result(applied=False, error="platform rejected update"))
    use_case = ApplyDeviceConfiguration(
        capability_detector=object,
        platform_ports=(failing,),
        command_ports=(),
    )

    result = use_case.execute(
        AccessPointConfigUpdate(
            sequence_id=1,
            config_version=2,
            config_version_inc=None,
            radios=(RadioConfig(RadioBand.TWO_G),),
            unhandled_keys=("macFilterAssoc",),
        )
    )

    assert result.applied is False
    assert result.error == "platform rejected update; unsupported keys: macFilterAssoc"


class PreflightPort(RecordingPort):
    def __init__(self, error=None):
        super().__init__(Result(changed=True))
        self.error = error

    def preflight(self, update, capabilities):
        return self.error


def test_apply_configuration_preflight_error_stops_before_any_port_runs():
    # The portal check lives on a later port, but the SSID port must not run.
    ssid_port = RecordingPort(Result(changed=True))
    portal_port = PreflightPort(error="openNDS gateway is the uplink")
    use_case = ApplyDeviceConfiguration(
        capability_detector=lambda: "host capabilities",
        platform_ports=(ssid_port, portal_port),
        command_ports=(),
    )

    result = use_case.execute(
        AccessPointConfigUpdate(
            sequence_id=1,
            config_version=2,
            config_version_inc=None,
            radios=(RadioConfig(RadioBand.TWO_G),),
            unhandled_keys=("macFilterAssoc",),
        )
    )

    assert result.applied is False
    assert result.changed is False
    assert result.error == "openNDS gateway is the uplink; unsupported keys: macFilterAssoc"
    assert ssid_port.calls == [] and portal_port.calls == []


def test_apply_configuration_runs_ports_when_preflight_passes():
    portal_port = PreflightPort(error=None)
    use_case = ApplyDeviceConfiguration(
        capability_detector=lambda: "host capabilities",
        platform_ports=(portal_port,),
        command_ports=(),
    )

    result = use_case.execute(
        AccessPointConfigUpdate(
            sequence_id=1,
            config_version=2,
            config_version_inc=None,
            radios=(RadioConfig(RadioBand.TWO_G),),
        )
    )

    assert result.applied is True
    assert len(portal_port.calls) == 1

