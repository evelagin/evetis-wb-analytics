"""Heldi STOP bf7d1370: an owner pause of the tick during a wake is a typed quiescent end, not a STOP. Offline."""
from datetime import datetime, timezone
import pytest
from tools.tenancy import owner_pause_image_check as X, owner_pause_window as W, tenant_backfill as BF


def test_only_the_owner_pause_is_tolerated_and_every_other_drift_stops():
    assert X.scheduler_check() == 'PASS'


def test_dispatch_boundary_declines_before_any_lease_or_intent():
    assert X.dispatch_boundary_check() == 'PASS'


def test_pause_before_claim_and_after_a_reconciled_page_closes_quiescent_without_stop():
    assert X.wake_end_check() == 'PASS'


def test_exact_vwfcr_sequence_reads_pass_and_the_next_page_dispatch_declines_before_lease():
    assert X.vwfcr_sequence_check() == 'PASS'


@pytest.mark.parametrize('suite', ['tools.tenancy.full_controller_closure_image_check', 'tools.tenancy.full_terminal_gate_image_check',
                                   'tools.tenancy.current_independence_image_check', 'tools.tenancy.finance_catchup_image_check'])
def test_regression_cr_v4_tg_s4_catchup_adapters(suite):
    import importlib
    out = importlib.import_module(suite).check()
    assert out and all(v == 'PASS' for v in out.values() if isinstance(v, str))


def test_current_and_catchup_dispatch_raise_the_typed_pause_without_intent():
    assert X.current_paths_check() == 'PASS'


def test_fbo_diagnostic_keeps_its_permission_and_lets_the_claim_close():
    assert X.diagnostic_check() == 'PASS'


def test_installed_adapter():
    assert X.check() == {'owner_pause_quiescent_adapter': 'PASS'}


@pytest.mark.parametrize('clock,execs,verdict', [
    ('2026-10-28T04:39:37+00:00', [], 'WAIT'),                                          # 37 s after the 04:39 fire
    ('2026-10-28T04:41:40+00:00', [], 'WAIT'),                                          # 20 s before 04:42
    ('2026-10-28T04:41:00+00:00', [{'createTime': '2026-10-28T04:39:01Z'}], 'WAIT'),    # the fired wake still runs
    ('2026-10-28T04:41:00+00:00', [{'createTime': '2026-10-28T04:39:01Z', 'completionTime': '2026-10-28T04:40:30Z'}], 'SAFE'),
    ('2026-10-28T04:40:37+00:00', [{'createTime': '2026-10-28T04:33:02Z'}], 'WAIT'),    # the real freeze moment: vwfcr still active
])
def test_owner_pause_window(clock, execs, verdict):
    assert W.decide(datetime.fromisoformat(clock), {'executions': execs}, '*/3 * * * *')[0] == verdict


@pytest.mark.parametrize('clock,verdict', [
    ('2026-10-28T04:40:30+00:00', 'WAIT'),        # */10: 30 s after the 04:40 fire
    ('2026-10-28T04:44:00+00:00', 'SAFE'),        # */10: 240 s after the 04:40 fire, 360 s to the next
    ('2026-10-28T04:49:40+00:00', 'WAIT'),        # */10: 20 s before 04:50
])
def test_owner_pause_window_follows_the_live_fallback_cadence(clock, verdict):
    assert W.decide(datetime.fromisoformat(clock), {'executions': []}, '*/10 * * * *')[0] == verdict


@pytest.mark.parametrize('listing,schedule', [
    ({'executions': [], 'nextPageToken': 'more'}, '*/3 * * * *'),      # an incomplete listing could hide the active wake
    ([], '*/3 * * * *'),                                                # not a listing page
    ({'executions': []}, '*/5 * * * *'),                                # unknown cadence: no window is computed
    ({'executions': []}, None),
])
def test_owner_pause_window_refuses_incomplete_evidence(listing, schedule):
    assert W.decide(datetime.fromisoformat('2026-10-28T04:41:00+00:00'), listing, schedule)[0] == 'REFUSE'


def test_window_requires_an_aware_clock():
    with pytest.raises(ValueError):
        W.decide(datetime(2026, 10, 28, 4, 41), {'executions': []}, '*/3 * * * *')


