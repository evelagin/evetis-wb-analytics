"""Owner-only terminal-success receipt reconciliation; never a source replay.

A closed certificate pins an exact post-dispatch STOP, intent, successful child,
source prefix and owner scope. Only canonical bookkeeping is allowed. The older
pre-source recovery remains separate. Cloud readers cannot create owner markers.
"""
import re
from tools.tenancy import durable_plan as D, tenant_backfill as BF, controller_stop_recovery as CR
from tools.tenancy.validation import parse_tenant_json as parse

KIND='CONTROLLER_DISPATCH_STOP_RECOVERED'
TYPE='CONTROLLER_POST_DISPATCH_TERMINAL_SUCCESS_UNRECONCILED_STOP'
VERSION=1
PREDICATES=frozenset({'terminal_success','operation_terminal','source_accounting','no_ambiguous_retry',
    'exact_units','persisted_keys','no_duplicates','predecessor_reconciled','no_successor',
    'exact_lease','no_later_generation','no_active_execution','bindings_valid','tenant_isolation',
    'original_stop_preserved','checkpoint_relationship'})
FIELDS=frozenset({'version','type','tenant','project','root_hash','stop_hash','intent_hash','receipt_hash',
    'predecessor_sequence','predecessor_receipt_hash','predecessor_reconciliation_hash','reconciliation_hash',
    'plan_id','state_hash','state_sequence','unit_start','unit_end','lease_generation','run_id',
    'controller_execution','controller_image','controller_source_sha','runtime_execution','runtime_image',
    'runtime_source_sha','implementation_sha','authorization','predicates','evidence_hashes','verified_at','hash'})
AUTH_FIELDS=frozenset({'owner_ack_sha256','tenant','project','root_hash','stop_hash','intent_hash','receipt_hash',
    'run_id','unit_start','unit_end','lease_generation','type','version'})


def fail(message):raise BF.B.EvidenceError('post-dispatch recovery: '+message)


def validate(proof):
    if not isinstance(proof,dict) or set(proof)!=FIELDS:fail('closed certificate schema required')
    if type(proof['version']) is not int or proof['version']!=VERSION or proof['type']!=TYPE:fail('unsupported contract')
    if D.digest({k:v for k,v in proof.items() if k!='hash'})!=proof['hash']:fail('certificate digest differs')
    for k in FIELDS:
        if k.endswith('_hash') or k=='hash':D.check_hash(proof[k])
    for k in ('controller_source_sha','runtime_source_sha','implementation_sha'):
        if not isinstance(proof[k],str) or not re.fullmatch('[0-9a-f]{40}',proof[k]):fail('unknown source')
    for k in ('controller_image','runtime_image'):
        if not isinstance(proof[k],str) or not re.fullmatch(r'[^\s]+@sha256:[0-9a-f]{64}',proof[k]):fail('unknown image')
    if not isinstance(proof['predicates'],dict) or set(proof['predicates'])!=PREDICATES or any(v is not True for v in proof['predicates'].values()):fail('mandatory predicate unknown/false')
    auth=proof['authorization']
    if not isinstance(auth,dict) or set(auth)!=AUTH_FIELDS:fail('exact owner scope required')
    D.check_hash(auth['owner_ack_sha256'])
    if any(auth[k]!=proof[k] for k in AUTH_FIELDS-{'owner_ack_sha256'}):fail('owner scope differs')
    for k in ('predecessor_sequence','state_sequence','unit_start','unit_end','lease_generation'):
        if type(proof[k]) is not int or proof[k]<1:fail('invalid sequence/generation')
    if proof['unit_end']!=proof['state_sequence'] or proof['unit_start']>proof['unit_end']:fail('unit range differs')
    if not isinstance(proof['evidence_hashes'],dict) or not proof['evidence_hashes']:fail('causal evidence absent')
    for h in proof['evidence_hashes'].values():D.check_hash(h)
    from tools.tenancy.cloud_controller import timestamp
    timestamp(proof['verified_at'])
    c=BF.target(proof['tenant'])
    if c['project_id']!=proof['project'] or proof['root_hash']!=BF.QF.ROOT:fail('foreign tenant/root')
    base=f"projects/{proof['project']}/locations/{c['region']}/jobs/"
    if not proof['controller_execution'].startswith(base+'tenant-backfill-controller/executions/tenant-backfill-controller-') or not proof['runtime_execution'].startswith(base+'ozon-runtime-daily/executions/ozon-runtime-daily-'):fail('foreign execution')
    return proof


