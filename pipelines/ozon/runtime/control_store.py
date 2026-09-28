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

tabledata.list НЕ видит строки потоковой вставки, пока они в write-optimized storage (до ~90 мин,
документация BigQuery). Поэтому всё, что требует линеаризации, живёт в метаданных таблиц
tenant_locks (tables.list консистентен): аренды L_<chunk>_<gen> и маркеры переходов S_<seq>.
Журналы tenant_ops — зеркало для аудита и SQL; решения, которые по ним принимаются, либо fail-closed
при отставании (нет свежего доказательства — нет перехода), либо проверяются ещё раз живым вызовом.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import lifecycle_core as LCORE

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
        """Строки таблицы — tabledata.list (без строк, ещё лежащих в буфере потоковой вставки)."""
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

    def _locks(self):
        return list(self.client.list_tables(f"{self.project}.{self.ds['tenant_locks']}"))

    def leases(self):
        """[(имя, until)] аренд L_* — tables.list; until из метки (секунды эпохи)."""
        out = []
        for t in self._locks():
            if not t.table_id.startswith("L_"):
                continue
            u = (getattr(t, "labels", None) or {}).get("until")
            out.append((t.table_id, datetime.fromtimestamp(int(u), tz=timezone.utc) if u and u.isdigit() else None))
        return out

    def _create(self, name: str, labels: dict, description: str, expires=None) -> bool:
        from google.cloud import bigquery
        t = bigquery.Table(f"{self.project}.{self.ds['tenant_locks']}.{name}",
                           schema=[bigquery.SchemaField("marker", "STRING")])
        t.labels = {k: LCORE._label(v) for k, v in labels.items()}
        t.description = self.redact(description)[:1000]
        if expires is not None:
            t.expires = expires
        try:
            self.client.create_table(t, exists_ok=False)
            return True
        except Exception as e:                                    # noqa: BLE001
            if getattr(e, "code", None) == 409 or type(e).__name__ == "Conflict":
                return False
            raise

    def create_lease(self, name: str, until, owner: str) -> bool:
        """Атомарно создать аренду: True — создана нами, False — имя уже занято (409)."""
        from checkpoints import LEASE_TABLE_KEEP
        return self._create(name, {"until": int(until.timestamp()), "owner": owner}, f"lease {owner}",
                            expires=until + LEASE_TABLE_KEEP)

    def ref_marker(self, name: str):
        """(метки, описание) таблицы-знака владельца в ref или None — tables.get (консистентно).

        У control в ref только mpaSqlSourceRead (get, getData): читать знаки владельца он может,
        создать или изменить — нет."""
        try:
            t = self.client.get_table(f"{self.project}.{self.ds['ref']}.{name}")
        except Exception as e:                                    # noqa: BLE001
            if getattr(e, "code", None) == 404 or type(e).__name__ == "NotFound":
                return None
            raise
        return (getattr(t, "labels", None) or {}, getattr(t, "description", None))

    def ref_series(self, name_of, limit: int = 9999) -> list:
        """[(имя, метки, описание)] серии name_of(1), name_of(2)… до первой отсутствующей (номера подряд:
        владелец занимает следующий номер tables.insert-ом)."""
        out = []
        for n in range(1, limit + 1):
            m = self.ref_marker(name_of(n))
            if m is None:
                return out
            out.append((name_of(n), m[0], m[1]))
        return out

    def state_chain(self) -> list[dict]:
        """Цепочка переходов из маркеров S_<seq> (метки; created — время сервера)."""
        return LCORE.chain_from_markers((t.table_id, getattr(t, "labels", None), getattr(t, "created", None))
                                        for t in self._locks())

    def record_transition(self, event: dict) -> bool:
        """Занять номер seq (маркер S_<seq>; 409 — параллельный переход, False) и дописать зеркало."""
        ok = self._create(LCORE.marker_name(event["seq"]), LCORE.marker_labels(event),
                          json.dumps({k: event.get(k) for k in ("reason_code", "run_id")}, ensure_ascii=False))
        if ok:
            self.append("TENANT_STATE_EVENTS", [event])
        return ok
