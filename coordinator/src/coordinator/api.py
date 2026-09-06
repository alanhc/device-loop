"""FastAPI 前門:lease API + exporter heartbeat + reaper 背景任務。

HTTP 這層只做輸入驗證與例外→狀態碼的轉換,狀態轉換邏輯全在 store.py。
MCP 前門(mcp_server.py)掛在同一個 app 上,共用同一條 DB 連線。
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import threading
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, ConfigDict, Field

from . import authz, db, mcp_server, store, web

log = logging.getLogger(__name__)

DEFAULT_DB_PATH = os.environ.get("COORDINATOR_DB", "coordinator.db")

# Reaper 掃描間隔與判定門檻。裝置 last_seen_at 超過 OFFLINE_AFTER_S 沒更新
# 就標 offline(§8:不用等使用者踩到才知道)。
REAPER_INTERVAL_S = int(os.environ.get("COORDINATOR_REAPER_INTERVAL_S", "10"))
OFFLINE_AFTER_S = int(os.environ.get("COORDINATOR_OFFLINE_AFTER_S", "120"))
HEARTBEAT_GRACE_S = int(os.environ.get("COORDINATOR_HEARTBEAT_GRACE_S", "30"))
# lease 過期後,等 exporter 收斂停掉服務的寬限。超過這段時間服務還在,
# 代表 exporter 沒收斂成功(掛了、或裝置卡到停不掉),才升級成強制回收。
# 預設給兩輪 heartbeat 的餘裕。
RECLAIM_AFTER_S = int(os.environ.get("COORDINATOR_RECLAIM_AFTER_S", "60"))

# MCP 的 streamable-HTTP transport 有 DNS rebinding protection,預設只放行
# 本機。遠端 agent(tailnet 上的其他機器)連進來時 Host header 不是
# localhost,會拿到 **421 Misdirected Request** 而不是連線失敗——症狀難查,
# 所以部署時務必設這個變數。
# 例:COORDINATOR_MCP_ALLOWED_HOSTS=100.69.80.97:8090,localhost,127.0.0.1
# 不寫死任何部署的 IP:這個 repo 是公開的,而且 coordinator 可能搬家。
MCP_ALLOWED_HOSTS = [
    h.strip()
    for h in os.environ.get(
        "COORDINATOR_MCP_ALLOWED_HOSTS", "127.0.0.1,localhost"
    ).split(",")
    if h.strip()
]
MCP_PATH = os.environ.get("COORDINATOR_MCP_PATH", "/mcp")


class ReserveRequest(BaseModel):
    device_id: str
    user_id: str
    ttl_s: int = Field(gt=0)
    purpose: str | None = None
    # 裝置忙碌時要不要排隊。預設 false = 維持既有的 409 行為,不帶這個
    # 參數的呼叫端(腳本、exporter)完全不受影響;true 回一張號碼牌。
    # MCP 的 reserve_device 預設帶 true,因為 agent 的正常期待就是排隊。
    queue: bool = False


class CancelJobRequest(BaseModel):
    user_id: str


class RenewRequest(BaseModel):
    # user_id 是持有權檢查用的。lease id 是連號整數,少了它任何人都能
    # renew/release 掉別人的 lease(見 authz.require_lease_owner)。
    user_id: str
    ttl_s: int = Field(gt=0)


class ReleaseRequest(BaseModel):
    user_id: str


class ReportedService(BaseModel):
    """Exporter 回報「我這邊實際跑著什麼」。

    刻意**不含 ``mediated``**:那是能力種類的靜態性質(flash 恆為 true),
    由 coordinator 依 store.MEDIATED_SERVICES 自己填。讓回報方宣告自己
    是否需要 mediation,等於把一個安全相關的事實交給被管制的一方。
    多送這個欄位會被擋下來(extra="forbid"),而不是默默忽略。
    """

    model_config = ConfigDict(extra="forbid")

    device_id: str
    service: str
    port: int | None = None
    # 給完整 endpoint 也可以;沒給就用 hosts.address + port 組出來。
    endpoint: str | None = None


class FlashResult(BaseModel):
    """Exporter 回報一個 flash 動作的結果。"""

    model_config = ConfigDict(extra="forbid")

    flash_id: int
    ok: bool
    detail: dict | None = None


class InstanceReport(BaseModel):
    """Exporter 回報一台 ephemeral VM 的實際狀態。"""

    model_config = ConfigDict(extra="forbid")

    instance_id: str
    state: str            # 'running' | 'gone' | 'failed'
    detail: dict | None = None


class RegisterImageRequest(BaseModel):
    """登記一份可以刷的 image(§6 image registry)。

    Flash 只接受登記過的 image:路徑是呼叫者自由填的字串的話,「刷了
    什麼進去」事後查不出來,而那正是這個 registry 存在的理由。
    """

    id: str
    device_id: str
    kind: str
    uri: str
    sha256: str
    known_good: bool = False
    note: str | None = None


class FlashRequest(BaseModel):
    device_id: str
    image_id: str
    user_id: str
    # 帶了就一併核對,避免拿舊 lease 的號碼矇混(見 authz.require_lease)。
    lease_id: int | None = None


class ReclaimResult(BaseModel):
    """Exporter 回報一個強制回收動作的結果。"""

    model_config = ConfigDict(extra="forbid")

    reclaim_id: int
    ok: bool
    detail: dict | None = None


class HeartbeatRequest(BaseModel):
    host_id: str
    identifiers: list[str] = Field(default_factory=list)
    # 這次回報涵蓋哪些 device class;省略代表 exporter 的預設 USB 掃描範圍。
    discoverable_classes: list[str] | None = None
    # 「看得到它活著」但不宣稱擁有的識別碼(tailnet-native 裝置,§5)。
    # 只把已知裝置的 last_seen_at 往前推:不建 row、不改 host、不參與
    # 缺席判定。
    seen: list[str] = Field(default_factory=list)
    # 目前實際跑著的 per-resource daemon。回應會帶 desired,exporter 自行
    # 比對收斂(reconcile);coordinator 不主動推送。
    services: list[ReportedService] = Field(default_factory=list)
    # 已執行完的強制回收動作結果。
    reclaims: list[ReclaimResult] = Field(default_factory=list)
    # 已執行完的 flash 結果(§6 唯一 mediated 的能力)。
    flashes: list[FlashResult] = Field(default_factory=list)
    # ephemeral VM 實例的實際狀態(§10/§12)。
    instances: list[InstanceReport] = Field(default_factory=list)


def _lease_dict(row: sqlite3.Row) -> dict:
    return dict(row)


def _queued_dict(conn: sqlite3.Connection, job: sqlite3.Row) -> dict:
    """號碼牌:排在第幾、輪到了沒有。"""
    out = {"queued": job["state"] == "queued", "job_id": job["id"],
           "state": job["state"], "device_id": job["device_id"],
           "position": store.queue_position(conn, job["id"]),
           "lease_id": job["lease_id"]}
    return out


def create_app(db_path: str | None = None) -> FastAPI:
    path = db_path or DEFAULT_DB_PATH

    # 連線、鎖與 MCP 在建立路由前就備好——MCP 的 ASGI app 要 mount 進這個
    # app,而它需要連線與鎖。
    # 一條共用連線 + 一把鎖:sync endpoint 跑在 threadpool、reaper 跑在
    # 另一條 thread,單一 writer 序列化存取比連線池簡單且夠用。MCP 前門
    # 沿用同一組,兩邊的寫入不會互相踩到。
    conn = db.connect(path, check_same_thread=False)
    db.init_db(conn)
    db.seed_db(conn)
    lock = threading.Lock()
    mcp = mcp_server.create_mcp(conn, lock)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.conn = conn
        app.state.lock = lock
        app.state.mcp = mcp
        app.state.heartbeat = store.HeartbeatProcessor(grace_s=HEARTBEAT_GRACE_S)
        _log_mcp_reachability()
        task = asyncio.create_task(_reaper_loop(app))
        try:
            # MCP 的 session manager 一定要在 app 的 lifespan 裡跑起來,
            # 否則 mount 進來的路由會在第一個請求就炸掉。
            async with mcp.session_manager.run():
                yield
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            conn.close()

    app = FastAPI(title="device-loop coordinator", lifespan=lifespan)

    def get_conn():
        """在鎖內交出連線,handler 回傳後才釋放——不用每個 handler 自己記得。"""
        with app.state.lock:
            yield app.state.conn

    app.include_router(web.create_router(get_conn))

    @app.get("/devices")
    def list_devices(conn: sqlite3.Connection = Depends(get_conn)) -> list[dict]:
        # retired 的 ephemeral 實例不列:它們的 row 只為了稽核鏈留著
        # (events.device_id 是硬性 FK),對使用者來說那台 VM 已經沒了。
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM devices WHERE state != 'retired' ORDER BY id"
            )
        ]

    @app.get("/devices/{device_id}/events")
    def device_events(
        device_id: str, conn: sqlite3.Connection = Depends(get_conn)
    ) -> list[dict]:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM events WHERE device_id = ? ORDER BY id", (device_id,)
            )
        ]

    @app.post("/leases", status_code=201)
    def reserve(
        req: ReserveRequest, conn: sqlite3.Connection = Depends(get_conn)
    ) -> dict:
        user_id = authz.caller_identity(req.user_id)
        try:
            lease = store.reserve(
                conn, req.device_id, user_id, req.ttl_s, req.purpose
            )
        except store.NotFound as e:
            raise HTTPException(404, str(e)) from e
        except store.Conflict as e:
            if not req.queue:
                raise HTTPException(409, str(e)) from e
            try:
                job = store.enqueue(conn, req.device_id, user_id, req.ttl_s)
            except store.NotFound as e2:
                raise HTTPException(404, str(e2)) from e2
            except store.Conflict as e2:
                raise HTTPException(409, str(e2)) from e2
            return _queued_dict(conn, job)
        return {"queued": False, **_lease_dict(lease)}

    @app.get("/jobs/{job_id}")
    def get_job(job_id: int, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise HTTPException(404, f"job {job_id} not found")
        return _queued_dict(conn, row)

    @app.post("/jobs/{job_id}/cancel")
    def cancel_job(
        job_id: int,
        req: CancelJobRequest,
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> dict:
        user_id = authz.caller_identity(req.user_id)
        try:
            return _queued_dict(conn, store.cancel_job(conn, job_id, user_id))
        except store.NotFound as e:
            raise HTTPException(404, str(e)) from e
        except store.Conflict as e:
            raise HTTPException(409, str(e)) from e

    @app.get("/leases/{lease_id}")
    def get_lease(
        lease_id: int, conn: sqlite3.Connection = Depends(get_conn)
    ) -> dict:
        row = conn.execute("SELECT * FROM leases WHERE id = ?", (lease_id,)).fetchone()
        if row is None:
            raise HTTPException(404, f"lease {lease_id} not found")
        return _lease_dict(row)

    @app.post("/leases/{lease_id}/renew")
    def renew(
        lease_id: int,
        req: RenewRequest,
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> dict:
        try:
            authz.require_lease_owner(conn, lease_id, req.user_id)
            return _lease_dict(store.renew(conn, lease_id, req.ttl_s))
        except authz.LeaseDenied as e:
            # 404 而不是 403:不存在與不是你的回同一種答案,連號的 id
            # 才不能拿來探測哪些 lease 存在。
            raise HTTPException(404, str(e)) from e
        except store.NotFound as e:
            raise HTTPException(404, str(e)) from e
        except store.Conflict as e:
            raise HTTPException(409, str(e)) from e

    @app.post("/leases/{lease_id}/release")
    def release(
        lease_id: int,
        req: ReleaseRequest,
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> dict:
        try:
            authz.require_lease_owner(conn, lease_id, req.user_id)
            return _lease_dict(store.release(conn, lease_id))
        except authz.LeaseDenied as e:
            raise HTTPException(404, str(e)) from e
        except store.NotFound as e:
            raise HTTPException(404, str(e)) from e
        except store.Conflict as e:
            raise HTTPException(409, str(e)) from e

    @app.get("/instances")
    def list_instances(conn: sqlite3.Connection = Depends(get_conn)) -> list[dict]:
        """目前存在的 ephemeral VM 實例(§10/§12)。"""
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM vm_instances WHERE state != 'gone' ORDER BY id"
            )
        ]

    @app.get("/images")
    def list_images(
        device_id: str | None = None, conn: sqlite3.Connection = Depends(get_conn)
    ) -> list[dict]:
        return [dict(r) for r in store.list_images(conn, device_id)]

    @app.post("/images", status_code=201)
    def register_image(
        req: RegisterImageRequest, conn: sqlite3.Connection = Depends(get_conn)
    ) -> dict:
        try:
            return dict(store.register_image(
                conn, req.id, req.device_id, req.kind, req.uri, req.sha256,
                req.known_good, req.note,
            ))
        except store.NotFound as e:
            raise HTTPException(404, str(e)) from e
        except store.ImageError as e:
            raise HTTPException(422, str(e)) from e

    @app.post("/flash", status_code=202)
    def flash(
        req: FlashRequest, conn: sqlite3.Connection = Depends(get_conn)
    ) -> dict:
        """要求刷一份 image。**唯一 mediated 的能力**(§6)。

        202 而不是 201:coordinator 只是把工作排進去,實際執行要等裝置所在
        host 的 exporter 下一輪 heartbeat 領走。查進度用 GET /flash/{id}。
        """
        user_id = authz.caller_identity(req.user_id)
        try:
            authz.require_lease(conn, req.device_id, user_id, req.lease_id)
        except authz.LeaseDenied as e:
            # 跟 lease 一樣回 404 而不是 403,不讓連號 id 拿來探測。
            raise HTTPException(404, str(e)) from e
        try:
            job = store.request_flash(
                conn, req.device_id, req.image_id, user_id, req.lease_id
            )
        except store.NotFound as e:
            raise HTTPException(404, str(e)) from e
        except store.Conflict as e:
            raise HTTPException(409, str(e)) from e
        return dict(job)

    @app.get("/flash/{flash_id}")
    def get_flash(
        flash_id: int, conn: sqlite3.Connection = Depends(get_conn)
    ) -> dict:
        row = conn.execute(
            "SELECT * FROM flash_jobs WHERE id = ?", (flash_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(404, f"flash job {flash_id} not found")
        return dict(row)

    @app.post("/heartbeat")
    def heartbeat(
        req: HeartbeatRequest, conn: sqlite3.Connection = Depends(get_conn)
    ) -> dict:
        try:
            # 一律用關鍵字:process() 的參數已經多到位置對應不可靠,
            # 中間插一個新參數就會把後面全部錯位,而且不會報錯——只是
            # 把 reclaims 當成 seen 傳進去。
            return app.state.heartbeat.process(
                conn,
                req.host_id,
                req.identifiers,
                discoverable_classes=req.discoverable_classes,
                services=[s.model_dump() for s in req.services],
                seen=req.seen,
                reclaims=[r.model_dump() for r in req.reclaims],
                flashes=[f.model_dump() for f in req.flashes],
                instances=[i.model_dump() for i in req.instances],
            )
        except store.NotFound as e:
            raise HTTPException(404, str(e)) from e

    # MCP 掛在同一個 app、同一個 process:沿用上面那條連線與那把鎖,不引入
    # 第二套並行控制,也不用第二條 SQLite 連線。容器只跑一個 uvicorn 就同時
    # 提供 REST、儀表板與 MCP,Dockerfile 的 CMD 不用改。
    # mount 放在所有路由之後:Starlette 的 mount 會吃掉整個 path prefix。
    app.mount(
        MCP_PATH,
        mcp.streamable_http_app(
            streamable_http_path="/",
            transport_security=TransportSecuritySettings(
                allowed_hosts=MCP_ALLOWED_HOSTS,
                allowed_origins=MCP_ALLOWED_HOSTS,
            ),
        ),
    )
    return app


def _log_mcp_reachability() -> None:
    """啟動時就把「誰連得進 MCP」講清楚。

    DNS rebinding protection 擋掉的請求回 421,不是連線失敗——遠端 agent
    連不進來時很難查到是這個原因。所以啟動時就把設定攤開,並在「綁在
    0.0.0.0 但只放行本機」這個一定會出事的組合上明確警告。
    """
    log.info("MCP mounted at %s; allowed hosts: %s",
             MCP_PATH, ", ".join(MCP_ALLOWED_HOSTS))
    if not any(h not in ("127.0.0.1", "localhost", "::1")
               for h in MCP_ALLOWED_HOSTS):
        log.warning(
            "MCP only accepts local connections. Remote agents will get "
            "421 Misdirected Request. Set COORDINATOR_MCP_ALLOWED_HOSTS to the "
            "host:port agents actually dial (e.g. 100.69.80.97:8090)."
        )


def _reaper_tick(app: FastAPI) -> None:
    """一輪回收。**同步函式**,而且會阻塞在鎖上——所以只能在 threadpool 跑。"""
    with app.state.lock:
        conn = app.state.conn
        store.reap_expired_leases(conn)
        store.reap_stale(conn, OFFLINE_AFTER_S)
        store.escalate_stuck_devices(conn, RECLAIM_AFTER_S)
        app.state.heartbeat.finalize_detaches(conn)
        # 裝置剛被放回 free 就交給佇列裡的下一個,不用等下一輪。
        # lease 結束卻還跑著的 VM:孤兒 VM 佔的是真的 RAM。
        store.reap_orphan_instances(conn)
        store.expire_stale_jobs(conn)
        store.run_scheduler(conn)
        conn.commit()


async def _reaper_loop(app: FastAPI) -> None:
    """背景任務:回收過期 lease、標記失聯裝置、結算 detach、升級強制回收。

    **實際工作丟到 threadpool,不在 event loop 上直接跑。**

    這裡原本是 ``with app.state.lock:`` 直接寫在 coroutine 裡,那會整個
    卡死服務,而且是真的發生過:``threading.Lock.acquire()`` 是阻塞呼叫,
    在 coroutine 裡執行時**整個 event loop 停住**——不只這個任務,連 accept
    新連線、讀既有請求、送回應全部停。而鎖的持有者是跑在 threadpool 上的
    sync endpoint,它要把回應送出去就得靠那個已經被凍住的 event loop。
    兩邊互等,服務永久卡死,容器看起來還是 healthy(行程活著、port 開著),
    只是所有請求都逾時。

    真機上的觸發條件:exporter 的一次 heartbeat 因為同時在 spawn 一台
    Cuttlefish 而變慢,鎖被多持有了一會兒,reaper 剛好在那個窗口醒來。

    ``run_in_executor(None, ...)`` 讓阻塞的部分在 threadpool 等鎖,event
    loop 繼續跑——跟 FastAPI 對待 sync endpoint 的做法一致(它們也是被丟到
    threadpool),所以鎖的競爭者全部在同一個世界裡,沒有人凍住排程器。
    """
    loop = asyncio.get_running_loop()
    while True:
        await asyncio.sleep(REAPER_INTERVAL_S)
        try:
            await loop.run_in_executor(None, _reaper_tick, app)
        except Exception:                       # noqa: BLE001
            # 一輪失敗不該讓 reaper 整個停掉——那會讓過期的 lease 永遠不
            # 回收,而且沒有任何人會發現。
            log.exception("reaper tick failed; continuing")


app = create_app()
