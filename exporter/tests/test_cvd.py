"""Cuttlefish 池:ephemeral 的第二種 provisioner(設計文件 §12)。

§12 的硬體分層:Android userspace 的 `any` 工作導去 AVD 平行跑,真機
Pixel 只留 kernel/thermal/perf。這裡測的是 Cuttlefish 特有的那幾件事——
實例編號是稀缺資源、實例活得比子行程久、對外是一台 adb 裝置。
"""

from __future__ import annotations

import pytest

from exporter.cvd import (
    ADB_PORT_BASE,
    MAX_INSTANCES,
    CvdError,
    CvdManager,
    adb_address,
    create_argv,
    parse_fleet,
    parse_instance_numbers,
    remove_argv,
    stop_argv,
)

SPEC = {"host_path": "/home/alanhc/cf", "product_path": "/home/alanhc/cf",
        "memory_mb": 4096, "cpus": 4}


def _desired(instance_id="cf-pool-0001", state="requested", spec=None):
    return {"instance_id": instance_id, "template_id": "cf-pool",
            "state": state, "class": "cuttlefish-vm",
            "spec": SPEC if spec is None else spec}


class FleetRunner:
    """包一層 FakeRunner,讓 cvd fleet 回得出東西。"""

    def __init__(self, inner, groups=()):
        self._inner = inner
        self.started = inner.started
        self.groups = set(groups)

    def start(self, argv):
        return self._inner.start(argv)

    def which(self, program):
        return self._inner.which(program)

    def output(self, argv, timeout_s=30.0):
        # 形狀照真的 cvd fleet:group 底下有 instances,每台帶 status。
        # 假輸出偷懶只給 group_name 的話,測到的是一個不存在的格式。
        return '{"groups": [%s]}' % ",".join(
            '{"group_name": "%s", "instances": [{"status": "Running"}]}' % g
            for g in sorted(self.groups)
        )


# ----------------------------------------------------------------- argv

def test_instance_number_is_explicit_never_left_to_cvd():
    """編號決定網橋與 adb port,兩台同號會搶同一組網橋。"""
    argv = create_argv(SPEC, 3, "dl_cf_pool_0001")
    assert argv[argv.index("--base_instance_num") + 1] == "3"
    assert argv[argv.index("--num_instances") + 1] == "1"


def test_daemon_is_required_so_the_tick_does_not_block():
    """沒有 --daemon 的話指令會前景跑到實例關掉為止,收斂迴圈卡死。"""
    assert "--daemon" in create_argv(SPEC, 1, "g")


def test_usage_stats_prompt_is_answered_up_front():
    """沒給的話 cvd 會在 terminal 問 y/n,而 exporter 沒有 terminal
    ——指令會卡到逾時。"""
    argv = create_argv(SPEC, 1, "g")
    assert argv[argv.index("--report_anonymous_usage_stats") + 1] == "n"


def test_sandbox_is_off_by_default_so_it_runs_under_systemd():
    """**實測出來的關鍵旗標**:crosvm 預設把每個虛擬裝置 fork 成獨立的
    jailed 行程,而那層 minijail 在 systemd user service 裡建不起來
    ("failed to create proxy device: Failed to configure tube")。互動
    shell 裡不會發生——這就是「手動跑得起來、服務跑不起來」的真正原因。"""
    assert "--enable_sandbox=false" in create_argv(SPEC, 1, "g")


def test_sandbox_can_be_turned_back_on_per_template():
    """關掉的是 crosvm 對 guest 的隔離。跑不受信任的 image 時要能打開,
    所以它是 spec 的欄位而不是寫死。"""
    argv = create_argv({**SPEC, "sandbox": True}, 1, "g")
    assert "--enable_sandbox=true" in argv


def test_image_paths_come_from_the_spec():
    argv = create_argv(SPEC, 1, "g")
    assert argv[argv.index("--host_path") + 1] == "/home/alanhc/cf"
    assert argv[argv.index("--product_path") + 1] == "/home/alanhc/cf"


def test_a_spec_without_images_is_refused():
    with pytest.raises(CvdError, match="host_path and product_path"):
        create_argv({"memory_mb": 2048}, 1, "g")


