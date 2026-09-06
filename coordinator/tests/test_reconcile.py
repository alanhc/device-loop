"""Coordinator 側的 reconcile:desired state 與 exporter 回報的 endpoint。

Exporter 不被推送,它每次 heartbeat 拿到「應該要跑什麼」自行收斂,並把
實際起好的 host:port 回報上來。
"""

from __future__ import annotations

from coordinator import store
from coordinator.store import HeartbeatProcessor, desired_services

PIXEL = "pixel8-shiba"
SERIAL = "38011FDJH00C9F"
ROG = "rog-laptop"


def test_no_lease_means_nothing_should_run(conn):
    assert desired_services(conn, ROG) == []


def test_active_lease_makes_the_class_services_desired(conn):
    store.reserve(conn, PIXEL, "alice", 600)
    desired = desired_services(conn, ROG)
    assert desired == [
        {"device_id": PIXEL, "service": "adb", "identifier": SERIAL,
         "lease_id": desired[0]["lease_id"]}
    ]


def test_desired_is_scoped_to_the_host_that_asks(conn):
    """別台 host 上的 lease 不該出現在這台的 desired 裡。"""
    store.reserve(conn, PIXEL, "alice", 600)
    assert desired_services(conn, "alanhc-14700") == []


def test_class_without_exporter_services_yields_nothing(conn):
    """SSH 上的 SBC 沒有 exporter 代管的服務,借了也不用起 daemon。"""
    store.reserve(conn, "milkv-jupiter", "alice", 600)
    assert desired_services(conn, "alanhc-14700") == []


def test_releasing_removes_it_from_desired(conn):
    lease = store.reserve(conn, PIXEL, "alice", 600)
    assert desired_services(conn, ROG) != []
    store.release(conn, lease["id"])
    assert desired_services(conn, ROG) == []


def test_moving_a_device_moves_the_desired_service(conn):
    """裝置搬到別台 host:desired 跟著搬,舊 host 不該再起它。"""
    store.reserve(conn, PIXEL, "alice", 600)
    HeartbeatProcessor().process(conn, "alanhc-14700", [SERIAL], ["android"])
    assert desired_services(conn, ROG) == []
    assert [d["device_id"] for d in desired_services(conn, "alanhc-14700")] == [PIXEL]


# ------------------------------------------------ exporter 回報的 endpoint

def _heartbeat(conn, services, host=ROG, idents=(SERIAL,)):
    return HeartbeatProcessor().process(
        conn, host, list(idents), ["android"], services
    )


def test_reported_port_becomes_an_endpoint_on_the_tailscale_ip(conn):
    """client 要能直接 `adb connect` 貼上去用,所以是 IP 不是 host id。"""
    store.reserve(conn, PIXEL, "alice", 600)
    _heartbeat(conn, [{"device_id": PIXEL, "service": "adb", "port": 9001}])
    row = conn.execute(
        "SELECT endpoint FROM device_services WHERE device_id = ?", (PIXEL,)
    ).fetchone()
    assert row["endpoint"] == "100.71.211.115:9001"


def test_a_full_endpoint_can_be_reported_directly(conn):
    store.reserve(conn, PIXEL, "alice", 600)
    _heartbeat(conn, [{"device_id": PIXEL, "service": "adb",
                       "endpoint": "100.71.211.115:9999"}])
    row = conn.execute(
        "SELECT endpoint FROM device_services WHERE device_id = ?", (PIXEL,)
    ).fetchone()
    assert row["endpoint"] == "100.71.211.115:9999"


def test_reporting_a_service_with_no_lease_is_ignored(conn):
    """exporter 可能還在收斂上一輪(lease 沒了但 daemon 還沒停),
    它回報的東西不能無條件當真。"""
    result = _heartbeat(conn, [{"device_id": PIXEL, "service": "adb", "port": 9001}])
    assert result["recorded"] == []
    assert conn.execute("SELECT COUNT(*) FROM device_services").fetchone()[0] == 0


