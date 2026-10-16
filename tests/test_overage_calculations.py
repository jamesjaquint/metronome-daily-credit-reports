import unittest
from copy import deepcopy
from overage_calculations import summarize

CTYPE={'id':'credits','name':'mParticle Credits'}
USD={'id':'usd','name':'USD (cents)'}
def invoice(total=0,status='FINALIZED',kind='PLAN_ARREARS',unit=None):
 return {'id':'inv','status':status,'type':kind,'start_timestamp':'2026-08-01T00:00:00Z','end_timestamp':'2026-09-01T00:00:00Z','credit_type':unit or USD,'total':total,'line_items':[{'total':1184.3226365967741,'credit_type':CTYPE}], 'invoice_adjustments':[{'credit_grant_id':'grant','total':-1184.3226365967741,'credit_type':CTYPE}]}
def ledger():
 return [{'customer_id':'customer','ledgers':[{'credit_type':CTYPE,'entries':[{'amount':91706,'reason':'Grant created','effective_at':'2025-08-12T00:00:00Z','credit_grant_id':'grant'},{'amount':-1184.3226365967741,'reason':'Credits used','effective_at':'2026-08-12T00:00:00Z','invoice_id':'inv','credit_grant_id':'grant'},{'amount':-90521.67736340323,'reason':'Credits expired','effective_at':'2026-08-12T00:00:00Z','credit_grant_id':'grant'}],'ending_balance':{'excluding_pending':0,'including_pending':0,'effective_at':'2026-09-01T00:00:00Z'}}]}]
class AuditTests(unittest.TestCase):
 def result(self,inv=None,pages=None):return summarize(inv if inv is not None else [invoice()],pages if pages is not None else ledger(),'2026-08-01T00:00:00Z','2026-09-01T00:00:00Z')
 def test_credit_units_are_not_converted_with_invoice_cents(self):
  r=self.result();self.assertEqual(r['commit_total'],91706);self.assertEqual(r['remaining_balance'],0)
 def test_expiration_is_unavailable_not_monthly_usage_or_overage(self):
  r=self.result();self.assertEqual(r['used_credits'],91706);self.assertAlmostEqual(r['monthly_usage_credits'],1184.3226365967741);self.assertEqual(r['utilization_pct'],100);self.assertEqual(r['overage'],0);self.assertEqual(r['audit_status'],'NO')
 def test_positive_finalized_usage_due_is_not_reduced_by_grant_twice(self):
  i=invoice(1505579);r=self.result([i]);self.assertEqual(r['invoice_total'],15055.79);self.assertEqual(r['overage'],15055.79);self.assertEqual(r['audit_status'],'YES')
 def test_drafts_and_voids_do_not_establish_zero_due(self):
  for status in ['VOID','DRAFT']:
   r=self.result([invoice(status=status)]);self.assertEqual(r['audit_status'],'REVIEW');self.assertIsNone(r['invoice_total']);self.assertIsNone(r['overage'])
 def test_fixed_purchase_is_not_a_usage_overage(self):
  r=self.result([invoice(total=100000,kind='CREDIT_PURCHASE')]);self.assertEqual(r['audit_status'],'REVIEW');self.assertIsNone(r['overage'])
 def test_missing_ledger_is_not_a_zero_balance(self):
  r=self.result(pages=[]);self.assertIsNone(r['remaining_balance']);self.assertIsNone(r['commit_total']);self.assertIsNone(r['utilization_pct'])
 def test_zero_posted_balance_does_not_fall_back_to_pending(self):
  p=ledger();p[0]['ledgers'][0]['ending_balance']['including_pending']=100;r=self.result(pages=p);self.assertEqual(r['remaining_balance'],0)
 def test_unknown_invoice_currency_is_review_not_dollars(self):
  r=self.result([invoice(unit={'name':'EUR (cents)','id':'eur'})]);self.assertEqual(r['audit_status'],'REVIEW');self.assertIsNone(r['invoice_total'])
 def test_paginated_duplicate_entries_are_counted_once(self):
  p=ledger();r=self.result(pages=p+deepcopy(p));self.assertEqual(r['commit_total'],91706)
 def test_ledger_reconciliation_failure_stops_calculation(self):
  p=ledger();p[0]['ledgers'][0]['ending_balance']['excluding_pending']=1
  with self.assertRaisesRegex(ValueError,'reconcile'):self.result(pages=p)
 def test_invoice_credit_deduction_must_reconcile_to_ledger(self):
  i=invoice();i['invoice_adjustments'][0]['total']=-3
  with self.assertRaisesRegex(ValueError,'deduction'):self.result([i])
 def test_expiration_exactly_at_cutoff_is_outside_api_ending_balance(self):
  p=ledger();l=p[0]['ledgers'][0]
  l['entries'][-1]['effective_at']='2026-09-01T00:00:00Z'
  l['ending_balance']['excluding_pending']=90521.67736340323
  r=self.result(pages=p)
  self.assertAlmostEqual(r['remaining_balance'],90521.67736340323)
  self.assertAlmostEqual(r['used_credits'],1184.3226365967741)
  self.assertEqual(r['expired_credits'],0)
 def test_invoice_from_next_month_is_excluded(self):
  i=invoice(10000);i['start_timestamp']='2026-09-01T00:00:00Z';r=self.result([invoice(),i]);self.assertEqual(r['invoice_total'],0)
if __name__=='__main__':unittest.main()
