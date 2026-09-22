#!/usr/bin/env python3
"""Определить, какие доказательства обязаны быть выполнены после изменения. Только чтение файлов.

Вопрос, на который отвечает инструмент: «изменился набор файлов — какие объекты данных
затронуты, кто их читает, какие контракты обязаны пройти и какой это риск». Ответ
детерминированный: он выводится из графа зависимостей и реестров, а не из рассуждений
модели. Там, где граф знает ответ, догадка не нужна и вредна.

Источники (все — файлы в репозитории):
  docs/architecture/system_inventory.json  объекты, зависимости, потребители
  quality/contract_registry.json           контракты и проверки на объект
  quality/suites.json                      наборы, серьёзность, ворота
  quality/code_asset_map.json              какой путь кода какой объект производит

Тиры назначаются правилами, а не весами: произвольный числовой вес — это скрытое
бизнес-решение. Правила в `TIER_RULES` ниже, каждое проверяемо.

  python tools/impact_analysis.py --git-diff origin/main
  python tools/impact_analysis.py --files sql/current/ozon_mart/FCT_OZON_SKU_PNL_DAILY.sql
  python tools/impact_analysis.py --tiers        # классификация всех вью
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
INVENTORY = REPO / "docs" / "architecture" / "system_inventory.json"
REGISTRY = REPO / "quality" / "contract_registry.json"
SUITES = REPO / "quality" / "suites.json"
CODE_MAP = REPO / "quality" / "code_asset_map.json"

CREATE_RE = re.compile(
    r"CREATE\s+(?:OR\s+REPLACE\s+)?(?:MATERIALIZED\s+)?"
    r"(VIEW|TABLE|PROCEDURE|FUNCTION|TABLE\s+FUNCTION)\s+(?:IF\s+NOT\s+EXISTS\s+)?"
    r"`?(?:[A-Za-z0-9_-]+\.)?`?([A-Za-z0-9_]+)`?\.`?([A-Za-z0-9_]+)`?",
    re.IGNORECASE)

TIER_RULES = {
    "TIER0": "объект входит в scope набора с severity=CRITICAL, либо это витринный/дашбордный "
             "объект, который читает Metabase",
    "TIER1": "прямая зависимость объекта TIER 0",
    "TIER2": "остальные вью витрин (*_mart)",
    "TIER3": "всё остальное: вью RAW, операционные и листовые контракты",
}


def load(path: Path) -> dict:
    if not path.exists():
        raise SystemExit(f"ERROR: нет {path.relative_to(REPO)} — сначала пересобери снимок/реестр")
    return json.loads(path.read_text(encoding="utf-8"))


# ------------------------------------------------------------------- тиры ---

def classify_tiers(inv: dict, reg: dict, suites: dict) -> dict[str, str]:
    objects = inv["objects"]
    critical_scope: set[str] = set()
    for s in suites["suites"]:
        if s.get("severity") == "CRITICAL":
            critical_scope.update(s.get("scope", []))

    tier: dict[str, str] = {}
    for key, o in objects.items():
        read_by_mb = "metabase" in (o.get("external_readers") or {})
        if key in critical_scope or (read_by_mb and o.get("layer") in ("dashboard", "mart")):
            tier[key] = "TIER0"
    for key in list(tier):
        for dep in objects.get(key, {}).get("depends_on", []):
            if dep in objects and dep not in tier:
                tier[dep] = "TIER1"
    for key, o in objects.items():
        if key in tier:
            continue
        tier[key] = "TIER2" if (o["type"] == "VIEW" and o["dataset"].endswith("_mart")) else "TIER3"
    return tier


# ------------------------------------------------------- файлы → объекты ---

def assets_of_file(rel: str, code_map: dict) -> tuple[set[str], list[str]]:
    """Объекты, которые производит файл, и заметки о том, откуда это известно."""
    assets: set[str] = set()
    notes: list[str] = []
    path = REPO / rel
    if rel.endswith(".sql") and path.exists():
        text = path.read_text(encoding="utf-8", errors="replace")
        for _, ds, obj in CREATE_RE.findall(text):
            assets.add(f"{ds}.{obj}")
        if assets:
            notes.append("из текста CREATE в самом файле")
    for pref in sorted(code_map["prefixes"], key=lambda p: -len(p["path"])):
        if rel.startswith(pref["path"]):
            if pref["produces"]:
                assets.update(pref["produces"])
                notes.append(f"по карте кода: {pref['pipeline']}")
            else:
                notes.append(f"по карте кода: {pref['pipeline']} (объектов не производит)")
            break
    if not notes:
        notes.append("соответствие объектам не установлено")
    return assets, notes


def changed_files(base: str | None, explicit: list[str]) -> list[str]:
    if explicit:
        return explicit
    ref = base or "origin/main"
    out = subprocess.run(["git", "-C", str(REPO), "diff", "--name-only", f"{ref}...HEAD"],
                         capture_output=True, text=True)
    if out.returncode != 0:
        raise SystemExit(f"ERROR: git diff против {ref} не выполнен: {out.stderr.strip()[:200]}")
    staged = subprocess.run(["git", "-C", str(REPO), "status", "--porcelain"],
                            capture_output=True, text=True).stdout
    files = set(f for f in out.stdout.split() if f)
    files.update(line[3:].strip() for line in staged.splitlines() if line[3:].strip())
    return sorted(files)


# ------------------------------------------------------------------ анализ ---

def analyse(files: list[str]) -> dict:
    inv, reg, suites, code_map = load(INVENTORY), load(REGISTRY), load(SUITES), load(CODE_MAP)
    objects = inv["objects"]
    tiers = classify_tiers(inv, reg, suites)
    graph = reg["evidence_graph"]

    direct: dict[str, list[str]] = defaultdict(list)
    file_notes: dict[str, list[str]] = {}
    for rel in files:
        a, notes = assets_of_file(rel, code_map)
        file_notes[rel] = notes
        for asset in a:
            direct[asset].append(rel)

    # Транзитивные потребители затронутых объектов — это и есть радиус поражения.
    affected = set(direct)
    frontier = list(direct)
    while frontier:
        for nxt in objects.get(frontier.pop(), {}).get("consumed_by", []):
            if nxt not in affected:
                affected.add(nxt)
                frontier.append(nxt)

    required_suites: dict[str, dict] = {}
    for s in suites["suites"]:
        hit = sorted(set(s.get("scope", [])) & affected)
        if hit:
            required_suites[s["suite"]] = {
                "severity": s["severity"], "gate": bool(s.get("gate")),
                "matched_assets": hit,
                "command": f"python tools/run_data_checks.py --suite {s['suite']} "
                           f"--project <PROJECT> --token-command \"gcloud auth print-access-token\"",
            }

    touches_evidence_system = any(f.startswith(("tools/", "quality/")) for f in files)
    touches_canonical = any(f.startswith("sql/current/") for f in files)
    touches_infra = any(f.startswith("infra/") for f in files)
    touches_appsscript = any(f.startswith("apps-script/") for f in files)

    asset_tiers = sorted({tiers.get(a, "TIER3") for a in affected}) or ["—"]
    risk_tier = min(asset_tiers) if affected else ("TIER2" if touches_evidence_system else "TIER3")

    uncovered = sorted(a for a in affected
                       if a in objects and not graph.get(a, {}).get("checks"))

    required_commands = []
    if touches_evidence_system:
        required_commands.append("python -m pytest -q tools/tests")
    if touches_canonical:
        required_commands += ["python tools/validate_current_sql.py",
                              "python tools/verify_current_sql_live.py --project <PROJECT> "
                              "--token-command \"gcloud auth print-access-token\""]
    if any(f.startswith("cloud/") for f in files):
        required_commands += ["cd cloud && npm run typecheck && npm run lint && npm test"]
    if any(f.startswith(("services/", "pipelines/")) for f in files):
        required_commands.append("python -m pytest -q <затронутый пакет>")
    if touches_infra:
        required_commands.append("terraform fmt -check -recursive && terraform validate")

    return {
        "schema_version": 1,
        "inventory_snapshot": inv.get("generated_at"),
        "files": files,
        "file_notes": file_notes,
        "directly_affected_assets": {k: sorted(v) for k, v in sorted(direct.items())},
        "downstream_assets": sorted(affected - set(direct)),
        "affected_total": len(affected),
        "asset_tiers": {a: tiers.get(a, "TIER3") for a in sorted(affected)},
        "risk_tier": risk_tier,
        "risk_tier_rule": TIER_RULES.get(risk_tier, "—"),
        "required_contracts": required_suites,
        "required_commands": required_commands,
        "assets_without_contract": uncovered,
        "flags": {
            "touches_evidence_system": touches_evidence_system,
            "touches_canonical_sql": touches_canonical,
            "touches_infrastructure": touches_infra,
            "touches_apps_script_production": touches_appsscript,
        },
    }


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--files", nargs="*", default=[])
    p.add_argument("--git-diff", metavar="REF", default=None)
    p.add_argument("--tiers", action="store_true", help="напечатать классификацию всех объектов")
    p.add_argument("--output")
    args = p.parse_args(argv)

    if args.tiers:
        inv, reg, suites = load(INVENTORY), load(REGISTRY), load(SUITES)
        tiers = classify_tiers(inv, reg, suites)
        counts: dict[str, int] = defaultdict(int)
        for k, t in tiers.items():
            if inv["objects"][k]["type"] == "VIEW":
                counts[t] += 1
        for t in ("TIER0", "TIER1", "TIER2", "TIER3"):
            print(f"{t}: {counts[t]:>3} вью — {TIER_RULES[t]}")
        if args.output:
            Path(args.output).write_text(json.dumps(
                {"rules": TIER_RULES, "tiers": dict(sorted(tiers.items()))},
                indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
            print(f"записано: {args.output}")
        return 0

    files = changed_files(args.git_diff, args.files)
    if not files:
        print("изменённых файлов нет")
        return 0
    res = analyse(files)
    if args.output:
        Path(args.output).write_text(json.dumps(res, indent=1, ensure_ascii=False) + "\n",
                                     encoding="utf-8")
    print(f"файлов: {len(files)} · затронуто объектов: {res['affected_total']} · "
          f"риск: {res['risk_tier']}")
    if res["directly_affected_assets"]:
        print("\nнепосредственно:")
        for a, fs in res["directly_affected_assets"].items():
            print(f"  {res['asset_tiers'].get(a,'—'):<6} {a}   ← {', '.join(fs)}")
    if res["downstream_assets"]:
        print(f"\nниже по течению ({len(res['downstream_assets'])}): "
              + ", ".join(res["downstream_assets"][:12])
              + (" …" if len(res["downstream_assets"]) > 12 else ""))
    if res["required_contracts"]:
        print("\nобязательные контракты:")
        for name, meta in res["required_contracts"].items():
            print(f"  {meta['severity']:<9}{'GATE' if meta['gate'] else 'observe':<8}{name}"
                  f"  ({len(meta['matched_assets'])} объектов)")
    else:
        print("\nобязательных контрактов нет: затронутые объекты не покрыты ни одним набором")
    if res["required_commands"]:
        print("\nобязательные команды:")
        for c in res["required_commands"]:
            print(f"  {c}")
    if res["assets_without_contract"]:
        print(f"\nбез контракта ({len(res['assets_without_contract'])}): "
              + ", ".join(res["assets_without_contract"][:10])
              + (" …" if len(res["assets_without_contract"]) > 10 else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
