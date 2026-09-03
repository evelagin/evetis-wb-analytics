#!/usr/bin/env python3
"""Runtime ingestion домена Ozon — общий слой.

Изоляция маркетплейсов: ничего из wb_raw и wb_mart не читается и не пишется.
Идентификаторы товаров резолвятся только через evetis_ref.REF_SKU_CHANNEL_MAP.

Секреты живут в памяти процесса. Токен Performance API эфемерный: не сохраняется,
не логируется, не коммитится.
"""
import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from google.cloud import bigquery, secretmanager

MSK = timezone(timedelta(hours=3))
PROJECT = os.environ.get("GCP_PROJECT_ID", "project-fa311fc0-4d87-4781-986")
DATASET = os.environ.get("BQ_RAW_DATASET", "ozon_raw")
LOCATION = os.environ.get("BQ_LOCATION", "EU")
RUNS_TABLE = "OZON_INGESTION_RUNS"

SELLER = "https://api-seller.ozon.ru"
PERF = "https://api-performance.ozon.ru"
BACKOFF = [3, 6, 12, 24, 48]

_secrets = {}
_bq = None
_perf_token = {"value": None, "at": 0}
STATS = {"requests": 0, "retries": 0}


def log(**kw):
    """Структурный лог. Секреты и токены сюда не попадают по построению."""
    print(json.dumps(kw, ensure_ascii=False, default=str), flush=True)


_sm = None


def secret(name):
    """Значение секрета из Secret Manager. В логи и на диск не попадает."""
    global _sm
    if name not in _secrets:
        if _sm is None:
            _sm = secretmanager.SecretManagerServiceClient()
        path = f"projects/{PROJECT}/secrets/{name}/versions/latest"
        _secrets[name] = _sm.access_secret_version(
            request={"name": path}).payload.data.decode("utf-8").strip()
    return _secrets[name]


def bq():
    global _bq
    if _bq is None:
        _bq = bigquery.Client(project=PROJECT, location=LOCATION)
    return _bq


def now_msk():
    return datetime.now(MSK)


def h(*parts):
    return hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:32]


# ------------------------------------------------------------------ HTTP
def _request(req, attempt=0, raw_text=False):
    STATS["requests"] += 1
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            body = r.read()
            return r.status, (body.decode("utf-8") if raw_text else json.loads(body))
    except urllib.error.HTTPError as e:
        payload = e.read().decode("utf-8", "replace")
        if e.code in (429, 500, 502, 503, 504) and attempt < len(BACKOFF):
            STATS["retries"] += 1
            time.sleep(BACKOFF[attempt])
            return _request(req, attempt + 1, raw_text)
        return e.code, {"_error": payload[:400]}
    except Exception as e:                                        # SSL, таймаут, обрыв
        if attempt < len(BACKOFF):
            STATS["retries"] += 1
            time.sleep(BACKOFF[attempt])
            return _request(req, attempt + 1, raw_text)
        return "NET_ERROR", {"_error": repr(e)[:300]}


def seller_post(path, body):
    req = urllib.request.Request(
        SELLER + path, data=json.dumps(body).encode(),
        headers={"Client-Id": secret("EVETIS_OZON_CLIENT_ID"),
                 "Api-Key": secret("EVETIS_OZON_API_KEY"),
                 "Content-Type": "application/json"})
    return _request(req)


def perf_token():
    """Эфемерный токен Performance API. Живёт 1800 с, обновляем каждые 25 минут."""
    if _perf_token["value"] and time.time() - _perf_token["at"] < 1500:
        return _perf_token["value"]
    body = json.dumps({"client_id": secret("EVETIS_OZON_PERFORMANCE_CLIENT_ID"),
                       "client_secret": secret("EVETIS_OZON_PERFORMANCE_CLIENT_SECRET"),
                       "grant_type": "client_credentials"}).encode()
    req = urllib.request.Request(PERF + "/api/client/token", data=body,
                                 headers={"Content-Type": "application/json"})
    code, d = _request(req)
    if code != 200:
        raise RuntimeError("не удалось получить токен Performance API")
    _perf_token["value"] = d["access_token"]
    _perf_token["at"] = time.time()
    return _perf_token["value"]


def perf_get(path, raw_text=True):
    req = urllib.request.Request(PERF + path,
                                 headers={"Authorization": f"Bearer {perf_token()}"})
    return _request(req, raw_text=raw_text)


