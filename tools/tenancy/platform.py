"""Факты плоскости управления VTS (Tenancy T3.1B) — единственное место в Python.

Это не правила вывода имён (они в naming.py), а зафиксированные значения физической
инфраструктуры, созданной владельцем 2026-09-25 (T3.1A/T3.1B). Их читают:

  * registry.py terraform-inputs — контракт для Terraform арендатора;
  * tenant_infra.py — обёртка init/plan (бакет state, префикс);
  * plan_scan.py — сканер плана (что считается EVETIS и платформой);
  * тесты — сверяют эти значения с infra/tenant/platform.tf и tenant-infra.yml.

Любое расхождение между этим файлом, infra/tenant/platform.tf и
.github/workflows/tenant-infra.yml — ошибка CI (tools/tests/test_tenancy_t32.py).
"""
from __future__ import annotations

import re
from pathlib import Path

from tools.tenancy import naming as N

# ── Иерархия (T3.1A) ───────────────────────────────────────────────────────
ORGANIZATION_ID = "1043233412973"
TENANTS_FOLDER_ID = "881419274207"
TENANTS_FOLDER = f"folders/{TENANTS_FOLDER_ID}"

# ── Проект платформы (T3.1B) ───────────────────────────────────────────────
PLATFORM_PROJECT_ID = "mpa-platform"
PLATFORM_PROJECT_NUMBER = "777428383056"
STATE_BUCKET = "mpa-platform-tfstate-777428383056"
RUNTIME_REGION = "europe-west1"
RUNTIME_REPOSITORY = "mpa-runtime"
RUNTIME_REGISTRY = f"{RUNTIME_REGION}-docker.pkg.dev/{PLATFORM_PROJECT_ID}/{RUNTIME_REPOSITORY}"
PROVISIONER_SA = f"sa-tenant-provisioner@{PLATFORM_PROJECT_ID}.iam.gserviceaccount.com"
WIF_PROVIDER = (f"projects/{PLATFORM_PROJECT_NUMBER}/locations/global/workloadIdentityPools/"
                "tenant-infra-pool/providers/github-tenant-infra")
TENANT_INFRA_WORKFLOW = ".github/workflows/tenant-infra.yml"
GITHUB_REPOSITORY = "evelagin/evetis-wb-analytics"
# Условие провайдера WIF — дословно как в живом mpa-platform (T3.1B, 2026-09-25).
WIF_ATTRIBUTE_CONDITION = (
    "assertion.repository_id == '1260095567' && assertion.repository_owner_id == '286048501' && "
    "assertion.repository == 'evelagin/evetis-wb-analytics' && assertion.ref == 'refs/heads/main' && "
    "assertion.ref_type == 'branch' && assertion.workflow_ref == "
    "'evelagin/evetis-wb-analytics/.github/workflows/tenant-infra.yml@refs/heads/main' && "
    "assertion.event_name == 'workflow_dispatch' && assertion.runner_environment == 'github-hosted'")

# ── API проекта арендатора ────────────────────────────────────────────────
# Провайдер Terraform арендатора ведёт квоту на проект арендатора
# (user_project_override), а не на mpa-platform. Поэтому три API включает ЧЕЛОВЕК
# при создании проекта (OD-4), до первого плана: без них нельзя ни прочитать проект,
# ни включить остальные API. Всё прочее включает модуль (infra/tenant/apis.tf).
TENANT_BOOTSTRAP_APIS = ("cloudbilling.googleapis.com", "cloudresourcemanager.googleapis.com",
                         "serviceusage.googleapis.com")
TENANT_BASE_APIS = ("iam.googleapis.com", "logging.googleapis.com")

# ── EVETIS: всё, что не может появиться в плане выделенного арендатора ────
EVETIS_PROJECT_ID = N.LEGACY_EVETIS["gcp_project_id"]
EVETIS_PROJECT_NUMBER = "37074083763"
EVETIS_FORBIDDEN_MARKERS = (
    EVETIS_PROJECT_ID,                                  # ID проекта и домен его SA
    EVETIS_PROJECT_NUMBER,                              # номер: сервис-агенты, compute SA
    f"evetis-wb-tfstate-{EVETIS_PROJECT_NUMBER}",       # state EVETIS
    "evetis_ref",                                       # справочник EVETIS (у арендатора — ref)
    "EVETIS_OZON_",                                     # префикс секретов EVETIS
)

# ── Утверждённый образ runtime (выпуск — T3.2b, файл infra/tenant/runtime_release.json) ──
RUNTIME_RELEASE_FILE = "infra/tenant/runtime_release.json"
RUNTIME_IMAGE_RE = re.compile(re.escape(RUNTIME_REGISTRY) + r"/[a-z0-9][a-z0-9._-]*@sha256:[0-9a-f]{64}")


class RuntimeReleaseError(ValueError):
    """Файл выпуска runtime невалиден или ссылка на образ изменяема."""


def check_runtime_image(ref) -> str | None:
    """None (не утверждён) или неизменяемая ссылка на образ платформы по digest.

    Отвергается всё остальное: пустая строка, тег (включая latest), реестр EVETIS,
    любой другой реестр, digest не той длины.
    """
    if ref is None:
        return None
    if not isinstance(ref, str) or not RUNTIME_IMAGE_RE.fullmatch(ref):
        raise RuntimeReleaseError(
            f"образ {ref!r} не является неизменяемой ссылкой {RUNTIME_REGISTRY}/<image>@sha256:<64 hex>")
    return ref


def load_runtime_release(repo_root) -> dict[str, str | None]:
    from tools.tenancy.validation import parse_tenant_json
    doc = parse_tenant_json((Path(repo_root) / RUNTIME_RELEASE_FILE).read_text(encoding="utf-8"))
    if not isinstance(doc, dict) or doc.get("schema_version") != 1 or not isinstance(doc.get("images"), dict):
        raise RuntimeReleaseError(f"{RUNTIME_RELEASE_FILE}: ожидается schema_version=1 и объект images")
    return {mp: check_runtime_image(ref) for mp, ref in sorted(doc["images"].items())}
