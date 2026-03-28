# scanner.py

A command-line tool that scans all **KuCoin Futures** markets, filters them by the cost of a minimum lot position (in USDT), computes RSI for each qualifying pair, and persists the results to a local SQLite database. Output is printed to the terminal with color-coded RSI and price-change values.

---

## Features

- Fetches every futures/swap market available on KuCoin via [ccxt](https://github.com/ccxt/ccxt)
- Filters pairs by the **minimum margin cost** of one lot: `lot_size × min_contracts × price / leverage`
- Calculates **RSI** using TA-Lib when available, with an automatic fallback to a pure-NumPy Wilder-smoothing implementation
- Computes the **24-hour price change** directly from OHLCV candles — no extra API call required
- Stores results in a **SQLite** database with upsert logic (insert on first run, update only when values change)
- Color-highlighted terminal output:
  - RSI < 30 → Cyan (oversold)
  - RSI > 70 → Magenta (overbought)
  - 30 ≤ RSI ≤ 70 → Yellow (neutral)
  - Positive Δ → Green · Negative Δ → Red
- Final summary table sorted by RSI (descending)

---

## Requirements

| Dependency | Version |
|---|---|
| Python | ≥ 3.8 |
| ccxt | latest |
| numpy | latest |
| colorama | latest |
| TA-Lib *(optional)* | latest |

---

## Installation

### 1. Clone the repository

```bash
git clone https://github.com/goodgamefinder/kucoinrsiscan/
cd getpairs
```

### 2. Create and activate a virtual environment (recommended)

```bash
python3 -m venv venv
source venv/bin/activate       # Linux / macOS
venv\Scripts\activate.bat      # Windows
```

### 3. Install dependencies

```bash
pip install ccxt numpy colorama
```

### 4. (Optional) Install TA-Lib for faster RSI calculation

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
[https://github.com/cgohlke/talib-build/releases](https://github.com/cgohlke/talib-build/releases) and install it with:
```bash
pip install TA_Lib‑<version>‑cp3x‑cp3x‑win_amd64.whl
```

If TA-Lib is not installed, the script falls back to the built-in NumPy implementation automatically — no action required.

---

## Usage

```
python getpairs.py [rsi] [timeframe] [lot_limit] [db] [options]
```

### Positional arguments

| Argument | Type | Default | Description |
|---|---|---|---|
| `rsi` | int | `24` | RSI period |
| `timeframe` | str | `1h` | OHLCV candle timeframe (e.g. `1m`, `5m`, `15m`, `1h`, `4h`, `1d`) |
| `lot_limit` | float | `0.03` | Maximum allowed minimum-lot cost in USDT |
| `db` | str | `futures_pairs.db` | Path to the SQLite database file |

### Optional arguments

| Flag | Default | Description |
|---|---|---|
| `--leverage` | `10.0` | Leverage multiplier for lot-cost calculation |
| `--ohlcv_limit` | `200` | Number of candles to fetch per symbol |
| `--delay` | `0.01` | Seconds to sleep between API requests |
| `--verbose` | `False` | Print full error tracebacks |

### Examples

**Run with all defaults** (RSI 24, 1h candles, lot ≤ 0.03 USDT at 10× leverage):
```bash
python getpairs.py
```

**15-minute candles, RSI period 14, lot limit 0.05 USDT**:
```bash
python getpairs.py 14 15m 0.05
```

**Custom database file, 20× leverage, verbose output**:
```bash
python getpairs.py 24 1h 0.03 my_data.db --leverage 20 --verbose
```

---

## How it works

1. **Load markets** — `exchange.load_markets()` returns every instrument KuCoin Futures exposes.
2. **Filter contracts** — only markets with `contract=True` or `type` in `{future, swap, contract}` are considered.
3. **Lot-cost filter** — for each symbol, the script resolves `contractSize` and `minOrderQty` from the market metadata, then computes `lot_size × min_contracts × last_price / leverage`. Symbols above `lot_limit` are skipped.
4. **OHLCV fetch** — `fetch_ohlcv` retrieves the last `ohlcv_limit` candles on the chosen timeframe.
5. **RSI** — TA-Lib's `RSI` is used when available; otherwise a Wilder-smoothing NumPy implementation is used. Symbols with a NaN final RSI value are skipped.
6. **24h change** — the script infers how many candles correspond to 24 hours given the chosen timeframe and computes `(close_now - close_24h_ago) / close_24h_ago × 100`.
7. **Upsert** — the record is inserted on the first encounter; subsequent runs update only the fields that have changed (comparison with small numeric tolerances to avoid spurious writes).
8. **Summary** — after processing all symbols, the full database is printed sorted by RSI descending.

---

## Database schema

```sql
CREATE TABLE futures_pairs (
    symbol        TEXT PRIMARY KEY,
    lot_price_usdt REAL,
    price         REAL,
    daily_change  REAL,
    rsi           REAL,
    updated_at    TEXT   -- ISO-8601 UTC timestamp
);
```

The schema is created automatically on first run. If the database already exists with an older schema, missing columns are added via `ALTER TABLE` without data loss.

---

## Notes

- The script makes one `fetch_ticker` call **and** one `fetch_ohlcv` call per matched symbol. On large runs this can take several minutes; the `--delay` flag controls the rate.
- No API keys are required — only public endpoints are used.
- The SQLite file is written in the current working directory by default; use the `db` positional argument to change the path.

---

## License

MIT
