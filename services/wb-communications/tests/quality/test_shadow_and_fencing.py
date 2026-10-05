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

def test_quality_shadow_own_ledger_only_and_idempotent():
    docs=_docs();before=copy.deepcopy(docs);rt=_rt(SNAP,docs)
    rt.settings=SimpleNamespace(v31_quality_shadow_enabled=True,v3_knowledge_snapshot_id=SNAP.snapshot_id)
    s=run_shadow(rt,max_items=10,deadline=1e18)
    assert s.v31_evaluated==3 and docs==before
    assert all(v['response_quality_v31']['version']==VERSION for v in rt.store.ledger.values())
    assert all('response_quality_v31' not in r for r in rt.writer.rows) # existing BQ schema
    again=run_shadow(rt,max_items=10,deadline=1e18)
    assert again.v31_evaluated==0 and docs==before

def test_shadow_backfill_bounded_and_baseline_not_invented():
    rt=_rt(SNAP,_docs());run_shadow(rt,max_items=10,deadline=1e18)
    rt.settings=SimpleNamespace(v31_quality_shadow_enabled=True,v3_knowledge_snapshot_id=SNAP.snapshot_id)
    count=len(rt.writer.rows)
    s=run_shadow(rt,max_items=1,deadline=1e18)
    assert s.v31_evaluated==1 and len(rt.writer.rows)==count
    q=next(v['response_quality_v31'] for v in rt.store.ledger.values() if 'response_quality_v31' in v)
    assert q['v3_quality']['verdict']=='UNPROVEN'

def test_quality_failure_isolated_from_existing_v3_and_no_raw_logs(monkeypatch,caplog):
    import app.response_quality.core as core
    rt=_rt(SNAP,_docs());rt.settings=SimpleNamespace(v31_quality_shadow_enabled=True)
    def fail(*a,**kw):raise ValueError('secret customer content')
    monkeypatch.setattr(core,'prepare',fail)
    s=run_shadow(rt,max_items=10,deadline=1e18)
    assert s.persisted==3 and s.errors==0 and s.v31_evaluated==0
    assert 'secret customer content' not in caplog.text

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

def test_quality_backfill_write_failure_isolated_and_bounded(monkeypatch):
    rt=_rt(SNAP,_docs());run_shadow(rt,max_items=10,deadline=1e18)
    rt.settings=SimpleNamespace(v31_quality_shadow_enabled=True,v3_knowledge_snapshot_id=SNAP.snapshot_id)
    count=len(rt.writer.rows)
    monkeypatch.setattr(rt.store,'mark',lambda *a:(_ for _ in ()).throw(ValueError('no ledger')))
    s=run_shadow(rt,max_items=1,deadline=1e18)
    assert s.errors==0 and s.v31_evaluated==1 and len(rt.writer.rows)==count

def test_shadow_evaluator_checks_previous_candidate_repetition():
    from app.v3.shadow import _quality_shadow
    rt=_rt(SNAP,{});rt.settings=SimpleNamespace(v31_quality_shadow_enabled=True,v3_knowledge_snapshot_id=SNAP.snapshot_id)
    msg=CASES[0]['message']|{'v2_final_answer':CASES[0]['v2']}
    first=_quality_shadow(rt,msg)
    next_result=_quality_shadow(rt,msg,previous_answers=[first['candidate_text']])
    assert next_result['quality']['dimensions']['repetition']=='NEEDS_IMPROVEMENT'
    assert next_result['status']=='READY'
