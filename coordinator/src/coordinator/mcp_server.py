"""MCP 前門:把 lease API 與裝置能力包成 tools(設計文件 §6)。

這是 agent 操作裝置的統一入口。**每個碰到裝置的 tool 都先過
``authz.require_lease``**——設計文件明講授權檢查不能省。

Phase 1 的 tool 集合:

- ``list_devices``——唯讀,不需要授權。
- ``reserve_device``——不需要既有 lease(它本來就是來要 lease 的)。
- ``renew_lease`` / ``release_device``——驗**持有權**:這條 lease 是不是
  你的。``leases.id`` 是連號整數,少了這層任何人都能 release 掉別人的
  lease 再把裝置搶走。
- ``adb_shell`` / ``get_uart_stream``——碰裝置,驗你持有**該裝置**的有效
  lease。

預期內的失敗(沒 lease、被別人借走、裝置不存在)一律拋 ``ToolError``,
訊息才會原樣送到 agent 手上;其他例外會被 SDK 當成 crash,agent 只看得到
「Error executing tool X」這種沒有資訊的字串,沒辦法據以修正行為。

**endpoint 從哪來**:``device_services`` 由 exporter 在 heartbeat 回報
實際 port 後寫入。這兩個 tool 查表把 endpoint 交給 agent 自己去連,不由
coordinator 代跑指令——§6 說 adb/uart 本來就是 client 直連,只有 flash
是 mediated(Phase 2)。

**endpoint 是最終一致的**:coordinator 算出 desired 之後要等 exporter
下一輪 heartbeat 起好 daemon 並回報,才會出現。所以 reserve 完立刻取
endpoint 一定撲空,那是 **pending 不是錯誤**;``_pending_reason`` 會把
「還沒好、稍等重試」跟「exporter 沒在跑」「這個 class 根本沒這種服務」
分開講,因為 agent 該採取的行動完全不同。
"""

from __future__ import annotations

import sqlite3
import threading
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import authz, store


