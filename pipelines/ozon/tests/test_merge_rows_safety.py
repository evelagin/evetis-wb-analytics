"""merge_rows: конфликт ключей fail-closed, идемпотентность одинаковых строк,
очистка staging при любом исходе, expires на staging, неизменность MERGE и грейна.

BigQuery не вызывается: common._bq подменён фальшивым клиентом, который
записывает порядок вызовов и умеет падать в заданной точке.
"""

from __future__ import annotations

import ast
import hashlib
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from google.cloud import bigquery

import common as C

ENTITIES_PY = Path(__file__).resolve().parents[1] / "runtime" / "entities.py"
TABLE = "RAW_OZON_TEST"
SCHEMA_COLS = ["accrual_id", "type_id", "sku", "amount_rub", "operation_name",
               "extracted_at", "ingestion_run_id", "source_endpoint", "source_payload_hash"]
KEYS = ["accrual_id", "type_id", "sku"]


class Boom(Exception):
    pass


class FakeJob:
    def __init__(self, raise_on_result=None, errors=None, rows=None):
        self._raise = raise_on_result
        self.errors = errors
        self.job_id = "job-1"
        self._rows = rows or []

    def result(self):
        if self._raise:
            raise self._raise
        return self._rows


class FakeClient:
    """fail_at ∈ {create, load_call, load_result, load_errors, merge, count_after}."""

    def __init__(self, fail_at=None, cleanup_raises=False, stale_cleanup_raises=False):
        self.fail_at = fail_at
        self.cleanup_raises = cleanup_raises
        self.events = []
        self.count = 10
        self.loaded = None
        self.created = None
        self.load_cfg = None
        self.merge_sql = None

    def get_table(self, table_id):
        # настоящие SchemaField: bigquery.Table проверяет тип элементов схемы
        return SimpleNamespace(schema=[bigquery.SchemaField(c, "STRING") for c in SCHEMA_COLS])

    def delete_table(self, table_id, not_found_ok=False):
        self.events.append(("delete", table_id))
        n_deletes = sum(1 for e in self.events if e[0] == "delete")
        if self.cleanup_raises and n_deletes >= 2:
            raise RuntimeError("cleanup exploded")

    def create_table(self, table):
        self.events.append(("create", f"{table.project}.{table.dataset_id}.{table.table_id}"))
        if self.fail_at == "create":
            raise Boom("create failed")
        self.created = table

    def load_table_from_file(self, fileobj, dest, job_config=None, location=None):
        self.events.append(("load", dest))
        if self.fail_at == "load_call":
            raise Boom("load call failed")
        self.loaded = fileobj.read().decode()
        self.load_cfg = job_config
        if self.fail_at == "load_result":
            return FakeJob(raise_on_result=Boom("load result failed"))
        if self.fail_at == "load_errors":
            return FakeJob(errors=[{"reason": "invalid"}])
        return FakeJob()

    def query(self, sql, location=None):
        if sql.startswith("MERGE"):
            self.events.append(("merge", None))
            self.merge_sql = sql
            if self.fail_at == "merge":
                return FakeJob(raise_on_result=Boom("merge failed"))
            self.count += 1
            return FakeJob()
        self.events.append(("count", None))
        if self.fail_at == "count_after" and any(e[0] == "merge" for e in self.events):
            return FakeJob(raise_on_result=Boom("count failed"))
        return FakeJob(rows=[{"c": self.count}])


@pytest.fixture
def fake_bq(monkeypatch):
    def make(**kw):
        client = FakeClient(**kw)
        monkeypatch.setattr(C, "_bq", client)
        return client
    return make


@pytest.fixture
def logs(monkeypatch):
    out = []
    monkeypatch.setattr(C, "log", lambda **kw: out.append(kw))
    return out


def row(accrual_id=1, type_id=32, sku="100000001", amount=-60.0, name="Логистика"):
    return {"accrual_id": accrual_id, "type_id": type_id, "sku": sku, "amount_rub": amount,
            "operation_name": name, "extracted_at": "2026-09-16T06:30:00+03:00",
            "ingestion_run_id": "rt-abc", "source_endpoint": "POST /x",
            "source_payload_hash": C.h(accrual_id, type_id, sku, amount)}


