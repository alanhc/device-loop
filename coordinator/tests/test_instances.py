"""Ephemeral VM 池(設計文件 §10 的裁決 + §12)。

§10 的開放問題選了「沿用同一張 devices 表」那條:template 是一筆 row,
lease 時 spawn 出來的實例是另一筆。這裡測的是那個選擇的後果——實例要能
被 lease/events/device_services 這三套既有機制當成一般裝置對待,而且
用完真的要消失。
"""

from __future__ import annotations

import pytest

from coordinator import store

OWNER = "alice"
TEMPLATE = "vm-pool-x86"
HOST = "alanhc-14700"


@pytest.fixture()
def template(conn, clock):
    conn.execute(
        "INSERT INTO devices (id, class, control, identifier, provisioning, host, "
        "tags, state) VALUES (?, 'qemu-template', 'qemu-spawn', ?, 'ephemeral', ?, "
        "?, 'free')",
        (TEMPLATE, f"{TEMPLATE}:template", HOST,
         '{"image":"/srv/vm/debian.qcow2","arch":"x86_64","accel":"kvm"}'),
    )
    conn.commit()
    return TEMPLATE


def _instances(conn):
    return conn.execute(
        "SELECT * FROM vm_instances ORDER BY id"
    ).fetchall()


# ------------------------------------------------------- lease 時才生出來

def test_leasing_a_template_spawns_an_instance(conn, clock, template):
    """借 template 等於「給我一台這種 VM」。"""
    lease = store.reserve(conn, template, OWNER, 600, now_fn=clock)
    rows = _instances(conn)
    assert len(rows) == 1
    assert rows[0]["template_id"] == template
    assert rows[0]["lease_id"] == lease["id"]
    assert rows[0]["state"] == "requested"


def test_the_instance_is_a_device_in_its_own_right(conn, clock, template):
    """lease/events/device_services 全部以 device_id 為軸,實例不進表的話
    這三套機制都要長出「如果是 VM 的話……」的分支。"""
    store.reserve(conn, template, OWNER, 600, now_fn=clock)
    instance = _instances(conn)[0]["id"]
    dev = conn.execute(
        "SELECT * FROM devices WHERE id = ?", (instance,)
    ).fetchone()
    assert dev["class"] == store.VM_INSTANCE_CLASS
    assert dev["provisioning"] == "ephemeral"
    assert dev["host"] == HOST


def test_instances_inherit_the_template_spec(conn, clock, template):
    """VM 要怎麼開(image/arch/accel)是 template 的性質。"""
    store.reserve(conn, template, OWNER, 600, now_fn=clock)
    spec = store.desired_instances(conn, HOST)[0]["spec"]
    assert spec["image"] == "/srv/vm/debian.qcow2"
    assert spec["accel"] == "kvm"


def test_spawning_from_a_non_template_is_refused(conn, clock):
    """訊息列出合法的 template class:ephemeral 現在有兩種 provisioner
    (qemu 與 cuttlefish),只說「不是 qemu-template」會誤導。"""
    with pytest.raises(store.Conflict, match="not a template"):
        store.spawn_instance(conn, "pixel8-shiba", now_fn=clock)


def test_each_lease_gets_its_own_instance_id(conn, clock, template):
    first = store.reserve(conn, template, OWNER, 600, now_fn=clock)
    store.release(conn, first["id"], now_fn=clock)
    store.reserve(conn, template, "bob", 600, now_fn=clock)
    assert [r["id"] for r in _instances(conn)] == [
        f"{TEMPLATE}-0001", f"{TEMPLATE}-0002"
    ]


# ------------------------------------------------------------ 收斂與回報

def test_desired_instances_are_scoped_to_the_host(conn, clock, template):
    store.reserve(conn, template, OWNER, 600, now_fn=clock)
    assert len(store.desired_instances(conn, HOST)) == 1
    assert store.desired_instances(conn, "rog-laptop") == []


