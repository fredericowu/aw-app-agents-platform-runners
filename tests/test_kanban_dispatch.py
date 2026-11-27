"""The Kanban→run bridge.

The pure decision logic (what prompt, which endpoint, resumable or not) is
separated from the I/O in ``kanban_dispatch`` precisely so it can be tested
without a Notion token, an agents-platform, or a workspace API — that is what
this file exercises. The orchestration in ``mcp_server`` on top of it is thin
by design: skip, read, move, fire, stamp.

Run: python -m pytest tests/test_kanban_dispatch.py
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents_platform_runners_app import kanban_dispatch as kd  # noqa: E402


def _card(**over):
    base = {"page_id": "page-1", "title": "Fix the thing",
            "agent_slug": "coder-sonnet", "workflow_slug": None,
            "target_slug": "some-target",
            "body_md": "The button does nothing on mobile.",
            "comments_md": "- **2026-08-01** — tried a CSS fix, no luck"}
    base.update(over)
    return base


# ── the prompt ──────────────────────────────────────────────────────────

def test_prompt_carries_the_body_and_the_comments():
    text = kd.build_run_input(_card())
    assert "The button does nothing on mobile." in text
    assert "tried a CSS fix" in text
    assert "page_id=page-1" in text
    # Without this the agent guesses at REST endpoints instead of calling the
    # tools it already has — the specific incident that put the line in.
    assert "aw-kanban" in text


def test_prompt_falls_back_to_the_title_when_the_card_has_no_body():
    text = kd.build_run_input(_card(body_md="", comments_md=""))
    assert "Run task: Fix the thing" in text
    assert "Comment history" not in text, "no comments means no empty section"


def test_prompt_orders_body_before_history():
    text = kd.build_run_input(_card())
    assert text.index("Task content") < text.index("Comment history")


# ── which endpoint ──────────────────────────────────────────────────────

def test_agent_card_dispatches_to_the_agent_endpoint():
    path, payload = kd.dispatch_payload(_card(), "INPUT")
    assert path == "/api/agents/coder-sonnet/run"
    assert payload["input"] == {"input": "INPUT"}
    assert payload["notion_task_id"] == "page-1"
    assert payload["target_slug"] == "some-target"


def test_workflow_card_dispatches_to_the_workflow_endpoint():
    path, _ = kd.dispatch_payload(
        _card(agent_slug=None, workflow_slug="spec-pipeline"), "INPUT")
    assert path == "/api/workflows/spec-pipeline/run"


def test_agent_wins_when_a_card_names_both():
    """Same precedence the monolith had. Worth pinning: a board where someone
    filled in both fields should behave predictably, not by dict ordering."""
    path, _ = kd.dispatch_payload(
        _card(agent_slug="coder-sonnet", workflow_slug="spec-pipeline"), "INPUT")
    assert path == "/api/agents/coder-sonnet/run"


def test_card_without_explicit_selection_uses_builtin_coder():
    path, payload = kd.dispatch_payload(_card(agent_slug=None, workflow_slug=None), "X")
    assert path == "/api/agents/coder/run"
    assert payload["input"] == {"input": "X"}


def test_card_without_a_target_gets_the_monolith_default():
    """Cards created before this port rely on it, so the default is behaviour,
    not a convenience."""
    _, payload = kd.dispatch_payload(_card(target_slug=""), "INPUT")
    assert payload["target_slug"] == kd.DEFAULT_TARGET_SLUG == "system-investigations"


def test_agent_dispatch_names_this_workspace(monkeypatch):
    """bug:runner-execute-401-monitor-shell-dispatch: naming the workspace
    explicitly lets AP-MT resolve the SAME workspace's Runner row instead of
    depending on inheritance alone."""
    monkeypatch.setenv("AW_WORKSPACE", "aw")
    _, payload = kd.dispatch_payload(_card(), "INPUT")
    assert payload["workspace"] == "aw"


def test_workflow_dispatch_now_also_sends_workspace(monkeypatch):
    """run_workflow_ep gained a workspace param (docs/design/
    ap-mt-workspace-cascade.md slice 1) — it used to silently drop it, which
    made this payload deliberately omit it to stay honest about what was
    used. Now that the server accepts it, the sweep path names it on the
    workflow leg too, same as the agent leg."""
    monkeypatch.setenv("AW_WORKSPACE", "aw")
    _, payload = kd.dispatch_payload(
        _card(agent_slug=None, workflow_slug="spec-pipeline"), "INPUT")
    assert payload["workspace"] == "aw"


# ── resume ──────────────────────────────────────────────────────────────

def test_resume_reuses_the_runs_own_agent_and_session():
    path, payload = kd.resume_payload(
        {"source_slug": "coder-sonnet", "session_id": "sess-1", "target_id": "t-1"},
        "page-1", "keep going")
    assert path == "/api/agents/coder-sonnet/run"
    assert payload["session_id"] == "sess-1"
    assert payload["input"] == {"input": "keep going"}
    assert payload["target_id"] == "t-1"
    assert payload["notion_task_id"] == "page-1"


def test_resume_omits_target_id_when_the_run_has_none():
    _, payload = kd.resume_payload(
        {"source_slug": "a", "session_id": "s"}, "page-1", "x")
    assert "target_id" not in payload


def test_resume_also_names_this_workspace(monkeypatch):
    monkeypatch.setenv("AW_WORKSPACE", "aw")
    _, payload = kd.resume_payload(
        {"source_slug": "coder-sonnet", "session_id": "sess-1"}, "page-1", "x")
    assert payload["workspace"] == "aw"


def test_resume_arms_the_callback_at_creation_when_a_caller_exists():
    _, payload = kd.resume_payload(
        {"source_slug": "a", "session_id": "s"}, "page-1", "x", callback_origin_run_id="mgr-run")
    assert payload["callback_origin_run_id"] == "mgr-run" and "caller_run_id" not in payload and payload["call_me_back"] is True


def test_resume_without_a_caller_sends_call_me_back_false_not_a_400():
    _, payload = kd.resume_payload(
        {"source_slug": "a", "session_id": "s"}, "page-1", "x")
    assert payload["call_me_back"] is False and "caller_run_id" not in payload


def test_resume_call_me_back_on_redirect_needs_no_caller_and_opt_out_wins():
    run = {"source_slug": "a", "session_id": "s"}
    _, p = kd.resume_payload(run, "p", "x", call_me_back_on="sess-A")
    assert p["call_me_back"] is True and p["call_me_back_on"] == "sess-A"
    _, p = kd.resume_payload(run, "p", "x", callback_origin_run_id="r", call_me_back=False)
    assert p["call_me_back"] is False
    assert "callback_origin_run_id" not in p  # opt-out sends no origin at all


def test_a_run_with_no_session_explains_itself_instead_of_failing():
    """A run that never opened a CLI session is a real state — an agent asking
    why should get a sentence, not a stack trace."""
    reason = kd.resume_payload({"source_slug": "coder-sonnet"}, "page-1", "x")
    assert isinstance(reason, str)
    assert "session_id" in reason


def test_a_run_with_no_agent_explains_itself():
    reason = kd.resume_payload({"session_id": "s"}, "page-1", "x")
    assert isinstance(reason, str)
    assert "source_slug" in reason


def test_a_monitor_wrapper_run_is_refused_even_with_a_session_id():
    """Belt-and-braces (Kanban 4115bf3b-9510-8156-9e5e-f6c772ad2538): a
    monitor/wrapper row that DOES carry a session_id must still be refused
    here, not resumed as if it were the real coding session — the caller is
    expected to have already filtered with category="work", but this must
    not silently resume one that slips through anyway."""
    reason = kd.resume_payload(
        {"source_slug": "monitor-shell", "session_id": "sess-wrapper", "category": "monitor"},
        "page-1", "x")
    assert isinstance(reason, str)
    assert "monitor" in reason


def test_a_work_category_run_still_resumes_normally():
    """category="work" (the default once the column exists) must not be
    mistaken for a non-work category and refused."""
    path, payload = kd.resume_payload(
        {"source_slug": "coder-sonnet", "session_id": "sess-1", "category": "work"},
        "page-1", "keep going")
    assert path == "/api/agents/coder-sonnet/run"
    assert payload["session_id"] == "sess-1"


# ── the board client's own failure surface ──────────────────────────────

def test_board_prefers_the_published_url_over_loopback(monkeypatch):
    """Both apps are `tier: inprocess`, which makes loopback look right and
    isn't: this module is imported by an MCP server the gateway spawns inside
    ITS OWN container, where 127.0.0.1 is the gateway. Loopback-first there
    doesn't fail loudly — it talks to the wrong server."""
    monkeypatch.delenv("AW_LOCAL_API_URL", raising=False)
    monkeypatch.setenv("AW_WORKSPACE_API_URL", "https://aw.example")
    assert kd.board_base_url() == "https://aw.example"