def perf_post(path, body):
    req = urllib.request.Request(
        PERF + path, data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {perf_token()}",
                 "Content-Type": "application/json"})
    return _request(req)


# ------------------------------------------------------- загрузка в BigQuery
def merge_rows(table, rows, keys, run_id):
    """Идемпотентная запись: staging → MERGE по логическому ключу → удаление staging.

    Повторный прогон на том же окне не создаёт дублей и не удваивает суммы.
    """
    if not rows:
        return {"received": 0, "inserted": 0, "updated": 0}
    client = bq()
    tgt = client.get_table(f"{PROJECT}.{DATASET}.{table}")
    cols = [f.name for f in tgt.schema]
    staging = f"_rt_{table}_{run_id.replace('-', '')[:10]}"

    cfg = bigquery.LoadJobConfig(
        source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
        schema=tgt.schema, write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
        create_disposition=bigquery.CreateDisposition.CREATE_IF_NEEDED)
    def _clean(v):
        # BigQuery NUMERIC принимает не более 9 знаков после запятой, а repr(float)
        # даёт артефакты вида 2000.3700000000001 — округляем до 4 знаков.
        return round(v, 4) if isinstance(v, float) else v

    data = "\n".join(
        json.dumps({k: _clean(r.get(k)) for k in cols}, ensure_ascii=False, default=str)
        for r in rows).encode()
    import io
    job = client.load_table_from_file(io.BytesIO(data), f"{PROJECT}.{DATASET}.{staging}",
                                      job_config=cfg, location=LOCATION)
    job.result()
    if job.errors:
        raise RuntimeError(f"load job {job.job_id}: {job.errors}")

    on = " AND ".join(
        f"COALESCE(CAST(T.{k} AS STRING),'\\x00')=COALESCE(CAST(S.{k} AS STRING),'\\x00')"
        for k in keys)
    setter = ", ".join(f"T.{c}=S.{c}" for c in cols if c not in keys)
    q = (f"MERGE `{PROJECT}.{DATASET}.{table}` T USING "
         f"(SELECT * EXCEPT(_rn) FROM (SELECT *, ROW_NUMBER() OVER "
         f"(PARTITION BY {','.join(keys)} ORDER BY extracted_at DESC) _rn "
         f"FROM `{PROJECT}.{DATASET}.{staging}`) WHERE _rn=1) S ON {on} "
         f"WHEN MATCHED THEN UPDATE SET {setter} "
         f"WHEN NOT MATCHED THEN INSERT ({','.join(cols)}) "
         f"VALUES ({','.join('S.'+c for c in cols)})")
    before = list(client.query(f"SELECT COUNT(*) c FROM `{PROJECT}.{DATASET}.{table}`",
                               location=LOCATION).result())[0]["c"]
    m = client.query(q, location=LOCATION)
    m.result()
    after = list(client.query(f"SELECT COUNT(*) c FROM `{PROJECT}.{DATASET}.{table}`",
                              location=LOCATION).result())[0]["c"]
    client.delete_table(f"{PROJECT}.{DATASET}.{staging}", not_found_ok=True)
    return {"received": len(rows), "inserted": after - before,
            "updated": len(rows) - (after - before)}


def record_run(run_id, entity, started, src_from, src_to, res, status,
               error=None, requests_n=0, retries=0):
    row = {"ingestion_run_id": run_id, "marketplace": "OZON", "entity": entity,
           "started_at": started.isoformat(), "completed_at": now_msk().isoformat(),
           "source_from": str(src_from), "source_to": str(src_to),
           "requests": requests_n, "rows_received": res.get("received", 0),
           "rows_inserted": res.get("inserted", 0), "rows_updated": res.get("updated", 0),
           "errors": 0 if status == "OK" else 1, "retry_count": retries,
           "status": status, "error_message": (str(error)[:400] if error else None),
           "job_execution": os.environ.get("CLOUD_RUN_EXECUTION")}
    bq().insert_rows_json(f"{PROJECT}.{DATASET}.{RUNS_TABLE}", [row])
    log(event="entity_done", **{k: row[k] for k in
        ("entity", "status", "rows_received", "rows_inserted", "rows_updated",
         "source_from", "source_to", "requests", "retry_count")})
