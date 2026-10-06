"""Run INSIDE the built controller image with network disabled, no credentials.

Checks packaged source provenance and concrete reader/append durable transport
across reconstructed controller objects. It proves offline artifact properties,
never actual IAM, source completeness or live unattended deployment.
"""
import copy
import json
import re
from datetime import datetime,date,timedelta,timezone
from tools.tenancy import cloud_access as A,durable_plan as D,cloud_tick as T
from tools.tenancy import tenant_backfill as BF,orchestration_contract as O
from tools.tenancy.validation import parse_tenant_json


def check(source_sha):
    assert re.fullmatch('[0-9a-f]{40}',source_sha)
    assert (BF.REPO/'CONTROLLER_SOURCE_SHA').read_text().strip()==source_sha
    BF.TT._req=lambda *a,**k:(_ for _ in ()).throw(RuntimeError('OWNER_AUTH_FALLBACK'))
    c=BF.target('client_001');now=datetime.now(timezone.utc);day=now.astimezone(BF.B.MSK).date()-timedelta(days=1)
    doc=BF.make_plan(c['tenant_id'],'ads_sku_daily',str(day),str(day),'offline-image-qualification',now.isoformat(),max_units=3)
    BF.validate_plan(doc,doc['ack_hash'])
    from tools.tenancy import platform as P
    releases=[parse_tenant_json(f.read_text()) for f in (BF.REPO/P.RUNTIME_RELEASES_DIR/'ozon').glob('*.json')]
    release=next(r for r in releases if r.get('image')==doc['image'])
    manifest=D.root_manifest(c['tenant_id'],release['source']['commit'],doc['image'],now.isoformat(),'QUALIFICATION',[doc]);root=manifest['hash']
    objects={};rows=[];calls=[];posts=[]
    class Tokens:
        def token(self,kind):calls.append(kind);return 'SYNTHETIC_NON_CREDENTIAL'
    def send(method,url,body=None,headers=None):
        path=url.split('?')[0]
        if path.endswith('/queries'):
            params={p['name']:p['parameterValue']['value'] for p in body['queryParameters']}
            values={r['evidence_json'] for r in rows if r['plan_hash']==params['root'] and ('record' not in params or r['backfill_id']==params['record'])}
            return {'jobReference':{'jobId':'synthetic'},'jobComplete':True,'schema':{'fields':[{'name':'evidence_json','type':'STRING'}]},'rows':[{'f':[{'v':v}]} for v in values]}
        if path.endswith('/insertAll'):
            rows.extend(copy.deepcopy(r['json']) for r in body['rows']);return {}
        if method=='POST' and path.endswith('/tables'):
            name=body['tableReference']['tableId']
            if name in objects:raise BF.TT.Conflict('HTTP 409')
            objects[name]=copy.deepcopy(body);return body
        if method=='GET' and path.endswith('/tables'):return {'tables':list(objects.values())}
        if method=='GET':
            name=path.rsplit('/',1)[-1]
            if name not in objects:raise BF.TT.TableError('HTTP 404')
            return copy.deepcopy(objects[name])
        raise AssertionError('unreviewed network route')
    def store():return A.CloudAccess(c,Tokens(),send).durable_records()
    first=store();first.commit(root,'MANIFEST',0,manifest,now)
    class Execution:
        quota_value={'status':'WAITING','eligible_at':(now+timedelta(hours=1)).isoformat()}
        def preflight(self,m):assert m==manifest
        def active_runtime_execution(self):return False
        def all_complete(self,m):return False
        def quota(self,m,n):return self.quota_value
        def next_plan(self,*a):return doc
        def recover_receipt(self,intent):return None
        def start(self,p,before,after):
            before({'run_id':'bf-synthetic-lost','lease_generation':1,'ack_hash':p['ack_hash']})
            posts.append('one simulated source dispatch')
            raise BF.B.EvidenceError('simulated lost POST response')
    engine=Execution()
    assert T.Tick(root,first,engine,lambda:now).run()['status']=='WAITING' and not posts
    restored=store()
    assert T.Tick(root,restored,engine,lambda:now).run()['status']=='WAITING' and not posts
    engine.quota_value={'status':'ELIGIBLE','allowance':15}
    try:T.Tick(root,restored,engine,lambda:now).run()
    except BF.B.EvidenceError:pass
    else:raise AssertionError('lost POST did not stop')
    assert len(posts)==1
    assert T.Tick(root,store(),engine,lambda:now).run()['status']=='STOPPED' and len(posts)==1
    client=A.CloudAccess(c,Tokens(),send)
    denied=0
    for method,url,body,authority in (
        ('POST',f"{BF.TT.BQ}/projects/{c['project_id']}/datasets/ozon_raw/tables/RAW_OZON_CATALOG/insertAll",{},'reader'),
        ('POST',f"{BF.TT.BQ}/projects/{c['project_id']}/queries",{'query':'DELETE business'},'append'),
        ('GET','https://secretmanager.googleapis.com/v1/projects/'+c['project_id']+'/secrets/any/versions/latest:access',None,'reader')):
        try:client._request(authority,method,url,body)
        except BF.TT.TableError:denied+=1
        else:raise AssertionError('authority boundary broadened')
    assert denied==3 and set(calls)=={'reader','append'}
    return {'source_sha':source_sha,'controller_implementation_hash':O.implementation_hash(BF.REPO),
            'runtime_implementation_hash':BF.B.implementation_hash(),
            'offline_restart':'PASS','lost_post_no_repeat':'PASS','quota_wait_no_source':'PASS',
            'reader_append_separation':'PASS','tenant_isolation':'PASS','live_deployment':'UNPROVEN'}


if __name__=='__main__':
    print(json.dumps(check((BF.REPO/'CONTROLLER_SOURCE_SHA').read_text().strip()),sort_keys=True))
