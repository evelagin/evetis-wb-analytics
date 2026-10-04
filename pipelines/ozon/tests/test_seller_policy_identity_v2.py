"""Owner-approved offline adversarial regression: policy, transport, binding, identity."""
import copy
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
import sys
import urllib.request

import pytest
import common as C
import credentials as CR
import identity as I
import main as M
import promo as P
import seller_policy as SP

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from tools.tenancy.seller_transport_scan import scan, violations

NOW = datetime(2026, 10, 4, tzinfo=timezone.utc)
CORE = ('synthetic-client', 'synthetic-inn')

@pytest.fixture(autouse=True)
def isolated_scope(monkeypatch):
    monkeypatch.setattr(C, '_seller_execution_scope', None)


def roles(*extra):
    return {'roles': [{'name': 'Admin read only', 'methods': sorted(C.SELLER_ALLOWED_PATHS | set(extra))}]}


@pytest.mark.parametrize('path', [
    '/v9/unknown', '/v1/cargoes/create', '/v1/notification/check',
    '/v1/supply-order/content/update/validation', '/v1/cargoes-label/file/*',
    '/v1/analytics/item_turnover'])
def test_blocked_capability(path):
    assert CR.evaluate_seller_roles(roles(path), ('catalog',), NOW)['status'] == 'FAIL'


@pytest.mark.parametrize('path', ['/v1/actions/hotsales/list', '/v1/cargoes-label/create'])
def test_inventory_warning_not_callable(path, monkeypatch):
    result = CR.evaluate_seller_roles(roles(path), ('catalog',), NOW)
    assert result['status'] == 'PASS' and any(path in w for w in result['warnings'])
    monkeypatch.setattr(C, '_seller_headers', lambda: pytest.fail('denied route read credentials'))
    with pytest.raises(C.ApiPathDenied):
        C.seller_post(path, {})


def test_review_inventory_counts_and_legacy_preserved():
    manifest = json.loads((C.POLICY_FILE if hasattr(C, 'POLICY_FILE') else CR.POLICY_FILE).with_name('seller_policy_reviews.json').read_text())
    records = manifest['reviews']
    assert sum(r['lifecycle'] == 'RETIRED' for r in records.values()) == 22
    assert sum(r['lifecycle'] == 'UNRESOLVED' and r['semantics'] == 'UNPROVEN' and 'alias_candidate' not in r for r in records.values()) == 28
    assert sum(r['semantics'] == 'BUSINESS_STATE_MUTATION' for r in records.values()) == 7
    assert sum(r['semantics'] == 'ARTIFACT_GENERATION' for r in records.values()) == 5
    assert sum(r['semantics'] == 'VALIDATION_OR_CHECK' for r in records.values()) == 1
    assert sum(r['semantics'] == 'EXTERNAL_SIDE_EFFECT' for r in records.values()) == 1
    p = CR.load_policy()
    assert len(p['methods']) == 532 and p['approved_mutation_methods'] == []
    for r in p['methods'].values():
        assert all(r[k] == r['legacy_record'][k] for k in ('class','http','deprecated','reason'))


@pytest.mark.parametrize('field,value', [('schema_version',3), ('schema_version',True), ('approved_mutation_methods',['/v1/cargoes/create']), ('api','other')])
def test_invalid_policy_top(field,value):
    p = copy.deepcopy(CR.load_policy()); p[field] = value
    with pytest.raises(ValueError): SP.validate(p)


@pytest.mark.parametrize('field,value', [('lifecycle','MAGIC'), ('semantics','READISH'), ('confidence','TRUST_ROLE'), ('side_effects','SAFE'), ('review',{}), ('legacy_record',None)])
def test_invalid_policy_record(field,value):
    p=copy.deepcopy(CR.load_policy());p['methods']['/v1/roles'][field]=value
    with pytest.raises(ValueError): SP.validate(p)


def test_duplicate_policy_keys_and_suggestions():
    with pytest.raises(ValueError): SP.parse('{"schema_version":2,"schema_version":1}')
    from tools.tenancy import ozon_method_policy as G
    assert G.classify('/v99/x/list','Получить список')[0] == 'READ'
    p=copy.deepcopy(CR.load_policy());r=p['methods']['/v1/roles'];r['confidence']='SUGGESTION'
    with pytest.raises(ValueError): SP.validate(p)


