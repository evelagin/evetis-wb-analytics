"""Heldi: advertising in the daily CURRENT contour and the owner ads catch-up. Offline; no cloud call."""
import pytest
from tools.tenancy import current_ads_image_check as X


@pytest.mark.parametrize('name', ['release_check', 'settle_check', 'gate_check', 'tick_check', 'fence_check', 'order_check',
                                  'authority_check', 'catchup_tick_check', 'catchup_scope_check', 'status_check'])
def test_contract(name):
    assert getattr(X, name)() == 'PASS'


def test_installed_adapter():
    assert X.check() == {'current_ads_adapter': 'PASS'}
