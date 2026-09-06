"""HTTP 層:狀態碼對應與端點連通性。邏輯本身由 test_lease / test_heartbeat 覆蓋。"""

import pytest
from fastapi.testclient import TestClient

from coordinator.api import create_app

PIXEL = "pixel8-shiba"
PIXEL_SERIAL = "38011FDJH00C9F"


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


def test_devices_seeded(client):
    devices = client.get("/devices").json()
    assert {d["id"] for d in devices} >= {PIXEL, "milkv-jupiter"}
    pixel = next(d for d in devices if d["id"] == PIXEL)
    assert pixel["identifier"] == PIXEL_SERIAL


def test_lease_lifecycle_over_http(client):
    r = client.post(
        "/leases",
        json={"device_id": PIXEL, "user_id": "alanhc", "ttl_s": 300,
              "purpose": "smoke test"},
    )
    assert r.status_code == 201
    lease_id = r.json()["id"]

    renew = client.post(
        f"/leases/{lease_id}/renew", json={"user_id": "alanhc", "ttl_s": 300}
    )
    assert renew.status_code == 200
    released = client.post(
        f"/leases/{lease_id}/release", json={"user_id": "alanhc"}
    )
    assert released.json()["status"] == "released"

    kinds = [e["kind"] for e in client.get(f"/devices/{PIXEL}/events").json()]
    assert kinds == ["reserve", "renew", "release"]


def test_double_reserve_returns_409(client):
    body = {"device_id": PIXEL, "user_id": "alanhc", "ttl_s": 300}
    assert client.post("/leases", json=body).status_code == 201
    r = client.post("/leases", json={**body, "user_id": "other"})
    assert r.status_code == 409


def test_unknown_device_returns_404(client):
    r = client.post(
        "/leases", json={"device_id": "nope", "user_id": "alanhc", "ttl_s": 60}
    )
    assert r.status_code == 404


def test_unknown_lease_returns_404(client):
    assert client.get("/leases/9999").status_code == 404
    assert client.post(
        "/leases/9999/renew", json={"user_id": "alanhc", "ttl_s": 60}
    ).status_code == 404
    assert client.post(
        "/leases/9999/release", json={"user_id": "alanhc"}
    ).status_code == 404


def test_release_twice_returns_409(client):
    lease_id = client.post(
        "/leases", json={"device_id": PIXEL, "user_id": "alanhc", "ttl_s": 60}
    ).json()["id"]
    body = {"user_id": "alanhc"}
    assert client.post(f"/leases/{lease_id}/release", json=body).status_code == 200
    assert client.post(f"/leases/{lease_id}/release", json=body).status_code == 409


def test_non_positive_ttl_is_rejected(client):
    r = client.post(
        "/leases", json={"device_id": PIXEL, "user_id": "alanhc", "ttl_s": 0}
    )
    assert r.status_code == 422


def test_heartbeat_endpoint_reports_diff(client):
    r = client.post(
        "/heartbeat",
        json={"host_id": "rog-laptop", "identifiers": [PIXEL_SERIAL, "NEWBOARD123"]},
    )
    assert r.status_code == 200
    assert r.json()["unregistered"] == ["NEWBOARD123"]

    new = next(
        d for d in client.get("/devices").json() if d["id"] == "NEWBOARD123"
    )
    assert new["state"] == "unregistered"


def test_heartbeat_unknown_host_returns_404(client):
    r = client.post("/heartbeat", json={"host_id": "ghost", "identifiers": []})
    assert r.status_code == 404


# ---------------------------------------------- lease 擁有權(HTTP 側)
# MCP 前門有的越權問題 HTTP 這邊一樣有,而且原本更嚴重:release 根本不收
# user_id,沒有任何東西可以拿來比對。

def test_another_user_cannot_release_your_lease_over_http(client):
    lease_id = client.post(
        "/leases", json={"device_id": PIXEL, "user_id": "alice", "ttl_s": 600}
    ).json()["id"]
    r = client.post(f"/leases/{lease_id}/release", json={"user_id": "mallory"})
    assert r.status_code == 404
    devices = {d["id"]: d for d in client.get("/devices").json()}
    assert devices[PIXEL]["state"] == "leased"


def test_another_user_cannot_renew_your_lease_over_http(client):
    lease_id = client.post(
        "/leases", json={"device_id": PIXEL, "user_id": "alice", "ttl_s": 600}
    ).json()["id"]
    r = client.post(
        f"/leases/{lease_id}/renew", json={"user_id": "mallory", "ttl_s": 9999}
    )
    assert r.status_code == 404