@pytest.mark.parametrize('url,method,profile', [
    ('http://api-seller.ozon.ru/v1/roles','POST','runtime'),
    ('https://other.invalid/v1/roles','POST','runtime'),
    ('https://api-seller.ozon.ru:443/v1/roles','POST','runtime'),
    ('https://api-seller.ozon.ru:8443/v1/roles','POST','runtime'),
    ('https://user@api-seller.ozon.ru/v1/roles','POST','runtime'),
    ('https://api-seller.ozon.ru/v1/roles?','POST','runtime'),
    ('https://api-seller.ozon.ru/v1/roles?a=1','POST','runtime'),
    ('https://api-seller.ozon.ru/v1/roles#x','POST','runtime'),
    ('https://api-seller.ozon.ru/v1/%72oles','POST','runtime'),
    ('https://api-seller.ozon.ru/v1/../v1/roles','POST','runtime'),
    ('https://api-seller.ozon.ru//v1/roles','POST','runtime'),
    ('https://api-seller.ozon.ru/v1/roles','GET','runtime'),
    ('https://api-seller.ozon.ru/v1/roles','POST','new-profile')])
def test_bad_route(url,method,profile,monkeypatch):
    monkeypatch.setattr(C,'_seller_headers',lambda: pytest.fail('credentials before denial'))
    monkeypatch.setattr(C,'_seller_open',lambda *a,**k: pytest.fail('network before denial'))
    with pytest.raises(C.ApiPathDenied): C._validate_seller_route(url,method,profile)
    if profile in C.SELLER_PROFILES:
        req=urllib.request.Request(url,method=method);req._seller_profile=profile
        with pytest.raises(C.ApiPathDenied): C._request(req)


def test_dispatch_revalidates_and_raw_headers_do_not_bypass(monkeypatch):
    monkeypatch.setattr(C,'_seller_open',lambda *a,**k: pytest.fail('network reached'))
    req=urllib.request.Request('https://other.invalid/v1/roles',headers={'Api-Key':'SYNTH'})
    with pytest.raises(C.ApiPathDenied): C._request(req)
    req=urllib.request.Request(C.SELLER+'/v1/roles',method='POST')
    with pytest.raises(C.ApiPathDenied): C._request(req)


@pytest.mark.parametrize('code',[301,302,303,307,308])
def test_redirect_denied(code):
    req=urllib.request.Request(C.SELLER+'/v1/roles',method='POST')
    with pytest.raises(C.ApiPathDenied):
        C._NoSellerRedirect().redirect_request(req,None,code,'redirect',{},'https://other.invalid/x')


def test_promo_uses_transport_and_tenant_cannot_opt_in(monkeypatch):
    calls=[]
    monkeypatch.setattr(C,'seller_call',lambda *a,**k: calls.append((a,k)) or (200,{}))
    assert P.promo_call(P.P_ACTIONS)==(200,{})
    assert calls[0][1]=={'method':'GET','profile':'promo'}


def test_tenant_promo_denied_before_credentials(monkeypatch):
    monkeypatch.setattr(C,'CONFIG',C.CONFIG._replace(project='mpa-t-client-001'))
    monkeypatch.setattr(C,'_seller_headers',lambda: pytest.fail('credentials reached'))
    with pytest.raises(C.ApiPathDenied): P.promo_call(P.P_ACTIONS)


@pytest.mark.parametrize('source',[
    'import urllib.request as u\nu.urlopen("x")',
    'from urllib.request import urlopen as send\nsend("x")',
    'import requests as r\nr.post("x")',
    'C._request(req)', 'from common import _seller_headers as h\nh()',
    'getattr(C,"_request")(req)', 'headers={"Api-Key":key}',
])
def test_ast_detects_new_sinks(source):
    assert violations(source,'new_entity.py')


def test_normal_runtime_ast_clean(): assert scan(ROOT/'pipelines/ozon/runtime')==[]


