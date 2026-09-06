"""Cuttlefish 池:ephemeral 的第二種 provisioner(設計文件 §12)。

§12 的硬體分層政策說得很直接:**真機時間是稀缺資源,只花在非真機不可的
事上**。Android userspace 的 `any` 工作有一條現成的路——14700 上 Cuttlefish
已經裝好(`cvd` + 預配 10 組 instance 網橋),AVD 就是 Android 的 ephemeral
池,真機 Pixel 只留 kernel/thermal/perf 工作。這個模組就是把那條路接上。

跟 ``vm.py`` 的關係:兩者是**同一個收斂模型的兩種 provisioner**。
coordinator 說「這台 host 上該有這些實例」,誰把它生出來是 exporter 的事。
所以這裡的介面跟 ``VMManager`` 一模一樣(``converge`` / ``stop_all`` /
``running``),差別只在起的是 ``cvd`` 而不是 ``qemu-system-*``。

三個 Cuttlefish 特有的地方:

- **實例編號是稀缺資源。** 每個實例佔一組預配的網橋(`cvd-etap-NN` 等,
  14700 上有 10 組),而編號決定了它的 adb port(``6520 + n - 1``)與
  網橋。所以編號要**明確指定**並在池內唯一,不能讓 cvd 自己挑——兩台
  同號的實例會搶同一組網橋。
- **對外就是一台 Android 裝置。** 實例的 adb 掛在 ``127.0.0.1:<6520+n>``,
  exporter 起一個 ``--one-device`` server 把它轉出來,client 看到的介面
  跟真 Pixel 完全一樣。§6 的 vnc 那列明講「Cuttlefish 自帶 WebRTC 串流,
  不走這條」,所以**不起 vnc**。
- **`cvd` 是 client/server 架構。** 指令送給常駐的 cvd server,自己很快
  就返回;實例的生命週期不綁在這個子行程上。所以這裡**不能**像 qemu 那樣
  用「子行程還活著嗎」判斷實例在不在——要問 ``cvd fleet``。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Callable

from .proc import ProcessRunner

log = logging.getLogger(__name__)

# 開一台 Android 起來要跑一陣子(assemble + boot)。給足餘裕,但不能無限等
# ——卡住的 cvd 會讓整個收斂迴圈停住。
START_TIMEOUT_S = 300.0
STOP_TIMEOUT_S = 120.0
# cvd fleet 只是問常駐 server 要狀態,很快。
FLEET_TIMEOUT_S = 30.0

# Cuttlefish 的 adb port 規則:第 n 台(1-based)掛在 6520 + n - 1。
ADB_PORT_BASE = 6520
# 14700 上預配了 10 組 instance 網橋(cvd-etap-01..10),所以池子最多 10 台。
# 超過的話實例會搶同一組網橋,兩台都不會正常。
MAX_INSTANCES = 10


class CvdError(Exception):
    """實例起不來——缺 cvd、image 路徑不對、池子滿了。"""


def adb_address(instance_num: int) -> str:
    """第 n 台實例的 adb 位址。exporter 用它起 --one-device server。"""
    return f"127.0.0.1:{ADB_PORT_BASE + instance_num - 1}"


def create_argv(spec: dict, instance_num: int, group: str) -> list[str]:
    """``cvd create``:生一台實例並啟動它。

    ``--base_instance_num`` 明確指定編號——不讓 cvd 自己挑,因為編號決定
    網橋與 adb port,兩台同號會搶同一組網橋。

    ``--daemon`` 讓 cvd 把實例丟到背景後返回:實例的生命週期歸常駐的 cvd
    server 管,不綁在這個子行程上。沒有它的話這個指令會一直前景跑到實例
    關掉為止,收斂迴圈就卡在這裡。

    ``--report_anonymous_usage_stats=n`` 是必要的:沒給的話 cvd 會在
    terminal 問一次 y/n,而 exporter 沒有 terminal——指令會卡到逾時。
    """
    host_path = spec.get("host_path")
    product_path = spec.get("product_path")
    if not host_path or not product_path:
        raise CvdError("spec needs host_path and product_path")

    argv = [
        "cvd",
        "--group_name", group,
        "create",
        "--host_path", str(host_path),
        "--product_path", str(product_path),
        "--base_instance_num", str(instance_num),
        "--num_instances", "1",
        "--daemon",
        "--report_anonymous_usage_stats", "n",
    ]
    # 記憶體/CPU 由 spec 指定:一台 host 上要塞好幾台 AVD,預設值未必合適。
    if memory := spec.get("memory_mb"):
        argv += ["--memory_mb", str(int(memory))]
    if cpus := spec.get("cpus"):
        argv += ["--cpus", str(int(cpus))]
    if extra := spec.get("extra_args"):
        argv.extend(extra)
    return argv


def stop_argv(group: str) -> list[str]:
    """停掉一整個 group。

    用 group 而不是 instance number:``cvd create`` 是以 group 為單位建的,
    停的時候用同一個單位才不會留下半個 group 的殘骸。
    """
    return ["cvd", "--group_name", group, "stop"]


def remove_argv(group: str) -> list[str]:
    """把 group 從 cvd 的 instance database 裡刪掉。

    **``cvd stop`` 不會釋放 instance number。** 停掉的 group 仍留在
    database 裡佔著它的編號,下一次 ``cvd create --base_instance_num 1``
    會失敗:``New instance conflicts with existing instance``。

    這是真機抓到的:先前手動測試留下的 group 已經 stop 了,exporter 要用
    同一個編號時 create 就吐 255。單元測試看不到——fake runner 不會維護
    cvd 的 instance database。

    編號是稀缺資源(14700 上只有 10 組預配網橋),不 remove 的話池子會
    一直漏,借滿十次之後就再也生不出實例。
    """
    return ["cvd", "--group_name", group, "remove"]


# `cvd fleet` 裡代表「這台實例真的在跑」的狀態。實測 cvd 1.32.0:
# 跑著的是 "Running"。
RUNNING_STATUS = "running"


def parse_fleet(stdout: str) -> set[str]:
    """從 ``cvd fleet`` 的輸出取出**還在跑**的 group 名稱。

    兩件事都是實測 cvd 1.32.0 得到的,兩件都不能從文件推出來:

    1. cvd 會在 JSON 前面印一行 log(``I cvd : main.cc:163 version: ...``),
       所以不能直接 ``json.loads`` 整份輸出——要從第一個 ``{`` 開始切。
    2. **`cvd stop` 之後 group 還留在 fleet 裡**,只是 instance 的
       ``status`` 變成 ``"Stopped"``。只看 group 在不在的話,停掉的實例
       會永遠被當成活著:exporter 不會重生它,而借它的人拿到一台開不起來
       的裝置。所以要看到至少一台 instance 是 Running 才算數。
    """
    start = stdout.find("{")
    if start < 0:
        return set()
    try:
        data = json.loads(stdout[start:])
    except ValueError:
        return set()
    names = set()
    for group in data.get("groups") or []:
        if not isinstance(group, dict):
            continue
        name = group.get("group_name")
        if not name:
            continue
        instances = group.get("instances") or []
        if any((i.get("status") or "").lower() == RUNNING_STATUS
               for i in instances if isinstance(i, dict)):
            names.add(name)
    return names


def parse_instance_numbers(stdout: str) -> set[int]:
    """cvd 的 instance database 裡被佔用的 instance number(**含已停的**)。

    ``instance_name`` 就是編號的字串形式(實測 cvd 1.32.0:``"1"``)。
    停掉的 group 仍列在 fleet 裡,而它的編號仍然不能重用——所以這裡
    **不看 status**,跟 ``parse_fleet`` 正好相反。
    """
    start = stdout.find("{")
    if start < 0:
        return set()
    try:
        data = json.loads(stdout[start:])
    except ValueError:
        return set()
    nums: set[int] = set()
    for group in data.get("groups") or []:
        if not isinstance(group, dict):
            continue
        for inst in group.get("instances") or []:
            if not isinstance(inst, dict):
                continue
            try:
                nums.add(int(inst.get("instance_name")))
            except (TypeError, ValueError):
                continue
    return nums


@dataclass
class RunningCvd:
    instance_id: str
    instance_num: int
    group: str

    def report(self) -> dict:
        return {
            "instance_id": self.instance_id,
            "state": "running",
            "detail": {
                "instance_num": self.instance_num,
                "group": self.group,
                # coordinator 據此算出 adb 服務要連的位址(desired_services
                # 的 `<service>_identifier`)——實例的 adb port 要等它真的
                # 生出來才知道,跟 endpoint 一樣是最終一致的。
                "adb_identifier": adb_address(self.instance_num),
            },
        }


class CvdManager:
    """起停 Cuttlefish 實例。介面跟 ``vm.VMManager`` 對齊。

    ``group`` 名稱由實例 id 推導,所以 exporter 重啟之後仍然認得出哪個
    group 屬於哪個實例——cvd server 是常駐的,實例活得比 exporter 久。
    """

    def __init__(self, runner: ProcessRunner,
                 fleet_fn: Callable[[], set[str]] | None = None):
        self._runner = runner
        self._fleet_fn = fleet_fn or self._cvd_fleet
        self._running: dict[str, RunningCvd] = {}

    # ------------------------------------------------------------ converge

    def converge(self, desired: list[dict]) -> tuple[list[str], list[str], list[dict]]:
        """讓實際跑著的實例往 desired 靠一步。

        回傳 (started, stopped, reports),跟 ``VMManager.converge`` 同一個
        形狀,agent 那邊不用分辨是哪種 provisioner。
        """
        wanted = {d["instance_id"]: d for d in desired
                  if d.get("state") in ("requested", "running")}
        stopping = [d["instance_id"] for d in desired
                    if d.get("state") == "stopping"]

        reports: list[dict] = []
        stopped: list[str] = []
        for instance_id in sorted(set(stopping) | (set(self._running) - set(wanted)
                                                   - set(stopping))):
            if self.stop(instance_id):
                stopped.append(instance_id)
            # 不管本來有沒有在跑都回報 gone:cvd server 是常駐的,exporter
            # 重啟後 coordinator 還記得那台實例,不回報它會永遠卡在 stopping。
            reports.append({"instance_id": instance_id, "state": "gone",
                            "detail": {"stopped_by": "exporter"}})

        live = self._fleet()
        started: list[str] = []
        for instance_id in sorted(wanted):
            vm = self._running.get(instance_id)
            # **在不在,要問 cvd fleet 而不是問子行程。** cvd 是 client/server
            # 架構,`cvd create` 很快就返回,實例歸常駐 server 管——用子行程
            # 判斷的話每輪都會以為它死了然後重生一台。
            if vm is not None and vm.group in live:
                reports.append(vm.report())
                continue
            if vm is not None:
                del self._running[instance_id]
            try:
                vm = self.start(instance_id, wanted[instance_id].get("spec") or {})
            except CvdError as e:
                log.error("failed to start %s: %s", instance_id, e)
                reports.append({"instance_id": instance_id, "state": "failed",
                                "detail": {"error": str(e)}})
                continue
            log.info("started %s as instance %d (group %s)",
                     instance_id, vm.instance_num, vm.group)
            started.append(instance_id)
            reports.append(vm.report())
        return started, stopped, reports

    # --------------------------------------------------------------- start

    def start(self, instance_id: str, spec: dict) -> RunningCvd:
        if self._runner.which("cvd") is None:
            raise CvdError("'cvd' not found in PATH")
        num = self._next_instance_num()
        group = self._group_for(instance_id)
        handle = self._runner.start(create_argv(spec, num, group))
        rc = handle.wait(timeout_s=START_TIMEOUT_S)
        if rc is None:
            # 卡住的 cvd。收掉這個子行程,但實例狀態未知——coordinator 收到
            # failed 會把裝置標成 maintenance,那正是這種情況該去的地方。
            handle.terminate()
            self._cleanup(group)
            raise CvdError(f"cvd create timed out after {START_TIMEOUT_S:.0f}s")
        if rc != 0:
            self._cleanup(group)
            raise CvdError(f"cvd create exited {rc}")
        vm = RunningCvd(instance_id, num, group)
        self._running[instance_id] = vm
        return vm

    def _cleanup(self, group: str) -> None:
        """失敗的 create 也要清乾淨。

        **失敗的 group 一樣會留在 cvd 的 instance database 裡佔著編號。**
        真機上就是這樣連環爆的:第一次 create 失敗,沒清;下一輪拿同一個
        編號重試,直接吐 `New instance conflicts with existing instance`
        (exit 255)——於是那個編號永遠卡住,重試永遠不會成功。

        清理本身失敗不往上拋:呼叫端正在報告一個更重要的錯誤(create 為
        什麼失敗),清理的錯誤蓋掉它只會更難查。
        """
        try:
            self._run_to_completion(stop_argv(group), f"cleanup stop {group}")
            self._run_to_completion(remove_argv(group), f"cleanup remove {group}")
        except Exception:                            # noqa: BLE001
            log.warning("failed to clean up %s after a failed create", group)

    def _group_for(self, instance_id: str) -> str:
        # cvd 的 group name 不接受任意字元;實例 id 是 `<template>-0001`
        # 這種形式,底線化之後拿來用,重啟後仍推導得出同一個名字。
        return "dl_" + instance_id.replace("-", "_").replace(".", "_")

    def _next_instance_num(self) -> int:
        """池子裡下一個沒被用的實例編號。

        編號決定網橋與 adb port,所以池內必須唯一;14700 上只預配了
        MAX_INSTANCES 組網橋,滿了就明確報錯,不要生一台去搶別人的網橋。

        **不只看自己起的實例,也要問 cvd。** cvd 的 instance database 是
        跨行程、跨重啟的:別的東西(手動測試、上一輪的 exporter)留下的
        group 一樣佔著編號,拿它的號去 create 會直接失敗。真機上就是這樣
        撞到的。
        """
        used = {vm.instance_num for vm in self._running.values()}
        used |= self._numbers_held_by_cvd()
        for num in range(1, MAX_INSTANCES + 1):
            if num not in used:
                return num
        raise CvdError(
            f"cuttlefish pool is full ({MAX_INSTANCES} instances, one per "
            "pre-provisioned bridge)"
        )

    def _numbers_held_by_cvd(self) -> set[int]:
        """cvd 的 instance database 目前佔著哪些編號(含已 stop 的)。

        問不到就回空集合:寧可讓 create 自己去撞那個明確的錯誤訊息,也不要
        因為查不到就拒絕生實例。
        """
        if self._runner.which("cvd") is None:
            return set()
        out = self._runner.output(["cvd", "fleet"], timeout_s=FLEET_TIMEOUT_S)
        if out is None:
            return set()
        return parse_instance_numbers(out)

    # ---------------------------------------------------------------- stop

    def stop(self, instance_id: str) -> bool:
        vm = self._running.pop(instance_id, None)
        if vm is None:
            return False
        self._run_to_completion(stop_argv(vm.group), f"stop {instance_id}")
        # **一定要 remove,不然編號不會被釋放**(見 remove_argv)。停掉的
        # group 仍佔著它的 instance number,下一次用同一號 create 會失敗。
        self._run_to_completion(remove_argv(vm.group), f"remove {instance_id}")
        return True

    def _run_to_completion(self, argv: list[str], what: str) -> None:
        handle = self._runner.start(argv)
        if handle.wait(timeout_s=STOP_TIMEOUT_S) is None:
            handle.terminate()
            log.warning("cvd %s timed out", what)

    def stop_all(self) -> list[str]:
        ids = sorted(self._running)
        for instance_id in ids:
            self.stop(instance_id)
        return ids

    def running(self) -> list[RunningCvd]:
        return [self._running[k] for k in sorted(self._running)]

    # --------------------------------------------------------------- fleet

    def _fleet(self) -> set[str]:
        try:
            return self._fleet_fn()
        except Exception:                            # noqa: BLE001
            # 問不到就當作「不知道」,而不是「都不在」——後者會讓收斂
            # 重生一整池已經在跑的實例。
            log.warning("cvd fleet failed; assuming known instances are still up")
            return {vm.group for vm in self._running.values()}

    def _cvd_fleet(self) -> set[str]:
        """目前 cvd server 上活著的 group 名稱。

        ``cvd fleet`` 回一份 JSON(``{"groups": [{"group_name": ...}]}``)。
        解析失敗一律當成「問不到」往上拋,由 ``_fleet()`` 決定怎麼處理
        ——那裡的策略是「保留已知狀態」而不是「假設都死了」。
        """
        if self._runner.which("cvd") is None:
            return set()
        out = self._runner.output(["cvd", "fleet"], timeout_s=FLEET_TIMEOUT_S)
        if out is None:
            raise CvdError("cvd fleet failed")
        return parse_fleet(out)
