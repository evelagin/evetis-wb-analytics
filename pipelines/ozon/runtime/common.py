#!/usr/bin/env python3
"""Runtime ingestion домена Ozon — общий слой.

Изоляция маркетплейсов: ничего из wb_raw и wb_mart не читается и не пишется.
Идентификаторы товаров резолвятся только через evetis_ref.REF_SKU_CHANNEL_MAP.

Секреты живут в памяти процесса. Токен Performance API эфемерный: не сохраняется,
не логируется, не коммитится.
"""
import hashlib
import io
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
            # surrogateescape: отчёты Performance API приходят ZIP-архивом, строгий
            # utf-8 на них падает. Round-trip .encode("utf-8","surrogateescape")
            # в entities.py восстанавливает байты один в один.
            return r.status, (body.decode("utf-8", "surrogateescape")
                              if raw_text else json.loads(body))
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
# Страховка на случай, когда процесс убит между созданием staging и finally
# (таймаут Cloud Run, OOM): таблица исчезнет сама. Прогон длится минуты, сутки —
# с запасом. Датасетный TTL сознательно не трогаем: он действовал бы и на RAW.
STAGING_TTL = timedelta(hours=24)

# Режимы обработки строк источника с одинаковым ключом слияния.
#   collapse_identical — полностью одинаковые строки схлопываются, различающиеся
#                        роняют загрузку (по умолчанию);
#   reject             — любой повтор ключа роняет загрузку. Для финансов: две
#                        одинаковые строки там могут быть двумя реальными
#                        начислениями на одну сумму, схлопнуть их — потерять деньги.
DUPLICATE_KEY_MODES = ("collapse_identical", "reject")

# Тот же маркер NULL, что в условии ON оператора MERGE ниже.
_NULL_KEY = "\x00"


class MergeKeyConflictError(RuntimeError):
    """Несколько строк источника претендуют на один ключ слияния.

    Раньше такие строки молча схлопывались ROW_NUMBER() с недетерминированным
    выбором победителя. Теперь это явный отказ загрузки до создания staging.

    Сообщение уходит в лог и в OZON_INGESTION_RUNS.error_message, поэтому в нём
    нет ни значений полезной нагрузки, ни значений ключа (accrual_id, sku,
    posting_number…). Только таблица, имена колонок ключа, режим, число ключей и
    строк, имена расходящихся колонок и отпечаток ключа (merge_key_fingerprint).
    """


def merge_key_fingerprint(key):
    """Короткий детерминированный отпечаток нормализованного ключа слияния.

    SHA-256 от частей merge_key(), разделённых \\x1f; первые 12 hex-символов.
    Один и тот же ключ даёт один и тот же отпечаток в любом прогоне, поэтому по
    нему можно найти конфликт повторно, не записывая сами значения в лог.
    """
    return hashlib.sha256("\x1f".join(key).encode("utf-8")).hexdigest()[:12]


def _clean(v):
    # BigQuery NUMERIC принимает не более 9 знаков после запятой, а repr(float)
    # даёт артефакты вида 2000.3700000000001 — округляем до 4 знаков.
    return round(v, 4) if isinstance(v, float) else v


def _loaded_value(v):
    """Значение ровно в том виде, в каком оно уходит в staging."""
    return json.dumps(_clean(v), ensure_ascii=False, default=str, sort_keys=True)


def merge_key(row, keys):
    """Ключ слияния так, как его сравнивает MERGE: COALESCE(CAST(k AS STRING), '\\x00').

    Ключевые колонки Ozon — STRING, INT64 и DATE, в Python это str/int. Для них
    str() совпадает с CAST(... AS STRING), поэтому группировка здесь не мельче,
    чем в BigQuery: всё, что MERGE сочтёт одним ключом, здесь тоже один ключ.
    """
    return tuple(_NULL_KEY if row.get(k) is None else str(row.get(k)) for k in keys)


