import sqlite3

import pytest

from coordinator import db


@pytest.fixture()
def conn():
    c = db.connect(":memory:")
    db.init_db(c)
    yield c
    c.close()


def test_all_tables_created(conn):
    names = {
        r["name"]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"hosts", "devices", "leases", "device_services", "events"} <= names


def test_seed_rows(conn):
    db.seed_db(conn)
    assert conn.execute("SELECT COUNT(*) FROM hosts").fetchone()[0] == 3
    # 6 台實體/本機裝置 + 1 個 Cuttlefish template(§12 的 ephemeral 池)。
    assert conn.execute("SELECT COUNT(*) FROM devices").fetchone()[0] == 7
    pixel = conn.execute(
        "SELECT * FROM devices WHERE id = 'pixel8-shiba'"
    ).fetchone()
    assert pixel["identifier"] == "38011FDJH00C9F"
    assert pixel["host"] == "rog-laptop"
    assert pixel["state"] == "free"


def test_seed_idempotent(conn):
    db.seed_db(conn)
    db.seed_db(conn)
    assert conn.execute("SELECT COUNT(*) FROM devices").fetchone()[0] == 7


def test_device_state_check_constraint(conn):
    db.seed_db(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE devices SET state = 'bogus' WHERE id = 'pixel8-shiba'"
        )


def test_event_kind_check_constraint(conn):
    db.seed_db(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO events (device_id, kind, created_at) VALUES (?, ?, ?)",
            ("pixel8-shiba", "not-a-kind", db.utcnow()),
        )
    conn.execute(
        "INSERT INTO events (device_id, kind, actor, created_at) VALUES (?, ?, ?, ?)",
        ("pixel8-shiba", "device_moved", "exporter", db.utcnow()),
    )


def test_foreign_keys_enforced(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO leases (device_id, user_id, created_at, expires_at) "
            "VALUES ('no-such-device', 'u', ?, ?)",
            (db.utcnow(), db.utcnow()),
        )
