#!/usr/bin/env python3
"""Единая точка сбора доказательств завершённости задачи. Только чтение.

`docs/architecture/DEFINITION_OF_DONE.md` описывает, когда задача закончена. Этот
инструмент делает описание исполнимым: одна команда собирает доказательства по всем
направлениям и возвращает машинный вердикт.

Направления и их источники:

  REPOSITORY        рабочее дерево, достижимость HEAD из origin/main, тесты инструментов
  SQL_VALIDATION    tools/validate_current_sql.py (контракт sql/current, C1–C18)
  GIT_PROD_PARITY   tools/verify_current_sql_live.py (read-only сверка с production)
  DATA_CONTRACTS    tools/run_data_checks.py по наборам, которые требует изменение
  PIPELINE_HEALTH   wb_ops.V_OPS_CURRENT_HEALTH и журнал прогонов Ozon
  REGRESSION        критичные наборы ворот, которых изменение не касалось
  UNRESOLVED_RULES  quality/unresolved_business_rules.json

Состав наборов определяет не человек и не модель, а `tools/impact_analysis.py` по
графу зависимостей: где граф знает ответ, догадка запрещена.

Вердикт: PASS (0) · FAIL (1) · BLOCKED (2) · ERROR (3).
BLOCKED — доказательство не получено (нет доступа, направление не покрыто, вернулся
EMPTY). Это НЕ успех и не провал: это отсутствие доказательства, и задача с ним
не считается завершённой.

  python tools/verify_task.py --project <PROJECT> --token-command "gcloud auth print-access-token"
  python tools/verify_task.py --offline           # только то, что не требует BigQuery
  python tools/verify_task.py --full --project …  # все наборы ворот, не только затронутые
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import impact_analysis  # noqa: E402
from lib.bq_readonly import BigQueryError, ReadOnlyBigQuery, resolve_token  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
DEFAULT_ARTIFACT_DIR = REPO / "artifacts" / "verification"

PASS, FAIL, BLOCKED, ERROR = "PASS", "FAIL", "BLOCKED", "ERROR"
PARTIAL, UNKNOWN, NA, EMPTY = "PARTIAL", "UNKNOWN", "NOT_APPLICABLE", "EMPTY"
EXIT = {PASS: 0, FAIL: 1, BLOCKED: 2, ERROR: 3}

# Как статус направления влияет на общий вердикт.
GOOD = {PASS, NA}
BAD = {FAIL, ERROR}


def run(cmd: list[str], cwd: Path = REPO, timeout: int = 1800) -> dict:
    started = time.monotonic()
    try:
        r = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout)
        out, rc = (r.stdout or "") + (r.stderr or ""), r.returncode
    except subprocess.TimeoutExpired:
        out, rc = f"истёк таймаут {timeout} с", 124
    except FileNotFoundError as e:
        out, rc = f"не найдена команда: {e}", 127
    return {"command": " ".join(cmd), "exit_code": rc,
            "seconds": round(time.monotonic() - started, 1),
            "tail": "\n".join(out.strip().splitlines()[-6:])}


def python_exe() -> str:
    return sys.executable or "python3"


DEP_HINT = ("pip install --only-binary=:all: --require-hashes -r tools/requirements-sql-ci.txt")


def classify(r: dict, ok_status: str = PASS, bad_status: str = FAIL) -> str:
    """Отличить «проверка провалилась» от «проверку не удалось выполнить».

    Отсутствующая зависимость — это отсутствие доказательства, а не отрицательное
    доказательство. Возвращать в этом случае FAIL значит обвинять код в том, чего
    никто не проверял.
    """
    if r["exit_code"] == 0:
        return ok_status
    tail = r.get("tail", "")
    if "ModuleNotFoundError" in tail or r["exit_code"] in (124, 127):
        r["tail"] = (tail + f"\n[инструмент не запустился — доказательство не получено; "
                            f"зависимости: {DEP_HINT}]").strip()
        return BLOCKED
    return bad_status


# ------------------------------------------------------------- REPOSITORY ---

def stage_repository(skip_tests: bool) -> dict:
    ev, status = [], PASS
    dirty = run(["git", "-C", str(REPO), "status", "--porcelain"])
    clean = dirty["exit_code"] == 0 and not dirty["tail"].strip()
    ev.append({"check": "рабочее дерево чистое", "status": PASS if clean else FAIL, **dirty})
    if not clean:
        status = FAIL

    anc = run(["git", "-C", str(REPO), "merge-base", "--is-ancestor", "origin/main", "HEAD"])
    ok = anc["exit_code"] == 0
    ev.append({"check": "HEAD достижим из origin/main", "status": PASS if ok else FAIL, **anc})
    if not ok:
        status = FAIL

    if skip_tests:
        ev.append({"check": "тесты инструментов", "status": BLOCKED,
                   "command": "—", "exit_code": None, "seconds": 0,
                   "tail": "пропущено по --skip-tests: доказательство не получено"})
        status = BLOCKED if status == PASS else status
    else:
        t = run([python_exe(), "-m", "pytest", "-q", "tools/tests"])
        st = classify(t)
        ev.append({"check": "тесты инструментов", "status": st, **t})
        if st == FAIL:
            status = FAIL
        elif st == BLOCKED and status == PASS:
            status = BLOCKED
    return {"status": status, "evidence": ev}


# --------------------------------------------------------- SQL_VALIDATION ---

def stage_sql_validation() -> dict:
    r = run([python_exe(), "tools/validate_current_sql.py"])
    return {"status": classify(r), "evidence": [r]}


# --------------------------------------------------------- GIT_PROD_PARITY ---

def stage_parity(project: str, token_command: str | None, artifact_dir: Path) -> dict:
    if not project:
        return {"status": BLOCKED, "evidence": [{"check": "сверка с production",
                "tail": "нет --project: доказательство паритета не получено"}]}
    out = artifact_dir / "parity.json"
    cmd = [python_exe(), "tools/verify_current_sql_live.py", "--project", project,
           "--output", str(out)]
    if token_command:
        cmd += ["--token-command", token_command]
    r = run(cmd)
    # Коды verify_current_sql_live: 0 MATCH/PENDING, 1 DRIFT, 2 UNPROVEN, 3 ERROR.
    status = {0: PASS, 1: FAIL, 2: PARTIAL, 3: ERROR}.get(r["exit_code"], ERROR)
    if "ModuleNotFoundError" in r.get("tail", "") or r["exit_code"] in (124, 127):
        status = BLOCKED
    covered = None
    if out.exists():
        try:
            data = json.loads(out.read_text(encoding="utf-8"))
            covered = len(data.get("objects", data.get("results", [])) or [])
        except (json.JSONDecodeError, OSError):
            covered = None
    return {"status": status, "objects_compared": covered, "evidence": [r],
            "note": "сверка покрывает только объекты sql/current; остальные вью "
                    "производства каноническим определением не описаны"}


# --------------------------------------------------------- DATA_CONTRACTS ---

def stage_contracts(suites: list[str], project: str, token_command: str | None,
                    artifact_dir: Path, kind: str) -> dict:
    if not suites:
        return {"status": NA, "suites": [], "evidence": [
            {"check": kind, "tail": "затронутых наборов нет"}]}
    if not project:
        return {"status": BLOCKED, "suites": suites, "evidence": [
            {"check": kind, "tail": "нет --project: контракты не исполнены"}]}
    results, status = [], PASS
    for name in suites:
        out = artifact_dir / f"{kind}_{name}.json"
        cmd = [python_exe(), "tools/run_data_checks.py", "--suite", name,
               "--project", project, "--output", str(out)]
        if token_command:
            cmd += ["--token-command", token_command]
        r = run(cmd)
        verdict = {0: PASS, 1: FAIL, 2: EMPTY, 3: ERROR}.get(r["exit_code"], ERROR)
        counts = None
        if out.exists():
            try:
                counts = json.loads(out.read_text(encoding="utf-8")).get("counts")
            except (json.JSONDecodeError, OSError):
                counts = None
        results.append({"suite": name, "status": verdict, "counts": counts, **r})
        if verdict in BAD:
            status = FAIL
        elif verdict == EMPTY and status == PASS:
            status = BLOCKED
    return {"status": status, "suites": suites, "evidence": results}


# --------------------------------------------------------- PIPELINE_HEALTH ---

HEALTH_SQL = """
SELECT scope_id, health_status, reason_code,
       FORMAT_TIMESTAMP('%F %R', last_checked_at) AS last_checked
