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
