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

    assert set(tool.inputSchema["properties"]) == {
        "card_id", "run_id", "filter_stale", "filter_stale_count", "offset", "statuses",
        "min_idle_hours", "include_tool_stats", "include_github", "repo", "workflow",
        "facts_only", "exclude_agents", "honor_stamp", "stamp_ttl_hours", "ci_since"}
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


def test_new_params_forwarded_only_when_passed(monkeypatch):
    seen = {}

    def handler(request):
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json={})

    _call(monkeypatch, handler, {"card_id": "c", "include_tool_stats": False,
                                 "include_github": True, "repo": " o/r ", "workflow": "ci.yml",
                                 "filter_stale_count": 9, "offset": 3})
    # stale-only params are not sent without filter_stale
    assert seen["params"] == {"card_id": "c", "include_tool_stats": "false",
                              "include_github": "true", "repo": "o/r", "workflow": "ci.yml"}
    _call(monkeypatch, handler, {"run_id": "r"})
    assert seen["params"] == {"run_id": "r"}   # nothing new leaks to an older AP-MT


def test_filter_stale_forwards_window_params(monkeypatch):
    seen = {}

    def handler(request):
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json={"cards": []})

    _call(monkeypatch, handler, {"filter_stale": True, "filter_stale_count": -1, "offset": 5,
                                 "min_idle_hours": 12.5, "statuses": ["running", " need_human "],
                                 "include_tool_stats": True})
    assert seen["params"] == {"filter_stale": "true", "filter_stale_count": "-1", "offset": "5",
                              "min_idle_hours": "12.5", "statuses": "running,need_human",
                              "include_tool_stats": "true"}
    _call(monkeypatch, handler, {"filter_stale": True})
    assert seen["params"] == {"filter_stale": "true"}


@pytest.mark.parametrize("arguments", [
    {"filter_stale": True, "card_id": "c"},
    {"filter_stale": True, "run_id": "r"},
])
def test_filter_stale_rejects_selectors_before_http(monkeypatch, arguments):
    def handler(_request):
        raise AssertionError("validation must happen before the HTTP request")

    result = _call(monkeypatch, handler, arguments)
    assert "400" in result[0].text and "filter_stale" in result[0].text


def test_filter_stale_false_keeps_exactly_one_selector_rule(monkeypatch):
    def handler(_request):
        raise AssertionError("validation must happen before the HTTP request")

    result = _call(monkeypatch, handler, {"filter_stale": False})
    assert "exactly one of card_id or run_id" in result[0].text


def test_v11_params_forwarded_only_when_passed(monkeypatch):
    seen = {}

    def handler(request):
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json={"cards": []})

    _call(monkeypatch, handler, {"filter_stale": True, "facts_only": True, "honor_stamp": False,
                                 "stamp_ttl_hours": 24, "exclude_agents": " "})
    assert seen["params"] == {"filter_stale": "true", "facts_only": "true", "honor_stamp": "false",
                              "stamp_ttl_hours": "24", "exclude_agents": ""}
    _call(monkeypatch, handler, {"card_id": "41b5bf3b-9510-8190-beeb-e2115e5be76f",
                                 "facts_only": True, "repo": "o/r", "workflow": "w.yml",
                                 "ci_since": "2026-11-22T10:00:00Z"})
    assert seen["params"] == {"card_id": "41b5bf3b-9510-8190-beeb-e2115e5be76f",
                              "facts_only": "true", "repo": "o/r", "workflow": "w.yml",
                              "ci_since": "2026-11-22T10:00:00Z"}
    _call(monkeypatch, handler, {"filter_stale": True})
    assert seen["params"] == {"filter_stale": "true"}     # nothing new leaks to an older AP-MT