def test_running_report_marks_the_instance_alive(conn, clock, template):
    store.reserve(conn, template, OWNER, 600, now_fn=clock)
    instance = _instances(conn)[0]["id"]
    store.record_instance_results(
        conn, [{"instance_id": instance, "state": "running",
                "detail": {"pid": 4242, "vnc_port": 5901}}], clock()
    )
    conn.commit()
    assert _instances(conn)[0]["state"] == "running"


def test_a_gone_instance_is_retired_not_deleted(conn, clock, template):
    """ephemeral 不該在池子裡留下痕跡,但 events.device_id 是硬性 FK 而
    §8 的稽核紀錄不能刪。所以用狀態解決:retired 借不到、不列出來,
    效果等同消失,稽核鏈完整。"""
    store.reserve(conn, template, OWNER, 600, now_fn=clock)
    instance = _instances(conn)[0]["id"]
    store.record_instance_results(
        conn, [{"instance_id": instance, "state": "gone"}], clock()
    )
    conn.commit()
    assert conn.execute(
        "SELECT state FROM devices WHERE id = ?", (instance,)
    ).fetchone()["state"] == "retired"
    assert _instances(conn)[0]["state"] == "gone"


def test_a_retired_instance_cannot_be_leased(conn, clock, template):
    store.reserve(conn, template, OWNER, 600, now_fn=clock)
    instance = _instances(conn)[0]["id"]
    store.record_instance_results(
        conn, [{"instance_id": instance, "state": "gone"}], clock()
    )
    conn.commit()
    with pytest.raises(store.Conflict, match="retired"):
        store.reserve(conn, instance, "bob", 600, now_fn=clock)


def test_a_gone_instance_still_has_its_audit_trail(conn, clock, template):
    """§8 的 events 是 append-only 稽核紀錄:VM 消失了,它存在過的事實
    不該跟著消失。"""
    store.reserve(conn, template, OWNER, 600, now_fn=clock)
    instance = _instances(conn)[0]["id"]
    store.record_instance_results(
        conn, [{"instance_id": instance, "state": "gone"}], clock()
    )
    conn.commit()
    kinds = [
        r["kind"] for r in conn.execute(
            "SELECT kind FROM events WHERE device_id = ? ORDER BY id", (instance,)
        )
    ]
    assert kinds == ["device_attached", "device_detached"]


def test_a_gone_instance_drops_its_endpoints(conn, clock, template):
    """endpoint 指向一台已經不存在的 VM 是最糟的一種:client 連得上
    一個空的 port。"""
    store.reserve(conn, template, OWNER, 600, now_fn=clock)
    instance = _instances(conn)[0]["id"]
    conn.execute(
        "INSERT INTO device_services (device_id, service, endpoint) "
        "VALUES (?, 'vnc', '100.69.80.97:5901')", (instance,)
    )
    store.record_instance_results(
        conn, [{"instance_id": instance, "state": "gone"}], clock()
    )
    conn.commit()
    assert conn.execute(
        "SELECT COUNT(*) FROM device_services WHERE device_id = ?", (instance,)
    ).fetchone()[0] == 0


# ----------------------------------------------------------- 用完就沒了

def test_releasing_the_lease_marks_the_instance_for_destruction(conn, clock,
                                                                template):
    """§12:「qemu ephemeral:直接銷毀重生,天然乾淨」。"""
    lease = store.reserve(conn, template, OWNER, 600, now_fn=clock)
    store.release(conn, lease["id"], now_fn=clock)
    assert _instances(conn)[0]["state"] == "stopping"


def test_an_expired_lease_also_destroys_its_vm(conn, clock, template):
    """持有者放著不管才是常見情況;孤兒 VM 吃的是真的 RAM。"""
    lease = store.reserve(conn, template, OWNER, 60, now_fn=clock)
    clock.advance(61)
    store.reap_expired_leases(conn, now_fn=clock)
    assert _instances(conn)[0]["state"] == "stopping"


