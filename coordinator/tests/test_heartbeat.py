"""Exporter heartbeat 的 inventory diff(§8):attached / detached / moved。"""

from coordinator import store

PIXEL = "pixel8-shiba"
PIXEL_SERIAL = "38011FDJH00C9F"
ROG = "rog-laptop"
H14700 = "alanhc-14700"


def _events(conn, device_id=PIXEL):
    return [
        (r["kind"], r["detail"])
        for r in conn.execute(
            "SELECT kind, detail FROM events WHERE device_id = ? ORDER BY id",
            (device_id,),
        )
    ]


def _kinds(conn, device_id=PIXEL):
    return [k for k, _ in _events(conn, device_id)]


def _device(conn, device_id=PIXEL):
    return conn.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone()


def _hb(clock, grace_s=30):
    return store.HeartbeatProcessor(grace_s=grace_s, now_fn=clock)


# ------------------------------------------------------------- 基本回報

def test_heartbeat_updates_host_and_device_last_seen(conn, clock):
    hb = _hb(clock)
    result = hb.process(conn, ROG, [PIXEL_SERIAL])

    # 只斷言 diff 的部分;回應另外帶 reconcile 用的 desired/recorded/dropped。
    assert {k: result[k] for k in ("moved", "attached", "unregistered", "detached")} == {
        "moved": [], "attached": [], "unregistered": [], "detached": []
    }
    assert _device(conn)["last_seen_at"] == clock()
    host = conn.execute("SELECT * FROM hosts WHERE id = ?", (ROG,)).fetchone()
    assert host["last_seen_at"] == clock()
    # 一切如常時不該產生任何 event
    assert _kinds(conn) == []


def test_heartbeat_from_unknown_host_raises(conn, clock):
    import pytest

    with pytest.raises(store.NotFound):
        _hb(clock).process(conn, "no-such-host", [])


# ---------------------------------------------------------------- attached

def test_unregistered_identifier_creates_device_awaiting_adoption(conn, clock):
    """沒見過的識別碼 → 建 row 等 adopt,不是丟掉。"""
    hb = _hb(clock)
    result = hb.process(conn, ROG, [PIXEL_SERIAL, "0123456789ABCDEF"])

    assert result["unregistered"] == ["0123456789ABCDEF"]
    new = _device(conn, "0123456789ABCDEF")
    assert new["state"] == "unregistered"
    assert new["identifier"] == "0123456789ABCDEF"
    assert new["host"] == ROG
    assert _kinds(conn, "0123456789ABCDEF") == ["device_attached"]


def test_known_identifier_seen_again_is_not_reattached(conn, clock):
    hb = _hb(clock)
    hb.process(conn, ROG, [PIXEL_SERIAL, "0123456789ABCDEF"])
    clock.advance(10)
    result = hb.process(conn, ROG, [PIXEL_SERIAL, "0123456789ABCDEF"])

    assert result["unregistered"] == []
    assert _kinds(conn, "0123456789ABCDEF") == ["device_attached"]


def test_offline_device_reappearing_is_attached_and_freed(conn, clock):
    conn.execute("UPDATE devices SET state = 'offline' WHERE id = ?", (PIXEL,))
    result = _hb(clock).process(conn, ROG, [PIXEL_SERIAL])

    assert result["attached"] == [PIXEL]
    assert _device(conn)["state"] == "free"
    assert _kinds(conn) == ["device_attached"]


# ---------------------------------------------------------------- detached

def test_missing_device_detaches_only_after_grace_window(conn, clock):
    hb = _hb(clock, grace_s=30)
    hb.process(conn, ROG, [PIXEL_SERIAL])

    clock.advance(10)
    result = hb.process(conn, ROG, [])  # 不見了,但還在 grace 內
    assert result["detached"] == []
    assert _device(conn)["state"] == "free"
    assert hb.pending_detach == {PIXEL: "2026-09-05T12:00:10+00:00"}

    clock.advance(31)
    result = hb.process(conn, ROG, [])
    assert result["detached"] == [PIXEL]
    assert _device(conn)["state"] == "offline"
    assert _kinds(conn) == ["device_detached"]
    assert hb.pending_detach == {}


def test_device_reappearing_within_grace_never_detaches(conn, clock):
    hb = _hb(clock, grace_s=30)
    hb.process(conn, ROG, [PIXEL_SERIAL])

    clock.advance(10)
    hb.process(conn, ROG, [])  # 一次漏報(udev 抖動)

    clock.advance(10)
    result = hb.process(conn, ROG, [PIXEL_SERIAL])
    assert result["detached"] == []
    assert hb.pending_detach == {}
    assert _device(conn)["state"] == "free"
    assert _kinds(conn) == []


