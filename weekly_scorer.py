#!/usr/bin/env python3
"""
weekly_scorer.py — Wöchentlicher Stock + Crypto Advisor mit QVM.

Scoring-Dimensionen:
    Quality   (20%): ROE, D/E, OpMargin, FCF Yield — via yfinance fundamentals
    Value     (15%): Forward PE, P/B, EV/EBITDA, Div Yield — via yfinance
    Momentum  (35%): SMA Trend, RSI, BB, 52w Position — via DB + yfinance
    Sentiment (30%): Newsletter + News Sentiment — via DB

Output: Discord + Email mit Rankings, DB-Storage.

Cron (Freitag 17:00 CET):
    0 17 * * 5 cd ~/stock_advisor && python3 weekly_scorer.py >> scorer.log 2>&1
"""

import os
import sys
import json
import argparse
import logging
import smtplib
from datetime import date, timedelta
from email.mime.text import MIMEText

import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv

from qvm_fundamentals import fetch_fundamentals, score_quality, score_value, score_momentum_from_db

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

DB_CONFIG = {
    "host":     os.getenv("STOCK_DB_HOST",   "localhost"),
    "port":     int(os.getenv("STOCK_DB_PORT", "5432")),
    "dbname":   os.getenv("STOCK_DB_NAME",   "stock_advisor"),
    "user":     os.getenv("STOCK_DB_USER",   "collector"),
    "password": os.getenv("STOCK_DB_PASS",   ""),
}

# Score-Gewichte (müssen 1.0 ergeben)
WEIGHT_Q = float(os.getenv("WEIGHT_Q", "0.20"))
WEIGHT_V = float(os.getenv("WEIGHT_V", "0.15"))
WEIGHT_M = float(os.getenv("WEIGHT_M", "0.35"))
WEIGHT_SENT = float(os.getenv("WEIGHT_SENT", "0.30"))

WEEKLY_BUDGET = float(os.getenv("WEEKLY_BUDGET", "200"))
BUY_THRESHOLD = float(os.getenv("BUY_THRESHOLD", "0.60"))
MAX_BUYS = int(os.getenv("MAX_BUYS", "3"))

GMAIL_USER = os.getenv("GMAIL_USER")
GMAIL_PASS = os.getenv("GMAIL_APP_PASSWORD")
EMAIL_RECIPIENTS = os.getenv("EMAIL_RECIPIENTS", GMAIL_USER or "")


def get_conn():
    return psycopg2.connect(**DB_CONFIG)


# ── Daten laden ──────────────────────────────────────────────────────

def ensure_qvm_columns(conn):
    """Fügt QVM-Spalten zur weekly_scores Tabelle hinzu falls nicht vorhanden."""
    with conn.cursor() as cur:
        for col in ["score_quality", "score_value", "score_momentum"]:
            cur.execute(f"""
                DO $$
                BEGIN
                    ALTER TABLE weekly_scores ADD COLUMN {col} NUMERIC(5,3);
                EXCEPTION
                    WHEN duplicate_column THEN NULL;
                END $$;
            """)
    conn.commit()


def get_all_tickers(conn) -> list[dict]:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""
            SELECT
                t.raw_ticker,
                COALESCE(tm.yf_ticker, t.raw_ticker) AS yf_ticker,
                t.name,
                t.sektor,
                t.prio,
                t.in_portfolio,
                t.shares,
                t.avg_buy
            FROM (
                SELECT ticker AS raw_ticker, name, sektor, NULL AS prio,
                       TRUE AS in_portfolio, shares, avg_buy
                FROM holdings
                UNION ALL
                SELECT ticker, name, sektor, prio,
                       FALSE, NULL, NULL
                FROM watchlist
                WHERE ticker NOT IN (SELECT ticker FROM holdings)
            ) t
            LEFT JOIN ticker_map tm ON tm.broker_ticker = t.raw_ticker
        """)
        return [dict(r) for r in cur.fetchall()]


def get_latest_technicals(conn, yf_ticker: str) -> dict | None:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""
            SELECT close, rsi_14, atr_pct, bb_position, sma_50, sma_200
            FROM stock_history
            WHERE ticker = %s
            ORDER BY date DESC
            LIMIT 1
        """, (yf_ticker,))
        row = cur.fetchone()
        return dict(row) if row else None


