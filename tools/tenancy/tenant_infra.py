#!/usr/bin/env python3
"""Единственная точка входа в Terraform арендатора (Tenancy T3.2).

  python tools/tenancy/tenant_infra.py render <tenant_id> <out_dir>
  python tools/tenancy/tenant_infra.py plan   <tenant_id> <work_dir>

Оператор задаёт ТОЛЬКО tenant_id. Всё остальное выводит реестр:
  * контракт   — registry.terraform_inputs(load_tenant(tenant_id));
  * бакет и префикс state — из контракта (platform.STATE_BUCKET, naming.terraform_state_prefix);
  * образ      — только из infra/tenant/runtime_release.json в main.

Произвольных команд Terraform, префикса, проекта или образа на входе нет. Команды
apply здесь НЕТ: в T3.2 план не применяется (применение — отдельные ворота T3.3).

plan: fmt -check → init (backend из контракта) → validate → plan -out → show -json →
сканер плана (plan_scan.py). Любой шаг с ненулевым кодом — отказ.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tools.tenancy import naming as N  # noqa: E402
from tools.tenancy import platform as PL  # noqa: E402
from tools.tenancy.registry import (TENANTS_DIR, RegistryInvalid, load_tenant,  # noqa: E402
                                    terraform_inputs)

TENANT_ROOT = REPO / "infra" / "tenant"
CONTRACT_FILE = "contract.auto.tfvars.json"


class TenantInfraError(RuntimeError):
    """Контракт или шаг Terraform не прошёл — дальше не идём."""


def contract_for(tenant_id: str, root: Path = TENANTS_DIR) -> dict:
    """Контракт арендатора — только через реестр (load_tenant → terraform_inputs)."""
    N.check_tenant_id(tenant_id)
    contract = terraform_inputs(load_tenant(tenant_id, root))
    if contract["state"]["prefix"] != N.terraform_state_prefix(tenant_id):
        raise TenantInfraError("префикс state в контракте не равен выводу naming")
    if contract["state"]["bucket"] != PL.STATE_BUCKET:
        raise TenantInfraError("бакет state в контракте не равен бакету платформы")
    return contract


def render(tenant_id: str, out_dir: Path, root: Path = TENANTS_DIR) -> dict:
    """Записать контракт (tfvars) и конфигурацию backend. Секретов в них нет по построению."""
    contract = contract_for(tenant_id, root)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / CONTRACT_FILE).write_text(
        json.dumps({"contract": contract}, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8")
    backend = {"bucket": contract["state"]["bucket"], "prefix": contract["state"]["prefix"]}
    (out_dir / "backend.json").write_text(json.dumps(backend, sort_keys=True) + "\n", encoding="utf-8")
    return contract


def _tf(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    proc = subprocess.run(["terraform", *args], cwd=cwd, text=True, capture_output=True)
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    if proc.returncode != 0:
        raise TenantInfraError(f"terraform {args[0]} завершился с кодом {proc.returncode}")
    return proc


def plan(tenant_id: str, work_dir: Path) -> int:
    from tools.tenancy.plan_scan import scan_plan

    contract = render(tenant_id, work_dir)
    if contract["marketplaces"].get("ozon") and not contract["marketplaces"]["ozon"]["runtime_image"]:
        raise TenantInfraError("в infra/tenant/runtime_release.json нет утверждённого образа Ozon "
                               "(выпуск — ворота T3.2b): план арендатора невозможен")
    var_file = str((work_dir / CONTRACT_FILE).resolve())
    plan_file = str((work_dir / "tenant.tfplan").resolve())
    _tf(["fmt", "-check", "-recursive"], TENANT_ROOT)
    _tf(["init", "-input=false", "-reconfigure",
         f"-backend-config=bucket={contract['state']['bucket']}",
         f"-backend-config=prefix={contract['state']['prefix']}"], TENANT_ROOT)
    _tf(["validate"], TENANT_ROOT)
    _tf(["plan", "-input=false", "-lock-timeout=120s", f"-var-file={var_file}", f"-out={plan_file}"],
        TENANT_ROOT)
    shown = _tf(["show", "-json", plan_file], TENANT_ROOT)
    (work_dir / "plan.json").write_text(shown.stdout, encoding="utf-8")
    from tools.tenancy.validation import parse_tenant_json   # единый строгий разборщик JSON
    findings = scan_plan(parse_tenant_json(shown.stdout), contract)
    for f in findings:
        print(f"FAIL plan-scan {f}", file=sys.stderr)
    print(f"plan-scan: нарушений {len(findings)}")
    return 1 if findings else 0


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[0] not in ("render", "plan"):
        print(__doc__, file=sys.stderr)
        return 3
    try:
        if argv[0] == "render":
            render(argv[1], Path(argv[2]))
            print(f"контракт {argv[1]} записан в {argv[2]}")
            return 0
        return plan(argv[1], Path(argv[2]))
    except RegistryInvalid as e:
        for f in e.findings:
            print(f"FAIL {f}", file=sys.stderr)
        return 1
    except (ValueError, TenantInfraError) as e:
        print(f"FAIL {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
