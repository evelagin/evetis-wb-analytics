"""Heldi S‴-a: exact terminal-gate recovery after a reconciled page, typed write path. Offline; no cloud call."""
from copy import deepcopy
import pytest
from tools.tenancy import full_terminal_gate_recovery as R,tenant_backfill as BF,durable_plan as D,controller_diagnostics as CD
from tools.tenancy import full_terminal_gate_image_check as X

def test_fixture_is_the_production_page_identity():
    f=X.fixture()
    assert f.shard=='0cbd288401b04b4085d6a71ee4b2cb358b7dcf5fd9a774337865eb48c8a6d716'   # supplies leaf 465 shard of the frozen FULL root
    assert f.doc['ack_hash']=='18721b388ab7f956ce4525f54e122624b964af8921094c4c890c0c533071777c'

def test_certificate_observed_validated_and_published_once():
    assert X.certificate_check()=='PASS' and X.publish_check()=='PASS'

@pytest.mark.parametrize('fault',X.negatives(),ids=lambda fn:fn.__name__)
def test_any_authority_or_effect_beyond_the_page_blocks(fault):
    f=X.fixture();fault(f)
    with pytest.raises(BF.B.EvidenceError):X.observe(f)
    assert not f.effects

@pytest.mark.parametrize('predicate',sorted(R.PREDICATES))
@pytest.mark.parametrize('value',[None,False,1,'UNKNOWN'])
def test_any_unknown_predicate_blocks_all_writes(predicate,value):
    f=X.fixture();p=X.observe(f);p['predicates'][predicate]=value;p=R.sealed({k:v for k,v in p.items() if k!='hash'})
    with pytest.raises(BF.B.EvidenceError),X.observing(f):R.publish(f.b,p,f.m)
    assert not f.effects

@pytest.mark.parametrize('fault',['foreign_root','typed_stop','source_budget','invented_cause','history','foreign_shard','old_release'])
def test_local_bookkeeping_never_exempts_source_or_foreign_root(fault):
    f=X.fixture();p=X.observe(f);root=f.root
    if fault=='foreign_root':p['root']='f'*64
    if fault=='typed_stop':root[-1]['payload']['diagnostic']=dict(version=1,stage='SOURCE_DISPATCH_BOUNDARY',category='EVIDENCE');p['stop_hash']=D.digest(root[-1]);p['protected_root_hashes']=sorted(D.digest(z) for z in root)
    if fault=='source_budget':p['source_budget']=1
    if fault=='invented_cause':p['historical_cause']='TRANSIENT_OR_TIMEZONE_ASSUMED'
    if fault=='history':root.pop(0)
    if fault=='foreign_shard':p['shard']='e'*64
    if fault=='old_release':p['old_image']=p['old_image'].split('@')[0]+'@sha256:'+'d'*64
    p=R.sealed({k:v for k,v in p.items() if k!='hash'})
    with pytest.raises(BF.B.EvidenceError),X.repo_with(f.new):R.validate(p,f.b,f.m,root)
    assert not f.effects

def test_recent_180_authority_is_pinned_and_untouched_by_publication():
    f=X.fixture();p=X.observe(f)
    assert p['recent_policy_hash']==D.digest(f.ctx['recent']) and p['source_budget']==0 and 'claim' not in p
    with X.observing(f):R.publish(f.b,p,f.m)
    assert [r['kind'] for r in f.root[:-1]]==['FULL_MANIFEST','CHUNK_PLAN','STOPPED'] and f.sh==X.fixture().sh   # no shard/page record invented

def test_every_write_failure_is_typed_with_ledger_and_never_repeats_post():
    assert X.write_path_check()=='PASS'

def test_held_lease_under_cadence_and_lost_closure_read_are_typed_stops():
    assert X.main_check()=='PASS'

def test_release_flag_requires_wake_budget_and_adapter_proof():
    from tools.tenancy import orchestration_contract as O
    f=X.fixture();settings=dict(release=f.new['source_sha'],root_hash=f.m['hash'],scheduler_state='PAUSED');c=deepcopy(f.b.c);c.pop('orchestration')
    O.block(c,settings,BF.REPO,f.new)
    with pytest.raises(ValueError,match='gates not PASS'):O.block(c,settings,BF.REPO,dict(f.new,verification={k:v for k,v in f.new['verification'].items() if k!='terminal_gate_recovery_adapter'}))
    without=dict(f.new);without.pop('controller_wake_budget_version');without['verification']={k:v for k,v in f.new['verification'].items() if k!='controller_wake_budget_adapter'}
    with pytest.raises(ValueError,match='terminal gate'):O.block(c,settings,BF.REPO,without)

def test_shared_acceptance_set_and_registrations_include_the_recovery():
    assert X.acceptance_check()=='PASS'
