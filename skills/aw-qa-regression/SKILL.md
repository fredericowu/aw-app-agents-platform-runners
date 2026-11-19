---
name: aw-qa-regression
description: Validate regression tests and mutation-test fixes in an isolated worktree.
---

## Mutation-test a regression test before trusting it

This contract already says to run the dev's claimed fix yourself rather
than take "added a test" on their word. It is not enough: **a passing
regression test is not proof the fix is what makes it pass.** A vacuously
true test — one that would pass against the pre-fix code too — hands you
the same green checkmark as a real one.

To verify a regression test actually exercises the fix:

1. **Temporarily un-apply the fix** in the file(s) it touches.
   - Single dirty file, fix is its only local change — on a shared
     checkout the **patch-file form is the safe default**, because it
     touches no shared state at all:

     ```bash
     git diff -- <file> > /tmp/fix.patch   # save the fix
     git apply -R /tmp/fix.patch           # un-apply it
     # ... rerun the test(s): they must now FAIL ...
     git apply /tmp/fix.patch              # restore
     ```

     If you do reach for the stash instead: **never a bare `git stash` or
     a bare `git stash pop`** — a shared checkout has one global stash
     stack per repo, and a concurrent session's own stash landing between
     your push and pop ends up on top. A bare pop once grabbed an
     unrelated ~10-day-old stash from another session and produced merge
     conflicts in files it had never touched. **`stash@{0}` does not fix
     that** — it is a *positional* reference into the stash reflog, which
     resolves to whatever is on top at the moment it runs, so writing it
     out literally reintroduces the very race you were avoiding. Resolve
     it to an immutable commit sha once, immediately after pushing, and
     restore by that sha:

     ```bash
     git stash push -- <file>
     FIX=$(git rev-parse stash@{0})   # resolve NOW — a real, frozen sha
     # ... rerun the test(s) ...
     git stash apply "$FIX"           # position-independent
     ```
   - Fix embedded in an otherwise-unrelated file, or you need to revert
     only part of a diff: make a targeted edit reverting the fix's logic
     (not the whole file), rerun, then restore with
     `git checkout -- <file>`. **That only restores the fix if the fix is
     already committed** — `git checkout --` resets to HEAD, so on an
     uncommitted fix it silently discards it instead of bringing it back,
     and you won't notice until a later diff comes up empty. Check
     `git status`/`git diff` first: if the fix is uncommitted, either
     commit it before mutating, or save it yourself
     (`git diff -- <file> > /tmp/fix.patch`) and restore with
     `git apply`.
2. **Confirm the new test(s) FAIL** against the un-fixed code. If they
   still pass, the test is vacuous — report that, don't approve.
3. **Restore and confirm `git diff` is empty** before recording your
   verdict. This is what makes the technique safe on a shared working
   tree; leaving a dirty revert behind for a concurrent session to trip
   over is worse than not mutation-testing at all.
4. **For a multi-part fix, mutate each part separately**, not all at once
   — that proves each part is independently load-bearing rather than one
   part's test passing by accident because of the other.

Coders are expected to do this on their own tests before pushing (it's in
`aw-agent-coder`), and several have unprompted. That doesn't replace your
pass: a coder catching their own vacuous test is cheaper, you catching one
they missed is the point of the review.