def test_stopping_also_removes_so_the_number_is_freed(runner):
    """**真機抓到的**:`cvd stop` 不釋放 instance number——停掉的 group 仍
    留在 cvd 的 database 裡佔著編號,下一次用同一號 create 會吐
    `New instance conflicts with existing instance`(exit 255)。

    編號是稀缺資源(10 組預配網橋),不 remove 的話池子會一直漏,借滿十次
    之後就再也生不出實例。"""
    fleet = FleetRunner(runner)
    mgr = CvdManager(fleet, fleet_fn=lambda: fleet.groups)
    mgr.converge([_desired()])
    mgr.converge([_desired(state="stopping")])
    argvs = [h.argv for h in runner.started]
    assert ["cvd", "--group_name", "dl_cf_pool_0001", "stop"] in argvs
    assert ["cvd", "--group_name", "dl_cf_pool_0001", "remove"] in argvs


def test_remove_argv_targets_the_group():
    assert remove_argv("dl_x") == ["cvd", "--group_name", "dl_x", "remove"]


def test_numbers_held_by_stopped_groups_are_not_reused(runner):
    """cvd 的 instance database 是跨行程、跨重啟的:別的東西(手動測試、
    上一輪的 exporter)留下的 group 一樣佔著編號。不問 cvd 的話會拿到
    一個已經被佔的號,create 直接失敗。"""
    class HeldRunner(FleetRunner):
        def output(self, argv, timeout_s=30.0):
            # 一個已經停掉、但還沒 remove 的 group 佔著 1 號。
            return ('{"groups": [{"group_name": "dl_stale", "instances": '
                    '[{"instance_name": "1", "status": "Stopped"}]}]}')

    mgr = CvdManager(HeldRunner(runner), fleet_fn=lambda: set())
    mgr.converge([_desired()])
    assert [vm.instance_num for vm in mgr.running()] == [2]


def test_instance_numbers_count_stopped_groups_too():
    """跟 parse_fleet 正好相反:那個只算 Running(判斷要不要重生),
    這個連 Stopped 都要算(編號還被佔著)。"""
    raw = ('{"groups": [{"group_name": "g", "instances": ['
           '{"instance_name": "1", "status": "Stopped"},'
           '{"instance_name": "3", "status": "Running"}]}]}')
    assert parse_instance_numbers(raw) == {1, 3}
    assert parse_fleet(raw) == {"g"}


def test_stop_targets_the_group_not_the_instance_number():
    """create 是以 group 為單位建的,停的時候用同一個單位才不會留殘骸。"""
    assert stop_argv("dl_cf_pool_0001") == [
        "cvd", "--group_name", "dl_cf_pool_0001", "stop"
    ]


# ------------------------------------------------------------- adb 位址

def test_adb_port_follows_the_instance_number():
    """Cuttlefish 的規則:第 n 台(1-based)掛在 6520 + n - 1。"""
    assert adb_address(1) == f"127.0.0.1:{ADB_PORT_BASE}"
    assert adb_address(3) == f"127.0.0.1:{ADB_PORT_BASE + 2}"


def test_running_instance_reports_its_adb_address(runner):
    """coordinator 據此算出 adb 服務要連哪裡——port 要等實例真的生出來
    才知道,跟 endpoint 一樣是最終一致的。"""
    mgr = CvdManager(FleetRunner(runner), fleet_fn=lambda: set())
    _, _, reports = mgr.converge([_desired()])
    assert reports[0]["state"] == "running"
    assert reports[0]["detail"]["adb_identifier"] == f"127.0.0.1:{ADB_PORT_BASE}"


# ------------------------------------------------------------- converge

def test_requested_instance_is_created(runner):
    mgr = CvdManager(FleetRunner(runner), fleet_fn=lambda: set())
    started, stopped, _ = mgr.converge([_desired()])
    assert started == ["cf-pool-0001"] and stopped == []
    assert runner.started[-1].argv[0] == "cvd"


def test_liveness_comes_from_cvd_fleet_not_the_subprocess(runner):
    """cvd 是 client/server:`cvd create` 很快就返回,實例歸常駐 server 管。
    用子行程判斷的話每輪都會以為它死了然後重生一台。"""
    fleet = FleetRunner(runner)
    mgr = CvdManager(fleet, fleet_fn=lambda: fleet.groups)
    mgr.converge([_desired()])
    # 子行程早就結束了(create 返回了),但實例在 fleet 上活著。
    fleet.groups.add("dl_cf_pool_0001")
    before = len(runner.started)
    started, _, reports = mgr.converge([_desired(state="running")])
    assert started == []
    assert len(runner.started) == before        # 沒有重生
    assert reports[0]["state"] == "running"


