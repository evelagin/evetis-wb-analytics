"""Live controller release guard (09.11.2026): a tenant plan may never move the installed controller.

Real case: the client_001 registry document pinned ba9d3d04 (image 7749f7d5) while the live job ran the
eca0335a recovery release 1a2b61a0 (image 450ecc9e). The tenant plan was 0/1/0 (controller job ->
stale image + env) and the scanner reported nothing. Synthetic Terraform show shape, no cloud.
"""
import copy
import json
import pathlib

from tools.tenancy import plan_scan as P
from tools.tests.test_orchestration_plan import qualified  # noqa: F401 — pytest fixture

REPO = pathlib.Path(__file__).resolve().parents[2]


def _controller(plan, c):
    name = c['orchestration']['job']['name']
    return next(r for r in plan['resource_changes']
                if r['type'] == 'google_cloud_run_v2_job' and r['change']['after']['name'] == name)


def _as_update(rc, before):
    rc['change']['actions'] = ['update']
    rc['change']['before'] = before


def test_noop_update_of_installed_release_is_clean(qualified):  # noqa: F811
    c, p = qualified
    rc = _controller(p, c)
    _as_update(rc, copy.deepcopy(rc['change']['after']))
    assert P.scan_plan(p, c) == []


def test_plan_that_rolls_controller_image_back_is_refused(qualified):  # noqa: F811
    c, p = qualified
    rc = _controller(p, c)
    installed = copy.deepcopy(rc['change']['after'])
    box = installed['template'][0]['template'][0]['containers'][0]
    box['image'] = box['image'].rsplit(':', 1)[0] + ':' + '7' * 64
    for e in box['env']:
        if e['name'] == 'CONTROLLER_IMAGE':
            e['value'] = box['image']
        if e['name'] == 'CONTROLLER_SOURCE_SHA':
            e['value'] = '6' * 40
    _as_update(rc, installed)
    found = P.scan_plan(p, c)
    assert any('installed controller image' in f for f in found)
    assert any("installed controller env ['CONTROLLER_IMAGE', 'CONTROLLER_SOURCE_SHA']" in f for f in found)


def test_env_only_drift_is_refused(qualified):  # noqa: F811
    c, p = qualified
    rc = _controller(p, c)
    installed = copy.deepcopy(rc['change']['after'])
    installed['template'][0]['template'][0]['containers'][0]['env'].append({'name': 'CURRENT_DATA_MODE', 'value': 'CURRENT_DATA_V1'})
    _as_update(rc, installed)
    assert any("installed controller env ['CURRENT_DATA_MODE']" in f for f in P.scan_plan(p, c))


def test_first_create_is_not_a_release_change(qualified):  # noqa: F811
    c, p = qualified
    assert _controller(p, c)['change']['actions'] == ['create']
    assert P.scan_plan(p, c) == []


def test_registry_pin_has_its_release_record():
    """The registry pin (canonical loader) names a registered release that is the one the job runs."""
    from tools.tenancy import tenant_backfill as BF
    c = BF.target('client_001')
    env = c['orchestration']['job']['env']
    record = REPO / 'infra/tenant/releases/backfill' / (env['CONTROLLER_SOURCE_SHA'] + '.json')
    release = json.loads(record.read_text())
    assert release['source_sha'] == env['CONTROLLER_SOURCE_SHA'] and release['image'] == env['CONTROLLER_IMAGE'] == c['orchestration']['job']['image']
    assert release['cadence_policy']['root'] == env['BACKFILL_ROOT_HASH']
