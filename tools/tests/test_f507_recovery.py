"""Exact incident consumption and real four-write fault boundaries, offline."""
from copy import deepcopy
from datetime import timedelta
import pytest
from tools.tenancy import f507_recovery as R, durable_plan as D, tenant_backfill as BF
from tools.tenancy import full_controller as H, current_controller as C, controller_cadence as CC
from tools.tenancy.f507_recovery_image_check import fixture, observed, ACK, NOW


@pytest.fixture(scope='module', autouse=True)
def _historical_runtime():
    """Frozen production incident: replay it with the runtime it actually ran with."""
    from tools.tenancy.runtime_history_fixture import historical_runtime
    with historical_runtime():
        yield


@pytest.fixture(scope='module')
def certificate():
    with fixture() as (b, _): return observed(b)


def reseal(p): return R.sealed({k:v for k,v in p.items() if k!='hash'})


@pytest.mark.parametrize('key,value', [('tenant','other'),('project','other'),('root','0'*64),
    ('manifest_hash','0'*64),('stop_hash','0'*64),('execution','foreign'),('old_source','b'*40),
    ('old_image','foreign'),('control_generation',506),('control_generation',True),
    ('claim_hash','0'*64),('closure_hash','0'*64),('runtime_image','foreign'),
    ('runtime_implementation_hash','0'*64),('current_policy_hash','0'*64),
    ('source_budget',1),('source_budget',False),('historical_cause','PAUSE_PROVEN'),
    ('new_source',R.OLD),('new_image','mutable:tag'),('owner_ack_sha256',R.IMPLEMENTATION_ACK),
    ('verified_at','2026-10-14T00:00:00Z')])
def test_closed_incident_pins_reject_resealed_expansion(certificate,key,value):
    from pipelines.ozon.runtime import full_resume
    p=deepcopy(certificate);p[key]=value
    with pytest.raises((ValueError,BF.B.EvidenceError)): R.header(reseal(p),full_resume.manifest())


@pytest.mark.parametrize('group', ['predicates','production_gates'])
@pytest.mark.parametrize('value', [None,False,'UNKNOWN',1])
def test_each_mandatory_predicate_is_strict_true(certificate,group,value):
    from pipelines.ozon.runtime import full_resume
    for name in certificate[group]:
        p=deepcopy(certificate);p[group][name]=value
        with pytest.raises(BF.B.EvidenceError,match='mandatory'):R.header(reseal(p),full_resume.manifest())


@pytest.mark.parametrize('fault',['extra_field','missing_field','bad_hash','archive','archive_hash','prefix','prefix_order'])
def test_closed_schema_archive_prefix_tampering(certificate,fault):
    from pipelines.ozon.runtime import full_resume
    p=deepcopy(certificate)
    if fault=='extra_field':p['extra']='x'
    elif fault=='missing_field':del p['effect_hash']
    elif fault=='bad_hash':p['hash']='0'*64
    elif fault=='archive':p['archive']['actor']['template']['serviceAccount']='foreign'
    elif fault=='archive_hash':p['archive_hash']='0'*64
    elif fault=='prefix':p['protected_root_hashes'].pop()
    else:p['protected_root_hashes'].reverse()
    if fault!='bad_hash':p=reseal(p)
    with pytest.raises((ValueError,BF.B.EvidenceError)):R.header(p,full_resume.manifest())


@pytest.mark.parametrize('scope',['missing','natural','development','foreign_stop','source_budget'])
def test_no_production_authority_from_phase_A_or_natural_wake(certificate,scope):
    with fixture() as (b,f):
        if scope=='missing':del b.f507_production_authority
        elif scope=='natural':b.current_execution='natural-controller'
        elif scope=='development':b.f507_production_authority['owner_ack_sha256']=R.IMPLEMENTATION_ACK
        elif scope=='foreign_stop':b.f507_production_authority['stop_hash']='0'*64
        else:b.f507_production_authority['source_budget']=1
        with pytest.raises(BF.B.EvidenceError):R.publish(b,certificate,b.manifest)
        assert not b.tables.writes and not f['inserts']


