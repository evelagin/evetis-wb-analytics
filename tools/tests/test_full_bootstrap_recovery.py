"""Exact source-free bootstrap migration never reopens a stopped source root."""
import copy
import json
import pytest
from tools.tenancy import full_bootstrap_recovery as R, durable_plan as D, full_history as F, tenant_backfill as BF
from tools.tests.test_full_safety import authority
from tools.tests.test_full_controller import NOW


@pytest.fixture
def migration(authority, monkeypatch):
    # Synthetic release fixture isolates closed migration validation; exact live
    # image release gates are exercised by the full authority/packaged suites.
    monkeypatch.setattr(BF, "validate_plan", lambda doc, ack: BF.target(doc["tenant_id"]))
    old=authority.m;new=copy.deepcopy(old)
    new['controller_source_sha']='c'*40
    new['controller_image']=new['controller_image'].split('@')[0]+'@sha256:'+'c'*64
    new['controller_implementation_hash']='d'*64
    new['hash']=D.digest({k:v for k,v in new.items() if k!='hash'})
    index=next(i for i,p in enumerate(old['programs']) if p['entity']=='catalog')
    doc=F.render_leaf(old,index,NOW.astimezone(BF.B.MSK).date());shard=F.shard_root(old,index,doc)
    execution=f"projects/{old['project']}/locations/europe-west1/jobs/tenant-backfill-controller/executions/tenant-backfill-controller-offline"
    def record(kind,payload,sequence=0):return {'version':D.VERSION,'root_hash':old['hash'],'kind':kind,'sequence':sequence,'payload':payload}
    records=[record('FULL_MANIFEST',old),record('CHUNK_PLAN',{'index':index,'plan':doc,'shard_root':shard},index),record('STOPPED',{'reason':'CONTROLLER_GATE_OR_EVIDENCE_FAILURE','controller_execution':execution})]
    proof={'version':R.VERSION,'type':R.TYPE,'tenant':old['tenant'],'project':old['project'],
           'old_root':old['hash'],'new_root':new['hash'],'stop_hash':D.digest(records[2]),'execution':execution,
           'chunk_plan_hash':D.digest(records[1]),'index':index,'shard_root':shard,
           'old_controller_image':old['controller_image'],'new_controller_image':new['controller_image'],
           'new_controller_source':new['controller_source_sha'],'semantic_hash':R.equivalent(old,new),
           'owner_ack_sha256':'a'*64,'predicates':{k:True for k in R.PREDICATES},
           'evidence_hashes':{'offline_regression':'b'*64},'verified_at':NOW.isoformat()}
    proof['hash']=D.digest(proof)
    return old,new,records,proof


def rehash(p):p['hash']=D.digest({k:v for k,v in p.items() if k!='hash'})


def test_exact_migration_preserves_stop_and_never_exempts_old_tick(migration):
    old,new,records,p=migration;before=copy.deepcopy(records)
    assert R.validate(p,old,new,records)==p
    assert D.decide_tick(records,old['hash'],{'status':'ELIGIBLE'})['action']=='STOPPED'
    records.append({'version':D.VERSION,'root_hash':old['hash'],'kind':R.KIND,'sequence':0,'payload':p})
    assert R.validate(p,old,new,records)==p
    assert D.decide_tick(records,old['hash'],{'status':'ELIGIBLE'})['action']=='STOPPED'
    assert records[:3]==before


@pytest.mark.parametrize('predicate',sorted(R.PREDICATES))
@pytest.mark.parametrize('value',[False,'UNPROVEN',None])
def test_every_unknown_or_failed_predicate_blocks(migration,predicate,value):
    old,new,records,p=migration;p['predicates'][predicate]=value;rehash(p)
    with pytest.raises(BF.B.EvidenceError):R.validate(p,old,new,records)


@pytest.mark.parametrize('field',['created_at','cutover','starts','boundaries','programs','finance','limitations','continuation','runtime_image','runtime_source_sha','retained_supplies'])
def test_semantic_or_runtime_change_cannot_be_hidden_as_recovery(migration,field):
    old,new,records,p=migration;new[field]=None;new['hash']=D.digest({k:v for k,v in new.items() if k!='hash'})
    with pytest.raises((BF.B.EvidenceError,TypeError,KeyError,ValueError,AttributeError)):R.validate(p,old,new,records)


@pytest.mark.parametrize('kind',['DISPATCH_INTENT','DISPATCH_RECEIPT','RECONCILED','CHUNK_COMPLETE'])
def test_any_source_or_completion_authority_blocks(migration,kind):
    old,new,records,p=migration;records.append({'version':D.VERSION,'root_hash':old['hash'],'kind':kind,'sequence':1,'payload':{}})
    with pytest.raises(BF.B.EvidenceError):R.validate(p,old,new,records)


@pytest.mark.parametrize('key',['old_root','new_root','stop_hash','chunk_plan_hash','shard_root','project','execution','type'])
def test_foreign_or_changed_scope_blocks(migration,key):
    old,new,records,p=migration;p[key]='f'*64;rehash(p)
    with pytest.raises(BF.B.EvidenceError):R.validate(p,old,new,records)


def test_owner_publication_is_additive_source_free_and_idempotent(migration, monkeypatch):
    from types import SimpleNamespace
    from tools.tenancy import orchestration_contract as O
    old,new,records,p=migration;before=copy.deepcopy(records);markers={}
    monkeypatch.setattr(BF,'start',lambda *a,**k:pytest.fail('source replay'))
    monkeypatch.setattr(O,'block',lambda *a,**k:{})
    monkeypatch.setattr(O,'verify_artifact_source',lambda *a:None)
    # Replace only the synthetic release-file read, retaining all proof guards.
    class File:
        def __truediv__(self,part):return self
        def read_text(self):return json.dumps({'schema_version':2,'image':new['controller_image'],'controller_implementation_hash':new['controller_implementation_hash']})
    monkeypatch.setattr(BF,'REPO',File())
    def commit(root,kind,seq,payload,at):
        r={'version':D.VERSION,'root_hash':root,'kind':kind,'sequence':seq,'payload':payload}
        if r not in records:records.append(r)
        return D.digest(r)
    def create(ds,name,labels,value):
        assert ds=='ref'
        if name in markers:return False
        markers[name]=(labels,value);return True
    b=SimpleNamespace(c={'tenant_id':old['tenant'],'project_id':old['project'],'datasets':{'ref':'ref'}},clock=lambda:NOW,
        store=SimpleNamespace(history=lambda root:records,commit=commit),
        tables=SimpleNamespace(get_table=lambda ds,name:markers.get(name),create_marker=create))
    first=R.publish(b,p,old,new)
    second=R.publish(b,p,old,new)
    assert first==second and first['source_dispatches']==0
    assert first['original_stop_preserved'] is True and first['old_root_resumable'] is False
    assert records[:3]==before and len(records)==4 and len(markers)==1