def test_reaper_tick_finalizes_detach_without_new_heartbeat(conn, clock):
    """host 整台掛掉時不會再有 heartbeat,grace 由 reaper tick 結算。"""
    hb = _hb(clock, grace_s=30)
    hb.process(conn, ROG, [PIXEL_SERIAL])
    clock.advance(5)
    hb.process(conn, ROG, [])

    clock.advance(60)
    assert hb.finalize_detaches(conn) == [PIXEL]
    conn.commit()
    assert _device(conn)["state"] == "offline"


def test_usb_scan_does_not_detach_ssh_or_local_devices(conn, clock):
    """exporter 掃 USB 列不出 SSH 上的 SBC 或本機 CPU,不能誤判成拔線。"""
    hb = _hb(clock, grace_s=30)
    hb.process(conn, H14700, [])  # 14700 上有 jupiter / 5070ti / raptor

    clock.advance(60)
    assert hb.finalize_detaches(conn) == []
    conn.commit()
    for device_id in ("milkv-jupiter", "14700-5070ti", "14700-raptor"):
        assert _device(conn, device_id)["state"] == "free"
        assert _kinds(conn, device_id) == []


def test_explicit_class_scope_narrows_detach_detection(conn, clock):
    """回報可明講涵蓋範圍;範圍外的 class 不參與缺席判定。"""
    hb = _hb(clock, grace_s=30)
    hb.process(conn, ROG, [PIXEL_SERIAL], discoverable_classes=["android"])

    clock.advance(10)
    hb.process(conn, ROG, [], discoverable_classes=["android"])
    assert set(hb.pending_detach) == {PIXEL}  # zen4 是 x86-cpu,不在範圍內

    clock.advance(31)
    assert hb.finalize_detaches(conn) == [PIXEL]
    conn.commit()
    assert _device(conn, "rog-zen4")["state"] == "free"


# ------------------------------------------------------------------- moved

def test_device_on_new_host_is_moved_not_reattached(conn, clock):
    """已知裝置出現在新 host → 更新 host 欄位並寫 device_moved。"""
    hb = _hb(clock)
    hb.process(conn, ROG, [PIXEL_SERIAL])

    clock.advance(10)
    result = hb.process(conn, H14700, [PIXEL_SERIAL])

    assert result["moved"] == [PIXEL]
    assert result["attached"] == []
    assert result["unregistered"] == []
    assert _device(conn)["host"] == H14700

    kinds = _kinds(conn)
    assert kinds == ["device_moved"]
    detail = _events(conn)[0][1]
    assert '"from": "rog-laptop"' in detail
    assert '"to": "alanhc-14700"' in detail


def test_move_converges_to_single_event_across_split_reports(conn, clock):
    """驗收 case:兩台 exporter 各自回報,時序不定。

    Pixel 從 ROG 拔掉插到 14700。ROG 先回報「看不到了」,14700 隨後
    回報「我看到了」。必須收斂成一筆 device_moved,不能是
    device_detached + device_attached 兩筆。
    """
    hb = _hb(clock, grace_s=30)
    hb.process(conn, ROG, [PIXEL_SERIAL])

    clock.advance(10)
    r1 = hb.process(conn, ROG, [])  # A 說不見了 → 進 grace,先不寫事件
    assert r1["detached"] == []

    clock.advance(5)
    r2 = hb.process(conn, H14700, [PIXEL_SERIAL])  # B 說看到了(仍在 grace 內)
    assert r2["moved"] == [PIXEL]

    clock.advance(60)  # grace 早就過了,但不該再補寫 detach
    assert hb.finalize_detaches(conn) == []
    conn.commit()

    assert _kinds(conn) == ["device_moved"]
    assert _device(conn)["host"] == H14700
    assert _device(conn)["state"] == "free"


def test_move_converges_when_new_host_reports_first(conn, clock):
    """反向時序:14700 先回報看到,ROG 之後才回報看不到。"""
    hb = _hb(clock, grace_s=30)
    hb.process(conn, ROG, [PIXEL_SERIAL])

    clock.advance(10)
    assert hb.process(conn, H14700, [PIXEL_SERIAL])["moved"] == [PIXEL]

    clock.advance(5)
    # ROG 回報看不到,但裝置的 host 欄位已經指向 14700,不算它該看到的
    assert hb.process(conn, ROG, [])["detached"] == []

    clock.advance(60)
    assert hb.finalize_detaches(conn) == []
    conn.commit()

    assert _kinds(conn) == ["device_moved"]
    assert _device(conn)["host"] == H14700


def test_move_during_active_lease_keeps_lease_and_records_event(conn, clock):
    """§8:搬機發生在 lease 進行中時,endpoint 失效但 lease 仍在(Phase 1 只記事件)。"""
    lease = store.reserve(conn, PIXEL, "alanhc", 300, None, clock)
    hb = _hb(clock)
    hb.process(conn, ROG, [PIXEL_SERIAL])

    clock.advance(10)
    assert hb.process(conn, H14700, [PIXEL_SERIAL])["moved"] == [PIXEL]

    assert _kinds(conn) == ["reserve", "device_moved"]
    assert _device(conn)["state"] == "leased"
    row = conn.execute("SELECT * FROM leases WHERE id = ?", (lease["id"],)).fetchone()
    assert row["status"] == "active"


