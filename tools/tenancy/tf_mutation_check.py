#!/usr/bin/env python3
"""Мутационная проверка корня infra/tenant (Tenancy T3.2). Офлайн, мок провайдера.

  python tools/tenancy/tf_mutation_check.py

Каждая мутация ослабляет ровно одно критичное правило в КОПИИ корня; `terraform test`
обязан после этого упасть. Если набор тестов проходит на ослабленном корне, правило ничем
не защищено — отказ. Если текст мутации в корне не найден (правило переписали), тоже
отказ: мутацию надо обновить вместе с правилом, а не молча потерять.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ROOT = REPO / "infra" / "tenant"
sys.path.insert(0, str(REPO))

# (имя, файл, регулярное выражение, замена)
MUTATIONS = [
    ("billing guard removed", "guards.tf",
     r"    precondition \{\n      condition     = try\(length\(data\.google_project\.tenant\.billing_account\) > 0, false\)\n.*?\n    \}\n", ""),
    ("billing guard back to null-unsafe try()", "guards.tf",
     r"try\(length\(data\.google_project\.tenant\.billing_account\) > 0, false\)",
     'try(data.google_project.tenant.billing_account, "") != ""'),
    ("folder guard (get) removed", "guards.tf",
     r"    precondition \{\n      condition     = data\.google_project\.tenant\.folder_id == local\.platform\.tenants_folder_id\n.*?\n    \}\n", ""),
    ("search parent guard removed", "guards.tf",
     r"    precondition \{\n      condition     = try\(local\.found\.parent\.id.*?\n    \}\n", ""),
    ("search project-id guard removed", "guards.tf",
     r"    precondition \{\n      condition     = try\(local\.found\.project_id == var\.contract\.project_id, false\)\n.*?\n    \}\n", ""),
    ("cross-API number guard removed", "guards.tf",
     r" && try\(local\.found\.number == data\.google_project\.tenant\.number, false\)", ""),
    ("EVETIS/platform number guard removed", "guards.tf",
     r"    precondition \{\n      condition     = !contains\(concat\(local\.evetis_forbidden_markers.*?\n    \}\n", ""),
    ("PAUSED validation weakened", "variables.tf",
     r'var\.contract\.scheduler_state == "PAUSED"', 'var.contract.scheduler_state != ""'),
    ("image regex accepts tags", "variables.tf",
     r"@sha256:\[0-9a-f\]\{64\}\$", r"(@sha256:[0-9a-f]{64}|:[a-z0-9.]+)$"),
    ("EVETIS marker validation removed", "variables.tf",
     r"!anytrue\(\[for m in local\.evetis_forbidden_markers : strcontains\(jsonencode\(var\.contract\), m\)\]\)",
     "true || var.contract.contract_version == 1"),
    ("state prefix validation weakened", "variables.tf",
     r'var\.contract\.state\.prefix == "tenants/\$\{var\.contract\.tenant_id\}"',
     'startswith(var.contract.state.prefix, "")'),
    ("namespace validation weakened", "variables.tf",
     r'condition = var\.contract\.project_id == format\("mpa-t%s-%s",',
     'condition = var.contract.project_id != "" || var.contract.project_id == format("mpa-t%s-%s",'),
    ("schedulers unpaused", "modules/ozon_runtime/main.tf", r"\n  paused    = true\n", "\n  paused    = false\n"),
    # T3.3: ACL датасета авторитетный. Без него BigQuery делает создателя (провижионера) OWNER.
    ("dataset ACL dropped (creator OWNER default)", "main.tf",
     r"\n  access \{\n    role          = \"OWNER\"\n.*?\n  \}\n\n  dynamic \"access\" \{.*?\n  \}\n", "\n"),
    ("provisioner added to dataset ACL", "main.tf",
     r"(\n  access \{\n    role          = \"OWNER\"\n    special_group = \"projectOwners\"\n  \}\n)",
     "\\1\n  access {\n    role          = \"OWNER\"\n    user_by_email = \"sa-tenant-provisioner@mpa-platform.iam.gserviceaccount.com\"\n  }\n"),
    ("runtime raw grant widened to OWNER", "main.tf",
     r'role = "WRITER", user_by_email', 'role = "OWNER", user_by_email'),
    # T3.3: явный retry_count = 0 API не хранит — вечный дрейф сходимости.
    ("scheduler redundant retry_config restored", "modules/ozon_runtime/main.tf",
     r"(\n  paused    = true\n)", "\\1\n  retry_config {\n    retry_count = 0\n  }\n"),
]


def _tf_test(workdir: Path, env: dict) -> int:
    subprocess.run(["terraform", "init", "-backend=false", "-input=false"], cwd=workdir, env=env,
                   capture_output=True, text=True, check=True)
    return subprocess.run(["terraform", "test", "-no-color"], cwd=workdir, env=env,
                          capture_output=True, text=True).returncode


def _computed_fields_check(workdir: Path, env: dict) -> list[str]:
    """Правило F сканера опирается на то, что creator/last_modifier в схеме провайдера только computed.

    Проверка по схеме провайдера из lockfile: если поле станет задаваемым (optional/required),
    исключение сканера перестанет быть безопасным — отказ.
    """
    from tools.tenancy.plan_scan import COMPUTED_APPLIER_FIELDS

    # providers schema требует инициализированный backend: в отдельной копии — локальный
    # (override-файл), чтобы не нужен был GCS. Провайдер — тот же, из lockfile и кэша.
    schema_dir = workdir.parent / "schema"
    shutil.copytree(workdir, schema_dir, ignore=shutil.ignore_patterns(".terraform"))
    (schema_dir / "backend_override.tf").write_text('terraform {\n  backend "local" {}\n}\n', encoding="utf-8")
    subprocess.run(["terraform", "init", "-input=false", "-lockfile=readonly"], cwd=schema_dir, env=env,
                   capture_output=True, text=True, check=True)
    out = subprocess.run(["terraform", "providers", "schema", "-json"], cwd=schema_dir, env=env,
                         capture_output=True, text=True, check=True).stdout
    res = json.loads(out)["provider_schemas"]["registry.terraform.io/hashicorp/google"]["resource_schemas"]
    bad = []
    for rtype, fields in COMPUTED_APPLIER_FIELDS.items():
        for f in sorted(fields):
            a = res[rtype]["block"]["attributes"].get(f, {})
            ok = a.get("computed") is True and not a.get("optional") and not a.get("required")
            print(f"{'computed-only' if ok else 'CONFIGURABLE'}  {rtype}.{f}")
            if not ok:
                bad.append(f"{rtype}.{f}: не computed-only в схеме провайдера — правило F сканера небезопасно")
    return bad


def main() -> int:
    failures = []
    with tempfile.TemporaryDirectory() as tmp:
        env = dict(os.environ, TF_PLUGIN_CACHE_DIR=str(Path(tmp) / "plugins"), TF_IN_AUTOMATION="1")
        Path(env["TF_PLUGIN_CACHE_DIR"]).mkdir()
        base = Path(tmp) / "base"
        shutil.copytree(ROOT, base, ignore=shutil.ignore_patterns(".terraform"))
        if _tf_test(base, env) != 0:
            print("FAIL: немутированный корень не проходит terraform test")
            return 1
        failures += _computed_fields_check(base, env)
        for name, rel, pattern, repl in MUTATIONS:
            work = Path(tmp) / re.sub(r"\W+", "_", name)
            shutil.copytree(ROOT, work, ignore=shutil.ignore_patterns(".terraform"))
            f = work / rel
            text = f.read_text(encoding="utf-8")
            mutated, n = re.subn(pattern, repl, text, count=1, flags=re.S)
            if n != 1:
                failures.append(f"{name}: шаблон мутации не найден в {rel} — обновите мутацию вместе с правилом")
                continue
            f.write_text(mutated, encoding="utf-8")
            rc = _tf_test(work, env)
            print(f"{'caught' if rc != 0 else 'SURVIVED'}  {name}")
            if rc == 0:
                failures.append(f"{name}: terraform test прошёл на ослабленном корне")
    for x in failures:
        print(f"FAIL {x}")
    print(f"мутаций: {len(MUTATIONS)}, не пойманы: {len(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
