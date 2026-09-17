import json,subprocess,urllib.request,time,re,sys
from concurrent.futures import ThreadPoolExecutor
P='project-fa311fc0-4d87-4781-986'
tok=subprocess.check_output(['gcloud','auth','print-access-token']).decode().strip()
H={'Authorization':'Bearer '+tok,'Content-Type':'application/json'}
d=json.load(open('pc/dashboard-2-before.json'))
D1,D2=sys.argv[1],sys.argv[2]; threads=int(sys.argv[3])
HEAVY=['V_DASH_KPI_DAILY','V_DASH_FINANCE_CORRECTED_DAILY','V_DASH_EXECUTIVE_ECONOMICS_DAILY','V_DASH_SETTLEMENT_DAILY','V_DASH_EXECUTIVE_BREAKDOWN_DAILY','V_DASH_BUYOUT_COHORT_DAILY','V_DASH_FRESHNESS_HEADER']
jobs=[]
for dc in sorted(d['dashcards'],key=lambda x:(x['row'],x['col'])):
    if not dc.get('card_id'): continue
    c=json.load(open(f"pc/cards/card-{dc['card_id']}.json"))
    sql=c['dataset_query']['stages'][0]['native'].replace('{{day}}',f"day BETWEEN '{D1}' AND '{D2}'")
    title=dc['visualization_settings'].get('card.title') or c['name']
    jobs.append((dc['id'],c['id'],title,sql))
def run(j):
    dcid,cid,title,sql=j
    body={"configuration":{"query":{"query":sql,"useLegacySql":False,"useQueryCache":False}},"jobReference":{"projectId":P,"location":"EU"}}
    t0=time.time()
    r=json.load(urllib.request.urlopen(urllib.request.Request(f'https://www.googleapis.com/bigquery/v2/projects/{P}/jobs',json.dumps(body).encode(),H)))
    jid=r['jobReference']['jobId']
    while True:
        g=json.load(urllib.request.urlopen(urllib.request.Request(f'https://www.googleapis.com/bigquery/v2/projects/{P}/jobs/{jid}?location=EU',headers=H)))
        if g['status']['state']=='DONE': break
        time.sleep(0.5)
    wall=time.time()-t0
    st=g['statistics']; q=st.get('query',{})
    refs=sorted({t['datasetId']+'.'+t['tableId'] for t in q.get('referencedTables',[])})
    views=[v for v in HEAVY if v in sql]
    return dict(dashcard=dcid,card=cid,title=title,err=(g['status'].get('errorResult') or {}).get('message'),
        bytes=int(st.get('totalBytesProcessed',0)),slot_ms=int(q.get('totalSlotMs',0)),
        exec_s=round((int(st['endTime'])-int(st['startTime']))/1000,2),wall_s=round(wall,2),
        views=views,base_tables=len(refs),refs=refs,stages=len(q.get('queryPlan',[])))
T0=time.time()
with ThreadPoolExecutor(threads) as ex: res=list(ex.map(run,jobs))
total=time.time()-T0
json.dump(dict(total_wall_s=total,threads=threads,results=res),open(f'pc/perf_{threads}.json','w'),ensure_ascii=False,indent=1)
for r in res: print(f"{r['dashcard']:>4} {r['card']:>4} {r['title'][:38]:38} {r['bytes']/1e6:8.1f}MB {r['exec_s']:6.2f}s slot {r['slot_ms']/1000:7.1f}s tables {r['base_tables']:2} stages {r['stages']:3} {','.join(v.replace('V_DASH_','') for v in r['views'])} {r['err'] or ''}")
print('TOTAL wall',round(total,1),'s; sum exec',round(sum(r['exec_s'] for r in res),1),'s; sum bytes',round(sum(r['bytes'] for r in res)/1e9,2),'GB; sum slot',round(sum(r['slot_ms'] for r in res)/1000,1),'s')
