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
