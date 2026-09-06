"""Exporter 端的 flash 執行(設計文件 §6)。

重點是「什麼情況下**絕對不動手**」:雜湊對不上、檔案不在、不認得的裝置
class。刷壞的代價是一台磚,所以這些前置條件都要在碰裝置之前擋掉。
"""

from __future__ import annotations

import pytest

from exporter.flash import FlashExecutor, sha256_file

DIGEST = "a" * 64


@pytest.fixture()
def image(tmp_path):
    p = tmp_path / "vendor.img"
    p.write_bytes(b"pretend this is a vendor partition")
    return p


def _job(image_path, digest=DIGEST, device_class="android", kind="vendor",
         flash_id=7):
    return {
        "flash_id": flash_id,
        "device_id": "pixel8-shiba",
        "identifier": "38011FDJH00C9F",
        "class": device_class,
        "image_id": "shiba-vendor-v3",
        "kind": kind,
        "uri": str(image_path),
        "sha256": digest,
    }


def _ok_hasher(digest=DIGEST):
    return lambda path: digest


# ------------------------------------------------------------ 成功路徑

def test_android_flash_uses_fastboot_with_the_serial(runner, image):
    """``-s`` 不能省:host 上可能插著好幾顆,刷錯裝置是最不能犯的錯。"""
    out = FlashExecutor(runner, hasher=_ok_hasher()).execute(_job(image))
    assert out.ok
    assert runner.started[-1].argv == [
        "fastboot", "-s", "38011FDJH00C9F", "flash", "vendor", str(image)
    ]


def test_riscv_sbc_flash_writes_the_block_device(runner, image):
    job = _job(image, device_class="riscv-sbc", kind="sd-card")
    job["identifier"] = "/dev/sdb"
    out = FlashExecutor(runner, hasher=_ok_hasher()).execute(job)
    assert out.ok
    argv = runner.started[-1].argv
    assert argv[0] == "dd" and f"of=/dev/sdb" in argv
    # conv=fsync:回報成功時資料要真的落盤,不是只寫進 page cache。
    assert "conv=fsync" in argv


def test_outcome_reports_the_digest_actually_flashed(runner, image):
    out = FlashExecutor(runner, hasher=_ok_hasher()).execute(_job(image))
    assert out.detail["sha256"] == DIGEST
    assert out.as_report() == {"flash_id": 7, "ok": True, "detail": out.detail}


# ------------------------------------------------- 絕對不動手的情況

def test_digest_mismatch_refuses_to_flash(runner, image):
    """手上這份不是 registry 登記的那份。不驗的話 registry 只是記帳。"""
    out = FlashExecutor(runner, hasher=_ok_hasher("b" * 64)).execute(_job(image))
    assert not out.ok
    assert "sha256 mismatch" in out.detail["error"]
    assert runner.started == []          # 裝置完全沒被碰過


def test_missing_image_refuses_to_flash(runner, tmp_path):
    out = FlashExecutor(runner).execute(_job(tmp_path / "nope.img"))
    assert not out.ok
    assert "not found" in out.detail["error"]
    assert runner.started == []


def test_unknown_device_class_is_refused_not_guessed(runner, image):
    """猜錯的代價是對一台不該用 fastboot 的板子跑 fastboot。"""
    out = FlashExecutor(runner, hasher=_ok_hasher()).execute(
        _job(image, device_class="openbmc")
    )
    assert not out.ok
    assert "no flash method" in out.detail["error"]
    assert runner.started == []


def test_incomplete_job_is_refused(runner, image):
    job = _job(image)
    job["kind"] = None
    out = FlashExecutor(runner, hasher=_ok_hasher()).execute(job)
    assert not out.ok
    assert runner.started == []


def test_missing_tool_is_reported_not_crashed(make_runner, image):
    runner = make_runner(missing={"fastboot"})
    out = FlashExecutor(runner, hasher=_ok_hasher()).execute(_job(image))
    assert not out.ok
    assert "not found in PATH" in out.detail["error"]


# ------------------------------------------------------------ 失敗路徑

def test_nonzero_exit_is_a_failure(runner, image):
    executor = FlashExecutor(runner, hasher=_ok_hasher())

    class FailingRunner:
        def __init__(self, inner):
            self._inner = inner
            self.started = inner.started

        def start(self, argv):
            handle = self._inner.start(argv)
            handle.exit_code = 1
            return handle

        def which(self, program):
            return self._inner.which(program)

    out = FlashExecutor(FailingRunner(runner), hasher=_ok_hasher()).execute(_job(image))
    assert not out.ok
    assert out.detail["exit_code"] == 1


def test_a_hung_flash_times_out_and_is_terminated(runner, image):
    """卡住的 fastboot 不能讓收斂迴圈跟著停住。"""
    class HangingRunner:
        def __init__(self, inner):
            self._inner = inner
            self.started = inner.started

        def start(self, argv):
            handle = self._inner.start(argv)
            handle.hangs = True
            return handle

        def which(self, program):
            return self._inner.which(program)

    out = FlashExecutor(HangingRunner(runner), hasher=_ok_hasher(),
                        timeout_s=1.0).execute(_job(image))
    assert not out.ok
    assert out.detail["error"] == "timed out"
    assert runner.started[-1].terminated


def test_flash_waits_for_completion_rather_than_terminating(runner, image):
    """刷完才算數。用 terminate 取代 wait 就是刷到一半砍掉——
    這正是 adb preflight 在真機上踩過的那個形狀的錯。"""
    FlashExecutor(runner, hasher=_ok_hasher()).execute(_job(image))
    handle = runner.started[-1]
    assert handle.waited and handle.work_done
    assert not handle.terminated


# ---------------------------------------------------------------- hashing

def test_sha256_file_matches_hashlib(tmp_path):
    import hashlib

    p = tmp_path / "blob.img"
    p.write_bytes(b"x" * (3 * 1024 * 1024 + 17))     # 跨多個 chunk
    assert sha256_file(p) == hashlib.sha256(p.read_bytes()).hexdigest()