def one(records,kind,h):
    rows=[r for r in records if r.get('kind')==kind and D.digest(r)==h]
    if len(rows)!=1:fail('exact '+kind+' absent/ambiguous')
    return rows[0]


def verify_records(proof,records,root,tenant,*,publishing=False,final=True):
    validate(proof)
    if root!=proof['root_hash'] or tenant!=proof['tenant']:fail('foreign scope')
    stop=one(records,'STOPPED',proof['stop_hash']);receipt=one(records,'DISPATCH_RECEIPT',proof['receipt_hash'])
    intent=one(records,'DISPATCH_INTENT',proof['intent_hash']);previous=one(records,'DISPATCH_RECEIPT',proof['predecessor_receipt_hash']);prior=one(records,'RECONCILED',proof['predecessor_reconciliation_hash'])
    seq=proof['predecessor_sequence']+1;doc=receipt['payload']['plan'];r=receipt['payload']['receipt']
    selected=[stop,receipt,intent,previous,prior]
    if final:selected.append(one(records,'RECONCILED',proof['reconciliation_hash']))
    if any(x.get('root_hash')!=root or x.get('version')!=D.VERSION for x in selected):fail('foreign/corrupt record')
    if stop['payload'].get('controller_execution')!=proof['controller_execution']:fail('STOP attribution differs')
    if receipt['sequence']!=seq or intent['sequence']!=seq or previous['sequence']!=seq-1 or prior['sequence']!=seq-1:fail('receipt sequence differs')
    if doc!=intent['payload']['plan'] or doc!=previous['payload']['plan']:fail('plan changed')
    if doc['tenant_id']!=tenant or doc['runtime_plan']['project']!=proof['project'] or doc['runtime_plan']['plan_id']!=proof['plan_id'] or doc['runtime_plan']['entity']!='supplies' or not BF.QF.matches(doc):fail('unsupported scope')
    if prior['payload'].get('sequence')!=proof['unit_start']-1:fail('predecessor source prefix differs')
    if r['run_id']!=proof['run_id'] or r['lease_generation']!=proof['lease_generation'] or r['ack_hash']!=doc['ack_hash']:fail('receipt owner differs')
    if any(intent['payload']['preparation'].get(k)!=r.get(k) for k in ('run_id','lease_generation','ack_hash')):fail('intent linkage differs')
    if final:
        recon=selected[-1]
        if recon['sequence']!=seq or recon['payload'].get('sequence')!=proof['unit_end'] or recon['payload'].get('checkpoint')!='RUNNING':fail('reconciliation differs/false COMPLETE')
    if publishing and any(x['kind'] in {'DISPATCH_INTENT','DISPATCH_RECEIPT','RECONCILED'} and x['sequence']>seq for x in records):fail('successor authority exists')
    return doc,r,seq


def marker(proof):return 'BFDR_'+validate(proof)['hash']
def marker_value(proof):return ({'recovery':'controller_dispatch','root':proof['root_hash'][:16]},D.encoded(proof))


