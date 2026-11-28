"""Firing agents-platform runs from Notion Kanban cards.

This is the half of the monolith's ``src/api/routes/notion_kanban.py`` that
aw-app-notion deliberately refused. That app owns "a Notion database used as a
Kanban board" and does not talk to an orchestrator — its own module docstring
says reimplementing dispatch there would hardcode it to one. It was right, and
this is where the other half belongs: this app already *is* the orchestrator
client, so a card-to-run bridge here costs one new dependency (the board) in a
process that already has the harder one (the platform).

Split, concretely:

* the **board** — reading Ready cards, their body and comments, stamping the
  run id back onto the card — goes over the workspace API to aw-app-notion.
* the **dispatch** — finding an in-flight run, firing an agent or workflow,
  resuming a session — goes to agents-platform, the same ``BASE`` every other
  tool in this app uses.

**Where this actually runs matters.** Both apps are ``tier: inprocess``, which
makes it tempting to call the board over loopback — and wrong. This module is
imported by ``mcp_server.py``, which aw-mcp-gateway spawns as a *stdio child of
its own container*: ``127.0.0.1`` there is the gateway, not the workspace, and
the workspace's environment (including its API key) is not inherited. So the
address comes from ``AW_WORKSPACE_API_URL`` and the key is baked into the
upstream's env by ``plugin.build_mcp_servers``, the same way
``AGENTS_PLATFORM_TOKEN`` already is. Loopback stays as a last-resort fallback
for running this module inside the server (tests, a REPL), not as the norm.

Importing aw-app-notion's ``KanbanBoard`` directly would sidestep all of that
and is worse: it reaches into another app's object graph, and from the gateway
container it isn't even importable.
"""
from __future__ import annotations

import asyncio
import fcntl
import hashlib
import http.client
import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid

from collections.abc import Callable
from pathlib import Path

from . import platform_base as platform_base_mod

log = logging.getLogger("aw_apps.agents_platform_runners.kanban")

RUNNERS_PREFIX = "/api/apps/agents-platform-runners"
DEFAULT_CODER_SETTING = "default_agent_coder"
NOTION_PREFIX = "/api/apps/notion"
API_URL_VAR = "AW_WORKSPACE_API_URL"
API_KEY_VAR = "AW_WORKSPACE_API_KEY"
CONTAINER_DIR = os.environ.get("AW_WORKSPACE_CONTAINER_DIR", "/opt/aw-workspace")

# Reading a card with body + comments is three Notion round trips behind one
# call, and Notion is paced at ~3 req/s — generous, not hot-path.
CARD_TIMEOUT_S = 60.0
DEFAULT_TIMEOUT_S = 30.0

# Where a card's run id is stamped so a human can jump from the card to the
# run. The monolith also wrote QARunId/QAAgent for qa-* agents; that stayed
# there with the QA flow and is not reproduced here.
RUN_ID_PROPERTY = "AgentRunId"
ACTIVE_RUN_STATUSES = ("pending", "running", "queued")
DISPATCH_FENCE = Path(__file__).resolve().parents[1] / "aw-app.json"

# The monolith's default when a card names no target. Kept identical: cards
# created before this port still rely on it.
DEFAULT_TARGET_SLUG = "system-investigations"

# Which side of the fence a claim token came from — see claim_card(). The
# monolith writes "mono:<uuid>"; only the prefix differs, so a human reading a
# stuck card can tell who was holding the lease when it stopped.
CLAIM_SIDE = "mt"

_SKILL_HINT = ("Load skill aw-kanban before acting on this card — it's the tool "
               "reference for the aw-kanban MCP (how to call the tools, the "
               "page_id from NOTION_TASK_ID).")


class BoardUnavailable(RuntimeError):
    """aw-app-notion could not be reached, or refused the call.

    Distinct from a platform error on purpose: "the board is unreachable" and
    "the run failed to start" are different outages with different owners, and
    a caller that can't tell them apart debugs the wrong one.
    """


def _env_file() -> str:
    home = os.environ.get("AW_WORKSPACE_HOME") or os.path.join(CONTAINER_DIR, ".aw-workspace")
    return os.path.join(home, ".env")


