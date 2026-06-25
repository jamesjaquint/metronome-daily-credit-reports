#!/usr/bin/env python3
"""
Daily Credit Usage Report for Metronome customers → Google Sheets
Uses invoices for exact totals + breakdowns for daily granularity.
Processes all customers from customers.json config.
"""

import os
import json
from datetime import datetime, timedelta
from dotenv import load_dotenv
import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build

# Load environment variables (just for API key now)
load_dotenv()

METRONOME_API_KEY = os.getenv('METRONOME_API_KEY')
SCOPES = ['https://www.googleapis.com/auth/spreadsheets']
CUSTOMERS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'customers.json')

def load_customers():
    """Load customers from config file."""
    with open(CUSTOMERS_FILE, 'r') as f:
        return json.load(f)

def save_customers(customers):
    """Save customers back to config file."""
    with open(CUSTOMERS_FILE, 'w') as f:
        json.dump(customers, f, indent=2)

def get_google_creds():
    """Get Google credentials from service account."""
    # Check for SERVICE_ACCOUNT_JSON env var first (for Lambda)
    sa_json = os.getenv('SERVICE_ACCOUNT_JSON')
    if sa_json:
        sa_info = json.loads(sa_json)
        return service_account.Credentials.from_service_account_info(sa_info, scopes=SCOPES)

    # Fall back to local file
    sa_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'doc-writer-jjaquint-fded78fa624f.json')
    return service_account.Credentials.from_service_account_file(sa_path, scopes=SCOPES)

def get_metronome_headers():
    return {
        'Authorization': f'Bearer {METRONOME_API_KEY}',
        'Content-Type': 'application/json'
    }

def create_spreadsheet(service, customer_name):
    """Create a new Google Sheet for a customer."""
    spreadsheet = service.spreadsheets().create(body={
        'properties': {'title': f'Metronome Credits - {customer_name}'},
        'sheets': [
            {'properties': {'title': 'Credit Usage'}},
            {'properties': {'title': 'Monthly Summary'}},
            {'properties': {'title': 'Daily Usage'}}
        ]
    }).execute()
    return spreadsheet['spreadsheetId']

def get_credit_grants(customer_id):
    """Get ALL credit grants from Metronome (paginated).

    IMPORTANT: listGrants takes limit/next_page as QUERY params, not body
    fields. Passing next_page in the JSON body is silently ignored and returns
    page 1 forever — which can miss the grant holding the live balance.
    """
    all_grants = []
    next_page = None
    while True:
        params = {'limit': 100}
        if next_page:
            params['next_page'] = next_page
        resp = requests.post(
            'https://api.metronome.com/v1/credits/listGrants',
            headers=get_metronome_headers(),
            params=params,
            json={'customer_ids': [customer_id]}
        )
        if resp.status_code != 200:
            raise Exception(f"Metronome API error: {resp.text}")
        result = resp.json()
        all_grants.extend(result.get('data', []))
        next_page = result.get('next_page')
        if not next_page:
            break
    return all_grants

def get_all_invoices(customer_id):
    """Get all invoices with pagination."""
    url = f'https://api.metronome.com/v1/customers/{customer_id}/invoices'
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

def get_invoice_totals_by_month(invoices):
    """Extract monthly totals from invoices - this is the source of truth."""
    monthly_totals = {}

    for inv in invoices:
        status = inv.get('status', '')
        if status not in ['FINALIZED', 'DRAFT']:
            continue

        start = inv.get('start_timestamp', '')[:10]
        end = inv.get('end_timestamp', '')[:10]

        # Parse the month from start date
        try:
            start_dt = datetime.strptime(start, '%Y-%m-%d')
            month_key = start_dt.strftime('%Y-%m')
        except:
            continue

        # Sum line items
        line_items_detail = {}
        for li in inv.get('line_items', []):
            total = li.get('total', 0)
            name = li.get('name', 'Unknown')

            # Categorize
            name_lower = name.lower()
            if 'data ingest' in name_lower or 'personalize' in name_lower:
                category = 'personalize'
            elif 'storage' in name_lower:
                category = 'storage'
            elif 'lookback' in name_lower:
                category = 'lookback'
            elif 'real time product' in name_lower:
                category = 'rt_products'
            else:
                category = 'other'

            if category not in line_items_detail:
                line_items_detail[category] = 0
            line_items_detail[category] += total

        invoice_total = sum(line_items_detail.values())

        if month_key not in monthly_totals:
            monthly_totals[month_key] = {
                'status': status,
                'start': start,
                'end': end,
                'total': 0,
                'personalize': 0,
                'storage': 0,
                'lookback': 0,
                'rt_products': 0,
                'other': 0
            }

        # Update totals (handle multiple invoices per month)
        monthly_totals[month_key]['total'] += invoice_total
        for cat in ['personalize', 'storage', 'lookback', 'rt_products', 'other']:
            monthly_totals[month_key][cat] += line_items_detail.get(cat, 0)

    return monthly_totals