@pytest.mark.parametrize('flag',[None,'','0','true','2'])
def test_dev_trial_omission_rejected_before_data_writes(flag,monkeypatch):
    monkeypatch.setattr(C,'PROJECT','mpa-t-client-001')
    monkeypatch.setenv('ENTITIES','catalog');monkeypatch.delenv('TENANT_BINDING_REQUIRED',raising=False)
    if flag is not None:monkeypatch.setenv('TENANT_BINDING_REQUIRED',flag)
    monkeypatch.setattr(C,'bq',lambda:pytest.fail('data client before rejection'))
    monkeypatch.setattr(C,'_seller_headers',lambda:pytest.fail('credential before rejection'))
    with pytest.raises(SystemExit) as e:M.main()
    assert e.value.code==2


def test_unknown_entity_contract_and_legacy_compatibility(monkeypatch):
    monkeypatch.setattr(C,'PROJECT',C.LEGACY_INGESTION_PROJECT)
    assert M.ingestion_execution_contract(['catalog'],{}) is False
    with pytest.raises(C.ConfigError):M.ingestion_execution_contract(['invented'],{})
    monkeypatch.setattr(C,'PROJECT','mpa-t-client-001')
    assert M.ingestion_execution_contract(['catalog'],{'TENANT_BINDING_REQUIRED':'1'}) is True
    with pytest.raises(C.ConfigError):M.ingestion_execution_contract(['promo'],{'TENANT_BINDING_REQUIRED':'1'})


def test_v2_optional_ogrn_and_golden_v1():
    fp=I.seller_fingerprint(*CORE,'',version=I.SELLER_V2)
    assert fp==I.seller_fingerprint(*CORE,None,version=I.SELLER_V2)==I.seller_fingerprint(*CORE,'legal',version=I.SELLER_V2)
    assert fp.startswith(I.SELLER_V2+':sha256:')
    old=hashlib.sha256(b'{"api":"seller","client_id":"c","inn":"i","ogrn":"o"}').hexdigest()
    assert I.seller_fingerprint('c','i','o')==old
    with pytest.raises(I.IdentityError):I.seller_fingerprint(*CORE,'')
    assert I.seller_fingerprint(*CORE,'legal')!=fp


@pytest.mark.parametrize('bad',[None,'', ' ', 17, {}, [], True, 'x\n', 'a b'])
def test_v2_invalid_core(bad):
    with pytest.raises(I.IdentityError):I.seller_fingerprint(bad,CORE[1],'',version=I.SELLER_V2)
    with pytest.raises(I.IdentityError):I.seller_fingerprint(CORE[0],bad,'',version=I.SELLER_V2)


def test_unknown_version_and_legal_change():
    with pytest.raises(I.IdentityError):I.seller_fingerprint(*CORE,'',version='future-v3')
    fp=I.seller_fingerprint(*CORE,'legal',version=I.SELLER_V2)
    b=I.Binding('seller','CONFIRMED',fp,'b','o',NOW,'confirmed','legal')
    assert I.live_status(b,fp,'legal')[0]==I.BOUND
    assert I.live_status(b,fp,'')[0]==I.MISMATCH
    assert I.live_status(b,fp,'other')[0]==I.MISMATCH
    assert I.live_status(b,I.seller_fingerprint(*CORE,'legal'),'legal')[0]==I.MISMATCH
    assert I.live_status(b,'future:sha256:'+('a'*64))[0]==I.INVALID_BINDING


def test_owner_cannot_create_machine_observation_or_reinterpret_incomplete():
    from tools.tenancy import tenant_binding as B
    fp=I.seller_fingerprint(*CORE,'',version=I.SELLER_V2)
    o={'api':'seller','observation_id':'o','observed_at':NOW.isoformat(),'identity_fingerprint':fp,
       'seller_client_id':CORE[0],'company_inn':CORE[1],'company_ogrn':'','status':'INCOMPLETE'}
    before=copy.deepcopy(o)
    assert B.decide_confirm([o],[],I.SELLER,'o',fp,'NONE',NOW,'OPERATOR:synthetic')[0]=='REJECT'
    assert o==before
    o['status']='OBSERVED'
    st,_,row=B.decide_confirm([o],[],I.SELLER,'o',fp,'NONE',NOW,'OPERATOR:synthetic')
    assert st=='WRITE' and json.loads(row['notes'])['owner_confirmation'] is True
    labels,desc=I.binding_marker(row)
    assert I.binding_from_markers([(I.binding_marker_name('seller',1),labels,desc)],'seller',NOW,[o]).status=='CONFIRMED'

