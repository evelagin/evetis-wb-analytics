"""Cloud-native bounded qualification adapter; no local credentials or source HTTP.

The only source execution path is the existing qualified tenant_backfill start.
An uncertain Run POST is never repeated. Full-history GO has separate gates and
is deliberately unavailable until the full-plan adapter itself is qualified.
"""
from __future__ import annotations
import json
import os
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from tools.tenancy import tenant_backfill as BF, durable_plan as D
from tools.tenancy import cloud_access as A, cloud_tick as T, orchestration_contract as O
from tools.tenancy.validation import parse_tenant_json


def timestamp(value):
    if isinstance(value,str) and re.fullmatch(r'[0-9]+(?:\.[0-9]+)?(?:[Ee][+-]?[0-9]+)?',value):
        # BigQuery REST may use scientific epoch seconds. Preserve microseconds
        # with Decimal rather than introducing a float rounding boundary.
        epoch=Decimal(value);seconds=int(epoch)
        try:
            return datetime.fromtimestamp(seconds,timezone.utc)+timedelta(microseconds=int((epoch-seconds)*1000000))
        except (OverflowError,OSError,ValueError):
            raise BF.B.EvidenceError('timestamp outside supported range') from None
    at=datetime.fromisoformat(value.replace('Z','+00:00'))
    if at.utcoffset() is None:
        raise BF.B.EvidenceError('timestamp without timezone')
    return at.astimezone(timezone.utc)


def descriptor_name(source, root, state):
    if not isinstance(source,str) or not re.fullmatch(r'[0-9a-f]{40}',source):
        raise BF.B.EvidenceError('immutable controller source required')
    if state not in {'PAUSED','ENABLED'}:
        raise BF.B.EvidenceError('explicit historical scheduler state required')
    return 'BF_SPEC_'+source+'_'+D.check_hash(root)+'_'+state


def bootstrap(env, send=A.wire):
    # The source file is injected at immutable image build, not guessed from a
    # mutable env/ref or an unavailable local checkout in the deployed job.
    source=(BF.REPO/'CONTROLLER_SOURCE_SHA').read_text().strip()
    if env.get('CONTROLLER_SOURCE_SHA')!=source:
        raise BF.B.EvidenceError('compiled controller provenance differs')
    c=BF.target(env.get('TENANT_ID',''))
    if env.get('GCP_PROJECT_ID')!=c['project_id']:
        raise BF.B.EvidenceError('controller target differs from canonical registry')
    root=D.check_hash(env.get('BACKFILL_ROOT_HASH'))
    access=A.CloudAccess(c,A.CloudTokens(c,send),send)
    marker=access.tables.get_table(c['datasets']['tenant_locks'],descriptor_name(source,root,env.get('HISTORICAL_SCHEDULER_STATE')))
    if marker is None:
        raise BF.B.EvidenceError('canonical deployment descriptor unavailable')
    descriptor=parse_tenant_json(marker[1] or '{}')
    if set(descriptor)!={'settings','release'} or descriptor['settings'].get('release')!=source or descriptor['settings'].get('root_hash')!=root:
        raise BF.B.EvidenceError('deployment descriptor scope/provenance differs')
    O.verify_artifact_source(descriptor['release'],BF.REPO)
    block=O.block(c,descriptor['settings'],BF.REPO,release=descriptor['release'])
    if any(env.get(k)!=v for k,v in block['job']['env'].items()):
        raise BF.B.EvidenceError('controller deployment env differs')
    c=dict(c,orchestration=block)
    execution=env.get('CLOUD_RUN_EXECUTION','')
    if not re.fullmatch(re.escape(O.JOB)+r'-[a-z0-9]+',execution) or env.get('CLOUD_RUN_TASK_INDEX')!='0' or env.get('CLOUD_RUN_TASK_COUNT')!='1' or env.get('CLOUD_RUN_TASK_ATTEMPT')!='0':
        raise BF.B.EvidenceError('single bounded controller execution required')
    base,_=BF.resources(c)
    access.c=c
    return Backend(access,base+'/jobs/'+O.JOB+'/executions/'+execution),root


