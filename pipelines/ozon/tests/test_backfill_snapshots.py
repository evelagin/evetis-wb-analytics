"""Current-only snapshots: bounded resume, exact source grain, no fake history."""
from datetime import datetime, timezone
import copy
import pytest
import backfill as F
import backfill_core as B
import common as C
from test_backfill_engine import harness, engine, plan


@pytest.fixture(autouse=True)
def observation_day(monkeypatch):
    monkeypatch.setattr(C, 'now_msk', lambda: datetime(2026, 10, 4, 12, tzinfo=B.MSK))
    monkeypatch.setattr(F, 'internal_preflight', lambda *a, **k: {'catalog': 'PASS'})


def test_price_pages_resume_without_repeating_offers_and_retain_commission_grain(harness, monkeypatch):
    db, writes, proofs, persist = harness
    requests = []
    def source(path, body):
        assert path == '/v5/product/info/prices'
        requests.append(copy.deepcopy(body))
        assert body['limit'] == 100 and body['filter']['visibility'] == 'ALL'
        offer = 'offer-a' if body['cursor'] == '' else 'offer-b'
        return 200, {'total': 2, 'items': [{'offer_id': offer, 'product_id': 7, 'price': {'price': '100'}, 'commissions': {}}],
                     'cursor': 'second' if body['cursor'] == '' else ''}
    monkeypatch.setattr(C, 'seller_post', source)
    p = plan('prices'); first = engine(p, unit_budget=1).run(persist)['evidence']['state']
    assert not first['complete'] and len(db['RAW_OZON_PRICES']) == 1
    final = engine(p, first).run(persist)['evidence']['state']
    assert final['complete'] and len(db['RAW_OZON_PRICES']) == 2
    assert len(db['RAW_OZON_PRICE_COMMISSIONS']) == 2
    assert [r['cursor'] for r in requests] == ['', 'second']
    assert all(r['snapshot_date'] == '2026-10-04' for r in db['RAW_OZON_PRICES'].values())
    assert sum(e['detail']['tables']['RAW_OZON_PRICES'] for e in proofs) == 2


@pytest.mark.parametrize('bad', ['duplicate', 'total_change', 'nonprogress'])
def test_price_source_contradiction_never_marks_complete_or_writes_bad_page(harness, monkeypatch, bad):
    db, writes, proofs, persist = harness
    def source(path, body):
        if not body['cursor']:
            return 200, {'total': 2, 'items': [{'offer_id': 'a', 'product_id': 1}], 'cursor': 'next'}
        if bad == 'duplicate': return 200, {'total': 2, 'items': [{'offer_id': 'a', 'product_id': 1}], 'cursor': ''}
        if bad == 'total_change': return 200, {'total': 3, 'items': [{'offer_id': 'b', 'product_id': 2}], 'cursor': 'next2'}
        return 200, {'total': 2, 'items': [], 'cursor': 'next'}
    monkeypatch.setattr(C, 'seller_post', source)
    p = plan('prices'); state = engine(p, unit_budget=1).run(persist)['evidence']['state']
    before = copy.deepcopy(db)
    with pytest.raises(B.EvidenceError): engine(p, state).run(persist)
    assert db == before and not state['complete']


def test_stock_cursor_is_catalog_key_and_never_sends_empty_sku(harness, monkeypatch):
    db, writes, proofs, persist = harness
    pages = {'': ['100', '101'], '101': ['102'], '102': []}; requests = []
    monkeypatch.setattr(F.Engine, 'stock_catalog_page', lambda self, day, last: pages[last])
    def source(path, body):
        assert path == '/v1/analytics/stocks' and body['skus']
        requests.append(body['skus'])
        return 200, {'items': [{'sku': sku, 'warehouse_id': 8, 'available_stock_count': 0} for sku in body['skus']]}
    monkeypatch.setattr(C, 'seller_post', source)
    p = plan('stocks'); state = engine(p, unit_budget=1).run(persist)['evidence']['state']
    assert not state['complete'] and state['progress']['last_sku'] == '101'
    final = engine(p, state).run(persist)['evidence']['state']
    assert final['complete'] and requests == [['100', '101'], ['102']]
    assert len(db['RAW_OZON_STOCKS']) == 3
    assert final['progress']['skus'] == 3


def test_stock_response_must_belong_to_exact_requested_catalog_cohort(harness, monkeypatch):
    db, _, _, persist = harness
    monkeypatch.setattr(F.Engine, 'stock_catalog_page', lambda *a: ['100'])
    monkeypatch.setattr(C, 'seller_post', lambda *a: (200, {'items': [{'sku': '999', 'warehouse_id': 1}]}))
    with pytest.raises(B.EvidenceError, match='outside exact'): engine(plan('stocks')).run(persist)
    assert not db


def test_seller_info_reuses_projection_that_excludes_legal_identity(harness, monkeypatch):
    db, _, proofs, persist = harness
    def source(path, body):
        assert body == {}
        if path == '/v1/seller/info':
            return 200, {'company': {'inn': 'NEVER_STORE_INN', 'ogrn': 'NEVER_STORE_OGRN', 'currency': 'RUB'}, 'subscription': {}}
        assert path == '/v1/rating/summary'
        return 200, {'premium': False}
    monkeypatch.setattr(C, 'seller_post', source)
    result = engine(plan('seller_info')).run(persist)
    assert result['evidence']['complete'] and len(db['RAW_OZON_SELLER_INFO']) == 1
    assert 'NEVER_STORE' not in str(db) and 'NEVER_STORE' not in str(proofs)


def test_clusters_source_missing_list_cannot_become_zero_row_complete(harness, monkeypatch):
    db, _, _, persist = harness
    monkeypatch.setattr(C, 'seller_post', lambda *a: (200, {}))
    with pytest.raises(B.EvidenceError, match='missing source list'): engine(plan('clusters')).run(persist)
    assert not db


def test_snapshot_rollover_stops_before_source_or_writes(harness, monkeypatch):
    db, _, _, persist = harness
    p = plan('seller_info')
    monkeypatch.setattr(C, 'now_msk', lambda: datetime(2026, 10, 5, 12, tzinfo=B.MSK))
    monkeypatch.setattr(C, 'seller_post', lambda *a: pytest.fail('stale snapshot source call'))
    with pytest.raises(B.EvidenceError, match='stale'): engine(p).run(persist)
    assert not db
