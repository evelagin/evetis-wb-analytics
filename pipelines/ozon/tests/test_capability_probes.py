"""Network-free capability inputs match the reviewed, bounded runtime reads."""
from datetime import date
from types import SimpleNamespace

import pytest

import backfill as F
import common as C
import lifecycle as LC
from control_store import ControlStore

TODAY = date(2026, 10, 5)


def context(rows, entities=("stocks",)):
    class Store:
        appended = []

        def rows(self, key, table, **kwargs):
            assert (key, table) == ("ozon_raw", "RAW_OZON_CATALOG")
            assert [f.name for f in kwargs["selected_fields"]] == ["snapshot_date", "sku"]
            assert kwargs["max_results"] == 1000
            return iter(rows[:kwargs["max_results"]])

        def append(self, table, values):
            self.appended.append((table, values))

    return SimpleNamespace(today_msk=TODAY, store=Store(), entities=entities, ads=False,
                           run_id="ctl-offline", now=__import__("datetime").datetime(2026, 10, 5))


def test_stocks_uses_one_current_real_positive_sku_and_runtime_body(entities, monkeypatch):
    rows = [{"snapshot_date": "2026-10-04", "sku": "91"},
            {"snapshot_date": TODAY, "sku": None}, {"snapshot_date": TODAY, "sku": "0"},
            {"snapshot_date": TODAY, "sku": "92"}]
    calls = []
    def source(path, body):
        assert path == "/v1/analytics/stocks" and body == {"skus": ["92"]}
        calls.append((path, body))
        return 200, {"items": []}
    monkeypatch.setattr(C, "seller_post", source)
    status, http, evidence = LC.stocks_probe(context(rows))
    assert (status, http) == ("AVAILABLE", 200)
    assert evidence == {"probe_input": "CURRENT_RETAINED_CATALOG_SKU", "sku_count": 1}
    assert "92" not in str(evidence)
    monkeypatch.setattr(entities, "_product_list_items", lambda: [{"sku": "92"}])
    monkeypatch.setattr(entities, "seller_post", source)
    monkeypatch.setattr(entities, "merge_rows", lambda *a, **k: {})
    entities.stocks("offline", "2026-10-05T00:00:00Z", None, None)
    assert calls[0] == calls[1]


@pytest.mark.parametrize("rows", [[], [{"snapshot_date": TODAY, "sku": None}],
    [{"snapshot_date": TODAY, "sku": 0}], [{"snapshot_date": "2026-10-04", "sku": "91"}],
    [{"snapshot_date": "2026-10-04", "sku": None}] * 1000 + [{"snapshot_date": TODAY, "sku": "92"}]])
def test_missing_or_bounded_out_input_never_calls_invalid_probe(rows, monkeypatch):
    monkeypatch.setattr(C, "seller_post", lambda *a: pytest.fail("must not send empty/fake SKU"))
    status, http, evidence = LC.stocks_probe(context(rows))
    assert (status, http) == ("UNKNOWN", None)
    assert evidence["sku_count"] == 0


@pytest.mark.parametrize("http,expected", [(200, "AVAILABLE"), (403, "DENIED"), (400, "UNAVAILABLE")])
def test_valid_stocks_probe_preserves_actual_source_result(http, expected, monkeypatch):
    monkeypatch.setattr(C, "seller_post", lambda p, b: (http, {}))
    assert LC.stocks_probe(context([{"snapshot_date": TODAY, "sku": "92"}]))[:2] == (expected, http)


def test_stocks_transport_denial_cannot_become_availability(monkeypatch):
    def denied(*a):
        raise C.ApiPathDenied("offline denial")
    monkeypatch.setattr(C, "seller_post", denied)
    with pytest.raises(C.ApiPathDenied):
        LC.stocks_probe(context([{"snapshot_date": TODAY, "sku": "92"}]))


def test_supplies_one_page_matches_actual_bounded_runtime_contract(entities, monkeypatch):
    captured = []
    monkeypatch.setattr(C, "seller_post", lambda p, b: (captured.append((p, b)) or (200, {})))
    assert LC.supplies_probe()[0] == 200
    probe_path, probe_body = captured[0]
    class Captured(Exception):
        pass
    def call(path, body):
        captured.append((path, body))
        raise Captured()
    engine = object.__new__(F.Engine)
    engine.p = {"order_batch": 5}
    engine.order_batches = 0
    engine.max_order_batches = 1
    engine.call = call
    state = {"progress": {"bundle": None, "pending": [], "cursor": ""}}
    with pytest.raises(Captured):
        engine.supplies(state)
    runtime_path, runtime_body = captured[1]
    assert probe_path == runtime_path == "/v3/supply-order/list"
    assert probe_body == dict(runtime_body, limit=1)
    assert probe_body["filter"]["states"] == entities.CPC_STATES
    assert probe_body["sort_by"] == "ORDER_CREATION" and probe_body["sort_dir"] == "DESC"
    assert probe_body["last_id"] == ""


def test_discovery_records_missing_stock_input_as_unknown_without_false_http(monkeypatch):
    ctx = context([])
    monkeypatch.setattr(LC, "require_state", lambda *a: None)
    monkeypatch.setattr(LC, "require_bound", lambda *a: ({}, {}))
    monkeypatch.setattr(C, "seller_post", lambda p, b: (200, {"result": {"postings": []}})
                        if p == "/v3/posting/fbs/list" else pytest.fail("unexpected call"))
    monkeypatch.setattr(C, "log", lambda **k: None)
    assert LC.cmd_discover(ctx) == 0
    stock = next(r for _, rs in ctx.store.appended for r in rs if r["capability"] == "stocks")
    assert stock["status"] == "UNKNOWN" and stock["http_status"] is None
    assert stock["evidence_kind"] == "INFERRED"


def test_catalog_input_uses_only_bounded_projected_tables_api():
    from google.cloud import bigquery
    calls = []
    fields = [bigquery.SchemaField("snapshot_date", "DATE"), bigquery.SchemaField("sku", "STRING")]
    client = SimpleNamespace(list_rows=lambda ref, **kw: (calls.append((ref, kw)) or []))
    store = ControlStore(client, "synthetic-project", {"ozon_raw": "ozon_raw"})
    assert list(store.rows("ozon_raw", "RAW_OZON_CATALOG", selected_fields=fields, max_results=1000)) == []
    assert calls == [("synthetic-project.ozon_raw.RAW_OZON_CATALOG",
                     {"selected_fields": fields, "max_results": 1000})]
