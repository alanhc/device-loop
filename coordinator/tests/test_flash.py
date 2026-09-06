"""Image registry 與 mediated flash(設計文件 §6)。

Flash 是能力表裡唯一 mediated、也是唯一破壞性的操作。這裡測的是那些
「錯了就是一台磚」的不變量:只刷登記過的 image、不跨裝置刷、同一台裝置
不並行刷、刷失敗的裝置不回到 free。
"""

from __future__ import annotations

import pytest

from coordinator import store

DEVICE = "pixel8-shiba"
JUPITER = "milkv-jupiter"
OWNER = "alice"
DIGEST = "a" * 64
OTHER_DIGEST = "b" * 64


def _image(conn, image_id="shiba-vendor-v3", device_id=DEVICE, kind="vendor",
           known_good=False, clock=None, sha256=DIGEST):
    return store.register_image(
        conn, image_id, device_id, kind, f"/srv/images/{image_id}.img",
        sha256, known_good, now_fn=clock or store.utcnow,
    )


# ---------------------------------------------------------- image registry

def test_registering_an_image_records_the_digest(conn, clock):
    img = _image(conn, clock=clock)
    assert img["sha256"] == DIGEST
    assert img["device_id"] == DEVICE


def test_sha256_must_be_64_hex_characters(conn, clock):
    """形式在登記時就驗。等 exporter 拿到才發現,只是把同一個錯誤延後到
    裝置已經進 fastboot mode 之後。"""
    with pytest.raises(store.ImageError, match="64 hex"):
        store.register_image(conn, "bad", DEVICE, "vendor", "/x.img",
                             "not-a-digest", now_fn=clock)


def test_digest_is_normalised_to_lowercase(conn, clock):
    img = store.register_image(conn, "up", DEVICE, "boot", "/b.img",
                               DIGEST.upper(), now_fn=clock)
    assert img["sha256"] == DIGEST


def test_image_for_an_unknown_device_is_rejected(conn, clock):
    with pytest.raises(store.NotFound):
        store.register_image(conn, "orphan", "no-such-device", "boot", "/b.img",
                             DIGEST, now_fn=clock)


def test_a_device_kind_has_only_one_known_good(conn, clock):
    """還原目標不能有歧義。登記新的 known-good 會把舊的降級——讓呼叫者
    自己記得清掉舊的,遲早會出現兩個 known-good。"""
    old = _image(conn, "shiba-vendor-v1", kind="vendor", known_good=True, clock=clock)
    new = _image(conn, "shiba-vendor-v2", kind="vendor", known_good=True, clock=clock)
    good = store.known_good_images(conn, DEVICE)
    assert [g["id"] for g in good] == [new["id"]]
    assert conn.execute(
        "SELECT known_good FROM images WHERE id = ?", (old["id"],)
    ).fetchone()["known_good"] == 0


def test_different_kinds_each_keep_their_own_known_good(conn, clock):
    """boot 跟 vendor 是不同的分割區,各自有自己的還原目標。"""
    _image(conn, "shiba-boot", kind="boot", known_good=True, clock=clock)
    _image(conn, "shiba-vendor", kind="vendor", known_good=True, clock=clock)
    assert {g["kind"] for g in store.known_good_images(conn, DEVICE)} == {
        "boot", "vendor"
    }


# ------------------------------------------------------------ flash 請求

def test_flash_requires_a_registered_image(conn, clock):
    store.reserve(conn, DEVICE, OWNER, 600, now_fn=clock)
    with pytest.raises(store.NotFound, match="image"):
        store.request_flash(conn, DEVICE, "never-registered", OWNER, now_fn=clock)


def test_image_cannot_be_flashed_to_a_different_device(conn, clock):
    """跨裝置刷是把 Pixel 的 boot.img 刷進別的板子那種等級的錯誤。"""
    _image(conn, "shiba-boot", device_id=DEVICE, kind="boot", clock=clock)
    store.reserve(conn, JUPITER, OWNER, 600, now_fn=clock)
    with pytest.raises(store.Conflict, match="belongs to device"):
        store.request_flash(conn, JUPITER, "shiba-boot", OWNER, now_fn=clock)


def test_one_flash_in_flight_per_device(conn, clock):
    """並行刷同一台裝置是直接把它變磚。"""
    _image(conn, clock=clock)
    _image(conn, "shiba-boot", kind="boot", clock=clock)
    lease = store.reserve(conn, DEVICE, OWNER, 600, now_fn=clock)
    store.request_flash(conn, DEVICE, "shiba-vendor-v3", OWNER, lease["id"],
                        now_fn=clock)
    with pytest.raises(store.Conflict, match="already has a flash in flight"):
        store.request_flash(conn, DEVICE, "shiba-boot", OWNER, lease["id"],
                            now_fn=clock)


