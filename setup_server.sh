#!/bin/bash
# ============================================================
# Stock Advisor — Server Setup
# Ausführen: bash setup_server.sh
# ============================================================
set -e

echo "═══════════════════════════════════════════════════"
echo "  Stock Advisor — Server Setup"
echo "═══════════════════════════════════════════════════"

SA_DIR="$HOME/stock_advisor"
cd "$SA_DIR"

# ── 1. Git Pull ──────────────────────────────────────────
echo ""
echo "[1/8] Git Pull..."
git pull

# ── 2. Python venv + Dependencies ────────────────────────
echo ""
echo "[2/8] Python venv..."
if [ ! -d "venv" ]; then
    python3 -m venv venv
fi
source venv/bin/activate
pip install -q psycopg2-binary yfinance anthropic python-dotenv openpyxl beautifulsoup4 requests
deactivate
echo "  ✅ venv bereit"

# ── 3. .env erstellen (falls nicht vorhanden) ────────────
echo ""
echo "[3/8] .env prüfen..."
if [ ! -f ".env" ]; then
    echo "  ⚠️  Keine .env gefunden!"
    echo "  Kopiere von Trading Station oder erstelle manuell:"
    echo "  cp env_stock_advisor.example .env && nano .env"
    echo ""
    echo "  Benötigte Werte:"
    echo "    STOCK_DB_HOST=localhost"
    echo "    STOCK_DB_PORT=5432"
    echo "    STOCK_DB_NAME=stock_advisor"
    echo "    STOCK_DB_USER=collector"
    echo "    STOCK_DB_PASS=<dein_passwort>"
    echo "    ANTHROPIC_API_KEY=sk-ant-..."
    echo "    CLAUDE_MODEL=claude-sonnet-4-6"
    echo "    PORTFOLIO_EXCEL=portfolio_template.xlsx"
    echo "    HISTORY_DAYS=7300"
    echo "    NEWS_BATCH_SIZE=60"
    echo "    WEEKLY_BUDGET=250"
    echo "    BUY_THRESHOLD=0.65"
    echo "    MAX_BUYS=5"
    echo ""
    echo "  WICHTIG: STOCK_DB_HOST=localhost (nicht 192.168.0.189)"
    echo "  weil die Scripts jetzt lokal auf dem Server laufen!"
else
    echo "  ✅ .env vorhanden"
    # Warnung falls noch externe IP drin steht
    if grep -q "192.168.0.189" .env 2>/dev/null; then
        echo "  ⚠️  .env enthält 192.168.0.189 — für Server-Betrieb auf localhost ändern!"
    fi
fi

# ── 4. Portfolio Excel kopieren (falls nicht vorhanden) ──
echo ""
echo "[4/8] Portfolio Excel..."
if [ ! -f "portfolio_template.xlsx" ]; then
    echo "  ⚠️  portfolio_template.xlsx fehlt!"
    echo "  Kopiere von Trading Station:"
    echo "    scp ahart@tradingstation:Desktop/Py/04_Projects/stockadvisor/portfolio_template.xlsx ."
    echo "  Oder via Samba:"
    echo "    cp /pfad/zur/datei/portfolio_template.xlsx ."
else
    echo "  ✅ portfolio_template.xlsx vorhanden"
fi

# ── 5. Cron Jobs ─────────────────────────────────────────
echo ""
echo "[5/8] Cron Jobs einrichten..."

VENV_PY="$SA_DIR/venv/bin/python3"
CRON_LOG="$SA_DIR/cron.log"

# Bestehende stock_advisor Crons entfernen
crontab -l 2>/dev/null | grep -v "stock_advisor" | crontab - 2>/dev/null || true

# Neue Crons hinzufügen
(crontab -l 2>/dev/null; cat << CRON

# ── Stock Advisor ─────────────────────────────────────────
# Mo-Fr 07:30: Portfolio sync + History update
30 7 * * 1-5 cd $SA_DIR && $VENV_PY sync_portfolio.py >> $CRON_LOG 2>&1
# Mo-Fr 08:00: News fetch + Claude Sentiment
0 8 * * 1-5 cd $SA_DIR && $VENV_PY fetch_news.py >> $CRON_LOG 2>&1
# Freitag 17:00: Weekly Scorer
0 17 * * 5 cd $SA_DIR && $VENV_PY weekly_scorer.py >> $CRON_LOG 2>&1
CRON
) | crontab -
echo "  ✅ Cron Jobs eingerichtet:"
echo "    Mo-Fr 07:30 — sync_portfolio.py"
echo "    Mo-Fr 08:00 — fetch_news.py"
echo "    Fr   17:00 — weekly_scorer.py"

# ── 6. Backup Integration ────────────────────────────────
echo ""
echo "[6/8] Backup Integration..."

BACKUP_SCRIPT="$HOME/backup/backup_stock_advisor.sh"
mkdir -p "$HOME/backup"

cat > "$BACKUP_SCRIPT" << 'BACKUP'
#!/bin/bash
# Stock Advisor DB Backup — täglich 03:15
BACKUP_DIR="$HOME/backup"
DB_NAME="stock_advisor"
DB_USER="collector"
KEEP_DAYS=7

FILENAME="$BACKUP_DIR/${DB_NAME}_$(date +%Y%m%d_%H%M%S).sql.gz"

docker exec bybit_postgres pg_dump -U $DB_USER $DB_NAME | gzip > "$FILENAME"

# Alte Backups löschen
find "$BACKUP_DIR" -name "${DB_NAME}_*.sql.gz" -mtime +$KEEP_DAYS -delete

echo "[$(date)] Backup: $FILENAME ($(du -h "$FILENAME" | cut -f1))"
BACKUP
chmod +x "$BACKUP_SCRIPT"