class Backend:
    def __init__(self,access,current_execution,clock=None):
        self.access,self.c=access,access.c
        self.current_execution=current_execution
        self.clock=clock or (lambda:datetime.now(timezone.utc))
        self.active=False
        self.store=access.durable_records()
    @property
    def tables(self):return self.access.tables
    def request(self,*args):return self.access.request(*args)
    def append_request(self,*args):return self.access.append_request(*args)
    def dispatch(self,*args):return self.access.dispatch(*args)
    def select(self,sql,params):return BF.select(self.c,sql,params,request=self.request)
    @property
    def journal(self):return f"{self.c['project_id']}.{self.c['datasets']['ozon_raw']}.OZON_INGESTION_RUNS"

    def preflight(self,manifest, *, owner_observation=False):
        if manifest['tenant']!=self.c['tenant_id'] or manifest['project']!=self.c['project_id']:
            raise BF.B.EvidenceError('foreign qualification manifest')
        self.active=False
        entities={p['runtime_plan']['entity'] for p in manifest['plans']}
        if not entities <= {'ads_sku_daily','supplies'}:
            raise BF.B.EvidenceError('qualification controller supports only frozen SKU/Supplies scopes')
        for doc in manifest['plans']:
            BF.preflight(BF.validate_plan(doc,doc['ack_hash']),doc,self.clock(),backend=self,allow_active=not owner_observation,owner_observation=owner_observation)
        # Both credential boundaries are required even for a Seller-only scope.
        bindings,creds=BF.TL.operator_binding(self.c,self.tables,self.clock(),['ads_sku_daily'])
        if set(bindings)!={'seller','performance'} or any(v!='BOUND' for v in bindings.values()) or set(creds)!={'seller','performance'} or any(v.get('status')!='PASS' for v in creds.values()):
            raise BF.B.EvidenceError('both binding/credential gates required')
        self.binding_status=bindings
        # Never rely on TTL-expiring authoritative manifests/fences/checkpoints.
        for ds in ('tenant_locks','tenant_ops'):
            url=f"{BF.TT.BQ}/projects/{self.c['project_id']}/datasets/{self.c['datasets'][ds]}?datasetView=METADATA"
            metadata=self.request('GET',url)
            if metadata.get('defaultTableExpirationMs') or metadata.get('defaultPartitionExpirationMs'):
                raise BF.B.EvidenceError('authoritative storage retention unsafe')
        table=self.request('GET',f"{BF.TT.BQ}/projects/{self.c['project_id']}/datasets/{self.c['datasets']['tenant_ops']}/tables/BACKFILL_CHECKPOINTS")
        if table.get('expirationTime') or (table.get('timePartitioning') or {}).get('expirationMs'):
            raise BF.B.EvidenceError('checkpoint evidence expires')
        for name,labels,created,expiry in self.tables.list_tables(self.c['datasets']['tenant_locks'],with_expiry=True):
            if name.startswith(('BFR_','BFQ_','BF_SPEC_')) and expiry:
                raise BF.B.EvidenceError('authoritative commit/deployment marker expires')

    def active_runtime_execution(self):return self.active

    def state(self,doc):
        p=doc['runtime_plan']
        params={'origin':('TIMESTAMP',p['origin']),'pid':('STRING',p['plan_id']),'entity':('STRING',p['entity'])}
        where='started_at >= @origin AND backfill_plan_id = @pid AND entity = @entity AND status = \'OK\''
        rows=self.select(f'SELECT MAX(backfill_sequence) AS sequence FROM `{self.journal}` WHERE {where}',params)
        if len(rows)!=1:raise BF.B.EvidenceError('source state inventory ambiguous')
        seq=rows[0]['sequence']
        if seq is None:return BF.B.initial(p)
        params['seq']=('INT64',seq)
        rows=self.select(f'SELECT DISTINCT evidence_json FROM `{self.journal}` WHERE {where} AND backfill_sequence = @seq LIMIT 2',params)
        if not rows:raise BF.B.EvidenceError('source checkpoint disappeared; never reset progress')
        states=[]
        for row in rows:
            proof=parse_tenant_json(row['evidence_json'])
            if proof.get('plan')!=p:raise BF.B.EvidenceError('source checkpoint plan drift')
            states.append(BF.B.validate(p,proof['state']))
        if any(s!=states[0] for s in states) or states[0]['sequence']!=seq:
            raise BF.B.EvidenceError('source checkpoint sequence conflict')
        return states[0]

    def quota(self,manifest,now):
        phases=[]
        cap=15; cooldown=None; calibrated=False
        for doc in manifest['plans']:
            if doc['runtime_plan']['entity']=='ads_sku_daily':
                state=self.state(doc);report=state['progress'].get('report')
                if BF.QF.matches(doc) and doc['runtime_plan']['plan_id']==BF.QF.SKU:
                    self.sku_accounting(doc,state,linkage=False)
                    cap=BF.QF.guard(doc['runtime_plan'],state);calibrated=True
                    cooldown=state['progress'].get('rate_limit',{}).get('eligible_at')
                if report is not None:
                    approved=[p for p in getattr(self,'verified_pre_source_failures',[]) if p['plan_id']==doc['runtime_plan']['plan_id'] and p['unit_sequence']==state['sequence']]
                    if approved:
                        from tools.tenancy.pre_source_recovery import PS
                        PS.validate_state(approved[0],doc['runtime_plan'],state)
                    else:phases.append(report['phase'])
        if len(phases)>1:raise BF.B.EvidenceError('multiple active async report scopes')
        phase=phases[0] if phases else None
        params={'since':('TIMESTAMP',(now-timedelta(hours=24)).isoformat())}
        rows=self.select(f"SELECT backfill_plan_id AS plan_id,backfill_sequence AS sequence,MAX(SAFE_CAST(JSON_VALUE(backfill_detail_json,'$.exports_reserved') AS INT64)) AS exports,MAX(started_at) AS reserved_at FROM `{self.journal}` WHERE started_at >= @since AND entity = 'ads_sku_daily' AND status = 'OK' AND backfill_sequence IS NOT NULL AND SAFE_CAST(JSON_VALUE(backfill_detail_json,'$.exports_reserved') AS INT64) > 0 GROUP BY backfill_plan_id,backfill_sequence",params)
        approved=getattr(self,'verified_pre_source_failures',[])
        excluded={(p['plan_id'],p['unit_sequence']) for p in approved}
        rows=[r for r in rows if (r['plan_id'],r['sequence']) not in excluded]
        reservations=[{'plan_id':r['plan_id'],'sequence':r['sequence'],'exports':r['exports'],
                       'at':timestamp(r['reserved_at']).isoformat()} for r in rows]
        excluded_runs=[p['run_id'] for p in approved]
        for i,run in enumerate(excluded_runs):params['excluded'+str(i)]=('STRING',run)
        exception=" AND NOT COALESCE(status='FAILED' AND ingestion_run_id IN ("+','.join('@excluded'+str(i) for i in range(len(excluded_runs)))+"),FALSE)" if excluded_runs else ''
        unknown=self.select(f"SELECT COUNT(*) AS n FROM `{self.journal}` WHERE started_at >= @since AND entity = 'ads_sku_daily' AND backfill_plan_id IS NULL{exception}",params)
        if len(unknown)!=1:raise BF.B.EvidenceError('ordinary export accounting missing')
        verdict=D.quota_decision(reservations,unknown[0]['n'],now,phase,cap,calibration=calibrated)
        if cooldown and timestamp(cooldown)>now and verdict['status']!='STOPPED':
            return {'status':'WAITING','allowance':0,'eligible_at':cooldown,'basis':'SOURCE_THROTTLE'}
        return verdict

    def all_complete(self,manifest):
        return all(self.state(doc)["complete"] for doc in manifest["plans"])

    def completion_evidence(self,manifest):
        checks=[]
        for doc in manifest['plans']:
            state=self.state(doc)
            if not state['complete']:return None
            if doc['runtime_plan']['entity']=='ads_sku_daily':
                records=self.store.history(manifest['hash'])
                prior=[r['payload'].get('accounting',{}) for r in records if r['kind']=='RECONCILED']
                proven=[a for a in prior if a.get('state_hash')==D.digest(state) and a.get('source_complete') is True and a.get('persisted_reconciled') is True]
                snapshot=max((a['snapshot_date'] for a in proven),default=None)
                checks.append(self.sku_accounting(doc,state,require_complete=True,snapshot=snapshot))
            else:
                proof=BF.verify_coverage(doc,doc['ack_hash'],backend=self)
                # Supplies enumeration includes documented missing-content limits;
                # they are reported explicitly and not upgraded to dated history.
                checks.append({'entity':'supplies','state_hash':D.digest(state),'source_complete':True,'coverage':proof})
        return {'source_complete':True,'persisted_reconciled':True,'scopes':checks}

    def sku_accounting(self,doc,state,require_complete=False,*,linkage=True,snapshot=None):
        p=doc['runtime_plan'];params={'origin':('TIMESTAMP',p['origin']),'pid':('STRING',p['plan_id']),'day':('DATE',p['from'])}
        if p['from']!=p['to']:raise BF.B.EvidenceError('SKU qualification requires frozen one-day cohort')
        units=self.select(f"SELECT DISTINCT backfill_sequence,backfill_detail_json FROM `{self.journal}` WHERE started_at >= @origin AND backfill_plan_id = @pid AND entity = 'ads_sku_daily' AND status = 'OK' AND backfill_sequence IS NOT NULL ORDER BY backfill_sequence",{'origin':params['origin'],'pid':params['pid']})
        by={}
        for u in units:
            detail=BF.source_detail(parse_tenant_json(u['backfill_detail_json'] or '{}'));seq=u['backfill_sequence']
            if seq in by and by[seq]!=detail:raise BF.B.EvidenceError('SKU unit proof conflict')
            by[seq]=detail
        if not by or sorted(by)!=list(range(1,state['sequence']+1)):
            raise BF.B.EvidenceError('SKU source proof gap')
        cohorts=[d for d in by.values() if d.get('action')=='SKU_COHORT']
        if len(cohorts)!=1 or cohorts[0].get('day')!=p['from']:
            raise BF.B.EvidenceError('SKU frozen cohort evidence ambiguous')
        required=cohorts[0]['required_campaigns']
        completed=sum(d['campaigns'] for d in by.values() if d.get('action')=='SKU_BATCH_COMPLETE')
        expected=sum(d['expected_unique_rows'] for d in by.values() if d.get('action')=='SKU_BATCH_COMPLETE')
        pending=state['progress'].get('pending',[])
        if completed+len(pending)!=required or require_complete and (not state['complete'] or completed!=required or pending or state['progress'].get('report')):
            raise BF.B.EvidenceError('SKU cohort incomplete or contradictory')
        raw=f"{self.c['project_id']}.ozon_raw.RAW_OZON_ADS_SKU_DAILY"
        counts=self.select(f'SELECT COUNT(*) AS rows_n,COUNT(DISTINCT TO_JSON_STRING(STRUCT(date,campaign_id,sku))) AS keys_n,COUNT(DISTINCT campaign_id) AS campaigns FROM `{raw}` WHERE date = @day',{'day':params['day']})
        if len(counts)!=1 or counts[0]['rows_n']!=counts[0]['keys_n'] or counts[0]['rows_n']!=expected or counts[0]['campaigns']!=completed:
            raise BF.B.EvidenceError('SKU prefix source/persisted reconciliation failed')
        # Structural identity only. Never print/store identifiers in controller logs.
        snapshot=snapshot or str(self.clock().astimezone(BF.B.MSK).date())
        link=self.select(f"SELECT COUNTIF(r.sku IS NULL OR NOT EXISTS (SELECT 1 FROM `{self.c['project_id']}.ozon_raw.RAW_OZON_CATALOG` c WHERE c.snapshot_date=@snapshot AND c.sku=r.sku)) AS unresolved FROM `{raw}` r WHERE date=@day",{'day':params['day'],'snapshot':('DATE',snapshot)})
        if linkage and (len(link)!=1 or link[0]['unresolved']):raise BF.B.EvidenceError('current SKU/Catalog linkage unproven')
        return {'entity':'ads_sku_daily','snapshot_date':snapshot,'required_campaigns':required,'completed_campaigns':completed,'pending_campaigns':len(pending),'rows':expected,'state_hash':D.digest(state),'state_bytes':len(D.encoded(state).encode()),'source_complete':state['complete'],'persisted_reconciled':True}

    def next_plan(self,manifest,records,quota):
        pending=[p for p in manifest['plans'] if not self.state(p)['complete']]
        if not pending:return None
        # Pending async report may be polled/downloaded with exhausted budget.
        chosen=next((p for p in pending if self.state(p)['progress'].get('report')),pending[0])
        if chosen['runtime_plan']['entity']=='ads_sku_daily':
            day=str(self.clock().astimezone(BF.B.MSK).date())
            certs=[r['payload'] for r in records if r['kind']=='SNAPSHOT_CERT' and r['payload'].get('day')==day]
            if not certs:
                self.sku_accounting(chosen,self.state(chosen),linkage=False) if self.state(chosen)['sequence'] else None
                return self.catalog_dependency(manifest,records,day)
            self.sku_accounting(chosen,self.state(chosen)) if self.state(chosen)['sequence'] else None
        return chosen

    def catalog_dependency(self,manifest,records,day):
        existing=[r['payload']['plan'] for r in records if r['kind']=='DEPENDENCY_PLAN' and r['payload'].get('day')==day]
        if len(existing)>1:raise BF.B.EvidenceError('dated Catalog dependency ambiguous')
        if existing:return existing[0]
        # Derive a fresh bounded snapshot without changing/reusing prior dated plans.
        prior=str(date.fromisoformat(day)-timedelta(days=1))
        origin=datetime.combine(date.fromisoformat(day),datetime.min.time(),BF.B.MSK).astimezone(timezone.utc).isoformat()
        doc=BF.make_plan(self.c['tenant_id'],'catalog',prior,prior,'cloud-cat-'+day,origin,today=date.fromisoformat(day),max_units=3)
        self.store.commit(manifest['hash'],'DEPENDENCY_PLAN',0,{'day':day,'plan':doc},self.clock())
        return doc

    def start(self,doc,before,after):return BF.start(doc,doc['ack_hash'],backend=self,on_prepared=before,on_receipt=after)

    def monitoring(self,root,result):
        """Cloud log projection: scope hashes/counts only, never source payloads.

        Chunk counts here mean frozen QUALIFICATION scopes, not a full-history
        plan. Failed attempts are distinct from failed/incomplete scopes.
        """
        records=self.store.history(root)
        manifests=[r['payload'] for r in records if r['kind']=='MANIFEST']
        if len(manifests)!=1:raise BF.B.EvidenceError('monitoring manifest ambiguous')
        manifest=manifests[0];scopes=[];now=self.clock()
        for doc in manifest['plans']:
            p=doc['runtime_plan'];state=self.state(doc)
            params={'pid':('STRING',p['plan_id']),'origin':('TIMESTAMP',p['origin']),'entity':('STRING',p['entity'])}
            # Failed aggregate runtime rows can omit backfill_plan_id. Attribute
            # them through committed immutable dispatch receipts, not geography
            # or a guessed source account. All run identifiers remain parameters.
            runs=sorted({r['payload']['receipt']['run_id'] for r in records if r['kind']=='DISPATCH_RECEIPT' and r['payload']['plan']['runtime_plan']['plan_id']==p['plan_id']})
            for i,run in enumerate(runs):params['run'+str(i)]=('STRING',run)
            scope='backfill_plan_id=@pid'
            if runs:scope+=' OR ingestion_run_id IN ('+','.join('@run'+str(i) for i in range(len(runs)))+')'
            stats=self.select(f"SELECT MAX(IF(status='OK',completed_at,NULL)) AS last_success,MAX(IF(status='FAILED',completed_at,NULL)) AS last_failure,COUNT(DISTINCT IF(status='FAILED',ingestion_run_id,NULL)) AS failed_attempts FROM `{self.journal}` WHERE started_at>=@origin AND ({scope}) AND entity=@entity",params)
            if len(stats)!=1:raise BF.B.EvidenceError('monitoring checkpoint provenance ambiguous')
            success=timestamp(stats[0]['last_success']) if stats[0]['last_success'] is not None else None
            failure=timestamp(stats[0]['last_failure']) if stats[0]['last_failure'] is not None else None
            if any(t and t>now for t in (success,failure)):raise BF.B.EvidenceError('monitoring checkpoint in future')
            pending=state['progress'].get('pending')
            scopes.append({'plan_id':p['plan_id'],'entity':p['entity'],'from':p['from'],'to':p['to'],
                           'sequence':state['sequence'],'source_complete':state['complete'],
                           'failed':bool(not state['complete'] and failure and (success is None or failure>success)),
                           'failed_attempts':stats[0]['failed_attempts'],
                           'pending_campaigns':len(pending) if isinstance(pending,list) else None,
                           'last_success':success.isoformat() if success else None,
                           'checkpoint_age_seconds':int((now-success).total_seconds()) if success else None,
                           'state_bytes':len(D.encoded(state).encode())})
        incomplete=sum(not s['source_complete'] for s in scopes)
        return {'root_hash':root,'purpose':manifest['purpose'],'chunk_grain':'FROZEN_QUALIFICATION_SCOPE',
                'total_chunks':len(scopes),'completed_chunks':len(scopes)-incomplete,
                'failed_chunks':sum(s['failed'] for s in scopes),
                'waiting_chunks':incomplete if result['status']=='WAITING' else 0,
                'bindings':self.binding_status,'active_runtime_execution':self.active,
                'quota_wait_until':result.get('eligible_at'),'scopes':scopes}

    def recover_receipt(self,intent):
        # A lost Run operation response cannot be invented from an execution
        # name. Preserve the elected intent/lease; operator forensic review is
        # safer than repeating a potentially accepted source execution.
        return None

    def reconcile(self,payload):
        doc=payload['plan'];receipt=payload['receipt']
        operation=self.request('GET',f"{BF.RUN_API}/{receipt['operation']}")
        if not operation.get('done'):return None
        if operation.get('error'):raise BF.B.EvidenceError('canonical source execution failed; preserve evidence')
        result=BF.reconcile(doc,doc['ack_hash'],receipt,backend=self)
        if doc['runtime_plan']['entity']=='catalog':
            state=self.state(doc)
            if state['complete']:
                proof=BF.verify_coverage(doc,doc['ack_hash'],backend=self)
                root=self.c['orchestration']['job']['env']['BACKFILL_ROOT_HASH']
                self.store.commit(root,'SNAPSHOT_CERT',0,{'day':doc['runtime_plan']['observation_date'],'plan_hash':doc['ack_hash'],'coverage':proof},self.clock())
        elif doc['runtime_plan']['entity']=='ads_sku_daily':
            result['accounting']=self.sku_accounting(doc,self.state(doc))
        else:result['coverage_readback']=BF.verify_coverage(doc,doc['ack_hash'],backend=self)
        return result


