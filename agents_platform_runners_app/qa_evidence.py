"""Structured producer claims for independent QA review."""

from __future__ import annotations

import asyncio
import json
import os
import re
from urllib.parse import urlsplit

import httpx
from jsonschema import Draft202012Validator


def _object(properties):
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def _text(max_length):
    return {"type": "string", "minLength": 1, "maxLength": max_length, "pattern": r"\S"}


_REFERENCE = _text(480)
_CODE = _object({
    "repo": {"type": "string", "maxLength": 140, "pattern": r"^[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+$"},
    "pr": {"type": "integer", "minimum": 1, "maximum": 2147483647},
    "merged_sha": {"type": "string", "pattern": r"^[a-f0-9]{40}$"},
})
QA_EVIDENCE_SCHEMA = _object({
    "version": {"const": 1, "type": "integer"},
    "revision": {"oneOf": [_CODE, _object({"artifact_revision": _text(180)})]},
    "scope": {"oneOf": [_object({"reference": _REFERENCE}), _object({"summary": _text(1200)})]},
    "checks": {
        "type": "array", "minItems": 1, "maxItems": 12,
        "items": _object({
            "id": _text(40),
            "text": _text(400),
            "result": {"oneOf": [{"const": "unchecked"}, _object({"observed": _text(480)})]},
        }),
    },
    "evidence_refs": {"type": "array", "maxItems": 12, "uniqueItems": True, "items": _REFERENCE},
    "known_gaps": {"type": "array", "maxItems": 8, "items": _text(400)},
})
_VALIDATOR = Draft202012Validator(QA_EVIDENCE_SCHEMA)


def _safe_reference(value):
    if re.search(r"[\s\\<>]", value):
        return False
    if value.startswith("artifact:"):
        path = value.removeprefix("artifact:")
        return bool(re.fullmatch(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*", path)) and all(
            part not in {".", ".."} for part in path.split("/")
        )
    try:
        url = urlsplit(value)
        return url.scheme == "https" and bool(url.hostname) and url.username is None
    except ValueError:
        return False


async def lookup_pr(repo, pr):
    """Read a PR's state and merge commit with the workspace's authenticated
    ``gh``. Runs only where that client exists (the workspace core process
    serving the app's routes), never in the MCP gateway container."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "gh", "pr", "view", str(pr), "--repo", repo,
            "--json", "state,mergeCommit",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise
        if proc.returncode != 0:
            raise ValueError("GitHub did not return the requested PR")
        data = json.loads(stdout)
    except (OSError, TimeoutError, ValueError) as exc:
        raise ValueError("Could not read the PR from GitHub") from exc
    return {"state": data.get("state"), "merge_sha": (data.get("mergeCommit") or {}).get("oid")}


LOOKUP_ROUTE = "/api/apps/agents-platform-runners/qa-evidence/pr"


async def _verify_merged_revision(revision):
    """Ask the workspace API (where ``gh`` is authenticated) for the PR; the
    MCP gateway's stdio child has no GitHub client of its own."""
    from . import kanban_dispatch

    key = os.environ.get(kanban_dispatch.API_KEY_VAR) or kanban_dispatch._from_env_file(kanban_dispatch.API_KEY_VAR)
    if not key:
        raise ValueError("Could not verify the PR's merged revision: workspace API key is not set")
    try:
        async with httpx.AsyncClient(timeout=25.0) as client:
            r = await client.post(
                f"{kanban_dispatch.board_base_url()}{LOOKUP_ROUTE}",
                json={"repo": revision["repo"], "pr": revision["pr"]},
                headers={"X-Api-Key": key},
            )
        if r.status_code != 200:
            raise ValueError("lookup refused")
        pr = r.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise ValueError("Could not verify the PR's merged revision") from exc
    if pr.get("state") != "MERGED" or pr.get("merge_sha") != revision["merged_sha"]:
        raise ValueError("The PR must be merged at the supplied SHA before QA dispatch")


async def render_qa_evidence(evidence):
    error = next(_VALIDATOR.iter_errors(evidence), None)
    if error:
        path = ".".join(map(str, error.absolute_path)) or "qa_evidence"
        raise ValueError(f"Invalid QA evidence at {path}: {error.validator} constraint")

    ids = [check["id"] for check in evidence["checks"]]
    if len(set(ids)) != len(ids):
        raise ValueError("QA evidence check IDs must be unique")
    references = list(evidence["evidence_refs"])
    if "reference" in evidence["scope"]:
        references.append(evidence["scope"]["reference"])
    if not all(map(_safe_reference, references)):
        raise ValueError("QA evidence references must be HTTPS links or relative artifact references")

    block = (
        "QA handoff v1 — producer claims. Verify revision, current scope and evidence independently.\n"
        + json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )
    if len(block.encode()) > 8192:
        raise ValueError("QA evidence exceeds 8192 bytes")
    if "merged_sha" in evidence["revision"]:
        await _verify_merged_revision(evidence["revision"])
    return block
