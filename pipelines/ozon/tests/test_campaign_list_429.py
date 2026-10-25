"""HTTP 429 on idempotent Performance reads: bounded transport retry, whole scenario, network-free.

Production 2026-10-09..19 (client_001): token 200 → page 1 200 → page 2 429 in the same
second, 38 attempts in a row, each followed by a 1-hour cooldown and a restart from page 1.
These tests drive the real transport (`C._request`) and the real engine with a fake socket.
"""
import io
import json
import urllib.error
import urllib.request

import pytest

import backfill as F
import backfill_core as B
import common as C
from test_backfill_engine import harness, plan  # noqa: F401  (fixture)

PAGE = '/api/client/campaign?page={}&pageSize=100'
REAL_PERF_GET, REAL_PERF_POST = C.perf_get, C.perf_post   # the harness denies them; this file drives the real transport


DAY = '2026-09-17'
EXPENSE = f'/api/client/statistics/expense?dateFrom={DAY}&dateTo={DAY}'
DAILY = f'/api/client/statistics/daily?dateFrom={DAY}&dateTo={DAY}'
STATUS = '/api/client/statistics/synthetic-uuid'
REPORT = '/api/client/statistics/report?UUID=synthetic-uuid'
ORDER = '/api/client/statistics'


class Source:
    """Fake Performance socket: scripted status per request URL, records every attempt."""

    def __init__(self, script=None, total=647, retry_after=None, spend=False):
        self.script, self.total, self.retry_after, self.calls = script or {}, total, retry_after, []
        self.spend = spend   # one SKU campaign with spend → the async report path is required

    def __call__(self, req, timeout=None):
        path = req.full_url[len(C.PERF):]
        self.calls.append((req.get_method(), path))
        queue = self.script.get(path)
        status = queue.pop(0) if queue else 200
        if status != 200:
            headers = {'Retry-After': str(self.retry_after)} if self.retry_after is not None else {}
            raise urllib.error.HTTPError(req.full_url, status, 'synthetic', headers, io.BytesIO(b'{}'))
        return Response(self.body(path))

    def body(self, path):
        if path.startswith('/api/client/campaign?page='):
            page = int(path.split('page=')[1].split('&')[0])
            first = (page - 1) * 100
            ids = range(first, min(first + 100, self.total))
            return {'total': str(self.total), 'list': [{'id': 10_000 + i, 'advObjectType': 'SKU'} for i in ids]}
        if path.startswith('/api/client/statistics/expense'):
            row = '10000;synthetic;12,50;0;0\n' if self.spend else ''
            return 'ID;Название;Расход;Расход бонусов;Расход с абонентского счета\n' + row
        if path.startswith('/api/client/statistics/daily'):
            row = '10000;synthetic;100;5;1;300\n' if self.spend else ''
            return 'ID;Название;Показы;Клики;Заказы, шт.;Заказы, ₽\n' + row
        if path == ORDER:
            return {'UUID': 'synthetic-uuid'}
        if path == STATUS:
            return {'state': 'OK'}
        if path == REPORT:
            return ('synthetic title\nДень;sku;Расход, ₽, с НДС;Показы;Клики\n'
                    '17.09.2026;555;12,50;100;5\n')
        raise AssertionError('unexpected synthetic path ' + path)


class Response:
    status = 200
    headers = {}

    def __init__(self, body):
        self._body = (body if isinstance(body, str) else json.dumps(body)).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, *a):
        return self._body


@pytest.fixture
def wire(monkeypatch):
    """Real perf_get/_request over a fake socket; sleeps recorded, never slept."""
    sleeps = []
    monkeypatch.setattr(C, 'perf_token', lambda: 'synthetic-token')
    monkeypatch.setattr(C, 'perf_get', REAL_PERF_GET)
    monkeypatch.setattr(C, 'perf_post', REAL_PERF_POST)
    monkeypatch.setattr(C.time, 'sleep', sleeps.append)
    monkeypatch.setattr(F.time, 'sleep', sleeps.append)
    monkeypatch.setattr(C, 'log', lambda **kw: None)
    monkeypatch.setattr(C, 'PERF_REQUEST_TRACE', [])
    monkeypatch.setattr(C, 'PERF_TRACE_COMPLETE', True)

    def install(source):
        monkeypatch.setattr(C.urllib.request, 'urlopen', source)
        return source
    return install, sleeps


def run(entity, persist):
    p = plan(entity)
    e = F.Engine(p, 'rt-synthetic-429', '2026-10-04T10:00:00+03:00', B.initial(p))
    return e, e.run(persist)


