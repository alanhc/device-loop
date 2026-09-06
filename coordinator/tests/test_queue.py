"""佇列(設計文件 §12)。

lease 仍是唯一的互斥原語,佇列只決定「下一個 lease 給誰」。Phase 2 只做
interactive(排隊等 lease)。
"""

from __future__ import annotations

import pytest

from coordinator import store
from coordinator.store import (
    cancel_job,
    enqueue,
    expire_stale_jobs,
    queue_position,
    run_scheduler,
)

PIXEL = "pixel8-shiba"


def test_enqueue_gives_a_position(conn, clock):
    store.reserve(conn, PIXEL, "alice", 600, now_fn=clock)
    job = enqueue(conn, PIXEL, "bob", 600, now_fn=clock)
    assert job["state"] == "queued"
    assert queue_position(conn, job["id"]) == 1


def test_queue_is_fifo(conn, clock):
    store.reserve(conn, PIXEL, "alice", 600, now_fn=clock)
    first = enqueue(conn, PIXEL, "bob", 600, now_fn=clock)
    clock.advance(1)
    second = enqueue(conn, PIXEL, "carol", 600, now_fn=clock)
    assert queue_position(conn, first["id"]) == 1
    assert queue_position(conn, second["id"]) == 2


def test_repeat_request_returns_the_same_ticket(conn, clock):
    """agent 重試不該塞爆佇列,也不該讓它在 FIFO 裡換位置。"""
    store.reserve(conn, PIXEL, "alice", 600, now_fn=clock)
    first = enqueue(conn, PIXEL, "bob", 600, now_fn=clock)
    again = enqueue(conn, PIXEL, "bob", 600, now_fn=clock)
    assert again["id"] == first["id"]
    assert conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE state = 'queued'"
    ).fetchone()[0] == 1


def test_repeat_request_does_not_jump_the_queue(conn, clock):
    store.reserve(conn, PIXEL, "alice", 600, now_fn=clock)
    bob = enqueue(conn, PIXEL, "bob", 600, now_fn=clock)
    clock.advance(1)
    enqueue(conn, PIXEL, "carol", 600, now_fn=clock)
    clock.advance(1)
    enqueue(conn, PIXEL, "bob", 600, now_fn=clock)      # bob 再按一次
    assert queue_position(conn, bob["id"]) == 1          # 還是原來的位置


def test_unregistered_device_cannot_be_queued_for(conn, clock):
    """§5:unregistered 不可被 lease,排隊等它也沒有意義。"""
    conn.execute(
        "INSERT INTO devices (id, class, control, identifier, provisioning, state) "
        "VALUES ('newboard', 'unknown', 'unknown', 'NEW', 'static', 'unregistered')"
    )
    with pytest.raises(store.Conflict):
        enqueue(conn, "newboard", "bob", 600, now_fn=clock)


def test_unknown_device_raises_not_found(conn, clock):
    with pytest.raises(store.NotFound):
        enqueue(conn, "nope", "bob", 600, now_fn=clock)


# ------------------------------------------------------------------ 取消

def test_cancel_frees_the_position(conn, clock):
    """放棄的 agent 不能一直佔位置——否則佇列漏水。"""
    store.reserve(conn, PIXEL, "alice", 600, now_fn=clock)
    bob = enqueue(conn, PIXEL, "bob", 600, now_fn=clock)
    clock.advance(1)
    carol = enqueue(conn, PIXEL, "carol", 600, now_fn=clock)
    cancel_job(conn, bob["id"], "bob", now_fn=clock)
    assert queue_position(conn, carol["id"]) == 1


def test_cannot_cancel_someone_elses_job(conn, clock):
    """job id 是連號整數,少了這層任何人都能把別人踢出佇列。"""
    store.reserve(conn, PIXEL, "alice", 600, now_fn=clock)
    bob = enqueue(conn, PIXEL, "bob", 600, now_fn=clock)
    with pytest.raises(store.NotFound, match="not found or not yours"):
        cancel_job(conn, bob["id"], "mallory", now_fn=clock)


def test_cancelling_twice_conflicts(conn, clock):
    store.reserve(conn, PIXEL, "alice", 600, now_fn=clock)
    bob = enqueue(conn, PIXEL, "bob", 600, now_fn=clock)
    cancel_job(conn, bob["id"], "bob", now_fn=clock)
    with pytest.raises(store.Conflict):
        cancel_job(conn, bob["id"], "bob", now_fn=clock)


def test_cancelling_lets_the_user_queue_again(conn, clock):
    """partial UNIQUE 只擋 queued;取消之後應該可以重新排。"""
    store.reserve(conn, PIXEL, "alice", 600, now_fn=clock)
    bob = enqueue(conn, PIXEL, "bob", 600, now_fn=clock)
    cancel_job(conn, bob["id"], "bob", now_fn=clock)
    again = enqueue(conn, PIXEL, "bob", 600, now_fn=clock)
    assert again["id"] != bob["id"]


# ------------------------------------------------------------ scheduler

