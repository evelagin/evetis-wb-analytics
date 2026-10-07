"""Owner-only metadata attestation for a proven failed-before-POST attempt.

Cloud readers may verify this contract; their append identity cannot publish its
ref marker. No failed receipt becomes RECONCILED/DONE, no old journal is edited.
"""
from tools.tenancy.validation import parse_tenant_json
from tools.tenancy import tenant_backfill as BF
import pre_source as PS

KIND = 'FAILED_PRE_SOURCE'


def verify_records(proof, records, root, tenant):
    if proof['root_hash']!=root or proof['tenant']!=tenant:
        raise BF.B.EvidenceError('foreign pre-source recovery root/tenant')
    seq=proof['dispatch_sequence']
    intents=[r for r in records if r['kind']=='DISPATCH_INTENT' and r['sequence']==seq]
    receipts=[r for r in records if r['kind']=='DISPATCH_RECEIPT' and r['sequence']==seq]
    if len(intents)!=1 or len(receipts)!=1:
        raise BF.B.EvidenceError('exact failed dispatch chain required')
    i,r=intents[0],receipts[0];doc=r['payload']['plan'];p=doc['runtime_plan']
    PS.validate(proof,p)
    if BF.B.digest(i)!=proof['intent_hash'] or BF.B.digest(r)!=proof['receipt_hash'] or i['payload']['plan']!=doc:
        raise BF.B.EvidenceError('failed receipt/intent identity conflict')
    receipt=r['payload']['receipt']
    if receipt['run_id']!=proof['run_id'] or receipt['lease_generation']!=proof['lease_generation'] or i['payload']['preparation']['run_id']!=proof['run_id']:
        raise BF.B.EvidenceError('failed run/lease linkage conflict')
    stops={BF.B.digest(x) for x in records if x['kind']=='STOPPED'}
    if not set(proof['stop_hashes'])<=stops:
        raise BF.B.EvidenceError('predecessor STOP evidence absent')
    if any(x['kind']=='RECONCILED' and x['sequence']==seq for x in records):
        raise BF.B.EvidenceError('cannot recover an already successful receipt')
    return doc


def load(backend, records, root, *, require_owner_marker=True):
    """Every recovery is checked against owner marker, source unit and terminal job."""
    proofs=[]
    for record in records:
        if record['kind']!=KIND:continue
        proof=record['payload'];doc=verify_records(proof,records,root,backend.c['tenant_id'])
        if record['sequence']!=proof['dispatch_sequence']:
            raise BF.B.EvidenceError('recovery sequence conflict')
        if require_owner_marker and backend.tables.get_table(backend.c['datasets']['ref'],PS.marker(proof))!=PS.marker_value(proof):
            raise BF.B.EvidenceError('owner-only pre-source recovery attestation missing')
        p=doc['runtime_plan'];params={'origin':('TIMESTAMP',p['origin']),'pid':('STRING',p['plan_id']),
            'seq':('INT64',proof['unit_sequence']),'run':('STRING',proof['run_id'])}
        rows=backend.select(f"SELECT DISTINCT evidence_json,backfill_detail_json FROM `{backend.journal}` WHERE started_at>=@origin AND backfill_plan_id=@pid AND backfill_sequence=@seq AND entity='ads_sku_daily' AND status='OK' LIMIT 2",params)
        if len(rows)!=1:raise BF.B.EvidenceError('failed intent source proof ambiguous')
        original=parse_tenant_json(rows[0]['evidence_json']);detail=BF.source_detail(parse_tenant_json(rows[0]['backfill_detail_json']))
        if original['plan']!=p or detail.get('action')!='REPORT_INTENT' or detail.get('exports_reserved')!=proof['exports_reserved']:
            raise BF.B.EvidenceError('failed source reservation mismatch')
        PS.validate_state(proof,p,original['state'])
        rows=backend.select(f"SELECT status FROM `{backend.journal}` WHERE started_at>=@origin AND ingestion_run_id=@run AND entity='ads_sku_daily' LIMIT 2",params)
        if len(rows)!=1 or rows[0]['status']!='FAILED':raise BF.B.EvidenceError('failed aggregate attribution unproven')
        execution=backend.request('GET',f"{BF.RUN_API}/projects/{p['project']}/locations/{backend.c['region']}/jobs/{next(n for n,j in backend.c['marketplaces']['ozon']['jobs'].items() if p['entity'] in j['entities'])}/executions/{proof['execution']}")
        env={x['name']:x.get('value') for x in execution['template']['containers'][0].get('env',[])}
        if not execution.get('completionTime') or execution.get('failedCount')!=1 or execution.get('succeededCount',0) or execution['template']['serviceAccount']!=f"{backend.c['marketplaces']['ozon']['service_accounts']['runtime']}@{p['project']}.iam.gserviceaccount.com" or execution['template']['containers'][0]['image']!=proof['image'] or env.get('INGESTION_RUN_ID')!=proof['run_id']:
            raise BF.B.EvidenceError('failed execution no longer matches attestation')
        proofs.append(proof)
    if len({p['dispatch_sequence'] for p in proofs})!=len(proofs) or len({(p['plan_id'],p['unit_sequence']) for p in proofs})!=len(proofs):
        raise BF.B.EvidenceError('conflicting recovery attestations')
    return proofs


