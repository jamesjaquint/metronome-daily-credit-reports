#!/usr/bin/env python3
"""
Shahid (MBC FZ LLC) - Weekly Analytics Credit Usage Report → Google Sheets
Pulls daily breakdowns from Metronome API, filters for Analytics line items,
and aggregates by ISO week.
"""

import os
import json
from datetime import datetime, timedelta
from dotenv import load_dotenv
import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build

# Load environment variables
load_dotenv()

METRONOME_API_KEY = os.getenv('METRONOME_API_KEY')
SCOPES = [
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/drive'
]

# Shahid config
CUSTOMER_ID = '9f0b3207-e86a-45d4-a66a-090029ccf29b'
CUSTOMER_NAME = 'MBC FZ LLC (Shahid)'

def get_google_creds():
    """Get Google credentials from service account."""
    sa_json = os.getenv('SERVICE_ACCOUNT_JSON')
    if sa_json:
        sa_info = json.loads(sa_json)
        return service_account.Credentials.from_service_account_info(sa_info, scopes=SCOPES)
    sa_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'doc-writer-jjaquint-fded78fa624f.json')
    return service_account.Credentials.from_service_account_file(sa_path, scopes=SCOPES)

def get_metronome_headers():
    return {
        'Authorization': f'Bearer {METRONOME_API_KEY}',
        'Content-Type': 'application/json'
    }

def get_all_invoices():
    """Get all invoices with pagination."""
    url = f'https://api.metronome.com/v1/customers/{CUSTOMER_ID}/invoices'
    all_invoices = []
    next_page = None

    while True:
        params = {}
        if next_page:
            params['next_page'] = next_page
        resp = requests.get(url, headers=get_metronome_headers(), params=params)
        result = resp.json()
        all_invoices.extend(result.get('data', []))
        next_page = result.get('next_page')
        if not next_page:
            break

    return all_invoices

def get_analytics_invoice_totals(invoices):
    """Extract monthly Analytics line item totals from invoices (source of truth)."""
    monthly_totals = {}

    for inv in invoices:
        status = inv.get('status', '')
        if status not in ['FINALIZED', 'DRAFT']:
            continue

        start = inv.get('start_timestamp', '')[:10]
        try:
            start_dt = datetime.strptime(start, '%Y-%m-%d')
            month_key = start_dt.strftime('%Y-%m')
        except:
            continue

        analytics_total = 0
        for li in inv.get('line_items', []):
            name = li.get('name', '')
            if 'analytics' in name.lower():
                analytics_total += li.get('total', 0)

        if analytics_total > 0:
            if month_key not in monthly_totals:
                monthly_totals[month_key] = {'total': 0, 'status': status}
            monthly_totals[month_key]['total'] += analytics_total

    return monthly_totals

def get_daily_breakdowns(start_date, end_date):
    """Get daily breakdowns with pagination."""
    url = f'https://api.metronome.com/v1/customers/{CUSTOMER_ID}/invoices/breakdowns'
    all_data = []
    next_page = None

    while True:
        params = {
            'starting_on': start_date.strftime('%Y-%m-%d'),
            'ending_before': end_date.strftime('%Y-%m-%d'),
            'window_size': 'day'
        }
        if next_page:
            params['next_page'] = next_page

        resp = requests.get(url, headers=get_metronome_headers(), params=params)
        if resp.status_code != 200:
            print(f"  Warning: Breakdowns API returned {resp.status_code}")
            break

        result = resp.json()
        all_data.extend(result.get('data', []))
        next_page = result.get('next_page')
        if not next_page:
            break

    return all_data

