"""強制回收的 coordinator 側(設計文件 §7 第 4 步)。

關鍵區分:lease **過期**只是「沒人租了」,**強制回收**是「這台可能是壞
的」。後者裝置要先扣在 maintenance,回收成功才放回 free——中間不能讓
下一個 agent 借到一台壞的。
"""

from __future__ import annotations

import pytest

from coordinator import store
from coordinator.store import (
    HeartbeatProcessor,
    NoPowerControl,
    escalate_stuck_devices,
    force_reclaim_lease,
    pending_reclaims,
    record_reclaim_results,
    request_reclaim,
)

PIXEL = "pixel8-shiba"
SERIAL = "38011FDJH00C9F"
ROG = "rog-laptop"
JUPITER = "milkv-jupiter"


def test_request_reclaim_uses_the_devices_power_control(conn, clock):
    action = request_reclaim(conn, PIXEL, "stuck", now_fn=clock)
    assert action["action"] == "adb-reboot"        # seed 裡 Pixel 就是這個
    assert action["state"] == "pending"


def test_reclaim_is_audited(conn, clock):
    request_reclaim(conn, PIXEL, "stuck", now_fn=clock)
    kinds = [r["kind"] for r in conn.execute(
        "SELECT kind FROM events WHERE device_id = ?", (PIXEL,))]
    assert "force_reclaim" in kinds


def test_only_one_outstanding_action_per_device(conn, clock):
    """重複下重開指令沒有意義,而且會讓「重開到一半又被重開」變可能。"""
    first = request_reclaim(conn, PIXEL, "stuck", now_fn=clock)
    again = request_reclaim(conn, PIXEL, "stuck again", now_fn=clock)
    assert first is not None
    assert again is None
    assert conn.execute(
        "SELECT COUNT(*) FROM reclaim_actions WHERE device_id = ?", (PIXEL,)
    ).fetchone()[0] == 1


def test_device_without_power_control_goes_to_maintenance(conn, clock):
    """§7:沒有 power_control 的裝置無法強制回收,標 maintenance 等人工——
    不能假裝收乾淨了放回 free。"""
    with pytest.raises(NoPowerControl):
        request_reclaim(conn, JUPITER, "stuck", now_fn=clock)
    state = conn.execute(
        "SELECT state FROM devices WHERE id = ?", (JUPITER,)
    ).fetchone()["state"]
    assert state == "maintenance"


def test_unregistered_device_raises_not_found(conn, clock):
    with pytest.raises(store.NotFound):
        request_reclaim(conn, "nope", "stuck", now_fn=clock)


# ------------------------------------------------------ force_reclaim_lease

def test_force_reclaim_marks_the_lease_and_holds_the_device(conn, clock):
    lease = store.reserve(conn, PIXEL, "alice", 600, now_fn=clock)
    out = force_reclaim_lease(conn, lease["id"], "client wedged", now_fn=clock)
    assert out["status"] == "force_reclaimed"
    state = conn.execute(
        "SELECT state FROM devices WHERE id = ?", (PIXEL,)
    ).fetchone()["state"]
    assert state == "maintenance"        # 不是 free:回收完成前不給人借


def test_a_force_reclaimed_device_cannot_be_reserved(conn, clock):
    lease = store.reserve(conn, PIXEL, "alice", 600, now_fn=clock)
    force_reclaim_lease(conn, lease["id"], "wedged", now_fn=clock)
    with pytest.raises(store.Conflict):
        store.reserve(conn, PIXEL, "bob", 600, now_fn=clock)


def test_successful_reclaim_returns_the_device_to_free(conn, clock):
    lease = store.reserve(conn, PIXEL, "alice", 600, now_fn=clock)
    force_reclaim_lease(conn, lease["id"], "wedged", now_fn=clock)
    action = conn.execute("SELECT * FROM reclaim_actions").fetchone()
    record_reclaim_results(conn, [{"reclaim_id": action["id"], "ok": True}], clock())
    conn.commit()
    state = conn.execute(
        "SELECT state FROM devices WHERE id = ?", (PIXEL,)
    ).fetchone()["state"]
    assert state == "free"
    assert store.reserve(conn, PIXEL, "bob", 600, now_fn=clock) is not None


def test_failed_reclaim_leaves_the_device_in_maintenance(conn, clock):
    """寧可少一台可用裝置,也不要把壞的交給下一個 agent。"""
    lease = store.reserve(conn, PIXEL, "alice", 600, now_fn=clock)
    force_reclaim_lease(conn, lease["id"], "wedged", now_fn=clock)
    action = conn.execute("SELECT * FROM reclaim_actions").fetchone()
    record_reclaim_results(
        conn, [{"reclaim_id": action["id"], "ok": False,
                "detail": {"exit_code": 1}}], clock()
    )
    conn.commit()
    state = conn.execute(
        "SELECT state FROM devices WHERE id = ?", (PIXEL,)
    ).fetchone()["state"]
    assert state == "maintenance"
    with pytest.raises(store.Conflict):
        store.reserve(conn, PIXEL, "bob", 600, now_fn=clock)