# ------------------- `seen`:證明活著但不宣稱擁有(§5 / tailnet-native)
# Jupiter 自己在 tailnet 上,沒有 exporter 代理(§10 已解決)。沒有這條路徑
# 的話它的 last_seen_at 永遠是 NULL,§8 的 reaper 偵測不到它離線。

JUPITER = "milkv-jupiter"
K14700 = "alanhc-14700"


def test_seen_refreshes_last_seen_without_claiming_the_device(conn, clock):
    """關鍵:更新 last_seen_at,但 host 不動。"""
    before = conn.execute(
        "SELECT host FROM devices WHERE id = ?", (JUPITER,)
    ).fetchone()["host"]
    result = _hb(clock).process(conn, ROG, [], ["android"], seen=[JUPITER])
    row = conn.execute(
        "SELECT host, last_seen_at FROM devices WHERE id = ?", (JUPITER,)
    ).fetchone()
    assert row["last_seen_at"] == clock()
    # ROG 回報看到它,但它的 host 沒有變成 rog-laptop——§5:可達性不能
    # 決定擁有權,不然三台 host 都看得到它就會來回搬機。
    assert row["host"] == before
    assert result["refreshed"] == [JUPITER]


def test_seen_never_creates_unregistered_rows(conn, clock):
    """tailnet 上有二十幾個節點(iPhone、別人的筆電)。全建成裝置的話
    裝置池會瞬間被垃圾填滿,而且每一台都被宣稱屬於回報者。"""
    before = conn.execute("SELECT COUNT(*) FROM devices").fetchone()[0]
    result = _hb(clock).process(
        conn, ROG, [], ["android"], seen=["iphone181", "laptop-u9mms3ki"]
    )
    assert conn.execute("SELECT COUNT(*) FROM devices").fetchone()[0] == before
    assert result["unregistered"] == []
    assert result["refreshed"] == []


def test_seen_does_not_trigger_absence_detection(conn, clock):
    """§5:掃描結果會因觀察點而不一致(macmini 掃不到 ROG,但 ROG 在)。
    所以「這次沒在 seen 裡」不能當離線證據。"""
    hb = _hb(clock)
    hb.process(conn, K14700, [], ["android"], seen=[JUPITER])
    clock.advance(1)
    hb.process(conn, K14700, [], ["android"], seen=[])      # 這輪沒看到
    clock.advance(3600)
    assert hb.finalize_detaches(conn) == []
    assert conn.execute(
        "SELECT state FROM devices WHERE id = ?", (JUPITER,)
    ).fetchone()["state"] == "free"


def test_a_tailnet_device_that_comes_back_returns_to_free(conn, clock):
    """reaper 依 last_seen_at 標了 offline,之後又看得到它——跟 USB 裝置
    重新插上同樣的處理。"""
    conn.execute(
        "UPDATE devices SET state = 'offline', last_seen_at = ? WHERE id = ?",
        (clock(), JUPITER),
    )
    conn.commit()
    clock.advance(60)
    result = _hb(clock).process(conn, K14700, [], ["android"], seen=[JUPITER])
    assert conn.execute(
        "SELECT state FROM devices WHERE id = ?", (JUPITER,)
    ).fetchone()["state"] == "free"
    assert result["refreshed"] == [JUPITER]
    kinds = [
        r["kind"] for r in conn.execute(
            "SELECT kind FROM events WHERE device_id = ? ORDER BY id", (JUPITER,)
        )
    ]
    assert "device_attached" in kinds


def test_the_reaper_can_finally_see_a_tailnet_device_go_offline(conn, clock):
    """整條路徑的重點:回報讓 last_seen_at 有值,reaper 才判定得出離線。
    沒有 seen 的話它永遠是 NULL,reap_stale 明確跳過 NULL。"""
    _hb(clock).process(conn, K14700, [], ["android"], seen=[JUPITER])
    clock.advance(3600)
    assert JUPITER in store.reap_stale(conn, 120, now_fn=clock)
    assert conn.execute(
        "SELECT state FROM devices WHERE id = ?", (JUPITER,)
    ).fetchone()["state"] == "offline"


def test_a_device_with_no_reporter_stays_undetectable(conn, clock):
    """反證:沒有任何回報的裝置,reaper 看不見它——這正是加 seen 要修的
    問題,留一條測試把它釘住。"""
    assert conn.execute(
        "SELECT last_seen_at FROM devices WHERE id = ?", (JUPITER,)
    ).fetchone()["last_seen_at"] is None
    assert store.reap_stale(conn, 120, now_fn=clock) == []
