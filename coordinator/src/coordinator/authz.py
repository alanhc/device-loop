"""Lease 授權檢查——MCP 前門的每個 tool call 都要先過這一關(設計文件 §6)。

設計文件把 MCP 定為「agent 操作裝置的統一前門」,授權檢查不能省。這裡
把檢查獨立成一個模組而不是散在各個 tool 裡,理由是它是安全邊界:漏掉
一個 tool 就等於那個 tool 沒有保護,集中在一處才審得動。

**目前不是真的認證。** §10 明列認證模型未設計,`user_id` 依約假設可信;
這層做的是「宣稱的 user 是否真的持有這台裝置的有效 lease」,擋的是
「借了 A 卻去操作 B」「lease 過期還在用」「別人的 lease」這類錯誤,不是
冒名。之後接上 Tailscale identity 時,只要把 ``claimed_user_id`` 換成
從連線推導出來的身分,檢查點的位置不用動。
"""

from __future__ import annotations

import sqlite3

from .db import utcnow
from .store import NowFn


class LeaseDenied(Exception):
    """呼叫者沒有權限操作這台裝置。訊息會回給 agent,要講得夠清楚。"""


# ---------------------------------------------------------------- 身分來源

# 把 MCP 開成網路服務之後,`user_id` 從「本機呼叫者自填」變成「網路上任何
# 人自填」。這個函式是**唯一**取得呼叫者身分的地方——所有 tool 與 endpoint
# 都必須經過它,不准直接用參數裡的 user_id。
#
# 現在它就只是把宣稱的值傳回去(§10:認證模型未設計),但位置放對了:
# 之後接 tailnet identity(用對端 IP 查 `tailscale whois`)只要改這一個
# 函式,上面所有 tool 一行都不用動。這跟把授權集中進本模組是同一個手法
# ——安全邊界要收斂在一處才審得動。


def caller_identity(claimed_user_id: str, peer: str | None = None) -> str:
    """取得呼叫者身分。

    ``peer`` 是連線對端(IP 或 socket 資訊),之後接真認證時會用到;現在
    只記錄下來方便稽核與除錯。

    **警告**:目前回傳的就是呼叫者自己宣稱的值,完全不驗證。網路上任何
    連得到 coordinator 的人都能冒充任何 user。緩解只有 tailnet ACL
    (只有 tailnet 內的節點連得到)。見 README 的安全警告。
    """
    if not claimed_user_id:
        raise LeaseDenied("user_id is required")
    return claimed_user_id


def require_lease_owner(
    conn: sqlite3.Connection,
    lease_id: int,
    user_id: str,
) -> sqlite3.Row:
    """確認 ``user_id`` 是 ``lease_id`` 這條 lease 的持有者,回傳那筆 lease。

    給 renew / release 這類「憑 lease_id 操作 lease 本身」的 tool 用。
    ``require_lease`` 是從裝置的角度問「誰現在持有這台裝置」,這裡是從
    lease 的角度問「這條 lease 是不是你的」——release 一條已經不 active
    的 lease 要回報的是「狀態不對」而不是「不是你的」,所以不共用同一個
    函式。

    **為什麼一定要驗**:``leases.id`` 是 INTEGER PRIMARY KEY,連號整數,
    猜測成本趨近於零。沒有這層檢查的話,任何人都可以
    ``release_device(lease_id=5)`` 釋放掉別人的 lease,裝置回到 free 後
    立刻搶走——直接打穿「一台裝置同時最多一條 active lease」這個系統唯一
    真正要緊的不變量,而且被害者不會收到任何錯誤,會繼續對一台已經不屬於
    它的裝置下指令。

    這跟 §10「認證模型未設計」不衝突:那條講的是 authentication(你是不是
    你宣稱的人),這裡做的是 authorization(這條 lease 是不是你的)。
    """
    row = conn.execute("SELECT * FROM leases WHERE id = ?", (lease_id,)).fetchone()
    if row is None:
        # 不區分「不存在」與「不是你的」——否則連號的 id 可以拿來探測
        # 哪些 lease 存在。
        raise LeaseDenied(f"lease {lease_id} not found or not yours")
    if row["user_id"] != user_id:
        raise LeaseDenied(f"lease {lease_id} not found or not yours")
    return row


def require_lease(
    conn: sqlite3.Connection,
    device_id: str,
    user_id: str,
    lease_id: int | None = None,
    now_fn: NowFn = utcnow,
) -> sqlite3.Row:
    """確認 ``user_id`` 現在持有 ``device_id`` 的有效 lease,回傳那筆 lease。

    ``lease_id`` 有給就一併核對,避免 agent 拿著舊 lease 的號碼、卻因為
    同一個 user 剛好有新 lease 而矇混過關——這種情況通常代表 agent 自己
    的狀態亂了,寧可報錯也不要默默放行。

    過期判定用 ``expires_at`` 而不是只看 ``status``:reaper 是週期性跑的,
    lease 過期到被標記成 expired 之間有一個 tick 的窗口,只看 status 會在
    那段時間放行已經過期的 lease。
    """
    row = conn.execute(
        "SELECT * FROM leases WHERE device_id = ? AND status = 'active'",
        (device_id,),
    ).fetchone()
    if row is None:
        raise LeaseDenied(
            f"no active lease on device {device_id!r}; reserve it first"
        )
    if row["user_id"] != user_id:
        # 不回報實際持有者是誰——那是別人的事,而且 user_id 目前不可信。
        raise LeaseDenied(
            f"device {device_id!r} is leased by someone else"
        )
    if lease_id is not None and row["id"] != lease_id:
        raise LeaseDenied(
            f"lease {lease_id} is not the active lease on device {device_id!r} "
            f"(active lease is {row['id']})"
        )
    if row["expires_at"] <= now_fn():
        # reaper 還沒掃到,但它已經過期了。
        raise LeaseDenied(
            f"lease {row['id']} on device {device_id!r} expired at {row['expires_at']}"
        )
    return row
