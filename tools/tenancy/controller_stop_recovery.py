"""Owner-attested post-reconciliation controller recovery, never a generic STOP bypass.

The writer supplies a freshly observed, source-free predicate certificate. An
owner-only REF marker pins its exact contents. Cloud readers can consume it, but
cannot create that marker. Original STOP/receipt/journal/lease rows are untouched.
Unknown predicates fail closed; successful receipts are never re-reconciled.
"""
import re
from tools.tenancy import durable_plan as D, tenant_backfill as BF
from tools.tenancy.validation import parse_tenant_json

KIND = 'CONTROLLER_STOP_RECOVERED'
TYPE = 'CONTROLLER_POST_RECONCILIATION_PRE_SOURCE_STOP'
VERSION = 1
PREDICATES = frozenset({
    'predecessor_terminal', 'source_persisted_reconciled',
    'no_successor_intent', 'no_successor_runtime', 'no_successor_receipt',
    'no_ambiguous_post', 'no_stopped_execution_async_uuid',
    'no_unreconciled_stopped_execution_rows', 'no_active_execution',
    'no_active_lease', 'bindings_valid', 'provenance_known', 'tenant_isolation',
    'original_stop_preserved', 'reconciled_before_stop',
})
FIELDS = frozenset({'version', 'type', 'tenant', 'project', 'root_hash', 'stop_hash',
    'predecessor_sequence', 'receipt_hash', 'reconciliation_hash', 'plan_id',
    'state_hash', 'state_sequence', 'controller_execution', 'controller_image',
    'controller_source_sha', 'runtime_execution', 'runtime_image', 'runtime_source_sha',
    'implementation_sha', 'authorization', 'predicates', 'evidence_hashes',
    'verified_at', 'hash'})
AUTH_FIELDS = frozenset({'owner_ack_sha256', 'tenant', 'project', 'root_hash',
    'stop_hashes', 'receipt_hash', 'reconciliation_hash', 'plan_id', 'type', 'version'})


def fail(message):
    raise BF.B.EvidenceError('controller stop recovery: ' + message)


def validate(proof):
    if not isinstance(proof, dict) or set(proof) != FIELDS:
        fail('closed proof schema required')
    if type(proof['version']) is not int or proof['version'] != VERSION or proof['type'] != TYPE:
        fail('unsupported typed recovery')
    if D.digest({k: v for k, v in proof.items() if k != 'hash'}) != proof['hash']:
        fail('proof hash differs')
    for k in ('root_hash','stop_hash','receipt_hash','reconciliation_hash','plan_id','state_hash','hash'):
        D.check_hash(proof[k])
    for k in ('implementation_sha','controller_source_sha','runtime_source_sha'):
        if not isinstance(proof[k],str) or not re.fullmatch('[0-9a-f]{40}',proof[k]):
            fail('unknown immutable source provenance')
    for k in ('controller_image','runtime_image'):
        if not isinstance(proof[k],str) or not re.fullmatch(r'[^\s]+@sha256:[0-9a-f]{64}',proof[k]):
            fail('unknown immutable image provenance')
    predicates=proof['predicates']
    if not isinstance(predicates,dict) or set(predicates)!=PREDICATES or any(v is not True for v in predicates.values()):
        fail('every predicate must be CONFIRMED, never unknown/inferred')
    auth=proof['authorization']
    if not isinstance(auth,dict) or set(auth)!=AUTH_FIELDS:
        fail('exact owner authorization required')
    for k in ('tenant','project','root_hash','receipt_hash','reconciliation_hash','plan_id','type','version'):
        if auth[k]!=proof[k]:fail('owner scope differs: '+k)
    D.check_hash(auth['owner_ack_sha256'])
    stops=auth['stop_hashes']
    if not isinstance(stops,list) or not stops or len(stops)!=len(set(stops)) or proof['stop_hash'] not in stops:
        fail('STOP not explicitly authorized')
    for h in stops:D.check_hash(h)
    if type(proof['predecessor_sequence']) is not int or proof['predecessor_sequence']<1 or type(proof['state_sequence']) is not int or proof['state_sequence']<1:
        fail('exact predecessor/state sequence required')
    if not isinstance(proof['evidence_hashes'],dict) or not proof['evidence_hashes']:
        fail('causal evidence digests required')
    for h in proof['evidence_hashes'].values():D.check_hash(h)
    from tools.tenancy.cloud_controller import timestamp
    timestamp(proof['verified_at'])
    c=BF.target(proof['tenant'])
    if proof['project']!=c['project_id']:fail('foreign tenant/project')
    base=f"projects/{proof['project']}/locations/{c['region']}/jobs/"
    if not proof['controller_execution'].startswith(base+'tenant-backfill-controller/executions/tenant-backfill-controller-'):
        fail('foreign controller execution')
    if not proof['runtime_execution'].startswith(base) or '/executions/' not in proof['runtime_execution']:
        fail('foreign predecessor runtime')
    return proof


