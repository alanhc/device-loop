"""Agent loop 的收斂行為。

Coordinator 不推送:exporter 每輪拿到 desired,自己補起缺的、停掉多的。
這裡用假的 client 驗收斂邏輯,不碰網路也不碰真裝置。
"""

from __future__ import annotations

import pytest

from exporter.agent import ExporterAgent
from exporter.coordinator_client import DesiredService, HeartbeatResult
from exporter.services import ServiceError, ServiceManager

PIXEL = "pixel8-shiba"
SERIAL = "38011FDJH00C9F"
TTY = "/dev/serial/by-id/usb-FTDI_FT232R-if00-port0"


class FakeClient:
    """記下回報內容,回傳預先設定好的 desired。"""

    def __init__(self, desired=None, reclaims=None, flashes=None, instances=None):
        self.desired = list(desired or [])
        self.reclaims = list(reclaims or [])
        self.flashes = list(flashes or [])
        self.instances = list(instances or [])
        self.reports: list[dict] = []
        self.closed = False
        self.fail_next = False

    def heartbeat(self, identifiers, discoverable_classes=None, seen=None,
                  services=None, reclaims=None, flashes=None, instances=None):
        if self.fail_next:
            self.fail_next = False
            raise ConnectionError("coordinator unreachable")
        self.reports.append({
            "identifiers": list(identifiers),
            "discoverable_classes": list(discoverable_classes or []),
            "seen": list(seen or []),
            "services": list(services or []),
            "reclaims": list(reclaims or []),
            "flashes": list(flashes or []),
            "instances": list(instances or []),
        })
        return HeartbeatResult([], [], [], [], list(self.desired),
                               list(self.reclaims), list(self.flashes),
                               list(self.instances))

    def close(self):
        self.closed = True


class StubScanner:
    def __init__(self, classes, found):
        self.classes = classes
        self._found = found

    def scan(self):
        return list(self._found)


@pytest.fixture()
def agent_parts(runner, ports):
    client = FakeClient()
    services = ServiceManager(runner, port_fn=ports)
    scanners = [StubScanner(("android",), [SERIAL])]
    agent = ExporterAgent(client, services, scanners)
    return agent, client, services


def _want(device_id=PIXEL, service="adb", identifier=SERIAL, lease_id=1):
    return DesiredService(device_id, service, identifier, lease_id)


def test_nothing_desired_starts_nothing(agent_parts):
    agent, client, services = agent_parts
    result = agent.tick()
    assert result.started == [] and result.stopped == []
    assert services.running() == []


def test_desired_service_gets_started(agent_parts):
    agent, client, services = agent_parts
    client.desired = [_want()]
    result = agent.tick()
    assert result.started == [(PIXEL, "adb")]
    assert [(s.device_id, s.service) for s in services.running()] == [(PIXEL, "adb")]


def test_already_running_service_is_left_alone(agent_parts):
    """收斂是冪等的——穩態下不該每輪都重起 daemon。"""
    agent, client, services = agent_parts
    client.desired = [_want()]
    agent.tick()
    port = services.running()[0].port
    result = agent.tick()
    assert result.started == [] and result.stopped == []
    assert services.running()[0].port == port   # 同一個 daemon,沒被重起


def test_service_no_longer_desired_is_stopped(agent_parts):
    """lease 結束 → coordinator 不再列它 → exporter 自己停掉。"""
    agent, client, services = agent_parts
    client.desired = [_want()]
    agent.tick()
    client.desired = []
    result = agent.tick()
    assert result.stopped == [(PIXEL, "adb")]
    assert services.running() == []


def test_running_services_are_reported_back(agent_parts):
    """coordinator 靠這個把實際 port 寫進 device_services。"""
    agent, client, services = agent_parts
    client.desired = [_want()]
    agent.tick()
    agent.tick()
    assert client.reports[-1]["services"] == [
        {"device_id": PIXEL, "service": "adb", "port": 9000}
    ]


