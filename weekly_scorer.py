#!/usr/bin/env python3
"""
weekly_scorer.py — Wöchentlicher Stock Advisor Scorer

Trend-First DCA Scoring:
- Trend ist das dominierende Signal (40%)
- Oversold in Downtrend = kein Bonus, sondern Warnung
- Bestes Setup: Uptrend + gesunder Pullback (RSI 35-50)
- Hohe Volatilität wird bestraft (riskant für DCA)

Cron (Freitag 17:00 CET):
    0 17 * * 5 cd ~/stock_advisor && python3 weekly_scorer.py >> scorer.log 2>&1

Manuell:
    python weekly_scorer.py                    # Normal run
    python weekly_scorer.py --dry-run          # Nur anzeigen, nicht in DB
    python weekly_scorer.py --budget 300       # Wöchentliches DCA Budget
"""

import os
import sys
import json
import argparse
import logging
from datetime import date, timedelta

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
    "host":     os.getenv("STOCK_DB_HOST",   "192.168.0.189"),
    "port":     int(os.getenv("STOCK_DB_PORT", "5432")),
    "dbname":   os.getenv("STOCK_DB_NAME",   "stock_advisor"),
    "user":     os.getenv("STOCK_DB_USER",   "collector"),
    "password": os.getenv("STOCK_DB_PASS",   ""),
}

WEIGHT_TECH = float(os.getenv("WEIGHT_TECH", "0.6"))
WEIGHT_SENT = float(os.getenv("WEIGHT_SENT", "0.4"))
WEEKLY_BUDGET = float(os.getenv("WEEKLY_BUDGET", "200"))
BUY_THRESHOLD = float(os.getenv("BUY_THRESHOLD", "0.65"))
MAX_BUYS = int(os.getenv("MAX_BUYS", "5"))


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

def score_technical(tech: dict) -> tuple[float, str]:
    """
    Trend-First Scoring für DCA Growth Investor:
    - Trend (40%): Uptrend stark bevorzugt, Downtrend bestraft
    - Entry (25%): RSI + BB — aber NUR im Uptrend als Bonus
    - Volatility (20%): Hohe ATR% = riskant für DCA
    - Momentum (15%): BB-Position als Momentum-Proxy
    """
    if not tech or tech.get("close") is None:
        return 0.3, "Keine Daten"

    close = float(tech["close"])
    rsi = float(tech["rsi_14"]) if tech.get("rsi_14") is not None else 50.0
    bb = float(tech["bb_position"]) if tech.get("bb_position") is not None else 0.5
    sma50 = float(tech["sma_50"]) if tech.get("sma_50") is not None else close
    sma200 = float(tech["sma_200"]) if tech.get("sma_200") is not None else close
    atr_pct = float(tech["atr_pct"]) if tech.get("atr_pct") is not None else 0.03

    reasons = []

    # ── TREND (40%) — das dominierende Signal
    if close > sma50 > sma200:
        trend_score = 0.95
        in_uptrend = True
        reasons.append("Uptrend ✓")
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
        reasons.append("Downtrend ✗")
    else:
        trend_score = 0.25
        in_uptrend = False
        reasons.append("<SMA50")

    # ── ENTRY TIMING (25%) — RSI context depends on trend
    if in_uptrend:
        # Uptrend: Pullback ist BUY-Chance
        if 30 <= rsi <= 45:
            entry_score = 0.95
            reasons.append(f"RSI {rsi:.0f} pullback in uptrend ★")
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
        # Downtrend: Oversold ist KEIN gutes Zeichen
        if rsi < 30:
            entry_score = 0.20
            reasons.append(f"RSI {rsi:.0f} oversold in downtrend ⚠")
        elif rsi < 45:
            entry_score = 0.30
            reasons.append(f"RSI {rsi:.0f} weak")
        elif rsi < 55:
            entry_score = 0.40
            reasons.append(f"RSI {rsi:.0f} stabilizing")
        else:
            entry_score = 0.35
            reasons.append(f"RSI {rsi:.0f}")

    # ── VOLATILITY (20%) — moderate ist am besten für DCA
    if atr_pct < 0.02:
        vol_score = 0.75
        reasons.append("LowVol")
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
        reasons.append(f"HighVol {atr_pct:.0%} ⚠")

    # ── MOMENTUM (15%) — BB position
    if in_uptrend:
        # In uptrend: midband = healthy, near lower = buying dip
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
        # In downtrend: near lower = falling knife
        if bb < 0.2:
            mom_score = 0.15
            reasons.append("BB falling knife")
        elif bb < 0.5:
            mom_score = 0.30
        else:
            mom_score = 0.45

    score = (trend_score * 0.40
             + entry_score * 0.25
             + vol_score * 0.20
             + mom_score * 0.15)

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
    tech_score, tech_reason = score_technical(tech)
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

