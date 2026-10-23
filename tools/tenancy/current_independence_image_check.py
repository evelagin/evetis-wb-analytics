"""Network-free in-image proof of S4 (2026-10-23): CURRENT independence from a FULL-local historical STOP.

  * STOP classification: only the closed FULL-leaf classes are LOCAL; untyped, unknown-write and
    shared-stage STOPs are GLOBAL;
  * admission needs every shared authority settled: released pilot lease by its exact owner (or a lease
    held by one receipted CURRENT run), no open historical intent, no operator hold, every ordinary
    Scheduler PAUSED. Lease expiry is never evidence;
  * the wake gate keeps acceptance and automatic recovery unchanged and opens only CURRENT;
  * CURRENT selects new work only for catalog, stocks, prices and finance_accrual, still drains every
    previously elected intent, and a PARTIAL entity does not stop the others.
Every patched seam is restored before returning.
"""
from contextlib import contextmanager
from copy import deepcopy
from datetime import date,datetime,timezone
from types import SimpleNamespace
from tools.tenancy import current_independence as CI,durable_plan as D,tenant_backfill as BF,full_history as F
from pipelines.ozon.runtime import full_resume as FR

NOW=datetime(2026,10,23,9,tzinfo=timezone.utc)
SHARED_STAGES=('LEASE','CHECKPOINT','SOURCE_DISPATCH_BOUNDARY','CONTROL_CLOSURE','BINDING','QUOTA','MANIFEST_ROOT','MONITORING_WATERMARK')


@contextmanager
def patched(obj,name,value):
    saved=getattr(obj,name);setattr(obj,name,value)
    try:yield
    finally:setattr(obj,name,saved)


def rec(root,kind,sq,p):return dict(version=D.VERSION,root_hash=root,kind=kind,sequence=sq,payload=p)


def leaves():
    m=FR.manifest();day=NOW.astimezone(BF.B.MSK).date()
    plans={}
    for i in (465,10):
        doc=F.render_leaf(m,i,day);plans[i]={'index':i,'plan':doc,'shard_root':F.shard_root(m,i,doc)}
    return m,plans


def leaf_stop(m,i):return rec(m['hash'],'STOPPED',0,{'reason':'FULL_DISPATCH_OR_REPORT_EVIDENCE_UNPROVEN','index':i})


def typed_stop(m,stage,context=None,readback=None):
    d={'version':1,'stage':stage,'category':'EVIDENCE'}
    if context is not None:d['context']=context
    if readback is not None:d['readback']=readback
    return rec(m['hash'],'STOPPED',0,{'reason':'CONTROLLER_GATE_OR_EVIDENCE_FAILURE','controller_execution':'projects/x/executions/e1','diagnostic':d})


def context(plans,i):return {'index':i,'shard':plans[i]['shard_root'],'sequence':3,'intent_hash':'1'*64,'receipt_hash':'2'*64}


def classify_check():
    m,plans=leaves()
    assert CI.classify(leaf_stop(m,465),m,plans)==('LOCAL',465)
    assert CI.classify(typed_stop(m,'RUNTIME_TERMINAL',context(plans,465)),m,plans)==('LOCAL',465)
    assert CI.classify(typed_stop(m,'RECONCILIATION_APPEND',context(plans,10),dict.fromkeys(('lease','checkpoint','intent','run','receipt'),'ABSENT')),m,plans)==('LOCAL',10)
    untyped=rec(m['hash'],'STOPPED',0,{'reason':'CONTROLLER_GATE_OR_EVIDENCE_FAILURE','controller_execution':'projects/x/executions/qhv6d'})
    globals_=[untyped,leaf_stop(m,999),leaf_stop(m,11),typed_stop(m,'RUNTIME_TERMINAL'),
              typed_stop(m,'RUNTIME_TERMINAL',context(plans,465),dict(lease='PRESENT',checkpoint='PRESENT',intent='PRESENT',run='UNKNOWN',receipt='ABSENT')),
              typed_stop(m,'PERSISTED_SOURCE_RECONCILIATION',dict(context(plans,465),shard='e'*64)),
              rec('f'*64,'STOPPED',0,{'reason':'FULL_DISPATCH_OR_REPORT_EVIDENCE_UNPROVEN','index':465}),
              rec(m['hash'],'STOPPED',0,{'reason':'DISPATCH_RECEIPT_UNPROVEN'})]
    globals_+=[typed_stop(m,s,context(plans,465)) for s in SHARED_STAGES]
    for stop in globals_:
        assert CI.classify(stop,m,plans)[0]=='GLOBAL',stop['payload']
    return 'PASS'


