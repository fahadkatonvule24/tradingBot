# tradebot

[![CI](https://github.com/fahadkatonvule24/tradingBot/actions/workflows/ci.yml/badge.svg)](https://github.com/fahadkatonvule24/tradingBot/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.10%20%E2%80%93%203.13-blue)
![License](https://img.shields.io/badge/license-MIT-green)

**A MetaTrader 5 trading bot built around one idea: the backtest must run the same code as live
trading.** A single decision engine sends orders through MT5 when live and through a realistic
simulated broker when backtesting, and the same engine powers a **live advisor dashboard** that
tells a person when to trade, where to put the stop and target, and how much they stand to win or
lose. Around it sit layered risk controls, three strategies, parallel walk-forward optimisation,
and HTML reports. It has been run end to end against a real MT5 terminal.

![The tradebot advisor dashboard on live MetaQuotes demo prices](docs/screenshots/dashboard.png)

> `tradebot ui` on live MetaQuotes demo prices. No symbol has a signal on this bar, so the
> advice is **WAIT**, with the exact trigger to watch for, the filters that pass or fail, and the
> plan the bot *would* send if a sell fired now: entry, stop, target, lot size, and the money lost
> at the stop or made at the target. Orders are off because the config is in dry-run mode.

## Contents

- [What it does](#what-it-does) · [Quick start](#quick-start) · [Advisor dashboard](#advisor-dashboard) · [Trades you place on the phone](#trades-you-place-on-the-phone) · [Your own accounts](#your-own-accounts) · [Market panel](#market-panel) · [Calculator](#calculator) · [News](#news) · [Safety model](#safety-model)
- [Architecture](#architecture) · [Strategies](#strategies) · [Risk and trade management](#risk-and-trade-management) · [Trading principles](#trading-principles)
- [Backtesting model](#backtesting-model) · [Results on real data](#results-on-real-data)
- [Bugs fixed from v23](#bugs-fixed-from-v23) · [Testing](#testing) · [Limitations](#limitations-and-next-steps)

## What it does

- **Advises you live** in a browser dashboard: streaming bid/ask, BUY / SELL / BLOCKED / SKIP / WAIT per
  symbol with the reason, entry, stop-loss, take-profit, lot size, and the money at risk and to
  gain, all priced at the live quote by the same engine that trades.
- **Counts the trades you place by hand.** Trade the same MT5 account on your phone and the loss
  limits, open risk, reversal warnings, exposure and the review in R count those trades with no
  logging; the bot still only manages its own. For an account at another broker, type in its
  equity (and leverage) and every size and limit is worked out for it instead.
- **Calculator:** lot size from risk, pip value, money at stop and target, margin at any
  leverage, margin level, how far price can go against you before a margin call, the most lots
  your margin cap allows.
- **Pulls the news:** this week's economic calendar from a public feed; entries pause around
  high-impact events, the panel lists what is coming, and you are warned 30 minutes before.
- **Reads the whole market:** currency strength, how the instruments move together, your net
  exposure per currency across every open trade, yesterday's and this week's levels and pivots,
  how much of a normal day's range is used, the volatility regime, the sessions and the swap.
- **Trades MT5** (demo or live) on closed bars, across many symbols, resolving broker suffixes
  (`EURUSD` → `EURUSDm`). All timestamps are converted from server time to UTC, including the
  broker's daylight-saving changes, which are detected from its own history.
- **Four entry setups** behind one interface: EMA crossover, Donchian breakout, trend pullback,
  and break-and-retest. They share filters for higher-timeframe trend, ADX, MACD, RSI, support/resistance and
  Fibonacci levels, all measured in ATR, so gold and forex use the same settings.
- **Sizes every trade from risk.** The volume is chosen so that hitting the stop loses
  `risk_percent` of equity. Per-lot risk comes from the terminal's own profit calculator, and the
  bot never rounds up to the broker's minimum lot.
- **Layered guards:** session hours, news blackouts, daily and weekly loss limits, a drawdown
  limit, losing-streak pause, trades per day, positions per symbol and in total, total open risk,
  margin, spread vs ATR, and a `STOP`-file kill switch.
- **Writes its own trading plan.** `tradebot plan` states every rule in plain words, generated
  from the settings the engine reads, so the written plan and the executed plan cannot differ.
  `tradebot review` then reports closed trades in R and compares the bot's signals with trades
  placed by hand.
- **Manages open trades:** partial take-profit, breakeven, ATR trailing stop, exit on an opposite
  signal, and an optional time stop. Stops only ever tighten. While the dashboard runs, trades
  placed by hand get breakeven at +1R (`management.auto_breakeven`) and the 2 ATR trail from +1.5R
  (`management.auto_trailing`) too, if they have a stop and algo trading is on in the terminal.
  On the bot's signals the trail made no difference with the 2R target and tighter trails did
  worse (`scripts/research_trailing.py`); it locks in more only when a trade runs well past 1.5R.
  Also for hand-placed trades, each with its switch: a stop at 1.5 ATR for a trade still without
  one after two minutes (`auto_stop`), half closed at +1.5R (`auto_partial`), and the whole trade
  closed when a reversal sign is confirmed while it is in profit (`auto_reverse_exit`: better on
  13 of 16 setup/timeframe runs, all four setups on H1; `scripts/research_reversal_exit.py`).
- **Backtests realistically:** bid/ask spread, slippage, commission on actual lot size, gaps
  through stops, conservative intrabar fills, and multi-symbol portfolios.
- **Walk-forward optimisation** in parallel, reporting out-of-sample results only.
- **Self-contained HTML reports:** equity, drawdown, monthly returns, per-symbol results, R
  distribution, and walk-forward windows. They follow light/dark theme.
- **Journals** every order, close, stop move and rejection to SQLite.
- **Survives a terminal restart.** If MetaTrader 5 closes or restarts, the dashboard and the
  trading loop log the outage once, retry at 1, 2, 5, 10 and then every 30 seconds, and carry on
  when it is back. Startup also waits for a terminal that is still logging in.

## Quick start

```powershell
git clone https://github.com/fahadkatonvule24/tradingBot.git
cd tradingBot
python -m venv .venv; .venv\Scripts\activate
pip install -e ".[mt5]"            # the MetaTrader5 package is Windows-only

tradebot check                     # connect to the open MT5 terminal, show account, symbols, signals
tradebot ui                        # the whole advisor in one command: all four setups, trends, reversal warnings, alerts
tradebot ntfy                      # optional: push alerts through the free ntfy app (send only)
tradebot whatsapp                  # optional: alerts to your own WhatsApp (send only)
tradebot telegram                  # optional: alerts and commands through a Telegram bot
tradebot run --once                # one dry-run cycle: decisions are logged, nothing is sent
tradebot run                       # keep running (dry run); Ctrl+C stops after the current cycle

tradebot fetch --bars 60000        # history to data/: trading + trend timeframes, tick volume, symbol specs
tradebot backtest                  # backtest config/config.json on data/ -> reports/*.html
tradebot optimize --folds 4        # walk-forward optimisation -> out-of-sample report
tradebot journal --days 7          # what the bot did this week
tradebot plan --out PLAN.md        # the written trading plan, generated from the config
tradebot review --days 30          # closed trades in R: the bot's signals against your own calls
tradebot account Exness 500        # your own account: sizes are worked out from this equity
tradebot review --account Exness   # the trades you logged for it, in R
tradebot news                      # this week's economic calendar for your instruments
```

No MetaTrader terminal? `python scripts/make_demo_data.py --out demo-data`, then run
`tradebot backtest --data demo-data --timeframe M15 --symbols EURUSD GBPUSD` to try the research tools on
synthetic data.

Automatic entries are enabled for all 15 configured instruments: the five forex pairs, gold,
and the nine stock symbols in `config/config.json`. Each still needs a valid signal and must pass
the spread, session, news, position, margin and risk limits. US stocks and Anthropic enter, and
have their stops managed, only while the exchange takes orders: 09:30 to 16:00 New York time on
weekdays, without NYSE holidays and with its 13:00 early closes (`risk.market_hours: "us_stocks"`).
Exness quotes them from 06:00 New York, but refuses orders before the open. An instrument your
account does not offer is skipped. Set `overrides.<symbol>.risk.auto_trade` to `false` to keep an instrument on the
watchlist for signals only. `tradebot check` shows each instrument's **Auto entry** mode, and the
trading loop logs its automatic-entry and signals-only lists at startup. The default config
remains a dry run; a separate local config is used only when passed with `-c`.

Broker symbol names are resolved again after an account switch, and cached prices and trade
state are cleared. Disabled contracts and failed Market Watch selections no longer hide an
available broker suffix. A second live trading loop using the same journal folder is refused,
including when a paused loop tries to resume while another loop is active.

## Advisor dashboard

The dashboard fits one laptop screen: the account summary, a searchable watchlist, the chart and
the signal summary, with strength, news and positions below. **Signal & plan** puts the advice and
the chart beside the trade plan, with the trend, structure and zones, filters and backtest record
in tabs underneath (a dot on a tab means something there needs a look). The chart's overlays
(signals, join windows, support/resistance zones, day levels, trend strip, swings, patterns,
liquidity sweeps, supply/demand zones, fair value gaps) are switched on and off from its legend;
swings, patterns and fair value gaps start off. The browser remembers the tab and the overlays.

**Sweeps, zones and gaps** (`tradebot/smc.py`) are context, measured before they were trusted
(`scripts/research_smc.py`). Each of them lost money as an entry of its own on the stored history.
Two held up as warnings in both test periods, so they change the advice: no join is offered while
price is at an opposite supply or demand zone, or within three bars of a closed sweep against the
trend (joins there averaged -0.08R to -0.15R, against -0.03R to -0.06R elsewhere). The other
cautions (a sweep on the bar still forming, an opposite fair value gap) are shown with their
record, without changing the advice. `scripts/research_concepts.py` measured five more the same
way (time of day, premium/discount, change of character, engulfing and pin-bar confirmation, equal
highs and lows ahead); none separated good entries from bad in both periods, so the bot uses none
of them. Time of day included: trading all day was not measurably worse than set hours. The sidebar exposes every section and tool; **Ctrl+K** (or **Cmd+K**) finds pages,
tools and instruments. Use the instrument selector above any section to change symbols. Light,
dark and system themes are available from the header.

To preview the interface without MetaTrader or a trading account:

```powershell
python scripts/preview_dashboard.py
```

Open `http://127.0.0.1:8766/`. The preview uses synthetic prices and an in-memory account book;
orders are disabled and account changes disappear when the preview stops.

**Experimental entry-quality checks (off).** `strategy.use_signal_quality_filter` is `false` in
the supplied config: it was switched on for a day and then measured, below. When on, before an
entry, all four setups reject weak or opposing candle bodies, closes that retreat too
far from the directional extreme, exceptional price spikes (including gaps), and entries stretched
too far beyond the fast EMA. EMA cross and pullback also check for choppy price movement; breakout
and retest can emerge from consolidation, so that check does not apply to them. The dashboard's
filter checklist and blocked-trigger reason explain the decision, and `tradebot plan` lists the
configured thresholds.

The spike and extension limits use the **previous bar's ATR**; directional efficiency uses only
the preceding window. A large signal candle cannot inflate its own volatility baseline to pass.
Incomplete or invalid quality inputs block entry. These are configurable warning signs, not a
prediction that a trade will lose, a win probability, or evidence of a profitable edge; they also
reject some winning signals. Existing opposite-signal exits retain the original strategy filters;
the quality checks only gate new entries. The performance tables elsewhere in this README were
measured without it. Set `strategy.use_signal_quality_filter` to `true` to try it;
programmatic `StrategyParams` defaults keep it disabled too.

A fixed-threshold replay of the six configured instruments on H1, from 10 November 2024 to
10 November 2026, produced the following results with the same costs, risk limits and exit rules:

| Setup | Trades, off / on | Average trade in R, off / on |
|---|---:|---:|
| EMA cross | 132 / 26 | -0.265 / -0.245 |
| Breakout | 463 / 249 | -0.083 / -0.136 |
| Pullback | 680 / 364 | -0.166 / -0.147 |
| Retest | 428 / 220 | -0.151 / -0.158 |

**The replay does not establish better signal accuracy.** All four remained negative per trade;
breakout and retest worsened on that measure. Fewer entries can reduce total losses without
improving predictions. This is previously available history, not a fresh holdout; thresholds
were not tuned to these results. Reproduce with `python scripts/research_signal_quality.py`;
it saves metrics and rejection counts to `reports/signal-quality-H1.json` without connecting to MT5.

**Can the bot tell a false signal from a good one?** Measured three ways on the H1 history
(`scripts/research_false_signals.py`; a false signal is a trade that ends below zero R after the
engine's own exits and costs). Questions 2 and 3 use only the research years before November
2024, because the holdout after it has been used four times already.

1. *The checks above, signal by signal.* Each setup ran with them off and on; the trades they
   remove are compared with the trades they keep. A filter that spots false signals removes worse
   trades than it keeps.

   | Setup | Removed (2016 – Oct 2024) | Kept | Removed (Nov 2024 – Nov 2026) | Kept | Years the removed were worse |
   |---|---:|---:|---:|---:|---:|
   | EMA cross | 436 · −0.14R | 94 · −0.23R | 106 · −0.28R | 24 · −0.18R | 4 of 9 |
   | Breakout | 1,098 · +0.00R | 910 · −0.09R | 236 · +0.00R | 221 · −0.19R | 2 of 9 |
   | Pullback | 1,788 · −0.09R | 1,238 · −0.08R | 356 · −0.19R | 298 · −0.16R | 4 of 9 |
   | Break and retest | 1,108 · −0.05R | 898 · −0.11R | 226 · −0.24R | 182 · −0.14R | 3 of 9 |

   On breakout the checks throw away the better half in both periods. Elsewhere the difference
   changes sign between periods and from year to year. Their smaller total loss comes from taking
   50–80% fewer trades, which skipping signals at random would also do.
2. *Twelve single measurements at the signal bar*: how far past the level it closed, candle body
   and close position, candle size against ATR, stretch from EMA 10 and EMA 50, efficiency of the
   prior move, ADX, RSI, MACD, volatility regime and the London–New York overlap. Rank
   correlations with the result are between −0.06 and +0.09 for all but one. The exception,
   volatility regime (+0.13, same sign in all 9 years), does not change the average trade: from
   the quietest fifth of signals to the busiest, winners rise from 40% to 44% but the average
   winner shrinks from 1.41R to 1.23R, and the average trade stays between −0.03R and −0.15R with
   no trend.
3. *All twelve together*: a logistic regression refitted every year on the years before it, so
   each prediction is out of sample. AUC **0.496** over 6,723 trades (0.5 is a coin flip). The 30%
   it rated least likely to win averaged −0.09R; the rest averaged −0.08R.

None of the three picks out the losers in advance, which matches the earlier machine-learning
result (50.4% right on direction) and the breakout variants under
[Trading principles](#trading-principles). So the bot does not claim to: each buy or sell alert
instead ends with how often that setup has lost on that symbol, and the failed-break and reversal
warnings watch a trade after it is open, which is when a false break becomes visible.

**Automatic market assessment.** A separate analysis card combines the closed-bar trend, higher-timeframe
agreement, momentum, setup observations, volatility and available price levels into one explanation.
It lists supporting evidence and conflicts, then gives bullish and bearish conditions to watch with
confirmation and invalidation levels. You do not need to choose indicators to receive this assessment.
It describes measured conditions; its scenarios are hypotheses, without an estimated win probability.

The assessment is independent of account equity, execution permissions and account risk blockers.
Stale quotes, stale bars or incomplete inputs pause current scenarios. The data source and bar time
are shown, and a meaningful change in interpretation creates an activity entry and an alert through
the existing notification channels. Initial loading and ordinary price/timestamp updates are quiet.
`/analysis SYMBOL` retrieves the same assessment through the existing Telegram command handler.
This adds no orders and does not change the strategies or their documented backtest results.

`tradebot ui` connects to the open MT5 terminal and serves a dashboard on `127.0.0.1`. For every
symbol, once a second:

| It shows | Where it comes from |
|---|---|
| Live bid / ask, spread, recent price | `symbol_info_tick` and closed bars, polled on one MT5 thread |
| **BUY / SELL**: take it now, valid until the forming bar closes | a signal on the last closed bar that passes every engine guard |
| **SKIP**: signal, but don't | the guard that blocks it (session, news, loss limit, spread, minimum lot, margin…) |
| **BLOCKED**: a trigger fired on the last bar and a strategy filter rejected it | the filter that rejected it (trend, ADX, MACD, RSI, room to S/R); the plan shows that trade in case you take it yourself, and the headline keeps the trigger visible for the next 12 bars |
| **WAIT**: no setup, and what would trigger one | the strategy's trigger, e.g. "a sell triggers when EMA 10 closes below EMA 50 (now 0.93 ATR above it)" |
| Entry, stop-loss, take-profit, lot size | `Engine.plan_entry`, the bot's own sizing, priced at the live ask (buy) or bid (sell) |
| Loss if stopped, profit at target | volume × distance × the terminal's own per-tick value |
| Filter checklist | the strategy's filters (trend, ADX, MACD, RSI, room to S/R) on the last closed bar |
| Chart | the last 120 bars plus the forming one, both EMAs, profit and loss zones, live price; hover for OHLC |
| Track record | a backtest of the current config on your downloaded history, per symbol |
| Timeframe switch | trade on M5, M15, M30, H1 or H4; the two higher timeframes that set the direction are chosen automatically (M5/M15/M30 → H1 + H4, H1 → H4 + D1, H4 → D1) and each gets its own row in the checklist |
| All four setups at once | EMA cross, Breakout, Pullback and Break & retest are all watched on every instrument, from the same bars, with no extra requests to the terminal. Each has a tile with its verdict on the last closed bar; a signal from any of them is announced with its priced plan and named in the watchlist and the tab title |
| Trend | for each instrument: uptrend, downtrend or none on the trading timeframe, how old and how strong it is, and whether it is intact, weakening or likely ending, with the higher timeframes beside it. Alerts when a trend starts, weakens and is likely ending |
| Setup switch | which setup the detail view (plan, checklist, chart levels, track record) shows on every instrument: EMA cross, Breakout, Pullback or Break & retest, or **As configured** when the config gives some instruments their own setup. Each says what it is waiting for, with the price level where it has one ("price broke above 1.0995; a buy triggers when a bar comes back to that level and closes above it again") |
| Buy / Sell switch on the plan | the same priced plan for either side, so a trade you choose yourself gets the bot's sizing, stop and target |
| Latest trigger | the most recent entry trigger in the last 12 bars, the filter that blocked it if one did, and how far price has moved since |
| Recent signals | the last triggers across all symbols, taken or blocked, and which came first afterwards, the stop or the target, plus how often signals occur (about two a week, each live for one bar, so WAIT is the normal state) |
| Early warning | while a bar is still forming: the exact close that would fire the next trigger, and a "forming, not confirmed" notice when the current price would fire it, with whether the filters would pass or block it |
| Risk limits | how much of each account limit is used: today's and this week's result against the loss limits, drawdown, losses in a row, entries today, open risk. The tile of a limit that is hit turns red and the card says entries are stopped |
| Plan | the written trading plan for the setup and timeframe on screen, with what those rules measured on your stored history |
| Reversal warning | for every open position on a watched instrument, the bot's or one you opened by hand: the signs that the market has turned against it since it opened. One sign is a warning; an opposite signal or two signs together is "get out". Shown in a strip across the top, on the instrument's card, under the position, in the tab title and in the alerts. A sign the bar still forming would add is shown as "forming, not confirmed" |
| Alerts | browser notifications, and optionally Telegram messages to your phone, when a signal fires, a filter blocks a trigger, a trigger is forming, an order is placed, the market turns against an open position, or a trend starts, weakens or is likely ending. Signals from all four setups are sent, not only the one on screen |
| Open positions, activity | every position on the account (bot or manual), and advice changes as they happen |

**The advice is the bot's own decision, not a separate indicator.** `advisor.py` calls the same
`Engine.entry_blocker` and `Engine.plan_entry` that `Engine.on_bar` runs before every live or
backtest trade, so the side, lot size, stop and target on the page are exactly the order
`tradebot run --live` would send at that moment.

**Top-down analysis, on any timeframe.** With `"trend_timeframes": "auto"` (the default), the
trend filter reads the two timeframes above the one you trade and takes a trigger only when both
agree with it. Pick the trading timeframe on the dashboard, or with `--timeframe` on `ui`, `run`,
`fetch` and `backtest`; a list such as `["M30", "H4"]` sets the ladder by hand. Lower timeframes
do give more signals, and worse ones. Default parameters, nothing tuned, six instruments:

| Trading timeframe · direction from | Trades a week | Winners | Average trade | Return | Period |
|---|---:|---:|---:|---:|---|
| H1 · H4 only (the earlier default) | 1.6 | 39.3% | −0.13R | −25.3% | Aug 2016 – Nov 2026 |
| H1 · H4 + D1 | 1.2 | 37.1% | −0.18R | −26.1% | Aug 2016 – Nov 2026 |
| M30 · H1 + H4 | 2.3 | 36.5% | −0.24R | −29.9% | Oct 2021 – Nov 2026 |
| M15 · H1 + H4 | 3.4 | 34.5% | −0.27R | −26.1% | Apr 2024 – Nov 2026 |
| M5 · H1 + H4 | 6.1 | 36.3% | −0.17R | −11.1% | Dec 2025 – Nov 2026 |

Over the one window they all share (Feb – Nov 2026) the order is the same: −2.5% on H1, −8.9% on
M30, −8.2% on M15, −12.3% on M5. A second higher timeframe removes trades without improving the
ones that remain, and a 5-minute stop is only two or three pips, so the spread is a large share
of every trade. The switch is there because a person's own read is often on a lower timeframe;
it is not a way to make these triggers profitable.

**Earlier entries: breakout, pullback, break and retest.** The EMA crossover is a late entry by
design: it fires after a move is under way. The setup switch (or `--strategy`, or
`"strategy": {"name": ...}`) changes what the advisor watches: `breakout` for the bar that starts
a move, `pullback` for a dip inside a trend, and `retest` for a broken level that price comes
back to and holds. They fire four to ten times as often, and they lose too. Default parameters,
nothing tuned, six instruments, direction from the automatic two-timeframe ladder:

| Timeframe (period) | Setup | Trades a week | Winners | Average trade | Return |
|---|---|---:|---:|---:|---:|
| H1 (Aug 2016 – Nov 2026) | ema_cross | 1.2 | 37.1% | −0.18R | −26.1% |
| | breakout | 4.7 | 42.1% | −0.04R | −26.5% |
| | pullback | 6.9 | 40.3% | −0.11R | −62.5% |
| | retest | 4.5 | 39.4% | −0.10R | −47.4% |
| M30 (Oct 2021 – Nov 2026) | ema_cross | 2.3 | 36.5% | −0.24R | −29.9% |
| | breakout | 9.3 | 39.5% | −0.13R | −54.2% |
| | pullback | 10.2 | 40.0% | −0.13R | −58.0% |
| | retest | 9.2 | 38.2% | −0.17R | −63.3% |
| M15 (Apr 2024 – Nov 2026) | ema_cross | 3.4 | 34.5% | −0.27R | −26.1% |
| | breakout | 12.0 | 36.4% | −0.19R | −51.8% |
| | pullback | 17.4 | 39.2% | −0.15R | −57.6% |
| | retest | 12.0 | 36.1% | −0.22R | −58.7% |
| M5 (Dec 2025 – Nov 2026) | ema_cross | 6.1 | 36.3% | −0.17R | −11.1% |
| | breakout | 15.1 | 36.7% | −0.16R | −25.5% |
| | pullback | 20.2 | 36.5% | −0.19R | −34.8% |
| | retest | 15.3 | 35.7% | −0.17R | −26.0% |

The retest rules (20-bar level, 12 bars to come back, within 0.25 ATR, a close back in the
direction of the break) were written down before this was run and have not been changed since.
A setup that trades more often with the same negative average loses more in total, which is why
the bigger returns column belongs to the busier setups. As with the timeframe switch, this is
for following a setup you already read on the chart, with the bot's sizing and stop.

**False breakouts.** On this history they are the normal case: 60% of H1 breakouts (65% on M15)
close back through the level within 12 bars, and after a breakout price moves one ATR further
in the same direction first only 48% of the time. Trading with the higher-timeframe trend makes
no difference to that (60.1% against 60.6%). Five ways of dealing with it were written down and
run once each, on six instruments with default risk settings:

| Approach | H1 trades a week | H1 average trade | M15 average trade |
|---|---:|---:|---:|
| Any close through the level (the rule until v3.7) | 6.1 | −0.09R | −0.24R |
| **Decisive close:** at least 0.25 ATR beyond the level | 4.7 | −0.04R | −0.19R |
| Two closes: enter only if the next bar also closes beyond it | 5.7 | −0.06R | −0.21R |
| Break and retest | 4.5 | −0.10R | −0.22R |
| Trade against the failed break | 3.7 | −0.13R | −0.11R |

The decisive close is now the breakout rule (`"breakout_min_atr": 0.25`; `0` restores the old
one). It was the best of five, so part of its lead may be luck; on M30 and M5, which were not
part of the comparison, it improved the average trade from −0.16R to −0.13R and from −0.21R to
−0.16R. It is still a losing rule. The first close through the level must itself be decisive: a
marginal poke followed by a strong bar is not treated as a fresh break.

Because most breaks fail, the advisor says so when one does. For the breakout and retest setups
a broken level is watched for 12 bars, and a close back through it puts "Break failed" on the
card and the watchlist, in the activity feed, and in the Telegram and browser alerts. If a later
bar closes beyond the level again, the card calls the break unsettled instead. Stops are
unchanged: 1.5 ATR, moved to breakeven at +1R, already bound what a false break costs.

**Knowing early without acting early.** The bot decides only when a bar closes: a crossover on a
bar that is still forming can appear and vanish several times, and v23 traded those. The early
warning gives the heads-up without breaking that rule. For the EMA crossover the closing price
that produces a cross has a closed form (with α = 2/(n+1), solve α_f·c + (1−α_f)·fast = α_s·c +
(1−α_s)·slow for c); checked against 48,000 real H1 bars across the six symbols, that level
predicts the actual trigger on 100% of them. The forming notice re-runs the real signal code with
the forming bar appended at the current price, so it also says which filter would block it. It is
labelled unconfirmed everywhere, and nothing is ever traded from it.

**Alerts to your phone.** Three channels, any mix of them:

- **ntfy**, a free push-notification app (Android and iPhone), no account needed. `tradebot ntfy`
  makes a private topic name such as `tradebot-3f9a1c7b2e4d`, saves it to `.env` as
  `NTFY_TOPIC`, and says how to subscribe to it in the app; run it again for a test notification.
  The first line of an alert is the notification's title, and its kind sets how loud it is:
  signals, failed breaks and get-out warnings vibrate and pop up; news, orders, assumed closes and
  the heartbeat make the usual sound; with `"alerts": {"ntfy": "all"}` the rest arrive silently.
  The default is the important alerts only, because ntfy.sh allows 250 messages a day per sender.
  Anyone who knows the topic name can read the alerts, so it is random and kept out of the
  committed config; `NTFY_SERVER` and `NTFY_TOKEN` point it at your own server or a reserved,
  protected topic.
- **WhatsApp**, through [CallMeBot](https://www.callmebot.com/blog/free-api-whatsapp-messages/)'s
  free API for messages to your own number. Save their number in your contacts, send it the
  activation message from WhatsApp, and it replies with an API key. Put `WHATSAPP_PHONE` (with the
  country code, e.g. `+256700123456`) and `WHATSAPP_APIKEY` in `.env`, then `tradebot whatsapp`
  sends a test message (`--sample` sends a past alert in the real format, marked as a sample).
  For several numbers, activate each one and list them comma-separated,
  each key in the same position as its number (`WHATSAPP_PHONE=+256700123456,+256770654321`,
  `WHATSAPP_APIKEY=1234567,7654321`); every number gets every alert, and a count that does not
  match turns WhatsApp off with a warning instead of pairing a number with the wrong key. It only
  sends, so the commands below need Telegram. By default it carries
  only what is worth acting on: a confirmed buy or sell, a failed break, get-out and reversal
  warnings and assumed closes on open positions, the news warning, and orders
  (`"alerts": {"whatsapp": "important"}`; `"all"` adds forming and blocked triggers, trend and
  analysis changes). Alerts that arrive together go out as one message, spaced out, because the
  free service does not publish its limits. The text passes through CallMeBot's server.
  **Delivery is not guaranteed:** on 16 Nov 2026 CallMeBot answered "Message queued" to every
  request for two activated numbers and delivered none, so do not rely on it alone.
- **Telegram**: `tradebot telegram` walks through creating a bot and sends a test message. The
  token and chat id live in `.env` (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`). It gets every
  alert and answers the commands listed under [Your own accounts](#your-own-accounts).

**Heartbeat.** `tradebot ui` sends STARTED when it starts, STOPPED when it is closed, and RUNNING
once each trading day when the session opens. A trading day without RUNNING means alerts are not
running (the PC was off or asleep, or the dashboard stopped without saying so).

Only the setup on screen sends signals. The other three still show theirs on their tiles;
`"alerts": {"other_setups": true}` announces them too. Each setup loses on its own, and four of
them send about four times the signals. Every size in an alert names the account it was worked
out for: on the terminal's demo account that is 100,000 USD, not your own equity (see
[Your own accounts](#your-own-accounts)). Every buy or sell alert ends with how often that setup
has lost on that symbol in the stored history. Credentials never go in the committed config,
messages go out from background threads so a slow network cannot delay anything, failures are
logged and dropped, and neither the token nor the API key is ever written to a log.

**The bot is the risk manager; direction can be yours.** The research below found no rule that
predicts direction, and the filters block winners as readily as losers (removing the trend filter
doubles the trade count and the loss). So the dashboard does not insist on its own side: you can
switch the plan to Buy or Sell and place that trade on your own call. A manual trade needs no
strategy signal, but it still passes the trading gate and every entry guard (session, news, loss
limits, position limits, spread), is sized by the engine from your risk settings, and is sent
with its stop and target. It is journalled as manual and tagged `tradebot manual` in MT5, so your
own trades can be told apart from the bot's.

**Placing the trade from the page** is possible but off by default. The button is enabled only
when every [safety gate](#safety-model) is open and the dashboard was started with
`tradebot ui --live`. On click the server does not trust the browser: it re-reads the quote,
recomputes the advice and every guard, and sends only the engine's size for that side, with the
stop and target attached. A bot trade is refused unless it still matches the current advice. Breakeven, partial profit and trailing apply while `tradebot run` manages
the position.

Two more buttons appear while orders are on, both your own call (tagged `tradebot manual`,
counted in the loss limits and with your trades in the review, never in the bot's scorecard):

- **Get in**, on the plan card, when the bot skipped its own signal or join because a limit held
  it back (a loss limit, positions, open risk, margin, hours, news, the spread). It sends the trade
  anyway at the configured risk per trade; the confirm dialog names the limit it sets aside. It
  never trades through stale data, and a trade that cannot be sized at your risk per trade is
  refused.
- **Duplicate**, on each of the bot's open trades: a second trade with the same lots, stop and
  target, at the live price, instead of placing the same trade again by hand on the phone. The
  dialog shows what the copy risks; a stop or target too close to the price to copy is refused.

Neither sets aside the `STOP` file: while it exists, no order leaves the dashboard. An order that
waits more than 20 seconds for the terminal is cancelled if it has not been sent yet, or reported as
"not confirmed yet" if it is being sent; the same confirmation sent twice places one order. A
partial fill is reported as a fill of the lots that were filled.

**Who manages trades placed by hand, and sends reversal warnings.** Exactly one process at a time,
decided by operating-system file locks next to the journal (`tradebot.*.lock`): the dashboard while
it runs; otherwise the trading loop (`tradebot run --live`), which takes over within one of its
cycles. Restarting the dashboard therefore leaves no trade unmanaged, and running both no longer
sends every reversal warning twice. A second dashboard sends nothing to the phone. The bot's own
trades (and Get in and Duplicate orders) are the live trading loop's; with no loop running, the
dashboard protects them the same way.

**The local server is hardened, because one endpoint can place orders.** It binds to `127.0.0.1`
only, rejects foreign `Host` headers (DNS rebinding), requires a per-session token sent in a
custom header (other sites can neither read it nor send it cross-origin), refuses cross-origin
`Origin`s, and serves a strict Content-Security-Policy. All page content is inserted as text,
never as HTML, and the server uses only the standard library.

![Dashboard in dark mode](docs/screenshots/dashboard-dark.png)

## Trades you place on the phone

Log in to the same MT5 account on the phone as in the terminal the dashboard reads, and nothing
has to be entered by hand: a position opened in the MetaTrader 5 app is on the account, so the
terminal sees it within a second. With `"account_trades": true` (the default, under `risk`), every
trade on the account counts, not only the bot's:

| What | Counts the trades you place by hand |
|---|---|
| Daily and weekly loss limits, drawdown, losing streak, trades per day | yes, from the account's deal history (deposits and withdrawals are not trades and are left out) |
| Positions per symbol and in total, open risk | yes |
| Reversal warning, exposure per currency, correlation warnings | yes |
| Closed trades and the review in R, on the dashboard and in `tradebot review` | yes, labelled your own calls against the bot's (the bot's carry its magic number) |
| Stop added, breakeven, partial profit, trailing stop, closing in profit on a confirmed reversal sign | yes, by the dashboard while `tradebot ui` runs, each with its `management.auto_*` switch and algo trading allowed in the terminal (`tradebot/protect.py`). The trading loop (`tradebot run`) manages only the bot's own positions |
| Closing on an opposite signal | **no**: only the bot's own positions |

R needs the stop a trade opened with. The phone usually sends it with the order; when a stop is
added after the trade opened, the first stop the dashboard sees is kept as the trade's 1R (in the
journal file), so moving it to breakeven later does not change what the trade risked.

`"account_trades": false` goes back to counting only the bot's own trades.

### Using it with Exness

Exness accounts are MT5 accounts (unless one was opened as MT4), and the Exness Trade app trades
the same account, so log the terminal in to it and every trade placed from the phone is counted:

1. In MetaTrader 5, **File → Login to Trade Account** with the account number, its trading
   password and its server, as shown for that account in the Exness Personal Area. (Or install
   Exness's own MT5 terminal next to this one and put its `terminal64.exe` in `MT5_PATH` in
   `.env`, so the MetaQuotes demo terminal stays as it is.)
2. `tradebot check` shows the account, server time and symbols; `tradebot ui` as before.

Exness symbol names carry the account type (`EURUSDm` on Standard, `EURUSDc` on Standard Cent,
`EURUSD` on Pro, Raw Spread and Zero); the configured `EURUSD` is matched to the one the account
has. Unlimited leverage, which MT5 reports as about two billion, is shown as unlimited, and the
calculator then uses the terminal's own margin figure. A real-money account is refused for orders
unless `allow_real_account` is set and every other gate is open, so the dashboard stays advice
only. An MT4 account cannot be read by the MetaTrader5 package: use the book for it.

## Your own accounts

For an account the terminal cannot see, at a broker the terminal is not logged in to, the book
(`book.py`, kept in the journal's SQLite file) holds the money instead. The terminal is then a
price feed.

**Type in the equity; the rest follows from it.** Under **Account** on the dashboard, with
`tradebot account NAME EQUITY [--leverage 500]`, or with `/equity NAME AMOUNT [lev=500]` in
Telegram, add each account with the equity its broker's app shows. Saving an account never
switches to it: the MT5 account stays in use until you choose **Use** (`/use NAME`,
`tradebot account NAME --use`). While one of them is in use, the engine asks the book for the
account, the open positions and the deal history, and the terminal only for prices and the
profit calculator. Nothing else changes: the lot size, the stop and target, the money at risk,
the daily and weekly loss limits, the losing-streak pause, the open-risk cap, the reversal
warning and the review are the same code, now answering for your account. With 2,000 in equity
and the default 0.25% risk, a 15-pip EURUSD stop gives 0.03 lots; on the 10,000 demo account it
gave 0.16. When an account is too small for the terminal's smallest lot at that stop, the plan
says so instead of rounding up. If your broker takes smaller lots, set its minimum lot and lot
step on the account.

**Several accounts at once.** The plan card and every signal alert give the size at each of
your accounts, not only the one in use:

```
BUY · EURUSDm
Buy EURUSDm now. …
BUY plan: entry ~1.10012 · SL 1.09862 · TP 1.10312 · 0.03 lots · risk 4.50 · target 9.00 · Exness
XM (8,000.00 USD): 0.13 lots · risk 19.50 USD
Tiny (300.00 USD): no size (minimum lot would risk 0.50% (> 0.25%))
```

**Log what you placed.** "I placed this BUY: log it" on the plan card, or `/took EURUSD buy` in
Telegram, records the trade with the plan's size, entry, stop and target; change any of them to
what you actually got (`/took EURUSD buy lots=0.02 entry=1.0851 sl=1.0821`). A trade logged while
the bot was signalling that side is marked as a bot signal, any other as your own call, so the
review can tell the two apart. Move the stop with `/sl ID PRICE`, close with `/close ID [PRICE]
[pnl=AMOUNT]`, or use the buttons on the open-positions table. When the terminal's price crosses
a logged stop or target, including on bars that closed while the dashboard was off, the trade is
closed in the book at that level, marked **assumed**, and you get an alert: another broker's
prices differ by a fraction of a pip, so set the result your broker shows, or reopen the trade if
it is still running.

**Nothing here places an order.** While one of your accounts is in use the order button is gone
and the server refuses orders, whatever the config says; the broker interface answers every
order with "place the order at your broker, then log it here". The Telegram commands can change
the book and read the analysis; none can trade, and only the chat set in `TELEGRAM_CHAT_ID` is
answered. Commands sent while the dashboard was off are skipped, not replayed.

| Telegram command | What it does |
|---|---|
| `/calc SYMBOL buy\|sell [risk=] [lots=] [sl=] [pips=] [tp=] [lev=] [equity=]` | the [calculator](#calculator): size, margin, leverage |
| `/news` | high- and medium-impact news for your instruments in the next 36 hours |
| `/plan SYMBOL buy\|sell` · `/analysis SYMBOL` | entry, stop, target and the size at each account; the market assessment |
| `/status` · `/market` · `/levels SYMBOL` | loss limits and open risk; strength, exposure and sessions; the day's levels |
| `/equity NAME AMOUNT [lev=]` · `/use NAME` · `/accounts` | add one of your accounts or set its equity and leverage; choose the one everything is worked out for (`/use mt5` for the terminal's) |
| `/took SYMBOL buy\|sell [lots=] [entry=] [sl=] [tp=] [account=]` | log a trade placed at another broker (with one account of your own, it goes there) |
| `/close ID [PRICE] [pnl=]` · `/sl ID PRICE` · `/tp ID PRICE` · `/open` | keep the book in step with that broker |

Three limits belong next to this. Amounts are in the terminal account's currency, because that
is what its profit calculator answers in, so keep the terminal on an account in the same currency
as yours. Margin is checked for one of your accounts only once its leverage is set: notional
value ÷ leverage, against `max_margin_percent`. And the book only knows what you log: for these
accounts, a trade placed and never logged is invisible to the limits.

## Market panel

One instrument's chart does not show what the rest of the market is doing. The panel below the
plan shows it, from the bars the advisor already holds, so it costs no extra requests to the
terminal:

| It shows | How |
|---|---|
| Currency strength | each currency's move over the last day (and week), averaged over its instruments and scaled by ATR × √bars, so gold and EURUSD count the same |
| Your exposure | net long or short amount per currency across every open position (the bot's, yours on MT5, or your book), in money and as a share of equity, and which positions make it up |
| Overlap | on the plan card: "a buy here adds to a bet you already hold: short USD (your buy EURUSD, sell USDJPY)" |
| Correlation | of returns over the last 500 bars; open positions on instruments above ±0.7 are named as one larger position, or as cancelling out |
| Levels | previous day and week high and low, previous close, today's open, this week's range, classic floor pivots (also drawn on the chart), each with its distance in ATR |
| Range used | today's range against the 20-day average daily range |
| Volatility | where the current ATR sits among its last 500 values: quiet, normal or busy |
| Sessions, swap | which sessions are open; overnight financing per lot at the terminal's broker, with the triple-swap day |
| Every instrument | one row each: advice, trend and its state, higher timeframes, RSI, ADX, volatility, range used, nearest level |

**What two of these are worth as forecasts.** Both are commonly read as one, so both were
measured with the panel's own code on the six default instruments, H1, March 2017 to November
2026, with the questions written down first (`scripts/research_market_context.py`):

| Question | Answer |
|---|---|
| Does the next day go the way the last day's currency strength favoured? | 49.7% of 14,142 samples; +0.03 ATR on average (t = 0.9). With a strong lean (difference ≥ 1): 50.3% and +0.09 ATR (t = 1.8), about one pip on EURUSD, which is less than the cost of the trade, and the six instruments are sampled at the same moments and share the dollar, so the samples are not independent |
| Is a day that has used its average range by 13:00 UTC done? | No. It extended its range by a further 32% of the average on average (median 19%), against 35% (median 26%) for days under half used; 42% of them went on by a quarter of a range or more |

So the panel describes what has happened and what you are exposed to. Strength pointed the same
way as the instrument's own last day 91% of the time: it is mostly that day's move, restated
across currencies.

## Calculator

**Calculator** on the dashboard (or `/calc` on the phone) works out a trade before you place it,
for any watched instrument, from the terminal's own contract sizes and profit calculator:

| You give (all optional but the instrument and side) | It answers |
|---|---|
| entry (default: the live price) | lot size, from the risk or as given, floored to the lot step |
| stop as a price or in pips (default: the bot's plan) | value of a pip; money lost at the stop and made at the target, R:R |
| target (default: the plan's) | what the position controls (notional value) |
| risk as % of equity, as money, or the lots (default: the config's 0.25%) | margin at the leverage chosen, and the terminal's own figure at the account's leverage |
| equity (default: the account's) | margin as a share of equity, with what is already open; free margin and margin level after |
| leverage 1:10 to 1:2000 (default: the account's) | the most lots on all free margin and within your margin cap |
| margin cap (default: `max_margin_percent`, 30%) | how many pips against you before the margin level reaches 100% (margin call) and 50% (a common stop-out) |
| | the leverage the account would be running at, and the same size at every leverage |

It warns when the smallest lot would risk more than asked (instead of rounding up), when the
margin would pass your cap, when there is not enough free margin, and when lots you typed risk
more than the config's per-trade risk. Margin at a chosen leverage is notional ÷ leverage, which
is how brokers margin forex and leveraged CFDs; some set gold, indices or crypto at less leverage
than forex, or a fixed margin per lot, so pick that instrument's leverage, or compare with the
terminal's figure, which is always shown beside it.

## News

The week's economic calendar is pulled from the public ForexFactory feed
(`schedule.news_feed` in `config/config.json`; empty turns it off). The dashboard and the trading
loop download it in the background, at start-up unless the last download is under four hours
old and every four hours after, into `news_feed.json`
(not committed); a failed download keeps the last one and is retried after half an hour, and a
backtest never touches the network. The request carries no account data. Your own
`config/news_blackouts.json` is still read beside it, for anything the feed does not carry.

- **Entries pause** from `news_before_minutes` (30) before to `news_after_minutes` (30) after
  each high-impact event for an instrument's currencies, as they did with the hand-kept file.
- **The market panel lists** the next 36 hours of high- and medium-impact events for your
  instruments: time, currency, forecast and previous figure, the instruments it touches, and
  whether entries are paused now.
- **One alert** (Telegram and the activity feed) when a high-impact event is 30 minutes away, with the
  pause window: "USD Non-Farm Employment Change in 25 min (13:30 UTC, high impact; forecast 120K,
  previous 98K). New entries are paused on EURUSDm from 13:00 to 14:00 UTC."
- `tradebot news [--days 2] [--all]` downloads and lists it in the terminal; `/news` on the phone.

## Safety model

Real orders are sent only when **every** gate is open. `tradebot check` tells you which one is closed.

| Gate | Default | Where |
|---|---|---|
| `dry_run` | `true` | `config/config.json` |
| `live_trading_confirm` | `false` | `config/config.json` |
| `--live` flag | off | command line |
| Algo trading enabled in the terminal | your choice | MT5 → Tools → Options → Expert Advisors |
| `allow_real_account` (only for real-money accounts) | `false` | `config/config.json` |

While running live, creating a file named `STOP` in the working directory pauses new entries
immediately, the dashboard's Get in and Duplicate included; open positions keep being managed. The
trading loop checks the account before every instrument: if the terminal is switched to another
account (a real-money one without `allow_real_account`), its orders stop until the check passes
again, and the loss limits are read from the new account's history. Credentials come from environment variables or a
`.env` file, never from the committed config.

![tradebot check against a MetaQuotes demo terminal](docs/screenshots/terminal-check.png)

> `tradebot check` against a live MetaQuotes demo terminal: server-time detection, symbol
> resolution, closed-bar signals for six instruments, and the trading gate refusing live orders
> because the config is in dry-run mode.

## Architecture

```mermaid
flowchart LR
    subgraph Data
      MT5[(MT5 terminal)] -->|closed bars, UTC| A[MT5Broker]
      CSV[(CSV history)] --> S[SimBroker]
    end
    A --> F[strategy.compute_signals<br/>vectorised, causal]
    CSV --> F
    F --> E[Engine.on_bar<br/>manage → guards → size → order]
    E -->|live / dry run| A
    E -->|backtest| S
    S --> R[metrics · walk-forward · HTML report]
    E --> J[(SQLite journal)]
    A -->|live ticks| U[advisor + ui<br/>127.0.0.1 dashboard]
    E -.->|entry_blocker · plan_entry| U
```

The engine only knows the `Broker` interface. Live trading passes it `MT5Broker`; the backtester
passes `SimBroker` and replays history bar by bar. **The backtest therefore exercises the same
guards, sizing and trade management as production.** v23 had a separate, simplified backtest
signal, so its results did not describe the live bot.

```
src/tradebot/
├── config.py        typed config sections, validation, per-symbol overrides
├── indicators.py    EMA, Wilder RSI/ATR/ADX, MACD (vectorised)
├── strategy.py      signals for ema_cross / breakout / pullback / retest + filters
├── engine.py        the decision procedure (live and backtest)
├── advisor.py       BUY/SELL/WAIT/SKIP advice and a priced trade plan, from the engine's own checks
├── ui/              `tradebot ui`: MT5 worker thread, hardened local server, dashboard page;
│                    track.py measures each setup's record in a separate low-priority process
├── risk.py          position sizing, portfolio risk, drawdown
├── plan.py          the written trading plan, generated from the config
├── review.py        closed trades in R, bot signals against manual trades
├── reversal.py      the signs that the market has turned against an open position
├── trend.py         the trend on the trading timeframe and the signs that it is ending
├── broker/          base.py (interface) · mt5.py (MetaTrader 5) · sim.py (simulation)
├── backtest.py      multi-symbol event-driven backtester
├── optimize.py      grid search + walk-forward (process pool)
├── metrics.py       Sharpe/Sortino/Calmar, drawdown, profit factor, R-multiples
├── report/          HTML report with inline SVG charts
├── live.py          trading loop and the live-trading gate
├── news.py          economic calendar: the weekly feed, your own file, blackouts
├── calculator.py    position size, margin and leverage
├── journal.py       SQLite journal
├── book.py          your own accounts and logged trades; BookBroker answers the engine from them
├── market.py        currency strength, correlation, exposure, levels, volatility, sessions
├── notify.py        phone alerts: ntfy and WhatsApp (send only), Telegram (and commands)
└── cli.py           `tradebot` command
```

## Strategies

| `strategy.name` | Entry trigger | Filters applied |
|---|---|---|
| `ema_cross` | fast EMA crosses slow EMA | trend, ADX, MACD, RSI cap, S/R room, Fibonacci (opt.) |
| `breakout` | a bar closes at least `breakout_min_atr` (0.25) ATR beyond the prior N-bar high/low. Options, both off by default: `use_volume_filter` (the break bar has 1.5 times the average tick volume of the previous 20 bars) and `breakout_target: "measured"` (target = the height of the range, projected from the level) | trend, ADX, MACD, volume (opt.) |
| `pullback` | fast RSI recovers from an extreme in the direction of the trend | trend, ADX, S/R room, Fibonacci (opt.) |
| `retest` | a bar closes through the prior N-bar high/low; within the next 12 bars price returns to that level and a bar closes away from it again. A close back through the level cancels the setup | trend, ADX |

The trend filter uses only **completed** higher-timeframe bars (for example the last closed H4
bar). A test asserts that every signal is causal: computing on any prefix of the data gives
exactly the same rows as computing on the full history.

## Risk and trade management

- **Stops and targets in ATR multiples** (`sl_atr`, `tp_atr`), never closer than the broker's
  stop level plus spread.
- **Sizing:** `volume = equity × risk_percent ÷ loss per lot at the stop`. If that is below the
  broker's minimum lot, the trade is **skipped**; v23 silently traded the minimum and over-risked.
- **Portfolio caps:** total open risk (counting the current stops of open positions), margin
  usage, positions per symbol and in total.
- **Loss limits** use realized P/L net of commission, swap and fees, per UTC day and ISO week, of
  every trade on the account, those placed by hand included (`account_trades`, see
  [Trades you place on the phone](#trades-you-place-on-the-phone)). A losing streak pauses
  entries until the next UTC day.
- **Drawdown limit:** live trading stops opening positions while the bot's realised result is
  `max_drawdown_percent` (10%) below its highest point of the last `drawdown_days` (365). Like
  the loss limits it is rebuilt from the broker's deal history, so it needs no saved state and
  survives restarts. Backtests do not apply it, because it would only cut a research run short;
  `tradebot backtest` prints the date it would have tripped.
- **Management is in R** (multiples of the initial risk): partial profit at `partial_tp_r`,
  breakeven plus a buffer at `breakeven_r`, ATR trail from `trail_start_r`. The original stop and
  volume are read back from MT5 order history, so this survives restarts.
- **Reversal warning:** `tradebot ui` and `tradebot run` watch every open position on a watched
  instrument and say when the market turns against it (see
  [Reversal warning](#reversal-warning-for-an-open-position-v311)). It is a warning to a person.
  The bot's own exits are unchanged: the stop, the target, the trail and an opposite signal.
- **Joining a missed signal** (`strategy.auto_join`, off by default): the bot enters a signal it
  did not take (a guard or limit stopped it) up to `join_window_bars` (5) bars late, once, exactly
  where the dashboard shows JOIN: the newest trigger was that taken signal, the trend still runs
  its way, the break it rode has not failed, and price is not at an opposite supply/demand zone or
  just after a closed sweep against it. Measured through the engine on the stored H1 history
  (configured setups, 2017-2022 / 2023-2026): 111 / 64 joins averaged +0.02R / -0.03R a trade, at
  0.25% risk 88 / 47 joins -0.01R / -0.12R, and the account totals were worse in three of the four
  runs and flat in one. Not consistent in both periods, so it is not an edge. (Before 2026-11-19
  the backtester skipped the bars a join happens on, so a backtest showed no joins at all.)

## Trading principles

The usual list of trading principles (protect capital, follow a written plan, stay disciplined,
cut losses and let profits run, be selective, journal and review, trade with the larger trend),
checked against what the code does. One rule and two commands were missing and were added in
v3.9; one principle cannot be met by code.

| Principle | How the bot handles it |
|---|---|
| Risk a small fixed share per trade | 0.25% of equity by default; the config refuses more than 5% |
| Stop where the idea is wrong; size from the stop | stop at 1.5 ATR sent with the order; `volume = equity × risk ÷ loss per lot at the stop`, rounded down, skipped if the minimum lot would over-risk |
| Reward at least twice the risk | target at 3 ATR against a 1.5 ATR stop |
| Daily and weekly loss limits | 2% and 5%, from the broker's deal history |
| Maximum drawdown rule | **added in v3.9:** entries stop 10% below the 365-day peak (live trading) |
| Limit leverage and correlated exposure | margin cap 30%; total open risk capped at 1% of equity and three positions. All six default instruments are US-dollar pairs, so the cap is the protection: if they all move together the loss is at most 0.75% |
| A written plan | **added in v3.9:** `tradebot plan`, generated from the config: what is traded, entry, exit, size, limits, when not to trade, review, and what the rules have measured. Also on the dashboard, following the setup and timeframe switches |
| Discipline, no revenge trading | decisions only on closed bars; stops only ever tighten; four losses in a row pause entries for the day; at most eight entries a day. A trade placed by hand from the dashboard goes through the same guards and the same sizing |
| Cut losses, let profits run | breakeven at +1R, half off at +1.5R, a 2 ATR trailing stop on the rest. Removing the target altogether was tested ("run" in [the results](#trying-to-improve-the-strategy)) and did not help |
| Patience and selectivity | every setup has filters, and WAIT is the normal state: about one to seven signals a week per setup on H1 |
| Journal and review | every order is journalled with its stop and target. **Added in v3.9:** `tradebot review` reports closed trades in R, split into the bot's signals and your own calls, and says when the sample is too small to mean anything |
| Trade with structure and context | direction comes from two higher timeframes that must agree |
| Paper-trade first, start small | dry run is the default; orders need three separate switches and real-money accounts are refused unless allowed |
| **Positive expectancy** | **not met.** Every setup loses on ten years of history ([results](#results-on-real-data)). The plan prints the measured figure and says to stay in dry run. Risk rules limit what a losing system costs; they cannot make it a winning one |

**Mean reversion**, the third common family next to trend following and breakouts, was measured
before deciding whether to build it. The textbook template (only when ADX(14) is
below 25; a bar closes outside the 20-period, 2-deviation Bollinger band with RSI(14) below 30 or
above 70; enter when the next bar closes back inside; stop 1 ATR beyond the extreme; target the
middle band) was written down first and run once per variant on the six instruments:

| Variant | H1 trades a week | H1 winners | H1 average trade | M15 winners | M15 average trade |
|---|---:|---:|---:|---:|---:|
| The template | 2.1 | 51.5% | −0.06R | 48.7% | −0.11R |
| Only trades with reward:risk of 1.5 or more | 0.5 | 29.4% | −0.19R | 33.9% | −0.04R |
| Without the ADX regime filter | 10.4 | 44.6% | −0.09R | 44.2% | −0.11R |

It loses in all six cells (H1 2016 to 2026, 1,131 trades for the template; M15 2024 to 2026, 876
trades), so it was not added as a setup. The regime filter does what it should, since leaving it
out makes H1 worse, but not enough. The numbers come from `scripts/research_mean_reversion.py`,
which uses a simpler fill model than the backtester (stop or target, whichever is touched first,
with the same spread and commission) and the whole history, holdout years included. Nothing was
tuned on it and nothing was adopted from it.

**Rule variations.** The same advice lists refinements for each family. The ones the bot did not
have were written down as fixed variants and run once each through the real engine, with an
adoption rule set beforehand: a variant must improve the average trade on both H1 and M15, and
then also on M30 and M5, which it has not seen.

| Variation | Setup | H1 average trade | M15 average trade |
|---|---|---:|---:|
| Volatility filter: enter only when ATR(14) is above its 100-bar average | EMA cross | −0.18R → −0.20R | −0.27R → −0.21R |
| | Breakout | −0.04R → −0.10R | −0.19R → −0.20R |
| | Pullback | −0.11R → −0.12R | −0.15R → −0.09R |
| | Break and retest | −0.10R → −0.09R | −0.22R → −0.24R |
| Squeeze first: Bollinger bandwidth below its 100-bar median before the break | Breakout | −0.04R → −0.05R | −0.19R → −0.14R |
| Exit as soon as the break fails (a close back through the level) | Breakout | −0.044R → −0.043R | −0.19R → −0.18R |
| Turtle exits: 2.5 ATR stop, no target, out on a close beyond the opposite 10-bar extreme | Breakout | −0.044R → −0.045R | −0.19R → −0.14R |
| Scale out: 2.5 ATR stop, half off at +2R, trail the rest, no target | Breakout | −0.04R → −0.07R | −0.19R → −0.19R |
| | EMA cross | −0.18R → −0.14R | −0.27R → −0.29R |
| RSI(2) dip-buy with the 200-bar average, out on a close beyond the 5-bar average or after 10 bars | new setup | −0.04R, 58.8% winners | −0.08R, 55.2% winners |

Each cell is the current rule → the variant. Only the exit on a failed break improved on both
timeframes, by a hundredth of an R or less. On the timeframes it had not seen it went from −0.13R
to −0.12R on M30 and from −0.16R to −0.20R on M5, so it was not adopted, and a failed break stays
a notice on the dashboard instead of an automatic exit. The RSI(2) rule is the clearest case of a
high win rate that still loses: 59% winners on H1, 8,317 trades, and a negative average, because
the losers are larger than the winners. **Nothing was adopted and the default rules are unchanged.**
Reproduce with `scripts/research_rule_variants.py`.

**Volume, measured moves and other markets (v3.10).** Three more pieces of the same advice were
built and then measured the same way. Spot FX has no central volume, so "volume" here is the
broker's tick count per bar, which `tradebot fetch` now saves. The measured move is the height of
the 20-bar range that broke, projected from the broken level; a trade whose target is closer than
1.5 times its risk is skipped (`min_reward_risk`). Average trade of the breakout, current rule →
variant:

| Breakout variant | H1 | M30 | M15 | M5 |
|---|---:|---:|---:|---:|
| Volume: the break bar has 1.5 times the average tick volume of the previous 20 bars | −0.044R → **−0.008R** | −0.130R → −0.118R | −0.185R → −0.186R | −0.162R → −0.153R |
| Measured-move target | −0.044R → −0.096R | −0.130R → −0.132R | −0.185R → −0.225R | −0.162R → −0.177R |
| Both | −0.044R → −0.053R | −0.130R → −0.131R | −0.185R → −0.178R | −0.162R → −0.128R |

The volume filter is the best single result in this README: on H1 the breakout goes from 4.7 to
3.0 trades a week and from −0.044R to −0.008R (t = −0.2, not distinguishable from zero), with the
loss over ten years falling from −26.5% to −6.5%. It did not pass the rule set beforehand, which
required an improvement on both H1 and M15, and M15 did not move. M30 and M5 were then run for
information only. So it ships as an option, off by default, and it is still not a profitable
rule. Measured-move targets were worse than the plain 3 ATR target on three timeframes and level
on the fourth.

All six default instruments have a US dollar leg, so their daily returns are correlated (mean
absolute correlation 0.43). A second set was fixed before any of its data was seen: EURUSD,
USDJPY and XAUUSD kept, plus EURGBP, AUDNZD and the US500 index (0.17). Same rules, same cost
model, average trade and largest drawdown:

| Setup | H1, current six | H1, diversified six | M15, current six | M15, diversified six |
|---|---:|---:|---:|---:|
| ema_cross | −0.18R · 28% | −0.21R · 32% | −0.27R · 26% | −0.29R · 26% |
| breakout | −0.04R · 34% | −0.06R · 39% | −0.19R · 53% | −0.15R · 43% |
| pullback | −0.11R · 63% | −0.16R · 76% | −0.15R · 58% | −0.13R · 52% |
| retest | −0.10R · 51% | −0.14R · 58% | −0.22R · 60% | −0.22R · 55% |

Less correlated instruments did not smooth anything: on H1 all four setups did worse and drew
down further, and M15 was mixed. Diversification spreads the risk of a system that makes money;
it does nothing for one whose average trade is negative, and here the added pairs were the
weakest ones (AUDNZD averaged −0.3R to −0.5R: a quiet pair with a wide spread). The default
instruments are unchanged. US500 results use its contract value for dollar amounts and the same
commission as a share of position value; for the two crosses the simulator converts profit at
today's exchange rate, which leaves R-multiples exact and the percentage returns approximate.
Reproduce with `scripts/research_breakout_extras.py`.

### Reversal warning for an open position (v3.11)

The entry rules say when to get in. Someone holding a trade asks the other question: has the
market turned against it? The check uses the same closed-bar facts as the strategy and counts
only what happened after the position opened, so nothing that was already true at the entry is
reported as news. It covers every position on a watched instrument, including one opened by hand
in MetaTrader.

| Sign | What it is | Taken back when |
|---|---|---|
| Opposite signal | the watched setup signals the other way and every filter passes it (the bot closes its own trades on this) | a new trigger fires in the position's favour |
| Structure | a bar closes beyond the prior 20-bar low (for a buy) or high (for a sell) | a bar closes back beyond that level |
| Averages | the fast EMA crosses the slow EMA against the position | they cross back |
| Trend | the higher-timeframe trend turns against the position | it no longer points against it |
| Failed break | the break the position was riding closes back through its level (breakout and retest setups) | price closes back beyond the level, or a new break fires |

One sign is a **warning**. The opposite signal, or two signs standing together, is the **get out**
level. A sign that the bar still forming would add is shown separately as "forming, not
confirmed", because it can disappear before the close. The page does not close positions: it
says what it sees, what you hold, your result so far and where your stop is.

What it has been worth was measured the same way as everything else: every trade of the
backtests was closed when the check said so. Average trade, as it is → closing at the get-out
level → closing at the first sign:

| Setup | H1 | M15 | M30 | M5 |
|---|---:|---:|---:|---:|
| ema_cross | −0.178R → −0.175R → −0.169R | −0.273R → −0.264R → −0.271R | −0.239R → −0.240R → −0.237R | −0.169R → −0.171R → −0.167R |
| breakout | −0.044R → −0.044R → −0.043R | −0.185R → −0.185R → −0.181R | −0.130R → −0.128R → −0.121R | −0.162R → −0.166R → −0.206R |
| pullback | −0.108R → −0.109R → −0.108R | −0.145R → −0.150R → −0.158R | −0.127R → −0.128R → −0.134R | −0.193R → −0.187R → −0.203R |
| retest | −0.103R → −0.104R → −0.101R | −0.219R → −0.220R → −0.229R | −0.165R → −0.168R → −0.156R | −0.171R → −0.182R → −0.167R |

Two things follow. First, on the bot's own trades the get-out level almost never has anything to
say: it fired on 4 to 91 trades out of 270 to 3,700, because a stop 1.5 ATR away is hit before two
signs can line up, and obeying it moved the average trade by 0.011R at most. Second, closing at
the first sign shortens trades by one to three bars and cuts the winning rate by up to four and a
half points; the average trade came out better on ten of the sixteen runs and worse on five,
never by more than 0.009R in its favour and by up to 0.044R against (breakout on M5). One
combination out of eight (ema_cross, first sign) came out ahead on all four timeframes, by
0.002R to 0.009R; with eight tries and four coin-flip comparisons each, chance alone does that
about four times in ten, so it was not made a rule. The
check is not an exit strategy and the bot does not trade on it. It is there for the case the
backtest cannot represent: a position opened by hand, with a wide stop or none, where nothing
else on the screen says the reason for the trade has gone. Reproduce with
`scripts/research_reversal_exit.py`.

### Trends, and when one is likely ending (v3.12)

A trend here is the one the EMA setup trades: EMA 10 above EMA 50 is an uptrend, below it a
downtrend, and it ends when they cross. That end is late by construction, so the dashboard also
watches for what tends to come first. Nobody can know that a trend is about to stop; these are
signs, and what each has been worth was measured on about 9,600 to 10,300 finished trends per
timeframe (six instruments, median length 21 or 22 bars). The question asked of every bar of every
trend: did the trend end within the next 10 bars?

| Showing on the bar | H1 | M15 | M30 | M5 |
|---|---:|---:|---:|---:|
| Any bar of a trend (the base rate) | 22% | 23% | 22% | 24% |
| Price closed on the wrong side of EMA 50 | **74%** | **73%** | **74%** | **73%** |
| The averages converging (gap at most half its widest, still narrowing) | 50% | 51% | 50% | 53% |
| MACD histogram on the other side of zero | 31% | 30% | 30% | 32% |
| ADX 20% below its high of the trend | 27% | 26% | 26% | 28% |
| Divergence (new 20-bar closing extreme, weaker momentum than the last) | 14% | 15% | 15% | 15% |

The first version of this feature counted the five signs equally and warned at two. The
measurement said that was wrong: with two signs standing, a trend ended within 10 bars 17% to 19%
of the time, less often than on an ordinary bar, and divergence, the textbook warning, came before
an ending less often than chance. So the count was dropped and the state now follows the two
signs that carried information. This rule was chosen after seeing those numbers, which is why
it is reported per instrument as well:

| State | Share of trend bars | Ended within 10 bars | Range across the six instruments |
|---|---:|---:|---:|
| Intact: neither sign | 78% | 14% to 15% | |
| Weakening: averages converging, price still on the right side | 10% to 11% | 30% to 33% | 28% to 37% |
| Likely ending: price closed beyond EMA 50 | 11% to 12% | 73% to 74% | 71% to 75% |

Nine trends in ten showed "likely ending" before they ended, first a median of 5 bars before
(quartiles 3 to 18). About 23% of those warnings came more than 20 bars early: the trend
recovered and ran on. Two limits belong next to these numbers. The two useful signs are close
relatives of the cross that defines the end, so part of what they predict is their own
definition. And none of the states says where price goes next: over the following 10 bars price
moved between −0.04 and +0.06 ATR in the trend's direction in every state on every timeframe,
which is nothing. The warning says the trend is close to being over. It does not say that price
will keep going against it, and it is not a signal to trade. The other three signs are still
listed on the page, labelled as not reliable on their own. Reproduce with
`scripts/research_trend_signs.py`.

## Backtesting model

| Aspect | Model |
|---|---|
| Prices | bars are bid (as MT5 provides); buys pay the spread, sells exit at the ask |
| Spread | the bar's recorded spread or `min_spread_points`, whichever is larger (demo feeds often record 0) |
| Execution | decisions at bar close, filled at that close ± spread/slippage |
| Stops / targets | checked on following bars' high/low; gaps fill at the open; a bar touching both counts as a stop |
| Costs | commission per lot actually traded (round trip), configurable slippage |
| Pip value | recomputed from price for USD-quoted and USD-based pairs |
| Portfolio | all symbols on one timeline sharing balance, positions and risk caps |

A 60,000-bar M15 backtest takes about 3 seconds. The accounting identity (final equity = start +
Σ trade P/L) is asserted in the tests.

![tradebot backtest in the terminal](docs/screenshots/terminal-backtest.png)

The guard summary at the bottom shows how many signals each guard blocked: session hours,
position sizes below the minimum lot, the losing-streak pause, and position limits.

## Results on real data

These runs used a single H4 trend filter (`"trend_timeframes": ["H4"]`) and counted any close
through a level as a breakout (`"breakout_min_atr": 0`), which were the defaults when they were
made; the research scripts pin both. The automatic two-timeframe ladder that is
now the default is measured [under the advisor dashboard](#advisor-dashboard).

Every strategy was walk-forward tested on six instruments (EURUSD, GBPUSD, USDJPY, AUDUSD,
USDCAD, XAUUSD) using history exported from a MetaQuotes demo terminal: 60,000 H1 bars (2017 –
Nov 2026), 60,000 M15 bars (Jun 2024 – Nov 2026), and the broker's real H4 bars for the trend
filter. Each run used 4 folds and default costs (≥ 1-pip spread, $7 per lot round trip). The
out-of-sample periods are Sep 2018 – Nov 2026 for H1 (8.2 years) and Oct 2024 – Nov 2026 for M15
(2.1 years). **Every configuration tested is shown, including the losers.**

| Strategy | TF | Out-of-sample trades | Return | Max DD | Sharpe | Profit factor | Avg trade | WF efficiency |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| ema_cross | H1 | 521 | **−20.0%** | 21.6% | −0.91 | 0.73 | −0.17R | −9.54 |
| breakout | H1 | 2,703 | −43.2% | 43.7% | −1.03 | 0.85 | −0.09R | −6.55 |
| pullback | H1 | 3,516 | −70.8% | 71.0% | −1.97 | 0.77 | −0.13R | n/a¹ |
| ema_cross | M15 | 362 | −20.4% | 21.0% | −2.33 | 0.63 | −0.26R | n/a¹ |
| breakout | M15 | 1,468 | −44.1% | 44.6% | −2.74 | 0.74 | −0.15R | n/a¹ |
| pullback | M15 | 2,458 | −53.3% | 54.4% | −2.83 | 0.80 | −0.12R | n/a¹ |

¹ Not even the best parameter set was profitable in-sample.

These numbers replace an earlier table computed on 3 years of H1 data with mislabelled summer
hours (see [what the advisor check found](#found-by-checking-the-advisor-against-mt5-v32)). On
that shorter window the corrected ema_cross H1 result is −6.5% out of sample (it was −4.9%).

**None of these strategies has an edge, before or after costs.** What the numbers show:

- **Costs make it worse, but they are not the whole story.** With spread, commission and slippage
  all set to zero, ema_cross with default settings still averages −0.06R per trade on H1 and
  −0.12R on M15. Costs add about 0.07R per trade on H1 and 0.13R on M15, where stops are tighter;
  that is why the default timeframe is H1 and why high-frequency variants lose fastest. (An
  earlier version of this README said zero-cost results were break-even. That was measured on
  the mislabelled data and did not survive the correction.)
- **In-sample results lie.** For ema_cross H1 the best in-sample Sharpe in any window was 0.26;
  the parameters it chose then lost in every one of the four unseen windows, one of them −16.3%.
  A negative walk-forward efficiency means the in-sample "edge" was fitted to noise. v23's
  optimizer only ever reported in-sample numbers.
- **The fill model leans conservative.** A bar that touches both stop and target counts as a
  stop. On the earlier dataset, a strategy plus its exact mirror image summed to about −0.05R per
  trade at zero cost, which is the size of that bias: small, and in the safe direction.

### Trying to improve the strategy

A structured attempt to find something better, designed so that a lucky result could not pass as
skill. The last two years (Nov 2024 – Nov 2026) were locked away as a holdout. Twelve families
were fixed in advance and walk-forward tested on the earlier data only: two triggers × three
timeframes × two exit styles ("target" = the current exits; "run" = no fixed target, trailing
stop only). The slower timeframes test whether lower relative costs help; "run" tests whether
cutting winners at a target was the problem.

| Family (2017 – Oct 2024, out-of-sample folds) | Trades | Return | Profit factor | Avg trade | t-stat |
|---|---:|---:|---:|---:|---:|
| ema_cross · H1 · run | 529 | **+3.6%** | 1.05 | +0.04R | +0.45 |
| breakout · H4 · target | 727 | −2.7% | 0.97 | −0.02R | −0.33 |
| ema_cross · H4 · target | 285 | −3.4% | 0.92 | −0.04R | −0.62 |
| ema_cross · H1 · target | 516 | −6.2% | 0.92 | −0.05R | −0.85 |
| ema_cross · D1 · target | 73 | −1.4% | 0.83 | −0.07R | −0.56 |
| ema_cross · H4 · run | 389 | −6.4% | 0.86 | −0.08R | −1.19 |
| breakout · H4 · run | 560 | −12.3% | 0.80 | −0.09R | −1.65 |
| breakout · H1 · run | 1,665 | −24.2% | 0.88 | −0.07R | −1.80 |
| breakout · H1 · target | 2,337 | −32.8% | 0.88 | −0.07R | −2.80 |
| breakout · D1 · target | 180 | −5.4% | 0.77 | −0.15R | −1.56 |
| breakout · D1 · run | 109 | −3.7% | 0.70 | −0.24R | −1.98 |
| ema_cross · D1 · run | 45 | −2.6% | 0.48 | −0.28R | −2.15 |

One family of twelve was positive, with a t-statistic of 0.45: what chance produces. It was still
taken to the holdout, once, with the parameters its last research window chose:

| Holdout, Nov 2024 – Nov 2026 | Trades | Return | Profit factor | Winners | Avg trade (after swap) |
|---|---:|---:|---:|---:|---:|
| Candidate: ema_cross · H1 · run | 77 | **−8.8%** | 0.37 | 19.5% | −0.47R |
| Current default config | 166 | −7.5% | 0.70 | 36.7% | −0.19R |

**The candidate failed and was not adopted.** Slower timeframes and letting winners run do not
rescue these triggers; none of the twelve predicts direction on these six instruments.

A second round tried three different *kinds* of idea, each tuned by walk-forward on the research
years only and then run once on the same holdout through the real engine:

| Idea | What it uses | Research (out-of-sample folds) | Holdout, Nov 2024 – Nov 2026 |
|---|---|---|---|
| **Fade** (H1) | mean reversion: trade against a move stretched ≥ 2 ATR from EMA 20 with RSI at an extreme | 334 trades, −0.10R each, t = −2.0 | 239 trades, 47.7% winners, **−4.3%**, PF 0.85 |
| **Rates** (D1) | different information: 3-month interbank rate differentials (OECD via FRED), traded monthly | 129 trades, −0.07R each, t = −0.6 | 32 trades, 34.4% winners, **−1.7%**, PF 0.58 |
| **Machine learning** (H1) | gradient boosting on the bot's 18 indicator features, predicting whether +1.5 ATR or −1.5 ATR comes first | 539 trades at its most confident, +0.00R each, t = +0.1 | 57 trades, 31.6% winners, **−5.0%**, PF 0.43 |

The machine-learning result answers "can it tell a buy from a sell?" most directly. Across
174,599 research-period bars it was right **50.4%** of the time, and 50.8% on 72,589 holdout
bars. On the 1% of bars where it was most confident it reached 55.2% in research, then 48.7%
on the holdout: the confidence was fitted noise.

Fifteen strategy variants across five kinds of idea have now been tested this way and none
predicts direction on these instruments after costs. (The Bollinger mean-reversion template was
checked later, more simply, and is reported under [Trading principles](#trading-principles).) **The default config is unchanged.**
Reproduce with `scripts/research_families.py`, `scripts/research_holdout.py` and
`scripts/research_round2.py` (`pip install -e ".[research]"`). The holdout has now been used
for four final tests, so the next idea needs fresh out-of-sample data: the honest source is
forward testing in dry-run mode, which the journal already records.

So the bot is ready to run, and the research tooling says plainly that these signal sets are not
worth trading with real money. Finding that out cheaply is the point of a backtester. New ideas
(regime filters, longer horizons, ML classifiers on the same features) can be evaluated under the
same walk-forward discipline by adding a trigger to `strategy.py`.

![Walk-forward report: out-of-sample equity and drawdown](docs/screenshots/report-overview.png)

<details>
<summary><b>More report sections</b>: per-symbol results, R distribution, monthly returns, walk-forward windows, dark mode</summary>

![Per-symbol results and trade outcomes](docs/screenshots/report-symbols.png)
![Monthly returns and walk-forward windows](docs/screenshots/report-walkforward.png)
![Dark mode](docs/screenshots/report-dark.png)
</details>

![tradebot optimize in the terminal](docs/screenshots/terminal-walkforward.png)

## Bugs fixed from v23

v23 is kept under the [`v23` tag](https://github.com/fahadkatonvule24/tradingBot/tree/v23). Each fix has a regression test.

| v23 behaviour | Impact | Now |
|---|---|---|
| Committed `config.json` had `dry_run: false` + `live_trading_confirm: true` | cloning and running sent real orders | dry run by default, five gates |
| ADX smoothed with a running **sum** | ADX ≈ 14× too large; "trending only" filter never filtered | Wilder average, tested against a textbook implementation |
| Sizing used `symbol_info().trade_tick_value` | on MetaQuotes-Demo, XAUUSD reports 0.1 while a tick pays 1.0 → **10× intended risk on gold** | tick value from the terminal's `order_calc_profit` |
| Backtest used a different, simplified signal | backtest/optimizer results did not describe the live bot | one engine for both |
| Signals computed on the still-forming bar | crossovers could appear and vanish within a bar | closed bars only |
| Backtest commission charged on the minimum lot | costs understated ~10× | per lot traded |
| Volume below the broker minimum was rounded up | trades risked more than configured | skipped unless explicitly allowed |
| Partial take-profit remembered in memory | a restart could take it again | derived from position history |
| Dashboard requests handled only in a refresh's spare time (found in v3.6 testing) | once a refresh took over a second, the timeframe switch and the order button were never served | requests are served after every refresh |
| Forex-pip filters on every symbol | crypto/gold spreads never passed; S/R and Fibonacci meaningless | everything in ATR units, per-symbol overrides |
| Local clock for sessions, UTC for news, server time for deals | guards disagreed about "today" | UTC everywhere, server offset detected |

### Found by checking the advisor against MT5 (v3.2)

Before trusting the dashboard, every number it shows was recomputed independently against the live
terminal. The indicators matched a plain-loop reimplementation to 1e-15 and the money was within
about 1% of `order_calc_profit`, but the check also exposed these problems:

| Found | Impact | Now |
|---|---|---|
| History converted with today's server offset (UTC+2) for every bar | the broker runs New York time + 7h, so **every bar from the US daylight-saving months was labelled an hour late**: the 8:30 New York payroll spike sat in its true UTC hour on 2 of 18 summer first-Fridays (44 of 55 after the fix). Backtests applied session hours and news blackouts an hour off for seven months a year, and a bot left running over a DST weekend would have treated a forming bar as closed | the broker's clock rule is detected from its week-open pattern (`broker/clock.py`) and every bar is converted with the offset of its own date |
| Live trend filter fetched 400 H4 bars for a 200-period EMA | the EMA still carried 2% of its seed: the live H4 trend disagreed with a fully warmed EMA on up to 8% of recent bars, exactly at trend changes | 6 periods of history (seed weight 0.0006%) |
| Backtest built H4 bars from H1 starting at 00:00 UTC | MT5's H4 bars start at 17:00 New York (22:00 UTC in winter), so the backtest graded a different trend filter from the one that trades | `fetch` saves the real H4 bars and the backtest uses them; the fallback resample is anchored like the broker's |
| Profit/loss from one tick value | for USDJPY/USDCAD a pip is worth less at the target than at the stop: the plan was up to $3 off per $250 | stop and target priced by the terminal at the exact prices; sizing uses the same figure |
| Countdown to the next bar close outside trading hours | "next decision 09:00" when nothing could open before 10:00 | shows when entries resume |

After the fixes, the live path (MT5 bars, real H4 bars) and the backtest path (CSV files) agree on
the trend on every bar they share, and on the signal on every one of the last 1,300 bars of the
live window, including every bar the bot acts on. The only differences sit in the first ~150 bars
of the live window, where its indicators are still warming up. The planned loss and profit match
`order_calc_profit` to the cent.

The corrected data also changed the verdict on individual symbols: over the same 3.4 years the
default backtest went from −1.1% to **−4.7%**, and EURUSD's apparent edge (profit factor 1.15)
became a loss (0.86). It had come from the mislabelled hours.

### Found from a losing trade (v3.13)

On Monday 16 November 2026 the dashboard said "Sell GBPUSD now" (breakout setup) at 07:00 UTC.
The sell was taken by hand and lost: the spike at the London open reached the stop at 08:40 UTC,
and price fell 35 pips after that. A replay of the day with the bot's own code reproduced every
alert, so the trigger did what its rule says. It fired an hour before the session the config
asks for, though:

| Found | Impact | Now |
|---|---|---|
| Session hours were fixed UTC values, right only while London and New York are on summer time | from late October to late March `london_new_york` opened at 07:00 UTC, an hour before London, and closed at 21:00 UTC, an hour before New York; the market panel showed London open at 07:00 UTC | sessions open and close on London and New York local time (`config.session_hours`), so their UTC hours move with each city's daylight saving, including the weeks when only one city has changed its clock; the written plan states both |

The fix makes the bot keep the hours it states; it does not make the setups profitable. Over the
H1 history, the trades that only the old hours took (mostly at 07:00 UTC in winter) were no worse
than the others: on average +0.17R for breakout, −0.04R for retest and −0.27R for pullback. The H1
setup table moves by under two points of return (breakout −26.5% → −26.9%, pullback −62.5% →
−60.8%, retest −47.4% → −45.3%, EMA cross −26.1% → −26.0%) and every setup still loses. On M30,
M15 and M5 each row moves by between −1.7 and +2.6 points, and every one still loses. The tables
above were measured before this fix.

A sweep of the rest of the system after that trade, with today's activity log as evidence (v3.14):

| Found | Impact | Now |
|---|---|---|
| A setup or timeframe switch cleared the record of what had been announced | each switch re-sent the same forming and blocked triggers for every symbol: the GBPUSD "buy trigger forming" for the 08:00 bar went out five times in 40 minutes | the once-only keys name the setup and the timeframe and survive a switch |
| Signals from the three setups not on screen were announced like the main one | four times the signals, each from a setup that loses on its own; the losing GBPUSD sell came from the breakout setup while the screen showed break and retest | off by default (`alerts.other_setups`); their tiles still show them |
| Alert sizes did not say which account they were for | on the terminal's demo account a plan read "1.2 lots · risk 249.60" with nothing to say that is 0.25% of 100,000 USD, not of a small live account | every size ends with "sized for …, equity …" |
| The analysis card only counted triggers that disagreed with each other | a pullback buy firing in a strong downtrend showed "No directional conflict" | a trigger against the card's direction is listed as a conflict |
| Every setup or timeframe click ran the record's 10-year backtest inside the dashboard | the page lagged: one backtest is 11.6 s of work, and competing with the page for the interpreter it took 96 s; with it running, page work was up to 0.75 s late (0.15 s now) | it runs in a separate low-priority process, a result is kept per setup and timeframe (switching back is instant), and a newer click stops a run nobody is waiting for, including the interpreter a virtual environment's launcher starts |

### Found by an audit (v3.14, 19 November 2026)

Sixteen findings in [docs/bug-audit-2026-11-19.md](docs/bug-audit-2026-11-19.md), each with its
adverse case as a test in `tests/test_audit_2026_11_19.py`:

| Found | Impact | Now |
|---|---|---|
| The trading loop checked the account once, at start | a terminal switched from demo to a real-money account kept receiving orders | the gate is checked before every instrument; orders stop (and resume) with it, and another account's loss history is read afresh |
| A dashboard order that timed out stayed queued | the page said it failed, then it was sent; a retry sent a second one | a queued order is cancelled at the timeout, one being sent is reported as pending, and a repeated request is answered, not re-sent |
| Half off and the trail read the last closed bar with no time | a trade opened at −0.2R was "half closed at +2R" from the bar before it | management waits for a bar that closed after the entry, and half off also needs the live price at the target R |
| Netting reversal deals were dropped | the loss realised by a reversal was missing from the loss limits | the deal is a close with its result plus an opening |
| A stale quote at start-up set the server clock | a 12-hour-old tick read as UTC−10, after which every fresh quote was "in the future" until a restart | the freshest of all quotes is used; a clock from a quote that is not live is measured again, and a quote from the future corrects it |
| A partial fill was reported as rejected | an open position labelled REJECTED, its volume lost | reported as a fill of the lots filled |
| The backtest decided each symbol before the next was moved to the same close | renaming a symbol changed the results: a stop at that close had not yet freed the slot | every symbol's bar is processed before any decision at that moment |
| Backtest sizing used a fixed tick value, its results a converted one | USDJPY risked $30 on a $25 budget | sizing, the recorded 1R and the result use one conversion |
| The automatic trend ladder is empty on D1 | with the trend filter on, every entry was blocked, silently | refused at start with the reason |
| `"trend_timeframes": "H4"` passed validation | the trend filter read timeframes "H" and "4" and crashed | a single timeframe name is one timeframe |
| The calculator's most lots used notional / leverage beside the terminal's margin | 2.72 lots "fit" a 30% cap that 0.30 lots fill | the most lots use the margin the trade does, and say which |
| Other accounts' sizes were kept for the whole bar | a size from an equity the open trades had since lost | worked out again when those accounts' equity moves |
| The EMA cross tile could not show EMA cross on a symbol with its own setup | "Now watching the ema cross setup", and nothing changed | a setup button shows that setup everywhere; **As configured** goes back to the config's own |
| A failed first contact with Telegram read commands from the start | an old `/equity` command could run again | commands are read only once Telegram has said where the backlog ends, and older ones are ignored |
| The plan described the main setup when every symbol overrode it | the written rules were not the ones traded | the plan names the setup the symbols trade |
| Get in and Duplicate ignored the `STOP` file | an order sent while entries were paused | the STOP file pauses every order from the dashboard |

## Testing

```bash
pip install -e ".[dev]"
pytest --cov=tradebot      # 730 tests
ruff check src tests scripts && ruff format --check src tests scripts   # as CI runs it
mypy                       # settings in pyproject.toml; no issues
```

Tests cover indicators against textbook references, strategy causality, every engine guard and
management rule, the simulated broker's fills and accounting, walk-forward window separation,
configuration validation, the live-trading gate, and the MT5 adapter. The advisor is tested in
every state with exact prices and lot sizes. The dashboard's order path is tested to send exactly
the advised trade and to refuse when the gate is closed, the `--live` flag is missing, or the
advice has changed. The web server is tested against missing tokens, foreign Host headers and
cross-origin requests. The adapter is tested against a fake `MetaTrader5` module, so CI runs on
Linux and Windows without a terminal.

## Limitations and next steps

- **Bar-level simulation:** fills inside a bar are approximated (conservatively). Tick data would
  tighten this for scalping timeframes.
- **The drawdown limit looks at realised results.** A position that is deep in loss but still
  open does not count until it closes; the stop on each position bounds that.
- **No regime model yet:** the strategies are classic technical rules. The results above show
  they are not enough on their own; the tooling is built to evaluate better ideas honestly.
- **The news comes from one public feed.** If ForexFactory changes or withdraws it, the download
  fails loudly, the last calendar stays in use, and `config/news_blackouts.json` still works.
- **The terminal can stall the bot.** The MetaTrader5 package blocks the whole Python process
  while the terminal is busy, for example during a large history download, so the dashboard
  freezes until the terminal answers. Download history (`tradebot fetch`) when the dashboard is
  not running.
- **One process, one terminal.** The dashboard is local by design; the news download, and
  ntfy, WhatsApp or Telegram alerts (and Telegram commands) if you configure them, are the only traffic that leaves the
  machine. The page itself is not reachable from a phone; the commands are how the phone talks to
  it. Accounts at other brokers are not read automatically: only the account the terminal is
  logged in to is.
- **It is only on while the PC is.** Alerts stop when the computer, the terminal or `tradebot ui`
  stops. Stops and targets crossed meanwhile are caught on the next start, from the closed bars.

## Disclaimer

Educational software. Trading leveraged products can lose more than you deposit. Simulated
results, including everything above, do not guarantee future performance. Test on a demo account.