def test_page_two_transient_429_completes_the_list_in_the_same_run(harness, wire):  # noqa: F811
    install, sleeps = wire
    source = install(Source({PAGE.format(2): [429]}))
    before = dict(C.STATS)
    e, out = run('ads_campaigns', harness[3])
    detail = harness[2][-1]['detail']
    assert out['evidence']['complete'] is True and detail['action'] == 'CAMPAIGNS_COMPLETE'
    assert detail['campaigns'] == 647 and len(harness[0]['RAW_OZON_ADS_CAMPAIGNS']) == 647
    assert [p for _, p in source.calls] == [PAGE.format(1), PAGE.format(2), PAGE.format(2)] + [PAGE.format(n) for n in range(3, 8)]
    # Logical accounting is unchanged (one call per page); the retry is transport telemetry.
    assert detail['logical_requests'] == 7 and detail['transport_requests'] == 8 and detail['transport_retries'] == 1
    assert C.STATS['retries'] - before['retries'] == 1
    # 2 s between pages, 3 s before the retried page; never the same second.
    assert sleeps == [2, 3, 2, 2, 2, 2, 2]
    assert 'rate_limit' not in out['evidence']['state']['progress']


def test_retry_is_bounded_then_falls_back_to_the_durable_cooldown(harness, wire):  # noqa: F811
    install, sleeps = wire
    source = install(Source({PAGE.format(2): [429] * 5}))
    e, out = run('ads_campaigns', harness[3])
    state, detail = out['evidence']['state'], harness[2][-1]['detail']
    assert [p for _, p in source.calls].count(PAGE.format(2)) == 5      # 1 + 4 bounded retries
    assert sleeps == [2, 3, 6, 12, 24]
    assert detail['action'] == 'SOURCE_THROTTLED' and state['progress']['rate_limit']['count'] == 1
    assert detail['logical_requests'] == 2 and detail['transport_retries'] == 4
    assert not state['complete'] and 'RAW_OZON_ADS_CAMPAIGNS' not in harness[0]


def test_exhausted_retry_keeps_the_controller_zero_effect_accounting(harness, wire):  # noqa: F811
    """The controller verifier must still accept the throttled prefix (no STOP)."""
    from tools.tenancy import tenant_backfill as T
    install, _ = wire
    install(Source({PAGE.format(2): [429] * 5}))
    e, out = run('ads_campaigns', harness[3])
    details = {1: T.source_detail(harness[2][-1]['detail'])}
    assert T.rejected_campaign_prefix(details, out['evidence']['state']) is True


def test_long_retry_after_is_not_waited_in_run(harness, wire):  # noqa: F811
    install, sleeps = wire
    source = install(Source({PAGE.format(2): [429]}, retry_after=7200))
    e, out = run('ads_campaigns', harness[3])
    assert [p for _, p in source.calls] == [PAGE.format(1), PAGE.format(2)] and sleeps == [2]
    assert harness[2][-1]['detail']['action'] == 'SOURCE_THROTTLED'
    assert harness[2][-1]['detail']['source_diagnostic']['retry-after'] == 7200


def test_short_retry_after_is_honoured(harness, wire):  # noqa: F811
    install, sleeps = wire
    install(Source({PAGE.format(2): [429]}, retry_after=10))
    e, out = run('ads_campaigns', harness[3])
    assert out['evidence']['complete'] is True and sleeps[:2] == [2, 10]


def test_sku_statistics_continue_after_a_retried_campaign_page(harness, wire):  # noqa: F811
    """Whole path: campaign registry (with one 429) → expense statistics of the day."""
    install, _ = wire
    source = install(Source({PAGE.format(2): [429]}, total=150))
    e, out = run('ads_sku_daily', harness[3])
    paths = [p for _, p in source.calls]
    assert paths[:3] == [PAGE.format(1), PAGE.format(2), PAGE.format(2)]
    assert any(p.startswith('/api/client/statistics/expense?') for p in paths[3:])
    assert all(d['detail']['action'] != 'SOURCE_THROTTLED' for d in harness[2])


