"""Ephemeral QEMU 池:按 coordinator 的 desired 生出/銷毀 VM(§10/§12)。

跟 ``services.py`` 同一個收斂模型,只是收斂的對象是整台 VM 而不是一個
存取 daemon:coordinator 說「這台 host 上該有這些實例」,exporter 補起
缺的、停掉多的,並回報實際狀態。

**銷毀就是全部的清理。** §12 講 interstitial hooks 時把 ephemeral 列為
「直接銷毀重生,天然乾淨」——所以這裡沒有任何 reset 邏輯,整台丟掉。
真機那條路才需要 ``adb reboot`` / restore known-good 那些麻煩事。

**架構決定用哪種加速**(§12 硬體分層):aarch64 的工作導去 M1 的 HVF
池才有意義,x86 host 上跑 aarch64 只能靠 TCG 模擬,慢一個量級。所以
``accel`` 由 spec 指定,不猜——猜錯的後果是 benchmark 數字沒有意義,
而那正是這個系統要避免的事。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

from .proc import Handle, ProcessRunner, free_port

log = logging.getLogger(__name__)

# 沒指定就用最保守的:純模擬,到處都跑得起來,只是慢。
DEFAULT_ARCH = "x86_64"
DEFAULT_ACCEL = "tcg"
DEFAULT_MEMORY_MB = 1024


class VMError(Exception):
    """VM 起不來——缺 qemu、spec 不合法。"""


def qemu_argv(spec: dict, vnc_port: int) -> list[str]:
    """組出一台 headless VM 的 qemu 指令。

    ``-vnc`` 用的是 **display number 而不是 port**(:0 = 5900),這是
    QEMU 的老陷阱:寫成 port 號會開到 5900+port 那個天邊的位置去。所以
    這裡換算,並且用 ``to=0`` 之外的明確 display,不讓 QEMU 自己挑。

    ``-nographic`` 不能跟 ``-vnc`` 一起用(前者把 display 關掉),所以
    console 走 ``-serial`` 到 stdio 之外的地方——這裡不接 console,要
    console 的話是另一個 socat 的事(§6 relay 原語)。
    """
    arch = spec.get("arch", DEFAULT_ARCH)
    accel = spec.get("accel", DEFAULT_ACCEL)
    memory = int(spec.get("memory_mb", DEFAULT_MEMORY_MB))
    image = spec.get("image")
    if not image:
        raise VMError("spec has no image")

    if vnc_port < 5900:
        raise VMError(f"vnc port {vnc_port} is below the VNC base port 5900")
    display = vnc_port - 5900

    argv = [
        f"qemu-system-{arch}",
        "-machine", spec.get("machine", "virt" if arch != "x86_64" else "q35"),
        "-accel", accel,
        "-m", str(memory),
        "-smp", str(int(spec.get("cpus", 2))),
        "-drive", f"file={image},format=qcow2,if=virtio",
        # snapshot:寫入不落回 base image。ephemeral 的實例本來就該用完
        # 就丟,而共用同一份 base image 的兩台 VM 互相污染是最難查的那種
        # 錯誤。
        "-snapshot",
        "-vnc", f":{display}",
        "-daemonize" if spec.get("daemonize") else "-nodefaults",
    ]
    if extra := spec.get("extra_args"):
        argv.extend(extra)
    return argv


@dataclass
class RunningVM:
    instance_id: str
    vnc_port: int
    handle: Handle

    def report(self) -> dict:
        return {
            "instance_id": self.instance_id,
            "state": "running",
            "detail": {"pid": self.handle.pid, "vnc_port": self.vnc_port},
        }


class VMManager:
    """起停 ephemeral VM,並保證退出時清乾淨。

    跟 ``ServiceManager`` 同樣的形狀與同樣的理由:實際啟動走注入的
    ``ProcessRunner``,所以這層在沒有 KVM、沒裝 qemu 的機器上完全可測。
    """

    def __init__(self, runner: ProcessRunner,
                 port_fn: Callable[[], int] = free_port):
        self._runner = runner
        self._port_fn = port_fn
        self._running: dict[str, RunningVM] = {}

    def converge(self, desired: list[dict]) -> tuple[list[str], list[str], list[dict]]:
        """讓實際跑著的 VM 往 desired 靠一步。

        回傳 (started, stopped, reports)。``reports`` 是要隨下一次
        heartbeat 送回去的實際狀態,包含**這一輪剛停掉的**——coordinator
        要收到 gone 才會把 row 清掉,不回報的話那些實例會永遠卡在
        stopping。
        """
        wanted = {d["instance_id"]: d for d in desired
                  if d.get("state") in ("requested", "running")}
        stopping = [d["instance_id"] for d in desired
                    if d.get("state") == "stopping"]

        reports: list[dict] = []
        stopped: list[str] = []
        # 停掉:被標成 stopping 的,以及 desired 裡整個不見了的(coordinator
        # 已經忘了它,那就是孤兒)。
        for instance_id in sorted(set(stopping) | (set(self._running) - set(wanted)
                                                   - set(stopping))):
            if self.stop(instance_id):
                stopped.append(instance_id)
            # 不管本來有沒有在跑都回報 gone:exporter 重啟後 coordinator
            # 還記得那台 VM,而它其實已經隨著上一個 process 一起死了。
            reports.append({"instance_id": instance_id, "state": "gone",
                            "detail": {"stopped_by": "exporter"}})

        started: list[str] = []
        for instance_id in sorted(wanted):
            vm = self._running.get(instance_id)
            if vm is not None and vm.handle.poll() is None:
                reports.append(vm.report())
                continue
            if vm is not None:
                del self._running[instance_id]       # 死掉的殘骸
            try:
                vm = self.start(instance_id, wanted[instance_id].get("spec") or {})
            except VMError as e:
                log.error("failed to spawn %s: %s", instance_id, e)
                reports.append({"instance_id": instance_id, "state": "failed",
                                "detail": {"error": str(e)}})
                continue
            log.info("spawned %s on vnc port %d", instance_id, vm.vnc_port)
            started.append(instance_id)
            reports.append(vm.report())
        return started, stopped, reports

    def start(self, instance_id: str, spec: dict) -> RunningVM:
        port = self._vnc_port()
        argv = qemu_argv(spec, port)
        if self._runner.which(argv[0]) is None:
            raise VMError(f"{argv[0]!r} not found in PATH")
        handle = self._runner.start(argv)
        rc = handle.poll()
        if rc is not None:
            raise VMError(f"qemu for {instance_id} exited immediately (rc={rc})")
        vm = RunningVM(instance_id, port, handle)
        self._running[instance_id] = vm
        return vm

    def _vnc_port(self) -> int:
        """挑一個 >= 5900 的 port。

        QEMU 的 ``-vnc :N`` 只表達得出 5900+N,所以隨機 port 池那套在這裡
        不適用——低於 5900 的 port 根本沒辦法叫 QEMU 去綁。
        """
        for _ in range(50):
            port = self._port_fn()
            if port >= 5900:
                return port
        raise VMError("could not find a free port at or above 5900")

    def stop(self, instance_id: str) -> bool:
        vm = self._running.pop(instance_id, None)
        if vm is None:
            return False
        vm.handle.terminate()
        return True

    def stop_all(self) -> list[str]:
        """退出時清理:ephemeral VM 不該活得比起它的 exporter 久。"""
        ids = sorted(self._running)
        for instance_id in ids:
            self.stop(instance_id)
        return ids

    def running(self) -> list[RunningVM]:
        return [self._running[k] for k in sorted(self._running)]
