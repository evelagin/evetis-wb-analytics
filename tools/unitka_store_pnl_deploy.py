#!/usr/bin/env python3
"""WB Store P&L, Phase C — развёртывание 6 новых вью wb_mart (OWNER ACK 2026-10-09).

Что делает: исполняет закрытый список операторов `CREATE OR REPLACE VIEW` из sql/unitka/store_pnl_v1.sql
в порядке графа зависимостей и перечитывает тело каждого представления из BigQuery. Больше ничего: ни таблиц
(снимок создаёт Terraform, infra/terraform/unitka_store_pnl.tf), ни данных, ни прав, ни расписаний, ни записи
в книгу. Существующие вью wb_mart не меняются: все шесть объектов новые и никем, кроме загрузчика
unitka-store-pnl и QA, не читаются.

  V_WB_FINANCE_OPERATION_MAP → V_WB_STORE_FINANCE_COHORT_DAILY → V_WB_STORE_ACCOUNT_LEDGER
  → V_WB_FINANCE_NEW_OPERATIONS → V_WB_STORE_FINANCE_COVERAGE
  → V_WB_STORE_PNL_MONTHLY (читает wb_ops.UNITKA_SKU_COMPONENTS_DAILY)

Откат: остановить Scheduler unitka-store-pnl-prod; вью остаются (их больше никто не читает), DROP — отдельное
решение владельца, этот инструмент DROP не исполняет.

Защиты (отказ ДО отправки): закрытый список; ровно один CREATE OR REPLACE VIEW своего имени в источнике;
запрещённые ключевые слова; существующий объект другого типа или новая вью с чужим телом; без --apply — только
dry-run; с --apply — чистое дерево и HEAD = свежий origin/main; таблица снимка должна существовать; после каждого
оператора — перечитывание тела.

usage:
  python tools/unitka_store_pnl_deploy.py --project <P> --token-command "gcloud auth print-access-token"
  python tools/unitka_store_pnl_deploy.py --project <P> --token-command "…" --apply
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
from unitka_refusals_deploy import statement, live_view  # noqa: E402

PROJECT = "project-fa311fc0-4d87-4781-986"
SOURCE = "sql/unitka/store_pnl_v1.sql"
SNAPSHOT = ("wb_ops", "UNITKA_SKU_COMPONENTS_DAILY")
# Порядок = граф зависимостей.
OBJECTS = [
    "V_WB_FINANCE_OPERATION_MAP",
    "V_WB_STORE_FINANCE_COHORT_DAILY",
    "V_WB_STORE_ACCOUNT_LEDGER",
    "V_WB_FINANCE_NEW_OPERATIONS",
    "V_WB_STORE_FINANCE_COVERAGE",
    "V_WB_STORE_PNL_MONTHLY",
]


def plan() -> list[tuple[str, str, str]]:
    return [(n, *statement("wb_mart", n, SOURCE)) for n in OBJECTS]


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
        d2.git("fetch", "--quiet", "origin", "main")
        if d2.git("status", "--porcelain"):
            raise SystemExit("--apply: рабочее дерево не чистое")
        if head != d2.git("rev-parse", "origin/main"):
            raise SystemExit("--apply: HEAD не совпадает с origin/main — развёртывается только слитый код")
    views = plan()                                                    # все проверки источника — до сети
    token = resolve_token(a.token_env, a.token_command, os.environ)
    print(f"git HEAD {head} · вью {len(views)} · режим {'APPLY' if a.apply else 'PLAN'}")
    snap, kind = d2.exists(a.project, *SNAPSHOT, token)
    if not snap or kind != "TABLE":
        raise SystemExit(f"{'.'.join(SNAPSHOT)}: таблицы снимка нет — сначала targeted apply infra/terraform/unitka_store_pnl.tf")
    state = {}
    for n, _, body in views:
        was, kind = d2.exists(a.project, "wb_mart", n, token)
        if was and kind != "VIEW":
            raise SystemExit(f"wb_mart.{n} существует и это {kind} — отказ")
        if was and live_view(a.project, "wb_mart", n, token)[0] != body:
            # новая вью: до развёртывания её нет; повтор после частичного прогона допустим, только если тело = Git
            raise SystemExit(f"wb_mart.{n}: уже существует с чужим телом — дрейф, отказ")
        state[n] = was
        print(f"{'SAME' if was else 'CREATE'} VIEW wb_mart.{n}")
    for i, (n, stmt, body) in enumerate(views):
        missing = [x for x, _, _ in views[:i] if not state[x] and f".wb_mart.{x}`" in body]
        if not a.apply and missing:
            # dry-run BigQuery не примет ссылку на ещё не созданную вью; тела проверены подстановкой
            # (docs/finance/WB_STORE_PNL_PHASE_C_2026-10-09.md, фикстура на живых данных)
            print(f"PLAN  wb_mart.{n}\tdry-run пропущен: читает создаваемые в этом прогоне {', '.join(missing)}")
            continue
        job = run_job(a.project, stmt, token, dry=not a.apply)
        st = job.get("statistics", {}).get("query", {}).get("statementType")
        if not a.apply:
            print(f"PLAN  wb_mart.{n}\t{st}\tdry-run OK")
            continue
        now, _ = live_view(a.project, "wb_mart", n, token)
        print(f"DONE  wb_mart.{n}\t{st}\tjob {job['jobReference']['jobId']}\treadback {'MATCH' if now == body else 'MISMATCH'}")
        if now != body:
            raise SystemExit(f"wb_mart.{n}: тело в BigQuery не совпало с Git — развёртывание остановлено")
    return 0


if __name__ == "__main__":
    sys.exit(main())