def test_an_instance_missing_from_the_fleet_is_recreated(runner):
    """實例真的沒了(host 重開、被人手動 stop)——這才該重生。"""
    fleet = FleetRunner(runner)
    mgr = CvdManager(fleet, fleet_fn=lambda: fleet.groups)
    mgr.converge([_desired()])
    started, _, _ = mgr.converge([_desired(state="running")])   # fleet 是空的
    assert started == ["cf-pool-0001"]


def test_stopping_instance_is_stopped_and_reported_gone(runner):
    fleet = FleetRunner(runner)
    mgr = CvdManager(fleet, fleet_fn=lambda: fleet.groups)
    mgr.converge([_desired()])
    started, stopped, reports = mgr.converge([_desired(state="stopping")])
    assert stopped == ["cf-pool-0001"]
    assert reports == [{"instance_id": "cf-pool-0001", "state": "gone",
                        "detail": {"stopped_by": "exporter"}}]
    # stop 之後還要 remove(見 test_stopping_also_removes_so_the_number_is_freed)
    assert [h.argv for h in runner.started][-2:] == [
        ["cvd", "--group_name", "dl_cf_pool_0001", "stop"],
        ["cvd", "--group_name", "dl_cf_pool_0001", "remove"],
    ]


def test_a_stopping_instance_never_started_is_still_reported_gone(runner):
    """cvd server 是常駐的,實例活得比 exporter 久:重啟後 coordinator
    還記得它。不回報 gone 的話它永遠卡在 stopping。"""
    mgr = CvdManager(FleetRunner(runner), fleet_fn=lambda: set())
    _, stopped, reports = mgr.converge([_desired(state="stopping")])
    assert stopped == []
    assert reports[0]["state"] == "gone"


# --------------------------------------------------- 編號是稀缺資源

def test_instance_numbers_are_unique_within_the_pool(runner):
    mgr = CvdManager(FleetRunner(runner), fleet_fn=lambda: set())
    mgr.converge([_desired("cf-pool-0001"), _desired("cf-pool-0002")])
    nums = sorted(vm.instance_num for vm in mgr.running())
    assert nums == [1, 2]


def test_a_freed_number_is_reused(runner):
    """編號是稀缺資源(14700 上只有 10 組預配網橋),不能用過就丟。"""
    fleet = FleetRunner(runner)
    mgr = CvdManager(fleet, fleet_fn=lambda: fleet.groups)
    mgr.converge([_desired("cf-pool-0001")])
    mgr.converge([_desired("cf-pool-0001", state="stopping")])
    mgr.converge([_desired("cf-pool-0002")])
    assert [vm.instance_num for vm in mgr.running()] == [1]


def test_the_pool_refuses_to_oversubscribe_the_bridges(runner):
    """滿了要明確報錯,不要生一台去搶別人的網橋——兩台都不會正常。"""
    mgr = CvdManager(FleetRunner(runner), fleet_fn=lambda: set())
    desired = [_desired(f"cf-pool-{i:04d}") for i in range(1, MAX_INSTANCES + 2)]
    started, _, reports = mgr.converge(desired)
    assert len(started) == MAX_INSTANCES
    failed = [r for r in reports if r["state"] == "failed"]
    assert len(failed) == 1
    assert "pool is full" in failed[0]["detail"]["error"]


# ------------------------------------------------------------ 失敗路徑

def test_missing_cvd_is_reported_not_crashed(make_runner):
    mgr = CvdManager(FleetRunner(make_runner(missing={"cvd"})),
                     fleet_fn=lambda: set())
    _, _, reports = mgr.converge([_desired()])
    assert reports[0]["state"] == "failed"
    assert "not found in PATH" in reports[0]["detail"]["error"]


def test_a_failed_create_cleans_up_so_the_number_is_not_leaked(runner):
    """**真機抓到的連環爆**:失敗的 group 一樣留在 cvd database 裡佔著
    編號。第一次失敗沒清 → 下一輪拿同一號重試 → `New instance conflicts
    with existing instance`(exit 255)→ 那個編號永遠卡住,重試永遠不會
    成功。"""
    class Failing(FleetRunner):
        def start(self, argv):
            handle = self._inner.start(argv)
            if "create" in argv:
                handle.exit_code = 255
            return handle

    mgr = CvdManager(Failing(runner), fleet_fn=lambda: set())
    mgr.converge([_desired()])
    argvs = [h.argv for h in runner.started]
    assert ["cvd", "--group_name", "dl_cf_pool_0001", "remove"] in argvs