def test_inventory_and_class_coverage_are_reported(agent_parts):
    agent, client, services = agent_parts
    agent.tick()
    assert client.reports[0]["identifiers"] == [SERIAL]
    assert client.reports[0]["discoverable_classes"] == ["android"]


def test_a_dead_daemon_is_restarted_next_tick(agent_parts):
    """daemon 自己死掉:先清殘骸再回報,否則會回報一個連不上的 endpoint。"""
    agent, client, services = agent_parts
    client.desired = [_want()]
    agent.tick()
    services.running()[0].handle.exit_code = 1
    result = agent.tick()
    assert result.started == [(PIXEL, "adb")]
    assert services.running()[0].handle.poll() is None


def test_dead_daemon_is_not_reported_as_running(agent_parts):
    """關鍵:死掉的 daemon 不能出現在回報裡,不然 client 會拿到死 endpoint。"""
    agent, client, services = agent_parts
    client.desired = [_want()]
    agent.tick()
    services.running()[0].handle.exit_code = 1
    agent.tick()
    # 這一輪回報時舊的已經清掉,新的還沒起——回報應該是空的
    assert client.reports[-1]["services"] == []


def test_one_failing_device_does_not_block_the_others(runner, ports):
    """ser2net 缺席不該讓 adb 也起不來。"""
    client = FakeClient([_want(), _want("board", "uart", TTY, 2)])
    services = ServiceManager(runner, port_fn=ports)
    runner.missing = {"ser2net"}
    agent = ExporterAgent(client, services, [StubScanner(("android",), [SERIAL])])
    result = agent.tick()
    assert result.started == [(PIXEL, "adb")]
    assert result.failed == [("board", "uart")]


def test_a_failed_start_is_retried_next_tick(runner, ports):
    """沒起成的下一輪還在 desired 裡,自然重試——不需要額外的重試佇列。"""
    client = FakeClient([_want()])
    services = ServiceManager(runner, port_fn=ports)
    runner.missing = {"adb"}
    agent = ExporterAgent(client, services, [])
    assert agent.tick().failed == [(PIXEL, "adb")]
    runner.missing = set()
    assert agent.tick().started == [(PIXEL, "adb")]


def test_coordinator_outage_does_not_stop_running_services(agent_parts):
    """coordinator 一時不在不該把裝置服務全停掉——那會踢掉正在用的 client。"""
    agent, client, services = agent_parts
    client.desired = [_want()]
    agent.tick()
    client.fail_next = True
    with pytest.raises(ConnectionError):
        agent.tick()
    assert [(s.device_id, s.service) for s in services.running()] == [(PIXEL, "adb")]


def test_run_survives_a_failed_tick(agent_parts):
    """run() 要吞掉例外繼續跑,否則 coordinator 重啟一次 exporter 就死了。"""
    agent, client, services = agent_parts
    client.fail_next = True
    agent._interval_s = 0
    calls = {"n": 0}
    real_tick = agent.tick

    def counting_tick():
        calls["n"] += 1
        if calls["n"] >= 3:
            agent.stop()
        return real_tick()

    agent.tick = counting_tick
    agent.run()
    assert calls["n"] >= 3          # 第一輪拋例外之後還有繼續跑


def test_shutdown_stops_every_daemon(agent_parts):
    """退出時清乾淨——§4 exporter 擁有裝置的責任。"""
    agent, client, services = agent_parts
    client.desired = [_want(), _want("board", "uart", TTY, 2)]
    agent.tick()
    assert len(services.running()) == 2
    agent.shutdown()
    assert services.running() == []
    assert client.closed


def test_run_cleans_up_even_when_stopped_immediately(agent_parts):
    agent, client, services = agent_parts
    client.desired = [_want()]
    agent.tick()
    agent._interval_s = 0
    agent.stop()
    agent.run()
    assert services.running() == []


# ------------------------------------------------------- 強制回收(§7 第 4 步)

