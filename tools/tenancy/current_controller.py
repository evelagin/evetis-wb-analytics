"""Opt-in operational P0 selection under the existing historical/source fences.

No ordinary Scheduler is enabled. Each Moscow-day cycle has distinct durable
plans, intents, receipts, reconciliation and DQ; the historical manifest is
never extended or reset. No policy means the historical path is unchanged.
"""
from datetime import date,timedelta
from tools.tenancy import tenant_backfill as BF,durable_plan as D,full_controller as H
from tools.tenancy import current_policy as P,current_contract as K,controller_cadence as CC
from tools.tenancy.validation import parse_tenant_json as parse
from tools.tenancy.controller_diagnostics import gate

def fail(s):raise BF.B.EvidenceError('operational current: '+s)

def policy(b):
    env=b.c['orchestration']['job']['env']
    if env.get('CURRENT_DATA_MODE') is None:return None
    if env['CURRENT_DATA_MODE']!=K.TYPE:fail('unknown operational mode')
    source=env['CONTROLLER_SOURCE_SHA']
    marker=b.tables.get_table(b.c['datasets']['ref'],P.name(source))
    from tools.tenancy import cloud_controller as C,orchestration_contract as O
    from tools.tenancy import current_release_handoff as HANDOFF
    association=b.tables.get_table(b.c['datasets']['ref'],HANDOFF.name(source))
    if marker is None and association is None:return None
    spec=b.tables.get_table(b.c['datasets']['tenant_locks'],C.descriptor_name(source,b.manifest['hash'],env['HISTORICAL_SCHEDULER_STATE']))
    release=parse(spec[1])['release'] if spec else {}
    O.verify_artifact_source(release,BF.REPO)
    if association is not None:p=HANDOFF.resolve(b,source,association,release)
    else:
        p=parse(marker[1]);P.validate(p,b.c,b.manifest['hash'],b.clock())
        if marker!=({'kind':'current_policy','root':p['hash'][:16]},D.encoded(p)):fail('owner marker differs')
    if (release.get('current_data_version')!=1 or release.get('verification',{}).get('current_data_adapter')!='PASS'
        or p['runtime_implementation_hash']!=BF.B.implementation_hash()):fail('exact current adapter is not qualified')
    if p['fbo_contract_hash'] is not None:
        from tools.tenancy import platform as PL
        runtime=[parse(f.read_text()) for f in (BF.REPO/PL.RUNTIME_RELEASES_DIR/'ozon').glob('*.json')]
        runtime=[r for r in runtime if r.get('image')==p['runtime_image']]
        if len(runtime)!=1 or runtime[0].get('verification',{}).get('built_artifact',{}).get('fbo_transport_window_contract')!=p['fbo_contract_hash']:fail('installed FBO ownership contract is not qualified')
    return p

def cycles(b,p):
    records=b.store.history(p['hash'])
    days={}
    for r in records:
        if r['kind']!='CURRENT_DAY':fail('unknown current index record')
        v=r['payload'];day=date.fromisoformat(v['day'])
        if (set(v)!={'day','root','policy_hash'} or v['root']!=P.cycle_root(p,day) or v['policy_hash']!=p['hash']
            or day>b.clock().astimezone(K.MSK).date()
            or r['sequence']!=(day-date.fromisoformat(p['first_day'])).days):fail('current cycle index differs')
        if v['day'] in days and days[v['day']]!=v:fail('conflicting current cycle authority')
        days[v['day']]=v
    return days

def plans(b,p,day):
    root=P.cycle_root(p,day);records=b.store.history(root);out={}
    for r in records:
        if r['kind'] not in {'CURRENT_PLAN','CURRENT_CERT','CURRENT_PARTIAL','CURRENT_CYCLE_PARTIAL','CURRENT_CYCLE_COMPLETE'}:fail('unknown current cycle record')
        if r['kind']!='CURRENT_PLAN':continue
        v=r['payload'];doc=v['plan'];entity=doc['runtime_plan']['entity']
        P.validate_document(p,day,doc)
        if set(v)!={'day','policy_hash','plan','shard'} or v['policy_hash']!=p['hash'] or v['day']!=day.isoformat() or v['shard']!=P.shard(p,day,doc):fail('current source plan ownership differs')
        if entity in out and out[entity]!=v:fail('conflicting current source plan')
        out[entity]=v
    return records,out

