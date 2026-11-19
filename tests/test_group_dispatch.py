"""MCP group selection preserves explicit overrides and deploy dedup guards."""
import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agents_platform_runners_app import mcp_server as m, work_loop


@pytest.mark.parametrize("override", [None, "", "variant"])
def test_group_dispatch(monkeypatch, override):
    seen = []
    def handler(request):
        seen.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"run_id": "r"})
    real_client = httpx.AsyncClient
    monkeypatch.setattr(m.httpx, "AsyncClient", lambda **kw: real_client(**{**kw, "transport": httpx.MockTransport(handler)}))
    monkeypatch.setenv("AW_WORKSPACE", "aw")
    args = {"group_slug": "coders", "input": "build", "target_slug": "target", "call_me_back": False}
    if override is not None:
        args["slug"] = override
    asyncio.run(m._call_tool("run_agent_async", args))
    path, body = seen[0]
    assert path == "/api/agent-groups/coders/run"
    assert body["workspace"] == "aw"
    assert body.get("agent_slug") == override
    assert "qa_rerun" not in body


def test_empty_override_returns_backend_rejection_instead_of_default_run(monkeypatch):
    def handler(request):
        body = json.loads(request.content)
        if body.get("agent_slug") == "":
            return httpx.Response(422, json={"detail": "invalid explicit member"})
        return httpx.Response(200, json={"run_id": "unintended-default-run"})
    real_client = httpx.AsyncClient
    monkeypatch.setattr(m.httpx, "AsyncClient", lambda **kw: real_client(**{**kw, "transport": httpx.MockTransport(handler)}))
    result = asyncio.run(m._call_tool("run_agent_async", {
        "group_slug": "coders", "slug": "", "input": "build", "target_slug": "target",
        "call_me_back": False,
    }))
    assert "422" in result[0].text
    assert "unintended-default-run" not in result[0].text


def test_lookup_uses_own_workspace(monkeypatch):
    seen = []
    def handler(request):
        seen.append(request.url)
        return httpx.Response(200, json={"agent_slug": "coder"})
    real_client = httpx.AsyncClient
    monkeypatch.setattr(m.httpx, "AsyncClient", lambda **kw: real_client(**{**kw, "transport": httpx.MockTransport(handler)}))
    monkeypatch.setenv("AW_WORKSPACE", "second")
    result = asyncio.run(m._call_tool("get_group_default", {"group_slug": "coders"}))
    assert seen[0].path == "/api/agent-groups/coders/default"
    assert seen[0].params["workspace"] == "second"
    assert json.loads(result[0].text)["agent_slug"] == "coder"


def test_deploy_dispatch_uses_qas_preserving_verified_deployment_rerun(monkeypatch):
    seen = []
    def handler(request):
        seen.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"run_id": "r"})
    monkeypatch.setattr(m.deploy_reconcile, "mark_qa_dispatched", lambda *a: None)
    row = {"page_id": "card", "target_slug": "target", "qa_dispatch_needed": True}
    board = Mock()
    board.card.return_value = {"body_md": "## Acceptance criteria\n- A1: Verify the panel."}

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            await m._deploy_followups(c, board, {"moved": [row]})
    asyncio.run(run())
    assert seen[0][0] == "/api/agent-groups/qas/run"
    assert seen[0][1]["qa_rerun"] is True
    assert "A1: Verify the panel." in seen[0][1]["input"]["input"]
    board.card.assert_called_once_with("card", include_comments=False)
    assert row["qa_dispatch"]["run_id"] == "r"


@pytest.mark.parametrize("slug,expected", [("qa", True), ("qa-haiku", True), ("quality", False), ("coder", False)])
def test_work_loop_qa_identity(slug, expected):
    assert work_loop.is_qa_agent(slug) is expected


def test_deploy_409_reads_existing_run_without_redispatch(monkeypatch):
    seen = []
    run_id = "a" * 32
    def handler(request):
        seen.append((request.method, request.url.path))
        if request.method == "POST":
            return httpx.Response(409, json={"detail": f"a QA run already exists: run {run_id} (qa)"})
        return httpx.Response(200, json={"id": run_id, "source_slug": "qa"})
    monkeypatch.setattr(m.deploy_reconcile, "mark_qa_dispatched", lambda *a: None)
    row = {"page_id": "card", "target_slug": "target", "qa_dispatch_needed": True}
    board = Mock()
    board.card.side_effect = m.kanban_dispatch.BoardUnavailable("board offline")

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            await m._deploy_followups(c, board, {"moved": [row]})
    asyncio.run(run())
    assert seen == [("POST", "/api/agent-groups/qas/run"), ("GET", f"/api/runs/{run_id}")]
    assert row["qa_dispatch"] == {"run_id": run_id, "existing": True}
