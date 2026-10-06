#!/usr/bin/env python3
"""Бизнес-дата заказа Ozon = сутки МСК — развёртывание 7 представлений ozon_mart и 2 Control Tower (2026-10-06).

Что делает: исполняет закрытый список операторов `CREATE OR REPLACE VIEW` из Git в порядке графа
зависимостей и перечитывает тело каждого представления из BigQuery. Больше ничего: ни таблиц, ни данных,
ни прав, ни расписаний, ни записи в книгу; `CALL sp_ct_refresh_daily()` — отдельный шаг владельца
(docs/ops/OZON_ORDER_DATE_MSK_2026-10-06.md). HTTP-вызовы — общие функции tools/promo_canonical_deploy.py.

Источники: ozon_mart — sql/current/ozon_mart/<OBJECT>.sql (по одному оператору в файле);
wb_mart — sql/control_tower/ct_ozon_order_date_msk_2026-10-06.sql (оператор своего имени).
Откат (--rollback): sql/ozon/order_date_msk_2026-10-06/rollback_<OBJECT>.sql — тела production до изменения.

Защиты (отказ ДО отправки):
  - объект вне закрытого списка — отказ; в источнике не ровно один CREATE OR REPLACE VIEW своего имени — отказ;
  - в операторе (вне строк и комментариев) есть DROP/INSERT/ALTER/CALL/… — отказ;
  - существующий объект другого типа — отказ;
  - без --apply ничего не создаётся: план и dry-run каждого оператора поверх живых объектов;
  - с --apply рабочее дерево чистое, HEAD = свежий origin/main (git fetch; в production — только слитый код);
  - с --apply живое тело каждого объекта до изменения = тело отката (вперёд) либо одно из двух (откат):
    дрейф production после снятия останавливает всё ДО первого CREATE;
  - после каждого оператора — перечитывание тела и описания; несовпадение останавливает развёртывание.

usage:
  python tools/ozon_order_date_msk_deploy.py --project <P> --token-command "gcloud auth print-access-token"
  python tools/ozon_order_date_msk_deploy.py --project <P> --token-command "…" --apply
  python tools/ozon_order_date_msk_deploy.py --project <P> --token-command "…" --rollback [--apply]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import promo_canonical_deploy as d2  # noqa: E402
from lib.bq_readonly import resolve_token, strip_sql_comments  # noqa: E402
from spp_daily_view_deploy import FORBIDDEN, run_job  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PROJECT = "project-fa311fc0-4d87-4781-986"
LABEL = "ozon-order-date-msk-deploy"
CT_MIGRATION = "sql/control_tower/ct_ozon_order_date_msk_2026-10-06.sql"
ROLLBACK_DIR = "sql/ozon/order_date_msk_2026-10-06"
# Порядок = граф зависимостей: уровень 0 → 1 → 2 ozon_mart, затем Control Tower (LIVE читает FCT_OZON_SKU_PNL_DAILY).
OBJECTS = [
    ("ozon_mart", "V_OZON_COMMISSION_POLICY", "sql/current/ozon_mart/V_OZON_COMMISSION_POLICY.sql"),
    ("ozon_mart", "V_OZON_LOGISTICS_ESTIMATOR", "sql/current/ozon_mart/V_OZON_LOGISTICS_ESTIMATOR.sql"),
    ("ozon_mart", "FCT_OZON_SKU_PNL_DAILY", "sql/current/ozon_mart/FCT_OZON_SKU_PNL_DAILY.sql"),
    ("ozon_mart", "FCT_OZON_SKU_PNL_MONTHLY", "sql/current/ozon_mart/FCT_OZON_SKU_PNL_MONTHLY.sql"),
    ("ozon_mart", "FCT_OZON_PNL_MONTHLY", "sql/current/ozon_mart/FCT_OZON_PNL_MONTHLY.sql"),
    ("ozon_mart", "V_OZON_SKU_PNL_DAILY_OPERATIONAL", "sql/current/ozon_mart/V_OZON_SKU_PNL_DAILY_OPERATIONAL.sql"),
    ("ozon_mart", "V_OZON_SKU_FORWARD_ECONOMICS_CURRENT", "sql/current/ozon_mart/V_OZON_SKU_FORWARD_ECONOMICS_CURRENT.sql"),
    ("wb_mart", "V_CT_ACTUAL_DAILY_LIVE", CT_MIGRATION),
    ("wb_mart", "V_CT_PLAN_VS_ACTUAL_DAILY", CT_MIGRATION),
]


def statement(dataset: str, name: str, path: str) -> tuple[str, str]:
    """(оператор, тело после AS) ровно для `project.dataset.name` из файла-источника."""
    text = (ROOT / path).read_text(encoding="utf-8")
    fqn = f"`{PROJECT}.{dataset}.{name}`"
    head = re.compile(r"^CREATE\s+OR\s+REPLACE\s+VIEW\s+(`[^`]+`)", re.I | re.M)
    starts = list(head.finditer(text))
    mine = [i for i, m in enumerate(starts) if m.group(1) == fqn]
    if len(mine) != 1 or len(head.findall(strip_sql_comments(text))) != len(starts):
        raise SystemExit(f"{dataset}.{name}: в {path} ожидался ровно один CREATE OR REPLACE VIEW {fqn}, найдено {len(mine)}")
    i = mine[0]
    end = starts[i + 1].start() if i + 1 < len(starts) else len(text)
    # Оператор — исходный текст (BigQuery хранит тело вместе с комментариями); проверки — по тексту без них.
    stmt = text[starts[i].start():end].strip()
    stmt = stmt[:-1].rstrip() if stmt.endswith(";") else stmt
    bare = strip_sql_comments(stmt)
    if len(re.findall(r"\bCREATE\b", bare, re.I)) != 1:
        raise SystemExit(f"{dataset}.{name}: в операторе больше одного CREATE")
    no_strings = re.sub(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'', "''", bare)
    hit = FORBIDDEN.search(no_strings)
    if hit:
        raise SystemExit(f"{dataset}.{name}: запрещённое ключевое слово {hit.group(0)!r}")
    m = re.search(r"\)\s*\nAS\s*\n|`\s*\nAS\s*\n|`\s+AS\s*\n", stmt)
    if not m:
        raise SystemExit(f"{dataset}.{name}: не найдено AS перед телом")
    return stmt, stmt[m.end():].strip()


def live_view(project: str, dataset: str, name: str, token: str) -> tuple[str, str | None]:
    """(тело, описание) представления в BigQuery."""
    code, t = d2.call("GET", f"{d2.HOST}/projects/{project}/datasets/{dataset}/tables/{name}", token)
    if code != 200:
        raise SystemExit(f"{dataset}.{name}: tables.get HTTP {code}")
    return ((t.get("view") or {}).get("query") or "").strip().rstrip(";").strip(), t.get("description")


def declared_description(stmt: str) -> str | None:
    """Описание из OPTIONS оператора (строковый литерал BigQuery в двойных кавычках) или None."""
    m = re.search(r'OPTIONS\s*\(\s*description\s*=\s*"((?:\\.|[^"\\])*)"\s*\)', stmt)
    return None if m is None else json.loads(f'"{m.group(1)}"')


def plan(rollback: bool) -> list[tuple[str, str, str, str]]:
    objs = list(reversed(OBJECTS)) if rollback else OBJECTS
    out = []
    for ds, n, path in objs:
        src = f"{ROLLBACK_DIR}/rollback_{n}.sql" if rollback else path
        stmt, body = statement(ds, n, src)
        out.append((ds, n, stmt, body))
    return out


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--project", required=True)
    p.add_argument("--token-command")
    p.add_argument("--token-env", default="BQ_DEPLOY_ACCESS_TOKEN")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--rollback", action="store_true", help="тела production до изменения, в обратном порядке")
    a = p.parse_args(argv)
    if a.project != PROJECT:
        raise SystemExit(f"проект {a.project!r} не совпадает с {PROJECT!r}")
    head = d2.git("rev-parse", "HEAD")
    if a.apply:
        d2.git("fetch", "--quiet", "origin", "main")
        if d2.git("status", "--porcelain"):
            raise SystemExit("--apply: рабочее дерево не чистое")
        if head != d2.git("rev-parse", "origin/main"):
            raise SystemExit("--apply: HEAD не совпадает с origin/main — развёртывается только слитый код")
    views = plan(a.rollback)                                         # все проверки до сети
    token = resolve_token(a.token_env, a.token_command, os.environ)
    mode = ("ROLLBACK " if a.rollback else "") + ("APPLY" if a.apply else "PLAN")
    print(f"git HEAD {head} · вью {len(views)} · режим {mode}")
    for ds, n, _, _ in views:
        was, kind = d2.exists(a.project, ds, n, token)
        if was and kind != "VIEW":
            raise SystemExit(f"{ds}.{n} существует и это {kind} — отказ")
        print(f"{'REPLACE' if was else 'CREATE'} VIEW {ds}.{n}")
    if a.apply:
        fwd = {(ds, n): b for ds, n, _, b in plan(False)}
        old = {(ds, n): b for ds, n, _, b in plan(True)}
        for ds, n, _, _ in views:
            now, _ = live_view(a.project, ds, n, token)
            allowed = [old[(ds, n)]] if not a.rollback else [fwd[(ds, n)], old[(ds, n)]]
            if now not in allowed:
                raise SystemExit(f"{ds}.{n}: живое тело не совпадает с ожидаемым состоянием до изменения — дрейф, отказ")
        print("дрейф: нет (живые тела = ожидаемые)")
    for ds, n, stmt, body in views:
        job = run_job(a.project, stmt, token, dry=not a.apply)
        st = job.get("statistics", {}).get("query", {}).get("statementType")
        if not a.apply:
            print(f"PLAN  {ds}.{n}\t{st}\tdry-run OK")
            continue
        now, desc = live_view(a.project, ds, n, token)
        want_desc = declared_description(stmt)
        same = now == body and (want_desc is None or desc == want_desc)
        print(f"DONE  {ds}.{n}\t{st}\tjob {job['jobReference']['jobId']}\treadback {'MATCH' if same else 'MISMATCH'}")
        if not same:
            raise SystemExit(f"{ds}.{n}: тело или описание в BigQuery не совпали с Git — развёртывание остановлено")
    return 0


if __name__ == "__main__":
    sys.exit(main())
