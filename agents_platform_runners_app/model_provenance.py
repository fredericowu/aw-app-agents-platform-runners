"""Model identifiers safe to expose in execution context and PR footers."""
from __future__ import annotations

import re
import tomllib
from pathlib import Path


def model_identifier(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    if value.startswith(("sk-", "ghp_", "github_pat_", "gho_", "ghs_", "ghu_", "ghr_")):
        return None
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}", value):
        return value
    return None


def model_args_are_unambiguous(extra_args: list[str]) -> bool:
    """Identify extra flags that can change the model or its config source."""
    args = iter(extra_args)
    for arg in args:
        flag, separator, value = arg.partition("=")
        if flag in {"--model", "-m", "--fallback-model", "--profile", "-p",
                    "--oss", "--settings", "--setting-sources", "--agent"}:
            return False
        if flag in {"-c", "--config"}:
            setting = value if separator else next(args, "")
            if setting.partition("=")[0].strip() != "model_reasoning_effort":
                return False
        elif flag.startswith(("-m", "-p", "-c")):
            return False
    return True


def launch_model(cli: str, model: str | None, extra_args: list[str]) -> str | None:
    """Resolve the runner's flag unless extra arguments make it ambiguous."""
    if not model_args_are_unambiguous(extra_args):
        return None
    if cli == "claude" and model in {"default", "opus", "sonnet", "haiku",
                                    "opusplan", "best"}:
        return None
    return model_identifier(model)


def codex_config_model(config_path: Path, cwd: Path) -> str | None:
    """Read an explicit home-config model when no other local layer may win.

    With a configured model, Codex exec passes it to thread/resume. With no
    configured model, the CLI may use persisted thread metadata or a default.
    """
    try:
        if Path("/etc/codex/config.toml").exists():
            return None
        if any((directory / ".codex" / "config.toml").exists()
               for directory in (cwd, *cwd.parents)):
            return None
        with config_path.open("rb") as config_file:
            return model_identifier(tomllib.load(config_file).get("model"))
    except (OSError, ValueError):
        return None
