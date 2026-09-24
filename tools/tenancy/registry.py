#!/usr/bin/env python3
"""Реестр арендаторов: tenants/<tenant_id>/tenant.json (Tenancy T1, ADR-08).

Офлайн-инструмент: ни облака, ни сети, ни секретов.

  python tools/tenancy/registry.py validate            # ворота CI
  python tools/tenancy/registry.py list                # валидированные арендаторы, JSON
  python tools/tenancy/registry.py runtime-env <tenant_id>

Коды выхода: 0 — успех (реестр валиден), 1 — есть нарушения, 3 — сбой инструмента.

ПРАВИЛО АВТОРИТЕТА РЕЕСТРА (T2.1). Потребители (Terraform и провижининг T3,
рендер T4, любые скрипты) узнают арендаторов ТОЛЬКО через этот модуль:
valid_tenants() / load_tenant() / `registry.py list`. Перебирать
`tenants/*/tenant.json` самостоятельно и разбирать файлы своим парсером запрещено:
такой потребитель увидел бы то, что реестр отверг (скрытый файл в `_schema/`,
повторяющиеся ключи, символическую ссылку).

runtime-env печатает окружение Ozon runtime (pipelines/ozon/runtime/common.py,
resolve_config), которое соответствует арендатору. Это мост «конфигурация →
тот же код»: одинаковый образ исполняется для любого арендатора с разным
окружением. Для EVETIS результат описательный: окружение production-job'ов
задаёт Terraform, а не реестр.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tools.tenancy import naming as N  # noqa: E402
from tools.tenancy.validation import (Finding, TenantDocumentError,  # noqa: E402
                                      load_tenant_document, validate_registry)

TENANTS_DIR = REPO / "tenants"
TENANT_FILE = "tenant.json"
# Зарезервированные каталоги tenants/ и ЗАКРЫТЫЙ список того, что в них может лежать
# (T2.1/L1). Всё остальное — нарушение: скрытый tenant.json в _schema/ реестр не
# читает, а наивный перебор tenants/*/tenant.json его подхватил бы.
RESERVED_DIRS = {"_schema": frozenset({"tenant.schema.json"})}


class RegistryInvalid(RuntimeError):
    """Реестр невалиден: потребителям ничего не выдаётся."""

    def __init__(self, findings: list[Finding]):
        super().__init__(f"реестр арендаторов невалиден: нарушений {len(findings)}")
        self.findings = findings


def _rel(root: Path, p: Path) -> str:
    return f"{root.name}/{p.relative_to(root)}"


def load_documents(root: Path = TENANTS_DIR) -> tuple[list[tuple[str, str, object]], list[Finding]]:
    """[(source, имя_каталога, документ)] и нарушения, найденные при чтении.

    Политика содержимого tenants/ (детерминированная, по белому списку):
      * символических ссылок нет нигде — ни каталогов, ни файлов;
      * зарезервированный каталог содержит ровно разрешённые обычные файлы;
      * любой другой элемент — каталог с именем, допустимым как tenant_id, и
        внутри ровно один обычный файл tenant.json;
      * файлов в корне tenants/ нет.
    """
    docs, problems = [], []

    def bad(p: Path, msg: str) -> None:
        problems.append(Finding(_rel(root, p), "$", "registry", msg))

    for entry in sorted(root.iterdir(), key=lambda p: p.name):
        if entry.is_symlink():
            bad(entry, "символические ссылки в tenants/ запрещены")
            continue
        if entry.name in RESERVED_DIRS:
            if not entry.is_dir():
                bad(entry, "зарезервированное имя обязано быть каталогом")
                continue
            allowed = RESERVED_DIRS[entry.name]
            for item in sorted(entry.iterdir(), key=lambda p: p.name):
                if item.name not in allowed or item.is_symlink() or not item.is_file():
                    bad(item, f"в зарезервированном каталоге {entry.name}/ допустимы только "
                              f"{sorted(allowed)} как обычные файлы")
            continue
        if entry.is_file():
            bad(entry, "файлы в корне tenants/ вне каталогов арендаторов запрещены")
            continue
        if not entry.is_dir():
            bad(entry, "элемент tenants/ не является ни каталогом, ни файлом")
            continue
        if not N.TENANT_ID_RE.fullmatch(entry.name):
            bad(entry, "имя каталога не является допустимым tenant_id")
            continue
        f = entry / TENANT_FILE
        src = _rel(root, f)
        extra = sorted(p.name for p in entry.iterdir() if p.name != TENANT_FILE)
        if extra:
            # В каталоге арендатора не место ничему, кроме контракта: ни ключам,
            # ни выгрузкам, ни «временным» заметкам с учётными данными.
            problems.append(Finding(src, "$", "registry",
                                    f"в каталоге арендатора посторонние файлы: {extra}"))
        if f.is_symlink() or not f.is_file():
            problems.append(Finding(src, "$", "registry",
                                    "tenant.json отсутствует или не является обычным файлом"))
            continue
        try:
            doc = load_tenant_document(f)
        except TenantDocumentError as e:
            problems.append(Finding(src, "$", "json", str(e)))
            continue
        docs.append((src, entry.name, doc))
    return docs, problems


def validate_root(root: Path = TENANTS_DIR) -> list[Finding]:
    docs, problems = load_documents(root)
    return problems + validate_registry(docs)


def valid_tenants(root: Path = TENANTS_DIR) -> dict[str, dict]:
    """{tenant_id: документ} — только из ЦЕЛИКОМ валидного реестра.

    Единственная точка, через которую потребители получают арендаторов: при любом
    нарушении — RegistryInvalid и ни одного документа (коллизия у соседа делает
    небезопасным и этого арендатора).
    """
    docs, problems = load_documents(root)
    findings = problems + validate_registry(docs)
    if findings:
        raise RegistryInvalid(findings)
    return {doc["tenant_id"]: doc for _src, _dir, doc in docs}


def ozon_runtime_env(doc: dict) -> dict[str, str]:
    """Окружение Ozon runtime для арендатора. Только ИМЕНА секретов, никаких значений.

    Ветвлений по конкретному арендатору нет: и EVETIS, и выделенный арендатор
    отображаются одним и тем же правилом.
    """
    db, ozon = doc["data_boundary"], doc["marketplaces"]["ozon"]
    env = {
        "GCP_PROJECT_ID": db["gcp_project_id"],
        "BQ_RAW_DATASET": db["datasets"]["ozon_raw"],
        "BQ_REF_DATASET": db["datasets"]["ref"],
        "BQ_LOCATION": db["bq_location"],
    }
    for role, var in N.OZON_SECRET_ENV_VARS.items():
        if role in ozon.get("secret_refs", {}):
            env[var] = ozon["secret_refs"][role]
    if ozon.get("entities"):
        env["ENTITIES"] = ",".join(ozon["entities"])
    return env


def load_tenant(tenant_id: str, root: Path = TENANTS_DIR) -> dict:
    """Документ одного арендатора из целиком валидного реестра."""
    N.check_tenant_id(tenant_id, legacy=(tenant_id == N.LEGACY_TENANT_ID))
    tenants = valid_tenants(root)
    if tenant_id not in tenants:
        raise N.NamingError(f"арендатора {tenant_id!r} в реестре нет")
    return tenants[tenant_id]


def tenant_summary(doc: dict) -> dict:
    """Идентичность арендатора для потребителей: без ссылок на секреты и прочего."""
    db = doc["data_boundary"]
    return {"tenant_id": doc["tenant_id"], "kind": db["kind"], "status": doc["status"],
            "config_authority": doc["config_authority"], "gcp_project_id": db["gcp_project_id"],
            "project_id_revision": db.get("project_id_revision", 1)
            if db["kind"] == "dedicated_project" else None}


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in ("validate", "list", "runtime-env"):
        print(__doc__, file=sys.stderr)
        return 3
    try:
        if argv[0] == "validate":
            findings = validate_root()
            for f in findings:
                print(f"FAIL {f}")
            docs, _ = load_documents()
            print(f"tenants: {len(docs)} документ(ов), нарушений: {len(findings)}")
            return 1 if findings else 0
        if argv[0] == "list":
            tenants = valid_tenants()
            print(json.dumps([tenant_summary(tenants[t]) for t in sorted(tenants)],
                             indent=2, ensure_ascii=False))
            return 0
        if len(argv) != 2:
            print("runtime-env <tenant_id>", file=sys.stderr)
            return 3
        # Окружение выдаётся только из целиком валидного реестра (load_tenant →
        # valid_tenants): коллизия у соседа делает небезопасным и этого арендатора.
        print(json.dumps(ozon_runtime_env(load_tenant(argv[1])), indent=2, ensure_ascii=False))
        return 0
    except RegistryInvalid as e:
        for f in e.findings:
            print(f"FAIL {f}", file=sys.stderr)
        return 1
    except N.NamingError as e:
        print(f"FAIL {e}", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001 — сбой инструмента отличаем от нарушения
        print(f"ERROR {type(e).__name__}: {e}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
