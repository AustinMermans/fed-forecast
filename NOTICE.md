# Data and attribution

Fed Forecast is an independent research project and is not affiliated with,
endorsed by, or sponsored by Polymarket, the Federal Reserve Board, or the
Federal Reserve Bank of New York.

- Prediction-market event metadata, order-book midpoints, and public price
  histories come from Polymarket's public Gamma and CLOB endpoints. Each live
  observation records its source URL, retrieval time, selected price source,
  and quality state. Contract wording and linked resolution rules govern the
  underlying markets.
- Target-range decisions and the meeting calendar come from the Federal
  Reserve. The latest effective Federal funds rate and target range come from
  the New York Fed's public reference-rates endpoint.
- Open-ended meeting buckets are represented as ±50 basis points only for
computing a displayed path. Prediction-market prices exclude fees, spread,
  slippage, and contract-specific settlement risk.

Primary public endpoints and documentation:

- [Polymarket developer documentation](https://docs.polymarket.com/)
- [Polymarket Gamma API](https://gamma-api.polymarket.com/)
- [Polymarket CLOB API](https://clob.polymarket.com/)
- [Federal Reserve FOMC calendars and information](https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm)
- [New York Fed reference rates](https://markets.newyorkfed.org/read?productCode=50&eventCodes=500&startDt=&endDt=&sort=postDt:-1,eventCode:1&format=csv)

The MIT license applies to this repository's code. It does not relicense or
grant rights in third-party data, market wording, trademarks, or source media.
