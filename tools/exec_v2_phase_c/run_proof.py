import json,subprocess,urllib.request,time
P='project-fa311fc0-4d87-4781-986'
tok=subprocess.check_output(['gcloud','auth','print-access-token']).decode().strip()
H={'Authorization':'Bearer '+tok,'Content-Type':'application/json'}
q=open('pc/proof.sql').read()
body={"configuration":{"query":{"query":q,"useLegacySql":False,"useQueryCache":False}},"jobReference":{"projectId":P,"location":"EU"}}
j=json.load(urllib.request.urlopen(urllib.request.Request(f'https://www.googleapis.com/bigquery/v2/projects/{P}/jobs',json.dumps(body).encode(),H)))
jid=j['jobReference']['jobId']; print('job',jid,flush=True)
while True:
    g=json.load(urllib.request.urlopen(urllib.request.Request(f'https://www.googleapis.com/bigquery/v2/projects/{P}/jobs/{jid}?location=EU',headers=H)))
    if g['status']['state']=='DONE': break
    time.sleep(10)
print('status',g['status'].get('errorResult'),flush=True)
kids=[]; tokp=None
while True:
    url=f'https://www.googleapis.com/bigquery/v2/projects/{P}/jobs?parentJobId={jid}&maxResults=500&projection=full'+(f'&pageToken={tokp}' if tokp else '')
    r=json.load(urllib.request.urlopen(urllib.request.Request(url,headers=H)))
    kids+=r.get('jobs',[]); tokp=r.get('nextPageToken')
    if not tokp: break
rows=[]
for k in kids:
    st=k['statistics']; qq=st.get('query',{})
    rows.append(dict(start=int(st['startTime']),end=int(st['endTime']),bytes=int(qq.get('totalBytesProcessed',0) or 0),slot=int(qq.get('totalSlotMs',0) or 0),type=qq.get('statementType'),sql=k['configuration']['query']['query'][:160]))
rows.sort(key=lambda r:r['start'])
json.dump(rows,open('pc/proof_children.json','w'),ensure_ascii=False,indent=1)
res=json.load(urllib.request.urlopen(urllib.request.Request(f'https://www.googleapis.com/bigquery/v2/projects/{P}/queries/{jid}?location=EU&maxResults=1000',headers=H)))
out=[[c['v'] for c in row['f']] for row in res.get('rows',[])]
json.dump(out,open('pc/proof_result.json','w'),ensure_ascii=False,indent=1)
print('compared pairs',len(out),'unequal',sum(1 for o in out if o[2]!='true'),flush=True)