def test_endpoint_is_dropped_when_the_lease_ends(conn):
    """endpoint 是 lease 期間才有效的動態值,留著會讓 client 連到停掉的 port。"""
    lease = store.reserve(conn, PIXEL, "alice", 600)
    _heartbeat(conn, [{"device_id": PIXEL, "service": "adb", "port": 9001}])
    store.release(conn, lease["id"])
    result = _heartbeat(conn, [{"device_id": PIXEL, "service": "adb", "port": 9001}])
    assert result["dropped"] == [f"{PIXEL}/adb"]
    assert conn.execute("SELECT COUNT(*) FROM device_services").fetchone()[0] == 0


def test_endpoint_is_dropped_when_the_exporter_stops_reporting_it(conn):
    """daemon 死掉、exporter 不再回報 → endpoint 要消失,不能繼續給 client。"""
    store.reserve(conn, PIXEL, "alice", 600)
    _heartbeat(conn, [{"device_id": PIXEL, "service": "adb", "port": 9001}])
    result = _heartbeat(conn, [])
    assert result["dropped"] == [f"{PIXEL}/adb"]
    assert conn.execute("SELECT COUNT(*) FROM device_services").fetchone()[0] == 0


def test_a_changed_port_replaces_the_old_endpoint(conn):
    """exporter 重起後 port 會不一樣,要蓋掉舊的而不是留兩筆。"""
    store.reserve(conn, PIXEL, "alice", 600)
    _heartbeat(conn, [{"device_id": PIXEL, "service": "adb", "port": 9001}])
    _heartbeat(conn, [{"device_id": PIXEL, "service": "adb", "port": 9002}])
    rows = conn.execute(
        "SELECT endpoint FROM device_services WHERE device_id = ?", (PIXEL,)
    ).fetchall()
    assert [r["endpoint"] for r in rows] == ["100.71.211.115:9002"]


def test_steady_state_reporting_is_quiet(conn):
    """同樣的 endpoint 重複回報不該每輪都寫一筆 event——heartbeat 很頻繁,
    events 是稽核紀錄不是 log。"""
    store.reserve(conn, PIXEL, "alice", 600)
    svc = [{"device_id": PIXEL, "service": "adb", "port": 9001}]
    _heartbeat(conn, svc)
    before = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    _heartbeat(conn, svc)
    _heartbeat(conn, svc)
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == before


def test_service_start_and_stop_are_audited(conn):
    """§8:exporter 每次啟停 per-resource daemon 都要留下稽核紀錄。"""
    lease = store.reserve(conn, PIXEL, "alice", 600)
    _heartbeat(conn, [{"device_id": PIXEL, "service": "adb", "port": 9001}])
    store.release(conn, lease["id"])
    _heartbeat(conn, [])
    kinds = [
        r["kind"]
        for r in conn.execute(
            "SELECT kind FROM events WHERE device_id = ? ORDER BY id", (PIXEL,)
        )
    ]
    assert "service_start" in kinds and "service_stop" in kinds


def test_heartbeat_returns_desired_for_the_exporter_to_converge_on(conn):
    store.reserve(conn, PIXEL, "alice", 600)
    result = _heartbeat(conn, [])
    assert [(d["device_id"], d["service"]) for d in result["desired"]] == [(PIXEL, "adb")]


# ---------------------------------------------------- mediated 的權責歸屬
# mediated 是能力種類的靜態性質(flash 恆為 true),不是某次 lease 的動態
# 值。由 coordinator 說了算——讓 exporter 宣告自己是否需要 mediation,
# 等於把安全相關的事實交給被管制的一方。

def test_mediated_is_filled_by_the_coordinator(conn):
    store.reserve(conn, PIXEL, "alice", 600)
    _heartbeat(conn, [{"device_id": PIXEL, "service": "adb", "port": 9001}])
    row = conn.execute(
        "SELECT mediated FROM device_services WHERE device_id = ?", (PIXEL,)
    ).fetchone()
    assert row["mediated"] == 0        # adb 是直連


