"""Per-resource daemon 的起停與清理(設計文件 §6)。"""

from __future__ import annotations

import pytest

from exporter.services import ADB, UART, ServiceError, ServiceManager


PIXEL = "38011FDJH00C9F"
TTY = "/dev/serial/by-id/usb-FTDI_FT232R-if00-port0"


def test_adb_argv_binds_one_device_on_chosen_port():
    argv = ADB.build_argv(PIXEL, 9001)
    assert argv == ["adb", "-P", "9001", "--one-device", PIXEL,
                    "server", "nodaemon", "-a"]


def test_adb_argv_is_scoped_to_the_serial():
    """``--one-device`` 是把這個 server 跟 host 上其他裝置隔開的關鍵。"""
    argv = ADB.build_argv(PIXEL, 9001)
    assert argv[argv.index("--one-device") + 1] == PIXEL


def test_uart_argv_uses_ser2net_with_rfc2217():
    """RFC2217 才有遠端 baud/DTR/RTS;raw TCP 不行(§6 relay 原語)。"""
    argv = UART.build_argv(TTY, 9002)
    assert argv[0] == "ser2net"
    joined = " ".join(argv)
    assert "telnet(rfc2217,mode=server)" in joined
    assert "tcp,9002" in joined
    assert f"serialdev(nouucplock=true),{TTY}," in joined


def test_uart_does_not_use_socat():
    """明確擋掉「用 socat 取代 ser2net」這條路。"""
    assert "socat" not in " ".join(UART.build_argv(TTY, 9002))


def test_start_launches_daemon_and_records_port(runner, ports):
    mgr = ServiceManager(runner, port_fn=ports)
    svc = mgr.start("pixel8-shiba", "adb", PIXEL)
    assert svc.port == 9000
    assert svc.handle.argv[:3] == ["adb", "-P", "9000"]
    # kill-server 前置 + 專屬 server
    assert len(runner.started) == 2


def test_endpoint_is_host_address_and_port(runner, ports):
    mgr = ServiceManager(runner, port_fn=ports)
    svc = mgr.start("pixel8-shiba", "adb", PIXEL)
    # client 要能直接 `adb connect` 貼上去用,所以是 tailscale IP 不是 host id
    assert svc.endpoint("100.71.211.115") == "100.71.211.115:9000"


def test_start_is_idempotent_while_running(runner, ports):
    """重複請求同一個能力不會起第二個 daemon。"""
    mgr = ServiceManager(runner, port_fn=ports)
    first = mgr.start("pixel8-shiba", "adb", PIXEL)
    again = mgr.start("pixel8-shiba", "adb", PIXEL)
    assert again is first
    assert len(runner.started) == 2   # kill-server + 一個 daemon,沒有第二個


def test_start_replaces_a_dead_daemon(runner, ports):
    """殘骸不會擋住重起——supervisor 靠這個復原。"""
    mgr = ServiceManager(runner, port_fn=ports)
    first = mgr.start("pixel8-shiba", "adb", PIXEL)
    first.handle.exit_code = 1
    second = mgr.start("pixel8-shiba", "adb", PIXEL)
    assert second is not first
    assert second.port == 9001
    # 每次啟動各一次 kill-server:kill + 死掉的 + kill + 重起的
    assert len(runner.started) == 4


def test_one_device_can_run_uart_and_adb_at_once(runner, ports):
    mgr = ServiceManager(runner, port_fn=ports)
    mgr.start("board", "adb", PIXEL)
    mgr.start("board", "uart", TTY)
    assert [(s.service, s.port) for s in mgr.running()] == [("adb", 9000), ("uart", 9001)]


def test_missing_program_is_reported_not_launched(make_runner):
    mgr = ServiceManager(make_runner(missing={"ser2net"}))
    with pytest.raises(ServiceError, match="ser2net"):
        mgr.start("board", "uart", TTY)


def test_daemon_that_exits_immediately_is_an_error(runner, ports):
    """起完立刻死掉不能當成功——否則 coordinator 會拿到一個連不上的 endpoint。"""
    class InstantDeath(type(runner)):
        def start(self, argv):
            handle = super().start(argv)
            handle.exit_code = 2
            return handle

    mgr = ServiceManager(InstantDeath(), port_fn=ports)
    with pytest.raises(ServiceError, match="exited immediately"):
        mgr.start("pixel8-shiba", "adb", PIXEL)


def test_unknown_service_is_rejected(runner):
    mgr = ServiceManager(runner)
    with pytest.raises(ServiceError, match="unknown service"):
        mgr.start("board", "telepathy", PIXEL)


