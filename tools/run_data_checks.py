#!/usr/bin/env python3
"""Выполнить приёмочные проверки данных против production. ТОЛЬКО ЧТЕНИЕ.

Зачем. В репозитории накоплены приёмочные SQL-артефакты, согласованные владельцем при
сдаче этапов. Их прогоняли руками один раз, после чего они переставали что-либо охранять.
Этот инструмент превращает их в повторяемые ворота: один прогон — один машиночитаемый
вердикт и код выхода, пригодный для CI и для Definition of Done автономного агента.

Бизнес-логика не переписывается. Три существующих способа записи приёмки исполняются
адаптерами (`tools/lib/acceptance.py`):

  check_blocks    `-- @check <ID>` + SELECT с колонкой `status`
  assert_script   `ASSERT <выражение> AS '<имя>';` → `SELECT IF(<то же выражение>, …)`
  verdict_select  самостоятельные SELECT с колонкой-вердиктом

Вердикт набора: FAIL (1) > EMPTY/UNPROVEN (2) > PASS (0); ошибка выполнения — 3.
Пустой результат — НЕ успех: проверка ничего не доказала. Явно объявить пустоту успехом
можно строкой `-- @expect empty_ok` (только адаптер check_blocks).

Отчёт JSON пишется по явному пути и по умолчанию НЕ в репозиторий: строки проверок
содержат значения бизнес-данных (docs/architecture/REPOSITORY_DATA_POLICY.md).

Примеры:
  python tools/run_data_checks.py --suite ozon_unit --project … --token-command "…"
  python tools/run_data_checks.py --suite wb_sku_performance_v2 --dry-run --project … --token-command "…"
  python tools/run_data_checks.py --list-all
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.acceptance import (  # noqa: E402
    Check,
    ParseResult,
    SuiteContractError,
    assert_to_select,
    make_check_id,
    parse_assert_script,
    parse_check_blocks,
    parse_source,
    parse_verdict_select,
    split_statements,
)
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

PASS, FAIL, EMPTY, ERROR = "PASS", "FAIL", "EMPTY", "ERROR"
BLOCKED = "BLOCKED"          # проверку нельзя исполнить: автор не выразил вердикт
EXIT = {PASS: 0, FAIL: 1, EMPTY: 2, ERROR: 3}

# Причины, по которым проверка исполняется, но НЕ влияет на вердикт ворот.
# Каждая обязана иметь доказательство в реестре: молчаливое исключение проверки
# из ворот — самый дешёвый способ сделать ворота бессмысленными.
OVERRIDE_STATUSES = {
    "superseded":     "условие заменено другой, действующей проверкой",
    "stale_snapshot": "условие фиксирует снимок состояния на дату, а не инвариант",
    "known_failing":  "подтверждённый дефект, ожидающий решения владельца",
}

# Совместимость с тестами и вызовами до введения адаптеров.
parse_checks = lambda text, source: parse_check_blocks(text, source).checks  # noqa: E731


# ------------------------------------------------------------------- реестр ---

def load_registry() -> dict:
    if not SUITES_PATH.exists():
        raise SuiteContractError(f"нет реестра наборов {SUITES_PATH.relative_to(REPO)}")
    return json.loads(SUITES_PATH.read_text(encoding="utf-8"))


def load_suite(name: str) -> dict:
    reg = load_registry()
    suites = {s["suite"]: s for s in reg["suites"]}
    if name not in suites:
        raise SuiteContractError(
            f"набор {name!r} не объявлен; доступны: {', '.join(sorted(suites))}")
    return suites[name]


def parse_suite(suite: dict) -> ParseResult:
    """Разобрать все файлы набора адаптером, объявленным в реестре."""
    merged, seen = ParseResult(), set()
    for rel in suite["files"]:
        path = REPO / rel
        res = parse_source(
            path.read_text(encoding="utf-8"), rel,
            suite.get("adapter", "check_blocks"),
            prefix=suite.get("id_prefix", ""),
            status_column=suite.get("status_column", "verdict"),
        )
        for c in res.checks:
            if c.check_id in seen:
                raise SuiteContractError(
                    f"{suite['suite']}: идентификатор {c.check_id!r} встречается дважды")
            seen.add(c.check_id)
        merged.checks.extend(res.checks)
    return merged


# --------------------------------------------------------------- исполнение ---

def run_check(bq, check: Check, max_failing_rows: int, dry_run: bool = False,
              override: dict | None = None) -> dict:
    result = {
        "check_id": check.check_id, "source": check.source, "line": check.line,
        "adapter": check.adapter, "label": check.label,
        "status": None, "rows": 0, "failing_rows": 0, "failing_sample": [],
        "bytes_processed": None, "error": None,
        "gate_relevant": not (override or {}).get("excluded_from_gate", False),
        "override": override,
    }
    if not check.executable:
        result["status"] = BLOCKED
        result["error"] = check.not_executable_reason
        return result
    try:
        before = getattr(bq, "bytes_billed", 0)
        rows = bq.query(check.sql, dry_run=dry_run)
        result["bytes_processed"] = getattr(bq, "bytes_billed", 0) - before
    except BigQueryError as e:
        result["status"] = ERROR
        result["error"] = str(e)[:500]
        return result
    if dry_run:
        # Dry-run доказывает только компилируемость и объём, но не вердикт.
        result["status"] = PASS
        result["error"] = None
        return result
    result["rows"] = len(rows)
    if not rows:
        result["status"] = PASS if check.expect_empty_ok else EMPTY
        if result["status"] == EMPTY:
            result["error"] = "проверка вернула 0 строк и ничего не доказала"
        return result
    col = check.status_column if check.status_column in rows[0] else "status"
    if col not in rows[0]:
        result["status"] = ERROR
        result["error"] = f"в результате нет колонки `{check.status_column}`"
        return result
    bad = [r for r in rows if (r.get(col) or "").upper() != PASS]
    unknown = [r for r in bad if (r.get(col) or "").upper() != FAIL]
    result["failing_rows"] = len(bad)
    result["failing_sample"] = bad[:max_failing_rows]
    if unknown:
        result["status"] = ERROR
        vals = sorted({(r.get(col) or "NULL") for r in unknown})[:3]
        result["error"] = f"{col} вне {{PASS,FAIL}}: {vals}"
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

def cmd_list_all() -> int:
    reg = load_registry()
    total_exec = total_blocked = 0
    for suite in reg["suites"]:
        try:
            res = parse_suite(suite)
        except (SuiteContractError, OSError) as e:
            print(f"{suite['suite']:<28} ОШИБКА РАЗБОРА: {e}")
            continue
        total_exec += len(res.executable)
        total_blocked += len(res.not_executable)
        gate = "GATE" if suite.get("gate") else "observe"
        print(f"{suite['suite']:<28} {gate:<8} {suite.get('adapter','check_blocks'):<15} "
              f"исполнимых {len(res.executable):>3}  неисполнимых {len(res.not_executable):>3}  "
              f"severity={suite.get('severity','—')}")
    backlog = reg.get("backlog", {}).get("files", [])
    print(f"\nвсего: {total_exec} исполнимых, {total_blocked} неисполнимых, "
          f"{len(backlog)} артефактов ещё не подключено")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--suite", help="имя набора из quality/suites.json")
    src.add_argument("--file", help="путь к файлу проверок")
    src.add_argument("--list-all", action="store_true", help="перечислить все наборы реестра")
    p.add_argument("--adapter", default="check_blocks", help="адаптер для --file")
    p.add_argument("--status-column", default="verdict", help="колонка вердикта для verdict_select")
    p.add_argument("--check", action="append", default=[], help="выполнить только эти ID")
    p.add_argument("--list", action="store_true", help="только разобрать и перечислить проверки")
    p.add_argument("--dry-run", action="store_true",
                   help="не читать данные: доказать компилируемость SQL и оценить объём")
    p.add_argument("--project")
    p.add_argument("--location", default="EU")
    p.add_argument("--bq-host", default="www.googleapis.com")
    p.add_argument("--token-env", default="BQ_READONLY_ACCESS_TOKEN")
    p.add_argument("--token-command")
    p.add_argument("--max-failing-rows", type=int, default=5)
    p.add_argument("--output", help="путь к JSON-отчёту (вне Git)")
    args = p.parse_args(argv)

    if args.list_all:
        try:
            return cmd_list_all()
        except (SuiteContractError, OSError, json.JSONDecodeError) as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return EXIT[ERROR]

    try:
        if args.suite:
            suite = load_suite(args.suite)
            res = parse_suite(suite)
            suite_name, gate = suite["suite"], bool(suite.get("gate"))
            timeout = int(suite.get("timeout_seconds", 180))
        else:
            path = Path(args.file)
            rel = str(path)
            suite = {}
            res = parse_source(path.read_text(encoding="utf-8"), rel, args.adapter,
                               prefix=path.stem.upper(), status_column=args.status_column)
            suite_name, gate, timeout = path.stem, False, 180
        checks = res.checks
        if args.check:
            wanted = set(args.check)
            missing = wanted - {c.check_id for c in checks}
            if missing:
                # Неизвестный идентификатор — ошибка, а не «ничего не выполнено»:
                # молчаливый пустой прогон выглядит как успех.
                raise SuiteContractError(f"нет таких проверок: {sorted(missing)}")
            checks = [c for c in checks if c.check_id in wanted]
    except (SuiteContractError, OSError, json.JSONDecodeError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return EXIT[ERROR]

    if args.list:
        for c in checks:
            mark = "" if c.executable else "  [BLOCKED] " + (c.not_executable_reason or "")
            print(f"{c.check_id:<62} {c.source}:{c.line}{mark}")
        print(f"\n{len(checks)} записей: {sum(1 for c in checks if c.executable)} исполнимых, "
              f"{sum(1 for c in checks if not c.executable)} неисполнимых")
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
                          host=args.bq_host, label_purpose="data-quality-gate",
                          timeout=timeout)
    overrides = suite.get("check_overrides", {}) if suite else {}
    unknown_overrides = set(overrides) - {c.check_id for c in res.checks}
    if unknown_overrides:
        # Исключение, указывающее в пустоту, — это забытое исключение: проверка
        # могла быть переименована, и ворота молча вернули бы её в состав.
        print(f"ERROR: исключения ссылаются на несуществующие проверки: "
              f"{sorted(unknown_overrides)}", file=sys.stderr)
        return EXIT[ERROR]

    started = dt.datetime.now(dt.timezone.utc)
    results = [run_check(bq, c, args.max_failing_rows, args.dry_run, overrides.get(c.check_id))
               for c in checks]
    gating = [r for r in results if r["status"] != BLOCKED and r["gate_relevant"]]
    overall = verdict(gating) if gating else EMPTY

    report = {
        "schema_version": 2,
        "suite": suite_name,
        "is_gate": gate,
        "mode": "dry_run" if args.dry_run else "execute",
        "project": args.project,
        "started_at": started.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "finished_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "verdict": overall,
        "counts": {s: sum(1 for r in results if r["status"] == s)
                   for s in (PASS, FAIL, EMPTY, ERROR, BLOCKED)},
        "excluded_from_gate": sum(1 for r in results if not r["gate_relevant"]),
        "bytes_processed": bq.bytes_billed,
        "source_contract": suite.get("source_contract"),
        "checks": results,
    }
    if args.output:
        out = Path(args.output).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")

    width = min(max((len(r["check_id"]) for r in results), default=10), 62)
    for r in results:
        note = ""
        if r["status"] == FAIL:
            note = f"  {r['failing_rows']} из {r['rows']} строк не PASS"
        elif r["error"]:
            note = f"  {r['error'][:110]}"
        if not r["gate_relevant"]:
            note += f"  [вне ворот: {(r['override'] or {}).get('status', '?')}]"
        print(f"{r['status']:<8}{r['check_id']:<{width}}{note}")
    c = report["counts"]
    gb = bq.bytes_billed / 1024 ** 3
    print(f"\n{suite_name}: {overall} — PASS {c[PASS]} / FAIL {c[FAIL]} / EMPTY {c[EMPTY]} / "
          f"ERROR {c[ERROR]} / BLOCKED {c[BLOCKED]} / вне ворот {report['excluded_from_gate']}  "
          f"({bq.queries_issued} запросов, {gb:.2f} ГБ{' dry-run' if args.dry_run else ''})")
    if args.output:
        print(f"отчёт: {args.output}")
    return EXIT[overall]


if __name__ == "__main__":
    raise SystemExit(main())
