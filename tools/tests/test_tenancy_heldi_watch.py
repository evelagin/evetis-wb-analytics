"""Heldi cloud watch (layer 2, tools/heldi_watch/health.py): checks, read-only boundary, emission, Terraform.

Offline: synthetic transport only (the test_tenancy* prefix blocks sockets). The synthetic STOP below is the
acceptance scenario of the proposal: a STOPPED record no owner recovery covers must give FAIL UNRESOLVED_STOP.
"""
from __future__ import annotations

import io
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.heldi_watch import health as W
from tools.tenancy import durable_plan as D, stop_acceptance as SA, tenant_tables as TT

REPO = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 10, 27, 7, 0, tzinfo=timezone.utc)            # 10:00 MSK, after the CURRENT deadline
ROOT = 'bb3ceca51ec5add6fe6fa3af8ec3720fbb5d9b07054b84e26d6bbfd6c8824222'
PROJECT = 'mpa-t-client-001'


def iso(t):
    return t.isoformat().replace('+00:00', 'Z')


def record(kind, seq, payload):
    return {'version': D.VERSION, 'root_hash': ROOT, 'kind': kind, 'sequence': seq, 'payload': payload}


STOP = record('STOPPED', 0, {'reason': 'CONTROLLER_GATE_OR_EVIDENCE_FAILURE', 'controller_execution': 'synthetic-26rzq'})
OTHER = record('FULL_MANIFEST', 0, {'synthetic': True})


def backend(records, mode='CURRENT_DATA_V1'):
    env = {'BACKFILL_ROOT_HASH': ROOT}
    if mode:
        env['CURRENT_DATA_MODE'] = mode
    store = SimpleNamespace(history=lambda root: list(records) if root == ROOT else [])
    return SimpleNamespace(c={'orchestration': {'job': {'env': env}}}, store=store, manifest={'hash': ROOT}, clock=lambda: NOW)


def codes(findings):
    return sorted(f['code'] for f in findings)


# ------------------------------------------------------------------ STOP (synthetic STOP acceptance)
def test_synthetic_unresolved_stop_is_fail(monkeypatch):
    monkeypatch.setattr(SA, 'approved', lambda b, m, r, **kw: [])
    got, facts = W.check_stop(backend([OTHER, STOP]))
    assert codes(got) == ['UNRESOLVED_STOP'] and got[0]['level'] == W.FAIL
    assert facts['unresolved'] == [D.digest(STOP)[:8]] and facts['stop_records'] == 1


def test_stop_covered_by_owner_recovery_is_ok(monkeypatch):
    monkeypatch.setattr(SA, 'approved', lambda b, m, r, **kw: [{'stop_hash': D.digest(STOP)}])
    got, facts = W.check_stop(backend([OTHER, STOP]))
    assert got == [] and facts['unresolved'] == []


def test_stop_gate_transport_failure_is_degraded_never_ok_or_fail(monkeypatch):
    def broken(*a, **k):
        raise TT.TableError('cloud transport HTTP 503')
    monkeypatch.setattr(SA, 'approved', broken)
    got, _ = W.check_stop(backend([STOP]))
    assert codes(got) == ['STOP_GATE_UNREADABLE'] and got[0]['level'] == W.DEGRADED


def test_stop_gate_evidence_failure_is_fail(monkeypatch):
    from tools.tenancy import tenant_backfill as BF
    def bad(*a, **k):
        raise BF.B.EvidenceError('certificate differs')
    monkeypatch.setattr(SA, 'approved', bad)
    got, _ = W.check_stop(backend([STOP]))
    assert codes(got) == ['STOP_EVIDENCE_FAILURE'] and got[0]['level'] == W.FAIL


# ------------------------------------------------------------------ CURRENT
def current(monkeypatch, *, cycle=True, certified=('catalog', 'stocks', 'prices', 'finance_accrual'), policy=True):
    from tools.tenancy import current_controller as CUR, full_controller as H
    monkeypatch.setattr(H, 'Backend', lambda b, m: b)
    monkeypatch.setattr(CUR, 'policy', lambda hb: {'hash': 'p'} if policy else None)
    monkeypatch.setattr(CUR, 'cycles', lambda hb, p: {'2026-10-27': {}} if cycle else {})
    recs = [{'kind': 'CURRENT_CERT', 'payload': {'fact': {'entity': e}}} for e in certified]
    monkeypatch.setattr(CUR, 'plans', lambda hb, p, day: (recs, {}))


