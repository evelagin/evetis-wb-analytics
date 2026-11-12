"""A2 «Durable Daily»: transient reads retry, local evidence stays local, RECENT_90 first. Offline; no cloud call."""
import pytest
from tools.tenancy import durable_daily_image_check as X


@pytest.mark.parametrize('name,check', X.CHECKS, ids=[n for n, _ in X.CHECKS])
def test_durable_daily_contract(name, check):
    assert check() == 'PASS'


def test_packaged_check_reports_the_adapter_gate():
    assert X.check() == {'durable_daily_adapter': 'PASS'}
