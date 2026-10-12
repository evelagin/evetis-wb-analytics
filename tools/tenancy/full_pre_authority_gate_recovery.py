"""Owner-only additive closure of terminal pre-claim controller gate STOPs.

Historical exception text may be unavailable. No recovery is permitted unless
the registered code requires a durable claim before source, that claim never
existed, every current global integrity gate passes, all source effects/claims
are settled or safely quarantined, and no later source execution occurred.
No control generation/closure or SOURCE success/lease is invented.
"""
from tools.tenancy import durable_plan as D,tenant_backfill as BF,full_history as F
from tools.tenancy.validation import parse_tenant_json as parse
KIND='FULL_PRE_AUTHORITY_GATE_STOP_RECOVERED'
TYPE='TERMINAL_PRE_CLAIM_SOURCE_FREE_GATE_V1'
PREDICATES=frozenset({'exact_pre_claim_stage','terminal_failed_controller','registered_claim_before_source',
    'no_control_claim_for_execution','canonical_control_series_closed','no_source_runtime_after_stop',
    'no_source_journal_after_stop','no_pending_source_authority','current_manifest_GO_bindings_caps_valid',
    'all_jobs_inactive','all_schedulers_paused','prior_certificates_valid','quarantined_effects_zero',
    'original_evidence_preserved','qualified_replacement','source_budget_zero'})
FIELDS=frozenset({'version','type','tenant','project','root','manifest_hash','stop_hash','execution',
    'old_source','old_image','new_source','new_image','new_implementation_hash','runtime_image',
    'owner_decision_sha256','qualification_hash','security_hash','effect_hash','source_probe_hash',
    'protected_root_hashes','predicates','verified_at','source_budget','historical_cause','hash'})
def fail(s):raise BF.B.EvidenceError('pre-authority gate recovery: '+s)
def sealed(p):return dict(p,hash=D.digest(p))
def authority(p):return 'BFPAG_AUTH_'+p['root']+'_'+p['stop_hash']
def certificate(p):return 'BFPAG_'+p['hash']
def value(p):return {'kind':'pre_authority_gate','root':p['root'][:16]},D.encoded(p)
def header(p,m):
    if not isinstance(p,dict) or set(p)!=FIELDS or type(p['version']) is not int or p['version']!=1 or p['type']!=TYPE or p['hash']!=D.digest({k:v for k,v in p.items() if k!='hash'}):fail('closed certificate required')
    if (p['tenant'],p['project'],p['root'],p['manifest_hash'])!=(m['tenant'],m['project'],m['hash'],D.digest(m)):fail('foreign manifest')
    if type(p['source_budget']) is not int or p['source_budget']!=0 or p['historical_cause']!='UNPROVEN_PRE_CLAIM_GATE':fail('source-free scope only; no invented cause')
    if not isinstance(p['predicates'],dict) or set(p['predicates'])!=PREDICATES or any(x is not True for x in p['predicates'].values()):fail('unknown mandatory predicate')
    for k in FIELDS:
        if k.endswith('_hash') or k.endswith('_sha256') or k in ('hash','root'):D.check_hash(p[k])
    if not isinstance(p['protected_root_hashes'],list) or p['protected_root_hashes']!=sorted(set(p['protected_root_hashes'])) or not 1<=len(p['protected_root_hashes'])<=10000:fail('bounded retained records required')
    F.stamp(p['verified_at']);return p
def validate(p,b,m,records):
    from tools.tenancy import full_controller as H,full_leaf_recovery as FL
    header(p,m)
    if H.only_manifest(records)!=m or not set(p['protected_root_hashes'])<={D.digest(z) for z in records}:fail('immutable root differs')
    stop=FL.one(records,'STOPPED',p['stop_hash'])
    if stop['payload']!={'reason':'CONTROLLER_GATE_OR_EVIDENCE_FAILURE','controller_execution':p['execution'],'diagnostic':{'version':1,'stage':'MANIFEST_ROOT','category':'EVIDENCE'}}:fail('only exact pre-claim gate supported')
    from tools.tenancy import orchestration_contract as O
    r=parse((BF.REPO/'infra/tenant/releases/backfill'/f'{p["old_source"]}.json').read_text())
    if r['image']!=p['old_image'] or r.get('verification',{}).get('cadence_overlap_adapter')!='PASS':fail('historical claim-before-source release absent')
    return p
