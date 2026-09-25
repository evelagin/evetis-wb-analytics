#!/usr/bin/env python3
"""Сканер плана Terraform арендатора (Tenancy T3.2).

  python tools/tenancy/plan_scan.py <plan.json> <tenant_id>

Вход — `terraform show -json <plan>` и контракт арендатора из реестра. План пригоден
к применению, только если нарушений 0. Правила (все — отказ):

  1. Ни одного идентификатора EVETIS где угодно в JSON плана: ID и номер проекта,
     state EVETIS, evetis_ref, секреты EVETIS_OZON_ (platform.EVETIS_FORBIDDEN_MARKERS).
  2. Только разрешённые типы ресурсов. Явно запрещены: сам проект, биллинг, папки и
     организация, авторитетный IAM проекта, ключи SA, ВЕРСИИ секретов, state/Storage,
     реестр образов, WIF.
  3. Действия только create / update / no-op / read. delete и replace — отказ:
     удаление — отдельные ворота (OD-10), не побочный эффект плана.
  4. Любая ссылка projects/<id> и любой домен <id>.iam.gserviceaccount.com — только
     проект арендатора. Единственное исключение — образ runtime из реестра платформы в
     поле image контейнера Cloud Run, и только ровно утверждённый digest контракта.
  5. Роль на проект — только из allow-list (как условие projectIamAdmin провижионера).
  6. Расписания — только paused = true.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tools.tenancy import platform as PL  # noqa: E402

ALLOWED_TYPES = frozenset({
    "terraform_data",
    "google_project_service",
    "google_bigquery_dataset", "google_bigquery_table", "google_bigquery_dataset_iam_member",
    "google_project_iam_member",
    "google_service_account",
    "google_secret_manager_secret", "google_secret_manager_secret_iam_member",
    "google_cloud_run_v2_job", "google_cloud_run_v2_job_iam_member",
    "google_cloud_scheduler_job",
})
FORBIDDEN_TYPE_PATTERNS = (
    r"^google_project$", r"^google_project_iam_(policy|binding)$", r"^google_billing_",
    r"^google_folder", r"^google_organization", r"^google_service_account_key$",
    r"^google_secret_manager_secret_version$", r"^google_storage_", r"^google_artifact_registry_",
    r"^google_iam_workload_identity_pool", r"^google_project_default_service_accounts$",
)
ALLOWED_ACTIONS = ({"create"}, {"update"}, {"no-op"}, {"read"})
PROJECT_ROLE_ALLOWLIST = frozenset({"roles/bigquery.jobUser"})
_PROJECT_REF = re.compile(r"projects/([a-z][a-z0-9-]{4,28}[a-z0-9])(?=/|$)")
_SA_DOMAIN = re.compile(r"@([a-z][a-z0-9-]{4,28}[a-z0-9])\.iam\.gserviceaccount\.com")


def _strings(node, path="$"):
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _strings(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _strings(v, f"{path}[{i}]")
    elif isinstance(node, str):
        yield path, node


def scan_plan(plan: dict, contract: dict) -> list[str]:
    findings: list[str] = []
    project = contract["project_id"]
    ozon = contract["marketplaces"].get("ozon") or {}
    approved_image = ozon.get("runtime_image")

    # 1. EVETIS — нигде, включая configuration, prior_state и переменные.
    for path, s in _strings(plan):
        for marker in PL.EVETIS_FORBIDDEN_MARKERS:
            if marker in s:
                findings.append(f"{path}: идентификатор EVETIS {marker!r}")

    for rc in plan.get("resource_changes", []):
        addr, rtype = rc.get("address", "?"), rc.get("type", "")
        actions = set(rc.get("change", {}).get("actions", []))
        after = rc.get("change", {}).get("after") or {}
        # 2. Типы ресурсов.
        if any(re.search(p, rtype) for p in FORBIDDEN_TYPE_PATTERNS):
            findings.append(f"{addr}: запрещённый тип ресурса {rtype}")
        elif rc.get("mode") == "managed" and rtype not in ALLOWED_TYPES:
            findings.append(f"{addr}: тип {rtype} не входит в разрешённые для арендатора")
        # 3. Действия.
        if actions not in ALLOWED_ACTIONS:
            findings.append(f"{addr}: действие {sorted(actions)} запрещено (delete/replace — отдельные ворота)")
        if rc.get("mode") != "managed":
            continue
        # 4. Только проект арендатора.
        if "project" in after and after["project"] != project:
            findings.append(f"{addr}: project={after['project']!r}, ожидается {project!r}")
        for path, s in _strings(after, addr):
            if s.startswith(PL.RUNTIME_REGISTRY + "/"):
                if not (rtype == "google_cloud_run_v2_job" and path.endswith(".image")
                        and s == approved_image):
                    findings.append(f"{path}: ссылка на реестр платформы вне утверждённого образа")
                continue
            for pid in _PROJECT_REF.findall(s) + _SA_DOMAIN.findall(s):
                if pid != project:
                    findings.append(f"{path}: ссылка на чужой проект {pid!r}")
        # 5. Роли на проект.
        if rtype == "google_project_iam_member" and after.get("role") not in PROJECT_ROLE_ALLOWLIST:
            findings.append(f"{addr}: роль {after.get('role')!r} вне allow-list")
        # 6. Расписания только на паузе.
        if rtype == "google_cloud_scheduler_job" and after.get("paused") is not True:
            findings.append(f"{addr}: расписание не на паузе")
        # Образ — ровно утверждённый.
        if rtype == "google_cloud_run_v2_job":
            for path, s in _strings(after, addr):
                if path.endswith(".image") and s != approved_image:
                    findings.append(f"{path}: образ {s!r} не равен утверждённому digest")
    return findings


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 3
    from tools.tenancy.tenant_infra import contract_for
    from tools.tenancy.validation import parse_tenant_json   # единый строгий разборщик JSON
    plan = parse_tenant_json(Path(argv[0]).read_text(encoding="utf-8"))
    findings = scan_plan(plan, contract_for(argv[1]))
    for f in findings:
        print(f"FAIL {f}")
    print(f"plan-scan: нарушений {len(findings)}")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