def observe(backend,proof,*,final=False):
    """Fresh GET/list/SELECT predicates only. Never coverage or source writes."""
    from tools.tenancy.cloud_controller import timestamp
    validate(proof);c=backend.c;records=backend.store.history(proof['root_hash'])
    doc,r,seq=verify_records(proof,records,proof['root_hash'],c['tenant_id'],publishing=True,final=final)
    manifest=one(records,'MANIFEST',D.digest(next(x for x in records if x['kind']=='MANIFEST')))['payload']
    backend.preflight(manifest,owner_observation=True)
    if backend.active_runtime_execution():fail('active execution')
    if c['orchestration']['job']['env']['HISTORICAL_SCHEDULER_STATE']!='PAUSED':fail('authority not frozen')
    CR.terminal_executions(backend,proof)
    # Historical deployment proofs stay distinct from the new implementation.
    historical=parse((BF.REPO/'infra/tenant/releases/backfill'/(proof['controller_source_sha']+'.json')).read_text())
    if historical['image']!=proof['controller_image']:fail('historical controller source/image differs')
    from tools.tenancy import platform as PL
    runtime=[parse(f.read_text()) for f in (BF.REPO/PL.RUNTIME_RELEASES_DIR/'ozon').glob('*.json')]
    runtime=[x for x in runtime if x.get('image')==proof['runtime_image']]
    if len(runtime)!=1 or runtime[0].get('source',{}).get('commit')!=proof['runtime_source_sha']:fail('runtime source/image differs')
    execution=backend.request('GET',BF.RUN_API+'/'+proof['runtime_execution'])
    if execution.get('cancelledCount',0) or execution.get('taskCount',1)!=1:fail('runtime termination differs')
    times=backend.select(f"SELECT evidence_json,updated_at FROM `{c['project_id']}.{c['datasets']['tenant_ops']}.BACKFILL_CHECKPOINTS` WHERE plan_hash=@root AND entity='orchestration' AND backfill_id IN (@stop,@intent,@receipt,@prior) LIMIT 10",{'root':('STRING',proof['root_hash']),'stop':('STRING',proof['stop_hash'][:16]),'intent':('STRING',proof['intent_hash'][:16]),'receipt':('STRING',proof['receipt_hash'][:16]),'prior':('STRING',proof['predecessor_reconciliation_hash'][:16])})
    by={}
    for x in times:
        h=D.digest(parse(x['evidence_json']));at=timestamp(x['updated_at'])
        if h in by and by[h]!=at:fail('timestamp conflict')
        by[h]=at
    needed={proof[k] for k in ('stop_hash','intent_hash','receipt_hash','predecessor_reconciliation_hash')}
    if set(by)!=needed or not by[proof['predecessor_reconciliation_hash']] < by[proof['intent_hash']] <= by[proof['receipt_hash']] < by[proof['stop_hash']]:fail('post-dispatch chronology differs')
    if not timestamp(execution['startTime']) < by[proof['stop_hash']] < timestamp(execution['completionTime']):fail('STOP not within exact child execution')
    p=doc['runtime_plan'];state=BF.read_proof(c,doc,r['run_id'],request=backend.request)['state']
    if state['complete'] or state['sequence']!=proof['unit_end'] or D.digest(state)!=proof['state_hash'] or backend.state(doc)!=state:fail('terminal/source state differs')
    # Streaming rows/fences cannot hide uncommitted successor authority.
    later=backend.select(f"SELECT COUNT(*) AS n FROM `{c['project_id']}.{c['datasets']['tenant_ops']}.BACKFILL_CHECKPOINTS` WHERE plan_hash=@root AND entity='orchestration' AND JSON_VALUE(evidence_json,'$.kind') IN ('DISPATCH_INTENT','DISPATCH_RECEIPT','RECONCILED') AND attempts>@seq",{'root':('STRING',proof['root_hash']),'seq':('INT64',seq)})
    if later!=[{'n':0}]:fail('successor row exists')
    locks=backend.tables.list_tables(c['datasets']['tenant_locks']);cid=BF.B.digest(['BOUNDED_PILOT_EXCLUSIVE',c['project_id']])[:16];gen=proof['lease_generation']
    for name,labels,created in locks:
        prefix='BFQ_'+proof['root_hash']+'_'
        if name.startswith(prefix) and (not name[len(prefix):].split('_')[0].isdigit() or int(name[len(prefix):].split('_')[0])>seq):fail('successor fence')
        match=BF.CK.LEASE_RE.match(name)
        if match and match.group(1)==cid and int(match.group(2))>gen:fail('later lease')
    lease=backend.tables.get_table(c['datasets']['tenant_locks'],f'L_{cid}_{gen:04d}');release=backend.tables.get_table(c['datasets']['tenant_locks'],f'LD_{cid}_{gen:04d}')
    if not lease or lease[0].get('owner')!=r['run_id'] or parse(lease[1]).get('ack_hash')!=doc['ack_hash']:fail('lease ownership differs')
    if release and (release[0].get('owner')!=r['run_id'] or parse(release[1]).get('ack_hash')!=doc['ack_hash']):fail('release ownership differs')
    if final and not release:fail('terminal release missing')
    _,_,ledger,_,hold=BF.TL.read_state(c,backend.tables)
    gens=[x['lease_generation'] for x in ledger if parse(x.get('evidence_json') or '{}').get('mode')=='BOUNDED_PILOT']
    if hold or not gens or max(gens)!=gen:fail('hold/later authority generation')
    # Lifecycle inventory deliberately projects away large checkpoint proofs.
    # Read only the two exact lease generations needed for acceptance.
    accepted_rows=backend.select(f"SELECT DISTINCT evidence_json FROM `{c['project_id']}.{c['datasets']['tenant_ops']}.BACKFILL_CHECKPOINTS` WHERE entity='supplies' AND plan_hash=@ack AND lease_generation BETWEEN @previous AND @current LIMIT 10",{'ack':('STRING',doc['ack_hash']),'previous':('INT64',gen-1),'current':('INT64',gen)})
    if len(accepted_rows)>=10:fail('accepted checkpoint inventory ambiguous')
    accepted=[]
    for x in accepted_rows:
        proof_row=parse(x['evidence_json']);body=proof_row.get('proof')
        if body is None:continue  # Canonical START checkpoint has no source proof.
        if not isinstance(body,dict) or not isinstance(body.get('state'),dict):fail('accepted checkpoint proof schema differs')
        accepted.append(body['state'])
    if not any(x.get('sequence')==proof['unit_start']-1 for x in accepted):fail('accepted predecessor absent')
    if any(x.get('sequence',0)>proof['unit_end'] or x.get('sequence')==proof['unit_end'] and x!=state for x in accepted):fail('accepted checkpoint conflict')
    if final and not any(x==state for x in accepted):fail('accepted terminal checkpoint absent')
    params={'origin':('TIMESTAMP',p['origin']),'pid':('STRING',p['plan_id'])}
    units=backend.select(f"SELECT ingestion_run_id,status,backfill_sequence,backfill_detail_json FROM `{backend.journal}` WHERE started_at>=@origin AND backfill_plan_id=@pid AND entity='supplies' AND backfill_sequence IS NOT NULL ORDER BY backfill_sequence",params)
    details={};owned=[]
    for u in units:
        n=u['backfill_sequence'];d=BF.source_detail(parse(u['backfill_detail_json']))
        if u['status']!='OK' or n in details:fail('duplicate/failed source unit')
        details[n]=d
        if proof['unit_start']<=n<=proof['unit_end']:
            if u['ingestion_run_id']!=r['run_id']+'-u'+str(n):fail('source unit owner differs')
            raw=parse(u['backfill_detail_json'])
            if type(raw.get('transport_requests')) is not int or type(raw.get('transport_retries')) is not int or raw['transport_retries']!=0:fail('request accounting missing/ambiguous retry')
            owned.append(n)
    if sorted(details)!=list(range(1,proof['unit_end']+1)) or owned!=list(range(proof['unit_start'],proof['unit_end']+1)):fail('unit gap/range differs')
    progress=state['progress'];pending={str(x[0]) for x in progress['pending']}
    if progress['bundle']:pending.add(str(progress['bundle']['link'][0]))
    bids=[x for x in progress['bundle_links'] if x not in pending];ids=[str(x) for x in progress['seen_orders']]
    checks=[('RAW_OZON_SUPPLY_ORDERS','order_id','order_id',ids,state['orders']),('RAW_OZON_SUPPLIES','order_id,supply_id','order_id',ids,state['supplies']),('RAW_OZON_SUPPLY_BUNDLES','bundle_id,sku','bundle_id',bids,sum(d['source_items'] for d in details.values() if d.get('bundle_complete')))]
    for table,keys,key,values,n in checks:
        rows=backend.select(f"SELECT COUNT(*) AS rows_n,COUNT(DISTINCT TO_JSON_STRING(STRUCT({keys}))) AS keys_n FROM `{c['project_id']}.{p['raw']}.{table}` WHERE CAST({key} AS STRING) IN UNNEST(JSON_VALUE_ARRAY(@ids))",{'ids':('STRING',D.encoded(values))})
        if rows!=[{'rows_n':n,'keys_n':n}]:fail('persisted natural keys differ')
    controller=backend.request('GET',BF.RUN_API+'/'+proof['controller_execution']);base=f"projects/{c['project_id']}/locations/{c['region']}"
    jobs=backend.request('GET',BF.RUN_API+'/'+base+'/jobs?pageSize=1000')
    if jobs.get('nextPageToken') or len(jobs.get('jobs',[]))!=5:fail('job inventory drift')
    children=[]
    for job in jobs['jobs']:
        for x in BF.execution_inventory(backend.request,job['name'])['executions']:
            if not x.get('completionTime'):fail('active execution')
            if '/jobs/tenant-backfill-controller/' not in x['name'] and timestamp(x['createTime'])>=timestamp(controller['startTime']):children.append(x['name'])
    if children!=[proof['runtime_execution']]:fail('second/unrelated child runtime')
    unknown=backend.select(f"SELECT COUNT(*) AS n FROM `{backend.journal}` WHERE started_at>=@start AND NOT (ingestion_run_id=@run OR STARTS_WITH(ingestion_run_id,CONCAT(@run,'-u')))",{'start':('TIMESTAMP',controller['startTime']),'run':('STRING',r['run_id'])})
    if unknown!=[{'n':0}]:fail('unattributed source activity')
    return doc,r,seq