def _from_env_file(name: str) -> str | None:
    try:
        with open(_env_file(), "r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith(f"{name}="):
                    return line.split("=", 1)[1].strip() or None
    except OSError:
        return None
    return None


def board_base_url(*, prefer_loopback: bool = False) -> str:
    """Where to reach the workspace API — see the module docstring.

    Published URL first, loopback last. This process is normally a stdio child
    of the gateway's container, where loopback is the gateway itself; a
    loopback-first order there doesn't fail loudly, it just talks to the wrong
    server.

    ``prefer_loopback`` inverts that for the one caller where the published URL
    is the wrong answer: the in-process watchdog (plugin.py) runs *inside* the
    workspace server, so the published URL would send its traffic out to the
    tunnel edge and back — and that edge cuts a request at ~30s, which is
    *shorter* than this module's own ``CARD_TIMEOUT_S``. A slow card read there
    dies at the edge rather than at the timeout that was sized for it.
    """
    loopback = f"http://127.0.0.1:{os.environ.get('AW_PORT', '9030')}"
    if prefer_loopback:
        return (os.environ.get("AW_LOCAL_API_URL") or loopback).rstrip("/")
    return (os.environ.get("AW_LOCAL_API_URL")
            or os.environ.get(API_URL_VAR)
            or _from_env_file(API_URL_VAR)
            or loopback).rstrip("/")


class BoardClient:
    """Workspace-local Kanban and default Coder reads and writes."""

    def __init__(self, *, base_url: str | None = None) -> None:
        self._base = (base_url or "").rstrip("/") or None

    def _request(self, method: str, path: str, body: dict | None = None,
                 timeout: float = DEFAULT_TIMEOUT_S) -> dict:
        base = self._base or board_base_url()
        service = "agents-platform-runners" if path.startswith(RUNNERS_PREFIX + "/") else "aw-app-notion"
        key = os.environ.get(API_KEY_VAR) or _from_env_file(API_KEY_VAR)
        if not key:
            raise BoardUnavailable(
                f"{API_KEY_VAR} is not set for this MCP upstream. It is baked into the "
                "stdio env by the app's plugin.build_mcp_servers() — an upstream missing "
                "it was registered by an older version of this app, so restart the "
                "workspace (which rewrites mcp.json) and then the mcp-gateway.")
        data = json.dumps(body).encode() if body is not None else None
        headers = {"X-Api-Key": key}
        if data:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            if exc.code == 404:
                raise BoardUnavailable(
                    f"{service} is not installed or the requested route is missing "
                    f"({detail})") from None
            raise BoardUnavailable(f"{service} returned {exc.code}: {detail}") from None
        except urllib.error.URLError as exc:
            raise BoardUnavailable(
                f"could not reach {service} at {base}: {exc.reason}") from None
        except (OSError, http.client.HTTPException) as exc:
            # Response-level timeouts/resets (socket.timeout == TimeoutError,
            # ConnectionResetError, ...) and malformed-response errors
            # (IncompleteRead, BadStatusLine — http.client.HTTPException is
            # NOT an OSError subclass) raised from h.getresponse() or this
            # method's own resp.read() are NOT wrapped into URLError by
            # urllib — only the send phase (h.request) is. Left uncaught,
            # one of these escapes three layers up (through check_deployed's
            # per-card BoardUnavailable guard and the MCP tool handler) and
            # kills the whole batch instead of just this card.
            raise BoardUnavailable(
                f"{service} at {base} did not respond in time: {exc}") from None
        try:
            return json.loads(raw.decode() or "{}")
        except ValueError:
            raise BoardUnavailable(
                f"{service} returned a non-JSON body") from None

    def default_coder(self) -> str:
        try:
            result = self._request("GET", f"{RUNNERS_PREFIX}/kanban/default-coder")
            if not isinstance(result, dict):
                raise ValueError("workspace lookup did not return an object")
            if result.get("workspace") != _this_workspace():
                raise ValueError("workspace lookup returned a different workspace")
            slug = result["agent_slug"]
            if not isinstance(slug, str) or not slug.strip():
                raise ValueError("workspace lookup did not return a concrete agent slug")
            return slug
        except (BoardUnavailable, KeyError, TypeError, ValueError) as exc:
            raise CoderDispatchError(
                f"Could not read {DEFAULT_CODER_SETTING} in workspace {_this_workspace()!r}: {exc}"
            ) from exc

    def ready_cards(self, status: str = "ready", limit: int = 50) -> list[dict]:
        query = urllib.parse.urlencode({"status": status, "limit": limit})
        body = self._request("GET", f"{NOTION_PREFIX}/kanban/cards?{query}")
        if isinstance(body, dict) and body.get("ok") is False:
            raise BoardUnavailable(body.get("error") or "board refused the card query")
        cards = body.get("cards") if isinstance(body, dict) else body
        return cards if isinstance(cards, list) else []

    def card(self, page_id: str, *, with_content: bool = True) -> dict:
        query = urllib.parse.urlencode({
            "include_body": str(with_content).lower(),
            "include_comments": str(with_content).lower()})
        return self._request("GET", f"{NOTION_PREFIX}/kanban/cards/{page_id}?{query}",
                             timeout=CARD_TIMEOUT_S)

    def move(self, page_id: str, status: str, comment: str = "") -> dict:
        """``comment`` is required by aw-app-notion's own ``move_card()`` for
        ``status="need_human"`` — it refuses the move with ``{"ok": False,
        ...}`` (not an exception) otherwise. Forward it in the same call
        rather than a separate ``add_comment()`` first: ``move_card()``
        already posts it before moving, so doing both would double-post."""
        body = {"page_id": page_id, "status": status}
        if comment:
            body["comment"] = comment
        result = self._request("POST", f"{NOTION_PREFIX}/kanban/move", body)
        if isinstance(result, dict) and result.get("ok") is False:
            raise BoardUnavailable(result.get("error") or "board refused the move")
        return result

    def add_comment(self, page_id: str, text: str) -> dict:
        result = self._request("POST", f"{NOTION_PREFIX}/kanban/comment",
                               {"page_id": page_id, "text": text})
        if isinstance(result, dict) and result.get("ok") is False:
            raise BoardUnavailable(result.get("error") or "board refused the comment")
        return result

    def set_property(self, page_id: str, name: str, value) -> dict:
        result = self._request("POST", f"{NOTION_PREFIX}/kanban/set-property",
                               {"page_id": page_id, "property": name, "value": value})
        if isinstance(result, dict) and result.get("ok") is False:
            raise BoardUnavailable(result.get("error") or "board refused the property write")
        return result

    def qa_findings(self, *, target_slug: str, page_id: str, revision: str,
                    findings: list[dict], summary: str = "") -> dict:
        """File QA findings on the target's findings card (create or reuse)."""
        result = self._request("POST", f"{NOTION_PREFIX}/kanban/qa-findings", {
            "target_slug": target_slug, "page_id": page_id, "revision": revision,
            "findings": findings, "summary": summary})
        if isinstance(result, dict) and result.get("ok") is False:
            raise BoardUnavailable(result.get("error") or "board refused the QA findings")
        return result

    def need_attention(self, *, limit: int = 10, reping: bool = True) -> dict:
        """``GET /kanban/cards-need-attention`` — the live F′ REST route
        (sync headless wrapper over aw-app-notion's own
        ``list_cards_need_attention``, Kanban feature:kanban-sweep-workflow-
        f-rest-route). Already accepts ``reping`` as a query param, so no
        aw-app-notion change was needed for this (card instructions item
        #3's conditional "if the REST route doesn't already accept a reping
        param" did not fire — verified against ``notion_app/routes.py``
        before writing this method).

        Response shape: ``{has_work, cards, counts: {count, scanned,
        truncated}}``, plus ``re_pinged``/``reping`` when ``reping=True``.
        No ``ok``-check here, same as ``card()`` above — the route itself
        turns a board-level failure into a real non-2xx, which ``_request``
        already raises as ``BoardUnavailable``."""
        query = urllib.parse.urlencode({"limit": limit, "reping": str(bool(reping)).lower()})
        return self._request("GET", f"{NOTION_PREFIX}/kanban/cards-need-attention?{query}")

    def properties(self, page_id: str, names: list[str] | None = None) -> dict:
        """``{property_name: value}`` — the read half of ``set_property``.

        ``card()``'s summary deliberately carries a fixed set of fields, and
        ``AgentRunId`` is not one of them; the claim read-back needs the raw
        property, so it comes from here.
        """
        query = {"page_id": page_id}
        if names:
            query["properties"] = ",".join(names)
        body = self._request("GET",
                             f"{NOTION_PREFIX}/kanban/properties?{urllib.parse.urlencode(query)}")
        if isinstance(body, dict) and body.get("ok") is False:
            raise BoardUnavailable(body.get("error") or "board refused the property read")
        return body if isinstance(body, dict) else {}


def build_run_input(card: dict) -> str:
    """The dispatch prompt, assembled the way the monolith assembled it.

    Body first because it is the task; comments after because they are the
    history that makes the task make sense; the skill hint last so the agent
    loads the tool reference instead of guessing at REST endpoints — which is
    the specific failure that put that line in the monolith.
    """
    page_id = card.get("page_id", "")
    title = card.get("title") or "(untitled card)"
    body = (card.get("body_md") or "").strip()
    comments = (card.get("comments_md") or "").strip()
    parts = [
        f'Kanban card: "{title}"',
        f"page_id={page_id}",
        "(same value as your $NOTION_TASK_ID env var — use either)",
        f"Task content:\n\n{body or f'Run task: {title}'}",
    ]
    if comments:
        parts.append(f"Comment history (oldest → newest):\n\n{comments}")
    parts.append(_SKILL_HINT)
    return "\n\n".join(parts)


def _this_workspace() -> str:
    """This workspace's own slug — same source runner_registration.py uses
    to register its Runner rows (bug:runner-execute-401-monitor-shell-
    dispatch): naming it explicitly on dispatch lets AP-MT resolve the
    Runner row directly instead of depending on inheritance alone."""
    return platform_base_mod.workspace_env("AW_WORKSPACE") or "aw"


class CoderDispatchError(ValueError):
    """The workspace Coder selection could not be resolved."""


def coder_setting(value: object) -> str:
    label = f"{DEFAULT_CODER_SETTING} in workspace {_this_workspace()!r}"
    if value is not None and not isinstance(value, str):
        raise CoderDispatchError(f"{label} must be a concrete agent slug")
    slug = value.strip() if isinstance(value, str) else ""
    if slug and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", slug):
        raise CoderDispatchError(f"{label} must be a concrete agent slug, got {slug!r}")
    return slug or "coder"


async def resolve_default_coder(platform: PlatformClient, read_setting: Callable[[], object]) -> str:
    slug = coder_setting(await asyncio.to_thread(read_setting))
    await platform.validate_agent(slug, setting=DEFAULT_CODER_SETTING)
    return slug


def dispatch_payload(card: dict, input_text: str, *, default_coder: str = "coder",
                     default_target_slug: str = DEFAULT_TARGET_SLUG) -> tuple[str, dict]:
    """Explicit agents and workflows take precedence over the workspace coder."""
    agent_slug = (card.get("agent_slug") or "").strip()
    workflow_slug = (card.get("workflow_slug") or "").strip()
    payload = {
        "input": {"input": input_text},
        "target_slug": (card.get("target_slug") or "").strip() or default_target_slug,
        "notion_task_id": card.get("page_id", ""),
        # AP-MT now accepts workspace on both the agent-run AND the
        # workflow-run path (docs/design/ap-mt-workspace-cascade.md,
        # slice 1 — run_workflow_ep used to silently drop it).
        "workspace": _this_workspace(),
    }
    path = (f"/api/agents/{agent_slug}/run" if agent_slug
            else f"/api/workflows/{workflow_slug}/run" if workflow_slug
            else f"/api/agents/{default_coder}/run")
    return path, payload


def resume_payload(run: dict, page_id: str, message: str, *,
                   callback_origin_run_id: str | None = None, call_me_back: bool = True,
                   call_me_back_on: str | None = None) -> tuple[str, dict] | str:
    """``(path, body)`` to resume the card's session, or a string saying why
    it can't be resumed. A card whose last run has no session is a real,
    explainable state — not an exception."""
    agent_slug = (run.get("source_slug") or "").strip()
    session_id = (run.get("session_id") or "").strip()
    category = (run.get("category") or "").strip()
    # Belt-and-braces: the caller is expected to have already filtered with
    # category="work" (invoke_kanban_agent does); this refuses a monitor/
    # wrapper row even if it somehow reaches here anyway, rather than
    # resuming its own empty conversation as if it were the real coding
    # session (Kanban 4115bf3b-9510-8156-9e5e-f6c772ad2538).
    if category and category != "work":
        return (f"the latest run for this card is a {category!r} dispatch (source_slug="
                f"{agent_slug!r}), not the real agent session — nothing to resume")
    if not agent_slug:
        return "the latest run for this card has no source_slug (agent) to resume"
    if not session_id:
        return ("the latest run for this card has no session_id — it never opened a "
                "resumable CLI session, so there is nothing to send a message to")
    payload = {"input": {"input": message}, "session_id": session_id,
               "notion_task_id": page_id, "workspace": _this_workspace()}
    if run.get("target_id"):
        payload["target_id"] = run["target_id"]
    # Carry the caller's call-me-back intent at creation so the resumed run
    # is never finalized before the callback is armed (card 41e5bf3b). The
    # origin is NOT the run's parent: a resumed session is not a child of
    # whoever nudged it (flow, cancel cascade and hop count stay the card's),
    # hence callback_origin_run_id, not caller_run_id. The endpoint rejects
    # call_me_back=true with neither an origin nor a redirect target (400),
    # so that case degrades to fire-and-forget.
    armed = bool(call_me_back and (callback_origin_run_id or call_me_back_on))
    if armed and callback_origin_run_id:
        payload["callback_origin_run_id"] = callback_origin_run_id
    if armed and call_me_back_on:
        payload["call_me_back_on"] = call_me_back_on
    payload["call_me_back"] = armed
    return f"/api/agents/{agent_slug}/run", payload


def claim_card(board: BoardClient, page_id: str, *, side: str = CLAIM_SIDE,
               token: str | None = None) -> str | None:
    """Lease this card for one dispatcher. Returns the winning token, or None.

    Two dispatchers watch this board — the monolith's Notion webhook and this
    app's sweep — and they read two *different* run databases, so neither can
    see the other's in-flight runs. "Is a run already going?" is therefore blind
    across the fence; the only signal both sides share is the card itself.

    Status alone isn't enough. Both sides write "In Progress" before firing,
    which handles a late duplicate delivery, but it is still check-then-act: if
    both GETs land before either PATCH, both fire. So instead of asking, each
    side *claims* — write a unique token into ``AgentRunId``, read it straight
    back, and only the one that reads its own token proceeds. Notion's page
    patch is last-write-wins and the following GET returns exactly one value, so
    exactly one side can win. It's a lease, not a timing heuristic.

    It fails **closed**: a stale or failed read makes *both* sides stand down,
    which leaves the card in Ready — visible on the board, and reclaimed by the
    next tick 60s later. The alternative failure (two runs on one card) is the
    one that costs money and confuses the human reading the card.

    ``AgentRunId`` is reused rather than a new property because both sides
    already write it today, and the value it settles on is still the real run
    id — the lease is only what lives there for the second or two in between.
    """
    token = token or f"{side}:{uuid.uuid4()}"
    board.set_property(page_id, RUN_ID_PROPERTY, token)
    current = board.properties(page_id, [RUN_ID_PROPERTY]).get(RUN_ID_PROPERTY)
    if str(current or "").strip() != token:
        log.info("kanban sweep: lost the claim on %s (holder=%r)", page_id, current)
        return None
    return token


async def size_hold(platform, page_id: str) -> dict | None:
    """The active size hold on ``page_id``, or None to proceed. Must be asked
    BEFORE any board write (claim, status move, comment): a held card that was
    already moved to In Progress would bounce back every tick. Tolerates a
    platform object without ``card_size`` and an AP-MT without the route."""
    ask = getattr(platform, "card_size", None)
    if ask is None:
        return None
    verdict = await ask(page_id)
    if verdict and verdict.get("held"):
        return verdict.get("hold") or {}
    return None


class PlatformClient:
    """The narrow slice of agents-platform the sweep needs.

    Wraps a caller-supplied ``httpx.AsyncClient`` instead of owning one, so the
    ``run_ready_cards`` tool hands over the very client — and identity-JWT auth
    headers — every other tool in ``mcp_server`` already uses, and the watchdog
    builds its own.
    """

    def __init__(self, client, base: str) -> None:
        self._c = client
        self._base = base.rstrip("/")

    async def latest_run(self, page_id: str) -> dict | None:
        runs = await self.card_runs(page_id, limit=1)
        return runs[0] if runs else None

    async def card_size(self, page_id: str) -> dict | None:
        """Ask AP-MT's card size gate about ``page_id`` (card 4265bf3b): measures the
        card (run ids / comment characters), records or clears its hold and
        returns ``{"held": bool, "mode": ..., "hold": {...}|None}``. ``None``
        means "no verdict" and the caller proceeds as before: an older AP-MT
        without the route (404/405), or any transport/HTTP failure. The gate
        fails open here on purpose; AP-MT's own dispatch doors still enforce."""
        try:
            resp = await self._c.post(f"{self._base}/api/card-size-holds/precheck",
                                      json={"page_id": page_id})
        except Exception:  # noqa: BLE001
            log.warning("card size check failed for %s", page_id, exc_info=True)
            return None
        if resp.status_code != 200:
            return None
        body = resp.json()
        return body if isinstance(body, dict) else None

    async def card_runs(self, page_id: str, *, limit: int) -> list[dict]:
        resp = await self._c.get(f"{self._base}/api/runs",
                                 params={"notion_task_id": page_id, "category": "work", "limit": limit})
        if resp.status_code != 200:
            raise RuntimeError(f"Card-bound run lookup failed for {page_id} (HTTP {resp.status_code})")
        return resp.json() or []

    async def running_iteration(self, plan_slug: str) -> tuple[str, list[str]] | None:
        """(iteration id, gate-candidate card ids) of the plan's running iteration, else None.
        Candidates come from the iteration ledger (the gate node's recorded output)."""
        resp = await self._c.get(f"{self._base}/api/plans/{plan_slug}/iterations",
                                 params={"limit": 5})
        if resp.status_code != 200:
            raise RuntimeError(f"plan iterations lookup failed (HTTP {resp.status_code})")
        running = next((i for i in resp.json() or [] if i.get("status") == "running"), None)
        if not running:
            return None
        ledger = await self._c.get(
            f"{self._base}/api/plans/{plan_slug}/iterations/{running['id']}/ledger")
        if ledger.status_code != 200:
            raise RuntimeError(f"iteration ledger lookup failed (HTTP {ledger.status_code})")
        rows = (ledger.json() or {}).get("rows") or []
        return running["id"], [r["card"] for r in rows if r.get("gate_candidate")]

    async def pending_questions(self) -> dict[str, dict]:
        """Pending ask_human questions keyed by dashless notion_task_id (newest wins)."""
        resp = await self._c.get(f"{self._base}/api/human-questions",
                                 params={"status": "pending", "kind": "ask_human", "limit": PENDING_QUESTIONS_LIMIT})
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}")
        rows = resp.json() or []
        if len(rows) >= PENDING_QUESTIONS_LIMIT:  # the route limits tenant-wide, newest first
            raise RuntimeError(f"truncated at {PENDING_QUESTIONS_LIMIT} pending questions; "
                               "parked-by-question detection would be incomplete")
        out: dict[str, dict] = {}
        for q in rows:
            key = _norm_id(q.get("notion_task_id"))
            if key and key not in out:  # the route is newest first
                out[key] = q
        return out

    async def annotate(self, plan_slug: str, iteration_id: str, items: list[dict]) -> None:
        resp = await self._c.post(
            f"{self._base}/api/plans/{plan_slug}/iterations/{iteration_id}/annotations",
            json={"items": items})
        if resp.status_code != 200:
            raise RuntimeError(f"annotation write failed (HTTP {resp.status_code}): "
                               f"{resp.text[:200]}")

    async def return_to_caller(self, run_id: str, message: str) -> dict:
        """Wake whoever dispatched ``run_id``. A no-op on the platform side when
        the run already has ``call_me_back``, since that path delivers on finish."""
        resp = await self._c.post(f"{self._base}/api/runs/{run_id}/return-to-caller",
                                  json={"message": message, "kind": "result"})
        if resp.status_code != 200:
            return {"ok": False, "reason": f"HTTP {resp.status_code}"}
        return resp.json() or {}

    async def validate_agent(self, slug: str, *, setting: str = "AgentSlug") -> None:
        workspace = _this_workspace()
        label = f"{setting}={slug!r} in workspace {workspace!r}"
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", slug):
            raise CoderDispatchError(f"{label} must be a concrete agent slug")
        try:
            response = await self._c.get(f"{self._base}/api/agents/{slug}")
            if response.status_code != 200:
                raise CoderDispatchError(
                    f"{label} could not be verified (HTTP {response.status_code}). "
                    "Check that the agent exists and is active in Agents Platform."
                )
            agent = response.json()
        except CoderDispatchError:
            raise
        except Exception as exc:
            raise CoderDispatchError(f"Could not verify {label}: {exc}") from exc
        if not isinstance(agent, dict) or agent.get("slug") != slug or agent.get("deleted_at"):
            raise CoderDispatchError(f"{label} must name an active agent")
        if setting == DEFAULT_CODER_SETTING:
            if agent.get("workspace") and agent["workspace"] != workspace:
                raise CoderDispatchError(f"{label} belongs to workspace {agent['workspace']!r}")
            try:
                runners = await self._c.get(f"{self._base}/api/runners", params={"workspace": workspace})
                runners.raise_for_status()
                registered = any(row.get("workspace") == workspace and row.get("base_url")
                                 for row in runners.json())
            except Exception as exc:
                raise CoderDispatchError(f"Could not verify workspace registration for {label}: {exc}") from exc
            if not registered:
                raise CoderDispatchError(f"{label} requires a registered runner in that workspace")

    async def dispatch(self, path: str, payload: dict) -> tuple[str, str, bool]:
        """Return the run ID, error, and whether a rejection was definitive."""
        try:
            resp = await self._c.post(f"{self._base}{path}", json=payload)
            if resp.status_code != 200:
                return "", (f"dispatch failed: {resp.status_code} {resp.text[:200]}"), (
                    resp.status_code in (400, 401, 403, 404, 409, 422, 429))
            run = resp.json() or {}
            run_id = str(run.get("run_id") or run.get("id") or "")
            return run_id, "" if run_id else "dispatch response omitted the run ID", False
        except Exception as exc:
            return "", f"dispatch outcome unknown: {exc}", False


