#!/usr/bin/env python3
"""DRO-1: рендер объектов evetis_health и read-only backtest на production-данных.

Объекты ещё не развёрнуты, поэтому проверять их на живых данных можно только так:
тела VIEW / TABLE FUNCTION из sql/health/ подставляются в запрос рекурсивно, момент
as_of фиксируется литералом, и получившийся SELECT уходит в BigQuery через
tools/lib/bq_readonly.py (отклоняет всё, кроме SELECT/WITH, ещё до отправки).
Ничего не создаёт и не пишет.

  python tools/dro1_health.py render TVF_DATA_HEALTH --as-of "2026-10-01 07:30:00+03"
  python tools/dro1_health.py backtest --token-command "gcloud auth print-access-token"

Без аргумента --token-command / переменной BQ_READONLY_ACCESS_TOKEN работает только
render (офлайн). Контракт читается тем же разбором, что и в тестах.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SQL_DIR = REPO / "sql" / "health"
DATASET = "evetis_health"

_CREATE_RE = re.compile(
    r"CREATE\s+(?:OR\s+REPLACE\s+)?(VIEW|TABLE\s+FUNCTION|PROCEDURE|TABLE(?:\s+IF\s+NOT\s+EXISTS)?)\s+`([^`]+)`",
    re.IGNORECASE,
)


def _skip_string(sql: str, i: int) -> int:
    quote = sql[i]
    i += 1
    while i < len(sql):
        if sql[i] == "\\":
            i += 2
            continue
        if sql[i] == quote:
            return i + 1
        i += 1
    raise ValueError("незакрытая строка")


def _skip_comment(sql: str, i: int) -> int:
    if sql.startswith("--", i) or sql[i] == "#":
        j = sql.find("\n", i)
        return len(sql) if j == -1 else j + 1
    if sql.startswith("/*", i):
        j = sql.find("*/", i + 2)
        return len(sql) if j == -1 else j + 2
    return i


def _match_paren(sql: str, i: int) -> int:
    """Индекс закрывающей скобки для sql[i] == '(' с учётом строк и комментариев."""
    assert sql[i] == "("
    depth = 0
    while i < len(sql):
        ch = sql[i]
        if ch in "'\"":
            i = _skip_string(sql, i)
            continue
        j = _skip_comment(sql, i)
        if j != i:
            i = j
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise ValueError("несбалансированные скобки")


def _statement_end(sql: str, i: int) -> int:
    """Конец оператора — первая ';' вне строк, комментариев и скобок."""
    depth = 0
    while i < len(sql):
        ch = sql[i]
        if ch in "'\"":
            i = _skip_string(sql, i)
            continue
        j = _skip_comment(sql, i)
        if j != i:
            i = j
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == ";" and depth == 0:
            return i
        i += 1
    return len(sql)


def _sub_identifier(sql: str, name: str, repl: str) -> str:
    """Заменить идентификатор name на repl вне строковых литералов и комментариев."""
    ident = re.compile(rf"(?<![\w.`]){re.escape(name)}(?![\w`])")
    out, i, start = [], 0, 0
    while i < len(sql):
        ch = sql[i]
        if ch in "'\"" or sql.startswith("--", i) or sql.startswith("/*", i) or ch == "#":
            out.append(ident.sub(lambda _m: repl, sql[start:i]))
            j = _skip_string(sql, i) if ch in "'\"" else _skip_comment(sql, i)
            out.append(sql[i:j])
            i = start = j
            continue
        i += 1
    out.append(ident.sub(lambda _m: repl, sql[start:]))
    return "".join(out)


def load_objects() -> dict[str, dict]:
    """Все CREATE из sql/health/*.sql: имя -> {kind, params, body, file}."""
    objects: dict[str, dict] = {}
    for path in sorted(SQL_DIR.glob("dro1_*.sql")):
        sql = path.read_text(encoding="utf-8")
        for m in _CREATE_RE.finditer(sql):
            # пропускаем CREATE внутри комментариев: строка с '--' до совпадения
            line_start = sql.rfind("\n", 0, m.start()) + 1
            if "--" in sql[line_start:m.start()]:
                continue
            kind = re.sub(r"\s+", " ", m.group(1).upper())
            name = m.group(2).split(".")[-1]
            pos = m.end()
            params: list[str] = []
            if kind in ("TABLE FUNCTION", "PROCEDURE"):
                open_p = sql.index("(", pos)
                close_p = _match_paren(sql, open_p)
                params = [p.strip().split()[0] for p in sql[open_p + 1:close_p].split(",") if p.strip()]
                pos = close_p + 1
            end = _statement_end(sql, pos)
            body = None
            if kind in ("VIEW", "TABLE FUNCTION"):
                opt = re.search(r"\bOPTIONS\s*\(", sql[pos:end], re.IGNORECASE)
                if opt:
                    open_o = pos + opt.end() - 1
                    pos = _match_paren(sql, open_o) + 1
                as_m = re.compile(r"\s*AS\s*", re.IGNORECASE).match(sql, pos)
                if not as_m:
                    raise ValueError(f"{name}: нет AS после заголовка")
                pos = as_m.end()
                body = sql[pos:end].strip()
                if kind == "TABLE FUNCTION":
                    if not (body.startswith("(") and _match_paren(body, 0) == len(body) - 1):
                        raise ValueError(f"{name}: тело TVF должно быть в скобках")
                    body = body[1:-1].strip()
            objects[name] = {"kind": kind, "params": params, "body": body, "file": path.name,
                             "statement": sql[m.start():end]}
    return objects


def render(name: str, args: list[str] | None = None, objects: dict | None = None, _depth: int = 0,
           tables: dict[str, str] | None = None) -> str:
    """Тело объекта с рекурсивно подставленными зависимостями evetis_health.

    tables — подмена таблиц evetis_health, которых ещё нет (до деплоя): имя -> SELECT.
    """
    objects = objects or load_objects()
    tables = tables or {}
    if _depth > 10:
        raise RecursionError("слишком глубокая подстановка")
    obj = objects[name]
    body = obj["body"]
    if body is None:
        raise ValueError(f"{name}: {obj['kind']} не рендерится")
    for param, arg in zip(obj["params"], args or []):
        body = _sub_identifier(body, param, f"({arg})")

    # TVF-вызовы: `evetis_health.TVF_X`(arg)
    pattern = re.compile(rf"`{DATASET}\.(TVF_\w+)`\s*\(")
    out, i = [], 0
    while True:
        m = pattern.search(body, i)
        if not m:
            out.append(body[i:])
            break
        open_p = m.end() - 1
        close_p = _match_paren(body, open_p)
        arg = body[open_p + 1:close_p].strip()
        inner = render(m.group(1), [arg], objects, _depth + 1, tables)
        out.append(body[i:m.start()])
        out.append(f"(\n{inner}\n)")
        i = close_p + 1
    body = "".join(out)
    # Вью и таблицы evetis_health: вью подставляем, таблицы оставляем (их нет до деплоя)
    def view_repl(m: re.Match) -> str:
        ref = m.group(1)
        dep = objects.get(ref)
        if ref in tables:
            return f"(\n{tables[ref]}\n)"
        if dep and dep["kind"] == "VIEW":
            return f"(\n{render(ref, None, objects, _depth + 1, tables)}\n)"
        return m.group(0)
    body = re.sub(rf"`{DATASET}\.(\w+)`", view_repl, body)
    return body


def load_contract() -> list[dict]:
    """Строки контракта — разбором литерала VIEW sqlglot-ом (тот же путь, что в тестах)."""
    import sqlglot
    from sqlglot import exp

    body = load_objects()["V_PIPELINE_CONTRACT"]["body"]
    tree = sqlglot.parse_one(body, read="bigquery")
    dt = next(d for d in tree.find_all(exp.DataType) if d.this == exp.DataType.Type.STRUCT)
    fields = [f.name for f in dt.expressions]
    rows = []
    for tup in tree.find_all(exp.Tuple):
        values = []
        for v in tup.expressions:
            if isinstance(v, exp.Null):
                values.append(None)
            elif isinstance(v, exp.Boolean):
                values.append(bool(v.this))
            elif isinstance(v, exp.Literal):
                values.append(int(v.this) if not v.is_string and re.fullmatch(r"-?\d+", v.this) else v.this)
            else:
                # DATE '2026-04-13' и подобное — как текст SQL; тест сверяет формат отдельно
                values.append(v.sql(dialect="bigquery"))
        if len(values) == len(fields):
            rows.append(dict(zip(fields, values)))
    return rows


# ── backtest ─────────────────────────────────────────────────────────────────
# Известные инциденты из аудита 2026-10-04. Момент — МСК. Ожидание — то, что
# детектор обязан был показать в этот момент (для потерь — ДО дедлайна).
BACKTEST_CASES = [
    # Реклама WB 30.09: прогон 01.10 ERROR/ADS_PARTIAL, данные восстановил прогон 02.10.
    ("ads_30_09_open", "2026-10-01 07:00:00", "ads_daily", {"reason_code": "SLOT_MISSED"}),
    ("mart_01_10_warn", "2026-10-01 08:00:00", "mart", {"reason_code": "SLOT_LATE"}),
    ("mart_01_10_fail", "2026-10-01 10:00:00", "mart", {"reason_code": "SLOT_MISSED", "severity": "CRITICAL"}),
    ("ads_30_09_now", "NOW", "ads_daily", {"serving_status": "HEALTHY"}),
    # Ставки: снимков нет за 13.09, 20.09, 01.10 (и 01.09). Слот 05:15, потеря в 00:00.
    ("bids_13_09_warn", "2026-09-13 07:00:00", "ads_query_bids", {"reason_code": "SLOT_LATE"}),
    ("bids_13_09_imminent", "2026-09-13 16:30:00", "ads_query_bids", {"reason_code": "DATA_LOSS_IMMINENT"}),
    ("bids_13_09_lost", "2026-09-14 00:30:00", "ads_query_bids", {"reason_code": "DATA_LOSS_CONFIRMED"}),
    ("bids_20_09_warn", "2026-09-20 07:00:00", "ads_query_bids", {"reason_code": "SLOT_LATE"}),
    ("bids_20_09_imminent", "2026-09-20 16:30:00", "ads_query_bids", {"reason_code": "DATA_LOSS_IMMINENT"}),
    ("bids_01_10_warn", "2026-10-01 07:00:00", "ads_query_bids", {"reason_code": "SLOT_LATE"}),
    ("bids_01_10_imminent", "2026-10-01 16:30:00", "ads_query_bids", {"reason_code": "DATA_LOSS_IMMINENT"}),
    # Остатки WB: снимка за 03.09 нет (2 прогона ERROR). Слот 06:30.
    ("stocks_03_09_warn", "2026-09-03 08:00:00", "stocks_snapshot", {"reason_code": "SLOT_LATE"}),
    ("stocks_03_09_imminent", "2026-09-03 16:30:00", "stocks_snapshot", {"reason_code": "DATA_LOSS_IMMINENT"}),
    ("stocks_03_09_lost", "2026-09-04 00:30:00", "stocks_snapshot", {"reason_code": "DATA_LOSS_CONFIRMED"}),
]

PERIOD_CASES = [
    ("ads_30_09_period", "NOW", "ads_daily", "2026-09-30", {"data_status": "RECOVERED"}),
    ("mart_30_09_period", "NOW", "mart", "2026-09-30", {"data_status": "RECOVERED"}),
    ("bids_01_10_period", "NOW", "ads_query_bids", "2026-10-01", {"data_status": "MISSING_UNRECOVERABLE"}),
    ("bids_20_09_period", "NOW", "ads_query_bids", "2026-09-20", {"data_status": "MISSING_UNRECOVERABLE"}),
    ("bids_13_09_period", "NOW", "ads_query_bids", "2026-09-13", {"data_status": "MISSING_UNRECOVERABLE"}),
    ("stocks_03_09_period", "NOW", "stocks_snapshot", "2026-09-03", {"data_status": "MISSING_UNRECOVERABLE"}),
]


READ_MODEL_CASES = [
    # Свежий снимок: состояние передаётся как есть, ни одного DETECTOR_STALE.
    ("read_model_fresh_snapshot", "CURRENT_TIMESTAMP()",
     lambda got: bool(got) and all(reason not in ("DETECTOR_STALE", "DETECTOR_NEVER_RAN") for _, reason in got)),
    # Снимок двухчасовой давности: детектор «остановился» → всё UNKNOWN, ни одного HEALTHY.
    ("read_model_detector_stale", "TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 2 HOUR)",
     lambda got: bool(got) and all(s == "UNKNOWN" and r == "DETECTOR_STALE" for s, r in got)),
]


def _as_of_sql(moment: str) -> str:
    if moment == "NOW":
        return "CURRENT_TIMESTAMP()"
    return f"TIMESTAMP('{moment}', 'Europe/Moscow')"


def _client(args):
    sys.path.insert(0, str(REPO / "tools"))
    from lib.bq_readonly import ReadOnlyBigQuery, resolve_token
    token = resolve_token(args.token_env, args.token_command, os.environ)
    return ReadOnlyBigQuery(project=args.project, token=token, label_purpose="dro1-backtest")


def run_backtest(args) -> int:
    bq = _client(args)
    objects = load_objects()
    results, failures = [], 0
    by_moment: dict[str, list] = {}
    for case in BACKTEST_CASES:
        by_moment.setdefault(case[1], []).append(case)
    for moment, cases in by_moment.items():
        sql = "SELECT * FROM (\n" + render("TVF_DATA_HEALTH", [_as_of_sql(moment)], objects) + "\n)"
        rows = {r["pipeline_id"]: r for r in bq.query(sql)}
        for cid, _, pipeline, expect in cases:
            row = rows.get(pipeline, {})
            got = {k: row.get(k) for k in expect}
            ok = got == expect
            failures += 0 if ok else 1
            results.append({"case": cid, "as_of_msk": moment, "pipeline": pipeline, "expected": expect,
                            "got": got, "result": "PASS" if ok else "FAIL",
                            "evidence": {k: row.get(k) for k in (
                                "serving_status", "data_status", "reason_code", "severity",
                                "latest_business_date", "target_business_date", "slot_late_minutes",
                                "minutes_to_loss", "run_status")}})
    by_moment = {}
    for case in PERIOD_CASES:
        by_moment.setdefault(case[1], []).append(case)
    for moment, cases in by_moment.items():
        sql = "SELECT * FROM (\n" + render("TVF_DATA_PERIOD_STATE", [_as_of_sql(moment)], objects) + "\n)"
        rows = {(r["pipeline_id"], r["period_date"]): r for r in bq.query(sql)}
        for cid, _, pipeline, day, expect in cases:
            row = rows.get((pipeline, day), {})
            got = {k: row.get(k) for k in expect}
            ok = got == expect
            failures += 0 if ok else 1
            results.append({"case": cid, "as_of_msk": moment, "pipeline": pipeline, "period": day,
                            "expected": expect, "got": got, "result": "PASS" if ok else "FAIL"})
    # Read model до деплоя: таблица снимков подменяется результатом TVF.
    for cid, as_of, expect in READ_MODEL_CASES:
        snap = ("SELECT *, 'simulated' AS detector_run_id FROM (\n"
                + render("TVF_DATA_HEALTH", [as_of], objects) + "\n)")
        sql = ("SELECT serving_status, reason_code, COUNT(*) AS n FROM (\n"
               + render("V_DATA_HEALTH_CURRENT", None, objects, tables={"DATA_HEALTH_SNAPSHOT": snap})
               + "\n) WHERE evaluation_mode = 'EVALUATED' GROUP BY 1, 2")
        got_rows = bq.query(sql)
        got = sorted((r["serving_status"], r["reason_code"]) for r in got_rows)
        ok = expect(got)
        failures += 0 if ok else 1
        results.append({"case": cid, "as_of_sql": as_of, "got": got, "result": "PASS" if ok else "FAIL"})
    checks = bq.query("SELECT detector, effective_status, staleness_reason, COUNT(*) AS n, "
                      "MIN(check_age_minutes) AS min_age FROM (\n"
                      + render("V_HEALTH_CHECK_CURRENT", None, objects) + "\n) GROUP BY 1, 2, 3")
    ext = [r for r in checks if r["detector"] == "HEALTH_EXT"]
    ok = bool(ext) and all(r["effective_status"] == "UNKNOWN" for r in ext)
    failures += 0 if ok else 1
    results.append({"case": "health_ext_12d_not_healthy", "as_of_sql": "CURRENT_TIMESTAMP()",
                    "got": checks, "result": "PASS" if ok else "FAIL"})

    sql_now = "SELECT * FROM (\n" + render("TVF_DATA_HEALTH", ["CURRENT_TIMESTAMP()"], objects) + "\n)"
    now_rows = bq.query(sql_now)
    report = {"cases": results, "failures": failures,
              "now": [{k: r.get(k) for k in ("pipeline_id", "evaluation_mode", "serving_status", "data_status",
                                             "run_status", "reason_code", "severity", "latest_business_date",
                                             "age_minutes", "slot_late_minutes", "caveats")} for r in now_rows],
              "bytes_processed": bq.bytes_billed, "queries": bq.queries_issued}
    out = Path(args.out) if args.out else None
    text = json.dumps(report, ensure_ascii=False, indent=1, default=str)
    if out:
        out.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 1 if failures else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--project", default="project-fa311fc0-4d87-4781-986")
    ap.add_argument("--token-env", default="BQ_READONLY_ACCESS_TOKEN")
    ap.add_argument("--token-command")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("render")
    r.add_argument("name")
    r.add_argument("--as-of", default="NOW", help="'YYYY-MM-DD HH:MM:SS' (МСК) или NOW")
    b = sub.add_parser("backtest")
    b.add_argument("--out")
    sub.add_parser("contract")
    args = ap.parse_args(argv)
    if args.cmd == "render":
        objects = load_objects()
        params = objects[args.name]["params"]
        print(render(args.name, [_as_of_sql(args.as_of)] if params else None, objects))
        return 0
    if args.cmd == "contract":
        print(json.dumps(load_contract(), ensure_ascii=False, indent=1))
        return 0
    return run_backtest(args)


if __name__ == "__main__":
    raise SystemExit(main())
