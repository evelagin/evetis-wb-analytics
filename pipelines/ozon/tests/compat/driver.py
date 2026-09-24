"""Драйвер различающего прогона Ozon runtime (Tenancy T2.2, L8). Не тест.

  python driver.py <каталог runtime> <сценарий>

Исполняет НЕИЗМЕНЁННЫЙ runtime (эталон до T2 или текущий) в отдельном процессе с
фальшивыми Secret Manager, BigQuery и Ozon API и печатает в stdout одну строку JSON —
полный наблюдаемый след:
  http      — метод, URL, тело, заголовки (значения учётных данных заменены на
              ИМЯ секрета, из которого они взяты);
  secrets   — какие секреты и в каком проекте запрошены;
  bq        — операции BigQuery: полный текст SQL, sha256 загружаемых данных,
              строки журнала OZON_INGESTION_RUNS (без меток времени);
  stdout    — строки лога runtime; stderr — трассировка необработанного исключения;
  exit      — код выхода, как у процесса Cloud Run.
Время, id прогона и слот акций заморожены; сна нет. Сети и облака нет.
"""
from __future__ import annotations

import hashlib
import io
import json
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from scenarios import SCENARIOS, FakeOzon, secrets_for  # noqa: E402

FIXED_NOW = datetime(2026, 9, 24, 10, 0, 0, tzinfo=timezone(timedelta(hours=3)))
SCHEMA_COLS = ["snapshot_date", "sku", "offer_id", "posting_number", "accrual_id", "type_id", "date",
               "campaign_id", "warehouse_id", "order_id", "supply_id", "bundle_id", "amount_rub",
               "extracted_at", "ingestion_run_id", "source_payload_hash", "status", "observation_id",
               "action_id", "product_id", "snapshot_ts", "sale_scheme", "commission_component",
               "value_num", "attributed_spend_rub"]


def _install_cloud_stubs():
    class Field:
        def __init__(self, name):
            self.name = name

    class Table:
        def __init__(self, ref, schema=None):
            self.ref, self.schema, self.expires = ref, schema, None

    g, c = types.ModuleType("google"), types.ModuleType("google.cloud")
    b, s = types.ModuleType("google.cloud.bigquery"), types.ModuleType("google.cloud.secretmanager")
    b.Client, b.Table, b.SchemaField = object, Table, Field
    b.LoadJobConfig = lambda **kw: kw
    b.SourceFormat = types.SimpleNamespace(NEWLINE_DELIMITED_JSON="NDJSON")
    b.WriteDisposition = types.SimpleNamespace(WRITE_APPEND="APPEND", WRITE_TRUNCATE="TRUNC")
    b.CreateDisposition = types.SimpleNamespace(CREATE_NEVER="NEVER")
    s.SecretManagerServiceClient = object
    c.bigquery, c.secretmanager, g.cloud = b, s, c
    sys.modules.update({"google": g, "google.cloud": c, "google.cloud.bigquery": b,
                        "google.cloud.secretmanager": s})
    return Field


def main(runtime_dir: str, scenario_name: str) -> dict:
    scenario = SCENARIOS[scenario_name]
    values = secrets_for(scenario)
    name_of = {v: n for n, v in values.items()}
    trace = {"http": [], "secrets": [], "bq": [], "stdout": [], "stderr": "", "exit": None}
    field = _install_cloud_stubs()
    sys.path.insert(0, runtime_dir)
    import common as C

    class FakeSecretManager:
        def access_secret_version(self, request):
            trace["secrets"].append(request["name"])
            name = request["name"].split("/secrets/")[1].split("/")[0]
            return types.SimpleNamespace(payload=types.SimpleNamespace(data=values[name].encode("utf-8")))

    class Job:
        errors = None

        def __init__(self, rows=None, job_id="job"):
            self.rows, self.job_id = rows or [], job_id

        def result(self):
            return self.rows

    class FakeBigQuery:
        def get_table(self, ref):
            trace["bq"].append(["get_table", ref])
            return types.SimpleNamespace(schema=[field(c) for c in SCHEMA_COLS])

        def delete_table(self, ref, not_found_ok=False):
            trace["bq"].append(["delete_table", ref])

        def create_table(self, table):
            trace["bq"].append(["create_table", table.ref])

        def load_table_from_file(self, f, ref, job_config=None, location=None, job_id=None):
            data = f.read()
            trace["bq"].append(["load", ref, job_id, hashlib.sha256(data).hexdigest(),
                                len(data.splitlines())])
            return Job(job_id=job_id or "job")

        def query(self, q, location=None):
            trace["bq"].append(["query", q])
            return Job([{"c": 0}] if "COUNT(*)" in q else [])

        def insert_rows_json(self, ref, rows):
            trace["bq"].append(["insert", ref, [{k: v for k, v in r.items()
                                                 if k not in ("started_at", "completed_at")}
                                                for r in rows]])
            if scenario.get("journal") == "fail":
                raise RuntimeError("BigQuery insertAll 503 backendError: journal unavailable")

    ozon = FakeOzon(scenario)

    def fake_request(req, attempt=0, raw_text=False):
        headers = {k: v for k, v in req.header_items()}
        body = req.data.decode() if req.data else None
        shown_headers = {k: (f"<secret:{name_of[v]}>" if v in name_of else
                             ("<bearer:performance_token>" if v.startswith("Bearer ") else v))
                         for k, v in sorted(headers.items())}
        shown_body = body
        for v, n in name_of.items():
            if shown_body:
                shown_body = shown_body.replace(v, f"<secret:{n}>")
        trace["http"].append([req.get_method(), req.full_url, shown_headers, shown_body])
        return ozon(req.full_url, headers, body)

    C._sm, C._bq, C._request = FakeSecretManager(), FakeBigQuery(), fake_request
    C.now_msk = lambda: FIXED_NOW
    import entities as E
    import promo as P
    import main as M
    for mod in (E, P):
        if hasattr(mod, "now_msk"):
            mod.now_msk = lambda: FIXED_NOW
    P.promo_slot = lambda now=None: "2026-09-24T04:00"
    E.time.sleep = lambda *_: None

    out, err = io.StringIO(), io.StringIO()
    real_out, real_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err
    try:
        M.main()
        code = 0
    except SystemExit as e:
        code = e.code
    except BaseException as e:                                  # noqa: BLE001
        # Как интерпретатор: необработанное исключение уходит в sys.excepthook.
        # У эталона это стандартный хук, у текущего runtime — safe_excepthook.
        sys.excepthook(type(e), e, e.__traceback__)
        code = 1
    finally:
        sys.stdout, sys.stderr = real_out, real_err
    # Строго по «\n»: splitlines() резал бы и по U+2028 и другим разделителям Юникода.
    trace["stdout"] = [line for line in out.getvalue().split("\n") if line]
    trace["stderr"] = err.getvalue()
    trace["exit"] = code
    # Откуда реально импортирован сравниваемый код: эталон и текущий runtime не
    # должны делить ни одного модуля (иначе прогон сравнивал бы код сам с собой).
    trace["modules"] = {m.__name__: str(Path(m.__file__).resolve()) for m in (C, E, P, M)}
    return trace


if __name__ == "__main__":
    result = main(sys.argv[1], sys.argv[2])
    sys.__stdout__.write(json.dumps(result, ensure_ascii=False, sort_keys=True, default=str) + "\n")
