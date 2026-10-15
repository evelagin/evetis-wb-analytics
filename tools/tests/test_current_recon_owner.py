"""Offline Phase B procedure/handoff preparation; no production authority."""
from copy import deepcopy
from datetime import datetime, timezone, date
from types import SimpleNamespace
import json
import pytest
from tools.tenancy import current_release_handoff as A, current_recon_recovery as R
from tools.tenancy import current_controller as C, current_policy as P, durable_plan as D
from tools.tenancy import tenant_backfill as BF, orchestration_contract as O, cloud_controller as CLOUD
from tools.tenancy import campaign_persistence_image_check as CHECK
from tools.tenancy.validation import parse_tenant_json as parse

from tools.tenancy.current_recon_image_check import setup, full_fixture, observed, NOW, OLD, NEW, ACK

def test_handoff_preserves_byte_exact_policy_and_every_current_plan_namespace():
    b, policy, handoff, release = setup(); before = deepcopy((b.c, policy, b.tables.values))
    accepted = C.policy(b)
    assert accepted == policy and D.encoded(accepted) == D.encoded(policy)
    for entity in P.ORDER:
        if entity == 'fbo_postings': continue
        doc = P.document(policy, date(2026, 10, 14), entity)
        assert P.document(accepted, date(2026, 10, 14), entity) == doc
        assert P.shard(accepted, date(2026, 10, 14), doc) == P.shard(policy, date(2026, 10, 14), doc)
    assert before == (b.c, policy, b.tables.values) and not b.tables.writes


@pytest.mark.parametrize('key,value', [('source_budget', 1), ('source_budget', False),
    ('policy_hash', '5' * 64), ('new_implementation_hash', '6' * 64), ('new_image', 'bad'),
    ('old_source', NEW), ('runtime_image', 'bad'), ('project', 'foreign'),
    ('created_at', '2026-10-15T00:00:00Z')])
def test_handoff_unknown_or_expanded_ownership_fails_closed(key, value):
    b, _, p, release = setup(); p.pop('hash'); p[key] = value; p['hash'] = D.digest(p)
    with pytest.raises((BF.B.EvidenceError, ValueError, FileNotFoundError)):
        A.resolve(b, NEW, A.value(p), release)
    assert not b.tables.writes


def test_handoff_rejects_parallel_new_policy_and_unauthorized_publisher():
    b, policy, p, release = setup()
    b.tables.values[(b.c['datasets']['ref'], P.name(NEW))] = ({}, D.encoded(policy))
    with pytest.raises(BF.B.EvidenceError, match='fork'): A.resolve(b, NEW, A.value(p), release)
    del b.tables.values[(b.c['datasets']['ref'], P.name(NEW))]
    b.phase_b_ack_sha256 = None
    with pytest.raises(BF.B.EvidenceError, match='Phase B'): A.publish(b, p, release)
    assert not b.tables.writes


def test_partial_prospective_RECON_has_no_completion_and_no_checkpoint_write():
    doc, state, units = CHECK.fixture(); unit = parse(units[0]['backfill_detail_json'])
    result = R.reconciliation(doc, state, unit, {'rows_n': 0, 'keys_n': 0})
    assert result == dict(checkpoint='RUNNING', sequence=1, rows_observed=0, orders=0, supplies=0,
        bundles=0, coverage='UNPROVEN_PENDING_DQ', lifecycle_changed=False, scheduler_changed=False,
        persistence=CHECK.observe(doc, state, units), source_complete=False)
    assert result['persistence']['full_history'] == result['persistence']['economic_finality'] == 'UNPROVEN'


@pytest.mark.parametrize('fault', ['rows', 'complete', 'sequence', 'POST', 'report', 'RAW'])
def test_no_unsafe_prefix_can_construct_reconciliation(fault):
    doc, state, units = CHECK.fixture(); unit = parse(units[0]['backfill_detail_json']); counts = {'rows_n': 0, 'keys_n': 0}
    if fault == 'rows': state['rows'] = 1
    elif fault == 'complete': state['complete'] = state['progress']['done'] = True
    elif fault == 'sequence': state['sequence'] = 2
    elif fault == 'POST': unit['source_diagnostic']['method'] = 'POST'
    elif fault == 'report': state['progress']['report'] = {'phase': 'INTENT'}
    else: counts = {'rows_n': 1, 'keys_n': 1}
    with pytest.raises(BF.B.EvidenceError): R.reconciliation(doc, state, unit, counts)