class FakeReclaimer:
    def __init__(self, ok=True):
        self.ok = ok
        self.executed: list[dict] = []

    def execute(self, reclaim):
        from exporter.reclaim import ReclaimOutcome

        self.executed.append(reclaim)
        return ReclaimOutcome(reclaim["reclaim_id"], self.ok, {"exit_code": 0})


def _reclaim_job(rid=1, device_id=PIXEL):
    return {"reclaim_id": rid, "device_id": device_id,
            "action": "adb-reboot", "identifier": SERIAL}


def test_reclaim_job_is_executed(runner, ports):
    client = FakeClient(reclaims=[_reclaim_job()])
    reclaimer = FakeReclaimer()
    agent = ExporterAgent(client, ServiceManager(runner, port_fn=ports), [],
                          reclaimer=reclaimer)
    result = agent.tick()
    assert [r["reclaim_id"] for r in reclaimer.executed] == [1]
    assert result.reclaimed == [PIXEL]


def test_reclaim_result_is_reported_on_the_next_tick(runner, ports):
    """這輪的 heartbeat 已經送出去了,結果只能下一輪回報。"""
    client = FakeClient(reclaims=[_reclaim_job()])
    agent = ExporterAgent(client, ServiceManager(runner, port_fn=ports), [],
                          reclaimer=FakeReclaimer())
    agent.tick()
    assert client.reports[0]["reclaims"] == []      # 第一輪還沒有結果
    client.reclaims = []
    agent.tick()
    assert client.reports[1]["reclaims"] == [
        {"reclaim_id": 1, "ok": True, "detail": {"exit_code": 0}}
    ]


def test_a_reported_result_is_not_reported_twice(runner, ports):
    client = FakeClient(reclaims=[_reclaim_job()])
    agent = ExporterAgent(client, ServiceManager(runner, port_fn=ports), [],
                          reclaimer=FakeReclaimer())
    agent.tick()
    client.reclaims = []
    agent.tick()
    agent.tick()
    assert client.reports[2]["reclaims"] == []


def test_failed_reclaim_is_still_reported(runner, ports):
    """失敗必須回報——coordinator 靠它決定把裝置留在 maintenance。"""
    client = FakeClient(reclaims=[_reclaim_job()])
    agent = ExporterAgent(client, ServiceManager(runner, port_fn=ports), [],
                          reclaimer=FakeReclaimer(ok=False))
    result = agent.tick()
    assert result.reclaimed == []
    client.reclaims = []
    agent.tick()
    assert client.reports[1]["reclaims"][0]["ok"] is False


def test_a_crashing_reclaimer_does_not_break_the_tick(runner, ports):
    class Exploding:
        def execute(self, reclaim):
            raise RuntimeError("boom")

    client = FakeClient(reclaims=[_reclaim_job()])
    agent = ExporterAgent(client, ServiceManager(runner, port_fn=ports), [],
                          reclaimer=Exploding())
    agent.tick()                       # 不該拋出來
    client.reclaims = []
    agent.tick()
    assert client.reports[1]["reclaims"][0]["ok"] is False


def test_reclaim_runs_after_convergence(runner, ports):
    """先停服務再重開裝置——不能對一台還跑著 daemon 的裝置下重開指令。"""
    client = FakeClient(desired=[_want()], reclaims=[_reclaim_job()])
    services = ServiceManager(runner, port_fn=ports)
    order: list[str] = []

    class Recording(FakeReclaimer):
        def execute(self, reclaim):
            order.append("reclaim")
            return super().execute(reclaim)

    original_start = services.start

    def recording_start(*a, **k):
        order.append("start")
        return original_start(*a, **k)

    services.start = recording_start
    agent = ExporterAgent(client, services, [], reclaimer=Recording())
    agent.tick()
    assert order == ["start", "reclaim"]


def test_no_reclaimer_configured_is_harmless(runner, ports):
    client = FakeClient(reclaims=[_reclaim_job()])
    agent = ExporterAgent(client, ServiceManager(runner, port_fn=ports), [])
    assert agent.tick().reclaimed == []
