"""Read-only проверка отпечатка финансов витрины (fin:v2) В САМОМ BigQuery на синтетике.

Зачем. Отпечаток версии финансов (cloud/src/loaders/mart/watermark.ts) решает, пропускать ли пересборку
витрины. Ложный NOOP недопустим, поэтому свойства алгоритма проверяются не только эталонной моделью в vitest,
но и движком BigQuery: те же TO_JSON_STRING/SHA256/STRING_AGG, тот же текст SQL.

Шаблон WATERMARK_SQL_TEMPLATE — дословная копия financeWatermarkSqlFrom('{source}') из watermark.ts;
расхождение ловит cloud/test/mart_watermark.test.ts (CI). Источник подставляется синтетический
(UNNEST массива STRUCT), поэтому скрипт ничего не читает из боевых таблиц, кроме режима --live.

Только SELECT: каждый запрос проходит assert_read_only (tools/lib/bq_readonly.py), хост www.googleapis.com.
В CI доступа к BigQuery нет — запуск ручной:

    python3 tools/mart_watermark_bq_check.py --token-command "gcloud auth print-access-token"
    python3 tools/mart_watermark_bq_check.py --token-command "gcloud auth print-access-token" --live

--live дополнительно считает версию живого wb_raw.V_WB_FINANCE_SEMANTIC (~0,36 ГБ billed). Код возврата 0 —
все ожидания выполнены, 1 — нарушение (вывод показывает какое).
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# ── Дословная копия financeWatermarkSqlFrom('{source}') (cloud/src/loaders/mart/watermark.ts). ──
WATERMARK_SQL_TEMPLATE = r"""
WITH row_digests AS (
  SELECT TO_JSON_STRING(s.report_id) AS part_key,
         TO_HEX(SHA256(TO_JSON_STRING(s))) AS row_digest
  FROM {source} s
),
parts AS (
  SELECT part_key,
         COUNT(*) AS part_rows,
         TO_HEX(SHA256(STRING_AGG(row_digest, '' ORDER BY row_digest))) AS part_digest
  FROM row_digests
  GROUP BY part_key
)
SELECT CONCAT('fin:v2:', CAST(IFNULL(SUM(part_rows), 0) AS STRING), ':',
              TO_HEX(SHA256(IFNULL(STRING_AGG(
                CONCAT(part_key, '=', CAST(part_rows AS STRING), ':', part_digest), '\n' ORDER BY part_key), '')))
       ) AS watermark
