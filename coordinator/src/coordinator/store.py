"""Lease 生命週期與 exporter heartbeat 的核心邏輯(純 SQLite,不含 HTTP)。

所有狀態轉換都同步寫一筆 events(設計文件 §7/§8)。
「一台裝置同時最多一條 active lease」由 DB 的 partial UNIQUE index 鎖死,
這裡只負責把 UNIQUE 衝突轉成 Conflict。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Callable

from .db import utcnow


class NotFound(Exception):
    pass


class Conflict(Exception):
    pass


NowFn = Callable[[], str]

# Exporter **預設**掃得出來的 device class(USB/adb)。SSH 上的 SBC 不在其中
# ——它們缺席只代表這次回報涵蓋不到,不是拔線。
#
# 這只是預設值:exporter 回報時可以帶 discoverable_classes 明確指定這次
# 掃描涵蓋哪些 class。跑 LocalResourceScanner 的 host 會把 x86-cpu / gpu-cuda
# 也帶進來——本機 CPU/GPU 跟 USB 一樣「插在我身上的只有我用得到」,可達性
# 推論得出歸屬,所以它們可以參與缺席判定。**網路裝置永遠不行**(§5:
# Jupiter 可以同時被三台 host 摸到),所以 riscv-sbc 這種不會出現在任何
# scanner 的 classes 裡。
DISCOVERABLE_CLASSES = ("android", "bbb", "unknown")

VM_TEMPLATE_CLASS = "qemu-template"
VM_INSTANCE_CLASS = "qemu-vm"

# Ephemeral 的第二種 provisioner:Cuttlefish(§12「AVD 就是 Android 的
# ephemeral 池,真機 Pixel 只留 kernel/thermal/perf 工作」)。
#
# 為什麼是「多一組 class」而不是「多一套機制」:template → 實例的整條路
# ——lease 時 spawn、release 時銷毀、heartbeat 收斂、retired 保留稽核——
# 跟 provisioner 是什麼完全無關。差別只在 exporter 那端起的是 `qemu-system-*`
# 還是 `cvd start`,而那本來就是 exporter 的事。所以這裡只是把「哪個
# template class 生出哪個 instance class」變成一張表。
CVD_TEMPLATE_CLASS = "cuttlefish-template"
CVD_INSTANCE_CLASS = "cuttlefish-vm"

# template class → (instance class, devices.control)。
# control 記的是「怎麼操作這台」,對 ephemeral 來說就是誰把它生出來的。
INSTANCE_CLASSES: dict[str, tuple[str, str]] = {
    VM_TEMPLATE_CLASS: (VM_INSTANCE_CLASS, "qemu-spawn"),
    CVD_TEMPLATE_CLASS: (CVD_INSTANCE_CLASS, "cvd-spawn"),
}


def is_template(device_class: str) -> bool:
    return device_class in INSTANCE_CLASSES


# 每種 device class 在有 active lease 時該由 exporter 起哪些服務(§6 能力表)。
# Coordinator 是這件事的真相來源:它據此算出 desired state 回給 exporter。
# Phase 1 只有 adb 與 uart;SSH 上的 SBC、本機 CPU、GPU 沒有 exporter 代管
# 的服務,不列在這裡。
CLASS_SERVICES: dict[str, tuple[str, ...]] = {
    "android": ("adb",),
    "bbb": ("uart",),
    # Phase 3。ephemeral QEMU 的 VM 自己就有 -vnc 在聽;桌面跑得起來的
    # SBC 裝置端有 vncserver(§6 vnc 那列的三種來源)。
    "qemu-vm": ("vnc",),
    "riscv-sbc": ("vnc",),
    # Cuttlefish 實例對外就是一台 Android 裝置:它自己會把 adb 掛在
    # 127.0.0.1:<6520+n>,exporter 起一個 --one-device server 把它轉出來,
    # 跟真 Pixel 完全一樣的介面。**不走 vnc**——§6 的 vnc 那列明講
    # 「Cuttlefish 自帶 WebRTC 串流,不走這條」。
    "cuttlefish-vm": ("adb",),
}

# 有些能力**不用裝置本身的 identifier**:adb 要 USB serial,但 camera 要
# ``/dev/video0``、VNC 要上游的 ``host:port``。那些值放在 devices.tags 的
# 這些鍵裡,desired_services 據此覆寫要送給 exporter 的 identifier。
#
# 為什麼不另開一張表:一台裝置多一種能力就多一個識別碼,但這些值的性質
# 跟 tags 裡其他東西一樣——都是「這台裝置的靜態描述」,而且是 adopt 的時候
# 一起填的。真的長到需要獨立建模時再拆,現在拆只是多一次 join。
#
# **沒有對應的 tag 就不算 desired**:一台沒接 camera 的裝置不該被要求起
# video daemon。少了這條,exporter 會拿裝置的 adb serial 去開 camera,
# 每輪都失敗一次。
SERVICE_IDENTIFIER_TAGS: dict[str, str] = {
    "video": "video_device",     # '/dev/video0'
    "vnc": "vnc_upstream",       # '127.0.0.1:5900'(VM 或 SBC 上的 vncserver)
}

# 哪些能力必須走 API 而非 client 直連(§6 的 mediated 欄位)。
#
# 這是能力種類的**靜態性質**,不是某一次 lease 的動態值,所以由
# coordinator 說了算,**不接受 exporter 回報**——否則等於讓回報方自己宣告
# 「我不需要 mediation」,一個安全相關的事實就被交給了被管制的一方。
# Exporter 只負責報「我起好了,port 是多少」。
#
# Phase 1 沒有 flash,所以目前全是 False;Phase 2 加 flash 時在這裡加
# 一筆 True 即可,不用回頭改 exporter 或回報格式。
MEDIATED_SERVICES: frozenset[str] = frozenset({"flash"})


def is_mediated(service: str) -> bool:
    return service in MEDIATED_SERVICES


def _tags(raw: str | None) -> dict:
    """devices.tags 是自由格式 JSON。壞掉的值不該讓整輪 heartbeat 失敗。"""
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _iso_plus(now_iso: str, seconds: int) -> str:
    dt = datetime.fromisoformat(now_iso)
    return (dt + timedelta(seconds=seconds)).isoformat(timespec="seconds")


def _event(
    conn: sqlite3.Connection,
    device_id: str,
    kind: str,
    actor: str | None,
    now: str,
    lease_id: int | None = None,
    detail: dict | None = None,
) -> None:
    conn.execute(
        "INSERT INTO events (device_id, lease_id, kind, actor, detail, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (device_id, lease_id, kind, actor,
         json.dumps(detail, ensure_ascii=False) if detail else None, now),
    )


def _has_failed_flash(conn: sqlite3.Connection, device_id: str) -> bool:
    """這台裝置最近一次 flash 是失敗的、而且之後沒有成功刷回去嗎?

    刷壞的裝置**不能因為 lease 結束就回到 free**。失敗當下裝置通常還在
    lease 裡(持有者正要救它),所以 record_flash_results 那時候不能動它的
    狀態;真正的關卡在這裡——lease 結束、要把裝置交出去的那一刻。少了這
    條,一台刷到上不了 boot 的手機會被若無其事地發給下一個 agent。

    只看**最後一筆**已完成的 flash:失敗之後又成功刷了 known-good 回去
    (§6 的 restore),裝置就是好的,不該永遠被扣住。
    """
    row = conn.execute(
        "SELECT state FROM flash_jobs WHERE device_id = ? "
        "AND state IN ('done', 'failed') ORDER BY id DESC LIMIT 1",
        (device_id,),
    ).fetchone()
    return row is not None and row["state"] == "failed"


def _hand_back(conn: sqlite3.Connection, device_id: str, now: str,
               actor: str, lease_id: int | None = None) -> None:
    """lease 結束時把裝置交出去:正常回 free,刷壞的扣在 maintenance。

    ephemeral:這條 lease 生出來的 VM 一起標成該銷毀。「用完就沒了」是
    ephemeral 的全部意義,而且那些 VM 吃的是真的 RAM。
    """
    if lease_id is not None:
        for row in conn.execute(
            "SELECT id FROM vm_instances WHERE lease_id = ? "
            "AND state IN ('requested', 'running')",
            (lease_id,),
        ).fetchall():
            conn.execute(
                "UPDATE vm_instances SET state = 'stopping' WHERE id = ?",
                (row["id"],),
            )
            conn.execute(
                "UPDATE devices SET state = 'maintenance' WHERE id = ?", (row["id"],)
            )

    if _has_failed_flash(conn, device_id):
        conn.execute(
            "UPDATE devices SET state = 'maintenance' WHERE id = ? AND state = 'leased'",
            (device_id,),
        )
        _event(conn, device_id, "flash_result", actor, now, lease_id,
               {"outcome": "held in maintenance after a failed flash; "
                           "restore a known-good image before reuse"})
        return
    conn.execute(
        "UPDATE devices SET state = 'free' WHERE id = ? AND state = 'leased'",
        (device_id,),
    )


# ---------------------------------------------------------------- lease API

def reserve(
    conn: sqlite3.Connection,
    device_id: str,
    user_id: str,
    ttl_s: int,
    purpose: str | None = None,
    now_fn: NowFn = utcnow,
) -> sqlite3.Row:
    now = now_fn()
    dev = conn.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone()
    if dev is None:
        raise NotFound(f"device {device_id!r} not registered")
    if dev["state"] != "free":
        raise Conflict(f"device {device_id!r} is {dev['state']}, not free")
    try:
        cur = conn.execute(
            "INSERT INTO leases (device_id, user_id, purpose, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (device_id, user_id, purpose, now, _iso_plus(now, ttl_s)),
        )
    except sqlite3.IntegrityError as e:
        # partial UNIQUE index idx_leases_active:同裝置已有 active lease
        raise Conflict(f"device {device_id!r} already has an active lease") from e
    lease_id = cur.lastrowid
    conn.execute("UPDATE devices SET state = 'leased' WHERE id = ?", (device_id,))
    _event(conn, device_id, "reserve", user_id, now, lease_id,
           {"ttl_s": ttl_s, "purpose": purpose})
    conn.commit()

    # Ephemeral:借 template 等於「給我一台這種 VM」,實例現在才生出來
    # (§10 的裁決:沿用同一張 devices 表,lease 時 spawn)。實例的
    # spawn 由 exporter 收斂,這裡只建 row。
    if is_template(dev["class"]):
        spawn_instance(conn, device_id, lease_id, now_fn)

    return conn.execute("SELECT * FROM leases WHERE id = ?", (lease_id,)).fetchone()


def _active_lease(conn: sqlite3.Connection, lease_id: int) -> sqlite3.Row:
    lease = conn.execute("SELECT * FROM leases WHERE id = ?", (lease_id,)).fetchone()
    if lease is None:
        raise NotFound(f"lease {lease_id} not found")
    if lease["status"] != "active":
        raise Conflict(f"lease {lease_id} is {lease['status']}, not active")
    return lease


def renew(
    conn: sqlite3.Connection,
    lease_id: int,
    ttl_s: int,
    now_fn: NowFn = utcnow,
) -> sqlite3.Row:
    now = now_fn()
    lease = _active_lease(conn, lease_id)
    conn.execute(
        "UPDATE leases SET renewed_at = ?, expires_at = ? WHERE id = ?",
        (now, _iso_plus(now, ttl_s), lease_id),
    )
    _event(conn, lease["device_id"], "renew", lease["user_id"], now, lease_id,
           {"ttl_s": ttl_s})
    conn.commit()
    return conn.execute("SELECT * FROM leases WHERE id = ?", (lease_id,)).fetchone()


def release(
    conn: sqlite3.Connection,
    lease_id: int,
    now_fn: NowFn = utcnow,
) -> sqlite3.Row:
    now = now_fn()
    lease = _active_lease(conn, lease_id)
    conn.execute(
        "UPDATE leases SET status = 'released', released_at = ? WHERE id = ?",
        (now, lease_id),
    )
    _hand_back(conn, lease["device_id"], now, lease["user_id"], lease_id)
    _event(conn, lease["device_id"], "release", lease["user_id"], now, lease_id)
    conn.commit()
    return conn.execute("SELECT * FROM leases WHERE id = ?", (lease_id,)).fetchone()


# ------------------------------------------------------------------- 佇列
# 設計文件 §12:lease 仍是唯一的互斥原語,佇列只決定「下一個 lease 給誰」。
# coordinator/exporter 分工、reaper、events 全部不變。


def enqueue(
    conn: sqlite3.Connection,
    device_id: str,
    user_id: str,
    timeout_s: int,
    kind: str = "interactive",
    now_fn: NowFn = utcnow,
) -> sqlite3.Row:
    """排一個 job 等這台裝置。已經排過的回**同一張號碼牌**,不報錯。

    重複請求回同一張是刻意的:agent 重試(網路抖動、自己重啟)不該把佇列
    塞滿,也不該讓它在 FIFO 裡插到更前面或更後面。partial UNIQUE index
    負責擋,這裡把衝突轉成「查出既有的那張」。
    """
    now = now_fn()
    dev = conn.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone()
    if dev is None:
        raise NotFound(f"device {device_id!r} not registered")
    if dev["state"] == "unregistered":
        # §5:未登記裝置不可被 lease,那排隊等它也沒有意義。
        raise Conflict(f"device {device_id!r} is unregistered and cannot be leased")

    try:
        cur = conn.execute(
            "INSERT INTO jobs (device_id, user_id, kind, timeout_s, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (device_id, user_id, kind, timeout_s, now),
        )
    except sqlite3.IntegrityError:
        conn.rollback()
        existing = conn.execute(
            "SELECT * FROM jobs WHERE device_id = ? AND user_id = ? "
            "AND kind = ? AND state = 'queued'",
            (device_id, user_id, kind),
        ).fetchone()
        if existing is not None:
            return existing
        raise
    conn.commit()
    return conn.execute("SELECT * FROM jobs WHERE id = ?", (cur.lastrowid,)).fetchone()


def queue_position(conn: sqlite3.Connection, job_id: int) -> int | None:
    """這個 job 在它那台裝置的佇列裡排第幾(1 = 下一個)。非 queued 回 None。"""
    job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if job is None:
        raise NotFound(f"job {job_id} not found")
    if job["state"] != "queued":
        return None
    return conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE device_id = ? AND state = 'queued' "
        "AND (created_at < ? OR (created_at = ? AND id <= ?))",
        (job["device_id"], job["created_at"], job["created_at"], job_id),
    ).fetchone()[0]


def cancel_job(
    conn: sqlite3.Connection,
    job_id: int,
    user_id: str,
    now_fn: NowFn = utcnow,
) -> sqlite3.Row:
    """放棄排隊。**必須有**——放棄的 agent 卡著位置等於佇列漏水。

    只有 job 的主人能取消,理由跟 lease 的擁有權檢查一樣:job id 是連號
    整數,少了這層任何人都能把別人踢出佇列。
    """
    now = now_fn()
    job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if job is None or job["user_id"] != user_id:
        # 不區分不存在與不是你的,連號 id 才不能拿來探測。
        raise NotFound(f"job {job_id} not found or not yours")
    if job["state"] != "queued":
        raise Conflict(f"job {job_id} is {job['state']}, not queued")
    conn.execute(
        "UPDATE jobs SET state = 'cancelled', finished_at = ? WHERE id = ?",
        (now, job_id),
    )
    conn.commit()
    return conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()


def _next_queued(conn: sqlite3.Connection, device_id: str) -> sqlite3.Row | None:
    """佇列裡的下一個。

    **純 FIFO。** 設計文件 §12 說「人的 interactive 排在 agent batch 前面」,
    但那需要分得出人跟 agent——目前 user_id 是不可驗證的自我宣告,照著做
    會變成「宣稱自己是人就插隊」,製造說謊的誘因,同時給人一種系統很公平
    的錯覺。等身分可信之後再在這個函式裡加那條規則,呼叫端不用動。
    """
    return conn.execute(
        "SELECT * FROM jobs WHERE device_id = ? AND state = 'queued' "
        "ORDER BY created_at, id LIMIT 1",
        (device_id,),
    ).fetchone()


def _before_lease_handoff(conn: sqlite3.Connection, device_id: str) -> None:
    """兩個 job 之間的裝置清理點(§12 的 interstitial hooks)。

    現在是 no-op。Phase 4 要在這裡做:android 的 ``adb reboot`` 或解除
    安裝、benchmark job 前的 thermal cooldown(等 skin temp 降回門檻)、
    刷壞的裝置 restore to known-good。位置先留著,免得之後要改 scheduler
    的結構。
    """
    return None


def run_scheduler(conn: sqlite3.Connection, now_fn: NowFn = utcnow) -> list[int]:
    """裝置變 free 時,把它交給佇列裡的下一個。

    只碰 ``free`` 的裝置:``maintenance``(回收失敗、等人工)跟 ``offline``
    都不該把裝置交出去。
    """
    now = now_fn()
    started: list[int] = []
    free_devices = conn.execute(
        "SELECT DISTINCT j.device_id FROM jobs j JOIN devices d ON d.id = j.device_id "
        "WHERE j.state = 'queued' AND d.state = 'free'"
    ).fetchall()
    for row in free_devices:
        device_id = row["device_id"]
        job = _next_queued(conn, device_id)
        if job is None:
            continue
        _before_lease_handoff(conn, device_id)
        try:
            lease = reserve(
                conn, device_id, job["user_id"], job["timeout_s"],
                purpose=f"job {job['id']}", now_fn=now_fn,
            )
        except (Conflict, NotFound):
            continue          # 剛好被別人搶走或裝置沒了,下一輪再說
        conn.execute(
            "UPDATE jobs SET state = 'running', lease_id = ?, started_at = ? "
            "WHERE id = ?",
            (lease["id"], now, job["id"]),
        )
        started.append(job["id"])
    conn.commit()
    return started


def expire_stale_jobs(conn: sqlite3.Connection, now_fn: NowFn = utcnow) -> list[int]:
    """排太久的 queued job 放棄掉——死掉的 agent 不該永遠佔著位置。

    用 job 自己的 ``timeout_s`` 當上限:那是呼叫者說「我最多等這麼久」。
    """
    now = now_fn()
    rows = conn.execute("SELECT * FROM jobs WHERE state = 'queued'").fetchall()
    expired: list[int] = []
    for job in rows:
        if _iso_plus(job["created_at"], job["timeout_s"]) > now:
            continue
        conn.execute(
            "UPDATE jobs SET state = 'cancelled', finished_at = ?, result = ? "
            "WHERE id = ?",
            (now, json.dumps({"reason": "timed out waiting in queue"}), job["id"]),
        )
        expired.append(job["id"])
    conn.commit()
    return expired


# ------------------------------------------------------------------ reaper

def reap_expired_leases(conn: sqlite3.Connection, now_fn: NowFn = utcnow) -> list[int]:
    """過期但未 release 的 lease → 'expired',裝置放回 free。

    §7 第 4 步的「先叫 exporter 停服務」由 reconcile 自動達成:lease 一
    過期,desired 就不再包含它的服務,exporter 下一輪自己停掉。所以這裡
    不需要主動叫停。

    真正還缺的是「**裝置本身**卡住」——服務停了、lease 沒了,但裝置還是
    壞的,下一個 agent 會借到一台不能用的。那要靠 power_control,見
    ``request_reclaim``;正常過期不觸發,只有走 force_reclaim 路徑才會。
    """
    now = now_fn()
    rows = conn.execute(
        "SELECT * FROM leases WHERE status = 'active' AND expires_at < ?", (now,)
    ).fetchall()
    for lease in rows:
        conn.execute(
            "UPDATE leases SET status = 'expired' WHERE id = ?", (lease["id"],)
        )
        _hand_back(conn, lease["device_id"], now, "reaper", lease["id"])
        _event(conn, lease["device_id"], "expire", "reaper", now, lease["id"],
               {"expired_at": lease["expires_at"]})
    conn.commit()
    return [r["id"] for r in rows]


# ------------------------------------------------------------ force reclaim

class NoPowerControl(Exception):
    """這台裝置沒有可用的強制回收機制,只能標 offline 等人工介入(§7)。"""


def request_reclaim(
    conn: sqlite3.Connection,
    device_id: str,
    reason: str,
    lease_id: int | None = None,
    actor: str = "reaper",
    now_fn: NowFn = utcnow,
) -> sqlite3.Row | None:
    """排一個強制回收動作,交給裝置所在 host 的 exporter 執行。

    Coordinator 不碰硬體(§4),所以它只**記錄**「該對這台裝置做什麼」,
    實際的 adb reboot / IPMI power cycle 由 exporter 收斂時執行——沿用
    heartbeat 通道,不另外開推送。

    沒有 ``power_control`` 的裝置(設計文件明列 Jupiter 目前如此)無法
    強制回收:標成 ``maintenance`` 並拋 ``NoPowerControl``,讓人知道要
    人工介入,而不是假裝收乾淨了把它放回 free 給下一個 agent 踩。

    同一台裝置只會有一個未完成的動作(DB 的 partial UNIQUE 鎖死);已經
    有一個在跑時回 None,不重複下指令。
    """
    now = now_fn()
    dev = conn.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone()
    if dev is None:
        raise NotFound(f"device {device_id!r} not registered")

    if not dev["power_control"]:
        conn.execute(
            "UPDATE devices SET state = 'maintenance' WHERE id = ?", (device_id,)
        )
        _event(conn, device_id, "force_reclaim", actor, now, lease_id,
               {"reason": reason, "outcome": "no power_control; needs manual attention"})
        conn.commit()
        raise NoPowerControl(
            f"device {device_id!r} has no power_control; marked maintenance"
        )

    try:
        cur = conn.execute(
            "INSERT INTO reclaim_actions (device_id, lease_id, action, created_at) "
            "VALUES (?, ?, ?, ?)",
            (device_id, lease_id, dev["power_control"], now),
        )
    except sqlite3.IntegrityError:
        conn.rollback()
        return None          # 已經有一個未完成的動作,不重複下

    _event(conn, device_id, "force_reclaim", actor, now, lease_id,
           {"reason": reason, "action": dev["power_control"],
            "reclaim_id": cur.lastrowid})
    conn.commit()
    return conn.execute(
        "SELECT * FROM reclaim_actions WHERE id = ?", (cur.lastrowid,)
    ).fetchone()


def force_reclaim_lease(
    conn: sqlite3.Connection,
    lease_id: int,
    reason: str,
    actor: str = "reaper",
    now_fn: NowFn = utcnow,
) -> sqlite3.Row:
    """強制收回一條 lease:標 force_reclaimed,並排一個裝置回收動作。

    跟正常過期的差別在**裝置狀態**:過期只是「沒人租了」,強制回收是
    「這台可能是壞的」。所以裝置先進 ``maintenance``,等回收動作成功
    才放回 free——中間不讓任何人借到它。
    """
    now = now_fn()
    lease = conn.execute("SELECT * FROM leases WHERE id = ?", (lease_id,)).fetchone()
    if lease is None:
        raise NotFound(f"lease {lease_id} not found")

    conn.execute(
        "UPDATE leases SET status = 'force_reclaimed', released_at = ? WHERE id = ?",
        (now, lease_id),
    )
    # 先扣住裝置:回收動作跑完之前不能被借走。
    conn.execute(
        "UPDATE devices SET state = 'maintenance' WHERE id = ?", (lease["device_id"],)
    )
    conn.commit()
    try:
        request_reclaim(conn, lease["device_id"], reason, lease_id, actor, now_fn)
    except NoPowerControl:
        pass                 # 已經標 maintenance 並寫了 event,等人工處理
    return conn.execute("SELECT * FROM leases WHERE id = ?", (lease_id,)).fetchone()


def escalate_stuck_devices(
    conn: sqlite3.Connection,
    reclaim_after_s: int,
    now_fn: NowFn = utcnow,
) -> list[str]:
    """lease 已經過期夠久、服務卻還沒停掉的裝置 → 升級成強制回收。

    §7 的順序是「先叫 exporter 停服務,逾時仍未回應才動用 power_control」。
    在 reconcile 模型下前半自動達成:lease 一過期 desired 就縮小,exporter
    下一輪自己停。所以這裡判斷的是**後半**——過了寬限期 device_services
    還在,代表收斂沒成功(exporter 掛了、或裝置卡到 daemon 停不掉),
    這才是需要動用 power_control 的情況。

    正常過期不會走到這裡:exporter 一收斂,endpoint 就被清掉了。
    """
    now = now_fn()
    cutoff = _iso_plus(now, -reclaim_after_s)
    # 寬限期從「reaper 標記過期的那一刻」算起,不是從 expires_at——
    # lease 可能是被手動改成很久以前才過期的,或 coordinator 停機一段時間
    # 後才重新掃到;用 expires_at 的話那些會立刻升級,根本沒給 exporter
    # 收斂的機會。expire event 的 created_at 就是「我們發現它過期」的時間。
    rows = conn.execute(
        "SELECT DISTINCT l.id AS lease_id, l.device_id "
        "FROM leases l "
        "JOIN device_services ds ON ds.device_id = l.device_id "
        "JOIN events e ON e.lease_id = l.id AND e.kind = 'expire' "
        "WHERE l.status = 'expired' AND e.created_at < ? "
        "AND NOT EXISTS (SELECT 1 FROM reclaim_actions r "
        "                WHERE r.device_id = l.device_id "
        "                AND r.state IN ('pending','running'))",
        (cutoff,),
    ).fetchall()
    escalated: list[str] = []
    for row in rows:
        try:
            action = request_reclaim(
                conn, row["device_id"],
                reason="services still running after lease expiry",
                lease_id=row["lease_id"], now_fn=now_fn,
            )
        except NoPowerControl:
            escalated.append(row["device_id"])   # 已標 maintenance,等人工
            continue
        if action is not None:
            conn.execute(
                "UPDATE devices SET state = 'maintenance' WHERE id = ? "
                "AND state != 'leased'",
                (row["device_id"],),
            )
            escalated.append(row["device_id"])
    conn.commit()
    return escalated


def pending_reclaims(conn: sqlite3.Connection, host_id: str) -> list[dict]:
    """這台 host 上待執行的回收動作——隨 heartbeat 回應交給 exporter。"""
    # 欄位名用 reclaim_id 而不是 id:exporter 回報結果時用同一個名字,
    # 兩邊不一致的話會變成「送出去的鍵跟收回來的鍵不同」,很容易寫錯。
    rows = conn.execute(
        "SELECT r.id AS reclaim_id, r.device_id, r.action, d.identifier "
        "FROM reclaim_actions r JOIN devices d ON d.id = r.device_id "
        "WHERE d.host = ? AND r.state IN ('pending', 'running') ORDER BY r.id",
        (host_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def record_reclaim_results(
    conn: sqlite3.Connection,
    results: list[dict],
    now: str,
) -> list[int]:
    """收下 exporter 回報的回收結果。

    成功的話裝置放回 free(它剛被重開,是乾淨的);失敗就留在
    ``maintenance``——寧可少一台可用裝置,也不要把壞的交給下一個 agent。
    """
    done: list[int] = []
    for res in results:
        rid = res.get("reclaim_id")
        row = conn.execute(
            "SELECT * FROM reclaim_actions WHERE id = ?", (rid,)
        ).fetchone()
        if row is None or row["state"] in ("done", "failed"):
            continue
        ok = bool(res.get("ok"))
        conn.execute(
            "UPDATE reclaim_actions SET state = ?, finished_at = ?, detail = ?, "
            "attempts = attempts + 1 WHERE id = ?",
            ("done" if ok else "failed", now,
             json.dumps(res.get("detail"), ensure_ascii=False) if res.get("detail")
             else None,
             rid),
        )
        if ok:
            conn.execute(
                "UPDATE devices SET state = 'free' WHERE id = ? AND state = 'maintenance'",
                (row["device_id"],),
            )
        _event(conn, row["device_id"], "force_reclaim", "exporter", now,
               row["lease_id"],
               {"reclaim_id": rid, "action": row["action"],
                "ok": ok, "detail": res.get("detail")})
        done.append(rid)
    return done


def reap_stale(
    conn: sqlite3.Connection,
    offline_after_s: int,
    now_fn: NowFn = utcnow,
) -> list[str]:
    """last_seen_at 過舊的裝置標 offline(從未回報過的 NULL 不算)。"""
    now = now_fn()
    cutoff = _iso_plus(now, -offline_after_s)
    rows = conn.execute(
        "SELECT * FROM devices WHERE last_seen_at IS NOT NULL "
        "AND last_seen_at < ? AND state NOT IN ('offline', 'maintenance')",
        (cutoff,),
    ).fetchall()
    for dev in rows:
        conn.execute("UPDATE devices SET state = 'offline' WHERE id = ?", (dev["id"],))
        _event(conn, dev["id"], "device_detached", "reaper", now,
               detail={"reason": "stale last_seen_at", "last_seen_at": dev["last_seen_at"]})
    conn.commit()
    return [r["id"] for r in rows]


# --------------------------------------------------------------- heartbeat

def _service_identifier(dev: sqlite3.Row, service: str, tags: dict) -> str | None:
    """這個能力該拿哪個識別碼去起服務。None = 現在還起不了,別送出去。

    三種來源,由具體到一般:

    1. **ephemeral 實例回報的實際位址**。Cuttlefish 的 adb 掛在
       ``127.0.0.1:<6520+n>``,而 n 要等 exporter 真的把它生出來才知道。
       這跟 endpoint 是同一種最終一致:實例還沒 running 就沒有位址,那是
       pending 不是錯誤。
    2. **devices.tags 裡的能力專屬識別碼**(camera 的 /dev/video0、VNC 的
       上游 host:port)。
    3. 裝置自己的 identifier(adb serial、序列埠路徑)。
    """
    # 1. ephemeral 實例:位址由 exporter 回報,不是預先知道的。
    detail = _tags(dev["instance_detail"]) if "instance_detail" in dev.keys() else {}
    if detail.get(f"{service}_identifier"):
        return detail[f"{service}_identifier"]
    if ("instance_state" in dev.keys() and dev["instance_state"] is not None
            and service in _INSTANCE_REPORTED_SERVICES.get(dev["class"], ())):
        # 是 ephemeral 實例、而且這個能力的位址本來就該由它回報,但還沒報
        # 上來。送出去只會讓 exporter 拿實例 id 當 adb serial 去連,每輪
        # 失敗一次。
        return None

    # 2. 能力專屬的 tag。
    tag = SERVICE_IDENTIFIER_TAGS.get(service)
    if tag is not None:
        # 這台裝置沒有這個能力所需的識別碼(沒接 camera、沒有上游 VNC)。
        return tags.get(tag) or None

    # 3. 裝置本身的識別碼。
    return dev["identifier"]


# 哪些 (instance class, service) 的識別碼一定要等實例回報。
_INSTANCE_REPORTED_SERVICES: dict[str, tuple[str, ...]] = {
    CVD_INSTANCE_CLASS: ("adb",),
}


def desired_services(conn: sqlite3.Connection, host_id: str) -> list[dict]:
    """這台 host 上「現在應該要跑」的 per-resource daemon。

    定義是:裝置目前觀測到在這台 host 上 + 有一條 active lease +
    它的 class 在 CLASS_SERVICES 裡。Exporter 拿這份跟自己實際跑著的
    比對後自行收斂(reconcile),coordinator 不主動推送——heartbeat 就是
    control-plane channel(§4),exporter 不需要開 listening port,
    coordinator 重啟後也靠下一輪 heartbeat 自動收斂。
    """
    # ephemeral 實例的 lease **掛在 template 上**,不在實例自己身上——
    # 借的人借的是「給我一台這種 VM」,實例是那條 lease 的產物。所以這裡
    # 要兩種連法都涵蓋:裝置自己有 active lease(一般裝置、以及 template),
    # 或它是某條 active lease 生出來的實例(vm_instances.lease_id)。
    #
    # 少了後者,Cuttlefish 實例的 adb 服務永遠不會被 desired 出來:實例
    # 本身沒有 lease,join 直接把它濾掉。QEMU 池沒踩到是因為 vnc 的識別碼
    # 來自 template 的 tags,不需要實例入列。
    rows = conn.execute(
        "SELECT d.id, d.class, d.identifier, d.tags, "
        "       COALESCE(l.id, vl.id) AS lease_id, "
        "       v.detail AS instance_detail, v.state AS instance_state "
        "FROM devices d "
        "LEFT JOIN leases l ON l.device_id = d.id AND l.status = 'active' "
        "LEFT JOIN vm_instances v ON v.id = d.id "
        "LEFT JOIN leases vl ON vl.id = v.lease_id AND vl.status = 'active' "
        "WHERE d.host = ? AND COALESCE(l.id, vl.id) IS NOT NULL "
        "ORDER BY d.id",
        (host_id,),
    ).fetchall()
    desired = []
    for dev in rows:
        tags = _tags(dev["tags"])
        for service in CLASS_SERVICES.get(dev["class"], ()):
            identifier = _service_identifier(dev, service, tags)
            if identifier is None:
                continue
            desired.append({
                "device_id": dev["id"],
                "service": service,
                "identifier": identifier,
                "lease_id": dev["lease_id"],
            })
    return desired


def record_services(
    conn: sqlite3.Connection,
    host_id: str,
    services: list[dict],
    now: str,
) -> dict:
    """把 exporter 回報的實際 endpoint 寫進 device_services。

    只收這台 host 上、而且真的有 active lease 的裝置——exporter 回報的
    東西不能無條件當真,它可能還在收斂上一輪的狀態(lease 剛沒了但
    daemon 還沒停)。同時把該停卻還在回報的清掉,讓 device_services
    始終等於「現在真的連得上的 endpoint」。

    endpoint 存 ``<tailscale IP>:<port>``——client 要能直接貼著用
    (`adb connect`),所以用 hosts.address 而不是 host id。
    """
    host = conn.execute("SELECT * FROM hosts WHERE id = ?", (host_id,)).fetchone()
    address = host["address"] if host else host_id

    valid = {
        (d["device_id"], d["service"]): d
        for d in desired_services(conn, host_id)
    }
    recorded: list[str] = []
    for svc in services:
        key = (svc.get("device_id"), svc.get("service"))
        if key not in valid:
            continue          # 不該跑的東西回報上來:忽略,等 exporter 自己收斂
        port = svc.get("port")
        endpoint = svc.get("endpoint") or (f"{address}:{port}" if port else None)
        if not endpoint:
            continue
        existing = conn.execute(
            "SELECT endpoint FROM device_services WHERE device_id = ? AND service = ?",
            key,
        ).fetchone()
        # mediated 由 coordinator 依 service 種類決定,不看 exporter 回報什麼。
        conn.execute(
            "INSERT INTO device_services (device_id, service, endpoint, mediated) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT (device_id, service) DO UPDATE SET "
            "endpoint = excluded.endpoint, mediated = excluded.mediated",
            (*key, endpoint, int(is_mediated(key[1]))),
        )
        if existing is None or existing["endpoint"] != endpoint:
            _event(conn, key[0], "service_start", "exporter", now,
                   lease_id=valid[key]["lease_id"],
                   detail={"service": key[1], "endpoint": endpoint})
        recorded.append(f"{key[0]}/{key[1]}")

    # 這台 host 上已經不該存在的 endpoint:lease 結束了、或 exporter 不再
    # 回報它。endpoint 是 lease 期間才有效的動態值,留著會讓 client 連到
    # 一個已經停掉的 port。
    reported = {(s.get("device_id"), s.get("service")) for s in services}
    stale = conn.execute(
        "SELECT ds.device_id, ds.service FROM device_services ds "
        "JOIN devices d ON d.id = ds.device_id WHERE d.host = ?",
        (host_id,),
    ).fetchall()
    dropped: list[str] = []
    for row in stale:
        key = (row["device_id"], row["service"])
        if key in valid and key in reported:
            continue
        conn.execute(
            "DELETE FROM device_services WHERE device_id = ? AND service = ?", key
        )
        _event(conn, key[0], "service_stop", "exporter", now,
               detail={"service": key[1],
                       "reason": "lease ended" if key not in valid else "not reported"})
        dropped.append(f"{key[0]}/{key[1]}")

    return {"recorded": sorted(recorded), "dropped": sorted(dropped)}


class HeartbeatProcessor:
    """Exporter inventory 回報的 diff 邏輯(§8)。

    搬機收斂:裝置從 host A 消失時不立即寫 device_detached,先進
    in-memory 的 grace window(pending_detach);若下一輪回報它出現在
    host B,收斂成單筆 device_moved。超過 grace 仍沒出現才 finalize 成
    device_detached + offline。pending 狀態不落 DB——coordinator 重啟
    後最壞情況是多等一輪 heartbeat 才判 detach,可接受。
    """

    def __init__(self, grace_s: int = 30, now_fn: NowFn = utcnow):
        self.grace_s = grace_s
        self.now_fn = now_fn
        self.pending_detach: dict[str, str] = {}  # device_id -> missing since (ISO)

    def process(
        self,
        conn: sqlite3.Connection,
        host_id: str,
        identifiers: list[str],
        discoverable_classes: list[str] | None = None,
        services: list[dict] | None = None,
        seen: list[str] | None = None,
        reclaims: list[dict] | None = None,
        flashes: list[dict] | None = None,
        instances: list[dict] | None = None,
    ) -> dict:
        """處理一次 exporter 回報。

        ``discoverable_classes`` 限定這次回報涵蓋哪些 device class,只有這些
        class 的裝置缺席才算「不見了」。exporter 掃 USB/adb 列不出 SSH 上的
        SBC 或本機 CPU,沒有這層限定就會把它們全誤判成拔線。省略時預設涵蓋
        USB 可發現的 class。
        """
        now = self.now_fn()
        host = conn.execute("SELECT * FROM hosts WHERE id = ?", (host_id,)).fetchone()
        if host is None:
            raise NotFound(f"host {host_id!r} not registered")
        conn.execute("UPDATE hosts SET last_seen_at = ? WHERE id = ?", (now, host_id))

        moved: list[str] = []
        attached: list[str] = []
        unregistered: list[str] = []

        # `seen`:只證明裝置活著,**不宣稱擁有**(§5)。tailnet-native 裝置
        # (Jupiter)走這條——它沒有任何 exporter 代理,沒人回報的話
        # last_seen_at 永遠是 NULL,§8 的 reaper 偵測不到它離線。
        #
        # 跟 identifiers 的三個差別,每一個都是 §5 直接要求的:不建
        # unregistered row(tailnet 上二十幾個節點會憑空變成裝置)、不改
        # devices.host(可達性不能決定擁有權,同一台裝置多個 host 都看得到)、
        # 不參與缺席判定(掃描結果因觀察點而異,不能當離線證據)。
        refreshed: list[str] = []
        for ident in seen or []:
            dev = conn.execute(
                "SELECT id, state FROM devices WHERE identifier = ?", (ident,)
            ).fetchone()
            if dev is None:
                continue                 # 沒登記過的 tailnet 節點:不是裝置
            conn.execute(
                "UPDATE devices SET last_seen_at = ? WHERE id = ?", (now, dev["id"])
            )
            if dev["state"] == "offline":
                # 它回來了。offline 是 reaper 依 last_seen_at 標的,既然
                # 又看得到它,就放回 free——跟 USB 裝置重新插上同樣的處理。
                conn.execute(
                    "UPDATE devices SET state = 'free' WHERE id = ?", (dev["id"],)
                )
                _event(conn, dev["id"], "device_attached", "exporter", now,
                       detail={"via": "tailnet", "reporter": host_id})
            refreshed.append(dev["id"])

        for ident in identifiers:
            dev = conn.execute(
                "SELECT * FROM devices WHERE identifier = ?", (ident,)
            ).fetchone()
            if dev is None:
                # 首次出現:直接建 row(4bfae39 裁決),id 暫用識別碼本身,
                # adopt 時再補 metadata、改 id 與 state。
                conn.execute(
                    "INSERT INTO devices (id, class, control, identifier, provisioning, "
                    "host, state, last_seen_at) "
                    "VALUES (?, 'unknown', 'unknown', ?, 'static', ?, 'unregistered', ?)",
                    (ident, ident, host_id, now),
                )
                _event(conn, ident, "device_attached", "exporter", now,
                       detail={"host": host_id, "unregistered": True})
                unregistered.append(ident)
            elif dev["host"] != host_id:
                conn.execute(
                    "UPDATE devices SET host = ?, last_seen_at = ? WHERE id = ?",
                    (host_id, now, dev["id"]),
                )
                _event(conn, dev["id"], "device_moved", "exporter", now,
                       detail={"from": dev["host"], "to": host_id})
                self.pending_detach.pop(dev["id"], None)
                moved.append(dev["id"])
            else:
                conn.execute(
                    "UPDATE devices SET last_seen_at = ? WHERE id = ?", (now, dev["id"])
                )
                self.pending_detach.pop(dev["id"], None)
                if dev["state"] == "offline":
                    conn.execute(
                        "UPDATE devices SET state = 'free' WHERE id = ?", (dev["id"],)
                    )
                    _event(conn, dev["id"], "device_attached", "exporter", now,
                           detail={"host": host_id})
                    attached.append(dev["id"])

        # 這台 host 上該看得到卻沒回報的裝置 → 進 grace window。
        # 只看這次回報涵蓋得到的 class,否則 SSH/本機類裝置會被誤判拔線。
        classes = (
            DISCOVERABLE_CLASSES if discoverable_classes is None
            else discoverable_classes
        )
        placeholders = ",".join("?" * len(classes)) or "NULL"
        expected = conn.execute(
            f"SELECT * FROM devices WHERE host = ? AND state != 'offline' "
            f"AND class IN ({placeholders})",
            (host_id, *classes),
        ).fetchall()
        visible = set(identifiers)
        for dev in expected:
            if dev["identifier"] not in visible and dev["id"] not in self.pending_detach:
                self.pending_detach[dev["id"]] = now

        detached = self.finalize_detaches(conn)

        # Reconcile:先收下 exporter 回報的實際 endpoint,再算出它接下來
        # 該收斂到的狀態。順序要緊——先記錄再算,回傳的 desired 才反映
        # 這一輪 inventory 之後的最新情況(例如裝置剛搬過來)。
        recorded = record_services(conn, host_id, services or [], now)
        record_reclaim_results(conn, reclaims or [], now)
        record_flash_results(conn, flashes or [], now)
        record_instance_results(conn, instances or [], now)
        conn.commit()
        return {
            "moved": moved,
            "attached": attached,
            "unregistered": unregistered,
            "detached": detached,
            "refreshed": sorted(refreshed),
            "desired": desired_services(conn, host_id),
            "reclaims": pending_reclaims(conn, host_id),
            "flashes": pending_flashes(conn, host_id),
            "instances": desired_instances(conn, host_id),
            **recorded,
        }

    def finalize_detaches(self, conn: sqlite3.Connection) -> list[str]:
        """grace window 到期仍未在任何 host 出現的裝置 → offline + detached。

        由 process() 尾端與 reaper tick 各自呼叫;呼叫端負責 commit。
        """
        now = self.now_fn()
        done: list[str] = []
        for device_id, since in list(self.pending_detach.items()):
            if _iso_plus(since, self.grace_s) > now:
                continue
            dev = conn.execute(
                "SELECT * FROM devices WHERE id = ?", (device_id,)
            ).fetchone()
            del self.pending_detach[device_id]
            if dev is None or dev["state"] == "offline":
                continue
            conn.execute(
                "UPDATE devices SET state = 'offline' WHERE id = ?", (device_id,)
            )
            _event(conn, device_id, "device_detached", "exporter", now,
                   detail={"host": dev["host"], "missing_since": since})
            done.append(device_id)
        return done


# ------------------------------------------------------- image registry(§6)
# Flash 是能力表裡唯一 mediated 的操作,也是唯一破壞性的。兩個約束跟著它:
# 只刷登記過的 image(否則「刷了什麼進去」事後查不出來),以及每台裝置的
# known-good 集合就是還原目標(§6「Image registry 與 known-good restore」)。


class ImageError(Exception):
    """Image 登記不合法——sha256 形式不對、裝置不存在。"""


def register_image(
    conn: sqlite3.Connection,
    image_id: str,
    device_id: str,
    kind: str,
    uri: str,
    sha256: str,
    known_good: bool = False,
    note: str | None = None,
    now_fn: NowFn = utcnow,
) -> sqlite3.Row:
    """登記一份可以刷的 image。

    ``sha256`` 在這裡就驗形式(64 個 hex),不是等 exporter 拿到才發現——
    寫錯的雜湊值一路傳到 exporter 才炸,只是把同一個錯誤延後到裝置已經
    進 fastboot mode 之後才發生。

    ``known_good`` 是每台裝置每種分割區唯一的還原目標,所以登記新的會把
    同一 (device, kind) 的舊 known-good 降級。讓呼叫者自己記得清掉舊的
    等於把不變量交給呼叫端維護,遲早會出現兩個 known-good、restore 不知道
    該選哪個。
    """
    now = now_fn()
    if conn.execute(
        "SELECT 1 FROM devices WHERE id = ?", (device_id,)
    ).fetchone() is None:
        raise NotFound(f"device {device_id!r} not registered")
    digest = sha256.strip().lower()
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise ImageError(
            f"sha256 must be 64 hex characters, got {sha256!r}"
        )

    if known_good:
        conn.execute(
            "UPDATE images SET known_good = FALSE WHERE device_id = ? AND kind = ?",
            (device_id, kind),
        )
    conn.execute(
        "INSERT INTO images (id, device_id, kind, uri, sha256, known_good, note, "
        "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (id) DO UPDATE SET device_id = excluded.device_id, "
        "kind = excluded.kind, uri = excluded.uri, sha256 = excluded.sha256, "
        "known_good = excluded.known_good, note = excluded.note",
        (image_id, device_id, kind, uri, digest, int(known_good), note, now),
    )
    conn.commit()
    return conn.execute("SELECT * FROM images WHERE id = ?", (image_id,)).fetchone()


def list_images(
    conn: sqlite3.Connection, device_id: str | None = None
) -> list[sqlite3.Row]:
    if device_id is None:
        return conn.execute("SELECT * FROM images ORDER BY device_id, id").fetchall()
    return conn.execute(
        "SELECT * FROM images WHERE device_id = ? ORDER BY id", (device_id,)
    ).fetchall()


def known_good_images(conn: sqlite3.Connection, device_id: str) -> list[sqlite3.Row]:
    """這台裝置的還原目標——每種分割區各一份(§6)。"""
    return conn.execute(
        "SELECT * FROM images WHERE device_id = ? AND known_good = TRUE ORDER BY kind",
        (device_id,),
    ).fetchall()


# -------------------------------------------------------- mediated flash(§6)


def request_flash(
    conn: sqlite3.Connection,
    device_id: str,
    image_id: str,
    user_id: str,
    lease_id: int | None = None,
    now_fn: NowFn = utcnow,
) -> sqlite3.Row:
    """排一個 flash 動作,交給裝置所在 host 的 exporter 執行。

    Coordinator 不碰硬體(§4),所以跟 reclaim 一樣只**記錄**「該對這台
    裝置刷什麼」,實際的 fastboot/dd 由 exporter 隨 heartbeat 領走執行。

    呼叫端負責授權(authz.require_lease);這裡負責的是資料完整性:

    - image 必須登記過,而且**必須屬於這台裝置**。跨裝置刷 image 是把
      Pixel 的 boot.img 刷進別的板子那種等級的錯誤,不能只靠呼叫者自己
      看清楚。
    - 同一台裝置同時只有一個未完成的 flash(partial UNIQUE 鎖死)。並行
      刷同一台裝置是直接把它變磚。
    - 裝置正在被強制回收(要重開了)時不排 flash:重開到一半開始刷,或
      刷到一半被重開,都是製造磚的可靠方法。
    """
    now = now_fn()
    dev = conn.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone()
    if dev is None:
        raise NotFound(f"device {device_id!r} not registered")
    image = conn.execute("SELECT * FROM images WHERE id = ?", (image_id,)).fetchone()
    if image is None:
        raise NotFound(f"image {image_id!r} not registered")
    if image["device_id"] != device_id:
        # 訊息講清楚它屬於誰:這不是安全邊界(呼叫者已經持有 lease),
        # 是防手滑,講清楚才修得掉。
        raise Conflict(
            f"image {image_id!r} belongs to device {image['device_id']!r}, "
            f"not {device_id!r}"
        )
    if conn.execute(
        "SELECT 1 FROM reclaim_actions WHERE device_id = ? "
        "AND state IN ('pending', 'running')",
        (device_id,),
    ).fetchone() is not None:
        raise Conflict(
            f"device {device_id!r} has a reclaim in flight; cannot flash until it "
            "finishes"
        )

    try:
        cur = conn.execute(
            "INSERT INTO flash_jobs (device_id, image_id, lease_id, user_id, "
            "created_at) VALUES (?, ?, ?, ?, ?)",
            (device_id, image_id, lease_id, user_id, now),
        )
    except sqlite3.IntegrityError as e:
        conn.rollback()
        raise Conflict(
            f"device {device_id!r} already has a flash in flight"
        ) from e

    _event(conn, device_id, "flash_start", user_id, now, lease_id,
           {"flash_id": cur.lastrowid, "image_id": image_id,
            "kind": image["kind"], "sha256": image["sha256"]})
    conn.commit()
    return conn.execute(
        "SELECT * FROM flash_jobs WHERE id = ?", (cur.lastrowid,)
    ).fetchone()


def pending_flashes(conn: sqlite3.Connection, host_id: str) -> list[dict]:
    """這台 host 上待執行的 flash——隨 heartbeat 回應交給 exporter。

    帶上 image 的 uri 與 sha256:exporter 刷之前要自己驗一次雜湊。
    Coordinator 說「刷這個」,exporter 驗「我手上這份真的是它」——不然
    registry 只是記帳,擋不住檔案在 exporter 本地被換掉。
    """
    rows = conn.execute(
        "SELECT f.id AS flash_id, f.device_id, d.identifier, d.class, "
        "i.id AS image_id, i.kind, i.uri, i.sha256 "
        "FROM flash_jobs f JOIN devices d ON d.id = f.device_id "
        "JOIN images i ON i.id = f.image_id "
        "WHERE d.host = ? AND f.state IN ('pending', 'running') ORDER BY f.id",
        (host_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def record_flash_results(
    conn: sqlite3.Connection,
    results: list[dict],
    now: str,
) -> list[int]:
    """收下 exporter 回報的 flash 結果。

    刷壞的裝置**不自動放回 free**:標 maintenance 等人工或 restore。刷
    失敗的裝置很可能連 boot 都上不去,交給下一個 agent 只是讓它也踩一次。
    成功的話裝置狀態不動——它還在 lease 裡,持有者要繼續用。
    """
    done: list[int] = []
    for res in results:
        fid = res.get("flash_id")
        row = conn.execute(
            "SELECT * FROM flash_jobs WHERE id = ?", (fid,)
        ).fetchone()
        if row is None or row["state"] in ("done", "failed"):
            continue
        ok = bool(res.get("ok"))
        conn.execute(
            "UPDATE flash_jobs SET state = ?, finished_at = ?, detail = ?, "
            "attempts = attempts + 1 WHERE id = ?",
            ("done" if ok else "failed", now,
             json.dumps(res.get("detail"), ensure_ascii=False)
             if res.get("detail") else None,
             fid),
        )
        if not ok:
            # 刷壞的裝置扣住。lease 還在的話持有者仍看得到它,但 lease
            # 結束後不會直接回到 free 給下一個人。
            conn.execute(
                "UPDATE devices SET state = 'maintenance' WHERE id = ? "
                "AND state != 'leased'",
                (row["device_id"],),
            )
        _event(conn, row["device_id"], "flash_result", "exporter", now,
               row["lease_id"],
               {"flash_id": fid, "image_id": row["image_id"], "ok": ok,
                "detail": res.get("detail")})
        done.append(fid)
    return done


# ------------------------------------------------ ephemeral VM 池(§10/§12)
# §10 的開放問題「ephemeral 裝置是否該拆成 device_templates」在這裡選了
# 「沿用同一張 devices 表」那條:template 是一筆 row,lease 時 spawn 出來
# 的實例是另一筆 row。lease/events/device_services 全部已經以 device_id
# 為軸,實例不進表的話這三套機制都要長出「如果是 VM 的話……」的分支。

def _instance_id(template_id: str, seq: int) -> str:
    return f"{template_id}-{seq:04d}"


def spawn_instance(
    conn: sqlite3.Connection,
    template_id: str,
    lease_id: int | None = None,
    now_fn: NowFn = utcnow,
) -> sqlite3.Row:
    """從 template 生一台 VM 實例,等 exporter 把它真的跑起來。

    Coordinator 不碰硬體(§4),VM 也一樣:這裡只建 row,實際的 qemu
    行程由 template 所在 host 的 exporter 隨 heartbeat 領走 spawn。

    實例的 ``identifier`` 用實例 id 本身:VM 沒有 USB serial 之類的天然
    穩定識別碼,而且它本來就短命——身分就是「這一次生出來的這台」。
    重要的是它**不參與 USB inventory 的缺席判定**(qemu-vm 不在
    DISCOVERABLE_CLASSES 裡),否則每輪掃描都會把它判成拔線。
    """
    now = now_fn()
    tpl = conn.execute(
        "SELECT * FROM devices WHERE id = ?", (template_id,)
    ).fetchone()
    if tpl is None:
        raise NotFound(f"template {template_id!r} not registered")
    if not is_template(tpl["class"]):
        raise Conflict(
            f"device {template_id!r} is {tpl['class']!r}, not a template "
            f"({', '.join(sorted(INSTANCE_CLASSES))})"
        )
    instance_class, control = INSTANCE_CLASSES[tpl["class"]]
    if tpl["host"] is None:
        raise Conflict(f"template {template_id!r} has no host to spawn on")

    seq = conn.execute(
        "SELECT COUNT(*) FROM vm_instances WHERE template_id = ?", (template_id,)
    ).fetchone()[0] + 1
    instance_id = _instance_id(template_id, seq)

    conn.execute(
        "INSERT INTO devices (id, class, control, identifier, provisioning, host, "
        "tags, state, last_seen_at) "
        "VALUES (?, ?, ?, ?, 'ephemeral', ?, ?, 'leased', ?)",
        (instance_id, instance_class, control, instance_id, tpl["host"],
         tpl["tags"], now),
    )
    conn.execute(
        "INSERT INTO vm_instances (id, template_id, host, lease_id, spec, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (instance_id, template_id, tpl["host"], lease_id, tpl["tags"], now),
    )
    _event(conn, instance_id, "device_attached", "coordinator", now, lease_id,
           {"template": template_id, "ephemeral": True, "class": instance_class})
    conn.commit()
    return conn.execute(
        "SELECT * FROM vm_instances WHERE id = ?", (instance_id,)
    ).fetchone()


def destroy_instance(
    conn: sqlite3.Connection,
    instance_id: str,
    now_fn: NowFn = utcnow,
) -> None:
    """標記一台實例該被銷毀。Exporter 下一輪停掉 qemu 之後才真的消失。

    §12:「qemu ephemeral:直接銷毀重生,天然乾淨」——所以不做任何
    interstitial 清理,整台丟掉就是最徹底的重置。
    """
    now = now_fn()
    row = conn.execute(
        "SELECT * FROM vm_instances WHERE id = ?", (instance_id,)
    ).fetchone()
    if row is None:
        raise NotFound(f"vm instance {instance_id!r} not found")
    if row["state"] == "gone":
        return
    conn.execute(
        "UPDATE vm_instances SET state = 'stopping' WHERE id = ?", (instance_id,)
    )
    conn.execute(
        "UPDATE devices SET state = 'maintenance' WHERE id = ?", (instance_id,)
    )
    conn.commit()


def desired_instances(conn: sqlite3.Connection, host_id: str) -> list[dict]:
    """這台 host 上該跑著的 VM 實例——隨 heartbeat 交給 exporter 收斂。

    跟 desired_services 同一個模型:coordinator 說「該有這些」,exporter
    自己補起缺的、停掉多的。``stopping`` 的也送過去,exporter 才知道要
    停哪些;它回報 gone 之後 row 才真的清掉。
    """
    # 帶上 class:exporter 有兩種 provisioner(qemu / cuttlefish),要靠它
    # 把實例分派給對的 manager。**不讓 exporter 猜**——猜錯的話會對一台
    # Cuttlefish 實例跑 qemu,或反過來。
    rows = conn.execute(
        "SELECT v.*, d.class FROM vm_instances v "
        "LEFT JOIN devices d ON d.id = v.id "
        "WHERE v.host = ? AND v.state != 'gone' ORDER BY v.id",
        (host_id,),
    ).fetchall()
    return [
        {"instance_id": r["id"], "template_id": r["template_id"],
         "state": r["state"], "class": r["class"], "spec": _tags(r["spec"])}
        for r in rows
    ]


def record_instance_results(
    conn: sqlite3.Connection,
    results: list[dict],
    now: str,
) -> list[str]:
    """收下 exporter 回報的 VM 實際狀態。

    ``gone`` 的實例**不刪 devices row,標成 retired**。原本這裡是刪掉的
    ——ephemeral 不該在池子裡留下痕跡——但 ``events.device_id`` 是硬性
    FK,刪 device 就得先刪掉它的 events,而 §8 明講 events 是 append-only
    的稽核紀錄。兩個要求擺在一起,稽核贏:一台 VM 刷過什麼、誰借過它,
    在它消失之後仍然要查得到。

    「不該在池子裡留下痕跡」用狀態解決而不是用刪除解決:``retired`` 的
    實例借不到(reserve 只接受 free),也不列在 list_devices 裡,效果跟
    刪掉一樣,但稽核鏈完整。
    """
    touched: list[str] = []
    for res in results:
        iid = res.get("instance_id")
        row = conn.execute(
            "SELECT * FROM vm_instances WHERE id = ?", (iid,)
        ).fetchone()
        if row is None:
            continue
        state = res.get("state")
        if state not in ("running", "gone", "failed"):
            continue

        if state == "running":
            # **只有還在 requested/running 的實例才接受 running 回報。**
            # Exporter 回報的是上一輪的觀測,而 coordinator 可能在那之後
            # 已經把它標成 stopping(lease 結束了)。少了這個條件,一份
            # 慢一拍的 running 回報會把 stopping 蓋回 running,VM 就再也
            # 停不掉——exporter 下一輪看到的 desired 又變回「該跑著」。
            # 這是 reconcile 模型的通則:回報是觀測,不是指令。
            conn.execute(
                "UPDATE vm_instances SET state = 'running', detail = ? WHERE id = ? "
                "AND state IN ('requested', 'running')",
                (json.dumps(res.get("detail"), ensure_ascii=False)
                 if res.get("detail") else None, iid),
            )
            conn.execute(
                "UPDATE devices SET last_seen_at = ? WHERE id = ?", (now, iid)
            )
            touched.append(iid)
            continue

        # gone / failed:實例沒了。標 retired,不刪 row(見 docstring)。
        conn.execute(
            "UPDATE vm_instances SET state = 'gone', finished_at = ?, detail = ? "
            "WHERE id = ?",
            (now, json.dumps(res.get("detail"), ensure_ascii=False)
             if res.get("detail") else None, iid),
        )
        _event(conn, iid, "device_detached", "exporter", now, row["lease_id"],
               {"ephemeral": True, "outcome": state,
                "detail": res.get("detail")})
        # endpoint 一定要清:指向一台已經不存在的 VM 的 endpoint 是最糟的
        # 一種,client 連得上一個空的 port。
        conn.execute("DELETE FROM device_services WHERE device_id = ?", (iid,))
        conn.execute(
            "UPDATE devices SET state = 'retired' WHERE id = ?", (iid,)
        )
        touched.append(iid)
    return touched


def reap_orphan_instances(
    conn: sqlite3.Connection,
    now_fn: NowFn = utcnow,
) -> list[str]:
    """lease 已經結束、實例卻還跑著的 VM → 標成該銷毀。

    ephemeral 的重點是「用完就沒了」。lease 一結束就該回收,不然池子會被
    孤兒 VM 佔滿——而且它們吃的是真的 RAM。
    """
    rows = conn.execute(
        "SELECT v.id FROM vm_instances v LEFT JOIN leases l ON l.id = v.lease_id "
        "WHERE v.state IN ('requested', 'running') "
        "AND (v.lease_id IS NULL OR l.status != 'active')"
    ).fetchall()
    for row in rows:
        destroy_instance(conn, row["id"], now_fn)
    return [r["id"] for r in rows]