FROM `{p}.wb_ops.V_OPS_CURRENT_HEALTH`
"""
OZON_SQL = """
SELECT entity, status,
       FORMAT_TIMESTAMP('%F %R', MAX(started_at)) AS last_run,
       TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), MAX(started_at), HOUR) AS hours_ago,
       SUM(errors) AS errors
FROM `{p}.ozon_raw.OZON_INGESTION_RUNS`
WHERE started_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY)
GROUP BY entity, status ORDER BY entity
"""
REGISTRY_SQL = """
SELECT COUNT(*) AS pipelines,
       COUNTIF(c.pipeline_id IS NULL) AS without_check,
       COUNTIF(c.pipeline_id IS NULL AND r.criticality = 'CRITICAL') AS critical_without_check
FROM `{p}.wb_ops.OPS_PIPELINE_REGISTRY` r
LEFT JOIN (SELECT DISTINCT SPLIT(scope_id, '/')[OFFSET(0)] AS pipeline_id
           FROM `{p}.wb_ops.OPS_HEALTH_STATE` WHERE scope = 'PIPELINE_CHECK') c
  USING (pipeline_id)
WHERE r.enabled
"""


def stage_health(project: str, token_command: str | None, token_env: str) -> dict:
    if not project:
        return {"status": BLOCKED, "evidence": [{"check": "здоровье конвейеров",
                "tail": "нет --project"}]}
    try:
        token = resolve_token(token_env, token_command, os.environ)
        bq = ReadOnlyBigQuery(project=project, token=token, label_purpose="definition-of-done")
        health = bq.query(HEALTH_SQL.format(p=project))
        ozon = bq.query(OZON_SQL.format(p=project))
        cov = bq.query(REGISTRY_SQL.format(p=project))
    except BigQueryError as e:
        return {"status": ERROR, "evidence": [{"check": "здоровье конвейеров",
                "tail": str(e)[:300]}]}

    unhealthy = [h for h in health if (h["health_status"] or "").upper() != "HEALTHY"]
    ozon_bad = [o for o in ozon if o["status"] != "OK" or int(o["errors"] or 0) > 0]
    ozon_stale = [o for o in ozon if o["status"] == "OK" and int(o["hours_ago"] or 0) > 48]
    c = cov[0] if cov else {}
    without = int(c.get("without_check") or 0)
    crit_without = int(c.get("critical_without_check") or 0)

    if unhealthy or ozon_bad:
        status = FAIL
    elif crit_without or without or ozon_stale:
        # Молчание непокрытого конвейера — это неизвестность, а не здоровье.
        status = UNKNOWN
    else:
        status = PASS
    return {
        "status": status,
        "checks_evaluated": len(health),
        "unhealthy": [{"scope": h["scope_id"], "status": h["health_status"],
                       "reason": h["reason_code"]} for h in unhealthy],
        "ozon_entities": len({o["entity"] for o in ozon}),
        "ozon_failures": ozon_bad,
        "ozon_stale_over_48h": [o["entity"] for o in ozon_stale],
        "registry_pipelines_enabled": int(c.get("pipelines") or 0),
        "pipelines_without_check": without,
        "critical_pipelines_without_check": crit_without,
        "note": "Ozon не зарегистрирован в OPS_PIPELINE_REGISTRY: его состояние взято "
                "из собственного журнала прогонов, а не из детектора здоровья",
    }


# --------------------------------------------------------------------- CLI ---

def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--project")
    p.add_argument("--token-command")
    p.add_argument("--token-env", default="BQ_READONLY_ACCESS_TOKEN")
    p.add_argument("--git-diff", metavar="REF", default="origin/main")
    p.add_argument("--files", nargs="*", default=[])
    p.add_argument("--suite", action="append", default=[],
                   help="исполнить именно эти наборы вместо выведенных из изменения")
    p.add_argument("--full", action="store_true", help="исполнить все наборы ворот")
    p.add_argument("--offline", action="store_true", help="без BigQuery: только репозиторий и SQL")
    p.add_argument("--skip-tests", action="store_true")
    p.add_argument("--artifact-dir", default=str(DEFAULT_ARTIFACT_DIR))
    p.add_argument("--task", default="unnamed", help="имя задачи для имени артефакта")
    args = p.parse_args(argv)

    started = dt.datetime.now(dt.timezone.utc)
    stamp = started.strftime("%Y%m%dT%H%M%SZ")
    artifact_dir = Path(args.artifact_dir).expanduser() / f"{args.task}_{stamp}"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    project = None if args.offline else args.project

    # Состав обязательных контрактов выводится из графа, а не назначается человеком.
    suites_cfg = impact_analysis.load(impact_analysis.SUITES)
    all_gates = [s["suite"] for s in suites_cfg["suites"] if s.get("gate")]
    impact = None
    if args.suite:
        required = list(dict.fromkeys(args.suite))
    elif args.full:
        required = all_gates
    else:
        files = impact_analysis.changed_files(args.git_diff, args.files)
        impact = impact_analysis.analyse(files) if files else None
        required = list((impact or {}).get("required_contracts", {}))
    regression = [] if args.full else [
        s["suite"] for s in suites_cfg["suites"]
        if s.get("gate") and s["severity"] == "CRITICAL" and s["suite"] not in required]

    stages = {}
    stages["REPOSITORY"] = stage_repository(args.skip_tests)
    stages["SQL_VALIDATION"] = stage_sql_validation()
    stages["GIT_PROD_PARITY"] = stage_parity(project, args.token_command, artifact_dir)
    stages["DATA_CONTRACTS"] = stage_contracts(required, project, args.token_command,
                                               artifact_dir, "contract")
    stages["PIPELINE_HEALTH"] = stage_health(project, args.token_command, args.token_env)
    stages["REGRESSION"] = stage_contracts(regression, project, args.token_command,
                                           artifact_dir, "regression")

    ubr = impact_analysis.load(REPO / "quality" / "unresolved_business_rules.json")
    open_rules = [r for r in ubr["rules"] if r["status"] == "open"]

    statuses = {k: v["status"] for k, v in stages.items()}
    if any(s in BAD for s in statuses.values()):
        verdict = FAIL
    elif any(s not in GOOD for s in statuses.values()):
        verdict = BLOCKED
    else:
        verdict = PASS

    report = {
        "schema_version": 1,
        "task": args.task,
        "started_at": started.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "finished_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "mode": "offline" if args.offline else "full",
        "project": project,
        "verdict": verdict,
        "stages": statuses,
        "unresolved_business_rules_open": len(open_rules),
        "unresolved_business_rules": [{"id": r["id"], "severity": r["severity"],
                                       "title": r["title"]} for r in open_rules],
        "impact": impact,
        "detail": stages,
    }
    report_path = artifact_dir / "verification.json"
    report_path.write_text(json.dumps(report, indent=1, ensure_ascii=False) + "\n",
                           encoding="utf-8")

    print(f"=== VERIFY TASK: {args.task} ===")
    for name in ("REPOSITORY", "SQL_VALIDATION", "GIT_PROD_PARITY", "DATA_CONTRACTS",
                 "PIPELINE_HEALTH", "REGRESSION"):
        extra = ""
        st = stages[name]
        if name in ("DATA_CONTRACTS", "REGRESSION") and st.get("suites"):
            extra = "  " + ", ".join(st["suites"])
        if name == "PIPELINE_HEALTH" and st.get("critical_pipelines_without_check") is not None:
            extra = (f"  проверок {st.get('checks_evaluated')}, "
                     f"конвейеров без проверки {st.get('pipelines_without_check')} "
                     f"(CRITICAL — {st.get('critical_pipelines_without_check')})")
        print(f"{statuses[name]:<15}{name}{extra}")
    print(f"{len(open_rules):<15}UNRESOLVED_BUSINESS_RULES")
    print(f"\nВЕРДИКТ: {verdict}")
    print(f"артефакт: {report_path}")
    return EXIT[verdict]


if __name__ == "__main__":
    raise SystemExit(main())