def audit(backend, root, dispatch_sequence, unit_sequence, query_id, now, source_reader):
    """Owner audit, read-only. No proof is inferred from an absence of UUID alone.

    The exact failed SQL identity and old registered code dominate the report POST.
    source_reader reads that immutable Git source; it is never a runtime dependency.
    Only this audited failure category is supported, never a generic reset command.
    """
    from tools.tenancy import durable_plan as D, platform as PL
    from tools.tenancy.validation import parse_tenant_json
    records=backend.store.history(root)
    receipts=[r for r in records if r['kind']=='DISPATCH_RECEIPT' and r['sequence']==dispatch_sequence]
    intents=[r for r in records if r['kind']=='DISPATCH_INTENT' and r['sequence']==dispatch_sequence]
    if len(receipts)!=1 or len(intents)!=1:raise BF.B.EvidenceError('exact failure chain required')
    receipt=receipts[0]['payload']['receipt'];doc=receipts[0]['payload']['plan'];p=doc['runtime_plan']
    run=receipt['run_id'];params={'origin':('TIMESTAMP',p['origin']),'pid':('STRING',p['plan_id']),
        'seq':('INT64',unit_sequence),'run':('STRING',run)}
    units=backend.select(f"SELECT DISTINCT backfill_sequence,evidence_json,backfill_detail_json FROM `{backend.journal}` WHERE started_at>=@origin AND backfill_plan_id=@pid AND entity='ads_sku_daily' AND status='OK' AND backfill_sequence>=@seq ORDER BY backfill_sequence LIMIT 2",params)
    if len(units)!=1 or units[0]['backfill_sequence']!=unit_sequence:raise BF.B.EvidenceError('failure checkpoint advanced or absent')
    unit=units[0];original=parse_tenant_json(unit['evidence_json']);state=original['state'];detail=BF.source_detail(parse_tenant_json(unit['backfill_detail_json']))
    if original['plan']!=p or detail.get('action')!='REPORT_INTENT' or detail.get('logical_requests')!=0:
        raise BF.B.EvidenceError('intent source boundary unproven')
    job=next(n for n,j in backend.c['marketplaces']['ozon']['jobs'].items() if p['entity'] in j['entities'])
    operation=backend.request('GET',f"{BF.RUN_API}/{receipt['operation']}")
    if operation.get('done') is not True or not operation.get('error') or not operation.get('metadata',{}).get('name'):
        raise BF.B.EvidenceError('failed operation not terminal')
    execution=backend.request('GET',f"{BF.RUN_API}/{operation['metadata']['name']}")
    if f'/jobs/{job}/executions/' not in execution['name']:raise BF.B.EvidenceError('foreign failed execution')
    cont=execution['template']['containers'][0];image=cont['image']
    releases=[parse_tenant_json(f.read_text()) for f in (BF.REPO/PL.RUNTIME_RELEASES_DIR/'ozon').glob('*.json')]
    releases=[r for r in releases if r['image']==image]
    if len(releases)!=1:raise BF.B.EvidenceError('failed runtime image provenance missing')
    source=releases[0]['source']['commit']
    # Fail closed if this specialized auditor is used after that old access path
    # changes. Inspect exact old source, never assume current code ran historically.
    code=source_reader(source,'pipelines/ozon/runtime/backfill.py')
    hook=code[code.index('    def before_submit():'):code.index('    engine.before_submit = before_submit')]
    post=code[code.index('        if report["phase"] == "INTENT":'):code.index('        d = self.call(f"/api/client/statistics/')]
    if 'CAPABILITY_PROFILE' not in hook or 'C.bq().query(' not in hook or post.index('self.before_submit()')>post.index('self.call("/api/client/statistics"'):
        raise BF.B.EvidenceError('immutable source does not prove permission failure before POST')
    q=backend.request('GET',f"{BF.TT.BQ}/projects/{p['project']}/jobs/{query_id}?location=EU")
    query=q.get('configuration',{}).get('query',{}).get('query','')
    expected_sa=f"{backend.c['marketplaces']['ozon']['service_accounts']['runtime']}@{p['project']}.iam.gserviceaccount.com"
    if q.get('user_email')!=expected_sa or q.get('status',{}).get('errorResult',{}).get('reason')!='accessDenied' or not query.lstrip().startswith('SELECT ') or query.count('`')!=2 or f"`{p['project']}.tenant_ops.CAPABILITY_PROFILE`" not in query:
        raise BF.B.EvidenceError('exact access failure identity/object unproven')
    from tools.tenancy.cloud_controller import timestamp
    at=timestamp(str(int(q['statistics']['creationTime'])/1000))
    if not execution.get('completionTime') or not timestamp(execution['startTime'])<=at<=timestamp(execution['completionTime']):
        raise BF.B.EvidenceError('query outside failed execution')
    rows=backend.select(f"SELECT COUNT(*) AS n FROM `{p['project']}.{p['raw']}.RAW_OZON_ADS_SKU_DAILY` WHERE ingestion_run_id=@run",{'run':params['run']})
    if len(rows)!=1 or rows[0]['n']!=0:raise BF.B.EvidenceError('failed source attempt wrote SKU data')
    backend.sku_accounting(doc,state)
    stop_hashes=[D.digest(r) for r in records if r['kind']=='STOPPED']
    if len(stop_hashes)!=1 or next(r for r in records if r['kind']=='STOPPED')['payload']!={'reason':'CONTROLLER_GATE_OR_EVIDENCE_FAILURE'}:
        raise BF.B.EvidenceError('exact predecessor STOP scope changed')
    report=state['progress'].get('report') or {}
    proof={'version':PS.VERSION,'root_hash':root,'tenant':backend.c['tenant_id'],'project':p['project'],
        'plan_id':p['plan_id'],'dispatch_sequence':dispatch_sequence,'run_id':run,'execution':execution['name'].split('/')[-1],
        'image':image,'source_sha':source,'lease_generation':receipt['lease_generation'],'intent_hash':D.digest(intents[0]),
        'receipt_hash':D.digest(receipts[0]),'stop_hashes':stop_hashes,'unit_sequence':unit_sequence,'state_hash':D.digest(state),
        'cohort_hash':report.get('cohort_hash'),'exports_reserved':detail.get('exports_reserved'),'query_id':query_id,
        'failure_stage':'CAPABILITY_PROFILE_ACCESS_DENIED_BEFORE_REPORT_POST','verified_at':now.isoformat(),
        'post_attempts':0,'uuid_present':bool(report.get('uuid')),'sku_rows_written':rows[0]['n']}
    proof['hash']=D.digest(proof);PS.validate_state(proof,p,state)
    record={'version':D.VERSION,'root_hash':root,'kind':KIND,'sequence':dispatch_sequence,'payload':proof}
    load(backend,records+[record],root,require_owner_marker=False)
    return proof


