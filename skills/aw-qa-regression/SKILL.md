---
name: aw-qa-regression
description: Replay a reported bug through the affected interface on the merged revision and show it no longer occurs.
---

## Replay the reported behaviour

1. Take the original report: the card's reproduction, the failing request,
   screen or command. If it has none, ask the caller before inventing one.
2. Run that exact scenario against the merged revision through the real
   interface (API, UI, CLI or deployed component).
3. Record the revision, environment, input, expected result, actual result
   and one piece of evidence (response, screenshot, log line or command
   output).
4. If the bug still reproduces, that is the finding: hand it to the Coder
   with the evidence. If it does not, record a pass.

Do not mutation-test the fix, remove code to see a test fail, or audit the
producer's unit tests. Those belong to the review and CI lanes. If the
scenario cannot be exercised, report it under `Not verified:`; that is
not a failure and never triggers rework.
