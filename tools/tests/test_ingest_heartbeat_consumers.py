"""Миграция Apps Script → Cloud Run: потребители журнала загрузок читают общий V_INGEST_HEARTBEAT.

После переключения заказов, продаж и рекламы на Cloud Run строки этих загрузок пишутся в
wb_raw.LOADER_RUNS (environment='prod'), а не в wb_raw.INGEST_RUNS. V_INGEST_HEARTBEAT объединяет оба
журнала, поэтому детекторы и витрины свежести, читающие INGEST_RUNS напрямую, после переключения
перестали бы видеть прогоны. Тест не даёт вернуть прямое чтение INGEST_RUNS для этих загрузок.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# Объекты, развёрнутые в production и читающие журнал orders/sales/ads.
CONSUMERS = [
    "sql/health/dro1_02_probe.sql",
    "sql/mart/dashboard_layer_v1.sql",
    "sql/ops/phase2_health/d2_health_ext_procedure.sql",
]
# Журнал отказов читает LOADER_RUNS отдельной веткой; INGEST_RUNS в нём остаётся для истории Apps Script.
ALLOWED_DIRECT = {"sql/health/dro1_07_run_failure_ledger.sql"}
DIRECT = re.compile(r"`(?:project-fa311fc0-4d87-4781-986\.)?wb_raw\.INGEST_RUNS`")


class IngestHeartbeatConsumers(unittest.TestCase):
    def test_consumers_read_heartbeat_not_ingest_runs(self):
        for rel in CONSUMERS:
            text = (ROOT / rel).read_text(encoding="utf-8")
            code = "\n".join(line.split("--", 1)[0] for line in text.splitlines())
            self.assertIsNone(DIRECT.search(code), f"{rel}: прямое чтение wb_raw.INGEST_RUNS")
            self.assertIn("V_INGEST_HEARTBEAT", code, f"{rel}: нет чтения V_INGEST_HEARTBEAT")

    def test_no_new_direct_readers_in_health_and_mart(self):
        offenders = []
        for path in list((ROOT / "sql/health").glob("*.sql")) + list((ROOT / "sql/mart").glob("*.sql")) + list(
            (ROOT / "sql/ops/phase2_health").glob("*.sql")
        ):
            rel = str(path.relative_to(ROOT))
            if rel in ALLOWED_DIRECT or "rollback" in rel:
                continue
            code = "\n".join(line.split("--", 1)[0] for line in path.read_text(encoding="utf-8").splitlines())
            if DIRECT.search(code):
                offenders.append(rel)
        self.assertEqual(offenders, [], "новые прямые читатели INGEST_RUNS — читайте V_INGEST_HEARTBEAT")

    def test_heartbeat_view_includes_only_prod_cloud_runs(self):
        # Контракт, на который опираются потребители: shadow-прогоны в журнал гейта не попадают.
        sql = (ROOT / "sql/mart3/pr_mart3a_ingest_runs.sql")
        if not sql.exists():
            self.skipTest("файл определения V_INGEST_HEARTBEAT не найден в репозитории")
        self.assertIn("environment = 'prod'", sql.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
