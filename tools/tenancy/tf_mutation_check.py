#!/usr/bin/env python3
"""Мутационная проверка корня infra/tenant (Tenancy T3.2). Офлайн, мок провайдера.

  python tools/tenancy/tf_mutation_check.py

Каждая мутация ослабляет ровно одно критичное правило в КОПИИ корня; `terraform test`
обязан после этого упасть. Если набор тестов проходит на ослабленном корне, правило ничем
не защищено — отказ. Если текст мутации в корне не найден (правило переписали), тоже
отказ: мутацию надо обновить вместе с правилом, а не молча потерять.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "infra" / "tenant"

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
]


def _tf_test(workdir: Path, env: dict) -> int:
    subprocess.run(["terraform", "init", "-backend=false", "-input=false"], cwd=workdir, env=env,
                   capture_output=True, text=True, check=True)
    return subprocess.run(["terraform", "test", "-no-color"], cwd=workdir, env=env,
                          capture_output=True, text=True).returncode


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