def test_phase_A_cannot_publish_or_observe_a_phase_B_true_certificate():
    b, _, _, _ = setup(); b.phase_b_ack_sha256 = None
    with pytest.raises(BF.B.EvidenceError, match='Phase B'):
        R.publish(b, {'owner_ack_sha256': ACK}, b.manifest)
    with pytest.raises(BF.B.EvidenceError, match='Phase B'):
        R.observe(b, b.manifest, '1' * 64, '2026-10-14', owner_ack_sha256=ACK,
                  qualification_hash='2' * 64, security_hash='3' * 64, source_probe_hash='4' * 64)
    assert not b.tables.writes and not b.store.writes


def test_compact_owner_reference_retains_large_certificate_content():
    p = dict(root='1' * 64, stop_hash='2' * 64, hash='3' * 64)
    marker = R.ref(p)
    assert len(marker[1].encode()) < 1024 and parse(marker[1])['certificate_hash'] == p['hash']


def test_real_durable_end_to_end_never_completes_or_rewrites_source_evidence():
    from tools.tenancy.current_recon_image_check import check
    assert check()['real_durable_append'] == 'PASS'


@pytest.mark.parametrize('stage', ['RECONCILED', 'owner_marker', R.KIND])
def test_lost_ack_at_every_publication_stage_resumes_identical_only(stage):
    with full_fixture() as (b,f):
        p=observed(b,f); oldcommit=b.store.commit; oldcreate=b.tables.create_marker; raised=[]
        def commit(root,kind,seq,value,now):
            result=oldcommit(root,kind,seq,value,now)
            if kind==stage and not raised: raised.append(True);raise OSError('lost accepted append response')
            return result
        def create(*args):
            result=oldcreate(*args)
            if stage=='owner_marker' and args[1]==R.auth(p) and not raised: raised.append(True);raise OSError('lost owner CAS response')
            return result
        b.store.commit=commit;b.tables.create_marker=create
        with pytest.raises(OSError):R.publish(b,p,b.manifest)
        b.store.commit=oldcommit;b.tables.create_marker=oldcreate
        assert R.publish(b,p,b.manifest)==p
        before=deepcopy((f['rows'],b.tables.values));assert R.publish(b,p,b.manifest)==p
        assert before==(f['rows'],b.tables.values)
        assert sum(parse(row['evidence_json'])['kind']=='RECONCILED' for row in f['rows'])==1
        assert sum(parse(row['evidence_json'])['kind']==R.KIND for row in f['rows'])==1


@pytest.mark.parametrize('fault', ['runtime_image','runtime_caps','control_SA','ordinary_missing','dedicated_missing',
    'dedicated_body','dedicated_scope','dedicated_active','active_source','successor_runtime',
    'journal_successor','RAW_rows','POST','receipt2','source_FAILED','runtime_env_duplicate',
    'claim506','claim_unclosed','lease_owner','lease_expiration','checkpoint_spacing',
    'orphan_current_intent','uncommitted_current_election','conflicting_RECON','another_STOP'])