def test_real_transport_happy_path_and_size_budget():
    from tools.tenancy.f507_recovery_image_check import check
    assert set(check().values())=={'PASS'}


@pytest.mark.parametrize('stage',['authority','certificate','sidecar','commit'])
def test_lost_ack_after_each_actual_write_never_reinserts(stage):
    with fixture() as (b,f):
        p=observed(b);create=b.tables.create_marker;write=b.store.write_record;raised=[]
        def lost_create(ds,name,*args):
            result=create(ds,name,*args)
            wanted=R.auth(p) if stage=='authority' else R.cert(p) if stage=='certificate' else 'BFR_'+D.digest(R.record(p))
            if stage!='sidecar' and name==wanted and not raised:
                raised.append(True);raise OSError('accepted metadata ACK lost')
            return result
        def lost_write(row):
            write(row)
            if stage=='sidecar' and not raised:raised.append(True);raise OSError('accepted sidecar ACK lost')
        b.tables.create_marker=lost_create;b.store.write_record=lost_write
        with pytest.raises(OSError):R.publish(b,p,b.manifest)
        b.tables.create_marker=create;b.store.write_record=write
        if stage!='commit':assert R.load(b,b.manifest,b.store.history(R.ROOT))==[]
        assert R.publish(b,p,b.manifest)==p
        assert len(b.tables.writes)==3 and len(f['inserts'])==1
        before=deepcopy((f['rows'],b.tables.values));assert R.publish(b,p,b.manifest)==p
        assert before==(f['rows'],b.tables.values)


@pytest.mark.parametrize('fault',['actor404','actor_template','active_source','scheduler_enabled',
    'runtime_drift','new_source_execution','journal_successor','source_CP_successor',
    'fence_changed','new_election','highwater269','missing_source_lease',
    'RAW_mismatch','RAW_duplicate','source_CP_bytes','current_state','pending_report',
    'new_unknown_STOP','security_fail','latest_queued','latest_absent','count_missing',
    'ordinary_target_drift','completed_historical_report'])
