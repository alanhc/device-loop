"""強制回收動作的執行(設計文件 §7 第 4 步)。

這些動作會實際影響硬體,所以全部用 fake runner——開發機上不會真的重開
任何東西。真機驗證要先取得同意(ROG 那顆 Pixel 有手工構築的分割區狀態)。
"""

from __future__ import annotations

from exporter.reclaim import ACTIONS, ReclaimExecutor

PIXEL = "38011FDJH00C9F"


def _job(action="adb-reboot", identifier=PIXEL, rid=1):
    return {"reclaim_id": rid, "device_id": "pixel8-shiba",
            "action": action, "identifier": identifier}


def test_adb_reboot_targets_the_specific_serial(runner):
    """host 上可能插著好幾顆,-s 確保打到正確的那一台。"""
    ReclaimExecutor(runner).execute(_job())
    assert runner.started[0].argv == ["adb", "-s", PIXEL, "reboot"]


def test_adb_reboot_does_not_use_a_per_device_port(runner):
    """回收發生在服務停掉之後,per-device server 已經不在了,走全域 server。"""
    ReclaimExecutor(runner).execute(_job())
    assert "-P" not in runner.started[0].argv


def test_successful_action_reports_ok(runner):
    outcome = ReclaimExecutor(runner).execute(_job())
    assert outcome.ok
    assert outcome.reclaim_id == 1
    assert outcome.detail["exit_code"] == 0


def test_action_is_waited_not_terminated(runner):
    """跟 preflight 同樣的教訓:一次性指令要等它跑完,不是砍掉它。"""
    ReclaimExecutor(runner).execute(_job())
    handle = runner.started[0]
    assert handle.waited and handle.work_done
    assert not handle.terminated


def test_failing_action_reports_the_exit_code(runner):
    original = runner.start

    def failing(argv):
        h = original(argv)
        h.exit_code = 1
        return h

    runner.start = failing
    outcome = ReclaimExecutor(runner).execute(_job())
    assert not outcome.ok
    assert outcome.detail["exit_code"] == 1


def test_unknown_action_is_refused_not_guessed(runner):
    """回收動作猜錯的代價是對錯的裝置做破壞性操作——寧可失敗。"""
    outcome = ReclaimExecutor(runner).execute(_job(action="rm-rf-everything"))
    assert not outcome.ok
    assert "unknown action" in outcome.detail["error"]
    assert runner.started == []          # 什麼都沒執行


def test_missing_identifier_is_refused(runner):
    """沒有識別碼就不知道要對誰動手,不能亂猜。"""
    outcome = ReclaimExecutor(runner).execute(_job(identifier=None))
    assert not outcome.ok
    assert runner.started == []


def test_missing_tool_is_reported_not_crashed(make_runner):
    outcome = ReclaimExecutor(make_runner(missing={"adb"})).execute(_job())
    assert not outcome.ok
    assert "not found in PATH" in outcome.detail["error"]


def test_a_hung_action_times_out_and_is_cleaned_up(runner):
    """卡住的裝置常讓指令一起卡住,不能讓收斂迴圈跟著停。"""
    original = runner.start

    def hanging(argv):
        h = original(argv)
        h.hangs = True
        return h

    runner.start = hanging
    outcome = ReclaimExecutor(runner, timeout_s=0.01).execute(_job())
    assert not outcome.ok
    assert outcome.detail["error"] == "timed out"
    assert runner.started[0].terminated      # 收掉了,沒留殭屍


def test_only_whitelisted_actions_exist():
    """白名單就是白名單——新增動作要明確加進來,不是靠字串拼接。"""
    assert set(ACTIONS) == {"adb-reboot", "ipmi-power-cycle"}


def test_ipmi_power_cycle_targets_the_bmc_address(runner):
    outcome = ReclaimExecutor(runner).execute(
        _job(action="ipmi-power-cycle", identifier="10.0.0.5")
    )
    assert outcome.ok
    assert runner.started[0].argv == [
        "ipmitool", "-H", "10.0.0.5", "chassis", "power", "cycle"
    ]