def test_the_reaper_catches_instances_whose_lease_vanished(conn, clock, template):
    """安全網:lease 不知怎麼沒了,VM 還跑著。"""
    lease = store.reserve(conn, template, OWNER, 600, now_fn=clock)
    conn.execute("UPDATE leases SET status = 'released' WHERE id = ?", (lease["id"],))
    conn.commit()
    assert store.reap_orphan_instances(conn, now_fn=clock) == [f"{TEMPLATE}-0001"]
    assert _instances(conn)[0]["state"] == "stopping"


def test_the_template_returns_to_free_for_the_next_user(conn, clock, template):
    """template 本身不是消耗品——它只是「可以生出這種 VM」的宣告。"""
    lease = store.reserve(conn, template, OWNER, 600, now_fn=clock)
    store.release(conn, lease["id"], now_fn=clock)
    assert conn.execute(
        "SELECT state FROM devices WHERE id = ?", (TEMPLATE,)
    ).fetchone()["state"] == "free"


def test_stopping_instances_are_still_sent_to_the_exporter(conn, clock, template):
    """exporter 要知道該停哪些;它回報 gone 之後 row 才真的清掉。"""
    lease = store.reserve(conn, template, OWNER, 600, now_fn=clock)
    store.release(conn, lease["id"], now_fn=clock)
    desired = store.desired_instances(conn, HOST)
    assert [d["state"] for d in desired] == ["stopping"]


def test_a_vm_is_not_mistaken_for_an_unplugged_usb_device(conn, clock, template):
    """qemu-vm 不在 DISCOVERABLE_CLASSES 裡:USB 掃描列不出 VM,把它算進
    缺席判定的話每輪都會判成拔線。"""
    assert store.VM_INSTANCE_CLASS not in store.DISCOVERABLE_CLASSES


def test_a_stale_running_report_does_not_resurrect_a_stopping_vm(conn, clock,
                                                                 template):
    """回報是**觀測**,不是指令。

    Exporter 回報的是上一輪的狀態,而 coordinator 可能在那之後已經把實例
    標成 stopping(lease 結束了)。少了這條保護,慢一拍的 running 回報會
    把 stopping 蓋回 running,exporter 下一輪看到的 desired 又變回「該跑
    著」——VM 再也停不掉,而它吃的是真的 RAM。真機沒有,是整合測試抓到的。
    """
    lease = store.reserve(conn, template, OWNER, 600, now_fn=clock)
    instance = _instances(conn)[0]["id"]
    store.record_instance_results(
        conn, [{"instance_id": instance, "state": "running"}], clock()
    )
    conn.commit()
    store.release(conn, lease["id"], now_fn=clock)
    assert _instances(conn)[0]["state"] == "stopping"

    # 上一輪送出去的 running 回報現在才回到 coordinator 手上。
    store.record_instance_results(
        conn, [{"instance_id": instance, "state": "running"}], clock()
    )
    conn.commit()
    assert _instances(conn)[0]["state"] == "stopping"


# ------------------------------ Cuttlefish:ephemeral 的第二種 provisioner
# §12:「AVD 就是 Android 的 ephemeral 池,真機 Pixel 只留 kernel/thermal/
# perf 工作」。template → 實例的整條路跟 provisioner 無關,所以這裡測的是
# 「兩種 template 共用同一套機制」以及 Cuttlefish 特有的 adb 位址回報。

CF_TEMPLATE = "cf-pool"


@pytest.fixture()
def cf_template(conn, clock):
    """Cuttlefish template。seed 裡已經有一筆(14700 上的 cf-pool),
    所以這裡只要確認它在,不要重建——重建會撞 identifier 的 UNIQUE。"""
    row = conn.execute(
        "SELECT id FROM devices WHERE id = ?", (CF_TEMPLATE,)
    ).fetchone()
    assert row is not None, "seed 應該要有 cf-pool template"
    return CF_TEMPLATE


