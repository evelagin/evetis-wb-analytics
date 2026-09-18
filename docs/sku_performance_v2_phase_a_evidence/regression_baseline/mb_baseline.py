#!/usr/bin/env python3
"""Regression baseline dashboard 3: every card x period (x SKU) via `mb card query`, formatted (as rendered) and raw.
Read-only. Output: baseline/<scope>/<period>/card-<id>.{fmt,raw}.csv + baseline/timings.json"""
import json, os, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor
CARDS = [71, 40, 41, 56, 57, 58, 59, 60, 61, 62, 63, 67, 68, 64, 65, 70, 66, 69]
PERIODS = {'full_history': '2024-09-05~2026-09-18', 'past30days': 'past30days', 'past7days': 'past7days',
           'final_2026-08-17_08-30': '2026-08-17~2026-08-30', 'incomplete_2026-09-01_09-18': '2026-09-01~2026-09-18'}
SKUS = {'high_KremRuki_252442517': 'Крем Руки', 'medium_SyvAKNE_305101361': 'Сыворотка АКНЕ',
        'low_noads_KremVishnya_593111986': 'Крем Вишня', 'bundle_noads_Nabor_567668635': 'Набор сыв+крем АКНЕ'}
SKU_PERIODS = ['past30days', 'final_2026-08-17_08-30']
tags = {}
for c in CARDS:
    d = json.loads(subprocess.check_output(['mb', 'card', 'get', str(c), '--full', '--json', '--max-bytes=0', '-p', 'evetis-dev']))
    tt = d['dataset_query']['stages'][0].get('template-tags') or []
    tt = tt if isinstance(tt, list) else list(tt.values())
    tags[c] = {t['name']: t['id'] for t in tt}
def run(job):
    scope, pname, c, fmt = job
    params = []
    if 'day' in tags[c]:
        params.append({"id": tags[c]['day'], "type": "date/all-options", "value": PERIODS[pname], "target": ["dimension", ["template-tag", "day"]]})
    if scope != 'all' and 'sku' in tags[c]:
        params.append({"id": tags[c]['sku'], "type": "string/=", "value": [SKUS[scope]], "target": ["dimension", ["template-tag", "sku"]]})
    if scope != 'all' and 'sku' not in tags[c]:
        return (job, None, 0)   # card ignores SKU filter on the dashboard — not part of SKU baseline
    args = ['mb', 'card', 'query', str(c), '--export-format', 'csv', '-p', 'evetis-dev', '--max-bytes=0'] + (['--format-rows'] if fmt else [])
    if params: args += ['--parameters', json.dumps(params, ensure_ascii=False)]
    t0 = time.time(); out = subprocess.run(args, capture_output=True, text=True); dt = time.time() - t0
    return (job, out.stdout if out.returncode == 0 else 'ERROR ' + out.stdout[:300] + out.stderr[:300], dt)
jobs = [('all', p, c, f) for p in PERIODS for c in CARDS for f in (True, False)]
jobs += [(s, p, c, f) for s in SKUS for p in SKU_PERIODS for c in CARDS for f in (True, False)]
timings = {}
# 1) timed dashboard-like pass: default period, 18 cards, concurrency 5 (Metabase frontend limit)
t0 = time.time()
with ThreadPoolExecutor(5) as ex:
    res = list(ex.map(run, [('all', 'past30days', c, True) for c in CARDS]))
timings['dashboard_like_pass'] = {'wall_s': round(time.time() - t0, 2), 'per_card_s': {str(j[2]): round(dt, 2) for j, _, dt in res}}
with ThreadPoolExecutor(6) as ex:
    for (scope, pname, c, fmt), txt, dt in ex.map(run, jobs):
        if txt is None: continue
        d = f'baseline/{scope}/{pname}'; os.makedirs(d, exist_ok=True)
        open(f'{d}/card-{c}.{"fmt" if fmt else "raw"}.csv', 'w').write(txt)
json.dump({'run_date': time.strftime('%Y-%m-%d %H:%M %z'), 'periods': PERIODS, 'skus': SKUS, **timings}, open('baseline/timings.json', 'w'), ensure_ascii=False, indent=1)
errs = [f for r, _, fs in os.walk('baseline') for f in fs if open(os.path.join(r, f)).read().startswith('ERROR')]
print('files', sum(len(fs) for _, _, fs in os.walk('baseline')), 'errors', errs[:5]); print(json.dumps(timings, indent=1))
