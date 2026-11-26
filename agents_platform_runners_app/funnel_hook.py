"""Activation-funnel ping: tell aw-backend a run SUCCEEDED, or first FAILED.

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
_POLL_MAX_S = 60.0
_MAX_CONCURRENT = 8
_COMPACT_INITIATORS = {"auto_compact", "pending_compact"}

_latch: set[str] = set()
# One failure ping per boot per workspace: aw-backend keeps one run_failed row
# per workspace, so after any 2xx (including recorded=false for a hosted
# workspace) there is nothing more to say. Unset on a non-2xx.
_failed_latched = False
_latch_lock = threading.Lock()
# Bounds outstanding pollers; the funnel is best-effort, so a run that finds
# every slot busy is simply not reported (the next success will be).
_slots = threading.BoundedSemaphore(_MAX_CONCURRENT)


def _link() -> tuple[str, str, str] | None:
    backend = os.environ.get("AW_BACKEND_URL", "").rstrip("/")
    workspace = os.environ.get("AW_WORKSPACE", "")
    token = os.environ.get("AW_WORKSPACE_HOST_TOKEN", "")
    if not (backend and workspace and token):
        return None
    return backend, workspace, token


def _counts(run: dict) -> str:
    """Classify a finalized AP-MT Run: "succeeded", "failed" or "ignore".

    Failed = status error, category work, not a compaction, and not a
    re-dispatched retry (``retried_from_run_id`` set). Cancelled and every
    other status is ignored.
    """
    status = run.get("status")
    if status not in ("success", "error") or run.get("category") != "work":
        return "ignore"
    raw = run.get("input")
    # AP-MT stores the prompt as {"input": "<text>"}; tolerate a bare string.
    prompt = str((raw.get("input") if isinstance(raw, dict) else raw) or "").strip()
    if prompt == "/compact" or run.get("initiator_kind") in _COMPACT_INITIATORS:
        return "ignore"
    if status == "success":
        return "succeeded"
    return "ignore" if run.get("retried_from_run_id") else "failed"


def _latch_key(model: str | None, model_slug: str | None) -> str:
    # Backend tiers fall back to model_slug when the requested model is
    # unclassified, so both inputs are part of the key.
    return f"{model or ''}|{model_slug or ''}"


def _wait_finalized(run_id: str, cfg: dict) -> dict | None:
    base = platform_base_mod.resolve(cfg).rstrip("/")
    headers = ({"Authorization": f"Bearer {cfg['agents_platform_token']}"}
               if cfg.get("agents_platform_token") else {})
    deadline = time.monotonic() + DEADLINE_S
    delay = 0.5
    with httpx.Client(headers=headers, timeout=10) as client:
        while time.monotonic() < deadline:
            resp = client.get(f"{base}/api/runs/{run_id}",
                              params={"metadata": "true"})
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
    global _failed_latched
    try:
        link = _link()
        if link is None or job.get("raw_command"):
            return False
        model = model_identifier(job.get("model"))
        run = _wait_finalized(run_id, execution_index._config)
        outcome = _counts(run) if run is not None else "ignore"
        if outcome == "ignore":
            return False
        model_slug = model_identifier(run.get("model_slug"))
        failed = outcome == "failed"
        key = _latch_key(model, model_slug)
        if (_failed_latched if failed else key in _latch):
            return False
        backend, workspace, token = link
        # Raw facts only: no error text, prompt or run id ever leaves here.
        resp = httpx.post(
            f"{backend}/api/workspaces/{workspace}/funnel/events",
            json={"event": "run_failed" if failed else "run_succeeded",
                  "model": model, "model_slug": model_slug,
                  "cli": job.get("cli") or "claude"},
            headers={"Authorization": f"Bearer {token}"}, timeout=15.0,
        )
        resp.raise_for_status()
        with _latch_lock:
            if failed:
                _failed_latched = True
            else:
                _latch.add(key)
        return True
    except Exception:  # noqa: BLE001 - the funnel must never touch a run
        log.warning("funnel: ping failed run=%s", run_id, exc_info=True)
        return False


def _run_slot(run_id: str, job: dict) -> None:
    try:
        report_run(run_id, job)
    finally:
        _slots.release()


def start(run_id: str, job: dict) -> threading.Thread | None:
    """Fire-and-forget; safe to call from any post-run site."""
    try:
        if _link() is None or job.get("raw_command"):
            return None
        if not _slots.acquire(blocking=False):
            return None
        thread = threading.Thread(target=_run_slot, args=(run_id, job),
                                  name=f"funnel-{run_id[:12]}", daemon=True)
        try:
            thread.start()
        except Exception:
            _slots.release()
            raise
        return thread
    except Exception:  # noqa: BLE001
        log.warning("funnel: could not start run=%s", run_id, exc_info=True)
        return None