def test_current_certified_by_deadline_is_ok(monkeypatch):
    current(monkeypatch)
    got, facts = W.check_current(backend([]), NOW)
    assert got == [] and facts['certified'] == ['catalog', 'finance_accrual', 'prices', 'stocks']


def test_current_missing_entity_after_deadline_is_fail(monkeypatch):
    current(monkeypatch, certified=('catalog', 'stocks'))
    got, _ = W.check_current(backend([]), NOW)
    assert codes(got) == ['CURRENT_NOT_CERTIFIED'] and got[0]['subject'] == ['2026-10-27', 'finance_accrual', 'prices']


def test_current_cycle_not_started_is_fail_like_26_10(monkeypatch):
    current(monkeypatch, cycle=False)
    got, _ = W.check_current(backend([]), NOW)
    assert codes(got) == ['CURRENT_CYCLE_NOT_STARTED'] and got[0]['level'] == W.FAIL


def test_current_before_deadline_is_not_judged(monkeypatch):
    current(monkeypatch, cycle=False)
    got, facts = W.check_current(backend([]), NOW.replace(hour=5))   # 08:00 MSK
    assert got == [] and facts['deadline_passed'] is False


def test_current_not_in_mode_is_skipped(monkeypatch):
    got, facts = W.check_current(backend([], mode=None), NOW)
    assert got == [] and facts == {'mode': None}


def test_current_deadline_constants():
    from tools.tenancy import current_controller as CUR
    assert W.CURRENT_DEADLINE_HOUR_MSK == 9 and CUR.INDEPENDENT_ENTITIES == ('catalog', 'stocks', 'prices', 'finance_accrual')


# ------------------------------------------------------------------ Scheduler
def sched_reader(states):
    jobs = [{'name': f'projects/{PROJECT}/locations/europe-west1/jobs/{n}', 'state': s} for n, s in states.items()]
    return SimpleNamespace(c={'project_id': PROJECT, 'region': 'europe-west1'}, request=lambda m, u, b=None: {'jobs': jobs})


ORDINARY_PAUSED = {'ozon-daily': 'PAUSED', 'ozon-fast': 'PAUSED', 'ozon-weekly': 'PAUSED', 'heldi-health-eval': 'ENABLED'}


def test_scheduler_expected_state_is_ok():
    got, facts = W.check_schedulers(sched_reader(dict(ORDINARY_PAUSED, **{'tenant-backfill-tick': 'ENABLED'})))
    assert got == [] and facts['states']['tenant-backfill-tick'] == 'ENABLED'


def test_tick_paused_is_degraded_and_ordinary_enabled_is_fail():
    got, _ = W.check_schedulers(sched_reader(dict(ORDINARY_PAUSED, **{'tenant-backfill-tick': 'PAUSED', 'ozon-fast': 'ENABLED'})))
    assert {(f['code'], f['level']) for f in got} == {('TICK_NOT_ENABLED', W.DEGRADED), ('ORDINARY_SCHEDULER_NOT_PAUSED', W.FAIL)}


def test_tick_missing_is_fail():
    got, _ = W.check_schedulers(sched_reader(ORDINARY_PAUSED))
    assert codes(got) == ['TICK_MISSING']


# ------------------------------------------------------------------ executions
def execution(name, start, seconds=None, failed=False):
    e = {'name': f'projects/{PROJECT}/locations/europe-west1/jobs/tenant-backfill-controller/executions/{name}',
         'createTime': iso(start - timedelta(seconds=3)), 'startTime': iso(start)}
    if seconds is not None:
        e['completionTime'] = iso(start + timedelta(seconds=seconds))
        e['failedCount' if failed else 'succeededCount'] = 1
    return e


def fact(name, status, t, completed=None, total=None):
    return dict(at=t, execution=name, status=status, completed=completed, total=total)


def test_execution_overrun_completed_and_running_is_fail():
    ex = [execution('long1', NOW - timedelta(hours=1), 1501), execution('run1', NOW - timedelta(seconds=1600))]
    got, facts = W.check_executions(ex, [], NOW)
    assert codes(got) == ['EXECUTION_OVERRUN', 'EXECUTION_OVERRUN'] and facts['running'] == 1
    ok, _ = W.check_executions([execution('fast', NOW - timedelta(hours=1), 940)], [], NOW)   # observed max 940 s
    assert ok == []