async def sweep_ready(board: BoardClient, platform: PlatformClient, *,
                      status: str = "ready", limit: int = 50,
                      dry_run: bool = False, claim_side: str = CLAIM_SIDE,
                      read_coder_setting: Callable[[], object] | None = None,
                      selected_card: dict | None = None,
                      default_target_slug: str = DEFAULT_TARGET_SLUG) -> dict:
    """Dispatch every Ready card that isn't already claimed or in flight.

    The single implementation behind both triggers: the ``run_ready_cards`` MCP
    tool (a human asking for a catch-all pass) and the in-process watchdog
    (plugin.py, every ``kanban_sweep_interval_s``). Deliberately one function —
    a second copy of this without the claim would put the double-fire back
    through the door the claim exists to close.

    Per card, and try/except per card so one bad card never ends the pass:
      1. skip if this platform already has a pending/running run for it — cheap,
         and the common case for a board the other side just dispatched from;
      2. re-read the card. Its body and comments are the prompt, and its status
         is the confirmation that it is still Ready and not something the other
         side moved out from under us between the list and now;
      3. resolve and validate the selected agent before writing to the card;
      4. claim the card and move it to running;
      5. dispatch and stamp the actual run ID, or recover a rejected claim.
    """
    cards = ([selected_card] if selected_card is not None
             else await asyncio.to_thread(board.ready_cards, status, limit))
    results: list[dict] = []

    for summary in cards:
        page_id = summary.get("page_id") or ""
        row: dict = {"page_id": page_id, "title": summary.get("title")}
        # The Notion display name of the Ready status, straight from a card the
        # board itself filtered as Ready — so the re-read below compares like
        # with like without this module having to know the status mapping.
        ready_name = summary.get("status")
        try:
            latest = await platform.latest_run(page_id)
            if latest and latest.get("status") in ACTIVE_RUN_STATUSES:
                results.append({**row, "skipped": "run-already-in-flight",
                                "run_id": latest.get("id")})
                continue

            card = await asyncio.to_thread(board.card, page_id)
            # list_cards' summary lacks body/comments; card() has them but is
            # one page's worth. Merge so the prompt sees everything.
            card = {**summary, **card}
            row["title"] = card.get("title")
            if QA_FINDINGS_TAG in _card_tags(card):
                # A findings card is a human triage list, not work to dispatch.
                results.append({**row, "skipped": "qa-findings-card"})
                continue
            if ready_name and card.get("status") != ready_name:
                results.append({**row, "skipped": "no-longer-ready",
                                "status": card.get("status")})
                continue

            explicit_agent = (card.get("agent_slug") or "").strip()
            workflow = (card.get("workflow_slug") or "").strip()
            default_coder = ""
            if explicit_agent:
                await platform.validate_agent(explicit_agent)
            elif not workflow:
                default_coder = await resolve_default_coder(
                    platform, read_coder_setting or board.default_coder)
            path, payload = dispatch_payload(
                card, build_run_input(card), default_coder=default_coder,
                default_target_slug=default_target_slug)
            if dry_run:
                results.append({**row, "would_dispatch": path,
                                "target_slug": payload["target_slug"]})
                continue

            held = await size_hold(platform, page_id)
            if held is not None:
                results.append({**row, "skipped": "size-hold", "reasons": held.get("reasons")})
                continue

            # Core and the gateway mount the same installed package. Hold its
            # manifest lock while changing the board and launching. This closes
            # the check/write gap between workspace triggers. The Notion stamp
            # still fences legacy dispatchers that do not share this filesystem.
            with DISPATCH_FENCE.open("rb") as fence:
                try:
                    fcntl.flock(fence, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    results.append({**row, "skipped": "workspace-dispatch-in-progress"})
                    continue
                current = await asyncio.to_thread(board.card, page_id, with_content=False)
                if current.get("status") != card.get("status") or any(
                    current.get(key) != card.get(key)
                    for key in ("agent_slug", "workflow_slug", "target_slug") if key in current
                ):
                    results.append({**row, "skipped": "card-changed"})
                    continue
                newest = await platform.latest_run(page_id)
                if newest and (newest.get("id") != (latest or {}).get("id")
                               or newest.get("status") in ACTIVE_RUN_STATUSES):
                    results.append({**row, "skipped": "card-run-changed", "run_id": newest.get("id")})
                    continue
                previous_stamp = await asyncio.to_thread(
                    board.properties, page_id, [RUN_ID_PROPERTY])
                token = await asyncio.to_thread(claim_card, board, page_id, side=claim_side)
                if token is None:
                    results.append({**row, "skipped": "claim-lost"})
                    continue

                await asyncio.to_thread(board.move, page_id, "running")
                run_id, error, rejected = await platform.dispatch(path, payload)
                if error and not rejected:
                    reconciled = await platform.latest_run(page_id)
                    expected = path.split("/")[3]
                    if (reconciled and reconciled.get("id") != (latest or {}).get("id")
                            and reconciled.get("source_slug") == expected
                            and reconciled.get("workspace") == payload["workspace"]
                            and reconciled.get("target_slug") == payload["target_slug"]):
                        run_id = reconciled.get("id")
                    if not run_id:
                        results.append({**row, "error": f"{error}. Check card-bound runs before retrying."})
                        continue
                if rejected:
                    stamp = await asyncio.to_thread(board.properties, page_id, [RUN_ID_PROPERTY])
                    if stamp.get(RUN_ID_PROPERTY) == token:
                        await asyncio.to_thread(board.set_property, page_id, RUN_ID_PROPERTY,
                                                previous_stamp.get(RUN_ID_PROPERTY) or "")
                        await asyncio.to_thread(board.move, page_id, status)
                    if default_coder:
                        error = (f"{DEFAULT_CODER_SETTING}={default_coder!r} in workspace "
                                 f"{payload['workspace']!r}: {error}")
                    results.append({**row, "error": error})
                    continue
                try:
                    await asyncio.to_thread(board.set_property, page_id, RUN_ID_PROPERTY, run_id)
                except BoardUnavailable as exc:
                    row["run_id_stamp_failed"] = str(exc)
                results.append({**row, "run_id": run_id, "target_slug": payload["target_slug"],
                                "agent_slug": explicit_agent or default_coder})
        except CoderDispatchError as exc:
            results.append({**row, "error": str(exc)})
        except BoardUnavailable as exc:
            results.append({**row, "error": f"board unavailable — {exc}"})
        except Exception as exc:  # pragma: no cover - defensive, per card
            log.warning("kanban sweep: card %s failed", page_id, exc_info=True)
            results.append({**row, "error": str(exc)})

    dispatched = sum(1 for r in results if r.get("run_id") and not r.get("skipped"))
    return {"considered": len(results), "dispatched": dispatched,
            "dry_run": dry_run, "results": results}


ATTENTION_DRIVER_AGENT = "project-manager"
ATTENTION_DEFAULT_LIMIT = 25
ATTENTION_CONCURRENCY = 4
# The route answers `pending` after 24s; a provisional annotation lands first.
ATTENTION_PROVISIONAL_AFTER_S = 20.0
ATTENTION_QUESTIONS_TIMEOUT_S = 8.0
PENDING_QUESTIONS_LIMIT = 500

_DRIVER_BRIEF = (
    "You are the Project Manager driving this card. Nothing else will pick it up. "
    "Your contract is the aw-agent-project-manager skill; follow it.\n"
    "1. Blocker handling starts with qa_search on the blocker in the card's own words (retry "
    "a failed search once and say so), then knowledge_search, then read the card, its comments "
    "and run history, and look for the server-side trace of whatever it waits on. The skill "
    "sets this order and the prerequisites for any ask_human or re-ping; it overrides this "
    "summary.\n"
    "2. Unblock it: resume the card's own agent with evidence (invoke_kanban_agent) or dispatch "
    "the agent that owns the missing decision (architect design, product-owner scope, coder "
    "build, debugger failure, qa verify), always passing this card's page_id. If that agent "
    "cannot unblock it, try the next. Do not do their work yourself; do not stack a run on a "
    "card with one in flight.\n"
    "3. An epic (the card has ChildCards) is its children: dispatch every open Backlog or Ready "
    "child, and close the parent only once every child is Done, Self-closed or Archived.\n"
    "4. Add every blocker answer that was not already in Q&A with qa_propose_create, tagged "
    "unblocking.\n"
    "5. Never move a card to Planned, and never move a Need Human card while it waits. A human "
    "is only for spend, credentials only the owner holds, an irreversible action on real data, "
    "or a product call: only after qa_search (retried once if it failed) and the server-side trace "
    "check, send ONE ask_human with the exact ask and your recommendation, verify "
    "it is pending, stamp SweepBlock `routed-human|1|<iso>|<the ask>`, and end the turn.\n"
    "6. End the turn with exactly one of: a dispatch/resume, a pending ask_human, or "
    "mark_flow_done (Done needs a real qa_run_id or a justified qa_not_needed)."
)


def human_judged_block(sweep_block: str | None) -> str | None:
    """The detail of a SweepBlock that says an agent concluded only a human can act, else None.

    Same rule as aw-app-notion ``_reping_human_judged`` (notion_app/kanban/cards.py): at least
    four ``|`` fields and either ``routed-human`` first or ``escalated`` second. Detail is free
    text and may contain ``|``. Anything malformed is "not judged"."""
    parts = (sweep_block or "").strip().split("|")
    if len(parts) < 4:
        return None
    if parts[0].strip() == "routed-human" or parts[1].strip() == "escalated":
        return "|".join(parts[3:]).strip() or "human-judged"
    return None


def build_driver_input(card: dict, summary: dict | None = None) -> str:
    """The per-card prompt for the attention driver: the standing brief, why
    the board flagged the card, then the card itself (same body/comments
    layout every other dispatch uses)."""
    flagged = {k: summary[k] for k in ("reason", "reasons", "blocker", "idle_hours", "status")
               if summary and summary.get(k) not in (None, "", [])}
    why = ("Why the board flagged it: " + json.dumps(flagged, default=str)
           if flagged else "")
    return "\n\n".join(p for p in (_DRIVER_BRIEF, why, build_run_input(card)) if p)


async def sweep_attention(board: BoardClient, platform: PlatformClient, *,
                          limit: int = ATTENTION_DEFAULT_LIMIT,
                          agent_slug: str = ATTENTION_DRIVER_AGENT,
                          dry_run: bool = False,
                          plan_slug: str | None = None,
                          default_target_slug: str = DEFAULT_TARGET_SLUG) -> dict:
    """Fire one driver prompt per card the board says needs attention.

    Replaces handing the whole list to the Manager: each card gets its own
    run of ``agent_slug`` (the Project Manager), whose only job is to drive that
    card to completion and dispatch whatever agent it needs. No claim and no
    status move: the card stays where it is, the driver moves it. A card with
    a run already pending/running is skipped, so a repeat tick never stacks a
    second driver on the same card, and a Need Human card parked by a human-judged SweepBlock is
    skipped (``parked-on-human``). One card's failure never ends the pass.

    With ``plan_slug`` the pass also writes the plan's running iteration's ledger annotations
    (action + why) for every gate candidate, so the iteration can close ``done``; a failure to
    annotate is reported under ``annotation`` and never fails the dispatches.
    """
    started = asyncio.get_running_loop().time()  # the route's 24s clock starts with this task
    annotator = await _open_annotator(platform, plan_slug) if plan_slug and not dry_run else None
    attention = await asyncio.to_thread(board.need_attention, limit=limit, reping=True)
    cards = attention.get("cards") if isinstance(attention, dict) else None
    cards = cards if isinstance(cards, list) else []
    results: list[dict] = []
    if cards and not dry_run:
        await platform.validate_agent(agent_slug)
    async def _fetch_questions() -> tuple[dict[str, dict], str | None]:
        try:  # fail open: a failed or slow lookup dispatches as before and is reported
            return await asyncio.wait_for(platform.pending_questions(),
                                          ATTENTION_QUESTIONS_TIMEOUT_S), None
        except Exception as exc:
            log.warning("attention sweep: pending human-questions fetch failed", exc_info=True)
            return {}, (str(exc) or type(exc).__name__)[:200]

    # Started with the pass (inside the work the provisional timer covers), awaited per card.
    lookup = asyncio.ensure_future(_fetch_questions()) if cards else None
    sem = asyncio.Semaphore(ATTENTION_CONCURRENCY)

    async def _one(summary: dict) -> dict:
        page_id = summary.get("page_id") or ""
        row: dict = {"page_id": page_id, "title": summary.get("title")}
        async with sem:
            try:
                parked = _parked_on_human(summary)
                if parked:
                    return {**row, "skipped": "parked-on-human", "detail": parked}
                latest = await platform.latest_run(page_id)
                if latest and latest.get("status") in ACTIVE_RUN_STATUSES:
                    return {**row, "skipped": "run-already-in-flight", "run_id": latest.get("id")}
                card = {**summary, **(await asyncio.to_thread(board.card, page_id))}
                row["title"] = card.get("title")
                questions, _err = await lookup
                parked = _parked_on_human(card) or _parked_by_question(card, questions)
                if parked:
                    return {**row, "skipped": "parked-on-human", "detail": parked}
                if QA_FINDINGS_TAG in _card_tags(card):
                    return {**row, "skipped": "qa-findings-card"}
                path, payload = dispatch_payload(
                    {**card, "agent_slug": agent_slug, "workflow_slug": ""},
                    build_driver_input(card, summary), default_target_slug=default_target_slug)
                if dry_run:
                    return {**row, "would_dispatch": path, "target_slug": payload["target_slug"]}
                held = await size_hold(platform, page_id)
                if held is not None:
                    return {**row, "skipped": "size-hold", "reasons": held.get("reasons")}
                run_id, error, _rejected = await platform.dispatch(path, payload)
                if error:
                    return {**row, "error": error}
                return {**row, "run_id": run_id, "agent_slug": agent_slug}
            except BoardUnavailable as exc:
                return {**row, "error": f"board unavailable: {exc}"}
            except Exception as exc:  # per card: one bad card never ends the pass
                log.warning("attention sweep: card %s failed", page_id, exc_info=True)
                return {**row, "error": str(exc)}

    summaries = {c.get("page_id"): c for c in cards}
    gathered = asyncio.ensure_future(asyncio.gather(*(_one(c) for c in cards)))
    if annotator:
        remaining = ATTENTION_PROVISIONAL_AFTER_S - (asyncio.get_running_loop().time() - started)
        done, _ = await asyncio.wait({gathered}, timeout=max(0.0, remaining))
        if not done:
            await annotator.write_provisional()
    results = list(await gathered)
    dispatched = sum(1 for r in results if r.get("run_id") and not r.get("skipped"))
    out = {"has_work": bool(cards), "considered": len(results), "dispatched": dispatched,
           "dry_run": dry_run, "results": results}
    if lookup:
        _, questions_error = await lookup
        if questions_error:
            out["human_questions"] = questions_error
    if annotator:
        out["annotation"] = await annotator.write_final(results, summaries, limit=limit)
    elif plan_slug and not dry_run:
        out["annotation"] = {"annotated": 0, "reason": "no-running-iteration"}
    return out


def _norm_id(page_id) -> str:
    return str(page_id or "").replace("-", "").strip().lower()


def _parked_by_question(card: dict, questions: dict[str, dict]) -> str | None:
    """Detail when a Need Human card has an ask_human already pending on it, from any agent."""
    if str(card.get("status") or "").strip().lower() != "need human":
        return None
    q = questions.get(_norm_id(card.get("page_id")))
    if not q:
        return None
    return f"ask_human {q.get('id')} by {q.get('agent_slug')}: {str(q.get('question') or '')[:200]}"


def _parked_on_human(card: dict) -> str | None:
    """The SweepBlock detail when this is a Need Human card a human-judged block parks."""
    if str(card.get("status") or "").strip().lower() != "need human":
        return None
    return human_judged_block(card.get("sweep_block"))


def annotation_for(row: dict, summary: dict) -> dict:
    """The ledger action + why for one card's sweep outcome."""
    if row.get("error"):
        return {"action": "dispatch-failed", "why": str(row["error"])[:500]}
    if row.get("skipped"):
        why = (row.get("detail") or row.get("run_id")
               or ("card carries the qa-findings tag" if row["skipped"] == "qa-findings-card"
                   else row["skipped"]))
        return {"action": str(row["skipped"]), "why": str(why)[:500]}
    reason = (summary or {}).get("reason") or ", ".join(
        (summary or {}).get("reasons") or []) or "flagged by the board"
    return {"action": f"dispatched-{row.get('agent_slug') or 'unknown'}",
            "why": f"{reason}; run {row.get('run_id')}"[:500]}


class _IterationAnnotator:
    """Writes the running iteration's per-card annotations (AP-MT plans API, same door and
    token as every other platform call here)."""

    def __init__(self, platform: PlatformClient, slug: str, iteration_id: str,
                 candidates: list[str]) -> None:
        self._p, self._slug, self._it, self.candidates = platform, slug, iteration_id, candidates

    async def _write(self, items: list[dict]) -> dict:
        if not items:
            return {"annotated": 0, "iteration_id": self._it}
        try:
            await self._p.annotate(self._slug, self._it, items)
        except Exception as exc:  # never fails the dispatches
            log.warning("attention sweep: annotation failed", exc_info=True)
            return {"annotated": 0, "iteration_id": self._it, "error": str(exc)}
        return {"annotated": len(items), "iteration_id": self._it}

    async def write_provisional(self) -> dict:
        return await self._write([
            {"card": c, "action": "dispatch-pending",
             "why": "sweep dispatches still running; the final annotation overwrites this"}
            for c in self.candidates])

    async def write_final(self, results: list[dict], summaries: dict, *, limit: int) -> dict:
        by_card = {r.get("page_id"): r for r in results if r.get("page_id")}
        items = []
        for c in self.candidates:
            row = by_card.get(c)
            if row is None:
                items.append({"card": c, "action": "not-in-dispatch-window",
                              "why": f"gate candidate not in the sweep's own board read (limit {limit})"})
            else:
                items.append({"card": c, **annotation_for(row, summaries.get(c) or {})})
        if not self.candidates:  # candidates unreadable: still record what we did
            items = [{"card": c, **annotation_for(r, summaries.get(c) or {})}
                     for c, r in by_card.items()]
        return await self._write(items)


async def _open_annotator(platform: PlatformClient, plan_slug: str):
    """The annotator for the plan's running iteration, or None (manual call, plan renamed,
    plans API unreachable): annotating is best effort and must not block the dispatches."""
    try:
        found = await platform.running_iteration(plan_slug)
    except Exception:
        log.warning("attention sweep: resolving the running iteration failed", exc_info=True)
        return None
    if not found:
        return None
    iteration_id, candidates = found
    return _IterationAnnotator(platform, plan_slug, iteration_id, candidates)


# The executor stamps AgentSlug with whoever ran last, so any of these can be on a card;
# none of them implements.
NON_IMPLEMENTERS = ("qa", "architect", "project-manager", "product-owner")
NON_IMPLEMENTER_PREFIXES = ("qa-", "architect-", "project-manager-", "product-owner-")


async def handoff_card(board: BoardClient, platform: PlatformClient, *, source_id: str,
                       page_id: str, revision: str, evidence: str) -> dict:
    """Transfer an in-progress card from its active Architect run.

    QA never hands a card back: it records its outcome with
    ``report_qa_findings`` and the card closes either way.

    The source is the only active run exempted. The shared dispatch lock
    serializes this transition with manual and scheduled Ready dispatch.
    A stable marker in the recipient input reconciles a lost response.
    """
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9./:@_-]{0,199}", revision) or not evidence.strip():
        raise ValueError("A stable reviewed revision or design reference and evidence are required")

    with DISPATCH_FENCE.open("rb") as fence:
        try:
            fcntl.flock(fence, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"ok": False, "reason": "workspace-dispatch-in-progress"}

        runs = await platform.card_runs(page_id, limit=5000)
        source = next((run for run in runs if run["id"] == source_id), None)
        if not source or source.get("workspace") != _this_workspace():
            raise ValueError("Caller must be a work run bound to this card and workspace")
        slug = source.get("source_slug") or ""
        if slug == "qa" or slug.startswith("qa-"):
            raise ValueError("QA does not hand cards back to a Coder: record the outcome with "
                             "report_qa_findings; findings go to the target's findings card")
        if not (slug == "architect" or slug.startswith("architect-")):
            raise ValueError("Only the card's Architect run can use this handoff")

        target = source.get("target_slug")
        if not target:
            raise ValueError("Caller has no target")
        stage = "architect-build"
        key = hashlib.sha256(json.dumps([page_id, target, stage, revision]).encode()).hexdigest()
        marker = f"Kanban handoff {key}"
        token = f"handoff:{key}"
        receipt = {"page_id": page_id, "target_slug": target, "revision": revision,
                   "source_run_id": source_id, "evidence": evidence}

        def recipient(rows: list[dict]) -> dict | None:
            return next((r for r in rows if r.get("target_slug") == target
                         and r.get("workspace") == _this_workspace()
                         and r.get("parent_run_id")
                         and ((r.get("input") or {}).get("input") or "").startswith(marker + "\n")), None)

        def result(run: dict, *, reused: bool) -> dict:
            return {**receipt, "ok": True, "run_id": run["id"],
                    "recipient": run.get("source_slug"), "reused": reused}

        previous = recipient(runs)
        if previous:
            response = result(previous, reused=True)
            try:
                props = await asyncio.to_thread(board.properties, page_id, [RUN_ID_PROPERTY])
                if props.get(RUN_ID_PROPERTY) == token:
                    await asyncio.to_thread(board.set_property, page_id, RUN_ID_PROPERTY, previous["id"])
                    await asyncio.to_thread(board.add_comment, page_id,
                                            f"{marker} → {previous['id']} ({previous.get('source_slug')})")
            except BoardUnavailable as exc:
                response["board_record_error"] = str(exc)
            return response
        if len(runs) == 5000:
            return {"ok": False, "reason": "incomplete-run-history"}
        if source.get("status") not in ACTIVE_RUN_STATUSES:
            return {"ok": False, "reason": "source-terminated"}
        if any(r["id"] != source_id and r.get("status") in ACTIVE_RUN_STATUSES for r in runs):
            return {"ok": False, "reason": "another-card-run-active"}

        card = await asyncio.to_thread(board.card, page_id)
        card_status = card.get("status")
        if card_status != "In Progress" or card.get("target_slug") != target:
            return {"ok": False, "reason": "expected-active-card-on-source-target"}
        selected = (card.get("agent_slug") or "").strip()
        if selected == slug or selected in NON_IMPLEMENTERS or selected.startswith(NON_IMPLEMENTER_PREFIXES):
            selected = ""
        card = {**card, "agent_slug": selected}
        default_coder = ""
        if selected:
            await platform.validate_agent(selected)
        elif not card.get("workflow_slug"):
            default_coder = await resolve_default_coder(platform, board.default_coder)
        if default_coder == slug or default_coder in NON_IMPLEMENTERS or default_coder.startswith(NON_IMPLEMENTER_PREFIXES):
            raise ValueError("Workspace default must name a Coder")

        stamp = (await asyncio.to_thread(board.properties, page_id, [RUN_ID_PROPERTY])).get(RUN_ID_PROPERTY)
        if str(stamp or "").startswith(("handoff:", "mono:", "mt:")):
            return {"ok": False, "reason": "unresolved-card-claim", "claim": stamp}

        findings = (f"{marker}\nStage: {stage}\nCard: {page_id}\nTarget: {target}\n"
                    f"Revision: {revision}\nSource run: {source_id}\n\n{evidence.strip()}")
        path, payload = dispatch_payload(card, f"{findings}\n\n{build_run_input(card)}",
                                         default_coder=default_coder)
        payload["caller_run_id"] = source_id
        held = await size_hold(platform, page_id)
        if held is not None:
            return {"ok": False, "reason": "size-hold", "reasons": held.get("reasons")}
        if not await asyncio.to_thread(claim_card, board, page_id, token=token):
            return {"ok": False, "reason": "claim-lost"}
        try:
            await asyncio.to_thread(board.add_comment, page_id, findings)
        except BoardUnavailable:
            await asyncio.to_thread(board.set_property, page_id, RUN_ID_PROPERTY, stamp or "")
            raise

        run_id, error, rejected = await platform.dispatch(path, payload)
        if rejected:
            await asyncio.to_thread(board.set_property, page_id, RUN_ID_PROPERTY, stamp or "")
            return {"ok": False, "reason": error}
        verified = recipient(await platform.card_runs(page_id, limit=100))
        if (not verified or verified.get("source_slug") != path.split("/")[3]
                or (run_id and run_id != verified["id"])):
            return {"ok": False, "reason": error or "dispatch not yet visible in card history",
                    "claim": token}
        response = result(verified, reused=False)
        try:
            await asyncio.to_thread(board.set_property, page_id, RUN_ID_PROPERTY, verified["id"])
            await asyncio.to_thread(board.add_comment, page_id,
                                    f"{marker} → {verified['id']} ({verified.get('source_slug')})")
        except BoardUnavailable as exc:
            response["board_record_error"] = str(exc)
        return response


