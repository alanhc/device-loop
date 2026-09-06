"""MCP 前門的 tool 行為與授權(設計文件 §6)。

重點不是 MCP 協定本身,是「每個碰裝置的 tool 都真的驗了 lease」——
漏掉一個就等於那個 tool 沒有保護。
"""

from __future__ import annotations

import threading

import pytest

from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError

from coordinator import db, store
from coordinator.mcp_server import create_mcp

DEVICE = "pixel8-shiba"
OWNER = "alice"

# 碰裝置、必須驗「你持有該裝置」的 tool。
DEVICE_TOOLS = ("adb_shell", "get_uart_stream", "flash_image", "scrcpy_launch",
                "get_video_stream", "get_vnc_stream")
# 操作既有 lease、必須驗「這條 lease 是你的」的 tool。
LEASE_TOOLS = ("renew_lease", "release_device", "get_queue_status", "cancel_queued")
# 只驗「這筆紀錄是你的」的 tool:它們碰的是自己送出的工作(flash job),
# 不是裝置本身。id 一樣是連號整數,所以擁有權還是要驗——不驗的話可以拿來
# 探測別人刷了什麼 image 進哪台裝置。
RECORD_TOOLS = ("get_flash_status",)
# 刻意不需要授權的 tool,列出來讓守衛測試知道它們是故意的。
# list_images 只讀 registry(哪些 image 登記過),不揭露任何 lease 或
# 使用者資訊,也不碰裝置。
UNAUTHENTICATED_TOOLS = ("list_devices", "reserve_device", "list_images")


@pytest.fixture()
def anyio_backend():
    """只跑 asyncio 後端——不需要為了這些測試把 trio 拉進相依。"""
    return "asyncio"


@pytest.fixture()
def conn():
    """MCP 把 sync tool 丟到 threadpool 跑,所以連線要 check_same_thread=False
    ——跟 api.py 同樣的理由與同樣的做法(單一連線 + 一把鎖序列化存取)。"""
    c = db.connect(":memory:", check_same_thread=False)
    db.init_db(c)
    db.seed_db(c)
    yield c
    c.close()


@pytest.fixture()
def mcp(conn):
    return create_mcp(conn, threading.Lock())


async def _call(mcp, name: str, **kwargs):
    """呼叫 tool 並取出結構化回傳值。

    MCP 會把 tool 拋出的例外包成 UnexpectedToolError,原始例外放在
    ``__cause__``;測試要斷言的是我們自己的例外型別,所以在這裡拆開。
    """
    try:
        result = await mcp.call_tool(name, kwargs)
    except UnexpectedToolError as e:
        # 非預期例外:SDK 把原因藏在 __cause__,測試要看真正的型別
        raise e.__cause__ from None
    payload = result.structured_content
    # 回傳 list 的 tool 會被包成 {"result": [...]}
    if isinstance(payload, dict) and set(payload) == {"result"}:
        return payload["result"]
    return payload


def _register_endpoint(conn, device_id: str, service: str, endpoint: str) -> None:
    conn.execute(
        "INSERT INTO device_services (device_id, service, endpoint, mediated) "
        "VALUES (?, ?, ?, 0)",
        (device_id, service, endpoint),
    )
    conn.commit()


@pytest.mark.anyio
async def test_list_devices_needs_no_lease(mcp):
    devices = await _call(mcp, "list_devices")
    assert any(d["id"] == DEVICE for d in devices)


@pytest.mark.anyio
async def test_reserve_then_adb_shell_returns_endpoint(mcp, conn):
    _register_endpoint(conn, DEVICE, "adb", "100.71.211.115:9001")
    lease = await _call(mcp, "reserve_device", device_id=DEVICE, user_id=OWNER, ttl_s=600)
    out = await _call(mcp, "adb_shell", device_id=DEVICE, user_id=OWNER,
                      lease_id=lease["id"])
    assert out["endpoint"] == "100.71.211.115:9001"
    assert out["connect"] == "adb connect 100.71.211.115:9001"


# 碰裝置的 tool 除了 device_id/user_id 之外還要的必填參數。授權檢查一定
# 在這些參數用到之前就發生——被擋下來時,image 存不存在都還沒被問過。
EXTRA_ARGS = {"flash_image": {"image_id": "any-image"}}


