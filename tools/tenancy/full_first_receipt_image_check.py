"""Packaged, network-free first-authority and completion separation checks."""
from copy import deepcopy
from datetime import date,timedelta
from tools.tenancy import full_first_receipt_recovery as R, full_leaf_recovery as FL, full_history as F, durable_plan as D, tenant_backfill as BF
from tools.tenancy.full_leaf_image_check import fixture as legacy_fixture,NOW


def fixture():
    m,p,old,root,sh,_,release,_,_=legacy_fixture()
    index=next(i for i,x in enumerate(m['programs']) if x['entity']=='finance_accrual')
    doc=F.render_leaf(m,index,NOW.astimezone(BF.B.MSK).date());shard=F.shard_root(m,index,doc)
    def record(root,kind,seq,payload):return dict(version=D.VERSION,root_hash=root,kind=kind,sequence=seq,payload=payload)
    run=dict(sh[-1]['payload']['receipt'],ack_hash=doc['ack_hash'])
    intent=record(shard,'DISPATCH_INTENT',1,dict(plan=doc,preparation=run));rec=record(shard,'DISPATCH_RECEIPT',1,dict(plan=doc,receipt=run))
    stop=root[-1];stop['payload']['diagnostic']=dict(version=1,stage='PERSISTED_SOURCE_RECONCILIATION',category='EVIDENCE',context=dict(index=index,shard=shard,sequence=1,intent_hash=D.digest(intent),receipt_hash=D.digest(rec)))
    root=[root[0],record(m['hash'],'CHUNK_PLAN',index,dict(index=index,plan=doc,shard_root=shard)),stop];sh=[intent,rec]
    state=BF.B.initial(doc['runtime_plan']);state.update(sequence=1,complete=True);state['progress']['next_day']=str(date.fromisoformat(doc['runtime_plan']['to'])+timedelta(days=1))
    p={k:v for k,v in p.items() if k not in {'hash','automatic_class_c'}};p.update(type=R.TYPE,automatic_first_receipt=True,historical_policies=[],initial_stop=D.digest(stop),initial_intent=D.digest(intent),initial_receipt=D.digest(rec),initial_unit_start=1,initial_unit_end=1);p=R.sealed(p)
    coverage=dict(source_sequence=1,readback=[dict(day=doc['runtime_plan']['from'],rows=0,keys=0,coverage='COMPLETE')],catalog_linkage='PENDING_CURRENT_DAY_DQ')
    result=dict(checkpoint='DONE',sequence=1,state_hash=D.digest(state),persisted_reconciled=True,source_complete=True,lifecycle_changed=False,scheduler_changed=False,coverage_readback=coverage,rows_observed=0,orders=0,supplies=0,bundles=0,coverage='UNPROVEN_PENDING_DQ')
    recon=record(shard,'RECONCILED',1,result)
    q={k:v for k,v in old.items() if k not in {'hash','prior_receipt_hash','prior_recon_hash'}}
    q.update(type=R.TYPE,index=index,shard=shard,stop_hash=D.digest(stop),intent_hash=D.digest(intent),receipt_hash=D.digest(rec),receipt_sequence=1,unit_start=1,unit_end=1,state_hash=D.digest(state),initial_state_hash=D.digest(BF.B.initial(doc['runtime_plan'])),coverage_hash=D.digest(coverage),policy_hash=p['hash'],reconciliation=result,reconciliation_hash=D.digest(recon),predicates=dict.fromkeys(R.PREDICATES,True))
    q=R.sealed(q)
    return m,p,q,root,sh,recon,release,state


def check():
    m,p,q,root,sh,recon,release,state=fixture();R.validate_policy(p,m,release);R.validate(q,m,p,root,sh)
    R.validate(q,m,p,root,sh+[recon],final=True)
    for field in R.PREDICATES:
        bad=deepcopy(q);bad['predicates'][field]=None;bad=R.sealed({k:v for k,v in bad.items() if k!='hash'})
        try:R.validate(bad,m,p,root,sh)
        except BF.B.EvidenceError:pass
        else:raise AssertionError('unknown first predicate accepted')
    try:FL.validate(q,m,p,root,sh)
    except BF.B.EvidenceError:pass
    else:raise AssertionError('first certificate became legacy class C')
    for bad in [dict(sh[0],sequence=0),dict(sh[1],sequence=2),dict(recon,sequence=0)]:
        try:R.validate(q,m,p,root,sh+[bad])
        except BF.B.EvidenceError:pass
        else:raise AssertionError('prior/successor authority accepted')
    return dict(first_receipt_terminal_success='PASS',first_receipt_unknown_blocks='PASS',first_receipt_no_fabricated_predecessor='PASS',source_persistence_not_catalog_completion='PASS',source_budget_zero='PASS')


if __name__=='__main__':
    import json
    print(json.dumps(check(),sort_keys=True))
