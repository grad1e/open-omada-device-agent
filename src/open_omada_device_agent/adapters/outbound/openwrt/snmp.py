"""Config -> Services -> SNMP: write /etc/config/snmpd for net-snmp.

The controller sends SET key ``snmp`` with Location, Contact, the SNMPv1/v2c
community and an optional SNMPv3 user. Access is read-only, as on an EAP.
snmpd runs only while v1/v2c or v3 is on; otherwise it is stopped and disabled.

UCI layout (OpenWrt ``snmpd`` package):
- ``@system[0]`` sysLocation/sysContact (sysName is dropped so the hostname is used)
- ``public``/``public6`` com2sec sections carry the community; deleted when
  v1/v2c is off
- ``openomada_v3`` v3 section; ``general.snmp_version`` must include v3 for
  the init script to apply it
"""
from __future__ import annotations

import logging
import re
from collections.abc import Mapping

from ....application.commands import ApplyDeviceConfigurationCommand
from ....application.contracts import PlatformCapabilities
from ....contexts.device.domain import (
    SNMP_AUTH_MODES,
    SNMP_PRIVACY_MODES,
    SnmpConfig,
)
from .uci import CommandRunner, ReconciliationResult, SubprocessRunner

log = logging.getLogger("open_omada.snmp")

V3_SECTION = "openomada_v3"
MIN_V3_PASSWORD = 8  # net-snmp rejects shorter auth/privacy passphrases
_SECRET_KEYS = {"community", "v3Password", "v3PrivacyPassword"}
_SHOW_LINE = re.compile(r"^snmpd\.([^.=]+)(?:\.([^=]+))?=(.*)$")

Sections = Mapping[str, Mapping[str, str]]


def validate_snmp(config: SnmpConfig) -> str | None:
    if config.v1v2c_enabled and config.community is None:
        return "SNMP v1/v2c needs a community"
    if not config.v3_enabled:
        return None
    if not config.v3_username:
        return "SNMPv3 needs a username"
    if config.v3_security_level >= 1:
        if config.v3_auth_mode not in SNMP_AUTH_MODES:
            return f"unknown SNMPv3 auth mode {config.v3_auth_mode}"
        if config.v3_password is None or len(config.v3_password.reveal()) < MIN_V3_PASSWORD:
            return f"SNMPv3 auth password needs at least {MIN_V3_PASSWORD} characters"
    if config.v3_security_level >= 2:
        if config.v3_privacy_mode not in SNMP_PRIVACY_MODES:
            return f"unknown SNMPv3 privacy mode {config.v3_privacy_mode}"
        password = config.v3_privacy_password
        if password is None or len(password.reveal()) < MIN_V3_PASSWORD:
            return f"SNMPv3 privacy password needs at least {MIN_V3_PASSWORD} characters"
    return None


def desired_sections(config: SnmpConfig) -> dict[str, dict[str, str] | None]:
    """Sections the agent owns; None means the section must not exist."""
    sections: dict[str, dict[str, str] | None] = {}
    if config.v1v2c_enabled and config.community is not None:
        community = config.community.reveal()
        for name, kind in (("public", "com2sec"), ("public6", "com2sec6")):
            sections[name] = {
                ".type": kind,
                "secname": "ro",
                "source": "default",
                "community": community,
            }
    else:
        sections["public"] = None
        sections["public6"] = None

    if config.v3_enabled:
        v3 = {".type": "v3", "username": config.v3_username, "allow_write": "0"}
        if config.v3_security_level >= 1 and config.v3_password is not None:
            v3["auth_type"] = SNMP_AUTH_MODES[config.v3_auth_mode]
            v3["auth_pass"] = config.v3_password.reveal()
        if config.v3_security_level >= 2 and config.v3_privacy_password is not None:
            v3["privacy_type"] = SNMP_PRIVACY_MODES[config.v3_privacy_mode]
            v3["privacy_pass"] = config.v3_privacy_password.reveal()
        sections[V3_SECTION] = v3
    else:
        sections[V3_SECTION] = None
    return sections


