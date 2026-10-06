"""Offline isolation/failure tests; no actual credentials or network calls."""
from datetime import datetime, timezone
import pytest
from tools.tenancy import cloud_access as C, tenant_backfill as BF

@pytest.fixture
def c(): return BF.target('client_001')

class Tokens:
    def __init__(self): self.calls=[]
    def token(self, kind): self.calls.append(kind); return 'SYNTHETIC_NON_CREDENTIAL'

@pytest.fixture
def access(c):
    calls=[];tokens=Tokens()
    client=C.CloudAccess(c,tokens,lambda *args: calls.append(args) or {})
    return client,calls,tokens

def body(query):
    return {'query':query,'useLegacySql':False,'location':'EU','maximumBytesBilled':'1073741824','timeoutMs':10000}

def test_existing_coordinator_queries_use_cloud_reader(access,c):
    client,calls,tokens=access
    client.request('POST',f'{C.TT.BQ}/projects/{c["project_id"]}/queries',body(f'SELECT DISTINCT evidence_json FROM `{c["project_id"]}.tenant_ops.BACKFILL_CHECKPOINTS` WHERE plan_hash = @root LIMIT 2'))
    assert len(calls)==1 and tokens.calls==['reader']

@pytest.mark.parametrize('query',[
    'DELETE FROM `mpa-t-client-001.tenant_ops.BACKFILL_CHECKPOINTS` WHERE TRUE',
    'SELECT * FROM `mpa-t-client-001.ozon_raw.RAW_OZON_CATALOG`; DELETE FROM `mpa-t-client-001.ozon_raw.RAW_OZON_CATALOG` WHERE TRUE',
    'SELECT * FROM `legacy-project.ozon_raw.RAW_OZON_CATALOG`',
    'SELECT * FROM `mpa-t-client-001.ozon_raw.RAW_OZON_*`',
    'SELECT * FROM `mpa-t-client-001.secrets.CREDENTIALS`',
    'SELECT * FROM UNNEST([1,2])',
    'SELECT EXTERNAL_QUERY("connection", "DELETE business") FROM `mpa-t-client-001.ozon_raw.RAW_OZON_CATALOG`',
    'SELECT `mpa-t-client-001.ozon_raw.remote_function`(payload) FROM `mpa-t-client-001.ozon_raw.RAW_OZON_CATALOG`',
    'SELECT * FROM RAW_OZON_CATALOG',
])
def test_query_mutation_or_foreign_or_indirect_source_denied_without_token(access,c,query):
    client,calls,tokens=access
    with pytest.raises(C.TT.TableError):client.request('POST',f'{C.TT.BQ}/projects/{c["project_id"]}/queries',body(query))
    assert not calls and not tokens.calls

@pytest.mark.parametrize('change',[{'location':'US'},{'useLegacySql':True},{'maximumBytesBilled':'2000000000'},{'destinationTable':{'projectId':'mpa-t-client-001'}}])
def test_query_envelope_drift_denied(access,c,change):
    client,calls,tokens=access
    with pytest.raises(C.TT.TableError):client.request('POST',f'{C.TT.BQ}/projects/{c["project_id"]}/queries',dict(body('SELECT * FROM `mpa-t-client-001.ozon_raw.RAW_OZON_CATALOG`'),**change))
    assert not calls and not tokens.calls

@pytest.mark.parametrize('method,url',[
    ('GET','https://secretmanager.googleapis.com/v1/projects/mpa-t-client-001/secrets/seller/versions/latest:access'),
    ('GET','https://api-seller.ozon.ru/v1/seller/info'),
    ('GET','https://www.googleapis.com/bigquery/v2/projects/legacy-project/datasets/ozon_raw/tables/RAW_OZON_CATALOG/data'),
    ('DELETE','https://run.googleapis.com/v2/projects/mpa-t-client-001/locations/europe-west1/jobs/ozon-runtime-daily'),
    ('POST','https://run.googleapis.com/v2/projects/mpa-t-client-001/locations/europe-west1/jobs/ozon-runtime-daily:run'),
    ('GET','https://run.googleapis.com/v2/projects/mpa-t-client-001/locations/europe-west1/jobs/ozon-dev-trial'),
    ('GET','https://run.googleapis.com/v2/projects/mpa-t-client-001/locations/us-central1/jobs'),
    ('GET','https://www.googleapis.com@attacker.example/bigquery/v2/projects/mpa-t-client-001/datasets/ref/tables/SELLER_BINDING/data'),
    ('GET','http://www.googleapis.com/bigquery/v2/projects/mpa-t-client-001/datasets/ref/tables/SELLER_BINDING/data'),
    ('GET','https://www.googleapis.com/bigquery/v2/projects/mpa-t-client-001/datasets/ref/tables/%2e%2e/data'),
])
def test_unapproved_cloud_routes_denied_before_auth(access,method,url):
    client,calls,tokens=access
    with pytest.raises(C.TT.TableError):client.request(method,url)
    assert not calls and not tokens.calls

