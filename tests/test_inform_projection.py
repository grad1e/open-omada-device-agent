from open_omada_device_agent.contexts.clients.domain import ClientPortalState, WirelessClientState
from open_omada_device_agent.projections.inform import InformAssembler, LanObservation


def test_inform_assembler_separates_observation_from_wire_projection():
    assembler = InformAssembler(
        device_info=lambda: {"name": "ap", "isFactory": True},
        lan=LanObservation(rate=100.0, duplex=1, port="LAN"),
        clients=lambda: (WirelessClientState(mac="aa:bb:cc:dd:ee:ff", rssi=-60),),
        client_projection=lambda clients: [{"mac": clients[0].mac}],
        wireless_projection=lambda: {"wSettings_2G": {"ch": "6"}},
    )

    body = assembler.build(need_reply=True, uptime=12)

    assert body["deviceInfo"]["upTime"] == "0 days 00:00:12"
    assert body["deviceInfo"]["isFactory"] is False
    assert body["clients"] == [{"mac": "aa:bb:cc:dd:ee:ff"}]
    assert body["wSettings_2G"] == {"ch": "6"}


def test_inform_assembler_projects_portal_auth_clients():
    assembler = InformAssembler(
        device_info=lambda: {"name": "ap"},
        lan=LanObservation(rate=100.0, duplex=1, port="LAN"),
        clients=lambda: (
            WirelessClientState(
                mac="aa:bb:cc:dd:ee:ff",
                rssi=-60,
                portal_state=ClientPortalState.AUTHENTICATED,
            ),
            WirelessClientState(mac="02:00:00:00:00:02", rssi=-70),
        ),
        client_projection=lambda clients: [
            {"mac": "AA-BB-CC-DD-EE-FF"},
            {"mac": "02-00-00-00-00-02"},
        ],
        wireless_projection=lambda: {},
    )

    body = assembler.build(need_reply=False, uptime=0)

    assert body["portalAuthClients"] == [{"mac": "AA-BB-CC-DD-EE-FF"}]


def test_inform_assembler_preserves_device_uptime_when_adapter_reports_it():
    assembler = InformAssembler(
        device_info=lambda: {"name": "ap", "upTime": 99},
        lan=LanObservation(rate=100.0, duplex=1, port="LAN"),
        clients=lambda: (),
        client_projection=lambda clients: [],
        wireless_projection=lambda: {},
    )

    body = assembler.build(need_reply=False, uptime=12)

    assert body["deviceInfo"]["upTime"] == "0 days 00:01:39"


def test_uptime_uses_the_controller_days_hh_mm_ss_format():
    from open_omada_device_agent.projections.inform import format_uptime

    assert format_uptime(0) == "0 days 00:00:00"
    assert format_uptime(2892) == "0 days 00:48:12"
    assert format_uptime(183845) == "2 days 03:04:05"  # parsed back to 183845 by the controller


def test_clients_without_a_signal_reading_are_left_out():
    projected = []
    assembler = InformAssembler(
        device_info=lambda: {"name": "ap"},
        lan=LanObservation(rate=100.0, duplex=1, port="LAN"),
        clients=lambda: (
            WirelessClientState(mac="aa:bb:cc:dd:ee:ff", rssi=-60),
            WirelessClientState(mac="02:00:00:00:00:02"),  # just re-associated
        ),
        client_projection=lambda clients: projected.extend(clients)
        or [{"mac": client.mac} for client in clients],
        wireless_projection=lambda: {},
    )

    body = assembler.build(need_reply=False, uptime=0)

    assert body["clients"] == [{"mac": "aa:bb:cc:dd:ee:ff"}]
    assert [client.mac for client in projected] == ["aa:bb:cc:dd:ee:ff"]


def _assembler(rogue_aps=None):
    return InformAssembler(
        device_info=lambda: {"name": "ap"},
        lan=LanObservation(rate=100.0, duplex=1, port="LAN"),
        clients=lambda: (),
        client_projection=lambda clients: [],
        wireless_projection=lambda: {},
        rogue_aps=rogue_aps,
    )


def test_inform_assembler_adds_rogue_ap_list_only_when_present():
    item = {"mac": "00-00-5E-00-53-01", "ssid": "n", "rid": 0, "ch": 1, "sec": 10, "pm": 7}

    assert "rogueApList" not in _assembler().build(need_reply=False, uptime=1)
    assert "rogueApList" not in _assembler(lambda: []).build(need_reply=False, uptime=1)
    assert _assembler(lambda: [item]).build(need_reply=False, uptime=1)["rogueApList"] == [item]


def test_only_logged_in_portal_clients_are_reported_as_portal_authed():
    assembler = InformAssembler(
        device_info=lambda: {"name": "ap"},
        lan=LanObservation(rate=100.0, duplex=1, port="LAN"),
        clients=lambda: (
            WirelessClientState(
                mac="02:00:00:00:00:01",
                rssi=-60,
                portal_state=ClientPortalState.UNAUTHENTICATED,
            ),
        ),
        client_projection=lambda clients: [{"mac": "02-00-00-00-00-01"}],
        wireless_projection=lambda: {},
    )

    body = assembler.build(need_reply=False, uptime=0)

    assert "portalAuthClients" not in body
