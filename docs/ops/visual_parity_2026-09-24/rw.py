# Write helper: ONLY the TEST workbook may be mutated. The single exception is the
# non-mutating sheets.copyTo endpoint on PROD (reads PROD, writes the copy into TEST).
import json, urllib.request, urllib.parse, time, ro
def _post(url, body):
    for i in range(6):
        req=urllib.request.Request(url, method='POST', data=json.dumps(body).encode(),
            headers={'Authorization':'Bearer '+ro.token('rw'),'Content-Type':'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=600) as r: return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code==429: time.sleep(20*(i+1)); continue
            raise RuntimeError(e.read().decode()[:3000])
def batch(requests):
    return _post(f'https://sheets.googleapis.com/v4/spreadsheets/{ro.TEST}:batchUpdate', {'requests':requests})
def copy_prod_sheet_to_test(prod_sheet_id):
    return _post(f'https://sheets.googleapis.com/v4/spreadsheets/{ro.PROD}/sheets/{prod_sheet_id}:copyTo', {'destinationSpreadsheetId': ro.TEST})