@pytest.mark.anyio
@pytest.mark.parametrize("tool", DEVICE_TOOLS)
async def test_device_tools_refuse_without_a_lease(mcp, conn, tool):
    """每個碰裝置的 tool 都要擋。"""
    _register_endpoint(conn, DEVICE, "adb", "host:1")
    _register_endpoint(conn, DEVICE, "uart", "host:2")
    with pytest.raises(ToolError, match="no active lease"):
        await _call(mcp, tool, device_id=DEVICE, user_id=OWNER,
                    **EXTRA_ARGS.get(tool, {}))


@pytest.mark.anyio
@pytest.mark.parametrize("tool", DEVICE_TOOLS)
async def test_device_tools_refuse_another_users_lease(mcp, conn, tool):
    _register_endpoint(conn, DEVICE, "adb", "host:1")
    _register_endpoint(conn, DEVICE, "uart", "host:2")
    store.reserve(conn, DEVICE, OWNER, 600)
    with pytest.raises(ToolError, match="leased by someone else"):
        await _call(mcp, tool, device_id=DEVICE, user_id="mallory",
                    **EXTRA_ARGS.get(tool, {}))


@pytest.mark.anyio
async def test_uart_reports_rfc2217(mcp, conn):
    _register_endpoint(conn, DEVICE, "uart", "100.71.211.115:9002")
    store.reserve(conn, DEVICE, OWNER, 600)
    out = await _call(mcp, "get_uart_stream", device_id=DEVICE, user_id=OWNER)
    assert out["protocol"] == "rfc2217"
    assert out["connect"] == "telnet 100.71.211.115 9002"


@pytest.mark.anyio
async def test_missing_endpoint_is_a_clear_error_not_a_crash(mcp, conn):
    """lease 有效但 exporter 還沒回報 endpoint——要講清楚是哪一種失敗。"""
    store.reserve(conn, DEVICE, OWNER, 600)
    with pytest.raises(ToolError, match="pending|never reported"):
        await _call(mcp, "adb_shell", device_id=DEVICE, user_id=OWNER)


@pytest.mark.anyio
async def test_reserve_conflicts_when_already_leased(mcp, conn):
    store.reserve(conn, DEVICE, OWNER, 600)
    with pytest.raises(ToolError, match="already has an active lease|not free"):
        await _call(mcp, "reserve_device", device_id=DEVICE, user_id="bob",
                    ttl_s=600, queue=False)


@pytest.mark.anyio
async def test_reserve_unknown_device_fails(mcp):
    with pytest.raises(ToolError, match="not registered"):
        await _call(mcp, "reserve_device", device_id="nope", user_id=OWNER, ttl_s=600)


@pytest.mark.anyio
async def test_release_frees_the_device_for_others(mcp, conn):
    lease = await _call(mcp, "reserve_device", device_id=DEVICE, user_id=OWNER, ttl_s=600)
    await _call(mcp, "release_device", lease_id=lease["id"], user_id=OWNER)
    again = await _call(mcp, "reserve_device", device_id=DEVICE, user_id="bob", ttl_s=600)
    assert again["user_id"] == "bob"


@pytest.mark.anyio
async def test_access_is_revoked_after_release(mcp, conn):
    """release 之後同一個 user 也不能再操作。"""
    _register_endpoint(conn, DEVICE, "adb", "host:1")
    lease = await _call(mcp, "reserve_device", device_id=DEVICE, user_id=OWNER, ttl_s=600)
    await _call(mcp, "release_device", lease_id=lease["id"], user_id=OWNER)
    with pytest.raises(ToolError, match="no active lease"):
        await _call(mcp, "adb_shell", device_id=DEVICE, user_id=OWNER)


@pytest.mark.anyio
async def test_renew_extends_the_lease(mcp, conn):
    lease = await _call(mcp, "reserve_device", device_id=DEVICE, user_id=OWNER, ttl_s=60)
    renewed = await _call(mcp, "renew_lease", lease_id=lease["id"], user_id=OWNER, ttl_s=600)
    assert renewed["expires_at"] > lease["expires_at"]


