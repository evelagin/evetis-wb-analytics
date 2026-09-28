#!/usr/bin/env python3
"""Реестр арендаторов: tenants/<tenant_id>/tenant.json (Tenancy T1, ADR-08).

Офлайн-инструмент: ни облака, ни сети, ни секретов.

  python tools/tenancy/registry.py validate            # ворота CI
  python tools/tenancy/registry.py list                # валидированные арендаторы, JSON
  python tools/tenancy/registry.py runtime-env <tenant_id>
  python tools/tenancy/registry.py terraform-inputs <tenant_id>   # контракт Terraform (T3.2)

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


TERRAFORM_CONTRACT_VERSION = 1


def terraform_inputs(tenant_id: str) -> dict:
    """Контракт «реестр → Terraform арендатора» (T3.2) — ЕДИНСТВЕННЫЙ производственный путь.

    tenant_id → канонический TENANTS_DIR → строгий загрузчик (load_tenant) → naming → контракт.
    Ни готового документа, ни другого корня реестра этот вход не принимает (L3): иначе
    экспорт можно было бы кормить документом, который реестр не проверял.
    """
    return _terraform_contract(load_tenant(tenant_id, TENANTS_DIR))


def _terraform_contract(doc: dict, repo: Path = REPO) -> dict:
    """Сборка контракта из УЖЕ провалидированного документа. Не для производства.

    Вызывают только terraform_inputs(tenant_id) и синтетические фикстуры тестов
    (tools/tenancy/synthetic.py), которые кладут документ во временную копию реестра
    и проводят его через тот же load_tenant. Никакого второго разборщика: имена —
    naming, расписание и таблицы — ozon_contract, факты платформы — platform.
    """
    from tools.tenancy import ozon_contract as OC
    from tools.tenancy import platform as PL
    from tools.tenancy import sql_identity as SI

    tid, db = doc["tenant_id"], doc["data_boundary"]
    if db["kind"] != "dedicated_project" or doc["config_authority"] != "TENANT_REGISTRY":
        raise N.NamingError(f"{tid!r} не выделенный арендатор реестра: Terraform арендатора "
                            "к нему неприменим (EVETIS ведёт свой Terraform)")
    revision = db.get("project_id_revision")
    project_id = N.derive_project_id(tid, revision)
    if project_id != db["gcp_project_id"]:           # валидатор уже проверил; не доверяем молча
        raise N.NamingError(f"{tid!r}: gcp_project_id не равен каноническому {project_id!r}")
    if doc["scheduler_state"] != "PAUSED":
        raise N.NamingError(f"{tid!r}: до ворот активации (T3.3+) scheduler_state обязан быть PAUSED")

    releases = PL.load_runtime_release(repo)
    marketplaces, apis = {}, set(PL.TENANT_BASE_APIS)
    ozon = doc["marketplaces"]["ozon"]
    entities = list(ozon.get("entities", [])) if ozon["enabled"] else []
    if ozon["enabled"]:
        base_env = ozon_runtime_env(doc)
        jobs = {}
        for job, spec in OC.jobs_for(entities).items():
            env = dict(base_env, ENTITIES=",".join(spec["entities"]),
                       STRICT_PAGE_CAPS=OC.DEDICATED_STRICT_PAGE_CAPS)
            jobs[job] = {"scheduler": spec["scheduler"], "schedule": spec["schedule"],
                         "time_zone": spec["time_zone"], "entities": spec["entities"], "env": env}
        marketplaces["ozon"] = {
            "service_accounts": N.dedicated_service_accounts(tid, "ozon"),
            "secret_ids": dict(sorted(ozon["secret_refs"].items())),
            "raw_dataset_key": "ozon_raw",
            "ref_dataset_key": "ref",
            "jobs": jobs,
            "runtime_image": releases.get("ozon"),
        }
        apis.update(OC.OZON_APIS)

    tables = []
    for ds_key, names in OC.tables_for(entities).items():
        tables += [OC.terraform_table(ds_key, t) for t in names]

    return {
        "contract_version": TERRAFORM_CONTRACT_VERSION,
        "tenant_id": tid,
        "status": doc["status"],
        "project_id": project_id,
        "project_id_revision": revision or 1,
        "parent_folder": PL.TENANTS_FOLDER,
        "region": PL.RUNTIME_REGION,
        "bq_location": db["bq_location"],
        "labels": dict(N.resource_labels(tid), managed_by="vts-tenant-infra"),
        "state": {"bucket": PL.STATE_BUCKET, "prefix": N.terraform_state_prefix(tid)},
        "scheduler_state": doc["scheduler_state"],
        "bootstrap_apis": sorted(PL.TENANT_BOOTSTRAP_APIS),
        "apis": sorted(apis),
        "datasets": dict(sorted(db["datasets"].items())),
        "tables": sorted(tables, key=lambda t: (t["dataset_key"], t["table_id"])),
        "marketplaces": marketplaces,
        # T4.1: идентичность развёртывания SQL (роли организации только в ACL датасетов арендатора).
        "sql_deployer": SI.contract_block(project_id, dict(sorted(db["datasets"].items()))),
    }


def tenant_summary(doc: dict) -> dict:
    """Идентичность арендатора для потребителей: без ссылок на секреты и прочего."""
    db = doc["data_boundary"]
    return {"tenant_id": doc["tenant_id"], "kind": db["kind"], "status": doc["status"],
            "config_authority": doc["config_authority"], "gcp_project_id": db["gcp_project_id"],
            "project_id_revision": db.get("project_id_revision", 1)
            if db["kind"] == "dedicated_project" else None}


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in ("validate", "list", "runtime-env", "terraform-inputs"):
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
            print(f"{argv[0]} <tenant_id>", file=sys.stderr)
            return 3
        if argv[0] == "terraform-inputs":
            # Детерминированный JSON: отсортированные ключи, без меток времени.
            print(json.dumps(terraform_inputs(argv[1]), indent=2,
                             ensure_ascii=False, sort_keys=True))
            return 0
        # Окружение выдаётся только из целиком валидного реестра (load_tenant →
        # valid_tenants): коллизия у соседа делает небезопасным и этого арендатора.
        print(json.dumps(ozon_runtime_env(load_tenant(argv[1])), indent=2, ensure_ascii=False))
        return 0
    except RegistryInvalid as e:
        for f in e.findings:
            print(f"FAIL {f}", file=sys.stderr)
        return 1
    except ValueError as e:   # NamingError, ContractError, RuntimeReleaseError — нарушение контракта
        print(f"FAIL {e}", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001 — сбой инструмента отличаем от нарушения
        print(f"ERROR {type(e).__name__}: {e}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
