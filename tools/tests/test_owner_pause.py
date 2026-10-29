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
    assert W.decide(datetime.fromisoformat(clock), execs)[0] == verdict


def test_window_requires_an_aware_clock():
    with pytest.raises(ValueError):
        W.decide(datetime(2026, 10, 28, 4, 41), [])
