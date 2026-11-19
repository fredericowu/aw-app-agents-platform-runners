"""`/session-heals/doctor` — the contributes.doctor entry for dead-session
self-healing (card 41d5bf3b-9510-8160-89ad-f4293f36ccee): green with no rows,
red after a heal_failed row or repeated zero-token resumes, and it prints the
counts. Also pins the manifest entry and the unretire MCP tool."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents_platform_runners_app.routes import build_routes  # noqa: E402


def _client(monkeypatch, summary: dict | None, status: int = 200):
    real = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/sessions/heals/summary"
        return httpx.Response(status, json=summary or {})

    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda *a, **kw: real(*a, **{**kw, "transport": httpx.MockTransport(handler)}))
    return TestClient(build_routes({"agents_platform_token": "t",
                                    "agents_platform_url": "http://ap.test"}))


def test_green_with_zero_rows(monkeypatch):
    r = _client(monkeypatch, {"counts": {}, "total": 0, "zero_token_resume_failures": [],
                              "cli_mismatched_sessions": []}).get("/session-heals/doctor").json()
    assert r["ok"] is True and r["detail"]["problems"] == []


def test_red_after_a_heal_failed_row_and_prints_counts(monkeypatch):
    r = _client(monkeypatch, {"counts": {"heal_failed": 2, "healed": 5}, "total": 7,
                              "zero_token_resume_failures": []}).get("/session-heals/doctor").json()
    assert r["ok"] is False
    assert r["detail"]["heals_24h"] == {"heal_failed": 2, "healed": 5}
    assert "2 self-heal(s) ended heal_failed" in r["detail"]["problems"][0]


def test_red_on_repeated_zero_token_resumes(monkeypatch):
    z = [{"session_id": "01a1f32a-aaaa", "failures": 4, "last_at": "x"}]
    red = _client(monkeypatch, {"counts": {}, "total": 0, "zero_token_resume_failures": z}
                  ).get("/session-heals/doctor").json()
    assert red["ok"] is False and "01a1f32a" in red["detail"]["problems"][0]



def test_cli_mismatch_is_listed_but_not_red(monkeypatch):
    m = [{"session_id": "s", "agent_slug": "a", "session_cli": "codex", "agent_cli": "claude"}]
    ok = _client(monkeypatch, {"counts": {"healed": 1}, "total": 1, "zero_token_resume_failures": [],
                               "cli_mismatched_sessions": m}).get("/session-heals/doctor").json()
    assert ok["ok"] is True and ok["detail"]["cli_mismatched_sessions"] == m


def test_unreachable_platform_is_red(monkeypatch):
    r = _client(monkeypatch, None, status=500).get("/session-heals/doctor").json()
    assert r["ok"] is False


def test_manifest_declares_the_doctor_entry():
    manifest = json.loads((ROOT / "aw-app.json").read_text())
    assert {"label": "Dead-session self-heal", "route": "/session-heals/doctor"} in manifest["contributes"]["doctor"]


def test_wrong_schema_is_red_not_green(monkeypatch):
    for body in ({"status": "ok"}, [{"counts": {}}], {"counts": {}, "total": "0"}):
        r = _client(monkeypatch, body).get("/session-heals/doctor").json()
        assert r["ok"] is False and "unexpected shape" in r["detail"]["problems"][0], body


def test_unretire_tool_posts_to_ap_mt_and_returns_its_answer(monkeypatch):
    import asyncio
    from agents_platform_runners_app import mcp_server
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        return httpx.Response(200, json={"session_id": "s-1", "unretired": True})

    monkeypatch.setattr(mcp_server, "_agents_base_transport", lambda: httpx.MockTransport(handler))
    monkeypatch.setattr(mcp_server, "BASE", "http://ap.test")
    out = asyncio.run(mcp_server._dispatch_tool("unretire_session", {"session_id": "s-1"}))
    assert seen == [("POST", "/api/sessions/s-1/unretire")]
    assert '"unretired": true' in out[0].text
    bad = asyncio.run(mcp_server._dispatch_tool("unretire_session", {}))
    assert seen == [("POST", "/api/sessions/s-1/unretire")] and "session_id is required" in bad[0].text
