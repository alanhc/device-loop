"""子行程執行層——把「怎麼真的起一個 daemon」抽成可注入的介面。

Exporter 的邏輯(挑 port、組指令、記錄啟停、退出清理)在沒有實體裝置的
機器上也要能測。所以所有 daemon 的實際啟動都走 ``ProcessRunner``:正式
環境用 ``SubprocessRunner``,測試用 ``FakeRunner`` 記下被要求跑什麼而
不真的 fork。這是交接文件明確要求的可替換執行層。
"""

from __future__ import annotations

import shutil
import signal
import socket
import subprocess
from typing import Protocol


class Handle(Protocol):
    """一個已啟動的子行程。只暴露 exporter 真正需要的三件事。"""

    @property
    def pid(self) -> int: ...

    def poll(self) -> int | None:
        """還在跑回 None,已結束回 exit code。"""
        ...

    def terminate(self, timeout_s: float = 5.0) -> int | None:
        """先 SIGTERM,逾時再 SIGKILL。回傳 exit code(不可得時 None)。"""
        ...

    def wait(self, timeout_s: float = 10.0) -> int | None:
        """等它自己跑完。逾時回 None(行程仍在跑,呼叫端自行決定要不要收)。

        給一次性的短命指令用(``adb kill-server``)。這種指令要的是
        「跑完」而不是「被殺掉」——用 terminate() 會在它還沒做完事情時
        就把它砍了。
        """
        ...


class ProcessRunner(Protocol):
    def start(self, argv: list[str]) -> Handle: ...

    def which(self, program: str) -> str | None:
        """程式在不在 PATH 上——起 daemon 前先驗,錯誤訊息才講得清楚。"""
        ...

    def output(self, argv: list[str], timeout_s: float = 30.0) -> str | None:
        """跑一個短命指令並拿回 stdout。逾時或非零結束回 None。

        跟 ``start()`` 的分工:``start`` 是給 daemon 用的(要 handle、要
        管生命週期、輸出丟掉),這個是給「問一個問題拿一個答案」用的
        ——``cvd fleet`` 要讀 JSON,``adb devices`` 之類的也是。

        **它一樣走注入層**,所以測試可以完全不碰真指令。這是刻意的:
        inventory 那次教訓(用 ``adb devices`` 掃描起了全域 server)之後,
        任何會跑子行程的路徑都要在測試裡看得見。
        """
        ...


class SubprocessRunner:
    """正式環境:真的 fork 出 daemon。"""

    def start(self, argv: list[str]) -> Handle:
        popen = subprocess.Popen(
            argv,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            start_new_session=True,  # 自成 process group,清理時整組收
        )
        return _PopenHandle(popen)

    def which(self, program: str) -> str | None:
        return shutil.which(program)

    def output(self, argv: list[str], timeout_s: float = 30.0) -> str | None:
        try:
            done = subprocess.run(
                argv, capture_output=True, text=True, timeout=timeout_s,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if done.returncode != 0:
            return None
        return done.stdout


class _PopenHandle:
    def __init__(self, popen: subprocess.Popen):
        self._popen = popen

    @property
    def pid(self) -> int:
        return self._popen.pid

    def poll(self) -> int | None:
        return self._popen.poll()

    def terminate(self, timeout_s: float = 5.0) -> int | None:
        if self._popen.poll() is not None:
            return self._popen.returncode
        self._popen.send_signal(signal.SIGTERM)
        try:
            return self._popen.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            self._popen.kill()
            try:
                return self._popen.wait(timeout=timeout_s)
            except subprocess.TimeoutExpired:
                return None

    def wait(self, timeout_s: float = 10.0) -> int | None:
        try:
            return self._popen.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            return None


def free_port() -> int:
    """跟核心要一個當下沒人用的 port。

    綁 0 讓核心挑,讀出號碼後立刻關掉再交給 daemon 去綁——中間有 race
    window(TOCTOU),但 daemon 起不來時 supervisor 會重試,對這個規模
    夠用,不值得為此自己維護 port 池。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]
