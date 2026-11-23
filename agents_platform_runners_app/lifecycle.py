"""Workspace lifecycle probes for graceful restart (card 4215bf3b, slice S3).

Two jobs, both called from ``routes.py`` behind ``require_execute_secret``:

* :func:`healthy_payload` - the ``GET /healthy`` body. ``ready`` is true only
  when this workspace can actually run agents: the three preconditions
  ``/execute`` checks (execute secret, shared Redis, container socket) AND the
  mcp-gateway has every local upstream reconnected. Federated/remote upstreams
  never gate (the core helper never lists them).
* :func:`restart_core` - the ``POST /lifecycle/restart`` worker. It shells out
  to ``aw-workspace-cli restart core --request-id X --json`` (slice S2, core PR
  57) and maps its one-JSON-object result to an HTTP status. The CLI owns the
  idempotency receipt; this layer only validates, runs and translates.

The core readiness helper (``src.apps.gateway_readiness``) ships with core, not
with this app, so it is imported lazily and a missing helper reads as NOT ready
(fail closed): releasing held agent fires on an unverified gateway is the exact
failure this gate exists to prevent.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import threading
from typing import Any, Awaitable, Callable

from . import execute as execute_mod
from . import shared_redis

log = logging.getLogger("aw_apps.agents_platform_runners.lifecycle")

REQUEST_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
CLI_ENV = "AW_WORKSPACE_CLI"
CLI_DEFAULT = "aw-workspace-cli"
# CLI preflight (15s) + remote-host dispatch (30s) + slack. Without --wait the
# CLI returns right after the dispatch is acknowledged.
CLI_TIMEOUT_S = 90.0

# CLI ``status`` -> HTTP status. ``dispatched`` / ``already_dispatched`` are
# the only accepted outcomes; everything else is a structured refusal with the
# CLI object passed through verbatim.
_HTTP_BY_STATUS = {
    "dispatched": 202,
    "already_dispatched": 202,
    "invalid_request_id": 400,
    "dispatch_pending": 409,
    "dispatch_unconfirmed": 409,
    "not_configured": 503,
    "preflight_failed": 503,
    "container_not_found": 503,
    "dispatch_failed": 502,
}
ACCEPTED_STATUSES = frozenset({"dispatched", "already_dispatched"})


def boot_info() -> dict[str, str]:
    """boot_id / git_head / version of the core process serving this request.

    Read from the env the core parent mints once before forking workers
    (``src/api/boot_info.py``), so every worker answers identically and the
    value changes if and only if core was restarted. Empty strings when this
    app runs standalone (no core)."""
    return {
        "boot_id": os.environ.get("AW_WORKSPACE_BOOT_ID", ""),
        "git_head": os.environ.get("AW_WORKSPACE_GIT_HEAD", ""),
        "workspace_version": os.environ.get("AW_WORKSPACE_VERSION", ""),
        "started_at": os.environ.get("AW_WORKSPACE_STARTED_AT", ""),
    }


def active_jobs() -> int:
    """Agent jobs running in THIS process (``start_job`` threads).

    Process-local: with several core workers a probe sees one worker's count.
    AP-MT's drain keys on its own Run rows; this is a secondary signal."""
    return sum(1 for t in threading.enumerate()
               if t.name.startswith("runner-exec-") and t.is_alive())


PROBE_TIMEOUT_S = 3.0


def _ping_redis(url: str) -> None:
    import redis
    client = redis.Redis.from_url(url, socket_connect_timeout=PROBE_TIMEOUT_S,
                                  socket_timeout=PROBE_TIMEOUT_S)
    try:
        client.ping()
    finally:
        client.close()


def _ping_container_engine(socket_path: str) -> None:
    import docker as docker_sdk
    client = docker_sdk.DockerClient(base_url="unix://" + socket_path,
                                     timeout=PROBE_TIMEOUT_S)
    try:
        client.ping()
    finally:
        client.close()


async def can_execute(cfg: dict) -> dict[str, Any]:
    """The preconditions ``/execute`` needs, each actually PROBED (bounded):
    a configured-but-unreachable Redis or container engine must not read as
    ready, or held runs are released into a workspace that cannot spawn
    containers or publish events. ``reasons`` names each failing one."""
    reasons: list[str] = []
    if not cfg.get("execute_secret"):
        reasons.append("execute_secret_not_configured")
    try:
        url = shared_redis.resolve(cfg)
    except Exception as exc:  # noqa: BLE001 - the failure is the finding
        log.warning("healthy: shared redis resolve failed: %s", exc)
        url, reasons = None, reasons + ["shared_redis_error"]
    else:
        if not url:
            reasons.append("shared_redis_not_resolved")
    if url:
        try:
            await asyncio.wait_for(asyncio.to_thread(_ping_redis, url), PROBE_TIMEOUT_S + 2)
        except Exception as exc:  # noqa: BLE001
            log.warning("healthy: shared redis ping failed: %s", exc)
            reasons.append("shared_redis_unreachable")
    if not execute_mod.CONTAINER_SOCKET:
        reasons.append("container_socket_not_set")
    else:
        try:
            await asyncio.wait_for(
                asyncio.to_thread(_ping_container_engine, execute_mod.CONTAINER_SOCKET),
                PROBE_TIMEOUT_S + 2)
        except Exception as exc:  # noqa: BLE001
            log.warning("healthy: container engine ping failed: %s", exc)
            reasons.append("container_engine_unreachable")
    return {"ok": not reasons, "reasons": reasons}


