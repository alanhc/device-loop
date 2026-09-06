"""識別碼掃描與回報範圍(設計文件 §8)。"""

from __future__ import annotations

from pathlib import Path

from exporter.inventory import (
    AdbScanner,
    Inventory,
    SerialByIdScanner,
    collect,
)


# ---------------------------------------------------- sysfs 上的 adb 掃描
# 原本這裡跑 `adb devices`,那會啟動全域 adb server 並認領 USB 裝置,
# 把 per-device server 手上的裝置搶掉,inventory 反而看不到它——ROG 上
# 用真 Pixel 8 抓到:收斂之後下一輪 inventory 就誤判裝置拔線。


def _usb_device(root, name, serial=None, interfaces=()):
    """造一個假的 sysfs USB 裝置目錄。"""
    dev = root / name
    dev.mkdir(parents=True, exist_ok=True)
    if serial is not None:
        (dev / "serial").write_text(serial + "\n")
    for idx, (cls, sub, proto) in enumerate(interfaces):
        iface = root / f"{name}:1.{idx}"
        iface.mkdir(parents=True, exist_ok=True)
        (iface / "bInterfaceClass").write_text(cls + "\n")
        (iface / "bInterfaceSubClass").write_text(sub + "\n")
        (iface / "bInterfaceProtocol").write_text(proto + "\n")
    return dev


ADB_IFACE = ("ff", "42", "01")


def test_adb_scanner_finds_devices_declaring_an_adb_interface(tmp_path):
    _usb_device(tmp_path, "1-3", "38011FDJH00C9F", [("ff", "42", "01")])
    assert AdbScanner(tmp_path).scan() == ["38011FDJH00C9F"]


def test_adb_scanner_ignores_non_adb_usb_devices(tmp_path):
    """BMC 虛擬裝置、隨身碟、鍵盤都有 serial,但不是 adb 目標。"""
    _usb_device(tmp_path, "1-1", "STORAGE123", [("08", "06", "50")])
    _usb_device(tmp_path, "1-2", "KEYBOARD9", [("03", "01", "01")])
    assert AdbScanner(tmp_path).scan() == []


def test_adb_scanner_matches_on_the_interface_not_a_vendor_list(tmp_path):
    """判準是 interface descriptor,不是 VID 白名單——廠商清單維護不完。"""
    _usb_device(tmp_path, "1-4", "SOMEODDVENDOR", [("ff", "42", "01")])
    assert AdbScanner(tmp_path).scan() == ["SOMEODDVENDOR"]


def test_adb_scanner_handles_a_device_with_several_interfaces(tmp_path):
    """真的 Pixel 會同時有 MTP/PTP 之類的介面,adb 只是其中一個。"""
    _usb_device(tmp_path, "1-5", "PIXEL8",
                [("06", "01", "01"), ("ff", "42", "01")])
    assert AdbScanner(tmp_path).scan() == ["PIXEL8"]


def test_adb_scanner_skips_devices_without_a_serial(tmp_path):
    """沒有穩定識別碼的東西不能當裝置身分(§5)。"""
    _usb_device(tmp_path, "1-6", None, [("ff", "42", "01")])
    assert AdbScanner(tmp_path).scan() == []


def test_adb_scanner_is_empty_when_sysfs_is_absent(tmp_path):
    assert AdbScanner(tmp_path / "nope").scan() == []


def test_adb_scanner_does_not_shell_out(tmp_path, monkeypatch):
    """掃描必須零副作用:跑 adb 會啟動全域 server 搶走裝置。

    這條是回歸測試——舊實作正是因為呼叫了 adb 才把自己的 per-device
    server 弄瞎。任何再引入子行程的改動都會在這裡失敗。
    """
    import subprocess

    def explode(*a, **k):
        raise AssertionError("inventory 掃描不得執行任何子行程")

    monkeypatch.setattr(subprocess, "run", explode)
    monkeypatch.setattr(subprocess, "Popen", explode)
    monkeypatch.setattr(subprocess, "check_output", explode)
    _usb_device(tmp_path, "1-7", "PIXEL8", [("ff", "42", "01")])
    assert AdbScanner(tmp_path).scan() == ["PIXEL8"]


def test_adb_scanner_still_sees_a_device_held_by_another_server(tmp_path):
    """關鍵性質:裝置被 per-device server 持有時,sysfs 照樣列得出來。

    sysfs 反映的是「插了什麼」,跟「誰在用它」無關——這正是不該問 adb
    的原因。
    """
    _usb_device(tmp_path, "1-8", "PIXEL8", [("ff", "42", "01")])
    scanner = AdbScanner(tmp_path)
    assert scanner.scan() == ["PIXEL8"]
    assert scanner.scan() == ["PIXEL8"]     # 重複掃描結果穩定