STAGING = f"{C.PROJECT}.{C.DATASET}._rt_{TABLE}_rtabc12345"
RUN_ID = "rt-abc12345-0000"


# ------------------------------------------------------------ валидация партии
def test_no_duplicates_returns_zero():
    assert C.validate_merge_batch(TABLE, [row(1), row(2)], KEYS, SCHEMA_COLS) == 0


def test_identical_duplicates_collapse():
    assert C.validate_merge_batch(TABLE, [row(1), row(1), row(1), row(2)], KEYS, SCHEMA_COLS) == 2


def test_same_key_different_payload_fails_closed():
    """Главный инвариант: одинаковый ключ + разная полезная нагрузка → отказ, а не схлопывание."""
    with pytest.raises(C.MergeKeyConflictError) as e:
        C.validate_merge_batch(TABLE, [row(1, amount=-60.0), row(1, amount=-45.5)],
                               KEYS, SCHEMA_COLS)
    msg = str(e.value)
    assert TABLE in msg
    assert "(accrual_id,type_id,sku)" in msg                  # имена колонок ключа
    assert "1 ключ" in msg and "строк 2" in msg and "rows=2" in msg
    assert "режим collapse_identical" in msg
    assert "differing_columns=amount_rub" in msg              # имя расходящейся колонки
    fp = C.merge_key_fingerprint(C.merge_key(row(1), KEYS))
    assert f"key_fp={fp}" in msg


# Значения, которые НЕ должны появиться в тексте исключения ни при каких условиях.
KEY_ACCRUAL = "ACCRUAL-KEYVALUE-9f3e71"
KEY_SKU = "SKU-KEYVALUE-5520417"
KEY_POSTING = "0123456789-0042-7"
TOKEN_LIKE = "Bearer eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJldmV0aXMifQ.SIGNATURE-VALUE"
API_KEY_LIKE = "api-key-3f6c2a1e-8d4b-4b8e-9a7f-0c1d2e3f4a5b"
PAYLOAD_VALUE = "PAYLOAD-VALUE-DO-NOT-LOG"


def _leak_probe_rows():
    a = row(KEY_ACCRUAL, type_id=KEY_POSTING, sku=KEY_SKU, amount=-60.0, name=TOKEN_LIKE)
    b = row(KEY_ACCRUAL, type_id=KEY_POSTING, sku=KEY_SKU, amount=-45.5, name=API_KEY_LIKE)
    b["source_endpoint"] = PAYLOAD_VALUE
    return [a, b]


@pytest.mark.parametrize("mode", ["collapse_identical", "reject"])
def test_conflict_message_has_no_payload_key_or_token_values(mode):
    with pytest.raises(C.MergeKeyConflictError) as e:
        C.validate_merge_batch(TABLE, _leak_probe_rows(), KEYS, SCHEMA_COLS, mode)
    msg = str(e.value)
    # значения ключа слияния
    for v in (KEY_ACCRUAL, KEY_SKU, KEY_POSTING, "KEYVALUE", "0042"):
        assert v not in msg, f"значение ключа в сообщении: {v}"
    # значения полезной нагрузки
    for v in (PAYLOAD_VALUE, "-60", "-45.5", "Логистика"):
        assert v not in msg, f"значение полезной нагрузки в сообщении: {v}"
    # токеноподобные строки
    for v in (TOKEN_LIKE, "Bearer", "eyJ", "SIGNATURE", API_KEY_LIKE, "api-key"):
        assert v not in msg, f"токеноподобная строка в сообщении: {v}"
    # диагностика при этом на месте
    assert TABLE in msg and "(accrual_id,type_id,sku)" in msg and f"режим {mode}" in msg
    assert "rows=2" in msg and "key_fp=" in msg
    assert "amount_rub" in msg and "operation_name" in msg and "source_endpoint" in msg


