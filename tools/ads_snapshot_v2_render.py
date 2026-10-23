#!/usr/bin/env python3
"""Рендер сверки снапшот-слоя расходов v2 (sql/raw/adv_costs_snapshot_v2_validation.sql).

Подставляет вместо плейсхолдеров либо живые вью, либо тела вью из файлов репозитория —
так один и тот же SELECT проверяет v2 ДО развёртывания (без DDL) и подтверждает ПОСЛЕ.
Только чтение файлов; результат печатается в stdout.

  python3 tools/ads_snapshot_v2_render.py pre  Q1   # NEW = тело v2, OLD = живые вью
  python3 tools/ads_snapshot_v2_render.py post Q1   # NEW = живые вью, OLD = тело v1
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
V1 = REPO / "sql/raw/adv_costs_snapshot_v1.sql"
V2 = REPO / "sql/raw/adv_costs_snapshot_v2_utc_edge.sql"
CHECKS = REPO / "sql/raw/adv_costs_snapshot_v2_validation.sql"
PROJ = "project-fa311fc0-4d87-4781-986"
LIVE_SNAP = f"`{PROJ}.wb_raw.V_ADV_COSTS_SNAPSHOT`"
LIVE_COV = f"`{PROJ}.wb_raw.V_ADV_COSTS_DAY_COVERAGE`"


def strip_comments(sql: str) -> str:
    return re.sub(r"--[^\n]*", "", sql)


def view_body(path: Path, view: str) -> str:
    sql = strip_comments(path.read_text(encoding="utf-8"))
    m = re.search(r"CREATE OR REPLACE VIEW `[^`]*\." + view + r"` AS\s*(.*?);\s*(?:\n|$)", sql, re.S)
    if not m:
        raise SystemExit(f"{view} не найдена в {path}")
    return "(" + m.group(1).strip() + ")"


def check_sql(name: str) -> str:
    text = CHECKS.read_text(encoding="utf-8")
    parts = re.split(r"(?m)^-- @check ", text)
    for p in parts[1:]:
        if p.startswith(name + " "):
            body = p.split("\n", 1)[1]
            return strip_comments(body).strip().rstrip(";")
    raise SystemExit(f"проверка {name} не найдена")


def render(mode: str, name: str) -> str:
    if mode == "pre":
        repl = {"@@SNAP_NEW@@": view_body(V2, "V_ADV_COSTS_SNAPSHOT"), "@@COV_NEW@@": view_body(V2, "V_ADV_COSTS_DAY_COVERAGE"),
                "@@SNAP_OLD@@": LIVE_SNAP, "@@COV_OLD@@": LIVE_COV}
    elif mode == "post":
        repl = {"@@SNAP_NEW@@": LIVE_SNAP, "@@COV_NEW@@": LIVE_COV,
                "@@SNAP_OLD@@": view_body(V1, "V_ADV_COSTS_SNAPSHOT"), "@@COV_OLD@@": view_body(V1, "V_ADV_COSTS_DAY_COVERAGE")}
    else:
        raise SystemExit("режим: pre | post")
    sql = check_sql(name)
    for k, v in repl.items():
        sql = sql.replace(k, v)
    return sql


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    print(render(sys.argv[1], sys.argv[2]))