def validate_merge_batch(table, rows, keys, cols, on_duplicate_key="collapse_identical"):
    """Проверка партии ДО staging и MERGE. Возвращает число схлопнутых дублей.

    Сравниваются все сохраняемые колонки (`cols` — схема целевой таблицы).
    Технические extracted_at / ingestion_run_id / source_endpoint внутри одного
    вызова одинаковы, так что ложных конфликтов не дают, а схлопнутые строки
    побайтно равны по всему, что попадёт в таблицу.
    """
    if on_duplicate_key not in DUPLICATE_KEY_MODES:
        raise ValueError(f"неизвестный режим дублей: {on_duplicate_key!r}")
    groups = {}
    for r in rows:
        groups.setdefault(merge_key(r, keys), []).append(r)
    dups = {k: g for k, g in groups.items() if len(g) > 1}
    if not dups:
        return 0
    conflicts = []
    for k, g in dups.items():
        differing = [c for c in cols if len({_loaded_value(r.get(c)) for r in g}) > 1]
        if on_duplicate_key == "reject" or differing:
            conflicts.append((k, len(g), differing))
    if conflicts:
        shown = "; ".join(
            f"key_fp={merge_key_fingerprint(k)} rows={n} "
            f"differing_columns={','.join(d) or 'none (identical)'}"
            for k, n, d in conflicts[:5])
        raise MergeKeyConflictError(
            f"{table}: {len(conflicts)} ключ(ей) слияния ({','.join(keys)}) "
            f"с несколькими строками источника, строк {sum(n for _, n, _ in conflicts)}, "
            f"режим {on_duplicate_key}; первые: {shown}")
    return sum(len(g) - 1 for g in dups.values())


def _drop_staging(client, staging_id):
    """Удаление staging, которое никогда не бросает.

    Вызывается из finally: исключение отсюда заменило бы исходную ошибку
    загрузки. Сбой очистки логируется; остаток подчистит STAGING_TTL.
    """
    try:
        client.delete_table(staging_id, not_found_ok=True)
    except Exception as e:                                        # noqa: BLE001
        log(event="staging_cleanup_failed", staging=staging_id,
            error=f"{type(e).__name__}: {str(e)[:200]}",
            ttl_hours=STAGING_TTL.total_seconds() / 3600)


def merge_rows(table, rows, keys, run_id, on_duplicate_key="collapse_identical"):
    """Идемпотентная запись: проверка партии → staging → MERGE по ключу → удаление staging.

    Повторный прогон на том же окне не создаёт дублей и не удваивает суммы.
    Конфликт ключей роняет загрузку ДО создания staging (MergeKeyConflictError).
    Staging удаляется в finally при любом исходе; вторая линия — expires на самой
    staging-таблице.
    """
    if not rows:
        return {"received": 0, "inserted": 0, "updated": 0}
    client = bq()
    tgt = client.get_table(f"{PROJECT}.{DATASET}.{table}")
    cols = [f.name for f in tgt.schema]
    collapsed = validate_merge_batch(table, rows, keys, cols, on_duplicate_key)
    if collapsed:
        log(event="merge_identical_duplicates_collapsed", table=table, rows=collapsed)
    staging = f"_rt_{table}_{run_id.replace('-', '')[:10]}"
    staging_id = f"{PROJECT}.{DATASET}.{staging}"

    data = "\n".join(
        json.dumps({k: _clean(r.get(k)) for k in cols}, ensure_ascii=False, default=str)
        for r in rows).encode()
    on = " AND ".join(
        f"COALESCE(CAST(T.{k} AS STRING),'\\x00')=COALESCE(CAST(S.{k} AS STRING),'\\x00')"
        for k in keys)
    setter = ", ".join(f"T.{c}=S.{c}" for c in cols if c not in keys)
    # Текст MERGE не изменён. ROW_NUMBER остаётся: MERGE требует не более одной
    # строки источника на ключ. После validate_merge_batch строки одного ключа
    # равны по всем колонкам, поэтому выбор победителя ни на что не влияет.
    q = (f"MERGE `{PROJECT}.{DATASET}.{table}` T USING "
         f"(SELECT * EXCEPT(_rn) FROM (SELECT *, ROW_NUMBER() OVER "
         f"(PARTITION BY {','.join(keys)} ORDER BY extracted_at DESC) _rn "
         f"FROM `{PROJECT}.{DATASET}.{staging}`) WHERE _rn=1) S ON {on} "
         f"WHEN MATCHED THEN UPDATE SET {setter} "
         f"WHEN NOT MATCHED THEN INSERT ({','.join(cols)}) "
         f"VALUES ({','.join('S.'+c for c in cols)})")
    try:
        # Остаток с тем же именем (повтор с явным INGESTION_RUN_ID) убираем до
        # создания: иначе WRITE_APPEND дописал бы партию к чужим строкам.
        client.delete_table(staging_id, not_found_ok=True)
        table_obj = bigquery.Table(staging_id, schema=tgt.schema)
        table_obj.expires = datetime.now(timezone.utc) + STAGING_TTL
        client.create_table(table_obj)
        # Таблица только что создана пустой, поэтому APPEND эквивалентен прежнему
        # TRUNCATE. TRUNCATE не используем: сохраняет ли он expires, в документации
        # клиента не сказано.
        cfg = bigquery.LoadJobConfig(
            source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
            schema=tgt.schema, write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
            create_disposition=bigquery.CreateDisposition.CREATE_NEVER)
        job = client.load_table_from_file(io.BytesIO(data), staging_id,
                                          job_config=cfg, location=LOCATION)
        job.result()
        if job.errors:
            raise RuntimeError(f"load job {job.job_id}: {job.errors}")

        before = list(client.query(f"SELECT COUNT(*) c FROM `{PROJECT}.{DATASET}.{table}`",
                                   location=LOCATION).result())[0]["c"]
        m = client.query(q, location=LOCATION)
        m.result()
        after = list(client.query(f"SELECT COUNT(*) c FROM `{PROJECT}.{DATASET}.{table}`",
                                  location=LOCATION).result())[0]["c"]
    finally:
        _drop_staging(client, staging_id)
    return {"received": len(rows), "inserted": after - before,
            "updated": len(rows) - (after - before)}