def create_mcp(
    conn: sqlite3.Connection,
    lock: threading.Lock,
    name: str = "device-loop",
) -> MCPServer:
    """建一個綁在既有 DB 連線上的 MCP server。

    跟 HTTP API 共用同一條連線與同一把鎖(api.py 的單一 writer 模型),
    所以 MCP 的寫入跟 HTTP 的寫入不會互相踩到。
    """
    mcp = MCPServer(
        name=name,
        instructions=(
            "裝置預約系統的前門。操作任何裝置前要先 reserve_device 拿到 "
            "lease,用完 release_device。碰裝置的 tool 都會驗證你持有該 "
            "裝置的有效 lease。"
        ),
    )

    def _ticket(job: sqlite3.Row) -> dict[str, Any]:
        return {
            "queued": job["state"] == "queued",
            "job_id": job["id"],
            "state": job["state"],
            "device_id": job["device_id"],
            "position": store.queue_position(conn, job["id"]),
            "lease_id": job["lease_id"],
        }

    def _endpoint(device_id: str, service: str) -> str | None:
        row = conn.execute(
            "SELECT endpoint FROM device_services WHERE device_id = ? AND service = ?",
            (device_id, service),
        ).fetchone()
        return row["endpoint"] if row else None

    def _pending_reason(device_id: str, service: str) -> str:
        """endpoint 還沒出現時,講清楚是「還沒好」還是「不會好」。

        endpoint 是最終一致的:coordinator 算出 desired 之後,要等 exporter
        下一輪 heartbeat 起好 daemon、把實際 port 回報上來,才會出現在
        device_services。所以 reserve 完立刻呼叫本 tool 一定會撲空——那是
        pending 不是錯誤,agent 該做的是稍等再試。

        但「exporter 掛了」「這個 class 根本沒有這種服務」也是撲空,對 agent
        來說行動完全不同,所以要分開講。
        """
        dev = conn.execute(
            "SELECT class, host FROM devices WHERE id = ?", (device_id,)
        ).fetchone()
        if dev is None:
            return f"device {device_id!r} not registered"
        if service not in store.CLASS_SERVICES.get(dev["class"], ()):
            return (
                f"device class {dev['class']!r} does not export {service!r}; "
                "this endpoint will never appear"
            )
        if dev["host"] is None:
            return (
                f"device {device_id!r} is not currently on any exporter host; "
                "no endpoint can be created"
            )
        host = conn.execute(
            "SELECT last_seen_at FROM hosts WHERE id = ?", (dev["host"],)
        ).fetchone()
        if host is None or host["last_seen_at"] is None:
            return (
                f"the exporter on host {dev['host']!r} has never reported in; "
                "it may not be running"
            )
        return (
            f"{service} endpoint for {device_id!r} is still pending — the exporter "
            f"on {dev['host']!r} reports it on its next heartbeat. Retry shortly."
        )

    @mcp.tool(description="列出所有裝置與目前狀態(free/leased/offline/unregistered)。")
    def list_devices() -> list[dict[str, Any]]:
        with lock:
            rows = conn.execute(
                "SELECT id, class, control, host, state, tags FROM devices "
                "WHERE state != 'retired' ORDER BY id"
            ).fetchall()
        return [dict(r) for r in rows]

    @mcp.tool(
        description=(
            "預約一台裝置一段時間(獨佔)。裝置 free 就直接回 lease;"
            "被別人借走時預設排隊,立刻回一張號碼牌(job_id + position),"
            "**不會卡住等待**——之後用 get_queue_status 輪詢。"
            "queue=false 則改成直接失敗。"
        )
    )
    def reserve_device(
        device_id: str, user_id: str, ttl_s: int,
        purpose: str | None = None, queue: bool = True,
    ) -> dict[str, Any]:
        caller = authz.caller_identity(user_id)
        with lock:
            try:
                lease = store.reserve(conn, device_id, caller, ttl_s, purpose)
            except store.NotFound as e:
                raise ToolError(str(e)) from e
            except store.Conflict as e:
                if not queue:
                    raise ToolError(str(e)) from e
                try:
                    job = store.enqueue(conn, device_id, caller, ttl_s)
                except (store.NotFound, store.Conflict) as e2:
                    raise ToolError(str(e2)) from e2
                return _ticket(job)
            return {"queued": False, **dict(lease)}

    @mcp.tool(
        description=(
            "查排隊進度。position 是還排第幾(1 = 下一個);"
            "輪到時 state 變 running,並帶回可以直接使用的 lease_id。"
        )
    )
    def get_queue_status(job_id: int, user_id: str) -> dict[str, Any]:
        caller = authz.caller_identity(user_id)
        with lock:
            job = conn.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if job is None or job["user_id"] != caller:
                raise ToolError(f"job {job_id} not found or not yours")
            return _ticket(job)

    @mcp.tool(
        description="放棄排隊,把位置讓給後面的人。不用了就要呼叫,否則會一直佔位。"
    )
    def cancel_queued(job_id: int, user_id: str) -> dict[str, Any]:
        caller = authz.caller_identity(user_id)
        with lock:
            try:
                return _ticket(store.cancel_job(conn, job_id, caller))
            except (store.NotFound, store.Conflict) as e:
                raise ToolError(str(e)) from e

    @mcp.tool(
        description=(
            "延長自己持有的 lease 的 TTL。長時間工作要定期呼叫,"
            "否則會被 reaper 回收。"
        )
    )
    def renew_lease(lease_id: int, user_id: str, ttl_s: int) -> dict[str, Any]:
        caller = authz.caller_identity(user_id)
        with lock:
            try:
                authz.require_lease_owner(conn, lease_id, caller)
                lease = store.renew(conn, lease_id, ttl_s)
            except authz.LeaseDenied as e:
                raise ToolError(str(e)) from e
            except (store.NotFound, store.Conflict) as e:
                raise ToolError(str(e)) from e
        return dict(lease)

    @mcp.tool(description="釋放自己持有的 lease,裝置回到 free 讓別人可以借。")
    def release_device(lease_id: int, user_id: str) -> dict[str, Any]:
        caller = authz.caller_identity(user_id)
        with lock:
            try:
                authz.require_lease_owner(conn, lease_id, caller)
                lease = store.release(conn, lease_id)
            except authz.LeaseDenied as e:
                raise ToolError(str(e)) from e
            except (store.NotFound, store.Conflict) as e:
                raise ToolError(str(e)) from e
        return dict(lease)

    @mcp.tool(
        description=(
            "取得該裝置的 adb 連線 endpoint。需要持有有效 lease。"
            "回傳 host:port,client 用 `adb -H <host> -P <port>` 連上後直接操作。"
        )
    )
    def adb_shell(device_id: str, user_id: str, lease_id: int | None = None) -> dict[str, Any]:
        caller = authz.caller_identity(user_id)
        with lock:
            try:
                lease = authz.require_lease(conn, device_id, caller, lease_id)
            except authz.LeaseDenied as e:
                raise ToolError(str(e)) from e
            endpoint = _endpoint(device_id, "adb")
        if endpoint is None:
            raise ToolError(_pending_reason(device_id, "adb"))
        host, _, port = endpoint.rpartition(":")
        return {
            "device_id": device_id,
            "lease_id": lease["id"],
            "endpoint": endpoint,
            # **不是 `adb connect`。** endpoint 是一個 adb **server**
            # (exporter 起的 --one-device server),不是裝置的 transport
            # ——`adb connect` 對它會回 offline。要用 -H/-P 把它當 server 用。
            # 真機驗證過的形式就是這個(Pixel 8 與 Cuttlefish 都是)。
            "connect": f"adb -H {host} -P {port} devices",
            "shell": f"adb -H {host} -P {port} shell",
        }

    @mcp.tool(
        description=(
            "取得該裝置的 UART endpoint(RFC2217 telnet)。需要持有有效 lease。"
            "client 可以遠端改 baud、拉 DTR/RTS。"
        )
    )
    def get_uart_stream(
        device_id: str, user_id: str, lease_id: int | None = None
    ) -> dict[str, Any]:
        caller = authz.caller_identity(user_id)
        with lock:
            try:
                lease = authz.require_lease(conn, device_id, caller, lease_id)
            except authz.LeaseDenied as e:
                raise ToolError(str(e)) from e
            endpoint = _endpoint(device_id, "uart")
        if endpoint is None:
            raise ToolError(_pending_reason(device_id, "uart"))
        host, _, port = endpoint.rpartition(":")
        return {
            "device_id": device_id,
            "lease_id": lease["id"],
            "endpoint": endpoint,
            "protocol": "rfc2217",
            "connect": f"telnet {host} {port}",
        }

    @mcp.tool(
        description=(
            "取得在自己桌面上跑 scrcpy 的指令(Android 畫面鏡射 + 操作)。"
            "需要持有有效 lease。scrcpy 疊在 adb 之上,同一條 lease 就涵蓋,"
            "不需要另外的服務。"
        )
    )
    def scrcpy_launch(
        device_id: str, user_id: str, lease_id: int | None = None
    ) -> dict[str, Any]:
        """§6:scrcpy「不需要額外的 host 端服務,同一條 adb lease 授權涵蓋」。

        所以這個 tool 不啟動任何東西,它把 adb endpoint 組成一行可以直接
        在**使用者自己桌面**上跑的指令。在無頭的 exporter host 上起 scrcpy
        等於開一個沒人看得到的視窗——真正要顯示畫面的是 client 那端。
        """
        caller = authz.caller_identity(user_id)
        with lock:
            try:
                lease = authz.require_lease(conn, device_id, caller, lease_id)
            except authz.LeaseDenied as e:
                raise ToolError(str(e)) from e
            endpoint = _endpoint(device_id, "adb")
        if endpoint is None:
            raise ToolError(_pending_reason(device_id, "adb"))
        host, _, port = endpoint.rpartition(":")
        return {
            "device_id": device_id,
            "lease_id": lease["id"],
            "endpoint": endpoint,
            # endpoint 是 adb **server** 而不是裝置 transport,所以 scrcpy
            # 要靠 ADB_SERVER_SOCKET 指向它,不是 --tcpip(那是叫 scrcpy
            # 自己去 connect 一台裝置,對 server 位址不成立)。
            "connect": f"adb -H {host} -P {port} devices",
            "command": (
                f"ADB_SERVER_SOCKET=tcp:{host}:{port} scrcpy"
            ),
            "note": "run this on your own desktop, not on the exporter host",
        }

    @mcp.tool(
        description=(
            "取得該裝置的 camera 串流 endpoint(MJPEG over HTTP)。"
            "需要持有有效 lease。照的是**實體螢幕**——裝置沒開機、卡在 "
            "fastboot 或 kernel panic 的時候,這是唯一看得到的東西。"
        )
    )
    def get_video_stream(
        device_id: str, user_id: str, lease_id: int | None = None
    ) -> dict[str, Any]:
        caller = authz.caller_identity(user_id)
        with lock:
            try:
                lease = authz.require_lease(conn, device_id, caller, lease_id)
            except authz.LeaseDenied as e:
                raise ToolError(str(e)) from e
            endpoint = _endpoint(device_id, "video")
        if endpoint is None:
            raise ToolError(_pending_reason(device_id, "video"))
        return {
            "device_id": device_id,
            "lease_id": lease["id"],
            "endpoint": endpoint,
            "format": "mjpeg",
            "stream_url": f"http://{endpoint}/stream",
            "snapshot_url": f"http://{endpoint}/snapshot",
        }

    @mcp.tool(
        description=(
            "取得該裝置的 VNC endpoint。需要持有有效 lease。"
            "看的是**軟體 framebuffer**(OS 活著才有);OS 死了要看畫面用 "
            "get_video_stream。"
        )
    )
    def get_vnc_stream(
        device_id: str, user_id: str, lease_id: int | None = None
    ) -> dict[str, Any]:
        caller = authz.caller_identity(user_id)
        with lock:
            try:
                lease = authz.require_lease(conn, device_id, caller, lease_id)
            except authz.LeaseDenied as e:
                raise ToolError(str(e)) from e
            endpoint = _endpoint(device_id, "vnc")
        if endpoint is None:
            raise ToolError(_pending_reason(device_id, "vnc"))
        return {
            "device_id": device_id,
            "lease_id": lease["id"],
            "endpoint": endpoint,
            "connect": f"vncviewer {endpoint}",
        }

    @mcp.tool(
        description=(
            "列出可以刷進某台裝置的 image(registry 裡登記過的)。"
            "known_good=true 的是還原目標:刷壞了用它救回來。"
        )
    )
    def list_images(device_id: str | None = None) -> list[dict[str, Any]]:
        with lock:
            return [dict(r) for r in store.list_images(conn, device_id)]

    @mcp.tool(
        description=(
            "刷一份 image 進裝置。需要持有有效 lease。**這是破壞性操作**,"
            "而且是唯一由 coordinator 代管(mediated)的能力——只接受 "
            "registry 裡登記過的 image,不能指定任意檔案路徑。"
            "**不會等它刷完**:立刻回 flash_id,用 get_flash_status 查進度。"
        )
    )
    def flash_image(
        device_id: str, image_id: str, user_id: str, lease_id: int | None = None
    ) -> dict[str, Any]:
        caller = authz.caller_identity(user_id)
        with lock:
            try:
                lease = authz.require_lease(conn, device_id, caller, lease_id)
            except authz.LeaseDenied as e:
                raise ToolError(str(e)) from e
            try:
                job = store.request_flash(
                    conn, device_id, image_id, caller, lease["id"]
                )
            except (store.NotFound, store.Conflict) as e:
                raise ToolError(str(e)) from e
        return {
            "flash_id": job["id"],
            "device_id": device_id,
            "image_id": image_id,
            "state": job["state"],
            "note": (
                "queued for the exporter on this device's host; it picks the job "
                "up on its next heartbeat. Poll get_flash_status."
            ),
        }

    @mcp.tool(
        description=(
            "查一個 flash 的進度。state 是 pending/running/done/failed;"
            "failed 的話 detail 有 exit code 與 stderr。"
        )
    )
    def get_flash_status(flash_id: int, user_id: str) -> dict[str, Any]:
        caller = authz.caller_identity(user_id)
        with lock:
            job = conn.execute(
                "SELECT * FROM flash_jobs WHERE id = ?", (flash_id,)
            ).fetchone()
            # 跟 job/lease 同樣的理由:連號整數 id,不區分「不存在」與
            # 「不是你的」,否則可以拿來探測別人刷了什麼。
            if job is None or job["user_id"] != caller:
                raise ToolError(f"flash job {flash_id} not found or not yours")
            return dict(job)

    return mcp
