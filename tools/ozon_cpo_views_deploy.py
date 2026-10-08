#!/usr/bin/env python3
"""Phase B «Оплата за заказ» Ozon — развёртывание 5 представлений ozon_mart (2026-10-08).

Что делает: исполняет закрытый список операторов `CREATE OR REPLACE VIEW` из sql/current/ozon_mart в порядке
графа зависимостей и перечитывает тело и описание каждого представления из BigQuery. Больше ничего: ни таблиц
(их создаёт Terraform, infra/terraform/ozon_cpo_orders.tf), ни данных, ни прав, ни расписаний, ни записи в книгу.
HTTP — общие функции tools/promo_canonical_deploy.py; разбор оператора — tools/ozon_order_date_msk_deploy.py.

  новые (CREATE):  V_OZON_ADS_CPO_ORDERS → V_OZON_ADS_CPO_RESIDUAL_DAILY, V_OZON_ADS_CPO_PROMOTED_DAILY
  изменённые:      FCT_OZON_SKU_PNL_DAILY (+cpo_expense_rub в хвосте) → V_OZON_SKU_PNL_DAILY_OPERATIONAL (+cpo_expense_rub)
Новые колонки — только в хвосте; прежние колонки и их значения не меняются (доказательство — QA и тесты).

Откат (--rollback): FCT и операционный слой получают тела production до изменения
(sql/ozon/cpo_orders_2026-10-08/rollback_<OBJECT>.sql); три новых представления остаются (их больше никто не
читает), DROP — отдельное решение владельца. Образ с Phase B читает cpo_expense_rub, поэтому при откате
сначала возвращается прежний образ, затем представления.

Защиты (отказ ДО отправки): закрытый список; ровно один CREATE OR REPLACE VIEW своего имени в источнике;
запрещённые ключевые слова; существующий объект другого типа; без --apply — только dry-run; с --apply —
чистое дерево и HEAD = свежий origin/main; живые тела до изменения = ожидаемые (новые — отсутствуют,
изменяемые — тела отката) — иначе дрейф, отказ до первого CREATE; после каждого оператора — перечитывание.

usage:
  python tools/ozon_cpo_views_deploy.py --project <P> --token-command "gcloud auth print-access-token"
  python tools/ozon_cpo_views_deploy.py --project <P> --token-command "…" --apply
  python tools/ozon_cpo_views_deploy.py --project <P> --token-command "…" --rollback [--apply]
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import promo_canonical_deploy as d2  # noqa: E402
from lib.bq_readonly import resolve_token  # noqa: E402
from spp_daily_view_deploy import run_job  # noqa: E402
from ozon_order_date_msk_deploy import statement, live_view, declared_description  # noqa: E402

PROJECT = "project-fa311fc0-4d87-4781-986"
ROLLBACK_DIR = "sql/ozon/cpo_orders_2026-10-08"
# Порядок = граф зависимостей (sql/current/ozon_mart/MANIFEST.json: уровни 0 → 1 → 2).
NEW = ["V_OZON_ADS_CPO_ORDERS", "V_OZON_ADS_CPO_PROMOTED_DAILY", "V_OZON_ADS_CPO_RESIDUAL_DAILY"]
CHANGED = ["FCT_OZON_SKU_PNL_DAILY", "V_OZON_SKU_PNL_DAILY_OPERATIONAL"]
OBJECTS = NEW + CHANGED


def plan(rollback: bool) -> list[tuple[str, str, str]]:
    if rollback:
        return [(n, *statement("ozon_mart", n, f"{ROLLBACK_DIR}/rollback_{n}.sql")) for n in reversed(CHANGED)]
    return [(n, *statement("ozon_mart", n, f"sql/current/ozon_mart/{n}.sql")) for n in OBJECTS]


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--project", required=True)
    p.add_argument("--token-command")
    p.add_argument("--token-env", default="BQ_DEPLOY_ACCESS_TOKEN")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--rollback", action="store_true", help="FCT и операционный слой — тела production до изменения")
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
    views = plan(a.rollback)                                          # все проверки источников — до сети
    fwd = {n: b for n, _, b in plan(False)}
    old = {n: b for n, _, b in plan(True)}
    token = resolve_token(a.token_env, a.token_command, os.environ)
    print(f"git HEAD {head} · вью {len(views)} · режим {('ROLLBACK ' if a.rollback else '') + ('APPLY' if a.apply else 'PLAN')}")
    state = {}
    for n, _, _ in views:
        was, kind = d2.exists(a.project, "ozon_mart", n, token)
        if was and kind != "VIEW":
            raise SystemExit(f"ozon_mart.{n} существует и это {kind} — отказ")
        state[n] = was
        print(f"{'REPLACE' if was else 'CREATE'} VIEW ozon_mart.{n}")
    if a.apply:
        for n, _, _ in views:
            if n in NEW:
                # Новое представление: до развёртывания его нет; повтор после частичного прогона — уже наше тело.
                if state[n] and live_view(a.project, "ozon_mart", n, token)[0] != fwd[n]:
                    raise SystemExit(f"ozon_mart.{n}: существует с чужим телом — дрейф, отказ")
                continue
            now, _ = live_view(a.project, "ozon_mart", n, token)
            allowed = [old[n], fwd[n]]                                # до изменения или уже наше (повтор)
            if now not in allowed:
                raise SystemExit(f"ozon_mart.{n}: живое тело не совпадает ни с прежним, ни с новым — дрейф, отказ")
        print("дрейф: нет (живые тела = ожидаемые)")
    base_missing = not a.rollback and not state.get(NEW[0], False)
    for n, stmt, body in views:
        if not a.apply and base_missing and n != NEW[0]:
            # Тело ссылается на ещё не созданную V_OZON_ADS_CPO_ORDERS: BigQuery dry-run его не примет.
            # Полное доказательство тел до развёртывания — dry-run с подстановкой (docs/finance/OZON_CPO_PHASE_B_2026-10-08.md §6).
            print(f"PLAN  ozon_mart.{n}\tSKIP dry-run: зависит от {NEW[0]}, который создаётся первым")
            continue
        job = run_job(a.project, stmt, token, dry=not a.apply)
        st = job.get("statistics", {}).get("query", {}).get("statementType")
        if not a.apply:
            print(f"PLAN  ozon_mart.{n}\t{st}\tdry-run OK")
            continue
        now, desc = live_view(a.project, "ozon_mart", n, token)
        want = declared_description(stmt)
        same = now == body and (want is None or desc == want)
        print(f"DONE  ozon_mart.{n}\t{st}\tjob {job['jobReference']['jobId']}\treadback {'MATCH' if same else 'MISMATCH'}")
        if not same:
            raise SystemExit(f"ozon_mart.{n}: тело или описание в BigQuery не совпали с Git — развёртывание остановлено")
    return 0


if __name__ == "__main__":
    sys.exit(main())