def test_board_falls_back_to_loopback_only_as_a_last_resort(monkeypatch):
    """Kept for running inside the server itself — tests, a REPL."""
    monkeypatch.delenv("AW_LOCAL_API_URL", raising=False)
    monkeypatch.delenv("AW_WORKSPACE_API_URL", raising=False)
    monkeypatch.delenv("AW_PORT", raising=False)
    monkeypatch.setattr(kd, "_from_env_file", lambda _n: None)
    assert kd.board_base_url() == "http://127.0.0.1:9030"


def test_the_upstream_env_carries_the_workspace_credentials(monkeypatch):
    """The bug this pair exists to prevent: the tools shipped, registered, and
    returned 503 on the first real call because the gateway's stdio child
    inherits nothing from the workspace server."""
    from agents_platform_runners_app import plugin

    monkeypatch.setenv("AW_WORKSPACE_API_URL", "https://aw.example")
    monkeypatch.setenv("AW_WORKSPACE_API_KEY", "k-123")
    env = plugin.build_mcp_servers({})["agents-platform-runners"]["env"]
    assert env["AW_WORKSPACE_API_URL"] == "https://aw.example"
    assert env["AW_WORKSPACE_API_KEY"] == "k-123"


def test_the_upstream_env_also_carries_aw_backend_credentials(monkeypatch):
    """kanban_check_deployed's core/remote-host adapters (deploy_reconcile.py)
    need AW_BACKEND_URL + AW_WORKSPACE baked in too, same reason as the
    AW_WORKSPACE_API_* pair above."""
    from agents_platform_runners_app import plugin

    monkeypatch.setenv("AW_BACKEND_URL", "https://api.aw.example")
    monkeypatch.setenv("AW_WORKSPACE", "acme")
    env = plugin.build_mcp_servers({})["agents-platform-runners"]["env"]
    assert env["AW_BACKEND_URL"] == "https://api.aw.example"
    assert env["AW_WORKSPACE"] == "acme"


def test_a_missing_credential_is_omitted_not_written_blank(monkeypatch):
    """A blank value would reach the board as a 401 nobody can place; absent,
    the upstream raises its own "not set" error naming where it comes from."""
    from agents_platform_runners_app import plugin

    monkeypatch.delenv("AW_WORKSPACE_API_KEY", raising=False)
    monkeypatch.setattr(plugin, "_workspace_env",
                        lambda n: "" if n == "AW_WORKSPACE_API_KEY" else "https://aw.example")
    env = plugin.build_mcp_servers({})["agents-platform-runners"]["env"]
    assert "AW_WORKSPACE_API_KEY" not in env
    assert env["AGENTS_BASE"]


def test_board_names_itself_when_it_cannot_be_reached(monkeypatch):
    import urllib.error

    monkeypatch.setenv("AW_WORKSPACE_API_KEY", "k")
    monkeypatch.setattr(kd.urllib.request, "urlopen",
                        lambda *a, **kw: (_ for _ in ()).throw(
                            urllib.error.URLError("Connection refused")))
    try:
        kd.BoardClient().ready_cards()
    except kd.BoardUnavailable as exc:
        assert "aw-app-notion" in str(exc)
    else:
        raise AssertionError("expected BoardUnavailable")


def test_a_404_is_reported_as_the_notion_app_being_absent(monkeypatch):
    """The likeliest real cause by far — this tool ships in an app that does
    not depend on aw-app-notion being installed."""
    import urllib.error

    monkeypatch.setenv("AW_WORKSPACE_API_KEY", "k")

    def gone(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 404, "nope", {}, None)

    monkeypatch.setattr(kd.urllib.request, "urlopen", gone)
    try:
        kd.BoardClient().ready_cards()
    except kd.BoardUnavailable as exc:
        assert "not installed" in str(exc)
    else:
        raise AssertionError("expected BoardUnavailable")


def test_a_response_level_timeout_is_also_reported_as_boardunavailable(monkeypatch):
    """Root cause of the live kanban_check_deployed hang on 2026-11-12: a
    socket timeout raised from h.getresponse()/resp.read() (the response
    phase) is a bare TimeoutError, NOT urllib.error.URLError — only the send
    phase (h.request) gets wrapped into URLError by urllib itself. Before
    this fix, that bare TimeoutError escaped _request() uncaught, then
    escaped check_deployed()'s per-card BoardUnavailable guard too, killing
    the whole batch three layers up with no result at all."""
    monkeypatch.setenv("AW_WORKSPACE_API_KEY", "k")

    class SlowResponse:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self):
            raise TimeoutError("timed out")

    monkeypatch.setattr(kd.urllib.request, "urlopen", lambda *a, **kw: SlowResponse())
    try:
        kd.BoardClient().move("p1", "done")
    except kd.BoardUnavailable as exc:
        assert "did not respond in time" in str(exc)
    else:
        raise AssertionError("expected BoardUnavailable, not a bare TimeoutError")


