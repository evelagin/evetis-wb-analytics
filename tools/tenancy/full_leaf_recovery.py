"""Owner-qualified FULL root -> shard class-C recovery; no source transport.

An owner-only ref policy pins the tenant, frozen manifest and exact replacement
artifact. Initial recovery is exact-owner-scoped. Automatic recovery is enabled
only after that initial incident has a committed recovery and shard RECON.
Generic/security/dispatch STOPs never become an automatic exemption.
"""
import re
from tools.tenancy import durable_plan as D, tenant_backfill as BF, full_history as F
from tools.tenancy.validation import parse_tenant_json as parse

TYPE='FULL_LEAF_POST_DISPATCH_TERMINAL_SUCCESS_UNRECONCILED_STOP'
VERSION=1
KIND='FULL_LEAF_STOP_RECOVERED'
POLICY_FIELDS=frozenset({'version','type','tenant','project','root','manifest_hash',
    'controller_source','controller_image','controller_implementation_hash',
    'runtime_source','runtime_image','runtime_implementation_hash','owner_ack_sha256',
    'initial_stop','initial_receipt','initial_intent','initial_generation',
    'initial_unit_start','initial_unit_end','automatic_class_c','hash'})
FIELDS=frozenset({'version','type','tenant','project','root','manifest_hash','index','shard',
    'stop_hash','intent_hash','receipt_hash','prior_receipt_hash','prior_recon_hash',
    'receipt_sequence','runtime_execution','run_id','generation','unit_start','unit_end',
    'state_hash','lease_release','lease_release_hash','unit_evidence_hash','coverage_hash',
    'policy_hash','implementation_source','controller_image','runtime_source','runtime_image',
    'reconciliation','reconciliation_hash','predicates','verified_at','hash'})
PREDICATES=frozenset({'exact_scope','terminal_success','exact_units','predecessor_reconciled',
    'checkpoint_exact','persisted_keys','no_duplicates','no_gaps','no_ambiguous_retry',
    'one_child','no_successor','exact_generation','existing_terminal_release',
    'no_later_generation','no_conflicting_execution','bindings_valid','immutable_authority',
    'original_stop_preserved','source_budget_zero'})
RECON_FIELDS=frozenset({'checkpoint','sequence','rows_observed','orders','supplies','bundles','coverage','lifecycle_changed','scheduler_changed','coverage_readback','source_complete','persisted_reconciled','state_hash'})
AUTO_STAGES=frozenset({'RUNTIME_TERMINAL','CHECKPOINT','LEASE',
    'PERSISTED_SOURCE_RECONCILIATION','RECONCILIATION_APPEND'})


def fail(message):raise BF.B.EvidenceError('full leaf recovery: '+message)
def sealed(value):return dict(value,hash=D.digest(value))
def policy_name(root,source):return 'BFFLR_AUTH_'+D.check_hash(root)+'_'+source
def policy_value(p):return {'kind':'full_leaf_recovery','root':p['root'][:16]},D.encoded(p)


def validate_policy(p,manifest,release,*,historical=False):
    F.validate_manifest(manifest)
    if not isinstance(p,dict) or set(p)!=POLICY_FIELDS:fail('closed owner policy required')
    if p['version']!=VERSION or type(p['version']) is not int or p['type']!=TYPE:fail('unsupported policy')
    if D.digest({k:v for k,v in p.items() if k!='hash'})!=p['hash']:fail('policy digest differs')
    for k in ('root','manifest_hash','owner_ack_sha256','initial_stop','initial_receipt','initial_intent','hash'):
        D.check_hash(p[k])
    if (p['tenant'],p['project'])!=('client_001','mpa-t-client-001'):fail('foreign automatic policy')
    if (p['tenant'],p['project'],p['root'],p['manifest_hash'])!=(manifest['tenant'],manifest['project'],manifest['hash'],D.digest(manifest)):
        fail('policy/frozen manifest differs')
    if any(p[k]!=manifest[k.replace('runtime_source','runtime_source_sha')] for k in ('runtime_source','runtime_image','runtime_implementation_hash')):
        fail('runtime provenance changed')
    if (p['controller_source'],p['controller_image'],p['controller_implementation_hash'])!=(release['source_sha'],release['image'],release['controller_implementation_hash']):
        fail('unqualified policy artifact')
    for k in ('controller_source','runtime_source'):
        if not re.fullmatch('[0-9a-f]{40}',p[k]):fail('unknown source')
    for k in ('initial_generation','initial_unit_start','initial_unit_end'):
        if type(p[k]) is not int or p[k]<1:fail('invalid initial scope')
    if p['initial_unit_end']<p['initial_unit_start'] or p['automatic_class_c'] is not True:fail('closed class authority required')
    from tools.tenancy import orchestration_contract as O
    if not historical:O.verify_artifact_source(release,BF.REPO)
    if release['schema_version'] not in {2,3,4} or release['verification']['full_history_adapter']!='PASS':fail('full artifact not qualified')
    return p


