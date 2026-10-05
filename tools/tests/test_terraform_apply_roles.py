"""Состав проектных ролей sa-terraform-apply — явный, без широких ролей.

Любое расширение списка local.terraform_apply_roles (infra/terraform/iam.tf) должно ломать
этот тест и проходить через ревью: SA применяет всю конфигурацию из infra.yml.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
IAM_TF = REPO / "infra" / "terraform" / "iam.tf"

EXPECTED = {
    "roles/run.admin",
    "roles/cloudscheduler.admin",
    "roles/artifactregistry.admin",
    "roles/secretmanager.admin",
    "roles/iam.serviceAccountAdmin",
    "roles/iam.workloadIdentityPoolAdmin",
    "roles/resourcemanager.projectIamAdmin",
    "roles/serviceusage.serviceUsageAdmin",
    "roles/bigquery.admin",
    # DRO-1 Gate B: политики, лог-метрика и чтение существующего канала.
    "roles/monitoring.alertPolicyEditor",
    "roles/monitoring.notificationChannelViewer",
    "roles/logging.configWriter",
}

FORBIDDEN = {
    "roles/owner", "roles/editor",
    "roles/monitoring.admin", "roles/monitoring.editor",
    "roles/monitoring.notificationChannelEditor",
    "roles/logging.admin",
}


def _apply_roles() -> list[str]:
    text = IAM_TF.read_text(encoding="utf-8")
    block = re.search(r"terraform_apply_roles\s*=\s*\[(.*?)\]", text, re.S).group(1)
    block = "\n".join(line.split("#")[0] for line in block.splitlines())
    return re.findall(r'"(roles/[\w.]+)"', block)


def test_apply_roles_are_exactly_the_reviewed_set():
    roles = _apply_roles()
    assert len(roles) == len(set(roles)), "дубликат роли"
    assert set(roles) == EXPECTED


def test_no_broad_roles_for_apply_sa():
    assert not set(_apply_roles()) & FORBIDDEN