def test_a_malformed_response_body_is_also_reported_as_boardunavailable(monkeypatch):
    """Code-review finding on this PR: http.client.HTTPException subtypes
    (e.g. IncompleteRead, BadStatusLine — raised from the same resp.read()
    call, but NOT OSError subclasses) are a distinct uncovered exception
    shape in the same spot as the bare TimeoutError above."""
    import http.client

    monkeypatch.setenv("AW_WORKSPACE_API_KEY", "k")

    class TruncatedResponse:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self):
            raise http.client.IncompleteRead(b"partial")

    monkeypatch.setattr(kd.urllib.request, "urlopen", lambda *a, **kw: TruncatedResponse())
    try:
        kd.BoardClient().move("p1", "done")
    except kd.BoardUnavailable as exc:
        assert "did not respond in time" in str(exc)
    else:
        raise AssertionError("expected BoardUnavailable, not a bare IncompleteRead")


def test_ready_cards_unwraps_the_boards_envelope(monkeypatch):
    monkeypatch.setenv("AW_WORKSPACE_API_KEY", "k")

    class R:
        def read(self): return b'{"count":1,"cards":[{"page_id":"p1"}]}'
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(kd.urllib.request, "urlopen", lambda *a, **kw: R())
    assert kd.BoardClient().ready_cards() == [{"page_id": "p1"}]


def test_card_asks_for_the_body_and_comments(monkeypatch):
    """The whole reason get_kanban_card grew those flags — a dispatch prompt
    without them is just the card's title."""
    monkeypatch.setenv("AW_WORKSPACE_API_KEY", "k")
    seen = {}

    class R:
        def read(self): return b"{}"
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def capture(req, timeout=None):
        seen["url"] = req.full_url
        return R()

    monkeypatch.setattr(kd.urllib.request, "urlopen", capture)
    kd.BoardClient().card("p1")
    assert "include_body=true" in seen["url"]
    assert "include_comments=true" in seen["url"]


def test_add_comment_posts_to_the_comment_endpoint(monkeypatch):
    """Regression: kanban_check_deployed called board.add_comment() against
    a BoardClient that had no such method at all — AttributeError on the
    first live run (never caught by tests, since deploy_reconcile's own
    tests use a local FakeBoard double that DID implement it)."""
    monkeypatch.setenv("AW_WORKSPACE_API_KEY", "k")
    seen = {}

    class R:
        def read(self): return b"{}"
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def capture(req, timeout=None):
        seen["url"] = req.full_url
        seen["method"] = req.get_method()
        seen["body"] = json.loads(req.data.decode())
        return R()

    monkeypatch.setattr(kd.urllib.request, "urlopen", capture)
    kd.BoardClient().add_comment("p1", "hello")
    assert seen["url"].endswith("/api/apps/notion/kanban/comment")
    assert seen["method"] == "POST"
    assert seen["body"] == {"page_id": "p1", "text": "hello"}


def test_move_forwards_its_comment_in_the_same_request(monkeypatch):
    """aw-app-notion's move_card() posts the comment itself, as part of the
    SAME call that moves the card — it isn't a separate add_comment() call.
    move() must carry it through, or need_human escalations silently no-op
    (see move_card()'s own required-comment guard)."""
    monkeypatch.setenv("AW_WORKSPACE_API_KEY", "k")
    seen = {}

    class R:
        def read(self): return b'{"ok": true}'
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def capture(req, timeout=None):
        seen["body"] = json.loads(req.data.decode())
        return R()

    monkeypatch.setattr(kd.urllib.request, "urlopen", capture)
    kd.BoardClient().move("p1", "need_human", comment="explain the problem")
    assert seen["body"] == {"page_id": "p1", "status": "need_human",
                            "comment": "explain the problem"}


def test_a_board_refused_move_raises_boardunavailable_not_a_silent_noop(monkeypatch):
    """Live bug: move_card() returns {"ok": False, "error": "..."} (not an
    HTTP error, not an exception) when status='need_human' has no comment.
    Before this fix, move() returned that dict as-is with no inspection, so
    the caller (check_deployed()) treated a refused move as a successful
    one — reporting a card as moved to Need Human when its Status never
    actually changed."""
    monkeypatch.setenv("AW_WORKSPACE_API_KEY", "k")

    class R:
        def read(self):
            return json.dumps({
                "ok": False,
                "error": "status='need_human' requires a comment explaining the problem",
            }).encode()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(kd.urllib.request, "urlopen", lambda *a, **kw: R())
    try:
        kd.BoardClient().move("p1", "need_human")
    except kd.BoardUnavailable as exc:
        assert "requires a comment" in str(exc)
    else:
        raise AssertionError("expected BoardUnavailable, not a silently-accepted refusal")


def test_the_watchdog_addresses_the_board_over_loopback(monkeypatch):
    """The in-process watchdog runs INSIDE the workspace server, so the
    published URL sends its traffic out to the tunnel edge and back — and that
    edge cuts at ~30s, under this module's own 60s card-read timeout. A slow
    card read there dies at the edge, not at the timeout sized for it."""
    monkeypatch.delenv("AW_LOCAL_API_URL", raising=False)
    monkeypatch.setenv("AW_WORKSPACE_API_URL", "https://aw.example")
    monkeypatch.setenv("AW_PORT", "9030")
    assert kd.board_base_url(prefer_loopback=True) == "http://127.0.0.1:9030"
    assert kd.board_base_url() == "https://aw.example", "the MCP path is unchanged"


# ── the claim ───────────────────────────────────────────────────────────
#
# The guard the whole cut-over rests on. Two dispatchers read two different run
# databases, so "is a run already in flight?" is blind across the fence — the
# card is the only shared signal, and a write-then-read-back on it is the only
# way to make exactly one side win.