def test_key_fingerprint_is_deterministic_short_and_opaque():
    key = C.merge_key(row(KEY_ACCRUAL, type_id=32, sku=KEY_SKU), KEYS)
    fp = C.merge_key_fingerprint(key)
    assert fp == C.merge_key_fingerprint(tuple(key))                     # детерминирован
    assert re.fullmatch(r"[0-9a-f]{12}", fp)                              # короткий hex
    assert fp == hashlib.sha256("\x1f".join(key).encode()).hexdigest()[:12]
    assert KEY_ACCRUAL not in fp and KEY_SKU not in fp
    other = C.merge_key(row(KEY_ACCRUAL, type_id=33, sku=KEY_SKU), KEYS)
    assert C.merge_key_fingerprint(other) != fp                           # различает ключи


def test_fingerprint_identifies_same_conflict_across_runs():
    rows = [row(7, amount=-1.0), row(7, amount=-2.0)]
    msgs = []
    for _ in range(2):
        with pytest.raises(C.MergeKeyConflictError) as e:
            C.validate_merge_batch(TABLE, rows, KEYS, SCHEMA_COLS)
        msgs.append(str(e.value))
    assert msgs[0] == msgs[1]


def test_float_representation_noise_is_not_a_conflict():
    """Сравнение идёт по загружаемому виду (_clean), как в staging."""
    a, b = row(1, amount=2000.37), row(1, amount=2000.3700000000001)
    b["source_payload_hash"] = a["source_payload_hash"]   # изолируем только округление
    assert C.validate_merge_batch(TABLE, [a, b], KEYS, SCHEMA_COLS) == 1


def test_differing_source_payload_hash_is_a_conflict():
    """Сравниваются ВСЕ сохраняемые колонки, включая source_payload_hash.

    Схлопнутые строки должны быть равны по всему, что попадёт в таблицу. Во всех
    не-финансовых сущностях hash строится из полей ключа и внутри ключа совпадать
    обязан; расхождение — признак, что под одним ключом разные объекты источника.
    """
    a, b = row(1), row(1)
    b["source_payload_hash"] = "different"
    with pytest.raises(C.MergeKeyConflictError, match="differing_columns=source_payload_hash"):
        C.validate_merge_batch(TABLE, [a, b], KEYS, SCHEMA_COLS)


def test_key_normalization_mirrors_merge_cast():
    """32 и '32' — один ключ для MERGE (CAST AS STRING), значит один и здесь."""
    a, b = row(1, type_id=32), row(1, type_id="32")
    with pytest.raises(C.MergeKeyConflictError):
        C.validate_merge_batch(TABLE, [a, b], KEYS, SCHEMA_COLS)   # type_id различается как значение


def test_null_keys_group_together():
    a, b = row(1, sku=None), row(1, sku=None, amount=-1.0)
    with pytest.raises(C.MergeKeyConflictError) as e:
        C.validate_merge_batch(TABLE, [a, b], KEYS, SCHEMA_COLS)
    assert f"key_fp={C.merge_key_fingerprint(C.merge_key(a, KEYS))}" in str(e.value)


def test_null_and_value_are_different_keys():
    assert C.validate_merge_batch(TABLE, [row(1, sku=None), row(1, sku="7")], KEYS, SCHEMA_COLS) == 0


def test_reject_mode_fails_even_on_identical_rows():
    """Финансы: две одинаковые строки могут быть двумя реальными начислениями."""
    with pytest.raises(C.MergeKeyConflictError, match="режим reject"):
        C.validate_merge_batch(TABLE, [row(1), row(1)], KEYS, SCHEMA_COLS, "reject")


def test_reject_mode_passes_unique_rows():
    assert C.validate_merge_batch(TABLE, [row(1), row(2)], KEYS, SCHEMA_COLS, "reject") == 0


def test_unknown_mode_is_rejected():
    with pytest.raises(ValueError):
        C.validate_merge_batch(TABLE, [row(1)], KEYS, SCHEMA_COLS, "last_wins")