def fake(m,*,ledger_rows,lease_tables,releases,schedulers=None,hold=False,pending=(),current=False):
    c=BF.target('client_001');cid=BF.B.digest(['BOUNDED_PILOT_EXCLUSIVE',c['project_id']])[:16]
    objects={f'L_{cid}_{g:04d}':({'owner':o,'until':'1'},'{}') for g,o in lease_tables.items()}
    objects.update({f'LD_{cid}_{g:04d}':({'owner':o},'{}') for g,o in releases.items()})
    sched=schedulers or {'tenant-backfill-tick':'PAUSED','ozon-daily':'PAUSED','ozon-fast':'PAUSED','ozon-weekly':'PAUSED'}
    base,_=BF.resources(c)
    def request(method,url,body=None):
        assert method=='GET' and url==BF.SCHED_API+'/'+base+'/jobs?pageSize=100','only the Scheduler list is read'
        return {'jobs':[{'name':base+'/jobs/'+n,'state':s} for n,s in sched.items()]}
    b=SimpleNamespace(c=c,clock=lambda:NOW,request=request,tables=SimpleNamespace(list_tables=lambda ds,**kw:[(n,v[0],NOW) for n,v in objects.items()],get_table=lambda ds,n:objects.get(n)))
    rows=[{'evidence_json':'{"mode": "BOUNDED_PILOT"}','lease_generation':g,'run_id':o} for g,o in ledger_rows]+[{'evidence_json':'{"kind": "STOPPED"}','lease_generation':None,'run_id':None}]
    return b,rows,hold,list(pending),current


def admit(m,plans,unresolved,setup):
    from tools.tenancy import recent_priority as RP
    b,rows,hold,pending,current=setup
    with patched(CI,'ledger',lambda _b:(rows,hold)),patched(RP,'pending_receipts',lambda _b,_c:pending),patched(CI,'current_receipt',lambda _b,run,g:current):
        return CI.admit(b,m,[],unresolved,plans,{},set())


def admission_check():
    m,plans=leaves();local=[leaf_stop(m,465)]
    settled=dict(ledger_rows=[(344,'bf-a'),(345,'bf-b'),(345,'bf-b')],lease_tables={345:'bf-b'},releases={344:'bf-a',345:'bf-b'})
    ok=admit(m,plans,local,fake(m,**settled))
    assert ok['admitted'] is True and ok['local_full_leaves']==[465] and ok['lease']=='RELEASED',ok
    # lease tables expire after 7 days: the durable ledger + release marker still prove release
    assert admit(m,plans,local,fake(m,**dict(settled,lease_tables={})))['admitted'] is True
    cases={
        'global STOP among locals':(local+[rec(m['hash'],'STOPPED',0,{'reason':'CONTROLLER_GATE_OR_EVIDENCE_FAILURE','controller_execution':'x'})],dict(settled),'GLOBAL_STOP'),
        'open historical intent':(local,dict(settled,pending=[(465,{})]),'HISTORICAL_AUTHORITY_OPEN'),
        'operator hold':(local,dict(settled,hold=True),'OPERATOR_HOLD'),
        'unreleased newest lease (unknown POST)':(local,dict(settled,lease_tables={345:'bf-b',346:'bf-c'}),'LEASE_HELD_WITHOUT_CURRENT_RECEIPT'),
        'expired unreleased lease is not evidence':(local,dict(settled,ledger_rows=[(346,'bf-c')],lease_tables={},releases={345:'bf-b'}),'LEASE_HELD_WITHOUT_CURRENT_RECEIPT'),
        'release by a different owner':(local,dict(settled,releases={345:'bf-x'}),'LEASE_release marker owner differs'),
        'ambiguous lease owner':(local,dict(settled,ledger_rows=[(345,'bf-b'),(345,'bf-z')]),'LEASE_lease owner ambiguous'),
        'no lease evidence':(local,dict(settled,ledger_rows=[],lease_tables={},releases={}),'LEASE_no pilot lease evidence'),
        'ordinary Scheduler enabled':(local,dict(settled,schedulers={'tenant-backfill-tick':'ENABLED','ozon-daily':'ENABLED','ozon-fast':'PAUSED','ozon-weekly':'PAUSED'}),'ORDINARY_SCHEDULER_NOT_PAUSED'),
    }
    for name,(stops,setup,blocker) in cases.items():
        got=admit(m,plans,stops,fake(m,**setup))
        assert got['admitted'] is False and got['blocker'].startswith(blocker),(name,got)
    held=dict(settled,lease_tables={345:'bf-b',346:'bf-cur'},current=True)
    assert admit(m,plans,local,fake(m,**held))['lease']=='HELD'                       # drained by this wake first
    return 'PASS'


