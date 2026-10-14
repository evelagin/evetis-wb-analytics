"""Operational freshness is independent of immutable historical completion.

Pure decisions only: no source transport, publication, READY lifecycle change,
or financial-finality inference. Cloud adapters must supply canonical natural
Scheduler/receipt/reconciliation/DQ facts; existence of this module is not GO.
"""
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import re

MSK=ZoneInfo('Europe/Moscow')
TYPE='CURRENT_DATA_V1'
ENTITIES=frozenset({'catalog','fbo_postings','stocks','prices','finance_accrual',
                    'ads_campaigns','ads_expense_daily','ads_sku_daily'})
SNAPSHOTS=frozenset({'catalog','stocks','prices','ads_campaigns'})
SLA_SECONDS={e:24*3600 for e in ENTITIES}
FACT_FIELDS=frozenset({'entity','current_scope','observed_at','source_complete',
    'persisted_reconciled','natural_keys_verified','duplicate_natural_keys',
    'coverage_gap_days','source_unit_gaps','source_unit_duplicates','mismatches',
    'finance_finality','certificate_hash','source_run_id'})
CYCLE_FIELDS=frozenset({'version','purpose','current_scope','sequence','moscow_day','started_at',
    'finished_at','natural','scheduler_job','scheduler_proof_hash',
    'controller_execution','source_concurrency','source_execution_overlap',
    'source_dispatches','receipt_reconciliation_verified','facts'})

def fail(reason):raise ValueError('operational current contract: '+reason)

def at(value):
    try:
        parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
        if parsed.utcoffset()!=timedelta(0):raise ValueError
        return parsed
    except (AttributeError,TypeError,ValueError):fail('exact aware UTC timestamp required')

def digest(value):
    if not isinstance(value,str) or not re.fullmatch('[0-9a-f]{64}',value):fail('immutable evidence reference required')
    return value

def source_window(entity, day):
    """Current rolling refresh never changes/reset the historical root dates.

    Snapshots describe actual execution day. Dated requests use finished days;
    the open current day cannot be silently labelled a complete business day.
    Finance and Performance serve the latest completed day first. Historical
    reconciliation and longer refresh windows retain their separate authority.
    """
    if entity not in ENTITIES or type(day) is not date:fail('known entity/Moscow day required')
    if entity in SNAPSHOTS:return dict(observation_date=day.isoformat(),from_date=(day-timedelta(days=1)).isoformat(),to_date=(day-timedelta(days=1)).isoformat())
    count={'fbo_postings':7,'finance_accrual':1,'ads_expense_daily':1,'ads_sku_daily':1}[entity]
    return dict(from_date=(day-timedelta(days=count)).isoformat(),to_date=(day-timedelta(days=1)).isoformat())

def check_fact(f, scope, now):
    if not isinstance(f,dict) or set(f)!=FACT_FIELDS or f['entity'] not in ENTITIES or f['current_scope']!=scope:fail('unknown/current fact scope differs')
    for key in ('source_complete','persisted_reconciled','natural_keys_verified'):
        if f[key] is not True:fail('source/accepted persistence/DQ proof incomplete')
    for key in ('duplicate_natural_keys','coverage_gap_days','source_unit_gaps','source_unit_duplicates','mismatches'):
        if type(f[key]) is not int or f[key]!=0:fail('unknown/nonzero duplicate/gap/mismatch')
    digest(f['certificate_hash'])
    if not isinstance(f['source_run_id'],str) or not re.fullmatch(r'bf-[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}',f['source_run_id']):fail('canonical source provenance required')
    if not 0<=(now-at(f['observed_at'])).total_seconds()<=SLA_SECONDS[f['entity']]:fail('freshness SLA unproven')
    if f['finance_finality']!=('PROVISIONAL' if f['entity']=='finance_accrual' else 'NOT_APPLICABLE'):fail('operational evidence cannot grant financial finality')
    return f

