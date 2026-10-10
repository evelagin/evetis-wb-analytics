"""Exact failed attempt closure, source budget zero; no fabricated RECON/OK."""
import re
from tools.tenancy import durable_plan as D,tenant_backfill as BF,full_history as F,full_leaf_recovery as FL
from tools.tenancy.validation import parse_tenant_json as parse
import cooldown_failed as CF

KIND=CF.KIND
SHARD_KIND=CF.SHARD_KIND

def fail(message):CF.fail(message)
def name(root,source):return 'BFCF_AUTH_'+D.check_hash(root)+'_'+source
def value(p):return {'kind':'failed_cooldown','root':p['root'][:16]},D.encoded(p)
def cert_name(p):return 'BFCF_'+p['root']+'_'+p['stop_hash']

def validate_policy(p,b,m,*,historical=False,release=None):
    CF.policy(p)
    if (p['root'],p['manifest_hash'])!=(m['hash'],D.digest(m)):fail('frozen manifest differs')
    from tools.tenancy import full_runtime_handoff as FH,orchestration_contract as O
    if historical:
        if release is not None:fail('historical registration must remain immutable')
        release=parse((BF.REPO/'infra/tenant/releases/backfill'/f"{p['controller_source']}.json").read_text())
    else:
        # A current image is built before its own registration JSON exists.
        # Bootstrap already requires this immutable, exact deployment descriptor;
        # use the same authority instead of a future file in the built image.
        from tools.tenancy import cloud_controller as C
        env=b.c['orchestration']['job']['env']
        settings={'release':env['CONTROLLER_SOURCE_SHA'],'root_hash':m['hash'],
                  'scheduler_state':env['HISTORICAL_SCHEDULER_STATE']}
        if release is None:
            marker=b.tables.get_table(b.c['datasets']['tenant_locks'],C.descriptor_name(
                settings['release'],settings['root_hash'],settings['scheduler_state']))
            descriptor=parse(marker[1]) if marker else {}
            if set(descriptor)!={'settings','release'} or descriptor['settings']!=settings:
                fail('current qualified descriptor unavailable or differs')
            release=descriptor['release']
        if O.block(b.c,settings,BF.REPO,release)!=b.c['orchestration']:
            fail('current canonical descriptor differs')
    if (release['image'],release['controller_implementation_hash'],release['runtime_image'],release['runtime_implementation_hash'])!=(p['controller_image'],p['controller_implementation_hash'],p['runtime_image'],p['runtime_implementation_hash']):fail('qualified release differs')
    if not historical:
        O.verify_artifact_source(release,BF.REPO)
        if FH.runtime_facts(b.c,m)!={k:p[k] for k in ('runtime_source','runtime_image','runtime_implementation_hash')}:fail('qualified runtime differs')
    return p

def policy(b,m,source=None):
    source=source or b.c['orchestration']['job']['env']['CONTROLLER_SOURCE_SHA']
    marker=b.tables.get_table(b.c['datasets']['ref'],name(m['hash'],source))
    if not marker:fail('exact owner authority missing')
    p=parse(marker[1]);validate_policy(p,b,m,historical=source!=b.c['orchestration']['job']['env']['CONTROLLER_SOURCE_SHA'])
    if marker!=value(p):fail('owner marker differs')
    return p

def publish_policy(b,p,m,release=None):
    validate_policy(p,b,m,release=release);n=name(m['hash'],p['controller_source']);old=b.tables.get_table(b.c['datasets']['ref'],n)
    if old is None:b.tables.create_marker(b.c['datasets']['ref'],n,*value(p))
    if b.tables.get_table(b.c['datasets']['ref'],n)!=value(p):fail('owner publication conflict')
    return n

