"""Publish a monthly report and its Summary controls in one atomic Sheets batch."""
from datetime import datetime, timezone
from decimal import Decimal
import math

from overage_calculations import HEADERS

SUMMARY_HEADERS = ['Month', 'Customers Audited', 'Overages Found', 'Total Overage $',
                   'Run Date', 'Rows Needing Review']
READ_ROWS = 1000


def _cell(value):
    if value is None or value == '':
        return {}
    key = 'numberValue' if isinstance(value, (int, float)) else 'stringValue'
    return {'userEnteredValue': {key: value}}


def _update(sheet_id, row, col, rows, width):
    return {'updateCells': {
        'range': {'sheetId': sheet_id, 'startRowIndex': row,
                  'endRowIndex': row + len(rows), 'startColumnIndex': col,
                  'endColumnIndex': col + width},
        'rows': [{'values': [_cell(r[c] if c < len(r) else None)
                             for c in range(width)]} for r in rows],
        'fields': 'userEnteredValue',
    }}


def build_plan(metadata, existing, month, results, allow_replace=False, run_at=None):
    """Build requests without writes. Existing annotations keep their customer IDs."""
    if not results:
        raise ValueError('Refusing to publish an empty customer audit')
    properties = {s['properties']['title']: s['properties'] for s in metadata['sheets']}
    used_ids = {p['sheetId'] for p in properties.values()}
    requests = []
    for title in [month, 'Summary']:
        if title not in properties:
            sheet_id = next(i for i in range(len(used_ids) + 2) if i not in used_ids)
            used_ids.add(sheet_id)
            properties[title] = {'title': title, 'sheetId': sheet_id,
                                 'gridProperties': {'rowCount': READ_ROWS, 'columnCount': 26}}
            requests.append({'addSheet': {'properties': properties[title]}})
    before = existing.get(month, [])
    if before and not allow_replace:
        raise ValueError(f'{month} already contains data. Use --replace-existing only for an intentional '
                         'full-month regeneration; historical billing decisions will be recalculated.')
    if len(results) + 1 > READ_ROWS:
        raise ValueError('Report exceeds the bounded publication range')
    by_id = {r['customer_id']: r for r in results}
    if len(by_id) != len(results):
        raise ValueError('Duplicate customer in report results')
    ordered = sorted(results, key=lambda r: (r['overage'] is not None, r['overage'] or 0), reverse=True)
    # Existing rows may have customer annotations beyond the managed columns.
    # Keep their order even when those annotations are not in the read rectangle.
    if before:
        old_ids = []
        for row in before[1:]:
            if not any(v not in (None, '') for v in row):
                continue
            if len(row) < 2 or not row[1] or row[1] not in by_id:
                raise ValueError('Cannot detach manually maintained annotations from their customer')
            old_ids.append(row[1])
        if len(old_ids) != len(set(old_ids)):
            raise ValueError('Duplicate annotated customer row')
        # Reject internal blank rows instead of compacting and shifting annotations.
        if len(old_ids) != len(before) - 1:
            raise ValueError('Existing report has internal blank rows; review its row structure')
        ordered = [by_id[cid] for cid in old_ids]
        ordered += [r for r in results if r['customer_id'] not in set(old_ids)]
    rows = [HEADERS]
    for r in ordered:
        rows.append([r['customer_name'], r['customer_id'], r['commit_total'], r['used_credits'],
                     r['remaining_balance'], r['invoice_total'], r['overage'],
                     f"{r['utilization_pct']:.1f}%" if r['utilization_pct'] is not None else '',
                     r['invoice_count'], r['audit_status']])
    monthly_id = properties[month]['sheetId']
    height = max(len(before), len(rows))
    requests.append(_update(monthly_id, 0, 0, rows + [[] for _ in range(height - len(rows))], 10))
    for col, width in [(2, 145), (3, 190), (4, 150), (7, 145)]:
        requests.append({'updateDimensionProperties': {
            'range': {'sheetId': monthly_id, 'dimension': 'COLUMNS', 'startIndex': col, 'endIndex': col + 1},
            'properties': {'pixelSize': width}, 'fields': 'pixelSize'}})
    requests.append({'repeatCell': {
        'range': {'sheetId': monthly_id, 'startRowIndex': 1, 'endRowIndex': len(rows),
                  'startColumnIndex': 2, 'endColumnIndex': 7},
        'cell': {'userEnteredFormat': {'numberFormat': {'type': 'NUMBER', 'pattern': '#,##0.00'}}},
        'fields': 'userEnteredFormat.numberFormat'}})
    requests.append({'repeatCell': {
        'range': {'sheetId': monthly_id, 'startRowIndex': 0, 'endRowIndex': 1,
                  'startColumnIndex': 0, 'endColumnIndex': 10},
        'cell': {'userEnteredFormat': {'wrapStrategy': 'WRAP'}},
        'fields': 'userEnteredFormat.wrapStrategy'}})
    requests.append({'updateDimensionProperties': {
        'range': {'sheetId': monthly_id, 'dimension': 'ROWS', 'startIndex': 0, 'endIndex': 1},
        'properties': {'pixelSize': 60}, 'fields': 'pixelSize'}})

    summary = existing.get('Summary', [])
    old_header = summary[0] if summary else []
    if len(old_header) > 5 and old_header[5] not in ('', SUMMARY_HEADERS[5]):
        raise ValueError('Summary column F already has a different purpose')
    if (len(old_header) <= 5 or not old_header[5]) and any(
            len(r) > 5 and r[5] not in ('', None) for r in summary[1:]):
        raise ValueError('Summary column F contains manually maintained data')
    matches = [i for i, row in enumerate(summary[1:], 1) if row and row[0] == month]
    if len(matches) > 1:
        raise ValueError('Duplicate month in Summary')
    summary_row = matches[0] if matches else max(1, len(summary))
    if summary_row >= READ_ROWS:
        raise ValueError('Summary exceeds bounded publication range')
    run_at = run_at or datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    count = sum(r['audit_status'] == 'YES' for r in results)
    total = sum((Decimal(str(r['overage'])) for r in results if r['overage'] is not None), Decimal(0))
    reviews = sum(r['audit_status'] == 'REVIEW' for r in results)
    summary_values = [month, len(results), count, None if reviews else float(round(total, 2)), run_at, reviews]
    summary_id = properties['Summary']['sheetId']
    if not summary:
        requests.append(_update(summary_id, 0, 0, [SUMMARY_HEADERS], 6))
    else:
        requests.append(_update(summary_id, 0, 5, [[SUMMARY_HEADERS[5]]], 1))
    requests.append(_update(summary_id, summary_row, 0, [summary_values], 6))
    requests += [
        {'updateDimensionProperties': {
            'range': {'sheetId': summary_id, 'dimension': 'COLUMNS', 'startIndex': 5, 'endIndex': 6},
            'properties': {'pixelSize': 165}, 'fields': 'pixelSize'}},
        {'repeatCell': {
            'range': {'sheetId': summary_id, 'startRowIndex': 0, 'endRowIndex': 1,
                      'startColumnIndex': 5, 'endColumnIndex': 6},
            'cell': {'userEnteredFormat': {'wrapStrategy': 'WRAP'}},
            'fields': 'userEnteredFormat.wrapStrategy'}},
        {'updateDimensionProperties': {
            'range': {'sheetId': summary_id, 'dimension': 'ROWS', 'startIndex': 0, 'endIndex': 1},
            'properties': {'pixelSize': 40}, 'fields': 'pixelSize'}},
    ]
    return {
        'requests': requests,
        'checks': [(f"'{month}'!A1:J{height}", rows + [[] for _ in range(height - len(rows))]),
                   (f"'Summary'!A{summary_row + 1}:F{summary_row + 1}", [summary_values]),
                   ("'Summary'!F1", [[SUMMARY_HEADERS[5]]])],
        'review_count': reviews,
    }


