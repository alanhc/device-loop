"""以 stdio 跑 MCP server 的進入點。

MCP client(agent)用 stdio 連進來。跟 HTTP API 是兩個獨立 process,
各自開自己的 DB 連線——SQLite 允許多個連線讀寫同一個檔案,寫入由
DB 層的鎖與 partial UNIQUE index 保護,不需要共用 process。

    uv run python -m coordinator.mcp_main
"""

from __future__ import annotations

import os
import threading

from . import db
from .mcp_server import create_mcp

DEFAULT_DB_PATH = os.environ.get("COORDINATOR_DB", "coordinator.db")


def main() -> None:
    conn = db.connect(DEFAULT_DB_PATH, check_same_thread=False)
    db.init_db(conn)
    db.seed_db(conn)
    create_mcp(conn, threading.Lock()).run()


if __name__ == "__main__":
    main()
