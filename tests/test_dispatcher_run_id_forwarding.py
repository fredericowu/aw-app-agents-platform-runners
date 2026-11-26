"""``dispatcher_run_id`` forwarding (card 4245bf3b) — the identity AP-MT's
``flow_handoff='connected'`` guard keys on. Asserted on the outgoing request
body of every run-creating tool route."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents_platform_runners_app import kanban_dispatch as kd  # noqa: E402
from agents_platform_runners_app import mcp_server  # noqa: E402


def _record(monkeypatch):
    recorder: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        recorder.append(request)
        if request.method == "GET" and "/api/runs" in request.url.path and "/api/runs/" not in request.url.path:
            return httpx.Response(200, json=[{"source_slug": "coder", "session_id": "s-1", "category": "work"}])
        return httpx.Response(200, json={"run_id": "r1", "target_id": "t1", "status": "success",
                                         "parent_run_id": "p1", "child_run_ids": []})

    monkeypatch.setattr(mcp_server, "_caller_run_id", lambda args: "mgr-run")
    real = httpx.AsyncClient
    monkeypatch.setattr(mcp_server.httpx, "AsyncClient",
                        lambda *a, **k: real(*a, **{**k, "transport": httpx.MockTransport(handler)}))
    return recorder


def _posts(recorder, suffix):
    return [json.loads(r.content) for r in recorder if r.method == "POST" and r.url.path.endswith(suffix)]


ARGS = {"_gateway_caller_run_id": "mgr-run", "target_slug": "tg", "input": "x"}


@pytest.mark.parametrize("tool,extra,suffix", [
    ("run_agent_async", {"slug": "debugger"}, "/api/agents/debugger/run"),
    ("run_workflow_async", {"slug": "wf"}, "/api/workflows/wf/run"),
    ("run_agents_parallel", {"agents": [{"slug": "debugger", "input": "a"}]}, "/api/runs/parallel"),
])
def test_run_creating_tools_forward_dispatcher_run_id(monkeypatch, tool, extra, suffix):
    rec = _record(monkeypatch)
    asyncio.run(mcp_server._call_tool(tool, {**ARGS, **extra}))
    bodies = _posts(rec, suffix) or [json.loads(r.content) for r in rec if r.method == "POST"]
    assert bodies and all(b.get("dispatcher_run_id") == "mgr-run" for b in bodies)


def test_dynamic_agent_tool_identifies_caller_without_changing_parentage(monkeypatch):
    rec = _record(monkeypatch)
    asyncio.run(mcp_server._call_tool("agent_debugger", dict(ARGS)))
    (body,) = _posts(rec, "/api/agents/debugger/run")
    assert body["dispatcher_run_id"] == "mgr-run"
    assert "caller_run_id" not in body  # would change parentage/card inheritance: out of scope


def test_dynamic_workflow_tool_identifies_caller(monkeypatch):
    rec = _record(monkeypatch)
    asyncio.run(mcp_server._call_tool("workflow_spec_pipeline", dict(ARGS)))
    (body,) = _posts(rec, "/api/workflows/spec-pipeline/run")
    assert body["dispatcher_run_id"] == "mgr-run" and "caller_run_id" not in body


def test_resume_payload_carries_dispatcher_even_when_no_callback_armed():
    _, payload = kd.resume_payload({"source_slug": "debugger", "session_id": "s"}, "p", "x",
                                   call_me_back=False, dispatcher_run_id="mgr-run")
    assert payload["dispatcher_run_id"] == "mgr-run" and payload["call_me_back"] is False
    assert "callback_origin_run_id" not in payload and "caller_run_id" not in payload


def test_resume_payload_without_dispatcher_is_unchanged():
    _, payload = kd.resume_payload({"source_slug": "a", "session_id": "s"}, "p", "x")
    assert "dispatcher_run_id" not in payload


def test_invoke_kanban_agent_forwards_dispatcher(monkeypatch):
    rec = _record(monkeypatch)
    asyncio.run(mcp_server._call_tool("invoke_kanban_agent", {
        "_gateway_caller_run_id": "mgr-run", "page_id": "page-1", "message": "go",
        "call_me_back": False}))
    (body,) = _posts(rec, "/api/agents/coder/run")
    assert body["dispatcher_run_id"] == "mgr-run"


def test_provisioner_allowlist_keeps_flow_handoff():
    from agents_platform_runners_app import agent_provisioner as ap
    src = Path(ap.__file__).read_text()
    assert '"flow_handoff"' in src


def _dispatch_via(dispatcher_run_id):
    import asyncio
    from agents_platform_runners_app import kanban_dispatch
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"run_id": "r1"})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await kanban_dispatch.PlatformClient(
                c, "http://x", dispatcher_run_id=dispatcher_run_id).dispatch(
                    "/api/agents/debugger/run", {"input": "x"})
    asyncio.run(go())
    return seen[0]


def test_ready_sweep_platform_client_forwards_initiating_run():
    assert _dispatch_via("mgr-run")["dispatcher_run_id"] == "mgr-run"


def test_ready_sweep_platform_client_stays_anonymous_for_watchdog():
    assert "dispatcher_run_id" not in _dispatch_via(None)


def test_both_mcp_sweep_entry_points_pass_the_caller_run():
    import inspect
    src = inspect.getsource(mcp_server._dispatch_tool)
    assert src.count("dispatcher_run_id=_caller_run_id(args)") >= 2