@pytest.mark.parametrize('method,path', [
    ('POST', ORDER),                                                      # report order: never retried
    ('GET', '/api/client/statistics/expense?dateFrom=2026-10-01&dateTo=2026-10-05'),  # lifecycle range
    ('GET', '/api/client/campaign'),                                      # unpaged ordinary read
    ('GET', '/api/client/campaign/1/v2/products?page=1&pageSize=100'),
])
def test_other_performance_requests_are_not_retried_on_429(wire, method, path):
    install, sleeps = wire
    source = install(Source({path: [429, 200]}))
    req = urllib.request.Request(C.PERF + path, data=b'{}' if method == 'POST' else None, method=method)
    status, _ = C._request(req)
    assert status == 429 and len(source.calls) == 1 and sleeps == []


@pytest.mark.parametrize('path', [EXPENSE, DAILY, STATUS, REPORT])
def test_idempotent_report_reads_are_retried_on_429(wire, path):
    install, sleeps = wire
    source = install(Source({path: [429, 429, 200]}))
    status, _ = C._request(urllib.request.Request(C.PERF + path), raw_text=True)
    assert status == 200 and [p for _, p in source.calls] == [path] * 3 and sleeps == [3, 6]


@pytest.mark.parametrize('failure', [429, 500, 503])
def test_report_order_post_is_never_repeated(wire, failure):
    """A report order with an unknown outcome must never be sent twice (duplicate report)."""
    install, sleeps = wire
    source = install(Source({ORDER: [failure, 200]}))
    status, _ = C.perf_post(ORDER, {'campaigns': ['10000'], 'dateFrom': DAY, 'dateTo': DAY, 'groupBy': 'DATE'})
    assert status == failure and source.calls == [('POST', ORDER)] and sleeps == []


def test_sku_report_flow_survives_429_on_every_read_without_a_second_order(harness, wire):  # noqa: F811
    """Whole ads_sku_daily day: campaigns → expense → order → status → download, 429 on each read."""
    install, _ = wire
    source = install(Source({PAGE.format(1): [429], EXPENSE: [429], STATUS: [429, 429], REPORT: [429]},
                            total=1, spend=True))
    e, out = run('ads_sku_daily', harness[3])
    actions = [d['detail']['action'] for d in harness[2]]
    assert actions == ['SKU_COHORT', 'REPORT_INTENT', 'REPORT_SUBMITTED', 'SKU_BATCH_COMPLETE']
    assert [c for c in source.calls if c[0] == 'POST'] == [('POST', ORDER)]
    assert out['evidence']['complete'] is True and 'rate_limit' not in out['evidence']['state']['progress']
    rows = harness[0]['RAW_OZON_ADS_SKU_DAILY']
    assert len(rows) == 1 and next(iter(rows.values()))['sku'] == '555'
    assert sum(d['detail']['transport_retries'] for d in harness[2]) == 5
    assert all(d['detail']['logical_requests'] == n for d, n in zip(harness[2], [2, 0, 1, 2]))


def test_exhausted_status_retry_keeps_the_report_in_poll_without_reordering(harness, wire):  # noqa: F811
    install, sleeps = wire
    source = install(Source({STATUS: [429] * 5}, total=1, spend=True))
    e, out = run('ads_sku_daily', harness[3])
    last = harness[2][-1]
    assert last['detail']['action'] == 'SOURCE_THROTTLED' and last['detail']['submission_rejected'] is False
    assert last['state']['progress']['report']['phase'] == 'POLL'
    assert [c for c in source.calls if c[0] == 'POST'] == [('POST', ORDER)]
    assert [p for _, p in source.calls].count(STATUS) == 5


def test_seller_429_backoff_is_unchanged(wire, monkeypatch):
    install, sleeps = wire
    assert C.PERF_429_RETRIES < len(C.BACKOFF) and C.BACKOFF == [3, 6, 12, 24, 48]
    assert not C._perf_429_retryable(urllib.request.Request(C.SELLER + '/v3/posting/fbo/list', method='POST'))


def test_installed_image_driver_runs_network_free():
    """The exact-image qualification step (ozon-runtime.v15) executes this driver."""
    import os
    import subprocess
    import sys
    from pathlib import Path
    tests = Path(__file__).resolve().parent
    env = dict(os.environ, PYTHONPATH=str(tests.parent / 'runtime'),
               GCP_PROJECT_ID='offline-qualification-project', BQ_REF_DATASET='ref')
    out = subprocess.run([sys.executable, str(tests / '_perf_429_image_driver.py')], env=env,
                         capture_output=True, text=True, timeout=120, check=True)
    facts = json.loads(out.stdout.strip().splitlines()[-1])
    assert facts == {'performance_429_bounded_get_retry': 'PASS', 'performance_429_retry_paths': 4,
                     'performance_429_sku_day': 'PASS', 'report_order_never_repeated': 'PASS'}