def allocate_budget(scored: list[dict], budget: float, max_buys: int = 5) -> list[dict]:
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
        weight = s["score_total"] / total_score
        s["suggested_eur"] = round(budget * weight, 2)

    return scored


# ── Output ───────────────────────────────────────────────────────────

def print_results(scored: list[dict]):
    scored.sort(key=lambda x: x["score_total"], reverse=True)

    buys = [s for s in scored if s["action"] == "BUY"]
    holds = [s for s in scored if s["action"] == "HOLD"]
    skips = [s for s in scored if s["action"] == "SKIP"]

    print(f"\n{'=' * 70}")
    print(f"  STOCK ADVISOR — {date.today()}")
    print(f"  Budget: €{WEEKLY_BUDGET:.0f} | Top {MAX_BUYS} | Tech {WEIGHT_TECH:.0%} / Sent {WEIGHT_SENT:.0%}")
    print(f"{'=' * 70}")

    if buys:
        print(f"\n  🟢 BUY\n")
        for i, s in enumerate(buys, 1):
            eur = s.get("suggested_eur", 0)
            tag = "📌" if s.get("in_portfolio") else "👀"
            name = s.get("name") or s["raw_ticker"]
            sektor = s.get("sektor") or ""
            print(f"  {i}. {s['raw_ticker']:<10s}  {name:<25s}  €{eur:>5.0f}   Score {s['score_total']:.2f}  {tag}")
            if sektor:
                print(f"     {'':10s}  {sektor}")
        total = sum(s.get("suggested_eur", 0) for s in buys)
        print(f"\n  {'─' * 50}")
        print(f"  Total: €{total:.0f} / €{WEEKLY_BUDGET:.0f}")

    if holds:
        print(f"\n  🟡 HOLD:")
        for s in holds:
            name = s.get("name") or ""
            tag = "📌" if s.get("in_portfolio") else ""
            print(f"     {s['raw_ticker']:<10s}  {name:<25s}  {s['score_total']:.2f}  {tag}")

    if skips:
        print(f"\n  🔴 SKIP:")
        for s in skips:
            name = s.get("name") or ""
            print(f"     {s['raw_ticker']:<10s}  {name:<25s}  {s['score_total']:.2f}")

    print(f"\n{'=' * 70}")

    if buys:
        print(f"\n  Detail:\n")
        for s in buys:
            print(f"  {s['raw_ticker']}: {s['reasoning']}")
        print()


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
                today,
                s["raw_ticker"],
                s["yf_ticker"],
                s["score_total"],
                s["score_tech"],
                s["score_sentiment"],
                s["action"],
                s.get("suggested_eur"),
                s["reasoning"],
            ))
    conn.commit()
    log.info("%d Scores in weekly_scores gespeichert", len(scored))


def send_discord_alert(scored: list[dict]):
    webhook_url = os.getenv("DISCORD_STOCK_WEBHOOK")
    if not webhook_url:
        return

    buys = [s for s in scored if s["action"] == "BUY"]
    if not buys:
        return

    import requests

    lines = [f"📊 **Stock Advisor — {date.today()}**\n"]
    for s in buys:
        eur = s.get("suggested_eur", 0)
        name = s.get("name") or s["raw_ticker"]
        tag = "📌" if s.get("in_portfolio") else "👀"
        lines.append(f"🟢 **{s['raw_ticker']}** {name} — Score {s['score_total']:.2f} — €{eur:.0f} {tag}")

    total = sum(s.get("suggested_eur", 0) for s in buys)
    lines.append(f"\n💰 Total: €{total:.0f} / €{WEEKLY_BUDGET:.0f}")

    payload = {"content": "\n".join(lines)}
    try:
        requests.post(webhook_url, json=payload, timeout=10)
        log.info("Discord Alert gesendet")
    except Exception as e:
        log.warning("Discord Fehler: %s", e)


# ── Main ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Nur anzeigen, nicht in DB")
    parser.add_argument("--budget", type=float, default=None, help="DCA Budget in EUR")
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

        scored = allocate_budget(scored, WEEKLY_BUDGET, MAX_BUYS)
        print_results(scored)

        if not args.dry_run:
            store_scores(conn, scored)
            send_discord_alert(scored)
            from email_report import send_email_report
            send_email_report(scored, WEEKLY_BUDGET, MAX_BUYS)
        else:
            log.info("Dry-run: Scores nicht gespeichert")

    finally:
        conn.close()


if __name__ == "__main__":
    main()