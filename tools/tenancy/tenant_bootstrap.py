#!/usr/bin/env python3
"""Bootstrap владельца для арендатора (Tenancy T4.1): привязка WIF на деплоер SQL и её сверка.

  python tools/tenancy/tenant_bootstrap.py expected <tenant_id>            # ожидаемая политика SA
  python tools/tenancy/tenant_bootstrap.py bind <tenant_id> [--execute]    # владелец; без флага — только показ
  python tools/tenancy/tenant_bootstrap.py verify <tenant_id> [--json]     # только чтение

Жизненный цикл (docs/architecture/TENANCY_DESIGN.md §4e):
  * платформа, один раз — роли организации mpaSql* (владелец; platform_roles.py verify);
  * арендатор, Terraform (провижионер) — SA sa-sql-deployer и 10 записей ACL датасетов;
  * арендатор, bootstrap владельца — ЭТОТ файл: roles/iam.workloadIdentityUser на SA деплоера
    для principalSet tenant-infra.yml@refs/heads/main. У провижионера нет
    iam.serviceAccounts.setIamPolicy, и давать его не будем, поэтому привязка — вне Terraform,
    но не вне репозитория: ожидаемое состояние выводится из sql_identity и сверяется `verify`.

bind пишет ТОЛЬКО в пустую политику SA и только с её etag: непустая политика, отличная от
ожидаемой, — отказ без записи (разбирать руками). Совпадающая — ничего не делает.

verify — закрытый отказ (выход 1), если:
  * SA деплоера нет, он выключен или у него есть ключи, созданные пользователем;
  * политика SA ≠ ровно одна привязка workloadIdentityUser без условия (отсутствие — тоже отказ);
  * деплоер или члены пула tenant-infra-pool встречаются в IAM организации, папки tenants/,
    проекта арендатора, mpa-platform или EVETIS;
  * пул привязан к любому другому SA проекта арендатора;
  * записи деплоера в ACL датасетов арендатора ≠ матрице sql_identity (роль, условие), или в ACL
    есть провижионер;
  * деплоер есть в ACL любого датасета EVETIS.
Любая ошибка чтения — тоже отказ: непроверенное не считается чистым.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tools.tenancy import platform as PL  # noqa: E402
from tools.tenancy import sql_identity as SI  # noqa: E402

BQ = "https://www.googleapis.com/bigquery/v2"
POOL_NEEDLE = f"workloadIdentityPools/{SI.WIF_POOL}/"


class BootstrapError(RuntimeError):
    """Живое состояние нельзя прочитать или изменить безопасно."""


# ── Чистые проверки (тестируются без облака) ───────────────────────────────
def _norm_bindings(policy: dict) -> list[tuple]:
    return sorted((b.get("role"), tuple(sorted(b.get("members") or [])), json.dumps(b.get("condition"), sort_keys=True))
                  for b in policy.get("bindings") or [])


def expected_policy_bindings() -> list[tuple]:
    return _norm_bindings({"bindings": SI.expected_sa_bindings()})


def check_sa(sa: dict | None, keys: list[dict], policy: dict | None) -> list[str]:
    if not sa:
        return ["SA деплоера не существует (сначала apply замороженного плана)"]
    out = []
    if sa.get("disabled"):
        out.append("SA деплоера выключен")
    if [k for k in keys if k.get("keyType") == "USER_MANAGED"]:
        out.append("у SA деплоера есть ключи, созданные пользователем")
    got = _norm_bindings(policy or {})
    if not got:
        out.append("привязки workloadIdentityUser на SA деплоера нет (bind не выполнен)")
    elif got != expected_policy_bindings():
        out.append(f"политика SA деплоера ≠ ожидаемой: {got}")
    return out


def check_foreign_policy(where: str, policy: dict, deployer: str) -> list[str]:
    """IAM вне SA деплоера: ни деплоера, ни членов пула арендаторов."""
    out = []
    for b in policy.get("bindings") or []:
        for m in b.get("members") or []:
            if deployer in m:
                out.append(f"{where}: деплоер SQL получил {b.get('role')} — только ACL датасетов допустимы")
            if POOL_NEEDLE in m:
                out.append(f"{where}: член пула {SI.WIF_POOL} получил {b.get('role')} вне SA деплоера")
    return out


def check_tenant_sa_policies(policies: dict[str, dict], deployer: str) -> list[str]:
    """Пул tenant-infra-pool привязан только к SA деплоера в проекте арендатора."""
    out = []
    for email, pol in sorted(policies.items()):
        if email == deployer:
            continue
        for b in pol.get("bindings") or []:
            if any(POOL_NEEDLE in m for m in b.get("members") or []):
                out.append(f"{email}: привязка пула {SI.WIF_POOL} ({b.get('role')}) — неожиданная привилегия")
    return out


def _acl_key(a: dict) -> tuple:
    c = a.get("condition")
    return (a.get("role"), a.get("userByEmail") or a.get("iamMember", "").removeprefix("serviceAccount:"),
            None if not c else (c.get("title") or "", c.get("description") or "", c.get("expression") or ""))


def check_tenant_acls(project: str, datasets: dict[str, str], acls: dict[str, list[dict]]) -> list[str]:
    deployer, out = SI.deployer_email(project), []
    want: dict[str, set] = {ds: set() for ds in datasets.values()}
    for g in SI.dataset_grants(project, datasets):
        c = g["condition"]
        want[datasets[g["dataset_key"]]].add(
            (g["role"], deployer, None if not c else (c["title"], c["description"], c["expression"])))
    for ds, entries in sorted(acls.items()):
        got = {_acl_key(a) for a in entries if deployer in (a.get("userByEmail") or a.get("iamMember") or "")}
        if got != want.get(ds, set()):
            out.append(f"{ds}: записи деплоера {sorted(got, key=repr)} ≠ матрице {sorted(want.get(ds, set()), key=repr)}")
        if any(PL.PROVISIONER_SA in json.dumps(a) for a in entries):
            out.append(f"{ds}: провижионер в ACL данных арендатора")
    for ds in sorted(set(want) - set(acls)):
        out.append(f"{ds}: ACL не прочитан")
    return out


def check_evetis_acls(acls: dict[str, list[dict]], deployer: str) -> list[str]:
    return [f"EVETIS {ds}: деплоер SQL в ACL" for ds, entries in sorted(acls.items())
            if any(deployer in json.dumps(a) for a in entries)]


# ── Живое чтение (владелец, gcloud + BigQuery REST) ─────────────────────────
def _parse(text):
    from tools.tenancy.validation import parse_tenant_json   # единый строгий разборщик JSON
    return parse_tenant_json(text.decode("utf-8") if isinstance(text, bytes) else text)


def _gcloud(*args) -> dict | list:
    p = subprocess.run(["gcloud", *args, "--format=json"], capture_output=True, text=True)
    if p.returncode != 0:
        raise BootstrapError(f"gcloud {' '.join(args[:3])}: {(p.stderr.strip().splitlines() or ['?'])[-1][:200]}")
    return _parse(p.stdout or "null")


def _bq(path: str) -> dict:
    token = subprocess.run(["gcloud", "auth", "print-access-token"], capture_output=True, text=True, check=True).stdout.strip()
    req = urllib.request.Request(f"{BQ}/{path}", headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return _parse(r.read())
    except urllib.error.HTTPError as e:
        raise BootstrapError(f"BigQuery {path.split('?')[0]}: HTTP {e.code}") from None


def _sa_state(project: str, email: str) -> tuple[dict | None, list, dict | None]:
    try:
        sa = _gcloud("iam", "service-accounts", "describe", email, f"--project={project}")
    except BootstrapError as e:
        if "NOT_FOUND" in str(e) or "does not exist" in str(e):
            return None, [], None
        raise
    keys = _gcloud("iam", "service-accounts", "keys", "list", f"--iam-account={email}", f"--project={project}")
    policy = _gcloud("iam", "service-accounts", "get-iam-policy", email, f"--project={project}")
    return sa, keys or [], policy or {}


def verify_live(contract: dict) -> tuple[list[str], dict]:
    project, datasets = contract["project_id"], contract["datasets"]
    deployer = SI.deployer_email(project)
    sa, keys, policy = _sa_state(project, deployer)
    findings = check_sa(sa, keys, policy)
    policies = {
        f"organizations/{PL.ORGANIZATION_ID}": _gcloud("organizations", "get-iam-policy", PL.ORGANIZATION_ID),
        PL.TENANTS_FOLDER: _gcloud("resource-manager", "folders", "get-iam-policy", PL.TENANTS_FOLDER_ID),
        f"projects/{project}": _gcloud("projects", "get-iam-policy", project),
        f"projects/{PL.PLATFORM_PROJECT_ID}": _gcloud("projects", "get-iam-policy", PL.PLATFORM_PROJECT_ID),
        f"projects/{PL.EVETIS_PROJECT_ID}": _gcloud("projects", "get-iam-policy", PL.EVETIS_PROJECT_ID),
    }
    for where, pol in policies.items():
        findings += check_foreign_policy(where, pol or {}, deployer)
    sa_policies = {a["email"]: _gcloud("iam", "service-accounts", "get-iam-policy", a["email"], f"--project={project}")
                   for a in _gcloud("iam", "service-accounts", "list", f"--project={project}") or []}
    findings += check_tenant_sa_policies(sa_policies, deployer)
    acls = {ds: _bq(f"projects/{project}/datasets/{ds}?accessPolicyVersion=3").get("access") or []
            for ds in datasets.values()}
    findings += check_tenant_acls(project, datasets, acls)
    evetis = {}
    for d in _bq(f"projects/{PL.EVETIS_PROJECT_ID}/datasets?all=true&maxResults=1000").get("datasets") or []:
        ds = d["datasetReference"]["datasetId"]
        evetis[ds] = _bq(f"projects/{PL.EVETIS_PROJECT_ID}/datasets/{ds}?accessPolicyVersion=3").get("access") or []
    findings += check_evetis_acls(evetis, deployer)
    evidence = {"deployer": deployer, "sa_policy_etag": (policy or {}).get("etag"),
                "sa_bindings": (policy or {}).get("bindings"), "user_managed_keys": len(
                    [k for k in keys if k.get("keyType") == "USER_MANAGED"]),
                "tenant_datasets_checked": sorted(acls), "evetis_datasets_checked": len(evetis),
                "iam_policies_checked": sorted(policies), "tenant_sa_policies_checked": len(sa_policies)}
    return findings, evidence


def bind(contract: dict, execute: bool) -> int:
    project = contract["project_id"]
    deployer = SI.deployer_email(project)
    sa, keys, policy = _sa_state(project, deployer)
    if not sa:
        raise BootstrapError("SA деплоера не существует — сначала apply замороженного плана")
    if [k for k in keys if k.get("keyType") == "USER_MANAGED"]:
        raise BootstrapError("у SA деплоера есть ключи пользователя — не привязываем")
    got = _norm_bindings(policy)
    if got == expected_policy_bindings():
        print(f"{deployer}: привязка уже ровно ожидаемая (etag {policy.get('etag')}) — ничего не делаем")
        return 0
    if got:
        raise BootstrapError(f"политика SA непустая и отличается от ожидаемой: {got} — запись запрещена")
    body = {"bindings": SI.expected_sa_bindings(), "etag": policy.get("etag"), "version": 1}
    print(f"{deployer}: пустая политика (etag {policy.get('etag')}) → {json.dumps(body['bindings'], ensure_ascii=False)}")
    if not execute:
        print("только показ: для записи — --execute (владелец)")
        return 0
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(body, f)
    _gcloud("iam", "service-accounts", "set-iam-policy", deployer, f.name, f"--project={project}")
    _sa, _k, after = _sa_state(project, deployer)
    if _norm_bindings(after) != expected_policy_bindings():
        raise BootstrapError("после записи политика SA не равна ожидаемой")
    print(f"{deployer}: привязка записана, etag {after.get('etag')}")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[0] not in ("expected", "bind", "verify"):
        print(__doc__, file=sys.stderr)
        return 3
    from tools.tenancy.tenant_infra import contract_for
    cmd, tenant_id, flags = argv[0], argv[1], set(argv[2:])
    if flags - {"--execute", "--json"} or ("--execute" in flags and cmd != "bind"):
        print(__doc__, file=sys.stderr)
        return 3
    contract = contract_for(tenant_id)
    try:
        if cmd == "expected":
            print(json.dumps({"service_account": SI.deployer_email(contract["project_id"]),
                              "bindings": SI.expected_sa_bindings()}, ensure_ascii=False, indent=2))
            return 0
        if cmd == "bind":
            return bind(contract, "--execute" in flags)
        findings, evidence = verify_live(contract)
    except BootstrapError as e:
        print(f"FAIL {e}", file=sys.stderr)
        return 1
    if "--json" in flags:
        print(json.dumps({"findings": findings, "evidence": evidence}, ensure_ascii=False, indent=2))
    else:
        for f in findings:
            print(f"FAIL {f}")
        print(f"tenant-bootstrap verify {tenant_id}: нарушений {len(findings)}")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
