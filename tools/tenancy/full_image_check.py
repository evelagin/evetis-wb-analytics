"""Network-free installed full-adapter protocol proof; synthetic state is not live GO."""
from datetime import timedelta
import copy
from tools.tenancy import full_history as F, full_controller as H, tenant_backfill as BF
from tools.tenancy import durable_plan as D, cloud_controller as C, orchestration_contract as O


def check(store, base, now, source):
    day=now.astimezone(BF.B.MSK).date();cutover=day-timedelta(days=1)
    starts={e:cutover for e in BF.B.DATED}
    chunks=[BF.CK.Chunk(e,cutover,cutover) for e in sorted(BF.B.DATED)]
    m=F.make_manifest(tenant=base.c['tenant_id'],created_at=now.isoformat(),cutover=str(cutover),
        chunks=chunks,runtime_source='1'*40,runtime_image=base.c['marketplaces']['ozon']['runtime_image'],
        controller_source=source,controller_image='europe-west1-docker.pkg.dev/mpa-platform/mpa-runtime/tenant-backfill-controller@sha256:'+'2'*64,
        controller_implementation=O.implementation_hash(BF.REPO),boundary_evidence={e:'3'*64 for e in BF.B.DATED},
        starts=starts,qualification_evidence={e:'4'*64 for e in F.GATES},
        retained_supplies=BF.QF.accepted_doc(next(d['runtime_plan']['plan_id'] for d in BF.QF.manifest()['plans'] if d['runtime_plan']['entity']=='supplies')))
    assert len(m['programs'])==11 and F.validate_manifest(m)
    for verdict in ('UNPROVEN','FAIL',True):
        gates={g:'PASS' for g in F.GATES};gates['sku90']=verdict
        try:F.verify_gate_results(gates,m['qualification_evidence'])
        except BF.B.EvidenceError:pass
        else:raise AssertionError('unproven full GO accepted')
    store.commit(m['hash'],'FULL_MANIFEST',0,m,now)
    original=(H.verify_authority,H.Backend.preflight,H.Backend.state,H.Backend.quota,
              H.Backend.active_runtime_execution,H.Backend.start,H.Backend.reconcile)
    posts=[]
    def scope(b,m):
        assert m['project']==b.c['project_id'] and m['tenant']==b.c['tenant_id']
        return b.c
    def source(b,doc,before,after):
        n=len(posts)+1;prep={'run_id':'synthetic-'+str(n),'lease_generation':n,'ack_hash':doc['ack_hash']}
        before(prep);posts.append(n)
        if n==3:raise BF.B.EvidenceError('simulated lost Run POST response')
        after(dict(prep,operation='projects/'+base.c['project_id']+'/locations/europe-west1/operations/synthetic-'+str(n)))
    try:
        # Isolate the actual CAS/dispatch protocol; synthetic authority is NOT a
        # deployment/GO claim. Production guards are restored before returning.
        H.verify_authority=scope;H.Backend.preflight=lambda *a,**k:None
        H.Backend.state=lambda b,d:BF.B.initial(d['runtime_plan'])
        H.Backend.quota=lambda *a:{'status':'ELIGIBLE','allowance':0}
        H.Backend.active_runtime_execution=lambda b:False
        H.Backend.start=source
        H.Backend.reconcile=lambda *a:{'source_complete':False,'persisted_reconciled':True}
        assert H.wake(base,m['hash'])[0]['source_dispatches']==1
        assert H.wake(base,m['hash'])[0]['source_dispatches']==1
        try:H.wake(base,m['hash'])
        except BF.B.EvidenceError:pass
        else:raise AssertionError('ambiguous dispatch ignored')
        assert H.wake(base,m['hash'])[0]['status']=='STOPPED' and posts==[1,2,3]
        assert not any(r['kind'] in {'CHUNK_COMPLETE','FULL_COMPLETE'} for r in store.history(m['hash']))
        # No dependency on a local checkpoint: every wake reconstructs markers.
        return {'full_history_adapter':'PASS','full_history_program_contract':'PASS',
                'full_history_restart_protocol':'PASS','full_history_no_false_complete':'PASS',
                'full_history_live_go':'UNPROVEN'}
    finally:
        (H.verify_authority,H.Backend.preflight,H.Backend.state,H.Backend.quota,
         H.Backend.active_runtime_execution,H.Backend.start,H.Backend.reconcile)=original
