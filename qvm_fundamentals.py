#!/usr/bin/env python3
"""
qvm_fundamentals.py — Quality / Value / Momentum Fundamentaldaten.

Holt Fundamental-Daten via yfinance und berechnet Q/V/M Scores (0.0 - 1.0).

Quality:  ROE, Debt/Equity, Operating Margin, FCF Yield
Value:    Forward PE, PB, EV/EBITDA, Dividend Yield (relativ zum Sektor)
Momentum: 1M/3M/6M/12M Returns, RSI, SMA-Trend (aus stock_history DB)

Usage:
    from qvm_fundamentals import fetch_fundamentals, score_quality, score_value, score_qvm

    fund = fetch_fundamentals("BBAI")
    q_score, q_reason = score_quality(fund)
    v_score, v_reason = score_value(fund)
"""

import logging
import numpy as np
import yfinance as yf

log = logging.getLogger(__name__)

# Cache damit wir pro Run nicht mehrfach yfinance abfragen
_info_cache = {}


def fetch_fundamentals(yf_ticker: str) -> dict:
    """
    Holt Fundamental-Daten von yfinance.
    Returns dict mit normalisierten Feldern, None-safe.
    """
    if yf_ticker in _info_cache:
        return _info_cache[yf_ticker]

    result = {
        "ticker": yf_ticker,
        "roe": None,
        "debt_equity": None,
        "operating_margin": None,
        "fcf_yield": None,
        "forward_pe": None,
        "price_to_book": None,
        "ev_ebitda": None,
        "dividend_yield": None,
        "market_cap": None,
        "revenue_growth": None,
        "earnings_growth": None,
        "current_ratio": None,
        "gross_margin": None,
        "sector": None,
        "industry": None,
        "price": None,
        "52w_high": None,
        "52w_low": None,
    }

    try:
        t = yf.Ticker(yf_ticker)
        info = t.info or {}

        result["roe"] = _safe_float(info.get("returnOnEquity"))
        result["debt_equity"] = _safe_float(info.get("debtToEquity"))
        result["operating_margin"] = _safe_float(info.get("operatingMargins"))
        result["forward_pe"] = _safe_float(info.get("forwardPE"))
        result["price_to_book"] = _safe_float(info.get("priceToBook"))
        result["ev_ebitda"] = _safe_float(info.get("enterpriseToEbitda"))
        result["dividend_yield"] = _safe_float(info.get("dividendYield"))
        result["market_cap"] = _safe_float(info.get("marketCap"))
        result["revenue_growth"] = _safe_float(info.get("revenueGrowth"))
        result["earnings_growth"] = _safe_float(info.get("earningsGrowth"))
        result["current_ratio"] = _safe_float(info.get("currentRatio"))
        result["gross_margin"] = _safe_float(info.get("grossMargins"))
        result["sector"] = info.get("sector")
        result["industry"] = info.get("industry")
        result["price"] = _safe_float(info.get("currentPrice") or info.get("regularMarketPrice"))
        result["52w_high"] = _safe_float(info.get("fiftyTwoWeekHigh"))
        result["52w_low"] = _safe_float(info.get("fiftyTwoWeekLow"))

        # FCF Yield berechnen: FCF / Market Cap
        fcf = _safe_float(info.get("freeCashflow"))
        mcap = result["market_cap"]
        if fcf is not None and mcap and mcap > 0:
            result["fcf_yield"] = fcf / mcap

        log.debug("%-10s  Fundamentals geladen", yf_ticker)

    except Exception as e:
        log.warning("%-10s  yfinance Fehler: %s", yf_ticker, e)

    _info_cache[yf_ticker] = result
    return result


def _safe_float(val) -> float | None:
    """Konvertiert zu float, None bei NaN/None/invalid."""
    if val is None:
        return None
    try:
        v = float(val)
        return None if np.isnan(v) or np.isinf(v) else v
    except (TypeError, ValueError):
        return None


def _normalize(val, low, high, invert=False) -> float:
    """
    Normalisiert Wert auf 0.0-1.0 Skala.
    low = schlechtester Wert (Score 0), high = bester Wert (Score 1).
    invert=True: niedrigere Werte sind besser (z.B. Debt/Equity, PE).
    """
    if val is None:
        return 0.5  # Kein Wert = neutraler Score
    if invert:
        val = -val
        low, high = -high, -low
    if high == low:
        return 0.5
    score = (val - low) / (high - low)
    return max(0.0, min(1.0, score))


# ═══════════════════════════════════════════════════════════════════════
#  QUALITY SCORE (0.0 - 1.0)
# ═══════════════════════════════════════════════════════════════════════