def incident(records,sh,p):
    stop=FL.one(records,'STOPPED',p['stop_hash']);rec=FL.one(sh,'DISPATCH_RECEIPT',p['receipt_hash']);intent=FL.one(sh,'DISPATCH_INTENT',p['intent_hash'])
    plans=[x['payload'] for x in records if x['kind']=='CHUNK_PLAN' and x['sequence']==p['index'] and x['payload']['shard_root']==p['shard']]
    if len(plans)!=1:fail('leaf authority ambiguous')
    doc=plans[0]['plan'];r=rec['payload']['receipt']
    if (doc['runtime_plan']['plan_id'],rec['sequence'],intent['sequence'],rec['root_hash'],intent['root_hash'])!=(p['plan_id'],4,4,p['shard'],p['shard']):fail('foreign failed authority')
    if F.shard_root(FR_manifest(),p['index'],doc)!=p['shard'] or rec['payload']['plan']!=doc or intent['payload']['plan']!=doc:fail('leaf/plan differs')
    if (r['run_id'],r['lease_generation'],r['ack_hash'])!=(p['run_id'],169,doc['ack_hash']) or any(intent['payload']['preparation'][k]!=r[k] for k in ('run_id','lease_generation','ack_hash')):fail('intent/run/generation differs')
    if any(x['kind']=='RECONCILED' and x['sequence']==4 for x in sh):fail('failed receipt must never become RECON')
    return doc,r

def FR_manifest():
    from pipelines.ozon.runtime import full_resume as FR
    return FR.manifest()

def closure(proof):return {'failed':True,'source_complete':False,'persisted_reconciled':False,
    'retry_category':CF.TYPE,'certificate_hash':proof['hash'],'run_id':proof['run_id'],'receipt_hash':proof['receipt_hash'],
    'state_hash':proof['state_hash'],'eligible_at':proof['eligible_at'],'source_budget':0}

def validate(proof,p,m,records,sh,*,final=False):
    CF.proof(proof,p);doc,r=incident(records,sh,p)
    prior=FL.one(sh,'RECONCILED',proof['prior_recon_hash']);previous=FL.one(sh,'DISPATCH_RECEIPT',proof['prior_receipt_hash'])
    if prior['sequence']!=3 or previous['sequence']!=3 or previous['payload']['plan']!=doc or prior['payload'].get('sequence')!=3 or prior['payload'].get('state_hash')!=p['state_hash'] or prior['payload'].get('persisted_reconciled') is not True or prior['payload'].get('source_complete') is not False:
        fail('accepted predecessor checkpoint differs')
    expected=D.digest({'version':D.VERSION,'root_hash':p['shard'],'kind':SHARD_KIND,'sequence':4,
        'payload':{'policy_hash':p['hash'],'stop_hash':p['stop_hash'],'receipt_hash':p['receipt_hash'],'state_hash':p['state_hash'],
            'run_id':p['run_id'],'failed':True,'source_complete':False,'persisted_reconciled':False,'eligible_at':proof['eligible_at'],'source_budget':0}})
    if expected!=proof['closure_hash']:fail('failed closure linkage differs')
    found=[x for x in sh if x['kind']==SHARD_KIND and x['sequence']==4]
    if found and (len(found)!=1 or D.digest(found[0])!=expected):fail('conflicting closure')
    if final and len(found)!=1:fail('failed closure not committed')
    return doc,r

