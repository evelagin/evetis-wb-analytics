#!/usr/bin/env python3
"""Реестр арендаторов: tenants/<tenant_id>/tenant.json (Tenancy T1, ADR-08).

Офлайн-инструмент: ни облака, ни сети, ни секретов.

  python tools/tenancy/registry.py validate            # ворота CI
  python tools/tenancy/registry.py runtime-env <tenant_id>

Коды выхода validate: 0 — реестр валиден, 1 — есть нарушения, 3 — сбой инструмента.

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
from tools.tenancy.validation import Finding, validate_registry  # noqa: E402

TENANTS_DIR = REPO / "tenants"
SKIP_DIRS = {"_schema"}


def load_documents(root: Path = TENANTS_DIR) -> tuple[list[tuple[str, str, object]], list[Finding]]:
    """[(source, имя_каталога, документ)] и нарушения, найденные при чтении."""
    docs, problems = [], []
    for d in sorted(p for p in root.iterdir() if p.is_dir() and p.name not in SKIP_DIRS):
        src = str((d / "tenant.json").relative_to(root.parent)) if root.parent in d.parents else str(d)
        f = d / "tenant.json"
        if not f.exists():
            problems.append(Finding(src, "$", "registry", "в каталоге арендатора нет tenant.json"))
            continue
        extra = sorted(p.name for p in d.iterdir() if p.name != "tenant.json")
        if extra:
            # В каталоге арендатора не место ничему, кроме контракта: ни ключам,
            # ни выгрузкам, ни «временным» заметкам с учётными данными.
            problems.append(Finding(src, "$", "registry",
                                    f"в каталоге арендатора посторонние файлы: {extra}"))
        try:
            doc = json.loads(f.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            problems.append(Finding(src, "$", "json", f"не JSON: {type(e).__name__}"))
            continue
        docs.append((src, d.name, doc))
    stray = sorted(p.name for p in root.iterdir() if p.is_file())
    if stray:
        problems.append(Finding(str(root.name), "$", "registry",
                                f"файлы в корне tenants/ вне каталогов арендаторов: {stray}"))
    return docs, problems


def validate_root(root: Path = TENANTS_DIR) -> list[Finding]:
    docs, problems = load_documents(root)
    return problems + validate_registry(docs)


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
    N.check_tenant_id(tenant_id, legacy=(tenant_id == N.LEGACY_TENANT_ID))
    return json.loads((root / tenant_id / "tenant.json").read_text(encoding="utf-8"))


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in ("validate", "runtime-env"):
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
        if len(argv) != 2:
            print("runtime-env <tenant_id>", file=sys.stderr)
            return 3
        # Окружение выдаётся только из целиком валидного реестра: коллизия у соседа
        # (например, тот же проект) делает небезопасным и этот арендатор.
        findings = validate_root()
        if findings:
            for f in findings:
                print(f"FAIL {f}", file=sys.stderr)
            return 1
        print(json.dumps(ozon_runtime_env(load_tenant(argv[1])), indent=2, ensure_ascii=False))
        return 0
    except N.NamingError as e:
        print(f"FAIL {e}", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001 — сбой инструмента отличаем от нарушения
        print(f"ERROR {type(e).__name__}: {e}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