def test_conflict_report_is_bounded():
    rows = [r for i in range(20) for r in (row(i, amount=-1.0), row(i, amount=-2.0))]
    with pytest.raises(C.MergeKeyConflictError) as e:
        C.validate_merge_batch(TABLE, rows, KEYS, SCHEMA_COLS)
    assert "20 ключ" in str(e.value) and "строк 40" in str(e.value)
    assert str(e.value).count("key_fp=") == 5


# ------------------------------------------------------------ merge_rows: поток
def test_conflict_fails_before_any_bigquery_write(fake_bq, logs):
    client = fake_bq()
    with pytest.raises(C.MergeKeyConflictError):
        C.merge_rows(TABLE, [row(1, amount=-1.0), row(1, amount=-2.0)], KEYS, RUN_ID)
    assert client.events == []          # ни staging, ни load, ни MERGE


def test_identical_duplicates_merge_idempotently(fake_bq, logs):
    client = fake_bq()
    res = C.merge_rows(TABLE, [row(1), row(1), row(2)], KEYS, RUN_ID)
    assert [e[0] for e in client.events] == ["delete", "create", "load", "count", "merge",
                                             "count", "delete"]
    assert res == {"received": 3, "inserted": 1, "updated": 2}
    assert {"event": "merge_identical_duplicates_collapsed", "table": TABLE, "rows": 1} in logs


def test_reject_mode_is_passed_through(fake_bq, logs):
    client = fake_bq()
    with pytest.raises(C.MergeKeyConflictError):
        C.merge_rows(TABLE, [row(1), row(1)], KEYS, RUN_ID, on_duplicate_key="reject")
    assert client.events == []


def test_empty_rows_touch_nothing(fake_bq):
    client = fake_bq()
    assert C.merge_rows(TABLE, [], KEYS, RUN_ID) == {"received": 0, "inserted": 0, "updated": 0}
    assert client.events == []


def test_staging_has_expiration_and_is_loaded_append_never_create(fake_bq, logs):
    client = fake_bq()
    t0 = datetime.now(timezone.utc)
    C.merge_rows(TABLE, [row(1)], KEYS, RUN_ID)
    created = client.created
    assert f"{created.project}.{created.dataset_id}.{created.table_id}" == STAGING
    assert t0 + timedelta(hours=23) < client.created.expires <= datetime.now(timezone.utc) + C.STAGING_TTL
    assert client.load_cfg.write_disposition == "WRITE_APPEND"
    assert client.load_cfg.create_disposition == "CREATE_NEVER"
    # остаток с тем же именем удаляется ДО создания
    assert client.events[0] == ("delete", STAGING) and client.events[1] == ("create", STAGING)


def test_merge_sql_text_is_unchanged(fake_bq, logs):
    """Эталон — текст MERGE из 0abc4c4 для тех же ключей и колонок."""
    client = fake_bq()
    C.merge_rows(TABLE, [row(1)], KEYS, RUN_ID)
    P, D = C.PROJECT, C.DATASET
    on = " AND ".join(
        f"COALESCE(CAST(T.{k} AS STRING),'\\x00')=COALESCE(CAST(S.{k} AS STRING),'\\x00')"
        for k in KEYS)
    setter = ", ".join(f"T.{c}=S.{c}" for c in SCHEMA_COLS if c not in KEYS)
    expected = (f"MERGE `{P}.{D}.{TABLE}` T USING "
                f"(SELECT * EXCEPT(_rn) FROM (SELECT *, ROW_NUMBER() OVER "
                f"(PARTITION BY {','.join(KEYS)} ORDER BY extracted_at DESC) _rn "
                f"FROM `{P}.{D}._rt_{TABLE}_rtabc12345`) WHERE _rn=1) S ON {on} "
                f"WHEN MATCHED THEN UPDATE SET {setter} "
                f"WHEN NOT MATCHED THEN INSERT ({','.join(SCHEMA_COLS)}) "
                f"VALUES ({','.join('S.'+c for c in SCHEMA_COLS)})")
    assert client.merge_sql == expected


def test_loaded_payload_is_unchanged(fake_bq, logs):
    client = fake_bq()
    C.merge_rows(TABLE, [row(1, amount=2000.3700000000001)], KEYS, RUN_ID)
    assert '"amount_rub": 2000.37' in client.loaded