FROM parts"""
# ── конец копии ──

WATERMARK_RE = re.compile(r"^fin:v2:\d+:[0-9a-f]{64}$")

COLUMNS = ("report_id STRING, rrd_id STRING, supplier_oper_name STRING, for_pay STRING, deduction STRING, "
           "_rr_date DATE, marketplace_fee_gap_rub NUMERIC")


def _lit(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, tuple) and v[0] == "DATE":
        return f"DATE '{v[1]}'"
    if isinstance(v, tuple) and v[0] == "NUMERIC":
        return f"NUMERIC '{v[1]}'"
    return "'" + str(v).replace("\\", "\\\\").replace("'", "\\'") + "'"


def row(report_id="900001", rrd_id="1", oper="Продажа", for_pay="512.34", deduction=None,
        rr_date="2026-10-06", fee="467.66"):
    return (report_id, rrd_id, oper, for_pay, deduction, ("DATE", rr_date), None if fee is None else ("NUMERIC", fee))


BASE = [
    row(rrd_id="1"),
    row(rrd_id="2", for_pay="100.00", fee="50.00"),
    row(rrd_id="3", oper="Логистика", for_pay="0", fee=None),
    row(report_id="800001", rrd_id="9", rr_date="2024-09-07"),
    row(report_id="409455520261011", rrd_id="5"),
]


def source_sql(rows: list[tuple]) -> str:
    if not rows:
        return f"(SELECT * FROM UNNEST(ARRAY<STRUCT<{COLUMNS}>>[]))"
    items = ", ".join("(" + ", ".join(_lit(v) for v in r) + ")" for r in rows)
    return f"(SELECT * FROM UNNEST(ARRAY<STRUCT<{COLUMNS}>>[{items}]))"


def _edit(i: int, **kw) -> list[tuple]:
    out = list(BASE)
    r = list(out[i])
    names = ["report_id", "rrd_id", "oper", "for_pay", "deduction", "rr_date", "fee"]
    for k, v in kw.items():
        j = names.index(k)
        r[j] = ("DATE", v) if k == "rr_date" else (("NUMERIC", v) if k == "fee" and v is not None else v)
    out[i] = tuple(r)
    return out


def _swap_for_pay() -> list[tuple]:
    out = [list(r) for r in BASE]
    out[0][3], out[1][3] = out[1][3], out[0][3]
    return [tuple(r) for r in out]


# (имя, строки, ожидание относительно BASE: "DIFF" — версия обязана измениться, "SAME" — совпасть)
SCENARIOS: list[tuple[str, list[tuple], str]] = [
    ("base_rerun", list(BASE), "SAME"),
    ("row_order_reversed", list(reversed(BASE)), "SAME"),
    ("numeric_scale_same_value_467.660", _edit(0, fee="467.660"), "SAME"),
    ("amount_for_pay_+0.01", _edit(0, for_pay="512.35"), "DIFF"),
    ("numeric_fee_+0.01", _edit(0, fee="467.67"), "DIFF"),
    ("date_+1d", _edit(1, rr_date="2026-10-07"), "DIFF"),
    ("category_change", _edit(2, oper="Хранение"), "DIFF"),
    ("remove1_add1_equal_count", BASE[1:] + [row(rrd_id="77")], "DIFF"),
    ("duplicate_row", BASE + [BASE[0]], "DIFF"),
    ("duplicate_pair_xor_trap", BASE + [BASE[0], BASE[0]], "DIFF"),
    ("swap_for_pay_two_rows", _swap_for_pay(), "DIFF"),
    ("late_correction_2024", _edit(3, for_pay="511.34"), "DIFF"),
    ("null_vs_string_null", _edit(2, deduction="null"), "DIFF"),
    ("null_vs_empty_string", _edit(2, deduction=""), "DIFF"),
    ("row_moved_to_other_report", _edit(4, report_id="900001"), "DIFF"),
    ("empty_source", [], "DIFF"),
]


def build_sql(rows: list[tuple]) -> str:
    return WATERMARK_SQL_TEMPLATE.replace("{source}", source_sql(rows))


def run(bq, live_project: str | None) -> int:
    base = bq.query(build_sql(BASE))[0]["watermark"]
    failures = 0
    print(f"{'base':38s} {base}")
    seen: dict[str, str] = {}
    for name, rows, expect in SCENARIOS:
        w = bq.query(build_sql(rows))[0]["watermark"]
        ok = bool(WATERMARK_RE.match(w or "")) and ((w == base) if expect == "SAME" else (w != base))
        if expect == "DIFF":
            # различные правки не должны давать одинаковые версии между собой
            if w in seen:
                ok = False
            seen[w] = name
        failures += 0 if ok else 1
        print(f"{name:38s} {w}  ожидание={expect}  {'OK' if ok else 'НАРУШЕНИЕ'}")
    if live_project:
        src = f"`{live_project}.wb_raw.V_WB_FINANCE_SEMANTIC`"
        w = bq.query(WATERMARK_SQL_TEMPLATE.replace("{source}", src))[0]["watermark"]
        ok = bool(WATERMARK_RE.match(w or ""))
        failures += 0 if ok else 1
        print(f"{'live V_WB_FINANCE_SEMANTIC':38s} {w}  {'OK' if ok else 'НАРУШЕНИЕ'}")
    print(f"запросов: {bq.queries_issued}, байт обработано: {bq.bytes_billed}")
    print("ИТОГ:", "PASS" if failures == 0 else f"FAIL ({failures})")
    return 0 if failures == 0 else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--project", default="project-fa311fc0-4d87-4781-986")
    ap.add_argument("--token-env", default="BQ_READONLY_ACCESS_TOKEN")
    ap.add_argument("--token-command")
    ap.add_argument("--live", action="store_true", help="ещё и версия живого V_WB_FINANCE_SEMANTIC (~0,36 ГБ)")
    args = ap.parse_args(argv)
    from lib.bq_readonly import ReadOnlyBigQuery, resolve_token
    token = resolve_token(args.token_env, args.token_command, os.environ)
    bq = ReadOnlyBigQuery(project=args.project, token=token, label_purpose="mart-watermark-check")
    return run(bq, args.project if args.live else None)


if __name__ == "__main__":
    sys.exit(main())
