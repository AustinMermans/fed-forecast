# Fed Forecast

[Fed Forecast](https://austinmermans.github.io/fed-forecast/) turns Polymarket's
five-outcome FOMC meeting contracts into a replayable distribution for the
upper bound of the Federal funds target range.

The site has three jobs: replay what the market expected on an earlier date,
show the latest archived contracts underneath today's implied path, and make the modeled
dependence between meetings inspectable through a conditional path explorer.
Quoted meeting probabilities are data; conditional repricing is a structural
model and is labeled accordingly.

Read the public [methodology and results note](https://austinmermans.github.io/fed-forecast/methodology.html)
for the probability transformation, rate translation, joint model, replay
provenance, cross-market consistency audit, and small-sample forecast scores.

## Data and model boundary

- Five separate binary Yes prices are normalized by their sum to form each
  meeting marginal. A discount factor common to every same-meeting outcome
  cancels in this normalization; raw sums and quote-quality diagnostics remain
  visible for non-common carry and trading frictions.
- Meeting actions map to representative −50/−25/0/+25/+50 bp changes and are
  accumulated from the official target-range upper bound.
- The separately traded year-end target-rate distribution is retained as an
  independent market view. Any difference from cumulative meeting actions is
  shown as disagreement, not an additional Fed move.
- The conditional tree uses a meeting-only persistence kernel followed by
  iterative proportional fitting. Its transitions are modeled; the meeting
  marginals are quoted. All finite action paths are enumerated exactly rather
  than sampled. No historical transition fit is active, and the year-end
  market never constrains the action path.
- Historical daily marks are reconstructed and labeled. Contemporary
  snapshots are archived four times per day. The realized target path is
  added afterward for comparison.

The inactive historical-transition diagnostic is published separately from
the live tree. Its legacy 15:30 full-communications sample contains 13
adjacent transitions (3 cuts, 10 holds, 0 hikes) and zero scored walk-forward
folds; it has no out-of-sample performance evidence and five failed production
gates. Ten-minute surfaces may be synthetic or non-simultaneous. A new 14:15
study remains pending sourced official-release timestamps and tight quote
synchronization. The compact, manifest-pinned record is available at
[`site/data/evidence-summary.json`](site/data/evidence-summary.json).

## Run locally

```sh
PYTHONPATH=src python -m unittest discover -s tests
python scripts/build_github_pages.py --output site \
  --evidence-summary site/data/evidence-summary.json
python -m http.server 8767 --directory site
```

To collect a current snapshot:

```sh
PYTHONPATH=src python -m fed_forecast refresh \
  --config config/markets.json \
  --output-dir outputs/meeting-scenarios
```

Collection is fail-closed: malformed or incomplete market topology never
replaces the last valid public snapshot. GitHub Actions refreshes the site four
times per day and deploys the static `site/` directory to GitHub Pages.

## Repository map

- `config/` — current market topology, model settings, and official decisions
- `src/fed_forecast/` — collection, validation, probability, and joint-model code
- `scripts/` — static-site builder and public-bundle validation
- `data/` — reconstructed historical replay inputs
- `outputs/` — immutable verified run archive used by the builder
- `site/` — deployable GitHub Pages application and compact public data
- `tests/` — model, replay, parser, and publication invariants

No Fed funds futures comparison, fee-adjusted probability, calibration claim,
or learned transition is part of Stage 1A.

The archive is explanatory research, not a trading recommendation or proof of
calibration. See the [data and attribution notice](NOTICE.md).
