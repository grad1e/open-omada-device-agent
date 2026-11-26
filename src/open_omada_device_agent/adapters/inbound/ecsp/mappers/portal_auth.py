"""Parse Omada AP portal authorization messages into client auth commands."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from .....application.commands import ApplyDeviceConfigurationCommand
from .....contexts.clients.domain import ClientAuthConfig


def parse_portal_auth_request(message: Mapping[str, Any]) -> ApplyDeviceConfigurationCommand:
    """Parse ECSP ``portal.auth`` / ``AuthorizationBody``.

    Controller 6.2 builds AP authorization bodies with ``authedUsers`` entries
    whose MAC field is named ``mac`` and commonly formatted as
    ``AA-BB-CC-DD-EE-FF``.  The OpenWrt command adapter normalizes that later
    before invoking ``ndsctl``.
    """
    body = message.get("body")
    if not isinstance(body, Mapping):
        raise ValueError("EVENT_PORTAL_AUTH body must be a JSON object")

    header = message.get("header") or {}
    sequence_id = _optional_int(header.get("seq"))
    client_configs = tuple(
        config
        for config in (
            _parse_authed_user(item)
            for item in _iter_authed_users(body.get("authedUsers"))
        )
        if config is not None
    )

    return ApplyDeviceConfigurationCommand(
        sequence_id=sequence_id,
        config_version=None,
        config_version_inc=None,
        client_configs=client_configs,
        raw_body=dict(body),
    )


def _iter_authed_users(raw: Any) -> Iterable[Mapping[str, Any]]:
    if raw is None:
        return ()
    if isinstance(raw, Mapping):
        return (raw,)
    if not isinstance(raw, Iterable) or isinstance(raw, (str, bytes)):
        raise ValueError("EVENT_PORTAL_AUTH authedUsers must be a list")
    users: list[Mapping[str, Any]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise ValueError("EVENT_PORTAL_AUTH authedUsers item must be a JSON object")
        users.append(item)
    return tuple(users)


def _parse_authed_user(raw: Mapping[str, Any]) -> ClientAuthConfig | None:
    result = _optional_int(raw.get("rst"))
    if result not in {0, 1}:
        return None
    return ClientAuthConfig(
        client_mac=_required_str(
            raw.get("mac") or raw.get("clientMac"),
            "EVENT_PORTAL_AUTH.authedUsers.mac",
        ),
        unauthenticated=result == 0,
        session_timeout_seconds=_session_seconds(raw.get("start"), raw.get("end")),
        raw=dict(raw),
    )


# Epoch values above this are milliseconds (seconds reach it only in 5138).
_EPOCH_MS_THRESHOLD = 100_000_000_000


def _session_seconds(start: Any, end: Any) -> int | None:
    """Return the portal session length from ``start``/``end`` epochs.

    The controller fills ``start``/``end`` from the portal's Authentication
    Timeout. Using the difference keeps the AP clock out of it; values in
    milliseconds are detected by magnitude.
    """
    try:
        start_value = _optional_int(start)
        end_value = _optional_int(end)
    except (TypeError, ValueError):
        return None
    if start_value is None or end_value is None or end_value <= start_value:
        return None
    duration = end_value - start_value
    if start_value >= _EPOCH_MS_THRESHOLD:
        duration //= 1000
    return duration or None


def _required_str(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} is required")
    return value


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)