def gate_check():
    from tools.tenancy import full_controller as H,stop_acceptance as SA,full_leaf_recovery as FL,full_first_receipt_recovery as FRR,controller_cadence as CC
    m,plans=leaves();stop=leaf_stop(m,465);calls=[]
    b=SimpleNamespace(clock=lambda:NOW)
    def run(records,admission,approved=()):
        with patched(SA,'approved',lambda *a,**k:[{'stop_hash':h} for h in approved]),patched(CC,'enabled',lambda _b:False),\
             patched(FL,'recover_pending',lambda *a:calls.append('FL')),patched(FRR,'recover_pending',lambda *a:calls.append('FR')),\
             patched(CI,'admit',lambda *a:calls.append('admit') or admission),patched(H,'reconstruct_plans',lambda *a:plans),patched(H,'completed',lambda *a:{}):
            return H.historical_gate(b,m,records,[],set())
    assert run([rec(m['hash'],'FULL_MANIFEST',0,m)],None)==(None,None) and not calls          # no STOP: FULL unchanged
    assert run([stop],None,approved=[D.digest(stop)])==(None,None) and not calls               # accepted STOP: unchanged
    ended,adm=run([stop],{'admitted':False,'blocker':'X'})
    assert ended=={'status':'STOPPED','source_dispatches':0} and adm is None and calls==['admit']
    calls.clear();ended,adm=run([stop],{'admitted':True,'local_full_leaves':[465]})
    assert ended is None and adm['admitted'] is True
    calls.clear();typed=typed_stop(m,'RUNTIME_TERMINAL',context(plans,465))
    run([typed],{'admitted':True,'local_full_leaves':[465]})
    assert calls[:2]==['FR','FL'] and calls[-1]=='admit'                                        # automatic recovery still first
    return 'PASS'


def selection_check():
    from tools.tenancy import current_controller as CUR,current_policy as P,controller_cadence as CC,recent_priority as RP,fbo_diagnostic_owner as DIAG
    day=NOW.astimezone(BF.B.MSK).date();p={'hash':'a'*64,'first_day':'2026-10-14','fbo_contract_hash':None}
    def item(e):return {'day':day.isoformat(),'policy_hash':p['hash'],'plan':{'ack_hash':e,'runtime_plan':{'entity':e}},'shard':'s-'+e}
    def run(certified=(),partial=(),pending=None):
        ticks=[];items={e:item(e) for e in P.ORDER if e!='fbo_postings'}
        records=[{'kind':'CURRENT_CERT','payload':{'fact':{'entity':e}}} for e in certified]
        records+=[{'kind':'CURRENT_PARTIAL','payload':{'entity':e,'policy_hash':p['hash'],'ack_hash':e,'state_hash':D.digest({'progress':{}}),'status':'PARTIAL','reason':'SOURCE_RETENTION_COMPLETENESS_UNPROVEN'}} for e in partial]
        def history(shard,**k):
            if pending and shard=='s-'+pending:return [{'kind':'DISPATCH_INTENT','sequence':1}]
            return []
        base=SimpleNamespace(clock=lambda:NOW,store=SimpleNamespace(history=history,commit=lambda *a:'h'),state=lambda d:{'progress':{}})
        def tick(b,pp,d,it):
            ticks.append(it['plan']['runtime_plan']['entity']);return {'status':'WAITING','source_dispatches':0}
        with patched(CUR,'policy',lambda b:p),patched(CC,'ensure',lambda b:None),patched(RP,'pending_receipts',lambda *a:[]),\
             patched(CUR,'cycles',lambda b,pp:{day.isoformat():{}}),patched(CUR,'plans',lambda b,pp,d:(records,items)),\
             patched(CUR,'validate_certificate',lambda *a:a[3]),patched(CUR,'Backend',lambda *a:None),patched(CUR,'source_tick',tick),\
             patched(DIAG,'queued',lambda b:None):
            result=CUR.wake(base,[],{},{},set())
        assert result is None if not pending else result['status']=='WAITING'
        return ticks
    assert run()==['catalog']                                                            # catalog first, as before
    assert run(certified=['catalog'])==['stocks','prices','finance_accrual']             # no advertising, no FBO
    assert run(certified=['catalog'],partial=['prices'])==['stocks','finance_accrual']    # a PARTIAL entity does not stop others
    assert run(certified=['catalog'],pending='ads_campaigns')==['ads_campaigns']          # an elected ads intent is still drained first
    assert CUR.INDEPENDENT_ENTITIES==('catalog','stocks','prices','finance_accrual')
    return 'PASS'


def check():
    for name,fn in (('classify',classify_check),('admission',admission_check),('gate',gate_check),('selection',selection_check)):
        assert fn()=='PASS',name
    return {'current_independence_adapter':'PASS'}


if __name__=='__main__':
    import json
    print(json.dumps(check(),sort_keys=True))
