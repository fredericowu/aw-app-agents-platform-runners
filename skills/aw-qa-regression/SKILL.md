---
name: aw-qa-regression
description: Validate regression tests and mutation-test fixes in an isolated worktree.
---

## Mutation-test the fix in isolation

1. Confirm the focused regression passes at the reviewed revision. Check
   that CI discovers the test.
2. Use a disposable worktree or scratch copy of that revision. Save the
   exact restoration patch or record the immutable revision before editing.
3. Remove one fix component and leave its test intact. Run the test and
   confirm it fails for the intended reason. Repeat separately for every
   component of a multi-part fix.
4. Restore the original code, rerun the test, and check the isolated
   working tree. Record the pass, the intentional failure and the restored
   pass in the verdict. A test that passes without its fix is insufficient
   evidence of the regression.

Make mutations exclusively in the isolated copy. The shared checkout and
its stash are used by concurrent sessions.
