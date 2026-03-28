# scanner.py

> Designed for fast futures screening of low-margin KuCoin contracts to identify RSI extremes before manual trade entry.

A command-line tool that scans all **KuCoin Futures** markets, filters them by the cost of a minimum lot position (in USDT), computes RSI for each qualifying pair, and persists the results to a local SQLite database. Output is printed to the terminal with color-coded RSI and price-change values.

---

## Why this exists

Most futures screeners either require a paid API subscription or don't account for the actual minimum capital needed to open a position. This tool calculates the real minimum margin cost per symbol (`lot_size × min_contracts × price / leverage`) so you can filter the universe down to only the pairs you can actually trade — then ranks them by RSI to surface potential entries at a glance.

---

## Architecture

```
KuCoin API
    │
    ├── load_markets()       → filter contract markets
    │
    ├── fetch_ticker()       → last price per symbol
    │
    └── fetch_ohlcv()        → candle data per symbol
              │
              ├── Lot-cost filter   (lot_size × min_contracts × price / leverage)
              │
              ├── RSI calculation   (TA-Lib if available, else NumPy/Wilder)
              │
              ├── 24h Δ calculation (from OHLCV, no extra API call)
              │
              └── SQLite upsert     → Terminal output (color-coded, sorted by RSI)
```

---

## Example output

```
[NEW]     DOGE/USDT:USDT | Lot: 0.00243000 USDT (x10) | Lot size: 1.00000000 | Price: 0.24300000 | Δ: -3.41% | RSI: 26.18
[NEW]     SHIB/USDT:USDT | Lot: 0.00000187 USDT (x10) | Lot size: 1.00000000 | Price: 0.00001870 | Δ: +1.05% | RSI: 31.74
[UPDATED] PEPE/USDT:USDT | Lot: 0.00000014 USDT (x10) | Lot size: 1.00000000 | Price: 0.00000140 | Δ: -0.71% | RSI: 68.92

Top pairs by RSI (descending):

PEPE/USDT:USDT  | Lot: 0.00000014 USDT (x10) | Price: 0.00000140 | Δ: -0.71% | RSI: 68.92  (2025-06-01T10:42:11+00:00)
SHIB/USDT:USDT  | Lot: 0.00000187 USDT (x10) | Price: 0.00001870 | Δ: +1.05% | RSI: 31.74  (2025-06-01T10:43:05+00:00)
DOGE/USDT:USDT  | Lot: 0.00243000 USDT (x10) | Price: 0.24300000 | Δ: -3.41% | RSI: 26.18  (2025-06-01T10:41:58+00:00)

Markets processed: 312 | Matched by lot: 47 | New: 3 | Updated: 44 | Skipped (NaN RSI): 0
```

RSI color coding: **Cyan** < 30 (oversold) · **Yellow** 30–70 (neutral) · **Magenta** > 70 (overbought)  
Price change: **Green** positive · **Red** negative

---

## Features

- Fetches every futures/swap market on KuCoin via [ccxt](https://github.com/ccxt/ccxt)
- Filters pairs by real minimum margin cost: `lot_size × min_contracts × price / leverage`
- Calculates RSI using TA-Lib when available, with automatic fallback to a pure-NumPy Wilder-smoothing implementation
- Computes 24-hour price change directly from OHLCV candles — no extra API call
- SQLite persistence with upsert logic (insert on first run, update only when values change)
- Final summary sorted by RSI descending
- No API keys required — public endpoints only

---

## Requirements

| Dependency | Notes |
|---|---|
| Python ≥ 3.8 | |
| ccxt | Exchange connectivity |
| numpy | RSI fallback + OHLCV handling |
| colorama | Terminal color output |
| TA-Lib | Optional — faster RSI; auto-detected at runtime |

---

## Installation

### 1. Clone the repository

```bash
git clone https://github.com/goodgamefinder/kucoinrsiscan.git
cd kucoinrsiscan
```

### 2. Create and activate a virtual environment (recommended)

```bash
python3 -m venv venv
source venv/bin/activate        # Linux / macOS
venv\Scripts\activate.bat       # Windows
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. (Optional) Install TA-Lib for faster RSI

TA-Lib requires the underlying C library to be installed first.

**Ubuntu / Debian**
```bash
sudo apt-get install ta-lib
pip install TA-Lib
```

**macOS**
```bash
brew install ta-lib
pip install TA-Lib
```

**Windows** — download a pre-built wheel from
[https://github.com/cgohlke/talib-build/releases](https://github.com/cgohlke/talib-build/releases) and install it with `pip install <wheel_file>.whl`.

If TA-Lib is not installed the script falls back to the built-in NumPy implementation automatically.

---

## Usage

```
python scanner.py [rsi] [timeframe] [lot_limit] [db] [options]
```

### Positional arguments

| Argument | Type | Default | Description |
|---|---|---|---|
| `rsi` | int | `24` | RSI period |
| `timeframe` | str | `1h` | OHLCV candle timeframe (`1m`, `5m`, `15m`, `1h`, `4h`, `1d`) |
| `lot_limit` | float | `0.03` | Maximum minimum-lot cost in USDT |
| `db` | str | `futures_pairs.db` | Path to the SQLite database file |

### Optional flags

| Flag | Default | Description |
|---|---|---|
| `--leverage` | `10.0` | Leverage multiplier for lot-cost calculation |
| `--ohlcv_limit` | `200` | Number of candles to fetch per symbol |
| `--delay` | `0.01` | Seconds to sleep between API requests |
| `--verbose` | off | Print full error tracebacks |

### Examples

```bash
# Run with all defaults (RSI 24, 1h candles, lot <= 0.03 USDT at 10x leverage)
python scanner.py

# 15-minute candles, RSI period 14, lot limit 0.05 USDT
python scanner.py 14 15m 0.05

# Custom database, 20x leverage, verbose error output
python scanner.py 24 1h 0.03 my_data.db --leverage 20 --verbose
```

---

## How it works

1. **Load markets** — `load_markets()` returns every instrument KuCoin Futures exposes.
2. **Filter contracts** — only markets where `contract=True` or `type` is `future`, `swap`, or `contract` are kept.
3. **Lot-cost filter** — the script resolves `contractSize` and `minOrderQty` from market metadata, then computes `lot_size × min_contracts × last_price / leverage`. Symbols above `lot_limit` are skipped.
4. **OHLCV fetch** — retrieves the last `ohlcv_limit` candles on the chosen timeframe.
5. **RSI** — uses TA-Lib when available; otherwise applies Wilder-smoothing in NumPy. Symbols with a NaN final RSI are skipped.
6. **24h change** — infers how many candles equal 24 hours for the chosen timeframe and computes `(close_now - close_24h_ago) / close_24h_ago * 100`.
7. **Upsert** — first encounter inserts a new row; subsequent runs update only fields that differ (with small numeric tolerances to avoid spurious writes).
8. **Summary** — after all symbols are processed, the full database is printed sorted by RSI descending.

---

## Database schema

```sql
CREATE TABLE futures_pairs (
    symbol         TEXT PRIMARY KEY,
    lot_price_usdt REAL,
    price          REAL,
    daily_change   REAL,
    rsi            REAL,
    updated_at     TEXT   -- ISO-8601 UTC timestamp
);
```

Created automatically on first run. If an older schema is detected, missing columns are added via `ALTER TABLE` without data loss.

---

## License

MIT
