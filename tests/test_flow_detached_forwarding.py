"""`flow_detached` passthrough on run_agent_async / run_agents_parallel
(card 41e5bf3b). Asserted on the outgoing request body, same approach as
test_qa_rerun_forwarding.py: a field the schema accepts and the handler drops
reaches AP-MT as a plain flow dispatch with no error."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents_platform_runners_app import mcp_server  # noqa: E402


def _call(monkeypatch, name, args) -> dict:
    recorder: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        recorder.append(request)
        return httpx.Response(200, json={"run_id": "r1", "target_id": "t1"})

    real = httpx.AsyncClient
    monkeypatch.setattr(mcp_server.httpx, "AsyncClient",
                        lambda *a, **k: real(*a, **{**k, "transport": httpx.MockTransport(handler)}))
    asyncio.run(mcp_server._call_tool(name, args))
    assert len(recorder) == 1
    return json.loads(recorder[0].content)


def _async(monkeypatch, **extra):
    return _call(monkeypatch, "run_agent_async",
                 {"slug": "coder-haiku", "target_slug": "t", "input": "x", **extra})


def test_flow_detached_true_is_forwarded(monkeypatch):
    assert _async(monkeypatch, flow_detached=True)["flow_detached"] is True


@pytest.mark.parametrize("extra", [{}, {"flow_detached": False}])
def test_flow_detached_omitted_unless_true(monkeypatch, extra):
    assert "flow_detached" not in _async(monkeypatch, **extra)


def test_parallel_passes_item_and_top_level_values(monkeypatch):
    body = _call(monkeypatch, "run_agents_parallel", {
        "agents": [{"slug": "coder-haiku", "input": "a"},
                   {"slug": "coder-haiku", "input": "b", "flow_detached": False}],
        "target_slug": "t", "flow_detached": True})
    assert body["flow_detached"] is True
    assert "flow_detached" not in body["agents"][0]
    assert body["agents"][1]["flow_detached"] is False


def test_schemas_declare_the_param_on_both_tools():
    tools = {t.name: t for t in asyncio.run(mcp_server._list_tools())}
    a = tools["run_agent_async"].inputSchema["properties"]["flow_detached"]
    assert a["type"] == "boolean" and a["default"] is False
    p = tools["run_agents_parallel"].inputSchema["properties"]
    assert p["flow_detached"]["type"] == "boolean"
    assert "flow_detached" in p["agents"]["items"]["properties"]
