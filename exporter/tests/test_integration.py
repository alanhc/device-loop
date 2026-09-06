"""Exporter 對真的 coordinator 跑一輪 reconcile。

這是這台機器上做得到的最遠一步:coordinator 是真的(in-process ASGI,
真的 SQLite、真的 HTTP 語意),exporter 的 agent loop 也是真的,只有
「起 daemon」那層是假的——這台沒有 USB 序列埠也沒有 adb 裝置。

接真 Pixel 的部分驗不了,見 README 的驗證狀態表。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

COORDINATOR_SRC = Path(__file__).resolve().parents[2] / "coordinator" / "src"
if COORDINATOR_SRC.is_dir():
    sys.path.insert(0, str(COORDINATOR_SRC))

pytest.importorskip("fastapi", reason="coordinator 沒裝就跳過整合測試")

from exporter.agent import ExporterAgent          # noqa: E402
from exporter.coordinator_client import CoordinatorClient  # noqa: E402
from exporter.services import ServiceManager      # noqa: E402

PIXEL = "pixel8-shiba"
SERIAL = "38011FDJH00C9F"
ROG = "rog-laptop"


class ASGIClient(CoordinatorClient):
    """把 exporter 的 client 接到 in-process 的 coordinator app。

    借用 starlette 的 TestClient 當 transport:它是真的同步 HTTP client,
    走完整的 ASGI 堆疊(routing、pydantic 驗證、狀態碼),只是不開 socket。
    exporter 端的程式碼一行都沒改,跑的是 heartbeat() 的正式路徑。
    """

    def __init__(self, test_client, host_id: str):
        self.base_url = ""
        self.host_id = host_id
        self._client = test_client


class StubScanner:
    classes = ("android",)

    def __init__(self, found):
        self._found = found

    def scan(self):
        return list(self._found)


@pytest.fixture()
def stack(tmp_path, runner, ports):
    from fastapi.testclient import TestClient

    from coordinator.api import create_app

    app = create_app(str(tmp_path / "it.db"))
    with TestClient(app) as http:          # lifespan 建表 + seed
        client = ASGIClient(http, ROG)
        services = ServiceManager(runner, port_fn=ports)
        agent = ExporterAgent(client, services, [StubScanner([SERIAL])])
        yield http, agent, services


def test_no_lease_means_exporter_starts_nothing(stack):
    http, agent, services = stack
    assert agent.tick().started == []
    assert services.running() == []


def test_reserve_then_exporter_converges_and_publishes_endpoint(stack):
    """完整的一圈:reserve → exporter 起 daemon → endpoint 進 device_services
    → MCP 那邊就取得到。"""
    http, agent, services = stack
    lease = http.post(
        "/leases", json={"device_id": PIXEL, "user_id": "alice", "ttl_s": 600}
    ).json()

    # 第一輪:拿到 desired,起 daemon,但 endpoint 還沒回報上去(pending)
    first = agent.tick()
    assert first.started == [(PIXEL, "adb")]

    # 第二輪:把實際 port 回報上去
    agent.tick()
    devices = {d["id"]: d for d in http.get("/devices").json()}
    assert devices[PIXEL]["state"] == "leased"

    endpoint = _endpoint(http, PIXEL, "adb")
    assert endpoint == "100.71.211.115:9000"     # tailscale IP + 實際 port
    assert lease["id"] > 0


def test_release_makes_the_exporter_stop_and_endpoint_disappear(stack):
    http, agent, services = stack
    lease = http.post(
        "/leases", json={"device_id": PIXEL, "user_id": "alice", "ttl_s": 600}
    ).json()
    agent.tick()
    agent.tick()
    assert _endpoint(http, PIXEL, "adb") is not None

    http.post(f"/leases/{lease['id']}/release", json={"user_id": "alice"})
    agent.tick()
    assert services.running() == []
    assert _endpoint(http, PIXEL, "adb") is None


def test_expired_lease_is_reclaimed_and_service_stopped(stack):
    """reaper 收掉過期 lease 之後,exporter 下一輪就會停掉服務——
    不需要 coordinator 主動通知。"""
    http, agent, services = stack
    http.post("/leases", json={"device_id": PIXEL, "user_id": "alice", "ttl_s": 1})
    agent.tick()
    assert services.running() != []

    from coordinator import store
    conn = http.app.state.conn
    with http.app.state.lock:
        conn.execute("UPDATE leases SET expires_at = '2000-01-01T00:00:00+00:00'")
        conn.commit()
        store.reap_expired_leases(conn)

    agent.tick()
    assert services.running() == []
    assert _endpoint(http, PIXEL, "adb") is None


def test_device_moving_hosts_is_seen_by_the_coordinator(stack):
    """exporter 回報的 inventory 真的驅動了 devices.host。"""
    http, agent, services = stack
    agent.tick()
    devices = {d["id"]: d for d in http.get("/devices").json()}
    assert devices[PIXEL]["host"] == ROG
    assert devices[PIXEL]["last_seen_at"] is not None


def test_unregistered_device_shows_up_for_adoption(tmp_path, runner, ports):
    """新板子隨便插進來就自動出現等 adopt(§5),不用改設定檔。"""
    from fastapi.testclient import TestClient

    from coordinator.api import create_app

    app = create_app(str(tmp_path / "it2.db"))
    with TestClient(app) as http:
        agent = ExporterAgent(
            ASGIClient(http, ROG),
            ServiceManager(runner, port_fn=ports),
            [StubScanner([SERIAL, "BRANDNEWBOARD"])],
        )
        agent.tick()
        devices = {d["id"]: d for d in http.get("/devices").json()}
        assert devices["BRANDNEWBOARD"]["state"] == "unregistered"


def _endpoint(http, device_id: str, service: str) -> str | None:
    conn = http.app.state.conn
    with http.app.state.lock:
        row = conn.execute(
            "SELECT endpoint FROM device_services WHERE device_id = ? AND service = ?",
            (device_id, service),
        ).fetchone()
    return row["endpoint"] if row else None


# --------------------------------------------- 強制回收走完整條路(§7 第 4 步)

class RecordingReclaimer:
    """記下被要求做什麼,不真的重開任何東西。

    真機上這會是 `adb reboot`——開發機不跑,ROG 那顆 Pixel 也要先取得
    同意才能跑(它有手工構築的分割區狀態)。
    """

    def __init__(self, ok=True):
        self.ok = ok
        self.executed: list[dict] = []

    def execute(self, reclaim):
        from exporter.reclaim import ReclaimOutcome

        self.executed.append(reclaim)
        return ReclaimOutcome(reclaim["reclaim_id"], self.ok, {"exit_code": 0})


def _stack_with_reclaimer(tmp_path, runner, ports, ok=True):
    from fastapi.testclient import TestClient

    from coordinator.api import create_app

    app = create_app(str(tmp_path / "rc.db"))
    http = TestClient(app)
    http.__enter__()
    reclaimer = RecordingReclaimer(ok=ok)
    agent = ExporterAgent(
        ASGIClient(http, ROG),
        ServiceManager(runner, port_fn=ports),
        [StubScanner([SERIAL])],
        reclaimer=reclaimer,
    )
    return http, agent, reclaimer


def test_wedged_device_is_force_reclaimed_end_to_end(tmp_path, runner, ports):
    """服務停不掉的裝置:升級 → 派給 exporter → 執行 → 回報 → 放回 free。"""
    from coordinator import store

    http, agent, reclaimer = _stack_with_reclaimer(tmp_path, runner, ports)
    try:
        lease = http.post(
            "/leases", json={"device_id": PIXEL, "user_id": "alice", "ttl_s": 600}
        ).json()
        agent.tick()
        agent.tick()                       # endpoint 已發布

        conn = http.app.state.conn
        with http.app.state.lock:
            # 模擬「lease 過期但 exporter 沒收斂」:過期,但 endpoint 留著
            conn.execute("UPDATE leases SET expires_at = '2000-01-01T00:00:00+00:00'")
            conn.commit()
            store.reap_expired_leases(conn)
            conn.execute("UPDATE events SET created_at = '2000-01-01T00:00:00+00:00' "
                         "WHERE kind = 'expire'")
            conn.commit()
            assert store.escalate_stuck_devices(conn, 60) == [PIXEL]

        # 下一輪 heartbeat 把回收動作派下來並執行
        agent.tick()
        assert [r["device_id"] for r in reclaimer.executed] == [PIXEL]

        # 再一輪把結果回報上去,裝置回到 free
        agent.tick()
        devices = {d["id"]: d for d in http.get("/devices").json()}
        assert devices[PIXEL]["state"] == "free"
        assert lease["id"] > 0
    finally:
        agent.shutdown()
        http.__exit__(None, None, None)


def test_failed_reclaim_keeps_the_device_out_of_circulation(tmp_path, runner, ports):
    """回收失敗的裝置不能放回 free——下一個 agent 會借到壞的。"""
    from coordinator import store

    http, agent, reclaimer = _stack_with_reclaimer(tmp_path, runner, ports, ok=False)
    try:
        http.post("/leases", json={"device_id": PIXEL, "user_id": "alice", "ttl_s": 600})
        agent.tick()
        agent.tick()
        conn = http.app.state.conn
        with http.app.state.lock:
            conn.execute("UPDATE leases SET expires_at = '2000-01-01T00:00:00+00:00'")
            conn.commit()
            store.reap_expired_leases(conn)
            conn.execute("UPDATE events SET created_at = '2000-01-01T00:00:00+00:00' "
                         "WHERE kind = 'expire'")
            conn.commit()
            store.escalate_stuck_devices(conn, 60)
        agent.tick()
        agent.tick()
        devices = {d["id"]: d for d in http.get("/devices").json()}
        assert devices[PIXEL]["state"] == "maintenance"
        grab = http.post(
            "/leases", json={"device_id": PIXEL, "user_id": "bob", "ttl_s": 600}
        )
        assert grab.status_code == 409
    finally:
        agent.shutdown()
        http.__exit__(None, None, None)


# ------------------------------------------------- mediated flash(§6)
# Flash 是唯一 mediated 的能力:client 不直連,由 coordinator 排工作、
# exporter 領走執行。這裡跑完整一圈——登記 image → 要求 flash → exporter
# 隨 heartbeat 領到 → 驗雜湊 → 執行 → 回報 → coordinator 記結果。
# 「起 fastboot」那層仍是假的(這台沒有真裝置,而且真機刷 Pixel 要先取得
# 同意,見 exporter README)。

DIGEST = "c" * 64


def _stack_with_flasher(tmp_path, runner, ports, digest=DIGEST):
    from fastapi.testclient import TestClient

    from coordinator.api import create_app
    from exporter.flash import FlashExecutor

    app = create_app(str(tmp_path / "fl.db"))
    http = TestClient(app)
    http.__enter__()
    agent = ExporterAgent(
        ASGIClient(http, ROG),
        ServiceManager(runner, port_fn=ports),
        [StubScanner([SERIAL])],
        # hasher 注入:不用真的準備一份幾百 MB 的 image,但走的是正式的
        # 「驗完才刷」路徑。
        flasher=FlashExecutor(runner, hasher=lambda path: digest),
    )
    return http, agent


def _register_image(http, tmp_path, image_id="shiba-vendor-v3", kind="vendor",
                    digest=DIGEST, known_good=False):
    img = tmp_path / f"{image_id}.img"
    img.write_bytes(b"vendor partition")
    resp = http.post("/images", json={
        "id": image_id, "device_id": PIXEL, "kind": kind, "uri": str(img),
        "sha256": digest, "known_good": known_good,
    })
    assert resp.status_code == 201, resp.text
    return img


def test_flash_round_trip_through_the_exporter(tmp_path, runner, ports):
    """登記 → 要求 → 領走 → 刷 → 回報,全程走真的 HTTP 與真的 agent loop。"""
    http, agent = _stack_with_flasher(tmp_path, runner, ports)
    try:
        img = _register_image(http, tmp_path)
        lease = http.post(
            "/leases", json={"device_id": PIXEL, "user_id": "alice", "ttl_s": 600}
        ).json()
        agent.tick()          # 讓 coordinator 認得這台 host 上有這顆裝置

        resp = http.post("/flash", json={
            "device_id": PIXEL, "image_id": "shiba-vendor-v3",
            "user_id": "alice", "lease_id": lease["id"],
        })
        assert resp.status_code == 202, resp.text
        flash_id = resp.json()["id"]
        assert http.get(f"/flash/{flash_id}").json()["state"] == "pending"

        # 這一輪 exporter 領到工作並執行(結果下一輪才回報)。
        result = agent.tick()
        assert result.flashed == [PIXEL]
        assert runner.started[-1].argv == [
            "fastboot", "-s", SERIAL, "flash", "vendor", str(img)
        ]

        agent.tick()          # 回報結果
        assert http.get(f"/flash/{flash_id}").json()["state"] == "done"

        events = http.get(f"/devices/{PIXEL}/events").json()
        kinds = [e["kind"] for e in events]
        assert "flash_start" in kinds and "flash_result" in kinds
    finally:
        http.__exit__(None, None, None)


def test_a_tampered_image_is_never_flashed(tmp_path, runner, ports):
    """Registry 說雜湊是 X,exporter 手上那份不是 X——絕不刷,而且裝置
    被扣在 maintenance,不會在 lease 結束後交給下一個 agent。"""
    http, agent = _stack_with_flasher(tmp_path, runner, ports, digest="d" * 64)
    try:
        _register_image(http, tmp_path)       # 登記的是 DIGEST,exporter 算出 d*64
        lease = http.post(
            "/leases", json={"device_id": PIXEL, "user_id": "alice", "ttl_s": 600}
        ).json()
        agent.tick()
        flash_id = http.post("/flash", json={
            "device_id": PIXEL, "image_id": "shiba-vendor-v3",
            "user_id": "alice", "lease_id": lease["id"],
        }).json()["id"]

        agent.tick()
        assert all(h.argv[0] != "fastboot" for h in runner.started)

        agent.tick()
        assert http.get(f"/flash/{flash_id}").json()["state"] == "failed"

        http.post(f"/leases/{lease['id']}/release", json={"user_id": "alice"})
        state = next(d for d in http.get("/devices").json()
                     if d["id"] == PIXEL)["state"]
        assert state == "maintenance"
    finally:
        http.__exit__(None, None, None)


def test_flash_without_a_lease_is_refused(tmp_path, runner, ports):
    """破壞性操作一定要有 lease——這是 §6 把 flash 列為 mediated 的理由。"""
    http, agent = _stack_with_flasher(tmp_path, runner, ports)
    try:
        _register_image(http, tmp_path)
        resp = http.post("/flash", json={
            "device_id": PIXEL, "image_id": "shiba-vendor-v3", "user_id": "mallory",
        })
        assert resp.status_code == 404
        agent.tick()
        assert all(h.argv[0] != "fastboot" for h in runner.started)
    finally:
        http.__exit__(None, None, None)


# ------------------------------------------ ephemeral VM 池(§10/§12)
# 借 template → coordinator 建實例 row → exporter spawn qemu → 回報 →
# release → 標成該銷毀 → exporter 停掉 → 回報 gone → 實例退場。
# 「起 qemu」那層仍是假的:這台機器上跑真的 VM 對單元測試來說太重,而且
# 收斂邏輯本身跟 qemu 起不起得來無關。

TEMPLATE = "vm-pool-x86"
K14700 = "alanhc-14700"


def _stack_with_vms(tmp_path, runner, ports):
    from fastapi.testclient import TestClient

    from coordinator.api import create_app
    from exporter.vm import VMManager

    app = create_app(str(tmp_path / "vm.db"))
    http = TestClient(app)
    http.__enter__()
    # 這台 exporter 代表 14700(template 掛在那裡),不是 ROG。
    agent = ExporterAgent(
        ASGIClient(http, K14700),
        ServiceManager(runner, port_fn=ports),
        [StubScanner([])],
        vms=VMManager(runner, port_fn=lambda: 5901),
    )
    return http, agent


def _add_template(http):
    """直接寫 DB:template 的登記是 adopt 流程的事,不是這個測試的主題。"""
    conn = http.app.state.conn
    conn.execute(
        "INSERT INTO devices (id, class, control, identifier, provisioning, host, "
        "tags, state) VALUES (?, 'qemu-template', 'qemu-spawn', ?, 'ephemeral', ?, "
        "?, 'free')",
        (TEMPLATE, f"{TEMPLATE}:template", K14700,
         '{"image":"/srv/vm/debian.qcow2","arch":"x86_64","accel":"kvm"}'),
    )
    conn.commit()


def test_ephemeral_vm_lifecycle_end_to_end(tmp_path, runner, ports):
    http, agent = _stack_with_vms(tmp_path, runner, ports)
    try:
        _add_template(http)
        lease = http.post(
            "/leases", json={"device_id": TEMPLATE, "user_id": "alice", "ttl_s": 600}
        ).json()

        # 借 template 就生出一台實例,等 exporter 把它跑起來。
        instances = http.get("/instances").json()
        assert [i["state"] for i in instances] == ["requested"]
        instance_id = instances[0]["id"]

        result = agent.tick()
        assert result.spawned == [instance_id]
        assert runner.started[-1].argv[0] == "qemu-system-x86_64"

        agent.tick()          # 回報 running
        assert http.get("/instances").json()[0]["state"] == "running"

        # release:VM 該被銷毀(§12「直接銷毀重生,天然乾淨」)。
        http.post(f"/leases/{lease['id']}/release", json={"user_id": "alice"})
        assert http.get("/instances").json()[0]["state"] == "stopping"

        result = agent.tick()
        assert result.destroyed == [instance_id]
        assert runner.started[-1].terminated

        agent.tick()          # 回報 gone
        assert http.get("/instances").json() == []

        # 退場的實例不該再出現在裝置清單裡。
        assert all(d["id"] != instance_id for d in http.get("/devices").json())
        # 但稽核紀錄留著(§8 append-only)。
        kinds = [e["kind"] for e in http.get(f"/devices/{instance_id}/events").json()]
        assert kinds == ["device_attached", "device_detached"]
    finally:
        http.__exit__(None, None, None)


def test_the_template_can_be_leased_again_after_the_vm_is_gone(tmp_path, runner,
                                                               ports):
    """template 不是消耗品——它只是「可以生出這種 VM」的宣告。"""
    http, agent = _stack_with_vms(tmp_path, runner, ports)
    try:
        _add_template(http)
        first = http.post(
            "/leases", json={"device_id": TEMPLATE, "user_id": "alice", "ttl_s": 600}
        ).json()
        agent.tick()
        http.post(f"/leases/{first['id']}/release", json={"user_id": "alice"})
        agent.tick()
        agent.tick()

        second = http.post(
            "/leases", json={"device_id": TEMPLATE, "user_id": "bob", "ttl_s": 600}
        )
        assert second.status_code == 201, second.text
        assert [i["id"] for i in http.get("/instances").json()] == [
            f"{TEMPLATE}-0002"
        ]
    finally:
        http.__exit__(None, None, None)


# --------------------------- Cuttlefish 池:ephemeral 的第二種 provisioner
# §12:Android userspace 的 `any` 工作導去 AVD 平行跑,真機 Pixel 只留
# kernel/thermal/perf。完整一圈:借 template → 生實例 → cvd create →
# 回報 adb 位址 → coordinator 據此 desired 出 adb 服務 → exporter 起
# --one-device server → release → 銷毀。
#
# 「起 cvd」那層是假的,但**參數本身在真硬體上驗過**(見 exporter README
# 的驗證狀態表):真的開了一台 aosp_cf_x86_64_only_phone 起來,adb 連得上、
# getprop 回得出 Cuttlefish x86_64 phone / Android 16。

CF_TEMPLATE = "cf-pool"
CF_SPEC = ('{"host_path":"/home/alanhc/cf","product_path":"/home/alanhc/cf",'
           '"memory_mb":4096}')


class StubFleet:
    """假的 cvd fleet:記著哪些 group 被 create 過。"""

    def __init__(self):
        self.groups: set[str] = set()


def _stack_with_cvd(tmp_path, runner, ports):
    from fastapi.testclient import TestClient

    from coordinator.api import create_app
    from exporter.cvd import CvdManager

    app = create_app(str(tmp_path / "cf.db"))
    http = TestClient(app)
    http.__enter__()
    fleet = StubFleet()

    class TrackingRunner:
        """包一層:cvd create 成功後把 group 記進 fleet,模擬常駐 server。"""

        def __init__(self, inner):
            self._inner = inner
            self.started = inner.started

        def start(self, argv):
            handle = self._inner.start(argv)
            if "create" in argv and "--group_name" in argv:
                fleet.groups.add(argv[argv.index("--group_name") + 1])
            elif "stop" in argv and "--group_name" in argv:
                fleet.groups.discard(argv[argv.index("--group_name") + 1])
            return handle

        def which(self, program):
            return self._inner.which(program)

        def output(self, argv, timeout_s=30.0):
            return self._inner.output(argv, timeout_s)

    tracking = TrackingRunner(runner)
    agent = ExporterAgent(
        ASGIClient(http, K14700),
        ServiceManager(tracking, port_fn=ports),
        [StubScanner([])],
        cvds=CvdManager(tracking, fleet_fn=lambda: set(fleet.groups)),
    )
    return http, agent


def _add_cf_template(http):
    """seed 已經有 cf-pool(14700 上的 Cuttlefish 池),確認它在就好。

    重建會撞 devices.identifier 的 UNIQUE——而且測試該用真的部署形狀,
    不是自己另外造一個。"""
    row = http.app.state.conn.execute(
        "SELECT host FROM devices WHERE id = ?", (CF_TEMPLATE,)
    ).fetchone()
    assert row is not None and row["host"] == K14700


def test_cuttlefish_instance_becomes_a_usable_adb_device(tmp_path, runner, ports):
    """整條路的重點:借一台 AVD,最後拿到的是一個 adb endpoint——跟借真
    Pixel 拿到的東西一模一樣。"""
    http, agent = _stack_with_cvd(tmp_path, runner, ports)
    try:
        _add_cf_template(http)
        lease = http.post(
            "/leases", json={"device_id": CF_TEMPLATE, "user_id": "alice",
                             "ttl_s": 600}
        ).json()
        instance_id = http.get("/instances").json()[0]["id"]

        # 第一輪:cvd create。
        result = agent.tick()
        assert result.spawned == [instance_id]
        create = [h for h in runner.started if "create" in h.argv][-1]
        assert create.argv[0] == "cvd"
        assert create.argv[create.argv.index("--base_instance_num") + 1] == "1"

        # 第二輪:回報 running + adb 位址,coordinator 據此 desired 出 adb,
        # 而收斂就在同一輪發生(回報先於算 desired,見 store 的 process())。
        result = agent.tick()
        assert http.get("/instances").json()[0]["state"] == "running"
        assert (instance_id, "adb") in result.started
        adb = [h for h in runner.started if h.argv[0] == "adb"
               and "--one-device" in h.argv][-1]
        assert adb.argv[adb.argv.index("--one-device") + 1] == "127.0.0.1:6520"

        # endpoint 進 device_services,client 可以直接貼著用。
        agent.tick()
        endpoint = http.app.state.conn.execute(
            "SELECT endpoint FROM device_services WHERE device_id = ? "
            "AND service = 'adb'", (instance_id,)
        ).fetchone()
        assert endpoint is not None and endpoint["endpoint"].endswith(":9000")

        # release:實例銷毀,服務跟著停。
        http.post(f"/leases/{lease['id']}/release", json={"user_id": "alice"})
        result = agent.tick()
        assert result.destroyed == [instance_id]
        stop = [h for h in runner.started if "stop" in h.argv][-1]
        assert stop.argv[0] == "cvd"

        agent.tick()
        assert http.get("/instances").json() == []
    finally:
        http.__exit__(None, None, None)


def test_adb_is_not_desired_before_the_instance_reports_its_port(
    tmp_path, runner, ports
):
    """實例還沒生出來就沒有 adb port。送 desired 的話 exporter 會拿實例 id
    當 adb serial 去連,每輪失敗一次。"""
    http, agent = _stack_with_cvd(tmp_path, runner, ports)
    try:
        _add_cf_template(http)
        http.post("/leases", json={"device_id": CF_TEMPLATE, "user_id": "alice",
                                   "ttl_s": 600})
        result = agent.tick()          # 只 create,還沒回報
        assert not result.started      # 沒有任何服務被起來
        assert all("--one-device" not in h.argv for h in runner.started)
    finally:
        http.__exit__(None, None, None)