def test_mandatory_fresh_predicates_reject_without_recovery_writes(fault):
    from tools.tenancy import controller_cadence as CC
    from tools.tenancy.current_recon_image_check import runtime_job
    with full_fixture() as (b,f):
        base,jobs=BF.resources(b.c); first=next(iter(jobs)); ds=b.c['datasets']['tenant_locks']
        if fault=='runtime_image':f['resources'][base+'/jobs/'+first]['template']['template']['containers'][0]['image']='foreign'
        elif fault=='runtime_caps':f['resources'][base+'/jobs/'+first]['template']['template']['containers'][0]['env'][0]['value']='foreign'
        elif fault=='control_SA':f['resources'][base+'/jobs/tenant-control']['template']['template']['serviceAccount']='foreign'
        elif fault=='ordinary_missing':f['schedules'][0]['name']=base+'/jobs/foreign'
        elif fault=='dedicated_missing':f['schedules'][-1]['name']=base+'/jobs/foreign'
        elif fault=='dedicated_body':f['schedules'][-1]['httpTarget']['body']='eyJvdmVycmlkZXMiOnt9fQ=='
        elif fault=='dedicated_scope':f['schedules'][-1]['httpTarget']['oauthToken']['scope']='foreign'
        elif fault=='dedicated_active':f['schedules'][-1]['state']='ENABLED'
        elif fault=='active_source':f['source'].pop('completionTime')
        elif fault=='successor_runtime':f['source']['createTime']='2026-10-14T06:35:00Z'
        elif fault=='journal_successor':
            old=b.select;b.select=lambda sql,params:[dict(n=1)] if 'COUNT(*) AS n' in sql else old(sql,params)
        elif fault=='RAW_rows':
            old=b.select;b.select=lambda sql,params:[dict(rows_n=1,keys_n=1)] if 'RAW_OZON_ADS_CAMPAIGNS' in sql else old(sql,params)
        elif fault=='POST':
            unit=parse(f['unit']['backfill_detail_json']);unit['source_diagnostic']['method']='POST';f['unit']['backfill_detail_json']=json.dumps(unit)
        elif fault=='receipt2':b.store.commit(P.shard(f['policy'],date(2026,10,14),f['doc']),'DISPATCH_RECEIPT',2,dict(plan=f['doc'],receipt={}),NOW)
        elif fault=='source_FAILED':f['source']['failedCount']=1;f['source']['succeededCount']=0
        elif fault=='runtime_env_duplicate':f['source']['template']['containers'][0]['env'].append(deepcopy(f['source']['template']['containers'][0]['env'][0]))
        elif fault in {'claim506','claim_unclosed'}:
            cn,xn=CC.names(b.manifest['hash'],517)
            if fault=='claim_unclosed':del b.tables.values[(ds,xn)]
            else:
                claim=parse(b.tables.values[(ds,cn)][1]);claim.pop('hash');claim['generation']=506;claim=CC.sealed(claim)
                close=parse(b.tables.values[(ds,xn)][1]);close.pop('hash');close['claim_hash']=claim['hash'];close=CC.sealed(close)
                del b.tables.values[(ds,cn)];del b.tables.values[(ds,xn)]
                cn,xn=CC.names(b.manifest['hash'],506);b.tables.values[(ds,cn)]=CC.claim_value(claim);b.tables.values[(ds,xn)]=({'kind':'controller_close','root':b.manifest['hash'][:16]},D.encoded(close))
        elif fault=='lease_owner':b.tables.values[(ds,f['lease_name'])][0]['owner']='foreign'
        elif fault=='lease_expiration':
            old=b.request;b.request=lambda method,url:dict(expirationTime='1') if url.startswith(BF.TT.BQ) else old(method,url)
        elif fault=='checkpoint_spacing':
            # A changed original proof content blocks. Byte formatting alone is
            # accepted once observed and then protected by the certificate.
            p=parse(f['cps'][1]['evidence_json']);p['proof']['state']['rows']=1;f['cps'][1]['evidence_json']=json.dumps(p)
        elif fault in {'orphan_current_intent','uncommitted_current_election'}:
            doc=P.document(f['policy'],date(2026,10,14),'prices');shard=P.shard(f['policy'],date(2026,10,14),doc)
            b.store.commit(P.cycle_root(f['policy'],date(2026,10,14)),'CURRENT_PLAN',0,dict(day='2026-10-14',policy_hash=f['policy']['hash'],plan=doc,shard=shard),NOW)
            if fault=='orphan_current_intent':b.store.commit(shard,'DISPATCH_INTENT',1,dict(plan=doc,preparation={}),NOW)
            else:b.tables.values[(ds,f'BFQ_{shard}_0000000001_DISPATCH_INTENT')]=({'root':shard[:16],'kind':'dispatch_intent'},D.encoded(dict(record_hash='7'*64)))
        elif fault=='conflicting_RECON':b.store.commit(P.shard(f['policy'],date(2026,10,14),f['doc']),'RECONCILED',1,dict(source_complete=True),NOW)
        elif fault=='another_STOP':b.store.commit(b.manifest['hash'],'STOPPED',1,dict(reason='foreign'),NOW)
        before=deepcopy((f['rows'],b.tables.values))
        with pytest.raises((BF.B.EvidenceError,ValueError,KeyError)):observed(b,f)
        assert before==(f['rows'],b.tables.values)