class FakeBoard:
    """A board where AgentRunId is a single last-write-wins cell, which is what
    Notion's page patch actually is."""

    def __init__(self, *, steal_with: str | None = None, cards=(), card=None):
        self.props: dict[str, str] = {}
        self.steal_with = steal_with
        self.moves: list[tuple[str, str]] = []
        self.writes: list[tuple[str, str]] = []
        self._cards = list(cards)
        self._card = card or {}

    def ready_cards(self, status="ready", limit=50):
        return self._cards

    def card(self, page_id, *, with_content=True):
        return dict(self._card)

    def move(self, page_id, status):
        self.moves.append((page_id, status))
        return {"ok": True}

    def set_property(self, page_id, name, value):
        self.writes.append((name, value))
        self.props[name] = value
        return {"ok": True}

    def properties(self, page_id, names=None):
        if self.steal_with is not None and self.writes:
            # The other side patched in between our write and our read-back.
            self.props[kd.RUN_ID_PROPERTY] = self.steal_with
            self.steal_with = None
        return dict(self.props)


def test_claim_won_returns_its_own_token():
    board = FakeBoard()
    token = kd.claim_card(board, "p1")
    assert token and token.startswith("mt:")
    assert board.props[kd.RUN_ID_PROPERTY] == token


def test_claim_lost_when_the_other_side_wrote_last():
    """Both sides patch, both read back, and the read returns ONE value — so
    the side that doesn't see its own token stands down. This is the case that
    a status check alone cannot catch: both GETs landed before either PATCH."""
    board = FakeBoard(steal_with="mono:someone-else")
    assert kd.claim_card(board, "p1") is None


def test_a_stale_read_fails_closed_rather_than_double_firing():
    """A read that returns something unrelated (empty, an old run id, a human's
    hand edit) makes us stand down. Both sides doing that leaves the card in
    Ready — visible on the board and reclaimed by the next tick — instead of
    two runs on one card."""
    assert kd.claim_card(FakeBoard(steal_with=""), "p1") is None
    assert kd.claim_card(FakeBoard(steal_with="run-from-yesterday"), "p1") is None


def test_the_claim_token_names_the_side_that_holds_it():
    """So a human looking at a card stuck mid-claim can tell who was holding
    the lease when it stopped."""
    assert kd.claim_card(FakeBoard(), "p1", side="mono").startswith("mono:")


# ── the sweep ───────────────────────────────────────────────────────────

class FakePlatform:
    def __init__(self, *, latest=None, run_id="run-1", error="", rejected=False):
        self._latest = latest
        self._run_id = run_id
        self._error = error
        self._rejected = rejected
        self.validated = []
        self.dispatched: list[tuple[str, dict]] = []

    async def latest_run(self, page_id):
        return self._latest

    async def validate_agent(self, slug, *, setting="AgentSlug"):
        self.validated.append((slug, setting))

    async def dispatch(self, path, payload):
        self.dispatched.append((path, payload))
        return ("", self._error, self._rejected) if self._error else (self._run_id, "", False)


def _sweep(board, platform, **kw):
    return asyncio.run(kd.sweep_ready(board, platform, **kw))


def _ready_summary(**over):
    base = {"page_id": "p1", "title": "Fix the thing", "status": "Ready",
            "agent_slug": "coder-sonnet", "target_slug": "some-target"}
    base.update(over)
    return base


def test_sweep_claims_moves_dispatches_and_stamps_in_that_order():
    """Move BEFORE dispatch is the monolith's order and it is deliberate: a
    card left in Ready after a good dispatch gets swept again and looks like a
    duplicate; one moved after a failed dispatch just sits there silently."""
    board = FakeBoard(cards=[_ready_summary()],
                      card={"page_id": "p1", "status": "Ready", "body_md": "do it"})
    platform = FakePlatform()
    out = _sweep(board, platform)

    assert out["dispatched"] == 1
    assert board.moves == [("p1", "running")]
    assert platform.dispatched[0][0] == "/api/agents/coder-sonnet/run"
    # First write is the claim token, last is the real run id.
    assert board.writes[0][1].startswith("mt:")
    assert board.writes[-1] == (kd.RUN_ID_PROPERTY, "run-1")


def test_sweep_stands_down_when_the_claim_is_lost():
    """Nothing is moved and nothing is dispatched — the other side won it."""
    board = FakeBoard(steal_with="mono:x", cards=[_ready_summary()],
                      card={"page_id": "p1", "status": "Ready"})
    platform = FakePlatform()
    out = _sweep(board, platform)

    assert out["dispatched"] == 0
    assert out["results"][0]["skipped"] == "claim-lost"
    assert board.moves == []
    assert platform.dispatched == []


def test_sweep_skips_a_card_with_an_in_flight_run_before_writing_anything():
    board = FakeBoard(cards=[_ready_summary()], card={"page_id": "p1", "status": "Ready"})
    out = _sweep(board, FakePlatform(latest={"id": "r-9", "status": "running"}))

    assert out["results"][0]["skipped"] == "run-already-in-flight"
    assert board.writes == []


def test_sweep_skips_a_card_the_other_side_already_moved():
    """The re-read is what catches the monolith winning the race between our
    list and our claim: the card is no longer Ready, so it isn't ours."""
    board = FakeBoard(cards=[_ready_summary()],
                      card={"page_id": "p1", "status": "In Progress"})
    out = _sweep(board, FakePlatform())

    assert out["results"][0]["skipped"] == "no-longer-ready"
    assert board.writes == []


def test_sweep_without_explicit_agent_dispatches_configured_coder():
    board = FakeBoard(cards=[_ready_summary(agent_slug=None)],
                      card={"page_id": "p1", "status": "Ready", "agent_slug": None})
    platform = FakePlatform()
    out = _sweep(board, platform, read_coder_setting=lambda: "  implementation  ")
    assert out["results"][0]["run_id"] == "run-1"
    assert platform.validated == [("implementation", "default_agent_coder")]
    path, payload = platform.dispatched[0]
    assert path == "/api/agents/implementation/run"
    assert payload["workspace"] == kd._this_workspace()
    assert payload["notion_task_id"] == "p1"
    assert payload["target_slug"] == "some-target"


def test_sweep_never_dispatches_a_qa_findings_card():
    for tags in (["qa-findings"], "bug, QA-Findings"):
        board = FakeBoard(cards=[_ready_summary()],
                          card={"page_id": "p1", "status": "Ready", "tags": tags})
        platform = FakePlatform()
        out = _sweep(board, platform)
        assert out["results"][0]["skipped"] == "qa-findings-card"
        assert platform.dispatched == [] and board.moves == []


def test_dry_run_writes_nothing_at_all():
    board = FakeBoard(cards=[_ready_summary()],
                      card={"page_id": "p1", "status": "Ready"})
    out = _sweep(board, FakePlatform(), dry_run=True)

    assert out["results"][0]["would_dispatch"] == "/api/agents/coder-sonnet/run"
    assert board.writes == [] and board.moves == []