def test_a_timed_out_create_also_cleans_up(runner):
    """逾時的 create 同樣可能已經在 database 裡建了 group。"""
    class Hanging(FleetRunner):
        def start(self, argv):
            handle = self._inner.start(argv)
            if "create" in argv:
                handle.hangs = True
            return handle

    mgr = CvdManager(Hanging(runner), fleet_fn=lambda: set())
    mgr.converge([_desired()])
    argvs = [h.argv for h in runner.started]
    assert ["cvd", "--group_name", "dl_cf_pool_0001", "remove"] in argvs


def test_a_nonzero_create_is_a_failure(runner):
    class Failing(FleetRunner):
        def start(self, argv):
            handle = self._inner.start(argv)
            handle.exit_code = 1
            return handle

    mgr = CvdManager(Failing(runner), fleet_fn=lambda: set())
    _, _, reports = mgr.converge([_desired()])
    assert reports[0]["state"] == "failed"
    assert "exited 1" in reports[0]["detail"]["error"]


def test_a_hung_create_times_out(runner):
    class Hanging(FleetRunner):
        def start(self, argv):
            handle = self._inner.start(argv)
            handle.hangs = True
            return handle

    mgr = CvdManager(Hanging(runner), fleet_fn=lambda: set())
    _, _, reports = mgr.converge([_desired()])
    assert reports[0]["state"] == "failed"
    assert "timed out" in reports[0]["detail"]["error"]
    assert runner.started[-1].terminated


def test_a_failing_fleet_query_keeps_known_instances(runner):
    """問不到就當「不知道」而不是「都不在」——後者會重生一整池已經在跑的
    實例,每台都吃好幾 GB RAM。"""
    def boom():
        raise CvdError("cvd fleet failed")

    fleet = FleetRunner(runner)
    mgr = CvdManager(fleet, fleet_fn=boom)
    mgr.converge([_desired()])
    before = len(runner.started)
    started, _, _ = mgr.converge([_desired(state="running")])
    assert started == []
    assert len(runner.started) == before


def test_stop_all_leaves_nothing_running(runner):
    mgr = CvdManager(FleetRunner(runner), fleet_fn=lambda: set())
    mgr.converge([_desired("cf-pool-0001"), _desired("cf-pool-0002")])
    assert mgr.stop_all() == ["cf-pool-0001", "cf-pool-0002"]
    assert mgr.running() == []


# --------------------------------------------------------------- fleet

def test_fleet_output_skips_the_log_line_cvd_prints_first():
    """cvd 1.32.0 會在 JSON 前面印一行 log,直接 json.loads 整份會炸。
    (實測過真的 cvd 輸出。)"""
    raw = (
        "09-06 14:27:42.584 262191 262191 I cvd : main.cc:163 version: 1.32.0\n"
        '{"groups": [{"group_name": "dl_cf_pool_0001",'
        ' "instances": [{"instance_name": "1", "status": "Running"}]}]}'
    )
    assert parse_fleet(raw) == {"dl_cf_pool_0001"}


def test_a_stopped_group_still_in_the_fleet_is_not_running():
    """**真機抓到的**:`cvd stop` 之後 group 還留在 fleet 裡,只是 instance
    的 status 變成 "Stopped"。只看 group 在不在的話,停掉的實例會永遠被
    當成活著——exporter 不會重生它,借它的人拿到一台開不起來的裝置。
    這份輸入是 cvd 1.32.0 實跑後複製的。"""
    raw = """{
        "groups": [{
            "group_name": "dl_smoke_0001",
            "instances": [{"instance_name": "1", "status": "Stopped"}]
        }]
    }"""
    assert parse_fleet(raw) == set()


def test_a_running_group_is_detected():
    """對照組:同一份結構、status 是 Running(實測輸出的形狀)。"""
    raw = """{
        "groups": [{
            "group_name": "dl_smoke_0001",
            "instances": [{"instance_name": "1", "status": "Running",
                           "adb_port": 6520}]
        }]
    }"""
    assert parse_fleet(raw) == {"dl_smoke_0001"}


def test_an_empty_fleet_parses_to_nothing():
    assert parse_fleet('{"groups": []}') == set()


def test_garbage_fleet_output_is_not_fatal():
    assert parse_fleet("not json at all") == set()
