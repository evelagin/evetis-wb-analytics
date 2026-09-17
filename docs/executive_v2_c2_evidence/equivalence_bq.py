#!/usr/bin/env python3
# Phase C2 эквивалентность: SQL карточки (канонические V_DASH_*) vs тот же SQL на V_DASH_EXECUTIVE_V2_DAILY.
# Сырые значения BigQuery (строки API), точное сравнение. Период подставляется вместо {{day}}.
# Запуск 17.09: карточки выгружены `mb card get <id> --full` в c2/cards_live/ (рабочий каталог вне репозитория).
import json,glob,re,subprocess,urllib.request,sys,time
from concurrent.futures import ThreadPoolExecutor
P='project-fa311fc0-4d87-4781-986'
CANON=r'wb_mart\.V_DASH_(KPI_DAILY|FINANCE_CORRECTED_DAILY|EXECUTIVE_ECONOMICS_DAILY|SETTLEMENT_DAILY|EXECUTIVE_BREAKDOWN_DAILY|BUYOUT_COHORT_DAILY)\b'
def to_layer(sql):
    new=re.sub(CANON,'wb_mart.V_DASH_EXECUTIVE_V2_DAILY',sql)
    assert not re.search(r'wb_mart\.V_DASH_(?!EXECUTIVE_V2_DAILY)',new), new
    return new
PERIODS={'P1_31.08-13.09':('2026-08-31','2026-09-13'),'P2_17-30.08':('2026-08-17','2026-08-30'),
 'P3_27.07-23.08':('2026-07-27','2026-08-23'),'P4_past30':('2026-08-18','2026-09-16'),
 'P5_01-16.09':('2026-09-01','2026-09-16'),'P6_01-17.09_today':('2026-09-01','2026-09-17')}
tok=subprocess.check_output(['gcloud','auth','print-access-token']).decode().strip()
def run(q):
    body=json.dumps({"query":q,"useLegacySql":False,"location":"EU","timeoutMs":200000,"maxResults":10000,"useQueryCache":False}).encode()
    r=urllib.request.Request(f'https://www.googleapis.com/bigquery/v2/projects/{P}/queries',data=body,headers={'Authorization':'Bearer '+tok,'Content-Type':'application/json'})
    for a in range(3):
        try:
            d=json.load(urllib.request.urlopen(r)); break
        except urllib.error.HTTPError as e:
            err=e.read().decode()[:500]
            if a==2: return {'error':err}
            time.sleep(5)
    assert d.get('jobComplete'), d
    return {'schema':[f['name'] for f in d['schema']['fields']],'rows':[[c['v'] for c in row['f']] for row in d.get('rows',[])],
            'bytes':int(d.get('totalBytesProcessed',0))}
def main():
    cards={}
    for f in glob.glob('c2/cards_live/card-*.json'):
        d=json.load(open(f)); cid=d['id']
        if cid==159: continue
        cards[cid]=d['dataset_query']['stages'][0]['native']
    jobs=[]
    for cid,sql in sorted(cards.items()):
        for pn,(a,b) in PERIODS.items():
            flt=f"day BETWEEN DATE '{a}' AND DATE '{b}'"
            assert '{{day}}' in sql, cid
            jobs.append((cid,pn,'old',sql.replace('{{day}}',flt)))
            jobs.append((cid,pn,'new',to_layer(sql).replace('{{day}}',flt)))
    with ThreadPoolExecutor(12) as ex:
        res=list(ex.map(lambda j:(j[0],j[1],j[2],run(j[3])),jobs))
    out={}
    for cid,pn,side,r in res: out.setdefault((cid,pn),{})[side]=r
    bad=0; tot={'old':0,'new':0}
    report=[]
    for (cid,pn),r in sorted(out.items()):
        for s in ('old','new'): tot[s]+=r[s].get('bytes',0)
        ok = 'error' not in r['old'] and 'error' not in r['new'] and r['old']['schema']==r['new']['schema'] and r['old']['rows']==r['new']['rows']
        if not ok: bad+=1
        report.append({'card':cid,'period':pn,'equal':ok,'old':r['old'],'new':r['new']})
    json.dump(report,open('c2/equiv_report.json','w'),ensure_ascii=False,indent=1)
    print(f"cards={len(cards)} periods={len(PERIODS)} pairs={len(out)} mismatches={bad}")
    print(f"bytes old={tot['old']/1e9:.2f} GB new={tot['new']/1e9:.3f} GB")
    for x in report:
        if not x['equal']: print('MISMATCH',x['card'],x['period'],json.dumps(x['old'],ensure_ascii=False)[:300],'||',json.dumps(x['new'],ensure_ascii=False)[:300])
main()