def _matches(actual, expected):
    for ri, expected_row in enumerate(expected):
        actual_row = actual[ri] if ri < len(actual) else []
        for ci, expected_value in enumerate(expected_row):
            observed = actual_row[ci] if ci < len(actual_row) else ''
            if expected_value in ('', None):
                if observed not in ('', None):
                    return False
            elif isinstance(expected_value, (float, int)):
                if not isinstance(observed, (float, int)) or not math.isclose(
                        observed, expected_value, rel_tol=0, abs_tol=0.0000001):
                    return False
            elif observed != expected_value:
                return False
        if any(v not in ('', None) for v in actual_row[len(expected_row):]):
            return False
    return not any(any(v not in ('', None) for v in row) for row in actual[len(expected):])


def publish_report(service, spreadsheet_id, month, results, allow_replace=False):
    """Read, guard, atomically publish, and verify without notes or source mutations."""
    sheets = service.spreadsheets()
    metadata = sheets.get(spreadsheetId=spreadsheet_id,
                          fields='spreadsheetId,sheets.properties').execute()
    properties = {s['properties']['title']: s['properties'] for s in metadata['sheets']}
    existing = {}
    for title in [month, 'Summary']:
        if title in properties:
            response = sheets.values().get(spreadsheetId=spreadsheet_id,
                                           range=f"'{title}'!A1:Z{READ_ROWS}",
                                           valueRenderOption='UNFORMATTED_VALUE').execute()
            existing[title] = response.get('values', [])
    plan = build_plan(metadata, existing, month, results, allow_replace=allow_replace)
    native_ranges = [address for address, _ in plan['checks']
                     if address.split('!')[0].strip("'") in properties]
    if native_ranges:
        native = sheets.get(spreadsheetId=spreadsheet_id, ranges=native_ranges,
                            includeGridData=True,
                            fields='sheets(data(rowData(values(dataValidation,chipRuns))))').execute()
        for tab in native.get('sheets', []):
            for block in tab.get('data', []):
                for row in block.get('rowData', []):
                    for cell in row.get('values', []):
                        if cell.get('dataValidation') or cell.get('chipRuns'):
                            raise ValueError('Publication target has validation or smart chips; review constraints first')
    # Recheck the visible data before writing so edits during preparation stop us.
    for title, values in existing.items():
        fresh = sheets.values().get(spreadsheetId=spreadsheet_id,
                                    range=f"'{title}'!A1:Z{READ_ROWS}",
                                    valueRenderOption='UNFORMATTED_VALUE').execute()
        if fresh.get('values', []) != values:
            raise ValueError('Spreadsheet changed during publication preparation')
    sheets.batchUpdate(spreadsheetId=spreadsheet_id,
                       body={'requests': plan['requests']}).execute()
    verified = sheets.values().batchGet(spreadsheetId=spreadsheet_id,
                                        ranges=[c[0] for c in plan['checks']],
                                        valueRenderOption='UNFORMATTED_VALUE').execute()
    blocks = verified.get('valueRanges', [])
    if len(blocks) != len(plan['checks']):
        raise ValueError('Publication verification returned incomplete ranges')
    for block, (address, expected) in zip(blocks, plan['checks']):
        if not _matches(block.get('values', []), expected):
            raise ValueError('Publication readback differs at ' + address)
    print(f"Published and verified {month}: {len(results)} customers; "
          f"{plan['review_count']} row(s) require review")