async def gateway_state(runtime_provider: Callable[[], Any] | None) -> dict[str, Any]:
    """Gateway section of ``/healthy``: ``{ready, reachable, tools,
    local_upstreams, not_ready, note}``.

    * no ``runtime_provider`` (standalone mode, no core) -> ready, ``checked``
      false: there is no gateway to ask.
    * gateway app not installed (``reachable is None``) -> ready.
    * helper missing / raises -> NOT ready, reason ``gateway_check_unavailable``.
    """
    if runtime_provider is None:
        return {"ready": True, "checked": False, "reachable": None, "tools": 0,
                "local_upstreams": 0, "not_ready": [],
                "note": "standalone mode: no core runtime to ask"}
    try:
        from src.apps.gateway_readiness import gateway_readiness, is_ready
    except ImportError as exc:
        return _gateway_unavailable(f"core has no gateway_readiness helper: {exc}")
    try:
        runtime = runtime_provider()
        status = await gateway_readiness(runtime)
        ready = bool(is_ready(status))
    except Exception as exc:  # noqa: BLE001 - never read as ready on error
        log.warning("healthy: gateway readiness failed", exc_info=True)
        return _gateway_unavailable(f"{type(exc).__name__}: {exc}")
    return {
        "ready": ready,
        "checked": True,
        "reachable": status.get("reachable"),
        "tools": status.get("tools", 0),
        "local_upstreams": len(status.get("local_upstreams") or []),
        "not_ready": list(status.get("not_ready") or []),
        "note": status.get("note", ""),
    }


def _gateway_unavailable(why: str) -> dict[str, Any]:
    return {"ready": False, "checked": True, "reachable": None, "tools": 0,
            "local_upstreams": 0,
            "not_ready": [{"kind": "gateway_check_unavailable", "server": None,
                           "app": None, "reason": why}],
            "note": why}


async def healthy_payload(cfg: dict, runtime_provider: Callable[[], Any] | None) -> dict:
    exec_state = await can_execute(cfg)
    gateway = await gateway_state(runtime_provider)
    not_ready: list[dict] = [
        {"kind": "can_execute", "server": None, "app": None, "reason": r}
        for r in exec_state["reasons"]
    ] + gateway["not_ready"]
    return {
        **boot_info(),
        "can_execute": exec_state["ok"],
        "active_jobs": active_jobs(),
        "gateway": gateway,
        "ready": exec_state["ok"] and gateway["ready"],
        "not_ready": not_ready,
    }


# ---------------------------------------------------------------- restart

Runner = Callable[[list[str], float], Awaitable[tuple[int | None, str, str]]]


async def _run_cli(argv: list[str], timeout: float) -> tuple[int | None, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.DEVNULL)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return None, "", f"timed out after {timeout:.0f}s"
    return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


def _parse_cli_object(stdout: str) -> dict | None:
    """The CLI prints exactly one JSON object with ``--json``; tolerate stray
    lines by taking the last line that parses to an object."""
    for line in reversed([ln for ln in stdout.splitlines() if ln.strip()]):
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


async def restart_core(request_id: Any, *, runner: Runner | None = None) -> tuple[int, dict]:
    """Run ``restart core --request-id X --json``; return ``(http_status, body)``.

    Body is always the CLI's object (verbatim) plus ``accepted`` and
    ``request_id``; a CLI that cannot be run or parsed yields a synthetic
    ``status`` (``cli_unavailable`` 503 / ``cli_failed`` 502)."""
    if not isinstance(request_id, str) or not REQUEST_ID_RE.fullmatch(request_id):
        return 400, {"accepted": False, "ok": False, "status": "invalid_request_id",
                     "request_id": request_id if isinstance(request_id, str) else None,
                     "error": "request_id must match [A-Za-z0-9_-]{1,64}"}
    cli = os.environ.get(CLI_ENV) or shutil.which(CLI_DEFAULT) or ""
    if not cli:
        return 503, {"accepted": False, "ok": False, "status": "cli_unavailable",
                     "request_id": request_id,
                     "error": f"{CLI_DEFAULT} not found on PATH (set {CLI_ENV})"}
    # ``--request-id=X`` as ONE token: ids may start with "-", which argparse
    # rejects as a separate value ("expected one argument").
    try:
        code, out, err = await (runner or _run_cli)(
            [cli, "restart", "core", f"--request-id={request_id}", "--json"], CLI_TIMEOUT_S)
    except OSError as exc:  # missing/non-executable AW_WORKSPACE_CLI
        return 503, {"accepted": False, "ok": False, "status": "cli_unavailable",
                     "request_id": request_id, "error": f"could not start {cli}: {exc}"}
    obj = _parse_cli_object(out)
    if obj is None:
        return 502, {"accepted": False, "ok": False, "status": "cli_failed",
                     "request_id": request_id, "exit_code": code,
                     "error": (err or out or "no JSON object on stdout").strip()[-500:]}
    status = str(obj.get("status") or "")
    accepted = status in ACCEPTED_STATUSES and bool(obj.get("ok", True))
    http = _HTTP_BY_STATUS.get(status, 502 if not accepted else 202)
    return http, {**obj, "accepted": accepted, "request_id": request_id}