def test_failed_with_bounded_wait_is_not_an_alarm_until_repeated():
    t = NOW - timedelta(minutes=30)
    one = [execution('w1', t, 60, failed=True)]
    got, _ = W.check_executions(one, [fact('w1', 'WAITING_READ_RETRY', t)], NOW)
    assert got == []
    many = [execution('w%d' % i, t + timedelta(minutes=i), 60, failed=True) for i in range(3)]
    got, _ = W.check_executions(many, [fact('w%d' % i, 'WAITING_READ_RETRY', t) for i in range(3)], NOW)
    assert codes(got) == ['REPEATED_READ_WAIT'] and got[0]['level'] == W.DEGRADED


def test_crash_without_status_is_fail_like_kc4bg_after_index_grace():
    t = NOW - timedelta(minutes=40)
    got, _ = W.check_executions([execution('kc4bg', t, 90, failed=True)], [], NOW)
    assert codes(got) == ['EXECUTION_FAILED_WITHOUT_WAIT'] and got[0]['subject'] == 'kc4bg'
    fresh = NOW - timedelta(minutes=3)
    got, _ = W.check_executions([execution('new', fresh, 60, failed=True)], [], NOW)
    assert got == []                                   # not indexed yet: judged next time, never cached as OK


def test_stopped_execution_is_fail():
    t = NOW - timedelta(minutes=50)
    got, _ = W.check_executions([execution('26rzq', t, 70, failed=True)], [fact('26rzq', 'STOPPED', t)], NOW)
    assert codes(got) == ['EXECUTION_STOPPED']


# ------------------------------------------------------------------ FULL progress
def window(statuses, completed=40, total=466):
    start = NOW - W.PROGRESS_WINDOW
    return [fact('e%d' % i, s, start + timedelta(minutes=3 * i), completed, total) for i, s in enumerate(statuses)]


def test_no_full_progress_with_work_remaining_is_degraded():
    got, info = W.check_progress(window(['WAIT_ACTIVE_CONTROLLER'] * 100), NOW, True)
    assert codes(got) == ['NO_FULL_PROGRESS'] and info['total_chunks'] == 466


def test_progress_ok_when_dispatched_complete_paused_or_window_not_covered():
    assert W.check_progress(window(['WAIT_ACTIVE_CONTROLLER'] * 99 + ['DISPATCHED']), NOW, True)[0] == []
    assert W.check_progress(window(['MONITORING'] * 100, completed=466), NOW, True)[0] == []
    assert W.check_progress(window(['WAIT_ACTIVE_CONTROLLER'] * 100), NOW, False)[0] == []
    late = [f for f in window(['WAIT_ACTIVE_CONTROLLER'] * 100) if f['at'] > NOW - timedelta(hours=2)]
    assert W.check_progress(late, NOW, True)[0] == []


# ------------------------------------------------------------------ read-only boundary
class Wire:
    """Synthetic network: metadata, Run, Scheduler, BigQuery listing, Logging. Records every call."""

    def __init__(self, identity='sa-heldi-watch@mpa-t-client-001.iam.gserviceaccount.com', entries=None, previous=None):
        self.identity, self.calls = identity, []
        self.entries, self.previous = entries or [], previous

    def __call__(self, method, url, body=None, headers=None):
        from tools.tenancy.cloud_access import METADATA
        self.calls.append((method, url))
        if url == METADATA + 'email':
            return self.identity
        if url == METADATA + 'token':
            return {'access_token': 'synthetic', 'expires_in': 3600}
        if url == W.LOGS:
            assert method == 'POST' and set(body) <= {'resourceNames', 'filter', 'orderBy', 'pageSize', 'pageToken'}
            if 'heldi-health-eval' in body['filter']:
                return {'entries': [self.previous] if self.previous else []}
            return {'entries': self.entries}
        if 'cloudscheduler' in url:
            return {'jobs': [{'name': f'projects/{PROJECT}/locations/europe-west1/jobs/{n}', 'state': s}
                             for n, s in dict(ORDINARY_PAUSED, **{'tenant-backfill-tick': 'ENABLED'}).items()]}
        if '/executions' in url:
            return {'executions': []}
        raise AssertionError('unexpected synthetic request %s %s' % (method, url))


def reader(wire):
    from tools.tenancy import tenant_backfill as BF
    c = BF.target('client_001')
    return W.Reader(c, W.Tokens(c['project_id'], wire), wire)


@pytest.mark.parametrize('job', ['tenant-backfill-controller', 'ozon-runtime-daily', 'heldi-health-eval'])
def test_reader_refuses_run_dispatch(job):
    w = Wire(); r = reader(w)
    with pytest.raises(TT.TableError):
        r.request('POST', W.run_url(r.c, job, ':run'), {})
    assert not [c for c in w.calls if c[1].endswith(':run')]


