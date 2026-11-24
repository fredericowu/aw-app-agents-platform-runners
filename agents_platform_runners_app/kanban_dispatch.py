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

    async def card_runs(self, page_id: str, *, limit: int) -> list[dict]:
        resp = await self._c.get(f"{self._base}/api/runs",
                                 params={"notion_task_id": page_id, "category": "work", "limit": limit})
        if resp.status_code != 200:
            raise RuntimeError(f"Card-bound run lookup failed for {page_id} (HTTP {resp.status_code})")
        return resp.json() or []

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
        if selected == slug or selected in ("qa", "architect") or selected.startswith(("qa-", "architect-")):
            selected = ""
        card = {**card, "agent_slug": selected}
        default_coder = ""
        if selected:
            await platform.validate_agent(selected)
        elif not card.get("workflow_slug"):
            default_coder = await resolve_default_coder(platform, board.default_coder)
        if default_coder == slug or default_coder in ("qa", "architect") or default_coder.startswith(("qa-", "architect-")):
            raise ValueError("Workspace default must name a Coder")

        stamp = (await asyncio.to_thread(board.properties, page_id, [RUN_ID_PROPERTY])).get(RUN_ID_PROPERTY)
        if str(stamp or "").startswith(("handoff:", "mono:", "mt:")):
            return {"ok": False, "reason": "unresolved-card-claim", "claim": stamp}

        findings = (f"{marker}\nStage: {stage}\nCard: {page_id}\nTarget: {target}\n"
                    f"Revision: {revision}\nSource run: {source_id}\n\n{evidence.strip()}")
        path, payload = dispatch_payload(card, f"{findings}\n\n{build_run_input(card)}",
                                         default_coder=default_coder)
        payload["caller_run_id"] = source_id
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

    key = hashlib.sha256(json.dumps([page_id, target, revision, verdict]).encode()).hexdigest()
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