@pytest.mark.anyio
async def test_every_tool_is_classified_for_authorization(mcp):
    """防止新增 tool 時忘了授權。

    原本這條只檢查「收 device_id + user_id」的 tool,結果漏掉了
    renew_lease / release_device——它們只收 lease_id,不符合那個形狀,
    但一樣需要授權(lease id 是連號整數)。改成要求**每個** tool 都被
    明確分類:碰裝置的、操作 lease 的、或刻意不需要授權的。
    """
    tools = await mcp.list_tools()
    names = {t.name for t in tools}
    classified = (set(DEVICE_TOOLS) | set(LEASE_TOOLS) | set(RECORD_TOOLS)
                  | set(UNAUTHENTICATED_TOOLS))
    assert classified <= names
    unclassified = names - classified
    assert not unclassified, (
        f"這些 tool 沒有分類,不知道該不該做授權檢查:{sorted(unclassified)}。"
        "碰裝置的加進 DEVICE_TOOLS、操作既有 lease 的加進 LEASE_TOOLS、"
        "驗自己紀錄擁有權的加進 RECORD_TOOLS、"
        "確定不需要授權的加進 UNAUTHENTICATED_TOOLS。"
    )


@pytest.mark.anyio
@pytest.mark.parametrize("tool", LEASE_TOOLS + DEVICE_TOOLS + RECORD_TOOLS)
async def test_authorized_tools_all_take_a_user_id(mcp, tool):
    """沒有 user_id 就無從驗證擁有權——這正是原本的漏洞形狀。"""
    tools = {t.name: t for t in await mcp.list_tools()}
    params = set((tools[tool].input_schema or {}).get("properties", {}))
    assert "user_id" in params, f"{tool} 需要授權卻沒收 user_id"


# ------------------------------------------------- lease 擁有權(越權)
# leases.id 是連號整數,猜測成本趨近於零。少了擁有權檢查,任何人都可以
# release 掉別人的 lease 讓裝置回到 free,再立刻搶走——被害者不會收到
# 任何錯誤,會繼續對一台已經不屬於它的裝置下指令。

@pytest.mark.anyio
async def test_another_user_cannot_release_your_lease(mcp, conn):
    lease = await _call(mcp, "reserve_device", device_id=DEVICE, user_id=OWNER, ttl_s=600)
    with pytest.raises(ToolError, match="not found or not yours"):
        await _call(mcp, "release_device", lease_id=lease["id"], user_id="mallory")
    # 裝置仍然是 alice 的
    state = conn.execute(
        "SELECT state FROM devices WHERE id = ?", (DEVICE,)
    ).fetchone()["state"]
    assert state == "leased"


@pytest.mark.anyio
async def test_another_user_cannot_renew_your_lease(mcp, conn):
    lease = await _call(mcp, "reserve_device", device_id=DEVICE, user_id=OWNER, ttl_s=600)
    with pytest.raises(ToolError, match="not found or not yours"):
        await _call(mcp, "renew_lease", lease_id=lease["id"], user_id="mallory", ttl_s=9999)


@pytest.mark.anyio
async def test_the_full_hijack_path_is_closed(mcp, conn):
    """完整攻擊路徑:release 掉 A 的 lease → 裝置 free → B 搶走。"""
    lease = await _call(mcp, "reserve_device", device_id=DEVICE, user_id=OWNER, ttl_s=600)
    with pytest.raises(ToolError):
        await _call(mcp, "release_device", lease_id=lease["id"], user_id="mallory")
    # mallory 現在會拿到號碼牌,但**拿不到裝置**——排隊不是搶奪。
    ticket = await _call(mcp, "reserve_device", device_id=DEVICE,
                         user_id="mallory", ttl_s=600)
    assert ticket["queued"] is True
    assert ticket["lease_id"] is None
    # A 仍然操作得動自己的裝置
    assert await _call(mcp, "renew_lease", lease_id=lease["id"], user_id=OWNER, ttl_s=600)


@pytest.mark.anyio
async def test_unknown_lease_id_does_not_leak_existence(mcp, conn):
    """連號 id 不該拿來探測哪些 lease 存在。

    兩種情形要回同一句話——訊息裡只有呼叫者自己傳進來的 id 不同,那是
    它本來就知道的,不構成洩漏。
    """
    lease = await _call(mcp, "reserve_device", device_id=DEVICE, user_id=OWNER, ttl_s=600)
    missing_id = lease["id"] + 999
    with pytest.raises(ToolError) as missing:
        await _call(mcp, "release_device", lease_id=missing_id, user_id="mallory")
    with pytest.raises(ToolError) as not_yours:
        await _call(mcp, "release_device", lease_id=lease["id"], user_id="mallory")
    # 把各自的 id 抽掉之後,兩句話必須一模一樣
    assert str(missing.value).replace(str(missing_id), "<id>") == \
           str(not_yours.value).replace(str(lease["id"]), "<id>")


