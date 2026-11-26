from open_omada_device_agent.discovery import PreAdoptDeduplicator


def test_first_pre_adopt_is_handled():
    assert PreAdoptDeduplicator(holdoff=5.0).is_duplicate(100.0) is False


def test_retransmissions_right_after_an_attempt_are_dropped():
    dedup = PreAdoptDeduplicator(holdoff=5.0)
    dedup.attempt_finished(100.0)

    assert dedup.is_duplicate(100.05) is True
    assert dedup.is_duplicate(104.9) is True


def test_a_later_adopt_click_is_handled_again():
    dedup = PreAdoptDeduplicator(holdoff=5.0)
    dedup.attempt_finished(100.0)

    assert dedup.is_duplicate(105.0) is False


class _StopLoop(Exception):
    pass


class _FakeSocket:
    def __init__(self, frames):
        self._frames = list(frames)

    def bind(self, _addr):
        pass

    def settimeout(self, _timeout):
        pass

    def getsockname(self):
        return ("0.0.0.0", 29810)

    def sendto(self, _frame, _target):
        pass

    def recvfrom(self, _size):
        if not self._frames:
            raise _StopLoop
        return self._frames.pop(0), ("192.0.2.1", 29810)

    def close(self):
        pass


def test_burst_of_pre_adopt_requests_triggers_one_adoption_attempt(monkeypatch):
    from types import SimpleNamespace

    import open_omada_device_agent.discovery as discovery
    from open_omada_device_agent.adoption import AuthenticationRejected

    pre_adopt = {
        "header": {"type": int(discovery.MessageType.PRE_ADOPT_REQUEST)},
        "body": {"adoptPort": 29814},
    }
    fake_socket = _FakeSocket([b"one", b"two", b"three"])
    attempts = []

    def reject(**_kwargs):
        attempts.append(1)
        raise AuthenticationRejected("wrong password")

    monkeypatch.setattr(discovery.socket, "socket", lambda *_a: fake_socket)
    monkeypatch.setattr(discovery, "build_discovery", lambda *a, **k: {})
    monkeypatch.setattr(discovery, "encode_frame", lambda _m: b"\0\0\0\0{}")
    monkeypatch.setattr(discovery, "decode_frame", lambda _d: pre_adopt)
    monkeypatch.setattr(discovery, "log_inbound", lambda *a, **k: None)
    monkeypatch.setattr(discovery, "run_v2_adoption", reject)
    settings = SimpleNamespace(
        controller_host="192.0.2.1",
        discovery_port=29810,
        local_discovery_port=0,
        controller_id="controller",
        site_id="",
        destination_controller_id="",
        mac="aa:bb:cc:dd:ee:ff",
        discovery_interval=999.0,
        manage_port=29814,
    )
    services = SimpleNamespace(
        settings=settings,
        state_repository=SimpleNamespace(load=lambda: None),
        device_profile=None,
    )

    try:
        discovery.run(services=services, force_discovery=True)
    except _StopLoop:
        pass

    assert len(attempts) == 1


def _discovery_services(state):
    from types import SimpleNamespace

    holder = {"state": state}
    settings = SimpleNamespace(
        controller_host="192.0.2.1",
        discovery_port=29810,
        local_discovery_port=0,
        controller_id="controller",
        site_id="",
        destination_controller_id="",
        mac="aa:bb:cc:dd:ee:ff",
        discovery_interval=999.0,
        manage_port=29814,
        reconnect_delay=0.5,
    )
    repo = SimpleNamespace(load=lambda: holder["state"])
    return SimpleNamespace(settings=settings, state_repository=repo, device_profile=None), holder


def test_forget_during_managed_session_restarts_discovery_as_factory(monkeypatch):
    import open_omada_device_agent.discovery as discovery

    services, holder = _discovery_services(state=object())
    advertised = []
    bootstrap_calls = []

    def session_forgotten(**_kwargs):
        holder["state"] = None  # FORGET_REQUEST clears local state
        return False

    def record_discovery(*_args, managed_restart, **_kwargs):
        advertised.append(managed_restart)
        return {}

    monkeypatch.setattr(discovery, "_run_saved_managed_session", session_forgotten)
    monkeypatch.setattr(
        discovery,
        "_try_bootstrap_managed_reconnect",
        lambda **_k: bootstrap_calls.append(1) or False,
    )
    monkeypatch.setattr(discovery.socket, "socket", lambda *_a: _FakeSocket([]))
    monkeypatch.setattr(discovery, "build_discovery", record_discovery)
    monkeypatch.setattr(discovery, "encode_frame", lambda _m: b"\0\0\0\0{}")

    try:
        discovery.run(services=services)
    except _StopLoop:
        pass

    assert advertised == [False]  # isFactory=true, not managed rediscovery
    assert bootstrap_calls == []  # no Device Account reconnect after Forget


