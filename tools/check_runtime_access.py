#!/usr/bin/env python3
"""Доказать, что фактическая runtime identity может прочитать всё, что читает объект.

Повод — UBR-012 (2026-09-22). Новое тело wb_mart.V_CT_ACTUAL_DAILY_LIVE получило
зависимость на ozon_mart. Под правами владельца вью корректна, ворота зелёные,
сверка сходится — а плановая пересборка исполняется sa-ct-refresh, у которого
доступа к ozon_mart не было. Развёртывание сломало production, и ни одна проверка
этого не увидела, потому что все они шли не от той личности.

Инструмент читает ACL датасетов (только чтение) и сравнивает их с датасетами,
которые упоминает тело объекта. Запускать ДО первой мутации.

  check_runtime_access.py --project P --token-command "gcloud auth print-access-token"
  check_runtime_access.py ... --files sql/control_tower/ct_new.sql

Коды выхода: 0 — доступ есть, 1 — не хватает прав, 3 — ошибка выполнения.
"""
import argparse, json, re, subprocess, sys, urllib.request, urllib.error
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
REGISTRY = REPO / "quality" / "runtime_identities.json"
REF = re.compile(r"`([a-z0-9-]+)\.([a-z_][a-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)`")
READ_ROLES = {"READER", "WRITER", "OWNER", "roles/bigquery.dataViewer",
              "roles/bigquery.dataEditor", "roles/bigquery.dataOwner"}


def token(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, check=True).stdout.strip()


def dataset_readers(project, dataset, tok):
    url = f"https://www.googleapis.com/bigquery/v2/projects/{project}/datasets/{dataset}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {tok}"})
    try:
        d = json.loads(urllib.request.urlopen(req, timeout=60).read())
    except urllib.error.HTTPError as e:
        raise SystemExit(f"ОШИБКА: не прочитать ACL {dataset}: {e.read().decode()[:200]}")
    out = set()
    for a in d.get("access", []):
        if a.get("role") in READ_ROLES:
            who = a.get("userByEmail") or a.get("groupByEmail") or a.get("iamMember")
            if who:
                out.add(who)
    return out


def datasets_referenced(text, project):
    return {ds for p, ds, _ in REF.findall(text) if p == project}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--token-command", required=True)
    ap.add_argument("--files", nargs="*", help="проверить только эти файлы")
    a = ap.parse_args()

    reg = json.loads(REGISTRY.read_text(encoding="utf-8"))
    tok = token(a.token_command)
    acl_cache, problems, checked = {}, [], 0

    for c in reg["consumers"]:
        ident = c["runtime_identity"]
        files = [f for f in c["sql_files"] if not a.files or f in a.files]
        for rel in files:
            p = REPO / rel
            if not p.exists():
                continue
            for ds in sorted(datasets_referenced(p.read_text(encoding="utf-8"), a.project)):
                if ds not in acl_cache:
                    acl_cache[ds] = dataset_readers(a.project, ds, tok)
                checked += 1
                if ident not in acl_cache[ds]:
                    problems.append((ident, rel, ds))
                    print(f"FAIL  {ident.split('@')[0]}: нет доступа на чтение {ds}  ← {rel}")

    if problems:
        print(f"\nruntime-доступ: {len(problems)} нарушение(й) из {checked} проверок.")
        print("Развёртывание НЕ начинать: объект будет создан, но исполнитель его не прочитает.")
        return 1
    print(f"OK: runtime-доступ подтверждён — {checked} пар (личность × датасет), нарушений нет.")
    print("Область: только ACL датасетов. Права на уровне таблиц не рассматриваются.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:
        print(f"ОШИБКА: {e}")
        sys.exit(3)
