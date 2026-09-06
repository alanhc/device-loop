"""測試用的假執行層——不 fork 任何真的子行程。"""

from __future__ import annotations

import pytest


class FakeHandle:
    """假的子行程。測試直接操縱 ``exit_code`` 模擬 daemon 死掉。

    ``terminate`` 與 ``wait`` 刻意分開記錄:一次性指令(``adb kill-server``)
    必須被 **wait** 到跑完,被 terminate 掉等於沒做完事。真機上這個差別
    是「per-device server 看不看得到裝置」,所以測試要能分辨。
    """

    def __init__(self, pid: int, argv: list[str]):
        self._pid = pid
        self.argv = argv
        self.exit_code: int | None = None
        self.terminated = False
        self.waited = False
        # 模擬「需要跑一段時間才完成工作」的指令:被 terminate 打斷的話
        # work_done 就是 False——真實的 kill-server 就是這樣。
        self.work_done = False
        self.hangs = False        # 設 True 模擬卡住的 preflight

    @property
    def pid(self) -> int:
        return self._pid

    def poll(self) -> int | None:
        return self.exit_code

    def terminate(self, timeout_s: float = 5.0) -> int | None:
        self.terminated = True
        if self.exit_code is None:
            self.exit_code = -15  # SIGTERM:工作沒做完就被砍了
        return self.exit_code

    def wait(self, timeout_s: float = 10.0) -> int | None:
        self.waited = True
        if self.hangs:
            return None
        if self.exit_code is None:
            self.exit_code = 0
            self.work_done = True   # 等到它跑完,工作才真的完成
        return self.exit_code


class FakeRunner:
    """記下被要求跑什麼,不真的執行。

    ``missing`` 裡的程式視同不在 PATH 上,用來測缺工具的錯誤路徑。
    """

    def __init__(self, missing: set[str] | None = None):
        self.started: list[FakeHandle] = []
        self.asked: list[list[str]] = []
        self.missing = missing or set()
        self._next_pid = 1000

    def start(self, argv: list[str]) -> FakeHandle:
        self._next_pid += 1
        handle = FakeHandle(self._next_pid, argv)
        self.started.append(handle)
        return handle

    def which(self, program: str) -> str | None:
        return None if program in self.missing else f"/usr/bin/{program}"

    def output(self, argv: list[str], timeout_s: float = 30.0) -> str | None:
        """一次性指令的 stdout。預設沒有輸出——需要的測試自己包一層,
        免得每個 fake 都要記得餵一份假 JSON。"""
        self.asked.append(argv)
        return None


@pytest.fixture
def make_runner():
    """工廠:測試要指定缺哪些工具時用這個。"""
    return FakeRunner


@pytest.fixture
def runner() -> FakeRunner:
    return FakeRunner()


@pytest.fixture
def ports():
    """可預測的 port 序號,讓斷言不用猜核心給什麼。"""
    counter = iter(range(9000, 9100))
    return lambda: next(counter)
