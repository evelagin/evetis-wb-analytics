"""Exact installed runtime, no network: bounded HTTP 429 retry on idempotent Performance reads.

Drives the real transport (common._request) and the real engine over a fake socket.
Proves: campaign pages, single-day statistics, report status and report download are
retried at most four times; the report order POST is never repeated; a whole
ads_sku_daily day completes with 429 on every read and exactly one POST.
"""
import io
import json
import socket
import sys
import urllib.error
import urllib.request
from datetime import date

sys.path.insert(0, '/app')
import backfill_core as B  # noqa: E402
import backfill as F  # noqa: E402
import common as C  # noqa: E402


def denied(*a, **kw):
    raise AssertionError('network/source/persistence denied')


socket.socket = socket.create_connection = denied
DAY = '2026-09-17'
PAGE = '/api/client/campaign?page={}&pageSize=100'
EXPENSE = f'/api/client/statistics/expense?dateFrom={DAY}&dateTo={DAY}'
DAILY = f'/api/client/statistics/daily?dateFrom={DAY}&dateTo={DAY}'
STATUS = '/api/client/statistics/synthetic-uuid'
REPORT = '/api/client/statistics/report?UUID=synthetic-uuid'
ORDER = '/api/client/statistics'


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


class Source:
    def __init__(self, script, total=1):
        self.script, self.total, self.calls = script, total, []

    def __call__(self, req, timeout=None):
        path = req.full_url[len(C.PERF):]
        self.calls.append((req.get_method(), path))
        queue = self.script.get(path)
        if queue:
            raise urllib.error.HTTPError(req.full_url, queue.pop(0), 'synthetic', {}, io.BytesIO(b'{}'))
        if path.startswith('/api/client/campaign?page='):
            return Response({'total': str(self.total), 'list': [{'id': 10000, 'advObjectType': 'SKU'}]})
        if path == EXPENSE:
            return Response('ID;Название;Расход;Расход бонусов;Расход с абонентского счета\n10000;synthetic;12,50;0;0\n')
        if path == ORDER:
            return Response({'UUID': 'synthetic-uuid'})
        if path == STATUS:
            return Response({'state': 'OK'})
        if path == REPORT:
            return Response('synthetic title\nДень;sku;Расход, ₽, с НДС;Показы;Клики\n17.09.2026;555;12,50;100;5\n')
        raise AssertionError('unexpected synthetic path')


sleeps = []
C.time.sleep = F.time.sleep = sleeps.append
C.perf_token = lambda: 'synthetic-token'
C.log = lambda **kw: None
C.secret = denied
C._seller_open = denied
C.record_run = denied

# 1. Transport: each idempotent read is retried, bounded by four retries.
for path in (PAGE.format(2), EXPENSE, DAILY, STATUS, REPORT):
    src = Source({path: [429, 429, 200]})
    C.urllib.request.urlopen = src
    status, _ = C.perf_get(path)
    assert status == 200 and len(src.calls) == 3, path
    src = Source({path: [429] * 6})
    C.urllib.request.urlopen = src
    status, _ = C.perf_get(path)
    assert status == 429 and len(src.calls) == 5, path
# 2. Report order: never repeated on 429, 5xx or a lost connection.
for failure in (429, 500, 503):
    src = Source({ORDER: [failure]})
    C.urllib.request.urlopen = src
    status, _ = C.perf_post(ORDER, {'campaigns': ['10000'], 'dateFrom': DAY, 'dateTo': DAY, 'groupBy': 'DATE'})
    assert status == failure and src.calls == [('POST', ORDER)]
# 3. Other reads keep the previous no-retry behaviour.
for path in ('/api/client/campaign', '/api/client/statistics/expense?dateFrom=2026-09-01&dateTo=2026-09-17'):
    src = Source({path: [429, 200]})
    C.urllib.request.urlopen = src
    status, _ = C.perf_get(path)
    assert status == 429 and len(src.calls) == 1, path

# 4. Whole ads_sku_daily day through the installed engine.
env = dict(BACKFILL_MODE=B.VERSION, TENANT_BINDING_REQUIRED='1', STRICT_PAGE_CAPS='1',
           BACKFILL_TARGET_PROJECT='synthetic-project', SINCE=DAY, UNTIL=DAY,
           BACKFILL_GENERATION='qualification', BACKFILL_ORIGIN='2026-10-04T00:00:00Z')
p = B.plan(env, 'ads_sku_daily', 'synthetic-project', 'ozon_raw', 'ref', date(2026, 10, 4))
written, proofs = {}, []


def merge(table, rows, keys, run_id, **kw):
    for row in rows:
        written.setdefault(table, {})[C.merge_key(row, keys)] = row
    return {'received': len(rows), 'inserted': len(rows), 'updated': 0}


C.merge_rows = merge
F.export_budget = lambda *a, **kw: 15
src = Source({PAGE.format(1): [429], EXPENSE: [429], STATUS: [429, 429], REPORT: [429]})
C.urllib.request.urlopen = src
engine = F.Engine(p, 'rt-synthetic-429', '2026-10-04T10:00:00+03:00', B.initial(p))
out = engine.run(lambda result, evidence: proofs.append(evidence))
actions = [x['detail']['action'] for x in proofs]
assert actions == ['SKU_COHORT', 'REPORT_INTENT', 'REPORT_SUBMITTED', 'SKU_BATCH_COMPLETE'], actions
assert [c for c in src.calls if c[0] == 'POST'] == [('POST', ORDER)]
assert out['evidence']['complete'] is True and len(written['RAW_OZON_ADS_SKU_DAILY']) == 1
assert sum(x['detail']['transport_retries'] for x in proofs) == 5
assert max(sleeps) <= C.BACKOFF[C.PERF_429_RETRIES - 1]
print(json.dumps(dict(performance_429_bounded_get_retry='PASS', performance_429_retry_paths=len(C.PERF_429_RETRY_GET),
                      report_order_never_repeated='PASS', performance_429_sku_day='PASS'), sort_keys=True))
