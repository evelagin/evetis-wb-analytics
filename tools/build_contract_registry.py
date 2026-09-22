#!/usr/bin/env python3
"""Собрать поадресный реестр проверок и граф доказательств. Только чтение файлов.

Реестр не пишется руками: 212 записей, поддерживаемых вручную, разойдутся с SQL за
неделю. Источники — `quality/suites.json` (метаданные набора), сами приёмочные
артефакты (состав проверок) и `docs/architecture/system_inventory.json` (объекты,
зависимости, потребители).

Результат — `quality/contract_registry.json`:

  * `checks[]`  — по одной записи на проверку: check_id, domain, pipeline,
    marketplace, severity, source_contract, execution_mode, expected_result,
    owner, type, status, dependencies, timeout, business_rule_reference.
  * `evidence_graph` — DATA ASSET → CONTRACT → CHECK → PIPELINE → CONSUMER.
    Отвечает на вопрос «если изменился объект X, какие доказательства обязаны
    быть выполнены».
  * `coverage` — что покрыто, что нет и почему нет.

Запуск: python tools/build_contract_registry.py
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_data_checks import parse_suite  # noqa: E402
from lib.acceptance import SuiteContractError  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
SUITES = REPO / "quality" / "suites.json"
UBR = REPO / "quality" / "unresolved_business_rules.json"
INVENTORY = REPO / "docs" / "architecture" / "system_inventory.json"
OUT = REPO / "quality" / "contract_registry.json"


def build(strict: bool = False) -> dict:
    reg = json.loads(SUITES.read_text(encoding="utf-8"))
    ubr = json.loads(UBR.read_text(encoding="utf-8"))
    inv = json.loads(INVENTORY.read_text(encoding="utf-8")) if INVENTORY.exists() else {"objects": {}}
    objects = inv.get("objects", {})

    checks: list[dict] = []
    asset_to_checks: dict[str, set[str]] = defaultdict(set)
    problems: list[str] = []

    for suite in reg["suites"]:
        try:
            parsed = parse_suite(suite)
        except (SuiteContractError, OSError) as e:
            problems.append(f"{suite['suite']}: {e}")
            continue
        overrides = suite.get("check_overrides", {})
        for c in parsed.checks:
            ov = overrides.get(c.check_id)
            if not c.executable:
                status, reason = "not_executable", c.not_executable_reason
            elif ov:
                status, reason = ov["status"], ov.get("reason")
            else:
                status, reason = "active", None
            checks.append({
                "check_id": c.check_id,
                "suite": suite["suite"],
                "domain": suite["domain"],
                "pipelines": suite.get("pipelines", []),
                "marketplace": suite.get("marketplace", []),
                "layer": suite.get("layer"),
                "severity": suite["severity"],
                "source_contract": suite.get("source_contract"),
                "source_artifact": f"{c.source}:{c.line}",
                "adapter": c.adapter,
                "execution_mode": suite.get("execution_mode", "read_only_select"),
                "expected_result": "status=PASS on every row",
                "owner": suite.get("owner"),
                "type": "data_contract",
                "status": status,
                "status_reason": reason,
                "gate_relevant": bool(c.executable and not (ov or {}).get("excluded_from_gate")),
                "dependencies": sorted(suite.get("scope", [])),
                "timeout_seconds": suite.get("timeout_seconds", 180),
                "freshness_requirement": suite.get("cadence"),
                "business_rule_reference": (ov or {}).get("unresolved_rule")
                                           or suite.get("business_rule_reference"),
                "label": c.label,
            })
            for asset in suite.get("scope", []):
                asset_to_checks[asset].add(c.check_id)

    if problems and strict:
        raise SuiteContractError("; ".join(problems))

    # --- граф доказательств: ASSET → CONTRACT → CHECK → PIPELINE → CONSUMER ---
    suites_by_asset: dict[str, set[str]] = defaultdict(set)
    for s in reg["suites"]:
        for a in s.get("scope", []):
            suites_by_asset[a].add(s["suite"])

    def downstream(asset: str, limit: int = 500) -> list[str]:
        """Транзитивные потребители объекта по графу вью из снимка инвентаря."""
        seen, stack = set(), [asset]
        while stack and len(seen) < limit:
            for nxt in objects.get(stack.pop(), {}).get("consumed_by", []):
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        return sorted(seen)

    graph = {}
    covered_assets = sorted(set(asset_to_checks) | set(suites_by_asset))
    for asset in covered_assets:
        obj = objects.get(asset, {})
        consumers = downstream(asset)
        graph[asset] = {
            "exists_in_production": bool(obj),
            "object_type": obj.get("type"),
            "layer": obj.get("layer"),
            "domain": obj.get("domain"),
            "contracts": sorted(suites_by_asset.get(asset, [])),
            "checks": sorted(asset_to_checks.get(asset, [])),
            "direct_consumers": obj.get("consumed_by", []),
            "transitive_consumers": consumers,
            "pipelines": sorted({p for s in reg["suites"] if asset in s.get("scope", [])
                                 for p in s.get("pipelines", [])}),
        }

    all_assets = set(objects)
    gate_assets = {a for a in covered_assets if any(
        c["gate_relevant"] for c in checks if a in c["dependencies"])}
    uncovered_with_consumers = sorted(
        a for a in all_assets - set(covered_assets)
        if objects[a].get("consumed_by") and objects[a]["type"] == "VIEW")

    active = [c for c in checks if c["status"] == "active"]
    return {
        "schema_version": 1,
        "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generated_by": "tools/build_contract_registry.py",
        "inventory_snapshot": inv.get("generated_at"),
        "summary": {
            "suites": len(reg["suites"]),
            "checks_total": len(checks),
            "checks_active": len(active),
            "checks_excluded_from_gate": sum(1 for c in checks if c["status"] in
                                             ("superseded", "stale_snapshot", "known_failing")),
            "checks_not_executable": sum(1 for c in checks if c["status"] == "not_executable"),
            "acceptance_artifacts_wired": len({f for s in reg["suites"] for f in s["files"]}),
            "acceptance_artifacts_in_backlog": len(reg["backlog"]["files"]),
            "assets_with_contract": len(covered_assets),
            "assets_under_gate": len(gate_assets),
            "unresolved_business_rules_open": sum(1 for r in ubr["rules"] if r["status"] == "open"),
            "parse_problems": len(problems),
        },
        "by_severity": {sev: sum(1 for c in active if c["severity"] == sev)
                        for sev in ("CRITICAL", "HIGH", "MEDIUM", "LOW")},
        "by_domain": {d: sum(1 for c in active if c["domain"] == d)
                      for d in sorted({c["domain"] for c in checks})},
        "checks": sorted(checks, key=lambda c: (c["suite"], c["check_id"])),
        "evidence_graph": dict(sorted(graph.items())),
        "coverage_gaps": {
            "comment": "Вью, у которых есть потребители внутри BigQuery, но нет ни одного контракта.",
            "views_without_contract": uncovered_with_consumers,
            "count": len(uncovered_with_consumers),
        },
        "unresolved_business_rules": [
            {"id": r["id"], "title": r["title"], "severity": r["severity"], "status": r["status"],
             "blocked_checks": r.get("blocked_checks", [])}
            for r in ubr["rules"]
        ],
        "parse_problems": problems,
    }


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--output", default=str(OUT))
    p.add_argument("--strict", action="store_true", help="падать, если хоть один набор не разобран")
    args = p.parse_args(argv)
    try:
        data = build(strict=args.strict)
    except SuiteContractError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 3
    Path(args.output).write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n",
                                 encoding="utf-8")
    s = data["summary"]
    print(f"{args.output}: {s['checks_total']} проверок "
          f"({s['checks_active']} активных, {s['checks_excluded_from_gate']} вне ворот, "
          f"{s['checks_not_executable']} неисполнимых); "
          f"{s['assets_with_contract']} объектов с контрактом; "
          f"{s['unresolved_business_rules_open']} нерешённых правил")
    if data["parse_problems"]:
        for pr in data["parse_problems"]:
            print(f"WARNING: {pr}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
