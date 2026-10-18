"""Qualified local quarantine of zero-effect typed FBO timestamp failures.

The business leaf remains incomplete. A source-free closure only settles the
failed authority; it never advances source state, creates RECON or authorizes a
source retry. Independent leaves may run after every global integrity gate.
"""
import re
from datetime import timedelta
from tools.tenancy import durable_plan as D,tenant_backfill as BF,full_history as F
from tools.tenancy.validation import parse_tenant_json as parse

KIND='FULL_LEAF_QUARANTINED_SOURCE_CONTRACT'
SHARD_KIND='DISPATCH_SOURCE_CONTAINED_FAILED_CLOSED'
TYPE='ZERO_EFFECT_TYPED_FBO_WINDOW_LOCAL_QUARANTINE_V1'
CLASSES=frozenset({'OUTSIDE_REQUESTED_INTERVAL','MISSING_TIMEZONE','INVALID_TIMESTAMP_FORMAT'})
PREDICATES=frozenset({'exact_failed_runtime','exact_stop_intent_receipt','typed_local_timestamp_failure',
    'initial_source_state','zero_RAW_provenance','zero_OK_source_units','no_runtime_bigquery_writes',
    'no_async_external_effect','no_successor_intent_receipt','no_successor_runtime','no_RECON',
    'source_jobs_inactive_controller_fenced','exact_scheduler_policy','exact_original_checkpoint_lease','no_later_source_lease',
    'bindings_security_healthy','manifest_recent_unchanged','previous_certificates_accepted',
    'qualified_diagnostic_and_isolation','explicit_owner_scope','source_budget_zero'})
FIELDS=frozenset({'version','type','tenant','project','root','manifest_hash','index','shard','plan_id','ack_hash',
    'stop_hash','controller_execution','execution','run_id','generation','sequence','intent_hash','receipt_hash',
    'original_runtime_image','controller_source','controller_image','runtime_image','runtime_implementation_hash',
    'owner_decision_sha256','qualification_hash','security_hash','effect_hash','checkpoint_hash','lease_hash',
    'protected_root_hashes','protected_shard_hashes','predicates','verified_at','source_budget',
    'state_hash','validation_class','status','isolation_scope','hash'})
def fail(s):raise BF.B.EvidenceError('local FBO quarantine: '+s)
def sealed(p):return dict(p,hash=D.digest(p))
def auth(p):return 'BFLQ_AUTH_'+p['root']+'_'+p['stop_hash']
def cert(p):return 'BFLQ_'+p['hash']
def value(p):return {'kind':'local_quarantine','root':p['root'][:16]},D.encoded(p)
def closure(p):return dict(version=D.VERSION,root_hash=p['shard'],kind=SHARD_KIND,sequence=p['sequence'],payload={k:p[k] for k in ('type','stop_hash','intent_hash','receipt_hash','run_id','generation','source_budget','status')})
def release_value(p):return {'owner':p['run_id']},D.encoded({'terminal':'ZERO_EFFECT_FAILED','ack_hash':p['ack_hash'],'certificate_hash':p['hash']})

POLICY_FIELDS=frozenset({'version','type','tenant','project','root','manifest_hash','controller_source','controller_image','runtime_image','runtime_implementation_hash','owner_decision_sha256','qualification_hash','security_hash','source_budget','classes','isolation_scope','hash'})
def policy_name(root,source):return 'BFLQP_'+root+'_'+source
def qualified_release(b,m,source):
    from tools.tenancy import orchestration_contract as O,cloud_controller as C
    env=b.c['orchestration']['job']['env']
    if source==env['CONTROLLER_SOURCE_SHA']:
        settings=dict(release=source,root_hash=m['hash'],scheduler_state=env['HISTORICAL_SCHEDULER_STATE'])
        marker=b.tables.get_table(b.c['datasets']['tenant_locks'],C.descriptor_name(source,m['hash'],settings['scheduler_state']))
        descriptor=parse(marker[1]) if marker else {}
        if set(descriptor)!={'settings','release'} or descriptor['settings']!=settings:fail('current qualified descriptor absent/differs')
        r=descriptor['release']
        if r.get('source_sha')!=source:fail('current qualified source differs')
        O.verify_artifact_source(r,BF.REPO)
    else:r=parse((BF.REPO/'infra/tenant/releases/backfill'/f'{source}.json').read_text())
    return r