def performance_fence(b,doc):
    if not doc['runtime_plan']['entity'].startswith('ads_'):return None
    p=policy(b)
    if p is None:return None
    reports=[];cooldowns=[]
    for text in cycles(b,p):
        _,items=plans(b,p,date.fromisoformat(text))
        item=items.get('ads_sku_daily')
        if item is None:continue
        state=b.state(item['plan']);pr=state['progress'];report=pr.get('report')
        if report:
            if report['phase']=='INTENT':fail('ambiguous current report intent; never create a second report')
            if report['phase']!='POLL':fail('unknown current report phase')
            reports.append(item['plan']['runtime_plan']['plan_id'])
        eligible=pr.get('rate_limit',{}).get('eligible_at')
        if eligible and K.at(eligible)>b.clock():cooldowns.append(eligible)
    if len(reports)>1:fail('multiple current async report scopes')
    if cooldowns:return {'status':'WAITING','allowance':0,'eligible_at':max(cooldowns,key=K.at),'basis':'SOURCE_COOLDOWN'}
    if reports and reports!=[doc['runtime_plan']['plan_id']]:return {'status':'WAITING','allowance':0,'eligible_at':(b.clock()+timedelta(minutes=3)).isoformat(),'basis':'EXISTING_ASYNC_SCOPE'}
    return None

def validate_certificate(b,p,day,cert,item):
    if (set(cert)!={'day','policy_hash','ack_hash','shard','state_hash','source_sequence','fact','coverage','accepted_receipt_hash','reconciliation_hash'}
        or (cert['policy_hash'],cert['day'],cert['ack_hash'],cert['shard'])!=(p['hash'],day.isoformat(),item['plan']['ack_hash'],item['shard'])):fail('current certificate ownership differs')
    K.check_fact(cert['fact'],p['hash'],b.clock())
    state=b.state(item['plan'])
    if state['complete'] is not True or D.digest(state)!=cert['state_hash'] or state['sequence']!=cert['source_sequence']:fail('current accepted source prefix differs')
    hs=b.store.history(item['shard'])
    receipts=[r for r in hs if r['kind']=='DISPATCH_RECEIPT' and D.digest(r)==cert['accepted_receipt_hash']]
    recon=[r for r in hs if r['kind']=='RECONCILED' and D.digest(r)==cert['reconciliation_hash']]
    if (len(receipts)!=1 or len(recon)!=1 or receipts[0]['sequence']!=recon[0]['sequence']
        or receipts[0]['payload']['plan']!=item['plan'] or receipts[0]['payload']['receipt']['run_id']!=cert['fact']['source_run_id']
        or recon[0]['payload'].get('source_complete') is not True):fail('current terminal receipt/reconciliation evidence differs')
    return cert

def catalog_current(b,day):
    p=policy(b)
    if p is None:return None
    d=date.fromisoformat(day);records,items=plans(b,p,d)
    certs=[r['payload'] for r in records if r['kind']=='CURRENT_CERT' and r['payload'].get('fact',{}).get('entity')=='catalog']
    if not certs:return None
    if len(certs)!=1 or 'catalog' not in items:fail('current Catalog certificate ambiguous')
    return validate_certificate(b,p,d,certs[0],items['catalog'])

