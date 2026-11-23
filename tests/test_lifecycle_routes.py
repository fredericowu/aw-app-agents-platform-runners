"""/healthy and /lifecycle/restart (graceful restart card 4215bf3b, slice S3).

The core side (src.apps.gateway_readiness from core PR 57, and the
``aw-workspace-cli restart core`` binary) is mocked: nothing here restarts or
queries anything real.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents_platform_runners_app import execute as execute_mod  # noqa: E402
from agents_platform_runners_app import lifecycle as lc  # noqa: E402
from agents_platform_runners_app import shared_redis  # noqa: E402
from agents_platform_runners_app.routes import build_routes  # noqa: E402

SECRET = "s3cret"
H = {"X-Runner-Secret": SECRET}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(execute_mod, "CONTAINER_SOCKET", "/var/run/x.sock")
    monkeypatch.setattr(shared_redis, "resolve", lambda cfg=None: "redis://r:6379/0")
    monkeypatch.setenv("AW_WORKSPACE_BOOT_ID", "boot-1")
    monkeypatch.setenv("AW_WORKSPACE_GIT_HEAD", "abc123")
    monkeypatch.setenv("AW_WORKSPACE_VERSION", "v9")
    monkeypatch.setattr(lc, "_ping_redis", lambda url: None)
    monkeypatch.setattr(lc, "_ping_container_engine", lambda path: None)


def _fake_readiness(monkeypatch, status):
    mod = types.ModuleType("src.apps.gateway_readiness")

    async def gateway_readiness(runtime):
        if isinstance(status, Exception):
            raise status
        return status

    def is_ready(st):
        if st.get("reachable") is None:
            return True
        return bool(st["reachable"]) and not st.get("not_ready")

    mod.gateway_readiness, mod.is_ready = gateway_readiness, is_ready
    for name in ("src", "src.apps"):
        monkeypatch.setitem(sys.modules, name, sys.modules.get(name) or types.ModuleType(name))
    monkeypatch.setitem(sys.modules, "src.apps.gateway_readiness", mod)


def _client(runtime_provider=lambda: object(), **cfg):
    cfg.setdefault("execute_secret", SECRET)
    return TestClient(build_routes(cfg, runtime_provider=runtime_provider))


OK = {"reachable": True, "tools": 600, "local_upstreams": ["a", "b"], "not_ready": []}


# ------------------------------------------------------------------ /healthy

def test_healthy_requires_secret():
    c = _client()
    assert c.get("/healthy").status_code == 401
    assert c.get("/healthy", headers={"X-Runner-Secret": "bad"}).status_code == 401


def test_healthy_unconfigured_secret_is_500():
    assert _client(execute_secret="").get("/healthy", headers=H).status_code == 500


def test_healthy_ready(monkeypatch):
    _fake_readiness(monkeypatch, OK)
    r = _client().get("/healthy", headers=H)
    assert r.status_code == 200
    b = r.json()
    assert b["ready"] is True and b["can_execute"] is True and b["not_ready"] == []
    assert (b["boot_id"], b["git_head"], b["workspace_version"]) == ("boot-1", "abc123", "v9")
    assert b["gateway"]["tools"] == 600 and b["gateway"]["local_upstreams"] == 2


def test_healthy_not_ready_when_upstream_disconnected(monkeypatch):
    nr = [{"kind": "dead_upstream", "server": "kanban", "app": "notion", "reason": "x"}]
    _fake_readiness(monkeypatch, {**OK, "not_ready": nr})
    b = _client().get("/healthy", headers=H).json()
    assert b["ready"] is False and b["can_execute"] is True
    assert b["not_ready"] == nr and b["gateway"]["ready"] is False


def test_healthy_gateway_unreachable_is_not_ready(monkeypatch):
    _fake_readiness(monkeypatch, {"reachable": False, "degraded": True, "note": "down"})
    assert _client().get("/healthy", headers=H).json()["ready"] is False


def test_healthy_gateway_not_installed_counts_ready(monkeypatch):
    _fake_readiness(monkeypatch, {"reachable": None, "degraded": False})
    assert _client().get("/healthy", headers=H).json()["ready"] is True


def test_healthy_readiness_exception_fails_closed(monkeypatch):
    _fake_readiness(monkeypatch, RuntimeError("boom"))
    b = _client().get("/healthy", headers=H).json()
    assert b["ready"] is False
    assert b["not_ready"][0]["kind"] == "gateway_check_unavailable"


def test_healthy_missing_core_helper_fails_closed(monkeypatch):
    monkeypatch.setitem(sys.modules, "src.apps.gateway_readiness", None)  # ImportError
    assert _client().get("/healthy", headers=H).json()["ready"] is False


def test_healthy_cannot_execute_without_socket(monkeypatch):
    _fake_readiness(monkeypatch, OK)
    monkeypatch.setattr(execute_mod, "CONTAINER_SOCKET", "")
    b = _client().get("/healthy", headers=H).json()
    assert b["can_execute"] is False and b["ready"] is False
    assert {"kind": "can_execute", "server": None, "app": None,
            "reason": "container_socket_not_set"} in b["not_ready"]


def test_healthy_cannot_execute_without_redis(monkeypatch):
    _fake_readiness(monkeypatch, OK)
    monkeypatch.setattr(shared_redis, "resolve", lambda cfg=None: None)
    assert _client().get("/healthy", headers=H).json()["ready"] is False


def _boom(*_a):
    raise ConnectionError("refused")


def test_healthy_configured_but_unreachable_redis_is_not_ready(monkeypatch):
    _fake_readiness(monkeypatch, OK)
    monkeypatch.setattr(lc, "_ping_redis", _boom)
    b = _client().get("/healthy", headers=H).json()
    assert b["ready"] is False and b["can_execute"] is False
    assert any(n["reason"] == "shared_redis_unreachable" for n in b["not_ready"])


def test_healthy_configured_but_dead_container_engine_is_not_ready(monkeypatch):
    _fake_readiness(monkeypatch, OK)
    monkeypatch.setattr(lc, "_ping_container_engine", _boom)
    b = _client().get("/healthy", headers=H).json()
    assert b["ready"] is False
    assert any(n["reason"] == "container_engine_unreachable" for n in b["not_ready"])


def test_restart_leading_hyphen_id_is_one_token(monkeypatch):
    calls: list = []
    _cli(monkeypatch, {"ok": True, "status": "dispatched"}, calls=calls)
    r = _client().post("/lifecycle/restart", headers=H, json={"request_id": "-lock"})
    assert r.status_code == 202
    assert "--request-id=-lock" in calls[0] and "-lock" not in calls[0]


def test_restart_unstartable_cli_is_structured_503(monkeypatch):
    monkeypatch.setenv(lc.CLI_ENV, "/nonexistent/cli")
    r = _client().post("/lifecycle/restart", headers=H, json={"request_id": "k"})
    assert r.status_code == 503 and r.json()["status"] == "cli_unavailable"
    assert r.json()["accepted"] is False


def test_healthy_standalone_skips_gateway():
    b = _client(runtime_provider=None).get("/healthy", headers=H).json()
    assert b["ready"] is True and b["gateway"]["checked"] is False


# ------------------------------------------------------- /lifecycle/restart

def _cli(monkeypatch, obj=None, code=0, out=None, calls=None):
    import json as _json
    monkeypatch.setenv(lc.CLI_ENV, "/bin/fake-cli")

    async def runner(argv, timeout):
        if calls is not None:
            calls.append(argv)
        return code, out if out is not None else _json.dumps(obj), ""

    monkeypatch.setattr(lc, "_run_cli", runner)


def test_restart_requires_secret():
    assert _client().post("/lifecycle/restart", json={"request_id": "a"}).status_code == 401


@pytest.mark.parametrize("rid", ["", "a b", "abc\n", "x" * 65, "../x", 5, None])
def test_restart_invalid_request_id_never_runs_cli(monkeypatch, rid):
    calls: list = []
    _cli(monkeypatch, {"ok": True, "status": "dispatched"}, calls=calls)
    r = _client().post("/lifecycle/restart", headers=H, json={"request_id": rid})
    assert r.status_code == 400 and r.json()["status"] == "invalid_request_id"
    assert calls == []


def test_restart_bad_body():
    c = _client()
    assert c.post("/lifecycle/restart", headers=H, content=b"nope").status_code == 400
    assert c.post("/lifecycle/restart", headers=H, json=[1]).status_code == 400


def test_restart_dispatched_passes_cli_object_through(monkeypatch):
    calls: list = []
    obj = {"ok": True, "exit_code": 0, "status": "dispatched", "expected_head": "h",
           "boot_id_before": "b0", "image_digest_before": "sha256:1", "receipt": "/r.json"}
    _cli(monkeypatch, obj, calls=calls)
    r = _client().post("/lifecycle/restart", headers=H, json={"request_id": "lock-1"})
    assert r.status_code == 202
    assert r.json() == {**obj, "accepted": True, "request_id": "lock-1"}
    assert calls == [["/bin/fake-cli", "restart", "core", "--request-id=lock-1", "--json"]]
    assert "--wait" not in calls[0]


def test_restart_replay_is_accepted_already_dispatched(monkeypatch):
    _cli(monkeypatch, {"ok": True, "exit_code": 0, "status": "already_dispatched",
                       "boot_id_before": "b0"})
    r = _client().post("/lifecycle/restart", headers=H, json={"request_id": "lock-1"})
    assert r.status_code == 202 and r.json()["accepted"] is True
    assert r.json()["boot_id_before"] == "b0"


@pytest.mark.parametrize("status,http", [
    ("not_configured", 503), ("preflight_failed", 503), ("container_not_found", 503),
    ("dispatch_failed", 502), ("dispatch_pending", 409), ("dispatch_unconfirmed", 409),
    ("brand_new_status", 502),
])
def test_restart_refusals_are_structured(monkeypatch, status, http):
    _cli(monkeypatch, {"ok": False, "exit_code": 1, "status": status, "error": "why"}, code=1)
    r = _client().post("/lifecycle/restart", headers=H, json={"request_id": "k"})
    assert r.status_code == http
    assert r.json()["accepted"] is False and r.json()["status"] == status
    assert r.json()["error"] == "why"


def test_restart_unparseable_cli_output(monkeypatch):
    _cli(monkeypatch, out="Traceback...", code=2)
    r = _client().post("/lifecycle/restart", headers=H, json={"request_id": "k"})
    assert r.status_code == 502 and r.json()["status"] == "cli_failed"
    assert r.json()["accepted"] is False


def test_restart_cli_missing(monkeypatch):
    monkeypatch.delenv(lc.CLI_ENV, raising=False)
    monkeypatch.setattr(lc.shutil, "which", lambda _n: None)
    r = _client().post("/lifecycle/restart", headers=H, json={"request_id": "k"})
    assert r.status_code == 503 and r.json()["status"] == "cli_unavailable"


def test_restart_tolerates_stray_stdout_lines(monkeypatch):
    _cli(monkeypatch, out='warning: x\n{"ok": true, "status": "dispatched"}\n')
    assert _client().post("/lifecycle/restart", headers=H,
                          json={"request_id": "k"}).status_code == 202