def load(backend,records,root):
    proofs=[]
    for record in records:
        if record['kind']!=KIND:continue
        proof=record['payload'];verify_records(proof,records,root,backend.c['tenant_id'])
        if record['sequence']!=proof['predecessor_sequence']+1:fail('recovery sequence differs')
        if backend.tables.get_table(backend.c['datasets']['ref'],marker(proof))!=marker_value(proof):fail('owner attestation missing')
        CR.terminal_executions(backend,proof)
        cid=BF.B.digest(['BOUNDED_PILOT_EXCLUSIVE',backend.c['project_id']])[:16]
        released=backend.tables.get_table(backend.c['datasets']['tenant_locks'],f"LD_{cid}_{proof['lease_generation']:04d}")
        if not released or released[0].get('owner')!=proof['run_id']:fail('terminal release missing/foreign')
        proofs.append(proof)
    if len({p['stop_hash'] for p in proofs})!=len(proofs):fail('conflicting recovery')
    return proofs


class SourceFreeBackend:
    """No dispatcher or marketplace/Secret payload transport in owner recovery."""
    def __init__(self,backend,proof=None):self.backend=backend;self.proof=proof
    def __getattr__(self,name):
        if name in {'start','next_plan','recover_receipt','reconcile'}:fail('source/coverage authority unavailable')
        return getattr(self.backend,name)
    def request(self,method,url,body=None):
        from urllib.parse import urlsplit
        host=urlsplit(url).hostname
        if host not in {'run.googleapis.com','cloudscheduler.googleapis.com','www.googleapis.com','bigquery.googleapis.com'} or ':run' in url or ':access' in url:fail('source/secret transport unavailable')
        if method not in {'GET','POST'}:fail('noncanonical mutation unavailable')
        if method=='POST' and ('/queries' not in url or not isinstance(body,dict) or not body.get('query','').lstrip().startswith('SELECT ') or ';' in body['query']):fail('only SELECT transport available')
        global_inventory=method=='GET' and url==BF.global_job_url(self.backend.c['project_id'])
        if not global_inventory and '/projects/'+self.backend.c['project_id']+'/' not in url:fail('foreign tenant transport unavailable')
        return self.backend.request(method,url,body) if body is not None else self.backend.request(method,url)

    def authorize_reconciliation(self,doc,receipt):
        """Owner observation is authority only for this fenced exact certificate."""
        from tools.tenancy import orchestration_contract as O
        if self.proof is None:fail('owner reconciliation certificate absent')
        proof=validate(self.proof)
        source=self.c['orchestration']['job']['env']['CONTROLLER_SOURCE_SHA']
        if proof['implementation_sha']!=source or self.current_execution is not None:fail('owner reconciliation implementation/context differs')
        release=parse((BF.REPO/'infra/tenant/releases/backfill'/(source+'.json')).read_text())
        O.verify_artifact_source(release,BF.REPO)
        if self.tables.get_table(self.c['datasets']['ref'],marker(proof))!=marker_value(proof):fail('owner reconciliation fence absent')
        expected,r,_=observe(self,proof)
        if expected!=doc or r!=receipt:fail('owner reconciliation receipt differs')
        return True


