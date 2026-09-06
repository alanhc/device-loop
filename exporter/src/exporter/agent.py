"""Exporter 的主迴圈:掃 inventory、回報、收斂到 coordinator 給的 desired。

**Reconcile 而非 push**(裁決於 2026-09-05)。Coordinator 不主動叫
exporter 起停服務;每次 heartbeat 的回應帶上「這台 host 現在該跑什麼」,
exporter 拿它跟自己實際跑著的比對,自行補起缺的、停掉多的,並把實際
起好的 host:port 回報上去。

這樣做的理由(對照被否決的 push):

- Heartbeat 本來就是 control-plane channel(§4),不用另外開連線。
- Exporter 不需要開 listening port。機會性節點(筆電)在 NAT 後面或
  睡醒之後照樣能用。
- Coordinator 重啟不需要記得通知過誰,下一輪 heartbeat 自動收斂。
- 失敗的語意簡單:沒收斂成功的東西下一輪還在 desired 裡,自然重試。

代價是 lease 到服務可用有一個 heartbeat 週期的延遲——所以 endpoint 是
**最終一致**的,MCP 那邊取 endpoint 要處理 pending 狀態。
"""

from __future__ import annotations

import logging
import signal
import threading
from dataclasses import dataclass, field

from .coordinator_client import CoordinatorClient, DesiredService
from .flash import FlashExecutor
from .inventory import Scanner, collect
from .reclaim import ReclaimExecutor
from .services import ServiceError, ServiceManager
from .cvd import CvdManager
from .vm import VMManager

log = logging.getLogger(__name__)

DEFAULT_INTERVAL_S = 5.0

# 哪種 instance class 歸哪個 provisioner 管。跟 coordinator 的
# store.INSTANCE_CLASSES 對應——那邊是真相來源(它決定 template 生出什麼),
# 這裡只是把實例分派給對的 manager。
VM_INSTANCE_CLASSES = ("qemu-vm",)
CVD_INSTANCE_CLASSES = ("cuttlefish-vm",)


@dataclass
class TickResult:
    """一輪收斂做了什麼。回傳出來讓測試看得見,也方便記 log。"""

    started: list[tuple[str, str]]
    stopped: list[tuple[str, str]]
    failed: list[tuple[str, str]]
    reported: list[dict]
    reclaimed: list[str] = field(default_factory=list)
    flashed: list[str] = field(default_factory=list)
    # ephemeral VM:這一輪生出/銷毀了哪些實例。
    spawned: list[str] = field(default_factory=list)
    destroyed: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.started or self.stopped or self.failed)


