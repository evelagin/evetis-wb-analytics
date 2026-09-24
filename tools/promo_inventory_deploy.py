#!/usr/bin/env python3
"""PR-PROMO-4 — развёртывание слоя запасов и распродажи в BigQuery.

Что делает, строго в этом порядке:
  1. DDL справочников владельца (sql/promotions/pr_promo4_planning_refs.sql): две пустые таблицы
     evetis_ref.REF_SALES_PLAN_APPROVAL и evetis_ref.REF_SKU_INVENTORY_TARGET
     (CREATE TABLE IF NOT EXISTS — существующие не трогаются);
  2. семь представлений evetis_mart, каждое — ровно один CREATE OR REPLACE VIEW своего имени,
     в порядке зависимостей.
Больше ничего: ни процедур, ни расписаний, ни прав, ни записи данных. Снимков нет — история
запаса уже ведётся Control Tower (evetis_ref.CT_INVENTORY_SNAPSHOT_DAILY) и переиспользуется.

Защиты (отказ ДО отправки):
  - в DDL ровно два CREATE TABLE IF NOT EXISTS из закрытого списка и ни одного другого оператора;
  - в DDL и в файлах вью нет DROP/DELETE/UPDATE/MERGE/TRUNCATE/ALTER/GRANT/REVOKE/EXPORT/INSERT;
  - в файле вью ровно один CREATE OR REPLACE VIEW своего полного имени;
  - существующий объект с тем же именем другого типа — отказ;
  - без --apply ничего не создаётся: план и dry-run тел поверх живых данных;
  - с --apply рабочее дерево чистое, HEAD = origin/main.

usage:
  python tools/promo_inventory_deploy.py --project <P> --token-command "gcloud auth print-access-token"
  python tools/promo_inventory_deploy.py --project <P> --token-command "…" --apply
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import promo_canonical_deploy as d2  # noqa: E402
import promo_inventory_render as ir  # noqa: E402
from lib.bq_readonly import resolve_token, strip_sql_comments  # noqa: E402

P = ir.PROJECT
TABLES = ir.NEW_TABLES
FORBIDDEN = re.compile(r"\b(DROP|DELETE|UPDATE|MERGE|TRUNCATE|ALTER|GRANT|REVOKE|EXPORT|INSERT|CALL|EXECUTE)\b", re.I)


def _no_strings(sql: str) -> str:
    return re.sub(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'', "''", strip_sql_comments(sql))


def ddl_script() -> str:
    text = ir.REFS_DDL.read_text(encoding="utf-8")
    bare = _no_strings(text)
    creates = re.findall(r"\bCREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+`([^`]+)`", bare, re.I)
    want = {f"{P}.{a}.{b}" for a, b in TABLES}
    if set(creates) != want or len(creates) != 2 or len(re.findall(r"\bCREATE\b", bare, re.I)) != 2:
        raise SystemExit(f"DDL: ожидались ровно 2 CREATE TABLE IF NOT EXISTS {sorted(want)}, найдено {creates}")
    hit = FORBIDDEN.search(bare)
    if hit:
        raise SystemExit(f"DDL: запрещённое ключевое слово {hit.group(0)!r}")
    return text


def view_statement(dataset: str, name: str) -> str:
    text = ir.base.object_path(dataset, name, ir.OBJECTS).read_text(encoding="utf-8")
    bare = _no_strings(text)
    fqn = f"`{P}.{dataset}.{name}`"
    creates = re.findall(r"\bCREATE\s+OR\s+REPLACE\s+VIEW\s+(`[^`]+`)", bare, re.I)
    if creates != [fqn] or len(re.findall(r"\bCREATE\b", bare, re.I)) != 1:
        raise SystemExit(f"{dataset}.{name}: ожидался ровно один CREATE OR REPLACE VIEW {fqn}, найдено {creates}")
    hit = FORBIDDEN.search(bare)
    if hit:
        raise SystemExit(f"{dataset}.{name}: запрещённое ключевое слово {hit.group(0)!r}")
    return text


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--project", required=True)
    p.add_argument("--token-command")
    p.add_argument("--token-env", default="BQ_DEPLOY_ACCESS_TOKEN")
    p.add_argument("--apply", action="store_true")
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
    views = [(d, n, view_statement(d, n)) for d, n, _ in ir.OBJECTS]     # все проверки до сети
    token = resolve_token(a.token_env, a.token_command, os.environ)
    print(f"git HEAD {head} · таблиц 2 · вью {len(views)} · режим {'APPLY' if a.apply else 'PLAN'}")

    for ds, t in TABLES:
        was, kind = d2.exists(a.project, ds, t, token)
        if was and kind != "TABLE":
            raise SystemExit(f"{ds}.{t} существует и это {kind} — отказ")
        print(f"{'KEEP ' if was else 'CREATE'} TABLE {ds}.{t}")
    for ds, n, _ in views:
        was, kind = d2.exists(a.project, ds, n, token)
        if was and kind != "VIEW":
            raise SystemExit(f"{ds}.{n} существует и это {kind} — отказ")
        print(f"{'REPLACE' if was else 'CREATE'} VIEW {ds}.{n}")

    if not a.apply:
        for ds, n, _ in views:
            probe = ir.render_predeploy(f"SELECT * FROM `{P}.{ds}.{n}`")
            job = d2.run_job(a.project, probe, token, dry=True)
            cols = len(job.get("statistics", {}).get("query", {}).get("schema", {}).get("fields", []))
            print(f"PLAN  {ds}.{n}\tbody dry-run OK, {cols} columns")
        return 0

    job = d2.run_job(a.project, ddl, token, dry=False)
    print(f"DONE  DDL script\tjob {job['jobReference']['jobId']}")
    for ds, n, sql in views:
        job = d2.run_job(a.project, sql, token, dry=False)
        st = job.get("statistics", {}).get("query", {}).get("statementType")
        print(f"DONE  {ds}.{n}\t{st}\tjob {job['jobReference']['jobId']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
