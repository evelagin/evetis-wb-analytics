"""Heldi: owner finance catch-up of missed accrual days. Offline; no cloud call."""
from datetime import date
import pytest
from tools.tenancy import finance_catchup_image_check as X, finance_catchup as FC


def test_closed_chained_owner_authority_admits_only_closed_days_after_the_full_cutoff():
    assert X.authority_check() == 'PASS'


def test_one_day_plan_per_authority_day_and_only_the_elected_plan_is_authorised():
    assert X.plan_check() == 'PASS'


def test_tick_elects_before_dispatch_never_repeats_and_completes_only_through_the_certificate():
    assert X.tick_check() == 'PASS'


def test_scope_disabled_without_marker_and_halts_on_partial_or_current_owned_day():
    assert X.scope_check() == 'PASS'


def test_open_catchup_drained_first_and_new_days_only_after_todays_current():
    assert X.order_check() == 'PASS'


def test_release_flag_requires_current_independence_and_adapter():
    assert X.release_check() == 'PASS'


def test_installed_adapter():
    assert X.check() == {'finance_catchup_adapter': 'PASS'}


@pytest.mark.parametrize('code', FC.DQ_CODES)
def test_dq_verdict_is_partial_not_stop(code):
    assert code.startswith('PILOT_')


def test_owner_gap_list_is_admissible_as_one_authority():
    c, m, p, a = X.fixture()
    assert [date.fromisoformat(d) for d in a['days']] == sorted(date.fromisoformat(d) for d in X.DAYS)
    assert FC.validate(a, c, m, p, X.NOW) is a


@pytest.mark.parametrize('day', ['2026-10-07', '2026-10-24', '2026-10-25'])
def test_days_owned_by_full_or_reachable_by_current_are_refused(day):
    c, m, p, a = X.fixture()
    x = FC.authority(c, m, p, serial=1, previous=None, owner_ack_sha256='2' * 64, created_at=X.CREATED, days=sorted(X.DAYS + [day]))
    with pytest.raises(Exception):
        FC.validate(x, c, m, p, X.NOW)


@pytest.mark.parametrize('created,ok', [('2026-10-26T10:00:00+00:00', False), ('2026-10-26T21:30:00+00:00', True), ('2026-10-27T09:00:00+00:00', True)])
def test_day_25_10_is_admissible_only_once_its_natural_cycle_can_no_longer_run(created, ok):
    c, m, p, a = X.fixture()
    from datetime import datetime, timezone
    x = FC.authority(c, m, p, serial=1, previous=None, owner_ack_sha256='2' * 64, created_at=created, days=X.DAYS + ['2026-10-25'])
    now = datetime(2026, 10, 28, tzinfo=timezone.utc)
    if ok:
        assert FC.validate(x, c, m, p, now) is x
    else:
        with pytest.raises(Exception):
            FC.validate(x, c, m, p, now)
