from open_omada_device_agent.adapters.inbound.ecsp.mappers.portal_auth import (
    parse_portal_auth_request,
)


def _session(user):
    message = {"header": {}, "body": {"authedUsers": [{"mac": "AA-BB-CC-DD-EE-FF", **user}]}}
    return parse_portal_auth_request(message).client_configs[0].session_timeout_seconds


def test_portal_session_length_from_epoch_seconds():
    assert _session({"rst": 1, "start": 1795666583, "end": 1795666583 + 8 * 3600}) == 28800


def test_portal_session_length_from_epoch_milliseconds():
    start = 1795666583000
    assert _session({"rst": 1, "start": start, "end": start + 3_600_000}) == 3600


def test_portal_session_length_missing_or_invalid_falls_back_to_default():
    assert _session({"rst": 1}) is None
    assert _session({"rst": 1, "start": 100, "end": 50}) is None
    assert _session({"rst": 1, "start": "x", "end": 50}) is None