def policy(backend,manifest,release=None):
    source=backend.c['orchestration']['job']['env']['CONTROLLER_SOURCE_SHA']
    if release is None:
        from tools.tenancy import cloud_controller as C
        env=backend.c['orchestration']['job']['env']
        descriptor=backend.tables.get_table(backend.c['datasets']['tenant_locks'],C.descriptor_name(source,manifest['hash'],env['HISTORICAL_SCHEDULER_STATE']))
        if not descriptor:fail('qualified descriptor absent')
        release=parse(descriptor[1]).get('release',{})
    marker=backend.tables.get_table(backend.c['datasets']['ref'],policy_name(manifest['hash'],source))
    if not marker:fail('owner class/release authority absent')
    p=parse(marker[1]);validate_policy(p,manifest,release)
    if marker!=policy_value(p):fail('owner policy metadata differs')
    return p


def publish_policy(backend,p,manifest,release):
    """Owner-only ref write. Cloud append identity cannot grant this permission."""
    validate_policy(p,manifest,release)
    if backend.c['tenant_id']!=p['tenant'] or backend.c['project_id']!=p['project']:fail('publisher target differs')
    name=policy_name(p['root'],p['controller_source']);value=policy_value(p)
    old=backend.tables.get_table(backend.c['datasets']['ref'],name)
    if old is None:
        backend.tables.create_marker(backend.c['datasets']['ref'],name,*value)
        old=backend.tables.get_table(backend.c['datasets']['ref'],name)
    if old!=value:fail('policy conflict/readback missing')
    return name


def one(records,kind,h):
    found=[r for r in records if r['kind']==kind and D.digest(r)==h]
    if len(found)!=1:fail('exact '+kind+' missing/ambiguous')
    return found[0]