def get_daily_breakdowns(customer_id, start_date, end_date):
    """Get daily breakdowns with pagination."""
    url = f'https://api.metronome.com/v1/customers/{customer_id}/invoices/breakdowns'
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
            print(f"    Warning: Breakdowns API returned {resp.status_code}")
            break

        result = resp.json()
        all_data.extend(result.get('data', []))
        next_page = result.get('next_page')
        if not next_page:
            break

    return all_data

def build_daily_usage(customer_id, months_back=2, plan_start=None, report_start=None):
    """Build daily usage that matches invoice totals exactly."""

    # Step 1: Get invoice totals (source of truth)
    print("    Getting invoice totals...")
    invoices = get_all_invoices(customer_id)
    monthly_invoice_totals = get_invoice_totals_by_month(invoices)

    # Step 2: Get date range
    today = datetime.now()

    if report_start:
        # Explicit per-customer anchor (e.g. plan/contract start). Floor to the
        # first of that month so monthly aggregation/reconciliation lines up.
        start_date = report_start.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        current_month_start = today.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        start_date = current_month_start
        for _ in range(months_back):
            start_date = (start_date - timedelta(days=1)).replace(day=1)
        if plan_start and plan_start > start_date:
            if plan_start.day > 1:
                if plan_start.month == 12:
                    start_date = plan_start.replace(year=plan_start.year + 1, month=1, day=1)
                else:
                    start_date = plan_start.replace(month=plan_start.month + 1, day=1)
            else:
                start_date = plan_start

    if today.month == 12:
        end_date = today.replace(year=today.year + 1, month=1, day=1)
    else:
        end_date = today.replace(month=today.month + 1, day=1)

    print(f"    Date range: {start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}")

    # Step 3: Get daily breakdowns
    print("    Getting daily breakdowns...")
    breakdown_data = get_daily_breakdowns(customer_id, start_date, end_date)
    print(f"    Got {len(breakdown_data)} day records from breakdowns API")

    # Step 4: Parse breakdowns into daily usage
    # Only include dates with actual data (skip future dates with 0 usage)
    daily_usage = {}
    breakdown_monthly_totals = {}
    today_str = datetime.now().strftime('%Y-%m-%d')

    for day_data in breakdown_data:
        date = day_data.get('breakdown_start_timestamp', '')[:10]
        if not date:
            continue

        # Skip future dates
        if date > today_str:
            continue

        # Check if this day has any actual usage
        day_has_data = False
        for li in day_data.get('line_items', []):
            for sub in li.get('sub_line_items', []):
                if sub.get('subtotal', 0) > 0:
                    day_has_data = True
                    break
            if day_has_data:
                break

        # Skip days with no actual usage data
        if not day_has_data:
            continue

        month_key = date[:7]  # YYYY-MM

        if date not in daily_usage:
            daily_usage[date] = {
                'date': date,
                'personalize': 0,
                'storage': 0,
                'lookback': 0,
                'rt_products': 0,
                'other': 0,
                'total': 0
            }

        if month_key not in breakdown_monthly_totals:
            breakdown_monthly_totals[month_key] = {
                'personalize': 0, 'storage': 0, 'lookback': 0,
                'rt_products': 0, 'other': 0, 'total': 0, 'days': 0
            }

        breakdown_monthly_totals[month_key]['days'] += 1

        for li in day_data.get('line_items', []):
            for sub in li.get('sub_line_items', []):
                sub_name = sub.get('name', '').lower()
                sub_total = sub.get('subtotal', 0)

                if 'personalize' in sub_name:
                    daily_usage[date]['personalize'] += sub_total
                    breakdown_monthly_totals[month_key]['personalize'] += sub_total
                elif 'storage' in sub_name or 'long term' in sub_name:
                    daily_usage[date]['storage'] += sub_total
                    breakdown_monthly_totals[month_key]['storage'] += sub_total
                elif 'lookback' in sub_name:
                    daily_usage[date]['lookback'] += sub_total
                    breakdown_monthly_totals[month_key]['lookback'] += sub_total
                elif 'real time product' in sub_name:
                    daily_usage[date]['rt_products'] += sub_total
                    breakdown_monthly_totals[month_key]['rt_products'] += sub_total
                else:
                    daily_usage[date]['other'] += sub_total
                    breakdown_monthly_totals[month_key]['other'] += sub_total

    # Step 5: Reconcile with invoice totals - distribute missing amounts across days
    # Only reconcile FINALIZED invoices, not drafts (current month)
    print("    Reconciling with invoice totals...")

    current_month = datetime.now().strftime('%Y-%m')

    for month_key, invoice_data in monthly_invoice_totals.items():
        if month_key not in breakdown_monthly_totals:
            continue

        # Skip current month (draft invoice) - only reconcile finalized months
        if month_key == current_month:
            print(f"      Skipping {month_key} (current month - draft invoice)")
            continue

        breakdown = breakdown_monthly_totals[month_key]
        days_in_month = breakdown['days']
        if days_in_month == 0:
            continue

        # Calculate differences per category
        for cat in ['personalize', 'storage', 'lookback', 'rt_products', 'other']:
            invoice_val = invoice_data.get(cat, 0)
            breakdown_val = breakdown.get(cat, 0)
            diff = invoice_val - breakdown_val

            if abs(diff) > 0.01:  # If there's a meaningful difference
                # Distribute difference evenly across days
                daily_adjustment = diff / days_in_month

                for date in daily_usage:
                    if date.startswith(month_key):
                        daily_usage[date][cat] += daily_adjustment

    # Step 6: Calculate daily totals
    for date in daily_usage:
        d = daily_usage[date]
        d['total'] = d['personalize'] + d['storage'] + d['lookback'] + d['rt_products'] + d['other']

    return list(daily_usage.values()), monthly_invoice_totals

