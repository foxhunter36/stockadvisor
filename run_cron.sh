#!/bin/bash
cd /home/server/stock_advisor
source venv/bin/activate
echo "$(date): Running $1" >> cron.log
python3 "$1.py" >> cron.log 2>&1 && touch ".last_run_$1"