def owner_policy(b,m,source=None):
    source=source or b.c['orchestration']['job']['env']['CONTROLLER_SOURCE_SHA']
    marker=b.tables.get_table(b.c['datasets']['ref'],policy_name(m['hash'],source))
    if marker is None:return None
    p=parse(marker[1])
    if (set(p)!=POLICY_FIELDS or p['version']!=1 or p['type']!=TYPE or p['hash']!=D.digest({k:v for k,v in p.items() if k!='hash'})
        or (p['tenant'],p['project'],p['root'],p['manifest_hash'])!=(m['tenant'],m['project'],m['hash'],D.digest(m))
        or p['controller_source']!=source or type(p['source_budget']) is not int or p['source_budget']!=0
        or p['classes']!=sorted(CLASSES) or p['isolation_scope']!='FBO_SOURCE'
        or marker!=({'kind':'local_quarantine_policy','root':m['hash'][:16]},D.encoded(p))):fail('qualified owner isolation policy differs')
    r=qualified_release(b,m,source)
    if (r.get('fbo_source_isolation_version')!=1 or r['verification'].get('fbo_source_isolation_adapter')!='PASS'
        or (r['image'],r['runtime_image'],r['runtime_implementation_hash'])!=(p['controller_image'],p['runtime_image'],p['runtime_implementation_hash'])):fail('owner policy qualified pair differs')
    return p

def header(p,m):
    if not isinstance(p,dict) or set(p)!=FIELDS or type(p['version']) is not int or p['version']!=1 or p['type']!=TYPE or p['hash']!=D.digest({k:v for k,v in p.items() if k!='hash'}):fail('closed certificate required')
    if (p['tenant'],p['project'],p['root'],p['manifest_hash'])!=(m['tenant'],m['project'],m['hash'],D.digest(m)):fail('foreign root')
    if type(p['index']) is not int or not 0<=p['index']<len(m['programs']) or m['programs'][p['index']]['entity']!='fbo_postings':fail('FBO leaf required')
    if p['isolation_scope'] not in {'LEAF','FBO_SOURCE'}:fail('bounded source isolation required')
    if p['validation_class'] not in CLASSES or p['status']!='QUARANTINED_SOURCE_CONTRACT' or type(p['source_budget']) is not int or p['source_budget']!=0:fail('source-contained quarantine only')
    if any(type(p[k]) is not int or p[k]<1 for k in ('generation','sequence')):fail('exact source authority required')
    if not isinstance(p['predicates'],dict) or set(p['predicates'])!=PREDICATES or any(v is not True for v in p['predicates'].values()):fail('unknown mandatory predicate')
    for k in FIELDS:
        if k.endswith('_hash') or k.endswith('_sha256') or k in ('hash','root','shard'):D.check_hash(p[k])
    for k in ('protected_root_hashes','protected_shard_hashes'):
        if not isinstance(p[k],list) or not 1<=len(p[k])<=10000 or p[k]!=sorted(set(p[k])):fail('bounded retained history required')
        for h in p[k]:D.check_hash(h)
    if not re.fullmatch('[0-9a-f]{40}',p['controller_source']):fail('immutable controller source required')
    F.stamp(p['verified_at']);return p