@pytest.mark.parametrize('path,record', list(json.loads(CR.POLICY_FILE.with_name('seller_policy_reviews.json').read_text())['reviews'].items()))
def test_each_reviewed_capability_has_exact_treatment_and_call_boundary(path,record,monkeypatch):
    p=CR.load_policy()['methods'][path]
    assert all(p[k]==record[k] for k in ('lifecycle','semantics','confidence','side_effects'))
    if ("POST",path) not in C.SELLER_PROFILES['runtime']:
        monkeypatch.setattr(C,'_seller_headers',lambda:pytest.fail('unreviewed callable credentials'))
        with pytest.raises(C.ApiPathDenied):C.seller_post(path,{})
    expected=('FAIL' if record['lifecycle']=='UNRESOLVED' or record['semantics'] in
              {'BUSINESS_STATE_MUTATION','EXTERNAL_SIDE_EFFECT','VALIDATION_OR_CHECK'} else
              'WARN' if record['lifecycle']=='RETIRED' or record['semantics']=='ARTIFACT_GENERATION' else 'PASS')
    assert SP.treatment(p,2)==expected


@pytest.mark.parametrize('code',[301,302,303,307,308])
def test_real_redirect_handler_rejects_before_second_dispatch(code):
    handler=C._NoSellerRedirect()
    import io
    import email.message
    response=io.BytesIO(b'')
    headers=email.message.Message();headers['Location']='https://other.invalid/x'
    req=urllib.request.Request(C.SELLER+'/v1/roles',method='POST')
    with pytest.raises(C.ApiPathDenied):
        getattr(handler,'http_error_'+str(code))(req,response,code,'redirect',headers)


@pytest.mark.parametrize('profile,method,path',[('runtime','POST','/v1/roles'),('control','POST','/v1/seller/info'),('promo','GET','/v1/actions')])
def test_approved_call_routes_to_reviewed_opener(profile,method,path,monkeypatch):
    import io
    monkeypatch.setattr(C,'_seller_headers',lambda:{'Api-Key':'SYNTHETIC_ONLY'})
    seen=[]
    class Response(io.BytesIO):
        status=200
    def send(req,timeout):
        seen.append((req.full_url,req.get_method(),req._seller_profile))
        return Response(b'{}')
    monkeypatch.setattr(C,'_seller_open',send)
    assert C.seller_call(path,None if method=='GET' else {},method=method,profile=profile)==(200,{})
    assert seen==[(C.SELLER+path,method,profile)]


@pytest.mark.parametrize('source',[
    'def new_sink():\n    return urllib.request.urlopen("https://api-seller.ozon.ru/x")',
    'def new_sink():\n    return _seller_headers()',
    'def new_sink():\n    return _request(req)',
    'import requests as r\ndef new_sink():\n    return r.post("x")',
])
def test_ast_does_not_exempt_new_sinks_in_common(source):
    assert violations(source,'common.py')


def test_execution_contract_legacy_project_is_canonical():
    from tools.tenancy import naming
    assert C.LEGACY_INGESTION_PROJECT==naming.LEGACY_EVETIS['gcp_project_id']


