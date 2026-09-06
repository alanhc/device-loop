"""Reaper 不能凍住 event loop(真機事故的回歸測試)。

事故形狀:``_reaper_loop`` 原本在 coroutine 裡直接 ``with app.state.lock:``。
``threading.Lock.acquire()`` 是阻塞呼叫,在 coroutine 裡執行會讓**整個
event loop 停住**——連 accept 新連線、讀請求、送回應都停。而鎖的持有者是
跑在 threadpool 上的 sync endpoint,它要送出回應得靠那個已經凍住的 loop。
兩邊互等,服務永久卡死,而容器看起來還是健康的(行程活著、port 開著),
只是每個請求都逾時。

觸發條件很平常:任何一個持鎖久一點的請求 + reaper 剛好在那個窗口醒來。
真機上是 exporter 的 heartbeat 撞上 Cuttlefish 的 spawn。
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from coordinator import api


@pytest.fixture()
def anyio_backend():
    return "asyncio"


class _FakeApp:
    """只提供 reaper 需要的東西。"""

    def __init__(self, lock):
        self.state = type("S", (), {})()
        self.state.lock = lock
        self.state.conn = None
        self.ticks = 0


@pytest.mark.anyio
async def test_the_reaper_does_not_freeze_the_event_loop(monkeypatch):
    """鎖被別人持有時,event loop 必須照常跑。

    這條測的是**機制**不是 store 邏輯:reaper 的工作有沒有丟到 threadpool。
    直接在 coroutine 裡等鎖的話,下面的心跳會整段停住。
    """
    lock = threading.Lock()
    app = _FakeApp(lock)

    def fake_tick(_app):
        with lock:              # 跟正式的 _reaper_tick 一樣會阻塞
            _app.ticks += 1

    monkeypatch.setattr(api, "_reaper_tick", fake_tick)
    monkeypatch.setattr(api, "REAPER_INTERVAL_S", 0.05)

    beats: list[float] = []

    async def heartbeat():
        for _ in range(12):
            beats.append(time.monotonic())
            await asyncio.sleep(0.05)

    # 模擬一個持鎖久一點的 sync endpoint(跑在 threadpool 上)。
    lock.acquire()

    def release_later():
        time.sleep(0.4)
        lock.release()

    threading.Thread(target=release_later, daemon=True).start()

    reaper = asyncio.create_task(api._reaper_loop(app))
    try:
        await heartbeat()
    finally:
        reaper.cancel()
        try:
            await reaper
        except asyncio.CancelledError:
            pass

    gaps = [b - a for a, b in zip(beats, beats[1:])]
    # 沒有任何一次心跳被鎖擋掉:有的話 gap 會接近持鎖的 0.4s。
    assert max(gaps) < 0.25, (
        f"event loop stalled for {max(gaps):.2f}s while the lock was held — "
        "reaper 又在 coroutine 裡直接等鎖了"
    )


@pytest.mark.anyio
async def test_a_failing_tick_does_not_kill_the_reaper(monkeypatch):
    """一輪失敗不該讓 reaper 整個停掉——那會讓過期的 lease 永遠不回收,
    而且沒有任何人會發現。"""
    app = _FakeApp(threading.Lock())
    calls = {"n": 0}

    def boom(_app):
        calls["n"] += 1
        raise RuntimeError("transient")

    monkeypatch.setattr(api, "_reaper_tick", boom)
    monkeypatch.setattr(api, "REAPER_INTERVAL_S", 0.02)

    reaper = asyncio.create_task(api._reaper_loop(app))
    await asyncio.sleep(0.2)
    reaper.cancel()
    try:
        await reaper
    except asyncio.CancelledError:
        pass

    assert calls["n"] > 1, "reaper 在第一次失敗之後就不跑了"