def validate(proof,manifest,p,root_records,shard_records,*,final=False):
    if not isinstance(proof,dict) or set(proof)!=FIELDS:fail('closed recovery certificate required')
    if proof['type']!=TYPE or type(proof['version']) is not int or proof['version']!=VERSION:fail('unsupported class')
    if proof['hash']!=D.digest({k:v for k,v in proof.items() if k!='hash'}):fail('certificate digest differs')
    if not isinstance(proof['predicates'],dict) or set(proof['predicates'])!=PREDICATES or any(v is not True for v in proof['predicates'].values()):fail('mandatory predicate unknown/false')
    for k in FIELDS:
        if k.endswith('_hash') or k in {'root','shard','hash'}:D.check_hash(proof[k])
    if (proof['tenant'],proof['project'],proof['root'],proof['manifest_hash'],proof['policy_hash'])!=(p['tenant'],p['project'],p['root'],p['manifest_hash'],p['hash']):fail('foreign authority')
    if (proof['implementation_source'],proof['controller_image'],proof['runtime_source'],proof['runtime_image'])!=(p['controller_source'],p['controller_image'],p['runtime_source'],p['runtime_image']):fail('artifact changed')
    index=proof['index'];seq=proof['receipt_sequence']
    if type(index) is not int or not 0<=index<len(manifest['programs']) or type(seq) is not int or seq<2:fail('predecessor authority required')
    for key in ('generation','unit_start','unit_end'):
        if type(proof[key]) is not int or proof[key]<1:fail('invalid generation/unit range')
    F.stamp(proof['verified_at'])
    if not isinstance(proof['reconciliation'],dict) or set(proof['reconciliation'])!=RECON_FIELDS:fail('closed reconciliation required')
    for key in ('rows_observed','orders','supplies','bundles'):
        if type(proof['reconciliation'][key]) is not int or proof['reconciliation'][key]<0:fail('unproven result counts')
    stops=one(root_records,'STOPPED',proof['stop_hash'])
    plans=[r for r in root_records if r['kind']=='CHUNK_PLAN' and r['sequence']==index and r['payload'].get('shard_root')==proof['shard']]
    if len(plans)!=1 or stops['root_hash']!=manifest['hash']:fail('root/leaf namespace differs')
    doc=plans[0]['payload']['plan']
    if F.shard_root(manifest,index,doc)!=proof['shard']:fail('leaf shard differs')
    F.validate_leaf(manifest,index,doc,F.stamp(proof['verified_at']).astimezone(BF.B.MSK).date())
    intent=one(shard_records,'DISPATCH_INTENT',proof['intent_hash']);rec=one(shard_records,'DISPATCH_RECEIPT',proof['receipt_hash'])
    previous=one(shard_records,'DISPATCH_RECEIPT',proof['prior_receipt_hash']);prior=one(shard_records,'RECONCILED',proof['prior_recon_hash'])
    if any(r['root_hash']!=proof['shard'] or r['version']!=D.VERSION for r in (intent,rec,previous,prior)):fail('foreign shard evidence')
    if any(r['sequence']!=n for r,n in ((intent,seq),(rec,seq),(previous,seq-1),(prior,seq-1))):fail('receipt sequence differs')
    if any(r['payload']['plan']!=doc for r in (intent,rec,previous)):fail('plan drift')
    receipt=rec['payload']['receipt']
    if any(receipt[k]!=proof[v] for k,v in [('run_id','run_id'),('lease_generation','generation')]):fail('run/generation differs')
    if receipt['ack_hash']!=doc['ack_hash'] or any(intent['payload']['preparation'][k]!=receipt[k] for k in ('run_id','lease_generation','ack_hash')):fail('intent/receipt linkage differs')
    base,jobs=BF.resources(BF.target(p['tenant']))
    job=next(n for n,j in jobs.items() if doc['runtime_plan']['entity'] in j['entities'])
    if not re.fullmatch(re.escape(base+'/jobs/'+job+'/executions/'+job+'-')+'[a-z0-9]+',proof['runtime_execution']):fail('foreign runtime')
    cid=BF.B.digest(['BOUNDED_PILOT_EXCLUSIVE',p['project']])[:16]
    if proof['lease_release']!=f"LD_{cid}_{proof['generation']:04d}":fail('lease namespace differs')
    if not isinstance(proof['run_id'],str) or not re.fullmatch(r'bf-[A-Za-z0-9-]+',proof['run_id']):fail('run id differs')
    result=proof['reconciliation']
    if result.get('state_hash')!=proof['state_hash'] or D.digest(result.get('coverage_readback'))!=proof['coverage_hash']:fail('persisted result linkage differs')
    if prior['payload'].get('persisted_reconciled') is not True or prior['payload']['sequence']!=proof['unit_start']-1:fail('accepted predecessor differs')
    if proof['unit_end']<proof['unit_start'] or proof['reconciliation'].get('sequence')!=proof['unit_end'] or proof['reconciliation'].get('persisted_reconciled') is not True:fail('result/source range differs')
    expected={'version':D.VERSION,'root_hash':proof['shard'],'kind':'RECONCILED','sequence':seq,'payload':proof['reconciliation']}
    if D.digest(expected)!=proof['reconciliation_hash']:fail('result digest differs')
    if type(result.get('source_complete')) is not bool or result.get('checkpoint')!=('DONE' if result['source_complete'] else 'RUNNING'):fail('completion evidence differs')
    if result.get('lifecycle_changed') is not False or result.get('scheduler_changed') is not False:fail('unexpected lifecycle mutation')
    if D.digest(result['coverage_readback'])!=proof['coverage_hash'] or result['coverage_readback'].get('source_sequence')!=proof['unit_end']:fail('coverage source range differs')
    if any(r['kind'] in {'DISPATCH_INTENT','DISPATCH_RECEIPT','RECONCILED'} and r['sequence']>seq for r in shard_records):fail('successor authority')
    # Static loading of historical certificates permits later canonical progress;
    # callers select only this receipt prefix when validating an accepted proof.
    existing=[r for r in shard_records if r['kind']=='RECONCILED' and r['sequence']==seq]
    if existing and existing!=[expected]:fail('conflicting reconciliation')
    if final and existing!=[expected]:fail('result reconciliation absent')
    return doc,receipt


class ObservationStore:
    def __init__(self,original):self.original=original
    def history(self,*a,**k):return self.original.history(*a,**k)
    def read(self,*a,**k):return self.original.read(*a,**k)
    def __getattr__(self,name):fail('observation authority write forbidden')