class Backend(H.Backend):
    def __init__(self,base,p,day):
        super().__init__(base,base.manifest)
        self.current_policy=p;self.current_day=day
        self.current_root=P.cycle_root(p,day)
        # Current source executions are ordinary bounded plans with their own
        # identity. They must never inherit a historical FULL dispatch context.
        self.full_dispatch_authority=None;self.full_dispatch_intent=None

    def authorize_full_leaf(self,doc):
        if policy(self)!=self.current_policy:fail('current policy changed')
        P.validate_document(self.current_policy,self.current_day,doc)
        _,source_plans=plans(self,self.current_policy,self.current_day)
        if source_plans.get(doc['runtime_plan']['entity'],{}).get('plan')!=doc:fail('current plan is not durably elected')
        return True

    def active_source_shard(self,doc):
        self.authorize_full_leaf(doc)
        return P.shard(self.current_policy,self.current_day,doc)

    def preflight(self,leaf,*,owner_observation=False):
        if owner_observation or leaf.get('hash')!=self.current_root or len(leaf.get('plans',[]))!=1:fail('current cloud-only scope differs')
        self.active=False
        doc=leaf['plans'][0]
        self.authorize_full_leaf(doc)
        BF.preflight(BF.validate_plan(doc,doc['ack_hash']),doc,self.clock(),backend=self,allow_active=True)

    def reconcile(self,payload):
        doc=payload['plan'];receipt=payload['receipt']
        with gate('RUNTIME_TERMINAL'):
            op=self.request('GET',BF.RUN_API+'/'+receipt['operation'])
            if not op.get('done'):return None
            if op.get('error'):fail('FAILED current source needs exact effect investigation; no automatic replay')
        result=BF.reconcile(doc,doc['ack_hash'],receipt,backend=self)
        result['persistence']=BF.verify_persistence(doc,doc['ack_hash'],backend=self)
        result['source_complete']=self.state(doc)['complete']
        return result

def certificate(b,p,day,item,history):
    doc=item['plan'];state=b.state(doc)
    if state['complete'] is not True:fail('partial source cannot receive current completion')
    receipts={r['sequence']:r for r in history if r['kind']=='DISPATCH_RECEIPT'}
    reconciled={r['sequence']:r for r in history if r['kind']=='RECONCILED'}
    if not receipts or set(receipts)!=set(reconciled):fail('current pending receipt cannot count as complete')
    last=receipts[max(receipts)]['payload']['receipt']
    proof=BF.verify_coverage(doc,doc['ack_hash'],backend=b)
    dated=doc['runtime_plan']['entity'] not in K.SNAPSHOTS and doc['runtime_plan']['entity'] not in {'stocks','prices'}
    expected_days=(date.fromisoformat(doc['runtime_plan']['to'])-date.fromisoformat(doc['runtime_plan']['from'])).days+1
    checks=[v for v in proof['readback'] if 'day' in v]
    if dated and (len(checks)!=expected_days or any(v.get('coverage')!='COMPLETE' for v in checks)):
        return None # Explicit UNKNOWN empty-retention evidence stays PARTIAL.
    entity=doc['runtime_plan']['entity'];at=b.clock().isoformat()
    fact=dict(entity=entity,current_scope=p['hash'],observed_at=at,source_complete=True,
        persisted_reconciled=True,natural_keys_verified=True,duplicate_natural_keys=0,
        coverage_gap_days=0,source_unit_gaps=0,source_unit_duplicates=0,mismatches=0,
        finance_finality='PROVISIONAL' if entity=='finance_accrual' else 'NOT_APPLICABLE',
        certificate_hash=D.digest(dict(plan=doc['ack_hash'],proof=proof,reconciled=[D.digest(r) for _,r in sorted(reconciled.items())])),source_run_id=last['run_id'])
    K.check_fact(fact,p['hash'],b.clock())
    return dict(day=day.isoformat(),policy_hash=p['hash'],ack_hash=doc['ack_hash'],shard=item['shard'],state_hash=D.digest(state),source_sequence=state['sequence'],fact=fact,coverage=proof,accepted_receipt_hash=D.digest(receipts[max(receipts)]),reconciliation_hash=D.digest(reconciled[max(reconciled)]))

