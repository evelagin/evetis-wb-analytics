"""Heldi: owner FBO refresh of the rolling 90 days (OWNER_FBO_REFRESH_V1). Offline; no cloud call."""
import pytest
from tools.tenancy import fbo_refresh_image_check as X, fbo_refresh as FBR, tenant_backfill as BF


@pytest.mark.parametrize('name', ['authority', 'schedule', 'verdict', 'tick', 'scope', 'order', 'release', 'log_shape'])
def test_refresh_contract(name):
    assert getattr(X, name + '_check')() == 'PASS'


def test_installed_adapter():
    assert X.check() == {'fbo_refresh_adapter': 'PASS'}


@pytest.mark.parametrize('mode,daily,weekly', [('DAILY7_WEEKLY8_35', 7, 28), ('DAILY14', 14, 0)])
def test_both_owner_options_are_configuration(mode, daily, weekly):
    from datetime import date
    a = X.fixture(mode)[3]
    units = [u for u in FBR.schedule(a, date(2026, 11, 9)) if u['kind'] != 'HISTORY']
    span = lambda k: sum((date.fromisoformat(u['to']) - date.fromisoformat(u['from'])).days + 1 for u in units if u['kind'] == k)
    assert (span('DAILY'), span('WEEKLY')) == (daily, weekly)


def test_default_is_option_1():
    assert FBR.DEFAULT_MODE == 'DAILY7_WEEKLY8_35' and X.fixture()[3]['refresh']['daily_days'] == 7


def test_stdout_line_pins_source_disappeared_and_partial_status():
    """Pinned for stream E (#383): metric heldi_source_disappeared and the PARTIAL alert parse this line."""
    assert X.log_shape_check() == 'PASS'
