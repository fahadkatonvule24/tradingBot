# Trading-system bug review, 16 November 2026

The review reproduced defects with synthetic data and the fake MetaTrader adapter before fixing
them. No live terminal or trading account was used for verification.

| Area | Defect | Corrected behavior |
|---|---|---|
| Configuration | Strings such as `"false"` were accepted as permission flags; nonfinite limits and invalid periods reached the engine. | Boolean types, finite numeric values, supported structures and numeric bounds are validated. The trading gate also checks permission booleans explicitly. |
| Order rounding | A 0.25-lot step could turn a requested 0.76 lots into 0.8; prices were rounded only to display digits. | Volume rounds down using the actual lot step and cap; prices align to the instrument's tick size. |
| Position sizing | Zero equity could divide by zero; a fixed lot could still be sized against negative equity. | Invalid equity, volatility, tick values and volume specifications produce an explicit rejection. |
| Portfolio risk | Positions without stops or unavailable symbol specifications contributed zero risk. | Risk is marked unknown, new entries are blocked, and the dashboard displays the condition without invalid JSON or JavaScript errors. |
| Margin | Missing, negative or nonfinite broker margin estimates bypassed the margin cap. | Executable plans reject unknown margin. Named manual accounts can still estimate risk-based size without leverage; their adapter refuses orders. |
| MT5 queries | Failed position/history queries were treated as empty results; an empty opening-history response was cached permanently. | API failures raise an error. Empty opening history is retried so the original stop and volume can be recovered. |
| Quotes and bars | Fresh bars could yield actionable advice using stale/future quotes; unfinished bars were not rejected by the advisor. | Quote age, clock lead, prices and bar completion are checked. The MT5 adapter rechecks the quote before opening or closing a trade. |
| Position management | Repeated polling could close a newer position on an older opposite signal. Stale bars retained their separate exit signal. | Reverse exits require a bar closed after the entry; stale entry and exit signals are suppressed. Live stop management uses the current quote. |
| News | A broken or temporarily missing calendar file discarded known blackouts. | The last readable calendar remains active until a valid replacement arrives. An explicit empty list clears it. |
| Historical data | Missing timestamps, nonfinite prices and negative spreads were accepted from CSV. | Invalid rows fail loading with a descriptive error before simulation. |

Regression cases are in `tests/test_audit_regressions.py`, alongside the existing sizing,
engine, advisor, manual-account and fake-MT5 suites. The margin test's open position now has a
bounded stop so it continues to isolate the margin guard. Local HTTP test timeouts allow for
Windows scheduling delays while retaining the response and security assertions.

These checks verify the implemented behavior. They do not establish profitable signal
prediction or replace execution testing against a broker's demo environment.
