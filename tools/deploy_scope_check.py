#!/usr/bin/env python3
"""Охват выката deploy-prod.yml: какие Job'ы режим `only_job` вправе менять — и что изменилось на деле.

Единственный источник правды о составе режимов. Workflow обновляет Job'ы своими шагами, а этот
инструмент до и после выката сравнивает снимки `gcloud run jobs list --format=json`:

  allowed <mode>
        — печатает Job'ы, которые режим вправе обновить (по одному в строке).
  check <mode> <before.json> <after.json> <digest>
        — 1) не изменился ни один Job вне allowed(mode): образ, generation, env, args, SA
             (Heldi, Ozon-пайплайны, shadow-контуры и любые будущие Job'ы — вне любого режима);
          2) состав Job'ов не изменился (выкат не создаёт и не удаляет Job'ы — это зона Terraform);
          3) каждый существующий Job из allowed(mode) теперь на digest — ни один не пропущен
             (урок PR-1: пропущенный Job молча остаётся на старом образе).

Код выхода 0 — охват соблюдён, 1 — нарушение (выкат считается неудачным), 2 — ошибка ввода.
Ничего не вызывает сам: только читает JSON, который скачал workflow.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from stocks_prod_deploy_check import _by_name  # noqa: E402  (один и тот же снимок состояния Job'а)

# Прежний промоушен «во все prod-Job'ы» — состав шагов deploy-prod.yml до миграции M2–M5.
LEGACY_ALL_JOBS = frozenset({
    "wb-stocks-prod", "wb-mart-prod", "wb-prices-prod", "wb-tariffs-prod",
    "unitka-engine-prod", "ozon-unitka-prod", "ozon-cpo-orders-prod",
    "unitka-store-pnl-prod", "ozon-store-pnl-prod", "wb-paid-storage-prod",
    "wb-promo-prod", "wb-funnel-prod",
})

# Целевые режимы: ровно Job'ы своего потока. Job'ов M2–M5 нет, пока Terraform не создал их флагом
# mN_prod_enabled=true (OWNER ACK cutover потока) — тогда шаг только предупреждает.
STREAM_JOBS = {
    "wb-stocks-prod": frozenset({"wb-stocks-prod"}),
    "wb-statistics": frozenset({"wb-orders-prod", "wb-sales-prod", "wb-sales-reconcile-prod"}),
    "wb-ads": frozenset({"wb-ads-prod", "wb-ads-query-prod", "wb-ads-costs-audit-prod"}),
    "wb-finance": frozenset({"wb-finance-prod"}),
    "wb-supplies-ref": frozenset({"wb-supplies-prod", "ref-sync-prod"}),
}

# Плоскость управления (этап C): координатор cutover. Отдельный режим, НЕ входит в all — плановый
# промоушен «во все prod-Job'ы» не должен менять логику переключения писателей посреди cutover.
CONTROL_JOBS = {
    "ingest-cutover": frozenset({"ingest-cutover-prod", "ingest-qualify-prod"}),
}

MODES = ("all", *STREAM_JOBS, *CONTROL_JOBS)
# Широкий режим требует набрать эту фразу в форме dispatch (deploy-prod.yml, вход confirm_all).
CONFIRM_ALL_PHRASE = "PROMOTE-ALL-PROD"


def allowed(mode: str) -> frozenset[str]:
    if mode == "all":
        return LEGACY_ALL_JOBS.union(*STREAM_JOBS.values())
    if mode in STREAM_JOBS:
        return STREAM_JOBS[mode]
    if mode in CONTROL_JOBS:
        return CONTROL_JOBS[mode]
    raise ValueError(f"неизвестный режим only_job: {mode!r}; допустимы {', '.join(MODES)}")


def check(mode: str, before: list, after: list, digest: str) -> list[str]:
    ok = allowed(mode)
    b, a = _by_name(before), _by_name(after)
    problems: list[str] = []
    if set(b) != set(a):
        problems.append(f"состав Job'ов изменился: +{sorted(set(a) - set(b))} −{sorted(set(b) - set(a))}")
    for name in sorted(set(a) & set(b)):
        if name not in ok and a[name] != b[name]:
            changed = [k for k in a[name] if a[name][k] != b[name][k]]
            problems.append(f"Job вне режима {mode} изменён: {name} ({', '.join(changed)})")
    for name in sorted(ok & set(a)):
        if a[name]["image"] != digest:
            problems.append(f"{name}: образ {a[name]['image']} ≠ {digest} — Job режима {mode} не обновлён")
        if name in b and a[name]["args"] != b[name]["args"]:
            problems.append(f"{name}: args изменились выкатом — args принадлежат Terraform")
        if name in b and a[name]["sa"] != b[name]["sa"]:
            problems.append(f"{name}: SA изменился выкатом — SA принадлежит Terraform")
    return problems


def _load(path: str):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def main(argv: list[str]) -> int:
    try:
        if len(argv) == 3 and argv[1] == "allowed":
            print("\n".join(sorted(allowed(argv[2]))))
            return 0
        if len(argv) == 6 and argv[1] == "check":
            problems = check(argv[2], _load(argv[3]), _load(argv[4]), argv[5])
        else:
            print(__doc__, file=sys.stderr)
            return 2
    except (OSError, ValueError, KeyError, IndexError, TypeError) as e:
        print(f"DEPLOY SCOPE CHECK: ошибка ввода — {e}", file=sys.stderr)
        return 2
    for p in problems:
        print(f"DEPLOY SCOPE CHECK: {p}", file=sys.stderr)
    if problems:
        return 1
    print(f"DEPLOY SCOPE CHECK: OK ({argv[2]}: {len(allowed(argv[2]))} Job'ов в режиме)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
