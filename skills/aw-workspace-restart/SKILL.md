---
name: aw-workspace-restart
description: Gracefully update or restart a workspace from an agent with update_workspace / workspace_restart_status / cancel_workspace_restart — when to use them instead of restarting core by hand, how the 1h lock and drain behave, and what to do for each failure outcome (dispatch_refused incl. uncommitted_host_edits, dispatch_failed, update_failed, unhealthy, drain_timeout, expired). Use before restarting or updating any workspace.
---

# Graceful workspace update / restart

Three MCP tools on the agents-platform-runners server:

- `update_workspace(workspace?, restart_only=false, reason)` — takes a restart lock and returns at once with the lock id and state. It does not block.
- `workspace_restart_status(lock_id)` — read the lock: `phase`, `outcome`, `failure_report`, `in_flight_run_ids`, `expires_at`, `checks_done`.
- `cancel_workspace_restart(lock_id)` — release the lock now; held fires replay immediately.

If the feature is off on this deployment, `update_workspace` returns a 503 "not enabled" error and nothing happens. Fall back to the manual path below.

## When to use which

- **Use `update_workspace`** whenever other runs, scheduled fires or human messages could be hit by the restart: it drains in-flight runs, holds new fires as scheduled wake-ups, verifies health afterwards and wakes you with a report.
  - `restart_only=false` (default): the same update the aw-console Update button runs (latest release, recreate, verify).
  - `restart_only=true`: restart the core only, no update.
- **Restart core by hand** (`aw-workspace-cli restart core`) only when the feature is off, or the workspace is already down and there is nothing to drain. A manual restart from inside a run kills that run and every other run on the workspace, with no hold and no report.
- Any workspace of your own tenant is allowed (`workspace` defaults to the one you run on). Another tenant's is refused (403 `cross_tenant`).

## How it behaves

- The lock lasts 1 hour at most.
- While held, new fires on the workspace become scheduled wake-ups and replay in order after release. Human messages are queued with a notice.
- In-flight runs get up to 30 minutes to drain. There is no abort and no force.
- You are woken with a report on success and on failure, then you decide. Nothing is retried automatically.
- After calling `update_workspace`, end your turn. Targeting your own workspace is expected to end your run; you are woken afterwards.

## Outcomes and what to do

Every failure report carries: lock id, phase reached, outcome, straggler run ids, aw-backend operation state and error, last `/healthy` payload, count of held fires, timestamps.

| Outcome | Meaning | Do |
|---|---|---|
| `dispatch_refused` | The workspace is untouched; lock released, fires replayed. The verbatim refusal is in the report. | Read it. 409 `uncommitted_host_edits` (lists the files): the host tree has uncommitted edits. Commit/stash them or ask the human; do not retry blindly, the same call will refuse again. 503 offline or latest version unknown: wait or check the host, then retry. 401/403: trust or tenant problem, tell the human. |
| `dispatch_failed` | The dispatch to the workspace did not go through. | Check `workspace_restart_status`, then retry once; if it repeats, report to the human with the failure report. |
| `update_failed` | The update operation reported failure. | Check `workspace_restart_status` first. If the lock is released (workspace still ready, often on the old version), decide whether to retry with a new `update_workspace`. If the lock is still active (`failed_holding`: workspace not ready, held fires still held, probing every minute until ready or expiry), calling `update_workspace` again just returns that same lock (`created: false`) and dispatches nothing. Instead wait for recovery or expiry, call `cancel_workspace_restart` if replaying held fires into a dead workspace is acceptable, or escalate to the human. |
| `unhealthy` | 20 checks passed without the workspace becoming ready. | Same as `update_failed`. Read the last `/healthy` payload in the report to see which part is not ready. |
| `drain_timeout` | Runs were still in flight at the 30 min deadline. Nothing was restarted; lock released, fires replayed. | The report lists the straggler run ids. Wait for them (`wait_run`), or ask the human to cancel them, then call `update_workspace` again. Never cancel other agents' runs on your own initiative. |
| `expired` | The 1h lock ended before completion; released. | Check `workspace_restart_status` and the workspace's own health, then decide whether to retry. |
| success | Workspace is ready on the expected version. | Verify what you needed (version, tools), then continue. |

If your own session lived on the workspace that did not come back, your wake cannot run there; the report stays on the lock row (`workspace_restart_status`) and is delivered to your human channel if you have one.
