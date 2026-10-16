#!/usr/bin/env python3
"""
Metronome Monthly Overage Audit → Google Sheets

Runs monthly (2nd of each month) to audit the previous month's overages.
Writes one tab per month to a shared Google Sheet, plus a Summary tab.

Usage:
  python overage_audit.py              # Audit previous month
  python overage_audit.py --month 2025-07  # Backfill a specific month
"""

import os
import sys
import json
import argparse
from datetime import datetime, timedelta
from calendar import monthrange
from dotenv import load_dotenv
import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build
from overage_calculations import CREDIT_ID, HEADERS, summarize

# Load environment variables
load_dotenv()

METRONOME_API_KEY = os.getenv('METRONOME_API_KEY')
BASE_URL = 'https://api.metronome.com'
SCOPES = ['https://www.googleapis.com/auth/spreadsheets']

# Overage audit spreadsheet — create manually in Google Sheets, share with SA, paste ID here
# SA email: metronome-lambda@doc-writer-jjaquint.iam.gserviceaccount.com
OVERAGE_SPREADSHEET_ID = os.getenv('OVERAGE_SPREADSHEET_ID', '1UHIO_zugXc9ZPGWxWgGhgeS2n1PjLHLJxS5PRnksZsA')

CUSTOMERS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'customers.json')


# ---------------------------------------------------------------------------
# Google Sheets helpers (reused from credit_report.py patterns)
# ---------------------------------------------------------------------------

def get_google_creds():
    """Get Google credentials from service account."""
    sa_json = os.getenv('SERVICE_ACCOUNT_JSON')
    if sa_json:
        sa_info = json.loads(sa_json)
        return service_account.Credentials.from_service_account_info(sa_info, scopes=SCOPES)

    sa_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'doc-writer-jjaquint-fded78fa624f.json')
    return service_account.Credentials.from_service_account_file(sa_path, scopes=SCOPES)


def get_spreadsheet_id():
    """Get the spreadsheet ID from constant or env var."""
    if not OVERAGE_SPREADSHEET_ID:
        print("ERROR: OVERAGE_SPREADSHEET_ID is not set.")
        print("  1. Create a Google Sheet named 'Metronome Overage Audit'")
        print("  2. Share it (Editor) with: metronome-lambda@doc-writer-jjaquint.iam.gserviceaccount.com")
        print("  3. Set OVERAGE_SPREADSHEET_ID env var or update the constant in this script")
        sys.exit(1)
    return OVERAGE_SPREADSHEET_ID


def ensure_sheet_exists(service, spreadsheet_id, sheet_name):
    """Ensure a sheet/tab exists, create if not."""
    sheet_metadata = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    sheets = [s['properties']['title'] for s in sheet_metadata.get('sheets', [])]

    if sheet_name not in sheets:
        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={'requests': [{'addSheet': {'properties': {'title': sheet_name}}}]}
        ).execute()


def write_sheet(service, spreadsheet_id, sheet_name, rows):
    """Clear and write rows to a sheet tab."""
    # Determine column range from widest row
    max_cols = max(len(r) for r in rows) if rows else 1
    col_letter = chr(ord('A') + max_cols - 1)

    service.spreadsheets().values().clear(
        spreadsheetId=spreadsheet_id,
        range=f'{sheet_name}!A:{col_letter}'
    ).execute()

    service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=f'{sheet_name}!A1',
        valueInputOption='RAW',
        body={'values': rows}
    ).execute()


# ---------------------------------------------------------------------------
# Metronome API helpers (kept from original overage_audit.py)
# ---------------------------------------------------------------------------

def get_headers():
    return {
        'Authorization': f'Bearer {METRONOME_API_KEY}',
        'Content-Type': 'application/json'
    }


def get_all_customers():
    """Get all customers from Metronome with pagination."""
    print("Fetching all customers...")
    customers = []
    next_page = None

    while True:
        params = {'limit': 100}
        if next_page:
            params['next_page'] = next_page

        resp = requests.get(
            f'{BASE_URL}/v1/customers',
            headers=get_headers(),
            params=params, timeout=45
        )

        if resp.status_code != 200:
            print(f"Error fetching customers: {resp.status_code} - {resp.text}")
            resp.raise_for_status()

        data = resp.json()
        customers.extend(data.get('data', []))
        next_page = data.get('next_page')

        if not next_page:
            break

    print(f"Found {len(customers)} total customers")
    return customers


def get_customer_invoices(customer_id, start_date, end_date):
    """Get invoices for a customer in date range."""
    invoices = []
    next_page = None

    while True:
        params = {
            'starting_on': start_date,
            'ending_before': end_date,
            'limit': 100
        }
        if next_page:
            params['next_page'] = next_page

        resp = requests.get(
            f'{BASE_URL}/v1/customers/{customer_id}/invoices',
            headers=get_headers(),
            params=params, timeout=45
        )

        if resp.status_code != 200:
            resp.raise_for_status()

        data = resp.json()
        invoices.extend(data.get('data', []))
        next_page = data.get('next_page')

        if not next_page:
            break

    return invoices


