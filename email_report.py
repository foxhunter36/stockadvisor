"""Email Report für Weekly Scorer."""
import os
import smtplib
from email.mime.text import MIMEText
from datetime import date

GMAIL_USER = os.getenv("GMAIL_USER")
GMAIL_PASS = os.getenv("GMAIL_APP_PASSWORD")


def send_email_report(scored: list[dict], weekly_budget: float, max_buys: int):
    if not GMAIL_USER or not GMAIL_PASS:
        return

    scored_sorted = sorted(scored, key=lambda x: x["score_total"], reverse=True)
    buys = [s for s in scored_sorted if s["action"] == "BUY"]
    holds = [s for s in scored_sorted if s["action"] == "HOLD"]
    skips = [s for s in scored_sorted if s["action"] == "SKIP"]

    lines = []
    lines.append(f"STOCK ADVISOR — Weekly Report {date.today()}")
    lines.append(f"Budget: €{weekly_budget:.0f} | Top {max_buys}")
    lines.append("=" * 65)

    if buys:
        lines.append("\n🟢 BUY EMPFEHLUNGEN\n")
        for i, s in enumerate(buys, 1):
            eur = s.get("suggested_eur", 0)
            tag = "📌 Portfolio" if s.get("in_portfolio") else "👀 Watchlist"
            name = s.get("name") or s["raw_ticker"]
            sektor = s.get("sektor") or ""
            lines.append(f"  {i}. {s['raw_ticker']:<10s}  {name}")
            lines.append(f"     Score: {s['score_total']:.2f}  |  €{eur:.0f}  |  {tag}")
            if sektor:
                lines.append(f"     Sektor: {sektor}")
            lines.append(f"     {s['reasoning']}")
            lines.append("")
        total = sum(s.get("suggested_eur", 0) for s in buys)
        lines.append(f"  Total: €{total:.0f} / €{weekly_budget:.0f}")

    if holds:
        lines.append(f"\n🟡 HOLD ({len(holds)} Ticker)\n")
        for s in holds:
            name = s.get("name") or ""
            tag = " 📌" if s.get("in_portfolio") else ""
            lines.append(f"  {s['raw_ticker']:<10s}  {name:<25s}  Score {s['score_total']:.2f}{tag}")

    if skips:
        lines.append(f"\n🔴 SKIP ({len(skips)} Ticker)\n")
        for s in skips:
            name = s.get("name") or ""
            lines.append(f"  {s['raw_ticker']:<10s}  {name:<25s}  Score {s['score_total']:.2f}")

    lines.append(f"\n{'=' * 65}")
    lines.append("Automatisch generiert von Stock Advisor")

    body = "\n".join(lines)

    msg = MIMEText(body, _charset="utf-8")
    msg["Subject"] = f"📊 Stock Advisor — Weekly Report {date.today()}"
    msg["From"] = GMAIL_USER
    msg["To"] = os.getenv("EMAIL_RECIPIENTS", GMAIL_USER)

    try:
        server = smtplib.SMTP("smtp.gmail.com", 587)
        server.starttls()
        server.login(GMAIL_USER, GMAIL_PASS)
        server.send_message(msg)
        server.quit()
        print("✅ Email Report gesendet")
    except Exception as e:
        print(f"⚠️ Email Fehler: {e}")