class ObservationTables:
    """Canonical coverage computes evidence but cannot append it during recovery."""
    def __init__(self,original,c):self.original,self.c=original,c
    def __getattr__(self,name):
        if name in {'create_marker','write_request'}:fail('observation mutation forbidden')
        return getattr(self.original,name)
    def append(self,ds,table,rows):
        if ds!=self.c['datasets']['tenant_ops'] or table not in {'DATA_COVERAGE','DQ_RESULTS'}:fail('unexpected coverage mutation')
        # No external call. These optional economic/coverage rows are not authority.


from tools.tenancy import full_controller as H


class SourceFreeObservation(H.Backend):
    @property
    def tables(self):return self.observation_tables
    def request(self,method,url,body=None):
        from tools.tenancy import controller_dispatch_recovery as R
        return R.SourceFreeBackend(self.original_backend).request(method,url,body)
    def append_request(self,*a):fail('observation append forbidden')
    def dispatch(self,*a):fail('source dispatch forbidden')
    def start(self,*a):fail('source start forbidden')
    def reconcile(self,*a):fail('checkpoint/lease rewrite forbidden')


def observer(base,manifest,index):
    b=SourceFreeObservation(base,manifest);b.index=index;b.original_backend=base
    # Inherited instance hooks must not shadow the source-free class boundary.
    for name in ('request','select','append_request','dispatch','start','reconcile'):
        b.__dict__.pop(name,None)
    b.store=ObservationStore(base.store)
    b.observation_tables=ObservationTables(H.ProjectedTables(b,base.tables),b.c)
    return b