def test_exact_append_uses_only_writer_boundary(access):
    client,calls,tokens=access
    client.append_checkpoint({'evidence_json':'synthetic proof'})
    assert tokens.calls==['append'] and len(calls)==1
    assert calls[0][1].endswith('/BACKFILL_CHECKPOINTS/insertAll')

@pytest.mark.parametrize('ds,table',[('ozon_raw','RAW_OZON_CATALOG'),('ref','SELLER_BINDING'),('tenant_ops','CAPABILITY_PROFILE')])
def test_writer_cannot_write_raw_binding_or_capability(access,ds,table):
    client,calls,tokens=access
    with pytest.raises(C.TT.TableError):client.append_request('POST',f'{C.TT.BQ}/projects/mpa-t-client-001/datasets/{ds}/tables/{table}/insertAll',{'rows':[{'json':{}}],'skipInvalidRows':False,'ignoreUnknownValues':False})
    assert not calls and not tokens.calls

def test_reader_cannot_append_and_writer_cannot_query(access,c):
    client,calls,tokens=access
    with pytest.raises(C.TT.TableError):client.request('POST',f'{C.TT.BQ}/projects/{c["project_id"]}/datasets/tenant_ops/tables/BACKFILL_CHECKPOINTS/insertAll',{})
    with pytest.raises(C.TT.TableError):client.append_request('POST',f'{C.TT.BQ}/projects/{c["project_id"]}/queries',body('SELECT * FROM `mpa-t-client-001.ozon_raw.RAW_OZON_CATALOG`'))
    assert not calls and not tokens.calls

def test_cloud_identity_mismatch_stops_before_requesting_token(c):
    calls=[]
    auth=C.CloudTokens(c,lambda *a,**kw:calls.append((a,kw)) or 'sa-ozon-runtime@mpa-t-client-001.iam.gserviceaccount.com')
    with pytest.raises(C.TT.TableError,match='identity'):auth.token('reader')
    assert len(calls)==1 and not auth.cache

def test_delegate_is_exact_dedicated_writer_short_lived_and_bq_only(c):
    now=datetime(2026,10,6,8,tzinfo=timezone.utc).timestamp();calls=[]
    def send(method,url,body=None,headers=None):
        calls.append((method,url,body,headers))
        if url.endswith('/email'):return 'sa-backfill-controller@mpa-t-client-001.iam.gserviceaccount.com'
        if url.endswith('/token'):return {'access_token':'SYNTHETIC_READER','expires_in':3600}
        return {'accessToken':'SYNTHETIC_WRITER','expireTime':'2026-10-06T08:10:00Z'}
    auth=C.CloudTokens(c,send,lambda:now)
    assert auth.token('append')=='SYNTHETIC_WRITER'
    assert calls[-1][1].endswith('/sa-backfill-append@mpa-t-client-001.iam.gserviceaccount.com:generateAccessToken')
    assert calls[-1][2]=={'scope':[C.BQ_SCOPE],'lifetime':'600s'}
    assert auth.token('append')=='SYNTHETIC_WRITER' and len(calls)==3

def test_wire_denies_redirect_without_following_or_disclosing_tokens():
    with pytest.raises(C.TT.TableError,match='redirect'):
        C.NoRedirect().redirect_request(None,None,302,'private server diagnostic',{},'https://attacker.example')

def test_cloud_records_survive_restart_without_owner_gcloud_or_local_receipt(c,monkeypatch):
    import copy
    from tools.tenancy import durable_plan as D
    monkeypatch.setattr(C.TT,'_req',lambda *a:pytest.fail('owner authentication fallback'))
    objects={};rows=[];tokens=Tokens()
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
            if name in objects:raise C.TT.Conflict('HTTP 409')
            objects[name]=copy.deepcopy(body);return body
        if method=='GET' and path.endswith('/tables'):
            return {'tables':list(objects.values())}
        if method=='GET':
            name=path.rsplit('/',1)[-1]
            if name not in objects:raise C.TT.TableError('HTTP 404')
            return copy.deepcopy(objects[name])
        pytest.fail('unreviewed transport operation')
    first=C.CloudAccess(c,tokens,send).durable_records()
    now=datetime(2026,10,6,8,tzinfo=timezone.utc);root='1'*64
    h=first.commit(root,'DISPATCH_INTENT',1,{'run_id':'synthetic-run'},now)
    restored=C.CloudAccess(c,tokens,send).durable_records()
    assert restored.read(root,h)['payload']=={'run_id':'synthetic-run'}
    assert len(restored.history(root))==1
    assert set(tokens.calls)=={'reader','append'}
    with pytest.raises(D.BF.B.EvidenceError,match='concurrent durable sequence'):
        restored.commit(root,'DISPATCH_INTENT',1,{'run_id':'different-run'},now)
    assert len(rows)==1