def score_quality(fund: dict) -> tuple[float, str]:
    """
    Quality Score basierend auf:
        ROE (30%):              >20% = top, <0% = schlecht
        Debt/Equity (25%):      <50% = top, >200% = schlecht
        Operating Margin (25%): >20% = top, <0% = schlecht
        FCF Yield (20%):        >8% = top, <0% = schlecht
    """
    reasons = []
    scores = []

    # ROE
    roe = fund.get("roe")
    roe_s = _normalize(roe, -0.10, 0.30)
    scores.append(("roe", roe_s, 0.30))
    if roe is not None:
        reasons.append(f"ROE {roe:.0%}")

    # Debt/Equity (niedriger = besser)
    de = fund.get("debt_equity")
    if de is not None:
        de_ratio = de / 100.0  # yfinance gibt als % zurück
    else:
        de_ratio = None
    de_s = _normalize(de_ratio, 2.0, 0.0)  # 200% = 0, 0% = 1
    scores.append(("de", de_s, 0.25))
    if de is not None:
        reasons.append(f"D/E {de:.0f}%")

    # Operating Margin
    om = fund.get("operating_margin")
    om_s = _normalize(om, -0.05, 0.25)
    scores.append(("om", om_s, 0.25))
    if om is not None:
        reasons.append(f"OpM {om:.0%}")

    # FCF Yield
    fcf = fund.get("fcf_yield")
    fcf_s = _normalize(fcf, -0.02, 0.10)
    scores.append(("fcf", fcf_s, 0.20))
    if fcf is not None:
        reasons.append(f"FCF {fcf:.1%}")

    total = sum(s * w for _, s, w in scores)
    total = round(max(0.0, min(1.0, total)), 3)

    if not reasons:
        reasons.append("Keine Fundamentaldaten")

    return total, "; ".join(reasons)


# ═══════════════════════════════════════════════════════════════════════
#  VALUE SCORE (0.0 - 1.0)
# ═══════════════════════════════════════════════════════════════════════

def score_value(fund: dict) -> tuple[float, str]:
    """
    Value Score basierend auf:
        Forward PE (30%):       10-15 = günstig, >40 = teuer, negativ = Verlust
        Price/Book (25%):       <2 = günstig, >10 = teuer
        EV/EBITDA (25%):        <10 = günstig, >25 = teuer
        Dividend Yield (20%):   >4% = top, 0% = neutral
    """
    reasons = []
    scores = []

    # Forward PE (niedriger = besser, aber negativ = schlecht)
    fpe = fund.get("forward_pe")
    if fpe is not None and fpe > 0:
        fpe_s = _normalize(fpe, 50.0, 8.0)  # 50+ = 0, <8 = 1
    elif fpe is not None and fpe < 0:
        fpe_s = 0.1  # Negative PE = Verlust = schlecht
        reasons.append(f"PE neg")
    else:
        fpe_s = 0.5
    scores.append(("fpe", fpe_s, 0.30))
    if fpe is not None and fpe > 0:
        reasons.append(f"FwdPE {fpe:.1f}")

    # Price/Book (niedriger = besser)
    pb = fund.get("price_to_book")
    pb_s = _normalize(pb, 15.0, 1.0)  # >15 = 0, <1 = 1
    scores.append(("pb", pb_s, 0.25))
    if pb is not None:
        reasons.append(f"P/B {pb:.1f}")

    # EV/EBITDA (niedriger = besser)
    ev = fund.get("ev_ebitda")
    if ev is not None and ev > 0:
        ev_s = _normalize(ev, 30.0, 5.0)  # >30 = 0, <5 = 1
    elif ev is not None:
        ev_s = 0.1
    else:
        ev_s = 0.5
    scores.append(("ev", ev_s, 0.25))
    if ev is not None and ev > 0:
        reasons.append(f"EV/E {ev:.1f}")

    # Dividend Yield
    dy = fund.get("dividend_yield")
    dy_s = _normalize(dy, 0.0, 0.05)  # 0% = 0, 5%+ = 1
    scores.append(("dy", dy_s, 0.20))
    if dy is not None and dy > 0:
        reasons.append(f"DivY {dy:.1%}")

    total = sum(s * w for _, s, w in scores)
    total = round(max(0.0, min(1.0, total)), 3)

    if not reasons:
        reasons.append("Keine Value-Daten")

    return total, "; ".join(reasons)


# ═══════════════════════════════════════════════════════════════════════
#  MOMENTUM SCORE (nutzt DB-Technicals)
# ═══════════════════════════════════════════════════════════════════════

