---
name: aw-qa-live-ui
description: Exercise a delivered UI in a clean browser and record interactive screenshot evidence.
---

## Before trusting a live DOM, confirm you're looking at what you think

A live frontend check can "confirm" a bug that does not exist. A review
once spent ~25 tool calls chasing a phantom zero-results bug before
discovering it had never been looking at the component under test. Three
distinct traps, all of which produce a confident wrong verdict:

- **A pinned layout/route key in `localStorage` can route you into a
  different UI entirely.** A stale value left by a prior session (in this
  ecosystem's workspace UI, `aw_layout_intent`) silently renders a
  separate layout implementation baked into a different bundle — not the
  app UI you are testing. Nothing visible on the page signals it; the
  feature just looks broken or missing. Clear it before starting:
  `localStorage.removeItem('<the key>')`.
- **Hash-only navigation does not reload JS.** Navigating by URL fragment
  alone is a same-document navigation in an SPA. After a fresh deploy it
  keeps running the **old** already-loaded bundle indefinitely while the
  address bar looks correct. Force a real full-URL navigation.
- **A cached bundle lies on any app's frontend, not just one.** A review
  of a different app's own SPA confirmed by direct API call that the
  backend already returned the new fields, while the live DOM still showed
  only the old rows — ~10 tool calls went into diffing asset hashes before
  recognising a cached bundle. A hard reload bypassing cache fixed it
  immediately.

So: if a backend fix is confirmed live by direct API call but the DOM
still shows old behaviour, **suspect a cached bundle before suspecting the
deploy or the fix.** Before trusting any live DOM or network result,
confirm you are looking at the component you think you are, running the
bundle you think you are — evaluate against the real DOM rather than only
the accessibility snapshot, start from known-clean `localStorage`, and
force a cache-busted reload after any fresh deploy. A "bug" that
disappears once you clear that key and force an uncached reload was never
a bug in the deployed code, and reporting it as one sends a coder to fix
nothing.

Exercise the interaction with Playwright and capture screenshot evidence
showing its result. Attach the screenshot to the card when present and cite
it in the verdict. Use an artifact reference in cardless reports.
