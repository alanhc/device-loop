"""強制回收:對卡住的裝置執行 power_control 動作(設計文件 §7 第 4 步)。

lease 過期時「停掉服務」由 reconcile 自動達成,但**裝置本身卡住**不會
自己好——下一個 agent 會借到一台不能用的。這個模組補的就是那一步。

**安全性**:這些動作會實際影響硬體(``adb reboot`` 真的會重開手機)。
所以:

- 執行層走注入的 ``ProcessRunner``,測試用 fake,開發機上不會真的重開
  任何東西。
- 只認識白名單裡的動作。coordinator 送來不認得的字串一律拒絕執行並回報
  失敗,不猜、不 fallback——回收動作猜錯的代價是對錯的裝置做破壞性操作。
- 動作有逾時。卡住的裝置很可能讓指令也卡住,不能讓收斂迴圈跟著停住。

ROG 上那顆 Pixel 8(``38011FDJH00C9F``)的 ``adb reboot`` 是可回復的,
但它上面有手工構築的分割區狀態,**真機測試前要先取得 alanhc 同意**,
不要因為「重開而已」就自己跑下去。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

from .proc import ProcessRunner

log = logging.getLogger(__name__)

# 單一動作的上限。卡住的裝置常讓 adb 指令一起卡住。
ACTION_TIMEOUT_S = 60.0


class UnknownAction(Exception):
    """不認識的回收動作。拒絕執行,不猜。"""


def _adb_reboot(identifier: str) -> list[str]:
    """重開 Android 裝置。

    走全域 adb server(不帶 -P):回收發生在服務已經停掉之後,這時候
    per-device server 不在了,裝置由全域 server 認領。``-s`` 指定序號,
    確保打到正確的那一台——host 上可能插著好幾顆。
    """
    return ["adb", "-s", identifier, "reboot"]


def _ipmi_power_cycle(identifier: str) -> list[str]:
    """BMC 電源循環。identifier 是 BMC 位址。

    尚未在任何真硬體上驗證過——14700 有 BMC 但目前沒有裝置用這條路徑。
    """
    return ["ipmitool", "-H", identifier, "chassis", "power", "cycle"]


# 白名單:對應 devices.power_control 的取值(schema 註解列的那些)。
ACTIONS: dict[str, Callable[[str], list[str]]] = {
    "adb-reboot": _adb_reboot,
    "ipmi-power-cycle": _ipmi_power_cycle,
}


@dataclass
class ReclaimOutcome:
    reclaim_id: int
    ok: bool
    detail: dict

    def as_report(self) -> dict:
        return {"reclaim_id": self.reclaim_id, "ok": self.ok, "detail": self.detail}


class ReclaimExecutor:
    """執行 coordinator 派下來的回收動作。"""

    def __init__(self, runner: ProcessRunner, timeout_s: float = ACTION_TIMEOUT_S):
        self._runner = runner
        self._timeout_s = timeout_s

    def execute(self, reclaim: dict) -> ReclaimOutcome:
        """跑一個回收動作,回傳結果讓 agent 隨下一輪 heartbeat 回報。

        任何失敗都回 ``ok=False`` 而不是拋例外——一個裝置回收失敗不該
        讓整輪收斂中斷,而且 coordinator 需要知道失敗原因(它會把裝置
        留在 maintenance,不放回 free)。
        """
        rid = reclaim.get("reclaim_id")
        action = reclaim.get("action")
        identifier = reclaim.get("identifier")

        build = ACTIONS.get(action)
        if build is None:
            log.error("unknown reclaim action %r; refusing to guess", action)
            return ReclaimOutcome(rid, False, {"error": f"unknown action {action!r}"})
        if not identifier:
            return ReclaimOutcome(rid, False, {"error": "no device identifier"})

        argv = build(identifier)
        if self._runner.which(argv[0]) is None:
            return ReclaimOutcome(
                rid, False, {"error": f"{argv[0]!r} not found in PATH"}
            )

        log.warning("force-reclaiming %s: %s", identifier, " ".join(argv))
        handle = self._runner.start(argv)
        rc = handle.wait(timeout_s=self._timeout_s)
        if rc is None:
            handle.terminate()
            return ReclaimOutcome(
                rid, False,
                {"error": "timed out", "timeout_s": self._timeout_s, "argv": argv},
            )
        return ReclaimOutcome(
            rid, rc == 0, {"exit_code": rc, "argv": argv}
        )
