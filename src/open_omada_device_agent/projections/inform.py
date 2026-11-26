"""Assemble domain observations into the ECSP inform read model."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Callable, Mapping
from ..contexts.clients.domain import ClientPortalState, WirelessClientState

@dataclass(frozen=True)
class LanObservation:
    rate: float
    duplex: int
    port: str

class InformAssembler:
    def __init__(
        self,
        *,
        device_info: Callable[[], Mapping[str, Any]],
        lan: LanObservation,
        clients: Callable[[], tuple[WirelessClientState, ...]],
        client_projection: Callable[[tuple[WirelessClientState, ...]], list[dict[str, Any]]],
        wireless_projection: Callable[[], Mapping[str, Any]],
    ) -> None:
        self._device_info = device_info
        self._lan = lan
        self._clients = clients
        self._client_projection = client_projection
        self._wireless_projection = wireless_projection

    def build(self, *, need_reply: bool, uptime: int) -> dict[str, Any]:
        info = dict(self._device_info())
        info["isFactory"] = False
        info["upTime"] = _uptime_value(info.get("upTime"), fallback=uptime)
        body: dict[str, Any] = {
            "needReply": 1 if need_reply else 0,
            "deviceInfo": info,
            "lanInfo": {
                "rate": _rate_value(self._lan.rate),
                "duplex": self._lan.duplex,
                "port": self._lan.port,
            },
        }
        # Controller 6.3.0.111 client insights unbox ``rssi`` for every client;
        # hostapd reports a station without ``signal`` for a moment after it
        # (re)associates, so it is reported again from the next inform.
        clients = tuple(client for client in self._clients() if client.rssi is not None)
        if clients:
            projected_clients = self._client_projection(clients)
            body["clients"] = projected_clients
            portal_clients = [
                projected
                for client, projected in zip(clients, projected_clients, strict=False)
                if client.portal_state is not ClientPortalState.UNKNOWN
            ]
            if portal_clients:
                body["portalAuthClients"] = portal_clients
        body.update(self._wireless_projection())
        return body


def _rate_value(rate: float) -> str:
    # The controller parses lanInfo.rate with Integer.parseInt; "100.0" breaks topology.
    return str(int(round(rate)))


def _uptime_value(value: Any, *, fallback: int) -> str:
    seconds = max(0, fallback)
    if value not in (None, ""):
        try:
            seconds = max(0, int(float(value)))
        except (TypeError, ValueError):
            return str(value)
    return format_uptime(seconds)


def format_uptime(seconds: int) -> str:
    """Render uptime as "<days> days HH:MM:SS".

    Controller statistics (monitor.statistic and the anomaly client-stat
    builder) parse deviceInfo.upTime with a StringTokenizer that expects this
    shape; a bare number raises NoSuchElementException on every inform.
    """
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{days} days {hours:02d}:{minutes:02d}:{secs:02d}"
