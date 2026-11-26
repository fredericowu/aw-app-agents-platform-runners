"""Tests for Antigravity CLI runner integration (agy)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents_platform_runners_app import execute as execute_mod
from agents_platform_runners_app import runner_registration as rr


def test_agy_in_cli_specs():
    assert "agy" in execute_mod.CLI_SPECS
    assert "antigravity" in execute_mod.CLI_SPECS
    spec = execute_mod.CLI_SPECS["agy"]
    assert spec["bin"] == "agy"
    assert spec["prompt_flag"] == "-p"
    assert spec["model_flag"] == "--model"
    assert spec["skip_perms_flag"] == "--dangerously-skip-permissions"
    assert spec["image_suffix"] == "gemini"


@pytest.mark.parametrize("model", [
    "gemini-3.8-flash-high",
    "claude-opus-5-5-high",
    "claude-sonnet-5-5-low",
    "gpt-oss-120b-medium",
])
def test_build_container_kwargs_for_agy(monkeypatch, tmp_path, model):
    monkeypatch.setenv("AW_WORKSPACE_HOST_DIR", "/host/workspace")
    monkeypatch.setenv("AW_WORKSPACE_HOME_HOST_DIR", "/host/home")
    monkeypatch.setattr(execute_mod, "WORKSPACE_HOST_DIR", "/host/workspace")
    monkeypatch.setattr(execute_mod, "WORKSPACE_HOME_HOST_DIR", "/host/home")
    monkeypatch.setattr(execute_mod, "WORKSPACE_CONTAINER_DIR", str(tmp_path / "ws"))
    monkeypatch.setattr(execute_mod, "REAL_HOME", str(tmp_path / "home"))

    home = tmp_path / "home"
    gemini_dir = home / ".gemini"
    gemini_dir.mkdir(parents=True)
    (gemini_dir / "antigravity-cli").mkdir()
    (gemini_dir / "antigravity-cli" / "antigravity-oauth-token").write_text("tok-123")

    ws = tmp_path / "ws"
    ws.mkdir(parents=True)

    job = {
        "run_id": "test-agy-run-1",
        "cli": "agy",
        "prompt": "write a function",
        "model": model,
        "dangerous_skip_permissions": True,
        "permissions": {"workspace_access": True},
    }

    image, argv, kwargs, _mcp = execute_mod._build_container_kwargs(job)

    assert "aw-sandbox-agent-cli-gemini" in image
    assert "command" in kwargs
    cmd = kwargs["command"]
    # Outer command is sh -lc
    assert cmd[0] == "sh"
    assert cmd[1] == "-lc"
    shell_script = cmd[2]
    assert "/home/ubuntu/.gemini" in shell_script
    assert "agy" in shell_script
    assert f"--model {model}" in shell_script

    # Verify mounts
    volumes = kwargs["volumes"]
    # Check bin mount
    bin_mount = any(v.get("bind") == "/usr/local/bin/agy" for v in volumes.values())
    assert bin_mount, f"Expected /usr/local/bin/agy in {volumes}"
    # Check gemini home mount
    gemini_mount = any(v.get("bind") == "/home/ubuntu/.gemini" for v in volumes.values())
    assert gemini_mount, f"Expected /home/ubuntu/.gemini in {volumes}"
    # Per-container tool cache (race fix): tmpfs, not shared
    assert kwargs["tmpfs"] == {"/home/ubuntu/.gemini/antigravity-cli/mcp": "mode=1777"}


def test_agy_resume_conversation(monkeypatch, tmp_path):
    monkeypatch.setenv("AW_WORKSPACE_HOST_DIR", "/host/workspace")
    monkeypatch.setenv("AW_WORKSPACE_HOME_HOST_DIR", "/host/home")
    monkeypatch.setattr(execute_mod, "WORKSPACE_HOST_DIR", "/host/workspace")
    monkeypatch.setattr(execute_mod, "WORKSPACE_HOME_HOST_DIR", "/host/home")
    monkeypatch.setattr(execute_mod, "WORKSPACE_CONTAINER_DIR", str(tmp_path / "ws"))
    monkeypatch.setattr(execute_mod, "REAL_HOME", str(tmp_path / "home"))

    home = tmp_path / "home"
    gemini_dir = home / ".gemini"
    gemini_dir.mkdir(parents=True)
    (gemini_dir / "antigravity-cli").mkdir()
    (gemini_dir / "antigravity-cli" / "antigravity-oauth-token").write_text("tok-123")

    ws = tmp_path / "ws"
    ws.mkdir(parents=True)

    job = {
        "run_id": "test-agy-run-2",
        "cli": "agy",
        "prompt": "continue this",
        "session_id": "conv-abc-123",
        "model": "gemini-3.8-flash-high",
        "permissions": {"workspace_access": True},
    }

    _image, _argv, kwargs, _mcp = execute_mod._build_container_kwargs(job)
    shell_script = kwargs["command"][2]
    assert "--conversation conv-abc-123" in shell_script


def test_runner_status_resolves_agy(monkeypatch, tmp_path):
    # Mock shutil.which returning None, but finding in .aw-workspace/bin/agy
    monkeypatch.setattr(rr.shutil, "which", lambda name: None)
    monkeypatch.setenv("AW_WORKSPACE_CONTAINER_DIR", str(tmp_path))
    bin_dir = tmp_path / ".aw-workspace" / "bin"
    bin_dir.mkdir(parents=True)
    agy_bin = bin_dir / "agy"
    agy_bin.write_text("#!/bin/sh\necho 1.5.9")
    agy_bin.chmod(0o755)

    status = rr.runner_status("agy")
    assert status["installed"] is True
    assert status["path"] == str(agy_bin)
    assert status["version"] == "1.5.9"


def _agy_env(monkeypatch, tmp_path):
    monkeypatch.setenv("AW_WORKSPACE_HOST_DIR", "/host/workspace")
    monkeypatch.setenv("AW_WORKSPACE_HOME_HOST_DIR", "/host/home")
    monkeypatch.setattr(execute_mod, "WORKSPACE_HOST_DIR", "/host/workspace")
    monkeypatch.setattr(execute_mod, "WORKSPACE_HOME_HOST_DIR", "/host/home")
    monkeypatch.setattr(execute_mod, "WORKSPACE_CONTAINER_DIR", str(tmp_path / "ws"))
    monkeypatch.setattr(execute_mod, "REAL_HOME", str(tmp_path / "home"))
    g = tmp_path / "home" / ".gemini"
    for d in ("antigravity-cli", "config"):
        (g / d).mkdir(parents=True)
    # real layout: the bulk dirs live UNDER antigravity-cli/
    for d in ("conversations", "brain", "log", "bin"):
        (g / "antigravity-cli" / d).mkdir()
    (g / "antigravity-cli" / "antigravity-oauth-token").write_text("tok")
    (g / "antigravity-cli" / "mcp").mkdir()
    (g / "antigravity-cli" / "conversations" / "c.pb").write_text("x" * 100)
    (g / "antigravity-cli" / "bin" / "agy").write_text("x" * 100)
    (g / "config" / "settings.json").write_text("{}")
    (tmp_path / "ws").mkdir(parents=True, exist_ok=True)
    return g


def _staged(tmp_path):
    return list((tmp_path / "home").glob(".gemini/isolated/*/creds"))[0]


def _job(run_id):
    return {"run_id": run_id, "cli": "agy", "prompt": "p",
            "model": "gemini-3.8-flash-low", "permissions": {"workspace_access": True}}


def test_agy_populated_home_stages_token_only(monkeypatch, tmp_path):
    _agy_env(monkeypatch, tmp_path)
    shared = tmp_path / "ws" / execute_mod._cli_home_rel(".gemini")
    (shared / "antigravity-cli").mkdir(parents=True)
    (shared / "antigravity-cli" / "antigravity-oauth-token").write_text("tok")
    execute_mod._build_container_kwargs(_job("agy-pop"))
    files = sorted(p.relative_to(_staged(tmp_path)).as_posix()
                   for p in _staged(tmp_path).rglob("*") if p.is_file())
    assert files == ["antigravity-cli/antigravity-oauth-token"]
    # host-side mountpoint for the tmpfs exists
    assert (shared / "antigravity-cli" / "mcp").is_dir()


def test_agy_unpopulated_home_first_copy_skips_bulk(monkeypatch, tmp_path):
    _agy_env(monkeypatch, tmp_path)
    execute_mod._build_container_kwargs(_job("agy-fresh"))
    staged = _staged(tmp_path)
    assert (staged / "antigravity-cli" / "antigravity-oauth-token").is_file()
    assert (staged / "config" / "settings.json").is_file()
    for bulk in ("conversations", "brain", "log", "bin"):
        assert not (staged / "antigravity-cli" / bulk).exists()
    assert not (staged / "antigravity-cli" / "mcp").exists()


def test_codex_kwargs_have_no_tmpfs(monkeypatch, tmp_path):
    _agy_env(monkeypatch, tmp_path)
    c = tmp_path / "home" / ".codex"
    c.mkdir()
    (c / "auth.json").write_text("{}")
    (c / "config.toml").write_text("")
    job = _job("codex-1")
    job.update(cli="codex", model="gpt-5")
    _i, _a, kwargs, _m = execute_mod._build_container_kwargs(job)
    assert "tmpfs" not in kwargs


def test_agy_staging_failure_fallback_still_gets_tmpfs(monkeypatch, tmp_path):
    _agy_env(monkeypatch, tmp_path)

    def boom(*a, **k):
        raise OSError("staging broke")

    monkeypatch.setattr(execute_mod.shutil, "copytree", boom)
    _i, _a, kwargs, _m = execute_mod._build_container_kwargs(_job("agy-fb"))
    assert kwargs["tmpfs"] == {"/home/ubuntu/.gemini/antigravity-cli/mcp": "mode=1777"}


def test_agy_no_host_home_bind_fallback_gets_tmpfs(monkeypatch, tmp_path):
    _agy_env(monkeypatch, tmp_path)
    monkeypatch.setattr(execute_mod, "WORKSPACE_HOME_HOST_DIR", "")
    monkeypatch.setenv("AW_WORKSPACE_HOME_HOST_DIR", "")
    _i, _a, kwargs, _m = execute_mod._build_container_kwargs(_job("agy-nohome"))
    assert kwargs["tmpfs"] == {"/home/ubuntu/.gemini/antigravity-cli/mcp": "mode=1777"}