def observe(base,manifest,stop_hash,index,*,automatic=False,existing=None):
    """Fresh bounded metadata/SELECT predicates, no source or resource writes."""
    from tools.tenancy import full_controller as H, cloud_controller as C
    root_records=base.store.history(manifest['hash']);stop=one(root_records,'STOPPED',stop_hash)
    plans=[r['payload'] for r in root_records if r['kind']=='CHUNK_PLAN' and r['sequence']==index]
    if len(plans)!=1:fail('exact leaf plan required')
    item=plans[0];doc=item['plan'];shard=item['shard_root'];b=observer(base,manifest,index)
    H.verify_authority(b,manifest);p=policy(b,manifest)
    sh=base.store.history(shard,max_records=F.MAX_LEAF_RECORDS)
    receipts=[r for r in sh if r['kind']=='DISPATCH_RECEIPT'];rec=max(receipts,key=lambda r:r['sequence']) if receipts else None
    if not rec or rec['sequence']<2:fail('no exact terminal predecessor')
    seq=rec['sequence'];receipt=rec['payload']['receipt']
    intents=[r for r in sh if r['kind']=='DISPATCH_INTENT' and r['sequence']==seq]
    previous=[r for r in sh if r['kind']=='DISPATCH_RECEIPT' and r['sequence']==seq-1]
    prior=[r for r in sh if r['kind']=='RECONCILED' and r['sequence']==seq-1]
    if len(intents)!=1 or len(previous)!=1 or len(prior)!=1:fail('predecessor/intent missing')
    intent=intents[0];previous=previous[0];prior=prior[0]
    if automatic:
        diagnostic=stop['payload'].get('diagnostic',{})
        context={'index':index,'shard':shard,'sequence':seq,'intent_hash':D.digest(intent),'receipt_hash':D.digest(rec)}
        if type(diagnostic.get('version')) is not int or diagnostic.get('version')!=1 or diagnostic.get('stage') not in AUTO_STAGES or diagnostic.get('context')!=context:fail('unsupported automatic STOP stage/scope')
        accepted=load(base,manifest,root_records)
        if not any(x['stop_hash']==p['initial_stop'] and x['receipt_hash']==p['initial_receipt'] for x in accepted):fail('initial exact recovery not accepted')
    elif (stop_hash,D.digest(rec),D.digest(intent),receipt['lease_generation'])!=(p['initial_stop'],p['initial_receipt'],p['initial_intent'],p['initial_generation']):fail('initial owner incident differs')
    if not automatic and (b.current_execution is not None or b.c['orchestration']['job']['env']['HISTORICAL_SCHEDULER_STATE']!='PAUSED'):fail('owner recovery requires frozen inactive observation')
    if any(r['kind'] in {'DISPATCH_INTENT','DISPATCH_RECEIPT','RECONCILED'} and r['sequence']>seq for r in sh):fail('successor intent')
    if any(r['kind']=='RECONCILED' and r['sequence']==seq for r in sh) and existing is None:fail('not class C: already reconciled')
    operation=b.request('GET',BF.RUN_API+'/'+receipt['operation'])
    if not operation.get('done') or operation.get('error'):fail('runtime nonterminal/failed')
    execution=operation.get('response') or {};name=execution.get('name','')
    base_name,jobs_config=BF.resources(b.c)
    job=next(n for n,j in jobs_config.items() if doc['runtime_plan']['entity'] in j['entities'])
    if not re.fullmatch(re.escape(base_name+'/jobs/'+job+'/executions/'+job+'-')+'[a-z0-9]+',name) or not execution.get('completionTime') or execution.get('succeededCount')!=1 or execution.get('failedCount',0) or execution.get('cancelledCount',0) or execution.get('taskCount')!=1 or execution.get('retriedCount',0) or execution.get('runningCount',0) or execution.get('reconciling',False):fail('runtime result ambiguous/failed')
    template=execution['template'];containers=template['containers'];env={v['name']:v.get('value') for v in containers[0]['env']} if len(containers)==1 else {}
    expected={'INGESTION_RUN_ID':receipt['run_id'],'ENTITIES':doc['runtime_plan']['entity'],'TENANT_BINDING_REQUIRED':'1','STRICT_PAGE_CAPS':'1','BACKFILL_TARGET_PROJECT':p['project'],'BACKFILL_RESUME_PLAN_ID':doc['runtime_plan']['plan_id']} if BF.QF.matches(doc) else {'INGESTION_RUN_ID':receipt['run_id'],'ENTITIES':doc['runtime_plan']['entity'],'TENANT_BINDING_REQUIRED':'1','STRICT_PAGE_CAPS':'1','BACKFILL_TARGET_PROJECT':p['project']}
    if len(containers)!=1 or containers[0]['image']!=p['runtime_image'] or template['serviceAccount']!=f"{b.c['marketplaces']['ozon']['service_accounts']['runtime']}@{p['project']}.iam.gserviceaccount.com" or any(env.get(k)!=v for k,v in expected.items()):fail('runtime provenance differs')
    rp=doc['runtime_plan']
    expected.update(SINCE=rp['from'],UNTIL=rp['to'],BACKFILL_GENERATION=rp['generation'],BACKFILL_ORIGIN=rp['origin'],BACKFILL_MODE=BF.B.VERSION,BACKFILL_MAX_REQUESTS=str(doc['max_requests']),BACKFILL_MAX_UNITS=str(doc['max_units']))
    if any(env.get(k)!=v for k,v in expected.items()):fail('runtime source budget/scope differs')
    state=BF.read_proof(b.c,doc,receipt['run_id'],request=b.request)['state']
    if (state['progress'].get('report') or {}).get('phase')=='INTENT':fail('not proven partial terminal source')
    if b.state(doc)!=state:fail('source state drift')
    start=prior['payload']['sequence']+1;end=state['sequence']
    if not automatic and (start,end)!=(p['initial_unit_start'],p['initial_unit_end']):fail('owner source range differs')
    params={'origin':('TIMESTAMP',rp['origin']),'pid':('STRING',rp['plan_id']),'entity':('STRING',rp['entity'])}
    units=b.select(f"SELECT ingestion_run_id,status,backfill_sequence,backfill_detail_json FROM `{b.journal}` WHERE started_at>=@origin AND backfill_plan_id=@pid AND entity=@entity AND backfill_sequence IS NOT NULL ORDER BY backfill_sequence",params)
    sequences=[];owned=[]
    for u in units:
        n=u['backfill_sequence'];sequences.append(n)
        if u['status']!='OK':fail('source unit not terminal OK')
        raw=parse(u['backfill_detail_json'])
        if start<=n<=end:
            if u['ingestion_run_id']!=receipt['run_id']+'-u'+str(n) or type(raw.get('transport_requests')) is not int or type(raw.get('transport_retries')) is not int or raw['transport_retries']!=0:fail('unit attribution/retry ambiguity')
            owned.append(n)
    if sequences!=list(range(1,end+1)) or owned!=list(range(start,end+1)):fail('duplicate/unit gap/successor')
    cp=b.select(f"SELECT lease_generation,lease_owner,evidence_json FROM `{p['project']}.tenant_ops.BACKFILL_CHECKPOINTS` WHERE entity=@entity AND plan_hash=@ack AND lease_generation BETWEEN @previous AND @current",{'entity':('STRING',rp['entity']),'ack':('STRING',doc['ack_hash']),'previous':('INT64',receipt['lease_generation']-1),'current':('INT64',receipt['lease_generation'])})
    states=[(r['lease_generation'],r['lease_owner'],(parse(r['evidence_json']).get('proof') or {}).get('state')) for r in cp]
    current=[s for g,o,s in states if g==receipt['lease_generation'] and s is not None]
    if not current or any(s!=state for s in current) or any(o!=receipt['run_id'] for g,o,s in states if g==receipt['lease_generation']) or not any(isinstance(s,dict) and s.get('sequence')==start-1 and D.digest(s)==prior['payload']['state_hash'] for g,o,s in states if g==receipt['lease_generation']-1):fail('existing checkpoint chain differs')
    later=b.select(f"SELECT COUNT(*) AS n FROM `{p['project']}.tenant_ops.BACKFILL_CHECKPOINTS` WHERE JSON_VALUE(evidence_json,'$.mode')='BOUNDED_PILOT' AND lease_generation>@current",{'current':('INT64',receipt['lease_generation'])})
    if later!=[{'n':0}]:fail('later checkpoint generation')
    cid=BF.B.digest(['BOUNDED_PILOT_EXCLUSIVE',p['project']])[:16];gen=receipt['lease_generation'];ln=f'L_{cid}_{gen:04d}';ld=f'LD_{cid}_{gen:04d}'
    lease=b.tables.get_table(b.c['datasets']['tenant_locks'],ln);release=b.tables.get_table(b.c['datasets']['tenant_locks'],ld)
    if not lease or not release or any(v[0].get('owner')!=receipt['run_id'] or parse(v[1]).get('ack_hash')!=doc['ack_hash'] for v in (lease,release)) or parse(release[1]).get('operation')!=receipt['operation']:fail('exact lease/LD linkage absent')
    for n,labels,created in b.tables.list_tables(b.c['datasets']['tenant_locks']):
        match=BF.CK.LEASE_RE.match(n)
        if match and match.group(1)==cid and int(match.group(2))>gen:fail('later generation')
    timeline=b.select(f"SELECT evidence_json,updated_at FROM `{p['project']}.tenant_ops.BACKFILL_CHECKPOINTS` WHERE entity='orchestration' AND plan_hash=@shard AND backfill_id=@prior LIMIT 2",{'shard':('STRING',shard),'prior':('STRING',D.digest(prior)[:16])})
    if len(timeline)!=1 or D.digest(parse(timeline[0]['evidence_json']))!=D.digest(prior):fail('predecessor time ambiguous')
    after=C.timestamp(timeline[0]['updated_at']);children=[]
    jobs=b.request('GET',BF.RUN_API+'/'+base_name+'/jobs?pageSize=1000')
    expected_jobs=set(b.c['marketplaces']['ozon']['jobs'])|{'tenant-control','tenant-backfill-controller'}
    if jobs.get('nextPageToken') or {x['name'].split('/')[-1] for x in jobs.get('jobs',[])}!=expected_jobs:fail('job inventory drift')
    for job in jobs['jobs']:
        for e in BF.execution_inventory(b.request,job['name'])['executions']:
            if not e.get('completionTime') and e['name']!=b.current_execution:fail('active execution')
            if '/jobs/tenant-backfill-controller/' not in e['name'] and C.timestamp(e['createTime'])>=after:children.append(e['name'])
    if sorted(children)!=[name]:fail('second/successor child')
    unknown=b.select(f"SELECT COUNT(*) AS n FROM `{b.journal}` WHERE started_at>=@start AND NOT (ingestion_run_id=@run OR STARTS_WITH(ingestion_run_id,CONCAT(@run,'-u')))",{'start':('TIMESTAMP',after.isoformat()),'run':('STRING',receipt['run_id'])})
    if unknown!=[{'n':0}]:fail('unattributed source activity')
    # Streaming rows without commit fences must not hide successor authority.
    successors=b.select(f"SELECT COUNT(*) AS n FROM `{p['project']}.tenant_ops.BACKFILL_CHECKPOINTS` WHERE entity='orchestration' AND plan_hash=@shard AND attempts>@seq AND JSON_VALUE(evidence_json,'$.kind') IN ('DISPATCH_INTENT','DISPATCH_RECEIPT','RECONCILED')",{'shard':('STRING',shard),'seq':('INT64',seq)})
    if successors!=[{'n':0}]:fail('uncommitted successor authority')
    times=b.select(f"SELECT evidence_json,updated_at FROM `{p['project']}.tenant_ops.BACKFILL_CHECKPOINTS` WHERE entity='orchestration' AND plan_hash IN (@root,@shard) AND backfill_id IN (@stop,@intent,@receipt) LIMIT 10",{'root':('STRING',manifest['hash']),'shard':('STRING',shard),'stop':('STRING',stop_hash[:16]),'intent':('STRING',D.digest(intent)[:16]),'receipt':('STRING',D.digest(rec)[:16])})
    by={}
    for row in times:
        h=D.digest(parse(row['evidence_json']));at=C.timestamp(row['updated_at'])
        if h in by and by[h]!=at:fail('incident timestamp conflict')
        by[h]=at
    if set(by)!={stop_hash,D.digest(intent),D.digest(rec)} or not after<by[D.digest(intent)]<=by[D.digest(rec)]<by[stop_hash] or C.timestamp(execution['completionTime'])>by[stop_hash]:fail('post-dispatch terminal chronology differs')
    controller=stop['payload'].get('controller_execution','')
    if not re.fullmatch(re.escape(base_name+'/jobs/tenant-backfill-controller/executions/tenant-backfill-controller-')+'[a-z0-9]+',controller):fail('STOP controller attribution missing')
    stopped=b.request('GET',BF.RUN_API+'/'+controller)
    if not stopped.get('completionTime') or not C.timestamp(stopped['startTime'])<=by[stop_hash]<=C.timestamp(stopped['completionTime']):fail('STOP outside controller execution')
    if base.clock()<by[stop_hash]:fail('future STOP')
    coverage=BF.verify_coverage(doc,doc['ack_hash'],backend=b)
    result={'checkpoint':'DONE' if state['complete'] else 'RUNNING','sequence':end,'rows_observed':state['rows'],'orders':state['orders'],'supplies':state['supplies'],'bundles':state['bundles'],'coverage':'UNPROVEN_PENDING_DQ','lifecycle_changed':False,'scheduler_changed':False,'coverage_readback':coverage,'source_complete':state['complete'],'persisted_reconciled':True,'state_hash':D.digest(state)}
    proof={'version':VERSION,'type':TYPE,'tenant':p['tenant'],'project':p['project'],'root':manifest['hash'],'manifest_hash':D.digest(manifest),'index':index,'shard':shard,'stop_hash':stop_hash,'intent_hash':D.digest(intent),'receipt_hash':D.digest(rec),'prior_receipt_hash':D.digest(previous),'prior_recon_hash':D.digest(prior),'receipt_sequence':seq,'runtime_execution':name,'run_id':receipt['run_id'],'generation':gen,'unit_start':start,'unit_end':end,'state_hash':D.digest(state),'lease_release':ld,'lease_release_hash':D.digest(release),'unit_evidence_hash':D.digest(units),'coverage_hash':D.digest(coverage),'policy_hash':p['hash'],'implementation_source':p['controller_source'],'controller_image':p['controller_image'],'runtime_source':p['runtime_source'],'runtime_image':p['runtime_image'],'reconciliation':result,'reconciliation_hash':D.digest({'version':D.VERSION,'root_hash':shard,'kind':'RECONCILED','sequence':seq,'payload':result}),'predicates':{k:True for k in PREDICATES},'verified_at':base.clock().isoformat()}
    proof=sealed(proof);validate(proof,manifest,p,root_records,sh)
    return proof