def test_one_bad_card_does_not_end_the_pass():
    """try/except per card. A board where one page 404s still dispatches the
    rest — the sweep is the only trigger once the webhook is off."""
    class Flaky(FakeBoard):
        def card(self, page_id, *, with_content=True):
            if page_id == "bad":
                raise kd.BoardUnavailable("aw-app-notion returned 404")
            return {"page_id": page_id, "status": "Ready"}

    board = Flaky(cards=[_ready_summary(page_id="bad"), _ready_summary(page_id="good")])
    out = _sweep(board, FakePlatform())

    assert "404" in out["results"][0]["error"]
    assert out["results"][1]["run_id"] == "run-1"
    assert out["dispatched"] == 1


def test_a_failed_dispatch_is_reported_not_swallowed():
    board = FakeBoard(cards=[_ready_summary()], card={"page_id": "p1", "status": "Ready"})
    out = _sweep(board, FakePlatform(error="dispatch failed: 500 boom"))

    assert out["dispatched"] == 0
    assert "500" in out["results"][0]["error"]


def test_the_prompt_still_gets_the_body_the_summary_lacks():
    """`list_cards` returns no body/comments; `card()` does. The merge is why
    the dispatched agent sees the task instead of just its title."""
    board = FakeBoard(cards=[_ready_summary()],
                      card={"page_id": "p1", "status": "Ready",
                            "body_md": "The button does nothing on mobile.",
                            "comments_md": "- tried a CSS fix"})
    platform = FakePlatform()
    _sweep(board, platform)

    sent = platform.dispatched[0][1]["input"]["input"]
    assert "does nothing on mobile" in sent and "tried a CSS fix" in sent

@pytest.mark.parametrize("value", [None, "", "   "])
def test_default_coder_reset(value):
    assert kd.coder_setting(value) == "coder"


def test_invalid_setting_leaves_card_ready():
    board = FakeBoard(cards=[_ready_summary(agent_slug=None)], card={"status": "Ready"})
    platform = FakePlatform()
    outcome = _sweep(board, platform, read_coder_setting=lambda: "invalid/slug")
    assert "default_agent_coder" in outcome["results"][0]["error"]
    assert not board.moves and not board.writes and not platform.dispatched


def test_explicit_selection_does_not_fetch_default():
    def fail():
        raise AssertionError("explicit card should not read the workspace default")

    board = FakeBoard(cards=[_ready_summary()], card={"status": "Ready"})
    assert _sweep(board, FakePlatform(), read_coder_setting=fail)["dispatched"] == 1


def test_definitive_rejection_restores_owned_claim_and_status():
    board = FakeBoard(cards=[_ready_summary()], card={"status": "Ready"})
    board.props[kd.RUN_ID_PROPERTY] = "previous-run"
    outcome = _sweep(board, FakePlatform(error="dispatch failed: 404", rejected=True))
    assert outcome["dispatched"] == 0
    assert board.props[kd.RUN_ID_PROPERTY] == "previous-run"
    assert board.moves == [("p1", "running"), ("p1", "ready")]


@pytest.mark.parametrize("agent,status,error", [
    ({"slug": "variant", "workspace": "other"}, 200, "belongs to workspace"),
    ({"slug": "variant", "deleted_at": "yesterday"}, 200, "active agent"),
    ({}, 404, "HTTP 404"),
])
def test_default_coder_agent_validation(monkeypatch, agent, status, error):
    monkeypatch.setenv("AW_WORKSPACE", "mine")

    async def resolve():
        transport = httpx.MockTransport(lambda request: httpx.Response(status, json=agent))
        async with httpx.AsyncClient(transport=transport) as client:
            return await kd.resolve_default_coder(
                kd.PlatformClient(client, "https://agents.test"), lambda: "variant")

    with pytest.raises(kd.CoderDispatchError, match=error) as failure:
        asyncio.run(resolve())
    assert "default_agent_coder='variant' in workspace 'mine'" in str(failure.value)


def test_concurrent_sweeps_only_launch_once():
    class SharedBoard(FakeBoard):
        def move(self, page_id, status):
            self._card["status"] = "In Progress" if status == "running" else "Ready"
            return super().move(page_id, status)

    class SlowPlatform(FakePlatform):
        async def dispatch(self, path, payload):
            await asyncio.sleep(0.02)
            return await super().dispatch(path, payload)

    board = SharedBoard(cards=[_ready_summary()], card={"status": "Ready"})
    platform = SlowPlatform()

    async def race():
        return await asyncio.gather(
            kd.sweep_ready(board, platform),
            kd.sweep_ready(board, platform, selected_card=_ready_summary()),
        )

    assert sum(result["dispatched"] for result in asyncio.run(race())) == 1
    assert len(platform.dispatched) == 1


class HandoffBoard(FakeBoard):
    def __init__(self, **card):
        super().__init__(card=_card(**{"status": "In Progress", **card}), cards=[_ready_summary()])
        self.comments = []

    def add_comment(self, page_id, text):
        self.comments.append(text)

    def default_coder(self):
        return "coder"


class HandoffPlatform(FakePlatform):
    def __init__(self, source_slug="architect", *, ambiguous=False):
        self.runs = [{"id": "source", "source_slug": source_slug, "workspace": "aw",
                      "target_slug": "some-target", "status": "running"}]
        self.ambiguous = ambiguous
        super().__init__(latest=self.runs[0])

    async def card_runs(self, page_id, *, limit):
        return list(self.runs)

    async def dispatch(self, path, payload):
        await super().dispatch(path, payload)
        self.runs.insert(0, {"id": "run-1", "source_slug": path.split("/")[3],
                             "workspace": payload["workspace"], "status": "pending",
                             "parent_run_id": payload["caller_run_id"],
                             "target_slug": payload["target_slug"], "input": payload["input"]})
        return ("", "timeout", False) if self.ambiguous else ("run-1", "", False)


def _handoff(board, platform, *, revision="abc123"):
    return kd.handoff_card(board, platform, source_id="source", page_id="page-1",
                           revision=revision, evidence="CI omits the mobile browser specs. "
                           "Inherited VPN 404s are tracked separately.")


def test_mobile_rework_reconciles_timeout_and_replays_against_sweeper():
    async def scenario():
        board = HandoffBoard(agent_slug="architect")
        platform = HandoffPlatform(ambiguous=True)
        accepted, sweep, competing = await asyncio.gather(
            _handoff(board, platform), kd.sweep_ready(board, platform), _handoff(board, platform))
        assert competing["reason"] == "workspace-dispatch-in-progress"
        assert sweep["dispatched"] == 0
        assert accepted["run_id"] == "run-1"
        assert accepted["target_slug"] == "some-target"
        assert accepted["revision"] == "abc123"
        assert "CI omits" in board.comments[0]
        assert "Inherited VPN" in board.comments[0]
        assert board.props[kd.RUN_ID_PROPERTY] == "run-1"
        assert not board.moves

        replay = await _handoff(board, platform)
        assert replay["reused"] and replay["run_id"] == accepted["run_id"]
        assert len(platform.dispatched) == 1
        assert len(board.comments) == 2

    asyncio.run(scenario())


