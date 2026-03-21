#!/usr/bin/env python3
"""
sync_portfolio.py

1. Liest portfolio_template.xlsx (Portfolio + Watchlist Sheets)
2. Upsert in PostgreSQL: holdings + watchlist
3. Resolved broker_ticker → yf_ticker via ticker_map
4. Lädt historische OHLCV + Technicals via yfinance für alle Ticker
5. Speichert in stock_history

Ausführen:
    python sync_portfolio.py                  # Excel sync + History update
    python sync_portfolio.py --history-only   # Nur yfinance update
    python sync_portfolio.py --excel-only     # Nur Excel sync
"""

import os
import sys
import argparse
import logging
from datetime import datetime, date, timedelta

import pandas as pd
import numpy as np
import psycopg2
from psycopg2.extras import execute_values
import yfinance as yf
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Config ───────────────────────────────────────────────────────────
EXCEL_PATH = os.getenv("PORTFOLIO_EXCEL", "portfolio_template.xlsx")

DB_CONFIG = {
    "host":     os.getenv("STOCK_DB_HOST",   "192.168.0.189"),
    "port":     int(os.getenv("STOCK_DB_PORT", "5432")),
    "dbname":   os.getenv("STOCK_DB_NAME",   "stock_advisor"),
    "user":     os.getenv("STOCK_DB_USER",   "collector"),
    "password": os.getenv("STOCK_DB_PASS",   ""),
}

HISTORY_DAYS = int(os.getenv("HISTORY_DAYS", "365"))


# ── DB ───────────────────────────────────────────────────────────────
def get_conn():
    return psycopg2.connect(**DB_CONFIG)


# ── Ticker Mapping ───────────────────────────────────────────────────
def resolve_yf_ticker(conn, broker_ticker: str) -> str:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT yf_ticker FROM ticker_map WHERE broker_ticker = %s",
            (broker_ticker,),
        )
        row = cur.fetchone()
        return row[0] if row else broker_ticker


