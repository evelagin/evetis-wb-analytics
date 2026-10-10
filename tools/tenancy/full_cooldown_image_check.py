"""Installed pure FAILED-closure checks; synthetic evidence, no source or live GO."""
from copy import deepcopy
from tools.tenancy import full_cooldown_recovery as R,full_history as F,durable_plan as D,tenant_backfill as BF
from tools.tenancy import full_runtime_handoff as FH
from pipelines.ozon.runtime import full_resume as FR
import cooldown_failed as CF


def check(*,artifact=True):
    m=FR.manifest();doc=F.render_leaf(m,CF.EXACT['index'],BF.datetime(2026,10,10).date())
    state=BF.B.initial(doc['runtime_plan']);state.update(sequence=3,requests=6,pages=3)
    state['progress']['rate_limit']={'count':3,'safe_cap':15,'eligible_at':'2026-10-09T20:03:39.579007+00:00'}
    assert D.digest(state)==CF.EXACT['state_hash']
    p=CF.sealed(dict(CF.EXACT,version=1,type=CF.TYPE,tenant=m['tenant'],project=m['project'],manifest_hash=D.digest(m),intent_hash='1'*64,
        original_runtime_source=m['runtime_source_sha'],original_runtime_image=m['runtime_image'],runtime_source='2'*40,
        runtime_image='europe-west1-docker.pkg.dev/mpa-platform/mpa-runtime/ozon-runtime@sha256:'+'2'*64,runtime_implementation_hash=BF.B.implementation_hash(),
        controller_source='3'*40,controller_image='europe-west1-docker.pkg.dev/mpa-platform/mpa-runtime/tenant-backfill-controller@sha256:'+'3'*64,controller_implementation_hash='4'*64,
        owner_ack_sha256='5'*64,endpoint_decision_sha256='6'*64,forensic_sha256='7'*64,endpoint_trace='UNPROVEN',static_source_effect_proof_accepted=True,source_budget=0))
    proof=CF.sealed(dict(CF.EXACT,version=1,type=CF.TYPE,tenant=m['tenant'],project=m['project'],policy_hash=p['hash'],intent_hash=p['intent_hash'],manifest_hash=p['manifest_hash'],
        failed_aggregate_hash='8'*64,unit_evidence_hash='9'*64,lease_hash='a'*64,checkpoint_hash='b'*64,prior_receipt_hash='c'*64,prior_recon_hash='d'*64,
        eligible_at=state['progress']['rate_limit']['eligible_at'],lease_release='LD_50c5d90dbf43ac9c_0169',lease_release_hash='e'*64,closure_hash='f'*64,
        predicates={k:True for k in CF.PREDICATES},verified_at='2026-10-10T08:00:00+00:00'))
    CF.proof(proof,p);closed=R.closure(proof)
    assert closed['failed'] is True and closed['source_complete'] is closed['persisted_reconciled'] is False and closed['source_budget']==0
    for predicate in CF.PREDICATES:
        for bad in (False,None,1,'UNKNOWN'):
            q=deepcopy(proof);q['predicates'][predicate]=bad;q=CF.sealed({k:v for k,v in q.items() if k!='hash'})
            try:CF.proof(q,p)
            except BF.B.EvidenceError:pass
            else:raise AssertionError('unknown mandatory predicate authorized FAILED recovery')
    for changed in ('run_id','receipt_hash','state_hash','root','shard'):
        q=deepcopy(proof);q[changed]='unknown';q=CF.sealed({k:v for k,v in q.items() if k!='hash'})
        try:CF.proof(q,p)
        except BF.B.EvidenceError:pass
        else:raise AssertionError('foreign incident accepted')
    if artifact:
        runtime=FH.runtime_facts(BF.target(m['tenant']),m)
        assert runtime['runtime_implementation_hash']==BF.B.implementation_hash()
    return dict(failed_cooldown_closed_incident='PASS',failed_cooldown_unknown_blocks='PASS',failed_cooldown_never_success_recon='PASS',full_frozen_runtime_handoff='PASS')


if __name__=='__main__':
    import json
    print(json.dumps(check(),sort_keys=True))