# Backup Cron hinzufügen (falls nicht schon drin)
if ! crontab -l 2>/dev/null | grep -q "backup_stock_advisor"; then
    (crontab -l 2>/dev/null; echo "# Stock Advisor Backup — täglich 03:15
15 3 * * * $BACKUP_SCRIPT >> $HOME/backup/backup.log 2>&1") | crontab -
fi
echo "  ✅ Backup: täglich 03:15, 7 Tage Rotation"
echo "    $BACKUP_SCRIPT"

# ── 7. MOTD Update ───────────────────────────────────────
echo ""
echo "[7/8] MOTD Update..."

MOTD_FILE="/etc/update-motd.d/99-stock-advisor"
sudo tee "$MOTD_FILE" > /dev/null << 'MOTD'
#!/bin/bash
echo ""
echo "═══════════════════════════════════════════════"
echo "  📊 STOCK ADVISOR"
echo "═══════════════════════════════════════════════"
echo "  DB:     stock_advisor (bybit_postgres)"
echo "  Crons:  sync 07:30 | news 08:00 | score Fr 17:00"
echo "  Backup: 03:15 (7d rotation)"
echo "  Logs:   ~/stock_advisor/cron.log"
echo ""
# Letzte Cron-Ausführung
if [ -f "$HOME/stock_advisor/cron.log" ]; then
    LAST=$(tail -1 "$HOME/stock_advisor/cron.log" 2>/dev/null)
    echo "  Last:   $LAST"
fi
echo ""
MOTD
sudo chmod +x "$MOTD_FILE"
echo "  ✅ MOTD installiert"

# ── 8. Health Check / Uptime Kuma ────────────────────────
echo ""
echo "[8/8] Health Check..."

# Status-Script das Uptime Kuma pollen kann
cat > "$SA_DIR/health_status.py" << 'HEALTH'
#!/usr/bin/env python3
"""
health_status.py — Einfacher HTTP Health-Check für Stock Advisor Crons.
Uptime Kuma pollt http://server:9094/stock-advisor

Prüft ob Crons in den letzten 26h gelaufen sind (Mo-Fr).
"""
import os
import json
import time
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler

PORT = 9094
SA_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(SA_DIR, "cron.log")
LAST_RUN_FILE = os.path.join(SA_DIR, ".last_run")


def touch_last_run():
    """Aufgerufen am Ende jedes Cron-Jobs."""
    with open(LAST_RUN_FILE, "w") as f:
        f.write(datetime.now().isoformat())


def check_health():
    """True wenn cron.log in den letzten 26h geschrieben wurde."""
    now = datetime.now()

    # Am Wochenende: immer OK (Crons laufen nur Mo-Fr)
    if now.weekday() >= 5:
        return True, "Weekend — no crons expected"

    # Prüfe .last_run File
    if os.path.exists(LAST_RUN_FILE):
        try:
            mtime = os.path.getmtime(LAST_RUN_FILE)
            age_h = (time.time() - mtime) / 3600
            if age_h < 26:
                return True, f"Last run {age_h:.1f}h ago"
        except Exception:
            pass

    # Fallback: cron.log prüfen
    if os.path.exists(LOG_FILE):
        try:
            mtime = os.path.getmtime(LOG_FILE)
            age_h = (time.time() - mtime) / 3600
            if age_h < 26:
                return True, f"Log updated {age_h:.1f}h ago"
        except Exception:
            pass

    return False, "No recent cron activity"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/stock-advisor", "/", "/health"):
            ok, msg = check_health()
            status = 200 if ok else 503
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "service": "stock-advisor",
                "status": "ok" if ok else "stale",
                "detail": msg,
            }).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Stock Advisor Health Check auf Port {PORT}")
    server.serve_forever()
HEALTH
chmod +x "$SA_DIR/health_status.py"

# Systemd Service für Health Check
sudo tee /etc/systemd/system/stock-advisor-health.service > /dev/null << SERVICE
[Unit]
Description=Stock Advisor Health Check
After=network.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$SA_DIR
ExecStart=$SA_DIR/venv/bin/python3 $SA_DIR/health_status.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
SERVICE

sudo systemctl daemon-reload
sudo systemctl enable stock-advisor-health
sudo systemctl start stock-advisor-health
echo "  ✅ Health Check auf Port 9094"
echo "    Uptime Kuma: HTTP → http://localhost:9094/stock-advisor"

# ── Zusammenfassung ──────────────────────────────────────
echo ""
echo "═══════════════════════════════════════════════════"
echo "  Setup abgeschlossen!"
echo "═══════════════════════════════════════════════════"
echo ""
echo "  Noch zu tun:"
echo "  1. .env erstellen/prüfen:  nano $SA_DIR/.env"
echo "     → STOCK_DB_HOST=localhost (nicht 192.168.0.189!)"
echo "  2. portfolio_template.xlsx auf Server kopieren"
echo "  3. Uptime Kuma: neuen Monitor erstellen:"
echo "     → Type: HTTP, URL: http://localhost:9094/stock-advisor"
echo "  4. Optional: DISCORD_STOCK_WEBHOOK in .env setzen"
echo "  5. Testen:"
echo "     cd $SA_DIR && source venv/bin/activate"
echo "     python3 sync_portfolio.py --excel-only"
echo "     python3 fetch_news.py"
echo "     python3 weekly_scorer.py --dry-run"
echo ""
echo "  Crons:"
echo "    crontab -l | grep stock"
echo ""
echo "  Logs:"
echo "    tail -f $SA_DIR/cron.log"
echo ""
echo "  Health:"
echo "    curl http://localhost:9094/stock-advisor"
echo ""