def load(backend,manifest,records):
    p=policy(backend,manifest);approved=[]
    for r in records:
        if r['kind']!=KIND:continue
        proof=r['payload'];sh=backend.store.history(proof['shard'],max_records=F.MAX_LEAF_RECORDS)
        prefix=[x for x in sh if x['sequence']<=proof['receipt_sequence']]
        authority=p
        if proof['implementation_source']!=p['controller_source']:
            from tools.tenancy import recent_priority as RP
            authority=RP.historical_policy(backend,manifest,p,proof['implementation_source'])
        validate(proof,manifest,authority,records,prefix,final=True)
        if r['root_hash']!=manifest['hash'] or r['sequence']!=proof['index']:fail('recovery record scope differs')
        fence=backend.tables.get_table(backend.c['datasets']['tenant_locks'],f"BFFLR_{proof['root']}_{proof['stop_hash']}")
        if fence!=({'kind':'full_leaf_recovery','root':proof['root'][:16]},D.encoded(proof)):fail('certificate fence absent/conflicting')
        approved.append(proof)
    if len({p['stop_hash'] for p in approved})!=len(approved):fail('duplicate recovery scope')
    return approved


def apply(base,manifest,stop_hash,index,*,automatic=False):
    """Only two canonical authority records; existing checkpoint/LD untouched."""
    p=policy(base,manifest);records=base.store.history(manifest['hash'])
    approved=load(base,manifest,records)
    previous=next((x for x in approved if x['stop_hash']==stop_hash),None)
    if previous:return {'accepted_sequence':previous['unit_end'],'lease_release':previous['lease_release'],'record_hash':D.digest({'version':D.VERSION,'root_hash':manifest['hash'],'kind':KIND,'sequence':index,'payload':previous}),'reconciliation_hash':previous['reconciliation_hash'],'source_calls':0,'source_dispatches':0}
    unresolved={D.digest(r) for r in records if r['kind']=='STOPPED'}-{x['stop_hash'] for x in approved}
    if unresolved!={stop_hash}:fail('another unresolved STOP')
    name=f'BFFLR_{manifest["hash"]}_{stop_hash}';existing=base.tables.get_table(base.c['datasets']['tenant_locks'],name)
    proof=observe(base,manifest,stop_hash,index,automatic=automatic,existing=parse(existing[1]) if existing else None)
    if existing:
        old=parse(existing[1]);old_sh=base.store.history(old['shard']);validate(old,manifest,p,records,old_sh)
        # Freeze the certificate/RECON payload across a lost append acknowledgement.
        for k in ('state_hash','unit_evidence_hash','lease_release_hash','coverage_hash','receipt_hash','intent_hash'):
            if old[k]!=proof[k]:fail('recovery retry evidence changed')
        proof=old
    value=({'kind':'full_leaf_recovery','root':manifest['hash'][:16]},D.encoded(proof))
    if existing is None:base.tables.create_marker(base.c['datasets']['tenant_locks'],name,*value)
    if base.tables.get_table(base.c['datasets']['tenant_locks'],name)!=value:fail('certificate CAS conflict')
    # No BF.reconcile: terminal checkpoint and LD already exist and must not repeat.
    rh=base.store.commit(proof['shard'],'RECONCILED',proof['receipt_sequence'],proof['reconciliation'],base.clock())
    if rh!=proof['reconciliation_hash']:fail('canonical RECON digest changed')
    h=base.store.commit(manifest['hash'],KIND,index,proof,base.clock())
    load(base,manifest,base.store.history(manifest['hash']))
    return {'record_hash':h,'reconciliation_hash':rh,'accepted_sequence':proof['unit_end'],'lease_release':proof['lease_release'],'source_calls':0,'source_dispatches':0}


