"""Tests for the activation-funnel ping (card 4175bf3b)."""
from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents_platform_runners_app import funnel_hook  # noqa: E402

JOB = {"cli": "claude", "model": "claude-haiku-4-5"}
OK_RUN = {"status": "success", "category": "work", "ended_at": 1.0,
          "input": "hi", "initiator_kind": "agent_run", "model_slug": "haiku-4-5"}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("AW_BACKEND_URL", "https://api.example")
    monkeypatch.setenv("AW_WORKSPACE", "ws1")
    monkeypatch.setenv("AW_WORKSPACE_HOST_TOKEN", "awlk_x")
    funnel_hook._latch.clear()
    yield
    funnel_hook._latch.clear()


class _Resp:
    def __init__(self, status=200):
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("x", request=None, response=None)


def _install(monkeypatch, run, post=None):
    calls = []
    monkeypatch.setattr(funnel_hook, "_wait_finalized", lambda *_a, **_k: run)

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append((url, json, headers))
        return post() if post else _Resp()

    monkeypatch.setattr(funnel_hook.httpx, "post", fake_post)
    return calls


def test_success_work_run_pings_with_raw_facts(monkeypatch):
    calls = _install(monkeypatch, OK_RUN)
    assert funnel_hook.report_run("r1", JOB) is True
    (url, body, headers), = calls
    assert url == "https://api.example/api/workspaces/ws1/funnel/events"
    assert body == {"event": "run_succeeded", "model": "claude-haiku-4-5",
                    "model_slug": "haiku-4-5", "cli": "claude"}
    assert headers == {"Authorization": "Bearer awlk_x"}


@pytest.mark.parametrize("patch", [
    {"status": "error"}, {"status": "cancelled"}, {"status": "running"},
    {"category": "monitor"}, {"category": None},
    {"input": "/compact"}, {"input": {"input": "/compact"}},
    {"initiator_kind": "auto_compact"},
    {"initiator_kind": "pending_compact"},
])
def test_non_counting_runs_never_ping(monkeypatch, patch):
    calls = _install(monkeypatch, {**OK_RUN, **patch})
    assert funnel_hook.report_run("r1", JOB) is False
    assert calls == []


def test_raw_command_job_never_counts(monkeypatch):
    calls = _install(monkeypatch, OK_RUN)
    assert funnel_hook.report_run("r1", {**JOB, "raw_command": "ls"}) is False
    assert funnel_hook.start("r1", {**JOB, "raw_command": "ls"}) is None
    assert calls == []


def test_inert_without_backend_link(monkeypatch):
    monkeypatch.delenv("AW_WORKSPACE_HOST_TOKEN")
    calls = _install(monkeypatch, OK_RUN)
    assert funnel_hook.report_run("r1", JOB) is False
    assert funnel_hook.start("r1", JOB) is None
    assert calls == []


def test_backend_failure_never_raises_and_does_not_latch(monkeypatch):
    calls = _install(monkeypatch, OK_RUN, post=lambda: _Resp(500))
    assert funnel_hook.report_run("r1", JOB) is False
    assert funnel_hook._latch == set()
    # next success retries (latch only set after a 2xx)
    monkeypatch.setattr(funnel_hook.httpx, "post", lambda *a, **k: _Resp())
    assert funnel_hook.report_run("r2", JOB) is True


def test_latch_is_per_model_and_skips_repeat(monkeypatch):
    calls = _install(monkeypatch, OK_RUN)
    assert funnel_hook.report_run("r1", JOB) is True
    assert funnel_hook.report_run("r2", JOB) is False          # same model: no ping
    assert funnel_hook.report_run("r3", {**JOB, "model": "claude-opus-5-5"}) is True
    assert len(calls) == 2


def test_latch_distinguishes_slugs_behind_an_unclassified_model(monkeypatch):
    calls = _install(monkeypatch, {**OK_RUN, "model_slug": "haiku-4-5"})
    job = {**JOB, "model": "default"}
    assert funnel_hook.report_run("r1", job) is True
    monkeypatch.setattr(funnel_hook, "_wait_finalized",
                        lambda *_a, **_k: {**OK_RUN, "model_slug": "sonnet-4-5"})
    assert funnel_hook.report_run("r2", job) is True          # different tier input
    assert len(calls) == 2


def test_start_bounds_concurrent_pollers(monkeypatch):
    import threading
    gate = threading.Event()
    monkeypatch.setattr(funnel_hook, "_slots", threading.BoundedSemaphore(2))
    monkeypatch.setattr(funnel_hook, "report_run", lambda *_a: gate.wait(5))
    _install(monkeypatch, OK_RUN)
    monkeypatch.setattr(funnel_hook, "report_run", lambda *_a: gate.wait(5))
    t1, t2 = funnel_hook.start("r1", JOB), funnel_hook.start("r2", JOB)
    assert t1 and t2
    assert funnel_hook.start("r3", JOB) is None               # no free slot
    gate.set(); t1.join(2); t2.join(2)
    assert funnel_hook.start("r4", JOB) is not None           # slot released


def test_wait_failure_never_raises(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("ap-mt down")
    monkeypatch.setattr(funnel_hook, "_wait_finalized", boom)
    assert funnel_hook.report_run("r1", JOB) is False


def test_secret_shaped_model_is_dropped(monkeypatch):
    calls = _install(monkeypatch, OK_RUN)
    funnel_hook.report_run("r1", {**JOB, "model": "sk-ant-secret"})
    assert calls[0][1]["model"] is None


def test_wait_finalized_polls_until_ended(monkeypatch):
    seq = [{"status": "running", "ended_at": None}, {"status": "success", "ended_at": 5.0}]

    class FakeClient:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url, params=None):
            assert params == {"metadata": "true"}   # bounded projection, not the full run
            body = seq.pop(0)
            return type("R", (), {"raise_for_status": lambda s: None, "json": lambda s: body})()

    monkeypatch.setattr(funnel_hook.httpx, "Client", FakeClient)
    monkeypatch.setattr(funnel_hook.time, "sleep", lambda _s: None)
    run = funnel_hook._wait_finalized("r1", {})
    assert run["status"] == "success" and seq == []
