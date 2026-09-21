#!/usr/bin/env python3
"""Выполнить декларативные проверки данных (`-- @check`) против production. ТОЛЬКО ЧТЕНИЕ.

Зачем. В репозитории накоплены наборы приёмочных SQL-проверок (`sql/**/*_validation.sql`).
Их прогоняли руками один раз при сдаче этапа, после чего они переставали что-либо охранять.
Этот инструмент превращает их в повторяемые ворота: один прогон — один машиночитаемый
вердикт и код выхода, пригодный для CI и для Definition of Done автономного агента.

Контракт файла проверок:
  * блок начинается строкой `-- @check <ID>` (ID: A-Z, 0-9, подчёркивание);
  * до первого маркера — шапка файла, она игнорируется;
  * блок содержит РОВНО один оператор, завершённый `;`;
  * оператор — SELECT/WITH; всё остальное отклоняется до отправки в BigQuery;
  * результат обязан содержать колонку `status` со значением `PASS` или `FAIL` в каждой строке;
  * пустой результат — это НЕ успех: проверка ничего не доказала (`EMPTY`). Если пустой
    результат и есть ожидаемый успех, блок помечается строкой `-- @expect empty_ok`.

Вердикт набора: FAIL (1) > EMPTY/UNPROVEN (2) > PASS (0); ошибка выполнения — 3.

Отчёт JSON пишется по явному пути. По умолчанию он НЕ пишется в репозиторий: строки
проверок содержат значения бизнес-данных, а Git хранит решения и код, а не данные
(docs/architecture/REPOSITORY_DATA_POLICY.md).

Примеры:
  python tools/run_data_checks.py --suite ozon_unit \
      --project project-fa311fc0-4d87-4781-986 \
      --token-command "gcloud auth print-access-token" \
      --output ~/checks/ozon_unit.json
  python tools/run_data_checks.py --file sql/scale1/fact_sku_daily_validation.sql --list
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.bq_readonly import (  # noqa: E402
    BigQueryError,
    ReadOnlyBigQuery,
    ReadOnlyViolation,
    assert_read_only,
    mask_sql_comments,
    resolve_token,
)

REPO = Path(__file__).resolve().parent.parent
SUITES_PATH = REPO / "quality" / "suites.json"

CHECK_MARKER = re.compile(r"^--\s*@check\s+([A-Z0-9_]+)\s*$", re.M)
EXPECT_EMPTY = re.compile(r"^--\s*@expect\s+empty_ok\s*$", re.M)

PASS, FAIL, EMPTY, ERROR = "PASS", "FAIL", "EMPTY", "ERROR"
EXIT = {PASS: 0, FAIL: 1, EMPTY: 2, ERROR: 3}


class SuiteContractError(RuntimeError):
    """Файл проверок не соответствует контракту `-- @check`."""


# ------------------------------------------------------------------ parsing ---

def parse_checks(text: str, source: str) -> list[dict]:
    """Разобрать файл на блоки. Нарушение контракта — ошибка, а не пропуск блока."""
    marks = list(CHECK_MARKER.finditer(text))
    if not marks:
        raise SuiteContractError(f"{source}: нет ни одного маркера `-- @check <ID>`")
    checks, seen = [], set()
    for i, m in enumerate(marks):
        check_id = m.group(1)
        if check_id in seen:
            raise SuiteContractError(f"{source}: повторяющийся идентификатор проверки {check_id!r}")
        seen.add(check_id)
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        block = text[m.end():end]
        stmts = split_statements(block)
        if len(stmts) != 1:
            raise SuiteContractError(
                f"{source}:{check_id}: ожидался ровно один оператор, найдено {len(stmts)}"
            )
        sql = stmts[0]
        try:
            assert_read_only(sql)
        except ReadOnlyViolation as e:
            raise SuiteContractError(f"{source}:{check_id}: {e}") from None
        checks.append({
            "check_id": check_id,
            "source": source,
            "line": text[:m.start()].count("\n") + 1,
            "sql": sql,
            "expect_empty_ok": bool(EXPECT_EMPTY.search(block)),
        })
    return checks


def split_statements(block: str) -> list[str]:
    """Разделить по `;` вне строк и комментариев. Пустые хвосты отбрасываются."""
    masked = mask_sql_comments(block)  # длина сохраняется: индексы маски = индексы block
    out, start = [], 0
    i, n = 0, len(masked)
    while i < n:
        ch = masked[i]
        if ch in "'\"":
            quote, i = ch, i + 1
            while i < n:
                if masked[i] == "\\":
                    i += 2
                    continue
                if masked[i] == quote:
                    break
                i += 1
        elif ch == ";":
            out.append(block[start:i].strip())
            start = i + 1
        i += 1
    tail = block[start:].strip()
    if tail:
        out.append(tail)
    return [s for s in out if s]


# ---------------------------------------------------------------- execution ---

def run_check(bq: ReadOnlyBigQuery, check: dict, max_failing_rows: int) -> dict:
    result = {
        "check_id": check["check_id"],
        "source": check["source"],
        "line": check["line"],
        "status": None,
        "rows": 0,
        "failing_rows": 0,
        "failing_sample": [],
        "error": None,
    }
    try:
        rows = bq.query(check["sql"])
    except BigQueryError as e:
        result["status"] = ERROR
        result["error"] = str(e)[:500]
        return result
    result["rows"] = len(rows)
    if not rows:
        result["status"] = PASS if check["expect_empty_ok"] else EMPTY
        if result["status"] == EMPTY:
            result["error"] = "проверка вернула 0 строк и ничего не доказала"
        return result
    if "status" not in rows[0]:
        result["status"] = ERROR
        result["error"] = "в результате нет колонки `status`"
        return result
    bad = [r for r in rows if (r.get("status") or "").upper() != PASS]
    unknown = [r for r in bad if (r.get("status") or "").upper() != FAIL]
    result["failing_rows"] = len(bad)
    result["failing_sample"] = bad[:max_failing_rows]
    if unknown:
        result["status"] = ERROR
        result["error"] = f"status вне {{PASS,FAIL}}: {sorted({(r.get('status') or 'NULL') for r in unknown})[:3]}"
    else:
        result["status"] = FAIL if bad else PASS
    return result


def verdict(results: list[dict]) -> str:
    statuses = {r["status"] for r in results}
    for s in (ERROR, FAIL, EMPTY):
        if s in statuses:
            return s
    return PASS


# --------------------------------------------------------------------- CLI ---

def load_suite(name: str) -> dict:
    if not SUITES_PATH.exists():
        raise SuiteContractError(f"нет реестра наборов {SUITES_PATH.relative_to(REPO)}")
    reg = json.loads(SUITES_PATH.read_text(encoding="utf-8"))
    suites = {s["suite"]: s for s in reg["suites"]}
    if name not in suites:
        raise SuiteContractError(
            f"набор {name!r} не объявлен; доступны: {', '.join(sorted(suites))}"
        )
    return suites[name]


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--suite", help="имя набора из quality/suites.json")
    src.add_argument("--file", help="путь к файлу проверок")
    p.add_argument("--check", action="append", default=[], help="выполнить только эти ID")
    p.add_argument("--list", action="store_true", help="только разобрать и перечислить проверки")
    p.add_argument("--project")
    p.add_argument("--location", default="EU")
    p.add_argument("--bq-host", default="www.googleapis.com")
    p.add_argument("--token-env", default="BQ_READONLY_ACCESS_TOKEN")
    p.add_argument("--token-command")
    p.add_argument("--max-failing-rows", type=int, default=5)
    p.add_argument("--output", help="путь к JSON-отчёту (вне Git)")
    args = p.parse_args(argv)

    try:
        if args.suite:
            suite = load_suite(args.suite)
            files = [REPO / f for f in suite["files"]]
            suite_name, gate = suite["suite"], suite.get("gate", False)
        else:
            files = [Path(args.file)]
            suite_name, gate = Path(args.file).stem, False

        checks = []
        for f in files:
            rel = str(f.relative_to(REPO)) if f.is_absolute() and REPO in f.parents else str(f)
            checks.extend(parse_checks(f.read_text(encoding="utf-8"), rel))
        if args.check:
            wanted = set(args.check)
            missing = wanted - {c["check_id"] for c in checks}
            if missing:
                raise SuiteContractError(f"нет таких проверок: {sorted(missing)}")
            checks = [c for c in checks if c["check_id"] in wanted]
    except (SuiteContractError, OSError, json.JSONDecodeError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return EXIT[ERROR]

    if args.list:
        for c in checks:
            print(f"{c['check_id']:<40} {c['source']}:{c['line']}"
                  f"{'  [empty_ok]' if c['expect_empty_ok'] else ''}")
        print(f"\n{len(checks)} проверок, контракт соблюдён")
        return 0

    if not args.project:
        print("ERROR: нужен --project", file=sys.stderr)
        return EXIT[ERROR]
    try:
        token = resolve_token(args.token_env, args.token_command, os.environ)
    except BigQueryError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return EXIT[ERROR]

    bq = ReadOnlyBigQuery(project=args.project, token=token, location=args.location,
                          host=args.bq_host, label_purpose="data-quality-gate")
    started = dt.datetime.now(dt.timezone.utc)
    results = [run_check(bq, c, args.max_failing_rows) for c in checks]
    overall = verdict(results)

    report = {
        "schema_version": 1,
        "suite": suite_name,
        "is_gate": gate,
        "project": args.project,
        "started_at": started.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "finished_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "verdict": overall,
        "counts": {s: sum(1 for r in results if r["status"] == s) for s in (PASS, FAIL, EMPTY, ERROR)},
        "checks": results,
    }
    if args.output:
        out = Path(args.output).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")

    width = max((len(r["check_id"]) for r in results), default=10)
    for r in results:
        note = ""
        if r["status"] == FAIL:
            note = f"  {r['failing_rows']} из {r['rows']} строк не PASS"
        elif r["error"]:
            note = f"  {r['error']}"
        print(f"{r['status']:<6} {r['check_id']:<{width}}{note}")
    c = report["counts"]
    print(f"\n{suite_name}: {overall} — PASS {c[PASS]} / FAIL {c[FAIL]} / EMPTY {c[EMPTY]} / ERROR {c[ERROR]}"
          f"  ({bq.queries_issued} запросов)")
    if args.output:
        print(f"отчёт: {args.output}")
    return EXIT[overall]


if __name__ == "__main__":
    raise SystemExit(main())
