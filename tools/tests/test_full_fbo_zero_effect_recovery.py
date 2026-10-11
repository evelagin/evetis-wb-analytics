from copy import deepcopy
from types import SimpleNamespace
import pytest
from tools.tenancy import full_fbo_zero_effect_recovery as R,durable_plan as D,tenant_backfill as BF,full_controller as H
from tools.tenancy.full_fbo_zero_effect_image_check import fixture,check

def test_packaged_owner_contract():assert set(check().values())=={'PASS'}

def test_owner_observation_requires_real_priority_loader_before_effect_checks(monkeypatch):
 from tools.tenancy import full_leaf_recovery as FL
 m,p,b,root,sh,objects,rows,effects,*_=fixture();calls=[]
 observer=SimpleNamespace(preflight=lambda doc:calls.append('preflight'))
 monkeypatch.setattr(FL,'observer',lambda *a:observer)
 monkeypatch.setattr(H,'verify_authority',lambda *a:None)
 # The real loader consumes the packaged deployment descriptor and rejects
 # the missing owner marker. It must not invent an API or bypass authority.
 with pytest.raises(BF.B.EvidenceError,match='owner priority authority absent'):
  R.observe(b,m,owner_decision_sha256='4'*64,qualification_hash='5'*64,security_hash='6'*64)
 assert calls==['preflight'] and not effects

@pytest.mark.parametrize('field',sorted(R.PREDICATES))
@pytest.mark.parametrize('bad',[False,None,1,'UNKNOWN'])
def test_every_unknown_predicate_blocks_before_writes(field,bad):
 m,p,b,root,sh,objects,rows,effects,*_=fixture();p['predicates'][field]=bad;p=R.sealed({k:v for k,v in p.items() if k!='hash'})
 with pytest.raises(BF.B.EvidenceError):R.publish(b,p,m)
 assert not effects

@pytest.mark.parametrize('fault',['foreign_root','foreign_stop','foreign_run','lease','positive_budget','extra_field','cause_claim','two_retries','wrong_sequence','missing_intent','changed_receipt','original_recon','closure','checkpoint','lease_bytes'])
def test_unknown_effect_or_changed_history_blocks(fault):
 m,p,b,root,sh,objects,rows,effects,*_=fixture()
 if fault=='foreign_root':p['root']='a'*64
 if fault=='foreign_stop':p['stop_hash']='a'*64
 if fault=='foreign_run':p['run_id']='other-run'
 if fault=='lease':p['lease_generation']=191
 if fault=='positive_budget':p['source_budget']=1
 if fault=='extra_field':p['extra']=True
 if fault=='cause_claim':p['legacy_cause']='ASSUMED_TIMEZONE'
 if fault=='two_retries':p['retry_limit']=2
 if fault=='wrong_sequence':p['retry_sequence']=3
 if fault=='missing_intent':sh.pop(0)
 if fault=='changed_receipt':sh[1]['payload']['receipt']['operation']='unknown'
 if fault=='original_recon':sh.append(dict(sh[0],kind='RECONCILED'))
 if fault=='closure':p['closure_hash']='a'*64
 if fault=='checkpoint':rows[0]['evidence_json']='{"mode":"BOUNDED_PILOT","proof":null}'
 if fault=='lease_bytes':objects['L_50c5d90dbf43ac9c_0190']=({'owner':p['run_id']},'{}')
 p=R.sealed({k:v for k,v in p.items() if k!='hash'})
 with pytest.raises(BF.B.EvidenceError):R.validate(p,b,m,root,final=False)
 assert not effects

def test_additive_settlement_idempotent_without_source_success_or_original_rewrite(monkeypatch):
 m,p,b,root,sh,objects,rows,effects,*_=fixture();old=deepcopy((root,sh,objects,rows))
 monkeypatch.setattr(R,'observe',lambda *a,**k:(deepcopy(p),{}))
 assert R.publish(b,p,m)==p and R.load(b,m,root)==[p]
 assert root[:len(old[0])]==old[0] and sh[:len(old[1])]==old[1] and rows[0]==old[3][0]
 assert all(objects[k]==v for k,v in old[2].items())
 assert len(rows)==2 and rows[1]['status']=='FAILED' and not any(z['kind']=='RECONCILED' for z in sh)
 before=deepcopy(effects);assert R.publish(b,p,m)==p and effects==before
 assert D.decide_tick(sh,p['shard'],{'status':'ELIGIBLE'},verified_fbo_zero_effect=[p])=={'action':'PREPARE_NEXT','sequence':2,'poll_only':False}

@pytest.mark.parametrize('change',['source_effect_hash','protected_shard_hashes','protected_root_hashes','original_lease_hash'])
def test_fresh_effect_change_blocks_every_write(monkeypatch,change):
 m,p,b,root,sh,objects,rows,effects,*_=fixture();fresh=deepcopy(p)
 fresh[change]=['a'*64] if change.endswith('hashes') else 'a'*64
 monkeypatch.setattr(R,'observe',lambda *a,**k:(fresh,{}))
 with pytest.raises(BF.B.EvidenceError,match='fresh mandatory'):R.publish(b,p,m)
 assert not effects

@pytest.mark.parametrize('http',['429','500','502','503','504','NET_ERROR','typed-window'])
def test_second_failed_attempt_never_releases_lease_or_retries_even_known_http(http):
 m,p,b,root,sh,objects,rows,effects,doc,receipt=fixture();b.verified_fbo_zero_effect_failures=[p]
 def denied(*a,**kw):raise AssertionError('must stop before any network/select/write')
 b.request=b.select=denied
 with pytest.raises(BF.B.EvidenceError,match='NO_AUTOMATIC_REPEAT'):
  H.Backend.reconcile_failed(b,doc,dict(receipt,run_id='new-run',lease_generation=191),{'error':{'message':http}})
 assert not effects

def test_unrelated_plan_not_exempted_or_retry_blocked():
 m,p,b,root,sh,objects,rows,effects,doc,receipt=fixture();b.verified_fbo_zero_effect_failures=[p]
 other=deepcopy(doc);other['runtime_plan']['plan_id']='f'*64
 assert not R.protected_retry(b,other,dict(receipt,run_id='new-run',lease_generation=191))
 with pytest.raises(BF.B.EvidenceError):D.decide_tick(sh,'f'*64,{'status':'ELIGIBLE'},verified_fbo_zero_effect=[p])

def test_historical_qualified_runtime_pair_survives_next_diagnostic_release():
 from tools.tenancy import orchestration_contract as O
 from tools.tenancy.validation import parse_tenant_json as parse
 c=deepcopy(BF.target('client_001'));before=deepcopy(c)
 r=parse((BF.REPO/'infra/tenant/releases/backfill/91c562a2994b5025fd66dfc7c09c68db43ded7ae.json').read_text())
 c['marketplaces']['ozon']['runtime_image']=r['runtime_image'].split('@')[0]+'@sha256:'+'f'*64
 original=deepcopy(c);retained=O.historical_contract(c,r)
 assert retained['marketplaces']['ozon']['runtime_image']==r['runtime_image'] and c==original
 bad=deepcopy(r);bad['runtime_implementation_hash']='f'*64
 with pytest.raises(ValueError,match='registration differs'):O.historical_contract(c,bad)