def marker(proof):
    return 'BFCR_'+validate(proof)['hash']


def marker_value(proof):
    return ({'recovery':'controller_pre_source','root':proof['root_hash'][:16]},D.encoded(proof))


def verify_records(proof, records, root, tenant, *, publishing=False):
    validate(proof)
    if proof['root_hash']!=root or proof['tenant']!=tenant:fail('foreign root/tenant')
    selected={}
    for kind,key in [('STOPPED','stop_hash'),('DISPATCH_RECEIPT','receipt_hash'),('RECONCILED','reconciliation_hash')]:
        rows=[r for r in records if r.get('kind')==kind and D.digest(r)==proof[key]]
        if len(rows)!=1 or rows[0].get('root_hash')!=root or rows[0].get('version')!=D.VERSION:
            fail('exact immutable '+kind+' absent')
        selected[kind]=rows[0]
    seq=proof['predecessor_sequence']
    receipt=selected['DISPATCH_RECEIPT'];recon=selected['RECONCILED'];stop=selected['STOPPED']
    if receipt['sequence']!=seq or recon['sequence']!=seq or stop['payload'].get('controller_execution')!=proof['controller_execution']:
        fail('predecessor or stopped execution mismatch')
    doc=receipt['payload']['plan'];p=doc['runtime_plan']
    if p['project']!=proof['project'] or p['plan_id']!=proof['plan_id'] or doc['tenant_id']!=tenant:
        fail('foreign predecessor plan')
    if recon['payload'].get('sequence')!=proof['state_sequence']:
        fail('predecessor checkpoint sequence mismatch')
    intents=[r for r in records if r.get('kind')=='DISPATCH_INTENT' and r.get('sequence')==seq]
    if len(intents)!=1 or intents[0]['payload']['plan']!=doc or any(intents[0]['payload']['preparation'].get(k)!=receipt['payload']['receipt'].get(k) for k in ('run_id','lease_generation','ack_hash')):
        fail('predecessor intent linkage differs')
    if publishing and any(r.get('kind') in {'DISPATCH_INTENT','DISPATCH_RECEIPT','RECONCILED'} and r.get('sequence',0)>seq for r in records):
        fail('successor source authority already exists')
    return doc


def terminal_executions(backend,proof):
    """GET only: retained controller and successful predecessor execution proof."""
    c=backend.c
    rows=backend.store.history(proof['root_hash'])
    receipt=next(r['payload']['receipt'] for r in rows if D.digest(r)==proof['receipt_hash'])
    operation=backend.request('GET',BF.RUN_API+'/'+receipt['operation'])
    if operation.get('done') is not True or operation.get('error') or operation.get('metadata',{}).get('name')!=proof['runtime_execution']:
        fail('predecessor operation linkage/termination differs')
    for key,role in [('controller','controller'),('runtime','runtime')]:
        execution=backend.request('GET',BF.RUN_API+'/'+proof[key+'_execution'])
        if execution.get('name')!=proof[key+'_execution'] or not execution.get('completionTime'):
            fail('historical execution absent/nonterminal')
        template=execution['template'];container=template['containers'][0]
        account=(c['marketplaces']['ozon']['service_accounts']['runtime'] if role=='runtime' else 'sa-backfill-controller')
        if template['serviceAccount']!=account+'@'+c['project_id']+'.iam.gserviceaccount.com' or container['image']!=proof[key+'_image']:
            fail('historical image/identity differs')
        env={e['name']:e.get('value') for e in container.get('env',[])}
        # Canonical runtime derives the tenant boundary from project/datasets;
        # TENANT_ID is mandatory for controller, optional (never foreign) for runtime.
        if env.get('TENANT_ID',c['tenant_id'])!=c['tenant_id'] or key=='controller' and env.get('TENANT_ID')!=c['tenant_id'] or env.get('GCP_PROJECT_ID')!=c['project_id'] or env.get('TENANT_BINDING_REQUIRED')!='1':
            fail('historical isolation/binding gate differs')
        if key=='runtime' and any(env.get(var)!=c['datasets'][ds] for var,ds in [('BQ_RAW_DATASET','ozon_raw'),('BQ_REF_DATASET','ref')]):
            fail('historical runtime dataset boundary differs')
        if env.get('STRICT_PAGE_CAPS')!='1':fail('historical strict caps differ')
        if key=='controller' and (env.get('BACKFILL_ROOT_HASH')!=proof['root_hash'] or env.get('CONTROLLER_SOURCE_SHA')!=proof['controller_source_sha']):
            fail('historical controller root/source differs')
        if key=='runtime' and env.get('INGESTION_RUN_ID')!=receipt['run_id']:
            fail('predecessor run attribution differs')
        if key=='runtime' and (execution.get('succeededCount')!=1 or execution.get('failedCount',0)):
            fail('predecessor did not terminate successfully')
        if key=='controller' and (execution.get('failedCount')!=1 or execution.get('succeededCount',0)):
            fail('stopped controller terminal state differs')