def get_current_balance(grants):
    """Summarize credits across ALL grants to match Metronome's Credits tab.

    Returns (total_granted, available, consumed, expired, plan_start):
      - total_granted: sum of every grant amount (all time)
      - consumed:      sum of all non-expiry deductions (incl. pending)
      - expired:       sum of deductions whose reason is 'Credits expired'
      - available:     total_granted - consumed - expired
      - plan_start:    earliest currently-active grant (default date anchor)
    """
    now = datetime.now(tz=__import__('datetime').timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')
    total_granted = 0.0
    consumed = 0.0
    expired = 0.0
    earliest_active = None

    for grant in grants:
        total_granted += grant.get('grant_amount', {}).get('amount', 0)

        for ded in grant.get('deductions', []):
            amt = -ded.get('amount', 0)  # deductions are negative
            if 'expir' in ded.get('reason', '').lower():
                expired += amt
            else:
                consumed += amt
        for pd in grant.get('pending_deductions', []):
            consumed += -pd.get('amount', 0)

        effective_at = grant.get('effective_at', '')
        expires_at = grant.get('expires_at', '')
        if effective_at <= now < expires_at:
            if earliest_active is None or effective_at < earliest_active:
                earliest_active = effective_at

    available = total_granted - consumed - expired
    plan_start = None
    if earliest_active:
        plan_start = datetime.strptime(earliest_active[:10], '%Y-%m-%d')

    return total_granted, available, consumed, expired, plan_start

def ensure_sheet_exists(service, spreadsheet_id, sheet_name):
    """Ensure a sheet exists, create if not."""
    try:
        sheet_metadata = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
        sheets = [s['properties']['title'] for s in sheet_metadata.get('sheets', [])]

        if sheet_name not in sheets:
            service.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={'requests': [{'addSheet': {'properties': {'title': sheet_name}}}]}
            ).execute()
    except Exception as e:
        print(f"    Note: {e}")

def delete_default_sheet(service, spreadsheet_id):
    """Delete the default 'Sheet1' if it exists."""
    try:
        sheet_metadata = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
        sheets = sheet_metadata.get('sheets', [])

        for sheet in sheets:
            if sheet['properties']['title'] == 'Sheet1':
                sheet_id = sheet['properties']['sheetId']
                service.spreadsheets().batchUpdate(
                    spreadsheetId=spreadsheet_id,
                    body={'requests': [{'deleteSheet': {'sheetId': sheet_id}}]}
                ).execute()
                break
    except Exception as e:
        pass  # Ignore if Sheet1 doesn't exist or can't be deleted