def bounded_wake(root, backend):
    """At most one reconcile and one new dispatch; no source/wait busy loop."""
    started=backend.clock()
    result=T.Tick(root,backend.store,backend,backend.clock).run()
    if result['status']=='RECONCILED' and (backend.clock()-started).total_seconds()<300:
        # The first Tick committed RECONCILED and released the exact terminal
        # lease. A fresh Tick reconstructs durable state and repeats all gates.
        result=T.Tick(root,backend.store,backend,backend.clock).run()
        result['prior_action']='RECONCILED'
    return result


def main():
    backend=None;root=None
    try:
        backend,root=bootstrap(os.environ)
        result=bounded_wake(root,backend)
        result['progress']=backend.monitoring(root,result)
        print(json.dumps(result,sort_keys=True))
        return 0 if result['status']!='STOPPED' else 2
    except A.TransientReadError:
        print(json.dumps({'status':'WAITING_READ_RETRY','reason':'TRANSIENT_READ_TRANSPORT'}))
        return 1
    except (BF.B.EvidenceError,BF.TT.TableError,ValueError,KeyError,OSError) as error:
        if isinstance(error,BF.B.EvidenceError) and str(error)=='pilot lease held; reconcile terminal execution before continuation':
            print(json.dumps({'status':'WAITING_LEASE','reason':'CANONICAL_LEASE_NOT_YET_RECLAIMABLE'}))
            return 1
        # No exception repr/response/header/env dumps: even a malformed upstream
        # diagnostic might contain identifiers or credential material.
        if backend is not None and root is not None:
            try:
                backend.store.commit(root,'STOPPED',0,{'reason':'CONTROLLER_GATE_OR_EVIDENCE_FAILURE','controller_execution':backend.current_execution},backend.clock())
            except (BF.B.EvidenceError,BF.TT.TableError,ValueError,KeyError,OSError):
                pass # Failure to publish STOP cannot authorize another dispatch.
        print(json.dumps({'status':'STOPPED','reason':'CONTROLLER_GATE_OR_EVIDENCE_FAILURE'}))
        return 2


if __name__=='__main__':raise SystemExit(main())
