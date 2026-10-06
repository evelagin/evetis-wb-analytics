import copy
from types import SimpleNamespace
import pytest
from app.response_quality import VERSION
from app.v3.snapshot import load_snapshot
from app.v3.shadow import run_shadow
from tests.v3.test_shadow import _rt, _docs
from tests.quality.test_response_quality import CASES
from app.services.repository import FirestoreRepository, MemoryRepository
from app.domain.models import GenerationResult
from app.domain.exceptions import InvalidTransition

SNAP=load_snapshot()

def _activated_docs():
    # R1: only communications created and first seen inside the activation window.
    from tests.quality.test_r1_shadow_pilot import NOW
    docs=_docs()
    for d in docs.values():d['source_created_at']=d['first_seen_at']=NOW.isoformat()
    return docs

def _run(rt):
    from tests.quality.test_r1_shadow_pilot import NOW
    return run_shadow(rt,max_items=10,deadline=1e18,wall_clock=lambda:NOW)

def test_quality_shadow_own_ledger_only_and_idempotent():
    from tests.quality.test_r1_shadow_pilot import r1
    docs=_activated_docs();before=copy.deepcopy(docs);rt=_rt(SNAP,docs)
    rt.settings=r1()
    s=_run(rt)
    assert s.v31_evaluated==3 and docs==before
    assert all(v['response_quality_v31']['version']==VERSION for v in rt.store.ledger.values())
    assert all('response_quality_v31' not in r for r in rt.writer.rows) # existing BQ schema
    again=_run(rt)
    assert again.v31_evaluated==0 and docs==before

def test_no_quality_backfill_after_activation():
    # Decided by the v3 baseline before 3.1E was on: never revisited afterwards.
    from tests.quality.test_r1_shadow_pilot import r1
    rt=_rt(SNAP,_activated_docs());_run(rt)
    rt.settings=r1()
    count=len(rt.writer.rows)
    s=_run(rt)
    assert s.v31_evaluated==0 and s.decided==0 and len(rt.writer.rows)==count
    assert all('response_quality_v31' not in v for v in rt.store.ledger.values())

def test_quality_baseline_not_invented_for_failed_v3():
    from tests.quality.test_r1_shadow_pilot import r1
    rt=_rt(SNAP,_activated_docs());rt.settings=r1()
    rt.engine.decide=lambda msg:(_ for _ in ()).throw(RuntimeError('v3 down'))
    s=_run(rt)
    assert s.errors==3 and s.v31_evaluated==3
    assert all(v['response_quality_v31']['v3_quality']['verdict']=='UNPROVEN' for v in rt.store.ledger.values())

def test_quality_failure_isolated_from_existing_v3_and_no_raw_logs(monkeypatch,caplog):
    import app.response_quality.core as core
    from tests.quality.test_r1_shadow_pilot import r1
    rt=_rt(SNAP,_activated_docs());rt.settings=r1()
    def fail(*a,**kw):raise ValueError('secret customer content')
    monkeypatch.setattr(core,'prepare',fail)
    s=_run(rt)
    assert s.persisted==3 and s.errors==0 and s.v31_evaluated==0
    assert 'secret customer content' not in caplog.text
    pilot=[v for k,v in rt.store.pilot.items() if k.startswith('C.')]
    assert len(pilot)==3 and {r['status'] for r in pilot}=={'ERROR'} and {r['error_class'] for r in pilot}=={'ValueError'}
    assert 'secret customer content' not in str(pilot)

def test_ingredient_neighbor_and_ambiguous_alias_remain_distinct():
    hits=lambda text:{i for i,_ in SNAP.ingredient_mentions(text)}
    assert hits('гиалурон')=={'hyaluronic_acid','sodium_hyaluronate'}
    assert 'salicylic_acid' not in hits('салицилоил')
    assert hits('никотинамид')=={'niacinamide'}
    assert hits('витамин b3')=={'niacinamide'}
    assert 'parfum' not in hits('benzyl alcohol')

class Snapshot:
    def __init__(self,doc):self.doc=doc;self.exists=bool(doc)
    def to_dict(self):return copy.deepcopy(self.doc)