def get_all_tickers(conn) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT t.raw_ticker,
                   COALESCE(tm.yf_ticker, t.raw_ticker) AS yf_ticker
            FROM (
                SELECT ticker AS raw_ticker FROM holdings
                UNION
                SELECT ticker FROM watchlist
            ) t
            LEFT JOIN ticker_map tm ON tm.broker_ticker = t.raw_ticker
        """)
        return [{"raw": r[0], "yf": r[1]} for r in cur.fetchall()]


# ── Technicals ───────────────────────────────────────────────────────
def _rsi(close: pd.Series, period=14) -> pd.Series:
    delta = close.diff()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    rs    = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _atr_pct(df: pd.DataFrame, period=14) -> pd.Series:
    tr = pd.concat([
        df["High"] - df["Low"],
        (df["High"] - df["Close"].shift()).abs(),
        (df["Low"]  - df["Close"].shift()).abs(),
    ], axis=1).max(axis=1)
    atr = tr.rolling(period).mean()
    return atr / df["Close"]


def _bb_position(close: pd.Series, period=20) -> pd.Series:
    sma  = close.rolling(period).mean()
    std  = close.rolling(period).std()
    upper = sma + 2 * std
    lower = sma - 2 * std
    band  = (upper - lower).replace(0, np.nan)
    return (close - lower) / band


def add_technicals(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["rsi_14"]      = _rsi(df["Close"])
    df["atr_pct"]     = _atr_pct(df)
    df["bb_position"] = _bb_position(df["Close"])
    df["sma_50"]      = df["Close"].rolling(50).mean()
    df["sma_200"]     = df["Close"].rolling(200).mean()
    return df


def _f(val):
    if val is None:
        return None
    try:
        v = float(val)
        return None if np.isnan(v) else v
    except (TypeError, ValueError):
        return None


# ── Excel sync ───────────────────────────────────────────────────────
def sync_excel(conn):
    log.info("Lese Excel: %s", EXCEL_PATH)
    xls = pd.ExcelFile(EXCEL_PATH)

    # ── Portfolio
    df_port = xls.parse("Portfolio")
    df_port.columns = df_port.columns.str.strip()

    required = {"Ticker", "Shares", "Avg_Buy_EUR"}
    missing  = required - set(df_port.columns)
    if missing:
        raise ValueError(f"Portfolio-Sheet fehlt Spalten: {missing}")

    df_port = df_port.dropna(subset=["Ticker", "Shares", "Avg_Buy_EUR"])
    df_port["Ticker"] = df_port["Ticker"].str.upper().str.strip()

    with conn.cursor() as cur:
        for _, row in df_port.iterrows():
            cur.execute("""
                INSERT INTO holdings (ticker, name, shares, avg_buy, broker, sektor, notizen, updated)
                VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (ticker, broker) DO UPDATE SET
                    name    = EXCLUDED.name,
                    shares  = EXCLUDED.shares,
                    avg_buy = EXCLUDED.avg_buy,
                    sektor  = EXCLUDED.sektor,
                    notizen = EXCLUDED.notizen,
                    updated = NOW()
            """, (
                row["Ticker"],
                str(row.get("Name", "") or ""),
                float(row["Shares"]),
                float(row["Avg_Buy_EUR"]),
                str(row.get("Broker", "") or ""),
                str(row.get("Sektor", "") or ""),
                str(row.get("Notizen", "") or ""),
            ))
    conn.commit()
    log.info("Portfolio: %d Positionen upserted", len(df_port))

    # ── Watchlist
    if "Watchlist" not in xls.sheet_names:
        log.warning("Kein Watchlist-Sheet gefunden, übersprungen.")
        return

    df_watch = xls.parse("Watchlist")
    df_watch.columns = df_watch.columns.str.strip()
    df_watch = df_watch.dropna(subset=["Ticker"])
    df_watch["Ticker"] = df_watch["Ticker"].str.upper().str.strip()

    with conn.cursor() as cur:
        for _, row in df_watch.iterrows():
            cur.execute("""
                INSERT INTO watchlist (ticker, name, sektor, prio, max_position, notizen, updated)
                VALUES (%s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (ticker) DO UPDATE SET
                    name         = EXCLUDED.name,
                    sektor       = EXCLUDED.sektor,
                    prio         = EXCLUDED.prio,
                    max_position = EXCLUDED.max_position,
                    notizen      = EXCLUDED.notizen,
                    updated      = NOW()
            """, (
                row["Ticker"],
                str(row.get("Name", "") or ""),
                str(row.get("Sektor", "") or ""),
                (lambda p: p if p in ('H','M','L') else 'M')(str(row.get("Prio", "") or "M")[0].upper()),
                _f(row.get("Max_Position_EUR")),
                str(row.get("Notizen", "") or ""),
            ))
    conn.commit()
    log.info("Watchlist: %d Ticker upserted", len(df_watch))


# ── yfinance history ─────────────────────────────────────────────────
def latest_date_in_db(conn, yf_ticker: str):
    with conn.cursor() as cur:
        cur.execute("SELECT MAX(date) FROM stock_history WHERE ticker = %s", (yf_ticker,))
        return cur.fetchone()[0]


def fetch_and_store_history(conn, yf_ticker: str):
    last = latest_date_in_db(conn, yf_ticker)

    if last is None:
        start = (datetime.today() - timedelta(days=HISTORY_DAYS)).strftime("%Y-%m-%d")
        log.info("%-10s  Vollbackfill (%d Tage)", yf_ticker, HISTORY_DAYS)
    elif (date.today() - last).days <= 1:
        log.info("%-10s  Aktuell, übersprungen", yf_ticker)
        return
    else:
        start = (last + timedelta(days=1)).strftime("%Y-%m-%d")
        log.info("%-10s  Update ab %s", yf_ticker, start)

    try:
        raw = yf.download(yf_ticker, start=start, progress=False, auto_adjust=True)
    except Exception as e:
        log.error("%-10s  yfinance Fehler: %s", yf_ticker, e)
        return

    if raw.empty:
        log.warning("%-10s  Keine Daten von yfinance", yf_ticker)
        return

    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)

    raw = add_technicals(raw)
    raw = raw.dropna(subset=["Close"])

    rows = []
    for dt, row in raw.iterrows():
        rows.append((
            yf_ticker,
            dt.date(),
            _f(row.get("Open")),
            _f(row.get("High")),
            _f(row.get("Low")),
            _f(row.get("Close")),
            int(row["Volume"]) if not pd.isna(row.get("Volume", float("nan"))) else None,
            _f(row.get("rsi_14")),
            _f(row.get("atr_pct")),
            _f(row.get("bb_position")),
            _f(row.get("sma_50")),
            _f(row.get("sma_200")),
        ))

    if not rows:
        return

    with conn.cursor() as cur:
        execute_values(cur, """
            INSERT INTO stock_history
                (ticker, date, open, high, low, close, volume,
                 rsi_14, atr_pct, bb_position, sma_50, sma_200)
            VALUES %s
            ON CONFLICT (ticker, date) DO UPDATE SET
                close       = EXCLUDED.close,
                volume      = EXCLUDED.volume,
                rsi_14      = EXCLUDED.rsi_14,
                atr_pct     = EXCLUDED.atr_pct,
                bb_position = EXCLUDED.bb_position,
                sma_50      = EXCLUDED.sma_50,
                sma_200     = EXCLUDED.sma_200
        """, rows)
    conn.commit()
    log.info("%-10s  %d Zeilen gespeichert", yf_ticker, len(rows))


def update_history(conn):
    tickers = get_all_tickers(conn)
    log.info("%d Ticker gefunden (Portfolio + Watchlist)", len(tickers))
    for t in tickers:
        fetch_and_store_history(conn, t["yf"])


# ── Main ─────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--excel-only",   action="store_true")
    parser.add_argument("--history-only", action="store_true")
    args = parser.parse_args()

    conn = get_conn()
    try:
        if not args.history_only:
            sync_excel(conn)
        if not args.excel_only:
            update_history(conn)
    finally:
        conn.close()

    log.info("Fertig.")


if __name__ == "__main__":
    main()