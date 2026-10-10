"""Packaged closed-type guards and current descriptor reader, no network."""
from copy import deepcopy
from types import SimpleNamespace
from tools.tenancy import full_policy_bootstrap_recovery as PB,full_cooldown_recovery as CF
from tools.tenancy import full_runtime_handoff as FH,tenant_backfill as BF,durable_plan as D
from tools.tenancy import orchestration_contract as O,cloud_controller as C
from pipelines.ozon.runtime import full_resume as FR
import cooldown_failed as CORE


def check():
    m=FR.manifest();c=deepcopy(BF.target('client_001'));facts=FH.runtime_facts(c,m)
    source=(BF.REPO/'CONTROLLER_SOURCE_SHA').read_text().strip() if (BF.REPO/'CONTROLLER_SOURCE_SHA').exists() else '3'*40
    release=dict(schema_version=3,source_sha=source,image='europe-west1-docker.pkg.dev/mpa-platform/mpa-runtime/tenant-backfill-controller@sha256:'+'3'*64,
        runtime_image=facts['runtime_image'],runtime_implementation_hash=facts['runtime_implementation_hash'],controller_implementation_hash=O.implementation_hash(BF.REPO),
        verification={k:'PASS' for k in ('ci','exact_image','offline_restart','lost_post_no_repeat','quota_wait_no_source',
            'tenant_isolation','reader_append_separation','full_history_adapter','recent_priority_adapter')})
    settings=dict(release=source,root_hash=m['hash'],scheduler_state='PAUSED');c['orchestration']=O.block(c,settings,BF.REPO,release)
    p=CORE.sealed(dict(CORE.EXACT,version=1,type=CORE.TYPE,tenant=m['tenant'],project=m['project'],manifest_hash=D.digest(m),intent_hash='1'*64,
        original_runtime_source=m['runtime_source_sha'],original_runtime_image=m['runtime_image'],**facts,controller_source=source,controller_image=release['image'],
        controller_implementation_hash=release['controller_implementation_hash'],owner_ack_sha256='5'*64,endpoint_decision_sha256='6'*64,
        forensic_sha256='7'*64,endpoint_trace='UNPROVEN',static_source_effect_proof_accepted=True,source_budget=0))
    reads=[];name=C.descriptor_name(source,m['hash'],'PAUSED')
    def get(ds,n):
        assert (ds,n)==(c['datasets']['tenant_locks'],name)
        reads.append(n);return ({},D.encoded(dict(settings=settings,release=release)))
    b=SimpleNamespace(c=c,tables=SimpleNamespace(get_table=get))
    assert CF.validate_policy(p,b,m)==p and reads==[name]
    assert not (BF.REPO/'infra/tenant/releases/backfill'/f'{source}.json').exists()
    v=PB.sealed(dict(PB.EXACT,version=1,type=PB.TYPE,tenant=m['tenant'],project=m['project'],manifest_hash=D.digest(m),
        new_source='2'*40,new_image=release['image'],new_implementation_hash=release['controller_implementation_hash'],runtime_image=facts['runtime_image'],
        owner_decision_sha256='1'*64,packaged_probe_hash='2'*64,replacement_image_check_hash='4'*64,
        protected_record_hashes=['5'*64],protected_history_hash=D.digest(['5'*64]),
        predicates={k:True for k in PB.PREDICATES},verified_at='2026-10-10T10:00:00Z',source_budget=0))
    PB.proof_header(v,m)
    for key in PB.PREDICATES:
        for bad in (False,None,1,'UNKNOWN'):
            q=deepcopy(v);q['predicates'][key]=bad;q=PB.sealed({k:w for k,w in q.items() if k!='hash'})
            try:PB.proof_header(q,m)
            except BF.B.EvidenceError:pass
            else:raise AssertionError('unknown predicate authorized bootstrap recovery')
    for key in ('stop_hash','execution','root','wake_claim_hash','failed4_certificate'):
        q=deepcopy(v);q[key]='unknown';q=PB.sealed({k:w for k,w in q.items() if k!='hash'})
        try:PB.proof_header(q,m)
        except BF.B.EvidenceError:pass
        else:raise AssertionError('another incident authorized')
    return dict(current_policy_descriptor_without_self_registration='PASS',exact_bootstrap_unknown_blocks='PASS',exact_bootstrap_source_budget_zero='PASS')


if __name__=='__main__':
    import json
    print(json.dumps(check(),sort_keys=True))