# ─────────────────────────── append-only наблюдения (PR-PROMO-1)
# merge_rows выше схлопывает строки по ключу, содержащему snapshot_date: для
# суточных сущностей это правильно, для наблюдателя акций — губительно. Состав
# акции и акционные цены меняются ВНУТРИ суток, а наблюдений четыре в день, и
# каждое обязано остаться отдельной строкой.
#
# Поэтому здесь отдельный путь записи: WRITE_APPEND с ДЕТЕРМИНИРОВАННЫМ job_id.
# BigQuery дедуплицирует load-джобы по job_id, поэтому повтор того же слота не
# создаёт вторую копию строк, а новый слот (другой job_id) пишется всегда.
# Ровно та же механика, что у наблюдателя цен WB (cloud/src/loaders/prices/bq.ts).

# Часы запуска наблюдателя акций, UTC. Совпадают с расписанием в Terraform и с
# PROMO_SLOT_HOURS_UTC наблюдателя WB: два маркетплейса наблюдаются в одних слотах,
# иначе сравнивать снимки между площадками пришлось бы с поправкой на время.
PROMO_SLOT_HOURS_UTC = (4, 9, 14, 19)


def promo_slot(now=None):
    """Слот наблюдения YYYY-MM-DDTHH:00 (UTC) — ближайший предшествующий запуск.

    До первого слота суток относится к последнему слоту предыдущих суток.
    Ретрай внутри слота получает тот же идентификатор и не задваивает историю.
    """
    now = now or datetime.now(timezone.utc)
    now = now.astimezone(timezone.utc)
    past = [h for h in sorted(PROMO_SLOT_HOURS_UTC) if h <= now.hour]
    if past:
        return f"{now:%Y-%m-%d}T{past[-1]:02d}:00"
    prev = now - timedelta(days=1)
    return f"{prev:%Y-%m-%d}T{max(PROMO_SLOT_HOURS_UTC):02d}:00"


def promo_observation_id(environment, slot):
    """Детерминированный id снимка. Стабилен между попытками одного слота."""
    return "OZPROMO_%s_%s" % (environment, slot.replace("-", "").replace(":", "").replace("T", ""))


def promo_load_job_id(environment, slot, table):
    """Детерминированный job_id load-джобы: ключ идемпотентности записи."""
    t = "".join(c if c.isalnum() else "_" for c in table.lower()).strip("_")
    return "ozpromo_%s_%s_%s" % (environment, slot.replace("-", "").replace(":", "").replace("T", ""), t)


def append_rows(table, rows, job_id):
    """Append-only запись снимка. Возвращает ('LOADED'|'REUSED', число строк).

    REUSED означает, что load-джоба с таким job_id уже выполнялась: строки на
    месте, повтор их не задвоил. Это НЕ ошибка и НЕ повод переписывать историю.
    """
    if not rows:
        return "LOADED", 0
    client = bq()
    tgt = client.get_table(f"{PROJECT}.{DATASET}.{table}")
    cols = [f.name for f in tgt.schema]
    data = "\n".join(
        json.dumps({k: _clean(r.get(k)) for k in cols}, ensure_ascii=False, default=str)
        for r in rows).encode()
    cfg = bigquery.LoadJobConfig(
        source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
        schema=tgt.schema,
        write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
        create_disposition=bigquery.CreateDisposition.CREATE_NEVER)
    try:
        job = client.load_table_from_file(io.BytesIO(data), f"{PROJECT}.{DATASET}.{table}",
                                          job_config=cfg, location=LOCATION, job_id=job_id)
        job.result()
        if job.errors:
            raise RuntimeError(f"load job {job.job_id}: {job.errors}")
        return "LOADED", len(rows)
    except Exception as e:                                            # noqa: BLE001
        if "Already Exists" in str(e) or getattr(e, "code", None) == 409:
            log(event="promo_append_reused", table=table, job_id=job_id, rows=len(rows))
            return "REUSED", len(rows)
        raise


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
