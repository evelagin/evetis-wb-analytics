#!/usr/bin/env python3
"""Ozon Store P&L, D3 — развёртывание 7 новых вью ozon_mart (OWNER ACK 2026-10-14; развёртывание — отдельный ACK).

Что делает: исполняет закрытый список операторов `CREATE OR REPLACE VIEW` из канонических файлов
sql/current/ozon_mart/<OBJECT>.sql в порядке графа зависимостей (rebuild_order манифеста) и перечитывает тело
и описание каждого представления из BigQuery. Больше ничего: ни таблиц, ни данных, ни прав, ни расписаний, ни
записи в книгу. Существующие вью ozon_mart (FCT_OZON_PNL_MONTHLY и др., L1–L4) не меняются: все семь объектов
новые и никем, кроме QA (sql/ozon/qa_ozon_store_pnl_v1.sql), не читаются.

  V_OZON_FINANCE_OPERATION_MAP → V_OZON_STORE_ACCOUNT_LEDGER → V_OZON_FINANCE_NEW_OPERATIONS
  → V_OZON_STORE_ADS_RECON_MONTHLY → V_OZON_STORE_FINANCE_COVERAGE → V_OZON_STORE_MONTH_STATE
  → V_OZON_STORE_PNL_MONTHLY

Откат: вью остаются (их никто не читает); DROP — отдельное решение владельца, этот инструмент DROP не исполняет
(операторы — docs/finance/OZON_STORE_PNL_PHASE_D3.md §7). После развёртывания — read-only снятие R2C и
отдельный PR: sync_state pending_deploy → captured_live.

Защиты (отказ ДО отправки): закрытый список = объекты D3 манифеста в состоянии pending_deploy; ровно один
CREATE OR REPLACE VIEW своего имени в файле; запрещённые ключевые слова; существующий объект другого типа или
новая вью с чужим телом; без --apply — только dry-run; с --apply — чистое дерево и HEAD = свежий origin/main;
после каждого оператора — перечитывание тела и описания.

usage:
  python tools/ozon_store_pnl_deploy.py --project <P> --token-command "gcloud auth print-access-token"
  python tools/ozon_store_pnl_deploy.py --project <P> --token-command "…" --apply
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import promo_canonical_deploy as d2  # noqa: E402
from lib.bq_readonly import resolve_token  # noqa: E402
from spp_daily_view_deploy import run_job  # noqa: E402
from unitka_refusals_deploy import declared_description, live_view, statement  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PROJECT = "project-fa311fc0-4d87-4781-986"
DATASET = "ozon_mart"
MANIFEST = "sql/current/ozon_mart/MANIFEST.json"
OBJECTS = {
    "V_OZON_FINANCE_OPERATION_MAP",
    "V_OZON_STORE_ACCOUNT_LEDGER",
    "V_OZON_FINANCE_NEW_OPERATIONS",
    "V_OZON_STORE_FINANCE_COVERAGE",
    "V_OZON_STORE_ADS_RECON_MONTHLY",
    "V_OZON_STORE_MONTH_STATE",
    "V_OZON_STORE_PNL_MONTHLY",
}


def ordered() -> list[str]:
    """Объекты D3 в порядке rebuild_order манифеста; каждый обязан быть pending_deploy и без снимка production."""
    m = json.loads((ROOT / MANIFEST).read_text(encoding="utf-8"))
    objs = {o["object_name"]: o for o in m["objects"]}
    for n in OBJECTS:
        o = objs.get(n)
        if o is None or o["sync_state"] != "pending_deploy" or o["live_body_sha256_at_capture"] is not None:
            raise SystemExit(f"{DATASET}.{n}: в манифесте ожидался новый объект pending_deploy без снимка production")
    return [n for n in m["rebuild_order"] if n in OBJECTS]


def plan() -> list[tuple[str, str, str]]:
    return [(n, *statement(DATASET, n, f"sql/current/{DATASET}/{n}.sql")) for n in ordered()]


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
    state = {}
    for n, _, body in views:
        was, kind = d2.exists(a.project, DATASET, n, token)
        if was and kind != "VIEW":
            raise SystemExit(f"{DATASET}.{n} существует и это {kind} — отказ")
        if was and live_view(a.project, DATASET, n, token)[0] != body:
            # новая вью: до развёртывания её нет; повтор после частичного прогона допустим, только если тело = Git
            raise SystemExit(f"{DATASET}.{n}: уже существует с чужим телом — дрейф, отказ")
        state[n] = was
        print(f"{'SAME' if was else 'CREATE'} VIEW {DATASET}.{n}")
    for i, (n, stmt, body) in enumerate(views):
        missing = [x for x, _, _ in views[:i] if not state[x] and f".{DATASET}.{x}`" in body]
        if not a.apply and missing:
            # dry-run BigQuery не примет ссылку на ещё не созданную вью; тела проверены подстановкой
            # (docs/finance/OZON_STORE_PNL_PHASE_D3.md §6, QA на живых данных)
            print(f"PLAN  {DATASET}.{n}\tdry-run пропущен: читает создаваемые в этом прогоне {', '.join(missing)}")
            continue
        job = run_job(a.project, stmt, token, dry=not a.apply)
        st = job.get("statistics", {}).get("query", {}).get("statementType")
        if not a.apply:
            print(f"PLAN  {DATASET}.{n}\t{st}\tdry-run OK")
            continue
        now, desc = live_view(a.project, DATASET, n, token)
        same = now == body and desc == declared_description(stmt)
        print(f"DONE  {DATASET}.{n}\t{st}\tjob {job['jobReference']['jobId']}\treadback {'MATCH' if same else 'MISMATCH'}")
        if not same:
            raise SystemExit(f"{DATASET}.{n}: тело или описание в BigQuery не совпали с Git — развёртывание остановлено")
    return 0


if __name__ == "__main__":
    sys.exit(main())
