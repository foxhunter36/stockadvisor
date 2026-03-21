#!/usr/bin/env python3
"""
weekly_scorer.py — Wöchentlicher Stock + Crypto Advisor

Trend-First DCA Scoring, separate Top N für Stocks und Crypto.
Output: Discord + Email mit beiden Rankings.

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

WEIGHT_TECH = float(os.getenv("WEIGHT_TECH", "0.6"))
WEIGHT_SENT = float(os.getenv("WEIGHT_SENT", "0.4"))
WEEKLY_BUDGET = float(os.getenv("WEEKLY_BUDGET", "200"))
BUY_THRESHOLD = float(os.getenv("BUY_THRESHOLD", "0.65"))
MAX_BUYS = int(os.getenv("MAX_BUYS", "3"))

GMAIL_USER = os.getenv("GMAIL_USER")
GMAIL_PASS = os.getenv("GMAIL_APP_PASSWORD")
EMAIL_RECIPIENTS = os.getenv("EMAIL_RECIPIENTS", GMAIL_USER or "")


def get_conn():
    return psycopg2.connect(**DB_CONFIG)


# ── Daten laden ──────────────────────────────────────────────────────

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


# ── Scoring Logik (Trend-First DCA) ─────────────────────────────────

def score_technical(tech: dict, is_crypto: bool = False) -> tuple[float, str]:
    if not tech or tech.get("close") is None:
        return 0.3, "Keine Daten"

    close = float(tech["close"])
    rsi = float(tech["rsi_14"]) if tech.get("rsi_14") is not None else 50.0
    bb = float(tech["bb_position"]) if tech.get("bb_position") is not None else 0.5
    sma50 = float(tech["sma_50"]) if tech.get("sma_50") is not None else close
    sma200 = float(tech["sma_200"]) if tech.get("sma_200") is not None else close
    atr_pct = float(tech["atr_pct"]) if tech.get("atr_pct") is not None else 0.03

    reasons = []

    # ── TREND (40%)
    if close > sma50 > sma200:
        trend_score = 0.95
        in_uptrend = True
        reasons.append("Uptrend")
    elif close > sma200 and close > sma50:
        trend_score = 0.80
        in_uptrend = True
        reasons.append(">SMA50+200")
    elif close > sma50:
        trend_score = 0.65
        in_uptrend = True
        reasons.append(">SMA50")
    elif close > sma200:
        trend_score = 0.45
        in_uptrend = False
        reasons.append(">SMA200 only")
    elif close < sma50 < sma200:
        trend_score = 0.10
        in_uptrend = False
        reasons.append("Downtrend")
    else:
        trend_score = 0.25
        in_uptrend = False
        reasons.append("<SMA50")

    # ── ENTRY TIMING (25%)
    if in_uptrend:
        if 30 <= rsi <= 45:
            entry_score = 0.95
            reasons.append(f"RSI {rsi:.0f} pullback ★")
        elif 45 < rsi <= 55:
            entry_score = 0.70
            reasons.append(f"RSI {rsi:.0f} neutral")
        elif rsi < 30:
            entry_score = 0.60
            reasons.append(f"RSI {rsi:.0f} deep dip")
        elif 55 < rsi <= 70:
            entry_score = 0.45
            reasons.append(f"RSI {rsi:.0f} elevated")
        else:
            entry_score = 0.15
            reasons.append(f"RSI {rsi:.0f} overbought")
    else:
        if rsi < 30:
            entry_score = 0.20
            reasons.append(f"RSI {rsi:.0f} oversold+down")
        elif rsi < 45:
            entry_score = 0.30
        elif rsi < 55:
            entry_score = 0.40
            reasons.append(f"RSI {rsi:.0f} stabilizing")
        else:
            entry_score = 0.35

    # ── VOLATILITY (20%) — Crypto hat höhere Schwellen
    if is_crypto:
        if atr_pct < 0.03:
            vol_score = 0.80
        elif atr_pct < 0.05:
            vol_score = 0.75
            reasons.append("GoodVol")
        elif atr_pct < 0.08:
            vol_score = 0.55
        elif atr_pct < 0.12:
            vol_score = 0.35
            reasons.append(f"Vol {atr_pct:.0%}")
        else:
            vol_score = 0.15
            reasons.append(f"HighVol {atr_pct:.0%}")
    else:
        if atr_pct < 0.02:
            vol_score = 0.75
        elif atr_pct < 0.035:
            vol_score = 0.85
            reasons.append("GoodVol")
        elif atr_pct < 0.05:
            vol_score = 0.60
        elif atr_pct < 0.08:
            vol_score = 0.35
            reasons.append(f"Vol {atr_pct:.1%}")
        else:
            vol_score = 0.15
            reasons.append(f"HighVol {atr_pct:.0%}")

    # ── MOMENTUM (15%)
    if in_uptrend:
        if 0.3 <= bb <= 0.6:
            mom_score = 0.80
        elif 0.15 <= bb < 0.3:
            mom_score = 0.90
            reasons.append("BB dip buy")
        elif bb < 0.15:
            mom_score = 0.60
        elif bb <= 0.8:
            mom_score = 0.55
        else:
            mom_score = 0.30
            reasons.append("BB high")
    else:
        if bb < 0.2:
            mom_score = 0.15
        elif bb < 0.5:
            mom_score = 0.30
        else:
            mom_score = 0.45

    score = (trend_score * 0.40 + entry_score * 0.25 + vol_score * 0.20 + mom_score * 0.15)
    return round(score, 3), "; ".join(reasons)


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


def score_ticker(tech: dict | None, sent: dict, ticker_info: dict) -> dict:
    is_crypto = ticker_info.get("sektor") == "Crypto"
    tech_score, tech_reason = score_technical(tech, is_crypto)
    sent_score, sent_reason = score_sentiment(sent)

    total = tech_score * WEIGHT_TECH + sent_score * WEIGHT_SENT
    total = round(total, 3)

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

    reasoning = f"T:{tech_score:.2f} ({tech_reason}) | S:{sent_score:.2f} ({sent_reason})"

    return {
        "score_total": total,
        "score_tech": tech_score,
        "score_sentiment": sent_score,
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

    lines = [f"\n  {title}\n  {'─' * 50}"]

    if buys:
        lines.append("")
        for i, s in enumerate(buys, 1):
            eur = s.get("suggested_eur", 0)
            tag = "📌" if s.get("in_portfolio") else "👀"
            name = s.get("name") or s["raw_ticker"]
            sektor = s.get("sektor") or ""
            lines.append(f"  {i}. {s['raw_ticker']:<10s}  {name:<22s}  €{eur:>5.0f}  Score {s['score_total']:.2f}  {tag}")
        total = sum(s.get("suggested_eur", 0) for s in buys)
        lines.append(f"  {'':34s}  €{total:>5.0f}  Total")
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
    print(f"\n{'=' * 60}")
    print(f"  STOCK + CRYPTO ADVISOR — {date.today()}")
    print(f"  Budget: €{WEEKLY_BUDGET:.0f} each | Top {MAX_BUYS}")
    print(f"{'=' * 60}")
    print(format_section("📈 STOCKS", stocks))
    print(format_section("🪙 CRYPTO", cryptos))

    stock_buys = [s for s in stocks if s["action"] == "BUY"]
    crypto_buys = [s for s in cryptos if s["action"] == "BUY"]
    if stock_buys or crypto_buys:
        print(f"\n  Detail:\n")
        for s in stock_buys + crypto_buys:
            print(f"  {s['raw_ticker']}: {s['reasoning']}")
    print(f"\n{'=' * 60}\n")


def store_scores(conn, scored: list[dict]):
    today = date.today()
    with conn.cursor() as cur:
        for s in scored:
            cur.execute("""
                INSERT INTO weekly_scores
                    (run_date, ticker, yf_ticker, score_total, score_tech,
                     score_sentiment, action, suggested_eur, reasoning)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (run_date, ticker) DO UPDATE SET
                    score_total     = EXCLUDED.score_total,
                    score_tech      = EXCLUDED.score_tech,
                    score_sentiment = EXCLUDED.score_sentiment,
                    action          = EXCLUDED.action,
                    suggested_eur   = EXCLUDED.suggested_eur,
                    reasoning       = EXCLUDED.reasoning
            """, (
                today, s["raw_ticker"], s["yf_ticker"],
                s["score_total"], s["score_tech"], s["score_sentiment"],
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

    lines = [f"📊 **Stock + Crypto Advisor — {date.today()}**"]

    if stock_buys:
        lines.append("\n**📈 Stocks**")
        for s in stock_buys:
            eur = s.get("suggested_eur", 0)
            name = s.get("name") or s["raw_ticker"]
            tag = "📌" if s.get("in_portfolio") else "👀"
            lines.append(f"🟢 **{s['raw_ticker']}** {name} — {s['score_total']:.2f} — €{eur:.0f} {tag}")

    if crypto_buys:
        lines.append("\n**🪙 Crypto**")
        for s in crypto_buys:
            eur = s.get("suggested_eur", 0)
            name = s.get("name") or s["raw_ticker"]
            tag = "📌" if s.get("in_portfolio") else "👀"
            lines.append(f"🟢 **{s['raw_ticker']}** {name} — {s['score_total']:.2f} — €{eur:.0f} {tag}")

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
        f"STOCK + CRYPTO ADVISOR — Weekly Report {date.today()}",
        f"Budget: €{WEEKLY_BUDGET:.0f} each | Top {MAX_BUYS}",
        "=" * 60,
    ]

    for title, scored in [("📈 STOCKS", stocks), ("🪙 CRYPTO", cryptos)]:
        scored_s = sorted(scored, key=lambda x: x["score_total"], reverse=True)
        buys = [s for s in scored_s if s["action"] == "BUY"]
        holds = [s for s in scored_s if s["action"] == "HOLD"]
        skips = [s for s in scored_s if s["action"] == "SKIP"]

        lines.append(f"\n{title}\n{'─' * 50}")
        if buys:
            for i, s in enumerate(buys, 1):
                eur = s.get("suggested_eur", 0)
                tag = "📌 Portfolio" if s.get("in_portfolio") else "👀 Watchlist"
                name = s.get("name") or s["raw_ticker"]
                lines.append(f"\n  {i}. {s['raw_ticker']:<10s}  {name}")
                lines.append(f"     Score: {s['score_total']:.2f}  |  €{eur:.0f}  |  {tag}")
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
                lines.append(f"    {s['raw_ticker']:<10s} {name:<22s} {s['score_total']:.2f}{tag}")

        if skips:
            lines.append(f"\n  SKIP ({len(skips)}):")
            for s in skips:
                name = s.get("name") or ""
                lines.append(f"    {s['raw_ticker']:<10s} {name:<22s} {s['score_total']:.2f}")

    lines.append(f"\n{'=' * 60}")
    lines.append("Automatisch generiert von Stock + Crypto Advisor")

    body = "\n".join(lines)
    msg = MIMEText(body, _charset="utf-8")
    msg["Subject"] = f"📊 Weekly Advisor — {date.today()}"
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
        tickers = get_all_tickers(conn)
        log.info("%d Ticker laden", len(tickers))

        scored = []
        for t in tickers:
            yf = t["yf_ticker"]
            tech = get_latest_technicals(conn, yf)
            sent = get_sentiment_7d(conn, yf)
            result = score_ticker(tech, sent, t)
            result["raw_ticker"] = t["raw_ticker"]
            result["yf_ticker"] = yf
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