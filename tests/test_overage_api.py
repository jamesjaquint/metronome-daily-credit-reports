import contextlib,io,unittest
from unittest.mock import Mock,patch
import overage_audit as audit
class APITests(unittest.TestCase):
 def test_ledger_pagination_uses_query_cursor_and_exact_cutoff(self):
  responses=[]
  for payload in [{'data':[{'a':1}],'next_page':'page2'},{'data':[{'a':2}]}]:
   response=Mock();response.json.return_value=payload;responses.append(response)
  with patch.object(audit.requests,'post',side_effect=responses) as post:
   result=audit.get_customer_credit_ledger('customer','2026-09-01T00:00:00Z')
  self.assertEqual(result,[{'a':1},{'a':2}])
  args=post.call_args_list[1].kwargs
  self.assertEqual(args['params']['next_page'],'page2')
  self.assertNotIn('next_page',args['json'])
  self.assertEqual(args['json']['ending_before'],'2026-09-01T00:00:00Z')
 def test_api_failure_is_not_interpreted_as_no_invoices(self):
  response=Mock(status_code=503);response.raise_for_status.side_effect=RuntimeError('API unavailable')
  with patch.object(audit.requests,'get',return_value=response):
   with self.assertRaisesRegex(RuntimeError,'API unavailable'):audit.get_customer_invoices('customer','2026-08-01','2026-09-01')
 def test_dry_run_does_not_access_google_or_write_sheet(self):
  result={'has_overage':False,'audit_status':'NO','invoice_total':0}
  with patch('sys.argv',['overage_audit.py','--month','2026-08','--dry-run']),patch.object(audit,'get_all_customers',return_value=[{'id':'c','name':'Test'}]),patch.object(audit,'audit_customer',return_value=result),patch.object(audit,'get_google_creds') as creds,patch.object(audit,'publish_report') as write,contextlib.redirect_stdout(io.StringIO()):
   audit.main()
  creds.assert_not_called();write.assert_not_called()

class ResponseGuardTests(unittest.TestCase):
 def test_success_status_without_data_is_not_an_empty_invoice_list(self):
  response=Mock();response.json.return_value={'message':'incomplete response'}
  with patch.object(audit.requests,'get',return_value=response):
   with self.assertRaisesRegex(ValueError,'Malformed'):audit.get_customer_invoices('customer','2026-08-01','2026-09-01')
 def test_repeated_cursor_stops_instead_of_looping_forever(self):
  response=Mock();response.json.return_value={'data':[],'next_page':'same'}
  with patch.object(audit.requests,'get',return_value=response):
   with self.assertRaisesRegex(ValueError,'repeated'):audit.get_customer_invoices('customer','2026-08-01','2026-09-01')
 def test_numeric_cursor_is_rejected(self):
  response=Mock();response.json.return_value={'data':[],'next_page':123}
  with patch.object(audit.requests,'get',return_value=response):
   with self.assertRaisesRegex(ValueError,'cursor'):audit.get_all_customers()
