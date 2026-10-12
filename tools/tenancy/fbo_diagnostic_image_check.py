"""Network-disabled checks of local quarantine and diagnostic owner boundaries."""
from copy import deepcopy
from tools.tenancy import full_fbo_diagnostic_closure as Z,durable_plan as D,tenant_backfill as BF
from tools.tenancy.full_fbo_zero_effect_image_check import fixture as prior_fixture

def fixture():
    m,old,b,root,sh,objects,rows,effects,doc,rc=prior_fixture()
    original_cp=deepcopy(rows)
    p=dict(version=1,type=Z.TYPE,tenant=m['tenant'],project=m['project'],root=m['hash'],manifest_hash=D.digest(m),index=269,
        shard=old['shard'],plan_id=old['plan_id'],ack_hash=old['ack_hash'],stop_hash=old['stop_hash'],controller_execution=old['controller_execution'],
        execution=old['execution'],run_id=old['run_id'],generation=190,sequence=1,intent_hash=old['intent_hash'],receipt_hash=old['receipt_hash'],
        original_runtime_image=old['original_runtime_image'],controller_source=old['new_source'],controller_image=old['new_image'],
        runtime_image=old['runtime_image'],runtime_implementation_hash=old['runtime_implementation_hash'],
        owner_decision_sha256='4'*64,qualification_hash='5'*64,security_hash='6'*64,effect_hash='7'*64,checkpoint_hash=D.digest(rows),lease_hash=old['original_lease_hash'],
        protected_root_hashes=old['protected_root_hashes'],protected_shard_hashes=old['protected_shard_hashes'],predicates=dict.fromkeys(Z.PREDICATES,True),
        verified_at=old['verified_at'],source_budget=0,state_hash=old['state_hash'],validation_class='OUTSIDE_REQUESTED_INTERVAL',status='QUARANTINED_SOURCE_CONTRACT',isolation_scope='FBO_SOURCE')
    p=Z.sealed(p)
    original_select=b.select
    b.select=lambda *a:deepcopy(original_cp)
    b.state=lambda d:BF.B.initial(d['runtime_plan'])
    return m,p,b,root,sh,objects,rows,effects

def check():
    m,p,b,root,sh,objects,rows,effects=fixture();Z.header(p,m);Z.validate(p,b,m,root,final=False)
    for k in Z.PREDICATES:
        q=deepcopy(p);q['predicates'][k]=None;q=Z.sealed({k:v for k,v in q.items() if k!='hash'})
        try:Z.header(q,m)
        except Exception:pass
        else:raise AssertionError('unknown predicate permitted')
    assert not effects and not any(r['kind'] in ('RECONCILED','CHUNK_COMPLETE') for r in sh+root)
    from pipelines.ozon.runtime.fbo_diagnostic import validate
    try:validate({},m['project'],'0'*64,b.clock())
    except ValueError:pass
    else:raise AssertionError('empty owner policy accepted')
    return {'fbo_local_quarantine_header':'PASS','fbo_quarantine_unknown_blocks':'PASS','fbo_quarantine_no_source_completion':'PASS','fbo_diagnostic_empty_authority_denied':'PASS'}

if __name__=='__main__':
    import json
    print(json.dumps(check(),sort_keys=True))