def build_analytics_daily(months_back=24):
    """Build daily analytics usage from breakdowns, reconciled with invoices."""

    # Step 1: Get invoice totals (source of truth)
    print("  Getting invoice totals...")
    invoices = get_all_invoices()
    monthly_invoice_totals = get_analytics_invoice_totals(invoices)
    print(f"  Found {len(monthly_invoice_totals)} months with Analytics charges")

    # Step 2: Determine date range
    today = datetime.now()
    start_date = today.replace(day=1)
    for _ in range(months_back):
        start_date = (start_date - timedelta(days=1)).replace(day=1)

    if today.month == 12:
        end_date = today.replace(year=today.year + 1, month=1, day=1)
    else:
        end_date = today.replace(month=today.month + 1, day=1)

    print(f"  Date range: {start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}")

    # Step 3: Get daily breakdowns
    print("  Getting daily breakdowns (this may take a moment)...")
    breakdown_data = get_daily_breakdowns(start_date, end_date)
    print(f"  Got {len(breakdown_data)} day records")

    # Step 4: Parse breakdowns - extract only Analytics items
    daily_usage = {}
    breakdown_monthly_totals = {}
    today_str = today.strftime('%Y-%m-%d')

    for day_data in breakdown_data:
        date = day_data.get('breakdown_start_timestamp', '')[:10]
        if not date or date > today_str:
            continue

        month_key = date[:7]
        analytics_total = 0

        for li in day_data.get('line_items', []):
            li_name = li.get('name', '').lower()
            # Check top-level line item name
            if 'analytics' in li_name:
                for sub in li.get('sub_line_items', []):
                    analytics_total += sub.get('subtotal', 0)

        if analytics_total > 0:
            daily_usage[date] = {'date': date, 'analytics': analytics_total}

            if month_key not in breakdown_monthly_totals:
                breakdown_monthly_totals[month_key] = {'total': 0, 'days': 0}
            breakdown_monthly_totals[month_key]['total'] += analytics_total
            breakdown_monthly_totals[month_key]['days'] += 1

    # Step 5: Reconcile with finalized invoice totals
    print("  Reconciling with invoice totals...")
    current_month = today.strftime('%Y-%m')

    for month_key, invoice_data in monthly_invoice_totals.items():
        if month_key not in breakdown_monthly_totals:
            continue
        if month_key == current_month:
            continue  # Skip draft month
        if invoice_data.get('status') != 'FINALIZED':
            continue

        breakdown = breakdown_monthly_totals[month_key]
        days_in_month = breakdown['days']
        if days_in_month == 0:
            continue

        diff = invoice_data['total'] - breakdown['total']
        if abs(diff) > 0.01:
            daily_adjustment = diff / days_in_month
            for date in daily_usage:
                if date.startswith(month_key):
                    daily_usage[date]['analytics'] += daily_adjustment

    return daily_usage, monthly_invoice_totals

def aggregate_by_week(daily_usage):
    """Aggregate daily usage into ISO weeks."""
    weekly = {}

    for date_str, data in sorted(daily_usage.items()):
        dt = datetime.strptime(date_str, '%Y-%m-%d')
        # ISO week: Monday-based
        iso_year, iso_week, _ = dt.isocalendar()
        week_key = f"{iso_year}-W{iso_week:02d}"

        # Find the Monday of this ISO week for labeling
        monday = dt - timedelta(days=dt.weekday())
        sunday = monday + timedelta(days=6)

        if week_key not in weekly:
            weekly[week_key] = {
                'week': week_key,
                'week_start': monday.strftime('%Y-%m-%d'),
                'week_end': sunday.strftime('%Y-%m-%d'),
                'analytics_credits': 0,
                'days_with_data': 0
            }

        weekly[week_key]['analytics_credits'] += data['analytics']
        weekly[week_key]['days_with_data'] += 1

    return weekly

def ensure_sheet_exists(service, spreadsheet_id, sheet_name):
    """Ensure a sheet tab exists, create if not."""
    try:
        meta = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
        sheets = [s['properties']['title'] for s in meta.get('sheets', [])]
        if sheet_name not in sheets:
            service.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={'requests': [{'addSheet': {'properties': {'title': sheet_name}}}]}
            ).execute()
    except Exception as e:
        print(f"    Note: {e}")

def setup_spreadsheet(service, spreadsheet_id):
    """Ensure required tabs exist in the spreadsheet."""
    for tab in ['Weekly Analytics', 'Daily Detail', 'Monthly Summary']:
        ensure_sheet_exists(service, spreadsheet_id, tab)

