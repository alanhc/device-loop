"""Lease 生命週期:reserve → renew → release、互斥、過期回收。"""

import pytest

from coordinator import store

PIXEL = "pixel8-shiba"


def _kinds(conn, device_id=PIXEL):
    return [
        r["kind"]
        for r in conn.execute(
            "SELECT kind FROM events WHERE device_id = ? ORDER BY id", (device_id,)
        )
    ]


def _state(conn, device_id=PIXEL):
    return conn.execute(
        "SELECT state FROM devices WHERE id = ?", (device_id,)
    ).fetchone()["state"]


def test_reserve_renew_release_cycle(conn, clock):
    lease = store.reserve(conn, PIXEL, "alanhc", 300, "kernel smoke test", clock)
    assert lease["status"] == "active"
    assert lease["expires_at"] == "2026-09-05T12:05:00+00:00"
    assert _state(conn) == "leased"

    clock.advance(120)
    renewed = store.renew(conn, lease["id"], 300, clock)
    assert renewed["renewed_at"] == "2026-09-05T12:02:00+00:00"
    assert renewed["expires_at"] == "2026-09-05T12:07:00+00:00"
    assert _state(conn) == "leased"

    clock.advance(60)
    released = store.release(conn, lease["id"], clock)
    assert released["status"] == "released"
    assert released["released_at"] == "2026-09-05T12:03:00+00:00"
    assert _state(conn) == "free"

    assert _kinds(conn) == ["reserve", "renew", "release"]


def test_lease_events_carry_actor_and_lease_id(conn, clock):
    lease = store.reserve(conn, PIXEL, "alanhc", 60, None, clock)
    store.release(conn, lease["id"], clock)
    rows = conn.execute(
        "SELECT * FROM events WHERE device_id = ? ORDER BY id", (PIXEL,)
    ).fetchall()
    assert all(r["actor"] == "alanhc" for r in rows)
    assert all(r["lease_id"] == lease["id"] for r in rows)


def test_second_reserve_conflicts_while_leased(conn, clock):
    store.reserve(conn, PIXEL, "alanhc", 300, None, clock)
    with pytest.raises(store.Conflict):
        store.reserve(conn, PIXEL, "someone-else", 300, None, clock)


def test_reserve_unknown_device_raises_not_found(conn, clock):
    with pytest.raises(store.NotFound):
        store.reserve(conn, "no-such-board", "alanhc", 60, None, clock)


def test_unregistered_device_is_not_leasable(conn, clock):
    """§5 裁決:unregistered 天然不可被 lease(reserve 只接受 free)。"""
    conn.execute(
        "INSERT INTO devices (id, class, control, identifier, provisioning, state) "
        "VALUES ('ffffffff', 'unknown', 'unknown', 'ffffffff', 'static', 'unregistered')"
    )
    with pytest.raises(store.Conflict):
        store.reserve(conn, "ffffffff", "alanhc", 60, None, clock)


def test_active_lease_uniqueness_enforced_by_db(conn, clock):
    """DB 層不變量:同裝置最多一條 active lease,繞過 API 也擋得住。"""
    import sqlite3

    store.reserve(conn, PIXEL, "alanhc", 300, None, clock)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO leases (device_id, user_id, created_at, expires_at) "
            "VALUES (?, 'sneaky', ?, ?)",
            (PIXEL, clock(), clock()),
        )


def test_release_is_not_repeatable(conn, clock):
    lease = store.reserve(conn, PIXEL, "alanhc", 300, None, clock)
    store.release(conn, lease["id"], clock)
    with pytest.raises(store.Conflict):
        store.release(conn, lease["id"], clock)


def test_renew_after_release_conflicts(conn, clock):
    lease = store.reserve(conn, PIXEL, "alanhc", 300, None, clock)
    store.release(conn, lease["id"], clock)
    with pytest.raises(store.Conflict):
        store.renew(conn, lease["id"], 300, clock)


# ------------------------------------------------------------------ reaper

def test_reaper_expires_overdue_lease_and_frees_device(conn, clock):
    lease = store.reserve(conn, PIXEL, "alanhc", 60, None, clock)

    clock.advance(30)
    assert store.reap_expired_leases(conn, clock) == []
    assert _state(conn) == "leased"

    clock.advance(31)
    assert store.reap_expired_leases(conn, clock) == [lease["id"]]
    assert _state(conn) == "free"

    row = conn.execute(
        "SELECT * FROM leases WHERE id = ?", (lease["id"],)
    ).fetchone()
    assert row["status"] == "expired"
    assert row["released_at"] is None
    assert _kinds(conn) == ["reserve", "expire"]

    expire_event = conn.execute(
        "SELECT * FROM events WHERE kind = 'expire'"
    ).fetchone()
    assert expire_event["actor"] == "reaper"
    assert expire_event["lease_id"] == lease["id"]


def test_renew_keeps_lease_alive_past_original_ttl(conn, clock):
    lease = store.reserve(conn, PIXEL, "alanhc", 60, None, clock)
    clock.advance(50)
    store.renew(conn, lease["id"], 60, clock)
    clock.advance(30)  # 已過原始 expires_at,但 renew 過了
    assert store.reap_expired_leases(conn, clock) == []
    assert _state(conn) == "leased"


def test_expired_device_can_be_reserved_again(conn, clock):
    store.reserve(conn, PIXEL, "alanhc", 60, None, clock)
    clock.advance(61)
    store.reap_expired_leases(conn, clock)
    again = store.reserve(conn, PIXEL, "another-agent", 60, None, clock)
    assert again["status"] == "active"


def test_reaper_marks_stale_devices_offline(conn, clock):
    conn.execute(
        "UPDATE devices SET last_seen_at = ? WHERE id = ?", (clock(), PIXEL)
    )
    clock.advance(60)
    assert store.reap_stale(conn, 120, clock) == []

    clock.advance(61)
    assert store.reap_stale(conn, 120, clock) == [PIXEL]
    assert _state(conn) == "offline"
    assert _kinds(conn) == ["device_detached"]


def test_reaper_ignores_never_seen_devices(conn, clock):
    """last_seen_at 為 NULL 代表還沒有 exporter 回報過,不是失聯。"""
    clock.advance(10_000)
    assert store.reap_stale(conn, 120, clock) == []
    assert _state(conn) == "free"
