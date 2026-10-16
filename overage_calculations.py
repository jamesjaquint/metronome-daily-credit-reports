"""Unit-safe monthly billing controls and historical posted credit availability."""
import json
from datetime import datetime
from decimal import Decimal

CREDIT_NAME = 'mParticle Credits'
CREDIT_ID = 'ad022a80-3d77-48ba-8d68-26ef290352ed'
TOLERANCE = Decimal('0.000001')
HEADERS = ['Customer Name', 'Customer ID', 'Credits Granted\n(All-Time)',
           'Credits Unavailable\n(Used / Expired / Other)', 'Remaining Credits\n(at Billing Close)',
           'Finalized Usage Invoice (USD)', 'Overage Due (USD)',
           'Credits Unavailable %', 'Finalized Usage Invoice Count', 'Has Overage']

def number(value):
 return Decimal(str(value))

def stamp(value):
 return datetime.fromisoformat(value.replace('Z', '+00:00'))

def summarize(invoices, pages, start_date, end_date):
 start,end=stamp(start_date),stamp(end_date)
 monthly=[i for i in invoices if start <= stamp(i['start_timestamp']) < end]
 finalized=[i for i in monthly if i.get('status')=='FINALIZED' and i.get('type')=='PLAN_ARREARS']
 unsupported=[i for i in monthly if i.get('status')=='FINALIZED' and i.get('type')!='PLAN_ARREARS']
 entries=[];seen=set();endings=[];notes=[]
 for page in pages:
  for ledger in page.get('ledgers',[]):
   if ledger.get('credit_type',{}).get('name')!=CREDIT_NAME:
    raise ValueError('Unexpected credit ledger unit')
   closing=ledger.get('ending_balance',{})
   if stamp(closing['effective_at'])!=end:raise ValueError('Credit ledger cutoff differs from report month')
   endings.append(number(closing['excluding_pending']))
   for entry in ledger.get('entries',[]):
    # listEntries returns expiration entries at the cutoff, but its ending
    # balance excludes expirations at that exact instant (expires BEFORE cutoff).
    # Billing deductions at the cutoff are included in that balance.
    if stamp(entry['effective_at']) == end and 'expir' in entry.get('reason', '').lower():
     continue
    if stamp(entry['effective_at'])>end:raise ValueError('Credit entry after report cutoff')
    key=json.dumps(entry,sort_keys=True)
    if key not in seen:entries.append(entry);seen.add(key)
 if endings:
  if max(endings)-min(endings)>TOLERANCE:raise ValueError('Paginated ledger balances disagree')
  remaining=endings[-1]
  if abs(sum((number(e['amount']) for e in entries),Decimal(0))-remaining)>TOLERANCE:
   raise ValueError('Full posted credit ledger does not reconcile to API ending balance')
  granted=sum((number(e['amount']) for e in entries if number(e['amount'])>0),Decimal(0))
  unavailable=granted-remaining
  expired=-sum((number(e['amount']) for e in entries if number(e['amount'])<0 and 'expir' in e.get('reason','').lower()),Decimal(0))
  utilization=unavailable/granted*100 if granted else None
 else:
  granted=remaining=unavailable=expired=utilization=None
  notes.append('No credit ledger returned; credit measures are unavailable, not assumed zero.')
 usage=Decimal(0);known_usage=True
 for inv in finalized:
  for line in inv.get('line_items',[]):
   if line.get('credit_type',{}).get('name')==CREDIT_NAME:usage+=number(line['total'])
   elif number(line.get('total',0))!=0:known_usage=False
  if endings:
   applied=sum((number(a['total']) for a in inv.get('invoice_adjustments',[]) if a.get('credit_grant_id') and a.get('credit_type',{}).get('name')==CREDIT_NAME),Decimal(0))
   deducted=sum((number(e['amount']) for e in entries if e.get('invoice_id')==inv['id']),Decimal(0))
   if abs(applied-deducted)>TOLERANCE:raise ValueError('Invoice grant deduction does not reconcile to credit ledger: '+inv['id'])
 valid_currency=all(i.get('credit_type',{}).get('name')=='USD (cents)' for i in finalized)
 if finalized and valid_currency and not unsupported:
  total=sum((number(i['total'])/100 for i in finalized),Decimal(0))
  overage=max(Decimal(0),total)
  status='YES' if overage>0 else 'NO'
 else:
  total=overage=None;status='REVIEW'
  notes.append('No finalized PLAN_ARREARS invoice.' if not finalized else 'Unsupported invoice currency or invoice type; review source billing.')
 if any(i.get('status')=='DRAFT' for i in monthly):
  status='REVIEW';total=overage=None
  notes.append('Draft invoice present; finalized amount may not cover the complete month.')
 if total is not None and total<0:
  status='REVIEW';notes.append('Negative invoice total; review credit/refund treatment.')
 if granted is not None:
  notes.append('All-time posted credits through billing close: '+str(granted)+' added; '+str(expired)+' expired. Unavailable includes used, expired, and other reductions.')
 notes.append('Monthly invoice line-item usage: '+str(usage)+' mParticle Credits.' if finalized and known_usage else 'Monthly credit usage unavailable or mixed units.')
 return {'commit_total':float(granted) if granted is not None else None,
         'remaining_balance':float(remaining) if remaining is not None else None,
         'used_credits':float(unavailable) if unavailable is not None else None,
         'expired_credits':float(expired) if expired is not None else None,
         'utilization_pct':float(utilization) if utilization is not None else None,
         'invoice_total':float(total) if total is not None else None,
         'overage':float(overage) if overage is not None else None,
         'invoice_count':len(finalized),'audit_status':status,'has_overage':status=='YES',
         'monthly_usage_credits':float(usage) if finalized and known_usage else None,
         'invoice_ids':[i['id'] for i in finalized], 'notes':' '.join(notes)}
