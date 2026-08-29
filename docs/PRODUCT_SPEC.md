# Fed Forecast — production product specification

## Purpose

Fed Forecast is a public, interest-rate-only research tool that translates Polymarket FOMC contracts into an intuitive distribution for the Federal Reserve target-rate path. Its primary job is explanatory: let a non-specialist see what the market expected on any archived day, how that forecast changed through time, what actually happened, and how a selected meeting outcome would condition later meetings.

It is not a trading recommendation, a substitute for Fed funds futures, or a complete statistical validation of prediction markets.

## Audience and headline

The primary audience is investors, policy watchers, and curious readers who understand rates but may not work directly with probability distributions.

Headline question: **What path for the Fed is Polymarket pricing, how has that distribution changed, and what would a realized meeting outcome imply for later meetings?**

The product must not claim that Polymarket is universally well calibrated. The archive is an illustrative monitor, and any calibration language must distinguish observed resolved-market evidence from qualitative inspection.

## Product surface

The production site is one page with three connected sections:

1. **Replay and realized path — primary feature.** A fixed six-month forward window plays archived daily and event-time forecasts from August 2024 through the present. The 0.25 percentage-point rate grid scrolls smoothly; the realized target path appears only after outcomes are known. Meeting coverage must never disappear merely because a narrower collector omitted a contract. Any carried quote is labeled.
2. **Current market surface.** The current five-outcome probabilities, market links, 24-hour volume, and liquidity sit directly below or beside the replay so readers can compare the implied path with its underlying contracts without losing context.
3. **Conditional path explorer.** At each meeting, users choose −50 bp or more, −25 bp, unchanged, +25 bp, or +50 bp or more. The model updates all later meeting distributions, animates the rate fan with the replay camera behavior, and clearly separates quoted marginals from modeled dependence.

The Treasury grid, payroll forecast, committee-consensus scenarios, speech article, and unrelated economic panels are out of scope for this repository.

## Visual direction

Retain the existing dark background, cyan forecast bands, cream realized path, muted orange event/selected-path accents, and restrained data-table encoding. Simplify typography, navigation, labels, and editorial chrome. The page should read as a polished research instrument, not a generic dashboard or a newspaper imitation.

The replay and current market table should be visible together on a typical laptop viewport. The main chart may use a sticky or compact control rail, but content must remain usable on mobile and by keyboard.

## Analytical contract

- The plotted policy rate is the **upper bound of the Federal funds target range**. The realized line changes only on the effective date of an announced target-range decision. EFFR may appear as separately labeled context but is never spliced into the target-rate series.
- Each meeting starts from five mutually exclusive Polymarket Yes-contract prices: −50 bp or more, −25 bp, unchanged, +25 bp, and +50 bp or more. The collector uses a CLOB Yes-token midpoint only when it lies inside a two-sided order book whose spread is at most 10 percentage points; otherwise it uses the contemporaneous Gamma `outcomePrices` Yes value and labels the observation degraded. Last trade is not used.
- Raw selected prices and their sum are retained. Display probabilities are `p_i / sum(p)` for the five outcomes. Collection fails rather than publishing when any outcome is absent, duplicated, non-finite, outside [0,1], or when the raw sum is zero.
- Open-ended 50+ bp tails use ±50 bp only as representatives for path arithmetic.
- The joint conditional tree preserves every observed one-dimensional meeting marginal and the quoted terminal-rate marginal within `1e-10` absolute error.
- Cross-meeting dependence comes from the versioned structural kernel in `config/model.json`: persistence strength 0.35, decay 0.70 per meeting edge, terminal-consistency sigma 100 bp, followed by iterative proportional fitting with a 2,000-iteration ceiling. Failure to converge is a hard build error. This structural model is not represented as historically estimated.
- A separately versioned resolved-market transition diagnostic may be shown only as diagnostic evidence. It must include its training-data manifest, cutoff date, row counts, fitting parameters, validation gates, and an explicit `active_in_tree: false` state until every production gate passes. Historical vintages never use parameters fitted with later observations.
- The actual Fed target is discrete. Off-lattice January expectations arise only from the conditional terminal bridge.
- Historical frames retain their original information set. Every quote stores source, source timestamp, collection timestamp, reconstruction status, and age at the frame. A contract is never backfilled before its first observed quote. Quotes older than seven calendar days are omitted from arithmetic and visibly marked stale; no later observation may fill an earlier frame. Later API reconstruction and contemporaneous collection are separately labeled.
- Prices exclude fees, spread, slippage, and contract-specific settlement risk.

## Market lifecycle and publication safety

- The configured contract universe is validated on every run against exact five-bucket labels, unique Yes tokens, meeting chronology, resolution rules, and the official FOMC calendar. A repository issue/failed Action is the rollover alert when an expiring meeting has no validated successor. Adding a meeting is an explicit reviewed configuration change; the collector never silently guesses a replacement contract.
- Network calls use bounded timeouts and retries. Collection writes to a temporary run directory; schema, topology, chronology, probability, and horizon checks must pass before an atomic replacement of public data. A failed or partial refresh leaves the last valid site untouched.
- The page shows quote time, refresh time, quote quality, and stale/error state. A successful unchanged run is a no-op.
- Repository automation has only `contents: write` and `pages/id-token` permissions required by its commit and deployment jobs, with concurrency protection and branch-compatible pull/rebase behavior.

## Repository and operations

- Standalone public repository: `AustinMermans/fed-forecast`.
- License: MIT.
- Static GitHub Pages deployment from `site/`.
- Scheduled public-data refresh four times daily (`17 */6 * * *`) plus manual dispatch.
- Each refresh runs the full test suite, collects the current markets, rebuilds compact public history, commits changed public data, and deploys Pages.
- The workflow uses no private credentials beyond GitHub's built-in token and only documented public endpoints.
- Raw run directories remain ignored; compact replay vintages and provenance needed to reproduce the public visualization are versioned.
- `NOTICE.md` identifies Polymarket and Federal Reserve sources, links resolution rules where available, records retrieval metadata, states that third-party data is not relicensed under MIT, and disclaims affiliation.

## Acceptance criteria

- A fresh clone, including a copy in a temporary directory outside Babel, can run tests and build the site with Python 3.11+ and no third-party Python dependencies. No symlink, absolute local path, `curve_forecaster` name, playground reference, or out-of-scope config/asset may remain.
- The site works through HTTP and under the GitHub Pages subpath.
- A pinned quote fixture proves source selection, raw sums, normalized five-way values, degraded fallback, and stale rejection.
- Archive validation checks every frame for increasing timestamps, no quote before first observation, nonnegative quote age, provenance fields, and a rolling six-month horizon. The final seven frames have consistent meeting coverage, including January 2027 in the current archive.
- Current meeting probabilities sum to one and the conditional tree preserves all five meeting marginals within configured tolerance.
- A rollover fixture proves that an expired meeting is rejected and a newly configured validated meeting appears without duplicate buckets.
- Every visible current market has a direct source link; activity is labeled unavailable when not archived.
- At 1440×900, the replay and at least the first rows of its source-market table are simultaneously visible. At 390×844 the layout has no horizontal page overflow. Replay and all five conditional outcomes are keyboard operable, focus is visible, color is not the only state cue, and `prefers-reduced-motion` disables interpolated playback.
- At least three sequential PM review rounds are recorded, findings are adjudicated, and accepted changes are implemented.
- Each review record in `reviews/` names the round and persona, lists severity-tagged findings, records adjudication and resulting changes, and ends with a gate summary.
- GitHub Actions and Pages succeed on the public repository.
