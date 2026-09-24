#!/usr/bin/env python3
"""PR-PROMO-3 — развёртывание слоя экономики акций в BigQuery.

Что делает, строго в этом порядке:
  1. DDL базиса (sql/promotions/pr_promo3_basis_snapshot.sql) одним скриптом: две таблицы
     снимков (CREATE TABLE IF NOT EXISTS) и две процедуры (CREATE OR REPLACE PROCEDURE);
  2. семь представлений экономики, каждое — ровно один CREATE OR REPLACE VIEW своего имени;
  3. с --snapshot — CALL обеих процедур с trigger = 'deploy' (первый снимок текущего слота).
Больше ничего: ни прав, ни расписаний (они в Terraform), ни изменения существующих объектов.

Защиты (отказ ДО отправки):
  - в DDL ровно четыре CREATE — две таблицы и две процедуры из закрытого списка;
  - в DDL нет DROP/DELETE/UPDATE/MERGE/TRUNCATE/ALTER/GRANT/REVOKE/EXPORT; INSERT — только в две
    таблицы снимков;
  - в файле вью ровно один CREATE OR REPLACE VIEW своего полного имени и ничего иного;
  - существующий объект с тем же именем другого типа — отказ;
  - без --apply ничего не создаётся: план и dry-run тел поверх живых данных;
  - с --apply рабочее дерево чистое, HEAD = origin/main.

usage:
  python tools/promo_economics_deploy.py --project <P> --token-command "gcloud auth print-access-token"
  python tools/promo_economics_deploy.py --project <P> --token-command "…" --apply [--snapshot]
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import promo_canonical_deploy as d2  # noqa: E402
import promo_economics_render as er  # noqa: E402
from lib.bq_readonly import resolve_token, strip_sql_comments  # noqa: E402

P = er.PROJECT
TABLES = [("wb_raw", "WB_PROMO_ECONOMICS_BASIS_SNAPSHOT"), ("ozon_raw", "OZON_PROMO_ECONOMICS_BASIS_SNAPSHOT")]
PROCEDURES = [("wb_mart", "sp_snapshot_wb_promo_economics_basis"),
              ("ozon_mart", "sp_snapshot_ozon_promo_economics_basis")]
FORBIDDEN_DDL = re.compile(r"\b(DROP|DELETE|UPDATE|MERGE|TRUNCATE|ALTER|GRANT|REVOKE|EXPORT)\b", re.I)


def _no_strings(sql: str) -> str:
    return re.sub(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'', "''", strip_sql_comments(sql))


def ddl_script() -> str:
    text = er.BASIS_DDL.read_text(encoding="utf-8")
    bare = _no_strings(text)
    creates = re.findall(r"\bCREATE\s+(TABLE\s+IF\s+NOT\s+EXISTS|OR\s+REPLACE\s+PROCEDURE)\s+`([^`]+)`", bare, re.I)
    want = {f"{P}.{a}.{b}" for a, b in TABLES + PROCEDURES}
    if {c[1] for c in creates} != want or len(creates) != 4 or len(re.findall(r"\bCREATE\b", bare, re.I)) != 4:
        raise SystemExit(f"DDL: ожидались ровно 4 CREATE {sorted(want)}, найдено {creates}")
    hit = FORBIDDEN_DDL.search(bare)
    if hit:
        raise SystemExit(f"DDL: запрещённое ключевое слово {hit.group(0)!r}")
    inserts = set(re.findall(r"\bINSERT\s+INTO\s+`([^`]+)`", bare, re.I))
    if inserts != {f"{P}.{a}.{b}" for a, b in TABLES}:
        raise SystemExit(f"DDL: INSERT разрешён только в таблицы снимков, найдено {sorted(inserts)}")
    return text


def view_statement(dataset: str, name: str) -> str:
    text = er.base.object_path(dataset, name, er.OBJECTS).read_text(encoding="utf-8")
    bare = _no_strings(text)
    fqn = f"`{P}.{dataset}.{name}`"
    creates = re.findall(r"\bCREATE\s+OR\s+REPLACE\s+VIEW\s+(`[^`]+`)", bare, re.I)
    if creates != [fqn] or len(re.findall(r"\bCREATE\b", bare, re.I)) != 1:
        raise SystemExit(f"{dataset}.{name}: ожидался ровно один CREATE OR REPLACE VIEW {fqn}, найдено {creates}")
    hit = d2.FORBIDDEN.search(bare)
    if hit:
        raise SystemExit(f"{dataset}.{name}: запрещённое ключевое слово {hit.group(0)!r}")
    return text


def routine_exists(project: str, dataset: str, name: str, token: str) -> bool:
    code, _ = d2.call("GET", f"{d2.HOST}/projects/{project}/datasets/{dataset}/routines/{name}", token)
    if code not in (200, 404):
        raise SystemExit(f"routines.get {dataset}.{name}: HTTP {code}")
    return code == 200


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--project", required=True)
    p.add_argument("--token-command")
    p.add_argument("--token-env", default="BQ_DEPLOY_ACCESS_TOKEN")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--snapshot", action="store_true", help="после развёртывания снять первый базис текущего слота")
    a = p.parse_args(argv)
    if a.project != P:
        raise SystemExit(f"проект {a.project!r} не совпадает с {P!r}")
    head = d2.git("rev-parse", "HEAD")
    if a.apply:
        if d2.git("status", "--porcelain"):
            raise SystemExit("--apply: рабочее дерево не чистое")
        if head != d2.git("rev-parse", "origin/main"):
            raise SystemExit("--apply: HEAD не совпадает с origin/main — развёртывается только слитый код")
    ddl = ddl_script()
    views = [(d, n, view_statement(d, n)) for d, n, _ in er.NEW_OBJECTS]     # все проверки до сети
    token = resolve_token(a.token_env, a.token_command, os.environ)
    print(f"git HEAD {head} · таблиц 2 · процедур 2 · вью {len(views)} · режим {'APPLY' if a.apply else 'PLAN'}")

    for ds, t in TABLES:
        was, kind = d2.exists(a.project, ds, t, token)
        if was and kind != "TABLE":
            raise SystemExit(f"{ds}.{t} существует и это {kind} — отказ")
        print(f"{'KEEP ' if was else 'CREATE'} TABLE {ds}.{t}")
    for ds, r in PROCEDURES:
        print(f"{'REPLACE' if routine_exists(a.project, ds, r, token) else 'CREATE'} PROCEDURE {ds}.{r}")
    for ds, n, _ in views:
        was, kind = d2.exists(a.project, ds, n, token)
        if was and kind != "VIEW":
            raise SystemExit(f"{ds}.{n} существует и это {kind} — отказ")
        print(f"{'REPLACE' if was else 'CREATE'} VIEW {ds}.{n}")

    if not a.apply:
        for ds, n, _ in views:
            probe = er.render_predeploy_file(f"-- @check Q\nSELECT * FROM `{P}.{ds}.{n}`;\n")
            probe = probe.split("-- @check Q\n", 1)[1].strip().rstrip(";")
            job = d2.run_job(a.project, probe, token, dry=True)
            cols = len(job.get("statistics", {}).get("query", {}).get("schema", {}).get("fields", []))
            print(f"PLAN  {ds}.{n}\tbody dry-run OK (виртуальный снимок), {cols} columns")
        return 0

    job = d2.run_job(a.project, ddl, token, dry=False)
    print(f"DONE  DDL script\tjob {job['jobReference']['jobId']}")
    for ds, n, sql in views:
        job = d2.run_job(a.project, sql, token, dry=False)
        st = job.get("statistics", {}).get("query", {}).get("statementType")
        print(f"DONE  {ds}.{n}\t{st}\tjob {job['jobReference']['jobId']}")
    if a.snapshot:
        for ds, r in PROCEDURES:
            job = d2.run_job(a.project, f"CALL `{P}.{ds}.{r}`('deploy')", token, dry=False)
            print(f"DONE  CALL {ds}.{r}('deploy')\tjob {job['jobReference']['jobId']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
