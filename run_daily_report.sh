#!/bin/bash
# Daily Metronome Credit Report
# Runs credit_report.py for all customers in customers.json

cd /Users/jamesjaquint/Documents/JJ_WORKSPACE/metronome-reports

LOG_FILE="logs/report_$(date +%Y-%m-%d).log"
mkdir -p logs

echo "=== Report Run: $(date) ===" >> "$LOG_FILE"

/usr/bin/python3 credit_report.py >> "$LOG_FILE" 2>&1

# Keep only last 30 days of logs
find logs -name "report_*.log" -mtime +30 -delete