def apply(backend,proof,now):
    """Owner marker + canonical terminal bookkeeping only; zero source budget.

    A crash leaves preserved partial bookkeeping and never permits dispatch. The
    same exact owner certificate can finish it; a different certificate fails.
    """
    from tools.tenancy import orchestration_contract as O
    backend=SourceFreeBackend(backend,proof)
    from tools.tenancy.cloud_controller import timestamp
    validate(proof)
    if timestamp(proof['verified_at'])>now:fail('future owner observation')
    source=backend.c['orchestration']['job']['env']['CONTROLLER_SOURCE_SHA']
    if proof['implementation_sha']!=source:fail('unqualified recovery implementation')
    release=parse((BF.REPO/'infra/tenant/releases/backfill'/(source+'.json')).read_text());O.verify_artifact_source(release,BF.REPO)
    doc,r,seq=observe(backend,proof)
    from tools.tenancy import pre_source_recovery as PR
    records=backend.store.history(proof['root_hash'])
    approved={p['stop_hash'] for p in CR.load(backend,records,proof['root_hash'])}|{h for p in PR.load(backend,records,proof['root_hash']) for h in p['stop_hashes']}|{p['stop_hash'] for p in load(backend,records,proof['root_hash'])}
    if {D.digest(x) for x in records if x['kind']=='STOPPED'}-approved-{proof['stop_hash']}:fail('another unresolved STOP')
    expected={'checkpoint':'RUNNING','sequence':proof['unit_end'],'rows_observed':None,'orders':None,'supplies':None,'bundles':None,'coverage':'UNPROVEN_PENDING_DQ','lifecycle_changed':False,'scheduler_changed':False}
    state=BF.read_proof(backend.c,doc,r['run_id'],request=backend.request)['state']
    expected.update(rows_observed=state['rows'],orders=state['orders'],supplies=state['supplies'],bundles=state['bundles'])
    row={'version':D.VERSION,'root_hash':proof['root_hash'],'kind':'RECONCILED','sequence':seq,'payload':expected}
    if D.digest(row)!=proof['reconciliation_hash']:fail('expected reconciliation digest differs')
    name=marker(proof);value=marker_value(proof);ds=backend.c['datasets']['ref']
    old=backend.tables.get_table(ds,name)
    if old is None:
        if backend.tables.create_marker(ds,name,*value):old=value
        else:old=backend.tables.get_table(ds,name)
    if old!=value:fail('owner marker conflict')
    # Re-observe after owner fence, before any canonical bookkeeping.
    observe(backend,proof)
    existing=[x for x in backend.store.history(proof['root_hash']) if x['kind']=='RECONCILED' and x['sequence']==seq]
    if existing:
        if existing!=[row]:fail('existing reconciliation differs')
    else:
        result=BF.reconcile(doc,doc['ack_hash'],r,backend=backend)
        if result!=expected:fail('canonical result differs; preserve partial bookkeeping')
        backend.store.commit(proof['root_hash'],'RECONCILED',seq,result,now)
    observe(backend,proof,final=True)
    h=backend.store.commit(proof['root_hash'],KIND,seq,proof,now)
    load(backend,backend.store.history(proof['root_hash']),proof['root_hash'])
    return {'record_hash':h,'reconciliation_hash':proof['reconciliation_hash'],'owner_marker':name,'release':f"LD_{BF.B.digest(['BOUNDED_PILOT_EXCLUSIVE',backend.c['project_id']])[:16]}_{proof['lease_generation']:04d}",'source_calls':0,'source_dispatches':0}
