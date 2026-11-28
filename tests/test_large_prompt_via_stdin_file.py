"""A large run prompt must reach the CLI via a file on stdin, not argv.

Card 4265bf3b-9510-8155-a213-ff801245ce4a. Runs on big cards failed with 0
tokens: ``exec container process `/usr/bin/sh`: Argument list too long``.
The cold spawn put system prompt + run input into ONE argv string, and for
codex the whole argv is re-quoted into a single ``sh -lc`` string, so the
kernel's 131,072-byte per-argument cap (MAX_ARG_STRLEN) refused the exec.

Run: python3 -m pytest tests/test_large_prompt_via_stdin_file.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents_platform_runners_app import execute as execute_mod  # noqa: E402

#: >200 KB, the acceptance size from the card.
HUGE = "run input line — padding well past MAX_ARG_STRLEN.\n" * 5000


def _build(cli, prompt, tmp_path, monkeypatch, **extra):
    monkeypatch.setattr(execute_mod, "WORKSPACE_CONTAINER_DIR", str(tmp_path / "ws"))
    monkeypatch.setattr(execute_mod, "WORKSPACE_HOST_DIR", "/host/aw-workspace")
    monkeypatch.setattr(execute_mod, "WORKSPACE_HOME_HOST_DIR", "")
    job = {"run_id": "r-bigprompt", "cli": cli, "agent_id": "a1",
           "session_id": "710ca728-30a6-4aed-b4ec-ce16815fde5d",
           "prompt": prompt, **extra}
    _img, argv, kwargs, _mcp = execute_mod._build_container_kwargs(job)
    return argv, kwargs


def _oversized(argv):
    return [a for a in argv if len(a.encode()) >= execute_mod.MAX_ARG_STRLEN]


@pytest.mark.parametrize("cli", ["claude", "codex"])
def test_huge_prompt_leaves_argv_and_goes_to_stdin_file(cli, tmp_path, monkeypatch):
    assert len(HUGE.encode()) > 200_000
    argv, kwargs = _build(cli, HUGE, tmp_path, monkeypatch, new_session=True,
                          append_system_prompt="persona " * 50)
    assert _oversized(argv) == []
    assert sum(len(a) for a in argv) < execute_mod.PROMPT_STDIN_THRESHOLD
    assert argv[:3] == ["sh", "-c", 'exec "$@" < "$AW_PROMPT_STDIN_FILE"']
    path = kwargs["environment"]["AW_PROMPT_STDIN_FILE"]
    assert path.endswith("/" + execute_mod.PROMPT_STDIN_FILENAME)
    host = tmp_path / "ws"
    staged = next(host.rglob(execute_mod.PROMPT_STDIN_FILENAME))
    body = staged.read_text(encoding="utf-8")
    assert HUGE in body  # full input delivered, not truncated


def test_codex_persona_still_prepended_into_staged_file(tmp_path, monkeypatch):
    _argv, _kw = _build("codex", HUGE, tmp_path, monkeypatch,
                        append_system_prompt="PERSONA-MARKER")
    staged = next((tmp_path / "ws").rglob(execute_mod.PROMPT_STDIN_FILENAME))
    assert staged.read_text(encoding="utf-8").startswith("PERSONA-MARKER")


def test_codex_reads_dash_and_claude_has_bare_p(tmp_path, monkeypatch):
    cargv, _ = _build("codex", HUGE, tmp_path, monkeypatch)
    import shlex
    inner = shlex.split(" ".join(cargv))
    assert "-" in inner and "exec" in inner  # `codex exec ... -` reads stdin
    kargv, _ = _build("claude", HUGE, tmp_path, monkeypatch, new_session=True)
    i = kargv.index("-p")
    assert kargv[i + 1].startswith("--")  # no prompt value after -p


@pytest.mark.parametrize("cli", ["claude", "codex"])
def test_small_prompt_keeps_the_argv_path(cli, tmp_path, monkeypatch):
    argv, kwargs = _build(cli, "hello", tmp_path, monkeypatch, new_session=True)
    assert "AW_PROMPT_STDIN_FILE" not in kwargs["environment"]
    assert any("hello" in a for a in argv)
