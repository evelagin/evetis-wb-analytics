#!/usr/bin/env python3
"""SPP-2 — развёртывание wb_mart.V_WB_SPP_DAILY (СПП WB по заказам, SKU × день).

Что делает: исполняет ровно один оператор `CREATE OR REPLACE VIEW` из Git
(sql/unitka/spp2/wb_mart/V_WB_SPP_DAILY.sql). Больше ничего: ни таблиц, ни данных, ни прав,
ни расписаний, ни записи в книгу. Маркетплейсы не вызываются.

Почему не sql/current: wb_mart не канонизирован (docs/architecture/CANONICAL_COVERAGE.md §3),
WB-вью идут тем же путём, что у PROMO-2 (sql/promotions/pr_promo2/wb_mart). HTTP-вызовы — общие
функции tools/promo_canonical_deploy.py, второй реализации нет.

Защиты (отказ ДО отправки):
  - объект вне закрытого списка OBJECTS — отказ;
  - в файле не ровно один `CREATE OR REPLACE VIEW` своего полного имени — отказ;
  - в тексте (вне строк и комментариев) есть DROP/INSERT/ALTER/… — отказ;
  - существующий объект с тем же именем другого типа — отказ;
  - без --apply ничего не создаётся: план и dry-run оператора поверх живых данных;
  - с --apply рабочее дерево чистое, HEAD = origin/main (в production — только слитый код).

usage:
  python tools/spp_daily_view_deploy.py --project <P> --token-command "gcloud auth print-access-token"
  python tools/spp_daily_view_deploy.py --project <P> --token-command "…" --apply
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import promo_canonical_deploy as d2  # noqa: E402
from lib.bq_readonly import resolve_token, strip_sql_comments  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PROJECT = "project-fa311fc0-4d87-4781-986"
OBJECTS = [("wb_mart", "V_WB_SPP_DAILY", "sql/unitka/spp2/wb_mart")]
LABEL = "unitka-spp-deploy"
FORBIDDEN = re.compile(r"\b(INSERT|UPDATE|DELETE|MERGE|TRUNCATE|DROP|ALTER|GRANT|REVOKE|EXPORT|CALL|"
                       r"EXECUTE\s+IMMEDIATE|ASSERT|BEGIN|COMMIT|ROLLBACK)\b", re.I)


def object_path(dataset: str, name: str) -> Path:
    for d, n, folder in OBJECTS:
        if (d, n) == (dataset, name):
            return ROOT / folder / f"{name}.sql"
    raise KeyError(f"{dataset}.{name} вне закрытого списка")


def statement(dataset: str, name: str) -> str:
    text = object_path(dataset, name).read_text(encoding="utf-8")
    bare = strip_sql_comments(text)
    fqn = f"`{PROJECT}.{dataset}.{name}`"
    creates = re.findall(r"\bCREATE\s+OR\s+REPLACE\s+VIEW\s+(`[^`]+`)", bare, re.I)
    if creates != [fqn] or len(re.findall(r"\bCREATE\b", bare, re.I)) != 1:
        raise SystemExit(f"{dataset}.{name}: ожидался ровно один CREATE OR REPLACE VIEW {fqn}, найдено {creates}")
    no_strings = re.sub(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'', "''", bare)   # описание — русский текст
    hit = FORBIDDEN.search(no_strings)
    if hit:
        raise SystemExit(f"{dataset}.{name}: запрещённое ключевое слово {hit.group(0)!r}")
    return text


def run_job(project: str, sql: str, token: str, dry: bool) -> dict:
    body = {"configuration": {"query": {"query": sql, "useLegacySql": False}, "dryRun": dry,
                              "labels": {"purpose": LABEL}},
            "jobReference": {"projectId": project, "location": "EU"}}
    code, job = d2.call("POST", f"{d2.HOST}/projects/{project}/jobs", token, body)
    if code != 200:
        raise SystemExit(f"jobs.insert HTTP {code}: {(job.get('error') or {}).get('message', '')[:300]}")
    if dry:
        return job
    jid = job["jobReference"]["jobId"]
    for _ in range(120):
        if job.get("status", {}).get("state") == "DONE":
            break
        time.sleep(1)
        _, job = d2.call("GET", f"{d2.HOST}/projects/{project}/jobs/{jid}?location=EU", token)
    err = job.get("status", {}).get("errorResult")
    if err:
        raise SystemExit(f"задание {jid} завершилось ошибкой: {err.get('message', '')[:300]}")
    return job


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--project", required=True)
    p.add_argument("--token-command")
    p.add_argument("--token-env", default="BQ_DEPLOY_ACCESS_TOKEN")
    p.add_argument("--apply", action="store_true")
    a = p.parse_args(argv)
    if a.project != PROJECT:
        raise SystemExit(f"проект {a.project!r} не совпадает с {PROJECT!r}")
    head = d2.git("rev-parse", "HEAD")
    if a.apply:
        if d2.git("status", "--porcelain"):
            raise SystemExit("--apply: рабочее дерево не чистое")
        if head != d2.git("rev-parse", "origin/main"):
            raise SystemExit("--apply: HEAD не совпадает с origin/main — развёртывается только слитый код")
    views = [(d, n, statement(d, n)) for d, n, _ in OBJECTS]        # все проверки до сети
    token = resolve_token(a.token_env, a.token_command, os.environ)
    print(f"git HEAD {head} · вью {len(views)} · режим {'APPLY' if a.apply else 'PLAN'}")
    for ds, n, _ in views:
        was, kind = d2.exists(a.project, ds, n, token)
        if was and kind != "VIEW":
            raise SystemExit(f"{ds}.{n} существует и это {kind} — отказ")
        print(f"{'REPLACE' if was else 'CREATE'} VIEW {ds}.{n}")
    for ds, n, sql in views:
        job = run_job(a.project, sql, token, dry=not a.apply)
        st = job.get("statistics", {}).get("query", {}).get("statementType")
        tag = "PLAN " if not a.apply else "DONE "
        print(f"{tag} {ds}.{n}\t{st}\t{'dry-run OK' if not a.apply else 'job ' + job['jobReference']['jobId']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
