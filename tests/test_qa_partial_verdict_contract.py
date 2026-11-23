"""Manager v2: QA must report verified / not-verified lists, not stop the flow."""
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent / "skills/aw-agent-qa/SKILL.md"


def test_qa_partial_verdict_contract():
    t = " ".join(SKILL.read_text().split())
    assert "Partly verified" in t
    assert "`Verified:`" in t and "`Not verified:`" in t
    assert "do not stop the flow and do not use `need_human`" in t


def test_qa_partial_verdict_owner_and_core_exceptions():
    t = " ".join(SKILL.read_text().split())
    assert "use the owner-only procedure below instead" in t
    assert "**Core criteria could not be verified.** Do not pass." in t
    assert 'return_to_caller_agent(kind="blocker")' in t
    assert "call `set_blocker` with the lists" in t