def test_scheduler_hands_the_device_to_the_next_in_line(conn, clock):
    lease = store.reserve(conn, PIXEL, "alice", 600, now_fn=clock)
    job = enqueue(conn, PIXEL, "bob", 600, now_fn=clock)
    store.release(conn, lease["id"], now_fn=clock)
    assert run_scheduler(conn, now_fn=clock) == [job["id"]]
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job["id"],)).fetchone()
    assert row["state"] == "running"
    assert row["lease_id"] is not None


def test_the_new_lease_belongs_to_the_queued_user(conn, clock):
    lease = store.reserve(conn, PIXEL, "alice", 600, now_fn=clock)
    job = enqueue(conn, PIXEL, "bob", 600, now_fn=clock)
    store.release(conn, lease["id"], now_fn=clock)
    run_scheduler(conn, now_fn=clock)
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job["id"],)).fetchone()
    new_lease = conn.execute(
        "SELECT * FROM leases WHERE id = ?", (row["lease_id"],)
    ).fetchone()
    assert new_lease["user_id"] == "bob"
    assert new_lease["status"] == "active"


def test_scheduler_respects_fifo(conn, clock):
    lease = store.reserve(conn, PIXEL, "alice", 600, now_fn=clock)
    first = enqueue(conn, PIXEL, "bob", 600, now_fn=clock)
    clock.advance(1)
    enqueue(conn, PIXEL, "carol", 600, now_fn=clock)
    store.release(conn, lease["id"], now_fn=clock)
    assert run_scheduler(conn, now_fn=clock) == [first["id"]]


def test_scheduler_does_nothing_while_the_device_is_busy(conn, clock):
    store.reserve(conn, PIXEL, "alice", 600, now_fn=clock)
    enqueue(conn, PIXEL, "bob", 600, now_fn=clock)
    assert run_scheduler(conn, now_fn=clock) == []


def test_scheduler_skips_devices_in_maintenance(conn, clock):
    """回收失敗、等人工的裝置不該被交出去。"""
    lease = store.reserve(conn, PIXEL, "alice", 600, now_fn=clock)
    enqueue(conn, PIXEL, "bob", 600, now_fn=clock)
    store.release(conn, lease["id"], now_fn=clock)
    conn.execute("UPDATE devices SET state = 'maintenance' WHERE id = ?", (PIXEL,))
    conn.commit()
    assert run_scheduler(conn, now_fn=clock) == []


def test_scheduler_skips_offline_devices(conn, clock):
    lease = store.reserve(conn, PIXEL, "alice", 600, now_fn=clock)
    enqueue(conn, PIXEL, "bob", 600, now_fn=clock)
    store.release(conn, lease["id"], now_fn=clock)
    conn.execute("UPDATE devices SET state = 'offline' WHERE id = ?", (PIXEL,))
    conn.commit()
    assert run_scheduler(conn, now_fn=clock) == []


def test_cancelled_jobs_are_skipped(conn, clock):
    lease = store.reserve(conn, PIXEL, "alice", 600, now_fn=clock)
    bob = enqueue(conn, PIXEL, "bob", 600, now_fn=clock)
    clock.advance(1)
    carol = enqueue(conn, PIXEL, "carol", 600, now_fn=clock)
    cancel_job(conn, bob["id"], "bob", now_fn=clock)
    store.release(conn, lease["id"], now_fn=clock)
    assert run_scheduler(conn, now_fn=clock) == [carol["id"]]


def test_handing_over_writes_a_reserve_event(conn, clock):
    """稽核要看得出裝置換手過。"""
    lease = store.reserve(conn, PIXEL, "alice", 600, now_fn=clock)
    enqueue(conn, PIXEL, "bob", 600, now_fn=clock)
    store.release(conn, lease["id"], now_fn=clock)
    run_scheduler(conn, now_fn=clock)
    actors = [r["actor"] for r in conn.execute(
        "SELECT actor FROM events WHERE device_id = ? AND kind = 'reserve'", (PIXEL,))]
    assert "bob" in actors


# ------------------------------------------------------------------ TTL

def test_a_job_that_waited_too_long_is_cancelled(conn, clock):
    """死掉的 agent 不該永遠佔位置。"""
    store.reserve(conn, PIXEL, "alice", 9999, now_fn=clock)
    job = enqueue(conn, PIXEL, "bob", 60, now_fn=clock)
    clock.advance(61)
    assert expire_stale_jobs(conn, now_fn=clock) == [job["id"]]
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job["id"],)).fetchone()
    assert row["state"] == "cancelled"
    assert "timed out" in (row["result"] or "")


def test_a_job_within_its_timeout_survives(conn, clock):
    store.reserve(conn, PIXEL, "alice", 9999, now_fn=clock)
    job = enqueue(conn, PIXEL, "bob", 600, now_fn=clock)
    clock.advance(60)
    assert expire_stale_jobs(conn, now_fn=clock) == []
    assert queue_position(conn, job["id"]) == 1


def test_position_is_none_once_running(conn, clock):
    lease = store.reserve(conn, PIXEL, "alice", 600, now_fn=clock)
    job = enqueue(conn, PIXEL, "bob", 600, now_fn=clock)
    store.release(conn, lease["id"], now_fn=clock)
    run_scheduler(conn, now_fn=clock)
    assert queue_position(conn, job["id"]) is None
