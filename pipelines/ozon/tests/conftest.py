"""Общие фикстуры офлайн-тестов Ozon runtime.

Тесты НЕ обращаются к Ozon API, BigQuery и Secret Manager. Облачных библиотек
в CI-проверке нет (pipelines/ozon/requirements-dev.txt — только pytest), поэтому
при их отсутствии подставляются минимальные заглушки ровно тех имён, которые
использует common.py. Если библиотеки установлены, берутся настоящие классы
конфигурации — с фальшивым клиентом работают и те, и другие.
"""

from __future__ import annotations

import os
import sys
import types
from pathlib import Path

import pytest

RUNTIME = Path(__file__).resolve().parents[1] / "runtime"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(RUNTIME))

# Tenancy T2: common.py не стартует без явного GCP_PROJECT_ID (fail-closed).
# Тестовый процесс получает заведомо не-production проект, а переменные
# арендатора из окружения разработчика вычищаются: тесты не должны зависеть от
# того, что выставлено в чьей-то оболочке, и не должны выглядеть как обращение
# к production. Конфигурации EVETIS и синтетического арендатора проверяются явно
# (test_runtime_config.py, test_tenant_portability.py), а не через это окружение.
OFFLINE_TEST_PROJECT = "offline-test-project"
TENANT_ENV_VARS = ("GCP_PROJECT_ID", "BQ_RAW_DATASET", "BQ_REF_DATASET", "BQ_LOCATION",
                   "OZON_SECRET_SELLER_CLIENT_ID", "OZON_SECRET_SELLER_API_KEY",
                   "OZON_SECRET_PERF_CLIENT_ID", "OZON_SECRET_PERF_CLIENT_SECRET",
                   "STRICT_PAGE_CAPS")
for _var in TENANT_ENV_VARS:
    os.environ.pop(_var, None)
os.environ["GCP_PROJECT_ID"] = OFFLINE_TEST_PROJECT


def _install_cloud_stubs():
    try:
        from google.cloud import bigquery, secretmanager  # noqa: F401
        return
    except ImportError:
        pass

    class _Cfg:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    class _SchemaField:
        def __init__(self, name, field_type, mode="NULLABLE"):
            self.name, self.field_type, self.mode = name, field_type, mode

    class _Table:
        """Как настоящий bigquery.Table: table_id — только имя таблицы,
        схема принимает лишь SchemaField."""
        def __init__(self, table_ref, schema=None):
            self.project, self.dataset_id, self.table_id = table_ref.split(".")
            if schema is not None and not all(isinstance(f, _SchemaField) for f in schema):
                raise ValueError("Schema items must either be fields or compatible mapping "
                                 "representations.")
            self.schema = schema
            self.expires = None

    bigquery = types.ModuleType("google.cloud.bigquery")
    bigquery.Client = object
    bigquery.SchemaField = _SchemaField
    bigquery.Table = _Table
    bigquery.LoadJobConfig = _Cfg
    bigquery.SourceFormat = types.SimpleNamespace(NEWLINE_DELIMITED_JSON="NEWLINE_DELIMITED_JSON")
    bigquery.WriteDisposition = types.SimpleNamespace(
        WRITE_APPEND="WRITE_APPEND", WRITE_TRUNCATE="WRITE_TRUNCATE")
    bigquery.CreateDisposition = types.SimpleNamespace(
        CREATE_NEVER="CREATE_NEVER", CREATE_IF_NEEDED="CREATE_IF_NEEDED")
    secretmanager = types.ModuleType("google.cloud.secretmanager")
    secretmanager.SecretManagerServiceClient = object

    google = sys.modules.setdefault("google", types.ModuleType("google"))
    cloud = types.ModuleType("google.cloud")
    cloud.bigquery = bigquery
    cloud.secretmanager = secretmanager
    google.cloud = cloud
    sys.modules.update({"google.cloud": cloud, "google.cloud.bigquery": bigquery,
                        "google.cloud.secretmanager": secretmanager})


_install_cloud_stubs()


@pytest.fixture
def entities(monkeypatch):
    """Модуль entities без сна и без сети: любой непропатченный вызов API падает."""
    import entities as E

    def _no_network(*_a, **_k):
        raise AssertionError("тест обратился к API без мока транспорта")

    monkeypatch.setattr(E.time, "sleep", lambda *_: None)
    monkeypatch.setattr(E, "seller_post", _no_network)
    monkeypatch.setattr(E, "perf_get", _no_network)
    monkeypatch.setattr(E, "perf_post", _no_network)
    monkeypatch.setattr(E, "log", lambda **_: None)
    return E


@pytest.fixture
def captured_merges(entities, monkeypatch):
    """Перехват merge_rows внутри entities: [(table, rows, keys, kwargs), ...]."""
    calls = []

    def _capture(table, rows, keys, run_id, **kw):
        calls.append((table, rows, keys, kw))
        return {"received": len(rows), "inserted": 0, "updated": 0}

    monkeypatch.setattr(entities, "merge_rows", _capture)
    return calls


class FakeTransport:
    """Сценарий ответов API: список (predicate(path, body) -> (code, payload))."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def __call__(self, path, body):
        self.calls.append((path, body))
        for match, response in self.script:
            if match(path, body):
                return response(path, body) if callable(response) else response
        raise AssertionError(f"нет ответа в сценарии для {path} {body}")


@pytest.fixture
def transport():
    return FakeTransport
