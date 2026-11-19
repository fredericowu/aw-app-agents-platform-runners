import asyncio
import json

import httpx
import pytest

from agents_platform_runners_app import mcp_server, qa_evidence


@pytest.fixture
def evidence():
    return {
        "version": 1,
        "revision": {"repo": "owner/repo", "pr": 7, "merged_sha": "a" * 40},
        "scope": {"summary": "Review the existing output"},
        "checks": [{"id": "1", "text": "Run the smoke check", "result": {"observed": "exit 0"}}],
        "evidence_refs": ["https://example.com/log", "artifact:qa/proof.png"],
        "known_gaps": [],
    }


@pytest.fixture
def outbound(monkeypatch):
    calls = []

    def handler(request):
        calls.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"run_id": "test-run"})

    client = httpx.AsyncClient
    monkeypatch.setattr(
        mcp_server.httpx, "AsyncClient",
        lambda **kwargs: client(**{**kwargs, "transport": httpx.MockTransport(handler)}),
    )
    monkeypatch.setattr(mcp_server, "_caller_run_id", lambda _: "caller")
    return calls


def _dispatch(**kwargs):
    return json.loads(asyncio.run(mcp_server._call_tool(
        "run_agent_async", {"target_slug": "test-target", **kwargs},
    ))[0].text)


def test_structured_dispatch_uses_same_block_and_preserves_routing(monkeypatch, evidence, outbound):
    checked = []

    async def verify(revision):
        checked.append(revision)

    monkeypatch.setattr(qa_evidence, "_verify_merged_revision", verify)
    assert _dispatch(slug="qa", qa_evidence=evidence, input="duplicated prose")["run_id"]
    assert _dispatch(
        group_slug="qas", qa_evidence=evidence, notion_task_id="fixture-card",
        call_me_back=False, qa_rerun=True,
    )["run_id"]
    direct, group = outbound
    assert direct[0] == "/api/agents/qa/run"
    assert group[0] == "/api/agent-groups/qas/run"
    assert direct[1]["input"] == group[1]["input"]
    payload = direct[1]["input"]["input"]
    assert json.loads(payload.split("\n", 1)[1]) == evidence
    assert "duplicated prose" not in payload
    assert "notion_task_id" not in direct[1]
    assert group[1]["notion_task_id"] == "fixture-card"
    assert group[1]["qa_rerun"] is True
    assert direct[1]["call_me_back"] is True and group[1]["call_me_back"] is False
    assert direct[1]["caller_run_id"] == group[1]["caller_run_id"] == "caller"
    assert checked == [evidence["revision"]] * 2


def test_invalid_evidence_is_rejected_before_dispatch(evidence, outbound):
    evidence["evidence_refs"] = ["file:///private/file"]
    assert _dispatch(slug="qa", qa_evidence=evidence)["status"] == 400
    assert _dispatch(slug="qa", qa_evidence=None)["status"] == 400
    assert _dispatch(slug="coder", qa_evidence=evidence)["status"] == 400
    assert outbound == []


def test_pre_merge_evidence_fails_closed(monkeypatch, evidence, outbound):
    class PendingPR:
        returncode = 0

        async def communicate(self):
            return b'{"state":"OPEN","mergeCommit":null}', None

    async def github(*args, **kwargs):
        assert args[:3] == ("gh", "pr", "view")
        return PendingPR()

    monkeypatch.setattr(qa_evidence.asyncio, "create_subprocess_exec", github)
    result = _dispatch(slug="qa", qa_evidence=evidence)
    assert result["status"] == 400
    assert "must be merged" in result["message"]
    assert outbound == []


def test_legacy_text_still_dispatches(outbound):
    assert _dispatch(slug="qa", input="legacy")["run_id"]
    assert outbound[0][1]["input"] == {"input": "legacy"}


def test_deploy_followup_uses_renderer_and_retains_rerun(monkeypatch, evidence, outbound):
    evidence["revision"] = {"artifact_revision": "release-v1"}
    row = {
        "page_id": "fixture-deploy-card", "target_slug": "test-target",
        "qa_dispatch_needed": True, "qa_evidence": evidence,
    }
    stamps = []
    monkeypatch.setattr(mcp_server.deploy_reconcile, "mark_qa_dispatched",
                        lambda _, page_id: stamps.append(page_id))

    async def run():
        async with httpx.AsyncClient() as client:
            await mcp_server._deploy_followups(client, None, {"moved": [row]})

    asyncio.run(run())
    assert stamps == ["fixture-deploy-card"]
    assert outbound[0][0] == "/api/agent-groups/qas/run"
    assert outbound[0][1]["qa_rerun"] is True
    assert outbound[0][1]["input"]["input"] == asyncio.run(qa_evidence.render_qa_evidence(evidence))
