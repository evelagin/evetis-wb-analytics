"""Драйвер доказательства переносимости (Tenancy T2). Не тест: запускается подпроцессом.

Импортирует НЕИЗМЕНЁННЫЙ runtime (common, entities, promo) с тем окружением,
которое ему передали, прогоняет сущности через фальшивые Secret Manager, BigQuery
и HTTP и печатает JSON: какие секреты (путь в Secret Manager) запрошены, в какие
таблицы шла запись, какие хосты вызывались. Никакой сети и облака.

Облачные библиотеки подменяются заглушками ДО импорта runtime, так же как в
conftest.py; поведение runtime от этого не меняется — фальшивые клиенты
подставляются в те же точки (common._sm, common._bq, common._request).
"""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path

RUNTIME = Path(__file__).resolve().parents[1] / "runtime"


def _install_stubs():
    class _Field:
        def __init__(self, name):
            self.name = name

    class _Table:
        def __init__(self, ref, schema=None):
            self.ref, self.schema, self.expires = ref, schema, None

    g, c = types.ModuleType("google"), types.ModuleType("google.cloud")
    b, s = types.ModuleType("google.cloud.bigquery"), types.ModuleType("google.cloud.secretmanager")
    b.Client, b.Table, b.SchemaField = object, _Table, _Field
    b.LoadJobConfig = lambda **kw: kw
    b.SourceFormat = types.SimpleNamespace(NEWLINE_DELIMITED_JSON="NDJSON")
    b.WriteDisposition = types.SimpleNamespace(WRITE_APPEND="APPEND")
    b.CreateDisposition = types.SimpleNamespace(CREATE_NEVER="NEVER")
    s.SecretManagerServiceClient = object
    c.bigquery, c.secretmanager, g.cloud = b, s, c
    sys.modules.update({"google": g, "google.cloud": c, "google.cloud.bigquery": b,
                        "google.cloud.secretmanager": s})
    return _Field


def main():
    field_cls = _install_stubs()
    sys.path.insert(0, str(RUNTIME))
    import common as C
    import entities as E
    import promo as P

    observed = {"secret_paths": [], "bq_objects": [], "hosts": [], "headers_present": []}

    class FakeSM:
        def access_secret_version(self, request):
            observed["secret_paths"].append(request["name"])
            payload = types.SimpleNamespace(data=b"synthetic-credential-value")
            return types.SimpleNamespace(payload=payload)

    class _Job:
        errors = None
        job_id = "job"

        def __init__(self, rows=None):
            self.rows = rows or []

        def result(self):
            return self.rows

    class FakeBQ:
        def get_table(self, ref):
            observed["bq_objects"].append(("get_table", ref))
            return types.SimpleNamespace(schema=[field_cls(n) for n in (
                "snapshot_date", "retrieved_at", "seller_id", "extracted_at")])

        def delete_table(self, ref, not_found_ok=False):
            observed["bq_objects"].append(("delete_table", ref))

        def create_table(self, table):
            observed["bq_objects"].append(("create_table", table.ref))

        def load_table_from_file(self, _f, ref, **_k):
            observed["bq_objects"].append(("load", ref))
            return _Job()

        def query(self, q, **_k):
            import re
            for ref in re.findall(r"`([^`]+)`", q):
                observed["bq_objects"].append(("query", ref))
            return _Job([{"c": 0}] if "COUNT(*)" in q else [])

        def insert_rows_json(self, ref, _rows):
            observed["bq_objects"].append(("insert", ref))

    def fake_request(req, *_a, **_k):
        from urllib.parse import urlparse
        observed["hosts"].append(urlparse(req.full_url).netloc)
        observed["headers_present"].append(sorted(k for k in req.headers))
        if req.full_url.endswith("/api/client/token"):
            return 200, {"access_token": "synthetic-token"}
        if req.full_url.endswith("/v1/seller/info"):
            return 200, {"company": {"tax_system": "USN"}, "subscription": {}}
        if req.full_url.endswith("/v1/rating/summary"):
            return 200, {}
        if req.full_url.endswith("/api/client/campaign"):
            return 200, json.dumps({"list": [], "total": "0"})
        raise AssertionError(req.full_url)

    C._sm, C._bq, C._request = FakeSM(), FakeBQ(), fake_request

    E.seller_info("rt-portability", "2026-09-24T00:00:00+03:00", None, None)
    E.ads_campaigns("rt-portability", "2026-09-24T00:00:00+03:00", None, None)
    P._sku_maps()
    C.record_run("rt-portability", "seller_info", C.now_msk(), "d", "d",
                 {"received": 1}, "OK")

    observed["config"] = C.CONFIG._asdict()
    print(json.dumps(observed, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