def check_cycle(c, scope, now):
    if not isinstance(c,dict) or set(c)!=CYCLE_FIELDS or c['version']!=1 or type(c['version']) is not int or c['purpose']!=TYPE or c['current_scope']!=scope:fail('closed current cycle required')
    if type(c['sequence']) is not int or c['sequence']<1:fail('cycle generation required')
    try:day=date.fromisoformat(c['moscow_day'])
    except (ValueError,TypeError):fail('actual Moscow cycle day required')
    if at(c['started_at']).astimezone(MSK).date()!=day:fail('cycle day/provenance differs')
    if not at(c['started_at'])<=at(c['finished_at'])<=now:fail('cycle chronology differs')
    if c['natural'] is not True or c['scheduler_job']!='tenant-backfill-tick':fail('manual/build/theoretical wake is not a natural cycle')
    digest(c['scheduler_proof_hash'])
    if not isinstance(c['controller_execution'],str) or not re.fullmatch(r'projects/mpa-t-client-001/locations/europe-west1/jobs/tenant-backfill-controller/executions/tenant-backfill-controller-[a-z0-9]+',c['controller_execution']):fail('qualified canonical controller provenance required')
    if type(c['source_concurrency']) is not int or c['source_concurrency']!=1 or type(c['source_execution_overlap']) is not int or c['source_execution_overlap']!=0:fail('shared source fencing unproven')
    if type(c['source_dispatches']) is not int or c['source_dispatches']<1 or c['receipt_reconciliation_verified'] is not True:fail('cycle lacks accepted actual source work')
    if not isinstance(c['facts'],list) or len(c['facts'])!=len(ENTITIES) or {f.get('entity') for f in c['facts']}!=ENTITIES:fail('current domain coverage incomplete')
    if len({f.get('source_run_id') for f in c['facts']})!=len(ENTITIES) or len({f.get('certificate_hash') for f in c['facts']})!=len(ENTITIES):fail('one entity per qualified source execution; copied domain evidence denied')
    # At cycle completion all daily certificates must have been fresh. The
    # final readiness check repeats freshness against the observer's NOW.
    for f in c['facts']:
        check_fact(f,scope,at(c['finished_at']))
        if at(f['observed_at'])<at(c['started_at']):fail('cached prior data is not a new current cycle')
    return c

def readiness(cycles,scope,now,*,analytics_visible,historical_independent,systemic_risk,fbo_contract_hash):
    """Require three consecutive actual current cycles, never FULL progress.

    Analytics visibility/historical independence come from independent bounded
    reads, not from source row defaults, local files, or the cron expression.
    """
    digest(scope)
    if not isinstance(now,datetime) or now.utcoffset()!=timedelta(0):fail('aware UTC observer time required')
    if any(type(v) is not bool for v in (analytics_visible,historical_independent,systemic_risk)):fail('unknown global/visibility predicate')
    if systemic_risk:return dict(status='BLOCKED_SYSTEMIC',operational_READY=False)
    if fbo_contract_hash is None:return dict(status='PENDING_FBO_SOURCE_CONTRACT',operational_READY=False)
    digest(fbo_contract_hash)
    if not analytics_visible or not historical_independent:return dict(status='PENDING_INDEPENDENT_PROOF',operational_READY=False)
    if not isinstance(cycles,list):fail('canonical current cycle inventory required')
    by={}
    for c in cycles:
        check_cycle(c,scope,now)
        if c['sequence'] in by:fail('duplicate cycle authority')
        by[c['sequence']]=c
    if len(by)<3:return dict(status='PENDING_NATURAL_CYCLES',operational_READY=False,accepted_cycles=len(by))
    last=max(by);chosen=[by.get(n) for n in range(last-2,last+1)]
    if any(c is None for c in chosen):return dict(status='PENDING_CONSECUTIVE_CYCLES',operational_READY=False)
    if len({c['controller_execution'] for c in chosen})!=3 or len({c['scheduler_proof_hash'] for c in chosen})!=3:fail('reused natural wake provenance')
    all_facts=[f for c in chosen for f in c['facts']]
    if (len({f['source_run_id'] for f in all_facts})!=len(all_facts)
        or len({f['certificate_hash'] for f in all_facts})!=len(all_facts)):
        fail('prior source work cannot be counted as another natural cycle')
    if any(at(a['finished_at'])>at(b['started_at']) for a,b in zip(chosen,chosen[1:])):fail('current cycle overlap')
    if any(date.fromisoformat(b['moscow_day'])-date.fromisoformat(a['moscow_day'])!=timedelta(days=1) for a,b in zip(chosen,chosen[1:])):fail('three consecutive daily cycles are required')
    for f in chosen[-1]['facts']:check_fact(f,scope,now)
    return dict(status='CURRENT_READY',operational_READY=True,natural_cycles=3,finance_finality='PROVISIONAL',historical_completeness='INDEPENDENT_NOT_INFERRED')