def test_mediated_lookup_matches_the_design_doc():
    """§6:flash 是唯一必須 mediated 的能力;其餘 client 直連。"""
    assert store.is_mediated("flash")
    assert not store.is_mediated("adb")
    assert not store.is_mediated("uart")


def test_exporter_cannot_declare_a_service_unmediated(conn):
    """就算 exporter 硬報 mediated=False,coordinator 也不採信。

    Phase 1 沒有 flash 能力,所以這裡直接呼叫 record_services 模擬
    Phase 2 的形狀,確認權責現在就分對了。
    """
    lease = store.reserve(conn, PIXEL, "alice", 600)
    conn.execute("UPDATE devices SET class = 'android' WHERE id = ?", (PIXEL,))
    # 假裝 android 也匯出 flash,讓它進 desired
    original = store.CLASS_SERVICES["android"]
    store.CLASS_SERVICES["android"] = ("adb", "flash")
    try:
        store.record_services(
            conn, ROG,
            [{"device_id": PIXEL, "service": "flash", "port": 9100,
              "mediated": False}],          # exporter 說「我不用 mediation」
            "2026-09-05T12:00:00+00:00",
        )
    finally:
        store.CLASS_SERVICES["android"] = original
    row = conn.execute(
        "SELECT mediated FROM device_services WHERE device_id = ? AND service = 'flash'",
        (PIXEL,),
    ).fetchone()
    assert row["mediated"] == 1           # coordinator 說了算
    assert lease["id"] > 0


# ------------------------------ Phase 3:能力專屬的識別碼(§6 video/vnc)
# adb 要 USB serial,但 camera 要 /dev/video0、VNC 要上游的 host:port。
# 那些值放在 devices.tags,desired_services 據此覆寫送給 exporter 的
# identifier——不然 exporter 會拿裝置的 adb serial 去開 camera。

def _tag(conn, device_id, tags):
    import json

    conn.execute("UPDATE devices SET tags = ? WHERE id = ?",
                 (json.dumps(tags), device_id))
    conn.commit()


def test_vnc_identifier_comes_from_the_tag_not_the_device(conn, clock):
    """§6 vnc 的三種來源都是「裝置端已經有一個 VNC server 在聽」,
    exporter 要知道的是那個上游位址。"""
    _tag(conn, "milkv-jupiter", {"vnc_upstream": "100.101.114.46:5900"})
    store.reserve(conn, "milkv-jupiter", "alice", 600, now_fn=clock)
    desired = store.desired_services(conn, "alanhc-14700")
    vnc = [d for d in desired if d["service"] == "vnc"]
    assert len(vnc) == 1
    assert vnc[0]["identifier"] == "100.101.114.46:5900"


def test_a_capability_without_its_tag_is_not_desired(conn, clock):
    """一台沒接 camera 的裝置不該被要求起 video daemon。少了這條,
    exporter 會拿 adb serial 去開 camera,每輪失敗一次。"""
    _tag(conn, "milkv-jupiter", {"arch": "riscv64"})     # 沒有 vnc_upstream
    store.reserve(conn, "milkv-jupiter", "alice", 600, now_fn=clock)
    desired = store.desired_services(conn, "alanhc-14700")
    assert [d["service"] for d in desired] == []


def test_adb_still_uses_the_device_identifier(conn, clock):
    """只有列在 SERVICE_IDENTIFIER_TAGS 裡的能力才覆寫。"""
    store.reserve(conn, "pixel8-shiba", "alice", 600, now_fn=clock)
    desired = store.desired_services(conn, "rog-laptop")
    assert desired[0]["identifier"] == "38011FDJH00C9F"


def test_malformed_tags_do_not_break_the_heartbeat(conn, clock):
    """tags 是自由格式 JSON;壞掉的值不該讓整輪 heartbeat 失敗。"""
    conn.execute("UPDATE devices SET tags = 'not json' WHERE id = 'milkv-jupiter'")
    conn.commit()
    store.reserve(conn, "milkv-jupiter", "alice", 600, now_fn=clock)
    assert store.desired_services(conn, "alanhc-14700") == []
