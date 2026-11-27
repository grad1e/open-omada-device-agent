from __future__ import annotations


def controller_setting(controller_id: str, destination_id: str = "") -> dict:
    return {
        "controllerId": controller_id,
        "destOmadacId": destination_id or controller_id,
    }