def get_customer_credit_ledger(customer_id, end_date):
    """Complete posted credit history through the specified billing close."""
    pages = []
    cursor = None
    seen = set()
    while True:
        params = {'limit': 100}
        if cursor:
            params['next_page'] = cursor
        resp = requests.post(
            f'{BASE_URL}/v1/credits/listEntries', headers=get_headers(),
            params=params, timeout=45,
            json={'customer_ids': [customer_id], 'credit_type_ids': [CREDIT_ID],
                  'ending_before': end_date})
        resp.raise_for_status()
        payload = resp.json()
        pages.extend(payload.get('data', []))
        cursor = payload.get('next_page')
        if not cursor:
            return pages
        if cursor in seen:
            raise ValueError('Repeated credit ledger pagination cursor')
        seen.add(cursor)


def audit_customer(customer, start_date, end_date):
    """Monthly finalized billing plus historical all-time credit availability."""
    customer_id = customer['id']
    invoices = get_customer_invoices(customer_id, start_date, end_date)
    if not invoices:
        return None
    pages = get_customer_credit_ledger(customer_id, end_date)
    result = summarize(invoices, pages, start_date, end_date)
    result.update(customer_id=customer_id, customer_name=customer.get('name', 'Unknown'))
    return result


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def get_audit_month(month_arg=None):
    """Return (start_iso, end_iso, label) for the audit month.

    If month_arg is None, audits the previous calendar month.
    Otherwise expects YYYY-MM format.
    """
    if month_arg:
        dt = datetime.strptime(month_arg, '%Y-%m')
    else:
        # Previous month
        today = datetime.now()
        first_of_this_month = today.replace(day=1)
        dt = (first_of_this_month - timedelta(days=1)).replace(day=1)

    _, last_day = monthrange(dt.year, dt.month)
    start = dt.strftime('%Y-%m-%dT00:00:00Z')

    # End = first day of next month
    if dt.month == 12:
        end_dt = dt.replace(year=dt.year + 1, month=1, day=1)
    else:
        end_dt = dt.replace(month=dt.month + 1, day=1)
    end = end_dt.strftime('%Y-%m-%dT00:00:00Z')

    label = dt.strftime('%b-%Y')  # e.g. "Jan-2026"
    return start, end, label


# ---------------------------------------------------------------------------
# Google Sheets output
# ---------------------------------------------------------------------------

def write_month_tab(service, spreadsheet_id, tab_name, results):
    """Write a single month's audit results to a tab."""
    ensure_sheet_exists(service, spreadsheet_id, tab_name)

    header = HEADERS
    ordered = sorted(results, key=lambda x: (x['overage'] is not None, x['overage'] or 0), reverse=True)
    def value(number):
        return number if number is not None else ''
    rows = [header]
    for r in ordered:
        rows.append([
            r['customer_name'], r['customer_id'], value(r['commit_total']),
            value(r['used_credits']), value(r['remaining_balance']),
            value(r['invoice_total']), value(r['overage']),
            f"{r['utilization_pct']:.1f}%" if r['utilization_pct'] is not None else '',
            r['invoice_count'], r['audit_status'],
        ])

    write_sheet(service, spreadsheet_id, tab_name, rows)
    metadata = service.spreadsheets().get(spreadsheetId=spreadsheet_id,
        fields='sheets.properties').execute()
    sheet_id = next(s['properties']['sheetId'] for s in metadata['sheets']
                    if s['properties']['title'] == tab_name)
    requests_body = []
    # Fit revised headers and display credit units without rounding stored values.
    for col, width in [(2, 145), (3, 190), (4, 150), (7, 145)]:
        requests_body.append({'updateDimensionProperties': {
            'range': {'sheetId': sheet_id, 'dimension': 'COLUMNS', 'startIndex': col, 'endIndex': col+1},
            'properties': {'pixelSize': width}, 'fields': 'pixelSize'}})
        if col != 7:
            requests_body.append({'repeatCell': {'range': {'sheetId': sheet_id,
                'startRowIndex': 1, 'endRowIndex': len(rows), 'startColumnIndex': col, 'endColumnIndex': col+1},
                'cell': {'userEnteredFormat': {'numberFormat': {'type': 'NUMBER', 'pattern': '#,##0.00'}}},
                'fields': 'userEnteredFormat.numberFormat'}})
    requests_body.append({'repeatCell': {'range': {'sheetId': sheet_id,
        'startRowIndex': 0, 'endRowIndex': 1, 'startColumnIndex': 0, 'endColumnIndex': 10},
        'cell': {'userEnteredFormat': {'wrapStrategy': 'WRAP'}}, 'fields': 'userEnteredFormat.wrapStrategy'}})
    requests_body.append({'updateDimensionProperties': {
        'range': {'sheetId': sheet_id, 'dimension': 'ROWS', 'startIndex': 0, 'endIndex': 1},
        'properties': {'pixelSize': 60}, 'fields': 'pixelSize'}})
    service.spreadsheets().batchUpdate(spreadsheetId=spreadsheet_id,
        body={'requests': requests_body}).execute()
    print(f"  Wrote {len(results)} rows to tab '{tab_name}'")


