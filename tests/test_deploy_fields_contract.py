"""Coder and QA contracts must set the Deploy* fields kanban_check_deployed reads."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _flat(rel: str) -> str:
    return " ".join((ROOT / rel).read_text().split())


def test_coder_sets_deploy_fields_at_ready_to_deploy():
    t = _flat("skills/aw-agent-coder/SKILL.md")
    assert "`DeployComponent`" in t and "`DeployToVersion`" in t
    assert "`app:<slug>`, `service:<name>`, `core`, `remote-host`" in t
    assert "Use `none` when the work needs no deployment" in t
    assert "Never `latest`" in t


def test_qa_sets_deploy_fields_before_ready_to_deploy():
    t = _flat("skills/aw-agent-qa/SKILL.md")
    assert "**Before `ready_to_deploy`:** set `DeployComponent` and `DeployToVersion`" in t
    assert "`none` when nothing needs deploying" in t
    assert "never `latest`" in t
