"""Network-free in-image proof of S4 (2026-10-23): CURRENT independence from a FULL-local historical STOP.

  * STOP classification: only the closed FULL-leaf classes are LOCAL; untyped, unknown-write and
    shared-stage STOPs are GLOBAL;
  * admission needs every shared authority settled: released pilot lease by its exact owner (or a lease
    held by one receipted CURRENT run), no open historical intent, no operator hold, every ordinary
    Scheduler PAUSED. Lease expiry is never evidence;
  * the wake gate keeps acceptance and automatic recovery unchanged and opens only CURRENT;
  * CURRENT selects new work only for catalog, stocks, prices and finance_accrual, still drains every
    previously elected intent, and a PARTIAL entity does not stop the others.
  * R0/S4: an admitted wake never touches the owner FBO diagnostic token (no BFDC, RunJob, POST, STOP).
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
    assert CI.classify(leaf_stop(m,465),m)==('CANDIDATE',465)
    assert CI.classify(typed_stop(m,'RUNTIME_TERMINAL',context(plans,465)),m)==('CANDIDATE',465)
    assert CI.classify(typed_stop(m,'RECONCILIATION_APPEND',context(plans,10),dict.fromkeys(('lease','checkpoint','intent','run','receipt'),'ABSENT')),m)==('CANDIDATE',10)
    untyped=rec(m['hash'],'STOPPED',0,{'reason':'CONTROLLER_GATE_OR_EVIDENCE_FAILURE','controller_execution':'projects/x/executions/qhv6d'})
    bool_version=typed_stop(m,'RUNTIME_TERMINAL',context(plans,465));bool_version['payload']['diagnostic']['version']=True
    globals_=[untyped,leaf_stop(m,999),typed_stop(m,'RUNTIME_TERMINAL'),bool_version,
              typed_stop(m,'RUNTIME_TERMINAL',context(plans,465),dict(lease='PRESENT',checkpoint='PRESENT',intent='PRESENT',run='UNKNOWN',receipt='ABSENT')),
              rec('f'*64,'STOPPED',0,{'reason':'FULL_DISPATCH_OR_REPORT_EVIDENCE_UNPROVEN','index':465}),
              rec(m['hash'],'STOPPED',0,{'reason':'DISPATCH_RECEIPT_UNPROVEN'})]
    globals_+=[typed_stop(m,s_,context(plans,465)) for s_ in SHARED_STAGES]
    for stop in globals_:
        assert CI.classify(stop,m)[0]=='GLOBAL',stop['payload']
    return 'PASS'


def shard_series(shard,n,*,reconciled=True,extra=()):
    out=[]
    for q in range(1,n+1):
        out.append(rec(shard,'DISPATCH_INTENT',q,{}));out.append(rec(shard,'DISPATCH_RECEIPT',q,{}))
        if reconciled or q<n:out.append(rec(shard,'RECONCILED',q,{}))
    return out+[rec(shard,k,0,{}) for k in extra]


def fake(m,*,ledger_rows,lease_tables,releases,schedulers=None,hold=False,pending=(),current=False,shards=None,report=None):
    c=BF.target('client_001');cid=BF.B.digest(['BOUNDED_PILOT_EXCLUSIVE',c['project_id']])[:16]
    objects={f'L_{cid}_{g:04d}':({'owner':o,'until':'1'},'{}') for g,o in lease_tables.items()}
    objects.update({f'LD_{cid}_{g:04d}':({'owner':o},'{}') for g,o in releases.items()})
    sched=schedulers or {'tenant-backfill-tick':'PAUSED','ozon-daily':'PAUSED','ozon-fast':'PAUSED','ozon-weekly':'PAUSED'}
    base,_=BF.resources(c)
    def request(method,url,body=None):
        assert method=='GET' and url==BF.SCHED_API+'/'+base+'/jobs?pageSize=100','only the Scheduler list is read'
        return {'jobs':[{'name':base+'/jobs/'+n,'state':s} for n,s in sched.items()]}
    shards=shards or {}
    b=SimpleNamespace(c=c,clock=lambda:NOW,request=request,tables=SimpleNamespace(list_tables=lambda ds,**kw:[(n,v[0],NOW) for n,v in objects.items()],get_table=lambda ds,n:objects.get(n)),
        store=SimpleNamespace(history=lambda shard,**kw:deepcopy(shards.get(shard,[]))),state=lambda doc:{'progress':({'report':report} if report else {})})
    rows=[{'evidence_json':'{"mode": "BOUNDED_PILOT"}','lease_generation':g,'run_id':o} for g,o in ledger_rows]+[{'evidence_json':'{"kind": "STOPPED"}','lease_generation':None,'run_id':None}]
    return b,rows,hold,list(pending),current


def admit(m,plans,unresolved,setup,records=None):
    from tools.tenancy import recent_priority as RP,full_controller as H
    b,rows,hold,pending,current=setup
    with patched(CI,'ledger',lambda _b:(rows,hold)),patched(RP,'pending_receipts',lambda _b,_c:pending),patched(CI,'current_receipt',lambda _b,run,g:current),\
         patched(H,'reconstruct_plans',lambda *a:plans),patched(H,'completed',lambda *a:{}):
        return CI.admit(b,m,records or [],unresolved,set())


def admission_check():
    m,plans=leaves();local=[leaf_stop(m,465)];sh=plans[465]['shard_root']
    settled=dict(ledger_rows=[(344,'bf-a'),(345,'bf-b'),(345,'bf-b')],lease_tables={345:'bf-b'},releases={344:'bf-a',345:'bf-b'},shards={sh:shard_series(sh,3)})
    ok=admit(m,plans,local,fake(m,**settled))
    assert ok['admitted'] is True and ok['local_full_leaves']==[465] and ok['lease']=='RELEASED',ok
    # lease tables expire after 7 days: the durable ledger + release marker still prove release
    assert admit(m,plans,local,fake(m,**dict(settled,lease_tables={})))['admitted'] is True
    assert admit(m,plans,local,fake(m,**dict(settled,report={'phase':'POLL'})))['admitted'] is True
    typed=[typed_stop(m,'RECONCILIATION_APPEND',context(plans,465))]
    assert admit(m,plans,typed,fake(m,**settled))['admitted'] is True
    cases={
        'global STOP among locals':(local+[rec(m['hash'],'STOPPED',0,{'reason':'CONTROLLER_GATE_OR_EVIDENCE_FAILURE','controller_execution':'x'})],dict(settled),'GLOBAL_STOP: untyped'),
        'leaf intent without receipt (unknown Run POST)':(local,dict(settled,shards={sh:shard_series(sh,3)[:-2]}),'GLOBAL_STOP: leaf intent/receipt not reconciled'),
        'leaf receipt not reconciled':(local,dict(settled,shards={sh:shard_series(sh,3,reconciled=False)}),'GLOBAL_STOP: leaf intent/receipt not reconciled'),
        'leaf shard STOP':(local,dict(settled,shards={sh:shard_series(sh,2,extra=('STOPPED',))}),'GLOBAL_STOP: leaf shard holds'),
        'leaf closure record':(local,dict(settled,shards={sh:shard_series(sh,2,extra=('DISPATCH_ZERO_EFFECT_FAILED_CLOSED',))}),'GLOBAL_STOP: leaf shard holds'),
        'ambiguous async report':(local,dict(settled,report={'phase':'INTENT'}),'GLOBAL_STOP: leaf asynchronous report'),
        'diagnostic of another shard':([typed_stop(m,'RECONCILIATION_APPEND',dict(context(plans,465),shard='e'*64))],dict(settled),'GLOBAL_STOP: diagnostic shard'),
        'leaf without plan':([leaf_stop(m,11)],dict(settled),'GLOBAL_STOP: leaf plan absent'),
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
    # an untyped STOP is decided from the records alone: no read at all
    def no_read(*a,**k):raise AssertionError('read before classification')
    b=SimpleNamespace(store=SimpleNamespace(history=no_read),tables=SimpleNamespace(list_tables=no_read,get_table=no_read),request=no_read,clock=lambda:NOW)
    from tools.tenancy import full_controller as H
    with patched(H,'reconstruct_plans',no_read),patched(CI,'ledger',no_read):
        got=CI.admit(b,m,[],[rec(m['hash'],'STOPPED',0,{'reason':'CONTROLLER_GATE_OR_EVIDENCE_FAILURE','controller_execution':'x'})],set())
    assert got=={'admitted':False,'blocker':'GLOBAL_STOP: untyped or non-leaf controller STOP'}
    return 'PASS'


def evidence_readers_check():
    """The real ledger reader and CURRENT receipt reader (not patched)."""
    from tools.tenancy import current_controller as CUR
    rows=[{'lease_generation':1}];seen=[]
    with patched(CI.TL,'read_state',lambda c,t:seen.append(1) or ('chain','decisions',rows,'ops',False)):
        assert CI.ledger(SimpleNamespace(c={},tables=None))==(rows,False) and seen
    day='2026-10-23';p={'hash':'a'*64}
    def items(*specs):return {e:{'shard':'s-'+e} for e,_ in specs}
    from tools.tenancy import finance_catchup as FC
    def run(specs,catchup=()):
        hist={'s-'+e:h for e,h in specs}
        b=SimpleNamespace(store=SimpleNamespace(history=lambda shard,**k:hist[shard]))
        with patched(CUR,'policy',lambda _b:p),patched(CUR,'cycles',lambda _b,_p:{day:{}}),patched(CUR,'plans',lambda _b,_p,_d:([],items(*specs))),\
             patched(FC,'receipts',lambda _b,_p:list(catchup)):
            return CI.current_receipt(b,'bf-cur',346)
    def series(run_id='bf-cur',g=346,reconciled=False):
        h=[rec('s','DISPATCH_INTENT',1,{}),rec('s','DISPATCH_RECEIPT',1,{'receipt':{'run_id':run_id,'lease_generation':g}})]
        return h+([rec('s','RECONCILED',1,{})] if reconciled else [])
    assert run([('catalog',series())]) is True
    assert run([('catalog',series()),('stocks',series())]) is False                 # two shards claim the lease
    assert run([('catalog',series(reconciled=True))]) is False                      # reconciled: lease should be released
    assert run([('catalog',series(g=345))]) is False and run([('catalog',series(run_id='bf-x'))]) is False
    assert run([('catalog',[rec('s','DISPATCH_INTENT',1,{})])]) is False             # intent without receipt
    held={'run_id':'bf-cur','lease_generation':346}
    assert run([],catchup=[held]) is True                                            # open finance catch-up day holds it
    assert run([('catalog',series())],catchup=[held]) is False                       # never two holders
    assert run([],catchup=[dict(held,lease_generation=345)]) is False
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


def wake_check():
    """Admitted mode: CURRENT only; no FULL drain, leaf or dispatch, no T5 repair; result names the local STOP."""
    from tools.tenancy import full_controller as H,full_fbo_diagnostic_closure as LQ,controller_cadence as CC,current_controller as CUR,recent_priority as RP
    m,plans=leaves();calls=[]
    base=SimpleNamespace(store=SimpleNamespace(history=lambda root:[rec(m['hash'],'FULL_MANIFEST',0,m),leaf_stop(m,465)]))
    def forbidden(name):
        def f(*a,**k):raise AssertionError(name+' in admitted mode')
        return f
    def run(admission,current):
        with patched(H,'only_manifest',lambda r:m),patched(H,'Backend',lambda b,mm:SimpleNamespace(clock=lambda:NOW,store=base.store)),\
             patched(H,'verify_authority',lambda *a:None),patched(CC,'acquire',lambda b:None),patched(H,'historical_gate',lambda *a:(None,admission)),\
             patched(H,'completed',lambda *a:{}),patched(H,'reconstruct_plans',lambda *a:plans),patched(H,'repair_t5_completions',lambda *a:calls.append('repair')),\
             patched(CUR,'wake',lambda *a:current),patched(RP,'load',forbidden('FULL queue')),patched(H,'leaf_wake',forbidden('leaf_wake')),patched(H,'_dispatch',forbidden('dispatch')):
            return H.wake(base,m['hash'])[0]
    adm={'admitted':True,'local_full_leaves':[465]}
    assert run(adm,None)=={'status':'MONITORING','source_dispatches':0,'basis':'FULL_HISTORY_LOCAL_STOP','local_full_leaves':[465]} and not calls
    assert run(adm,{'status':'DISPATCHED','source_dispatches':1})=={'status':'DISPATCHED','source_dispatches':1} and not calls
    return 'PASS'


def selection_check():
    from tools.tenancy import current_controller as CUR,current_policy as P,controller_cadence as CC,recent_priority as RP,fbo_diagnostic_owner as DIAG,finance_catchup as FC
    day=NOW.astimezone(BF.B.MSK).date();p={'hash':'a'*64,'first_day':'2026-10-14','fbo_contract_hash':None}
    def item(e):return {'day':day.isoformat(),'policy_hash':p['hash'],'plan':{'ack_hash':e,'runtime_plan':{'entity':e,'from':'2026-10-22'}},'shard':'s-'+e}
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
             patched(DIAG,'queued',lambda b:None),patched(FC,'scan',lambda *a:{'pending':[]}),patched(FC,'wake',lambda *a:None):
            result=CUR.wake(base,[],{},{},set())
        assert result is None if not pending else result['status']=='WAITING'
        return ticks
    assert run()==['catalog']                                                            # catalog first, as before
    assert run(certified=['catalog'])==['stocks','prices','finance_accrual']             # no advertising, no FBO
    assert run(certified=['catalog'],partial=['prices'])==['stocks','finance_accrual']    # a PARTIAL entity does not stop others
    assert run(certified=['catalog'],pending='ads_campaigns')==['ads_campaigns']          # an elected ads intent is still drained first
    assert CUR.INDEPENDENT_ENTITIES==('catalog','stocks','prices','finance_accrual')
    return 'PASS'


def diagnostic_token_check():
    """R0/S4: an admitted wake (FULL-local STOP unresolved) never touches the owner FBO diagnostic token.

    No BFDQUEUE/BFDA read, no BFDC claim, no RunJob/source POST and no new STOP; CURRENT continues.
    Without a local STOP the queue is consulted exactly as before."""
    from tools.tenancy import current_controller as CUR,current_policy as P,controller_cadence as CC,recent_priority as RP,fbo_diagnostic_owner as DIAG,finance_catchup as FC
    from tools.tenancy import full_controller as H,stop_acceptance as SA,full_leaf_recovery as FL,full_first_receipt_recovery as FRR
    day=NOW.astimezone(BF.B.MSK).date();p={'hash':'a'*64,'first_day':'2026-10-14','fbo_contract_hash':None}
    writes=[];reads=[];ticks=[]
    tables=SimpleNamespace(get_table=lambda ds,n:reads.append(n) or (({'kind':'fbo_diagnostic_queue'},D.encoded({'policy_hash':'d'*64})) if n.startswith('BFDQUEUE_') else None),
                           create_marker=lambda *a:writes.append(a) or True)
    c=BF.target('client_001')
    def run(admission):
        reads.clear();writes.clear();ticks.clear();commits=[]
        items={e:{'day':day.isoformat(),'policy_hash':p['hash'],'plan':{'ack_hash':e,'runtime_plan':{'entity':e,'from':'2026-10-22'}},'shard':'s-'+e} for e in CUR.INDEPENDENT_ENTITIES}
        records=[{'kind':'CURRENT_CERT','payload':{'fact':{'entity':'catalog'}}}]
        base=SimpleNamespace(c=c,tables=tables,clock=lambda:NOW,wake_activity={},current_execution='x',
            store=SimpleNamespace(history=lambda shard,**k:[],commit=lambda *a:commits.append(a) or 'h'),state=lambda d:{'progress':{}})
        if admission is not None:base.current_independence=admission
        with patched(CUR,'policy',lambda b:p),patched(CC,'ensure',lambda b:None),patched(RP,'pending_receipts',lambda *a:[]),\
             patched(CUR,'cycles',lambda b,pp:{day.isoformat():{}}),patched(CUR,'plans',lambda b,pp,d:(records,items)),\
             patched(CUR,'validate_certificate',lambda *a:a[3]),patched(CUR,'Backend',lambda *a:None),\
             patched(CUR,'source_tick',lambda b,pp,d,it:ticks.append(it['plan']['runtime_plan']['entity']) or {'status':'WAITING','source_dispatches':0}),\
             patched(DIAG,'policy',lambda *a,**k:(_ for _ in ()).throw(AssertionError('diagnostic permission read'))),\
             patched(DIAG,'dispatch',lambda *a:(_ for _ in ()).throw(AssertionError('diagnostic dispatched'))),\
             patched(FC,'scan',lambda *a:{'pending':[]}),patched(FC,'wake',lambda *a:None):
            try:result=CUR.wake(base,[],{},{},set())
            except AssertionError as e:result={'refused':str(e)}
        return result,commits
    result,commits=run({'admitted':True,'local_full_leaves':[465]})
    assert result is None and not writes and not commits and not any(n.startswith(('BFDQUEUE_','BFDA_','BFDC_')) for n in reads),(result,reads)
    assert ticks==['stocks','prices','finance_accrual']                                   # CURRENT continues
    # No local STOP (no admission, or an ended one): the token path is unchanged and reaches the owner permission.
    for admission in (None,{'admitted':False,'blocker':'X'}):
        result,commits=run(admission)
        assert result=={'refused':'diagnostic permission read'} and any(n.startswith('BFDQUEUE_') for n in reads) and not writes and not ticks
    # The S4 gate is what marks the wake: historical_gate stores the admission on the wake's backend.
    m,plans=leaves();stop=leaf_stop(m,465);b=SimpleNamespace(clock=lambda:NOW)
    with patched(SA,'approved',lambda *a,**k:[]),patched(CC,'enabled',lambda _b:False),patched(FL,'recover_pending',lambda *a:None),\
         patched(FRR,'recover_pending',lambda *a:None),patched(CI,'admit',lambda *a:{'admitted':True,'local_full_leaves':[465]}),\
         patched(H,'reconstruct_plans',lambda *a:plans),patched(H,'completed',lambda *a:{}):
        H.historical_gate(b,m,[stop],[],set())
    assert CUR.local_full_stop(b) is True and CUR.local_full_stop(SimpleNamespace()) is False
    return 'PASS'


def check():
    for name,fn in (('classify',classify_check),('admission',admission_check),('evidence',evidence_readers_check),('gate',gate_check),('wake',wake_check),('selection',selection_check),('diagnostic_token',diagnostic_token_check)):
        assert fn()=='PASS',name
    return {'current_independence_adapter':'PASS'}


if __name__=='__main__':
    import json
    print(json.dumps(check(),sort_keys=True))
