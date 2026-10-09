"""Network-free installed full-adapter protocol proof; synthetic state is not live GO."""
from datetime import timedelta
import copy
from tools.tenancy import full_history as F, full_controller as H, tenant_backfill as BF
from tools.tenancy import durable_plan as D, cloud_controller as C, orchestration_contract as O, recent_priority as RP


def monitoring_check(store, base, manifest, now):
    """Installed ordered-read race proof, without network or recovery authority."""
    from types import SimpleNamespace
    i=next(i for i,p in enumerate(manifest['programs']) if p['entity']=='catalog')
    calls=[]
    def observe(offsets, success):
        times=iter(now+timedelta(seconds=n) for n in offsets)
        b=SimpleNamespace(c=base.c,store=store,clock=lambda:next(times),
            state=lambda d:BF.B.initial(d['runtime_plan']),
            select=lambda *a:[{'last_success':(now+timedelta(seconds=success)).isoformat(),'failed_attempts':1}],
            journal='mpa-t-client-001.ozon_raw.OZON_INGESTION_RUNS',
            binding_status={'seller':'BOUND','performance':'BOUND'})
        return H.monitoring(b,manifest['hash'],{'status':'MONITORING','index':i})
    for success in (0,1.5,3,4):
        out=observe((0,1,2,4),success)
        assert out['current_scope']['checkpoint_age_seconds']==int(4-success)
        assert out['current_scope']['source_complete'] is False and out['completed_chunks']==0
        assert out['current_scope']['failed_attempts']==1
    for offsets,success in (((0,1,2,4),5),((0,1,2,1),0),((1,0,2,4),0),((0,2,1,4),0)):
        try:observe(offsets,success)
        except BF.B.EvidenceError:calls.append('DENIED')
        else:raise AssertionError('full monitoring temporal integrity weakened')
    assert calls==['DENIED']*4
    return 'PASS'


def paused_entrypoint_check():
    """Reproduce -m/import module identity without network or recovery authority."""
    import contextlib
    import io
    import runpy
    import warnings
    from types import SimpleNamespace
    with warnings.catch_warnings():
        warnings.filterwarnings('ignore', category=RuntimeWarning, message=".*found in sys.modules.*")
        entry = runpy.run_module('tools.tenancy.cloud_controller', run_name='offline_qualified_entrypoint')
    backend = object.__new__(H.Backend)
    backend.c = {'orchestration': {'job': {'env': {'HISTORICAL_SCHEDULER_STATE': 'PAUSED'}}}}
    backend.current_execution = 'offline'
    backend.clock = lambda: None
    writes = []
    backend.store = SimpleNamespace(commit=lambda *a: writes.append(a))
    namespace = entry['main'].__globals__
    namespace['bootstrap'] = lambda env: (backend, 'a' * 64)
    namespace['bounded_wake'] = lambda root, b: b.start({}, lambda *a: None, lambda *a: None)
    original = BF.start
    def denied(*args, **kwargs):
        raise AssertionError('paused entrypoint reached source/lease boundary')
    BF.start = denied
    try:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = entry['main']()
        assert result == 0 and not writes
        assert __import__('json').loads(output.getvalue()) == {'status': 'QUIESCENT_PAUSED', 'source_dispatches': 0}
        assert entry['SourceDispatchPaused'] is H.C.SourceDispatchPaused
        return 'PASS'
    finally:
        BF.start = original


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
              H.Backend.active_runtime_execution,H.Backend.start,H.Backend.reconcile,RP.load)
    posts=[]
    paused_entrypoint=paused_entrypoint_check()
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
        # This synthetic legacy CAS fixture has no live owner descriptor.
        # Priority authority/drain behavior is exercised independently by the
        # installed recent_priority_image_check after these stubs are restored.
        RP.load=lambda *a,**k:None
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
        watermark=monitoring_check(store,base,m,now)
        # No dependency on a local checkpoint: every wake reconstructs markers.
        return {'full_history_adapter':'PASS','full_history_program_contract':'PASS',
                'full_history_restart_protocol':'PASS','full_history_no_false_complete':'PASS',
                'full_history_monitoring_watermark':watermark,
                'full_history_paused_entrypoint':paused_entrypoint,
                'full_history_live_go':'UNPROVEN'}
    finally:
        (H.verify_authority,H.Backend.preflight,H.Backend.state,H.Backend.quota,
         H.Backend.active_runtime_execution,H.Backend.start,H.Backend.reconcile,RP.load)=original