def update_summary_tab(service, spreadsheet_id, month_label, results):
    """Append/update a row on the Summary tab for this month's run."""
    ensure_sheet_exists(service, spreadsheet_id, 'Summary')

    # Read existing summary data
    resp = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range='Summary!A:E'
    ).execute()
    existing = resp.get('values', [])

    header = ['Month', 'Customers Audited', 'Overages Found', 'Total Overage $', 'Run Date']

    # Build data map from existing rows (skip header)
    data_map = {}
    for row in existing[1:] if len(existing) > 1 else []:
        if row:
            data_map[row[0]] = row

    # Upsert current month
    overage_count = sum(1 for r in results if r['has_overage'])
    total_overage = sum(r['overage'] for r in results if r['overage'] is not None)

    data_map[month_label] = [
        month_label,
        len(results),
        overage_count,
        round(total_overage, 2),
        datetime.now().strftime('%Y-%m-%d %H:%M'),
    ]

    # Sort by month label chronologically
    def sort_key(label):
        try:
            return datetime.strptime(label, '%b-%Y')
        except ValueError:
            return datetime.min

    sorted_months = sorted(data_map.keys(), key=sort_key)
    rows = [header] + [data_map[m] for m in sorted_months]

    write_sheet(service, spreadsheet_id, 'Summary', rows)
    review_count = sum(r['audit_status'] == 'REVIEW' for r in results)
    print(f"  Updated Summary tab ({len(sorted_months)} months); {review_count} row(s) require review")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description='Monthly Metronome Overage Audit')
    parser.add_argument('--month', type=str, default=None,
                        help='Month to audit in YYYY-MM format (default: previous month)')
    parser.add_argument('--dry-run', action='store_true',
                        help='Read Metronome and print results without accessing Google Sheets')
    args = parser.parse_args()

    start_date, end_date, month_label = get_audit_month(args.month)

    print("=" * 60)
    print("METRONOME MONTHLY OVERAGE AUDIT")
    print(f"Audit Month: {month_label}")
    print(f"Period: {start_date[:10]} to {end_date[:10]}")
    print(f"Run Date: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 60)

    # Get all Metronome customers
    customers = get_all_customers()
    if not customers:
        print("No customers found!")
        return

    # Audit each customer
    results = []
    print(f"\nAuditing {len(customers)} customers for {month_label}...")

    for i, customer in enumerate(customers):
        name = customer.get('name', 'Unknown')
        print(f"  [{i+1}/{len(customers)}] {name}...", end=' ')

        result = audit_customer(customer, start_date, end_date)

        if result:
            results.append(result)
            if result['has_overage']:
                print(f"OVERAGE: ${result['overage']:,.2f}")
            elif result['audit_status'] == 'REVIEW':
                print('REVIEW: finalized billing evidence missing or unsupported')
            elif result['invoice_total'] > 0:
                print(f"OK (${result['invoice_total']:,.2f})")
            else:
                print("No invoices")
        else:
            print("No data")

    if not results:
        print("No audit results to write.")
        return

    if args.dry_run:
        print(json.dumps({'month': month_label, 'results': results}, indent=2))
        return results

    # Google Sheets auth
    print("\nAuthenticating with Google...")
    creds = get_google_creds()
    sheets_service = build('sheets', 'v4', credentials=creds)

    spreadsheet_id = get_spreadsheet_id()
    print(f"Spreadsheet: https://docs.google.com/spreadsheets/d/{spreadsheet_id}")

    # Write to Google Sheets
    print(f"\nWriting results to Google Sheets...")
    write_month_tab(sheets_service, spreadsheet_id, month_label, results)
    update_summary_tab(sheets_service, spreadsheet_id, month_label, results)

    # Console summary
    overage_accounts = [r for r in results if r['has_overage']]
    total_overage = sum(r['overage'] for r in overage_accounts)

    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"Total customers audited: {len(results)}")
    print(f"Customers with overages: {len(overage_accounts)}")
    print(f"Total overage amount: ${total_overage:,.2f}")
    print(f"Sheet: https://docs.google.com/spreadsheets/d/{spreadsheet_id}")

    if overage_accounts:
        print("\n" + "=" * 60)
        print("ACCOUNTS WITH OVERAGES")
        print("=" * 60)
        for r in sorted(overage_accounts, key=lambda x: x['overage'], reverse=True):
            print(f"\n{r['customer_name']}")
            print(f"  All-time credits granted: {r['commit_total']}")
            print(f"  Invoiced: ${r['invoice_total']:,.2f}")
            print(f"  Overage: ${r['overage']:,.2f}")
            print(f"  Credits unavailable %: {r['utilization_pct']}")

    return results


if __name__ == "__main__":
    main()