def test_flash_is_refused_while_a_reclaim_is_in_flight(conn, clock):
    """重開到一半開始刷、或刷到一半被重開,都是製造磚的可靠方法。"""
    _image(conn, clock=clock)
    store.request_reclaim(conn, DEVICE, "stuck", now_fn=clock)
    store.reserve(conn, DEVICE, OWNER, 600, now_fn=clock)
    with pytest.raises(store.Conflict, match="reclaim in flight"):
        store.request_flash(conn, DEVICE, "shiba-vendor-v3", OWNER, now_fn=clock)


def test_flash_request_writes_an_audit_event(conn, clock):
    """§6:每次執行要記錄 requester/image/時間,方便出事回溯。"""
    _image(conn, clock=clock)
    lease = store.reserve(conn, DEVICE, OWNER, 600, now_fn=clock)
    job = store.request_flash(conn, DEVICE, "shiba-vendor-v3", OWNER, lease["id"],
                              now_fn=clock)
    row = conn.execute(
        "SELECT * FROM events WHERE kind = 'flash_start' AND device_id = ?",
        (DEVICE,),
    ).fetchone()
    assert row["actor"] == OWNER
    assert row["lease_id"] == lease["id"]
    assert "shiba-vendor-v3" in row["detail"]
    assert job["user_id"] == OWNER


# ------------------------------------------------ 派給 exporter 與收結果

def test_pending_flash_carries_the_digest_to_the_exporter(conn, clock):
    """Exporter 刷之前要自己驗一次雜湊,所以 uri 跟 sha256 都要送過去——
    不然 registry 只是記帳,擋不住檔案在 exporter 本地被換掉。"""
    _image(conn, clock=clock)
    store.reserve(conn, DEVICE, OWNER, 600, now_fn=clock)
    store.request_flash(conn, DEVICE, "shiba-vendor-v3", OWNER, now_fn=clock)
    pending = store.pending_flashes(conn, "rog-laptop")
    assert len(pending) == 1
    assert pending[0]["sha256"] == DIGEST
    assert pending[0]["kind"] == "vendor"
    assert pending[0]["identifier"] == "38011FDJH00C9F"
    assert pending[0]["class"] == "android"


def test_pending_flashes_are_scoped_to_the_host(conn, clock):
    """別台 host 的 exporter 不該領到不是它的裝置的工作。"""
    _image(conn, clock=clock)
    store.reserve(conn, DEVICE, OWNER, 600, now_fn=clock)
    store.request_flash(conn, DEVICE, "shiba-vendor-v3", OWNER, now_fn=clock)
    assert store.pending_flashes(conn, "alanhc-14700") == []


def test_successful_flash_leaves_the_device_leased(conn, clock):
    """刷成功的裝置還在 lease 裡,持有者要繼續用。"""
    _image(conn, clock=clock)
    store.reserve(conn, DEVICE, OWNER, 600, now_fn=clock)
    job = store.request_flash(conn, DEVICE, "shiba-vendor-v3", OWNER, now_fn=clock)
    store.record_flash_results(conn, [{"flash_id": job["id"], "ok": True,
                                       "detail": {"exit_code": 0}}], clock())
    conn.commit()
    assert conn.execute(
        "SELECT state FROM devices WHERE id = ?", (DEVICE,)
    ).fetchone()["state"] == "leased"
    assert conn.execute(
        "SELECT state FROM flash_jobs WHERE id = ?", (job["id"],)
    ).fetchone()["state"] == "done"


def test_failed_flash_holds_the_device_in_maintenance(conn, clock):
    """刷壞的裝置可能連 boot 都上不去,不能在 lease 結束後直接交給下一個
    agent——寧可少一台可用裝置。"""
    _image(conn, clock=clock)
    lease = store.reserve(conn, DEVICE, OWNER, 600, now_fn=clock)
    job = store.request_flash(conn, DEVICE, "shiba-vendor-v3", OWNER, lease["id"],
                              now_fn=clock)
    store.release(conn, lease["id"], now_fn=clock)      # 裝置回到 free
    store.record_flash_results(conn, [{"flash_id": job["id"], "ok": False,
                                       "detail": {"exit_code": 1}}], clock())
    conn.commit()
    assert conn.execute(
        "SELECT state FROM devices WHERE id = ?", (DEVICE,)
    ).fetchone()["state"] == "maintenance"


