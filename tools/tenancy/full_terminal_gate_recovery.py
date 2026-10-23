"""Owner-only closure of the exact untyped terminal gate STOP after a reconciled FULL page.

Heldi S‴-a (2026-10-22): wake qhv6d committed STOP 8ad33cc6 without a closed
diagnostic after it had reconciled supplies page 103 and before any authority
for page 104 existed: no lease, no checkpoint, no intent, no Run POST, no journal
row. The historical exception text is unavailable, so the cause stays UNPROVEN.
Recovery is permitted only while every page, lease, claim, runtime and journal
fact proves that nothing followed the reconciled page, every prior certificate
and quarantine still revalidates, the owner froze all Schedulers and the
replacement image types every write-path failure. No lease, receipt, claim,
completion or next-page authority is invented.
"""
from datetime import datetime
from tools.tenancy import durable_plan as D,tenant_backfill as BF,full_history as F
from tools.tenancy.validation import parse_tenant_json as parse
KIND='FULL_TERMINAL_GATE_STOP_RECOVERED'
TYPE='FULL_TERMINAL_GATE_AFTER_RECONCILED_PAGE_V1'
CAUSE='UNPROVEN_WRITE_PATH_GATE'
REASON='CONTROLLER_GATE_OR_EVIDENCE_FAILURE'
PREDICATES=frozenset({'exact_untyped_terminal_stop','terminal_failed_controller','claim_closed_stopped_without_dispatch',
    'canonical_control_series_closed','reconciled_page_before_stop','page_lease_released_before_stop',
    'no_next_page_authority','no_source_runtime_after_stop','no_source_journal_after_stop','page_state_unchanged',
    'current_manifest_GO_bindings_caps_valid','all_jobs_inactive','all_schedulers_paused','prior_certificates_valid',
    'recent_priority_preserved','original_evidence_preserved','qualified_replacement','source_budget_zero'})
FIELDS=frozenset({'version','type','tenant','project','root','manifest_hash','stop_hash','execution',
    'index','shard','page_sequence','receipt_hash','recon_hash','lease_generation','claim_generation','recent_policy_hash',
    'old_source','old_image','new_source','new_image','new_implementation_hash','runtime_image',
    'owner_decision_sha256','qualification_hash','security_hash','effect_hash','source_probe_hash',
    'protected_root_hashes','predicates','verified_at','source_budget','historical_cause','hash'})
PAGE_KINDS=('DISPATCH_INTENT','DISPATCH_RECEIPT','RECONCILED')
FRESH_SECONDS=900
# The publication is three logical operations: two create-only markers and one root record. The root
# record (DurableRecords.commit) is itself two HTTP writes — the checkpoint row, then its BFR_ commit
# marker — so an exact owner writer admits four HTTP writes, each once (see publication_writes()).
PUBLICATION=('authority_marker','certificate_marker','root_record')
def publication_writes(b,p,m):
    """The four HTTP writes of publish(), in order: (dataset, table) — the executor's exact write set."""
    from tools.tenancy import durable_plan as D
    record=dict(version=D.VERSION,root_hash=m['hash'],kind=KIND,sequence=0,payload=p)
    return [(b.c['datasets']['ref'],authority(p)),(b.c['datasets']['tenant_locks'],certificate(p)),
            (b.c['datasets']['tenant_ops'],'BACKFILL_CHECKPOINTS'),(b.c['datasets']['tenant_locks'],'BFR_'+D.digest(record))]
def fail(s):raise BF.B.EvidenceError('terminal gate recovery: '+s)
def sealed(p):return dict(p,hash=D.digest(p))
def authority(p):return 'BFTG_AUTH_'+p['root']+'_'+p['stop_hash']
def certificate(p):return 'BFTG_'+p['hash']
def value(p):
    # The full closed proof (with the growing root inventory) lives in its content-addressed durable root
    # record; the owner markers bind that exact proof by digest, within the 16-KiB table-description limit
    # (as the controller-closure and CURRENT recoveries do). 2026-10-23: the full certificate (34.5 KB) was
    # rejected with HTTP 400 before any effect.
    return {'kind':'terminal_gate','root':p['root'][:16]},D.encoded(dict(version=1,type='TERMINAL_GATE_CERTIFICATE_REF',
        root=p['root'],stop_hash=p['stop_hash'],certificate_hash=p['hash'],source_budget=0))
