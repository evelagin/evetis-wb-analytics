"""Installed-image proof with synthetic authority; never live tenant recovery."""
import copy
from tools.tenancy import controller_stop_recovery as R,durable_plan as D,tenant_backfill as BF


def check():
    doc=copy.deepcopy(BF.QF.accepted_doc(next(p['runtime_plan']['plan_id'] for p in BF.QF.manifest()['plans'] if p['runtime_plan']['entity']=='supplies')))
    root=BF.QF.ROOT
    row=lambda kind,payload,seq=1:{'version':D.VERSION,'root_hash':root,'kind':kind,'sequence':seq,'payload':payload}
    receipt={'run_id':'synthetic-predecessor','lease_generation':1,'ack_hash':doc['ack_hash']}
    base=f"projects/{doc['runtime_plan']['project']}/locations/europe-west1/jobs/"
    stopped=base+'tenant-backfill-controller/executions/tenant-backfill-controller-synthetic'
    rows=[row('DISPATCH_INTENT',{'plan':doc,'preparation':receipt}),row('DISPATCH_RECEIPT',{'plan':doc,'receipt':receipt}),row('RECONCILED',{'sequence':1}),row('STOPPED',{'controller_execution':stopped,'reason':'synthetic'},0)]
    proof={'version':R.VERSION,'type':R.TYPE,'tenant':doc['tenant_id'],'project':doc['runtime_plan']['project'],'root_hash':root,'stop_hash':D.digest(rows[3]),'predecessor_sequence':1,'receipt_hash':D.digest(rows[1]),'reconciliation_hash':D.digest(rows[2]),'plan_id':doc['runtime_plan']['plan_id'],'state_hash':'1'*64,'state_sequence':1,'controller_execution':stopped,'controller_image':'synthetic/controller@sha256:'+'2'*64,'controller_source_sha':'3'*40,'runtime_execution':base+'ozon-runtime-daily/executions/ozon-runtime-daily-synthetic','runtime_image':'synthetic/runtime@sha256:'+'4'*64,'runtime_source_sha':'5'*40,'implementation_sha':'6'*40,'predicates':dict.fromkeys(R.PREDICATES,True),'evidence_hashes':{'synthetic':'7'*64},'verified_at':'2026-10-08T00:00:00Z'}
    proof['authorization']={k:proof[k] for k in ('tenant','project','root_hash','receipt_hash','reconciliation_hash','plan_id','type','version')}
    proof['authorization'].update(owner_ack_sha256='8'*64,stop_hashes=[proof['stop_hash']])
    def seal(p):p['hash']=D.digest({k:v for k,v in p.items() if k!='hash'})
    seal(proof);original=copy.deepcopy(rows)
    R.verify_records(proof,rows,root,doc['tenant_id'],publishing=True)
    rows.append(row(R.KIND,proof))
    assert D.decide_tick(rows,root,{'status':'ELIGIBLE'},verified_controller_recoveries=[proof])['sequence']==2
    assert rows[:4]==original
    assert D.decide_tick(rows,root,{'status':'ELIGIBLE'})['action']=='STOPPED'
    for name in R.PREDICATES:
        bad=copy.deepcopy(proof);bad['predicates'][name]=None;seal(bad)
        try:R.validate(bad)
        except BF.B.EvidenceError:pass
        else:raise AssertionError('unknown predicate authorized recovery')
    successor=row('DISPATCH_INTENT',{'synthetic':True},2)
    try:R.verify_records(proof,rows+[successor],root,doc['tenant_id'],publishing=True)
    except BF.B.EvidenceError:pass
    else:raise AssertionError('successor authority ignored')
    extra=row('STOPPED',{'controller_execution':stopped+'-later','reason':'synthetic'},0)
    assert D.decide_tick(rows+[extra],root,{'status':'ELIGIBLE'},verified_controller_recoveries=[proof])['action']=='STOPPED'
    return 'PASS'
