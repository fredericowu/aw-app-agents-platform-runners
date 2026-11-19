import pytest

from scripts.report_qa_replay import build_report


def test_report_qa_replay_keeps_verdicts_and_excludes_incomplete_usage():
    rows = {}
    manifest = []
    for number, input_tokens in enumerate([100, 200, 0], start=1):
        run_id = f"{number:032x}"
        helper_id = f"{number + 10:032x}"
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