def reorder_sheets(service, spreadsheet_id):
    """Reorder sheets: Credit Usage, Daily Usage, Monthly Summary."""
    try:
        sheet_metadata = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
        sheets = {s['properties']['title']: s['properties']['sheetId'] for s in sheet_metadata.get('sheets', [])}

        desired_order = ['Credit Usage', 'Daily Usage', 'Monthly Summary']
        requests = []

        for index, name in enumerate(desired_order):
            if name in sheets:
                requests.append({
                    'updateSheetProperties': {
                        'properties': {'sheetId': sheets[name], 'index': index},
                        'fields': 'index'
                    }
                })

        if requests:
            service.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={'requests': requests}
            ).execute()
    except Exception as e:
        pass  # Ignore reorder errors

def get_month_label(date_str):
    """Get month label from date string."""
    dt = datetime.strptime(date_str, '%Y-%m-%d')
    return dt.strftime('%b %Y')

def update_spreadsheet(service, spreadsheet_id, customer_name, daily_usage, invoice_totals, grants):
    """Update the spreadsheet with daily usage data."""

    # Ensure all required sheets exist
    ensure_sheet_exists(service, spreadsheet_id, 'Credit Usage')
    ensure_sheet_exists(service, spreadsheet_id, 'Monthly Summary')
    ensure_sheet_exists(service, spreadsheet_id, 'Daily Usage')

    # Remove default Sheet1 if present and reorder tabs
    delete_default_sheet(service, spreadsheet_id)
    reorder_sheets(service, spreadsheet_id)

    # Prepare headers
    daily_headers = [[
        'Month',
        'Date',
        'Personalize',
        'Storage',
        'RT Lookback',
        'RT Products',
        'Other',
        'Total Credits Used'
    ]]

    daily_rows = []
    monthly_totals = {}

    for d in sorted(daily_usage, key=lambda x: x.get('date', ''), reverse=True):
        month = get_month_label(d['date'])

        if month not in monthly_totals:
            monthly_totals[month] = {
                'personalize': 0, 'storage': 0, 'lookback': 0,
                'rt_products': 0, 'other': 0, 'total': 0
            }

        monthly_totals[month]['personalize'] += d['personalize']
        monthly_totals[month]['storage'] += d['storage']
        monthly_totals[month]['lookback'] += d['lookback']
        monthly_totals[month]['rt_products'] += d['rt_products']
        monthly_totals[month]['other'] += d['other']
        monthly_totals[month]['total'] += d['total']

        daily_rows.append([
            month,
            d['date'],
            round(d['personalize'], 2),
            round(d['storage'], 2),
            round(d['lookback'], 2),
            round(d['rt_products'], 2),
            round(d['other'], 2),
            round(d['total'], 2)
        ])

    # Write daily data
    service.spreadsheets().values().clear(
        spreadsheetId=spreadsheet_id,
        range='Daily Usage!A:H'
    ).execute()

    service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range='Daily Usage!A1',
        valueInputOption='RAW',
        body={'values': daily_headers + daily_rows}
    ).execute()

    # Monthly Summary
    monthly_headers = [[
        'Month',
        'Personalize',
        'Storage',
        'RT Lookback',
        'RT Products',
        'Other',
        'Total Credits Used'
    ]]

    monthly_rows = []
    for month in sorted(monthly_totals.keys(), key=lambda mk: datetime.strptime(mk, '%b %Y'), reverse=True):
        m = monthly_totals[month]

        monthly_rows.append([
            month,
            round(m['personalize'], 2),
            round(m['storage'], 2),
            round(m['lookback'], 2),
            round(m['rt_products'], 2),
            round(m['other'], 2),
            round(m['total'], 2)
        ])

    service.spreadsheets().values().clear(
        spreadsheetId=spreadsheet_id,
        range='Monthly Summary!A:G'
    ).execute()

    service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range='Monthly Summary!A1',
        valueInputOption='RAW',
        body={'values': monthly_headers + monthly_rows}
    ).execute()

    # Credit Usage Summary (mirrors Metronome Credits tab)
    total_granted, available, consumed, expired, _ = get_current_balance(grants)

    summary = [
        ['Metric', 'Value'],
        ['Customer', customer_name],
        ['Report Date', datetime.now().strftime('%Y-%m-%d %H:%M')],
        ['Data Source', 'Invoices + Daily Breakdowns (reconciled); credits from listGrants'],
        ['', ''],
        ['=== Credit Balance (Metronome Credits tab) ===', ''],
        ['Available Balance', round(available, 2)],
        ['Consumed', round(consumed, 2)],
        ['Expired', round(expired, 2)],
        ['Total Granted', round(total_granted, 2)],
        ['Consumed %', f"{(consumed/total_granted*100):.1f}%" if total_granted > 0 else 'N/A'],
        ['', ''],
        ['=== Credit Grants (when applied) ===', ''],
        ['Applied (Effective)', 'Amount (Expires)'],
    ]

    for grant in sorted(grants, key=lambda g: g.get('effective_at', '')):
        eff = grant.get('effective_at', '')[:10]
        exp = grant.get('expires_at', '')[:10]
        amt = grant.get('grant_amount', {}).get('amount', 0)
        summary.append([eff, f"{amt:,.0f} (exp {exp})"])

    summary += [
        ['', ''],
        ['=== Monthly Totals (Invoice Match) ===', ''],
    ]

    for month in sorted(monthly_totals.keys(), key=lambda mk: datetime.strptime(mk, '%b %Y'), reverse=True):
        month_dt = datetime.strptime(month, '%b %Y')
        month_key = month_dt.strftime('%Y-%m')
        invoice_total = invoice_totals.get(month_key, {}).get('total', 0)
        summary.append([month, round(invoice_total, 2)])


    service.spreadsheets().values().clear(
        spreadsheetId=spreadsheet_id,
        range='Credit Usage!A:B'
    ).execute()

    service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range='Credit Usage!A1',
        valueInputOption='RAW',
        body={'values': summary}
    ).execute()

    print(f"    Updated spreadsheet with {len(daily_rows)} days across {len(monthly_totals)} months")
    return monthly_totals