@pytest.mark.anyio
async def test_owner_can_still_renew_and_release(mcp, conn):
    lease = await _call(mcp, "reserve_device", device_id=DEVICE, user_id=OWNER, ttl_s=60)
    renewed = await _call(mcp, "renew_lease", lease_id=lease["id"], user_id=OWNER, ttl_s=600)
    assert renewed["expires_at"] > lease["expires_at"]
    released = await _call(mcp, "release_device", lease_id=lease["id"], user_id=OWNER)
    assert released["status"] == "released"


@pytest.mark.anyio
async def test_denial_reason_reaches_the_agent(mcp, conn):
    """預期內的失敗要用 ToolError,訊息才會原樣送到 agent 手上。

    用其他例外的話 SDK 會當成 crash,agent 只看得到
    「Error executing tool adb_shell」,沒辦法據以修正行為(該去 reserve?
    還是 lease 過期了?)。這條走完整的 call_tool 路徑驗訊息真的送得到。
    """
    store.reserve(conn, DEVICE, OWNER, 600)
    with pytest.raises(ToolError) as e:
        await mcp.call_tool("adb_shell", {"device_id": DEVICE, "user_id": "mallory"})
    # ToolError 的訊息會原樣進到送回 client 的 payload;非預期例外只會得到
    # 「Error executing tool adb_shell」而沒有下文。
    assert "leased by someone else" in str(e.value)
    assert OWNER not in str(e.value)        # 仍然不洩漏持有者


# ------------------------------------------------- endpoint 的最終一致性
# endpoint 要等 exporter 下一輪 heartbeat 回報才會出現。reserve 完立刻取
# 一定撲空,那是 pending 不是錯誤——但「exporter 沒在跑」「這個 class 根本
# 沒這種服務」也是撲空,對 agent 來說行動完全不同,訊息要分得開。

@pytest.mark.anyio
async def test_pending_endpoint_tells_the_agent_to_retry(mcp, conn):
    """exporter 在線但還沒回報:稍等重試。"""
    conn.execute(
        "UPDATE hosts SET last_seen_at = '2026-09-05T12:00:00+00:00' "
        "WHERE id = 'rog-laptop'"
    )
    store.reserve(conn, DEVICE, OWNER, 600)
    with pytest.raises(ToolError, match="pending"):
        await _call(mcp, "adb_shell", device_id=DEVICE, user_id=OWNER)


@pytest.mark.anyio
async def test_silent_exporter_is_distinguished_from_pending(mcp, conn):
    """exporter 從沒回報過:不是稍等就會好,是它可能沒在跑。"""
    store.reserve(conn, DEVICE, OWNER, 600)
    with pytest.raises(ToolError, match="never reported|may not be running"):
        await _call(mcp, "adb_shell", device_id=DEVICE, user_id=OWNER)


@pytest.mark.anyio
async def test_service_the_class_never_exports_says_so(mcp, conn):
    """riscv-sbc 沒有 adb;這個 endpoint 等再久也不會出現,要講明白。"""
    store.reserve(conn, "milkv-jupiter", OWNER, 600)
    with pytest.raises(ToolError, match="never appear"):
        await _call(mcp, "adb_shell", device_id="milkv-jupiter", user_id=OWNER)


@pytest.mark.anyio
async def test_endpoint_is_returned_once_the_exporter_reports_it(mcp, conn):
    """走完整條路:reserve → exporter 回報 → endpoint 拿得到。"""
    from coordinator.store import HeartbeatProcessor

    store.reserve(conn, DEVICE, OWNER, 600)
    hb = HeartbeatProcessor()
    result = hb.process(
        conn, "rog-laptop", ["38011FDJH00C9F"], ["android"],
        [{"device_id": DEVICE, "service": "adb", "port": 9001}],
    )
    assert result["recorded"] == [f"{DEVICE}/adb"]
    out = await _call(mcp, "adb_shell", device_id=DEVICE, user_id=OWNER)
    assert out["endpoint"] == "100.71.211.115:9001"   # tailscale IP,不是 host id


# ------------------------------------------------------------ 佇列(MCP)

@pytest.mark.anyio
async def test_reserve_queues_by_default_and_does_not_block(mcp, conn):
    """MCP tool call 不能卡幾分鐘等輪到——立刻回號碼牌。"""
    store.reserve(conn, DEVICE, OWNER, 600)
    ticket = await _call(mcp, "reserve_device", device_id=DEVICE,
                         user_id="bob", ttl_s=600)
    assert ticket["queued"] is True
    assert ticket["position"] == 1
    assert ticket["lease_id"] is None


