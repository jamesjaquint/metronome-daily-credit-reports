import unittest
from unittest.mock import patch
import overage_audit as audit
class UnitRegression(unittest.TestCase):
 def test_usd_invoice_does_not_convert_mparticle_credit_grants(self):
  inv={'id':'inv','status':'FINALIZED','type':'PLAN_ARREARS','credit_type':{'name':'USD (cents)'},'total':0,'start_timestamp':'2026-08-01T00:00:00Z','end_timestamp':'2026-09-01T00:00:00Z','line_items':[],'invoice_adjustments':[]}
  grant={'grant_amount':{'amount':91706,'credit_type':{'name':'mParticle Credits'}},'balance':{'excluding_pending':0,'including_pending':0}}
  ledger=[{'customer_id':'customer','ledgers':[{'credit_type':{'name':'mParticle Credits'},'entries':[{'amount':91706,'effective_at':'2025-08-12T00:00:00Z','reason':'Grant created'},{'amount':-91706,'effective_at':'2026-08-12T00:00:00Z','reason':'Credits expired'}],'ending_balance':{'excluding_pending':0,'including_pending':0,'effective_at':'2026-09-01T00:00:00Z'}}]}]
  with patch.object(audit,'get_customer_invoices',return_value=[inv]),patch.object(audit,'get_customer_credit_ledger',return_value=ledger):
   r=audit.audit_customer({'id':'customer','name':'Test'},'2026-08-01T00:00:00Z','2026-09-01T00:00:00Z')
  self.assertEqual(r['commit_total'],91706)