def test_reader_refuses_every_bigquery_write_and_append_authority():
    w = Wire(); r = reader(w)
    bq = f'https://www.googleapis.com/bigquery/v2/projects/{PROJECT}'
    for method, url, body in [('POST', bq + '/datasets/tenant_ops/tables/BACKFILL_CHECKPOINTS/insertAll', {'rows': [{}]}),
                              ('POST', bq + '/datasets/tenant_locks/tables', {'tableReference': {'tableId': 'BFR_' + '0' * 64}}),
                              ('DELETE', bq + '/datasets/tenant_locks/tables/X', None),
                              ('POST', bq + '/queries', {'query': 'DELETE FROM `mpa-t-client-001.tenant_ops.BACKFILL_CHECKPOINTS` WHERE TRUE'}),
                              ('POST', 'https://cloudscheduler.googleapis.com/v1/projects/%s/locations/europe-west1/jobs/tenant-backfill-tick:pause' % PROJECT, {})]:
        with pytest.raises(TT.TableError):
            r.request(method, url, body)
    with pytest.raises(TT.TableError):
        r.access.append_request('POST', bq + '/datasets/tenant_ops/tables/BACKFILL_CHECKPOINTS/insertAll', {'rows': [{}], 'skipInvalidRows': False, 'ignoreUnknownValues': False})
    with pytest.raises(TT.TableError):
        W.Tokens(PROJECT, w).token('append')
    assert all(m == 'GET' for m, u in w.calls)             # only metadata reads happened, nothing was sent


def test_logging_read_is_bounded_to_cloud_run_job_filters():
    r = reader(Wire())
    with pytest.raises(TT.TableError):
        r.logs('logName="projects/mpa-t-client-001/logs/cloudaudit.googleapis.com%2Fdata_access"')
    with pytest.raises(TT.TableError):
        r.logs('resource.type="cloud_run_job" AND x', page_size=5000)


def test_foreign_identity_never_evaluates_and_has_no_heartbeat():
    w = Wire(identity='sa-backfill-controller@mpa-t-client-001.iam.gserviceaccount.com'); out = io.StringIO()
    assert W.main([], send=w, clock=lambda: NOW, out=out) == 3
    lines = [json.loads(x) for x in out.getvalue().splitlines()]
    assert [x['heldi_watch'] for x in lines] == ['UNAVAILABLE']
    assert all('logging' not in u and 'run.googleapis' not in u for _, u in w.calls)


def test_source_has_no_write_path():
    src = (REPO / 'tools/heldi_watch/health.py').read_text()
    for token in (':run', ':pause', ':resume', 'insertAll', 'append_checkpoint', 'create_marker', '.commit(', "'PATCH'", "'DELETE'", 'gcloud run'):
        assert token not in src, token


# ------------------------------------------------------------------ evaluation, synthetic STOP end to end, emission
def fake_context(records):
    def ctx(reader, clock):
        return backend(records, mode=None), {'generation': 60}, []
    return ctx


def run_main(monkeypatch, records, previous=None, approved=()):
    monkeypatch.setattr(W, 'controller_context', fake_context(records))
    monkeypatch.setattr(SA, 'approved', lambda b, m, r, **kw: [{'stop_hash': h} for h in approved])
    w = Wire(previous=previous); out = io.StringIO()
    assert W.main([], send=w, clock=lambda: NOW, out=out) == 0
    return [json.loads(x) for x in out.getvalue().splitlines()], w


def test_synthetic_stop_end_to_end_emits_fail_health_and_heartbeat(monkeypatch):
    lines, w = run_main(monkeypatch, [OTHER, STOP])
    health = [x for x in lines if x['heldi_watch'] == 'HEALTH']
    assert len(health) == 1 and health[0]['health'] == 'FAIL' and health[0]['severity'] == 'ERROR'
    assert health[0]['reasons'] == ['UNRESOLVED_STOP'] and D.digest(STOP)[:8] in health[0]['summary']
    assert lines[-1]['heldi_watch'] == 'HEARTBEAT' and lines[-1]['health'] == 'FAIL'
    assert {m for m, u in w.calls if u != W.LOGS} == {'GET'}   # Logging entries:list is the only POST


