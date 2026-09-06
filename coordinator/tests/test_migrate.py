"""既有 DB 的 schema 遷移。

`IF NOT EXISTS` 讓建表 idempotent,但只對**整張表**成立:放寬的 CHECK
約束不會套用到已經存在的表上。這台 coordinator 的 DB 掛在 volume 上、
從 Phase 1 就一直在跑,所以「新 DB 對、舊 DB 錯」不是假想——而且症狀是
執行期才炸(CHECK constraint failed),不是啟動時。
"""

from __future__ import annotations

import sqlite3

import pytest

from coordinator import db, store

# Phase 1 的 devices 表:CHECK 裡沒有 'retired'。
PHASE1_DEVICES = """
CREATE TABLE devices (
    id            TEXT PRIMARY KEY,
    class         TEXT NOT NULL,
    control       TEXT NOT NULL,
    identifier    TEXT NOT NULL UNIQUE,
    provisioning  TEXT NOT NULL CHECK (provisioning IN ('static', 'ephemeral')),
    host          TEXT,
    power_control TEXT,
    tags          JSON,
    state         TEXT NOT NULL DEFAULT 'free'
                  CHECK (state IN ('free', 'leased', 'offline', 'maintenance',
                                   'unregistered')),
    last_seen_at  TIMESTAMP
);
"""


@pytest.fixture()
def legacy(tmp_path):
    """一個 Phase 1 形狀的 DB,裡面已經有資料。"""
    path = tmp_path / "legacy.db"
    conn = db.connect(path)
    conn.executescript(PHASE1_DEVICES)
    conn.execute(
        "INSERT INTO devices (id, class, control, identifier, provisioning, host, "
        "power_control, tags, state) VALUES "
        "('pixel8-shiba', 'android', 'adb', '38011FDJH00C9F', 'static', "
        "'rog-laptop', 'adb-reboot', '{\"sve2\":true}', 'leased')"
    )
    conn.commit()
    conn.close()
    return path


def test_a_phase1_db_cannot_store_the_new_state(legacy):
    """先證明問題是真的:沒遷移的話新狀態寫不進去。"""
    conn = db.connect(legacy)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE devices SET state = 'retired' WHERE id = 'pixel8-shiba'")
    conn.close()


def test_init_db_migrates_the_check_constraint(legacy):
    conn = db.connect(legacy)
    db.init_db(conn)
    conn.execute("UPDATE devices SET state = 'retired' WHERE id = 'pixel8-shiba'")
    conn.commit()
    assert conn.execute(
        "SELECT state FROM devices WHERE id = 'pixel8-shiba'"
    ).fetchone()["state"] == "retired"
    conn.close()


def test_migration_preserves_existing_rows(legacy):
    """重建表不能弄丟資料——那些 row 是正在跑的 farm 的狀態。"""
    conn = db.connect(legacy)
    db.init_db(conn)
    row = conn.execute(
        "SELECT * FROM devices WHERE id = 'pixel8-shiba'"
    ).fetchone()
    assert row["identifier"] == "38011FDJH00C9F"
    assert row["power_control"] == "adb-reboot"
    assert row["state"] == "leased"
    assert row["tags"] == '{"sve2":true}'
    conn.close()


def test_migration_is_idempotent(legacy):
    """啟動就跑一次,重啟很多次也只該遷移一次。"""
    conn = db.connect(legacy)
    db.init_db(conn)
    db.init_db(conn)
    assert conn.execute(
        "SELECT COUNT(*) FROM devices"
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name = 'devices_old'"
    ).fetchone()[0] == 0
    conn.close()


def test_foreign_keys_are_on_again_afterwards(legacy):
    """遷移期間要關 FK,關掉沒開回來的話後面所有的完整性保護都失效。"""
    conn = db.connect(legacy)
    db.init_db(conn)
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    conn.close()


def test_a_fresh_db_needs_no_migration(tmp_path):
    conn = db.connect(tmp_path / "fresh.db")
    db.init_db(conn)
    db.seed_db(conn)
    conn.execute("UPDATE devices SET state = 'retired' WHERE id = 'pixel8-shiba'")
    conn.commit()
    conn.close()


def test_the_migrated_db_still_works_end_to_end(legacy):
    """遷移完的 DB 要能正常跑 lease,不只是能寫新狀態。"""
    conn = db.connect(legacy)
    db.init_db(conn)
    conn.execute("UPDATE devices SET state = 'free' WHERE id = 'pixel8-shiba'")
    conn.commit()
    lease = store.reserve(conn, "pixel8-shiba", "alice", 600)
    assert lease["status"] == "active"
    store.release(conn, lease["id"])
    assert conn.execute(
        "SELECT state FROM devices WHERE id = 'pixel8-shiba'"
    ).fetchone()["state"] == "free"
    conn.close()
