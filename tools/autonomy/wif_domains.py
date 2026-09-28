"""S1 по ВСЕМ доменам доверия GCP WIF, которые принимают OIDC-токены этого репозитория.

Зачем. `wif_check` моделирует пул EVETIS. У репозитория есть второй домен доверия —
`mpa-platform / tenant-infra-pool` (T3.1A, вне Terraform этого репозитория), и job'ы AE
(`id-token: write` на main) могут выпустить токен и для него. Проверять его вручную
недостаточно: новый пул, созданный завтра, так же тихо расширил бы достижимые идентичности.

Что делает:
  * каждый домен из `quality/autonomy/wif_trust_domains.json` проверяется на точных claims
    GitHub: базовые случаи A–E (`wif_check.cases`) и случаи арендатора T1–T13;
  * `--live`: сканирует ВСЕ доступные проекты, все пулы и провайдеры с issuer GitHub. Провайдер,
    чьё условие пропускает хоть один токен этого репозитория (или не вычисляется подмножеством
    CEL), обязан быть в реестре — иначе FAIL. Домен из реестра, которого нет вживую, — тоже FAIL.

Только чтение. Код выхода 0 — все домены соответствуют инварианту и неучтённых нет.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from tools.autonomy.wif_check import (MAIN, Case, CelError, WifConfig, cases, cel, claims, from_snapshot,
                                      load_terraform)

REPO = Path(__file__).resolve().parent.parent.parent
INVENTORY = REPO / "quality" / "autonomy" / "wif_trust_domains.json"
TENANT_SA = "sa-tenant-provisioner"
GITHUB_ACTIONS_BOT_ID = "41898282"         # github-actions[bot]: actor прогонов, запущенных через GITHUB_TOKEN


def load_inventory(path: Path = INVENTORY) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def tenant_cases() -> list[Case]:
    E = frozenset
    ae_branch = "refs/heads/ae/fix-x-1234abcd"
    return [
        Case("T1", "tenant-infra.yml, dispatch из main — легитимно", claims("tenant-infra.yml", MAIN), E({TENANT_SA})),
        Case("T2", "tenant-infra.yml на не-main ветке", claims("tenant-infra.yml", "refs/heads/feature-x"), E()),
        Case("T3", "двойник «tenant-infra.yml@x.yml» на main", claims("tenant-infra.yml@x.yml", MAIN), E()),
        Case("T4", "tenant-infra.yml, push в main (не dispatch)", claims("tenant-infra.yml", MAIN, event="push"), E()),
        Case("T5", "tenant-infra.yml на self-hosted раннере",
             claims("tenant-infra.yml", MAIN, runner_environment="self-hosted"), E()),
        Case("T6", "пересозданный репозиторий с тем же именем (другой repository_id)",
             claims("tenant-infra.yml", MAIN, repository_id="1111111111"), E()),
        Case("T7", "чужой владелец с тем же логином (другой repository_owner_id)",
             claims("tenant-infra.yml", MAIN, repository_owner_id="2222222"), E()),
        Case("T8", "tenant-infra.yml на ветке кандидата ae/*", claims("tenant-infra.yml", ae_branch), E()),
        Case("T9", "AE-workflow вызывает tenant-infra.yml как переиспользуемый",
             claims("autonomy-run.yml", MAIN, job_workflow="tenant-infra.yml"), E()),
        Case("T10", "tenant-infra.yml по расписанию", claims("tenant-infra.yml", MAIN, event="schedule"), E()),
        Case("T11", "dispatch через GITHUB_TOKEN (github-actions[bot])",
             claims("tenant-infra.yml", MAIN, actor_id=GITHUB_ACTIONS_BOT_ID), E()),
        Case("T12", "повторный запуск прогона владельца (actor наследуется, run_attempt 2)",
             claims("tenant-infra.yml", MAIN, run_attempt="2"), E()),
        Case("T13", "другой пользователь с правом записи", claims("tenant-infra.yml", MAIN, actor_id="12345678"), E()),
    ]


def expectations(domain: dict) -> list[Case]:
    """Полный набор случаев домена: свои ожидания + чужие случаи с ожиданием «ничего из привилегий домена»."""
    base, tenant = cases(), tenant_cases()
    if domain["expectations"] == "wif_check.cases":
        # В пуле EVETIS токены tenant-infra.yml не получают ничего.
        return base + [Case(c.id, c.title, c.claims, frozenset()) for c in tenant]
    if domain["expectations"] == "tenant_cases":
        # В пуле арендатора НИ ОДИН базовый случай (AE, deploy, infra, кандидаты) ничего не получает.
        return tenant + [Case(c.id, c.title, c.claims, frozenset()) for c in base]
    raise ValueError(f"{domain['id']}: неизвестный набор ожиданий {domain['expectations']}")


def verify_domain(cfg: WifConfig, domain: dict) -> list[dict]:
    rows = []
    priv = set(domain.get("privileged", []))
    for c in expectations(domain):
        got = cfg.obtainable(c.claims)
        rows.append({"domain": domain["id"], "case": c.id, "title": c.title, "expect": sorted(c.expect),
                     "got": sorted(got), "privileged_leak": sorted((got - c.expect) & priv),
                     "status": "PASS" if got == c.expect else "FAIL"})
    return rows


def desired_config(domain: dict) -> WifConfig:
    d = domain["desired"]
    if d["source"] == "terraform":
        return load_terraform(REPO / d["path"])
    if d["source"] == "snapshot":
        return from_snapshot(json.loads((REPO / d["path"]).read_text(encoding="utf-8")), d["path"])
    raise ValueError(f"{domain['id']}: неизвестный источник {d['source']}")


# ------------------------------------------------------------------------ живой скан ---
@dataclass
class Discovered:
    project: str
    pool: str
    provider: str
    issuer: str | None
    config: WifConfig | None
    trusts_repo: bool          # условие пропускает хоть один токен этого репозитория
    evaluable: bool            # условие вычисляется подмножеством CEL


def probe_claims() -> list[dict]:
    return [c.claims for c in cases() + tenant_cases()]


def trust_of(condition: str) -> tuple[bool, bool]:
    """(доверяет ли репозиторию, вычислимо ли). Невычислимое трактуется как доверяющее (fail closed)."""
    trusts = False
    for cl in probe_claims():
        try:
            if cel(condition or "true", {"assertion": cl}) is True:
                trusts = True
        except CelError:
            return True, False
    return trusts, True


def check_inventory(discovered: list[Discovered], inventory: dict) -> list[str]:
    findings = []
    approved = {(d["project"], d["pool"], d["provider"]): d for d in inventory["domains"]}
    live = {(x.project, x.pool, x.provider): x for x in discovered}
    for key, x in live.items():
        if x.issuer != inventory["github_issuer"]:
            continue
        if not x.evaluable:
            findings.append(f"{'/'.join(key)}: условие провайдера не вычисляется — доверие не доказано (fail closed)")
        if x.trusts_repo and key not in approved:
            findings.append(f"{'/'.join(key)}: НЕУЧТЁННЫЙ домен доверия репозитория — внести в реестр с тестами или удалить")
    for key in approved:
        if key not in live:
            findings.append(f"{'/'.join(key)}: домен из реестра не найден вживую — реестр устарел")
    return findings


def _j(*args) -> list | dict:
    out = subprocess.run(["gcloud", *args, "--format=json"], capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(f"gcloud {' '.join(args[:4])}: {out.stderr.strip()[:300]}")
    return json.loads(out.stdout or "[]")


def discover_live() -> list[Discovered]:
    """Только чтение: все проекты → пулы → провайдеры; для GitHub-провайдеров — привязки SA этого пула."""
    found = []
    for proj in _j("projects", "list"):
        pid = proj["projectId"]
        try:
            pools = _j("iam", "workload-identity-pools", "list", "--location=global", f"--project={pid}")
        except RuntimeError:
            continue           # API IAM выключен / нет прав на чтение — пулов там быть не может
        for pool in pools:
            if pool.get("state") == "DELETED":
                continue
            pool_id = pool["name"].rsplit("/", 1)[1]
            for prov in _j("iam", "workload-identity-pools", "providers", "list", f"--workload-identity-pool={pool_id}",
                           "--location=global", f"--project={pid}"):
                if prov.get("state") == "DELETED":
                    continue
                prov_id = prov["name"].rsplit("/", 1)[1]
                issuer = (prov.get("oidc") or {}).get("issuerUri")
                trusts, evaluable = trust_of(prov.get("attributeCondition") or "")
                cfg = None
                if issuer == "https://token.actions.githubusercontent.com":
                    bindings = {}
                    for sa in _j("iam", "service-accounts", "list", f"--project={pid}"):
                        pol = _j("iam", "service-accounts", "get-iam-policy", sa["email"], f"--project={pid}")
                        mine = [{**b, "members": [m for m in b["members"] if f"workloadIdentityPools/{pool_id}/" in m]}
                                for b in pol.get("bindings", [])]
                        mine = [b for b in mine if b["members"]]
                        if mine:
                            bindings[sa["email"].split("@")[0]] = mine
                    cfg = from_snapshot({"provider": prov, "service_account_bindings": bindings}, f"live {pid}/{pool_id}")
                found.append(Discovered(pid, pool_id, prov_id, issuer, cfg, trusts, evaluable))
    return found


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--live", action="store_true", help="живой скан всех проектов (read-only gcloud)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    inv = load_inventory()
    rows, findings = [], []
    if a.live:
        disc = discover_live()
        findings = check_inventory(disc, inv)
        by_key = {(x.project, x.pool, x.provider): x for x in disc}
        for d in inv["domains"]:
            x = by_key.get((d["project"], d["pool"], d["provider"]))
            if x and x.config:
                rows += verify_domain(x.config, d)
    else:
        for d in inv["domains"]:
            rows += verify_domain(desired_config(d), d)
    ok = not findings and all(r["status"] == "PASS" for r in rows)
    if a.json:
        print(json.dumps({"live": a.live, "findings": findings, "results": rows}, ensure_ascii=False, indent=2))
    else:
        for f in findings:
            print(f"FINDING {f}")
        for dom in dict.fromkeys(r["domain"] for r in rows):
            rs = [r for r in rows if r["domain"] == dom]
            print(f"{dom}: {sum(r['status'] == 'PASS' for r in rs)}/{len(rs)} PASS, "
                  f"утечек привилегий {sum(bool(r['privileged_leak']) for r in rs)}")
            for r in rs:
                if r["status"] != "PASS":
                    print(f"  FAIL {r['case']} {r['title']} → {r['got']} (ожидалось {r['expect']})")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
