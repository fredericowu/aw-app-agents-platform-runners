---
name: aw-qa-deployment
description: Verify the exact released and deployed revision for a deployable delivery.
---

## Mandatory for deployable-code deliveries: commit/push, release cut, deploy by default

Scope: this applies whenever the delivery touches deployable code — a
changed file that needs a build, a restart, a release, or a running
process to pick it up (backend/frontend source, a migration, a config file
the app reads at runtime). It does **not** apply to a docs-only change, a
read-only investigation, or something already applied live through an API
call with nothing left to build or ship. This is part of standard
validation for anything in scope, not a separate optional pass — run all
three steps, in order, before you decide the verdict.

1. **Confirm the coder actually committed and pushed.** Check `git log` /
   `git status` on the real repo yourself — not the coder's summary. The
   bar is a clean working tree with the reviewed diff present on the
   pushed branch/SHA. If the code is correct but still sits uncommitted, or
   committed but not pushed, that is **not** a scope question for
   `need_human` — it's a plain `aw-agent-coder` Definition-of-Done gap.
   Return it via the evidence-backed Coder handoff above. QA does not
   commit or push in the coder’s place.
2. **Check whether this delivery needs a release cut and/or has a pending
   prod deploy.** Read the repo's own release/CI setup — the same
   discovery `aw-agent-coder` is expected to do: CI/release workflow
   config, README, `docs/` — to find out whether a push to the default
   branch already ships it (nothing further needed), or whether it still
   needs an explicit release (version bump + tag) and/or a separate deploy
   step to actually reach production. A change that's pushed but still one
   tag or one deploy away from prod is exactly as incomplete as an
   uncommitted diff, and is part of this same check, not a different one.
3. **Execute it by default.** Cut the release and run the deploy yourself
   (the same actions `aw-agent-coder`'s Definition of Done already expects
   from the coder), then verify it actually landed — CI green for that
   exact commit, and the running container/app/version matching the new
   commit or tag, not just a green workflow (see
   `aw-autoskill-verify-deploy-in-container` and sibling skills for how to
   check a live container rather than trusting the pipeline status alone).
   For CI evidence, filter the chronological workflow list by the reviewed
   merge SHA and include `headSha`:

   ```bash
   gh run list --repo <owner/repo> --commit <reviewed-merge-sha> --limit 20 \
     --json databaseId,workflowName,status,conclusion,createdAt,headSha
   ```

   For each required workflow, find the latest run for that exact SHA and
   confirm it finished successfully. If a release build runs on a separate
   commit or tag, also verify that ref includes the reviewed revision and
   validate its own workflows on its exact SHA.
   Do this **without waiting for approval** — ship is the default, not an
   ask. The one exception: the card or the dispatch instructions
   explicitly say not to deploy without approval (e.g. a card flagged for
   a controlled/staged rollout, an explicit Need Human gate on release).
   In that case stop short of the deploy step, say so explicitly in your
   verdict, and route accordingly — `ready_to_deploy` if everything else
   passes, `need_human` if the explicit instruction itself is ambiguous.
   If `is_live`/`is_deployment_needed` kanban properties exist on the card,
   correct them to match what you actually verified (e.g. flip `is_live`
   to `true` once you've confirmed the deploy yourself).

Record what you found and did at each of the three steps in your
`set_qa_status` comment — the commit SHA you verified, the version/tag you
cut, the live check that proves the deploy landed — the same evidence
discipline as the rest of this review. "Released and deployed" with
nothing to back it up is not an acceptable verdict any more than "I
confirmed it logs correctly" is.
