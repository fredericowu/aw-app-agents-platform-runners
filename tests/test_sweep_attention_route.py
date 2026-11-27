"""POST /kanban/sweep-attention: the kanban-sweep workflow's dispatch step."""
from __future__ import annotations

import sys
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents_platform_runners_app import kanban_dispatch as kd  # noqa: E402
from agents_platform_runners_app.routes import build_routes  # noqa: E402

CFG = {"execute_secret": "s3cr3t", "agents_platform_token": "tok"}
H = {"x-runner-secret": "s3cr3t"}


def test_requires_the_runner_secret():
    c = TestClient(build_routes(dict(CFG)))
    assert c.post("/kanban/sweep-attention", json={}).status_code == 401


def test_503_without_a_platform_token():
    c = TestClient(build_routes({"execute_secret": "s3cr3t"}))
    assert c.post("/kanban/sweep-attention", json={}, headers=H).status_code == 503


def test_rejects_a_bad_limit():
    c = TestClient(build_routes(dict(CFG)))
    assert c.post("/kanban/sweep-attention", json={"limit": 0}, headers=H).status_code == 422
    assert c.post("/kanban/sweep-attention", json={"limit": True}, headers=H).status_code == 422


def test_returns_the_per_card_results(monkeypatch):
    seen = {}
    seen_extra = {}

    async def fake(board, platform, *, limit, dry_run, agent_slug, **_):
        seen.update(limit=limit, dry_run=dry_run, agent_slug=agent_slug)
        seen_extra.update(_)
        return {"has_work": True, "considered": 1, "dispatched": 1, "dry_run": dry_run,
                "results": [{"page_id": "a", "run_id": "r1"}]}

    monkeypatch.setattr(kd, "sweep_attention", fake)
    c = TestClient(build_routes(dict(CFG)))
    r = c.post("/kanban/sweep-attention", json={"limit": 5}, headers=H)
    assert r.status_code == 200
    assert r.json()["ok"] is True and r.json()["dispatched"] == 1
    assert seen == {"limit": 5, "dry_run": False, "agent_slug": "project-manager"}


def test_board_down_is_a_502(monkeypatch):
    async def fake(*a, **k):
        raise kd.BoardUnavailable("down")

    monkeypatch.setattr(kd, "sweep_attention", fake)
    c = TestClient(build_routes(dict(CFG)))
    assert c.post("/kanban/sweep-attention", json={}, headers=H).status_code == 502


def test_plan_slug_is_forwarded_and_validated(monkeypatch):
    got = {}

    async def fake(board, platform, **kw):
        got.update(kw)
        return {"has_work": False, "considered": 0, "dispatched": 0, "dry_run": False, "results": []}

    monkeypatch.setattr(kd, "sweep_attention", fake)
    c = TestClient(build_routes(dict(CFG)))
    assert c.post("/kanban/sweep-attention", json={"plan_slug": "kanban-sweep"}, headers=H).status_code == 200
    assert got["plan_slug"] == "kanban-sweep"
    got.clear()
    assert c.post("/kanban/sweep-attention", json={}, headers=H).status_code == 200
    assert got["plan_slug"] is None
    for bad in ("../x", "", 5, "a b"):
        assert c.post("/kanban/sweep-attention", json={"plan_slug": bad}, headers=H).status_code == 422
