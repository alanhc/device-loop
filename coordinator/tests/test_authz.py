"""Lease 授權檢查——MCP 前門的安全邊界(設計文件 §6)。"""

from __future__ import annotations

import pytest

from coordinator import store
from coordinator.authz import LeaseDenied, require_lease

DEVICE = "pixel8-shiba"
OWNER = "alice"


def test_holder_of_an_active_lease_is_allowed(conn, clock):
    lease = store.reserve(conn, DEVICE, OWNER, 600, now_fn=clock)
    got = require_lease(conn, DEVICE, OWNER, now_fn=clock)
    assert got["id"] == lease["id"]


def test_device_with_no_lease_is_denied(conn, clock):
    with pytest.raises(LeaseDenied, match="no active lease"):
        require_lease(conn, DEVICE, OWNER, now_fn=clock)


def test_another_users_lease_is_denied(conn, clock):
    store.reserve(conn, DEVICE, OWNER, 600, now_fn=clock)
    with pytest.raises(LeaseDenied, match="leased by someone else"):
        require_lease(conn, DEVICE, "mallory", now_fn=clock)


def test_denial_does_not_disclose_the_actual_holder(conn, clock):
    """user_id 目前不可信,而且持有者是誰不干呼叫者的事。"""
    store.reserve(conn, DEVICE, OWNER, 600, now_fn=clock)
    with pytest.raises(LeaseDenied) as e:
        require_lease(conn, DEVICE, "mallory", now_fn=clock)
    assert OWNER not in str(e.value)


def test_lease_on_a_different_device_does_not_grant_access(conn, clock):
    """借了 A 不能操作 B——這是 lease-by-identity 的重點。"""
    store.reserve(conn, DEVICE, OWNER, 600, now_fn=clock)
    with pytest.raises(LeaseDenied, match="no active lease"):
        require_lease(conn, "milkv-jupiter", OWNER, now_fn=clock)


def test_released_lease_is_denied(conn, clock):
    lease = store.reserve(conn, DEVICE, OWNER, 600, now_fn=clock)
    store.release(conn, lease["id"], now_fn=clock)
    with pytest.raises(LeaseDenied, match="no active lease"):
        require_lease(conn, DEVICE, OWNER, now_fn=clock)


def test_expired_lease_is_denied_before_the_reaper_notices(conn, clock):
    """關鍵時間窗:reaper 是週期性跑的,lease 過期到被標成 expired 之間
    status 還是 'active'。只看 status 會在那段時間放行過期的 lease。"""
    store.reserve(conn, DEVICE, OWNER, 60, now_fn=clock)
    clock.advance(61)
    # reaper 還沒跑,DB 裡 status 仍是 active
    row = conn.execute(
        "SELECT status FROM leases WHERE device_id = ?", (DEVICE,)
    ).fetchone()
    assert row["status"] == "active"
    with pytest.raises(LeaseDenied, match="expired"):
        require_lease(conn, DEVICE, OWNER, now_fn=clock)


def test_lease_is_valid_right_up_to_expiry(conn, clock):
    store.reserve(conn, DEVICE, OWNER, 60, now_fn=clock)
    clock.advance(59)
    assert require_lease(conn, DEVICE, OWNER, now_fn=clock) is not None


def test_renewing_extends_access(conn, clock):
    lease = store.reserve(conn, DEVICE, OWNER, 60, now_fn=clock)
    clock.advance(50)
    store.renew(conn, lease["id"], 600, now_fn=clock)
    clock.advance(100)
    assert require_lease(conn, DEVICE, OWNER, now_fn=clock) is not None


def test_matching_lease_id_is_accepted(conn, clock):
    lease = store.reserve(conn, DEVICE, OWNER, 600, now_fn=clock)
    assert require_lease(conn, DEVICE, OWNER, lease["id"], now_fn=clock) is not None


def test_stale_lease_id_is_rejected_even_for_the_same_user(conn, clock):
    """同一個 user 前一輪的 lease 號碼不該矇混過關——通常代表 agent 自己
    的狀態亂了,寧可報錯也不要默默放行。"""
    old = store.reserve(conn, DEVICE, OWNER, 600, now_fn=clock)
    store.release(conn, old["id"], now_fn=clock)
    new = store.reserve(conn, DEVICE, OWNER, 600, now_fn=clock)
    assert new["id"] != old["id"]
    with pytest.raises(LeaseDenied, match="not the active lease"):
        require_lease(conn, DEVICE, OWNER, old["id"], now_fn=clock)


def test_unregistered_device_has_no_lease_so_is_denied(conn, clock):
    """unregistered 裝置不可被 lease(§5),所以也不可能通過檢查。"""
    conn.execute(
        "INSERT INTO devices (id, class, control, identifier, provisioning, state) "
        "VALUES ('newboard', 'unknown', 'unknown', 'NEWSERIAL', 'static', 'unregistered')"
    )
    with pytest.raises(LeaseDenied, match="no active lease"):
        require_lease(conn, "newboard", OWNER, now_fn=clock)