def observe(base,m):
    from tools.tenancy import full_controller as H,cloud_controller as C
    p=policy(base,m);records=base.store.history(m['hash']);sh=base.store.history(p['shard'],max_records=F.MAX_LEAF_RECORDS)
    doc,r=incident(records,sh,p);b=FL.observer(base,m,p['index'])
    previous=[z for z in sh if z['kind']=='DISPATCH_RECEIPT' and z['sequence']==3]
    prior=[z for z in sh if z['kind']=='RECONCILED' and z['sequence']==3]
    if len(previous)!=1 or len(prior)!=1:fail('exact accepted predecessor missing')
    H.verify_authority(b,m);b.preflight({'hash':m['hash'],'plans':[doc]})
    if b.current_execution is not None or b.c['orchestration']['job']['env']['HISTORICAL_SCHEDULER_STATE']!='PAUSED':fail('recovery must be inactive and paused')
    if any(x['kind'] in {'DISPATCH_INTENT','DISPATCH_RECEIPT','RECONCILED'} and x['sequence']>4 for x in sh):fail('successor authority')
    base_name,jobs=BF.resources(b.c);execution_name=base_name+'/jobs/ozon-runtime-daily/executions/'+p['runtime_execution']
    op=b.request('GET',BF.RUN_API+'/'+r['operation']);x=b.request('GET',BF.RUN_API+'/'+execution_name)
    if not op.get('done') or not op.get('error') or op.get('metadata',{}).get('name')!=execution_name:fail('failed operation differs')
    if not x.get('completionTime') or x.get('failedCount')!=1 or x.get('succeededCount',0) or x.get('retriedCount',0) or x.get('cancelledCount',0) or x.get('runningCount',0) or x.get('taskCount')!=1:fail('terminal failed execution unknown')
    t=x['template'];cs=t['containers'];env={v['name']:v.get('value') for v in cs[0]['env']} if len(cs)==1 else {}
    expected={'INGESTION_RUN_ID':p['run_id'],'ENTITIES':'ads_sku_daily','BACKFILL_TARGET_PROJECT':p['project'],
        'BACKFILL_MODE':BF.B.VERSION,'SINCE':'2026-10-05','UNTIL':'2026-10-07','BACKFILL_GENERATION':doc['runtime_plan']['generation'],
        'BACKFILL_ORIGIN':doc['runtime_plan']['origin'],'TENANT_BINDING_REQUIRED':'1','STRICT_PAGE_CAPS':'1',
        'BACKFILL_MAX_REQUESTS':'400','BACKFILL_MAX_UNITS':'20'}
    if len(cs)!=1 or cs[0]['image']!=p['original_runtime_image'] or t['serviceAccount']!='sa-ozon-runtime@mpa-t-client-001.iam.gserviceaccount.com' or any(env.get(k)!=v for k,v in expected.items()) or len(env)!=len(cs[0]['env']):fail('historical source scope/image differs')
    for job in jobs:
        for e in BF.execution_inventory(b.request,base_name+'/jobs/'+job)['executions']:
            if not e.get('completionTime'):fail('active source execution')
            if e.get('createTime','')>x['createTime']:fail('successor source execution')
    rows=b.select(f"SELECT ingestion_run_id,status,backfill_plan_id,backfill_sequence,evidence_json,backfill_detail_json,error_message,requests,retry_count,rows_received,rows_inserted,rows_updated FROM `{b.journal}` WHERE entity='ads_sku_daily' AND (backfill_plan_id=@pid OR ingestion_run_id=@run OR STARTS_WITH(ingestion_run_id,CONCAT(@run,'-u'))) ORDER BY started_at LIMIT 50",{'pid':('STRING',p['plan_id']),'run':('STRING',p['run_id'])})
    if len(rows)>=50:fail('source unit inventory truncated')
    failed=[z for z in rows if z['ingestion_run_id']==p['run_id']]
    if len(failed)!=1:fail('original FAILED aggregate ambiguous')
    f=failed[0]
    if (f['status'],f['backfill_plan_id'],f['backfill_sequence'],f['error_message'],f['requests'],f['retry_count'],f['rows_received'],f['rows_inserted'],f['rows_updated'])!=('FAILED',None,None,"EvidenceError('SOURCE_REPEATED_LIMIT_OWNER_REVIEW')",3,0,0,0,0):fail('original failure class/effects differs')
    units=[z for z in rows if z['backfill_sequence'] is not None]
    if sorted(z['backfill_sequence'] for z in units)!=[1,2,3]:fail('new/gapped source units')
    for z in units:
        s=parse(z['evidence_json']);d=parse(z['backfill_detail_json']);pr=s['state']['progress']
        BF.B.validate(doc['runtime_plan'],s['state'])
        if s['plan']!=doc['runtime_plan'] or z['status']!='OK' or d.get('action')!='SOURCE_THROTTLED' or d.get('source_diagnostic',{}).get('http_status')!=429 or 'pending' in pr or pr.get('report') is not None or any(z[k]!=0 for k in ('rows_received','rows_inserted','rows_updated')):fail('not initialization-only source rejection')
    s=parse(next(z for z in units if z['backfill_sequence']==3)['evidence_json'])['state']
    if D.digest(s)!=p['state_hash'] or b.state(doc)!=s:fail('source checkpoint differs')
    if C.timestamp(s['progress']['rate_limit']['eligible_at'])<=C.timestamp(x['startTime']):fail('not active cooldown defect')
    columns=b.select(f"SELECT table_name,column_name FROM `{p['project']}.ozon_raw.INFORMATION_SCHEMA.COLUMNS` WHERE STARTS_WITH(table_name,'RAW_') AND column_name IN ('ingestion_run_id','run_id') ORDER BY table_name,column_name",{})
    if len(columns)!=14 or any(not re.fullmatch('[A-Za-z0-9_]+',v) for z in columns for v in z.values()):fail('RAW provenance inventory changed')
    q=' UNION ALL '.join(f"SELECT '{z['table_name']}' AS table_name,COUNT(*) AS n FROM `{p['project']}.ozon_raw.{z['table_name']}` WHERE {z['column_name']}=@run OR STARTS_WITH({z['column_name']},CONCAT(@run,'-u'))" for z in columns)
    footprint=b.select(q,{'run':('STRING',p['run_id'])})
    if len(footprint)!=14 or any(z['n']!=0 for z in footprint):fail('failed run has business rows/unknown footprint')
    from datetime import timedelta
    start=C.timestamp(x['startTime'])-timedelta(seconds=5);end=C.timestamp(x['completionTime'])+timedelta(seconds=5)
    jl=b.request('GET',BF.TT.BQ+'/projects/'+p['project']+'/jobs?allUsers=true&projection=full&maxResults=1000&minCreationTime='+str(int(start.timestamp()*1000))+'&maxCreationTime='+str(int(end.timestamp()*1000)))
    if jl.get('nextPageToken'):fail('job inventory truncated')
    jobs_proof=[]
    for j in jl.get('jobs',[]):
        if j.get('user_email')!='sa-ozon-runtime@mpa-t-client-001.iam.gserviceaccount.com':continue
        ref=j['jobReference'];v=b.request('GET',BF.TT.BQ+'/projects/'+p['project']+'/jobs/'+ref['jobId']+'?location='+ref.get('location','EU'))
        if v.get('status',{}).get('errorResult') or v.get('statistics',{}).get('query',{}).get('statementType')!='SELECT':fail('source business write/unknown query')
        jobs_proof.append(ref['jobId'])
    if len(jobs_proof)!=2:fail('runtime query count changed')
    cid=D.digest(['BOUNDED_PILOT_EXCLUSIVE',p['project']])[:16];ln=f'L_{cid}_0169';ld=f'LD_{cid}_0169'
    lease=b.tables.get_table(b.c['datasets']['tenant_locks'],ln)
    if not lease or lease[0].get('owner')!=p['run_id'] or parse(lease[1]).get('ack_hash')!=doc['ack_hash']:fail('lease attribution differs')
    for n,*_ in b.tables.list_tables(b.c['datasets']['tenant_locks']):
        match=BF.CK.LEASE_RE.match(n)
        if match and match[1]==cid and int(match[2])>169:fail('later lease generation')
    cp=b.select(f"SELECT status,lease_generation,lease_owner,error_code,evidence_json FROM `{p['project']}.tenant_ops.BACKFILL_CHECKPOINTS` WHERE plan_hash=@ack AND lease_generation=@generation",{'ack':('STRING',doc['ack_hash']),'generation':('INT64',169)})
    original=[z for z in cp if z['status']=='RUNNING']
    if len(original)!=1 or original[0]['lease_owner']!=p['run_id'] or parse(original[0]['evidence_json'])!={'mode':'BOUNDED_PILOT','proof':None}:fail('original RUNNING checkpoint changed')
    if any(z['status'] not in {'RUNNING','FAILED'} for z in cp):fail('unexpected terminal checkpoint')
    proof=dict(CF.EXACT,version=1,type=CF.TYPE,tenant=p['tenant'],project=p['project'],policy_hash=p['hash'],intent_hash=p['intent_hash'],manifest_hash=p['manifest_hash'],failed_aggregate_hash=D.digest(f),unit_evidence_hash=D.digest(units),lease_hash=D.digest(lease),checkpoint_hash=D.digest(original[0]),prior_receipt_hash=D.digest(previous[0]),prior_recon_hash=D.digest(prior[0]),eligible_at=s['progress']['rate_limit']['eligible_at'],lease_release=ld,lease_release_hash=D.digest(({'owner':p['run_id']},D.encoded({'operation':r['operation'],'ack_hash':doc['ack_hash'],'terminal':'FAILED_COOLDOWN_INIT'}))),closure_hash='',predicates={k:True for k in CF.PREDICATES},verified_at=base.clock().isoformat())
    payload={'policy_hash':p['hash'],'stop_hash':p['stop_hash'],'receipt_hash':p['receipt_hash'],'state_hash':p['state_hash'],'run_id':p['run_id'],'failed':True,'source_complete':False,'persisted_reconciled':False,'eligible_at':proof['eligible_at'],'source_budget':0}
    proof['closure_hash']=D.digest({'version':D.VERSION,'root_hash':p['shard'],'kind':SHARD_KIND,'sequence':4,'payload':payload})
    proof=CF.sealed(proof);validate(proof,p,m,records,sh)
    expected={'mode':'BOUNDED_PILOT','failed_cooldown_certificate':proof['hash'],'source_state_hash':p['state_hash'],'source_budget':0}
    for z in cp:
        if z['status']=='FAILED':
            fence=base.tables.get_table(base.c['datasets']['tenant_locks'],cert_name(p))
            old=parse(fence[1]) if fence else None
            if not old:fail('unfenced FAILED checkpoint')
            validate(old,p,m,records,sh)
            expected['failed_cooldown_certificate']=old['hash']
            if z['lease_owner']!=p['run_id'] or z['error_code']!='QUOTA' or parse(z['evidence_json'])!=expected:fail('conflicting FAILED checkpoint')
    return proof,payload,doc,r

