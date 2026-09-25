#!/usr/bin/env python3
"""PR-PLAN-1 — развёртывание плана продаж и траектории запаса в BigQuery.

Что делает, строго в этом порядке:
  1. хранилище (sql/plan/plan1_storage.sql): пять таблиц evetis_ref (CREATE TABLE IF NOT EXISTS —
     существующие не трогаются) и две nullable-колонки реестра утверждений
     (ALTER TABLE REF_SALES_PLAN_APPROVAL ADD COLUMN IF NOT EXISTS);
  2. девять представлений PR-PLAN-1 и два переключённых представления PR-PROMO-4
     (V_SALES_PLAN_MONTHLY_CURRENT, V_SKU_INVENTORY_TRAJECTORY_MONTHLY_CURRENT), каждое — ровно
     один CREATE OR REPLACE VIEW своего имени, в порядке зависимостей;
  3. процедуры (sql/plan/plan1_procedures.sql) — только CREATE OR REPLACE PROCEDURE из
     закрытого списка. Процедуры не вызываются: запись плана и поступлений — отдельные ручные шаги.
Ни расписаний, ни прав, ни сервисных аккаунтов, ни записи данных.

Защиты (отказ ДО отправки):
  - в хранилище ровно 5 CREATE TABLE IF NOT EXISTS из закрытого списка и один ALTER TABLE
    реестра утверждений только с ADD COLUMN IF NOT EXISTS; нет DROP/DELETE/UPDATE/MERGE/
    TRUNCATE/GRANT/REVOKE/EXPORT/INSERT/CALL;
  - в файле вью ровно один CREATE OR REPLACE VIEW своего полного имени и ни одного запрещённого слова;
  - в процедурах ровно 7 CREATE OR REPLACE PROCEDURE из закрытого списка; INSERT только в таблицы
    PR-PLAN-1 и реестр утверждений; нет DROP/DELETE/UPDATE/MERGE/TRUNCATE/ALTER/GRANT/REVOKE/EXPORT;
  - существующий объект с тем же именем другого типа — отказ;
  - без --apply ничего не создаётся: план и dry-run тел представлений поверх живых данных;
  - с --apply рабочее дерево чистое, HEAD = origin/main.

usage:
  python tools/plan1_deploy.py --project <P> --token-command "gcloud auth print-access-token"
  python tools/plan1_deploy.py --project <P> --token-command "…" --apply
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
ROOT = ir.ROOT
STORAGE = ROOT / "sql" / "plan" / "plan1_storage.sql"
PROCEDURES = ROOT / "sql" / "plan" / "plan1_procedures.sql"
TABLES = ir.PLAN1_TABLES
APPROVAL = ("evetis_ref", "REF_SALES_PLAN_APPROVAL")
REPOINTED = ["V_SALES_PLAN_MONTHLY_CURRENT", "V_SKU_INVENTORY_TRAJECTORY_MONTHLY_CURRENT"]
VIEWS: list[tuple[str, str]] = [(d, n) for d, n, _ in ir.RENDER_OBJECTS
                                if n in {o[1] for o in ir.PLAN1_OBJECTS} or n in REPOINTED]
PROCS = ["sp_plan_register_legacy_ct", "sp_plan_propose_observed_run_rate", "sp_plan_event_core", "sp_plan_submit",
         "sp_plan_approve", "sp_plan_close", "sp_inbound_record"]
WRITABLE = {f"{P}.{d}.{n}" for d, n in TABLES} | {f"{P}.{APPROVAL[0]}.{APPROVAL[1]}"}
FORBIDDEN_DDL = re.compile(r"\b(DROP|DELETE|UPDATE|MERGE|TRUNCATE|GRANT|REVOKE|EXPORT|INSERT|CALL|EXECUTE)\b", re.I)
FORBIDDEN_VIEW = re.compile(r"\b(DROP|DELETE|UPDATE|MERGE|TRUNCATE|ALTER|GRANT|REVOKE|EXPORT|INSERT|CALL|EXECUTE)\b", re.I)
FORBIDDEN_PROC = re.compile(r"\b(DROP|DELETE|UPDATE|MERGE|TRUNCATE|ALTER|GRANT|REVOKE|EXPORT|EXECUTE)\b", re.I)


STRING_LITERAL = re.compile(r'"(?:\\.|[^"\\])*"' + "|" + r"'(?:\\.|[^'\\])*'")


def _no_strings(sql: str) -> str:
    # Сырые строки: литералы SQL с экранированием (например '\n') вырезаются целиком.
    return STRING_LITERAL.sub("''", strip_sql_comments(sql))


def storage_script() -> str:
    text = STORAGE.read_text(encoding="utf-8")
    bare = _no_strings(text)
    creates = re.findall(r"\bCREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+`([^`]+)`", bare, re.I)
    want = {f"{P}.{a}.{b}" for a, b in TABLES}
    if set(creates) != want or len(creates) != len(TABLES) or len(re.findall(r"\bCREATE\b", bare, re.I)) != len(TABLES):
        raise SystemExit(f"хранилище: ожидались ровно {len(TABLES)} CREATE TABLE IF NOT EXISTS {sorted(want)}, найдено {creates}")
    alters = re.findall(r"\bALTER\s+TABLE\s+`([^`]+)`((?:\s*ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS\s+\w+\s+\w+\s*(?:OPTIONS\s*\([^)]*\))?\s*,?)+)\s*;",
                        bare, re.I)
    if [a for a, _ in alters] != [f"{P}.{APPROVAL[0]}.{APPROVAL[1]}"] or len(re.findall(r"\bALTER\b", bare, re.I)) != 1:
        raise SystemExit(f"хранилище: допустим ровно один ALTER TABLE {APPROVAL} только с ADD COLUMN IF NOT EXISTS, найдено {alters}")
    hit = FORBIDDEN_DDL.search(bare)
    if hit:
        raise SystemExit(f"хранилище: запрещённое ключевое слово {hit.group(0)!r}")
    return text


def view_statement(dataset: str, name: str) -> str:
    text = ir.base.object_path(dataset, name, ir.RENDER_OBJECTS).read_text(encoding="utf-8")
    bare = _no_strings(text)
    fqn = f"`{P}.{dataset}.{name}`"
    creates = re.findall(r"\bCREATE\s+OR\s+REPLACE\s+VIEW\s+(`[^`]+`)", bare, re.I)
    if creates != [fqn] or len(re.findall(r"\bCREATE\b", bare, re.I)) != 1:
        raise SystemExit(f"{dataset}.{name}: ожидался ровно один CREATE OR REPLACE VIEW {fqn}, найдено {creates}")
    hit = FORBIDDEN_VIEW.search(bare)
    if hit:
        raise SystemExit(f"{dataset}.{name}: запрещённое ключевое слово {hit.group(0)!r}")
    return text


def procedures_script() -> str:
    text = PROCEDURES.read_text(encoding="utf-8")
    bare = _no_strings(text)
    procs = re.findall(r"\bCREATE\s+OR\s+REPLACE\s+PROCEDURE\s+`" + re.escape(P) + r"\.evetis_ref\.(\w+)`", bare, re.I)
    if procs != PROCS or len(re.findall(r"\bCREATE\s+OR\s+REPLACE\b", bare, re.I)) != len(PROCS):
        raise SystemExit(f"процедуры: ожидались {PROCS}, найдено {procs}")
    hit = FORBIDDEN_PROC.search(bare)
    if hit:
        raise SystemExit(f"процедуры: запрещённое ключевое слово {hit.group(0)!r}")
    targets = set(re.findall(r"\bINSERT\s+INTO\s+`([^`]+)`", bare, re.I))
    if not targets or not targets <= WRITABLE:
        raise SystemExit(f"процедуры: INSERT вне разрешённых таблиц: {sorted(targets - WRITABLE)}")
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
    storage = storage_script()
    views = [(d, n, view_statement(d, n)) for d, n in VIEWS]            # все проверки до сети
    procs = procedures_script()
    token = resolve_token(a.token_env, a.token_command, os.environ)
    print(f"git HEAD {head} · таблиц {len(TABLES)} + ALTER · вью {len(views)} · процедур {len(PROCS)} · "
          f"режим {'APPLY' if a.apply else 'PLAN'}")

    for ds, t in TABLES + [APPROVAL]:
        was, kind = d2.exists(a.project, ds, t, token)
        if was and kind != "TABLE":
            raise SystemExit(f"{ds}.{t} существует и это {kind} — отказ")
        print(f"{'KEEP ' if was else 'CREATE'} TABLE {ds}.{t}" + ("  (+2 nullable колонки)" if (ds, t) == APPROVAL else ""))
    if not d2.exists(a.project, *APPROVAL, token)[0]:
        raise SystemExit("реестр утверждений PR-PROMO-4 не найден — сначала PR-PROMO-4")
    for ds, n, _ in views:
        was, kind = d2.exists(a.project, ds, n, token)
        if was and kind != "VIEW":
            raise SystemExit(f"{ds}.{n} существует и это {kind} — отказ")
        print(f"{'REPLACE' if was else 'CREATE'} VIEW {ds}.{n}")
    for name in PROCS:
        print(f"CREATE OR REPLACE PROCEDURE evetis_ref.{name}")

    if not a.apply:
        for ds, n, _ in views:
            probe = ir.render_predeploy(f"SELECT * FROM `{P}.{ds}.{n}`")
            job = d2.run_job(a.project, probe, token, dry=True)
            cols = len(job.get("statistics", {}).get("query", {}).get("schema", {}).get("fields", []))
            print(f"PLAN  {ds}.{n}\tbody dry-run OK, {cols} columns")
        return 0

    job = d2.run_job(a.project, storage, token, dry=False)
    print(f"DONE  storage script\tjob {job['jobReference']['jobId']}")
    for ds, n, sql in views:
        job = d2.run_job(a.project, sql, token, dry=False)
        st = job.get("statistics", {}).get("query", {}).get("statementType")
        print(f"DONE  {ds}.{n}\t{st}\tjob {job['jobReference']['jobId']}")
    job = d2.run_job(a.project, procs, token, dry=False)
    print(f"DONE  procedures script\tjob {job['jobReference']['jobId']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