def test_stop_terminates_and_forgets(runner, ports):
    mgr = ServiceManager(runner, port_fn=ports)
    svc = mgr.start("pixel8-shiba", "adb", PIXEL)
    assert mgr.stop("pixel8-shiba", "adb") is True
    assert svc.handle.terminated
    assert mgr.running() == []
    assert mgr.stop("pixel8-shiba", "adb") is False


def test_stop_device_stops_every_service_of_that_device(runner, ports):
    """lease 結束時一次收乾淨。"""
    mgr = ServiceManager(runner, port_fn=ports)
    mgr.start("board", "adb", PIXEL)
    mgr.start("board", "uart", TTY)
    mgr.start("other", "adb", "OTHER")
    assert mgr.stop_device("board") == ["adb", "uart"]
    assert [s.device_id for s in mgr.running()] == ["other"]


def test_stop_all_cleans_up_on_exit(runner, ports):
    mgr = ServiceManager(runner, port_fn=ports)
    mgr.start("board", "adb", PIXEL)
    mgr.start("board", "uart", TTY)
    assert mgr.stop_all() == [("board", "adb"), ("board", "uart")]
    assert mgr.running() == []
    # preflight 是一次性指令,是被 wait 掉的不是 terminate 掉的;
    # 真正要確認清乾淨的是長命的 daemon。
    daemons = [h for h in runner.started if h.argv != ["adb", "kill-server"]]
    assert daemons and all(h.terminated for h in daemons)


def test_reap_dead_reports_daemons_that_died_on_their_own(runner, ports):
    mgr = ServiceManager(runner, port_fn=ports)
    alive = mgr.start("board", "adb", PIXEL)
    dead = mgr.start("board", "uart", TTY)
    dead.handle.exit_code = 137
    assert mgr.reap_dead() == [("board", "uart")]
    assert [s.service for s in mgr.running()] == ["adb"]
    assert alive.handle.poll() is None


# ----------------------------------------- adb kill-server preflight
# 一顆 USB 裝置只能被一個 adb server 認領。全域 server(5037)還活著時
# 專屬 server 看不到裝置——服務起得來、endpoint 也回報了,但 client
# 連進去是空的。alanhc-19 在 ROG 上用真 Pixel 8 實測到這個靜默失敗。

def test_adb_start_kills_the_global_server_first(runner, ports):
    mgr = ServiceManager(runner, port_fn=ports)
    mgr.start("pixel8-shiba", "adb", PIXEL)
    assert [h.argv for h in runner.started][0] == ["adb", "kill-server"]


def test_adb_kill_server_runs_before_the_daemon_not_after(runner, ports):
    """順序要緊:kill-server 跑在專屬 server 之後會把它自己殺掉。"""
    mgr = ServiceManager(runner, port_fn=ports)
    mgr.start("pixel8-shiba", "adb", PIXEL)
    argvs = [h.argv for h in runner.started]
    assert argvs.index(["adb", "kill-server"]) < next(
        i for i, a in enumerate(argvs) if "--one-device" in a
    )


def test_kill_server_runs_for_every_device_not_just_the_first(runner, ports):
    """第二顆裝置也要 kill 一次。

    曾經做過「一個生命週期只跑一次」的優化,但它在多裝置下會漏:全域
    server 起來之後才接上的第二顆裝置會先被它認領,preflight 不再跑的話
    新的 --one-device server 就拿到空清單——同一個靜默失敗,只是要兩顆
    裝置才觸發。ROG 之後會接 pixel-10,這情境會真的發生。
    """
    mgr = ServiceManager(runner, port_fn=ports)
    mgr.start("pixel-a", "adb", "SERIAL_A")
    mgr.start("pixel-b", "adb", "SERIAL_B")
    kills = [h for h in runner.started if h.argv == ["adb", "kill-server"]]
    assert len(kills) == 2


def test_kill_server_precedes_each_device_daemon(runner, ports):
    """順序在每一顆裝置上都要成立,不只第一顆。"""
    mgr = ServiceManager(runner, port_fn=ports)
    mgr.start("pixel-a", "adb", "SERIAL_A")
    mgr.start("pixel-b", "adb", "SERIAL_B")
    argvs = [h.argv for h in runner.started]
    assert argvs == [
        ["adb", "kill-server"],
        ["adb", "-P", "9000", "--one-device", "SERIAL_A", "server", "nodaemon", "-a"],
        ["adb", "kill-server"],
        ["adb", "-P", "9001", "--one-device", "SERIAL_B", "server", "nodaemon", "-a"],
    ]