def load(b,m,records):
    out=[]
    for row in records:
        if row['kind']!=KIND:continue
        proof=row['payload']
        # The original owner policy remains the immutable authority of this incident.
        names=[n for n,*_ in b.tables.list_tables(b.c['datasets']['ref']) if n.startswith('BFCF_AUTH_'+m['hash']+'_')]
        matches=[]
        for n in names:
            marker=b.tables.get_table(b.c['datasets']['ref'],n);p=parse(marker[1])
            if p.get('hash')==proof.get('policy_hash'):
                validate_policy(p,b,m,historical=p['controller_source']!=b.c['orchestration']['job']['env']['CONTROLLER_SOURCE_SHA']);matches.append(p)
        if len(matches)!=1:fail('historical exact owner policy missing')
        p=matches[0];sh=b.store.history(proof['shard'],max_records=F.MAX_LEAF_RECORDS);doc,r=validate(proof,p,m,records,sh,final=True)
        if row['root_hash']!=m['hash'] or row['sequence']!=p['index'] or b.tables.get_table(b.c['datasets']['tenant_locks'],cert_name(p))!=value(proof):fail('certificate/root fence missing')
        release=b.tables.get_table(b.c['datasets']['tenant_locks'],proof['lease_release'])
        if D.digest(release)!=proof['lease_release_hash']:fail('terminal failed release changed')
        cp=b.select(f"SELECT DISTINCT status,lease_owner,error_code,evidence_json FROM `{p['project']}.tenant_ops.BACKFILL_CHECKPOINTS` WHERE plan_hash=@ack AND lease_generation=169",{'ack':('STRING',doc['ack_hash'])})
        expected={'mode':'BOUNDED_PILOT','failed_cooldown_certificate':proof['hash'],'source_state_hash':p['state_hash'],'source_budget':0}
        if len(cp)!=2 or {z['status'] for z in cp}!={'RUNNING','FAILED'}:fail('original/additive checkpoint linkage missing')
        for z in cp:
            wanted=expected if z['status']=='FAILED' else {'mode':'BOUNDED_PILOT','proof':None}
            if z['lease_owner']!=p['run_id'] or parse(z['evidence_json'])!=wanted or z['error_code']!=('QUOTA' if z['status']=='FAILED' else None):fail('checkpoint linkage changed')
        out.append(proof)
    if len(out)>1:fail('only exact single recovery accepted')
    return out