def test_v2_confirmation_performance_link_is_separate_and_required():
    from tools.tenancy import tenant_binding as B
    fp=I.seller_fingerprint(*CORE,'',version=I.SELLER_V2)
    seller={'api':'seller','binding_id':'synthetic-b','identity_fingerprint':fp,'status':'CONFIRMED',
            'confirmed_at':NOW.isoformat(),'confirmed_by':'OPERATOR:synthetic','source_observation_id':'s',
            'notes':json.dumps({'identity_version':I.SELLER_V2,'owner_confirmation':True,'legal_evidence':{'ogrn':''}})}
    labels,description=I.binding_marker(seller)
    items=[(I.binding_marker_name('seller',1),labels,description)]
    pfp=I.performance_fingerprint('synthetic-performance')
    obs={'api':'performance','observation_id':'p','observed_at':NOW.isoformat(),'status':'OBSERVED',
         'performance_client_id':'synthetic-performance','identity_fingerprint':pfp,'evidence_json':'[]'}
    assert B.decide_confirm([obs],items,I.PERFORMANCE,'p',pfp,'NONE',NOW,'OPERATOR:synthetic')[0]=='REJECT'
    obs['evidence_json']=json.dumps({'seller_binding_fingerprint':fp,'sku_evidence_coverage':'SAMPLED'})
    status,_,row=B.decide_confirm([obs],items,I.PERFORMANCE,'p',pfp,'NONE',NOW,'OPERATOR:synthetic')
    assert status=='WRITE'
    assert I.binding_evidence(row)[1]==fp
    row['notes']=json.dumps({'binding_protocol':'future'})
    with pytest.raises(I.IdentityError):I.binding_evidence(row)


def test_normal_v2_binding_gate_log_contains_no_identity_material(monkeypatch,capsys):
    monkeypatch.setattr(C,'PROJECT','mpa-t-client-001')
    monkeypatch.setenv('ENTITIES','catalog');monkeypatch.setenv('TENANT_BINDING_REQUIRED','1')
    monkeypatch.setattr(M,'binding_gate',lambda *a:('seller:MISMATCH','LEGAL_EVIDENCE_CHANGED'))
    with pytest.raises(SystemExit) as e:M.main()
    assert e.value.code==3
    log=capsys.readouterr().out
    assert 'LEGAL_EVIDENCE_CHANGED' in log
    for value in (*CORE, I.seller_fingerprint(*CORE,'',version=I.SELLER_V2)):
        assert value not in log


def test_identity_log_boundary_does_not_destroy_stored_evidence(capsys):
    fp=I.seller_fingerprint(*CORE,'',version=I.SELLER_V2)
    payload={'identity_fingerprint':fp,'nested':{'seller_binding_fingerprint':fp,'company_inn':CORE[1]},'reason':'identity='+fp}
    C.log(event='synthetic_identity',**payload)
    out=capsys.readouterr().out
    assert fp not in out and CORE[1] not in out
    assert payload['identity_fingerprint']==fp
    assert C.redact_value(payload)['identity_fingerprint']==fp
    assert fp not in C.safe_error_text('synthetic error '+fp)


def test_unredirected_headers_do_not_bypass_dispatch(monkeypatch):
    monkeypatch.setattr(C,'_seller_open',lambda *a,**k:pytest.fail('Seller socket reached'))
    monkeypatch.setattr(C.urllib.request,'urlopen',lambda *a,**k:pytest.fail('generic socket reached'))
    req=urllib.request.Request('https://other.invalid/x',method='POST')
    req.add_unredirected_header('Api-Key','SYNTHETIC_ONLY')
    with pytest.raises(C.ApiPathDenied):C._request(req)


def test_static_reflective_sink_detected():
    assert violations('C.__dict__["_request"](req)','new_entity.py')


def test_policy_roundtrip_preserves_all_provenance_and_is_stable():
    policy=CR.load_policy()
    rendered=SP.dumps(policy)
    assert SP.parse(rendered)==policy
    assert SP.dumps(SP.parse(rendered))==rendered
    assert CR.POLICY_FILE.read_text()==rendered


DANGEROUS_REPORTED_PATHS = (
    '/v1/cargoes/create', '/v1/cargoes/delete', '/v1/cargoes/transport/activate',
    '/v1/cargoes/transport/bind', '/v1/cargoes/transport/create', '/v2/cargoes/delete',
    '/v1/pricing-strategy/status', '/v1/notification/check',
)