def validate(p,b,m,records,*,final=True):
    from tools.tenancy import full_leaf_recovery as FL,full_controller as H
    header(p,m)
    if H.only_manifest(records)!=m or not set(p['protected_root_hashes'])<={D.digest(r) for r in records}:fail('original root changed')
    stop=FL.one(records,'STOPPED',p['stop_hash'])
    expected={'reason':'CONTROLLER_GATE_OR_EVIDENCE_FAILURE','controller_execution':p['controller_execution'],
      'diagnostic':{'version':1,'stage':'RUNTIME_TERMINAL','category':'EVIDENCE','context':dict(index=p['index'],shard=p['shard'],sequence=p['sequence'],intent_hash=p['intent_hash'],receipt_hash=p['receipt_hash'])}}
    if stop['payload']!=expected:fail('only exact local source STOP covered')
    sh=b.store.history(p['shard'],max_records=F.MAX_LEAF_RECORDS)
    i=FL.one(sh,'DISPATCH_INTENT',p['intent_hash']);r=FL.one(sh,'DISPATCH_RECEIPT',p['receipt_hash'])
    doc=i['payload']['plan'];rc=r['payload']['receipt']
    if r['payload']['plan']!=doc or (doc['runtime_plan']['plan_id'],doc['ack_hash'])!=(p['plan_id'],p['ack_hash']) or doc['runtime_plan']['entity']!='fbo_postings':fail('frozen FBO plan differs')
    if i['sequence']!=p['sequence'] or r['sequence']!=p['sequence'] or rc['run_id']!=p['run_id'] or rc['lease_generation']!=p['generation'] or any(i['payload']['preparation'][k]!=rc[k] for k in ('run_id','lease_generation','ack_hash')):fail('exact failed authority differs')
    if F.shard_root(m,p['index'],doc)!=p['shard'] or not any(z['kind']=='CHUNK_PLAN' and z['sequence']==p['index'] and z['payload']['plan']==doc for z in records):fail('leaf/root ownership differs')
    if not set(p['protected_shard_hashes'])<={D.digest(z) for z in sh} or any(z['kind']=='RECONCILED' and z['sequence']==p['sequence'] for z in sh):fail('history changed or fake RECON')
    cid=BF.B.digest(['BOUNDED_PILOT_EXCLUSIVE',b.c['project_id']])[:16];ds=b.c['datasets']['tenant_locks'];ln=BF.CK.lease_name(cid,p['generation'])
    if D.digest(b.tables.get_table(ds,ln))!=p['lease_hash']:fail('original lease bytes differ')
    cp=b.select(f"SELECT * FROM `{p['project']}.tenant_ops.BACKFILL_CHECKPOINTS` WHERE entity='fbo_postings' AND plan_hash=@ack AND lease_generation=@gen ORDER BY updated_at LIMIT 30",{'ack':('STRING',p['ack_hash']),'gen':('INT64',p['generation'])})
    if D.digest(cp)!=p['checkpoint_hash']:fail('original checkpoint bytes differ')
    if b.state(doc)!=BF.B.initial(doc['runtime_plan']) or D.digest(b.state(doc))!=p['state_hash']:fail('quarantined source state changed')
    if final and (closure(p) not in sh or b.tables.get_table(ds,f'LD_{cid}_{p["generation"]:04d}')!=release_value(p)):fail('additive zero-effect closure absent')
    return doc,rc

def load(b,m,records):
    items=[r for r in records if r['kind']==KIND];out=[]
    if len(items)>len(m['programs']):fail('bounded quarantine inventory')
    for r in items:
        p=r['payload'];validate(p,b,m,records)
        if r['root_hash']!=m['hash'] or r['sequence']!=p['index']:fail('quarantine record scope differs')
        if b.tables.get_table(b.c['datasets']['tenant_locks'],cert(p))!=value(p):fail('exact certificate absent')
        authority=b.tables.get_table(b.c['datasets']['ref'],auth(p))
        if authority!=value(p):
            policy=owner_policy(b,m,p['controller_source'])
            if not policy or any(policy[k]!=p[k] for k in ('controller_source','controller_image','runtime_image','runtime_implementation_hash','owner_decision_sha256','qualification_hash','security_hash','isolation_scope')):fail('exact owner authority absent')
        if any(x['index']==p['index'] and x['hash']!=p['hash'] for x in out):fail('conflicting local quarantine')
        out.append(p)
    return out

