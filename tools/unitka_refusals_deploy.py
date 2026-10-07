#!/usr/bin/env python3
"""Юнитка WB, Phase 1A — доказанные отказы вне Orders API: развёртывание 2 новых и 4 изменённых вью wb_mart (2026-10-07).

Что делает: исполняет закрытый список операторов `CREATE OR REPLACE VIEW` из Git в порядке графа
зависимостей и перечитывает тело каждого представления из BigQuery. Больше ничего: ни таблиц, ни данных,
ни прав, ни расписаний, ни записи в книгу. Образ движка (чтение новых колонок, заметки S) — отдельный шаг.
Порядок важен: вью отдают НОВЫЕ колонки в хвосте, старый образ читает явный список колонок и их не замечает,
поэтому вью разворачиваются ПЕРВЫМИ, образ — после. HTTP-вызовы — общие функции tools/promo_canonical_deploy.py.

Источники: sql/unitka/refusals_v1.sql (2 новые вью), engine_v1_views.sql, integrity_v1.sql, reconcile_v1.sql
(оператор своего имени). Откат (--rollback): sql/unitka/refusals_v1_rollback/rollback_<OBJECT>.sql — тела
production до изменения, только для 4 изменённых вью. Две новые вью при откате остаются: после отката их никто
не читает (DROP — отдельное решение владельца, этот инструмент DROP не исполняет).

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
  python tools/unitka_refusals_deploy.py --project <P> --token-command "gcloud auth print-access-token"
  python tools/unitka_refusals_deploy.py --project <P> --token-command "…" --apply
  python tools/unitka_refusals_deploy.py --project <P> --token-command "…" --rollback [--apply]
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
LABEL = "unitka-refusals-v1-deploy"
ROLLBACK_DIR = "sql/unitka/refusals_v1_rollback"
# Порядок = граф зависимостей: доказательство → сутки → факты → целостность.
OBJECTS = [
    ("wb_mart", "V_UNITKA_REFUSAL_EVIDENCE", "sql/unitka/refusals_v1.sql"),
    ("wb_mart", "V_UNITKA_REFUSAL_DAILY", "sql/unitka/refusals_v1.sql"),
    ("wb_mart", "V_UNITKA_DAILY_FACT", "sql/unitka/engine_v1_views.sql"),
    ("wb_mart", "V_UNITKA_INTEGRITY", "sql/unitka/integrity_v1.sql"),
    ("wb_mart", "V_UNITKA_RECON_FACT", "sql/unitka/reconcile_v1.sql"),
    ("wb_mart", "V_UNITKA_RECON_INTEGRITY", "sql/unitka/reconcile_v1.sql"),
]
NEW_OBJECTS = {"V_UNITKA_REFUSAL_EVIDENCE", "V_UNITKA_REFUSAL_DAILY"}


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
    stmt = text[starts[i].start():end]
    # Оператор кончается ПЕРВОЙ «;» в конце строки: дальше идут комментарии-заголовки следующей секции, которые BigQuery
    # не хранит в теле вью (иначе перечитывание и проверка дрейфа никогда не совпадут). Комментарии внутри тела «;» в
    # конце строки не содержат — это проверяет тест (тело кончается известным хвостом оператора).
    cut = re.search(r";[ \t]*(?:\n|$)", stmt)
    stmt = (stmt[:cut.start()] if cut else stmt).strip()
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
    objs = [o for o in reversed(OBJECTS) if o[1] not in NEW_OBJECTS] if rollback else OBJECTS
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
            was, _ = d2.exists(a.project, ds, n, token)
            if n in NEW_OBJECTS:
                # новая вью: до изменения её нет; повтор после частичного развёртывания допустим, если тело = Git
                if was and live_view(a.project, ds, n, token)[0] != fwd[(ds, n)]:
                    raise SystemExit(f"{ds}.{n}: новая вью уже существует с чужим телом — отказ")
                continue
            now, _ = live_view(a.project, ds, n, token)
            allowed = [old[(ds, n)], fwd[(ds, n)]]
            if now not in allowed:
                raise SystemExit(f"{ds}.{n}: живое тело не совпадает ни с телом до изменения, ни с целевым — дрейф, отказ")
        print("дрейф: нет (живые тела = ожидаемые)")
    pending = {n for _, n, _, _ in views}            # вперёд: все объекты списка меняются в этом прогоне
    for ds, n, stmt, body in views:
        pending.discard(n)
        deps = sorted(x for x in pending if f".wb_mart.{x}`" in body) if not a.rollback else []
        earlier = sorted(x for _, x, _, _ in views[:[v[1] for v in views].index(n)] if f".wb_mart.{x}`" in body) if not a.rollback else []
        if not a.apply and earlier:
            # dry-run BigQuery видит ЖИВОЕ (ещё старое) тело зависимости, а не новое — оператор проверен подстановкой тел
            print(f"PLAN  {ds}.{n}\tdry-run пропущен: читает изменяемые в этом прогоне {', '.join(earlier)}")
            continue
        assert not deps, f"{n}: читает объект, который в порядке графа идёт позже: {deps}"
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
