"""Network-free installed priority/read-only coverage proof; never live GO."""
from copy import deepcopy
from tools.tenancy import recent_priority as P, durable_plan as D
from tools.tenancy.full_leaf_image_check import fixture, NOW


def protocol_check(m,p):
    """Run the actual drain/order/CAS boundary inside the installed image."""
    from contextlib import ExitStack
    from types import SimpleNamespace
    from unittest.mock import patch
    from tools.tenancy import full_controller as H, full_history as F, tenant_backfill as BF
    objects={};rows=[];states={};posts=[]
    class Metadata:
        def list_tables(self,ds):return [(n,v[0],None) for n,v in objects.items()]
        def get_table(self,ds,n):return objects.get(n)
        def create_marker(self,ds,n,labels,desc):
            if n in objects:return False
            objects[n]=(labels,desc);return True
    def select(c,q,params):
        return [{'evidence_json':v} for v in {r['evidence_json'] for r in rows
                if r['plan_hash']==params['root'][1] and ('record' not in params or r['backfill_id']==params['record'][1])}]
    c=BF.target('client_001');store=D.DurableRecords(c,Metadata(),select,lambda r:rows.append(deepcopy(r)))
    base=SimpleNamespace(c=c,store=store,clock=lambda:NOW,binding_status={'seller':'BOUND','performance':'BOUND'})
    store.commit(m['hash'],'FULL_MANIFEST',0,m,NOW)
    catalog=next(i for i,x in enumerate(m['programs']) if x['entity']=='catalog')
    doc=F.render_leaf(m,catalog,NOW.astimezone(BF.B.MSK).date());sh=F.shard_root(m,catalog,doc)
    proof=dict(index=catalog,shard_root=sh,ack_hash=doc['ack_hash'],state_hash='1'*64,sequence=1,dispatch_attempts=1,
        source_complete=True,persisted_reconciled=True,reused_qualification=None,coverage=dict(source_sequence=1,readback=[dict(rows=1,keys=1)]),completed_at=NOW.isoformat())
    store.commit(m['hash'],'CHUNK_PLAN',catalog,dict(index=catalog,plan=doc,shard_root=sh),NOW)
    store.commit(m['hash'],'CHUNK_COMPLETE',catalog,proof,NOW)
    store.commit(m['hash'],'SNAPSHOT_CERT',0,dict(day=str(NOW.astimezone(BF.B.MSK).date()),plan_hash=doc['ack_hash'],coverage=proof['coverage']),NOW)
    initial=BF.B.initial(doc['runtime_plan']);initial.update(sequence=1,complete=True);states[doc['ack_hash']]=initial
    supplies=next(i for i,x in enumerate(m['programs']) if x['entity']=='supplies')
    supply=F.render_leaf(m,supplies,NOW.astimezone(BF.B.MSK).date());item=dict(index=supplies,plan=supply,shard_root=F.shard_root(m,supplies,supply))
    store.commit(m['hash'],'CHUNK_PLAN',supplies,item,NOW)
    def state(b,d):return deepcopy(states.setdefault(d['ack_hash'],BF.B.initial(d['runtime_plan'])))
    def source(b,d,before,after):
        n=len(posts)+1;prep=dict(run_id=f'synthetic-{n}',lease_generation=n,ack_hash=d['ack_hash'])
        before(prep);posts.append(d['ack_hash']);after(dict(prep,operation=f'projects/mpa-t-client-001/locations/europe-west1/operations/synthetic-{n}'))
    with ExitStack() as stack:
        for target,value in [('verify_authority',lambda b,m:b.c),('Backend.preflight',lambda *a,**k:None),
            ('Backend.state',state),('Backend.quota',lambda *a:dict(status='ELIGIBLE',allowance=0)),
            ('Backend.active_runtime_execution',lambda b:False),('Backend.start',source),
            ('Backend.reconcile',lambda *a:dict(source_complete=False,persisted_reconciled=True))]:
            stack.enter_context(patch('tools.tenancy.full_controller.'+target,value))
        stack.enter_context(patch('tools.tenancy.recent_priority.load',lambda *a,**k:p))
        H.leaf_wake(H.Backend(base,m),m,supplies,item,[])
        preserved=deepcopy(states[supply['ack_hash']])
        result,_=H.wake(base,m['hash'])
        assert result['source_dispatches']==1 and result['index']==p['selection'][0]['index']
        assert states[supply['ack_hash']]==preserved and len(posts)==2
        history=store.history(item['shard_root'])
        assert [r['kind'] for r in history].count('DISPATCH_INTENT')==1
        assert [r['kind'] for r in history].count('RECONCILED')==1
        # A still-active receipt drains before another source; it is not dropped
        # by priority, quota or receipt age.
        with patch('tools.tenancy.full_controller.Backend.reconcile',lambda *a:None):
            assert H.wake(base,m['hash'])[0]['status']=='MONITORING' and len(posts)==2
    return 'PASS'


def check():
    m,old,_,_,_,_,release,*_=fixture()
    release=deepcopy(release);release['schema_version']=3
    release['verification']['recent_priority_adapter']='PASS'
    p=P.make(m,release,design_hash='a'*64,owner_ack_sha256='b'*64,
        historical_recovery_policies=[{'source':'c'*40,'policy_hash':old['hash']}],activated_at=NOW.isoformat())
    P.validate(p,m,release)
    original=deepcopy(m)
    order=P.ordered(m,{},p);recent=[x['index'] for x in p['selection']]
    assert order[:len(recent)]==recent and sorted(order)==list(range(len(m['programs'])))
    assert P.ordered(m,{i:{} for i in recent},p)==[i for i in P.ordered(m,{},None) if i not in recent]
    for key,bad in [('until','2026-10-09'),('days',181),('runtime_source','c'*40),('selection',[])]:
        q=deepcopy(p);q[key]=bad;q=P.sealed({k:v for k,v in q.items() if k!='hash'})
        try:P.validate(q,m,release)
        except Exception as e:
            from tools.tenancy import tenant_backfill as BF
            assert isinstance(e,BF.B.EvidenceError)
        else:raise AssertionError('priority authority broadened')
    out=P.progress(m,[],{},p,now=NOW,waiting_receipts=1,unresolved_stops=0)
    assert not any(w['ready'] for w in out['windows'].values())
    assert all(w['finance_economic_finality']=='PROVISIONAL' for w in out['windows'].values())
    assert m==original and D.digest(m)==p['manifest_hash']
    return dict(recent_priority_adapter='PASS',recent_frozen_scope='PASS',
        recent_no_false_ready='PASS',recent_readonly_progress='PASS',recent_installed_drain_order=protocol_check(m,p))


if __name__=='__main__':
    import json
    print(json.dumps(check(),sort_keys=True))
