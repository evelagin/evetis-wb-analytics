#!/usr/bin/env python3
"""Сверка ролей организации платформы с доверенной базой (Tenancy T4.1). Только чтение.

  python tools/tenancy/platform_roles.py verify [--json]

Роли mpaSql* создал владелец 2026-09-28 (временная organizationRoleAdmin, снята). Постоянно у
владельца только roles/iam.organizationRoleViewer — этого достаточно для iam.roles.get/list.
Ожидаемые определения — tools/tenancy/sql_identity.SQL_ROLES.

Закрытый отказ (выход 1), если у любой роли:
  * её нет или она удалена (deleted);
  * стадия не GA;
  * не хватает права или есть лишнее (надмножество НЕ принимается);
  * заголовок или описание отличаются от доверенной базы;
  * есть право из sql_identity.NEVER_FOR_DEPLOYER;
а также если в организации есть ещё роль с префиксом mpaSql, которой нет в доверенной базе.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tools.tenancy import platform as PL  # noqa: E402
from tools.tenancy import sql_identity as SI  # noqa: E402


def check_roles(described: dict[str, dict | None], listed: list[dict]) -> list[str]:
    """described: role_id → ответ roles.get (None — нет роли); listed: roles.list --show-deleted."""
    out = []
    for rid, spec in SI.SQL_ROLES.items():
        live = described.get(rid)
        name = SI.role_name(rid)
        if not live:
            out.append(f"{name}: роли нет")
            continue
        if live.get("name") != name:
            out.append(f"{name}: живое имя {live.get('name')!r}")
        if live.get("deleted"):
            out.append(f"{name}: роль удалена (deleted=true)")
        if live.get("stage") != spec["stage"]:
            out.append(f"{name}: стадия {live.get('stage')!r}, ожидается {spec['stage']}")
        got = set(live.get("includedPermissions") or [])
        if got - spec["permissions"]:
            out.append(f"{name}: лишние права {sorted(got - spec['permissions'])}")
        if spec["permissions"] - got:
            out.append(f"{name}: не хватает прав {sorted(spec['permissions'] - got)}")
        if got & SI.NEVER_FOR_DEPLOYER:
            out.append(f"{name}: запрещённые деплоеру права {sorted(got & SI.NEVER_FOR_DEPLOYER)}")
        for field in ("title", "description"):
            if live.get(field) != spec[field]:
                out.append(f"{name}: {field} отличается от доверенной базы")
    known = {SI.role_name(r) for r in SI.SQL_ROLES}
    for r in listed:
        n = r.get("name", "")
        if n.startswith(SI.ORG_ROLE_PREFIX + "mpaSql") and n not in known:
            out.append(f"{n}: роль mpaSql* вне доверенной базы")
    return out


def _parse(text):
    from tools.tenancy.validation import parse_tenant_json   # единый строгий разборщик JSON
    return parse_tenant_json(text.decode("utf-8") if isinstance(text, bytes) else text)


def _gcloud(*args) -> tuple[int, str]:
    p = subprocess.run(["gcloud", *args, "--format=json"], capture_output=True, text=True)
    return p.returncode, (p.stdout if p.returncode == 0 else p.stderr)


def read_live(run=_gcloud) -> tuple[dict[str, dict | None], list[dict], dict[str, str]]:
    described, errors = {}, {}
    for rid in SI.SQL_ROLES:
        code, out = run("iam", "roles", "describe", rid, f"--organization={PL.ORGANIZATION_ID}")
        if code == 0:
            described[rid] = _parse(out)
        elif "NOT_FOUND" in out:
            described[rid] = None
        else:                             # нет прав или сеть — не «роли нет», а невозможность проверить
            errors[rid] = out.strip().splitlines()[-1][:200] if out.strip() else f"код {code}"
    code, out = run("iam", "roles", "list", f"--organization={PL.ORGANIZATION_ID}", "--show-deleted")
    if code != 0:
        errors["list"] = out.strip().splitlines()[-1][:200] if out.strip() else f"код {code}"
    listed = _parse(out) if code == 0 else []
    return described, listed, errors


def main(argv: list[str], run=_gcloud) -> int:
    if not argv or argv[0] != "verify" or len(argv) > 2 or (len(argv) == 2 and argv[1] != "--json"):
        print(__doc__, file=sys.stderr)
        return 3
    described, listed, errors = read_live(run)
    findings = [f"{k}: проверить нельзя — {v}" for k, v in sorted(errors.items())] + check_roles(described, listed)
    report = {"roles": {SI.role_name(r): {k: (d or {}).get(k) for k in ("stage", "etag", "deleted")}
                        | {"permissions": sorted((d or {}).get("includedPermissions") or [])}
                        for r, d in described.items()},
              "findings": findings}
    if argv[-1] == "--json":
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for name, r in report["roles"].items():
            print(f"{name}: stage={r['stage']} etag={r['etag']} permissions={r['permissions']}")
        for f in findings:
            print(f"FAIL {f}")
        print(f"platform-roles: нарушений {len(findings)}")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