# ------------------------------------------------------------ очистка staging
@pytest.mark.parametrize("fail_at, exc", [
    ("create", Boom), ("load_call", Boom), ("load_result", Boom),
    ("load_errors", RuntimeError), ("merge", Boom), ("count_after", Boom),
])
def test_staging_dropped_on_every_failure(fake_bq, logs, fail_at, exc):
    client = fake_bq(fail_at=fail_at)
    with pytest.raises(exc):
        C.merge_rows(TABLE, [row(1)], KEYS, RUN_ID)
    assert client.events[-1] == ("delete", STAGING)


def test_staging_dropped_on_success(fake_bq, logs):
    client = fake_bq()
    C.merge_rows(TABLE, [row(1)], KEYS, RUN_ID)
    assert client.events[-1] == ("delete", STAGING)
    assert [e for e in client.events if e[0] == "merge"]


def test_cleanup_failure_does_not_mask_original_exception(fake_bq, logs):
    client = fake_bq(fail_at="merge", cleanup_raises=True)
    with pytest.raises(Boom, match="merge failed"):
        C.merge_rows(TABLE, [row(1)], KEYS, RUN_ID)
    assert any(l.get("event") == "staging_cleanup_failed" and l.get("staging") == STAGING
               for l in logs)


def test_cleanup_failure_after_successful_merge_is_logged_not_raised(fake_bq, logs):
    """Данные уже слиты: отметить прогон FAILED из-за очистки значило бы соврать в журнале.
    Остаток подчистит expires."""
    client = fake_bq(cleanup_raises=True)
    res = C.merge_rows(TABLE, [row(1)], KEYS, RUN_ID)
    assert res["inserted"] == 1
    assert any(l.get("event") == "staging_cleanup_failed" for l in logs)


# ------------------------------------------------------------ грейн заморожен
EXPECTED_MERGES = {
    "RAW_OZON_CATALOG": (["snapshot_date", "sku"], None),
    "RAW_OZON_PRICES": (["snapshot_date", "offer_id"], None),
    "RAW_OZON_PRICE_COMMISSIONS": (["snapshot_date", "offer_id", "sale_scheme",
                                    "commission_component"], None),
    "RAW_OZON_SELLER_INFO": (["snapshot_date"], None),
    "RAW_OZON_STOCKS": (["snapshot_date", "sku", "warehouse_id"], None),
    "RAW_OZON_POSTINGS_FBO": (["posting_number", "sku"], None),
    "RAW_OZON_FINANCE_ACCRUAL": (["accrual_id", "type_id", "sku"], "reject"),
    "RAW_OZON_ADS_CAMPAIGNS": (["snapshot_date", "campaign_id"], None),
    "RAW_OZON_ADS_EXPENSE_DAILY": (["date", "campaign_id"], None),
    "RAW_OZON_ADS_SKU_DAILY": (["date", "campaign_id", "sku"], None),
    "RAW_OZON_CLUSTERS": (["snapshot_date", "warehouse_id"], None),
    "RAW_OZON_SUPPLY_ORDERS": (["order_id"], None),
    "RAW_OZON_SUPPLIES": (["order_id", "supply_id"], None),
    "RAW_OZON_SUPPLY_BUNDLES": (["bundle_id", "sku"], None),
}


def test_merge_keys_of_every_entity_are_frozen():
    """Любая смена ключа слияния — это смена грейна production-таблицы. Тест фиксирует
    ключи 0abc4c4 и то, что только финансы идут в режиме reject."""
    tree = ast.parse(ENTITIES_PY.read_text(encoding="utf-8"))
    found = {}
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "merge_rows"):
            table = ast.literal_eval(node.args[0])
            keys = ast.literal_eval(node.args[2])
            mode = next((ast.literal_eval(k.value) for k in node.keywords
                         if k.arg == "on_duplicate_key"), None)
            assert table not in found, f"{table} пишется из двух мест"
            found[table] = (keys, mode)
    assert found == EXPECTED_MERGES
