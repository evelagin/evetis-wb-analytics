import copy
from datetime import date,datetime,timedelta,timezone
import pytest
from tools.tenancy import current_contract as C

NOW=datetime(2026,10,12,15,30,tzinfo=timezone.utc)
SCOPE='1'*64
def fact(entity,when=NOW):
    i=sorted(C.ENTITIES).index(entity)+1
    return dict(entity=entity,current_scope=SCOPE,observed_at=when.isoformat(),
        source_complete=True,persisted_reconciled=True,natural_keys_verified=True,
        duplicate_natural_keys=0,coverage_gap_days=0,source_unit_gaps=0,
        source_unit_duplicates=0,mismatches=0,
        finance_finality='PROVISIONAL' if entity=='finance_accrual' else 'NOT_APPLICABLE',
        certificate_hash=str(i)*64,source_run_id='bf-02277a37-829d-45a4-90f4-f3ff8f9cb2f'+str(i))
def cycle(n):
    end=NOW-timedelta(minutes=(3-n)*10)
    return dict(version=1,purpose=C.TYPE,current_scope=SCOPE,sequence=n,
        started_at=(end-timedelta(minutes=1)).isoformat(),finished_at=end.isoformat(),
        natural=True,scheduler_job='tenant-backfill-tick',scheduler_proof_hash=str(n)*64,
        controller_execution='projects/mpa-t-client-001/locations/europe-west1/jobs/tenant-backfill-controller/executions/tenant-backfill-controller-'+str(n),
        source_concurrency=1,source_execution_overlap=0,source_dispatches=1,
        receipt_reconciliation_verified=True,facts=[fact(e,end) for e in sorted(C.ENTITIES)])
def ready(cycles,**overrides):
    args=dict(analytics_visible=True,historical_independent=True,systemic_risk=False,fbo_contract_hash='a'*64)
    args.update(overrides);return C.readiness(cycles,SCOPE,NOW,**args)

def test_operational_ready_does_not_claim_historical_or_financial_finality():
    r=ready([cycle(n) for n in range(1,4)])
    assert r['operational_READY'] is True and r['natural_cycles']==3
    assert r['finance_finality']=='PROVISIONAL' and r['historical_completeness']=='INDEPENDENT_NOT_INFERRED'

def test_two_actual_cycles_are_insufficient():assert ready([cycle(1),cycle(2)])['operational_READY'] is False
def test_missing_successful_current_cycle_breaks_consecutive_proof():
    x=cycle(2);x['sequence']=3;y=cycle(3);y['sequence']=4
    assert ready([cycle(1),x,y])['status']=='PENDING_CONSECUTIVE_CYCLES'

@pytest.mark.parametrize('field,value',[('natural',False),('source_dispatches',0),('source_execution_overlap',1),('source_concurrency',2),('receipt_reconciliation_verified',False),('scheduler_job','ozon-daily'),('version',True)])
def test_no_manual_theoretical_or_overlapping_cycle_can_count(field,value):
    x=cycle(3);x[field]=value
    with pytest.raises(ValueError):ready([cycle(1),cycle(2),x])

@pytest.mark.parametrize('field,value',[('source_complete',False),('persisted_reconciled',False),('natural_keys_verified',False),('duplicate_natural_keys',1),('coverage_gap_days',1),('source_unit_gaps',1),('source_unit_duplicates',1),('mismatches',1),('duplicate_natural_keys',False),('current_scope','3'*64)])
def test_unknown_or_nonzero_effect_predicate_blocks_ready(field,value):
    x=cycle(3);x['facts'][0][field]=value
    with pytest.raises(ValueError):ready([cycle(1),cycle(2),x])

def test_finance_finality_is_not_granted_by_fresh_ingestion():
    x=cycle(3);next(f for f in x['facts'] if f['entity']=='finance_accrual')['finance_finality']='FINAL'
    with pytest.raises(ValueError):ready([cycle(1),cycle(2),x])

@pytest.mark.parametrize('entity,age',[('catalog',24*3600+1),('stocks',24*3600+1),('prices',24*3600+1),('fbo_postings',6*3600+1),('ads_sku_daily',24*3600+1),('finance_accrual',24*3600+1)])
def test_freshness_sla_is_observed_not_inferred(entity,age):
    x=cycle(3);next(f for f in x['facts'] if f['entity']==entity)['observed_at']=(NOW-timedelta(seconds=age)).isoformat()
    with pytest.raises(ValueError):ready([cycle(1),cycle(2),x])

def test_same_natural_execution_cannot_be_counted_twice():
    xs=[cycle(n) for n in range(1,4)];xs[2]['controller_execution']=xs[1]['controller_execution']
    with pytest.raises(ValueError):ready(xs)

def test_one_source_run_cannot_be_relabelled_as_multiple_operational_domains():
    x=cycle(3);x['facts'][1]['source_run_id']=x['facts'][0]['source_run_id']
    with pytest.raises(ValueError):ready([cycle(1),cycle(2),x])

def test_gap_free_fbo_contract_is_required_even_with_three_counters():
    assert ready([cycle(n) for n in range(1,4)],fbo_contract_hash=None)['status']=='PENDING_FBO_SOURCE_CONTRACT'

@pytest.mark.parametrize('key', ['analytics_visible','historical_independent'])
def test_independent_analytics_and_background_proof_required(key):
    assert ready([cycle(n) for n in range(1,4)],**{key:False})['operational_READY'] is False

def test_known_systemic_risk_blocks_both_paths():
    assert ready([],systemic_risk=True)['status']=='BLOCKED_SYSTEMIC'

def test_snapshot_day_is_actual_and_dated_windows_do_not_include_unfinished_day():
    day=date(2026,10,12)
    assert C.source_window('catalog',day)==dict(observation_date='2026-10-12',from_date='2026-10-11',to_date='2026-10-11')
    assert C.source_window('finance_accrual',day)==dict(from_date='2026-09-12',to_date='2026-10-11')
    assert C.source_window('fbo_postings',day)==dict(from_date='2026-10-05',to_date='2026-10-11')

@pytest.mark.parametrize('v',['2026-10-12T15:00:00','2026-10-12T18:00:00+03:00','invalid'])
def test_observer_evidence_requires_normalized_aware_utc(v):
    with pytest.raises(ValueError):C.at(v)
