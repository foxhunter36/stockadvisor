#!/usr/bin/env python3
"""
parse_sentiment.py — Parsed den [SENTIMENT_BLOCK] aus newsletter_digest
und speichert in email_sentiment Tabelle.

Wird von newsletter_digest_imap.py aufgerufen:
    from parse_sentiment import parse_and_store_sentiment
    parse_and_store_sentiment(claude_response_text)

Oder standalone zum Testen:
    echo '...[SENTIMENT_BLOCK_START]{"IONQ":{"sentiment":1.3,"relevance":4}}[SENTIMENT_BLOCK_END]...' | python parse_sentiment.py
"""

import os
import sys
import json
import re
import logging
from datetime import date

import psycopg2
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

DB_CONFIG = {
    "host":     os.getenv("STOCK_DB_HOST",   "192.168.0.189"),
    "port":     int(os.getenv("STOCK_DB_PORT", "5432")),
    "dbname":   os.getenv("STOCK_DB_NAME",   "stock_advisor"),
    "user":     os.getenv("STOCK_DB_USER",   "postgres"),
    "password": os.getenv("STOCK_DB_PASS",   ""),
}


def get_conn():
    return psycopg2.connect(**DB_CONFIG)


def get_valid_tickers(conn) -> set[str]:
    """Alle Ticker aus holdings + watchlist (+ yf_ticker aus ticker_map)."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT UPPER(ticker) FROM holdings
            UNION
            SELECT UPPER(ticker) FROM watchlist
            UNION
            SELECT UPPER(yf_ticker) FROM ticker_map
        """)
        return {r[0] for r in cur.fetchall()}


def extract_sentiment_block(text: str) -> dict:
    """Extrahiert JSON aus [SENTIMENT_BLOCK_START]...[SENTIMENT_BLOCK_END]."""
    pattern = r'\[SENTIMENT_BLOCK_START\]\s*(.*?)\s*\[SENTIMENT_BLOCK_END\]'
    match = re.search(pattern, text, re.DOTALL)

    if not match:
        log.warning("Kein SENTIMENT_BLOCK gefunden")
        return {}

    raw = match.group(1).strip()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        log.error("Ungültiges JSON im SENTIMENT_BLOCK: %s", e)
        log.debug("Raw: %s", raw[:500])
        return {}

    return data


def store_email_sentiment(conn, sentiments: dict, source: str = "newsletter"):
    """Speichert Ticker-Sentiments in email_sentiment."""
    today = date.today()
    stored = 0

    with conn.cursor() as cur:
        for ticker, scores in sentiments.items():
            if not isinstance(scores, dict):
                continue

            sentiment = float(scores.get("sentiment", 0.0))
            relevance = int(scores.get("relevance", 1))

            # Clamp values
            sentiment = max(-2.0, min(2.0, sentiment))
            relevance = max(1, min(5, relevance))

            cur.execute("""
                INSERT INTO email_sentiment (date, ticker, sentiment, relevance, source)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (date, ticker, source) DO UPDATE SET
                    sentiment = EXCLUDED.sentiment,
                    relevance = EXCLUDED.relevance
            """, (today, ticker.upper().strip(), sentiment, relevance, source))
            stored += 1

    conn.commit()
    log.info("%d Ticker-Sentiments gespeichert (source=%s)", stored, source)
    return stored


def parse_and_store_sentiment(text: str, source: str = "newsletter") -> int:
    """Hauptfunktion: Text → Parse → DB. Returns Anzahl gespeicherter Ticker."""
    sentiments = extract_sentiment_block(text)

    if not sentiments:
        log.info("Keine Sentiments zu speichern")
        return 0

    log.info("Geparste Ticker (roh): %s", list(sentiments.keys()))

    conn = get_conn()
    try:
        valid = get_valid_tickers(conn)
        filtered = {k: v for k, v in sentiments.items() if k.upper().strip() in valid}
        dropped = set(sentiments.keys()) - set(filtered.keys())

        if dropped:
            log.info("Gefiltert (nicht in Holdings/Watchlist): %s", sorted(dropped))
        log.info("Behalte %d von %d Ticker", len(filtered), len(sentiments))

        count = store_email_sentiment(conn, filtered, source)
    finally:
        conn.close()

    return count


# ── Standalone: stdin lesen ──────────────────────────────────────────
if __name__ == "__main__":
    if not sys.stdin.isatty():
        text = sys.stdin.read()
    elif len(sys.argv) > 1:
        with open(sys.argv[1]) as f:
            text = f.read()
    else:
        print("Usage: echo 'text' | python parse_sentiment.py")
        print("       python parse_sentiment.py <file>")
        sys.exit(1)

    count = parse_and_store_sentiment(text)
    print(f"✅ {count} Ticker-Sentiments gespeichert")