def get_sentiment_7d(conn, yf_ticker: str) -> dict:
    since = date.today() - timedelta(days=7)
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""
            SELECT
                AVG(sentiment) AS avg_sentiment,
                MAX(relevance) AS max_relevance,
                COUNT(*) AS news_count
            FROM (
                SELECT sentiment, relevance FROM email_sentiment
                WHERE ticker = %s AND date >= %s
                UNION ALL
                SELECT sentiment, relevance FROM news_sentiment
                WHERE ticker = %s AND date >= %s
            ) combined
        """, (yf_ticker, since, yf_ticker, since))
        row = cur.fetchone()
        return {
            "avg_sentiment": float(row["avg_sentiment"]) if row["avg_sentiment"] else 0.0,
            "max_relevance": int(row["max_relevance"]) if row["max_relevance"] else 0,
            "news_count": int(row["news_count"]),
        }


# ── Sentiment Scoring ────────────────────────────────────────────────

def score_sentiment(sent: dict) -> tuple[float, str]:
    avg_s = sent["avg_sentiment"]
    max_r = sent["max_relevance"]
    count = sent["news_count"]
    reasons = []

    if count == 0:
        return 0.5, "Keine News"

    base = (avg_s + 2.0) / 4.0
    base = max(0.0, min(1.0, base))

    if max_r >= 4:
        relevance_mult = 1.2
        reasons.append(f"Rel:{max_r}")
    elif max_r >= 3:
        relevance_mult = 1.1
    else:
        relevance_mult = 1.0

    if count >= 5:
        conf_mult = 1.1
        reasons.append(f"{count}src")
    elif count <= 1:
        conf_mult = 0.85
        reasons.append("LowCov")
    else:
        conf_mult = 1.0

    score = base * relevance_mult * conf_mult
    score = max(0.0, min(1.0, score))

    if avg_s > 0.5:
        reasons.append(f"Pos({avg_s:+.1f})")
    elif avg_s < -0.5:
        reasons.append(f"Neg({avg_s:+.1f})")
    else:
        reasons.append(f"Neut({avg_s:+.1f})")

    return round(score, 3), "; ".join(reasons)


# ── QVM + Sentiment Scoring ──────────────────────────────────────────

def score_ticker(tech: dict | None, sent: dict, fund: dict, ticker_info: dict) -> dict:
    """
    Kombiniertes Scoring: Quality + Value + Momentum + Sentiment.

    Gewichte:
        Q (20%): Fundamentale Unternehmensqualität
        V (15%): Ist es gerade günstig bewertet?
        M (35%): Trend, RSI, BB, 52w — technisches Momentum
        S (30%): News + Newsletter Sentiment
    """
    q_score, q_reason = score_quality(fund)
    v_score, v_reason = score_value(fund)
    m_score, m_reason = score_momentum_from_db(tech, fund)
    s_score, s_reason = score_sentiment(sent)

    total = (
        q_score * WEIGHT_Q +
        v_score * WEIGHT_V +
        m_score * WEIGHT_M +
        s_score * WEIGHT_SENT
    )
    total = round(total, 3)

    # Prio-Boost für High-Priority Watchlist-Ticker
    prio = ticker_info.get("prio")
    if prio == "H":
        total = min(1.0, total * 1.05)
        total = round(total, 3)

    if total >= BUY_THRESHOLD:
        action = "BUY"
    elif total >= 0.40:
        action = "HOLD"
    else:
        action = "SKIP"

    reasoning = (
        f"Q:{q_score:.2f} ({q_reason}) | "
        f"V:{v_score:.2f} ({v_reason}) | "
        f"M:{m_score:.2f} ({m_reason}) | "
        f"S:{s_score:.2f} ({s_reason})"
    )

    return {
        "score_total": total,
        "score_quality": q_score,
        "score_value": v_score,
        "score_momentum": m_score,
        "score_tech": m_score,  # Backward compat mit altem DB-Schema
        "score_sentiment": s_score,
        "action": action,
        "reasoning": reasoning,
        "in_portfolio": ticker_info.get("in_portfolio"),
        "shares": ticker_info.get("shares"),
        "name": ticker_info.get("name", ""),
        "sektor": ticker_info.get("sektor", ""),
    }


# ── Budget Allocation ────────────────────────────────────────────────

def allocate_budget(scored: list[dict], budget: float, max_buys: int) -> list[dict]:
    buys = sorted(
        [s for s in scored if s["action"] == "BUY"],
        key=lambda x: x["score_total"],
        reverse=True,
    )
    for s in buys[max_buys:]:
        s["action"] = "HOLD"
    buys = buys[:max_buys]
    if not buys:
        return scored
    total_score = sum(s["score_total"] for s in buys)
    if total_score == 0:
        return scored
    for s in buys:
        s["suggested_eur"] = round(budget * s["score_total"] / total_score, 2)
    return scored


# ── Helpers ──────────────────────────────────────────────────────────

def split_stock_crypto(scored):
    stocks = [s for s in scored if s.get("sektor") != "Crypto"]
    cryptos = [s for s in scored if s.get("sektor") == "Crypto"]
    return stocks, cryptos


def format_section(title, scored):
    scored.sort(key=lambda x: x["score_total"], reverse=True)
    buys = [s for s in scored if s["action"] == "BUY"]
    holds = [s for s in scored if s["action"] == "HOLD"]
    skips = [s for s in scored if s["action"] == "SKIP"]

    lines = [f"\n  {title}\n  {'─' * 60}"]

    if buys:
        lines.append("")
        for i, s in enumerate(buys, 1):
            eur = s.get("suggested_eur", 0)
            tag = "📌" if s.get("in_portfolio") else "👀"
            name = s.get("name") or s["raw_ticker"]
            q = s.get("score_quality", 0)
            v = s.get("score_value", 0)
            m = s.get("score_momentum", 0)
            lines.append(
                f"  {i}. {s['raw_ticker']:<10s}  {name:<20s}  "
                f"€{eur:>5.0f}  QVM {q:.2f}/{v:.2f}/{m:.2f}  "
                f"Total {s['score_total']:.2f}  {tag}"
            )
        total = sum(s.get("suggested_eur", 0) for s in buys)
        lines.append(f"  {'':46s}  €{total:>5.0f}  Total")
    else:
        lines.append("  Keine BUY-Empfehlungen")

    if holds:
        lines.append(f"\n  HOLD: " + ", ".join(
            f"{s['raw_ticker']}{'📌' if s.get('in_portfolio') else ''}"
            for s in holds[:8]
        ))
        if len(holds) > 8:
            lines.append(f"        +{len(holds)-8} weitere")

    if skips:
        lines.append(f"  SKIP: " + ", ".join(s["raw_ticker"] for s in skips[:8]))
        if len(skips) > 8:
            lines.append(f"        +{len(skips)-8} weitere")

    return "\n".join(lines)


# ── Output ───────────────────────────────────────────────────────────

def print_results(stocks, cryptos):
    print(f"\n{'=' * 65}")
    print(f"  STOCK + CRYPTO ADVISOR (QVM) — {date.today()}")
    print(f"  Budget: €{WEEKLY_BUDGET:.0f} each | Top {MAX_BUYS}")
    print(f"  Weights: Q={WEIGHT_Q:.0%} V={WEIGHT_V:.0%} M={WEIGHT_M:.0%} S={WEIGHT_SENT:.0%}")
    print(f"{'=' * 65}")
    print(format_section("📈 STOCKS", stocks))
    print(format_section("🪙 CRYPTO", cryptos))

    stock_buys = [s for s in stocks if s["action"] == "BUY"]
    crypto_buys = [s for s in cryptos if s["action"] == "BUY"]
    if stock_buys or crypto_buys:
        print(f"\n  Detail:\n")
        for s in stock_buys + crypto_buys:
            print(f"  {s['raw_ticker']}: {s['reasoning']}")
    print(f"\n{'=' * 65}\n")


def store_scores(conn, scored: list[dict]):
    today = date.today()
    with conn.cursor() as cur:
        for s in scored:
            cur.execute("""
                INSERT INTO weekly_scores
                    (run_date, ticker, yf_ticker, score_total, score_tech,
                     score_sentiment, score_quality, score_value, score_momentum,
                     action, suggested_eur, reasoning)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (run_date, ticker) DO UPDATE SET
                    score_total     = EXCLUDED.score_total,
                    score_tech      = EXCLUDED.score_tech,
                    score_sentiment = EXCLUDED.score_sentiment,
                    score_quality   = EXCLUDED.score_quality,
                    score_value     = EXCLUDED.score_value,
                    score_momentum  = EXCLUDED.score_momentum,
                    action          = EXCLUDED.action,
                    suggested_eur   = EXCLUDED.suggested_eur,
                    reasoning       = EXCLUDED.reasoning
            """, (
                today, s["raw_ticker"], s["yf_ticker"],
                s["score_total"], s["score_tech"], s["score_sentiment"],
                s.get("score_quality"), s.get("score_value"), s.get("score_momentum"),
                s["action"], s.get("suggested_eur"), s["reasoning"],
            ))
    conn.commit()
    log.info("%d Scores gespeichert", len(scored))


def send_discord_alert(stocks, cryptos):
    webhook_url = os.getenv("DISCORD_STOCK_WEBHOOK")
    if not webhook_url:
        return

    stock_buys = [s for s in stocks if s["action"] == "BUY"]
    crypto_buys = [s for s in cryptos if s["action"] == "BUY"]
    if not stock_buys and not crypto_buys:
        return

    import requests

    lines = [f"📊 **Stock + Crypto Advisor (QVM) — {date.today()}**"]

    if stock_buys:
        lines.append("\n**📈 Stocks**")
        for s in stock_buys:
            eur = s.get("suggested_eur", 0)
            name = s.get("name") or s["raw_ticker"]
            tag = "📌" if s.get("in_portfolio") else "👀"
            q = s.get("score_quality", 0)
            v = s.get("score_value", 0)
            m = s.get("score_momentum", 0)
            lines.append(
                f"🟢 **{s['raw_ticker']}** {name} — "
                f"Q:{q:.2f} V:{v:.2f} M:{m:.2f} = {s['score_total']:.2f} — "
                f"€{eur:.0f} {tag}"
            )

    if crypto_buys:
        lines.append("\n**🪙 Crypto**")
        for s in crypto_buys:
            eur = s.get("suggested_eur", 0)
            name = s.get("name") or s["raw_ticker"]
            tag = "📌" if s.get("in_portfolio") else "👀"
            lines.append(
                f"🟢 **{s['raw_ticker']}** {name} — {s['score_total']:.2f} — €{eur:.0f} {tag}"
            )

    s_total = sum(s.get("suggested_eur", 0) for s in stock_buys)
    c_total = sum(s.get("suggested_eur", 0) for s in crypto_buys)
    lines.append(f"\n💰 Stocks €{s_total:.0f} + Crypto €{c_total:.0f} = €{s_total+c_total:.0f}")

    try:
        requests.post(webhook_url, json={"content": "\n".join(lines)}, timeout=10)
        log.info("Discord gesendet")
    except Exception as e:
        log.warning("Discord Fehler: %s", e)


def send_email_report(stocks, cryptos):
    if not GMAIL_USER or not GMAIL_PASS:
        return

    lines = [
        f"STOCK + CRYPTO ADVISOR (QVM) — Weekly Report {date.today()}",
        f"Budget: €{WEEKLY_BUDGET:.0f} each | Top {MAX_BUYS}",
        f"Weights: Q={WEIGHT_Q:.0%} V={WEIGHT_V:.0%} M={WEIGHT_M:.0%} S={WEIGHT_SENT:.0%}",
        "=" * 65,
    ]

    for title, scored in [("📈 STOCKS", stocks), ("🪙 CRYPTO", cryptos)]:
        scored_s = sorted(scored, key=lambda x: x["score_total"], reverse=True)
        buys = [s for s in scored_s if s["action"] == "BUY"]
        holds = [s for s in scored_s if s["action"] == "HOLD"]
        skips = [s for s in scored_s if s["action"] == "SKIP"]

        lines.append(f"\n{title}\n{'─' * 55}")
        if buys:
            for i, s in enumerate(buys, 1):
                eur = s.get("suggested_eur", 0)
                tag = "📌 Portfolio" if s.get("in_portfolio") else "👀 Watchlist"
                name = s.get("name") or s["raw_ticker"]
                q = s.get("score_quality", 0)
                v = s.get("score_value", 0)
                m = s.get("score_momentum", 0)
                lines.append(f"\n  {i}. {s['raw_ticker']:<10s}  {name}")
                lines.append(f"     Q:{q:.2f}  V:{v:.2f}  M:{m:.2f}  S:{s['score_sentiment']:.2f}  = {s['score_total']:.2f}")
                lines.append(f"     €{eur:.0f}  |  {tag}")
                lines.append(f"     {s['reasoning']}")
            total = sum(s.get("suggested_eur", 0) for s in buys)
            lines.append(f"\n  Total: €{total:.0f}")
        else:
            lines.append("  Keine BUY-Empfehlungen")

        if holds:
            lines.append(f"\n  HOLD ({len(holds)}):")
            for s in holds:
                name = s.get("name") or ""
                tag = " 📌" if s.get("in_portfolio") else ""
                lines.append(f"    {s['raw_ticker']:<10s} {name:<20s} {s['score_total']:.2f}{tag}")

        if skips:
            lines.append(f"\n  SKIP ({len(skips)}):")
            for s in skips:
                name = s.get("name") or ""
                lines.append(f"    {s['raw_ticker']:<10s} {name:<20s} {s['score_total']:.2f}")

    lines.append(f"\n{'=' * 65}")
    lines.append("Automatisch generiert von Stock + Crypto Advisor (QVM)")

    body = "\n".join(lines)
    msg = MIMEText(body, _charset="utf-8")
    msg["Subject"] = f"📊 Weekly QVM Advisor — {date.today()}"
    msg["From"] = GMAIL_USER
    msg["To"] = EMAIL_RECIPIENTS

    try:
        server = smtplib.SMTP("smtp.gmail.com", 587)
        server.starttls()
        server.login(GMAIL_USER, GMAIL_PASS)
        server.send_message(msg)
        server.quit()
        log.info("Email gesendet")
    except Exception as e:
        log.warning("Email Fehler: %s", e)


# ── Main ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--budget", type=float, default=None)
    args = parser.parse_args()

    global WEEKLY_BUDGET
    if args.budget:
        WEEKLY_BUDGET = args.budget

    conn = get_conn()
    try:
        # DB-Schema erweitern falls nötig
        ensure_qvm_columns(conn)

        tickers = get_all_tickers(conn)
        log.info("%d Ticker laden", len(tickers))

        scored = []
        for t in tickers:
            yf_ticker = t["yf_ticker"]
            log.info("%-10s  Scoring...", yf_ticker)

            tech = get_latest_technicals(conn, yf_ticker)
            sent = get_sentiment_7d(conn, yf_ticker)
            fund = fetch_fundamentals(yf_ticker)

            result = score_ticker(tech, sent, fund, t)
            result["raw_ticker"] = t["raw_ticker"]
            result["yf_ticker"] = yf_ticker
            scored.append(result)

        stocks, cryptos = split_stock_crypto(scored)
        stocks = allocate_budget(stocks, WEEKLY_BUDGET, MAX_BUYS)
        cryptos = allocate_budget(cryptos, WEEKLY_BUDGET, MAX_BUYS)

        print_results(stocks, cryptos)

        if not args.dry_run:
            store_scores(conn, stocks + cryptos)
            send_discord_alert(stocks, cryptos)
            send_email_report(stocks, cryptos)
        else:
            log.info("Dry-run: nicht gespeichert")
    finally:
        conn.close()


if __name__ == "__main__":
    main()