def test_unchanged_state_writes_only_heartbeat_and_reminds_after_12h(monkeypatch):
    first, _ = run_main(monkeypatch, [OTHER, STOP])
    fp = first[0]['fingerprint']
    prev = {'timestamp': iso(NOW - timedelta(hours=1)), 'jsonPayload': {'fingerprint': fp, 'health': 'FAIL'}}
    lines, _ = run_main(monkeypatch, [OTHER, STOP], previous=prev)
    assert [x['heldi_watch'] for x in lines] == ['HEARTBEAT']
    prev['timestamp'] = iso(NOW - W.REMINDER)
    lines, _ = run_main(monkeypatch, [OTHER, STOP], previous=prev)
    assert [x['heldi_watch'] for x in lines] == ['HEALTH', 'HEARTBEAT']


def test_recovery_emits_ok_once(monkeypatch):
    first, _ = run_main(monkeypatch, [OTHER, STOP])
    prev = {'timestamp': iso(NOW - timedelta(minutes=30)), 'jsonPayload': {'fingerprint': first[0]['fingerprint'], 'health': 'FAIL'}}
    lines, _ = run_main(monkeypatch, [OTHER, STOP], previous=prev, approved=[D.digest(STOP)])
    assert lines[0]['heldi_watch'] == 'HEALTH' and lines[0]['health'] == 'OK' and lines[0]['previous'] == 'FAIL'


def test_synthetic_fail_proves_alert_path_without_production_stop(monkeypatch):
    monkeypatch.setattr(W, 'controller_context', fake_context([OTHER]))
    monkeypatch.setattr(SA, 'approved', lambda b, m, r, **kw: [])
    out = io.StringIO()
    assert W.main(['--synthetic-fail', 'syn-20261027'], send=Wire(), clock=lambda: NOW, out=out) == 0
    health = json.loads(out.getvalue().splitlines()[0])
    assert health['health'] == 'FAIL' and health['reasons'] == ['SYNTHETIC_FAIL'] and 'syn-20261027' in health['summary']
    with pytest.raises(SystemExit):
        W.main(['--synthetic-fail', 'bad id; rm'], send=Wire(), clock=lambda: NOW, out=io.StringIO())


def test_broken_check_is_degraded_not_ok(monkeypatch):
    def ctx(reader, clock):
        raise TT.TableError('cloud transport HTTP 403')
    monkeypatch.setattr(W, 'controller_context', ctx)
    out = io.StringIO()
    assert W.main([], send=Wire(), clock=lambda: NOW, out=out) == 0
    health = json.loads(out.getvalue().splitlines()[0])
    assert health['health'] == 'DEGRADED' and health['reasons'] == ['READ_FAILED']


# ------------------------------------------------------------------ Terraform and image (static)
TF = (REPO / 'infra/tenant-monitoring/watch.tf').read_text()


def tf_list(name):
    body = re.search(name + r'\s*=\s*\[(.*?)\]', TF, re.S).group(1)
    return re.findall(r'"([^"]+)"', body)


def test_watch_roles_are_exact_and_read_only():
    assert tf_list('watch_read_permissions') == ['bigquery.jobs.create', 'cloudscheduler.jobs.list', 'logging.logEntries.list',
                                                 'run.executions.get', 'run.executions.list', 'run.jobs.get']
    assert tf_list('watch_data_permissions') == ['bigquery.datasets.get', 'bigquery.tables.get', 'bigquery.tables.getData', 'bigquery.tables.list']
    assert tf_list('watch_datasets') == ['ozon_raw', 'ref', 'tenant_ops', 'tenant_locks']
    assert 'role    = "roles/' not in TF and 'jobUser' not in TF.split('НЕ roles/bigquery.jobUser')[1].replace('vtsClientQueryRunner', '')
    assert re.findall(r'permissions = \["([^"]+)"\]', TF) == ['run.jobs.run']           # wake role only
    assert 'google_bigquery_dataset_iam' not in TF                                      # dataset ACL is authoritative in infra/tenant


def test_scheduler_wakes_only_the_watch_job_and_starts_paused():
    uris = re.findall(r'uri\s*=\s*"([^"]+)"', TF)
    assert uris == ['https://run.googleapis.com/v2/projects/${var.project_id}/locations/${var.region}/jobs/heldi-health-eval:run']
    assert 'paused    = !var.watch_active' in TF and 'tenant-backfill-tick' not in re.search(r'resource "google_cloud_scheduler_job" "watch".*?\n}\n', TF, re.S).group(0)


def test_dockerfile_builds_from_the_live_controller_image_only():
    df = (REPO / 'tools/heldi_watch/Dockerfile').read_text()
    assert re.findall(r'^FROM (.+)$', df, re.M) == ['${CONTROLLER_IMAGE}']
    assert re.findall(r'^COPY (.+)$', df, re.M) == ['tools/heldi_watch /app/tools/heldi_watch']
