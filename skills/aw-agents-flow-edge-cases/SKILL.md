---
name: aw-agents-flow-edge-cases
description: On-demand Agents Flow guidance for terminal tool failures, partial reviews, ad-hoc card binding and callback recovery.
---

# Agents Flow edge cases

## When no `outcome` value matches what you were told to do

Every `mark_flow_done` outcome **advances the card**: `success` and
`partial` move it to `done`, `failed` moves it to `need_human`. None of them
expresses "verdict recorded, card stays exactly where it is." So a review
explicitly scoped to less than the whole card — one phase of a multi-phase
epic, with other phases still unreviewed — can be handed an instruction that
no available outcome value can honour.

Recognising that is the skill. Before calling `mark_flow_done` on a review
scoped to part of a card, check whether your dispatch said not to advance it
yet. If it did, none of the three values is safe.

What to do instead, in order:

1. **Record the verdict anyway** (`set_qa_status`) — the verdict itself
   needs to exist regardless. But see the warning below: this is *not* a
   side-effect-free way to record one.
2. **If you have a caller, `return_to_caller_agent`** and let them decide
   what happens to the card. They know the whole epic's state; your
   sub-review does not.
3. **If you have no caller, end the turn with a plain text report** rather
   than forcing a `mark_flow_done` that would prematurely advance the card.

This is a different question from "which outcome mirrors my verdict." That
one assumes some value fits. This is about noticing when none does.

**`set_qa_status(status="done")` can itself move the card.** Writing a
`done` QA verdict and moving the card's lane are *not* independent on this
Kanban integration. Two separate reviews, both explicitly told not to
advance a card, recorded their verdict this way and moved the card to Done
as a side effect — the second one found the first had made the identical
mistake and fixed it the same way. Treat it like `mark_flow_done`:

1. Call `set_qa_status` to record the verdict.
2. Immediately `get_kanban_card` to see whether the card's own status moved.
3. If it moved and you were told not to advance, move it back explicitly
   with `move_kanban_task` to its pre-review status before ending the turn.

Expect this, rather than being surprised by it.

The distinction is not "never call `mark_flow_done` on a sub-review" — a
later review of the *full* epic, once every phase has actually been
reviewed, is exactly the right moment to advance it.

## Card binding is not guaranteed on an ad-hoc dispatch

`page_id` on the `aw-kanban` tools, and `mark_flow_done`'s card-move, both
rely on this run's context carrying the card (`NOTION_TASK_ID` /
`_aw_context`). On a run genuinely dispatched **from** a card through a flow
node, that holds and the auto-fill is the right default.

On an **ad-hoc** dispatch — started directly, not from a flow node bound to
a card — that context may be unpopulated even though a real card exists for
the change. This app's dispatcher owns populating it, and the gap is a real
product gap, not intended behaviour. Two symptoms, same root cause:

```
set_qa_status(status="done")                  # returns page_id: null — verdict never landed
set_qa_status(status="done", page_id="<id>")  # explicit — this is the one that sticks

mark_flow_done(outcome="success")             # call succeeds, but the card doesn't move
get_kanban_card(page_id="<id>")               # still sitting un-moved
move_kanban_task(page_id="<id>", ...)         # explicit fallback — this moves it
```

So:

- **The moment you have the card's `page_id`** — from `get_kanban_card`,
  `get_kanban_properties`, or because the dispatch handed it to you — hold
  onto it and pass it explicitly on **every** card-writing call in the run,
  rather than trying the bare form and discovering the failure from a null
  return afterwards.
- **A null `page_id` in a response does not mean no card exists.** It can
  equally mean this run's context wasn't wired to one. Check before
  concluding "no card for this review" and skipping the write.
- **After any `mark_flow_done` on an ad-hoc dispatch, don't assume success
  means the card moved.** If you know the `page_id`, confirm with
  `get_kanban_card` and move it explicitly if it didn't.

## When the terminal action itself FAILS to execute

A tool call that errors or times out is **not** the same as work that
failed, and the two must not land in the same place. `mark_flow_done(
outcome="failed")` says *the task did not work out*. A `mark_flow_done`
that raises a connection error says *I could not record what already
happened* — the verdict you reached is still valid, it just has not been
written down.