@pytest.mark.anyio
async def test_free_device_is_leased_without_queueing(mcp, conn):
    out = await _call(mcp, "reserve_device", device_id=DEVICE,
                      user_id=OWNER, ttl_s=600)
    assert out["queued"] is False
    assert out["id"] > 0


@pytest.mark.anyio
async def test_queue_status_shows_position(mcp, conn):
    store.reserve(conn, DEVICE, OWNER, 600)
    ticket = await _call(mcp, "reserve_device", device_id=DEVICE,
                         user_id="bob", ttl_s=600)
    status = await _call(mcp, "get_queue_status",
                         job_id=ticket["job_id"], user_id="bob")
    assert status["position"] == 1


@pytest.mark.anyio
async def test_cannot_read_another_users_ticket(mcp, conn):
    store.reserve(conn, DEVICE, OWNER, 600)
    ticket = await _call(mcp, "reserve_device", device_id=DEVICE,
                         user_id="bob", ttl_s=600)
    with pytest.raises(ToolError, match="not found or not yours"):
        await _call(mcp, "get_queue_status",
                    job_id=ticket["job_id"], user_id="mallory")


@pytest.mark.anyio
async def test_cancel_queued_releases_the_slot(mcp, conn):
    store.reserve(conn, DEVICE, OWNER, 600)
    bob = await _call(mcp, "reserve_device", device_id=DEVICE,
                      user_id="bob", ttl_s=600)
    carol = await _call(mcp, "reserve_device", device_id=DEVICE,
                        user_id="carol", ttl_s=600)
    await _call(mcp, "cancel_queued", job_id=bob["job_id"], user_id="bob")
    status = await _call(mcp, "get_queue_status",
                         job_id=carol["job_id"], user_id="carol")
    assert status["position"] == 1


@pytest.mark.anyio
async def test_the_whole_queue_handoff_over_mcp(mcp, conn):
    """alice 借 → bob 排隊 → alice 釋放 → scheduler 交棒 → bob 拿到 lease。"""
    lease = await _call(mcp, "reserve_device", device_id=DEVICE,
                        user_id=OWNER, ttl_s=600)
    ticket = await _call(mcp, "reserve_device", device_id=DEVICE,
                         user_id="bob", ttl_s=600)
    await _call(mcp, "release_device", lease_id=lease["id"], user_id=OWNER)
    store.run_scheduler(conn)
    status = await _call(mcp, "get_queue_status",
                         job_id=ticket["job_id"], user_id="bob")
    assert status["state"] == "running"
    assert status["lease_id"] is not None
    # bob 現在真的能操作裝置了
    _register_endpoint(conn, DEVICE, "adb", "100.71.211.115:9001")
    out = await _call(mcp, "adb_shell", device_id=DEVICE, user_id="bob")
    assert out["endpoint"] == "100.71.211.115:9001"


@pytest.mark.anyio
async def test_every_tool_taking_user_id_goes_through_caller_identity():
    """身分只能從 authz.caller_identity 取得,不准直接用參數。

    網路化之後 user_id 從「本機呼叫者自填」變成「網路上任何人自填」。
    現在 caller_identity 還只是把宣稱值傳回去,但**位置**要對——之後接
    tailnet identity 只改那一個函式。任何 tool 繞過它,那個 tool 就會
    在改認證時被漏掉。

    用原始碼檢查而不是行為檢查,因為「有沒有繞過」現在還看不出行為差異
    ——正是這種還沒有症狀的疏漏最需要守衛。
    """
    import ast
    import inspect

    from coordinator import mcp_server

    def calls_caller_identity(fn: ast.FunctionDef) -> bool:
        """這個函式體內有沒有真的呼叫 caller_identity。

        不能用 ast.dump 的字串搜尋:巢狀函式的 dump 會包含外層文字,
        結果是「什麼都通過」的假守衛(第一版就是這樣寫的,注入一個繞過的
        tool 之後測試照樣綠)。要走 Call 節點。
        """
        for sub in ast.walk(fn):
            if not isinstance(sub, ast.Call):
                continue
            f = sub.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
            if name == "caller_identity":
                return True
        return False

    tree = ast.parse(inspect.getsource(mcp_server))
    offenders = [
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and "user_id" in {a.arg for a in node.args.args}
        and not calls_caller_identity(node)
    ]
    assert not offenders, (
        f"這些 tool 收了 user_id 卻沒過 authz.caller_identity:{offenders}。"
        "身分取得要收斂在一處,之後接真認證才不會漏掉。"
    )
