#!/usr/bin/env python3
"""Mig1b-0: проверки целевого выката ОДНОГО Job'а `wb-stocks-prod`.

Используется `.github/workflows/deploy-prod-stocks.yml` до и после `gcloud run jobs update`.
Только чтение JSON, который вернул `gcloud run jobs describe/list --format=json`.

  pre   <shadow.json> <digest>                 — digest = образ wb-stocks-shadow (проверен в shadow)
  post  <stocks_prod.json> <before_list.json> <after_list.json> <digest> <git_sha>
        — у wb-stocks-prod образ = digest, env = боевые таблицы + provenance, SA = prod,
          args не изменены этим выкатом; ни один ДРУГОЙ Job не изменился (образ, generation, env, args).

Код выхода 0 — проверка пройдена, 1 — нарушение (выкат считается неудачным), 2 — ошибка ввода.
"""
from __future__ import annotations

import json
import sys

TARGET_JOB = "wb-stocks-prod"
PROD_SA_PREFIX = "sa-loaders-prod@"

# Боевые таблицы и режим — те же, что пишет Apps Script (cloud/src/loaders/stocks/targets.ts).
PROD_STOCKS_ENV = {
    "STOCKS_RAW_TABLE": "RAW_WB_STOCKS",
    "STOCKS_SNAPSHOT_TABLE": "WB_STOCKS_SNAPSHOTS",
    "STOCKS_T5_TABLE": "RAW_WB_STOCKS_T5",
    "STOCKS_T5_MODE": "on",
    "REF_SKU_TABLE": "REF_SKU_MASTER",
    "WB_ANALYTICS_SECRET": "WB_TOKEN_ANALYTICS",
}


def _container(job: dict) -> dict:
    return job["spec"]["template"]["spec"]["template"]["spec"]["containers"][0]


def _state(job: dict) -> dict:
    c = _container(job)
    return {
        "image": c.get("image"),
        "generation": job.get("metadata", {}).get("generation"),
        "args": c.get("args"),
        "env": {e["name"]: e.get("value") for e in c.get("env", [])},
        "sa": job["spec"]["template"]["spec"]["template"]["spec"].get("serviceAccountName"),
    }


def _by_name(jobs: list) -> dict:
    return {j["metadata"]["name"]: _state(j) for j in jobs}


def check_pre(shadow: dict, digest: str) -> list[str]:
    img = _container(shadow).get("image")
    return [] if img == digest else [f"digest не равен образу wb-stocks-shadow: {img} ≠ {digest}"]


def check_post(target: dict, before: list, after: list, digest: str, git_sha: str) -> list[str]:
    problems: list[str] = []
    t = _state(target)
    if t["image"] != digest:
        problems.append(f"{TARGET_JOB}: образ {t['image']} ≠ {digest}")
    want = dict(PROD_STOCKS_ENV, IMAGE_DIGEST=digest, GIT_SHA=git_sha, ENVIRONMENT="prod")
    for k, v in want.items():
        if t["env"].get(k) != v:
            problems.append(f"{TARGET_JOB}: env {k}={t['env'].get(k)!r}, ожидалось {v!r}")
    if not str(t["sa"] or "").startswith(PROD_SA_PREFIX):
        problems.append(f"{TARGET_JOB}: SA {t['sa']} — не prod-загрузчик")

    b, a = _by_name(before), _by_name(after)
    if TARGET_JOB not in b:
        problems.append(f"{TARGET_JOB} отсутствует в снимке ДО выката")
    elif b[TARGET_JOB]["args"] != t["args"]:
        problems.append(f"{TARGET_JOB}: args изменились выкатом ({b[TARGET_JOB]['args']} → {t['args']}); args — зона Terraform")
    if set(b) != set(a):
        problems.append(f"состав Job'ов изменился: +{sorted(set(a) - set(b))} −{sorted(set(b) - set(a))}")
    for name in sorted(set(a) & set(b)):
        if name != TARGET_JOB and a[name] != b[name]:
            changed = [k for k in a[name] if a[name][k] != b[name][k]]
            problems.append(f"посторонний Job изменён: {name} ({', '.join(changed)})")
    return problems


def _load(path: str):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def main(argv: list[str]) -> int:
    try:
        if len(argv) == 4 and argv[1] == "pre":
            problems = check_pre(_load(argv[2]), argv[3])
        elif len(argv) == 7 and argv[1] == "post":
            problems = check_post(_load(argv[2]), _load(argv[3]), _load(argv[4]), argv[5], argv[6])
        else:
            print(__doc__, file=sys.stderr)
            return 2
    except (OSError, ValueError, KeyError, IndexError, TypeError) as e:
        print(f"STOCKS DEPLOY CHECK: ошибка ввода — {e}", file=sys.stderr)
        return 2
    for p in problems:
        print(f"STOCKS DEPLOY CHECK: {p}", file=sys.stderr)
    if problems:
        return 1
    print(f"STOCKS DEPLOY CHECK: OK ({argv[1]})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
