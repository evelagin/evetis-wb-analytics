"""Packaged, network-free FULL leaf certificate checks; synthetic, never live GO."""
from copy import deepcopy
from datetime import date, datetime, timezone
from tools.tenancy import full_leaf_recovery as R, full_history as F
from tools.tenancy import durable_plan as D, tenant_backfill as BF, orchestration_contract as O

NOW=datetime(2026,10,9,7,tzinfo=timezone.utc)


def fixture():
    c=BF.target('client_001');day=date(2026,10,6)
    supply=next(d for d in BF.QF.manifest()['plans'] if d['runtime_plan']['entity']=='supplies')
    m=F.make_manifest(tenant='client_001',created_at='2026-10-07T13:00:00Z',
        cutover=str(day),chunks=[BF.CK.Chunk(e,day,day) for e in sorted(BF.B.DATED)],
        runtime_source='1'*40,runtime_image=c['marketplaces']['ozon']['runtime_image'],
        controller_source='2'*40,controller_image='europe-west1-docker.pkg.dev/mpa-platform/mpa-runtime/tenant-backfill-controller@sha256:'+'3'*64,
        controller_implementation='4'*64,boundary_evidence={e:'5'*64 for e in BF.B.DATED},
        starts={e:day for e in BF.B.DATED},qualification_evidence={e:'6'*64 for e in F.GATES},retained_supplies=supply)
    index=next(i for i,x in enumerate(m['programs']) if x['entity']=='supplies')
    doc=F.render_leaf(m,index,NOW.astimezone(BF.B.MSK).date());shard=F.shard_root(m,index,doc)
    def record(root,kind,seq,payload):return dict(version=D.VERSION,root_hash=root,kind=kind,sequence=seq,payload=payload)
    run={'run_id':'bf-synthetic-current','lease_generation':2,'ack_hash':doc['ack_hash'],
         'operation':'projects/mpa-t-client-001/locations/europe-west1/operations/synthetic'}
    prior_run=dict(run,run_id='bf-synthetic-prior',lease_generation=1)
    before=BF.B.initial(doc['runtime_plan']);before['sequence']=1
    state=deepcopy(before);state['sequence']=3
    prior=record(shard,'RECONCILED',1,dict(sequence=1,state_hash=D.digest(before),persisted_reconciled=True,source_complete=False,checkpoint='RUNNING'))
    previous=record(shard,'DISPATCH_RECEIPT',1,dict(plan=doc,receipt=prior_run))
    intent=record(shard,'DISPATCH_INTENT',2,dict(plan=doc,preparation=run))
    receipt=record(shard,'DISPATCH_RECEIPT',2,dict(plan=doc,receipt=run))
    sh=[record(shard,'DISPATCH_INTENT',1,dict(plan=doc,preparation=prior_run)),previous,prior,intent,receipt]
    stop=record(m['hash'],'STOPPED',0,dict(reason='SYNTHETIC_OLD_GENERIC_STOP',controller_execution='projects/mpa-t-client-001/locations/europe-west1/jobs/tenant-backfill-controller/executions/tenant-backfill-controller-synthetic'))
    root=[record(m['hash'],'FULL_MANIFEST',0,m),record(m['hash'],'CHUNK_PLAN',index,dict(index=index,plan=doc,shard_root=shard)),stop]
    release=dict(schema_version=2,source_sha='7'*40,image='europe-west1-docker.pkg.dev/mpa-platform/mpa-runtime/tenant-backfill-controller@sha256:'+'8'*64,
        runtime_image=m['runtime_image'],runtime_implementation_hash=BF.B.implementation_hash(),controller_implementation_hash=O.implementation_hash(BF.REPO),
        verification=dict.fromkeys(['ci','exact_image','offline_restart','lost_post_no_repeat','quota_wait_no_source','tenant_isolation','reader_append_separation','full_history_adapter'],'PASS'))
    policy=R.sealed(dict(version=1,type=R.TYPE,tenant='client_001',project=c['project_id'],root=m['hash'],manifest_hash=D.digest(m),
        controller_source=release['source_sha'],controller_image=release['image'],controller_implementation_hash=release['controller_implementation_hash'],
        runtime_source=m['runtime_source_sha'],runtime_image=m['runtime_image'],runtime_implementation_hash=m['runtime_implementation_hash'],
        owner_ack_sha256='9'*64,initial_stop=D.digest(stop),initial_receipt=D.digest(receipt),initial_intent=D.digest(intent),
        initial_generation=2,initial_unit_start=2,initial_unit_end=3,automatic_class_c=True))
    coverage=dict(source_sequence=3,readback=[dict(table='SYNTHETIC',rows=0,keys=0)])
    result=dict(checkpoint='RUNNING',sequence=3,state_hash=D.digest(state),persisted_reconciled=True,source_complete=False,
        lifecycle_changed=False,scheduler_changed=False,coverage_readback=coverage,rows_observed=0,orders=0,supplies=0,bundles=0,coverage='UNPROVEN_PENDING_DQ')
    recon=record(shard,'RECONCILED',2,result)
    proof=R.sealed(dict(version=1,type=R.TYPE,tenant='client_001',project=c['project_id'],root=m['hash'],manifest_hash=D.digest(m),index=index,shard=shard,
        stop_hash=D.digest(stop),intent_hash=D.digest(intent),receipt_hash=D.digest(receipt),prior_receipt_hash=D.digest(previous),prior_recon_hash=D.digest(prior),
        receipt_sequence=2,runtime_execution='projects/mpa-t-client-001/locations/europe-west1/jobs/ozon-runtime-daily/executions/ozon-runtime-daily-synthetic',
        run_id=run['run_id'],generation=2,unit_start=2,unit_end=3,state_hash=D.digest(state),lease_release='LD_'+BF.B.digest(['BOUNDED_PILOT_EXCLUSIVE',c['project_id']])[:16]+'_0002',
        lease_release_hash='a'*64,unit_evidence_hash='b'*64,coverage_hash=D.digest(coverage),policy_hash=policy['hash'],
        implementation_source=policy['controller_source'],controller_image=policy['controller_image'],runtime_source=policy['runtime_source'],runtime_image=policy['runtime_image'],
        reconciliation=result,reconciliation_hash=D.digest(recon),predicates=dict.fromkeys(R.PREDICATES,True),verified_at=NOW.isoformat()))
    return m,policy,proof,root,sh,recon,release,before,state


def check():
    m,p,proof,root,sh,recon,release,_,_=fixture()
    R.validate_policy(p,m,release);R.validate(proof,m,p,root,sh)
    try:R.validate(proof,m,p,root,sh,final=True)
    except BF.B.EvidenceError:pass
    else:raise AssertionError('missing shard RECON accepted')
    R.validate(proof,m,p,root,sh+[recon],final=True)
    for field in R.PREDICATES:
        q=deepcopy(proof);q['predicates'][field]=None;q=R.sealed({k:v for k,v in q.items() if k!='hash'})
        try:R.validate(q,m,p,root,sh)
        except BF.B.EvidenceError:pass
        else:raise AssertionError('unknown predicate accepted')
    from tools.tenancy.controller_diagnostics import STAGES,GateFailure,safe
    for stage in STAGES:
        assert safe(GateFailure(stage,'EVIDENCE'))==dict(version=1,stage=stage,category='EVIDENCE')
    return dict(full_leaf_class_c_v1='PASS',full_leaf_unknown_blocks='PASS',full_leaf_root_shard_linkage='PASS',typed_gate_diagnostics='PASS')


if __name__=='__main__':
    import json
    print(json.dumps(check(),sort_keys=True))
