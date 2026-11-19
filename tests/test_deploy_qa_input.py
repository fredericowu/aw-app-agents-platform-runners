"""mcp_server._deploy_qa_input() — the QA dispatch message for one
kanban_check_deployed row (C3, card 4165bf3b-9510-81ea).

Extracted as a pure function so the no_deploy_needed branch is testable
without spinning up the whole async tool-dispatch handler: the generic
'verified <component> reached <to_version> live' message reads as nonsense
for a DeployComponent='none' sentinel card, which has neither.

Run: python -m pytest tests/test_deploy_qa_input.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents_platform_runners_app import mcp_server  # noqa: E402


def test_normal_deployed_row_names_component_and_version():
    row = {"verdict": "deployed", "component": "app:notion", "to_version": "v0.35.1"}
    text = mcp_server._deploy_qa_input(row)
    assert "app:notion" in text
    assert "v0.35.1" in text
    assert "no deployment is needed" not in text


def test_no_deploy_needed_row_gets_its_own_message_not_the_generic_one():
    row = {"verdict": "no_deploy_needed", "component": "none", "to_version": ""}
    text = mcp_server._deploy_qa_input(row)
    assert "no deployment is needed" in text
    assert "reached" not in text  # the generic template's phrasing must not leak through


def test_handoff_uses_authored_sections_without_copying_history():
    row = {"page_id": "card", "verdict": "deployed", "component": "app:demo",
           "to_version": "v1", "env": "prod",
           "observations": [{"workspace": "main", "verdict": "deployed"}]}
    card = {
        "url": "https://notion.so/card",
        "body_md": (
            "## Objective\nRetire settings.\n"
            "## Acceptance criteria\n- A1: Back restores the previous panel.\n"
            "## Artifact under review\nPR #12, SHA abc123\n"
            "## Delivery evidence\nCI run 7\n"
            "## Prior decisions\n" + "x" * 1601 + "\n"
            "## Unrelated history\nDo not copy this history."
        ),
        "comments_md": "Do not copy these comments.",
    }

    text = mcp_server._deploy_qa_input(row, card)

    assert "https://notion.so/card" in text
    assert "Verified workspaces: main" in text
    assert "A1: Back restores the previous panel." in text
    assert "PR #12, SHA abc123" in text
    assert "Delivery evidence (reported, verify independently)" in text
    assert "Read this section on the Kanban card" in text
    assert "Do not copy" not in text
