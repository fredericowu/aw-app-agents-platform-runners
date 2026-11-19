"""Regression test for the ~8h forced-relogin bug
(docs/knowledge_base/memory/aw-workspace-runner-claude-oauth-token-not-refreshed-relogin-20260807.md).

The Claude OAuth access token has an ~8h TTL and is only ever written by the
workspace's own login. The old code copied $HOME/.claude into the workspace
tree before every spawn and mounted .credentials.json READ-ONLY, so a token
refresh the spawned CLI performed could never persist back — auth silently
died every ~8h until a human re-logged in.

Fix (Frederico 2026-08-07, replicating agentic-workspace docker_agent.py):
when AW_WORKSPACE_HOME_HOST_DIR is known, mount the LIVE $HOME/.claude rw
DIRECTLY as the sibling container's ~/.claude — no copy, no read-only shadow —
so refreshes land on the source of truth. This test pins that contract and
that the legacy copy+RO fallback still applies when the host $HOME path is
unknown.

Run: .venv/aw/bin/python -m pytest tests/test_creds_mount.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents_platform_runners_app import execute as execute_mod  # noqa: E402


def _make_home(tmp_path: Path) -> Path:
    """A fake $HOME with a populated .claude creds dir + .claude.json."""
    home = tmp_path / "home"
    claude = home / ".claude"
    claude.mkdir(parents=True)
    (claude / ".credentials.json").write_text('{"claudeAiOauth": {"accessToken": "tok"}}')
    (home / ".claude.json").write_text("{}")
    return home


def _volumes_for(job: dict) -> dict:
    _image, _argv, kwargs, _mcp = execute_mod._build_container_kwargs(job)
    return kwargs["volumes"]


def test_direct_home_mount_is_rw_and_uncopied(tmp_path, monkeypatch):
    home = _make_home(tmp_path)
    home_host = "/host/aw-workspace-home"  # what the podman daemon sees for $HOME
    monkeypatch.setattr(execute_mod, "REAL_HOME", str(home))
    monkeypatch.setattr(execute_mod, "WORKSPACE_HOME_HOST_DIR", home_host)
    monkeypatch.setattr(execute_mod, "WORKSPACE_HOST_DIR", "/host/aw-workspace")
    monkeypatch.setattr(execute_mod, "WORKSPACE_CONTAINER_DIR", str(tmp_path / "ws"))
    # A blanking resync must NOT happen in direct mode — fail loudly if called.
    monkeypatch.setattr(execute_mod, "_sync_home_creds_into_workspace",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("resync must not run in direct mode")))

    vols = _volumes_for({"run_id": "r1", "cli": "claude", "prompt": "hi"})

    # .claude mounted rw straight from the LIVE $HOME host path — no copy.
    claude_src = f"{home_host}/.claude"
    assert claude_src in vols, vols
    assert vols[claude_src] == {"bind": "/home/ubuntu/.claude", "mode": "rw"}

    # ~/.claude.json is NOT bind-mounted (shared single-file inode corrupts
    # under concurrent CLIs); a per-run seed is used instead.
    assert not any(v["bind"] == "/home/ubuntu/.claude.json" for v in vols.values()), vols

    # No read-only .credentials.json shadow anywhere — that's what blocked
    # refresh persistence.
    assert not any(m["mode"] == "ro" and m["bind"].endswith(".credentials.json")
                   for m in vols.values()), vols

    # No separate isolated mount: it lives inside the whole-.claude mount.
    assert not any(b == "/home/ubuntu/.claude/isolated/r1" for b in
                   (v["bind"] for v in vols.values()))


def test_oauth_token_injected_as_env(tmp_path, monkeypatch):
    home = _make_home(tmp_path)
    monkeypatch.setattr(execute_mod, "REAL_HOME", str(home))
    monkeypatch.setattr(execute_mod, "WORKSPACE_HOME_HOST_DIR", "/host/aw-workspace-home")
    monkeypatch.setattr(execute_mod, "WORKSPACE_HOST_DIR", "/host/aw-workspace")
    monkeypatch.setattr(execute_mod, "WORKSPACE_CONTAINER_DIR", str(tmp_path / "ws"))
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)

    tok_file = tmp_path / "secrets" / "claude_code_oauth_token"
    tok_file.parent.mkdir(parents=True)
    tok_file.write_text("sk-ant-oat01-DURABLE\n")
    monkeypatch.setattr(execute_mod, "CLAUDE_OAUTH_TOKEN_FILE", str(tok_file))

    _img, _argv, kw, _mcp = execute_mod._build_container_kwargs({"run_id": "r3", "cli": "claude", "prompt": "hi"})
    # Trimmed, injected as env — durable auth that never blanks the creds file.
    assert kw["environment"]["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-DURABLE"

    # Not injected for a non-claude CLI (they don't read this var).
    _img, _argv, kw2, _mcp2 = execute_mod._build_container_kwargs({"run_id": "r4", "cli": "codex", "prompt": "hi"})
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in kw2["environment"]


def test_fallback_copies_and_shadows_credentials_ro(tmp_path, monkeypatch):
    home = _make_home(tmp_path)
    ws = tmp_path / "ws"
    monkeypatch.setattr(execute_mod, "REAL_HOME", str(home))
    monkeypatch.setattr(execute_mod, "WORKSPACE_HOME_HOST_DIR", "")  # unknown -> fallback
    monkeypatch.setattr(execute_mod, "WORKSPACE_HOST_DIR", "/host/aw-workspace")
    monkeypatch.setattr(execute_mod, "WORKSPACE_CONTAINER_DIR", str(ws))

    called = {"resync": False}

    def fake_resync(real_home, spec):
        called["resync"] = True
        # emulate the copy so the mount guards see a populated tree
        dst = ws / ".claude"
        dst.mkdir(parents=True, exist_ok=True)
        (dst / ".credentials.json").write_text("{}")
        (ws / ".claude.json").write_text("{}")

    monkeypatch.setattr(execute_mod, "_sync_home_creds_into_workspace", fake_resync)

    vols = _volumes_for({"run_id": "r2", "cli": "claude", "prompt": "hi"})

    assert called["resync"], "fallback must resync creds into the workspace tree"
    # .credentials.json mounted read-only in the fallback (blank-write guard).
    cred_src = f"/host/aw-workspace/.claude/.credentials.json"
    assert vols[cred_src] == {"bind": "/home/ubuntu/.claude/.credentials.json", "mode": "ro"}


def _direct_env(tmp_path, monkeypatch, home):
    monkeypatch.setattr(execute_mod, "REAL_HOME", str(home))
    monkeypatch.setattr(execute_mod, "WORKSPACE_HOME_HOST_DIR", "/host/aw-workspace-home")
    monkeypatch.setattr(execute_mod, "WORKSPACE_HOST_DIR", "/host/aw-workspace")
    monkeypatch.setattr(execute_mod, "WORKSPACE_CONTAINER_DIR", str(tmp_path / "ws"))
    monkeypatch.setattr(execute_mod, "_CLAUDE_JSON_READ_RETRY_S", 0)


def _build(job_id):
    return execute_mod._build_container_kwargs({"run_id": job_id, "cli": "claude", "prompt": "hi"})


def test_seed_written_parseable_and_wired(tmp_path, monkeypatch):
    import json, os, stat
    home = _make_home(tmp_path)
    (home / ".claude.json").write_text('{"oauthAccount": {"a": 1}, "userID": "u1"}')
    _direct_env(tmp_path, monkeypatch, home)
    _img, argv, kw, _mcp = _build("s1")
    seed = home / ".claude" / "isolated" / "s1" / "claude.json.seed"
    assert json.loads(seed.read_text()) == {"oauthAccount": {"a": 1}, "userID": "u1"}
    assert stat.S_IMODE(os.stat(seed).st_mode) == 0o600
    assert kw["environment"]["AW_CLAUDE_JSON_SEED"] == "/home/ubuntu/.claude/isolated/s1/claude.json.seed"
    # cold wrapper: copy-if-absent then exec so stdin (the prompt) passes through
    assert argv[:2] == ["sh", "-c"] and 'exec "$@"' in argv[2]
    assert "aw-cli-with-quota" in argv


def test_corrupt_master_falls_back_to_newest_valid_backup(tmp_path, monkeypatch):
    import json, os
    home = _make_home(tmp_path)
    (home / ".claude.json").write_text('{"oauthAccount": {"a": 1}')  # truncated
    bdir = home / ".claude" / "backups"
    bdir.mkdir()
    old = bdir / ".claude.json.backup.1"; old.write_text('{"v": "old"}')
    new = bdir / ".claude.json.backup.2"; new.write_text('{"v": "new"}')
    bad = bdir / ".claude.json.backup.3"; bad.write_text('{"v":')  # newest but corrupt
    os.utime(old, (1000, 1000)); os.utime(new, (2000, 2000)); os.utime(bad, (3000, 3000))
    _direct_env(tmp_path, monkeypatch, home)
    _build("s2")
    seed = home / ".claude" / "isolated" / "s2" / "claude.json.seed"
    assert json.loads(seed.read_text()) == {"v": "new"}


def test_no_valid_source_spawns_without_seed(tmp_path, monkeypatch):
    home = _make_home(tmp_path)
    (home / ".claude.json").write_text("{not json")
    _direct_env(tmp_path, monkeypatch, home)
    _img, argv, kw, _mcp = _build("s3")
    assert not (home / ".claude" / "isolated" / "s3" / "claude.json.seed").exists()
    assert "AW_CLAUDE_JSON_SEED" not in kw["environment"]
    assert argv[0] == "aw-cli-with-quota"  # no copy wrapper
    assert not any(v["bind"] == "/home/ubuntu/.claude.json" for v in kw["volumes"].values())
