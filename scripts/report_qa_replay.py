"""Read persisted QA replay counters using explicit run IDs and fixture verdicts.

The manifest is a JSON array of observations. Each observation has case,
variant, revision, route, review_type, card_id (null for cardless), run_id,
verdict, and optional linked_run_ids. Record verdicts from the fixture's
review and status evidence. Historical traces use a separate variant.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from statistics import median
from uuid import UUID

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agents_platform_runners_app import platform_base  # noqa: E402


def _card_id(value):
    return UUID(value) if value else None


def _usage(row):
    return {field: row.get(field) for field in ("tokens_in", "tokens_out")}


def build_report(manifest, fetch_run):
    samples = []
    groups = defaultdict(list)
    seen = set()
    for entry in manifest:
        route = entry["route"]
        if route not in {"card_bound", "cardless_a2a", "post_deploy"}:
            raise ValueError(f"Unknown route: {route}")
        review_type = entry.get("review_type", "unknown")
        if review_type not in {"ui", "code_regression", "deploy", "other", "unknown"}:
            raise ValueError(f"Unknown review type: {review_type}")
        card_id = _card_id(entry["card_id"])
        if (card_id is None) != (route == "cardless_a2a"):
            raise ValueError(f"Card binding does not match route: {route}")
        run_ids = [entry["run_id"], *entry.get("linked_run_ids", [])]
        rows = []
        for run_id in run_ids:
            if not re.fullmatch(r"[0-9a-f]{32}|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", run_id):
                raise ValueError(f"Invalid run ID: {run_id}")
            run_uuid = UUID(run_id)
            if run_uuid in seen:
                raise ValueError(f"Run counted twice: {run_id}")
            seen.add(run_uuid)
            row = fetch_run(run_id)
            if row["id"] != run_id:
                raise ValueError(f"Run response does not match {run_id}")
            if row["kind"] == "workflow":
                raise ValueError(f"Workflow rollup cannot be counted: {run_id}")
            rows.append(row)
        review, *linked = rows
        if _card_id(review.get("notion_task_id")) != card_id:
            raise ValueError(f"Review {review['id']} does not belong to manifest card")
        # Linked IDs are explicit because a callback can resume the caller's
        # session rather than the review's session. Use a single attribution
        # per run, including when a helper could be linked from two reviews.
        terminal = {"success", "error", "cancelled"}
        covered = all(
            row["status"] in terminal
            and type(row.get("tokens_in")) is int
            and row["tokens_in"] > 0
            for row in rows
        )
        model = review.get("model_slug") or "unknown"
        key = {
            "case": entry["case"],
            "variant": entry["variant"],
            "revision": entry["revision"],
            "route": route,
            "review_type": review_type,
            "model": model,
        }
        sample = {
            **key,
            "card_id": entry["card_id"],
            "verdict": entry["verdict"],
            "review": {"run_id": review["id"], **_usage(review)},
            "linked": [{"run_id": row["id"], **_usage(row)} for row in linked],
            "combined_input_tokens": sum(row["tokens_in"] for row in rows) if covered else None,
        }
        samples.append(sample)
        groups[tuple(key.items())].append(sample)

    slices = []
    for key, group in groups.items():
        tokens = [sample["combined_input_tokens"] for sample in group
                  if sample["combined_input_tokens"] is not None]
        slices.append({
            **dict(key),
            "samples": len(group),
            "samples_with_input_counters": len(tokens),
            "median_combined_input_tokens": median(tokens) if tokens else None,
            "verdicts": [sample["verdict"] for sample in group],
        })
    return {"samples": samples, "slices": slices}


def platform_url(config):
    """Same resolution the app itself uses, so a config that omits
    agents_platform_base (the usual case) still finds the platform."""
    return platform_base.resolve(config).rstrip("/")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path, help="Runners app config JSON")
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    manifest = json.loads(args.manifest.read_text())
    with httpx.Client(
        base_url=platform_url(config),
        headers={"Authorization": f"Bearer {config['agents_platform_token']}"},
        timeout=30,
    ) as client:
        def fetch_run(run_id):
            response = client.get(f"/api/runs/{run_id}")
            response.raise_for_status()
            return response.json()

        print(json.dumps(build_report(manifest, fetch_run), indent=2))


if __name__ == "__main__":
    main()
