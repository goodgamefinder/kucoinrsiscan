#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
getpairs.py
Fetches all KuCoin futures pairs, filters them by minimum lot price (in USDT),
calculates RSI, writes/updates records in SQLite, and prints results with color highlighting.
"""

import argparse
import ccxt
import numpy as np
import sqlite3
import time
import math
import traceback
from datetime import datetime, timezone
from colorama import Fore, Style, init

# Try to import TA-Lib; fall back to the numpy implementation if unavailable
try:
    import talib  # type: ignore
    HAVE_TALIB = True
except Exception:
    HAVE_TALIB = False

init(autoreset=True)

# ----------------- Arguments -----------------
parser = argparse.ArgumentParser(description="KuCoin Futures — collect pairs by minimum lot size and RSI")
parser.add_argument("rsi", nargs="?", type=int, default=24, help="RSI period (default: 24)")
parser.add_argument("timeframe", nargs="?", type=str, default="1h", help="OHLCV timeframe (e.g. 15m, 1h)")
parser.add_argument("lot_limit", nargs="?", type=float, default=0.03, help="max minimum-lot price in USDT (default: 0.03)")
parser.add_argument("db", nargs="?", type=str, default="futures_pairs.db", help="SQLite database file path")
parser.add_argument("--leverage", type=float, default=10.0, help="leverage used to calculate minimum lot cost (default: 10.0)")
parser.add_argument("--ohlcv_limit", type=int, default=200, help="number of candles to fetch for RSI calculation")
parser.add_argument("--delay", type=float, default=0.01, help="delay between API requests in seconds")
parser.add_argument("--verbose", action="store_true", help="print full error tracebacks")
args = parser.parse_args()

RSI_PERIOD = args.rsi
TIMEFRAME = args.timeframe
LOT_PRICE_LIMIT = args.lot_limit
DB_PATH = args.db
LEVERAGE = args.leverage
OHLCV_LIMIT = args.ohlcv_limit
DELAY = args.delay
VERBOSE = args.verbose

# ----------------- Helpers -----------------
def create_exchange():
    """Create the exchange object for KuCoin Futures; fall back to standard KuCoin with defaultType=future."""
    try:
        ex = ccxt.kucoinfutures({'enableRateLimit': True})
    except Exception:
        ex = ccxt.kucoin({'enableRateLimit': True})
        try:
            ex.options = ex.options or {}
            ex.options['defaultType'] = 'future'
        except Exception:
            pass
    ex.enableRateLimit = True
    ex.verbose = False
    return ex

exchange = create_exchange()

# ----------------- DB: schema creation / migration -----------------
conn = sqlite3.connect(DB_PATH, timeout=30)
cur = conn.cursor()

def ensure_db_schema():
    """
    Creates the table if it does not exist.
    If the table already exists but is missing expected columns,
    adds them via ALTER TABLE.
    """
    # CREATE TABLE IF NOT EXISTS leaves an existing table untouched
    cur.execute("""
    CREATE TABLE IF NOT EXISTS futures_pairs (
        symbol TEXT PRIMARY KEY,
        lot_price_usdt REAL,
        price REAL,
        daily_change REAL,
        rsi REAL,
        updated_at TEXT
    )
    """)
    conn.commit()

    # Verify that all expected columns are present (handles old schema upgrades)
    cur.execute("PRAGMA table_info('futures_pairs')")
    cols_info = cur.fetchall()
    cols = [c[1] for c in cols_info]  # column name is at index 1
    needed = {
        'symbol': "TEXT PRIMARY KEY",
        'lot_price_usdt': "REAL",
        'price': "REAL",
        'daily_change': "REAL",
        'rsi': "REAL",
        'updated_at': "TEXT"
    }
    for col_name in needed.keys():
        if col_name not in cols:
            # Add the missing column
            try:
                cur.execute(f"ALTER TABLE futures_pairs ADD COLUMN {col_name} {needed[col_name]}")
                conn.commit()
                if VERBOSE:
                    print(Fore.CYAN + f"Added column '{col_name}' to futures_pairs")
            except Exception as e:
                # Report but do not abort on ALTER TABLE failure
                print(Fore.RED + f"Could not add column '{col_name}': {e}")
                if VERBOSE:
                    traceback.print_exc()

ensure_db_schema()

# ----------------- RSI (TA-Lib or numpy fallback) -----------------
def numpy_rsi(prices, period):
    prices = np.asarray(prices, dtype='float64')
    if prices.size < period + 1:
        return np.full_like(prices, np.nan, dtype='float64')
    deltas = np.diff(prices)
    up = np.where(deltas > 0, deltas, 0.0)
    down = np.where(deltas < 0, -deltas, 0.0)

    # Initial simple average
    avg_up = np.mean(up[:period])
    avg_down = np.mean(down[:period])
    rs = avg_up / avg_down if avg_down != 0 else np.inf
    rsi = np.empty(prices.shape[0], dtype='float64')
    rsi[:] = np.nan
    rsi[period] = 100.0 - (100.0 / (1.0 + rs))
    # Wilder smoothing
    for i in range(period + 1, prices.size):
        cur_up = up[i - 1]
        cur_down = down[i - 1]
        avg_up = (avg_up * (period - 1) + cur_up) / period
        avg_down = (avg_down * (period - 1) + cur_down) / period
        rs = avg_up / avg_down if avg_down != 0 else np.inf
        rsi[i] = 100.0 - (100.0 / (1.0 + rs))
    return rsi

def safe_calculate_rsi(close_prices, period):
    try:
        if HAVE_TALIB:
            arr = talib.RSI(close_prices.astype('float64'), timeperiod=period)
            return np.asarray(arr, dtype='float64')
        else:
            return numpy_rsi(close_prices, period)
    except Exception:
        if VERBOSE:
            traceback.print_exc()
        return numpy_rsi(close_prices, period)

# ----------------- Market helpers -----------------
def safe_get_min_lot_info(market):
    """
    Extracts lot size (contract size in base units) and minimum lot count (minimum contracts).
    Returns (lot_size, min_lots_count).
    """
    info = market.get('info', {})

    # Contract size — multiplier in base units per contract
    lot_size = market.get('contractSize')
    if lot_size is not None:
        lot_size = abs(float(lot_size))
    else:
        lot_size = abs(safe_float(info.get('multiplier', '1.0')))

    if lot_size == 0:
        lot_size = 1.0

    # Minimum number of contracts
    min_lots_count = 1.0

    limits = market.get('limits', {})
    am = limits.get('amount', {})
    v = am.get('min')
    if v is not None:
        min_lots_count = float(v)

    prec = market.get('precision', {})
    p = prec.get('amount')
    if p is not None and min_lots_count == 1.0:
        min_lots_count = float(p)

    # Check additional info keys
    for key in ('lotSize', 'minOrderQty', 'minOrderSize', 'minQty', 'sizeIncrement', 'qtyStep', 'minSize', 'limitMin'):
        if key in info:
            val = safe_float(info[key])
            if val is not None:
                min_lots_count = val
            break

    # minNotional / minCost would require price — skipped here

    return lot_size, min_lots_count

def fetch_last_and_ticker(symbol):
    """Returns (last_price, ticker_dict), or (None, None) on error."""
    try:
        t = exchange.fetch_ticker(symbol)
        last = t.get('last') or t.get('close') or None
        return last, t
    except Exception as e:
        if VERBOSE:
            print(Fore.RED + f"fetch_ticker error for {symbol}: {e}")
        return None, None

def fetch_ohlcv_safe(symbol, timeframe, limit):
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        if not ohlcv:
            return None
        return np.array(ohlcv, dtype='float64')
    except Exception as e:
        if VERBOSE:
            print(Fore.RED + f"fetch_ohlcv error for {symbol}: {e}")
        return None

def compute_daily_change_from_ohlcv(ohlcv):
    """
    Calculates the 24-hour price change from OHLCV data.
    Compares the latest close price with the close price approximately 24 hours ago.
    """
    if ohlcv is None or len(ohlcv) < 2:
        return 0.0

    try:
        # Latest close price
        current_close = float(ohlcv[-1][4])

        # Number of candles that represent 24 hours for the current timeframe
        timeframe_hours = {
            '1m': 1/60,
            '5m': 5/60,
            '15m': 15/60,
            '30m': 0.5,
            '1h': 1,
            '2h': 2,
            '4h': 4,
            '6h': 6,
            '12h': 12,
            '1d': 24
        }

        tf_hours = timeframe_hours.get(TIMEFRAME, 1)  # default to 1 hour
        candles_in_24h = int(24 / tf_hours)

        if len(ohlcv) <= candles_in_24h:
            # Not enough data — use the earliest available close
            past_close = float(ohlcv[0][4])
        else:
            # Close price from ~24 hours ago
            past_close = float(ohlcv[-(candles_in_24h + 1)][4])

        if past_close == 0:
            return 0.0

        # Percentage change
        daily_change = ((current_close - past_close) / past_close) * 100.0
        return daily_change

    except Exception as e:
        if VERBOSE:
            print(Fore.RED + f"Error computing daily change: {e}")
        return 0.0

def safe_float(x):
    try:
        return float(x) if x is not None else None
    except Exception:
        return None

def color_for_rsi(r):
    try:
        if r is None or math.isnan(r):
            return Fore.WHITE
        r = float(r)
        if r < 30:
            return Fore.CYAN
        elif r > 70:
            return Fore.MAGENTA
        else:
            return Fore.YELLOW
    except Exception:
        return Fore.WHITE

def color_for_change(ch):
    try:
        if ch is None or ch == 0.0:
            return Fore.WHITE
        return Fore.GREEN if float(ch) > 0 else Fore.RED
    except Exception:
        return Fore.WHITE

# ----------------- Upsert (insert / update) -----------------
def upsert_db(symbol, lot_price_usdt, price, daily_change, rsi):
    """
    Inserts a new record or updates an existing one.
    Returns (changed: bool, is_new: bool).
    """
    cur.execute("SELECT lot_price_usdt, price, daily_change, rsi FROM futures_pairs WHERE symbol=?", (symbol,))
    row = cur.fetchone()
    now = datetime.now(timezone.utc).isoformat()
    changed = False
    is_new = False

    if row is None:
        cur.execute(
            "INSERT INTO futures_pairs (symbol, lot_price_usdt, price, daily_change, rsi, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (symbol, lot_price_usdt, price, daily_change, rsi, now)
        )
        conn.commit()
        changed = True
        is_new = True
        return changed, is_new

    # Compare with a small tolerance
    prev_lot = safe_float(row[0])
    prev_price = safe_float(row[1])
    prev_dc = safe_float(row[2])
    prev_rsi = safe_float(row[3])

    def differs(a, b, rel_tol=1e-9, abs_tol=1e-8):
        """Returns True if a and b differ beyond the given tolerances."""
        if a is None or b is None:
            return True
        try:
            return abs(a - b) > max(abs_tol, abs(a) * rel_tol)
        except Exception:
            return True

    if differs(prev_lot, lot_price_usdt) or differs(prev_price, price, rel_tol=1e-9) or differs(prev_dc, daily_change, rel_tol=1e-6, abs_tol=1e-6) or differs(prev_rsi, rsi, rel_tol=1e-6, abs_tol=1e-6):
        cur.execute(
            "UPDATE futures_pairs SET lot_price_usdt=?, price=?, daily_change=?, rsi=?, updated_at=? WHERE symbol=?",
            (lot_price_usdt, price, daily_change, rsi, now, symbol)
        )
        conn.commit()
        changed = True

    return changed, is_new

# ----------------- Main -----------------
def main():
    print(Fore.YELLOW + "Fetching KuCoin futures pairs... (this may take a while)")

    try:
        markets = exchange.load_markets()
    except Exception as e:
        print(Fore.RED + "Failed to load markets: " + str(e))
        if VERBOSE:
            traceback.print_exc()
        return

    if not markets:
        print(Fore.RED + "Markets are empty — possible API/network issue or stale ccxt version. Check your connection and ccxt version.")
        return

    processed = 0
    matched = 0
    updated_count = 0
    new_count = 0
    skipped_nan_rsi = 0

    for symbol, mkt in markets.items():
        processed += 1
        try:
            # Keep only contract markets: 'contract' == True or type in (swap, future, contract)
            try:
                is_contract = bool(mkt.get('contract')) or (str(mkt.get('type') or '').lower() in ('future', 'swap', 'contract'))
            except Exception:
                is_contract = False

            if not is_contract:
                continue

            lot_size, min_lots_count = safe_get_min_lot_info(mkt)

            last, ticker = fetch_last_and_ticker(symbol)
            if last is None:
                if VERBOSE:
                    print(Fore.RED + f"Skipping {symbol}: could not fetch last price")
                time.sleep(DELAY)
                continue

            try:
                # Minimum margin cost = lot_size × min_lots_count × price / leverage
                lot_price_usdt = (float(lot_size) * float(min_lots_count) * float(last)) / LEVERAGE
            except Exception:
                lot_price_usdt = float('inf')

            if lot_price_usdt > LOT_PRICE_LIMIT:
                time.sleep(DELAY)
                continue

            matched += 1

            ohlcv = fetch_ohlcv_safe(symbol, TIMEFRAME, OHLCV_LIMIT)
            if ohlcv is None or ohlcv.shape[0] < RSI_PERIOD + 2:
                if VERBOSE:
                    got = 0 if ohlcv is None else ohlcv.shape[0]
                    print(Fore.RED + f"Skipping {symbol}: not enough OHLCV candles for RSI (got {got})")
                time.sleep(DELAY)
                continue

            close = ohlcv[:, 4].astype('float64')
            rsi_arr = safe_calculate_rsi(close, RSI_PERIOD)
            rsi_val = safe_float(rsi_arr[-1])
            if rsi_val is None or math.isnan(rsi_val):
                skipped_nan_rsi += 1
                if VERBOSE:
                    print(Fore.RED + f"Skipping {symbol}: RSI = NaN")
                time.sleep(DELAY)
                continue

            daily_change = compute_daily_change_from_ohlcv(ohlcv)

            changed, is_new = upsert_db(symbol, lot_price_usdt, float(last), daily_change, rsi_val)

            if changed:
                if is_new:
                    tag = Fore.GREEN + "[NEW]"
                    new_count += 1
                else:
                    tag = Fore.YELLOW + "[UPDATED]"
                    updated_count += 1
                change_color = color_for_change(daily_change)
                rsi_color = color_for_rsi(rsi_val)
                if min_lots_count > 1:
                    print(f"{tag} {Fore.WHITE}{symbol} | Lot: {lot_price_usdt:.8f} USDT (x{LEVERAGE}) | Lot size: {lot_size:.8f} × {min_lots_count:.0f} | Price: {last:.8f} | {change_color}Δ: {daily_change:.2f}% | {rsi_color}RSI: {rsi_val:.2f}")
                else:
                    print(f"{tag} {Fore.WHITE}{symbol} | Lot: {lot_price_usdt:.8f} USDT (x{LEVERAGE}) | Lot size: {lot_size:.8f} | Price: {last:.8f} | {change_color}Δ: {daily_change:.2f}% | {rsi_color}RSI: {rsi_val:.2f}")
            else:
                if VERBOSE:
                    if min_lots_count > 1:
                        print(Fore.WHITE + f"[OK] {symbol} (no change) | Lot size: {lot_size:.8f} × {min_lots_count:.0f}")
                    else:
                        print(Fore.WHITE + f"[OK] {symbol} (no change)")

            time.sleep(DELAY)

        except Exception as e:
            if VERBOSE:
                print(Fore.RED + f"Error processing {symbol}: {e}")
                traceback.print_exc()
            else:
                print(Fore.RED + f"Error processing {symbol}: {e}")
            time.sleep(DELAY)
            continue

    # Final output from DB — sorted by RSI descending
    print("\n" + Fore.GREEN + "Top pairs by RSI (descending):\n")
    try:
        cur.execute("SELECT symbol, lot_price_usdt, price, daily_change, rsi, updated_at FROM futures_pairs ORDER BY rsi DESC")
        rows = cur.fetchall()
    except Exception as e:
        print(Fore.RED + f"Could not read from DB: {e}")
        if VERBOSE:
            traceback.print_exc()
        rows = []

    if not rows:
        print(Fore.RED + "Database is empty — nothing was recorded.")
    else:
        for r in rows:
            sym, lotp, price, dc, rsi_v, updated_at = r
            ch_col = color_for_change(dc)
            r_col = color_for_rsi(rsi_v)
            print(f"{Fore.WHITE}{sym} | Lot: {lotp:.8f} USDT (x{LEVERAGE}) | Price: {price:.8f} | {ch_col}Δ: {dc:.2f}% | {r_col}RSI: {rsi_v:.2f} {Style.DIM}({updated_at})")

    print("\n" + Fore.CYAN + f"Markets processed: {processed} | Matched by lot: {matched} | New: {new_count} | Updated: {updated_count} | Skipped (NaN RSI): {skipped_nan_rsi}")

if __name__ == "__main__":
    try:
        main()
    finally:
        try:
            conn.close()
        except Exception:
            pass
