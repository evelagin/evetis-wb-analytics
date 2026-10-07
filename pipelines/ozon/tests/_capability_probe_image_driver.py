"""Exact installed-image probe qualification; no clients, credentials or network."""
import json
import sys
from datetime import date
from types import SimpleNamespace

sys.path.insert(0, "/app")
import common as C
import lifecycle as LC
import entities as E
import backfill as F
from control_store import ControlStore


def qualify():
    today = date(2026, 10, 5)
    retained = [{"snapshot_date": today, "sku": None}, {"snapshot_date": today, "sku": "92"}]
    calls = []
    def list_rows(ref, **kwargs):
        assert [f.name for f in kwargs["selected_fields"]] == ["snapshot_date", "sku"]
        assert kwargs["max_results"] == E.PRODUCT_LIST_LIMIT
        return retained
    store = ControlStore(SimpleNamespace(list_rows=list_rows), C.PROJECT, {"ozon_raw": C.DATASET})
    ctx = SimpleNamespace(today_msk=today, store=store)
    def call(path, body):
        calls.append((path, body))
        return 200, {"items": []}
    C.seller_post = call
    assert LC.stocks_probe(ctx)[:2] == ("AVAILABLE", 200)
    assert calls == [("/v1/analytics/stocks", {"skus": ["92"]})]
    retained.clear()
    assert LC.stocks_probe(ctx)[:2] == ("UNKNOWN", None) and len(calls) == 1
    LC.supplies_probe()
    probe = calls[-1]
    class Captured(Exception):
        pass
    def capture(path, body):
        calls.append((path, body))
        raise Captured()
    engine = object.__new__(F.Engine)
    engine.p = {"order_batch": 5}
    engine.order_batches = 0
    engine.max_order_batches = 1
    engine.call = capture
    try:
        engine.supplies({"progress": {"bundle": None, "pending": [], "cursor": ""}})
    except Captured:
        pass
    else:
        raise AssertionError("runtime body was not captured")
    assert probe[0] == calls[-1][0] == "/v3/supply-order/list"
    assert probe[1] == dict(calls[-1][1], limit=1)
    print(json.dumps({"capability_probe_contract": "PASS", "stocks_empty_input": "UNKNOWN_NO_CALL",
                      "supplies_runtime_shape": "PASS", "network": "NONE", "credential_reads": 0}))


if __name__ == "__main__":
    qualify()
