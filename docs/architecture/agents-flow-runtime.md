# Agents Flow runtime notes

## Executor and wakeups

- **`model_slug` backfill:** `_run_agent_impl` only backfills `model_slug`
  onto a Run row when it creates that row itself. `ask_human` resume and the
  native flow reprompt both pre-create the row (explicit `run_id`, to avoid
  a race) before calling `run_agent()`, landing in the "row already exists"
  branch that skips it — any new pre-create-then-run path needs to copy
  `model_slug` explicitly too, and should be covered by
  `rearm_stuck_wakeup_runs()`'s boot-time sweep (re-fires runs stuck
  `pending`/`initiator_kind=wakeup` for 20s+).
- **Kanban-move vs. an active QA cycle:** `mark_flow_done` and
  `_escalate_need_human` both move the card via `/api/notion/kanban/move`,
  which hard-locks `done`/`need_human`/`ready_to_deploy` while
  `QAStatus=In Progress`. Neither falls back to `set-qa-status` to force it
  through (that would clobber an active QA review) — a rejected/failed move
  just pings sysadmins on Telegram so a human notices, rather than dropping
  it silently.
- An agent in multiple enabled flows gets a merged connected-agents list —
  fine as-is, revisit only if it gets noisy in practice.
