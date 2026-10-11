#!/usr/bin/env python3
"""WB Store P&L, H0 (OWNER ACK 2026-10-11) — развёртывание исторического слоя и правок Phase C в wb_mart (+ wb_secure).

Что делает: исполняет закрытый список операторов `CREATE OR REPLACE VIEW` из Git в порядке графа зависимостей и
перечитывает тело каждого представления из BigQuery. Больше ничего: ни таблиц, ни данных, ни прав, ни расписаний,
ни записи в книгу. Справочник себестоимости правит отдельный инструмент (tools/cogs_bundle_start_fix.py).

  новые, первыми (store_inventory_loss_v1.sql): wb_secure.V_WB_INVENTORY_LOSS_ITEMS (поштучный аудит, только владелец) →
                                  wb_mart.V_WB_INVENTORY_LOSS_MONTHLY → _COMPENSATION_MATCH → _STATUS (агрегаты для
                                  P&L; читают закрытые таблицы wb_secure.WB_INVENTORY_LOSS_* — сначала wb_secure.tf и импорт)
  изменяемые (store_pnl_v1.sql):  V_WB_FINANCE_OPERATION_MAP → V_WB_STORE_FINANCE_COHORT_DAILY →
                                  V_WB_STORE_ACCOUNT_LEDGER → V_WB_FINANCE_NEW_OPERATIONS →
                                  V_WB_STORE_FINANCE_COVERAGE → V_WB_STORE_PNL_MONTHLY
  новые (store_pnl_historical_v1.sql): V_WB_FINANCE_HISTORICAL_OVERRIDES → V_WB_STORE_PNL_HISTORICAL →
                                  V_WB_STORE_PNL_PRESENTATION; затем wb_secure.V_WB_INVENTORY_LOSS_CLAIM_TRACKING
                                  (претензия, справочно, только владелец, читает журнал)

Порядок с образом: вью — ПЕРВЫМИ, образ с чтением V_WB_STORE_PNL_PRESENTATION — сразу после, В ОДНОМ ОКНЕ при
остановленном Scheduler. 🔴 Прежний образ с новой V_WB_STORE_PNL_MONTHLY НЕсовместим: его проверка строки не знает
списания утраты, август расходится на сумму списания → прогон падает STORE_PNL_IDENTITY (безопасно: ничего не пишет).
Откат — в обратном порядке: сначала прежний образ, затем --rollback вью (иначе HISTORICAL/PRESENTATION ссылаются на
колонки, которых в откатанной MONTHLY нет).

Откат (--rollback): 6 изменяемых вью получают тела production до H0 (sql/unitka/store_pnl_h0_rollback/), в обратном
порядке. Девять новых вью остаются (их читает только новый образ; при откате сначала возвращается прежний образ), DROP —
отдельное решение владельца.

Защиты (отказ ДО отправки): закрытый список; ровно один CREATE OR REPLACE VIEW своего имени в источнике; запрещённые
ключевые слова; объект другого типа; без --apply — только dry-run; с --apply — чистое дерево и HEAD = свежий origin/main;
живое тело изменяемой вью до изменения = тело отката (или уже целевое — повтор); новая вью — отсутствует или с целевым
телом; после каждого оператора — перечитывание.

usage:
  python tools/unitka_store_pnl_h0_deploy.py --project <P> --token-command "gcloud auth print-access-token"
  python tools/unitka_store_pnl_h0_deploy.py --project <P> --token-command "…" --apply
  python tools/unitka_store_pnl_h0_deploy.py --project <P> --token-command "…" --rollback [--apply]
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
ROLLBACK_DIR = "sql/unitka/store_pnl_h0_rollback"
CHANGED = [
    ("V_WB_FINANCE_OPERATION_MAP", "sql/unitka/store_pnl_v1.sql"),
    ("V_WB_STORE_FINANCE_COHORT_DAILY", "sql/unitka/store_pnl_v1.sql"),
    ("V_WB_STORE_ACCOUNT_LEDGER", "sql/unitka/store_pnl_v1.sql"),
    ("V_WB_FINANCE_NEW_OPERATIONS", "sql/unitka/store_pnl_v1.sql"),
    ("V_WB_STORE_FINANCE_COVERAGE", "sql/unitka/store_pnl_v1.sql"),
    ("V_WB_STORE_PNL_MONTHLY", "sql/unitka/store_pnl_v1.sql"),
]
LOSS = [
    ("V_WB_INVENTORY_LOSS_ITEMS", "sql/unitka/store_inventory_loss_v1.sql"),
    ("V_WB_INVENTORY_LOSS_MONTHLY", "sql/unitka/store_inventory_loss_v1.sql"),
    ("V_WB_INVENTORY_LOSS_COMPENSATION_MATCH", "sql/unitka/store_inventory_loss_v1.sql"),
    ("V_WB_INVENTORY_LOSS_STATUS", "sql/unitka/store_inventory_loss_v1.sql"),
]
NEW = [
    ("V_WB_FINANCE_HISTORICAL_OVERRIDES", "sql/unitka/store_pnl_historical_v1.sql"),
    ("V_WB_STORE_PNL_HISTORICAL", "sql/unitka/store_pnl_historical_v1.sql"),
    ("V_WB_STORE_PNL_PRESENTATION", "sql/unitka/store_pnl_historical_v1.sql"),
    ("V_WB_INVENTORY_LOSS_CLAIM_TRACKING", "sql/unitka/store_inventory_loss_v1.sql"),
]
OBJECTS = LOSS + CHANGED + NEW
NEW_NAMES = {n for n, _ in LOSS + NEW}
# Закрытый слой (поштучный аудит и претензия) — в wb_secure (ACL только владелец); всё остальное — wb_mart.
SECURE = {"V_WB_INVENTORY_LOSS_ITEMS", "V_WB_INVENTORY_LOSS_CLAIM_TRACKING"}


def dataset_of(name: str) -> str:
    return "wb_secure" if name in SECURE else "wb_mart"


def norm(body: str) -> str:
    """BigQuery хранит тело вью БЕЗ начальных строк комментариев после AS: сравнивать тела только без них
    (иначе живое тело после развёртывания «не совпадает» с Git — ложный MISMATCH и ложный дрейф при повторе)."""
    lines = body.strip().splitlines()
    while lines and (not lines[0].strip() or lines[0].lstrip().startswith("--")):
        lines.pop(0)
    return "\n".join(lines).strip()


def plan(rollback: bool) -> list[tuple[str, str, str]]:
    if rollback:
        return [(n, *statement("wb_mart", n, f"{ROLLBACK_DIR}/rollback_{n}.sql")) for n, _ in reversed(CHANGED)]
    return [(n, *statement(dataset_of(n), n, path)) for n, path in OBJECTS]


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--project", required=True)
    p.add_argument("--token-command")
    p.add_argument("--token-env", default="BQ_DEPLOY_ACCESS_TOKEN")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--rollback", action="store_true", help="6 изменяемых вью — тела production до H0, в обратном порядке")
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
    fwd = {n: norm(b) for n, _, b in plan(False)}
    old = {n: norm(b) for n, _, b in plan(True)}
    token = resolve_token(a.token_env, a.token_command, os.environ)
    print(f"git HEAD {head} · вью {len(views)} · режим {('ROLLBACK ' if a.rollback else '') + ('APPLY' if a.apply else 'PLAN')}")
    state = {}
    for n, _, _ in views:
        was, kind = d2.exists(a.project, dataset_of(n), n, token)
        if was and kind != "VIEW":
            raise SystemExit(f"{dataset_of(n)}.{n} существует и это {kind} — отказ")
        state[n] = was
        print(f"{'REPLACE' if was else 'CREATE'} VIEW {dataset_of(n)}.{n}")
    # Дрейф проверяется ВСЕГДА (и в PLAN): живое тело — прежнее или уже целевое; новое — нет или целевое.
    for n, _, _ in views:
        if n in NEW_NAMES:
            if state[n] and norm(live_view(a.project, dataset_of(n), n, token)[0]) != fwd[n]:
                raise SystemExit(f"{dataset_of(n)}.{n}: новая вью уже существует с чужим телом — дрейф, отказ")
            continue
        now = norm(live_view(a.project, "wb_mart", n, token)[0])
        if now not in (old[n], fwd[n]):
            raise SystemExit(f"wb_mart.{n}: живое тело не совпадает ни с прежним (откат), ни с целевым — дрейф, отказ")
    print("дрейф: нет (живые тела = ожидаемые)")
    order = [n for n, _, _ in views]
    for i, (n, stmt, body) in enumerate(views):
        earlier = [x for x in order[:i] if f".{dataset_of(x)}.{x}`" in body]
        if not a.apply and earlier and not a.rollback:
            # dry-run BigQuery видит ЖИВОЕ (ещё старое или отсутствующее) тело зависимости; тела проверены подстановкой
            print(f"PLAN  {dataset_of(n)}.{n}\tdry-run пропущен: читает изменяемые в этом прогоне {', '.join(earlier)}")
            continue
        job = run_job(a.project, stmt, token, dry=not a.apply)
        st = job.get("statistics", {}).get("query", {}).get("statementType")
        if not a.apply:
            print(f"PLAN  {dataset_of(n)}.{n}\t{st}\tdry-run OK")
            continue
        now = norm(live_view(a.project, dataset_of(n), n, token)[0])
        print(f"DONE  {dataset_of(n)}.{n}\t{st}\tjob {job['jobReference']['jobId']}\treadback {'MATCH' if now == norm(body) else 'MISMATCH'}")
        if now != norm(body):
            raise SystemExit(f"{dataset_of(n)}.{n}: тело в BigQuery не совпало с Git — развёртывание остановлено")
    return 0


if __name__ == "__main__":
    sys.exit(main())
