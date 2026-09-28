"""Драйвер «новый арендатор без значений секретов» (Tenancy T3.2). Не тест.

Импортирует НЕИЗМЕНЁННЫЙ runtime с окружением job'а из контракта реестра и прогоняет
main.main() так, как его запустил бы Cloud Run вручную до внесения ключей: в Secret
Manager арендатора есть только контейнеры, версий нет — доступ отвечает NOT_FOUND.
Печатает JSON: какие пути секретов запрошены, куда шла запись BigQuery, были ли
HTTP-вызовы, код выхода. Сети и облака нет.
"""
from __future__ import annotations

import io
import json
import sys
import types
from pathlib import Path

RUNTIME = Path(__file__).resolve().parents[1] / "runtime"
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _portability_driver import _install_stubs  # noqa: E402


def main():
    field_cls = _install_stubs()
    sys.path.insert(0, str(RUNTIME))
    import common as C
    import main as M

    seen = {"secret_paths": [], "bq": [], "http": [], "exit": None}

    class NoVersions:
        def access_secret_version(self, request):
            seen["secret_paths"].append(request["name"])
            raise RuntimeError(f"404 NOT_FOUND: Secret [{request['name']}] has no versions")

    class _Job:
        errors, job_id = None, "job"

        def result(self):
            return []

    class FakeBQ:
        def list_rows(self, ref):
            # ref.SELLER_BINDING нового арендатора пуст: привязку ещё никто не подтверждал.
            seen["bq"].append(("list_rows", ref))
            return []

        def get_table(self, ref):
            seen["bq"].append(("get_table", ref))
            return types.SimpleNamespace(schema=[field_cls("ingestion_run_id")])

        def insert_rows_json(self, ref, rows):
            seen["bq"].append(("insert", ref, [r.get("status") for r in rows]))

        def query(self, q, **_k):
            seen["bq"].append(("query", q[:60]))
            return _Job()

        def load_table_from_file(self, _f, ref, **_k):
            seen["bq"].append(("load", ref))
            return _Job()

    def no_network(req, *_a, **_k):
        seen["http"].append(req.full_url)
        raise AssertionError("HTTP без учётных данных недопустим")

    C._sm, C._bq, C._request = NoVersions(), FakeBQ(), no_network
    out = io.StringIO()
    real = sys.stdout
    sys.stdout = out
    try:
        M.main()
    except SystemExit as e:
        seen["exit"] = e.code
    finally:
        sys.stdout = real
    seen["config"] = C.CONFIG._asdict()
    seen["log"] = [json.loads(l) for l in out.getvalue().split("\n") if l.startswith("{")]
    print(json.dumps(seen, ensure_ascii=False, sort_keys=True, default=str))


if __name__ == "__main__":
    main()
