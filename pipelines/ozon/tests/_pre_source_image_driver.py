"""Installed image, zero network/secrets: pre-intent failure and positive immutable recovery."""
import copy,json,sys
sys.path.insert(0,'/app')
import common as C,backfill as F,backfill_core as B,pre_source as P,qualification as Q

def qualify():
    denied=lambda *a,**kw:(_ for _ in ()).throw(AssertionError('real network/secret'))
    C.secret=denied;C.perf_token=denied;C.bq=denied;C.perf_post=denied
    p=Q.accepted_doc(Q.SKU)['runtime_plan'];s=B.initial(p);s.update(sequence=14,rows=30)
    s['progress'].update(pending=[str(i) for i in range(31,91)],report=None,skipped=0);writes=[]
    e=F.Engine(p,'retry','safe',s,unit_budget=3)
    e.before_intent=lambda:(_ for _ in ()).throw(B.EvidenceError('CAPABILITY_PROFILE denied'))
    try:e.run(lambda *a:writes.append(a))
    except B.EvidenceError as ex:assert 'CAPABILITY_PROFILE' in str(ex)
    else:raise AssertionError('missing permission accepted')
    assert not writes
    s['sequence']=15;s['progress']['report']={'phase':'INTENT','batch':[str(i) for i in range(31,41)],'execution':'failed',
        'cohort_hash':Q.cohort(p,p['from'],[str(i) for i in range(31,41)])}
    proof={'version':P.VERSION,'root_hash':'1'*64,'tenant':'client_001','project':p['project'],'plan_id':p['plan_id'],
        'dispatch_sequence':6,'run_id':'failed','execution':'failed-execution','image':'europe-west1-docker.pkg.dev/mpa-platform/mpa-runtime/ozon-runtime@sha256:'+'2'*64,
        'source_sha':'3'*40,'lease_generation':6,'intent_hash':'4'*64,'receipt_hash':'5'*64,'stop_hashes':['6'*64],
        'unit_sequence':15,'state_hash':B.digest(s),'cohort_hash':s['progress']['report']['cohort_hash'],'exports_reserved':10,
        'query_id':'query','failure_stage':'CAPABILITY_PROFILE_ACCESS_DENIED_BEFORE_REPORT_POST','verified_at':'2026-10-07T08:00:00Z',
        'post_attempts':0,'uuid_present':False,'sku_rows_written':0};proof['hash']=B.digest(proof)
    old=copy.deepcopy(s);e=F.Engine(p,'retry','safe',s,unit_budget=1);e.before_intent=lambda:None;e.recovery_reader=lambda *a:proof
    out=e.run(lambda r,ev:writes.append(copy.deepcopy(ev)))
    assert writes[0]['detail']['action']=='PRE_SOURCE_RECOVERED' and writes[0]['state']['sequence']==16
    assert writes[0]['state']['rows']==30 and writes[0]['state']['progress']['pending']==old['progress']['pending']
    assert out['evidence']['state']['progress']['report'] is None and old['sequence']==15
    ambiguous=copy.deepcopy(proof);ambiguous['post_attempts']=1;ambiguous['hash']=B.digest({k:v for k,v in ambiguous.items() if k!='hash'})
    try:P.recover(ambiguous,p,old)
    except B.EvidenceError:pass
    else:raise AssertionError('ambiguous POST released')
    return {'pre_source_recovery':'PASS','missing_permission_before_intent':'PASS','failed15_preserved_new16':'PASS','ambiguous_post_blocked':'PASS',
        'network':'NONE','credential_reads':0,'backfill_implementation_hash':B.implementation_hash()}

if __name__=='__main__':print(json.dumps(qualify(),sort_keys=True))