def source_tick(b,p,day,item):
    doc=item['plan'];shard=item['shard'];leaf={'hash':b.current_root,'plans':[doc]}
    b.preflight(leaf)
    if b.active_runtime_execution():return {'status':'MONITORING','source_dispatches':0,'operational_entity':doc['runtime_plan']['entity']}
    history=b.store.history(shard);q=b.quota(leaf,b.clock())
    decision=D.decide_tick(history,shard,q,b.active_runtime_execution())
    action=decision['action']
    if action=='MONITOR':return {'status':'MONITORING','source_dispatches':0,'operational_entity':doc['runtime_plan']['entity']}
    if action in {'STOPPED','RECOVER_RECEIPT_OR_STOP'}:fail('uncertain current dispatch or source authority; never repeat')
    if action=='RECONCILE':
        seq=decision['sequence'];payload=next(r['payload'] for r in history if r['kind']=='DISPATCH_RECEIPT' and r['sequence']==seq)
        if payload['plan']!=doc:fail('current receipt plan differs')
        result=b.reconcile(payload)
        if result is None:return {'status':'MONITORING','source_dispatches':0}
        b.store.commit(shard,'RECONCILED',seq,result,b.clock())
        history=b.store.history(shard)
    if b.state(doc)['complete']:
        cert=certificate(b,p,day,item,history)
        if cert is not None:
            b.store.commit(b.current_root,'CURRENT_CERT',P.ORDER.index(doc['runtime_plan']['entity']),cert,b.clock())
        else:
            b.store.commit(b.current_root,'CURRENT_PARTIAL',P.ORDER.index(doc['runtime_plan']['entity']),{'entity':doc['runtime_plan']['entity'],'policy_hash':p['hash'],'ack_hash':doc['ack_hash'],'state_hash':D.digest(b.state(doc)),'status':'PARTIAL','reason':'SOURCE_RETENTION_COMPLETENESS_UNPROVEN'},b.clock())
        return {'status':'RECONCILED_BOUNDARY' if cert is not None else 'WAITING','source_dispatches':0,'operational_entity':doc['runtime_plan']['entity'],'current_complete':cert is not None}
    if action in {'WAITING','RECONCILE'}:return {'status':'WAITING','source_dispatches':0,'operational_entity':doc['runtime_plan']['entity']}
    if action!='PREPARE_NEXT':fail('unknown current tick decision')
    if doc['runtime_plan'].get('observation_date')!=None and day!=b.clock().astimezone(K.MSK).date():
        return {'status':'SNAPSHOT_DAY_ROLLOVER','source_dispatches':0}
    prepared=[];received=[];seq=decision['sequence']
    def before(preparation):
        if prepared:fail('one intent per source wake')
        b.store.commit(shard,'DISPATCH_INTENT',seq,{'plan':doc,'preparation':preparation,'current_parent_execution':b.current_execution},b.clock());prepared.append(preparation)
    def after(receipt):
        if len(prepared)!=1 or received or any(receipt.get(k)!=prepared[0][k] for k in ('run_id','lease_generation','ack_hash')):fail('current intent/receipt mismatch')
        b.store.commit(shard,'DISPATCH_RECEIPT',seq,{'plan':doc,'receipt':receipt},b.clock());received.append(receipt)
    with gate('SOURCE_DISPATCH_BOUNDARY'):b.start(doc,before,after)
    if len(prepared)!=1 or len(received)!=1:fail('uncertain current dispatch outcome; never repeat')
    return {'status':'DISPATCHED','source_dispatches':1,'operational_entity':doc['runtime_plan']['entity'],'operational_day':day.isoformat()}