def load(backend, records, root, *, require_owner_marker=True):
    """Read-only authority verification; never modifies quota or source receipts."""
    proofs=[]
    for record in records:
        if record.get('kind')!=KIND:continue
        proof=record['payload'];verify_records(proof,records,root,backend.c['tenant_id'])
        if record['sequence']!=proof['predecessor_sequence']:fail('recovery sequence mismatch')
        if require_owner_marker and backend.tables.get_table(backend.c['datasets']['ref'],marker(proof))!=marker_value(proof):
            fail('owner-only immutable attestation missing')
        terminal_executions(backend,proof)
        proofs.append(proof)
    if len({p['stop_hash'] for p in proofs})!=len(proofs):fail('conflicting recovery attestations')
    return proofs


def publish(backend,proof,now):
    """Owner-only additive commit after independent source-free re-observation.

    The canonical observer re-reads metadata/SELECT and cannot start/reconcile
    source execution. The certificate pins the audit timestamp, closed schema,
    exact owner scope and confirmed predicates.
    """
    validate(proof)
    from tools.tenancy import orchestration_contract as O
    source=backend.c['orchestration']['job']['env']['CONTROLLER_SOURCE_SHA']
    if proof['implementation_sha']!=source:
        fail('recovery implementation differs from qualified deployed source')
    release=parse_tenant_json((BF.REPO/'infra/tenant/releases/backfill'/(source+'.json')).read_text())
    O.verify_artifact_source(release,BF.REPO)
    records=backend.store.history(proof['root_hash'])
    verify_records(proof,records,proof['root_hash'],backend.c['tenant_id'],publishing=True)
    manifest=next(r['payload'] for r in records if r['kind']=='MANIFEST')
    backend.preflight(manifest,owner_observation=True)
    if backend.active_runtime_execution():fail('active authority execution')
    fresh=observe(backend,proof,now)
    if fresh!=proof:fail('fresh predicate/provenance certificate differs')
    terminal_executions(backend,proof)
    candidate={'version':D.VERSION,'root_hash':proof['root_hash'],'kind':KIND,'sequence':proof['predecessor_sequence'],'payload':proof}
    existing=[r for r in records if r.get('kind')==KIND and r['payload'].get('stop_hash')==proof['stop_hash']]
    if existing and existing!=[candidate]:fail('immutable recovery conflict')
    name=marker(proof);value=marker_value(proof);ds=backend.c['datasets']['ref']
    old=backend.tables.get_table(ds,name)
    if old is None:
        if backend.tables.create_marker(ds,name,*value):old=value
        else:old=backend.tables.get_table(ds,name)
    if old!=value:fail('owner marker conflict')
    h=backend.store.commit(proof['root_hash'],KIND,proof['predecessor_sequence'],proof,now)
    load(backend,backend.store.history(proof['root_hash']),proof['root_hash'])
    return {'record_hash':h,'owner_marker':name,'original_stop_hash':proof['stop_hash'],'source_dispatches':0}


