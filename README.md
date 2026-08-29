# Fed Forecast

[Fed Forecast](https://austinmermans.github.io/fed-forecast/) turns Polymarket's
five-outcome FOMC meeting contracts into a replayable distribution for the
upper bound of the Federal funds target range.

The site has three jobs: replay what the market expected on an earlier date,
show the latest archived contracts underneath today's implied path, and make the modeled
dependence between meetings inspectable through a conditional path explorer.
Quoted meeting probabilities are data; conditional repricing is a structural
model and is labeled accordingly.

## Run locally

```sh
PYTHONPATH=src python -m unittest discover -s tests
python scripts/build_github_pages.py --output site
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

See [the product specification](docs/PRODUCT_SPEC.md),
[implementation plan](docs/IMPLEMENTATION_PLAN.md), and [data notice](NOTICE.md).
