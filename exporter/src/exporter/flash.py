"""執行 coordinator 派下來的 flash(設計文件 §6 能力表唯一 mediated 的能力)。

跟 ``reclaim.py`` 同一個形狀,因為是同一種東西:coordinator 不碰硬體(§4),
它只把「該對這台裝置做什麼」寫成狀態,exporter 隨 heartbeat 領走執行。

Flash 比 reclaim 更危險——reclaim 最壞是重開一次,flash 刷壞就是磚。所以
在 reclaim 那幾條之外再加兩條:

- **刷之前自己驗 sha256。** Coordinator 的 registry 說「這個 uri 的雜湊
  應該是 X」,但檔案在 exporter 本地,可能被換掉、下載到一半、或根本是
  另一個版本。不驗的話 registry 只是記帳,擋不住真正刷進去的東西不對。
  這是唯一一個「檢查失敗就絕對不動手」的前置條件。
- **只認識白名單裡的 device class → flash 方法。** 不認得的一律拒絕,
  不猜:猜錯的代價是對一台不該用 fastboot 的板子跑 fastboot。

真機注意:這個模組**從未在真硬體上跑過**。ROG 上那顆 Pixel 8 有 alanhc
手工構築的分割區狀態,真機驗證前要先取得同意——跟 ``reclaim.py`` 的
``adb-reboot`` 同樣的理由,而且後果嚴重得多。
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .proc import ProcessRunner

log = logging.getLogger(__name__)

# 刷一份 image 的上限。比 reclaim 寬很多:vendor/system 分割區動輒幾百 MB,
# 真的要跑一陣子。但不能無限等——卡住的 fastboot 會讓收斂迴圈跟著停住。
FLASH_TIMEOUT_S = 600.0

# 讀檔算雜湊的 chunk。大 image 不能一次讀進記憶體。
_HASH_CHUNK = 1024 * 1024


class UnsupportedFlash(Exception):
    """不認識的 device class / image kind 組合。拒絕執行,不猜。"""


def _fastboot_argv(identifier: str, kind: str, path: str) -> list[str]:
    """Android:``fastboot -s <serial> flash <partition> <file>``。

    ``-s`` 指定序號,確保打到正確的那一台——host 上可能插著好幾顆,
    而刷錯裝置是這個系統最不能犯的錯。

    這裡**不做** ``fastboot reboot-bootloader`` 之類的 mode 切換:裝置得
    先在 fastboot mode 才刷得動,而「怎麼進 fastboot」每台不一樣(Pixel
    走 adb reboot bootloader,Jupiter 要短接 boot pin,見 §10 開放問題)。
    把它塞進來等於在猜,而猜錯是把裝置留在一個半吊子的狀態。
    """
    return ["fastboot", "-s", identifier, "flash", kind, path]


def _dd_sd_argv(identifier: str, kind: str, path: str) -> list[str]:  # noqa: ARG001
    """SD/eMMC 整卡寫入。``identifier`` 是 block device 路徑。

    ``conv=fsync`` 確保回報成功時資料真的落盤——不然 dd 回 0 只代表寫進
    page cache,拔卡就是一張刷壞的卡。

    **尚未在真硬體上驗證**;Jupiter 的 flash 機制本身還是 §10 的開放問題
    (SD 卡插拔重燒還是 USB burn mode 未定)。
    """
    return ["dd", f"if={path}", f"of={identifier}", "bs=4M", "conv=fsync"]


# 白名單:device class → 組指令的函式。對應 coordinator 送來的 devices.class。
FLASHERS: dict[str, Callable[[str, str, str], list[str]]] = {
    "android": _fastboot_argv,
    "riscv-sbc": _dd_sd_argv,
}


def sha256_file(path: Path, chunk: int = _HASH_CHUNK) -> str:
    """算檔案的 sha256。分段讀,image 可能有好幾 GB。"""
    h = hashlib.sha256()
    with path.open("rb") as f:
        while block := f.read(chunk):
            h.update(block)
    return h.hexdigest()


@dataclass
class FlashOutcome:
    flash_id: int
    ok: bool
    detail: dict

    def as_report(self) -> dict:
        return {"flash_id": self.flash_id, "ok": self.ok, "detail": self.detail}


class FlashExecutor:
    """執行 coordinator 派下來的 flash 動作。

    ``hasher`` 可注入:測試不用真的準備一份幾百 MB 的 image。
    """

    def __init__(
        self,
        runner: ProcessRunner,
        timeout_s: float = FLASH_TIMEOUT_S,
        hasher: Callable[[Path], str] = sha256_file,
    ):
        self._runner = runner
        self._timeout_s = timeout_s
        self._hasher = hasher

    def execute(self, flash: dict) -> FlashOutcome:
        """刷一份 image,回傳結果讓 agent 隨下一輪 heartbeat 回報。

        任何失敗都回 ``ok=False`` 而不是拋例外——一台裝置刷失敗不該讓整輪
        收斂中斷,而且 coordinator 需要知道原因(它會把裝置標成
        maintenance,不放回去給下一個 agent)。
        """
        fid = flash.get("flash_id")
        identifier = flash.get("identifier")
        device_class = flash.get("class")
        kind = flash.get("kind")
        uri = flash.get("uri")
        expected = (flash.get("sha256") or "").lower()

        build = FLASHERS.get(device_class)
        if build is None:
            log.error("no flash method for device class %r; refusing to guess",
                      device_class)
            return FlashOutcome(
                fid, False, {"error": f"no flash method for class {device_class!r}"}
            )
        if not identifier or not kind or not uri:
            return FlashOutcome(
                fid, False,
                {"error": "incomplete flash job (identifier/kind/uri required)"},
            )

        # 先驗雜湊,再碰裝置。順序要緊:驗證失敗時裝置完全沒被動過。
        path = Path(uri)
        if not path.is_file():
            return FlashOutcome(fid, False, {"error": f"image not found at {uri!r}"})
        try:
            actual = self._hasher(path)
        except OSError as e:
            return FlashOutcome(fid, False, {"error": f"cannot read {uri!r}: {e}"})
        if actual.lower() != expected:
            # 手上這份不是 registry 登記的那份。絕不刷。
            log.error("sha256 mismatch for %s: expected %s, got %s",
                      uri, expected, actual)
            return FlashOutcome(
                fid, False,
                {"error": "sha256 mismatch; refusing to flash",
                 "expected": expected, "actual": actual, "uri": uri},
            )

        argv = build(identifier, kind, str(path))
        if self._runner.which(argv[0]) is None:
            return FlashOutcome(
                fid, False, {"error": f"{argv[0]!r} not found in PATH"}
            )

        log.warning("flashing %s (%s) with %s: %s",
                    identifier, kind, flash.get("image_id"), " ".join(argv))
        handle = self._runner.start(argv)
        rc = handle.wait(timeout_s=self._timeout_s)
        if rc is None:
            # 卡住的 fastboot。收掉它,但裝置狀態未知——coordinator 收到
            # ok=False 會把它標成 maintenance,那正是這種情況該去的地方。
            handle.terminate()
            return FlashOutcome(
                fid, False,
                {"error": "timed out", "timeout_s": self._timeout_s, "argv": argv},
            )
        return FlashOutcome(
            fid, rc == 0,
            {"exit_code": rc, "argv": argv, "image_id": flash.get("image_id"),
             "sha256": actual},
        )