@pytest.mark.parametrize('path', DANGEROUS_REPORTED_PATHS + (
    '/v1/analytics/item_turnover', '/v1/supply-order/content/update/validation',
    '/v1/cargoes-label/file/*', '/v99/new/list',
))
@pytest.mark.parametrize('profile', ('runtime', 'control', 'promo'))
def test_owner_required_denials_before_credentials_or_network(path, profile, monkeypatch):
    monkeypatch.setattr(C, '_seller_headers', lambda: pytest.fail('credential construction reached'))
    monkeypatch.setattr(C, '_request', lambda *a, **k: pytest.fail('network dispatch reached'))
    with pytest.raises(C.ApiPathDenied):
        C.seller_call(path, {}, profile=profile)

@pytest.mark.parametrize('source', (
    'import common as C\nkey=C._secrets[C.CONFIG.secret_seller_api_key]',
    'import common as C\nC.urllib.request.urlopen(req)',
    'from common import urllib as u\nu.request.urlopen(req)',
    'import common as C\nC._sm.access_secret_version(request={})',
    'from google.cloud import secretmanager\nsecretmanager.SecretManagerServiceClient()',
    'import google.cloud.secretmanager as sm\nsm.SecretManagerServiceClient()',
    'import common as C\nname="_secrets"\ngetattr(C,name)',
    'import common as C\nC.__dict__["_secrets"]',
    'import common as C\nvars(C)',
    'import common as C\nC.SELLER_PROFILES={"runtime":{("POST","/v1/cargoes/create")}}',
    'import common as C\nC.PROJECT=C.LEGACY_INGESTION_PROJECT',
    'import common as C\nC.seller_call=lambda *a: 0',
    'import common as C\nC.seller_call.__globals__["_secrets"]',
    'import common as C\nC.sys.modules["urllib.request"].urlopen(req)',
))
def test_ast_closes_exported_cache_sdk_and_http_aliases(source):
    assert violations(source, 'new_entity.py')


def test_direct_credential_api_and_cache_reads_denied(monkeypatch):
    monkeypatch.setattr(C, '_sm', None)
    cache = C._CredentialCache()
    cache[C.CONFIG.secret_seller_api_key] = 'SYNTHETIC_ONLY'
    monkeypatch.setattr(C, '_secrets', cache)
    with pytest.raises(C.ApiPathDenied): C.secret(C.CONFIG.secret_seller_api_key)
    with pytest.raises(C.ApiPathDenied): C._seller_headers()
    for read in (lambda: cache[C.CONFIG.secret_seller_api_key], cache.get, cache.items,
                 cache.values, cache.keys, cache.copy, lambda: iter(cache)):
        with pytest.raises(C.ApiPathDenied): read()
    assert C._sm is None
    assert 'SYNTHETIC_ONLY' not in repr(cache)


@pytest.mark.parametrize('source', (
    'import common as C\nC.bq()._http.request("https://api-seller.ozon.ru/v1/cargoes/create", method="POST", headers=h)',
    'import common as C\nC.bq()._connection.api_request(method="GET",path="/secrets/key/versions/latest:access")',
    'client.request(url, method="POST", headers=h)',
    'from google.auth.transport.requests import AuthorizedSession\nAuthorizedSession(creds).request("POST", url)',
    'from googleapiclient.discovery import build\nbuild("secretmanager","v1").projects().secrets().versions().access(name=n).execute()',
    'import grpc\ngrpc.insecure_channel("x")',
    'getattr(client, dynamic_method)(url)',
))
@pytest.mark.parametrize('filename', ('new_entity.py', 'common.py'))
def test_raw_sdk_dispatch_is_never_an_alternate_seller_transport(source, filename):
    assert violations(source, filename)


@pytest.mark.parametrize('source', (
    'from urllib import request as r\nr.urlopen(req)',
    'from google.auth import transport as t\nt.requests.AuthorizedSession(creds).get(url)',
    'import google.auth as a\na.transport.requests.AuthorizedSession(creds).get(url)',
    'import subprocess\nsubprocess.run(["curl",url])',
    'import os\nos.system(command)',
    'import runpy\nrunpy.run_path(path)',
))
def test_parent_imports_and_external_dispatch_cannot_evade_gate(source):
    assert violations(source, 'new_entity.py')