def test_leasing_a_cuttlefish_template_spawns_an_instance(conn, clock, cf_template):
    store.reserve(conn, cf_template, OWNER, 600, now_fn=clock)
    rows = _instances(conn)
    assert len(rows) == 1
    assert rows[0]["template_id"] == cf_template


def test_a_cuttlefish_instance_gets_the_cuttlefish_class(conn, clock, cf_template):
    """class 決定兩件事:exporter 派給哪個 provisioner,以及它提供 adb
    而不是 vnc。"""
    store.reserve(conn, cf_template, OWNER, 600, now_fn=clock)
    dev = conn.execute(
        "SELECT class, control FROM devices WHERE id = ?",
        (f"{CF_TEMPLATE}-0001",),
    ).fetchone()
    assert dev["class"] == store.CVD_INSTANCE_CLASS
    assert dev["control"] == "cvd-spawn"


def test_cuttlefish_instances_export_adb_not_vnc(conn, clock, cf_template):
    """§6 的 vnc 那列明講「Cuttlefish 自帶 WebRTC 串流,不走這條」。
    對外它就是一台 Android 裝置。"""
    assert store.CLASS_SERVICES[store.CVD_INSTANCE_CLASS] == ("adb",)
    assert store.CLASS_SERVICES[store.VM_INSTANCE_CLASS] == ("vnc",)


def test_the_exporter_is_told_which_provisioner_owns_an_instance(
    conn, clock, cf_template, template
):
    """兩種 provisioner 在同一台 host 上並存時,exporter 不能用猜的——
    猜錯會對一台 Cuttlefish 實例跑 qemu,或反過來。"""
    store.reserve(conn, cf_template, OWNER, 600, now_fn=clock)
    store.reserve(conn, template, "bob", 600, now_fn=clock)
    by_id = {i["instance_id"]: i for i in store.desired_instances(conn, HOST)}
    assert by_id[f"{CF_TEMPLATE}-0001"]["class"] == store.CVD_INSTANCE_CLASS
    assert by_id[f"{TEMPLATE}-0001"]["class"] == store.VM_INSTANCE_CLASS


def test_adb_service_waits_for_the_instance_to_report_its_address(
    conn, clock, cf_template
):
    """Cuttlefish 的 adb port 要等實例真的生出來才知道(6520+n)。還沒報
    上來就送 desired 的話,exporter 會拿實例 id 當 adb serial 去連,每輪
    失敗一次。"""
    store.reserve(conn, cf_template, OWNER, 600, now_fn=clock)
    assert store.desired_services(conn, HOST) == []


def test_adb_service_uses_the_address_the_instance_reported(
    conn, clock, cf_template
):
    store.reserve(conn, cf_template, OWNER, 600, now_fn=clock)
    instance = f"{CF_TEMPLATE}-0001"
    store.record_instance_results(conn, [{
        "instance_id": instance, "state": "running",
        "detail": {"instance_num": 1, "adb_identifier": "127.0.0.1:6520"},
    }], clock())
    conn.commit()
    desired = store.desired_services(conn, HOST)
    assert len(desired) == 1
    assert desired[0]["service"] == "adb"
    assert desired[0]["identifier"] == "127.0.0.1:6520"


def test_releasing_destroys_the_cuttlefish_instance(conn, clock, cf_template):
    """§12:ephemeral 的清理方式就是直接銷毀重生。"""
    lease = store.reserve(conn, cf_template, OWNER, 600, now_fn=clock)
    store.release(conn, lease["id"], now_fn=clock)
    assert _instances(conn)[0]["state"] == "stopping"


def test_both_provisioners_share_one_instance_table(conn, clock, cf_template,
                                                    template):
    """整個重點:多一種 provisioner 只是多一組 class,不是多一套機制。"""
    store.reserve(conn, cf_template, OWNER, 600, now_fn=clock)
    store.reserve(conn, template, "bob", 600, now_fn=clock)
    assert sorted(r["id"] for r in _instances(conn)) == [
        f"{CF_TEMPLATE}-0001", f"{TEMPLATE}-0001"
    ]
