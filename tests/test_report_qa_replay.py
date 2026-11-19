from uuid import UUID

import pytest

from scripts.report_qa_replay import build_report


def test_report_qa_replay_keeps_verdicts_and_excludes_incomplete_usage():
    rows = {}
    manifest = []
    for number, input_tokens in enumerate([100, 200, 0], start=1):
        run_id = f"{number:032x}"
        helper_id = str(UUID(int=number + 10))
        for row_id, tokens in [(run_id, input_tokens), (helper_id, 10)]:
            rows[row_id] = {
                "id": row_id, "kind": "agent", "status": "success",
                "model_slug": "test-model", "tokens_in": tokens, "tokens_out": 5,
            }
        manifest.append({
            "case": "cardless", "variant": "baseline", "revision": "fixture-v1",
            "route": "cardless_a2a", "card_id": None, "run_id": run_id,
            "linked_run_ids": [helper_id], "verdict": "pass",
        })
    report = build_report(manifest, rows.__getitem__)
    assert report["slices"][0]["median_combined_input_tokens"] == 160
    assert report["slices"][0]["samples_with_input_counters"] == 2
    assert report["slices"][0]["samples"] == 3
    assert report["slices"][0]["verdicts"] == ["pass", "pass", "pass"]
    assert report["samples"][0]["linked"][0]["tokens_out"] == 5
    assert report["samples"][2]["combined_input_tokens"] is None

    manifest[2]["linked_run_ids"] = manifest[0]["linked_run_ids"]
    with pytest.raises(ValueError, match="Run counted twice"):
        build_report(manifest, rows.__getitem__)

    rows[manifest[0]["linked_run_ids"][0]]["kind"] = "workflow"
    with pytest.raises(ValueError, match="Workflow rollup"):
        build_report(manifest, rows.__getitem__)


def test_platform_url_uses_workspace_env_when_config_omits_it(monkeypatch):
    from scripts.report_qa_replay import platform_url

    monkeypatch.setenv("AW_AGENTS_PLATFORM_BASE", "https://ap.example/")
    assert platform_url({"agents_platform_token": "t"}) == "https://ap.example"
    assert platform_url({"agents_platform_base": "https://explicit.example/"}) == "https://explicit.example"


def test_platform_url_refuses_the_shared_public_default(monkeypatch):
    from agents_platform_runners_app import platform_base
    from scripts.report_qa_replay import platform_url

    monkeypatch.delenv("AW_AGENTS_PLATFORM_BASE", raising=False)
    monkeypatch.setattr(platform_base, "workspace_env", lambda name: "")
    with pytest.raises(SystemExit, match="No platform URL"):
        platform_url({"agents_platform_token": "t"})


def test_documented_manifest_example_is_a_json_array_that_builds():
    import json
    import pathlib
    import re

    doc = pathlib.Path(__file__).resolve().parents[1].joinpath("docs/qa-replay.md").read_text()
    example = json.loads(re.search(r"```json\n(.*?)```", doc, re.S).group(1))
    assert isinstance(example, list) and example[0]["run_id"]
