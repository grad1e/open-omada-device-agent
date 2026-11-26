"""Network helpers used to build device telemetry."""
from __future__ import annotations

import logging
import socket
import time
from functools import lru_cache
from urllib.error import URLError
from urllib.request import urlopen

from . import config

log = logging.getLogger("open_omada.network")


LOCAL = "local"
PUBLIC = "auto"


def get_device_ip(
    configured: str | None = None,
    controller_host: str = "",
    lookup_url: str | None = None,
    lookup_timeout: float | None = None,
) -> str:
    """Return the IP address advertised in ECSP deviceInfo.

    ``local`` (the default) is the address this host uses to reach the
    controller, so the device list shows the AP's management LAN address.
    ``auto`` is a best-effort public-IP lookup through a third-party service.
    Any other value is advertised as-is.
    """
    configured = (config.DEVICE_IP if configured is None else configured).strip()
    if configured.lower() == LOCAL or not configured:
        return local_source_ip(controller_host) or "0.0.0.0"
    if configured.lower() == PUBLIC:
        return _public_ip(lookup_url, lookup_timeout)
    return configured


# get_device_ip runs on every inform (every few seconds). Re-check the route
# only this often, so a hostname is not resolved on every inform and a DHCP
# address change still shows up within a minute.
LOCAL_IP_CACHE_SECONDS = 60.0
_local_ip_cache: dict[tuple[str, int], tuple[float, str | None]] = {}
_local_ip_warned: set[str] = set()


def local_source_ip(host: str, port: int = 29810) -> str | None:
    """Source address the kernel picks for traffic to ``host``.

    Connecting a UDP socket only selects a route; no packet is sent. IPv4 and
    IPv6 controller addresses and hostnames are supported. The result is
    cached for ``LOCAL_IP_CACHE_SECONDS``.
    """
    if not host:
        return None
    now = time.monotonic()
    cached = _local_ip_cache.get((host, port))
    if cached is not None and now - cached[0] < LOCAL_IP_CACHE_SECONDS:
        return cached[1]
    address = _probe_source_ip(host, port)
    _local_ip_cache[(host, port)] = (now, address)
    return address


def forget_local_ip() -> None:
    """Drop cached source addresses; call it when the AP's address changes."""
    _local_ip_cache.clear()


def _probe_source_ip(host: str, port: int) -> str | None:
    try:
        family, _type, _proto, _name, target = socket.getaddrinfo(
            host, port, type=socket.SOCK_DGRAM
        )[0]
        with socket.socket(family, socket.SOCK_DGRAM) as probe:
            probe.connect(target)
            address = probe.getsockname()[0]
    except OSError as exc:
        # Warn once per host; the caller advertises 0.0.0.0 meanwhile.
        level = logging.DEBUG if host in _local_ip_warned else logging.WARNING
        _local_ip_warned.add(host)
        log.log(level, "Could not determine the local address towards %s: %s", host, exc)
        return None
    _local_ip_warned.discard(host)
    return None if address in ("0.0.0.0", "::", "") else address


@lru_cache(maxsize=8)
def _public_ip(lookup_url: str | None, lookup_timeout: float | None) -> str:
    try:
        with urlopen(  # noqa: S310 - the URL is explicitly operator-configured
            lookup_url or config.PUBLIC_IP_LOOKUP_URL,
            timeout=(config.PUBLIC_IP_LOOKUP_TIMEOUT if lookup_timeout is None else lookup_timeout),
        ) as response:
            value = response.read(128).decode("ascii", errors="strict").strip()
            return value or "0.0.0.0"
    except (OSError, URLError, UnicodeError, ValueError) as exc:
        log.warning("Public-IP lookup failed: %s", exc)
        return "0.0.0.0"
