#!/usr/bin/env python3
"""PR-PROMO-2 — развёртывание 16 представлений канонического слоя состояния акций.

Что делает: исполняет ровно 16 операторов `CREATE OR REPLACE VIEW` из Git в порядке
зависимостей (tools/promo_canonical_render.OBJECTS). Больше ничего: ни таблиц, ни данных,
ни прав, ни расписаний. Маркетплейсы не вызываются.

Защиты (отказ ДО отправки):
  - объект вне закрытого списка OBJECTS — отказ;
  - файл содержит не ровно один `CREATE OR REPLACE VIEW` своего полного имени — отказ;
  - в тексте встречается любой иной оператор изменения (DROP, INSERT, ALTER, …) — отказ;
  - без --apply ничего не создаётся: только план и dry-run каждого оператора;
  - с --apply рабочее дерево обязано быть чистым, а HEAD — совпадать с origin/main
    (провенанс: в production попадает только слитый код).

Отчёт: для каждого объекта — существовал ли он до (tables.get), statementType задания,
id задания. Токен не печатается.

usage:
  python tools/promo_canonical_deploy.py --project <P> --token-command "gcloud auth print-access-token"
  python tools/promo_canonical_deploy.py --project <P> --token-command "…" --apply
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import promo_canonical_render as render  # noqa: E402
from lib.bq_readonly import resolve_token, strip_sql_comments  # noqa: E402

HOST = "https://www.googleapis.com/bigquery/v2"
FORBIDDEN = re.compile(r"\b(INSERT|UPDATE|DELETE|MERGE|TRUNCATE|DROP|ALTER|GRANT|REVOKE|EXPORT|CALL|"
                       r"EXECUTE\s+IMMEDIATE|ASSERT|BEGIN|COMMIT|ROLLBACK)\b", re.I)


def statement(dataset: str, name: str) -> str:
    text = render.object_path(dataset, name).read_text(encoding="utf-8")
    bare = strip_sql_comments(text)
    fqn = f"`{render.PROJECT}.{dataset}.{name}`"
    creates = re.findall(r"\bCREATE\s+OR\s+REPLACE\s+VIEW\s+(`[^`]+`)", bare, re.I)
    if creates != [fqn] or len(re.findall(r"\bCREATE\b", bare, re.I)) != 1:
        raise SystemExit(f"{dataset}.{name}: ожидался ровно один CREATE OR REPLACE VIEW {fqn}, найдено {creates}")
    # строковые литералы (описание) не проверяем на ключевые слова: там русский текст
    no_strings = re.sub(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'', "''", bare)
    hit = FORBIDDEN.search(no_strings)
    if hit:
        raise SystemExit(f"{dataset}.{name}: запрещённое ключевое слово {hit.group(0)!r}")
    return text


def call(method: str, url: str, token: str, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except json.JSONDecodeError:
            return e.code, {}


def exists(project: str, dataset: str, name: str, token: str) -> tuple[bool, str | None]:
    code, body = call("GET", f"{HOST}/projects/{project}/datasets/{dataset}/tables/{name}", token)
    if code == 404:
        return False, None
    if code != 200:
        raise SystemExit(f"tables.get {dataset}.{name}: HTTP {code}")
    return True, body.get("type")


def run_job(project: str, sql: str, token: str, dry: bool) -> dict:
    body = {"configuration": {"query": {"query": sql, "useLegacySql": False}, "dryRun": dry,
                              "labels": {"purpose": "promo-canonical-deploy"}},
            "jobReference": {"projectId": project, "location": "EU"}}
    code, job = call("POST", f"{HOST}/projects/{project}/jobs", token, body)
    if code != 200:
        msg = (job.get("error") or {}).get("message", "")
        raise SystemExit(f"jobs.insert HTTP {code}: {msg[:300]}")
    if dry:
        return job
    jid = job["jobReference"]["jobId"]
    for _ in range(120):
        if job.get("status", {}).get("state") == "DONE":
            break
        time.sleep(1)
        _, job = call("GET", f"{HOST}/projects/{project}/jobs/{jid}?location=EU", token)
    err = job.get("status", {}).get("errorResult")
    if err:
        raise SystemExit(f"задание {jid} завершилось ошибкой: {err.get('message', '')[:300]}")
    return job


def git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True,
                          cwd=render.ROOT).stdout.strip()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--project", required=True)
    p.add_argument("--token-command")
    p.add_argument("--token-env", default="BQ_DEPLOY_ACCESS_TOKEN")
    p.add_argument("--apply", action="store_true")
    a = p.parse_args(argv)
    if a.project != render.PROJECT:
        raise SystemExit(f"проект {a.project!r} не совпадает с проектом слоя {render.PROJECT!r}")

    head = git("rev-parse", "HEAD")
    if a.apply:
        if git("status", "--porcelain"):
            raise SystemExit("--apply: рабочее дерево не чистое")
        if head != git("rev-parse", "origin/main"):
            raise SystemExit("--apply: HEAD не совпадает с origin/main — развёртывается только слитый код")

    stmts = [(d, n, statement(d, n)) for d, n, _ in render.OBJECTS]   # все проверки до любого запроса
    token = resolve_token(a.token_env, a.token_command, os.environ)
    print(f"git HEAD {head} · объектов {len(stmts)} · режим {'APPLY' if a.apply else 'PLAN (dry-run)'}")
    for d, n, sql in stmts:
        was, kind = exists(a.project, d, n, token)
        if was and kind != "VIEW":
            raise SystemExit(f"{d}.{n} существует и это {kind}, а не VIEW — отказ")
        if not a.apply:
            # DDL зависимого представления нельзя проверить dry-run, пока не созданы его
            # зависимости. Поэтому в плане компилируется тело: SELECT * с подстановкой тел
            # зависимостей из Git поверх живых RAW — тот же текст, который создаст DDL.
            probe = render.render_predeploy(f"SELECT * FROM `{render.PROJECT}.{d}.{n}`")
            job = run_job(a.project, probe, token, dry=True)
            cols = len(job.get("statistics", {}).get("query", {}).get("schema", {}).get("fields", []))
            print(f"PLAN  {d}.{n}\t{'REPLACE' if was else 'CREATE'}\tbody dry-run OK, {cols} columns")
            continue
        job = run_job(a.project, sql, token, dry=False)
        st = job.get("statistics", {}).get("query", {}).get("statementType")
        print(f"DONE  {d}.{n}\t{'REPLACE' if was else 'CREATE'}\t{st}\tjob {job['jobReference']['jobId']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
