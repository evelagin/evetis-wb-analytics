#!/usr/bin/env python3
"""Этап C, ревью #426: прогон ВСЕГО SQL координатора, gate и DDL через парсер BigQuery — ТОЛЬКО dryRun.

Ручной/опциональный (в CI не запускается: нужен токен владельца). Ничего не создаёт и не меняет: каждый запрос
уходит с "dryRun": true на REST www.googleapis.com/bigquery/v2/projects/<p>/queries (хост bigquery.googleapis.com
у владельца блокируется). Таблиц wb_ops.INGEST_* ещё нет, поэтому:
  FULL   — запрос как есть (wb_raw, INFORMATION_SCHEMA — реальные объекты): синтаксис + семантика;
  CTE    — ссылки на wb_ops.INGEST_* заменены производной таблицей той же схемы (схема — из DDL): синтаксис + семантика;
  SCRIPT — многооператорный скрипт целиком: синтаксис (семантику dry-run скрипта BigQuery проверяет не всю);
  PROXY  — DML в таблицы INGEST_* переписан в SELECT тех же выражений над производной таблицей: семантика выражений,
           типов параметров и условий CAS.

  cd cloud && npx vite-node --config vitest.config.ts scripts/emit_cutover_sql.ts > /tmp/cutover_sql.json
  BQ_TOKEN=$(gcloud auth print-access-token) python3 tools/bq_dryrun_cutover_sql.py /tmp/cutover_sql.json [--ddl sql/ops/ingest_writer_ownership.sql]

Код выхода 0 — все проверки OK, 1 — есть ошибки.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PROJECT = os.environ.get("GCP_PROJECT_ID", "project-fa311fc0-4d87-4781-986")
ENDPOINT = f"https://www.googleapis.com/bigquery/v2/projects/{PROJECT}/queries"
TABLES = ("INGEST_WRITER_OWNERSHIP", "INGEST_CUTOVER_LOG", "INGEST_QUALIFICATION")


def ddl_schemas(ddl: str) -> dict[str, str]:
    """INGEST_* → тип STRUCT<…> из CREATE TABLE в DDL."""
    out = {}
    for t in TABLES:
        m = re.search(rf"CREATE TABLE IF NOT EXISTS `wb_ops\.{t}` \((.*?)\n\)", ddl, re.S)
        cols = []
        for line in m.group(1).splitlines():
            c = re.match(r"\s+([a-z_]+)\s+([A-Z0-9]+)", line)
            if c:
                cols.append(f"{c.group(1)} {c.group(2)}")
        out[t] = "STRUCT<" + ", ".join(cols) + ">"
    return out


def derived(schemas: dict[str, str], sql: str) -> str:
    for t, st in schemas.items():
        sql = re.sub(rf"`(?:[A-Za-z0-9_-]+\.)?wb_ops\.{t}`", f"(SELECT * FROM UNNEST(ARRAY<{st}>[]))", sql)
    return sql


def refs_ingest(sql: str) -> bool:
    return any(f"wb_ops.{t}" in sql for t in TABLES)


def proxies(schemas: dict[str, str], sql: str) -> list[str]:
    """DML в INGEST_* → SELECT тех же выражений (условие CAS, SET, VALUES) над производной таблицей."""
    out = []
    for m in re.finditer(r"MERGE\s+(`[^`]+`)\s+(\w+)\s+USING\s+(\(.*?\))\s+(\w+)\s+ON\s+(.*?)\s+WHEN MATCHED AND (.*?) THEN UPDATE SET(.*?);", sql, re.S):
        tbl, ta, using, sa, on, cond, sets = m.groups()
        exprs = ", ".join(f"{e.split('=', 1)[1].strip()} AS {e.split('=', 1)[0].strip()}" for e in re.split(r",\s*\n\s*|,\s+(?=[a-z_]+ = )", sets.strip()) if "=" in e)
        out.append(derived(schemas, f"SELECT {exprs} FROM {tbl} {ta}, {using} {sa} WHERE {on} AND {cond}"))
    for m in re.finditer(r"MERGE\s+(`[^`]+`)\s+(\w+)\s+USING\s+\((.*?)\)\s+(\w+)\s+ON\s+(.*?)\s+WHEN NOT MATCHED THEN\s+INSERT\s*\((.*?)\)\s*VALUES\s*\((.*?)\);", sql, re.S):
        _tbl, _ta, using, sa, _on, _cols, vals = m.groups()
        out.append(derived(schemas, f"SELECT {vals} FROM ({using}) {sa}"))
    m = re.match(r"\s*INSERT INTO\s+(`[^`]+`)\s*\((.*?)\)\s*(SELECT\b.*)$", sql, re.S)
    if m:
        out.append(derived(schemas, m.group(3)))
    for m in re.finditer(r"INSERT INTO\s+(`[^`]+`)\s*\((.*?)\)\s*VALUES\s*\((.*?)\);", sql, re.S):
        _tbl, cols, vals = m.groups()
        names = [c.strip() for c in cols.split(",")]
        out.append(f"SELECT {vals}")
        assert len(names) == len(re.findall(r",(?![^()]*\))", vals)) + 1, "число колонок INSERT ≠ числу значений"
    return out


def schema_problems(ddl: str, sql: str) -> list[str]:
    """Колонки INSERT и SET в INGEST_* — есть в схеме DDL (PROXY проверяет выражения, но не имена колонок цели)."""
    cols = {t: set(re.findall(r"([a-z_]+) [A-Z]", ddl_schemas(ddl)[t])) for t in TABLES}
    out = []
    for m in re.finditer(r"INSERT INTO\s+`[^`]*wb_ops\.(\w+)`\s*\((.*?)\)", sql, re.S):
        out += [f"{m.group(1)}: нет колонки {c.strip()}" for c in m.group(2).split(",") if c.strip() not in cols.get(m.group(1), set())]
    for m in re.finditer(r"MERGE\s+`[^`]*wb_ops\.(\w+)`.*?UPDATE SET(.*?);", sql, re.S):
        for c in re.findall(r"(?:^|,)\s*([a-z_]+)\s*=", m.group(2)):
            if c not in cols.get(m.group(1), set()):
                out.append(f"{m.group(1)}: нет колонки {c}")
    return out


def dry_run(token: str, sql: str, params: list[dict]) -> tuple[bool, str]:
    body = {"query": sql, "useLegacySql": False, "dryRun": True, "location": "EU"}
    if params:
        body["parameterMode"] = "NAMED"
        body["queryParameters"] = [
            {"name": p["name"], "parameterType": {"type": p["type"]}, "parameterValue": {"value": p["value"]}} for p in params
        ]
    assert body["dryRun"] is True  # только dry-run: ни одного исполнения
    req = urllib.request.Request(ENDPOINT, data=json.dumps(body).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            d = json.load(r)
        return True, f"{d.get('statementType', '?')} bytes={d.get('totalBytesProcessed', '?')}"
    except urllib.error.HTTPError as e:
        try:
            msg = json.load(e)["error"]["message"]
        except Exception:  # noqa: BLE001
            msg = str(e)
        return False, msg


def plan(stmts: list[dict], ddl: str) -> list[tuple[str, str, str, list]]:
    """(имя, режим, sql, params) — всё, что уходит в dry-run."""
    schemas = ddl_schemas(ddl)
    jobs = []
    for s in stmts:
        sql, params = s["sql"], s["params"]
        is_script = bool(re.match(r"\s*(BEGIN|DECLARE|ASSERT|INSERT|UPDATE|MERGE|DELETE)\b", sql)) or ";" in sql.strip().rstrip(";")
        if not refs_ingest(sql):
            jobs.append((s["name"], "FULL", sql, params))
        elif not is_script:
            jobs.append((s["name"], "CTE", derived(schemas, sql), params))
        else:
            jobs.append((s["name"], "SCRIPT", sql, params))
            for i, p in enumerate(proxies(schemas, sql)):
                jobs.append((f"{s['name']}#proxy{i + 1}", "PROXY", p, params))
            if sql.lstrip().startswith("ASSERT"):
                jobs.append((s["name"], "CTE", derived(schemas, sql.split(";")[0]), params))
    # DDL: каждый CREATE — отдельно (семантика схемы), весь файл — синтаксис, seed и ASSERT — через производные таблицы.
    jobs.append(("ddl.file", "SCRIPT", ddl, []))
    for m in re.finditer(r"(CREATE TABLE IF NOT EXISTS `wb_ops\.(\w+)`.*?);\s*\n", ddl, re.S):
        jobs.append((f"ddl.create.{m.group(2)}", "FULL", m.group(1), []))
    code = re.sub(r"--[^\n]*", "", ddl)  # справка о CAS в комментариях — не код
    seed = re.sub(r"\bstocks_owner\b(?=\s*(?:IN\b|,|=))", "'APPS_SCRIPT'", code)
    for i, p in enumerate(proxies(schemas, seed)):
        jobs.append((f"ddl.seed#proxy{i + 1}", "PROXY", p, []))
    for i, m in enumerate(re.finditer(r"^(ASSERT .*?);\s*$", seed, re.S | re.M)):
        body = m.group(1)
        jobs.append((f"ddl.assert{i + 1}", "CTE", derived(schemas, body), []))
    return jobs


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    token = os.environ.get("BQ_TOKEN", "").strip()
    if not token:
        print("нужен BQ_TOKEN (gcloud auth print-access-token); значение не печатается", file=sys.stderr)
        return 2
    stmts = json.load(open(argv[1], encoding="utf-8"))
    ddl_path = argv[argv.index("--ddl") + 1] if "--ddl" in argv else str(REPO / "sql/ops/ingest_writer_ownership.sql")
    ddl = open(ddl_path, encoding="utf-8").read()
    bad = 0
    for s in stmts + [{"name": "ddl.file", "sql": re.sub(r"--[^\n]*", "", ddl)}]:
        for p in schema_problems(ddl, s["sql"]):
            bad += 1
            print(f"FAIL SCHEMA {s['name']}: {p}")
    for name, mode, sql, params in plan(stmts, ddl):
        ok, msg = dry_run(token, sql, params)
        # Скрипт с DML в ещё не созданные INGEST_*: парсер прошёл весь текст и упал на поиске таблицы —
        # синтаксис подтверждён, семантика DML — в PROXY.
        if not ok and mode == "SCRIPT" and re.search(r"Not found: Table [\w:.-]+wb_ops\.INGEST_\w+ was not found", msg):
            ok, msg = True, f"синтаксис OK; таблицы ещё нет ({msg.split(' at ')[0]}) — семантика в PROXY"
        # Файлы паритета потоков читают shadow-таблицы __CR: пока shadow потока не включён, их нет.
        elif not ok and name.startswith("qualify.parity.") and re.search(r"Not found: Table [\w:.-]+__CR was not found", msg):
            ok, msg = True, f"синтаксис OK; shadow-таблиц ещё нет ({msg.split(' at ')[0]})"
        bad += 0 if ok else 1
        print(f"{'OK  ' if ok else 'FAIL'} {mode:6} {name}: {msg}")
    print(f"итого: {'ошибок ' + str(bad) if bad else 'все OK'}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