def test_original_lease_cannot_disappear_early_or_change_after_expiry():
    from datetime import timedelta
    with full_fixture() as (b,f):
        p=observed(b,f);R.publish(b,p,b.manifest);ds=b.c['datasets']['tenant_locks'];saved=b.tables.values.pop((ds,f['lease_name']))
        with pytest.raises(BF.B.EvidenceError,match='expiration'):R.load(b,b.manifest,b.store.history(b.manifest['hash']))
        b.clock=lambda:NOW+timedelta(days=8)
        assert R.load(b,b.manifest,b.store.history(b.manifest['hash']))==[p]
        changed=deepcopy(saved);changed[0]['owner']='foreign';b.tables.values[(ds,f['lease_name'])]=changed
        with pytest.raises(BF.B.EvidenceError,match='expiration'):R.load(b,b.manifest,b.store.history(b.manifest['hash']))


def test_retained_loader_allows_later_source_progress_but_rejects_changed_original_bytes():
    with full_fixture() as (b,f):
        p=observed(b,f);R.publish(b,p,b.manifest)
        b.state=lambda doc:dict(sequence=2,complete=True);b.store.commit(p['shard'],'DISPATCH_INTENT',2,dict(later=True),NOW)
        assert R.load(b,b.manifest,b.store.history(b.manifest['hash']))==[p]
        f['cps'][1]['evidence_json']=json.dumps(parse(f['cps'][1]['evidence_json']),separators=(',',':'))
        with pytest.raises(BF.B.EvidenceError,match='bytes'):R.load(b,b.manifest,b.store.history(b.manifest['hash']))


def test_reconciled_rejected_prefix_waits_for_quota_then_only_new_attempt_not_replay():
    with full_fixture() as (b,f):
        p=observed(b,f);R.publish(b,p,b.manifest);day=date(2026,10,14)
        b.current_root=P.cycle_root(f['policy'],day);b.current_execution='offline-natural-controller'
        b.preflight=lambda *a:None;b.active_runtime_execution=lambda:False
        b.quota=lambda *a:dict(status='WAITING',allowance=0,eligible_at='2026-10-14T20:30:00Z')
        calls=[]
        def start(doc,before,after):
            prep=dict(run_id='bf-00000000-0000-0000-0000-000000000002',lease_generation=231,ack_hash=doc['ack_hash'])
            before(prep);calls.append(deepcopy(prep));after(dict(prep,operation='offline-new-operation'))
        b.start=start;item=dict(plan=f['doc'],shard=p['shard'])
        assert C.source_tick(b,f['policy'],day,item)['source_dispatches']==0 and not calls
        b.quota=lambda *a:dict(status='ELIGIBLE',allowance=0)
        result=C.source_tick(b,f['policy'],day,item)
        assert result['source_dispatches']==1 and len(calls)==1
        assert calls[0]['run_id']!=p['run_id'] and calls[0]['lease_generation']>p['source_generation']
        hs=b.store.history(p['shard']);assert sorted(r['sequence'] for r in hs if r['kind']=='DISPATCH_INTENT')==[1,2]
        assert [r for r in hs if r['kind']=='RECONCILED']==[R.recon_record(p)]
        assert not any(parse(row['evidence_json'])['kind'] in {'CURRENT_CERT','COMPLETE','CURRENT_CYCLE_COMPLETE'} for row in f['rows'])
        b.reconcile=lambda payload:None
        assert C.source_tick(b,f['policy'],day,item)['source_dispatches']==0
        assert len(calls)==1  # Unreconciled new receipt cannot become a second dispatch.