def test_fresh_initial_predicates_abort_before_writes(fault,monkeypatch):
    # Undo nested pytest patches before the packaged fixture restores its originals.
    with fixture() as (b,f), monkeypatch.context() as monkeypatch:
        ds=b.c['datasets']['tenant_locks'];orig_request=b.request;orig_select=b.select
        if fault=='actor404':
            b.request=lambda method,url,body=None: (_ for _ in ()).throw(BF.TT.TableError('HTTP 404')) if url.endswith(f['base']['actor']['name']) else orig_request(method,url,body)
        elif fault=='actor_template':
            def changed(method,url,body=None):
                x=orig_request(method,url,body)
                if url.endswith(f['base']['actor']['name']):x['template']['serviceAccount']='foreign'
                return x
            b.request=changed
        elif fault=='active_source':next(v for k,v in f['resources'].items() if 'ozon-runtime' in k)['runningCount']=1
        elif fault=='scheduler_enabled':f['schedules']['jobs'][0]['state']='ENABLED'
        elif fault=='runtime_drift':next(v for k,v in f['resources'].items() if 'ozon-runtime' in k)['template']['template']['containers'][0]['image']='foreign'
        elif fault=='new_source_execution':next(v for k,v in f['inventories'].items() if 'ozon-runtime' in k)[-1]['createTime']=NOW.isoformat()
        elif fault in {'journal_successor','source_CP_successor'}:
            predicate='OZON_INGESTION_RUNS' if fault=='journal_successor' else 'entity!=\'orchestration\''
            b.select=lambda sql,params:[dict(n=1)] if 'COUNT(*) AS n' in sql and predicate in sql else orig_select(sql,params)
        elif fault=='fence_changed':
            name=next(n for n in f['base']['fence_hashes'] if n.startswith('BFQ_'));b.tables.values[(ds,name)]=({},'changed')
        elif fault=='new_election':
            name='BFQ_'+'f'*64+'_0000000001_DISPATCH_INTENT';b.tables.values[(ds,name)]=({},'orphan');b.tables.created[(ds,name)]=NOW
        elif fault=='highwater269':b.tables.values[(ds,'L_50c5d90dbf43ac9c_0269')]=({},'orphan')
        elif fault=='missing_source_lease':del b.tables.values[(ds,'L_50c5d90dbf43ac9c_0268')]
        elif fault in {'RAW_mismatch','RAW_duplicate'}:
            def counts(sql,params):
                rows=orig_select(sql,params)
                if 'BIT_XOR' in sql and 'RAW_OZON_CATALOG`' in sql:
                    rows[0]['rows_n']+=1
                    if fault=='RAW_mismatch':rows[0]['keys_n']+=1
                return rows
            b.select=counts
        elif fault=='source_CP_bytes':
            next(v for v in f['fresh']['checkpoints'].values() if v)[0]['evidence_json']+=' '
        elif fault in {'current_state','pending_report'}:
            state=next(iter(f['states'].values()))
            if fault=='current_state':state['rows']+=1
            else:state['progress']['report']={'phase':'INTENT'}
        elif fault=='new_unknown_STOP':
            b.store.commit(R.ROOT,'STOPPED',0,dict(reason='unrelated'),NOW)
        elif fault=='latest_queued':next(iter(f['resources'].values()))['latestCreatedExecution']['completionStatus']='EXECUTION_PENDING'
        elif fault=='latest_absent':next(iter(f['resources'].values()))['latestCreatedExecution']['name']='missing'
        elif fault=='count_missing':next(iter(f['resources'].values())).pop('executionCount')
        elif fault=='ordinary_target_drift':next(z for z in f['schedules']['jobs'] if not z['name'].endswith('tenant-backfill-tick'))['httpTarget']['uri']='https://foreign.invalid'
        elif fault=='completed_historical_report':
            doc=next(r['payload']['plan'] for r in f['roots'][R.ROOT] if r['kind']=='CHUNK_PLAN' and r['payload']['plan']['runtime_plan']['entity'].startswith('ads_'))
            original=b.state
            def report(d):
                st=original(d)
                if d==doc:st['progress']['report']={'phase':'INTENT'}
                return st
            b.state=report
        else:monkeypatch.setattr(H,'verify_authority',lambda *a:(_ for _ in ()).throw(BF.B.EvidenceError('security unknown')))
        before=len(b.tables.writes);before_inserts=len(f['inserts'])
        with pytest.raises((BF.B.EvidenceError,BF.TT.TableError,ValueError)):
            observed(b)
        assert len(b.tables.writes)==before and len(f['inserts'])==before_inserts


@pytest.mark.parametrize('fault',['claim','closure','reference','protected_root','protected_current','archive','qualification'])
def test_retained_immutable_evidence_cannot_be_replaced(fault):
    with fixture() as (b,f):
        p=observed(b);R.publish(b,p,b.manifest);ds=b.c['datasets']['tenant_locks']
        if fault in {'claim','closure'}:
            name=CC.names(R.ROOT,1129)[fault=='closure'];b.tables.values[(ds,name)]=({},'corrupt')
        elif fault=='reference':
            path=next(iter(p['archive']['reference_hashes']));dataset,name=path.split('/',1);del b.tables.values[(dataset,name)]
        elif fault in {'protected_root','protected_current'}:
            h=p['protected_root_hashes'][0] if fault=='protected_root' else next(iter(p['archive']['protected_current'].values()))[0]
            del b.tables.values[(ds,'BFR_'+h)]
        elif fault=='archive':p['archive']['actor']['completionTime']='2026-10-16T00:00:00Z';p=reseal(p)
        else:p['new_implementation_hash']='0'*64;p=reseal(p)
        with pytest.raises((BF.B.EvidenceError,BF.TT.TableError,ValueError)):R.validate(p,b,b.manifest,b.store.history(R.ROOT))


