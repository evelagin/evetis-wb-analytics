"""Synthetic packaged recovery and monitoring invariants; no network/source calls."""
from copy import deepcopy
from datetime import datetime,timedelta,timezone
from types import SimpleNamespace
from tools.tenancy import controller_dispatch_recovery as R,durable_plan as D,tenant_backfill as BF


def fixture():
    doc=deepcopy(BF.QF.accepted_doc('9b4702a141928eb1f4630ec85b2488838610dace63bd80701a3bf215d7ac1320'));root=BF.QF.ROOT
    def row(kind,payload,seq):return {'version':D.VERSION,'root_hash':root,'kind':kind,'sequence':seq,'payload':payload}
    prior_run={'run_id':'synthetic-prior','lease_generation':1,'ack_hash':doc['ack_hash']}
    run={'run_id':'synthetic-current','lease_generation':2,'ack_hash':doc['ack_hash'],'operation':'projects/mpa-t-client-001/locations/europe-west1/operations/synthetic'}
    previous=row('DISPATCH_RECEIPT',{'plan':doc,'receipt':prior_run},1);prior=row('RECONCILED',{'checkpoint':'RUNNING','sequence':1},1)
    pi=row('DISPATCH_INTENT',{'plan':doc,'preparation':prior_run},1);intent=row('DISPATCH_INTENT',{'plan':doc,'preparation':run},2);receipt=row('DISPATCH_RECEIPT',{'plan':doc,'receipt':run},2)
    base='projects/mpa-t-client-001/locations/europe-west1/jobs/';execution=base+'tenant-backfill-controller/executions/tenant-backfill-controller-synthetic'
    stop=row('STOPPED',{'controller_execution':execution,'reason':'SYNTHETIC_MONITORING'},0)
    recon=row('RECONCILED',{'checkpoint':'RUNNING','sequence':3},2)
    p={'version':R.VERSION,'type':R.TYPE,'tenant':'client_001','project':'mpa-t-client-001','root_hash':root,'stop_hash':D.digest(stop),'intent_hash':D.digest(intent),'receipt_hash':D.digest(receipt),'predecessor_sequence':1,'predecessor_receipt_hash':D.digest(previous),'predecessor_reconciliation_hash':D.digest(prior),'reconciliation_hash':D.digest(recon),'plan_id':doc['runtime_plan']['plan_id'],'state_hash':'a'*64,'state_sequence':3,'unit_start':2,'unit_end':3,'lease_generation':2,'run_id':run['run_id'],'controller_execution':execution,'controller_image':'example/controller@sha256:'+'b'*64,'controller_source_sha':'c'*40,'runtime_execution':base+'ozon-runtime-daily/executions/ozon-runtime-daily-synthetic','runtime_image':'example/runtime@sha256:'+'d'*64,'runtime_source_sha':'e'*40,'implementation_sha':'f'*40,'predicates':dict.fromkeys(R.PREDICATES,True),'evidence_hashes':{'synthetic':'1'*64},'verified_at':'2026-10-08T06:00:00Z'}
    p['authorization']={k:p[k] for k in R.AUTH_FIELDS-{'owner_ack_sha256'}};p['authorization']['owner_ack_sha256']='2'*64;seal(p)
    return p,[pi,previous,prior,intent,receipt,stop],recon


def seal(p):p['hash']=D.digest({k:v for k,v in p.items() if k!='hash'})


def check():
    p,rows,recon=fixture();before=deepcopy(rows)
    R.verify_records(p,rows,p['root_hash'],p['tenant'],publishing=True,final=False)
    try:R.verify_records(p,rows,p['root_hash'],p['tenant'])
    except BF.B.EvidenceError:pass
    else:raise AssertionError('unreconciled receipt silently accepted')
    rows.append(recon);rows.append({'version':D.VERSION,'root_hash':p['root_hash'],'kind':R.KIND,'sequence':2,'payload':p})
    assert D.decide_tick(rows,p['root_hash'],{'status':'ELIGIBLE'},verified_dispatch_recoveries=[p])['sequence']==3
    assert rows[:len(before)]==before
    assert D.decide_tick(rows,p['root_hash'],{'status':'ELIGIBLE'})['action']=='STOPPED'
    for name in R.PREDICATES:
        q=deepcopy(p);q['predicates'][name]=None;seal(q)
        try:R.validate(q)
        except BF.B.EvidenceError:pass
        else:raise AssertionError('unknown predicate accepted')
    extra=deepcopy(rows[-3]);extra['payload']['controller_execution']+='-other'
    assert D.decide_tick(rows+[extra],p['root_hash'],{'status':'ELIGIBLE'},verified_dispatch_recoveries=[p])['action']=='STOPPED'
    from tools.tenancy import cloud_controller as C
    now=datetime(2026,10,8,6,tzinfo=timezone.utc);times=iter([now,now,now+timedelta(seconds=2)])
    doc=before[4]['payload']['plan'];state=BF.B.initial(doc['runtime_plan'])
    b=object.__new__(C.Backend);b.clock=lambda:next(times);b.state=lambda _:state;b.binding_status={};b.active=True
    b.store=SimpleNamespace(history=lambda _: [{'kind':'MANIFEST','payload':{'purpose':'QUALIFICATION','plans':[doc]}}]);b.c=BF.target('client_001')
    b.select=lambda *args:[{'last_success':(now+timedelta(seconds=1)).isoformat(),'last_failure':None,'failed_attempts':0}]
    assert b.monitoring(p['root_hash'],{'status':'MONITORING'})['scopes'][0]['checkpoint_age_seconds']==1
    sent=[];guard=R.SourceFreeBackend(SimpleNamespace(request=lambda *args:sent.append(args)))
    for method,url,body in [('POST','https://api-seller.ozon.ru/v1/supply-order/list',{}),('POST','https://run.googleapis.com/v2/projects/mpa-t-client-001/locations/europe-west1/jobs/ozon-runtime-daily:run',{}),('GET','https://secretmanager.googleapis.com/v1/projects/mpa-t-client-001/secrets/synthetic/versions/latest:access',None)]:
        try:guard.request(method,url,body)
        except BF.B.EvidenceError:pass
        else:raise AssertionError('source authority exposed during recovery')
    assert not sent
    return {'typed_terminal_recovery':'PASS','old_stop_not_bypassed':'PASS','monitoring_read_watermark':'PASS','source_budget_zero':'PASS'}