def test_forget_after_fresh_adoption_keeps_the_agent_running(monkeypatch):
    import open_omada_device_agent.discovery as discovery

    services, holder = _discovery_services(state=None)
    pre_adopt = {
        "header": {"type": int(discovery.MessageType.PRE_ADOPT_REQUEST)},
        "body": {"adoptPort": 29814},
    }
    cycles = []

    def adopt(**_kwargs):
        holder["state"] = object()  # initial sync persisted state

    def session_then_forget(**_kwargs):
        cycles.append(1)
        holder["state"] = None
        return False

    sockets = iter([_FakeSocket([b"pre-adopt"]), _FakeSocket([])])
    monkeypatch.setattr(discovery.socket, "socket", lambda *_a: next(sockets))
    monkeypatch.setattr(discovery, "build_discovery", lambda *a, **k: {})
    monkeypatch.setattr(discovery, "encode_frame", lambda _m: b"\0\0\0\0{}")
    monkeypatch.setattr(discovery, "decode_frame", lambda _d: pre_adopt)
    monkeypatch.setattr(discovery, "log_inbound", lambda *a, **k: None)
    monkeypatch.setattr(discovery, "run_v2_adoption", adopt)
    monkeypatch.setattr(discovery, "_run_saved_managed_session", session_then_forget)
    monkeypatch.setattr(discovery, "_try_bootstrap_managed_reconnect", lambda **_k: False)

    try:
        discovery.run(services=services)
    except _StopLoop:
        pass  # reached the second (factory) discovery socket instead of exiting
    else:
        raise AssertionError("run() returned after Forget instead of rediscovering")

    assert cycles == [1]


def test_no_adopt_exits_after_the_first_pre_adopt_request(monkeypatch):
    import open_omada_device_agent.discovery as discovery

    services, _holder = _discovery_services(state=None)
    pre_adopt = {
        "header": {"type": int(discovery.MessageType.PRE_ADOPT_REQUEST)},
        "body": {"adoptPort": 29814},
    }
    attempts = []
    # A second frame proves run() returned instead of reading on.
    monkeypatch.setattr(
        discovery.socket, "socket", lambda *_a: _FakeSocket([b"pre-adopt", b"retransmit"])
    )
    monkeypatch.setattr(discovery, "build_discovery", lambda *a, **k: {})
    monkeypatch.setattr(discovery, "encode_frame", lambda _m: b"\0\0\0\0{}")
    monkeypatch.setattr(discovery, "decode_frame", lambda _d: pre_adopt)
    monkeypatch.setattr(discovery, "log_inbound", lambda *a, **k: None)
    monkeypatch.setattr(discovery, "run_v2_adoption", lambda **_k: attempts.append(1))
    monkeypatch.setattr(discovery, "_run_saved_managed_session", lambda **_k: attempts.append(2))

    discovery.run(services=services, no_adopt=True)  # must return, not loop forever

    assert attempts == []


def test_once_returns_after_one_adoption_cycle(monkeypatch):
    import open_omada_device_agent.discovery as discovery

    services, holder = _discovery_services(state=None)
    pre_adopt = {
        "header": {"type": int(discovery.MessageType.PRE_ADOPT_REQUEST)},
        "body": {"adoptPort": 29814},
    }
    sessions = []

    def adopt(**_kwargs):
        holder["state"] = object()

    # A second socket would mean run() started another discovery cycle.
    sockets = iter([_FakeSocket([b"pre-adopt"])])
    monkeypatch.setattr(discovery.socket, "socket", lambda *_a: next(sockets))
    monkeypatch.setattr(discovery, "build_discovery", lambda *a, **k: {})
    monkeypatch.setattr(discovery, "encode_frame", lambda _m: b"\0\0\0\0{}")
    monkeypatch.setattr(discovery, "decode_frame", lambda _d: pre_adopt)
    monkeypatch.setattr(discovery, "log_inbound", lambda *a, **k: None)
    monkeypatch.setattr(discovery, "run_v2_adoption", adopt)
    monkeypatch.setattr(
        discovery, "_run_saved_managed_session", lambda **_k: sessions.append(1) or True
    )
    monkeypatch.setattr(discovery.time, "sleep", lambda _s: None)

    discovery.run(services=services, once=True)

    assert sessions == [1]


def test_discovery_survives_a_network_unreachable_send(monkeypatch):
    # Right after an address change the AP can have no route for a moment;
    # that must not kill the agent.
    import errno
    from types import SimpleNamespace

    import open_omada_device_agent.discovery as discovery

    class _NoRouteOnce(_FakeSocket):
        def __init__(self):
            super().__init__([])
            self.sends = 0

        def sendto(self, _frame, _target):
            self.sends += 1
            if self.sends == 1:
                raise OSError(errno.ENETUNREACH, "Network unreachable")

    fake_socket = _NoRouteOnce()
    monkeypatch.setattr(discovery.socket, "socket", lambda *_a: fake_socket)
    monkeypatch.setattr(discovery, "build_discovery", lambda *a, **k: {})
    monkeypatch.setattr(discovery, "encode_frame", lambda _m: b"\0\0\0\0{}")
    settings = SimpleNamespace(
        controller_host="192.0.2.1",
        discovery_port=29810,
        local_discovery_port=0,
        controller_id="controller",
        site_id="",
        destination_controller_id="",
        mac="aa:bb:cc:dd:ee:ff",
        discovery_interval=0.0,
        manage_port=29814,
    )
    services = SimpleNamespace(
        settings=settings,
        state_repository=SimpleNamespace(load=lambda: None),
        device_profile=None,
    )

    try:
        discovery.run(services=services, force_discovery=True)
    except _StopLoop:
        pass

    assert fake_socket.sends == 2  # retried after the failed send