def observe(backend,proof,now):
    """Canonical fresh owner audit. GET/list/SELECT only; no source/coverage writes.

    Historical source dominance is accepted by the exact owner authorization and
    its retained causal digests. Fresh inventory, raw accounting, bindings, lease,
    terminal executions and absence of successor authority are independently read.
    No absence of UUID alone is used as proof of no POST.
    """
    validate(proof)
    from tools.tenancy.cloud_controller import timestamp
    records=backend.store.history(proof['root_hash'])
    doc=verify_records(proof,records,proof['root_hash'],backend.c['tenant_id'],publishing=True)
    manifest=next(r['payload'] for r in records if r['kind']=='MANIFEST')
    backend.preflight(manifest,owner_observation=True)
    terminal_executions(backend,proof)
    p=doc['runtime_plan'];c=backend.c;seq=proof['predecessor_sequence']
    # No fence/streaming row may hide outside committed history.
    successor=backend.select(f"SELECT COUNT(*) AS n FROM `{c['project_id']}.{c['datasets']['tenant_ops']}.BACKFILL_CHECKPOINTS` WHERE plan_hash=@root AND entity='orchestration' AND JSON_VALUE(evidence_json,'$.kind') IN ('DISPATCH_INTENT','DISPATCH_RECEIPT','RECONCILED') AND attempts>@seq",{'root':('STRING',proof['root_hash']),'seq':('INT64',seq)})
    if successor!=[{'n':0}]:fail('successor committed/uncommitted source records exist')
    for name,labels,created in backend.tables.list_tables(c['datasets']['tenant_locks']):
        prefix='BFQ_'+proof['root_hash']+'_'
        if name.startswith(prefix):
            suffix=name[len(prefix):].split('_',1)[0]
            if not suffix.isdigit() or int(suffix)>seq:fail('successor/unknown dispatch fence exists')
    times=backend.select(f"SELECT evidence_json,updated_at FROM `{c['project_id']}.{c['datasets']['tenant_ops']}.BACKFILL_CHECKPOINTS` WHERE plan_hash=@root AND entity='orchestration' AND backfill_id IN (@stop,@recon) LIMIT 10",{'root':('STRING',proof['root_hash']),'stop':('STRING',proof['stop_hash'][:16]),'recon':('STRING',proof['reconciliation_hash'][:16])})
    by={}
    for row in times:
        h=D.digest(parse_tenant_json(row['evidence_json']));at=timestamp(row['updated_at'])
        if h in by and by[h]!=at:fail('historical timestamp conflict')
        by[h]=at
    if set(by)!={proof['stop_hash'],proof['reconciliation_hash']} or by[proof['reconciliation_hash']]>=by[proof['stop_hash']]:
        fail('reconciliation before STOP not proven')
    controller=backend.request('GET',BF.RUN_API+'/'+proof['controller_execution'])
    start=controller.get('startTime')
    if not start:fail('stopped execution start missing')
    # Entire journal bounded by this incident start, not just known plan IDs.
    rows=backend.select(f"SELECT COUNT(*) AS n FROM `{backend.journal}` WHERE started_at>=@start",{'start':('TIMESTAMP',start)})
    if rows!=[{'n':0}]:fail('new/unattributed source journal activity exists')
    base=f"projects/{c['project_id']}/locations/{c['region']}"
    jobs=backend.request('GET',BF.RUN_API+'/'+base+'/jobs?pageSize=1000')
    if jobs.get('nextPageToken') or len(jobs.get('jobs',[]))!=5:fail('tenant job inventory drift')
    for job in jobs['jobs']:
        for x in BF.execution_inventory(backend.request,job['name'])['executions']:
            if not x.get('completionTime'):fail('active tenant execution')
            if '/jobs/tenant-backfill-controller/' not in x['name'] and timestamp(x['createTime'])>=timestamp(start):
                fail('successor or unrelated child runtime exists')
    state=backend.state(doc)
    if state['sequence']!=proof['state_sequence'] or D.digest(state)!=proof['state_hash'] or state['progress'].get('report'):
        fail('checkpoint/async source evidence differs')
    if p['entity']!='supplies':fail('unsupported post-reconciliation source accounting')
    units=backend.select(f"SELECT DISTINCT backfill_sequence,backfill_detail_json FROM `{backend.journal}` WHERE started_at>=@origin AND backfill_plan_id=@pid AND entity='supplies' AND status='OK' AND backfill_sequence IS NOT NULL ORDER BY backfill_sequence",{'origin':('TIMESTAMP',p['origin']),'pid':('STRING',p['plan_id'])})
    details={}
    for u in units:
        d=BF.source_detail(parse_tenant_json(u['backfill_detail_json']));n=u['backfill_sequence']
        if n in details and details[n]!=d:fail('source sequence conflict')
        details[n]=d
    if sorted(details)!=list(range(1,state['sequence']+1)):fail('source evidence gap')
    progress=state['progress'];pending={str(x[0]) for x in progress['pending']}
    if progress['bundle']:pending.add(str(progress['bundle']['link'][0]))
    bids=[x for x in progress['bundle_links'] if x not in pending]
    if len(bids)!=state['bundles']:fail('bundle source counts differ')
    ids=[str(x) for x in progress['seen_orders']]
    checks=[('RAW_OZON_SUPPLY_ORDERS','order_id','order_id',ids,state['orders']),
        ('RAW_OZON_SUPPLIES','order_id,supply_id','order_id',ids,state['supplies']),
        ('RAW_OZON_SUPPLY_BUNDLES','bundle_id,sku','bundle_id',bids,sum(d['source_items'] for d in details.values() if d.get('bundle_complete')))]
    for table,keys,key,values,n in checks:
        out=backend.select(f"SELECT COUNT(*) AS rows_n,COUNT(DISTINCT TO_JSON_STRING(STRUCT({keys}))) AS keys_n FROM `{c['project_id']}.{p['raw']}.{table}` WHERE CAST({key} AS STRING) IN UNNEST(JSON_VALUE_ARRAY(@ids))",{'ids':('STRING',D.encoded(values))})
        if out!=[{'rows_n':n,'keys_n':n}]:fail('source/persisted key mismatch')
    _,_,ledger,_,hold=BF.TL.read_state(c,backend.tables)
    if hold:fail('owner hold')
    cid=BF.B.digest(['BOUNDED_PILOT_EXCLUSIVE',c['project_id']])[:16]
    receipt=next(r['payload']['receipt'] for r in records if D.digest(r)==proof['receipt_hash'])
    gen=receipt['lease_generation']
    generations=[r['lease_generation'] for r in ledger if parse_tenant_json(r.get('evidence_json') or '{}').get('mode')=='BOUNDED_PILOT']
    if not generations or max(generations)!=gen:fail('successor/unknown lease generation')
    released=backend.tables.get_table(c['datasets']['tenant_locks'],f'LD_{cid}_{gen:04d}')
    if not released or released[0].get('owner')!=receipt['run_id'] or parse_tenant_json(released[1]).get('ack_hash')!=doc['ack_hash']:
        fail('predecessor lease release not exact')
    for name,labels,_ in backend.tables.list_tables(c['datasets']['tenant_locks']):
        match=BF.CK.LEASE_RE.match(name)
        if match and match.group(1)==cid:
            generation=int(match.group(2))
            if generation>gen:fail('successor lease exists')
            release=backend.tables.get_table(c['datasets']['tenant_locks'],f'LD_{cid}_{generation:04d}')
            if not release or not labels.get('owner') or release[0].get('owner')!=labels['owner']:
                fail('lease lacks exact terminal owner release')
            if generation==gen and labels['owner']!=receipt['run_id']:
                fail('predecessor lease owner differs')
    # Historical exact source/image registrations, not mutable HEAD assumptions.
    from tools.tenancy import platform as PL
    controller_release=BF.REPO/'infra/tenant/releases/backfill'/(proof['controller_source_sha']+'.json')
    if not controller_release.is_file():fail('historical controller provenance absent')
    release=parse_tenant_json(controller_release.read_text())
    if release['image']!=proof['controller_image'] or any(x!='PASS' for x in release['verification'].values()):
        fail('historical controller qualification differs')
    runtimes=[parse_tenant_json(f.read_text()) for f in (BF.REPO/PL.RUNTIME_RELEASES_DIR/'ozon').glob('*.json')]
    runtimes=[r for r in runtimes if r['image']==proof['runtime_image'] and r['source']['commit']==proof['runtime_source_sha']]
    if len(runtimes)!=1:fail('historical runtime provenance absent')
    # Certificate pins exact fresh audit time. Caller cannot reuse an old audit.
    if timestamp(proof['verified_at'])!=now:fail('fresh observation timestamp differs')
    return proof
