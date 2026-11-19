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
    assert seen[0].url.params.get("metadata") == "true"
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


def test_event_window_forwards_and_returns_the_id_tiebreak(monkeypatch):
    events = [{"id": "e1", "ts": "2026-11-19T12:00:00", "kind": "log", "payload": {}},
              {"id": "e2", "ts": "2026-11-19T12:00:00", "kind": "log", "payload": {}}]
    response, seen = _run_detail(
        monkeypatch,
        {"events_limit": 2, "events_after_ts": "2026-11-19T11:00:00", "events_after_id": "e0"},
        httpx.Response(200, json=events))
    run = json.loads(response)
    ev = [r for r in seen if r.url.path.endswith("/events")][0]
    assert ev.url.params["after_id"] == "e0"
    assert run["events_next_after_ts"] == "2026-11-19T12:00:00"
    assert run["events_next_after_id"] == "e2"


def test_execution_history_points_at_full_record_not_a_bare_detail_call():
    text = mcp_server._format_execution_history(
        "q", [{"run_id": "run-1", "score": 0.5, "text": "hit", "metadata": {}}], 5)
    assert "full_record=True" in text


def test_event_page_is_byte_bounded_and_resumable(monkeypatch):
    events = [{"id": f"e{i}", "ts": f"2026-11-19T12:00:{i:02d}", "kind": "tool_result",
               "payload": {"text": "line\n" * 4000}} for i in range(20)]
    response, _ = _run_detail(monkeypatch, {"events_limit": 20}, httpx.Response(200, json=events))
    run = json.loads(response)
    kept = run["events"]
    assert len(json.dumps(kept).encode()) < 40000
    assert 1 <= len(kept) < 20
    assert "events_truncated" in run
    assert run["events_next_after_id"] == kept[-1]["id"]
    assert all(len(e["payload"]["text"]) < 2200 for e in kept)


def test_one_event_can_be_fetched_in_full(monkeypatch):
    events = [{"id": f"e{i}", "ts": f"2026-11-19T12:00:{i:02d}", "kind": "tool_result",
               "payload": {"text": "z" * 20000}} for i in range(3)]
    response, _ = _run_detail(monkeypatch, {"event_id": "e1"}, httpx.Response(200, json=events))
    run = json.loads(response)
    assert [e["id"] for e in run["events"]] == ["e1"]
    assert len(run["events"][0]["payload"]["text"]) == 20000