def process_customer(service, customer, customers, customers_modified):
    """Process a single customer."""
    customer_name = customer['name']
    customer_id = customer['id']
    spreadsheet_id = customer.get('spreadsheet_id')

    print(f"\n{'='*50}")
    print(f"Customer: {customer_name}")
    print(f"{'='*50}")

    # Auto-create spreadsheet if needed
    if not spreadsheet_id:
        print(f"  Creating new Google Sheet...")
        spreadsheet_id = create_spreadsheet(service, customer_name)
        customer['spreadsheet_id'] = spreadsheet_id
        customers_modified[0] = True
        print(f"  Created: https://docs.google.com/spreadsheets/d/{spreadsheet_id}")

    # Check if customer has credit grants (active plan)
    print(f"  Fetching credit grants...")
    grants = get_credit_grants(customer_id)

    if not grants:
        print(f"  No credit grants found - skipping")
        return None

    print(f"  Found {len(grants)} grants")

    # Get balance and plan start date
    total_granted, available, consumed, expired, plan_start = get_current_balance(grants)

    # Optional per-customer report start (e.g. plan/contract start date)
    report_start = None
    rs = customer.get('report_start')
    if rs:
        report_start = datetime.strptime(rs[:10], '%Y-%m-%d')

    # Build daily usage
    print(f"  Building daily usage (reconciled with invoices)...")
    daily_usage, invoice_totals = build_daily_usage(customer_id, months_back=2, plan_start=plan_start, report_start=report_start)
    print(f"  Found {len(daily_usage)} days of data")

    # Update spreadsheet
    monthly_totals = update_spreadsheet(service, spreadsheet_id, customer_name, daily_usage, invoice_totals, grants)

    # Print summary
    print(f"\n  Summary:")
    print(f"    Available: {available:,.2f} credits (granted {total_granted:,.0f}, consumed {consumed:,.0f}, expired {expired:,.0f})")
    for month in sorted(monthly_totals.keys(), key=lambda mk: datetime.strptime(mk, '%b %Y'), reverse=True):
        print(f"    {month}: {monthly_totals[month]['total']:,.2f} credits")
    print(f"  Sheet: https://docs.google.com/spreadsheets/d/{spreadsheet_id}")

    return monthly_totals

def main():
    print(f"=== Metronome Daily Credit Report ===")
    print(f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M')}")

    # Load customers
    customers = load_customers()
    print(f"Loaded {len(customers)} customers from config")

    # Authenticate with Google
    print("Authenticating with Google...")
    creds = get_google_creds()
    service = build('sheets', 'v4', credentials=creds)

    # Track if we need to save config (new spreadsheets created)
    customers_modified = [False]

    # Process each customer
    for customer in customers:
        try:
            process_customer(service, customer, customers, customers_modified)
        except Exception as e:
            print(f"  ERROR: {e}")
            continue

    # Save config if spreadsheet IDs were added
    if customers_modified[0]:
        save_customers(customers)
        print(f"\nUpdated customers.json with new spreadsheet IDs")

    print(f"\n{'='*50}")
    print("All customers processed!")

if __name__ == "__main__":
    main()
