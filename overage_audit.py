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
            params=params
        )

        if resp.status_code != 200:
            print(f"Error fetching customers: {resp.status_code} - {resp.text}")
            break

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
            params=params
        )

        if resp.status_code != 200:
            return []

        data = resp.json()
        invoices.extend(data.get('data', []))
        next_page = data.get('next_page')

        if not next_page:
            break

    return invoices


def get_customer_balances(customer_id):
    """Get all balances (commits + credits) for a customer."""
    resp = requests.get(
        f'{BASE_URL}/v1/customers/{customer_id}/balances',
        headers=get_headers(),
        params={
            'include_balance': 'true',
            'include_contract_balances': 'true'
        }
    )

    if resp.status_code == 200:
        return resp.json().get('data', [])

    return get_customer_commits(customer_id)


def get_customer_commits(customer_id):
    """Get commits for a customer."""
    all_commits = []
    next_page = None

    while True:
        params = {
            'include_balance': 'true',
            'include_contract_commits': 'true'
        }
        if next_page:
            params['next_page'] = next_page

        resp = requests.get(
            f'{BASE_URL}/v1/customers/{customer_id}/commits',
            headers=get_headers(),
            params=params
        )

        if resp.status_code != 200:
            break

        data = resp.json()
        all_commits.extend(data.get('data', []))
        next_page = data.get('next_page')

        if not next_page:
            break

    return all_commits


def get_customer_credits(customer_id):
    """Get credit grants for a customer."""
    resp = requests.post(
        f'{BASE_URL}/v1/credits/listGrants',
        headers=get_headers(),
        json={'customer_ids': [customer_id]}
    )

    if resp.status_code != 200:
        return []

    return resp.json().get('data', [])


def calculate_commit_total(commits):
    """Sum up total commit value from various response structures."""
    total = 0
    for commit in commits:
        amount = 0

        if 'grant_amount' in commit:
            amt = commit.get('grant_amount', {})
            amount = amt.get('amount', 0) if isinstance(amt, dict) else amt
        elif 'access_schedule' in commit:
            schedule = commit.get('access_schedule', {})
            for item in schedule.get('schedule_items', []):
                amount += item.get('amount', 0)
        elif 'access_amount' in commit:
            amt = commit.get('access_amount', {})
            amount = amt.get('amount', 0) if isinstance(amt, dict) else amt
        elif 'amount' in commit:
            amount = commit.get('amount', 0)

        total += amount
    return total


def calculate_remaining_balance(commits):
    """Get remaining balance from commits/credits."""
    total = 0
    for commit in commits:
        balance = 0

        if 'balance' in commit:
            bal = commit.get('balance', {})
            if isinstance(bal, dict):
                balance = bal.get('excluding_pending') or bal.get('including_pending') or bal.get('amount', 0)
            else:
                balance = bal
        elif 'available_balance' in commit:
            balance = commit.get('available_balance', 0)

        total += balance
    return total


def parse_invoice_totals(invoices):
    """Parse invoices into a single total. Converts cents to dollars if needed."""
    total = 0
    is_cents = False
    count = 0

    for inv in invoices:
        status = inv.get('status', '')
        if status not in ['FINALIZED', 'DRAFT']:
            continue

        credit_type = inv.get('credit_type', {})
        if isinstance(credit_type, dict) and 'cents' in credit_type.get('name', '').lower():
            is_cents = True

        total += inv.get('total', 0)
        count += 1

    if is_cents:
        total = total / 100

    return total, is_cents, count


def audit_customer(customer, start_date, end_date):
    """Audit a single customer for overages in a given month."""
    customer_id = customer.get('id')
    customer_name = customer.get('name', 'Unknown')

    invoices = get_customer_invoices(customer_id, start_date, end_date)

    if not invoices:
        return None

    balances = get_customer_balances(customer_id)
    commits = get_customer_commits(customer_id)
    credits = get_customer_credits(customer_id)

    all_financial = balances + commits + credits

    invoice_total, is_cents, invoice_count = parse_invoice_totals(invoices)
    commit_total = calculate_commit_total(all_financial)
    remaining_balance = calculate_remaining_balance(all_financial)

    if is_cents:
        commit_total = commit_total / 100
        remaining_balance = remaining_balance / 100

    used_credits = commit_total - remaining_balance if commit_total > 0 else invoice_total
    overage = max(0, invoice_total - commit_total) if commit_total > 0 else 0
    utilization_pct = (used_credits / commit_total * 100) if commit_total > 0 else 0

    return {
        'customer_id': customer_id,
        'customer_name': customer_name,
        'commit_total': commit_total,
        'remaining_balance': remaining_balance,
        'used_credits': used_credits,
        'invoice_total': invoice_total,
        'overage': overage,
        'utilization_pct': utilization_pct,
        'invoice_count': invoice_count,
        'has_overage': overage > 0 or utilization_pct > 100,
    }


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

    header = [
        'Customer Name',
        'Customer ID',
        'Commit Total',
        'Used Credits',
        'Remaining Balance',
        'Invoice Total',
        'Overage Amount',
        'Utilization %',
        'Invoice Count',
        'Has Overage',
    ]

    rows = [header]
    for r in sorted(results, key=lambda x: x['overage'], reverse=True):
        rows.append([
            r['customer_name'],
            r['customer_id'],
            round(r['commit_total'], 2),
            round(r['used_credits'], 2),
            round(r['remaining_balance'], 2),
            round(r['invoice_total'], 2),
            round(r['overage'], 2),
            f"{r['utilization_pct']:.1f}%",
            r['invoice_count'],
            'YES' if r['has_overage'] else 'NO',
        ])

    write_sheet(service, spreadsheet_id, tab_name, rows)
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
    total_overage = sum(r['overage'] for r in results)

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
    print(f"  Updated Summary tab ({len(sorted_months)} months)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description='Monthly Metronome Overage Audit')
    parser.add_argument('--month', type=str, default=None,
                        help='Month to audit in YYYY-MM format (default: previous month)')
    args = parser.parse_args()

    start_date, end_date, month_label = get_audit_month(args.month)

    print("=" * 60)
    print("METRONOME MONTHLY OVERAGE AUDIT")
    print(f"Audit Month: {month_label}")
    print(f"Period: {start_date[:10]} to {end_date[:10]}")
    print(f"Run Date: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 60)

    # Google Sheets auth
    print("\nAuthenticating with Google...")
    creds = get_google_creds()
    sheets_service = build('sheets', 'v4', credentials=creds)

    spreadsheet_id = get_spreadsheet_id()
    print(f"Spreadsheet: https://docs.google.com/spreadsheets/d/{spreadsheet_id}")

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
            elif result['invoice_total'] > 0:
                print(f"OK (${result['invoice_total']:,.2f})")
            else:
                print("No invoices")
        else:
            print("No data")

    if not results:
        print("No audit results to write.")
        return

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
            print(f"  Commit: ${r['commit_total']:,.2f}")
            print(f"  Invoiced: ${r['invoice_total']:,.2f}")
            print(f"  Overage: ${r['overage']:,.2f}")
            print(f"  Utilization: {r['utilization_pct']:.1f}%")

    return results


if __name__ == "__main__":
    main()
