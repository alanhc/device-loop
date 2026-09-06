"""執行層本身:free_port 與真子行程的終止行為。

這裡是少數真的 fork 的測試——用 ``sleep`` 而不是任何裝置工具,所以在
沒有實體裝置的機器上照樣跑得過。
"""

from __future__ import annotations

import socket

from exporter.proc import SubprocessRunner, free_port


def test_free_port_returns_a_bindable_port():
    port = free_port()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("", port))  # 沒被佔住才綁得起來


def test_free_port_does_not_repeat_immediately():
    assert len({free_port() for _ in range(5)}) > 1


def test_subprocess_runner_starts_and_polls():
    handle = SubprocessRunner().start(["sleep", "30"])
    try:
        assert handle.pid > 0
        assert handle.poll() is None
    finally:
        handle.terminate()


def test_terminate_stops_a_running_process():
    handle = SubprocessRunner().start(["sleep", "30"])
    handle.terminate()
    assert handle.poll() is not None


def test_terminate_is_safe_on_an_already_dead_process():
    handle = SubprocessRunner().start(["true"])
    handle.terminate()
    assert handle.terminate() == handle.poll()


def test_which_finds_real_programs_and_misses_fake_ones():
    runner = SubprocessRunner()
    assert runner.which("sh") is not None
    assert runner.which("definitely-not-a-real-program-xyz") is None
