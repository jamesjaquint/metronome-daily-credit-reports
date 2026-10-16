import unittest
from copy import deepcopy
from unittest.mock import Mock
import overage_audit as audit
from test_overage_calculations import invoice, ledger
from overage_calculations import summarize

START='2026-08-01T00:00:00Z'
END='2026-09-01T00:00:00Z'
class CalculationReviewTests(unittest.TestCase):
 def test_refund_grant_reason_mentioning_expiry_is_not_an_expiration(self):
  p=ledger();l=p[0]['ledgers'][0]
  l['entries'].append({'amount':100,'reason':'Grant created: Refund for grant that expired prematurely','effective_at':END,'credit_grant_id':'refund'})
  l['ending_balance']['excluding_pending']=100
  r=summarize([invoice()],p,START,END)
  self.assertEqual(r['commit_total'],91806)
  self.assertEqual(r['remaining_balance'],100)
 def test_cutoff_invoice_deduction_mentioning_expiry_is_still_a_deduction(self):
  p=ledger();l=p[0]['ledgers'][0];l['entries'][1]['effective_at']=END
  l['entries'][1]['reason']='Credit deduction from grant: credits replaced after expired trial'
  r=summarize([invoice()],p,START,END)
  self.assertEqual(r['remaining_balance'],0)
 def test_a_positive_invoice_cannot_hide_a_negative_invoice(self):
  positive=invoice(20000);positive['invoice_adjustments']=[];positive['id']='positive'
  negative=invoice(-10000);negative['invoice_adjustments']=[];negative['id']='negative'
  r=summarize([positive,negative],[],START,END)
  self.assertEqual(r['audit_status'],'REVIEW')
  self.assertIsNone(r['overage'])
 def test_duplicate_invoice_id_does_not_double_the_due(self):
  i=invoice(15000);i['invoice_adjustments']=[]
  r=summarize([i,deepcopy(i)],[],START,END)
  self.assertEqual(r['invoice_total'],150)
 def test_non_grant_positive_adjustment_is_not_counted_as_a_new_grant(self):
  p=ledger();l=p[0]['ledgers'][0]
  l['entries'].append({'amount':100,'reason':'Invoice deduction reversed','effective_at':'2026-08-15T00:00:00Z','credit_grant_id':'grant'})
  l['ending_balance']['excluding_pending']=100
  with self.assertRaisesRegex(ValueError,'positive credit entry'):
   summarize([invoice()],p,START,END)
 def test_cross_month_invoice_requires_review_instead_of_claiming_monthly_total(self):
  i=invoice(10000);i['end_timestamp']='2026-09-12T00:00:00Z';i['invoice_adjustments']=[]
  r=summarize([i],[],START,END)
  self.assertEqual(r['audit_status'],'REVIEW');self.assertIsNone(r['overage'])
 def test_missing_entries_cannot_be_interpreted_as_an_empty_zero_ledger(self):
  p=ledger();del p[0]['ledgers'][0]['entries']
  i=invoice(10000);i['invoice_adjustments']=[]
  with self.assertRaisesRegex(ValueError,'entries'):summarize([i],p,START,END)
if __name__=='__main__':unittest.main()
