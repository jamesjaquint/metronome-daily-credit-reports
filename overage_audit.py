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
from overage_calculations import CREDIT_ID, summarize
from overage_publication import publish_report

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


# ---------------------------------------------------------------------------
# Metronome API helpers (kept from original overage_audit.py)
# ---------------------------------------------------------------------------

def get_headers():
    return {
        'Authorization': f'Bearer {METRONOME_API_KEY}',
        'Content-Type': 'application/json'
    }


def get_api_pages(path, body=None, params=None):
    """Retrieve complete data or fail; malformed responses are not empty data."""
    output = []
    query = dict(params or {})
    query.setdefault('limit', 100)
    seen = set()
    while True:
        if body is None:
            response = requests.get(BASE_URL + path, headers=get_headers(), params=query, timeout=45)
        else:
            response = requests.post(BASE_URL + path, headers=get_headers(), params=query,
                                     json=body, timeout=45)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get('data'), list):
            raise ValueError('Malformed Metronome data response: ' + path)
        output.extend(payload['data'])
        cursor = payload.get('next_page')
        if cursor is None or cursor == '':
            return output
        if not isinstance(cursor, str) or cursor in seen:
            raise ValueError('Invalid or repeated Metronome pagination cursor: ' + path)
        seen.add(cursor)
        query = {**query, 'next_page': cursor}


def get_all_customers():
    print('Fetching all customers...')
    customers = get_api_pages('/v1/customers')
    if len({c['id'] for c in customers}) != len(customers):
        raise ValueError('Duplicate customer in Metronome response')
    print(f'Found {len(customers)} total customers')
    return customers


def get_customer_invoices(customer_id, start_date, end_date):
    return get_api_pages(f'/v1/customers/{customer_id}/invoices', params={
        'starting_on': start_date, 'ending_before': end_date})


def get_customer_credit_ledger(customer_id, end_date):
    return get_api_pages('/v1/credits/listEntries', body={
        'customer_ids': [customer_id], 'credit_type_ids': [CREDIT_ID], 'ending_before': end_date})


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
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description='Monthly Metronome Overage Audit')
    parser.add_argument('--month', type=str, default=None,
                        help='Month to audit in YYYY-MM format (default: previous month)')
    parser.add_argument('--dry-run', action='store_true',
                        help='Read Metronome and print results without accessing Google Sheets')
    parser.add_argument('--replace-existing', action='store_true',
                        help='Allow intentional full-month regeneration of an existing tab')
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
    publish_report(sheets_service, spreadsheet_id, month_label, results,
                   allow_replace=args.replace_existing)

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