def test_restarting_a_dead_adb_daemon_kills_the_global_server_again(runner, ports):
    """重起也要重跑 preflight——daemon 死掉這段期間全域 server 可能已經
    把裝置搶走了。"""
    mgr = ServiceManager(runner, port_fn=ports)
    first = mgr.start("pixel8-shiba", "adb", PIXEL)
    first.handle.exit_code = 1
    mgr.start("pixel8-shiba", "adb", PIXEL)
    kills = [h for h in runner.started if h.argv == ["adb", "kill-server"]]
    assert len(kills) == 2


def test_idempotent_start_does_not_rerun_preflight(runner, ports):
    """服務還活著時重複請求直接回既有的,不該多殺一次全域 server——
    那是沒必要的干擾。"""
    mgr = ServiceManager(runner, port_fn=ports)
    mgr.start("pixel8-shiba", "adb", PIXEL)
    mgr.start("pixel8-shiba", "adb", PIXEL)
    kills = [h for h in runner.started if h.argv == ["adb", "kill-server"]]
    assert len(kills) == 1


def test_uart_does_not_kill_the_adb_server(runner, ports):
    """preflight 綁在能力上,不是全域行為——起 uart 不該動到 adb。"""
    mgr = ServiceManager(runner, port_fn=ports)
    mgr.start("board", "uart", TTY)
    assert ["adb", "kill-server"] not in [h.argv for h in runner.started]


def test_kill_server_reruns_for_adb_after_only_uart_was_started(runner, ports):
    mgr = ServiceManager(runner, port_fn=ports)
    mgr.start("board", "uart", TTY)
    mgr.start("pixel8-shiba", "adb", PIXEL)
    assert ["adb", "kill-server"] in [h.argv for h in runner.started]


# --------------------------------------------------- ser2net YAML 形式
# 以下三條對應在本機用 ser2net 4.6.0 實跑出來的解析結果。

def test_uart_declares_connection_exactly_once():
    """每段 -Y 都寫 connection: 會被解析成另起一個新連線,第一個連線就
    變成「有 accepter 沒 connector」。實測錯誤訊息:
    'No connector given in connection'。"""
    ys = [a for a in UART.build_argv(TTY, 9002) if a.startswith(("connection:", "  "))]
    assert sum(y.lstrip().startswith("connection:") for y in ys) == 1


def test_uart_continuation_lines_are_indented():
    argv = UART.build_argv(TTY, 9002)
    connector = argv[argv.index("-Y", argv.index("-Y") + 1) + 1]
    assert connector.startswith("  ")
    assert "connector: serialdev" in connector


def test_uart_accepter_is_an_rfc2217_server():
    assert "telnet(rfc2217,mode=server)" in " ".join(UART.build_argv(TTY, 9002))


def test_uart_disables_uucp_lock():
    """/var/lock 的 uucp lock 檔會擋住開啟序列埠。"""
    assert "nouucplock=true" in " ".join(UART.build_argv(TTY, 9002))


def test_uart_passes_device_path_as_its_own_field():
    """路徑是獨立的逗號欄位;括號裡放的是 gensio 選項(nouucplock)。
    形式對齊 Labgrid SerialPortExport——它是實際在真硬體上跑的版本。"""
    joined = " ".join(UART.build_argv(TTY, 9002))
    assert f"serialdev(nouucplock=true),{TTY}," in joined
    assert f"serialdev({TTY})" not in joined


def test_uart_stays_in_the_foreground():
    """-d 不 daemonize、-n 不讀預設設定檔——生命週期由 exporter 管。"""
    argv = UART.build_argv(TTY, 9002)
    assert "-d" in argv and "-n" in argv


# --------------------------------------- preflight 必須等它跑完(真機 bug)
# ROG 上用真 Pixel 8 抓到:preflight 起了 kill-server 之後立刻 terminate,
# 全域 server 根本沒被殺掉(它要幾十毫秒連上 5037 要求退出)。症狀是
# per-device server 起得來、endpoint 也發布了,但 client 連進去看不到裝置。
# 本機也重現:terminate 之後 5037 還在,改成 wait 就消失。

def test_preflight_is_waited_not_terminated(runner, ports):
    mgr = ServiceManager(runner, port_fn=ports)
    mgr.start("pixel8-shiba", "adb", PIXEL)
    kill = next(h for h in runner.started if h.argv == ["adb", "kill-server"])
    assert kill.waited, "kill-server 要被 wait 到跑完"
    assert not kill.terminated, "terminate 會在它做完事之前把它砍掉"


