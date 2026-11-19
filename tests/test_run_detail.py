import asyncio
import json

import httpx

from agents_platform_runners_app import mcp_server


def _run_detail(monkeypatch, args, event_response):
    seen = []
    oversized = "💡" * 4000
    run = dict.fromkeys(mcp_server._RUN_SUMMARY_FIELDS, oversized)
    run.update(id="test-run", tokens_in=27, input={"input": oversized})

    def handler(request):
        seen.append(request)
        if request.url.path.endswith("/events"):
            return event_response
        return httpx.Response(200, json=run)

    client = httpx.AsyncClient
    monkeypatch.setattr(
        mcp_server.httpx, "AsyncClient",
        lambda **kwargs: client(**{**kwargs, "transport": httpx.MockTransport(handler)}),
    )
    response = asyncio.run(mcp_server._call_tool(
        "get_run_detail", {"run_id": "test-run", **args},
    ))[0].text
    return response, seen


def test_default_run_detail_stays_under_four_kib_and_never_fetches_events(monkeypatch):
    response, seen = _run_detail(monkeypatch, {}, None)
    run = json.loads(response)
    assert len(response.encode()) < 4096
    assert len(seen) == 1
    assert seen[0].url.params.get("summary") == "true"
    assert run["id"] == "test-run"
    assert run["tokens_in"] == 27
    assert "input" not in run
    assert "events" not in run


def test_explicit_full_record_does_not_fetch_trace(monkeypatch):
    response, seen = _run_detail(monkeypatch, {"full_record": True}, None)
    assert "input" in json.loads(response)
    assert len(seen) == 1
    assert "summary" not in seen[0].url.params


def test_legacy_events_limit_returns_full_record_and_scoped_trace(monkeypatch):
    png = "iVBORw0KGgo" + "A" * 120
    ts = "2026-11-19T08:00:00"
    event = {
        "ts": ts, "kind": "tool_result",
        "payload": {"content": [
            {"type": "image", "data": png, "artifact_path": "/test/proof.png"},
            {"type": "text", "text": f"Screenshot: data:image/png;base64,{png}"},
        ]},
    }
    response, seen = _run_detail(
        monkeypatch, {"events_limit": 1, "events_after_ts": ts, "event_kinds": "tool_result"},
        httpx.Response(200, json=[event]),
    )
    run = json.loads(response)
    assert "input" in run
    assert dict(seen[1].url.params) == {"after_ts": ts, "kinds": "tool_result", "limit": "1"}
    assert run["events_next_after_ts"] == ts
    assert "/test/proof.png" in response
    assert png not in response


def test_event_error_is_distinct_from_empty_trace(monkeypatch):
    args = {"events_limit": 2, "full_record": False}
    response, _ = _run_detail(monkeypatch, args, httpx.Response(200, json=[]))
    assert json.loads(response)["events"] == []
    monkeypatch.undo()

    response, _ = _run_detail(monkeypatch, args, httpx.Response(502))
    run = json.loads(response)
    assert run["events_error"] == {"status": 502}
    assert "events" not in run
    assert "input" not in run