def test_flash_result_is_recorded_once(conn, clock):
    """重送同一筆結果不該重複計數或翻轉狀態。"""
    _image(conn, clock=clock)
    store.reserve(conn, DEVICE, OWNER, 600, now_fn=clock)
    job = store.request_flash(conn, DEVICE, "shiba-vendor-v3", OWNER, now_fn=clock)
    result = [{"flash_id": job["id"], "ok": True, "detail": {"exit_code": 0}}]
    assert store.record_flash_results(conn, result, clock()) == [job["id"]]
    assert store.record_flash_results(conn, result, clock()) == []
    assert conn.execute(
        "SELECT attempts FROM flash_jobs WHERE id = ?", (job["id"],)
    ).fetchone()["attempts"] == 1


def test_finished_flash_frees_the_device_for_the_next_flash(conn, clock):
    """一次只能一個是「同時」的限制,不是「一輩子一次」。"""
    _image(conn, clock=clock)
    _image(conn, "shiba-boot", kind="boot", clock=clock)
    store.reserve(conn, DEVICE, OWNER, 600, now_fn=clock)
    first = store.request_flash(conn, DEVICE, "shiba-vendor-v3", OWNER, now_fn=clock)
    store.record_flash_results(conn, [{"flash_id": first["id"], "ok": True}], clock())
    conn.commit()
    second = store.request_flash(conn, DEVICE, "shiba-boot", OWNER, now_fn=clock)
    assert second["state"] == "pending"


# --------------------------- 刷壞的裝置不回到 free(lease 結束的那一刻)
# 失敗當下裝置通常還在 lease 裡(持有者正要救它),所以扣住的動作不能在
# 收結果的時候做,要在「把裝置交出去」的那一刻做。少了這條,一台刷到上
# 不了 boot 的手機會被若無其事地發給下一個 agent。

def _failed_flash(conn, clock, lease):
    _image(conn, clock=clock)
    job = store.request_flash(conn, DEVICE, "shiba-vendor-v3", OWNER, lease["id"],
                              now_fn=clock)
    store.record_flash_results(conn, [{"flash_id": job["id"], "ok": False}], clock())
    conn.commit()
    return job


def _state(conn):
    return conn.execute(
        "SELECT state FROM devices WHERE id = ?", (DEVICE,)
    ).fetchone()["state"]


def test_release_after_a_failed_flash_holds_the_device(conn, clock):
    lease = store.reserve(conn, DEVICE, OWNER, 600, now_fn=clock)
    _failed_flash(conn, clock, lease)
    store.release(conn, lease["id"], now_fn=clock)
    assert _state(conn) == "maintenance"


def test_expiry_after_a_failed_flash_holds_the_device(conn, clock):
    """走 reaper 的路徑也要擋——持有者放著不管才是常見情況。"""
    lease = store.reserve(conn, DEVICE, OWNER, 60, now_fn=clock)
    _failed_flash(conn, clock, lease)
    clock.advance(61)
    store.reap_expired_leases(conn, now_fn=clock)
    assert _state(conn) == "maintenance"


def test_a_held_device_cannot_be_reserved(conn, clock):
    lease = store.reserve(conn, DEVICE, OWNER, 600, now_fn=clock)
    _failed_flash(conn, clock, lease)
    store.release(conn, lease["id"], now_fn=clock)
    with pytest.raises(store.Conflict, match="maintenance"):
        store.reserve(conn, DEVICE, "bob", 600, now_fn=clock)


def test_restoring_a_known_good_image_puts_it_back_in_circulation(conn, clock):
    """§6 的 restore:刷回 known-good 之後裝置就是好的,不該永遠被扣住。"""
    lease = store.reserve(conn, DEVICE, OWNER, 600, now_fn=clock)
    _failed_flash(conn, clock, lease)
    _image(conn, "shiba-vendor-known-good", kind="vendor", known_good=True,
           clock=clock)
    restore = store.request_flash(conn, DEVICE, "shiba-vendor-known-good", OWNER,
                                  lease["id"], now_fn=clock)
    store.record_flash_results(conn, [{"flash_id": restore["id"], "ok": True}],
                               clock())
    conn.commit()
    store.release(conn, lease["id"], now_fn=clock)
    assert _state(conn) == "free"


def test_a_successful_flash_never_holds_the_device(conn, clock):
    lease = store.reserve(conn, DEVICE, OWNER, 600, now_fn=clock)
    _image(conn, clock=clock)
    job = store.request_flash(conn, DEVICE, "shiba-vendor-v3", OWNER, lease["id"],
                              now_fn=clock)
    store.record_flash_results(conn, [{"flash_id": job["id"], "ok": True}], clock())
    conn.commit()
    store.release(conn, lease["id"], now_fn=clock)
    assert _state(conn) == "free"
