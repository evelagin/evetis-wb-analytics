import json, subprocess, urllib.request, urllib.parse, sys, time
SA='sa-unitka-sheet-rehearsal@project-fa311fc0-4d87-4781-986.iam.gserviceaccount.com'
PROD='1E4L4JuwfEqr9owhsGkAjb8F24lRpWpWkEyVmSuRxaJg'
TEST='1HTgd__BMvvuDsS02HdiGtkpyrzWqeX5pp9TrN6k8ftY'
_tok={}
def token(scope='readonly'):
    if scope not in _tok:
        sc='https://www.googleapis.com/auth/spreadsheets'+('.readonly' if scope=='readonly' else '')
        _tok[scope]=subprocess.check_output(['gcloud','auth','print-access-token','--impersonate-service-account='+SA,'--scopes='+sc],stderr=subprocess.DEVNULL).decode().strip()
    return _tok[scope]
def get(sid, path='', **params):
    q=urllib.parse.urlencode(params, doseq=True)
    url=f'https://sheets.googleapis.com/v4/spreadsheets/{sid}'+urllib.parse.quote(path,safe='/:!')+('?'+q if q else '')
    for i in range(6):
        req=urllib.request.Request(url, method='GET', headers={'Authorization':'Bearer '+token('readonly')})
        try:
            with urllib.request.urlopen(req, timeout=300) as r: return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code==429: time.sleep(20*(i+1)); continue
            raise RuntimeError(e.read().decode()[:2000])
