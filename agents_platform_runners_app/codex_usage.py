"""Extract one Codex turn's exact usage from its append-only rollout.

``codex exec --json`` reports cumulative thread totals on ``turn.completed``.
Those values are useful for a CLI status line, but they are not a per-turn
billing record.  The rollout contains the response-level
``token_usage_record`` rows and the terminal ``task_complete`` error.  This
helper snapshots the rollout before an invocation and emits one synthetic
``turn.usage.delta`` frame from only the bytes appended by that invocation.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class CodexTurnUsageCapture:
    def __init__(self, codex_home: str | Path, session_id: str | None = None):
        self.codex_home = Path(codex_home)
        self.session_id = session_id or None
        self._sizes = self._snapshot_sizes()

    def _snapshot_sizes(self) -> dict[Path, int]:
        root = self.codex_home / "sessions"
        if not root.exists():
            return {}
        out: dict[Path, int] = {}
        for path in root.glob("**/rollout-*.jsonl"):
            try:
                out[path] = path.stat().st_size
            except OSError:
                continue
        return out

    def observe_cli_line(self, line: str) -> None:
        """Learn a fresh thread id from the public CLI stream."""
        try:
            event = json.loads(line)
        except (TypeError, json.JSONDecodeError):
            return
        if event.get("type") == "thread.started":
            self.session_id = event.get("thread_id") or event.get("thread", {}).get("id") or self.session_id

    def _rollout_path(self) -> Path | None:
        if not self.session_id:
            return None
        matches = list((self.codex_home / "sessions").glob(
            f"**/rollout-*-{self.session_id}.jsonl"))
        if not matches:
            return None
        return max(matches, key=lambda path: path.stat().st_mtime)

    def event(self) -> dict[str, Any] | None:
        path = self._rollout_path()
        if path is None:
            return None
        offset = self._sizes.get(path, 0)
        usage = {
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "cache_write_input_tokens": 0,
            "output_tokens": 0,
            "reasoning_output_tokens": 0,
        }
        context_tokens: int | None = None
        terminal_error: dict[str, Any] | None = None
        records = 0
        try:
            with path.open("r", encoding="utf-8") as fh:
                fh.seek(offset)
                for raw in fh:
                    try:
                        row = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if row.get("type") == "token_usage_record":
                        current = (row.get("payload") or {}).get("usage") or {}
                        for key in usage:
                            usage[key] += int(current.get(key) or 0)
                        context_tokens = int(current.get("input_tokens") or 0) or context_tokens
                        records += 1
                    elif (row.get("type") == "event_msg"
                          and (row.get("payload") or {}).get("type") == "task_complete"):
                        error = (row.get("payload") or {}).get("error")
                        if isinstance(error, dict):
                            terminal_error = error
        except OSError:
            return None
        if not records and terminal_error is None:
            return None
        return {
            "type": "turn.usage.delta",
            "usage": usage,
            "context_tokens": context_tokens,
            "usage_records": records,
            "error": terminal_error,
        }