def when(v):
    if isinstance(v,datetime):
        if v.utcoffset() is None:fail('marker timestamp unzoned')
        return v
    return F.stamp(v)
def header(p,m):
    if not isinstance(p,dict) or set(p)!=FIELDS or type(p['version']) is not int or p['version']!=1 or p['type']!=TYPE or p['hash']!=D.digest({k:v for k,v in p.items() if k!='hash'}):fail('closed certificate required')
    if (p['tenant'],p['project'],p['root'],p['manifest_hash'])!=(m['tenant'],m['project'],m['hash'],D.digest(m)):fail('foreign manifest')
    if type(p['source_budget']) is not int or p['source_budget']!=0 or p['historical_cause']!=CAUSE:fail('source-free scope only; no invented cause')
    if not isinstance(p['predicates'],dict) or set(p['predicates'])!=PREDICATES or any(x is not True for x in p['predicates'].values()):fail('unknown mandatory predicate')
    for k in FIELDS:
        if k.endswith('_hash') or k.endswith('_sha256') or k in ('hash','root','shard'):D.check_hash(p[k])
    for k in ('index','page_sequence','lease_generation','claim_generation'):
        if type(p[k]) is not int or p[k]<(0 if k=='index' else 1) or p[k]>9999999999:fail('bounded page/lease/claim identity required')
    if p['index']>=len(m['programs']):fail('page leaf outside manifest')
    if not isinstance(p['protected_root_hashes'],list) or p['protected_root_hashes']!=sorted(set(p['protected_root_hashes'])) or not 1<=len(p['protected_root_hashes'])<=10000:fail('bounded retained records required')
    F.stamp(p['verified_at']);return p
def validate(p,b,m,records):
    from tools.tenancy import full_controller as H,full_leaf_recovery as FL
    header(p,m)
    if H.only_manifest(records)!=m or not set(p['protected_root_hashes'])<={D.digest(z) for z in records}:fail('immutable root differs')
    stop=FL.one(records,'STOPPED',p['stop_hash'])
    if stop['sequence']!=0 or stop['payload']!={'reason':REASON,'controller_execution':p['execution']}:fail('only the exact untyped terminal gate supported')
    plans=[r for r in records if r['kind']=='CHUNK_PLAN' and r['sequence']==p['index']]
    if not plans or any(r['payload'].get('shard_root')!=p['shard'] or r['payload'].get('index')!=p['index'] for r in plans):fail('page shard provenance differs')
    r=parse((BF.REPO/'infra/tenant/releases/backfill'/f'{p["old_source"]}.json').read_text())
    v=r.get('verification',{})
    if r['image']!=p['old_image'] or v.get('cadence_overlap_adapter')!='PASS' or v.get('controller_wake_budget_adapter')!='PASS':fail('historical claim-before-source release absent')
    return p
def load(b,m,records):
    out=[]
    for r in records:
        if r['kind']!=KIND:continue
        p=r['payload'];validate(p,b,m,records)
        if r['sequence']!=0 or r['root_hash']!=m['hash'] or any(b.tables.get_table(ds,n)!=value(p) for ds,n in ((b.c['datasets']['ref'],authority(p)),(b.c['datasets']['tenant_locks'],certificate(p)))):fail('owner acceptance absent')
        out.append(p)
    return out
