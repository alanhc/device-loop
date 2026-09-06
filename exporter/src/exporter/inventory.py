"""掃出本機看得到的穩定識別碼(設計文件 §8)。

Coordinator 拿識別碼比對 registry 做 diff,所以這裡只回報**識別碼**,
不回報「插在哪個 port」——身分綁識別碼,不綁拓樸位置。

兩個來源對應 Phase 1 的兩種能力,**都是純觀測、零副作用**:

- sysfs(``/sys/bus/usb/devices``)裡宣告 adb interface 的裝置 serial。
  不用 ``adb devices``——那會啟動全域 adb server 並認領 USB 裝置,把
  per-device server 的裝置搶掉(見 AdbScanner)。
- ``/dev/serial/by-id/`` 的 symlink 名(USB 序列轉接器的穩定路徑)。

**掃描不能有副作用**是這個模組的核心約束:它每個 heartbeat 週期都會跑,
任何會搶佔資源的做法都會跟自己起的服務打架。

同時回報 ``discoverable_classes``,告訴 coordinator 這次掃描涵蓋哪些
class——USB 掃描列不出 SSH 上的 SBC 跟本機 CPU,不講清楚的話它們會被
誤判成拔線(coordinator §8 的缺席判定只作用在範圍內)。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

# nvidia-smi 偶爾會卡(驅動在重置、GPU 掛了)。inventory 每輪都跑,
# 不能讓它把整個收斂迴圈拖住。
GPU_PROBE_TIMEOUT_S = 5.0
# tailscale status 要問協調伺服器,網路不好時會慢。
TAILNET_PROBE_TIMEOUT_S = 10.0

SERIAL_BY_ID = Path("/dev/serial/by-id")


class Scanner(Protocol):
    """一種識別碼來源。測試注入假的,正式用真掃描。"""

    @property
    def classes(self) -> tuple[str, ...]:
        """這個 scanner 涵蓋哪些 device class。"""
        ...

    def scan(self) -> list[str]: ...

    # 可選,預設 True:掃到的東西算不算「插在這台 host 上」。
    # False 代表這個來源只證明裝置**活著**(tailnet 節點),不證明它歸誰
    # ——回報走 Inventory.seen,不會建 row 也不會改 devices.host(§5)。
    # owns: bool


# ADB 的 USB interface descriptor(Google 定義,見 AOSP
# system/core/adb/usb_descriptors 與 udev 規則的慣例):
# class 0xff(vendor specific)/ subclass 0x42 / protocol 0x01。
# 這是「這顆 USB 裝置說得出 adb」的判準,不用問 adb 也不用查 VID 白名單
# (VID 每家廠商都不同,維護不完)。
ADB_INTERFACE = ("ff", "42", "01")
USB_DEVICES = Path("/sys/bus/usb/devices")


class AdbScanner:
    """從 sysfs 直接讀 USB serial,**不呼叫 adb**。

    原本這裡跑 ``adb devices``,那是錯的:那個指令會**啟動全域 adb
    server**,而全域 server 會認領 USB 裝置。時序上的後果是——收斂讓
    per-device server 拿到裝置之後,下一輪 inventory 又跑 ``adb devices``
    起了全域 server,全域看到空清單(裝置在 per-device server 手上),
    inventory 就判定裝置不見了,回報 detached,lease 進行中的裝置被誤判
    拔線。ROG 上用真 Pixel 8 抓到。

    改成讀 sysfs:exporter 該從作業系統知道插了什麼,而不是去問一個會
    搶走資源的工具。這樣掃描是純觀測、零副作用,跟 per-device server
    完全不衝突——裝置被誰持有都照樣列得出來。

    判準是 ADB interface descriptor 而不是 VID 白名單:任何宣告
    ``ff/42/01`` 介面的裝置就是 adb 目標,不用維護廠商清單。
    """

    classes = ("android",)

    def __init__(self, root: Path = USB_DEVICES):
        self._root = root

    def scan(self) -> list[str]:
        if not self._root.is_dir():
            return []
        found = []
        for dev in sorted(self._root.iterdir()):
            serial = _read(dev / "serial")
            if not serial or not _has_adb_interface(dev):
                continue
            found.append(serial)
        return sorted(set(found))


def _read(path: Path) -> str | None:
    try:
        return path.read_text().strip() or None
    except OSError:
        return None


def _has_adb_interface(dev: Path) -> bool:
    """這顆裝置有沒有 adb 的 interface descriptor。

    Interface 目錄是 ``<dev>:<config>.<iface>``,掛在裝置目錄底下。
    """
    for iface in dev.parent.glob(f"{dev.name}:*"):
        triple = (
            _read(iface / "bInterfaceClass"),
            _read(iface / "bInterfaceSubClass"),
            _read(iface / "bInterfaceProtocol"),
        )
        if triple == ADB_INTERFACE:
            return True
    return False


class SerialByIdScanner:
    """``/dev/serial/by-id/`` 下的 symlink——USB 序列埠的穩定名字。

    回報完整路徑,因為 ser2net 的 ``serialdev()`` 直接吃它;``/dev/ttyUSB0``
    這種號碼會隨插拔順序變,不能當身分。
    """

    classes = ("bbb",)

    def __init__(self, root: Path = SERIAL_BY_ID):
        self._root = root

    def scan(self) -> list[str]:
        if not self._root.is_dir():
            return []
        return sorted(str(p) for p in self._root.iterdir())


class LocalResourceScanner:
    """這台 host **自己身上**的資源:本機 CPU、插在這台的 GPU。

    為什麼這也算 inventory:§8 的 reaper 靠 ``last_seen_at`` 判定裝置是否
    還在,而沒有任何 scanner 回報的裝置永遠停在 ``NULL``——那些裝置的
    offline 狀態**永遠不會被偵測到**,只能等有人去借才發現。14700 上的
    CPU 與 GPU 正是這種:它們不在 USB 上,Phase 1 的兩個 scanner 都看不到。

    為什麼這**不**違反 §5「誰看得到不能決定誰擁有」:那條講的是**網路**
    裝置——Jupiter 可以同時被三台 host 摸到,所以可達性不能推論歸屬。
    本機資源沒有這個問題,前提跟 USB 一樣成立:插在我身上的 GPU 只有我
    用得到,不可能同時出現在另一台 host 的掃描裡。所以它可以參與缺席
    判定,而網路掃描不行。

    identifier 用 ``<host>:cpu`` 這種形式(schema 註解裡就是這樣寫的),
    由建構時傳入的 host id 組出來——不從本機主機名推論,因為 coordinator
    的 ``hosts.id`` 是明確設定的值,兩者未必一樣(``alanhc-14700`` vs.
    ``uname -n``)。

    GPU 走 ``nvidia-smi`` 探測而不是假設它在:顯卡可能被拔掉、驅動可能
    沒載入(ROG 上那張 Blackwell 就是驅動未裝的狀態)。**探測必須零副
    作用**——``--query-gpu`` 只讀狀態,不碰任何 compute context,跟
    ``AdbScanner`` 不呼叫 ``adb`` 是同一條規矩。
    """

    classes = ("x86-cpu", "gpu-cuda")

    def __init__(self, host_id: str, probe: "Callable[[], list[str]] | None" = None):
        self._host_id = host_id
        # 注入點是**探測函式**而不是 ProcessRunner:這裡要的是一次性的
        # 「讀完就結束」,不是 daemon 的生命週期管理,用 ProcessRunner 只是
        # 為了共用型別而繞遠路。測試直接給一個回傳清單的函式。
        self._probe = probe or self._nvidia_smi

    def scan(self) -> list[str]:
        found = [f"{self._host_id}:cpu"]        # 跑得到這行就代表 CPU 在
        found.extend(self._gpus())
        return found

    def _gpus(self) -> list[str]:
        return self._probe()

    def _nvidia_smi(self) -> list[str]:
        """在場的 CUDA 裝置,回報成 ``cuda:<index>``(對齊 seed 的寫法)。

        探測不到就回空的:驅動沒載入的顯卡對這個系統來說就是不能用,
        報上去只會讓人借到一台跑不動 CUDA 的機器。
        """
        if shutil.which("nvidia-smi") is None:
            return []
        try:
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=index", "--format=csv,noheader"],
                capture_output=True, text=True, timeout=GPU_PROBE_TIMEOUT_S,
            )
        except (OSError, subprocess.SubprocessError):
            return []
        if out.returncode != 0:
            return []
        return [f"cuda:{line.strip()}" for line in out.stdout.splitlines()
                if line.strip()]


class TailnetScanner:
    """tailnet 上線的節點——給 **tailnet-native 裝置**用(§5 最後一條)。

    §10 已解決的那條:Jupiter 自己加入了 tailnet,不需要任何 exporter 代理,
    「誰擁有它」的問題直接消失。但「誰**看得到**它還活著」沒有跟著解決:
    沒有任何 scanner 回報它,``last_seen_at`` 就永遠是 NULL,§8 的 reaper
    偵測不到它離線。

    為什麼這**不是** §5 反對的那種網路掃描:那條反對的是**可達性推論**
    ——ping 得到就說「它在我這」,而 Jupiter 可以同時被三台 host 摸到,
    於是每輪 heartbeat 產生一筆 device_moved。這裡用的是
    ``tailscale status`` 的 ``Online`` 旗標,那是**協調伺服器的判斷**,
    不是本機的觀測:**每個節點看到的是同一份答案**,所以兩台 exporter
    同時跑這個 scanner 也不會互相搶著宣稱擁有權。

    identifier 用 tailnet 節點名(``DNSName`` 的第一段)而不是 IP,因為
    §5 明講識別碼不能用 IP——DHCP/tailnet 換位址就變成一台新裝置。

    **回報走 ``seen`` 而不是 ``identifiers``。** 這是關鍵的分野:
    ``identifiers`` 的語意是「插在我身上的東西」,coordinator 收到沒見過
    的就建一筆 unregistered row **並把 host 填成回報者**——tailnet 上有
    二十幾個節點(iPhone、筆電、別人的機器),全報上去會憑空生出二十幾台
    「裝置」,而且每一台都被宣稱屬於這台 host。那正是 §5 反對的
    「可達性決定擁有權」。

    ``seen`` 只做一件事:**把已知裝置的 last_seen_at 往前推**。不建 row、
    不改 host、不參與缺席判定(tailnet 節點離線通常是「筆電睡著了」而不是
    「裝置沒了」,而且 §5 說掃描結果會因觀察點而不一致,不能當離線證據)。
    """

    classes: tuple[str, ...] = ()
    # 回報走 Inventory.seen:只更新 last_seen_at,不建 row、不宣稱擁有。
    owns = False

    def __init__(self, probe: "Callable[[], list[str]] | None" = None):
        self._probe = probe or self._tailscale_status

    def scan(self) -> list[str]:
        return self._probe()

    def _tailscale_status(self) -> list[str]:
        if shutil.which("tailscale") is None:
            return []
        try:
            out = subprocess.run(
                ["tailscale", "status", "--json"],
                capture_output=True, text=True, timeout=TAILNET_PROBE_TIMEOUT_S,
            )
        except (OSError, subprocess.SubprocessError):
            return []
        if out.returncode != 0:
            return []
        try:
            status = json.loads(out.stdout)
        except ValueError:
            return []

        names: list[str] = []
        # 自己也算:跑著 exporter 的這台一定在線上,而它本身可能就是一台
        # 裝置(macbook-m1pro 的 device 即 host)。
        for node in [status.get("Self") or {}, *(status.get("Peer") or {}).values()]:
            if not node.get("Online"):
                continue
            name = (node.get("DNSName") or "").split(".")[0]
            if name:
                names.append(name)
        return names


@dataclass
class Inventory:
    identifiers: list[str] = field(default_factory=list)
    discoverable_classes: list[str] = field(default_factory=list)
    # 「我看得到它還活著」但**不宣稱擁有**的識別碼(tailnet-native 裝置)。
    # 跟 identifiers 分開是刻意的,見 TailnetScanner 的 docstring。
    seen: list[str] = field(default_factory=list)


def collect(scanners: list[Scanner]) -> Inventory:
    """跑過所有 scanner,合併成一次 heartbeat 的回報內容。

    某個 scanner 掃不到東西(沒插裝置)跟它不存在是兩回事:只要 scanner
    在,它的 class 就進 ``discoverable_classes``,coordinator 才能把
    「這次沒看到」正確解讀成拔線。
    """
    identifiers: list[str] = []
    classes: list[str] = []
    seen: list[str] = []
    for scanner in scanners:
        # `owns = False` 的 scanner 回報的是 liveness,不是擁有權:它掃到的
        # 東西不該建 row、不該被宣稱屬於這台 host(§5)。
        if getattr(scanner, "owns", True):
            identifiers.extend(scanner.scan())
        else:
            seen.extend(scanner.scan())
        for cls in scanner.classes:
            if cls not in classes:
                classes.append(cls)
    return Inventory(
        identifiers=sorted(set(identifiers)),
        discoverable_classes=sorted(classes),
        seen=sorted(set(seen)),
    )
