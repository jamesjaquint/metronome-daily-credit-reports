import unittest
from copy import deepcopy
from unittest.mock import Mock
from overage_publication import build_plan, publish_report, SUMMARY_HEADERS, _matches
from overage_calculations import HEADERS

def result(cid='customer',status='NO',due=0):
 return {'customer_id':cid,'customer_name':cid,'commit_total':100,'used_credits':25,
         'remaining_balance':75,'invoice_total':due if status!='REVIEW' else None,
         'overage':due if status!='REVIEW' else None,'utilization_pct':25,
         'invoice_count':1 if status!='REVIEW' else 0,'audit_status':status}
def metadata():return {'sheets':[{'properties':{'title':'Summary','sheetId':5,'gridProperties':{'rowCount':1000,'columnCount':26}}}]}
def updates(plan):return [r['updateCells'] for r in plan['requests'] if 'updateCells' in r]
def vals(update):return [[c.get('userEnteredValue',{}).get('numberValue',c.get('userEnteredValue',{}).get('stringValue','')) for c in r['values']] for r in update['rows']]
class PublicationTests(unittest.TestCase):
 def test_summary_exposes_unresolved_rows_instead_of_presenting_zero_as_complete(self):
  plan=build_plan(metadata(),{},'Aug-2026',[result('known','YES',100),result('unresolved','REVIEW')],run_at='test')
  summary=next(u for u in updates(plan) if u['range']['sheetId']==5 and u['range']['startRowIndex']==1)
  self.assertEqual(vals(summary),[['Aug-2026',2,1,'','test',1]])
 def test_existing_month_requires_explicit_full_regeneration(self):
  with self.assertRaisesRegex(ValueError,'replace-existing'):build_plan(metadata(),{'Aug-2026':[HEADERS]},'Aug-2026',[result()])
 def test_annotations_remain_with_their_customer_on_regeneration(self):
  meta=metadata();meta['sheets'].append({'properties':{'title':'Aug-2026','sheetId':6}})
  old=[HEADERS,['low','low',0,0,0,0,0,'0%',1,'NO','Keep with low'],['high','high',0,0,0,0,0,'0%',1,'NO','Keep with high']]
  plan=build_plan(meta,{'Aug-2026':old},'Aug-2026',[result('high','YES',100),result('low')],allow_replace=True)
  monthly=next(u for u in updates(plan) if u['range']['sheetId']==6)
  self.assertEqual([r[1] for r in vals(monthly)[1:]],['low','high'])
  self.assertTrue(all(u['range']['endColumnIndex']<=10 for u in updates(plan)))
 def test_a_removed_customer_cannot_orphan_manual_annotations(self):
  with self.assertRaisesRegex(ValueError,'annotations'):build_plan(metadata(),{'Aug-2026':[HEADERS,['gone','gone',0,0,0,0,0,'0%',1,'NO','owner']]},'Aug-2026',[result()],allow_replace=True)
 def test_summary_custom_column_is_not_overwritten(self):
  with self.assertRaisesRegex(ValueError,'different purpose'):build_plan(metadata(),{'Summary':[SUMMARY_HEADERS[:5]+['Owner']]},'Aug-2026',[result()])
 def test_other_summary_months_are_not_rewritten(self):
  old=[SUMMARY_HEADERS[:5],['Jul-2026',12,1,100,'original run']]
  plan=build_plan(metadata(),{'Summary':old},'Aug-2026',[result()])
  self.assertFalse(any(u['range']['sheetId']==5 and u['range']['startRowIndex']==1 for u in updates(plan)))
 def test_no_cell_notes_or_comments_are_written(self):
  plan=build_plan(metadata(),{},'Aug-2026',[result()])
  for request in plan['requests']:
   self.assertNotIn('note',str(request));self.assertNotIn('comment',str(request))
 def test_replacement_and_summary_are_one_atomic_call_without_clear(self):
  service=Mock();api=service.spreadsheets.return_value
  api.get.return_value.execute.return_value=metadata()
  api.values.return_value.get.return_value.execute.return_value={'values':[]}
  def readback(**kwargs):
   # Observe the actual publication payload so the simulated readback is independent
   # of how many non-value/format requests accompany the data writes.
   requests=api.batchUpdate.call_args.kwargs['body']['requests']
   output=[]
   for address in kwargs['ranges']:
    if address.startswith("'Aug-2026'"):
     u=next(r['updateCells'] for r in requests if 'updateCells' in r and r['updateCells']['range']['sheetId']!=5)
     rows=vals(u)
    elif address=="'Summary'!F1":rows=[[SUMMARY_HEADERS[5]]]
    else:
     u=next(r['updateCells'] for r in requests if 'updateCells' in r and r['updateCells']['range']['sheetId']==5 and r['updateCells']['range']['startRowIndex']==1)
     rows=vals(u)
    output.append({'range':address,'values':rows})
   response=Mock();response.execute.return_value={'valueRanges':output};return response
  api.values.return_value.batchGet.side_effect=readback
  publish_report(service,'spreadsheet','Aug-2026',[result()])
  api.batchUpdate.assert_called_once();api.values.return_value.clear.assert_not_called();api.values.return_value.update.assert_not_called()
 def test_failed_batch_does_not_attempt_separate_summary_write(self):
  service=Mock();api=service.spreadsheets.return_value
  api.get.return_value.execute.return_value=metadata();api.values.return_value.get.return_value.execute.return_value={'values':[]}
  api.batchUpdate.return_value.execute.side_effect=RuntimeError('batch rejected')
  with self.assertRaisesRegex(RuntimeError,'batch rejected'):publish_report(service,'spreadsheet','Aug-2026',[result()])
  api.batchUpdate.assert_called_once();api.values.return_value.batchGet.assert_not_called();api.values.return_value.clear.assert_not_called()
 def test_changed_data_stops_publication(self):
  service=Mock();api=service.spreadsheets.return_value
  api.get.return_value.execute.return_value=metadata()
  api.values.return_value.get.return_value.execute.side_effect=[{'values':[]},{'values':[['new user data']]}]
  with self.assertRaisesRegex(ValueError,'changed during'):publish_report(service,'spreadsheet','Aug-2026',[result()])
  api.batchUpdate.assert_not_called()
 def test_readback_detects_wrong_customer_or_missing_cells(self):
  self.assertFalse(_matches([['wrong',100]],[['right',100]]))
  self.assertFalse(_matches([['right']],[['right',100]]))
 def test_empty_report_does_not_clear_existing_data(self):
  with self.assertRaisesRegex(ValueError,'empty'):build_plan(metadata(),{},'Aug-2026',[])
 def test_target_validation_stops_writes(self):
  service=Mock();api=service.spreadsheets.return_value
  api.get.return_value.execute.side_effect=[metadata(),{'sheets':[{'data':[{'rowData':[{'values':[{'dataValidation':{'strict':True}}]}]}]}]}]
  api.values.return_value.get.return_value.execute.return_value={'values':[]}
  with self.assertRaisesRegex(ValueError,'validation'):publish_report(service,'spreadsheet','Aug-2026',[result()])
  api.batchUpdate.assert_not_called()
if __name__=='__main__':unittest.main()
