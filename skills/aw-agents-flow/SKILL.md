---
name: aw-agents-flow
description: Terminal actions for agents in an enabled Agents Flow. Injected at dispatch.
---

# Agents Flow handoff

Use terminal actions only when the dispatch provides an explicit Agents Platform run ID. If it is absent, continue as an ordinary conversation. No terminal action is required in that case, and do not search for, infer, or fabricate a run ID.

End the turn with one of these actions. The dispatch lists connected agents and says whether a caller is waiting. You may discover other agents with `list_agents`.

- **Handoff** — `run_agent_async(slug=..., input=..., target_slug=..., call_me_back=false)` starts a fresh run with only the supplied input. Use the same target and omit `notion_task_id` to inherit the card. Set `call_me_back=true` if you need the result. After a timeout, check `list_target_runs` before dispatching again.
- **Report back** — `return_to_caller_agent(message=..., kind="result")` resumes your caller's session. Use `kind="question"` for a decision or `kind="blocker"` when stuck. It requires a caller and safely no-ops if that caller already requested a callback.
- **Finish** — `mark_flow_done(summary=..., outcome=...)` moves a bound card to Done for `success` or `partial`, or Need Human for `failed` with the summary as the explanation. Supply `qa_run_id` or `qa_not_needed=true` when appropriate. If both are omitted, the platform looks for a recent successful QA run on the same card or target. Supply at most one. For a completed design that still needs implementation, use `mark_as_planned(summary=...)` to move the card to Planned without QA.

**QA deduplication** — On a QA dispatch 409, use the existing run named in the response via `get_run`, `wait_run`, or `register_callback`. Do not retry. Use `qa_rerun=true` for new merged work after a verdict or a deliberate parallel review.

**Human decision** — After investigating, use `ask_human(question=...)` to request input and resume on reply. Move a bound card to Need Human with a comment and still take a terminal action.

**On demand** — Read `skills/aw-agents-flow-edge-cases/SKILL.md` or call the workspace KB tool `load_skill(name="aw-agents-flow-edge-cases")` (`aw__kb__load_skill` via the gateway) for terminal tool errors, reviews that must leave the card in place, ad-hoc card binding, or callback recovery and redirection.
