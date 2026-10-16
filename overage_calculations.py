"""Unit-safe monthly billing controls and historical posted credit availability."""
import json
from datetime import datetime
from decimal import Decimal

CREDIT_NAME = 'mParticle Credits'
CREDIT_ID = 'ad022a80-3d77-48ba-8d68-26ef290352ed'
TOLERANCE = Decimal('0.000001')
HEADERS = [
    'Customer Name', 'Customer ID', 'Credits Granted\n(All-Time)',
    'Credits Unavailable\n(Used / Expired / Other)',
    'Remaining Credits\n(at Billing Close)', 'Finalized Usage Invoice (USD)',
    'Overage Due (USD)', 'Credits Unavailable %',
    'Finalized Usage Invoice Count', 'Has Overage',
]


def number(value):
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError('Non-finite financial amount')
    return result


def stamp(value):
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('Source timestamp is missing its timezone')
    return result


def is_expiration(entry):
    # Grant names and reasons are free text. Mentioning expiry is insufficient.
    return (number(entry['amount']) <= 0
            and not entry.get('invoice_id')
            and entry.get('reason', '').strip().casefold() == 'credits expired')


def unique_invoices(invoices):
    by_id = {}
    for invoice in invoices:
        invoice_id = invoice['id']
        if invoice_id in by_id and by_id[invoice_id] != invoice:
            raise ValueError('Conflicting versions of invoice ' + invoice_id)
        by_id[invoice_id] = invoice
    return list(by_id.values())


def summarize(invoices, pages, start_date, end_date):
    start, end = stamp(start_date), stamp(end_date)
    monthly = [i for i in unique_invoices(invoices)
               if start <= stamp(i['start_timestamp']) < end]
    finalized = [i for i in monthly
                 if i.get('status') == 'FINALIZED' and i.get('type') == 'PLAN_ARREARS']
    unsupported = [i for i in monthly
                   if i.get('status') == 'FINALIZED' and i.get('type') != 'PLAN_ARREARS']
    unsupported_period = any(not (stamp(i['start_timestamp']) < stamp(i['end_timestamp']) <= end)
                             for i in finalized)
    entries, seen, endings, diagnostics = [], set(), [], []
    for page in pages:
        if not isinstance(page.get('ledgers'), list):
            raise ValueError('Malformed credit ledger response')
        for ledger in page['ledgers']:
            if not isinstance(ledger.get('entries'), list):
                raise ValueError('Missing credit ledger entries')
            if ledger.get('credit_type', {}).get('name') != CREDIT_NAME:
                raise ValueError('Unexpected credit ledger unit')
            closing = ledger['ending_balance']
            if stamp(closing['effective_at']) != end:
                raise ValueError('Credit ledger cutoff differs from report month')
            endings.append(number(closing['excluding_pending']))
            for entry in ledger.get('entries', []):
                # The API excludes expiry exactly at cutoff from ending balance,
                # but includes the closing invoice deductions at that instant.
                if stamp(entry['effective_at']) == end and is_expiration(entry):
                    continue
                if stamp(entry['effective_at']) > end:
                    raise ValueError('Credit entry after report cutoff')
                key = json.dumps(entry, sort_keys=True)
                if key not in seen:
                    entries.append(entry)
                    seen.add(key)
    if endings:
        if max(endings) - min(endings) > TOLERANCE:
            raise ValueError('Paginated ledger balances disagree')
        remaining = endings[-1]
        if abs(sum((number(e['amount']) for e in entries), Decimal(0)) - remaining) > TOLERANCE:
            raise ValueError('Full posted credit ledger does not reconcile to API ending balance')
        positive = [e for e in entries if number(e['amount']) > 0]
        if any(not e.get('reason', '').startswith('Grant created') for e in positive):
            raise ValueError('Unsupported positive credit entry; cannot establish credits granted')
        granted = sum((number(e['amount']) for e in positive), Decimal(0))
        unavailable = granted - remaining
        expired = -sum((number(e['amount']) for e in entries if is_expiration(e)), Decimal(0))
        utilization = unavailable / granted * 100 if granted else None
    else:
        granted = remaining = unavailable = expired = utilization = None
        diagnostics.append('No credit ledger returned; credit measures are unavailable, not assumed zero.')

    usage, known_usage = Decimal(0), not unsupported_period
    for invoice in finalized:
        for line in invoice.get('line_items', []):
            if line.get('credit_type', {}).get('name') == CREDIT_NAME:
                usage += number(line['total'])
            elif number(line.get('total', 0)) != 0:
                known_usage = False
        if endings:
            applied = sum((number(a['total']) for a in invoice.get('invoice_adjustments', [])
                           if a.get('credit_grant_id')
                           and a.get('credit_type', {}).get('name') == CREDIT_NAME), Decimal(0))
            deducted = sum((number(e['amount']) for e in entries
                            if e.get('invoice_id') == invoice['id']), Decimal(0))
            if abs(applied - deducted) > TOLERANCE:
                raise ValueError('Invoice grant deduction does not reconcile to credit ledger: ' + invoice['id'])

    valid_currency = all(i.get('credit_type', {}).get('name') == 'USD (cents)' for i in finalized)
    negative_invoice = any(number(i['total']) < 0 for i in finalized)
    draft = any(i.get('status') == 'DRAFT' for i in monthly)
    if (finalized and valid_currency and not unsupported and not unsupported_period
            and not negative_invoice and not draft):
        total = sum((number(i['total']) / 100 for i in finalized), Decimal(0))
        overage = max(Decimal(0), total)
        status = 'YES' if overage > 0 else 'NO'
    else:
        total = overage = None
        status = 'REVIEW'
        if not finalized:
            diagnostics.append('No finalized PLAN_ARREARS invoice.')
        if not valid_currency or unsupported:
            diagnostics.append('Unsupported invoice currency or invoice type; review source billing.')
        if unsupported_period:
            diagnostics.append('Invoice period crosses the report cutoff or has an invalid interval.')
        if negative_invoice:
            diagnostics.append('Negative invoice present; review credit/refund treatment before netting amounts.')
        if draft:
            diagnostics.append('Draft invoice present; finalized amount may not cover the complete month.')
    if granted is not None:
        diagnostics.append(f'All-time posted credits through billing close: {granted} added; {expired} expired. '
                           'Unavailable includes used, expired, and other reductions.')
    diagnostics.append(f'Monthly invoice line-item usage: {usage} mParticle Credits.'
                       if finalized and known_usage else 'Monthly credit usage unavailable or mixed units.')

    def optional(value):
        return float(value) if value is not None else None

    return {
        'commit_total': optional(granted), 'remaining_balance': optional(remaining),
        'used_credits': optional(unavailable), 'expired_credits': optional(expired),
        'utilization_pct': optional(utilization), 'invoice_total': optional(total),
        'overage': optional(overage), 'invoice_count': len(finalized),
        'audit_status': status, 'has_overage': status == 'YES',
        'monthly_usage_credits': float(usage) if finalized and known_usage else None,
        'invoice_ids': [i['id'] for i in finalized], 'notes': ' '.join(diagnostics),
    }
