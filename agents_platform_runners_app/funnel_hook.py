"""Activation-funnel ping: tell aw-backend a run actually SUCCEEDED.

Card 4175bf3b (funnel-design-rev2-2026-11-26). A sibling of the
execution-index hook, called from the same two post-run sites in execute.py,
but deliberately NOT inside ``execution_index.index_run``: that returns early
when ``execution_index_mode`` is "off", which would silently disable the funnel
on any workspace with indexing off.

Counts a run only when AP-MT's finalized Run row says ``status == "success"``
and ``category == "work"`` and it is not a compaction. Never counts a
``raw_command`` job (monitor shell wrappers) and never counts a process exit
code - AP-MT decides success, and right after the done sentinel the row can
still read ``running``, so this polls until ``ended_at`` is set.

Posture copied from aw-workspace core's ``component_report.py``: best-effort,
never raises into a run, inert without a backend link
(``AW_BACKEND_URL`` / ``AW_WORKSPACE`` / ``AW_WORKSPACE_HOST_TOKEN``).

The ping carries raw facts only (model, model_slug, cli); aw-backend does the
tier classification. aw-backend dedups per (workspace, placement, tier); the
in-process latch below only saves round-trips and is set after a 2xx.
"""
from __future__ import annotations

import logging
import os
import threading
import time

import httpx

from . import execution_index
from . import platform_base as platform_base_mod
from .model_provenance import model_identifier

log = logging.getLogger(__name__)

DEADLINE_S = 24 * 3600
_POLL_MAX_S = 15.0
_COMPACT_INITIATORS = {"auto_compact", "pending_compact"}

_latch: set[str] = set()
_latch_lock = threading.Lock()


def _link() -> tuple[str, str, str] | None:
    backend = os.environ.get("AW_BACKEND_URL", "").rstrip("/")
    workspace = os.environ.get("AW_WORKSPACE", "")
    token = os.environ.get("AW_WORKSPACE_HOST_TOKEN", "")
    if not (backend and workspace and token):
        return None
    return backend, workspace, token


def _counts(run: dict) -> bool:
    if run.get("status") != "success" or run.get("category") != "work":
        return False
    prompt = str(run.get("input") or "").strip()
    return not (prompt == "/compact" or run.get("initiator_kind") in _COMPACT_INITIATORS)


def _latch_key(model: str | None, model_slug: str | None) -> str:
    return model or model_slug or ""


def _wait_finalized(run_id: str, cfg: dict) -> dict | None:
    base = platform_base_mod.resolve(cfg).rstrip("/")
    headers = ({"Authorization": f"Bearer {cfg['agents_platform_token']}"}
               if cfg.get("agents_platform_token") else {})
    deadline = time.monotonic() + DEADLINE_S
    delay = 0.5
    with httpx.Client(headers=headers, timeout=10) as client:
        while time.monotonic() < deadline:
            resp = client.get(f"{base}/api/runs/{run_id}")
            resp.raise_for_status()
            run = resp.json()
            if run.get("ended_at") is not None:
                return run
            time.sleep(delay)
            delay = min(delay * 2, _POLL_MAX_S)
    return None


def report_run(run_id: str, job: dict) -> bool:
    """Blocking body of the hook; returns True when a ping was accepted.
    Never raises."""
    try:
        link = _link()
        if link is None or job.get("raw_command"):
            return False
        model = model_identifier(job.get("model"))
        if model is not None and _latch_key(model, None) in _latch:
            return False
        run = _wait_finalized(run_id, execution_index._config)
        if run is None or not _counts(run):
            return False
        model_slug = model_identifier(run.get("model_slug"))
        key = _latch_key(model, model_slug)
        if key in _latch:
            return False
        backend, workspace, token = link
        resp = httpx.post(
            f"{backend}/api/workspaces/{workspace}/funnel/events",
            json={"event": "run_succeeded", "model": model,
                  "model_slug": model_slug, "cli": job.get("cli") or "claude"},
            headers={"Authorization": f"Bearer {token}"}, timeout=15.0,
        )
        resp.raise_for_status()
        with _latch_lock:
            _latch.add(key)
        return True
    except Exception:  # noqa: BLE001 - the funnel must never touch a run
        log.warning("funnel: run_succeeded ping failed run=%s", run_id, exc_info=True)
        return False


def start(run_id: str, job: dict) -> threading.Thread | None:
    """Fire-and-forget; safe to call from any post-run site."""
    try:
        if _link() is None or job.get("raw_command"):
            return None
        thread = threading.Thread(target=report_run, args=(run_id, job),
                                  name=f"funnel-{run_id[:12]}", daemon=True)
        thread.start()
        return thread
    except Exception:  # noqa: BLE001
        log.warning("funnel: could not start run=%s", run_id, exc_info=True)
        return None
