#!/usr/bin/env python3
"""Fail-closed преддеплойная проверка переменных окружения. Stage A Closeout, F-19.

ЗАЧЕМ. `deploy/env.production.yaml` — снимок конфигурации на момент сборки образа
(2026-07-23). С тех пор production ушёл вперёд: ревизии 11…25 меняли ТОЛЬКО
переменные окружения. Команда из DEPLOY.md

    gcloud run deploy --env-vars-file deploy/env.production.yaml

перезаписывает НАБОР переменных целиком и молча выключила бы публикацию ответов в
Wildberries: в файле `WB_PUBLISH_ENABLED: "false"`, в живом сервисе — `true`.

ЧТО ДЕЛАЕТ. Сравнивает выбранный env-файл с тем, что реально работает в Cloud Run,
и завершается с ненулевым кодом, если хоть одна переменная изменилась бы или
исчезла. Ничего не деплоит и ничего не меняет — только читает.

ИСПОЛЬЗОВАНИЕ.

    python3 deploy/preflight_env.py                      # против живого Cloud Run
    python3 deploy/preflight_env.py --offline            # против снимка в репозитории
    python3 deploy/preflight_env.py --accept-changes     # осознанно разрешить дельту

Без `--accept-changes` любая дельта — это отказ. Это и есть fail-closed: деплой
не должен молча менять поведение production.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_FILE = HERE / "env.production.yaml"
LIVE_SNAPSHOT = HERE / "env.production.live.yaml"

PROJECT = "project-fa311fc0-4d87-4781-986"
REGION = "europe-west1"
SERVICE = "evetis-wb-communications"

# Переменные, изменение которых меняет ПОВЕДЕНИЕ во внешнем мире.
# Расхождение по ним печатается отдельно и первым.
BEHAVIOURAL = {
    "WB_PUBLISH_ENABLED",
    "WB_QUESTIONS_ENABLED",
    "WB_QUESTION_PUBLISH_ENABLED",
    "WB_VERIFY_BEFORE_PUBLISH",
    "COMMUNICATION_ENGINE_V2_ENABLED",
    "COMMUNICATION_ENGINE_V2_SHADOW_ONLY",
    "COMMUNICATION_ENGINE_V2_PRIMARY",
}


def parse_env_yaml(path: Path) -> dict[str, str]:
    """Плоский `KEY: "value"`. Полноценный YAML-парсер здесь не нужен и не ставится."""
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r'^([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*)$', line)
        if not m:
            continue
        key, value = m.group(1), m.group(2).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key] = value
    return out


def read_live() -> dict[str, str]:
    """Живые НЕсекретные переменные Cloud Run. Значения секретов не читаются."""
    proc = subprocess.run(
        ["gcloud", "run", "services", "describe", SERVICE,
         f"--project={PROJECT}", f"--region={REGION}", "--format=json"],
        capture_output=True, text=True, check=True,
    )
    container = json.loads(proc.stdout)["spec"]["template"]["spec"]["containers"][0]
    return {e["name"]: e["value"] for e in container.get("env", []) if "value" in e}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--file", type=Path, default=DEFAULT_FILE)
    ap.add_argument("--offline", action="store_true",
                    help="сравнивать со снимком env.production.live.yaml, а не с Cloud Run")
    ap.add_argument("--accept-changes", action="store_true",
                    help="осознанно разрешить перечисленные изменения")
    args = ap.parse_args()

    candidate = parse_env_yaml(args.file)
    if args.offline:
        live, source = parse_env_yaml(LIVE_SNAPSHOT), f"снимок {LIVE_SNAPSHOT.name}"
    else:
        try:
            live, source = read_live(), f"живой Cloud Run {SERVICE}"
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            print(f"ОТКАЗ: не удалось прочитать живую конфигурацию: {exc}", file=sys.stderr)
            print("Проверка fail-closed: без сравнения деплой не разрешается.", file=sys.stderr)
            return 2

    changed = {k: (live[k], candidate[k]) for k in candidate if k in live and live[k] != candidate[k]}
    removed = sorted(set(live) - set(candidate))
    added = sorted(set(candidate) - set(live))

    print(f"Файл деплоя : {args.file}")
    print(f"Сравнение с : {source}")
    print(f"Переменных  : в файле {len(candidate)}, живых {len(live)}")
    print()

    behavioural = {k: v for k, v in changed.items() if k in BEHAVIOURAL}
    if behavioural:
        print("🔴 ИЗМЕНИЛОСЬ БЫ ПОВЕДЕНИЕ ВО ВНЕШНЕМ МИРЕ:")
        for k, (was, now) in sorted(behavioural.items()):
            print(f"   {k}: {was!r} -> {now!r}")
        print()

    other = {k: v for k, v in changed.items() if k not in BEHAVIOURAL}
    if other:
        print("Прочие изменения значений:")
        for k, (was, now) in sorted(other.items()):
            print(f"   {k}: {was!r} -> {now!r}")
        print()

    if removed:
        print("Переменные ИСЧЕЗЛИ бы (деплой с --env-vars-file перезаписывает набор целиком):")
        for k in removed:
            print(f"   {k} (сейчас {live[k]!r})")
        print()

    if added:
        print("Новые переменные:")
        for k in added:
            print(f"   {k} = {candidate[k]!r}")
        print()

    delta = len(changed) + len(removed) + len(added)
    if delta == 0:
        print("OK: файл совпадает с production. Деплой не изменит конфигурацию.")
        return 0

    if args.accept_changes:
        print(f"ПРИНЯТО: {delta} изменений разрешены явным --accept-changes.")
        return 0

    print(f"ОТКАЗ: {delta} изменений конфигурации не подтверждены.")
    print("Деплой остановлен. Варианты:")
    print("  1) привести файл к фактическому production (см. env.production.live.yaml);")
    print("  2) деплоить аддитивно: gcloud run services update --update-env-vars ...;")
    print("  3) если изменение намеренное — повторить с --accept-changes.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
