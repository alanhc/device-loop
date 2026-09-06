"""Ephemeral QEMU 池的收斂(設計文件 §10/§12)。

跟服務收斂同一個模型,但對象是整台 VM。這裡測的是那些「不測就會在真的
接上池子時才發現」的地方:VNC display number 的換算、孤兒 VM 的回報、
退出時不留下活著的 VM。
"""

from __future__ import annotations

import pytest

from exporter.vm import VMError, VMManager, qemu_argv

SPEC = {"image": "/srv/vm/debian.qcow2", "arch": "x86_64", "accel": "kvm",
        "memory_mb": 2048, "cpus": 4}


@pytest.fixture()
def vnc_ports():
    """VNC port 一定 >= 5900——QEMU 的 -vnc :N 只表達得出 5900+N。"""
    counter = iter(range(5901, 5999))
    return lambda: next(counter)


# ---------------------------------------------------------------- argv

def test_vnc_port_is_converted_to_a_display_number():
    """QEMU 的老陷阱:``-vnc`` 吃的是 display number,不是 port。
    寫成 port 號會開到 5900+port 那個天邊的位置去。"""
    argv = qemu_argv(SPEC, 5903)
    assert argv[argv.index("-vnc") + 1] == ":3"


def test_a_port_below_5900_is_rejected(): 
    """低於 5900 的 port 根本沒辦法叫 QEMU 去綁,早點講清楚。"""
    with pytest.raises(VMError, match="5900"):
        qemu_argv(SPEC, 5899)


def test_spec_without_an_image_is_rejected():
    with pytest.raises(VMError, match="image"):
        qemu_argv({"arch": "aarch64"}, 5901)


def test_accel_comes_from_the_spec_and_is_never_guessed():
    """§12:aarch64 導去 M1 的 HVF 才有意義,x86 上只能 TCG 模擬,慢一個
    量級。猜錯的後果是 benchmark 數字沒有意義。"""
    argv = qemu_argv({**SPEC, "arch": "aarch64", "accel": "hvf"}, 5901)
    assert argv[0] == "qemu-system-aarch64"
    assert argv[argv.index("-accel") + 1] == "hvf"


def test_snapshot_keeps_instances_from_polluting_the_base_image():
    """共用同一份 base image 的兩台 VM 互相污染是最難查的那種錯誤。"""
    assert "-snapshot" in qemu_argv(SPEC, 5901)


# ------------------------------------------------------------- converge

def _desired(instance_id="pool-0001", state="requested", spec=None):
    return {"instance_id": instance_id, "template_id": "pool",
            "state": state, "spec": spec if spec is not None else SPEC}


def test_requested_instance_is_spawned(runner, vnc_ports):
    vms = VMManager(runner, port_fn=vnc_ports)
    started, stopped, reports = vms.converge([_desired()])
    assert started == ["pool-0001"] and stopped == []
    assert reports[0]["state"] == "running"
    assert runner.started[-1].argv[0] == "qemu-system-x86_64"


def test_a_running_instance_is_not_spawned_twice(runner, vnc_ports):
    vms = VMManager(runner, port_fn=vnc_ports)
    vms.converge([_desired()])
    started, _, reports = vms.converge([_desired(state="running")])
    assert started == []
    assert len(runner.started) == 1
    assert reports[0]["detail"]["vnc_port"] == 5901


def test_stopping_instance_is_terminated_and_reported_gone(runner, vnc_ports):
    """coordinator 要收到 gone 才會把 row 清掉;不回報的話實例永遠卡在
    stopping。"""
    vms = VMManager(runner, port_fn=vnc_ports)
    vms.converge([_desired()])
    started, stopped, reports = vms.converge([_desired(state="stopping")])
    assert stopped == ["pool-0001"]
    assert reports == [{"instance_id": "pool-0001", "state": "gone",
                        "detail": {"stopped_by": "exporter"}}]
    assert runner.started[-1].terminated


def test_an_instance_the_coordinator_forgot_is_stopped(runner, vnc_ports):
    """desired 裡整個不見了 = 孤兒,吃的是真的 RAM。"""
    vms = VMManager(runner, port_fn=vnc_ports)
    vms.converge([_desired()])
    started, stopped, _ = vms.converge([])
    assert stopped == ["pool-0001"]


def test_a_stopping_instance_that_was_never_running_is_still_reported_gone(
    runner, vnc_ports
):
    """exporter 重啟後 coordinator 還記得那台 VM,但它已經隨上一個
    process 一起死了。不回報 gone 的話它永遠卡在 stopping。"""
    vms = VMManager(runner, port_fn=vnc_ports)
    _, stopped, reports = vms.converge([_desired(state="stopping")])
    assert stopped == []
    assert reports == [{"instance_id": "pool-0001", "state": "gone",
                        "detail": {"stopped_by": "exporter"}}]


def test_a_dead_vm_is_respawned(runner, vnc_ports):
    vms = VMManager(runner, port_fn=vnc_ports)
    vms.converge([_desired()])
    runner.started[-1].exit_code = 1          # qemu 自己死了
    started, _, reports = vms.converge([_desired(state="running")])
    assert started == ["pool-0001"]
    assert reports[0]["state"] == "running"


def test_a_bad_spec_is_reported_not_raised(runner, vnc_ports):
    """一台 VM 起不來不該中斷整輪收斂。"""
    vms = VMManager(runner, port_fn=vnc_ports)
    started, _, reports = vms.converge([_desired(spec={"arch": "x86_64"})])
    assert started == []
    assert reports[0]["state"] == "failed"
    assert "image" in reports[0]["detail"]["error"]


def test_missing_qemu_is_reported(make_runner, vnc_ports):
    vms = VMManager(make_runner(missing={"qemu-system-x86_64"}), port_fn=vnc_ports)
    _, _, reports = vms.converge([_desired()])
    assert reports[0]["state"] == "failed"
    assert "not found in PATH" in reports[0]["detail"]["error"]


def test_stop_all_leaves_nothing_running(runner, vnc_ports):
    """ephemeral VM 不該活得比起它的 exporter 久。"""
    vms = VMManager(runner, port_fn=vnc_ports)
    vms.converge([_desired("pool-0001"), _desired("pool-0002")])
    assert vms.stop_all() == ["pool-0001", "pool-0002"]
    assert vms.running() == []
    assert all(h.terminated for h in runner.started)
