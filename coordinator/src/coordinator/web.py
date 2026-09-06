"""唯讀為主的網頁儀表板,由 coordinator 自己服務。

**為什麼不做成獨立的前端**:coordinator 跑在 tailnet 的私有位址上,外部
託管的頁面連不到它。由 coordinator 服務也順便讓頁面跟 API 永遠同源,
不需要處理 CORS。

頁面只用原生 JS、不抓任何 CDN——tailnet 內的機器不保證出得去外網,
而且一個管理介面不該在網路不通時變成白畫面。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse

_INDEX = Path(__file__).parent / "static" / "index.html"


def create_router(get_conn) -> APIRouter:
    """`get_conn` 由 api.py 傳進來,共用同一把鎖與連線。"""
    router = APIRouter()

    @router.get("/", response_class=HTMLResponse, include_in_schema=False)
    def index() -> str:
        return _INDEX.read_text(encoding="utf-8")

    @router.get("/api/overview")
    def overview(conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        """一次拿齊整頁要的東西。

        分開打三支 API 的話,三次查詢之間狀態可能已經變了,畫面會出現
        「裝置是 free 但下面列著它的 active lease」這種自相矛盾的組合。
        同一個連線一次讀完就沒有這個問題。
        """
        devices = [dict(r) for r in conn.execute("SELECT * FROM devices ORDER BY id")]
        leases = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM leases WHERE status = 'active' ORDER BY id DESC"
            )
        ]
        services = [
            dict(r) for r in conn.execute("SELECT * FROM device_services")
        ]
        events = [
            dict(r)
            for r in conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT 40")
        ]
        hosts = [dict(r) for r in conn.execute("SELECT * FROM hosts ORDER BY id")]
        return {
            "devices": devices,
            "leases": leases,
            "services": services,
            "events": events,
            "hosts": hosts,
        }

    return router