def wake(base,historical_records,historical_plans,done,quarantined):
    p=policy(base)
    if p is None:return None
    CC.ensure(base)
    from tools.tenancy import recent_priority as RP
    # An immutable elected historical authority takes precedence over P0/P1
    # reordering. Neither midnight nor a new operational policy resets it.
    if RP.pending_receipts(base,historical_candidates(historical_records,historical_plans,done,quarantined)):return None
    day=base.clock().astimezone(K.MSK).date();known=cycles(base,p)
    # Drain every committed current receipt before making a new day's plan.
    pending=[];reports=[];sku_plans=[]
    for text,v in sorted(known.items()):
        d=date.fromisoformat(text);records,items=plans(base,p,d)
        for entity,item in items.items():
            hs=base.store.history(item['shard'])
            intents=[r for r in hs if r['kind']=='DISPATCH_INTENT']
            if intents and not any(r['kind']=='RECONCILED' and r['sequence']==max(z['sequence'] for z in intents) for r in hs):pending.append((d,item))
            if entity=='ads_sku_daily':sku_plans.append((d,item))
    if len(pending)>1:fail('multiple current source authorities')
    if pending:
        d,item=pending[0];return source_tick(Backend(base,p,d),p,d,item)
    for d,item in sku_plans:
        report=base.state(item['plan'])['progress'].get('report')
        if report:
            if report['phase']=='INTENT':fail('ambiguous current async intent after terminal receipt')
            if report['phase']!='POLL':fail('unknown current async phase')
            reports.append((d,item))
    if len(reports)>1:fail('multiple current async scopes')
    if reports:
        d,item=reports[0];result=source_tick(Backend(base,p,d),p,d,item)
        if result['status']!='WAITING':return result
    if day.isoformat() not in known:
        root=P.cycle_root(p,day);base.store.commit(p['hash'],'CURRENT_DAY',(day-date.fromisoformat(p['first_day'])).days,{'day':day.isoformat(),'root':root,'policy_hash':p['hash']},base.clock())
    records,items=plans(base,p,day);certs={};partial=set()
    for r in records:
        if r['kind']=='CURRENT_PARTIAL':
            v=r['payload'];entity=v['entity'];item=items.get(entity)
            if (set(v)!={'entity','policy_hash','ack_hash','state_hash','status','reason'} or not item
                or (v['policy_hash'],v['ack_hash'],v['status'],v['reason'])!=(p['hash'],item['plan']['ack_hash'],'PARTIAL','SOURCE_RETENTION_COMPLETENESS_UNPROVEN')
                or D.digest(base.state(item['plan']))!=v['state_hash']):fail('current partial evidence differs')
            partial.add(entity);continue
        if r['kind']!='CURRENT_CERT':continue
        cert=r['payload'];entity=cert.get('fact',{}).get('entity');item=items.get(entity)
        if not item:fail('current certificate lacks a plan')
        validate_certificate(base,p,day,cert,item)
        if entity in certs and certs[entity]!=cert:fail('conflicting current certificate')
        certs[entity]=cert
    if 'catalog' in certs:
        from tools.tenancy import fbo_diagnostic_owner as DIAGNOSTIC
        result=DIAGNOSTIC.queued(base)
        if result is not None:return result
    for entity in P.eligible(p,{e:True for e in certs}):
        if entity in partial:continue
        item=items.get(entity)
        if item is None:
            doc=P.document(p,day,entity);item={'day':day.isoformat(),'policy_hash':p['hash'],'plan':doc,'shard':P.shard(p,day,doc)}
            base.store.commit(P.cycle_root(p,day),'CURRENT_PLAN',P.ORDER.index(entity),item,base.clock())
        current=Backend(base,p,day);result=source_tick(current,p,day,item)
        if result['status'] in {'WAITING','SNAPSHOT_DAY_ROLLOVER'}:continue
        return result
    # PARTIAL is explicit while FBO or quota-dependent sources are unresolved.
    # It never grants operational READY, historical COMPLETE or Finance FINAL.
    return None

def historical_candidates(records,plans,done,quarantined):
    """Catalog dependencies carry the same immutable dispatch authority as leaves."""
    selected={i:v for i,v in plans.items() if i not in done and i not in quarantined}
    selected.update({('dependency',n):r['payload'] for n,r in enumerate(records) if r['kind']=='DEPENDENCY_PLAN'})
    return selected