def apply(b,m):
    p=policy(b,m);records=b.store.history(m['hash']);old=load(b,m,records)
    if old:return old[0]
    from tools.tenancy import full_leaf_recovery as FL
    accepted=FL.load(b,m,records) if any(x['kind']==FL.KIND for x in records) else []
    unresolved={D.digest(x) for x in records if x['kind']=='STOPPED'}-{x['stop_hash'] for x in accepted}
    if unresolved!={p['stop_hash']}:fail('another unresolved incident')
    proof,payload,doc,r=observe(b,m);fence=b.tables.get_table(b.c['datasets']['tenant_locks'],cert_name(p))
    if fence:
        previous=parse(fence[1]);validate(previous,p,m,records,b.store.history(p['shard']))
        for k in ('failed_aggregate_hash','unit_evidence_hash','lease_hash','checkpoint_hash','state_hash'):
            if previous[k]!=proof[k]:fail('recovery retry evidence changed')
        proof=previous
    else:b.tables.create_marker(b.c['datasets']['tenant_locks'],cert_name(p),*value(proof))
    if b.tables.get_table(b.c['datasets']['tenant_locks'],cert_name(p))!=value(proof):fail('certificate fence conflict')
    row=BF.checkpoint(doc,p['run_id'],'FAILED',169,F.stamp(proof['verified_at']))
    row['error_code']='QUOTA';row['evidence_json']=D.encoded({'mode':'BOUNDED_PILOT','failed_cooldown_certificate':proof['hash'],'source_state_hash':p['state_hash'],'source_budget':0})
    existing=b.select(f"SELECT evidence_json FROM `{p['project']}.tenant_ops.BACKFILL_CHECKPOINTS` WHERE plan_hash=@ack AND lease_generation=169 AND status='FAILED'",{'ack':('STRING',doc['ack_hash'])})
    if not existing:b.tables.append(b.c['datasets']['tenant_ops'],'BACKFILL_CHECKPOINTS',[row])
    existing=b.select(f"SELECT DISTINCT evidence_json FROM `{p['project']}.tenant_ops.BACKFILL_CHECKPOINTS` WHERE plan_hash=@ack AND lease_generation=169 AND status='FAILED'",{'ack':('STRING',doc['ack_hash'])})
    if existing!=[{'evidence_json':row['evidence_json']}]:fail('FAILED checkpoint readback unproven')
    release=({'owner':p['run_id']},D.encoded({'operation':r['operation'],'ack_hash':doc['ack_hash'],'terminal':'FAILED_COOLDOWN_INIT'}))
    ld=b.tables.get_table(b.c['datasets']['tenant_locks'],proof['lease_release'])
    if ld is None:b.tables.create_marker(b.c['datasets']['tenant_locks'],proof['lease_release'],*release)
    if b.tables.get_table(b.c['datasets']['tenant_locks'],proof['lease_release'])!=release:fail('FAILED lease closure conflict')
    h=b.store.commit(p['shard'],SHARD_KIND,4,payload,b.clock())
    if h!=proof['closure_hash']:fail('closure digest changed')
    b.store.commit(m['hash'],KIND,p['index'],proof,b.clock())
    if load(b,m,b.store.history(m['hash']))!=[proof]:fail('final recovery readback differs')
    return proof

def publish_accepted(b,m):
    """Owner-only quota attestation after independently validated additive closure."""
    found=load(b,m,b.store.history(m['hash']))
    if len(found)!=1:fail('accepted recovery missing')
    proof=found[0];p=policy(b,m);CF.proof(proof,p)
    doc={'policy':p,'proof':proof};n='BFCF_ACCEPTED_'+proof['hash'];v=({'kind':'failed_cooldown'},D.encoded(doc))
    old=b.tables.get_table(b.c['datasets']['ref'],n)
    if old is None:b.tables.create_marker(b.c['datasets']['ref'],n,*v)
    if b.tables.get_table(b.c['datasets']['ref'],n)!=v:fail('accepted quota attestation differs')
    return n
