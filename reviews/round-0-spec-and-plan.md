# Round 0 — specification and plan review

**Persona:** skeptical product and engineering PM
**Artifact:** `docs/PRODUCT_SPEC.md` and `docs/IMPLEMENTATION_PLAN.md`

## Findings

- **P1:** Define the plotted rate and price-selection hierarchy.
- **P1:** Add replay provenance, age, staleness, and no-look-ahead rules.
- **P1:** Make the conditional model reproducible and distinguish it from historical estimation.
- **P1:** Recast the unsupported claim that distributions sharpen near meetings as a question for the replay.
- **P1:** Add meeting-contract rollover and incomplete-topology handling.
- **P1:** Protect the last good site from partial refreshes.
- **P1:** Add concrete responsive, keyboard, focus, and reduced-motion gates.
- **P2:** Prove the extraction works outside Babel and contains no old paths or scope.
- **P2:** Separate code licensing from third-party data rights.
- **P3:** Make review records auditable.

## Adjudication and changes

All findings were accepted. The product contract now specifies the target-range upper bound; CLOB-midpoint/Gamma-fallback hierarchy; normalization; provenance and stale handling; structural kernel and IPF parameters; diagnostic-only historical fit; explicit rollover workflow; atomic last-good publication; responsive and keyboard requirements; clean-copy validation; third-party data notice; and review-record format. The headline was changed from an empirical calibration claim to a question the replay lets readers inspect.

## Gate

P0=0 · P1=0 · P2=0 · P3=0 after revision.