def test_preflight_actually_completes_its_work(runner, ports):
    """直接斷言「工作真的做完了」,而不只是「有呼叫過」。

    舊測試只驗 kill-server 被 start 過,所以完全抓不到這個 bug——
    指令被送出跟指令生效是兩回事。
    """
    mgr = ServiceManager(runner, port_fn=ports)
    mgr.start("pixel8-shiba", "adb", PIXEL)
    kill = next(h for h in runner.started if h.argv == ["adb", "kill-server"])
    assert kill.work_done


def test_preflight_runs_before_the_daemon_and_completes_first(runner, ports):
    """順序 + 完成度:daemon 起來的時候 kill-server 必須已經做完。"""
    mgr = ServiceManager(runner, port_fn=ports)
    mgr.start("pixel8-shiba", "adb", PIXEL)
    argvs = [h.argv for h in runner.started]
    kill_idx = argvs.index(["adb", "kill-server"])
    daemon_idx = next(i for i, a in enumerate(argvs) if "--one-device" in a)
    assert kill_idx < daemon_idx
    assert runner.started[kill_idx].work_done


def test_a_hung_preflight_is_cleaned_up_and_does_not_block(runner, ports):
    """卡住的 preflight 要被收掉、服務照起——整個收斂迴圈不能卡在這裡。"""
    mgr = ServiceManager(runner, port_fn=ports)
    original = runner.start

    def hanging_start(argv):
        handle = original(argv)
        if argv == ["adb", "kill-server"]:
            handle.hangs = True
        return handle

    runner.start = hanging_start
    svc = mgr.start("pixel8-shiba", "adb", PIXEL)
    kill = next(h for h in runner.started if h.argv == ["adb", "kill-server"])
    assert kill.waited and kill.terminated   # 等過、逾時、收掉
    assert svc.port == 9000                  # 服務還是起來了


# ------------------------------------------- Phase 3:video 與 vnc(§6)

def test_video_streams_mjpeg_over_http(runner, ports):
    """ustreamer 把 UVC camera 包成 MJPEG——scrcpy 補不到的觀測空窗
    (fastboot 選單、boot splash、kernel panic 上螢幕的時候)。"""
    mgr = ServiceManager(runner, port_fn=ports)
    svc = mgr.start("pixel8-shiba", "video", "/dev/video0")
    argv = runner.started[-1].argv
    assert argv[0] == "ustreamer"
    assert "--device" in argv and "/dev/video0" in argv
    assert argv[argv.index("--format") + 1] == "mjpeg"
    # client 要從 tailnet 連進來,跟 adb 的 -a 同樣的理由。
    assert argv[argv.index("--host") + 1] == "0.0.0.0"
    assert svc.port == 9000


def test_vnc_relays_an_existing_server_rather_than_starting_one(runner, ports):
    """§6 的三種來源(QEMU -vnc、SBC 的 vncserver、host 桌面的 TigerVNC)
    都已經有一個在聽。exporter 自己起 X server 是完全不同的責任,而且會
    跟裝置上已經在跑的那個打架。"""
    mgr = ServiceManager(runner, port_fn=ports)
    mgr.start("milkv-jupiter", "vnc", "127.0.0.1:5900")
    argv = runner.started[-1].argv
    assert argv[0] == "socat"
    assert argv[-1] == "TCP:127.0.0.1:5900"
    # fork:看同一台裝置的兩個 viewer 是正常用法。
    assert "fork" in argv[1]


def test_video_and_vnc_need_no_preflight(runner, ports):
    """只有 adb 需要 kill-server:它是唯一會被全域 server 搶走裝置的能力。"""
    mgr = ServiceManager(runner, port_fn=ports)
    mgr.start("pixel8-shiba", "video", "/dev/video0")
    mgr.start("pixel8-shiba", "vnc", "127.0.0.1:5900")
    assert all("kill-server" not in " ".join(h.argv) for h in runner.started)


def test_scrcpy_is_not_a_host_side_service(runner, ports):
    """§6:scrcpy 疊在 adb 之上,client 端執行即可。在無頭的 exporter host
    上起 scrcpy 等於開一個沒人看得到的視窗。"""
    from exporter.services import SPECS

    assert "scrcpy" not in SPECS
    mgr = ServiceManager(runner, port_fn=ports)
    with pytest.raises(ServiceError, match="unknown service"):
        mgr.start("pixel8-shiba", "scrcpy", "38011FDJH00C9F")