def publish(backend, proof, now):
    """Explicit owner-only append, no deletion/reset/rewriting of predecessor evidence.

    Caller must have passed deployed-image/IAM preflight and fresh isolation gates.
    It supplies owner Tables + durable writer, not cloud append identity for ref.
    """
    from tools.tenancy import durable_plan as D
    root=proof['root_hash'];records=backend.store.history(root)
    record={'version':D.VERSION,'root_hash':root,'kind':KIND,'sequence':proof['dispatch_sequence'],'payload':proof}
    existing=[r for r in records if r['kind']==KIND and r['sequence']==proof['dispatch_sequence']]
    if existing and existing!=[record]:raise BF.B.EvidenceError('immutable recovery already differs')
    manifest=next(r['payload'] for r in records if r['kind']=='MANIFEST')
    backend.preflight(manifest,owner_observation=True)
    if backend.active_runtime_execution():raise BF.B.EvidenceError('active execution: recovery publication deferred')
    load(backend,records if existing else records+[record],root,require_owner_marker=False)
    name=PS.marker(proof);labels,description=PS.marker_value(proof);ds=backend.c['datasets']['ref']
    old=backend.tables.get_table(ds,name)
    if old is None:
        if not backend.tables.create_marker(ds,name,labels,description):old=backend.tables.get_table(ds,name)
        else:old=(labels,description)
    if old!=(labels,description):raise BF.B.EvidenceError('immutable owner marker conflict')
    h=backend.store.commit(root,KIND,proof['dispatch_sequence'],proof,now)
    load(backend,backend.store.history(root),root)
    return {'record_hash':h,'owner_marker':name,'failed_run_id':proof['run_id'],
            'failed_unit_sequence':proof['unit_sequence'],'old_reservation_preserved':True,'old_STOP_preserved':True}
