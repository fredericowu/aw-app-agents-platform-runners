"""MCP contract for the recursive run cost analytics wrapper."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from agents_platform_runners_app import mcp_server


def _transport(handler):
    return httpx.MockTransport(handler)


def _call(monkeypatch, handler, arguments):
    monkeypatch.setattr(mcp_server, "_agents_base_transport",
                        lambda: _transport(handler))
    return asyncio.run(mcp_server._call_tool("run_cost_analytics", arguments))


def _payload(result):
    return json.loads(result[0].text)


def test_tool_is_registered_without_top_level_union(monkeypatch):
    monkeypatch.setattr(mcp_server, "_ensure_running", lambda: None)
    monkeypatch.setattr(
        mcp_server,
        "_agents_base_transport",
        lambda: _transport(lambda _request: httpx.Response(200, json=[])),
    )
    tools = asyncio.run(mcp_server._list_tools())
    tool = next(tool for tool in tools if tool.name == "run_cost_analytics")

    assert set(tool.inputSchema["properties"]) == {"card_id", "run_id"}
    assert "required" not in tool.inputSchema
    assert "oneOf" not in tool.inputSchema
    assert "anyOf" not in tool.inputSchema


@pytest.mark.parametrize(("arguments", "expected_params"), [
    ({"card_id": " 41b5bf3b-9510-8190-beeb-e2115e5be76f "},
     {"card_id": "41b5bf3b-9510-8190-beeb-e2115e5be76f"}),
    ({"run_id": " run-123 "}, {"run_id": "run-123"}),
])
def test_selector_is_trimmed_and_forwarded(monkeypatch, arguments, expected_params):
    def handler(request):
        assert request.url.path == "/api/runs/cost-analytics"
        assert dict(request.url.params) == expected_params
        return httpx.Response(200, json={"totals": {"run_count": 1}})

    assert _payload(_call(monkeypatch, handler, arguments)) == {
        "totals": {"run_count": 1}
    }


@pytest.mark.parametrize("arguments", [
    {},
    {"card_id": "", "run_id": "  "},
    {"card_id": "card", "run_id": "run"},
])
def test_mutual_exclusion_is_validated_before_http(monkeypatch, arguments):
    def handler(_request):
        raise AssertionError("validation must happen before the HTTP request")

    result = _call(monkeypatch, handler, arguments)
    assert "400" in result[0].text
    assert "exactly one of card_id or run_id" in result[0].text


@pytest.mark.parametrize("status", [404, 409, 422])
def test_backend_errors_are_propagated(monkeypatch, status):
    body = {"detail": f"backend-{status}"}

    def handler(_request):
        return httpx.Response(status, json=body)

    result = _call(monkeypatch, handler, {"run_id": "missing"})
    assert str(status) in result[0].text
    assert f"backend-{status}" in result[0].text