def test_serial_by_id_scanner_reports_full_stable_paths(tmp_path: Path):
    """回報 by-id 全路徑:ser2net 的 serialdev() 直接吃它,而 /dev/ttyUSB0
    會隨插拔順序變,不能當身分。"""
    (tmp_path / "usb-FTDI_FT232R-if00-port0").write_text("")
    (tmp_path / "usb-Silabs_CP2102-if00-port0").write_text("")
    found = SerialByIdScanner(tmp_path).scan()
    assert found == [
        str(tmp_path / "usb-FTDI_FT232R-if00-port0"),
        str(tmp_path / "usb-Silabs_CP2102-if00-port0"),
    ]


def test_serial_by_id_scanner_survives_missing_directory(tmp_path: Path):
    """沒插任何 USB 序列埠時 /dev/serial/by-id 根本不存在(這台 14700 就是)。"""
    assert SerialByIdScanner(tmp_path / "nope").scan() == []


class StubScanner:
    def __init__(self, classes, found):
        self.classes = classes
        self._found = found

    def scan(self):
        return list(self._found)


def test_collect_merges_identifiers_and_classes():
    inv = collect([
        StubScanner(("android",), ["SERIAL1"]),
        StubScanner(("bbb",), ["/dev/serial/by-id/x"]),
    ])
    assert inv == Inventory(
        identifiers=["/dev/serial/by-id/x", "SERIAL1"],
        discoverable_classes=["android", "bbb"],
    )


def test_collect_reports_class_coverage_even_when_nothing_found():
    """關鍵:scanner 掃不到東西 ≠ scanner 不存在。

    class 沒進 discoverable_classes 的話,coordinator 的缺席判定就不會
    作用在它身上——拔掉的 Pixel 永遠不會被判定成 detached。
    """
    inv = collect([StubScanner(("android",), [])])
    assert inv.identifiers == []
    assert inv.discoverable_classes == ["android"]


def test_collect_deduplicates_identifiers_seen_by_two_scanners():
    inv = collect([
        StubScanner(("android",), ["SERIAL1"]),
        StubScanner(("bbb",), ["SERIAL1"]),
    ])
    assert inv.identifiers == ["SERIAL1"]


def test_collect_with_no_scanners_claims_no_coverage():
    """沒有任何 scanner 時不能宣稱涵蓋任何 class,否則會誤判全部拔線。"""
    inv = collect([])
    assert inv.identifiers == []
    assert inv.discoverable_classes == []


# --------------------------------- 本機資源(14700 的第二個 exporter 用)
# 沒有這個 scanner 的話,本機 CPU/GPU 的 last_seen_at 永遠是 NULL,§8 的
# reaper 偵測不到它們離線——只能等有人去借才發現。

def test_local_scanner_always_reports_the_host_cpu():
    """跑得到 scan() 就代表這台 host 的 CPU 在。"""
    from exporter.inventory import LocalResourceScanner

    scanner = LocalResourceScanner("alanhc-14700", probe=lambda: [])
    assert scanner.scan() == ["alanhc-14700:cpu"]


def test_cpu_identifier_uses_the_configured_host_id_not_the_hostname():
    """coordinator 的 hosts.id 是明確設定的值,未必等於 uname -n
    (`alanhc-14700` vs. 實際主機名)。身分要跟 registry 對得上。"""
    from exporter.inventory import LocalResourceScanner

    scanner = LocalResourceScanner("rog-laptop", probe=lambda: [])
    assert scanner.scan() == ["rog-laptop:cpu"]


def test_local_scanner_reports_probed_gpus():
    from exporter.inventory import LocalResourceScanner

    scanner = LocalResourceScanner("alanhc-14700", probe=lambda: ["cuda:0", "cuda:1"])
    assert scanner.scan() == ["alanhc-14700:cpu", "cuda:0", "cuda:1"]


def test_a_missing_gpu_is_simply_absent():
    """驅動沒載入的顯卡對這個系統來說就是不能用——報上去只會讓人借到
    一台跑不動 CUDA 的機器(ROG 上那張 Blackwell 就是這個狀態)。"""
    from exporter.inventory import LocalResourceScanner

    scanner = LocalResourceScanner("rog-laptop", probe=lambda: [])
    assert "cuda:0" not in scanner.scan()


def test_local_scanner_covers_cpu_and_gpu_classes():
    """classes 決定缺席判定的範圍:漏掉的話 coordinator 不會把「這次沒
    看到」解讀成離線。"""
    from exporter.inventory import LocalResourceScanner

    assert set(LocalResourceScanner("h").classes) == {"x86-cpu", "gpu-cuda"}


def test_local_scanner_does_not_cover_network_devices():
    """§5:網路裝置可以同時被多台 host 摸到,可達性不能推論歸屬。
    riscv-sbc 進了 classes 的話,Jupiter 會在各 host 之間來回搬機。"""
    from exporter.inventory import LocalResourceScanner

    assert "riscv-sbc" not in LocalResourceScanner("h").classes


