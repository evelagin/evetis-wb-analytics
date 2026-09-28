"""Хранилище control (Tenancy T5): BigQuery только через Tables API, без query jobs.

Почему без запросов. У sa-tenant-control нет bigquery.jobs.create, поэтому у него нет и DML:
UPDATE/DELETE/MERGE над журналами tenant_ops для него физически невозможны. Запись — только
tabledata.insertAll (дописать), чтение — tabledata.list, аренда — tables.insert в tenant_locks.
Журналы состояний, чекпойнтов и наблюдений становятся append-only на уровне IAM, а не на
честном слове кода.

Методы клиента google-cloud-bigquery, которые здесь используются, и соответствующие вызовы API:
  list_rows        → tabledata.list        (bigquery.tables.getData)
  insert_rows_json → tabledata.insertAll   (bigquery.tables.updateData)
  list_tables      → tables.list           (bigquery.tables.list, только tenant_locks)
  create_table     → tables.insert         (bigquery.tables.create, только tenant_locks)
Никаких client.query / load_table_* / delete_* / update_table — тест проверяет по дереву разбора.
"""
from __future__ import annotations

import hashlib
import json

APPEND_TABLES = frozenset({"TENANT_STATE_EVENTS", "SELLER_IDENTITY_OBSERVATIONS", "CAPABILITY_PROFILE",
                           "HISTORY_BOUNDARIES", "BACKFILL_CHECKPOINTS", "DATA_COVERAGE", "DQ_RESULTS"})
READ_DATASETS = ("tenant_ops", "ref", "ozon_raw")


class StoreError(RuntimeError):
    """Запись или чтение control не удались — переход не выполняется (fail closed)."""


class ControlStore:
    def __init__(self, client, project: str, datasets: dict, redact=None):
        # redact — граница безопасности runtime (common.redact_value): строка журнала не может
        # унести секрет, даже если API повторил его в ответе (например, в названии компании).
        self.client, self.project, self.ds = client, project, dict(datasets)
        self.redact = redact or (lambda x: x)

    def _ref(self, key: str, table: str) -> str:
        return f"{self.project}.{self.ds[key]}.{table}"

    def rows(self, key: str, table: str):
        """Все строки таблицы (включая буфер потоковой вставки) — tabledata.list."""
        if key not in READ_DATASETS:
            raise StoreError(f"чтение {key} не предусмотрено")
        for r in self.client.list_rows(self._ref(key, table)):
            yield dict(r.items())

    def append(self, table: str, rows: list[dict]) -> int:
        """Дописать строки в журнал tenant_ops (insertAll). Идентификаторы строк детерминированы —
        повтор вызова в пределах окна дедупликации BigQuery не задваивает строку."""
        if table not in APPEND_TABLES:
            raise StoreError(f"запись в {table} не предусмотрена")
        if not rows:
            return 0
        rows = [self.redact(r) for r in rows]
        ids = [hashlib.sha256(json.dumps(r, sort_keys=True, default=str).encode()).hexdigest()[:32] for r in rows]
        errors = self.client.insert_rows_json(self._ref("tenant_ops", table), rows, row_ids=ids)
        if errors:
            raise StoreError(f"{table}: insertAll вернул {len(errors)} ошибок")
        return len(rows)

    def leases(self):
        """[(имя, момент истечения)] таблиц-аренд в tenant_locks — tables.list."""
        return [(t.table_id, getattr(t, "expires", None))
                for t in self.client.list_tables(f"{self.project}.{self.ds['tenant_locks']}")]

    def create_lease(self, name: str, expires, owner: str) -> bool:
        """Атомарно создать аренду: True — создана нами, False — имя уже занято (409)."""
        from google.cloud import bigquery
        t = bigquery.Table(f"{self.project}.{self.ds['tenant_locks']}.{name}",
                           schema=[bigquery.SchemaField("owner", "STRING")])
        t.expires = expires
        t.description = f"lease {owner}"
        try:
            self.client.create_table(t, exists_ok=False)
            return True
        except Exception as e:                                    # noqa: BLE001
            if getattr(e, "code", None) == 409 or type(e).__name__ == "Conflict":
                return False
            raise