def score_momentum_from_db(tech: dict | None, fund: dict | None = None) -> tuple[float, str]:
    """
    Momentum Score aus stock_history Technicals + 52w Range.
    
        SMA Trend (30%):        Price > SMA50 > SMA200 = Uptrend
        RSI Zone (25%):         30-60 im Uptrend = ideal
        BB Position (20%):      0.2-0.6 = guter Entry
        52w Position (25%):     Nähe am 52w High = stark
    """
    reasons = []
    scores = []

    if not tech or tech.get("close") is None:
        return 0.5, "Keine Momentum-Daten"

    close = float(tech["close"])
    rsi = float(tech["rsi_14"]) if tech.get("rsi_14") is not None else 50.0
    bb = float(tech["bb_position"]) if tech.get("bb_position") is not None else 0.5
    sma50 = float(tech["sma_50"]) if tech.get("sma_50") is not None else close
    sma200 = float(tech["sma_200"]) if tech.get("sma_200") is not None else close

    # ── SMA Trend (30%)
    if close > sma50 > sma200:
        trend_s = 0.95
        reasons.append("Uptrend")
    elif close > sma50 and close > sma200:
        trend_s = 0.80
        reasons.append(">SMA50+200")
    elif close > sma50:
        trend_s = 0.60
    elif close > sma200:
        trend_s = 0.40
    elif close < sma50 < sma200:
        trend_s = 0.10
        reasons.append("Downtrend")
    else:
        trend_s = 0.25
    scores.append(("trend", trend_s, 0.30))

    # ── RSI Zone (25%)
    if 30 <= rsi <= 50:
        rsi_s = 0.90
        reasons.append(f"RSI {rsi:.0f} buy zone")
    elif 50 < rsi <= 65:
        rsi_s = 0.70
    elif rsi < 30:
        rsi_s = 0.50
        reasons.append(f"RSI {rsi:.0f} oversold")
    elif rsi <= 75:
        rsi_s = 0.40
        reasons.append(f"RSI {rsi:.0f} elevated")
    else:
        rsi_s = 0.15
        reasons.append(f"RSI {rsi:.0f} overbought")
    scores.append(("rsi", rsi_s, 0.25))

    # ── BB Position (20%)
    if 0.15 <= bb <= 0.55:
        bb_s = 0.85
        reasons.append(f"BB dip")
    elif bb < 0.15:
        bb_s = 0.50
    elif bb <= 0.75:
        bb_s = 0.60
    else:
        bb_s = 0.25
        reasons.append("BB high")
    scores.append(("bb", bb_s, 0.20))

    # ── 52w Position (25%)
    if fund:
        high52 = fund.get("52w_high")
        low52 = fund.get("52w_low")
        if high52 and low52 and high52 > low52:
            pos_52w = (close - low52) / (high52 - low52)
            # 0.5-0.8 = ideal (stark aber nicht überkauft)
            if 0.4 <= pos_52w <= 0.75:
                w52_s = 0.85
                reasons.append(f"52w {pos_52w:.0%}")
            elif pos_52w > 0.9:
                w52_s = 0.40
                reasons.append(f"52w {pos_52w:.0%} near high")
            elif pos_52w < 0.2:
                w52_s = 0.30
                reasons.append(f"52w {pos_52w:.0%} near low")
            else:
                w52_s = 0.60
        else:
            w52_s = 0.5
    else:
        w52_s = 0.5
    scores.append(("52w", w52_s, 0.25))

    total = sum(s * w for _, s, w in scores)
    total = round(max(0.0, min(1.0, total)), 3)

    return total, "; ".join(reasons)


# ═══════════════════════════════════════════════════════════════════════
#  COMBINED QVM SCORE
# ═══════════════════════════════════════════════════════════════════════

def score_qvm(
    fund: dict,
    tech: dict | None = None,
    weight_q: float = 0.30,
    weight_v: float = 0.20,
    weight_m: float = 0.50,
) -> dict:
    """
    Kombinierter QVM Score.

    Default-Gewichte:
        Quality:  30% — langfristige Unternehmensqualität
        Value:    20% — ist es gerade günstig?
        Momentum: 50% — bewegt es sich in die richtige Richtung?

    Momentum hat bewusst das höchste Gewicht weil dein Portfolio
    Growth/Tech-lastig ist — da zählt Trend mehr als klassische Value-Metriken.
    """
    q_score, q_reason = score_quality(fund)
    v_score, v_reason = score_value(fund)
    m_score, m_reason = score_momentum_from_db(tech, fund)

    total = q_score * weight_q + v_score * weight_v + m_score * weight_m
    total = round(max(0.0, min(1.0, total)), 3)

    return {
        "qvm_total": total,
        "q_score": q_score,
        "v_score": v_score,
        "m_score": m_score,
        "q_reason": q_reason,
        "v_reason": v_reason,
        "m_reason": m_reason,
        "reasoning": f"Q:{q_score:.2f} ({q_reason}) | V:{v_score:.2f} ({v_reason}) | M:{m_score:.2f} ({m_reason})",
    }


# ═══════════════════════════════════════════════════════════════════════
#  STANDALONE TEST
# ═══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO)

    tickers = sys.argv[1:] if len(sys.argv) > 1 else ["BBAI", "TENB", "IONQ", "PLUG", "AAPL"]

    print(f"\n{'Ticker':<10s} {'Q':>5s} {'V':>5s} {'M':>5s} {'QVM':>5s}  Details")
    print("─" * 80)

    for ticker in tickers:
        fund = fetch_fundamentals(ticker)
        result = score_qvm(fund)  # Ohne DB-Technicals, Momentum nur aus 52w
        print(
            f"{ticker:<10s} "
            f"{result['q_score']:>5.2f} "
            f"{result['v_score']:>5.2f} "
            f"{result['m_score']:>5.2f} "
            f"{result['qvm_total']:>5.2f}  "
            f"{result['reasoning'][:60]}"
        )