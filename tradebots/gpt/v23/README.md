# MT5 Python Trading Bot

This folder now contains one MetaTrader 5 bot: `mt5_bot.py`.

The strategy is intentionally conservative and auditable:

- Connects to your local MT5 terminal using the official `MetaTrader5` Python package.
- Reads candles for multiple configured symbols and one timeframe.
- Uses an EMA crossover filtered by RSI, ATR volatility, and a higher-timeframe trend filter.
- Filters trades using nearby support/resistance and Fibonacci retracement zones.
- Uses ATR-aware stop loss and take profit distances.
- Sizes trades from account equity and stop-loss distance, unless `fixed_lot` is set.
- Limits risk per trade, total open risk, total bot positions, margin usage, spread, and daily loss.
- Skips symbols with stale candle data.
- Pauses entries around manually configured high-impact news events.
- Can trail stops and move stops toward breakeven on profitable trades.
- Writes order attempts and closes to `trade_journal.csv`.
- Starts in `dry_run` mode so it logs intended trades without sending live orders.

## Setup

1. Install Python dependencies:

```powershell
python -m pip install -r requirements.txt
```

2. Open MetaTrader 5 and log in to your broker account.

3. In MT5, enable algorithmic trading:

- Tools > Options > Expert Advisors
- Enable Algo Trading

4. Edit `config.json` for your symbols, timeframe, risk, stop loss, and take profit.

The default forex list is:

```json
"symbols": ["EURUSD", "USDJPY", "GBPUSD", "AUDUSD", "USDCAD", "USDCHF", "EURGBP"]
```

The active crypto list is:

```json
"BTCUSD", "ETHUSD", "SOLUSD", "LINKUSD", "XRPUSD", "ADAUSD"
```

Your current Exness server did not expose `BTCETH`, `BTCUSDT`, `ETHUSDT`, or `LTCBTC` under those names. If your broker adds them later, put them back in `symbols`.

The bot will resolve broker suffixes automatically, for example `EURUSD` to `EURUSDm`.

## News Blackouts

Edit `news_blackouts.json` before major news. Times must be UTC:

```json
[
  {
    "time_utc": "2026-05-08T12:30:00Z",
    "currency": "USD",
    "impact": "high",
    "event": "Nonfarm payrolls"
  }
]
```

The bot pauses affected currency pairs before and after each event using:

```json
"news_blackout_before_minutes": 30,
"news_blackout_after_minutes": 30
```

You can either leave login details empty and use the already-open MT5 terminal, or set these environment variables:

```powershell
$env:MT5_LOGIN="12345678"
$env:MT5_PASSWORD="your_password"
$env:MT5_SERVER="YourBroker-Server"
$env:MT5_PATH="C:\Program Files\MetaTrader 5\terminal64.exe"
```

## Run

Check the MT5 connection:

```powershell
python mt5_bot.py --check
```

Run one decision cycle:

```powershell
python mt5_bot.py --once
```

This scans all configured symbols once.

Backtest the current config on MT5 candle history:

```powershell
python mt5_bot.py --backtest 5000
```

This scans all configured symbols, prints a ranking, and writes `backtest_results.csv`.

Optimize the strategy across all configured symbols:

```powershell
python mt5_bot.py --optimize 5000 --top 10
```

This tests EMA, RSI, and ATR stop/target combinations and writes `optimizer_results.csv`.

Review a dry-run forward test:

```powershell
python mt5_bot.py --forward-report 7
```

This summarizes `trade_journal.csv` for the last 7 days.

Run continuously:

```powershell
python mt5_bot.py
```

## Live Trading

The default config has:

```json
"dry_run": true
```

Keep this enabled until connection, symbols, pricing, stops, and volume sizing are correct for your broker. To allow real orders, change it to:

```json
"dry_run": false,
"live_trading_confirm": true
```

Both settings are required. This prevents accidental live trading from a single config edit.

Trading can lose money. Test this on a demo account first.

No bot is guaranteed to be profitable. Treat the backtest as a filter, then forward-test on a demo account because spreads, slippage, broker execution, and market regime changes can invalidate historical results.
