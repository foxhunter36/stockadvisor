#!/usr/bin/env python3
"""
fetch_news.py — News-Sentiment Pipeline für Stock Advisor

1. Holt aktuelle News pro Ticker via yfinance
2. Batch-Scoring der Headlines via Claude API (sentiment + relevance)
3. Speichert in news_sentiment Tabelle

Cron (täglich, Trading Station oder Server):
    0 8 * * 1-5 cd ~/stock-advisor && python fetch_news.py >> fetch_news.log 2>&1

Umgebungsvariablen (.env):
    ANTHROPIC_API_KEY
    STOCK_DB_HOST / STOCK_DB_PORT / STOCK_DB_NAME / STOCK_DB_USER / STOCK_DB_PASS
"""

import os
import sys
import json
import logging
from datetime import datetime, date, timedelta

import psycopg2
from psycopg2.extras import execute_values
import yfinance as yf
from anthropic import Anthropic, APIError
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

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")

# Max Headlines pro Claude-Call (Kontext-Budget)
BATCH_SIZE = int(os.getenv("NEWS_BATCH_SIZE", "60"))


def get_conn():
    return psycopg2.connect(**DB_CONFIG)


def get_all_yf_tickers(conn) -> list[str]:
    """Alle yfinance-Ticker aus holdings + watchlist (via ticker_map)."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT COALESCE(tm.yf_ticker, t.raw_ticker) AS yf_ticker
            FROM (
                SELECT ticker AS raw_ticker FROM holdings
                UNION
                SELECT ticker FROM watchlist
            ) t
            LEFT JOIN ticker_map tm ON tm.broker_ticker = t.raw_ticker
        """)
        return [r[0] for r in cur.fetchall()]


def fetch_yf_news(ticker: str, max_items: int = 10) -> list[dict]:
    """Holt News für einen Ticker via yfinance."""
    try:
        t = yf.Ticker(ticker)
        news = t.news or []
    except Exception as e:
        log.warning("%-10s  yfinance news Fehler: %s", ticker, e)
        return []

    result = []
    for item in news[:max_items]:
        content = item.get("content", item)
        title = content.get("title", "")
        provider = content.get("provider", {})
        publisher = provider.get("displayName", "") if isinstance(provider, dict) else str(provider)
        canonical = content.get("canonicalUrl", {})
        url = canonical.get("url", "") if isinstance(canonical, dict) else ""

        pub_str = content.get("pubDate", "")
        if pub_str:
            try:
                pub_date = datetime.fromisoformat(pub_str.replace("Z", "+00:00")).date()
            except (ValueError, TypeError):
                pub_date = date.today()
        else:
            pub_date = date.today()

        if title:
            result.append({
                "ticker": ticker,
                "title": title,
                "publisher": publisher,
                "url": url,
                "date": pub_date,
            })

    return result


def fetch_all_news(tickers: list[str]) -> list[dict]:
    """Sammelt News für alle Ticker."""
    all_news = []
    for ticker in tickers:
        news = fetch_yf_news(ticker)
        log.info("%-10s  %d Headlines", ticker, len(news))
        all_news.extend(news)
    return all_news


def score_with_claude(news_items: list[dict]) -> dict:
    """
    Schickt Headlines an Claude API, bekommt Sentiment + Relevance zurück.
    Returns: {(ticker, title): {"sentiment": float, "relevance": int}}
    """
    if not ANTHROPIC_API_KEY:
        log.error("Kein ANTHROPIC_API_KEY gesetzt")
        return {}

    if not news_items:
        return {}

    client = Anthropic(api_key=ANTHROPIC_API_KEY)

    # Headlines als nummerierte Liste
    headlines_block = ""
    for i, item in enumerate(news_items):
        headlines_block += f'{i}: [{item["ticker"]}] {item["title"]} (via {item["publisher"]})\n'

    prompt = f"""Du bist ein Finanzanalyse-Assistent. Bewerte folgende News-Headlines.

Für jede Headline:
- sentiment: -2.0 (sehr negativ) bis +2.0 (sehr positiv). 0.0 = neutral.
- relevance: 1 (irrelevant/Werbung) bis 5 (earnings, FDA, major contract, M&A etc.)

REGELN:
- Generische Markt-Headlines ohne Ticker-Bezug: relevance 1-2
- Earnings/Guidance/Analyst-Ratings: relevance 4-5
- Regulatorische Entscheidungen: relevance 4-5
- Promo/Clickbait: relevance 1, sentiment 0.0
- Wenn die Headline für den spezifischen Ticker nicht direkt relevant ist: relevance 1

Headlines:
{headlines_block}

Antworte NUR mit einem JSON-Objekt. Keys = Headline-Nummern (als String).
Beispiel:
{{
  "0": {{"sentiment": 1.2, "relevance": 4}},
  "1": {{"sentiment": -0.5, "relevance": 2}}
}}

Kein Markdown, keine Erklärung, nur JSON."""

    try:
        resp = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip()

        # Manchmal gibt Claude ```json zurück
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()

        scores = json.loads(raw)
    except (APIError, json.JSONDecodeError, IndexError, KeyError) as e:
        log.error("Claude Scoring Fehler: %s", e)
        return {}

    # Map zurück auf (ticker, title)
    result = {}
    for idx_str, score in scores.items():
        try:
            idx = int(idx_str)
            item = news_items[idx]
            result[(item["ticker"], item["title"])] = {
                "sentiment": float(score.get("sentiment", 0.0)),
                "relevance": int(score.get("relevance", 1)),
            }
        except (ValueError, IndexError):
            continue

    return result


def store_news_sentiment(conn, news_items: list[dict], scores: dict):
    """Speichert gescorete News in news_sentiment Tabelle."""
    rows = []
    for item in news_items:
        key = (item["ticker"], item["title"])
        score = scores.get(key, {"sentiment": 0.0, "relevance": 1})

        rows.append((
            item["date"],
            item["ticker"],
            item["title"],
            item["publisher"],
            item["url"],
            score["sentiment"],
            score["relevance"],
        ))

    if not rows:
        return

    with conn.cursor() as cur:
        execute_values(cur, """
            INSERT INTO news_sentiment
                (date, ticker, title, publisher, url, sentiment, relevance)
            VALUES %s
            ON CONFLICT (date, ticker, title) DO UPDATE SET
                sentiment = EXCLUDED.sentiment,
                relevance = EXCLUDED.relevance
        """, rows)
    conn.commit()
    log.info("%d News-Sentiments gespeichert", len(rows))


def main():
    conn = get_conn()
    try:
        tickers = get_all_yf_tickers(conn)
        log.info("%d Ticker für News-Fetch", len(tickers))

        all_news = fetch_all_news(tickers)
        log.info("%d Headlines total gesammelt", len(all_news))

        if not all_news:
            log.info("Keine News gefunden, beende.")
            return

        # Batch-Scoring (in Chunks von BATCH_SIZE)
        all_scores = {}
        for i in range(0, len(all_news), BATCH_SIZE):
            batch = all_news[i:i + BATCH_SIZE]
            log.info("Scoring Batch %d-%d (%d Headlines)",
                     i, i + len(batch), len(batch))
            batch_scores = score_with_claude(batch)
            all_scores.update(batch_scores)

        log.info("%d von %d Headlines gescored", len(all_scores), len(all_news))

        store_news_sentiment(conn, all_news, all_scores)
    finally:
        conn.close()

    log.info("Fertig.")


if __name__ == "__main__":
    main()