def test_coder_uses_direct_qa_dispatch_instead_of_board_handoff():
    board = HandoffBoard()
    platform = HandoffPlatform("coder")
    with pytest.raises(ValueError, match="Only the card's Architect"):
        asyncio.run(_handoff(board, platform))
    assert not platform.dispatched
    assert not board.writes


def test_architect_handoff_resolves_default_coder():
    board = HandoffBoard(agent_slug="architect")
    platform = HandoffPlatform("architect")
    receipt = asyncio.run(_handoff(board, platform, revision="design:1"))
    assert receipt["recipient"] == "coder"
    assert receipt["run_id"] == "run-1"
    assert platform.dispatched[0][0] == "/api/agents/coder/run"


def test_handoff_keeps_concurrent_recipient_gate():
    board = HandoffBoard()
    platform = HandoffPlatform()
    platform.runs.append({"id": "other", "status": "queued"})
    assert asyncio.run(_handoff(board, platform))["reason"] == "another-card-run-active"
    assert not platform.dispatched
    assert not board.writes



def test_qa_cannot_hand_a_card_back_to_a_coder():
    board = HandoffBoard()
    platform = HandoffPlatform("qa-sonnet")
    with pytest.raises(ValueError, match="report_qa_findings"):
        asyncio.run(_handoff(board, platform))
    assert not platform.dispatched
    assert not board.writes



# ── report_qa_findings: QA closes the card, never hands it back ─────────

class QABoard(FakeBoard):
    def __init__(self):
        super().__init__(card={"status": "Testing", "target_slug": "some-target",
                               "comments_md": ""})
        self.comments = []
        self.filed = []

    def move(self, page_id, status, comment=""):
        self.comments.append(comment)
        self._card["comments_md"] += comment
        return super().move(page_id, status)

    def qa_findings(self, **kw):
        self.filed.append(kw)
        return {"ok": True, "findings_url": "https://notion.so/findings-1",
                "findings_page_id": "findings-1", "added": len(kw["findings"])}


class QAPlatform(HandoffPlatform):
    def __init__(self, source_slug="qa-sonnet"):
        super().__init__(source_slug)
        self.woken = []

    async def return_to_caller(self, run_id, message):
        self.woken.append((run_id, message))
        return {"ok": True}


ISSUE = {"title": "POST /x returns 500", "severity": "major",
         "evidence": "curl -X POST /x -> 500, expected 201"}


def _report(board, platform, **over):
    kw = dict(source_id="source", page_id="page-1", verdict="issues", revision="abc123",
              summary="exercised the API", findings=[ISSUE])
    kw.update(over)
    return asyncio.run(kd.report_qa_findings(board, platform, **kw))


def test_issues_file_the_finding_close_the_card_and_wake_the_caller_without_dispatching():
    board, platform = QABoard(), QAPlatform()
    receipt = _report(board, platform)
    assert receipt["card_status"] == "done" and receipt["findings_card"].endswith("findings-1")
    assert board.filed[0]["target_slug"] == "some-target"
    assert board.filed[0]["findings"] == [ISSUE]
    assert board.props["QAStatus"] == "issues"
    assert board.moves == [("page-1", "done")]
    assert "POST /x returns 500" in board.comments[0]
    assert not platform.dispatched, "QA must never dispatch a Coder"
    assert platform.woken and "POST /x returns 500" in platform.woken[0][1]


def test_pass_closes_the_card_without_touching_the_findings_card():
    board, platform = QABoard(), QAPlatform()
    receipt = _report(board, platform, verdict="passed", findings=[], pass_status="ready_to_deploy")
    assert receipt["card_status"] == "ready_to_deploy"
    assert board.props["QAStatus"] == "ready_to_deploy"
    assert not board.filed


def test_a_replay_of_the_same_verdict_files_nothing_twice():
    board, platform = QABoard(), QAPlatform()
    _report(board, platform)
    again = _report(board, platform)
    assert again["reused"] is True
    assert len(board.filed) == 1 and len(board.comments) == 1


def test_issues_without_runtime_evidence_are_refused():
    board, platform = QABoard(), QAPlatform()
    with pytest.raises(ValueError, match="could not reproduce"):
        _report(board, platform, findings=[{"title": "looks risky", "evidence": ""}])
    assert not board.filed and not board.moves


def test_only_a_qa_run_can_report_findings():
    board, platform = QABoard(), QAPlatform("coder")
    with pytest.raises(ValueError, match="Only the card's QA run"):
        _report(board, platform)
    assert not board.filed and not board.moves


def test_a_failed_wake_does_not_undo_the_recorded_verdict():
    class Broken(QAPlatform):
        async def return_to_caller(self, run_id, message):
            raise RuntimeError("platform down")

    board = QABoard()
    receipt = _report(board, Broken())
    assert receipt["ok"] is True and receipt["wake"]["ok"] is False
    assert board.moves == [("page-1", "done")]


def test_partial_report_does_not_swallow_a_later_reproduced_failure_on_same_revision():
    board, platform = QABoard(), QAPlatform()
    partial = {"title": "Not verified: UI login", "severity": "minor",
               "evidence": "browser session got 401; needs an authenticated session"}
    _report(board, platform, findings=[partial])
    later = _report(board, platform)
    assert not later.get("reused")
    assert len(board.filed) == 2 and board.filed[1]["findings"] == [ISSUE]


# ── attention sweep: one driver prompt per card ─────────────────────────

class _AttnBoard:
    def __init__(self, cards, bodies=None):
        self._cards, self._bodies = cards, bodies or {}

    def need_attention(self, *, limit, reping):
        self.asked = (limit, reping)
        return {"has_work": bool(self._cards), "cards": self._cards}

    def card(self, page_id, **_):
        return self._bodies.get(page_id, {"page_id": page_id, "body_md": "body", "comments_md": ""})


class _AttnPlatform:
    def __init__(self, active=(), questions=None, questions_error=None):
        self.active, self.dispatched, self.validated = set(active), [], []
        self.questions, self.questions_error = questions or {}, questions_error

    async def pending_questions(self):
        if self.questions_error:
            raise RuntimeError(self.questions_error)
        return self.questions

    async def validate_agent(self, slug, **_):
        self.validated.append(slug)

    async def latest_run(self, page_id):
        return {"id": "r0", "status": "running"} if page_id in self.active else None

    async def dispatch(self, path, payload):
        self.dispatched.append((path, payload))
        return f"run-{len(self.dispatched)}", "", False


