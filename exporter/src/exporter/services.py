"""Per-resource daemon:一個 lease 上的一種存取能力對應一個子行程。

Phase 1 兩種(設計文件 §6 能力表):

- **uart**:``ser2net`` 把序列埠包成 RFC2217 telnet。**不能用 socat 取代**
  ——RFC2217 才能讓 client 遠端改 baud rate、拉 DTR/RTS,SBC 進 boot mode
  或救磚靠的就是這些 serial 訊號腳,raw TCP 做不到(§6 「relay 原語」)。
- **adb**:針對單一 USB serial 起專用的
  ``adb -P <port> --one-device <serial> server nodaemon -a``,client
  ``adb connect host:port``。照抄 Labgrid ``ADBExport``:每個裝置一個
  獨立 adb server 實例,不會跟 host 上其他裝置混在一起。啟動前必須先
  ``adb kill-server``,理由見 ``ADB_PREFLIGHT``。

每種能力是一個 ``ServiceSpec``:給定識別碼與 port,組出 argv。實際啟動
一律走注入的 ``ProcessRunner``,所以這層在沒有實體裝置的機器上完全可測。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

from .proc import Handle, ProcessRunner, free_port

log = logging.getLogger(__name__)

# preflight 是短命指令,正常幾十毫秒;給足餘裕但不能無限等,
# 卡住的話整個 exporter 的收斂迴圈都會停住。
PREFLIGHT_TIMEOUT_S = 15.0


class ServiceError(Exception):
    """能力起不來——缺工具、識別碼形式不對、daemon 立刻死掉。"""


@dataclass(frozen=True)
class ServiceSpec:
    """一種存取能力的啟動方式。

    ``build_argv`` 拿 (identifier, port) 組出完整指令列;``program`` 是
    起之前要確認存在的執行檔。
    """

    name: str                                    # 'uart' | 'adb'
    program: str                                 # 'ser2net' | 'adb'
    build_argv: Callable[[str, int], list[str]]
    mediated: bool = False                       # §6:flash 才是 True
    # 這種能力第一次啟動前要先跑一次的指令(見 ADB_PREFLIGHT)。
    preflight: tuple[str, ...] | None = None
    # daemon 起來之後要對它跑的指令(見 _adb_postflight)。拿
    # (identifier, port) 組 argv;回 None 代表這個識別碼不需要。
    postflight: Callable[[str, int], list[str] | None] | None = None


DEFAULT_BAUD = "9600n81"


def _uart_argv(identifier: str, port: int, baud: str = DEFAULT_BAUD) -> list[str]:
    """ser2net 前景模式,單一連線定義。

    ``-Y`` 是把 YAML 片段接在設定檔尾端,可以給多次;不在引號內的 ``#``
    會被 ser2net 換成換行。所以**只有第一段寫 ``connection:``**,後續段落
    是縮排的續行——每段都寫 ``connection:`` 會被解析成另起一個新連線,
    第一個連線就變成「有 accepter 沒 connector」。這個錯誤在本機用
    ser2net 4.6.0 重現過:重複寫會得到
    ``No connector given in connection``,改成續行才解析得過。

    ``telnet(rfc2217,mode=server)`` 才是 RFC2217 server:client 可以遠端
    改 baud、拉 DTR/RTS。``nouucplock=true`` 避免 ``/var/lock`` 的 uucp
    lock 檔擋住開啟序列埠,寫在 gensio 名字後的括號裡(裝置路徑是後面
    獨立的逗號欄位)——形式對齊 Labgrid ``SerialPortExport``,它是實際
    在真硬體上跑的版本。baud 只是連上前的初始值,client 一連上就能改。

    **這段沒有在真實序列埠上驗證過**:本機用 ser2net 4.6.0 驗到 YAML
    解析正確(見上),但這台沒有實體序列埠,連得上、能改 baud/DTR 這
    部分要在有序列埠的機器上才驗得到。
    """
    return [
        "ser2net",
        "-d",                       # 不 daemonize,由 exporter 直接管生命週期
        "-n",                       # 不讀預設設定檔,只吃這裡給的 -Y
        "-Y", f"connection: &con01#  accepter: telnet(rfc2217,mode=server),tcp,{port}",
        "-Y", f"  connector: serialdev(nouucplock=true),{identifier},{baud},local",
    ]


def _video_argv(identifier: str, port: int) -> list[str]:
    """ustreamer 把對準裝置的 UVC camera 包成 MJPEG over HTTP(§6)。

    這是 scrcpy 補不到的觀測空窗:fastboot 選單、boot splash 卡住、kernel
    panic 上螢幕的時候 adb 根本不存在,camera 是唯一的眼睛——而 Phase 2 的
    mediated flash 正好製造大量這種時刻(§6:「video + image registry 是
    一組的」)。PiKVM 用的就是這套。

    ``identifier`` 是 ``/dev/video*``。``--device-timeout`` 讓拔掉的 camera
    不會讓 daemon 卡死;``--slowdown`` 在沒有 client 連著的時候降到 1fps,
    因為這條串流的常態是沒人在看,而 exporter host 上同時可能有好幾個。
    綁 0.0.0.0:client 要從 tailnet 連進來,跟 adb 的 ``-a`` 同樣的理由。
    """
    return [
        "ustreamer",
        "--device", identifier,
        "--host", "0.0.0.0",
        "--port", str(port),
        "--format", "mjpeg",
        "--device-timeout", "5",
        "--slowdown",
    ]


def _vnc_argv(identifier: str, port: int) -> list[str]:
    """把裝置端既有的 VNC server 轉出來(§6 vnc 那列的三種來源共用)。

    刻意**不自己起 vncserver**:三種來源(QEMU 原生 ``-vnc``、SBC 上跑的
    vncserver、exporter host 桌面的 TigerVNC)都已經有一個在聽的 VNC
    server,exporter 要做的只是把它接到一個 lease 期間才存在的 port 上。
    自己起一個 X server 是完全不同的責任,而且會跟裝置上已經在跑的那個
    打架。

    ``identifier`` 是 ``host:port`` 形式的上游位址。用 socat 而不是
    ser2net:§6 的 relay 原語說得很清楚——真序列埠才需要 RFC2217,其他
    一切 fd 類通道用 socat 包成 TCP 就好。

    ``reuseaddr`` 讓 daemon 重起時不用等 TIME_WAIT;``fork`` 讓多個
    viewer 可以同時連(看同一台裝置的兩個視窗是正常用法)。
    """
    return [
        "socat",
        f"TCP-LISTEN:{port},reuseaddr,fork",
        f"TCP:{identifier}",
    ]


def is_network_device(identifier: str) -> bool:
    """這個 adb 識別碼是網路裝置(``host:port``)還是 USB serial?

    Cuttlefish 實例是前者(``127.0.0.1:6520``),真 Pixel 是後者
    (``38011FDJH00C9F``)。兩者的接法完全不同,見 ``_adb_postflight``。
    """
    host, sep, port = identifier.rpartition(":")
    return bool(sep) and host != "" and port.isdigit()


def _adb_argv(identifier: str, port: int) -> list[str]:
    """專屬 adb server,只綁這一顆裝置。

    ``-a`` 讓它聽在所有介面(client 要從 tailnet 連進來),``nodaemon``
    讓它留在前景由 exporter 管,``--one-device`` 是把它跟 host 上其他
    裝置隔開的關鍵。
    """
    return [
        "adb", "-P", str(port), "--one-device", identifier,
        "server", "nodaemon", "-a",
    ]


def _adb_postflight(identifier: str, port: int) -> list[str] | None:
    """網路裝置要在 server 起來之後明確 ``adb connect`` 一次。

    **USB 與網路裝置的差別**:USB 裝置插著就會被 server 認領,所以
    ``--one-device <serial>`` 就夠了。網路裝置(Cuttlefish 實例是
    ``127.0.0.1:<6520+n>``)不會自己出現——``--one-device`` 只是「限定
    只准這一台」,不是「去把它接上」。少了這一步,per-device server
    起得來、endpoint 也發布得出去,但 client 連進來看到的是
    ``offline``:真機上就是這樣抓到的。

    對 USB serial 回 None(不需要,而且對 serial 跑 connect 會失敗)。
    """
    if not is_network_device(identifier):
        return None
    return ["adb", "-P", str(port), "connect", identifier]


# 一顆 USB 裝置同時只能被一個 adb server 認領。host 上的全域 server
# (port 5037,任何人跑一次 `adb devices` 就會起來)會先搶走 Pixel,
# 導致 --one-device 的專屬 server 起得來、endpoint 也回報得出去,但
# client 連進來 `adb devices` 是空的——靜默失敗,很難查。所以啟動
# adb 類服務前先無條件 kill 全域 server。Labgrid ADBExport._start()
# 同樣的理由做同樣的事。已經跑起來的 --one-device server 不受影響,
# 所以整個 exporter 生命週期只需要做一次。
# 於 ROG + 真 Pixel 8 實測確認(alanhc-19,2026-09-05)。
ADB_PREFLIGHT = ("adb", "kill-server")

UART = ServiceSpec(name="uart", program="ser2net", build_argv=_uart_argv)
ADB = ServiceSpec(name="adb", program="adb", build_argv=_adb_argv,
                  preflight=ADB_PREFLIGHT, postflight=_adb_postflight)
VIDEO = ServiceSpec(name="video", program="ustreamer", build_argv=_video_argv)
VNC = ServiceSpec(name="vnc", program="socat", build_argv=_vnc_argv)

# **scrcpy 不在這裡,而且是刻意的。** §6 能力表寫得很明白:scrcpy 疊在
# adb 連線之上,client 端執行 scrcpy 指向那條 adb 連線即可,「不需要額外
# 的 host 端服務,同一條 adb lease 授權涵蓋」。在 exporter 上起一個
# scrcpy 行程等於在無頭的 host 上開一個沒人看得到的視窗——真正要跑
# scrcpy 的是使用者的桌面。所以它是 coordinator 那邊的一個 tool
# (把 adb endpoint 包成一行可以直接貼的指令),不是一種 daemon。

SPECS: dict[str, ServiceSpec] = {s.name: s for s in (UART, ADB, VIDEO, VNC)}

# 哪種 device class 提供哪些能力。要跟 coordinator 的 store.CLASS_SERVICES
# 一致——那邊是真相來源(它算 desired),這裡是 exporter 自己的參考。
CLASS_SERVICES: dict[str, tuple[str, ...]] = {
    "android": ("adb",),
    "bbb": ("uart",),
    # ephemeral QEMU:VM 自己的 ``-vnc`` 已經在聽,socat 把它轉出來。
    "qemu-vm": ("vnc",),
    # 桌面跑得起來的 SBC:裝置端有 vncserver(§6 vnc 的第二種來源)。
    "riscv-sbc": ("vnc",),
}


@dataclass
class RunningService:
    """一個已起來的 per-resource daemon。"""

    device_id: str
    service: str
    identifier: str
    port: int
    handle: Handle

    def endpoint(self, host_address: str) -> str:
        return f"{host_address}:{self.port}"


class ServiceManager:
    """起停 per-resource daemon,並保證退出時清乾淨。

    key 是 ``(device_id, service)``——同一台裝置可以同時開 uart 跟 adb,
    但同一種能力不會重複起(重複請求回既有的那個)。
    """

    def __init__(self, runner: ProcessRunner, port_fn: Callable[[], int] = free_port):
        self._runner = runner
        self._port_fn = port_fn
        self._running: dict[tuple[str, str], RunningService] = {}

    def start(self, device_id: str, service: str, identifier: str) -> RunningService:
        key = (device_id, service)
        existing = self._running.get(key)
        if existing is not None and existing.handle.poll() is None:
            return existing            # 已經在跑,idempotent
        if existing is not None:
            del self._running[key]     # 死掉的殘骸,清掉重起

        spec = SPECS.get(service)
        if spec is None:
            raise ServiceError(f"unknown service {service!r}")
        if self._runner.which(spec.program) is None:
            raise ServiceError(
                f"{spec.program!r} not found in PATH; cannot export {service}"
            )

        self._run_preflight(spec)

        port = self._port_fn()
        handle = self._runner.start(spec.build_argv(identifier, port))
        rc = handle.poll()
        if rc is not None:
            raise ServiceError(
                f"{spec.program} for {device_id}/{service} exited immediately (rc={rc})"
            )
        svc = RunningService(device_id, service, identifier, port, handle)
        self._running[key] = svc
        self._run_postflight(spec, identifier, port)
        return svc

    def _run_postflight(self, spec: ServiceSpec, identifier: str, port: int) -> None:
        """daemon 起來之後要做的事(網路裝置的 ``adb connect``)。

        失敗不拋例外:daemon 本身已經起來了,而下一輪收斂會再跑一次
        ——把整個服務判定為失敗反而會把它停掉重起,更糟。真正連不上的話
        client 會看到 offline,而 endpoint 是最終一致的,這跟其他 pending
        狀態同一類。
        """
        if spec.postflight is None:
            return
        argv = spec.postflight(identifier, port)
        if argv is None:
            return
        handle = self._runner.start(argv)
        rc = handle.wait(timeout_s=PREFLIGHT_TIMEOUT_S)
        if rc is None:
            handle.terminate()
            log.warning("postflight %s timed out", " ".join(argv))
        elif rc != 0:
            log.warning("postflight %s exited %d", " ".join(argv), rc)

    def _run_preflight(self, spec: ServiceSpec) -> None:
        """每次啟動該能力前都跑一次前置指令(adb 是 kill-server)。

        **無條件跑,不做「一個生命週期只跑一次」的優化。** 曾經這樣寫過,
        但它在多裝置情境下會漏:全域 server 起來之後才接上的第二顆裝置,
        會先被全域 server 認領,而 preflight 已經跑過就不再跑,新的
        --one-device server 於是拿到空的裝置清單——正是 ADB_PREFLIGHT
        要修掉的那個靜默失敗,只是要兩顆裝置才觸發。ROG 之後會接
        pixel-10,這個情境會真的發生。

        重跑的代價是安全的:``adb kill-server`` 不帶 ``-P`` 只打預設的
        5037,動不到跑在其他 port 上的 --one-device server。本機實測
        確認過:5038 上的 server 在 kill-server 之後仍在 listen 且行程還
        活著。Labgrid ADBExport._start() 也是每次無條件跑。

        走 ProcessRunner 而不是直接 subprocess,測試才驗得到它有被呼叫。

        **必須 wait() 等它跑完,不能 terminate()。** 這裡原本寫的是
        「起完就 terminate,已結束就是 no-op」——那個假設是錯的:
        kill-server 要花幾十毫秒連上 5037 並要求它退出,立刻 SIGTERM 會在
        它做完之前就把它砍掉,全域 server 活得好好的。真機症狀是 per-device
        server 起得來、endpoint 也發布了,但 client 連進去看不到裝置
        (裝置還被全域 server 持有)。ROG 上用真 Pixel 8 抓到,本機也重現:
        terminate 之後 5037 還在,改成 wait 就消失。
        """
        if spec.preflight is None:
            return
        handle = self._runner.start(list(spec.preflight))
        rc = handle.wait(timeout_s=PREFLIGHT_TIMEOUT_S)
        if rc is None:
            # 卡住了:收掉並繼續。裝置可能還被全域 server 持有,但這比
            # 整個 exporter 卡在這裡好——下一輪還會再試一次。
            handle.terminate()
            log.warning("preflight %s timed out after %.0fs",
                        " ".join(spec.preflight), PREFLIGHT_TIMEOUT_S)
        elif rc != 0:
            log.warning("preflight %s exited %d", " ".join(spec.preflight), rc)

    def stop(self, device_id: str, service: str) -> bool:
        """停掉一個能力。回傳有沒有東西真的被停掉。"""
        svc = self._running.pop((device_id, service), None)
        if svc is None:
            return False
        svc.handle.terminate()
        return True

    def stop_device(self, device_id: str) -> list[str]:
        """停掉一台裝置的所有能力(lease 結束時用)。"""
        stopped = []
        for key in [k for k in self._running if k[0] == device_id]:
            if self.stop(*key):
                stopped.append(key[1])
        return sorted(stopped)

    def stop_all(self) -> list[tuple[str, str]]:
        """退出時清理全部子行程。"""
        keys = sorted(self._running)
        for key in keys:
            self.stop(*key)
        return keys

    def running(self) -> list[RunningService]:
        return [self._running[k] for k in sorted(self._running)]

    def reap_dead(self) -> list[tuple[str, str]]:
        """掃掉自己死掉的 daemon,回傳哪些不見了——supervisor 據此重起。"""
        dead = [k for k, s in self._running.items() if s.handle.poll() is not None]
        for key in dead:
            del self._running[key]
        return sorted(dead)