QA_FINDINGS_TAG = "qa-findings"


def _card_tags(card: dict) -> set[str]:
    raw = card.get("tags") or []
    if isinstance(raw, str):
        raw = raw.split(",")
    return {str(t).strip().lower() for t in raw if str(t).strip()}


QA_VERDICTS = ("passed", "issues")
QA_PASS_STATUSES = ("done", "ready_to_deploy")


def _qa_card_comment(verdict: str, revision: str, summary: str, findings: list[dict],
                     findings_url: str, marker: str) -> str:
    lines = [marker,
             f"QA {'passed' if verdict == 'passed' else 'found issues'} at revision {revision}."]
    if summary.strip():
        lines.append(summary.strip())
    for f in findings:
        lines.append(f"- [{f.get('severity') or 'minor'}] {f['title']}: {f['evidence']}")
    if findings_url:
        lines.append(f"Filed on the target's findings card: [QA findings]({findings_url})")
    return "\n".join(lines)


async def report_qa_findings(board: BoardClient, platform: PlatformClient, *, source_id: str,
                             page_id: str, verdict: str, revision: str, summary: str = "",
                             findings: list[dict] | None = None,
                             pass_status: str = "done") -> dict:
    """QA's terminal action: record the outcome and close the card.

    ``passed`` stamps the card and moves it to ``pass_status``. ``issues``
    files each reproduced finding on the target's findings card (one per
    target, reused across cards), comments on the tested card, stamps
    QAStatus=issues and moves it to Done. Nothing is dispatched: QA never
    sends the card back to a Coder. The dispatching channel is woken with the
    findings once the board is updated.
    """
    findings = findings or []
    if verdict not in QA_VERDICTS:
        raise ValueError(f"verdict must be one of {QA_VERDICTS}")
    if pass_status not in QA_PASS_STATUSES:
        raise ValueError(f"pass_status must be one of {QA_PASS_STATUSES}")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9./:@_-]{0,199}", revision or ""):
        raise ValueError("A stable reviewed revision is required")
    if verdict == "issues" and not any(
            isinstance(f, dict) and str(f.get("title") or "").strip()
            and str(f.get("evidence") or "").strip() for f in findings):
        raise ValueError("verdict 'issues' needs at least one finding with a title and runtime "
                         "evidence; a suspicion you could not reproduce is not a finding")

    runs = await platform.card_runs(page_id, limit=5000)
    source = next((run for run in runs if run["id"] == source_id), None)
    if not source or source.get("workspace") != _this_workspace():
        raise ValueError("Caller must be a work run bound to this card and workspace")
    slug = source.get("source_slug") or ""
    if not (slug == "qa" or slug.startswith("qa-")):
        raise ValueError("Only the card's QA run can report QA findings")
    target = source.get("target_slug")
    if not target:
        raise ValueError("Caller has no target")

    # A partly-verified report (every finding is a "Not verified:" item) must not share a
    # dedup identity with a later report of reproduced failures on the same revision.
    partial = bool(findings) and all(
        isinstance(f, dict) and str(f.get("title") or "").startswith("Not verified:")
        for f in findings)
    key_parts = [page_id, target, revision, verdict] + (["partial"] if partial else [])
    key = hashlib.sha256(json.dumps(key_parts).encode()).hexdigest()
    marker = f"QA findings {key[:16]}"
    card = await asyncio.to_thread(board.card, page_id)
    if marker in (card.get("comments_md") or ""):
        return {"ok": True, "reused": True, "page_id": page_id, "verdict": verdict,
                "revision": revision}

    findings_url = ""
    filed: dict = {}
    if verdict == "issues":
        filed = await asyncio.to_thread(
            board.qa_findings, target_slug=target, page_id=page_id, revision=revision,
            findings=findings, summary=summary)
        findings_url = filed.get("findings_url") or ""
    clean = [f for f in findings if isinstance(f, dict) and f.get("title") and f.get("evidence")]
    comment = _qa_card_comment(verdict, revision, summary, clean, findings_url, marker)
    status = "done" if verdict == "issues" else pass_status
    await asyncio.to_thread(board.set_property, page_id, "QAStatus",
                            "issues" if verdict == "issues" else status)
    await asyncio.to_thread(board.move, page_id, status, comment)

    receipt = {"ok": True, "page_id": page_id, "verdict": verdict, "revision": revision,
               "card_status": status, "findings_card": findings_url or None,
               "findings_page_id": filed.get("findings_page_id"), "added": filed.get("added", 0)}
    try:
        receipt["wake"] = await platform.return_to_caller(source_id, comment)
    except Exception as exc:  # a failed wake must not undo a recorded verdict
        receipt["wake"] = {"ok": False, "reason": str(exc)}
    return receipt


__all__ = ["BoardClient", "BoardUnavailable", "PlatformClient", "build_run_input",
           "dispatch_payload", "resume_payload", "board_base_url", "claim_card",
           "sweep_ready", "report_qa_findings", "RUN_ID_PROPERTY", "DEFAULT_TARGET_SLUG", "CLAIM_SIDE"]
