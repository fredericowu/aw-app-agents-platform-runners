"""Subscription quota events are emitted by every Claude/Codex runner path."""

from pathlib import Path

from agents_platform_runners_app import execute


def _job(cli: str) -> dict:
    return {"run_id": f"quota-{cli}", "cli": cli, "prompt": "hello"}


def test_cold_claude_and_codex_commands_use_quota_wrapper(tmp_path, monkeypatch):
    monkeypatch.setattr(execute, "REAL_HOME", tmp_path)
    monkeypatch.setattr(execute, "WORKSPACE_HOST_DIR", str(tmp_path))
    monkeypatch.setattr(execute, "WORKSPACE_CONTAINER_DIR", str(tmp_path))

    for cli in ("claude", "codex"):
        _image, argv, _kwargs, _mcp = execute._build_container_kwargs(_job(cli))
        assert argv[:2] == ["aw-cli-with-quota", cli]


def test_warm_relays_publish_quota_before_done_sentinel():
    shared = Path(execute.__file__).resolve().parent.parent / "agent-images" / "shared"
    claude = (shared / "aw-warm-relay.py").read_text()
    codex = (shared / "aw-warm-relay-codex.py").read_text()

    assert claude.index('["/usr/local/bin/aw-quota-report", "claude"]') < claude.index(
        'r.xadd(stream_key, {"done": "1"')
    assert codex.index('["/usr/local/bin/aw-quota-report", "codex"]') < codex.index(
        'r.xadd(stream_key, {"done": "1"')