def observe(b,m,index,sequence,*,owner_decision_sha256,qualification_hash,security_hash,cloud=False):
    from tools.tenancy import full_controller as H,full_leaf_recovery as FL,orchestration_contract as O,recent_priority as RP,full_runtime_handoff as FH
    records=b.store.history(m['hash']);plan=next(z['payload'] for z in records if z['kind']=='CHUNK_PLAN' and z['sequence']==index)
    sh=b.store.history(plan['shard_root'],max_records=F.MAX_LEAF_RECORDS)
    i=next(z for z in sh if z['kind']=='DISPATCH_INTENT' and z['sequence']==sequence);r=next(z for z in sh if z['kind']=='DISPATCH_RECEIPT' and z['sequence']==sequence);doc=i['payload']['plan'];rc=r['payload']['receipt'];rp=doc['runtime_plan'];run=rc['run_id'];gen=rc['lease_generation']
    if any(z['sequence']>sequence for z in sh) or any(z['kind']=='RECONCILED' and z['sequence']==sequence for z in sh):fail('successor/reconciliation exists')
    stop=[z for z in records if z['kind']=='STOPPED' and z['payload'].get('diagnostic',{}).get('context',{}).get('receipt_hash')==D.digest(r)]
    if len(stop)!=1:fail('exact one local source STOP required')
    if not cloud and b.current_execution is not None:fail('inactive owner observation required')
    if cloud and (b.current_execution is None or owner_policy(b,m) is None):fail('qualified cloud isolation authority required')
    H.verify_authority(H.Backend(b,m),m);priority=RP.load(b,m)
    if len(priority['selection'])!=69 or len(priority['parents'])!=64:fail('frozen RECENT selection differs')
    base,_=BF.resources(b.c);ss=b.request('GET',BF.SCHED_API+'/'+base+'/jobs?pageSize=100')
    if len(ss.get('jobs',[]))!=4 or ss.get('nextPageToken') or any(z['state']!=('ENABLED' if cloud and z['name'].endswith('/tenant-backfill-tick') else 'PAUSED') for z in ss['jobs']):fail('exact Scheduler state required')
    runtime_inventory=[]
    for n in (*b.c['marketplaces']['ozon']['jobs'],'tenant-control',O.JOB):
        xs=BF.execution_inventory(b.request,base+'/jobs/'+n)['executions']
        if n==O.JOB and cloud:
            from tools.tenancy import controller_cadence as CC
            if not getattr(b,'wake_claim',None):fail('canonical cloud controller claim required')
            CC.inventory(b,finalization_wait=False)  # may follow a committed STOP: never report a wait
        elif any(not x.get('completionTime') for x in xs):fail('active execution')
        if n in b.c['marketplaces']['ozon']['jobs']:runtime_inventory.extend(xs)
    op=b.request('GET',BF.RUN_API+'/'+rc['operation']);execution=op.get('metadata',{}).get('name');x=b.request('GET',BF.RUN_API+'/'+execution)
    if any(F.stamp(z['createTime'])>F.stamp(x['createTime']) and z['name']!=execution for z in runtime_inventory):fail('successor runtime exists')
    co=x['template']['containers'][0];env={z['name']:z.get('value') for z in co['env']}
    if (not op.get('done') or not op.get('error') or x.get('failedCount')!=1 or not x.get('completionTime') or x.get('retriedCount',0) or x.get('cancelledCount',0) or x.get('taskCount')!=1
        or env.get('INGESTION_RUN_ID')!=run or env.get('ENTITIES')!='fbo_postings' or env.get('SINCE')!=rp['from'] or env.get('UNTIL')!=rp['to']
        or env.get('TENANT_BINDING_REQUIRED')!='1' or env.get('STRICT_PAGE_CAPS')!='1' or x['template']['serviceAccount']!='sa-ozon-runtime@'+m['project']+'.iam.gserviceaccount.com'):fail('exact terminal failed runtime differs')
    j=b.select(f"SELECT ingestion_run_id,status,entity,backfill_sequence,evidence_json,error_message FROM `{b.journal}` WHERE ingestion_run_id=@run OR STARTS_WITH(ingestion_run_id,CONCAT(@run,'-u')) LIMIT 1000",{'run':('STRING',run)})
    if len(j)!=1 or (j[0]['ingestion_run_id'],j[0]['status'],j[0]['entity'],j[0]['backfill_sequence'])!=(run,'FAILED','fbo_postings',None):fail('source units/effects differ')
    proof=parse(j[0]['evidence_json']);diag=proof.get('fbo_window_validation',{});state=proof['state']
    if diag.get('validation_class') not in CLASSES or state!=BF.B.initial(rp) or b.state(doc)!=state:fail('typed local zero-state evidence required')
    if (diag.get('requested_utc_from'),diag.get('requested_utc_to'),diag.get('utc_until_exclusive'))!=(BF.B.stamp(state['progress']['pending'][0][0]),BF.B.stamp(state['progress']['pending'][0][1]-1),BF.B.stamp(state['progress']['pending'][0][1])):fail('request window differs')
    columns=b.select(f"SELECT table_name,column_name FROM `{m['project']}.ozon_raw.INFORMATION_SCHEMA.COLUMNS` WHERE STARTS_WITH(table_name,'RAW_') AND column_name IN ('ingestion_run_id','run_id') ORDER BY table_name,column_name",{})
    if len(columns)!=14 or any(not re.fullmatch('RAW_[A-Z0-9_]+',z['table_name']) for z in columns):fail('complete RAW provenance inventory required')
    raw=b.select(' UNION ALL '.join(f"SELECT '{z['table_name']}' AS table_name,COUNT(*) AS n FROM `{m['project']}.ozon_raw.{z['table_name']}` WHERE {z['column_name']}=@run OR STARTS_WITH({z['column_name']},CONCAT(@run,'-u'))" for z in columns),{'run':('STRING',run)})
    if len(raw)!=14 or any(type(z['n']) is not int or z['n']!=0 for z in raw):fail('nonzero/unknown source effects')
    start=F.stamp(x['startTime'])-timedelta(seconds=5);end=F.stamp(x['completionTime'])+timedelta(seconds=5)
    jobs=b.request('GET',BF.TT.BQ+'/projects/'+m['project']+'/jobs?allUsers=true&projection=full&maxResults=1000&minCreationTime='+str(int(start.timestamp()*1000))+'&maxCreationTime='+str(int(end.timestamp()*1000)))
    if jobs.get('nextPageToken'):fail('truncated runtime effect inventory')
    js=[]
    for z in jobs.get('jobs',[]):
        if z.get('user_email')!=x['template']['serviceAccount']:continue
        ref=z['jobReference'];q=b.request('GET',BF.TT.BQ+'/projects/'+m['project']+'/jobs/'+ref['jobId']+'?location='+ref.get('location','EU'))
        if q.get('status',{}).get('state')!='DONE' or q['status'].get('errorResult') or q.get('statistics',{}).get('query',{}).get('statementType')!='SELECT' or set(q['configuration'])-{'query','jobType'}:fail('runtime business write not excluded')
        js.append(q)
    if len(js)!=1:fail('complete zero-write runtime inventory required')
    import seller_policy as SP
    sp=parse((BF.REPO/'pipelines/ozon/runtime/seller_method_policy.json').read_text());SP.validate(sp)
    if any(sp['methods'][path].get('semantics')!='READ' or sp['methods'][path].get('side_effects')!='NONE' for path in ('/v3/posting/fbo/list','/v1/seller/info')):fail('ambiguous external effect')
    cid=BF.B.digest(['BOUNDED_PILOT_EXCLUSIVE',m['project']])[:16];ds=b.c['datasets']['tenant_locks'];lease=b.tables.get_table(ds,BF.CK.lease_name(cid,gen))
    if not lease or lease[0].get('owner')!=run or parse(lease[1]).get('ack_hash')!=doc['ack_hash'] or b.tables.get_table(ds,f'LD_{cid}_{gen:04d}') is not None:fail('original lease/release differs')
    if any((mt:=BF.CK.LEASE_RE.match(n)) and mt[1]==cid and int(mt[2])>gen for n,*_ in b.tables.list_tables(ds)):fail('later source authority')
    cp=b.select(f"SELECT * FROM `{m['project']}.tenant_ops.BACKFILL_CHECKPOINTS` WHERE entity='fbo_postings' AND plan_hash=@ack AND lease_generation=@gen ORDER BY updated_at LIMIT 30",{'ack':('STRING',doc['ack_hash']),'gen':('INT64',gen)})
    if len(cp)!=1 or cp[0]['status']!='RUNNING' or cp[0]['lease_owner']!=run:fail('original checkpoint differs')
    from tools.tenancy import full_cooldown_recovery as FC,full_policy_bootstrap_recovery as PB,full_pretransport_recovery as PT,full_first_receipt_recovery as FR,full_controller_closure_recovery as CR,full_fbo_zero_effect_recovery as ZE
    accepted=load(b,m,records)+FL.load(b,m,records)+FC.load(b,m,records)+PB.load(b,m,records)+PT.load(b,m,records)+FR.load(b,m,records)+CR.load(b,m,records)+ZE.load(b,m,records)
    if cloud:
        from tools.tenancy import full_pre_authority_gate_recovery as PAG
        covered={z['stop_hash'] for z in accepted if 'stop_hash' in z}|{h for z in accepted if 'stop_hashes' in z for h in z['stop_hashes']}|{z['stop_hash'] for z in PAG.load(b,m,records)}
        if {D.digest(z) for z in records if z['kind']=='STOPPED'}-covered!={D.digest(stop[0])}:fail('another global STOP blocks automatic local isolation')
    source=b.c['orchestration']['job']['env']['CONTROLLER_SOURCE_SHA'];r=qualified_release(b,m,source)
    if r.get('fbo_source_isolation_version')!=1 or r['verification'].get('fbo_source_isolation_adapter')!='PASS':fail('local-isolation/diagnostic image unqualified')
    effects=dict(raw=sorted(raw,key=lambda z:z['table_name']),journal=j,jobs=js,state=state,checkpoints=cp,lease=lease,previous_certificates=sorted(z['hash'] for z in accepted),priority_hash=priority['hash'])
    p=dict(version=1,type=TYPE,tenant=m['tenant'],project=m['project'],root=m['hash'],manifest_hash=D.digest(m),index=index,shard=plan['shard_root'],plan_id=rp['plan_id'],ack_hash=doc['ack_hash'],stop_hash=D.digest(stop[0]),controller_execution=stop[0]['payload']['controller_execution'],execution=execution,run_id=run,generation=gen,sequence=sequence,intent_hash=D.digest(i),receipt_hash=D.digest(next(z for z in sh if z['kind']=='DISPATCH_RECEIPT' and z['sequence']==sequence)),original_runtime_image=co['image'],controller_source=source,controller_image=b.c['orchestration']['job']['image'],runtime_image=b.c['marketplaces']['ozon']['runtime_image'],runtime_implementation_hash=BF.B.implementation_hash(),owner_decision_sha256=owner_decision_sha256,qualification_hash=qualification_hash,security_hash=security_hash,effect_hash=D.digest(effects),checkpoint_hash=D.digest(cp),lease_hash=D.digest(lease),protected_root_hashes=sorted(D.digest(z) for z in records),protected_shard_hashes=sorted(D.digest(z) for z in sh),predicates=dict.fromkeys(PREDICATES,True),verified_at=b.clock().isoformat(),source_budget=0,state_hash=D.digest(state),validation_class=diag['validation_class'],status='QUARANTINED_SOURCE_CONTRACT',isolation_scope='FBO_SOURCE')
    p=sealed(p);validate(p,b,m,records,final=False);return p,effects