class ExporterAgent:
    """把 inventory、heartbeat、服務收斂綁在一起的主迴圈。

    ``tick()`` 是一輪完整的收斂,可以單獨呼叫——測試不需要真的跑迴圈。
    """

    def __init__(
        self,
        client: CoordinatorClient,
        services: ServiceManager,
        scanners: list[Scanner],
        interval_s: float = DEFAULT_INTERVAL_S,
        reclaimer: ReclaimExecutor | None = None,
        flasher: FlashExecutor | None = None,
        vms: VMManager | None = None,
        cvds: CvdManager | None = None,
    ):
        self._client = client
        self._services = services
        self._scanners = scanners
        self._interval_s = interval_s
        self._reclaimer = reclaimer
        self._flasher = flasher
        self._vms = vms
        self._cvds = cvds
        self._stop = threading.Event()
        # 上一輪執行完、還沒回報出去的回收結果。
        self._pending_reclaim_reports: list[dict] = []
        # 同上,flash 的結果。刷一份 image 可能要好幾分鐘,一定跨輪。
        self._pending_flash_reports: list[dict] = []
        # VM 實例的實際狀態。跟服務回報不同,這個是**每輪重算**的完整快照
        # ——coordinator 要靠它才知道實例還在不在。
        self._instance_reports: list[dict] = []

    # ------------------------------------------------------------ one tick

    def tick(self) -> TickResult:
        """掃描 → 回報目前狀態 → 收斂到 coordinator 給的 desired。

        先把自己死掉的 daemon 清掉再回報,否則會回報一個已經連不上的
        endpoint,coordinator 就會把它當成有效的交給 client。
        """
        dead = self._services.reap_dead()
        for device_id, service in dead:
            log.warning("daemon for %s/%s died; will restart", device_id, service)

        inventory = collect(self._scanners)
        reports, self._pending_reclaim_reports = self._pending_reclaim_reports, []
        flash_reports, self._pending_flash_reports = self._pending_flash_reports, []
        result = self._client.heartbeat(
            identifiers=inventory.identifiers,
            discoverable_classes=inventory.discoverable_classes,
            seen=inventory.seen,
            services=self._running_report(),
            reclaims=reports,
            flashes=flash_reports,
            instances=self._instance_reports,
        )
        tick = self._converge(result.desired)
        # Flash 先於 reclaim:reclaim 會重開裝置,而 coordinator 已經擋掉
        # 「有 reclaim 在飛的裝置不排 flash」,所以同一輪不會兩個都有;
        # 順序固定只是讓行為可預測。
        tick.flashed = self._run_flashes(result.flashes)
        tick.reclaimed = self._run_reclaims(result.reclaims)
        tick.spawned, tick.destroyed = self._converge_vms(result.instances)
        return tick

    def _converge_vms(self, instances: list[dict]) -> tuple[list[str], list[str]]:
        """收斂 ephemeral 池(§10/§12)。

        跟服務收斂同一個模型,只是對象是整台 VM。回報存起來下一輪送——
        這輪的 heartbeat 已經出去了。

        **兩種 provisioner,一個模型**:qemu 與 cuttlefish 各自收斂自己那
        一半。誰負責哪一台由 coordinator 給的 ``class`` 決定,exporter 不
        猜——猜錯的話會對一台 Cuttlefish 實例跑 qemu,或反過來。
        """
        managers = [
            (self._vms, VM_INSTANCE_CLASSES),
            (self._cvds, CVD_INSTANCE_CLASSES),
        ]
        started: list[str] = []
        stopped: list[str] = []
        reports: list[dict] = []
        claimed: set[str] = set()
        for manager, classes in managers:
            if manager is None:
                continue
            mine = [i for i in instances if i.get("class") in classes]
            claimed.update(i["instance_id"] for i in mine)
            s, t, r = manager.converge(mine)
            started.extend(s)
            stopped.extend(t)
            reports.extend(r)

        # 沒有任何 manager 認領的實例:class 不認得,或該 provisioner 沒
        # 設定。**明確回報 failed 而不是默默忽略**——不回報的話它會永遠
        # 停在 requested,coordinator 每輪都送、exporter 每輪都丟掉,而
        # 借它的人只看到一台永遠不會好的裝置。
        for inst in instances:
            iid = inst.get("instance_id")
            if iid is None or iid in claimed or inst.get("state") == "stopping":
                continue
            log.error("no provisioner for instance %s (class=%r)",
                      iid, inst.get("class"))
            reports.append({
                "instance_id": iid, "state": "failed",
                "detail": {"error": f"no provisioner for class {inst.get('class')!r}"},
            })

        self._instance_reports = reports
        return sorted(started), sorted(stopped)

    def _run_flashes(self, flashes: list[dict]) -> list[str]:
        """執行 coordinator 派下來的 flash(§6 唯一 mediated 的能力)。

        結果跟 reclaim 一樣存起來下一輪回報——這輪的 heartbeat 已經送出去
        了,而刷一份 image 動輒幾分鐘,本來就跨輪。
        """
        if not flashes or self._flasher is None:
            return []
        done: list[str] = []
        for flash in flashes:
            try:
                outcome = self._flasher.execute(flash)
            except Exception as e:                      # noqa: BLE001
                # 刷失敗不該中斷這一輪;coordinator 收到 ok=False 會把裝置
                # 標成 maintenance,不放回去給下一個 agent。
                log.exception("flash %s crashed", flash.get("flash_id"))
                self._pending_flash_reports.append({
                    "flash_id": flash.get("flash_id"),
                    "ok": False,
                    "detail": {"error": str(e)},
                })
                continue
            self._pending_flash_reports.append(outcome.as_report())
            if outcome.ok:
                done.append(flash.get("device_id", "?"))
        return done

    def _run_reclaims(self, reclaims: list[dict]) -> list[str]:
        """執行 coordinator 派下來的強制回收動作。

        結果存起來下一輪回報——這輪的 heartbeat 已經送出去了。回收動作
        本身很慢(重開手機),而且做完之後裝置狀態才會變,下一輪回報反而
        比較準。

        **在收斂之後才做**:回收的前提是服務已經停掉,先收斂才不會對一台
        還跑著 daemon 的裝置下重開指令。
        """
        if not reclaims or self._reclaimer is None:
            return []
        done: list[str] = []
        for reclaim in reclaims:
            try:
                outcome = self._reclaimer.execute(reclaim)
            except Exception as e:                      # noqa: BLE001
                # 回收失敗不該中斷這一輪;coordinator 收到 ok=False 會把
                # 裝置留在 maintenance,不放回去給下一個 agent。
                log.exception("reclaim %s crashed", reclaim.get("reclaim_id"))
                self._pending_reclaim_reports.append({
                    "reclaim_id": reclaim.get("reclaim_id"),
                    "ok": False,
                    "detail": {"error": str(e)},
                })
                continue
            self._pending_reclaim_reports.append(outcome.as_report())
            if outcome.ok:
                done.append(reclaim.get("device_id", "?"))
        return done

    def _running_report(self) -> list[dict]:
        """目前實際跑著什麼——coordinator 據此更新 device_services。"""
        return [
            {"device_id": s.device_id, "service": s.service, "port": s.port}
            for s in self._services.running()
        ]

    def _converge(self, desired: list[DesiredService]) -> TickResult:
        """讓實際狀態往 desired 靠一步。

        停多的、起缺的。起不來的記下來但不中斷這一輪——一台裝置的問題
        不該讓其他裝置的服務也停擺;沒起成的下一輪還在 desired 裡,
        自然重試。
        """
        wanted = {d.key: d for d in desired}
        running = {(s.device_id, s.service) for s in self._services.running()}

        stopped: list[tuple[str, str]] = []
        for key in sorted(running - set(wanted)):
            if self._services.stop(*key):
                log.info("stopped %s/%s (no longer desired)", *key)
                stopped.append(key)

        started: list[tuple[str, str]] = []
        failed: list[tuple[str, str]] = []
        for key in sorted(set(wanted) - running):
            spec = wanted[key]
            try:
                svc = self._services.start(spec.device_id, spec.service, spec.identifier)
            except ServiceError as e:
                # 缺工具、daemon 秒退、識別碼不對——記下來下一輪再試。
                log.error("failed to start %s/%s: %s", *key, e)
                failed.append(key)
                continue
            log.info("started %s/%s on port %d", key[0], key[1], svc.port)
            started.append(key)

        return TickResult(
            started=started,
            stopped=stopped,
            failed=failed,
            reported=self._running_report(),
        )

    # ---------------------------------------------------------------- loop

    def run(self) -> None:
        """跑到收到停止訊號為止。退出時清掉所有子行程。"""
        try:
            while not self._stop.is_set():
                try:
                    self.tick()
                except Exception:
                    # 網路不通、coordinator 重啟中——記下來繼續跑。exporter
                    # 不該因為 coordinator 一時不在就把裝置服務全停掉,
                    # 那會踢掉正在用的 client。
                    log.exception("heartbeat tick failed; retrying next interval")
                self._stop.wait(self._interval_s)
        finally:
            self.shutdown()

    def stop(self) -> None:
        self._stop.set()

    def shutdown(self) -> None:
        """退出清理:停掉所有起過的 daemon(§4 exporter 擁有裝置的責任)。"""
        stopped = self._services.stop_all()
        if stopped:
            log.info("stopped %d service(s) on shutdown", len(stopped))
        for manager in (self._vms, self._cvds):
            if manager is None:
                continue
            # ephemeral 實例不該活得比起它的 exporter 久:留著的話
            # coordinator 重啟後沒人認得它們,而它們還吃著 RAM。
            killed = manager.stop_all()
            if killed:
                log.info("stopped %d ephemeral instance(s) on shutdown",
                         len(killed))
        self._client.close()

    def install_signal_handlers(self) -> None:
        """SIGTERM/SIGINT 走正常關閉路徑,子行程才會被收乾淨。"""
        def handler(signum, frame):  # noqa: ARG001
            log.info("received signal %s; shutting down", signum)
            self.stop()

        signal.signal(signal.SIGTERM, handler)
        signal.signal(signal.SIGINT, handler)
