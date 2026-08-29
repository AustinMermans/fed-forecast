# Fed Forecast — implementation plan

## Stage 1 — clean extraction

1. Create a standalone `fed-forecast` Git repository without modifying the playground.
2. Copy only the Fed path, meeting-scenario, replay, reporting, and public API modules required by the production tool. Preserve the compact historical-transition diagnostic as versioned evidence, not a live dependency or active tree input.
3. Rename the Python package and command surface from `curve_forecaster` to `fed_forecast`.
4. Carry forward the verified historical replay, compact public vintages, relevant configurations, and focused tests.
5. Add MIT licensing, a data-provenance notice, production README, strict ignores, versioned model configuration, archive schema, and a minimal project manifest.

Gate: the focused test suite passes; pinned fixtures prove quote transformation and topology; a current live snapshot can be collected into an ignored temporary directory; and the project builds from a clean copy outside Babel with no old names, paths, or unused assets.

## Stage 2 — production site

1. Reduce the page to replay, current market table, conditional tree, and concise methodology.
2. Place replay and current market information in one laptop-height analysis surface.
3. Preserve fixed-window playback, smooth camera motion, meeting rules, realized path, five-outcome branches, volume/liquidity, and carried-quote labeling.
4. Remove Treasury, payroll, committee-consensus, speech-event, and unused artifact code/data.
5. Build a standalone site bundle from current verified output plus the versioned archive, with explicit quote/replay provenance and stale-state labeling.

Gate: HTTP smoke returns the page and JSON; schema, full-archive chronology/provenance, probability sums, horizon continuity, source links, Pages-subpath loading, laptop/mobile layout, keyboard controls, focus, and reduced-motion behavior pass automated checks.

## Stage 3 — operations and publication

1. Add a four-times-daily GitHub Actions workflow that collects into temporary storage with bounded retries, validates a complete snapshot, atomically publishes only valid changed data, tests, builds, commits compact data changes, and deploys Pages while retaining the last good site on failure.
2. Add a manual refresh path and concurrency protection.
3. Create the public GitHub repository, set its description/homepage, push `main`, configure Pages for GitHub Actions, and dispatch the first deployment.
4. Verify the workflow and deployed URL.

Gate: the public Pages URL serves the current site and its data bundle, with the repository clean and the workflow reproducible.

## Review sequence

After the first integrated site build, conduct three sequential product-manager reviews:

1. **Cold-read PM:** comprehension, hierarchy, and whether the product earns attention in the first minute.
2. **Rates PM:** usefulness to a fixed-income audience, analytical honesty, and market/path linkage.
3. **Publication PM:** prose, visual restraint, mobile/desktop coherence, trust, and launch readiness.

Accepted findings are implemented between rounds. A final engineering audit verifies data contracts and operations after PM changes.

Each round is recorded in `reviews/round-N-*.md` with persona, severity-tagged findings, adjudication, implemented changes, and the resulting P0–P3 gate. The final engineering audit is recorded separately.