def test_canonical_acceptance_survives_future_progress_and_provider_history_expiry():
    with fixture() as (b,f):
        p=observed(b);R.publish(b,p,b.manifest);ds=b.c['datasets']['tenant_locks']
        b.request=lambda *a,**k:(_ for _ in ()).throw(BF.TT.TableError('HTTP 404 expired actor'))
        b.state=lambda *a:(_ for _ in ()).throw(AssertionError('latest state must not be frozen forever'))
        del b.tables.values[(ds,'L_50c5d90dbf43ac9c_0268')]
        b.tables.values[(ds,'L_50c5d90dbf43ac9c_0269')]=({'owner':'next-natural'},'later')
        b.clock=lambda:NOW+timedelta(days=14)
        b.store.commit(R.ROOT,'WAITING',0,dict(reason='normal later progress'),b.clock())
        root=next(iter(p['archive']['protected_current']))
        b.store.commit(root,'CURRENT_PARTIAL',99,dict(reason='normal later progress'),b.clock())
        assert R.load(b,b.manifest,b.store.history(R.ROOT))==[p]
        assert R.publish(b,p,b.manifest)==p


def test_full_root_consumer_approves_only_f507_and_never_another_STOP(monkeypatch):
    from tools.tenancy import full_leaf_recovery as FL,full_cooldown_recovery as FC,full_policy_bootstrap_recovery as PB
    from tools.tenancy import full_pretransport_recovery as PT,full_first_receipt_recovery as FR,full_controller_closure_recovery as CR
    from tools.tenancy import full_fbo_zero_effect_recovery as ZE,full_fbo_diagnostic_closure as LQ,full_pre_authority_gate_recovery as PAG,current_recon_recovery as CUR
    # Undo nested pytest patches before the packaged fixture restores its originals.
    with fixture() as (b,f), monkeypatch.context() as monkeypatch:
        p=observed(b);R.publish(b,p,b.manifest);b.full_tables=b.tables
        monkeypatch.setattr(CC,'acquire',lambda *a:None);monkeypatch.setattr(CC,'enabled',lambda *a:False)
        for module in (FC,PB,PT,FR,CR,ZE,LQ,PAG,CUR):monkeypatch.setattr(module,'load',lambda *a:[])
        monkeypatch.setattr(FL,'load',lambda *a:deepcopy(f['prior']))
        monkeypatch.setattr(H,'repair_t5_completions',lambda *a:None)
        monkeypatch.setattr(C,'wake',lambda *a:dict(status='WAITING',source_dispatches=0))
        result,_=H.wake(b,R.ROOT);assert result==dict(status='WAITING',source_dispatches=0)
        b.store.commit(R.ROOT,'STOPPED',0,dict(reason='another unsupported exact incident'),NOW)
        result,_=H.wake(b,R.ROOT);assert result==dict(status='STOPPED',source_dispatches=0)
        assert R.load(b,b.manifest,b.store.history(R.ROOT))==[p]


def test_slow_observation_or_transport_never_turns_stale_facts_into_fresh(monkeypatch):
    # Undo nested pytest patches before the packaged fixture restores its originals.
    with fixture() as (b,f), monkeypatch.context() as monkeypatch:
        original=R.fresh_boundary; clock=[NOW]; b.clock=lambda:clock[0]
        def slow(*args):
            result=original(*args);clock[0]+=timedelta(minutes=16);return result
        monkeypatch.setattr(R,'fresh_boundary',slow)
        with pytest.raises(BF.B.EvidenceError,match='15min'):observed(b)
        assert not b.tables.writes and not f['inserts']


@pytest.mark.parametrize('after_first_write',[False,True])
def test_freshness_rechecked_immediately_before_each_write(monkeypatch,after_first_write):
    # Undo nested pytest patches before the packaged fixture restores its originals.
    with fixture() as (b,f), monkeypatch.context() as monkeypatch:
        p=observed(b);original_observe=R.observe;original_create=b.tables.create_marker;clock=[NOW];b.clock=lambda:clock[0]
        def slow_observe(*args,**kwargs):
            value=original_observe(*args,**kwargs)
            if not after_first_write:clock[0]+=timedelta(minutes=16)
            return value
        def slow_create(*args):
            value=original_create(*args);clock[0]+=timedelta(minutes=16);return value
        monkeypatch.setattr(R,'observe',slow_observe);b.tables.create_marker=slow_create
        with pytest.raises(BF.B.EvidenceError,match='expired'):R.publish(b,p,b.manifest)
        assert len(b.tables.writes)==int(after_first_write) and not f['inserts']
