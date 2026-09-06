"""MCP 走 HTTP transport,掛在同一個 app 裡(Phase 2)。

stdio 只有同機的 client 用得到;tailnet 上的 agent 要 HTTP。這裡驗的是
「掛進去之後兩邊都還能用」以及那個很難查的 421。
"""

from __future__ import annotations

import json
import os
import re

import pytest
from fastapi.testclient import TestClient

from coordinator.api import create_app

MCP_HEADERS = {"Accept": "application/json, text/event-stream"}


def _client(tmp_path, allowed="testserver,localhost,127.0.0.1", monkeypatch=None):
    if monkeypatch is not None:
        monkeypatch.setenv("COORDINATOR_MCP_ALLOWED_HOSTS", allowed)
    import importlib

    from coordinator import api as api_mod
    importlib.reload(api_mod)
    return TestClient(api_mod.create_app(str(tmp_path / "mcp.db")))


def _initialize(c):
    r = c.post("/mcp/", json={
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "t", "version": "0"}}},
        headers=MCP_HEADERS)
    return r, r.headers.get("mcp-session-id")


def _rpc(c, sid, method, params=None, rid=2):
    body = {"jsonrpc": "2.0", "id": rid, "method": method}
    if params is not None:
        body["params"] = params
    r = c.post("/mcp/", json=body, headers={**MCP_HEADERS, "mcp-session-id": sid})
    m = re.search(r"data: (\{.*\})", r.text)
    return r, (json.loads(m.group(1)) if m else None)


def test_mcp_and_rest_are_served_by_the_same_app(tmp_path, monkeypatch):
    """一個 process、一條 DB 連線、一把鎖——容器只跑一個 uvicorn。"""
    with _client(tmp_path, monkeypatch=monkeypatch) as c:
        assert c.get("/devices").status_code == 200
        r, sid = _initialize(c)
        assert r.status_code == 200
        assert sid


def test_tools_are_listed_over_http(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch=monkeypatch) as c:
        _, sid = _initialize(c)
        c.post("/mcp/", json={"jsonrpc": "2.0", "method": "notifications/initialized"},
               headers={**MCP_HEADERS, "mcp-session-id": sid})
        _, payload = _rpc(c, sid, "tools/list")
        names = {t["name"] for t in payload["result"]["tools"]}
        assert {"reserve_device", "get_queue_status", "cancel_queued",
                "adb_shell", "get_uart_stream"} <= names


def test_a_tool_call_works_over_http(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch=monkeypatch) as c:
        _, sid = _initialize(c)
        c.post("/mcp/", json={"jsonrpc": "2.0", "method": "notifications/initialized"},
               headers={**MCP_HEADERS, "mcp-session-id": sid})
        _, payload = _rpc(c, sid, "tools/call", {
            "name": "reserve_device",
            "arguments": {"device_id": "pixel8-shiba", "user_id": "alice",
                          "ttl_s": 600}})
        assert payload["result"]["structuredContent"]["queued"] is False


def test_disallowed_host_gets_421_not_a_connection_error(tmp_path, monkeypatch):
    """這正是遠端 agent 連不進來時會看到的東西。

    421 而不是連線失敗,所以症狀是「連得上但一直被拒」——不知道 DNS
    rebinding protection 存在的話會查很久。
    """
    with _client(tmp_path, allowed="127.0.0.1", monkeypatch=monkeypatch) as c:
        r, _ = _initialize(c)          # TestClient 的 Host 是 testserver
        assert r.status_code == 421


def test_startup_warns_when_only_local_hosts_are_allowed(tmp_path, monkeypatch, caplog):
    """設定錯的話要在啟動時就看得見,不是等 agent 連進來才炸。"""
    import importlib
    import logging

    monkeypatch.setenv("COORDINATOR_MCP_ALLOWED_HOSTS", "127.0.0.1,localhost")
    from coordinator import api as api_mod
    importlib.reload(api_mod)
    with caplog.at_level(logging.WARNING):
        api_mod._log_mcp_reachability()
    assert "421" in caplog.text


def test_no_warning_when_a_remote_host_is_allowed(tmp_path, monkeypatch, caplog):
    import importlib
    import logging

    monkeypatch.setenv("COORDINATOR_MCP_ALLOWED_HOSTS", "100.69.80.97:8090,localhost")
    from coordinator import api as api_mod
    importlib.reload(api_mod)
    with caplog.at_level(logging.WARNING):
        api_mod._log_mcp_reachability()
    assert "421" not in caplog.text