def test_reclaim_result_is_audited_with_the_outcome(conn, clock):
    lease = store.reserve(conn, PIXEL, "alice", 600, now_fn=clock)
    force_reclaim_lease(conn, lease["id"], "wedged", now_fn=clock)
    action = conn.execute("SELECT * FROM reclaim_actions").fetchone()
    record_reclaim_results(conn, [{"reclaim_id": action["id"], "ok": True}], clock())
    conn.commit()
    rows = [r for r in conn.execute(
        "SELECT detail FROM events WHERE device_id = ? AND kind = 'force_reclaim'",
        (PIXEL,))]
    assert any('"ok": true' in (r["detail"] or "") for r in rows)


def test_a_result_is_only_applied_once(conn, clock):
    lease = store.reserve(conn, PIXEL, "alice", 600, now_fn=clock)
    force_reclaim_lease(conn, lease["id"], "wedged", now_fn=clock)
    action = conn.execute("SELECT * FROM reclaim_actions").fetchone()
    res = [{"reclaim_id": action["id"], "ok": True}]
    assert record_reclaim_results(conn, res, clock()) == [action["id"]]
    assert record_reclaim_results(conn, res, clock()) == []


# ------------------------------------------------------------- 升級的判準

def _expire(conn, lease_id, clock):
    conn.execute("UPDATE leases SET expires_at = ? WHERE id = ?",
                 ("2000-01-01T00:00:00+00:00", lease_id))
    conn.commit()
    store.reap_expired_leases(conn, now_fn=clock)


def test_normal_expiry_does_not_escalate(conn, clock):
    """服務有正常收斂掉的話,不需要動用 power_control。"""
    lease = store.reserve(conn, PIXEL, "alice", 600, now_fn=clock)
    _expire(conn, lease["id"], clock)
    assert escalate_stuck_devices(conn, 60, now_fn=clock) == []
    assert conn.execute("SELECT COUNT(*) FROM reclaim_actions").fetchone()[0] == 0


def test_services_still_running_after_expiry_escalates(conn, clock):
    """endpoint 還在 = exporter 沒收斂成功,這才是要動用 power_control 的情況。"""
    lease = store.reserve(conn, PIXEL, "alice", 600, now_fn=clock)
    HeartbeatProcessor(now_fn=clock).process(
        conn, ROG, [SERIAL], ["android"],
        [{"device_id": PIXEL, "service": "adb", "port": 9001}],
    )
    _expire(conn, lease["id"], clock)
    clock.advance(120)
    assert escalate_stuck_devices(conn, 60, now_fn=clock) == [PIXEL]
    assert conn.execute("SELECT COUNT(*) FROM reclaim_actions").fetchone()[0] == 1


def test_escalation_waits_for_the_grace_period(conn, clock):
    """剛過期就升級的話,exporter 根本還沒機會收斂。"""
    lease = store.reserve(conn, PIXEL, "alice", 600, now_fn=clock)
    HeartbeatProcessor(now_fn=clock).process(
        conn, ROG, [SERIAL], ["android"],
        [{"device_id": PIXEL, "service": "adb", "port": 9001}],
    )
    _expire(conn, lease["id"], clock)
    assert escalate_stuck_devices(conn, 3600, now_fn=clock) == []


def test_escalation_does_not_repeat_while_one_is_outstanding(conn, clock):
    lease = store.reserve(conn, PIXEL, "alice", 600, now_fn=clock)
    HeartbeatProcessor(now_fn=clock).process(
        conn, ROG, [SERIAL], ["android"],
        [{"device_id": PIXEL, "service": "adb", "port": 9001}],
    )
    _expire(conn, lease["id"], clock)
    clock.advance(120)
    escalate_stuck_devices(conn, 60, now_fn=clock)
    escalate_stuck_devices(conn, 60, now_fn=clock)
    assert conn.execute("SELECT COUNT(*) FROM reclaim_actions").fetchone()[0] == 1


# --------------------------------------------------- 透過 heartbeat 派送

def test_pending_reclaims_are_scoped_to_the_host(conn, clock):
    request_reclaim(conn, PIXEL, "stuck", now_fn=clock)
    assert [r["device_id"] for r in pending_reclaims(conn, ROG)] == [PIXEL]
    assert pending_reclaims(conn, "alanhc-14700") == []


def test_pending_reclaim_carries_the_identifier_to_act_on(conn, clock):
    request_reclaim(conn, PIXEL, "stuck", now_fn=clock)
    job = pending_reclaims(conn, ROG)[0]
    assert job["identifier"] == SERIAL
    assert job["action"] == "adb-reboot"


def test_heartbeat_delivers_and_clears_reclaims(conn, clock):
    request_reclaim(conn, PIXEL, "stuck", now_fn=clock)
    hb = HeartbeatProcessor(now_fn=clock)
    first = hb.process(conn, ROG, [SERIAL], ["android"])
    assert [r["device_id"] for r in first["reclaims"]] == [PIXEL]

    rid = first["reclaims"][0]["reclaim_id"]
    second = hb.process(conn, ROG, [SERIAL], ["android"],
                        reclaims=[{"reclaim_id": rid, "ok": True}])
    assert second["reclaims"] == []      # 做完了就不再派
