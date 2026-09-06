"""SQLite 連線與 schema 初始化。

時間戳一律存 ISO 8601 UTC 字串(``2026-09-05T06:00:00+00:00``),
字典序即時間序,方便直接用字串比較掃過期 lease。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path

SCHEMA_FILE = "schema.sql"
SEED_FILE = "seed.sql"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: str | Path, check_same_thread: bool = True) -> sqlite3.Connection:
    """開一條連線。

    FastAPI 把 sync endpoint 丟到 threadpool 跑,reaper 又在另一條 thread,
    所以 API 那層要 ``check_same_thread=False``——搭配呼叫端的鎖序列化存取
    (見 api.py)。單純的測試/CLI 用預設值即可。
    """
    conn = sqlite3.connect(path, check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _read_sql(name: str) -> str:
    return resources.files(__package__).joinpath(name).read_text(encoding="utf-8")


# devices.state 的合法值。schema.sql 是新建 DB 的真相來源,這份清單是**既有
# DB** 的:CHECK 約束寫在 CREATE TABLE 裡,而 schema 全用 IF NOT EXISTS,
# 所以加一個新狀態不會套用到已經在跑的 DB 上——那台 coordinator 會在第一次
# 寫入新狀態時撞 CHECK 失敗。這個清單讓 migrate_db 看得出來該不該重建。
DEVICE_STATES = (
    "free", "leased", "offline", "maintenance", "unregistered", "retired",
)


def init_db(conn: sqlite3.Connection) -> None:
    """建表(idempotent,schema 全部用 IF NOT EXISTS)並補上既有 DB 的遷移。"""
    conn.executescript(_read_sql(SCHEMA_FILE))
    conn.commit()
    migrate_db(conn)


def migrate_db(conn: sqlite3.Connection) -> None:
    """把既有 DB 的 schema 補到跟 schema.sql 一致。

    `IF NOT EXISTS` 讓建表 idempotent,但**只對整張表成立**:新增的
    欄位、放寬的 CHECK 約束都不會套用到已經存在的表上。這台 coordinator
    的 DB 掛在 volume 上、從 Phase 1 就一直在跑,所以「新 DB 對、舊 DB 錯」
    是真的會發生的情況——而且症狀是執行期才炸(CHECK constraint failed),
    不是啟動時。

    現在只有一件事要遷移:``devices.state`` 的 CHECK 要接受 ``retired``
    (Phase 3 的 ephemeral 實例退場狀態)。SQLite 不能 ALTER 一個 CHECK,
    只能整張表重建。
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'devices'"
    ).fetchone()
    if row is None or "'retired'" in row[0]:
        return

    # 重建 devices:把舊表改名、依 schema.sql 建新的、搬資料、丟掉舊的。
    #
    # 外鍵在重建期間要關掉,否則 leases/events 那幾張表指向的 devices 一被
    # 改名就報錯。兩個 sqlite3 的細節在這裡會咬人:
    #
    # 1. **PRAGMA foreign_keys 在 transaction 裡是 no-op**(不報錯,只是
    #    默默沒作用)。python 的 sqlite3 預設會為 DML 隱式開 transaction,
    #    所以要先 commit,而且 pragma 本身也得在 transaction 外面下。
    # 2. `ALTER TABLE ... RENAME` 在新版 SQLite 會**改寫其他表的 FK 參照**
    #    (legacy_alter_table 預設關),所以 leases.device_id 會被改成指向
    #    devices_old。要的是「舊資料搬進新表」而不是「參照跟著搬」,所以
    #    連 legacy_alter_table 一起打開。
    conn.commit()
    conn.isolation_level = None          # autocommit:pragma 才吃得到
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("PRAGMA legacy_alter_table = ON")
    try:
        conn.execute("ALTER TABLE devices RENAME TO devices_old")
        # schema.sql 是完整腳本,其他表都有 IF NOT EXISTS,只會補建 devices。
        conn.executescript(_read_sql(SCHEMA_FILE))
        # executescript 會 commit 並開一個新 transaction,而 PRAGMA
        # foreign_keys 在 transaction 裡是 no-op——上面關掉的在這裡已經
        # 失效了。要在搬資料**之前**再關一次,否則舊 row 的 host 指向
        # 一個(這個 DB 裡可能還沒有的)hosts row 就會擋下整個遷移。
        conn.execute("PRAGMA foreign_keys = OFF")
        cols = [r[1] for r in conn.execute("PRAGMA table_info(devices)")]
        old_cols = [r[1] for r in conn.execute("PRAGMA table_info(devices_old)")]
        shared = ", ".join(c for c in cols if c in old_cols)
        conn.execute(f"INSERT INTO devices ({shared}) SELECT {shared} FROM devices_old")
        conn.execute("DROP TABLE devices_old")
    finally:
        conn.execute("PRAGMA legacy_alter_table = OFF")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.isolation_level = ""        # 還原 python sqlite3 的預設行為


def seed_db(conn: sqlite3.Connection) -> None:
    """灌入設計文件第 5 節的範例 row;已有資料時跳過。"""
    if conn.execute("SELECT COUNT(*) FROM devices").fetchone()[0] == 0:
        conn.executescript(_read_sql(SEED_FILE))
        conn.commit()