class Ref:
    def __init__(self,client):self.client=client
    def get(self,transaction):return Snapshot(self.client.doc)
class Transaction:
    def __init__(self,client):self.client=client;self.updates=[]
    def update(self,ref,fields):self.updates.append(copy.deepcopy(fields));self.client.doc.update(copy.deepcopy(fields))
class Client:
    def __init__(self,doc):self.doc=doc;self.tx=Transaction(self)
    def transaction(self):return self.tx

@pytest.mark.parametrize('mode',['manual','regenerate'])
def test_firestore_recovery_is_in_same_fenced_transaction(monkeypatch,mode):
    from google.cloud import firestore
    monkeypatch.setattr(firestore,'transactional',lambda f:f)
    doc=dict(status='editing' if mode=='manual' else 'regenerating',lock_token='own',generation_number=4,
        ai_answer='original',final_answer='old',answer_versions=[],nm_id='535581674')
    client=Client(doc);repo=FirestoreRepository.__new__(FirestoreRepository)
    monkeypatch.setattr(repo,'_lazy',lambda:client);monkeypatch.setattr(repo,'_doc',lambda _:Ref(client))
    gen=GenerationResult('fixed','deterministic','v31',{},0,'')
    recovery={'status':'READY','repaired':True}
    if mode=='manual':after=repo.commit_manual_answer('id','operator','own',4,prepared_gen=gen,recovery=recovery)
    else:after=repo.commit_regenerate('id',GenerationResult('operator','fake','v2',{},0,''),'own',prepared_gen=gen,recovery=recovery)
    assert len(client.tx.updates)==1
    assert after['generation_number']==6 and after['final_answer']=='fixed' and after['ai_answer']=='original'
    assert [v['source'] for v in after['answer_versions']]==(['manual','policy_repair'] if mode=='manual' else ['regenerated','policy_repair'])
    assert client.tx.updates[0]['response_recovery']==recovery
    assert 'nm_id' not in client.tx.updates[0]

@pytest.mark.parametrize('mode',['manual','regenerate'])
def test_firestore_stale_recovery_does_not_commit(monkeypatch,mode):
    from google.cloud import firestore
    monkeypatch.setattr(firestore,'transactional',lambda f:f)
    client=Client(dict(status='editing' if mode=='manual' else 'regenerating',lock_token='other',generation_number=4))
    repo=FirestoreRepository.__new__(FirestoreRepository)
    monkeypatch.setattr(repo,'_lazy',lambda:client);monkeypatch.setattr(repo,'_doc',lambda _:Ref(client))
    with pytest.raises(InvalidTransition):
        if mode=='manual':repo.commit_manual_answer('id','operator','own',4,recovery={'status':'READY'})
        else:repo.commit_regenerate('id',None,'own',recovery={'status':'READY'})
    assert not client.tx.updates

def test_quality_ledger_write_failure_isolated_and_bounded(monkeypatch):
    from tests.quality.test_r1_shadow_pilot import r1
    rt=_rt(SNAP,_activated_docs());rt.settings=r1()
    monkeypatch.setattr(rt.store,'mark',lambda *a:(_ for _ in ()).throw(ValueError('no ledger')))
    s=_run(rt)
    assert s.errors==0 and s.v31_evaluated==3 and len(rt.writer.rows)==3
    again=_run(rt)          # ledger lost, pilot claims kept: v3 re-decides, 3.1E does not
    assert again.v31_evaluated==0 and again.v31_skips=={'SHADOW_ALREADY_EVALUATED':3}

def test_shadow_evaluator_checks_previous_candidate_repetition():
    from app.v3.shadow import _quality_shadow
    rt=_rt(SNAP,{});rt.settings=SimpleNamespace(v31_quality_shadow_enabled=True,v3_knowledge_snapshot_id=SNAP.snapshot_id)
    msg=CASES[0]['message']|{'v2_final_answer':CASES[0]['v2']}
    first=_quality_shadow(rt,msg)
    next_result=_quality_shadow(rt,msg,previous_answers=[first['candidate_text']])
    assert next_result['quality']['dimensions']['repetition_penalty']=='NEEDS_IMPROVEMENT'
    assert next_result['status']=='READY'
