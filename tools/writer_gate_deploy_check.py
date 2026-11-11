#!/usr/bin/env python3
"""Этап C: предпроверка deploy-prod — образ с writer-ownership gate не продвигается в prod-Job потока, пока нет
таблицы владения (6 строк, по одной на поток) и права её чтения у SA этого Job'а. Иначе Job после выката
fail-closed (WRITER_GATE_UNAVAILABLE) — поток встанет.

  needs <mode> <jobs.json>
        — печатает yes/no: есть ли в охвате режима существующие prod-Job'ы под gate (только чтение снимка).
  check <mode> <jobs.json> <rows.json> <policy.json>
        — rows.json: `bq head --format=json` таблицы wb_ops.INGEST_WRITER_OWNERSHIP;
          policy.json: `bq get-iam-policy --format=json` той же таблицы.
          Проверяет 6 строк шести потоков (owner из списка, epoch ≥ 1) и что SA каждого Job'а под gate
          имеет на таблице dataViewer/dataEditor/dataOwner (табличная привязка, её создаёт ingest_cutover.tf).

Код выхода 0 — можно выкатывать, 1 — нельзя, 2 — ошибка ввода. Сам ничего не вызывает.
Job под gate: Job'ы потоков M2–M5 всегда; wb-stocks-prod — только при env WRITER_GATE_STOCKS=1 (M1, §8 документа).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from deploy_scope_check import allowed  # noqa: E402
from stocks_prod_deploy_check import _by_name  # noqa: E402

STREAMS = ("stocks", "statistics", "ads", "finance", "supplies", "ref")
OWNERS = ("APPS_SCRIPT", "CLOUD_RUN", "NONE")
GATED_STREAM_JOBS = frozenset({
    "wb-orders-prod", "wb-sales-prod", "wb-sales-reconcile-prod",
    "wb-ads-prod", "wb-ads-query-prod", "wb-ads-costs-audit-prod",
    "wb-finance-prod", "wb-supplies-prod", "ref-sync-prod",
})
READ_ROLES = ("roles/bigquery.dataViewer", "roles/bigquery.dataEditor", "roles/bigquery.dataOwner", "roles/bigquery.admin")


def gated_jobs(mode: str, jobs: list) -> dict[str, str]:
    """Существующие Job'ы режима, которые читают строку владения: имя → SA."""
    state = _by_name(jobs)
    out = {}
    for name in sorted(allowed(mode) & set(state)):
        s = state[name]
        if name in GATED_STREAM_JOBS or (name == "wb-stocks-prod" and s["env"].get("WRITER_GATE_STOCKS") == "1"):
            out[name] = s["sa"] or ""
    return out


def check(mode: str, jobs: list, rows: list, policy: dict) -> list[str]:
    problems: list[str] = []
    targets = gated_jobs(mode, jobs)
    if not targets:
        return problems
    streams = [str(r.get("stream", "")) for r in rows]
    if sorted(streams) != sorted(STREAMS):
        problems.append(f"таблица владения: потоки {sorted(streams)}, ожидались ровно {sorted(STREAMS)} по одной строке")
    for r in rows:
        if str(r.get("owner", "")) not in OWNERS:
            problems.append(f"таблица владения: поток {r.get('stream')} — owner {r.get('owner')!r}")
        try:
            if int(str(r.get("epoch", "0"))) < 1:
                raise ValueError
        except ValueError:
            problems.append(f"таблица владения: поток {r.get('stream')} — epoch {r.get('epoch')!r}")
    readers = {m for b in policy.get("bindings", []) if b.get("role") in READ_ROLES for m in b.get("members", [])}
    for name, sa in targets.items():
        if not sa:
            problems.append(f"{name}: у Job'а нет serviceAccountName")
        elif f"serviceAccount:{sa}" not in readers:
            problems.append(f"{name}: SA {sa} не читает wb_ops.INGEST_WRITER_OWNERSHIP — после выката Job встанет (WRITER_GATE_UNAVAILABLE)")
    return problems


def _load(path: str):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def main(argv: list[str]) -> int:
    try:
        if len(argv) == 4 and argv[1] == "needs":
            print("yes" if gated_jobs(argv[2], _load(argv[3])) else "no")
            return 0
        if len(argv) == 6 and argv[1] == "check":
            problems = check(argv[2], _load(argv[3]), _load(argv[4]), _load(argv[5]))
        else:
            print(__doc__, file=sys.stderr)
            return 2
    except (OSError, ValueError, KeyError, IndexError, TypeError) as e:
        print(f"WRITER GATE DEPLOY CHECK: ошибка ввода — {e}", file=sys.stderr)
        return 2
    for p in problems:
        print(f"WRITER GATE DEPLOY CHECK: {p}", file=sys.stderr)
    if problems:
        return 1
    print(f"WRITER GATE DEPLOY CHECK: OK ({argv[2]})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