def recover_pending(backend,manifest,records):
    """Only explicitly enabled, typed, fully proven class C; never generic STOP."""
    approved=load(backend,manifest,records)
    unresolved=[r for r in records if r['kind']=='STOPPED' and D.digest(r) not in {x['stop_hash'] for x in approved}]
    if len(unresolved)!=1:return None
    stop=unresolved[0];diag=stop['payload'].get('diagnostic',{})
    if diag.get('stage') not in AUTO_STAGES:return None
    candidates=[]
    for r in records:
        if r['kind']!='CHUNK_PLAN':continue
        sh=backend.store.history(r['payload']['shard_root'],max_records=F.MAX_LEAF_RECORDS)
        receipts=[x for x in sh if x['kind']=='DISPATCH_RECEIPT']
        if receipts:
            last=max(receipts,key=lambda x:x['sequence'])
            fence=backend.tables.get_table(backend.c['datasets']['tenant_locks'],f"BFFLR_{manifest['hash']}_{D.digest(stop)}")
            finishing=fence is not None and parse(fence[1]).get('receipt_hash')==D.digest(last) and parse(fence[1]).get('index')==r['sequence']
            if finishing or not any(x['kind']=='RECONCILED' and x['sequence']==last['sequence'] for x in sh):candidates.append(r['sequence'])
    if len(candidates)!=1:return None
    return apply(backend,manifest,D.digest(stop),candidates[0],automatic=True)