def build_snmp_batch(config: SnmpConfig, current: Sections) -> list[str]:
    """Return ``uci batch`` lines that move ``current`` to ``config``."""
    lines: list[str] = []
    for name, wanted in desired_sections(config).items():
        have = current.get(name)
        if wanted is None:
            if have is not None:
                lines.append(f"delete snmpd.{name}")
            continue
        if have is None or have.get(".type") != wanted[".type"]:
            if have is not None:
                lines.append(f"delete snmpd.{name}")
            have = {}
            lines.append(f"set snmpd.{name}={wanted['.type']}")
        for option, value in wanted.items():
            if option != ".type" and have.get(option) != value:
                lines.append(_set(name, option, value))
        for option in have:
            if option != ".type" and option not in wanted:
                lines.append(f"delete snmpd.{name}.{option}")

    system = current.get("@system[0]")
    if system is None:
        lines.append("add snmpd system")
        system = {}
    for option, value in (("sysLocation", config.location), ("sysContact", config.contact)):
        if system.get(option, "") != value:
            lines.append(_set("@system[0]", option, value))
    if "sysName" in system:
        # The package default is a placeholder; without it snmpd uses the hostname.
        lines.append("delete snmpd.@system[0].sysName")

    general = current.get("general")
    if general is None or general.get(".type") != "snmpd":
        lines.append("set snmpd.general=snmpd")
        general = {}
    version = "v1/v2c/v3" if config.v3_enabled else "v1/v2c"
    for option, value in (("enabled", "1" if config.enabled else "0"), ("snmp_version", version)):
        if general.get(option) != value:
            lines.append(_set("general", option, value))
    return lines


def parse_snmpd_show(text: str) -> dict[str, dict[str, str]]:
    sections: dict[str, dict[str, str]] = {}
    for line in text.splitlines():
        match = _SHOW_LINE.match(line.strip())
        if match is None:
            continue
        name, option, value = match.groups()
        section = sections.setdefault(name, {})
        section[option or ".type"] = _unquote(value)
    return sections


class OpenWrtSnmpAdapter:
    """Command port for SET key ``snmp``."""

    def __init__(
        self,
        *,
        runner: CommandRunner | None = None,
        init_script: str = "/etc/init.d/snmpd",
    ) -> None:
        self._runner = runner or SubprocessRunner()
        self._init_script = init_script

    def reconcile(
        self,
        update: ApplyDeviceConfigurationCommand,
        capabilities: PlatformCapabilities,
    ) -> ReconciliationResult:
        config = update.snmp
        if config is None:
            return ReconciliationResult(applied=True, changed=False)
        if not capabilities.supports_snmp:
            # Same as before SNMP support: keep the setting on the controller only.
            log.info("SNMP settings received but snmpd is not installed; not applied")
            return ReconciliationResult(applied=True, changed=False)
        raw = update.raw_body.get("snmp")
        if isinstance(raw, Mapping):
            log.debug(
                "SNMP SET body: %s",
                {k: ("***" if k in _SECRET_KEYS and v else v) for k, v in raw.items()},
            )
        error = validate_snmp(config)
        if error:
            return ReconciliationResult(applied=False, changed=False, error=error)

        shown = self._runner.run(["uci", "-q", "show", "snmpd"])
        batch = build_snmp_batch(config, parse_snmpd_show(shown.stdout))
        if batch:
            result = self._runner.run(
                ["uci", "-q", "batch"], input_text="\n".join([*batch, "commit snmpd"]) + "\n"
            )
            if result.returncode != 0:
                self._runner.run(["uci", "revert", "snmpd"])
                return ReconciliationResult(
                    applied=False, changed=False, error="uci batch for snmpd failed"
                )

        service_changed = self._apply_service(config.enabled, restart=bool(batch))
        log.info(
            "SNMP %s (v1/v2c=%s, v3=%s level=%s auth=%s privacy=%s, location=%r, contact=%r)",
            "enabled" if config.enabled else "disabled",
            config.v1v2c_enabled,
            config.v3_enabled,
            config.v3_security_level,
            config.v3_auth_mode,
            config.v3_privacy_mode,
            config.location,
            config.contact,
        )
        changed = bool(batch) or service_changed
        return ReconciliationResult(applied=True, changed=changed, command_count=len(batch))

    def _apply_service(self, enabled: bool, *, restart: bool) -> bool:
        is_enabled = self._runner.run([self._init_script, "enabled"]).returncode == 0
        if enabled:
            if not is_enabled:
                self._runner.run([self._init_script, "enable"])
            if restart or not is_enabled:
                self._runner.run([self._init_script, "restart"])
                return True
            return False
        if is_enabled or restart:
            self._runner.run([self._init_script, "stop"])
            self._runner.run([self._init_script, "disable"])
            return True
        return False


def _set(section: str, option: str, value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("'", "'\\''")
    return f"set snmpd.{section}.{option}='{escaped}'"


def _unquote(value: str) -> str:
    # ``uci show`` prints 'a'\''b' for a value containing a quote.
    if value.startswith("'") and value.endswith("'"):
        value = value[1:-1]
    return value.replace("'\\''", "'")