def load(b,m,records):
    out=[]
    for r in records:
        if r['kind']!=KIND:continue
        p=r['payload'];validate(p,b,m,records)
        if r['sequence']!=0 or r['root_hash']!=m['hash'] or any(b.tables.get_table(ds,n)!=value(p) for ds,n in ((b.c['datasets']['ref'],authority(p)),(b.c['datasets']['tenant_locks'],certificate(p)))):fail('owner acceptance absent')
        out.append(p)
    return out
def observe(b,m,stop_hash,*,owner_decision_sha256,qualification_hash,security_hash,source_probe_hash):
    from tools.tenancy import full_controller as H,full_leaf_recovery as FL,full_fbo_diagnostic_closure as LQ,controller_cadence as CC,orchestration_contract as O,recent_priority as RP
    records=b.store.history(m['hash']);stop=FL.one(records,'STOPPED',stop_hash);execution=stop['payload']['controller_execution']
    x=b.request('GET',BF.RUN_API+'/'+execution);co=x['template']['containers'][0];env={z['name']:z.get('value') for z in co['env']}
    if not x.get('completionTime') or x.get('failedCount')!=1 or x.get('succeededCount',0) or x.get('cancelledCount',0) or x.get('retriedCount',0) or x.get('taskCount')!=1:fail('exact terminal FAILED controller required')
    if env.get('BACKFILL_ROOT_HASH')!=m['hash'] or env.get('HISTORICAL_SCHEDULER_STATE')!='ENABLED':fail('claim-enabled exact root required')
    old=parse((BF.REPO/'infra/tenant/releases/backfill'/f'{env["CONTROLLER_SOURCE_SHA"]}.json').read_text())
    c=O.historical_contract(b.c,old,m);block=O.block(c,dict(release=old['source_sha'],root_hash=m['hash'],scheduler_state='ENABLED'),BF.REPO,old)
    O.verify_job({'template':{'taskCount':1,'template':x['template']}},block)
    if b.current_execution is not None:fail('inactive owner freeze required')
    H.verify_authority(H.Backend(b,m),m);done=H.completed(records,m)
    quarantines=LQ.load(b,m,records);quarantined=H.isolated_indices(m,quarantines)
    plans=H.reconstruct_plans(records,m,b.clock())
    if RP.pending_receipts(b,{i:p for i,p in plans.items() if i not in done and i not in quarantined}):fail('unsettled source authority')
    ds=b.c['datasets']['tenant_locks'];prefix='BFCW_'+m['hash']+'_';generations=[];claims=[]
    for n,*_ in b.tables.list_tables(ds):
        if not n.startswith(prefix):continue
        marker=b.tables.get_table(ds,n);p=parse(marker[1])
        # Immutable historical claims were independently accepted by their
        # qualified deployment. Validate the complete CAS series/closures here;
        # checking every historical execution again is unrelated to this gate.
        if (set(p)!=CC.CLAIM_FIELDS or p.get('version')!=1 or p.get('root')!=m['hash'] or p.get('tenant')!=m['tenant'] or p.get('project')!=m['project'] or p['hash']!=D.digest({k:v for k,v in p.items() if k!='hash'}) or marker!=CC.claim_value(p)):fail('corrupt canonical claim')
        if n!=CC.names(m['hash'],p['generation'])[0]:fail('claim generation/name differs')
        if p['execution']==execution:fail('controller reached source-capable claim')
        if not CC.closed(b,p):fail('unclosed canonical control authority')
        generations.append(p['generation']);claims.append(p['hash'])
    if not generations or sorted(generations)!=list(range(1,max(generations)+1)):fail('control generation gap')
    base,_=BF.resources(b.c);ss=b.request('GET',BF.SCHED_API+'/'+base+'/jobs?pageSize=100')
    if len(ss.get('jobs',[]))!=4 or ss.get('nextPageToken') or any(z['state']!='PAUSED' for z in ss['jobs']):fail('owner freeze required')
    for n in (*b.c['marketplaces']['ozon']['jobs'],'tenant-control',O.JOB):
        xs=BF.execution_inventory(b.request,base+'/jobs/'+n)['executions']
        if any(not z.get('completionTime') for z in xs):fail('active execution')
        if n!=O.JOB and any(F.stamp(z['createTime'])>=F.stamp(x['createTime']) for z in xs):fail('source/control execution after failed pre-claim gate')
    journal=b.select(f"SELECT ingestion_run_id,status,entity,backfill_sequence FROM `{b.journal}` WHERE started_at>=@start LIMIT 1000",{'start':('TIMESTAMP',x['createTime'])})
    if journal:fail('source journal after failed pre-claim gate')
    from tools.tenancy import full_cooldown_recovery as FC,full_policy_bootstrap_recovery as PB,full_pretransport_recovery as PT,full_first_receipt_recovery as FR,full_controller_closure_recovery as CR,full_fbo_zero_effect_recovery as ZE
    accepted=FL.load(b,m,records)+FC.load(b,m,records)+PB.load(b,m,records)+PT.load(b,m,records)+FR.load(b,m,records)+ZE.load(b,m,records)+quarantines
    control=CR.load(b,m,records);covered={p['stop_hash'] for p in accepted}|{h for p in control for h in p['stop_hashes']}
    if {D.digest(z) for z in records if z['kind']=='STOPPED'}-covered!={stop_hash}:fail('other global STOP remains')
    source=b.c['orchestration']['job']['env']['CONTROLLER_SOURCE_SHA'];new=parse((BF.REPO/'infra/tenant/releases/backfill'/f'{source}.json').read_text());O.verify_artifact_source(new,BF.REPO)
    if new.get('fbo_source_isolation_version')!=1:fail('replacement not qualified')
    facts=dict(controller=x,claims=sorted(claims),source_journal=journal,quarantines=sorted(z['hash'] for z in quarantines),accepted=sorted(z['hash'] for z in accepted+control))
    p=dict(version=1,type=TYPE,tenant=m['tenant'],project=m['project'],root=m['hash'],manifest_hash=D.digest(m),stop_hash=stop_hash,execution=execution,old_source=old['source_sha'],old_image=old['image'],new_source=source,new_image=new['image'],new_implementation_hash=new['controller_implementation_hash'],runtime_image=new['runtime_image'],owner_decision_sha256=owner_decision_sha256,qualification_hash=qualification_hash,security_hash=security_hash,effect_hash=D.digest(facts),source_probe_hash=source_probe_hash,protected_root_hashes=sorted(D.digest(z) for z in records),predicates=dict.fromkeys(PREDICATES,True),verified_at=b.clock().isoformat(),source_budget=0,historical_cause='UNPROVEN_PRE_CLAIM_GATE')
    p=sealed(p);validate(p,b,m,records);return p,facts
def publish(b,p,m):
    header(p,m)
    if not 0<=(b.clock()-F.stamp(p['verified_at'])).total_seconds()<=900:fail('fresh proof required')
    fresh,_=observe(b,m,p['stop_hash'],**{k:p[k] for k in ('owner_decision_sha256','qualification_hash','security_hash','source_probe_hash')})
    if any(p[k]!=fresh[k] for k in FIELDS-{'hash','verified_at'}):fail('fresh predicates changed')
    for ds,n in ((b.c['datasets']['ref'],authority(p)),(b.c['datasets']['tenant_locks'],certificate(p))):
        if b.tables.get_table(ds,n) is None:b.tables.create_marker(ds,n,*value(p))
        if b.tables.get_table(ds,n)!=value(p):fail('publication conflict')
    b.store.commit(m['hash'],KIND,0,p,b.clock())
    if p not in load(b,m,b.store.history(m['hash'])):fail('independent acceptance differs')
    return p
