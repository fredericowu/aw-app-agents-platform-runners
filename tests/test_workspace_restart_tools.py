"""update_workspace / workspace_restart_status / cancel_workspace_restart.

Run: /usr/bin/python3.12 -m pytest tests/test_workspace_restart_tools.py
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from agents_platform_runners_app import mcp_server

TOOLS = ("update_workspace", "workspace_restart_status", "cancel_workspace_restart")


@pytest.fixture(autouse=True)
def _caller(monkeypatch):
    monkeypatch.setattr(mcp_server, "_WARM_CURRENT_RUN_ID_PATH", "/nonexistent/x")
    monkeypatch.setenv("AW_RUN_ID", "run-abc")


def _call(monkeypatch, handler, name, arguments):
    monkeypatch.setattr(mcp_server, "_agents_base_transport",
                        lambda: httpx.MockTransport(handler))
    return json.loads(asyncio.run(mcp_server._call_tool(name, arguments))[0].text)


def test_tools_registered_with_schema(monkeypatch):
    monkeypatch.setattr(mcp_server, "_ensure_running", lambda: None)
    monkeypatch.setattr(mcp_server, "_agents_base_transport",
                        lambda: httpx.MockTransport(lambda _r: httpx.Response(200, json=[])))
    tools = {t.name: t for t in asyncio.run(mcp_server._list_tools())}
    for n in TOOLS:
        assert n in tools
    assert tools["update_workspace"].inputSchema["required"] == ["reason"]
    d = tools["update_workspace"].description
    for phrase in ("1 hour", "30 minutes", "NO abort", "wake-up", "OWN tenant", "END YOUR TURN"):
        assert phrase in d
    assert tools["workspace_restart_status"].inputSchema["required"] == ["lock_id"]


def test_update_posts_body_with_caller_run_id(monkeypatch):
    seen = {}

    def handler(req):
        seen["path"], seen["body"] = req.url.path, json.loads(req.content)
        return httpx.Response(201, json={"id": "lock1", "phase": "draining"})

    out = _call(monkeypatch, handler, "update_workspace",
                {"workspace": "aw", "restart_only": True, "reason": "pick up fix",
                 "caller_run_id": "evil"})
    assert out == {"id": "lock1", "phase": "draining"}
    assert seen["path"] == "/api/workspace-restarts"
    assert seen["body"] == {"caller_run_id": "run-abc", "workspace": "aw",
                            "restart_only": True, "reason": "pick up fix"}


def test_workspace_omitted_defaults_server_side(monkeypatch):
    seen = {}

    def handler(req):
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"id": "l"})

    _call(monkeypatch, handler, "update_workspace", {"reason": "r", "workspace": "  "})
    assert "workspace" not in seen["body"]
    assert seen["body"]["restart_only"] is False


def test_update_503_is_clear_feature_off(monkeypatch):
    out = _call(monkeypatch, lambda _r: httpx.Response(503, text="disabled"),
                "update_workspace", {"reason": "r"})
    assert out["error"] and out["status"] == 503
    assert "not enabled" in out["message"]


def test_update_other_errors_pass_through(monkeypatch):
    out = _call(monkeypatch, lambda _r: httpx.Response(403, text="cross_tenant"),
                "update_workspace", {"reason": "r"})
    assert out["status"] == 403 and "cross_tenant" in out["message"]


def test_reason_required_before_http(monkeypatch):
    def handler(_r):
        raise AssertionError("no HTTP expected")

    assert _call(monkeypatch, handler, "update_workspace", {"reason": " "})["status"] == 400


def test_status_get(monkeypatch):
    seen = {}

    def handler(req):
        seen["m"], seen["p"] = req.method, req.url.path
        return httpx.Response(200, json={"phase": "polling", "checks_done": 3})

    out = _call(monkeypatch, handler, "workspace_restart_status", {"lock_id": "L1"})
    assert seen == {"m": "GET", "p": "/api/workspace-restarts/L1"}
    assert out["checks_done"] == 3


def test_cancel_post(monkeypatch):
    seen = {}

    def handler(req):
        seen["m"], seen["p"] = req.method, req.url.path
        return httpx.Response(200, json={"outcome": "cancelled"})

    out = _call(monkeypatch, handler, "cancel_workspace_restart", {"lock_id": "L1"})
    assert seen == {"m": "POST", "p": "/api/workspace-restarts/L1/cancel"}
    assert out["outcome"] == "cancelled"


@pytest.mark.parametrize("lock", ["", "a/b", "../x"])
def test_lock_id_validated(monkeypatch, lock):
    def handler(_r):
        raise AssertionError("no HTTP expected")

    for n in ("workspace_restart_status", "cancel_workspace_restart"):
        assert _call(monkeypatch, handler, n, {"lock_id": lock})["status"] == 400


def test_no_caller_run(monkeypatch):
    monkeypatch.delenv("AW_RUN_ID")
    out = _call(monkeypatch, lambda _r: httpx.Response(200, json={}),
                "update_workspace", {"reason": "r"})
    assert out["status"] == 400


@pytest.mark.parametrize("val", ["false", "true", 0, 1, "", []])
def test_restart_only_must_be_boolean(monkeypatch, val):
    def handler(_r):
        raise AssertionError("no HTTP expected")

    out = _call(monkeypatch, handler, "update_workspace", {"reason": "r", "restart_only": val})
    assert out["status"] == 400 and "boolean" in out["message"]


def test_restart_only_null_is_default(monkeypatch):
    seen = {}

    def handler(req):
        seen["b"] = json.loads(req.content)
        return httpx.Response(200, json={})

    _call(monkeypatch, handler, "update_workspace", {"reason": "r", "restart_only": None})
    assert seen["b"]["restart_only"] is False