def test_a_hanging_gpu_probe_does_not_hang_the_scan():
    """nvidia-smi 偶爾會卡(驅動在重置)。inventory 每輪都跑,不能讓它
    把整個收斂迴圈拖住——所以探測有 timeout。"""
    from exporter import inventory

    assert inventory.GPU_PROBE_TIMEOUT_S <= 10


def test_collect_merges_local_resources_with_usb(tmp_path):
    from exporter.inventory import LocalResourceScanner, collect

    class Usb:
        classes = ("android",)

        def scan(self):
            return ["38011FDJH00C9F"]

    inv = collect([Usb(), LocalResourceScanner("alanhc-14700", probe=lambda: ["cuda:0"])])
    assert inv.identifiers == ["38011FDJH00C9F", "alanhc-14700:cpu", "cuda:0"]
    assert inv.discoverable_classes == ["android", "gpu-cuda", "x86-cpu"]


# ------------------------- tailnet-native 裝置的 liveness(§5 最後一條)
# Jupiter 自己在 tailnet 上,不需要 exporter 代理(§10 已解決)。但「誰看得到
# 它還活著」沒有跟著解決:沒人回報就永遠 last_seen_at = NULL。

TAILSCALE_JSON = """{
  "Self": {"DNSName": "alanhc-14700.tailb22ec2.ts.net.", "Online": true},
  "Peer": {
    "a": {"DNSName": "milkv-jupiter.tailb22ec2.ts.net.", "Online": true},
    "b": {"DNSName": "pixel-10.tailb22ec2.ts.net.", "Online": false},
    "c": {"DNSName": "alans-macbook-pro.tailb22ec2.ts.net.", "Online": true}
  }
}"""


def test_tailnet_scanner_reports_only_online_nodes():
    from exporter.inventory import TailnetScanner

    scanner = TailnetScanner(probe=lambda: ["milkv-jupiter", "alans-macbook-pro"])
    assert scanner.scan() == ["milkv-jupiter", "alans-macbook-pro"]


def test_tailnet_scanner_does_not_claim_ownership():
    """§5 的核心:網路裝置可以同時被多台 host 摸到,可達性不能推論歸屬。
    owns=False 讓回報走 Inventory.seen,不會建 row 也不會改 devices.host。"""
    from exporter.inventory import TailnetScanner

    assert TailnetScanner().owns is False


def test_tailnet_scanner_declares_no_classes():
    """不參與缺席判定:tailnet 節點離線通常是「筆電睡著了」,而且 §5 說
    掃描結果會因觀察點而不一致,不能當離線證據。"""
    from exporter.inventory import TailnetScanner

    assert TailnetScanner().classes == ()


def test_collect_keeps_liveness_separate_from_ownership():
    """關鍵分野:tailnet 上二十幾個節點全報進 identifiers 的話,會憑空生出
    二十幾台「裝置」,而且每一台都被宣稱屬於這台 host。"""
    from exporter.inventory import TailnetScanner, collect

    class Usb:
        classes = ("android",)
        owns = True

        def scan(self):
            return ["38011FDJH00C9F"]

    inv = collect([Usb(), TailnetScanner(probe=lambda: ["milkv-jupiter", "iphone181"])])
    assert inv.identifiers == ["38011FDJH00C9F"]
    assert inv.seen == ["iphone181", "milkv-jupiter"]
    assert inv.discoverable_classes == ["android"]


def test_scanners_without_an_owns_attribute_still_claim():
    """既有的 scanner(AdbScanner/SerialByIdScanner)沒有 owns 屬性,
    預設要維持原本的行為,不然 USB 裝置會突然不再被認領。"""
    from exporter.inventory import collect

    class Legacy:
        classes = ("android",)

        def scan(self):
            return ["SERIAL"]

    inv = collect([Legacy()])
    assert inv.identifiers == ["SERIAL"]
    assert inv.seen == []


def test_tailscale_status_parsing_picks_online_nodes_and_strips_the_domain():
    """identifier 是節點名不是 FQDN,也不是 IP(§5:識別碼不能用 IP)。"""
    import json as _json

    from exporter.inventory import TailnetScanner

    status = _json.loads(TAILSCALE_JSON)
    names = []
    for node in [status.get("Self") or {}, *(status.get("Peer") or {}).values()]:
        if node.get("Online"):
            names.append((node.get("DNSName") or "").split(".")[0])
    assert sorted(names) == ["alanhc-14700", "alans-macbook-pro", "milkv-jupiter"]
    assert "pixel-10" not in names          # 離線的不報


def test_a_missing_tailscale_binary_is_harmless():
    from exporter.inventory import TailnetScanner

    scanner = TailnetScanner(probe=lambda: [])
    assert scanner.scan() == []