def test_hijack_path_is_closed_over_http(client):
    """release 掉別人的 lease → 裝置 free → 搶走,整條路要斷。"""
    lease_id = client.post(
        "/leases", json={"device_id": PIXEL, "user_id": "alice", "ttl_s": 600}
    ).json()["id"]
    client.post(f"/leases/{lease_id}/release", json={"user_id": "mallory"})
    grab = client.post(
        "/leases", json={"device_id": PIXEL, "user_id": "mallory", "ttl_s": 600}
    )
    assert grab.status_code == 409
    ok = client.post(f"/leases/{lease_id}/release", json={"user_id": "alice"})
    assert ok.status_code == 200


def test_missing_user_id_is_rejected(client):
    """少了 user_id 就無從檢查——要擋在輸入驗證,不能預設放行。"""
    lease_id = client.post(
        "/leases", json={"device_id": PIXEL, "user_id": "alice", "ttl_s": 600}
    ).json()["id"]
    assert client.post(f"/leases/{lease_id}/release").status_code == 422
    assert client.post(
        f"/leases/{lease_id}/renew", json={"ttl_s": 60}
    ).status_code == 422


def test_heartbeat_rejects_a_reported_mediated_flag(client):
    """mediated 由 coordinator 決定;exporter 多送這個欄位要被擋下來,
    而不是默默忽略——默默忽略的話,回報方會以為自己講了算。"""
    r = client.post("/heartbeat", json={
        "host_id": "rog-laptop",
        "identifiers": [],
        "services": [{"device_id": PIXEL, "service": "adb",
                      "port": 9001, "mediated": False}],
    })
    assert r.status_code == 422


def test_heartbeat_accepts_a_well_formed_service_report(client):
    r = client.post("/heartbeat", json={
        "host_id": "rog-laptop",
        "identifiers": [],
        "services": [{"device_id": PIXEL, "service": "adb", "port": 9001}],
    })
    assert r.status_code == 200
    assert "desired" in r.json()


# ----------------------------------------------------- 佇列(opt-in 參數)
# 兩個前門用同一套語意、同一份實作,只是預設值不同:HTTP 不帶 queue 就是
# 既有的 409 行為(腳本、exporter 不受影響),MCP 的 reserve_device 預設
# 排隊(agent 的正常期待)。儀表板可以自己選要不要排。

def test_reserve_without_queue_still_returns_409(client):
    """向後相容:既有呼叫端一行都不用改。"""
    body = {"device_id": PIXEL, "user_id": "alice", "ttl_s": 600}
    assert client.post("/leases", json=body).status_code == 201
    r = client.post("/leases", json={**body, "user_id": "bob"})
    assert r.status_code == 409


def test_reserve_with_queue_returns_a_ticket(client):
    body = {"device_id": PIXEL, "user_id": "alice", "ttl_s": 600}
    client.post("/leases", json=body)
    r = client.post("/leases", json={**body, "user_id": "bob", "queue": True})
    assert r.status_code == 201
    out = r.json()
    assert out["queued"] is True
    assert out["position"] == 1
    assert out["lease_id"] is None


def test_a_free_device_is_leased_directly_even_with_queue(client):
    """常見情況不用多跑一趟佇列。"""
    r = client.post("/leases", json={
        "device_id": PIXEL, "user_id": "alice", "ttl_s": 600, "queue": True})
    assert r.json()["queued"] is False
    assert r.json()["id"] > 0


def test_queue_status_is_readable(client):
    body = {"device_id": PIXEL, "user_id": "alice", "ttl_s": 600}
    client.post("/leases", json=body)
    job = client.post("/leases", json={**body, "user_id": "bob", "queue": True}).json()
    r = client.get(f"/jobs/{job['job_id']}")
    assert r.status_code == 200
    assert r.json()["position"] == 1


def test_cancelling_a_queued_job_over_http(client):
    body = {"device_id": PIXEL, "user_id": "alice", "ttl_s": 600}
    client.post("/leases", json=body)
    job = client.post("/leases", json={**body, "user_id": "bob", "queue": True}).json()
    r = client.post(f"/jobs/{job['job_id']}/cancel", json={"user_id": "bob"})
    assert r.status_code == 200
    assert r.json()["state"] == "cancelled"


def test_cannot_cancel_another_users_job_over_http(client):
    body = {"device_id": PIXEL, "user_id": "alice", "ttl_s": 600}
    client.post("/leases", json=body)
    job = client.post("/leases", json={**body, "user_id": "bob", "queue": True}).json()
    r = client.post(f"/jobs/{job['job_id']}/cancel", json={"user_id": "mallory"})
    assert r.status_code == 404


def test_unknown_job_returns_404(client):
    assert client.get("/jobs/9999").status_code == 404
