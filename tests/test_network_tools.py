import pytest

from open_omada_device_agent import network_tools
from open_omada_device_agent.network_tools import get_device_ip, local_source_ip


@pytest.fixture(autouse=True)
def _fresh_cache():
    network_tools._local_ip_cache.clear()
    network_tools._local_ip_warned.clear()


class _FakeUdp:
    def __init__(self, address, *, fail=False):
        self.address = address
        self.fail = fail
        self.connected = None

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def connect(self, target):
        if self.fail:
            raise OSError("Network is unreachable")
        self.connected = target

    def getsockname(self):
        return (self.address, 40000)


def test_local_mode_advertises_the_address_used_to_reach_the_controller(monkeypatch):
    probe = _FakeUdp("192.0.2.10")
    monkeypatch.setattr(network_tools.socket, "socket", lambda *_a: probe)

    assert get_device_ip("local", "192.0.2.20") == "192.0.2.10"
    assert probe.connected == ("192.0.2.20", 29810)


def test_empty_setting_means_local(monkeypatch):
    monkeypatch.setattr(network_tools.socket, "socket", lambda *_a: _FakeUdp("10.0.0.5"))

    assert get_device_ip("", "10.0.0.1") == "10.0.0.5"


def test_local_mode_falls_back_to_unspecified_without_a_route(monkeypatch):
    monkeypatch.setattr(network_tools.socket, "socket", lambda *_a: _FakeUdp("", fail=True))

    assert get_device_ip("local", "192.0.2.1") == "0.0.0.0"
    assert local_source_ip("") is None


def test_fixed_address_is_advertised_as_is(monkeypatch):
    def no_socket(*_a):
        raise AssertionError("no route lookup for a fixed address")

    monkeypatch.setattr(network_tools.socket, "socket", no_socket)

    assert get_device_ip("192.168.1.20", "192.168.1.1") == "192.168.1.20"
    assert get_device_ip("0.0.0.0", "192.168.1.1") == "0.0.0.0"


def test_local_source_ip_uses_a_real_udp_socket_without_sending():
    # Loopback always has a route; connect() on UDP sends nothing.
    assert local_source_ip("127.0.0.1") == "127.0.0.1"


def test_route_is_cached_between_informs(monkeypatch):
    probes = []

    def make(*_a):
        probes.append(1)
        return _FakeUdp("192.0.2.10")

    monkeypatch.setattr(network_tools.socket, "socket", make)

    assert local_source_ip("192.0.2.20") == "192.0.2.10"
    assert local_source_ip("192.0.2.20") == "192.0.2.10"
    assert probes == [1]


def test_ipv6_controller_uses_an_ipv6_socket(monkeypatch):
    families = []

    def make(family, *_a):
        families.append(family)
        return _FakeUdp("2001:db8::10")

    monkeypatch.setattr(network_tools.socket, "socket", make)

    assert local_source_ip("2001:db8::1") == "2001:db8::10"
    assert families == [network_tools.socket.AF_INET6]


def test_route_failure_warns_once(monkeypatch, caplog):
    monkeypatch.setattr(network_tools.socket, "socket", lambda *_a: _FakeUdp("", fail=True))
    monkeypatch.setattr(network_tools, "LOCAL_IP_CACHE_SECONDS", 0.0)

    with caplog.at_level("DEBUG", logger="open_omada.network"):
        local_source_ip("192.0.2.1")
        local_source_ip("192.0.2.1")

    assert [r.levelname for r in caplog.records] == ["WARNING", "DEBUG"]