**Do not touch the card's status to report a failed terminal action.** In
particular do not call `set_blocker` or `move_kanban_task(status=
"need_human")` for it. Doing so overwrites a healthy delivery with a status
that reads as "this needs a human decision", and the next person to see the
board has no way to tell that the review actually passed. Any verdict you
already recorded — `set_qa_status`, a comment, a property — stands; leave it
alone.

**Do this instead:**

1. **Retry once.** These failures are usually a gateway upstream that
   dropped and is re-checked every 60s (`aw-workspace-cli doctor` names it:
   "an upstream the gateway failed to connect to serves zero tools until a
   reload"). A second attempt often just works.
2. **If it still fails, end your turn** and say so in your final output, in
   plain words: which tool, what error, and what the outcome would have
   been. Name the run id.

Ending without a terminal action is already handled, and handled better
than anything you can improvise: the runtime **reprompts you once**, which
is a free second attempt after the upstream has had time to recover, and if
that also produces nothing it escalates — setting `flow_needs_human` on
every run in the flow (the yellow border on the Flow chip) and pinging
sysadmins on Telegram with your run id and the reason. See
`_maybe_reprompt_or_escalate` / `_escalate_need_human` in
`core/executor.py`.

So the honest failure path costs one reprompt and produces an alert that
carries the run id. Reaching for `set_blocker` instead produces a card that
lies about a delivery, and no alert at all.

**Live example (2026-08-21).** A QA agent finished a review, called
`set_qa_status` successfully — the `aw-kanban` upstream was fine throughout
— and then hit connection errors on `mark_flow_done` twice and a timeout on
`return_to_caller_agent`, both on the `agents-platform-runners` upstream.
Following its own "don't hunt for workarounds, flag it" rule, it called
`set_blocker`. That moved a card whose delivery had just passed review to
**Need Human**, and a human had to read the whole run to discover nothing
was wrong. The agent obeyed its contract exactly; the contract was missing
this section.

## Callback recovery and redirection

**Redirect the wake-up to someone else (`call_me_back_on`, 2026-07-17):**
by default a `call_me_back=true` dispatch wakes YOUR OWN session. Pass
`call_me_back_on="<session_id>"` to wake a **different** session instead
once the dispatched run finishes — chains a hand-off in one call ("run
agent B, and when B is done, wake up session C") instead of B having to
explicitly call C itself with `return_to_caller_agent` or another
`run_agent_async`. Ignored when `call_me_back=false`.

**Changed your mind after dispatching with `call_me_back=false`
(`register_callback`, 2026-07-17):** if you fire-and-forgot a run and later
want to be woken up when it finishes, `register_callback(run_id="<the run "
"you dispatched>")` subscribes retroactively — same delivery mechanism as
`call_me_back=true`, just armed after the fact instead of at dispatch time.
Optional `session_id` redirects the wake-up to a different session (same as
`call_me_back_on`). If the run already finished by the time you call this,
the wake-up fires immediately with its result instead of being dropped.

**Flow-level wakeup (2026-07-16):** whichever run first registered a
`call_me_back` anywhere in this flow's chain (`core.wakeups.FlowWaiter`,
keyed by `flow_run_id`) gets resumed once `mark_flow_done`/`mark_as_planned`
fires, however many hops deep that happened — separate from, and in addition
to, the ordinary one-hop `call_me_back` wakeup each dispatch already gets.

That first registrant is normally a **Source** — a Telegram/Watch session
dispatching INTO the flow from outside it. Until 2026-09-14 that exact case
never armed: the waiter was keyed off the CALLER's own `flow_run_id`, and a
Source is not a flow node, so it has none. No row was written, no error, no
log. It looked like it worked because the one-hop callback still fired when
the directly-dispatched agent ended its turn — the gap only showed when that
agent handed off internally (Architect → Coder → QA) instead of concluding
the flow itself, and the Source was then never told the flow finished,
`need_human` verdicts included. Now keyed off the WATCHED run's flow, so a
Source gets its flow-done wake-up like any in-flow hop.

## Restarting or updating a workspace

Use `update_workspace` (not a hand `restart core`) so in-flight runs drain and held fires replay. If it returns a lock id, end your turn and wait for the outcome wake-up; if it returns an error, handle it in the current turn. See the `aw-workspace-restart` skill for the failure outcomes.
