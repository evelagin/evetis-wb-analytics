"""Run INSIDE the built controller image with network disabled, no credentials.

Checks packaged source provenance and concrete reader/append durable transport
across reconstructed controller objects. It proves offline artifact properties,
never actual IAM, source completeness or live unattended deployment.
"""
import copy
import json
import re
import sys
from datetime import datetime,date,timedelta,timezone
from tools.tenancy import cloud_access as A,durable_plan as D,cloud_tick as T
from tools.tenancy import tenant_backfill as BF,orchestration_contract as O,cloud_controller as C
from tools.tenancy.validation import parse_tenant_json


def check_quota_query(c, doc):
    """Exercise concrete Backend -> serialized EU REST SELECT -> typed rows.

    Stub BigQuery accepts only the exact non-reserved output schema. This catches
    alias/row-mapping drift hidden by a mock of Backend.quota itself. No source or
    append operation is allowed. The same check runs in the immutable image.
    """
    import sqlglot
    from sqlglot import exp
    now=datetime(2026,10,6,8,tzinfo=timezone.utc)
    calls=[]
    class Tokens:
        def token(self,kind):
            assert kind=='reader'
            return 'SYNTHETIC_NON_CREDENTIAL'
    def send(method,url,body=None,headers=None):
        assert method=='POST' and url==f"{BF.TT.BQ}/projects/{c['project_id']}/queries"
        assert body['useLegacySql'] is False and body['location']=='EU'
        assert body['maximumBytesBilled']=='1073741824'
        assert body['queryParameters']==[{'name':'since','parameterType':{'type':'TIMESTAMP'},'parameterValue':{'value':(now-timedelta(hours=24)).isoformat()}}]
        tree=sqlglot.parse_one(body['query'],read='bigquery')
        assert isinstance(tree,exp.Select)
        assert {t.catalog for t in tree.find_all(exp.Table)}=={c['project_id']}
        assert {t.db for t in tree.find_all(exp.Table)}=={c['datasets']['ozon_raw']}
        assert {t.name for t in tree.find_all(exp.Table)}=={'OZON_INGESTION_RUNS'}
        sqlglot.parse_one(tree.sql(dialect='bigquery'),read='bigquery')
        calls.append(body['query'])
        if tree.named_selects==['plan_id','sequence','exports','reserved_at']:
            assert 'MAX(started_at) AS reserved_at' in body['query']
            fields=[('plan_id','STRING'),('sequence','INT64'),('exports','INT64'),('reserved_at','TIMESTAMP')]
            values=[['1'*64,'2','10','1.791225494492827E9'],['1'*64,'5','5','1791225494.492827']]
        else:
            assert tree.named_selects==['n'] and 'backfill_plan_id IS NULL' in body['query']
            fields=[('n','INT64')];values=[['0']]
        return {'jobReference':{'jobId':'synthetic'},'jobComplete':True,
                'schema':{'fields':[{'name':n,'type':t} for n,t in fields]},
                'rows':[{'f':[{'v':v} for v in row]} for row in values]}
    backend=C.Backend(A.CloudAccess(c,Tokens(),send),'synthetic-controller',lambda:now)
    backend.state=lambda _:BF.B.initial(doc['runtime_plan'])
    verdict=backend.quota({'plans':[doc]},now)
    assert verdict=={'status':'WAITING','allowance':0,'eligible_at':'2026-10-06T18:38:14.492827+00:00'}
    assert len(calls)==2
    return 'PASS'


def check_pre_source():
    # Installed pure contract: existing STOP/failed dispatch stays historical;
    # only its exact owner-verified recovery allows a new dispatch sequence.
    from tools.tenancy import pre_source_recovery as PR
    root='1'*64
    record=lambda kind,seq,payload:{'version':D.VERSION,'root_hash':root,'kind':kind,'sequence':seq,'payload':payload}
    stop=record('STOPPED',0,{'reason':'historical-failure'})
    proof={'dispatch_sequence':1,'root_hash':root,'stop_hashes':[D.digest(stop)]}
    rows=[record('DISPATCH_INTENT',1,{}),record('DISPATCH_RECEIPT',1,{}),stop,record(PR.KIND,1,proof)]
    assert D.decide_tick(rows,root,{'status':'ELIGIBLE'},verified_failures=[proof])['sequence']==2
    assert D.decide_tick(rows,root,{'status':'ELIGIBLE'})['action']=='STOPPED'
    rows.append(record('STOPPED',0,{'reason':'new-failure','controller_execution':'new'}))
    assert D.decide_tick(rows,root,{'status':'ELIGIBLE'},verified_failures=[proof])['action']=='STOPPED'
    return 'PASS'


def check(source_sha):
    assert sys.version_info[:2]==(3,12), 'production Python 3.12 required'
    assert re.fullmatch('[0-9a-f]{40}',source_sha)
    assert (BF.REPO/'CONTROLLER_SOURCE_SHA').read_text().strip()==source_sha
    BF.TT._req=lambda *a,**k:(_ for _ in ()).throw(RuntimeError('OWNER_AUTH_FALLBACK'))
    c=BF.target('client_001');now=datetime.now(timezone.utc);day=now.astimezone(BF.B.MSK).date()-timedelta(days=1)
    doc=BF.make_plan(c['tenant_id'],'ads_sku_daily',str(day),str(day),'offline-image-qualification',now.isoformat(),max_units=3)
    BF.validate_plan(doc,doc['ack_hash'])
    assert check_quota_query(c,doc)=='PASS'
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
    from tools.tenancy.full_image_check import check as full_check
    full=full_check(store(),C.Backend(client,'synthetic-full-controller',lambda:now),now,source_sha)
    from tools.tenancy.controller_stop_image_check import check as check_controller_stop
    from tools.tenancy.controller_dispatch_image_check import check as check_dispatch
    from tools.tenancy.full_leaf_image_check import check as check_full_leaf
    return {**check_full_leaf(),**full,'controller_dispatch_recovery':check_dispatch(),'controller_stop_recovery':check_controller_stop(),'python_version':sys.version.split()[0],'source_sha':source_sha,'controller_implementation_hash':O.implementation_hash(BF.REPO),
            'runtime_implementation_hash':BF.B.implementation_hash(),
            'pre_source_recovery':check_pre_source(),'quota_query_syntax':'PASS','offline_restart':'PASS','lost_post_no_repeat':'PASS','quota_wait_no_source':'PASS',
            'reader_append_separation':'PASS','tenant_isolation':'PASS','live_deployment':'UNPROVEN'}


if __name__=='__main__':
    print(json.dumps(check((BF.REPO/'CONTROLLER_SOURCE_SHA').read_text().strip()),sort_keys=True))