def update_spreadsheet(service, spreadsheet_id, daily_usage, weekly_data, monthly_totals):
    """Write data to Google Sheet."""

    # === Weekly Analytics tab ===
    weekly_headers = [[
        'Week',
        'Week Start (Mon)',
        'Week End (Sun)',
        'Analytics Credits',
        'Days with Data'
    ]]

    weekly_rows = []
    for week_key in sorted(weekly_data.keys()):
        w = weekly_data[week_key]
        weekly_rows.append([
            w['week'],
            w['week_start'],
            w['week_end'],
            round(w['analytics_credits'], 2),
            w['days_with_data']
        ])

    service.spreadsheets().values().clear(
        spreadsheetId=spreadsheet_id, range='Weekly Analytics!A:E'
    ).execute()
    service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id, range='Weekly Analytics!A1',
        valueInputOption='RAW',
        body={'values': weekly_headers + weekly_rows}
    ).execute()

    # === Daily Detail tab ===
    daily_headers = [['Date', 'Analytics Credits']]
    daily_rows = []
    for date_str in sorted(daily_usage.keys(), reverse=True):
        d = daily_usage[date_str]
        daily_rows.append([d['date'], round(d['analytics'], 2)])

    service.spreadsheets().values().clear(
        spreadsheetId=spreadsheet_id, range='Daily Detail!A:B'
    ).execute()
    service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id, range='Daily Detail!A1',
        valueInputOption='RAW',
        body={'values': daily_headers + daily_rows}
    ).execute()

    # === Monthly Summary tab ===
    monthly_headers = [['Month', 'Analytics Credits (Invoice)', 'Source']]
    monthly_rows = []
    for month_key in sorted(monthly_totals.keys()):
        m = monthly_totals[month_key]
        monthly_rows.append([
            month_key,
            round(m['total'], 2),
            m.get('status', 'Unknown')
        ])

    service.spreadsheets().values().clear(
        spreadsheetId=spreadsheet_id, range='Monthly Summary!A:C'
    ).execute()
    service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id, range='Monthly Summary!A1',
        valueInputOption='RAW',
        body={'values': monthly_headers + monthly_rows}
    ).execute()

    print(f"  Written: {len(weekly_rows)} weeks, {len(daily_rows)} days, {len(monthly_rows)} months")

def main():
    print(f"=== Shahid Weekly Analytics Credit Report ===")
    print(f"Customer: {CUSTOMER_NAME}")
    print(f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M')}")

    # Build daily analytics usage
    daily_usage, monthly_totals = build_analytics_daily(months_back=24)
    print(f"  Found {len(daily_usage)} days with analytics usage")

    if not daily_usage:
        print("  No analytics usage found!")
        return

    # Aggregate by week
    weekly_data = aggregate_by_week(daily_usage)
    print(f"  Aggregated into {len(weekly_data)} weeks")

    # Print summary to console
    print(f"\n  === Weekly Analytics Credits (last 12 weeks) ===")
    recent_weeks = sorted(weekly_data.keys(), reverse=True)[:12]
    for wk in recent_weeks:
        w = weekly_data[wk]
        print(f"    {w['week']} ({w['week_start']} to {w['week_end']}): {w['analytics_credits']:,.2f} credits ({w['days_with_data']} days)")

    # Write to Google Sheet (shared with service account)
    spreadsheet_id = '1YN14i7dx6-MX295S6pfzOEYqNCCuWggNRO8tYqvhZ5U'

    print(f"\n  Authenticating with Google...")
    creds = get_google_creds()
    service = build('sheets', 'v4', credentials=creds)

    setup_spreadsheet(service, spreadsheet_id)
    update_spreadsheet(service, spreadsheet_id, daily_usage, weekly_data, monthly_totals)

    print(f"\n  Done! Sheet: https://docs.google.com/spreadsheets/d/{spreadsheet_id}")

if __name__ == "__main__":
    main()