def test_attention_sweep_fires_one_po_run_per_card_with_its_own_prompt():
    cards = [{"page_id": "a", "title": "A", "status": "In Progress", "agent_slug": "coder"},
             {"page_id": "b", "title": "B", "status": "Need Human"}]
    plat = _AttnPlatform()
    out = asyncio.run(kd.sweep_attention(_AttnBoard(cards), plat))
    assert out["dispatched"] == 2 and out["considered"] == 2
    assert plat.validated == ["project-manager"]
    paths = sorted(p for p, _ in plat.dispatched)
    # the card's own agent_slug (coder) must NOT win: the PM drives every card
    assert paths == ["/api/agents/project-manager/run"] * 2
    by_card = {pl["notion_task_id"]: pl["input"]["input"] for _, pl in plat.dispatched}
    assert set(by_card) == {"a", "b"}
    assert "page_id=a" in by_card["a"] and "page_id=b" not in by_card["a"]
    assert "Project Manager driving this card" in by_card["a"]
    assert "mark_flow_done" in by_card["a"] and "Never move a card to Planned" in by_card["a"]


def test_attention_sweep_skips_a_card_with_a_run_in_flight_and_survives_one_failure():
    class Boom(_AttnBoard):
        def card(self, page_id, **kw):
            if page_id == "bad":
                raise RuntimeError("kaboom")
            return super().card(page_id, **kw)

    cards = [{"page_id": "busy"}, {"page_id": "bad"}, {"page_id": "ok"}]
    plat = _AttnPlatform(active={"busy"})
    out = asyncio.run(kd.sweep_attention(Boom(cards), plat))
    rows = {r["page_id"]: r for r in out["results"]}
    assert rows["busy"]["skipped"] == "run-already-in-flight"
    assert "kaboom" in rows["bad"]["error"]
    assert rows["ok"]["run_id"] and out["dispatched"] == 1


def test_attention_sweep_with_no_work_dispatches_nothing_and_validates_nothing():
    plat = _AttnPlatform()
    out = asyncio.run(kd.sweep_attention(_AttnBoard([]), plat))
    assert out == {"has_work": False, "considered": 0, "dispatched": 0,
                   "dry_run": False, "results": []}
    assert plat.dispatched == [] and plat.validated == []


def test_attention_sweep_dry_run_dispatches_nothing():
    plat = _AttnPlatform()
    out = asyncio.run(kd.sweep_attention(_AttnBoard([{"page_id": "a"}]), plat, dry_run=True))
    assert plat.dispatched == [] and out["results"][0]["would_dispatch"] == "/api/agents/project-manager/run"


# ── project-manager driver: parked skip + ledger annotation ─────────────

def test_driver_is_the_project_manager_not_the_product_owner():
    assert kd.ATTENTION_DRIVER_AGENT == "project-manager"
    assert "finish with a terminal action (mark_flow_done" not in kd._DRIVER_BRIEF


@pytest.mark.parametrize("block,detail", [
    ("routed-human|1|2026-11-27T10:00:00Z|approve the spend", "approve the spend"),
    ("routed-human|1|2026-11-27T10:00:00Z|a|b pipe", "a|b pipe"),
    ("k|escalated|2026-11-27T10:00:00Z|why", "why"),
    ("routed-human|1|only-three", None),
    ("", None), (None, None), ("garbage", None),
    ("coder|2|2026-11-27T10:00:00Z|x", None),
])
def test_human_judged_block_matches_the_notion_rule(block, detail):
    assert kd.human_judged_block(block) == detail


def test_attention_sweep_skips_a_parked_need_human_card_without_a_run():
    parked = {"page_id": "p", "status": "Need Human",
              "sweep_block": "routed-human|1|2026-11-27T10:00:00Z|approve spend"}
    # same block on a card that is NOT Need Human must not park it
    moved = {"page_id": "m", "status": "In Progress", "sweep_block": parked["sweep_block"]}
    plat = _AttnPlatform()
    out = asyncio.run(kd.sweep_attention(_AttnBoard([parked, moved]), plat))
    rows = {r["page_id"]: r for r in out["results"]}
    assert rows["p"]["skipped"] == "parked-on-human" and rows["p"]["detail"] == "approve spend"
    assert rows["m"]["run_id"]
    assert [pl["notion_task_id"] for _, pl in plat.dispatched] == ["m"]


def _q(qid="q1", agent="architect", text="approve the spend?"):
    return {"id": qid, "agent_slug": agent, "question": text}


def test_need_human_card_with_a_pending_ask_human_is_parked_without_a_run():
    card = {"page_id": "ab-cd", "status": "Need Human"}  # no SweepBlock at all
    plat = _AttnPlatform(questions={"abcd": _q(text="x" * 300)})
    out = asyncio.run(kd.sweep_attention(_AttnBoard([card]), plat))
    row = out["results"][0]
    assert row["skipped"] == "parked-on-human"
    assert row["detail"] == "ask_human q1 by architect: " + "x" * 200
    assert plat.dispatched == []
    assert "human_questions" not in out


def test_pending_ask_human_does_not_park_a_card_outside_need_human():
    card = {"page_id": "abcd", "status": "In Progress"}
    plat = _AttnPlatform(questions={"abcd": _q()})
    out = asyncio.run(kd.sweep_attention(_AttnBoard([card]), plat))
    assert out["results"][0]["run_id"] and len(plat.dispatched) == 1


def test_question_lookup_failure_fails_open_and_is_reported():
    card = {"page_id": "abcd", "status": "Need Human"}
    plat = _AttnPlatform(questions_error="HTTP 503")
    out = asyncio.run(kd.sweep_attention(_AttnBoard([card]), plat))
    assert out["human_questions"] == "HTTP 503"
    assert out["results"][0]["run_id"] and len(plat.dispatched) == 1


def test_question_matches_regardless_of_dashes_on_either_side():
    dashed = "4255bf3b-9510-817c-9a01-eb9e3bf9d313"
    plain = dashed.replace("-", "")
    for card_id, key in ((dashed, plain), (plain, plain)):
        plat = _AttnPlatform(questions={key: _q()})
        out = asyncio.run(kd.sweep_attention(
            _AttnBoard([{"page_id": card_id, "status": "Need Human"}]), plat))
        assert out["results"][0]["skipped"] == "parked-on-human"


