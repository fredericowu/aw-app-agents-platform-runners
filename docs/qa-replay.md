# QA replay measurement

Use a disposable card or fixture board and a fresh QA session for each
repetition. Keep the merged artifact revision, model, tool access and evidence
access fixed across variants. Preserve the fixture's verdict and routing writes.
Record the expected findings, acceptance evidence and routing alongside the
observed outcome before entering its verdict in the measurement manifest.

Run the read-only report against persisted Agents Platform run rows:

```sh
python3 scripts/report_qa_replay.py --config /path/to/runners-app-config.json manifest.json
```

The manifest is a JSON array with one observation per QA review:

```json
[
  {
    "case": "verified-deploy",
    "variant": "baseline",
    "revision": "fixture-release-v1",
    "route": "post_deploy",
    "review_type": "deploy",
    "card_id": "00000000-0000-0000-0000-000000000001",
    "run_id": "00000000000000000000000000000001",
    "linked_run_ids": ["00000000000000000000000000000002"],
    "verdict": "pass"
  }
]
```

Use `card_bound` for direct reviews with a card and `cardless_a2a` with a
null card ID for cardless reviews. Use `ui`, `code_regression`, `deploy`,
or `other` to classify fixtures. Omit the classification for unknown live
cases. List each attributable helper or resume run once, including callbacks
that resume the caller's session. Review and helper counters come from their
own persisted run rows. The report rejects workflow rollups and duplicate
run IDs. It omits a sample from the median if any listed run has an absent
or zero input counter or is still active. A verdict is the outcome observed
in the fixture. The run's execution status alone cannot establish it.

Keep the two historical traces under their own variant. Compare each
candidate against three isolated baseline repetitions of the same case.
Record setup differences and counter coverage alongside each comparison.
Require matching verdicts, material findings, evidence and flow actions
before using token savings to judge a slice. The proposed release threshold
is a 20% reduction in median combined input on each benchmark case.

| Case | Artifact and expected observations |
| --- | --- |
| System tab rework | Workspace Agent merge `8d276a51fb5ea66ec5dba9c037bd9d9fd0325473`, installed release v0.5.0. The duplicate runs-feed fetch requires rework. Verify notifications, the answer continuation, escalation rejection, and screenshot proof. The historical dispatch's source-target mismatch prevented the rework handoff. A replay needs a correctly bound disposable target and card to verify the intended transition. |
| Workspace binding pass | Agents Platform merge `10807b98064a61273150fbce5c4cd4cda312c2ee`. Verify persisted workspace binding, allowed QA and Architect transfers, replay deduplication, role and binding rejection, and tenant isolation. |
| Cardless A2A | Review a merged fixture against explicit acceptance criteria. Check that the result reaches the caller and produces no card writes. |
| Post-deploy | Review a pinned fixture deployment and verify the deployed version and outcome writes. |
| Comment correction | Add a material scope correction only in a disposable card's comments. Score the replay against the corrected scope and record the action it takes. |

If a historical deployment or artifact is unavailable, document the missing
evidence and any equivalent fixture used. A fixture with different setup
supports a paired comparison against its own baseline.

The report sends the config's bearer token and the listed run IDs to the
platform URL. Run it with `agents_platform_base` in the config or
`AW_AGENTS_PLATFORM_BASE` set; it exits instead of guessing a shared host.