def publish(b,p,m,*,cloud=False):
    header(p,m)
    if not 0 <= (b.clock()-F.stamp(p['verified_at'])).total_seconds() <= 900:fail('fresh proof required')
    fresh,_=observe(b,m,p['index'],p['sequence'],owner_decision_sha256=p['owner_decision_sha256'],qualification_hash=p['qualification_hash'],security_hash=p['security_hash'],cloud=cloud)
    if any(fresh[k]!=p[k] for k in FIELDS-{'hash','verified_at'}):fail('fresh effects/history changed')
    markers=((b.c['datasets']['tenant_locks'],cert(p)),) if cloud else ((b.c['datasets']['ref'],auth(p)),(b.c['datasets']['tenant_locks'],cert(p)))
    for ds,n in markers:
        old=b.tables.get_table(ds,n)
        if old is None:b.tables.create_marker(ds,n,*value(p))
        if b.tables.get_table(ds,n)!=value(p):fail('owner certificate conflict')
    cid=BF.B.digest(['BOUNDED_PILOT_EXCLUSIVE',p['project']])[:16];ds=b.c['datasets']['tenant_locks']
    b.tables.create_marker(ds,f'LD_{cid}_{p["generation"]:04d}',*release_value(p))
    z=closure(p);b.store.commit(p['shard'],SHARD_KIND,p['sequence'],z['payload'],b.clock());b.store.commit(p['root'],KIND,p['index'],p,b.clock())
    if p not in load(b,m,b.store.history(m['hash'])):fail('independent closure acceptance differs')
    return p