def _preflight_world(monkeypatch, tick_state):
    """The REAL BF.preflight over a mocked read-only cloud: one own controller execution, ordinary Schedulers PAUSED,
    the dedicated tick at `tick_state` with its exact target/identity. Every write fails the test."""
    from copy import deepcopy
    from datetime import date
    from types import SimpleNamespace
    import lifecycle_core as L
    from tools.tenancy import registry as R, orchestration_contract as OC
    c = deepcopy(R.terraform_inputs('client_001')); o = deepcopy(BF.target('client_001')['orchestration'])
    o.pop('cadence', None)                                       # the cadence claim path is covered by its own suites
    c['orchestration'] = o
    p = BF.make_plan('client_001', 'fbo_postings', '2026-09-17', '2026-09-17', 'offline-pilot', '2026-10-04T00:00:00Z', date(2026, 10, 4))
    base, expected = BF.resources(c)
    runtime_sa = f"{c['marketplaces']['ozon']['service_accounts']['runtime']}@{c['project_id']}.iam.gserviceaccount.com"
    jobs = [{'name': base + '/jobs/' + n, 'template': {'template': {'timeout': '3600s', 'maxRetries': 0, 'serviceAccount': runtime_sa,
             'containers': [{'image': p['image'], 'env': [{'name': k, 'value': v} for k, v in cfg['env'].items()]}]}}} for n, cfg in expected.items()]
    jobs.append({'name': base + '/jobs/tenant-control', 'template': {'template': {'timeout': '3600s', 'maxRetries': 0, 'serviceAccount': c['control']['email'],
                 'containers': [{'image': p['image'], 'command': ['python', 'lifecycle.py'], 'args': ['status'],
                                 'env': [{'name': k, 'value': v} for k, v in c['control']['job']['env'].items()]}]}}})
    controller = base + '/jobs/' + o['job']['name']; own = controller + '/executions/' + o['job']['name'] + '-own'
    jobs.append({'name': controller})
    tick = X.schedule(o, state=tick_state); tick['name'] = base + '/jobs/' + o['scheduler']['name']
    sched = [{'name': base + '/jobs/' + cfg['scheduler'], 'state': 'PAUSED'} for cfg in expected.values()] + [tick]
    monkeypatch.setattr(BF.TL, 'read_state', lambda *a: ([], {}, [], [], False))
    monkeypatch.setattr(L, 'current_state', lambda _: L.VALIDATING)
    monkeypatch.setattr(BF.TL, 'operator_binding', lambda *a: ({'seller': 'BOUND'}, {'seller': {'status': 'PASS'}}))
    monkeypatch.setattr(OC, 'verify_job', lambda job, expected: None)   # the controller template contract has its own suite
    def read(method, url, body=None):
        assert method == 'GET', 'preflight wrote: %s %s' % (method, url)
        if 'cloudscheduler' in url: return {'jobs': sched}
        if '/namespaces/' in url:
            return {'items': [{'metadata': {'name': j['name'].rsplit('/', 1)[-1], 'labels': {'cloud.googleapis.com/location': c['region']}}} for j in jobs]}
        if controller + '/executions' in url: return {'executions': [{'name': own}]}
        if '/executions' in url: return {'executions': []}
        return {'jobs': jobs}
    backend = SimpleNamespace(c=c, tables=SimpleNamespace(), request=read, current_execution=own)
    return c, p, backend


def test_real_preflight_with_the_tick_paused_reads_pass_and_the_dispatch_preflight_declines(monkeypatch):
    """SHOULD-1: the REAL BF.preflight (not a double). Reads/reconciliation (dispatch=False) pass with the owner's pause;
    the dispatch preflight (dispatch=True) raises the typed SourceDispatchPaused; ENABLED passes both; drift stops both."""
    from tools.tenancy.cloud_tick import SourceDispatchPaused
    now = datetime(2026, 10, 4, tzinfo=timezone.utc)
    c, p, b = _preflight_world(monkeypatch, 'PAUSED')
    assert BF.preflight(c, p, now, backend=b) == (b.tables, [])
    with pytest.raises(SourceDispatchPaused):
        BF.preflight(c, p, now, backend=b, dispatch=True)
    c, p, b = _preflight_world(monkeypatch, 'ENABLED')
    assert BF.preflight(c, p, now, backend=b, dispatch=True) == (b.tables, [])
    c, p, b = _preflight_world(monkeypatch, 'UPDATE_FAILED')
    for dispatch in (False, True):
        with pytest.raises(BF.B.EvidenceError):
            BF.preflight(c, p, now, backend=b, dispatch=dispatch)


def test_local_fbo_quarantine_declines_before_its_stop_when_the_owner_paused_the_tick(monkeypatch):
    """SHOULD-4: full_fbo_diagnostic_closure.automatic raises SourceDispatchPaused BEFORE committing the local STOP."""
    assert X.closure_check() == 'PASS'


def test_owner_pause_release_flag_requires_independence_and_finance_catchup():
    assert X.release_check() == 'PASS'


def test_fbo_diagnostic_never_dispatches_under_a_paused_descriptor():
    assert X.diagnostic_check() == 'PASS'      # includes the PAUSED-descriptor/PAUSED-tick case (EXACT, still declined)