def observe(b,m,stop_hash,*,index,owner_decision_sha256,qualification_hash,security_hash,source_probe_hash):
    from tools.tenancy import full_controller as H,full_leaf_recovery as FL,controller_cadence as CC,orchestration_contract as O,recent_priority as RP,stop_acceptance as SA
    records=b.store.history(m['hash']);stop=FL.one(records,'STOPPED',stop_hash);execution=stop['payload'].get('controller_execution')
    if stop['sequence']!=0 or stop['payload']!={'reason':REASON,'controller_execution':execution} or not isinstance(execution,str):fail('only the exact untyped terminal gate supported')
    x=b.request('GET',BF.RUN_API+'/'+execution);co=x['template']['containers'][0];env={z['name']:z.get('value') for z in co['env']}
    if not x.get('completionTime') or x.get('failedCount')!=1 or x.get('succeededCount',0) or x.get('cancelledCount',0) or x.get('retriedCount',0) or x.get('taskCount')!=1:fail('exact terminal FAILED controller required')
    if env.get('BACKFILL_ROOT_HASH')!=m['hash'] or env.get('HISTORICAL_SCHEDULER_STATE')!='ENABLED':fail('claim-enabled exact root required')
    old=parse((BF.REPO/'infra/tenant/releases/backfill'/f'{env["CONTROLLER_SOURCE_SHA"]}.json').read_text())
    c=O.historical_contract(b.c,old,m);block=O.block(c,dict(release=old['source_sha'],root_hash=m['hash'],scheduler_state='ENABLED'),BF.REPO,old)
    O.verify_job({'template':{'taskCount':1,'template':x['template']}},block)
    if b.current_execution is not None:fail('inactive owner freeze required')
    H.verify_authority(b,m);done=H.completed(records,m)
    started=F.stamp(x['createTime'])
    ds=b.c['datasets']['tenant_locks'];listing={n:(labels,created) for n,labels,created in b.tables.list_tables(ds)}
    if 'BFR_'+stop_hash not in listing:fail('terminal stop marker absent')
    stopped_at=when(listing['BFR_'+stop_hash][1])
    if not started<stopped_at<=b.clock():fail('terminal stop outside its controller execution')
    # Canonical control series: every claim closed; the terminal claim closed
    # STOPPED with zero dispatches; every later claim dispatched nothing.
    prefix='BFCW_'+m['hash']+'_';claims=[];own=None
    for n in sorted(listing):
        if not n.startswith(prefix):continue
        marker=b.tables.get_table(ds,n);p=parse(marker[1])
        if (set(p)!=CC.CLAIM_FIELDS or p.get('version')!=1 or p.get('root')!=m['hash'] or p.get('tenant')!=m['tenant'] or p.get('project')!=m['project'] or p['hash']!=D.digest({k:v for k,v in p.items() if k!='hash'}) or marker!=CC.claim_value(p)):fail('corrupt canonical claim')
        if n!=CC.names(m['hash'],p['generation'])[0]:fail('claim generation/name differs')
        close=b.tables.get_table(ds,CC.names(m['hash'],p['generation'])[1])
        if close is None:fail('unclosed canonical control authority')
        q=parse(close[1])
        if (set(q)!=CC.CLOSE_FIELDS or q.get('version')!=1 or q['claim_hash']!=p['hash'] or q['status'] not in CC.STATUSES or q['hash']!=D.digest({k:v for k,v in q.items() if k!='hash'}) or type(q['source_dispatches']) is not int):fail('corrupt canonical closure')
        if p['execution']==execution:
            if own is not None or q['status']!='STOPPED' or q['source_dispatches']!=0:fail('terminal claim closure differs')
            own=p
        claims.append((p['generation'],p['hash'],q['source_dispatches']))
    if own is None:fail('terminal controller claim absent')
    generations=sorted(g for g,_,_ in claims)
    if not generations or generations!=list(range(1,max(generations)+1)):fail('control generation gap')
    if any(sd!=0 for g,_,sd in claims if g>own['generation']):fail('source dispatch after terminal gate')
    # The reconciled page: last shard sequence is a persisted reconciliation
    # fenced before the STOP; no intent, receipt, fence or lease exists beyond it.
    plans=H.reconstruct_plans(records,m,b.clock())
    if index in done or index not in plans:fail('page leaf not an open full leaf')
    item=plans[index];doc=item['plan'];shard=item['shard_root'];sh=b.store.history(shard)
    seqs={k:[r['sequence'] for r in sh if r['kind']==k] for k in PAGE_KINDS}
    if not seqs['RECONCILED']:fail('reconciled page absent')
    page=max(seqs['RECONCILED'])
    if max(max(v,default=0) for v in seqs.values())!=page or any(r['sequence']>page for r in sh):fail('shard authority beyond reconciled page')
    recon=[r for r in sh if r['kind']=='RECONCILED' and r['sequence']==page];receipt=[r for r in sh if r['kind']=='DISPATCH_RECEIPT' and r['sequence']==page]
    if len(recon)!=1 or len(receipt)!=1:fail('reconciled page receipt/reconciliation ambiguous')
    recon,receipt=recon[0],receipt[0]
    if recon['payload'].get('persisted_reconciled') is not True or recon['payload'].get('failed'):fail('page not persisted-reconciled')
    run_id=receipt['payload']['receipt']['run_id'];lease_generation=receipt['payload']['receipt']['lease_generation']
    if not isinstance(run_id,str) or not run_id.startswith('bf-') or type(lease_generation) is not int:fail('page receipt identity malformed')
    for k in PAGE_KINDS:
        if f'BFQ_{shard}_{page:010d}_{k}' not in listing:fail('page fence absent')
    fences=[int(n[len(shard)+5:len(shard)+15]) for n in listing if n.startswith('BFQ_'+shard+'_') and n[len(shard)+5:len(shard)+15].isdigit()]
    if not fences or max(fences)!=page:fail('next page authority exists')
    if not started<=when(listing[f'BFQ_{shard}_{page:010d}_RECONCILED'][1])<stopped_at:fail('page not reconciled inside the terminal wake')
    cid=BF.B.digest(['BOUNDED_PILOT_EXCLUSIVE',b.c['project_id']])[:16]
    leases={int(z.group(2)):n for n in listing if (z:=BF.CK.LEASE_RE.match(n)) and z.group(1)==cid}
    releases={int(n[len('LD_'+cid+'_'):]):n for n in listing if n.startswith('LD_'+cid+'_') and n[len('LD_'+cid+'_'):].isdigit()}
    if not leases or max(leases)!=lease_generation or not releases or max(releases)!=lease_generation:fail('lease generation beyond reconciled page')
    if listing[leases[lease_generation]][0].get('owner')!=run_id:fail('page lease owner differs')
    released=b.tables.get_table(ds,releases[lease_generation])
    if released is None or released[0].get('owner')!=run_id or not when(listing[releases[lease_generation]][1])<stopped_at:fail('page lease not released before terminal stop')
    # No source effect after the page: Run executions and the ingestion journal.
    base,_=BF.resources(b.c)
    for n in (*b.c['marketplaces']['ozon']['jobs'],'tenant-control',O.JOB):
        xs=BF.execution_inventory(b.request,base+'/jobs/'+n)['executions']
        if any(not z.get('completionTime') for z in xs):fail('active execution')
        if n==O.JOB:continue
        for z in xs:
            at=F.stamp(z['createTime'])
            if at>=stopped_at:fail('source execution after terminal gate')
            if at>=started:
                zenv={e['name']:e.get('value') for e in ((z.get('template') or {}).get('containers') or [{}])[0].get('env',[])}
                if zenv.get('INGESTION_RUN_ID')!=run_id:fail('foreign source execution during terminal wake')
    rows=b.select(f"SELECT ingestion_run_id FROM `{b.journal}` WHERE started_at>=@start LIMIT 1000",{'start':('TIMESTAMP',x['createTime'])})
    if len(rows)>=1000:fail('journal window unbounded')
    if any(not isinstance(r.get('ingestion_run_id'),str) or not (r['ingestion_run_id']==run_id or r['ingestion_run_id'].startswith(run_id+'-u')) for r in rows):fail('foreign source journal during terminal wake')
    if b.select(f"SELECT ingestion_run_id FROM `{b.journal}` WHERE started_at>=@stop LIMIT 1",{'stop':('TIMESTAMP',stopped_at.isoformat())}):fail('source journal after terminal gate')
    # The pilot ledger itself (independent of lease-table expiry): no checkpoint of a later lease or a foreign run since the wake.
    ledger=f"`{b.c['project_id']}.{b.c['datasets']['tenant_ops']}.BACKFILL_CHECKPOINTS`"
    foreign=b.select(f"SELECT COUNT(*) AS n FROM {ledger} WHERE updated_at>=@start AND entity!='orchestration' AND (lease_generation>@lease OR run_id!=@run)",
                     {'start':('TIMESTAMP',x['createTime']),'lease':('INT64',lease_generation),'run':('STRING',run_id)})
    if len(foreign)!=1 or foreign[0].get('n') not in (0,'0'):fail('pilot checkpoint beyond reconciled page')
    if D.digest(b.state(doc))!=recon['payload'].get('state_hash'):fail('reconciled page state changed')
    ss=b.request('GET',BF.SCHED_API+'/'+base+'/jobs?pageSize=100')
    if len(ss.get('jobs',[]))!=4 or ss.get('nextPageToken') or any(z['state']!='PAUSED' for z in ss['jobs']):fail('owner freeze required')
    if [D.digest(z) for z in SA.unresolved(b,m,records,fresh=True)]!=[stop_hash]:fail('other global STOP remains')
    accepted=sorted(SA.accepted_hashes(b,m,records,fresh=True))
    rp=RP.load(b,m)
    if rp is None:fail('owner priority authority absent')
    # The replacement is read from its exact installed PAUSED/ENABLED descriptor (a candidate's own registration
    # file cannot precede its build); qualified_release verifies it against the packaged implementation.
    from tools.tenancy import full_fbo_diagnostic_closure as LQ
    source=b.c['orchestration']['job']['env']['CONTROLLER_SOURCE_SHA'];new=LQ.qualified_release(b,m,source)
    if new.get('terminal_gate_recovery_version')!=1 or new.get('verification',{}).get('terminal_gate_recovery_adapter')!='PASS':fail('replacement not qualified')
    facts=dict(controller=x,claims=sorted(claims),page=dict(index=index,shard=shard,sequence=page,receipt=D.digest(receipt),reconciliation=D.digest(recon),lease_generation=lease_generation),
        journal_rows=len(rows),accepted=accepted,recent=D.digest(rp),stopped_at=stopped_at.isoformat())
    p=dict(version=1,type=TYPE,tenant=m['tenant'],project=m['project'],root=m['hash'],manifest_hash=D.digest(m),stop_hash=stop_hash,execution=execution,
        index=index,shard=shard,page_sequence=page,receipt_hash=D.digest(receipt),recon_hash=D.digest(recon),lease_generation=lease_generation,claim_generation=own['generation'],recent_policy_hash=D.digest(rp),
        old_source=old['source_sha'],old_image=old['image'],new_source=source,new_image=new['image'],new_implementation_hash=new['controller_implementation_hash'],runtime_image=new['runtime_image'],
        owner_decision_sha256=owner_decision_sha256,qualification_hash=qualification_hash,security_hash=security_hash,effect_hash=D.digest(facts),source_probe_hash=source_probe_hash,
        protected_root_hashes=sorted(D.digest(z) for z in records),predicates=dict.fromkeys(PREDICATES,True),verified_at=b.clock().isoformat(),source_budget=0,historical_cause=CAUSE)
    p=sealed(p);validate(p,b,m,records);return p,facts
def fresh(b,p):
    if not 0<=(b.clock()-F.stamp(p['verified_at'])).total_seconds()<=FRESH_SECONDS:fail('fresh proof required')
def publish(b,p,m):
    header(p,m);fresh(b,p)
    again,_=observe(b,m,p['stop_hash'],index=p['index'],**{k:p[k] for k in ('owner_decision_sha256','qualification_hash','security_hash','source_probe_hash')})
    if any(p[k]!=again[k] for k in FIELDS-{'hash','verified_at'}):fail('fresh predicates changed')
    fresh(b,p)   # the bound is re-checked right before the first write: a slow re-observation publishes nothing
    for ds,n in ((b.c['datasets']['ref'],authority(p)),(b.c['datasets']['tenant_locks'],certificate(p))):
        if b.tables.get_table(ds,n) is None:b.tables.create_marker(ds,n,*value(p))
        if b.tables.get_table(ds,n)!=value(p):fail('publication conflict')
    b.store.commit(m['hash'],KIND,0,p,b.clock())
    if p not in load(b,m,b.store.history(m['hash'])):fail('independent acceptance differs')
    return p