def automatic(b,doc,receipt):
    """No blanket FAILED recovery: only this qualified, owner-scoped class."""
    from tools.tenancy import full_controller as H
    m=b.manifest;p=owner_policy(b,m)
    if p is None or doc['runtime_plan']['entity']!='fbo_postings':return None
    # Check class before appending a local STOP; unrelated errors remain root STOP.
    rows=b.select(f"SELECT DISTINCT evidence_json FROM `{b.journal}` WHERE ingestion_run_id=@run AND entity='fbo_postings' AND status='FAILED' LIMIT 2",{'run':('STRING',receipt['run_id'])})
    if len(rows)!=1:return None
    diag=parse(rows[0]['evidence_json']).get('fbo_window_validation',{})
    if diag.get('validation_class') not in CLASSES:return None
    records=b.store.history(m['hash']);item=next(r['payload'] for r in records if r['kind']=='CHUNK_PLAN' and r['sequence']==b.index)
    sh=b.store.history(item['shard_root']);rc=next(r for r in sh if r['kind']=='DISPATCH_RECEIPT' and r['payload']['receipt']==receipt)
    intent=next(r for r in sh if r['kind']=='DISPATCH_INTENT' and r['sequence']==rc['sequence'])
    stop={'reason':'CONTROLLER_GATE_OR_EVIDENCE_FAILURE','controller_execution':b.current_execution,'diagnostic':{'version':1,'stage':'RUNTIME_TERMINAL','category':'EVIDENCE','context':dict(index=b.index,shard=item['shard_root'],sequence=rc['sequence'],intent_hash=D.digest(intent),receipt_hash=D.digest(rc))}}
    b.store.commit(m['hash'],'STOPPED',0,stop,b.clock())
    cert,effects=observe(b,m,b.index,rc['sequence'],**{k:p[k] for k in ('owner_decision_sha256','qualification_hash','security_hash')},cloud=True)
    publish(b,cert,m,cloud=True)
    return {'local_quarantine':cert['hash'],'index':b.index,'source_dispatches':0,'status':'LOCAL_SOURCE_QUARANTINED'}