def test_pending_questions_client_keys_dashless_and_keeps_the_newest():
    class _Resp:
        status_code = 200
        def json(self):
            return [{"id": "new", "notion_task_id": "ab-cd"},
                    {"id": "old", "notion_task_id": "abcd"},
                    {"id": "none", "notion_task_id": None}]

    class _C:
        async def get(self, url, params=None):
            self.seen = (url, params)
            return _Resp()

    c = _C()
    got = asyncio.run(kd.PlatformClient(c, "http://ap/").pending_questions())
    assert got["abcd"]["id"] == "new" and list(got) == ["abcd"]
    assert c.seen == ("http://ap/api/human-questions",
                      {"status": "pending", "kind": "ask_human", "limit": 500})


class _PlanPlatform(_AttnPlatform):
    def __init__(self, candidates, *, running=True, annotate_error=None, **kw):
        super().__init__(**kw)
        self.candidates, self.running, self.annotate_error = candidates, running, annotate_error
        self.writes = []

    async def running_iteration(self, slug):
        assert slug == "kanban-sweep"
        return ("it-1", self.candidates) if self.running else None

    async def annotate(self, slug, iteration_id, items):
        if self.annotate_error:
            raise RuntimeError(self.annotate_error)
        self.writes.append((slug, iteration_id, items))


def _sweep_with_plan(cards, plat, **kw):
    return asyncio.run(kd.sweep_attention(_AttnBoard(cards), plat, plan_slug="kanban-sweep", **kw))


def test_annotates_every_gate_candidate_on_every_branch():
    cards = [
        {"page_id": "ok", "reason": "idle 50h"},
        {"page_id": "busy"},
        {"page_id": "parked", "status": "Need Human",
         "sweep_block": "routed-human|1|2026-11-27T10:00:00Z|approve spend"},
        {"page_id": "qa", "tags": ["qa-findings"]},
        {"page_id": "bad"},
    ]

    class Plat(_PlanPlatform):
        async def dispatch(self, path, payload):
            if payload["notion_task_id"] == "bad":
                return "", "HTTP 500", False
            return await super().dispatch(path, payload)

    plat = Plat(["ok", "busy", "parked", "qa", "bad", "outside"], active={"busy"})
    out = _sweep_with_plan(cards, plat)
    assert out["annotation"] == {"annotated": 6, "iteration_id": "it-1"}
    (slug, it, items), = plat.writes
    assert (slug, it) == ("kanban-sweep", "it-1")
    by = {i["card"]: i for i in items}
    assert set(by) == {"ok", "busy", "parked", "qa", "bad", "outside"}
    assert by["ok"]["action"] == "dispatched-project-manager"
    assert "idle 50h" in by["ok"]["why"] and "run-" in by["ok"]["why"]
    assert by["busy"] == {"card": "busy", "action": "run-already-in-flight", "why": "r0"}
    assert by["parked"]["action"] == "parked-on-human" and by["parked"]["why"] == "approve spend"
    assert by["qa"]["action"] == "qa-findings-card" and by["qa"]["why"]
    assert by["bad"]["action"] == "dispatch-failed" and "HTTP 500" in by["bad"]["why"]
    assert by["outside"]["action"] == "not-in-dispatch-window"
    assert all(i["action"].strip() and i["why"].strip() for i in items)


def test_no_running_iteration_means_no_annotation_and_the_dispatches_still_happen():
    plat = _PlanPlatform(["a"], running=False)
    out = _sweep_with_plan([{"page_id": "a"}], plat)
    assert out["dispatched"] == 1 and plat.writes == []
    assert out["annotation"] == {"annotated": 0, "reason": "no-running-iteration"}


def test_an_annotation_failure_never_fails_the_dispatches():
    plat = _PlanPlatform(["a"], annotate_error="HTTP 403")
    out = _sweep_with_plan([{"page_id": "a"}], plat)
    assert out["dispatched"] == 1
    assert out["annotation"]["annotated"] == 0 and "403" in out["annotation"]["error"]


def test_no_plan_slug_or_dry_run_writes_nothing():
    plat = _PlanPlatform(["a"])
    asyncio.run(kd.sweep_attention(_AttnBoard([{"page_id": "a"}]), plat))
    _sweep_with_plan([{"page_id": "a"}], plat, dry_run=True)
    assert plat.writes == []


def test_slow_pass_writes_a_provisional_annotation_first_then_overwrites(monkeypatch):
    monkeypatch.setattr(kd, "ATTENTION_PROVISIONAL_AFTER_S", 0.01)

    class Slow(_PlanPlatform):
        async def dispatch(self, path, payload):
            await asyncio.sleep(0.1)
            return await super().dispatch(path, payload)

    plat = Slow(["a"])
    out = _sweep_with_plan([{"page_id": "a"}], plat)
    assert [w[2][0]["action"] for w in plat.writes] == ["dispatch-pending", "dispatched-project-manager"]
    assert out["annotation"]["annotated"] == 1


@pytest.mark.parametrize("stamped", ["project-manager", "product-owner"])
def test_handoff_never_builds_with_a_pm_or_po_stamped_on_the_card(stamped):
    board = HandoffBoard(agent_slug=stamped)
    platform = HandoffPlatform("architect")
    receipt = asyncio.run(_handoff(board, platform, revision="design:1"))
    assert receipt["recipient"] == "coder"
    assert platform.dispatched[0][0] == "/api/agents/coder/run"


def test_provisional_deadline_counts_from_the_start_of_the_request(monkeypatch):
    """Setup (iteration read) eats most of the budget, so the provisional write
    must land after the REMAINING time, not a fresh 20s."""
    monkeypatch.setattr(kd, "ATTENTION_PROVISIONAL_AFTER_S", 0.15)

    class Plat(_PlanPlatform):
        async def running_iteration(self, slug):
            await asyncio.sleep(0.12)
            return await super().running_iteration(slug)

        async def dispatch(self, path, payload):
            await asyncio.sleep(0.2)
            return await super().dispatch(path, payload)

    plat = Plat(["a"])
    times = []
    orig = plat.annotate

    async def stamped(slug, it, items):
        times.append(asyncio.get_running_loop().time())
        await orig(slug, it, items)

    plat.annotate = stamped
    asyncio.run(kd.sweep_attention(_AttnBoard([{"page_id": "a"}]), plat, plan_slug="kanban-sweep"))
    assert [w[2][0]["action"] for w in plat.writes][0] == "dispatch-pending"
    # provisional fired ~0.15s after start (not 0.12 + 0.15), i.e. before dispatch ended at ~0.32
    assert times[1] - times[0] > 0.1


def test_ledger_action_names_the_agent_that_was_dispatched():
    plat = _PlanPlatform(["a"])
    asyncio.run(kd.sweep_attention(_AttnBoard([{"page_id": "a"}]), plat, plan_slug="kanban-sweep",
                                   agent_slug="product-owner"))
    assert plat.writes[0][2][0]["action"] == "dispatched-product-owner"
