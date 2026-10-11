#!/usr/bin/env python3
"""Правка начала действия состава двух наборов в evetis_ref.REF_BUNDLE_COMPONENTS (решение владельца).

Что меняется — ровно четыре значения effective_from и больше ничего:

  EVT-SET-TON-CREAM-ACNE    BC-28 (EVT-FT-ACNE-150), BC-29 (EVT-FC-ACNE-50)  2026-05-03 → 2026-05-01
  EVT-SET-ACNE-TONIC-SERUM  BC-30 (EVT-FT-ACNE-150), BC-31 (EVT-FS-ACNE-30)  2026-07-09 → 2026-07-01

Зачем: по одной единице каждого набора продано до начала интервала состава (заказ WB 2026-05-01, nm 952068582
и 2026-07-01, nm 1083392113) — у этих единиц нет себестоимости. Суммы себестоимости НЕ меняются: производная
себестоимость набора (V_BUNDLE_COGS_DERIVED → V_PRODUCT_COGS_EFFECTIVE) на продлённом участке остаётся
314.445 и 350.785 — компоненты на [новое, старое) стоят столько же, сколько на старом начале.

Режимы:
  PLAN (по умолчанию, только чтение через lib.bq_readonly): читает 4 строки и сверяет идентичность; проверяет,
    что другие строки этих наборов не пересекают новое начало; что у каждого компонента одна строка
    REF_SKU_COGS_HISTORY покрывает [новое, старое] (цена на продлённом участке = цене на старом начале);
    исполняет живое тело V_BUNDLE_COGS_DERIVED с подставленными исправленными строками и печатает интервалы
    до/после; считает затронутые продажи (когорта финотчёта WB × REF_SKU_CHANNEL_MAP, дата заказа в
    [новое, старое)) — ровно 1 единица на набор, иначе STOP. Печатает план (JSON) с before-image 4 строк и
    sha256-отпечатком.
  APPLY (--apply --plan-fingerprint <sha> --manifest-dir <D>): чистое дерево, HEAD = свежий origin/main,
    повтор PLAN с тем же отпечатком; манифест (план + before-image) пишется ДО записи; одна транзакция
    BigQuery из 4 охраняемых UPDATE, каждый обязан изменить ровно 1 строку, итог ровно 4 — иначе RAISE и
    ROLLBACK; затем перечитывание строк и производной себестоимости в манифест.
  ROLLBACK (--rollback --manifest-dir <D>): без --apply — только проверка, что таблица в состоянии «после»;
    с --apply и теми же защитами — обратная транзакция (effective_from из before-image) и перечитывание.

usage:
  python tools/cogs_bundle_start_fix.py --project <P> --token-command "gcloud auth print-access-token"
  python tools/cogs_bundle_start_fix.py --project <P> --token-command "…" --apply --plan-fingerprint <sha> --manifest-dir <D>
  python tools/cogs_bundle_start_fix.py --project <P> --token-command "…" --rollback --manifest-dir <D> [--apply]
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.bq_readonly import ReadOnlyBigQuery, resolve_token  # noqa: E402

TOOL = "cogs_bundle_start_fix/v1"
ROOT = Path(__file__).resolve().parent.parent
PROJECT = "project-fa311fc0-4d87-4781-986"
HOST = "https://www.googleapis.com/bigquery/v2"
TABLE = f"`{PROJECT}.evetis_ref.REF_BUNDLE_COMPONENTS`"
COGS_HISTORY = f"`{PROJECT}.evetis_ref.REF_SKU_COGS_HISTORY`"
DERIVED_VIEW = f"`{PROJECT}.evetis_ref.V_BUNDLE_COGS_DERIVED`"
EFFECTIVE_VIEW = f"`{PROJECT}.evetis_ref.V_PRODUCT_COGS_EFFECTIVE`"
COHORT = f"`{PROJECT}.wb_mart.V_WB_STORE_FINANCE_COHORT_DAILY`"
CHANNEL_MAP = f"`{PROJECT}.evetis_ref.REF_SKU_CHANNEL_MAP`"
OPEN_END = "9999-12-31"
FP_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class Correction:
    bundle_component_id: str
    bundle_internal_sku: str
    component_internal_sku: str
    component_qty: int
    old_from: str
    new_from: str


@dataclass(frozen=True)
class Bundle:
    internal_sku: str
    nm_id: int
    old_from: str
    new_from: str
    cogs_rub: str          # производная себестоимость на продлённом участке (не меняется)
    affected_units: int    # единицы с датой заказа в [new_from, old_from)


# Закрытый список: ровно то, что одобрил владелец. Ничего шире.
CORRECTIONS: tuple[Correction, ...] = (
    Correction("BC-28", "EVT-SET-TON-CREAM-ACNE", "EVT-FT-ACNE-150", 1, "2026-05-03", "2026-05-01"),
    Correction("BC-29", "EVT-SET-TON-CREAM-ACNE", "EVT-FC-ACNE-50", 1, "2026-05-03", "2026-05-01"),
    Correction("BC-30", "EVT-SET-ACNE-TONIC-SERUM", "EVT-FT-ACNE-150", 1, "2026-07-09", "2026-07-01"),
    Correction("BC-31", "EVT-SET-ACNE-TONIC-SERUM", "EVT-FS-ACNE-30", 1, "2026-07-09", "2026-07-01"),
)
BUNDLES: tuple[Bundle, ...] = (
    Bundle("EVT-SET-TON-CREAM-ACNE", 952068582, "2026-05-03", "2026-05-01", "314.445", 1),
    Bundle("EVT-SET-ACNE-TONIC-SERUM", 1083392113, "2026-07-09", "2026-07-01", "350.785", 1),
)


class PlanRefused(Exception):
    """План не прошёл проверку — запись невозможна."""


# ───────────────────────────── чистая логика (офлайн-тестируемая) ─────────────────────────────

def _check_spec(corrections=CORRECTIONS, bundles=BUNDLES) -> None:
    by_bundle = {b.internal_sku: b for b in bundles}
    if len(by_bundle) != len(bundles) or len({c.bundle_component_id for c in corrections}) != len(corrections):
        raise PlanRefused("спецификация: повтор набора или bundle_component_id")
    for c in corrections:
        b = by_bundle.get(c.bundle_internal_sku)
        if b is None or (c.old_from, c.new_from) != (b.old_from, b.new_from) or not c.new_from < c.old_from:
            raise PlanRefused(f"спецификация: {c.bundle_component_id} не согласована с набором")


def _sql_str(v: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.\-]+", v):
        raise PlanRefused(f"недопустимое значение для SQL-литерала: {v!r}")
    return f"'{v}'"


def _sql_date(v: str) -> str:
    dt.date.fromisoformat(v)
    return f"DATE '{v}'"


def _end(v) -> str:
    return v or OPEN_END


def check_identity(rows: list[dict], corrections=CORRECTIONS, bundles=BUNDLES) -> tuple[list[dict], list[dict]]:
    """rows — все строки REF_BUNDLE_COMPONENTS этих наборов и с этими id. Возвращает (целевые, прочие)."""
    _check_spec(corrections, bundles)
    ids = {c.bundle_component_id for c in corrections}
    targets, others = [], []
    for r in rows:
        (targets if r.get("bundle_component_id") in ids else others).append(r)
    by_id: dict[str, list[dict]] = {}
    for r in targets:
        by_id.setdefault(r["bundle_component_id"], []).append(r)
    out = []
    for c in corrections:
        found = by_id.get(c.bundle_component_id, [])
        if len(found) != 1:
            raise PlanRefused(f"{c.bundle_component_id}: ожидалась ровно 1 строка, найдено {len(found)}")
        r = found[0]
        want = {"bundle_internal_sku": c.bundle_internal_sku, "component_internal_sku": c.component_internal_sku,
                "component_qty": c.component_qty, "effective_from": c.old_from, "effective_to": None}
        got = {k: r.get(k) for k in want}
        if got["component_qty"] is not None:
            got["component_qty"] = int(got["component_qty"])
        if got != want:
            raise PlanRefused(f"{c.bundle_component_id}: идентичность не совпала: ожидалось {want}, в таблице {got}")
        out.append(r)
    return out, others


def check_no_overlap(others: list[dict], bundles=BUNDLES) -> None:
    """Никакая другая строка этих наборов не пересекает [новое начало, ∞): иначе сдвиг создаст наложение составов."""
    names = {b.internal_sku: b for b in bundles}
    for r in others:
        b = names.get(r.get("bundle_internal_sku"))
        if b is None:
            continue
        if _end(r.get("effective_to")) >= b.new_from:
            raise PlanRefused(
                f"{b.internal_sku}: другая строка {r.get('bundle_component_id')} "
                f"[{r.get('effective_from')}, {r.get('effective_to')}] пересекает новое начало {b.new_from}")


def check_component_costs(history: list[dict], corrections=CORRECTIONS) -> list[dict]:
    """Ровно одна строка REF_SKU_COGS_HISTORY компонента покрывает [новое, старое] — цена не меняется.

    Именно эту таблицу читает V_BUNDLE_COGS_DERIVED (V_PRODUCT_COGS_EFFECTIVE для единиц — её же проекция).
    """
    evidence = []
    for c in corrections:
        rows = [h for h in history if h.get("internal_sku") == c.component_internal_sku
                and h["effective_from"] <= c.old_from and _end(h.get("effective_to")) >= c.new_from]
        if len(rows) != 1:
            raise PlanRefused(
                f"{c.bundle_component_id}/{c.component_internal_sku}: на [{c.new_from}, {c.old_from}] "
                f"{len(rows)} строк себестоимости, нужна ровно 1 (цена на продлённом участке изменилась или её нет)")
        h = rows[0]
        if not (h["effective_from"] <= c.new_from and _end(h.get("effective_to")) >= c.old_from):
            raise PlanRefused(f"{c.bundle_component_id}/{c.component_internal_sku}: строка {h.get('cogs_history_id')} "
                              f"не покрывает [{c.new_from}, {c.old_from}] целиком")
        evidence.append({"bundle_component_id": c.bundle_component_id, "component_internal_sku": c.component_internal_sku,
                         "cogs_history_id": h.get("cogs_history_id"), "effective_from": h["effective_from"],
                         "effective_to": h.get("effective_to"), "product_cogs_rub": str(Decimal(str(h["product_cogs_rub"])))})
    return evidence


def corrected_source_sql(corrections=CORRECTIONS) -> str:
    """Подзапрос «REF_BUNDLE_COMPONENTS после правки» — для read-only расчёта after-image."""
    whens = "\n".join(
        f"      WHEN bundle_component_id = {_sql_str(c.bundle_component_id)}"
        f" AND bundle_internal_sku = {_sql_str(c.bundle_internal_sku)}"
        f" AND effective_from = {_sql_date(c.old_from)} THEN {_sql_date(c.new_from)}"
        for c in corrections)
    return (f"(SELECT * REPLACE (CASE\n{whens}\n      ELSE effective_from END AS effective_from)\n"
            f"   FROM {TABLE})")


def substitute_body(body: str, corrections=CORRECTIONS) -> str:
    """Живое тело V_BUNDLE_COGS_DERIVED, в котором единственная ссылка на таблицу заменена исправленными строками."""
    if body.count(TABLE) != 1 or len(re.findall(r"REF_BUNDLE_COMPONENTS", body)) != 1:
        raise PlanRefused("тело V_BUNDLE_COGS_DERIVED: ожидалась ровно одна ссылка на REF_BUNDLE_COMPONENTS")
    return body.replace(TABLE, corrected_source_sql(corrections))


def _norm_interval(r: dict) -> dict:
    return {"internal_sku": r["internal_sku"], "effective_from": r["effective_from"],
            "effective_to": r.get("effective_to"), "product_cogs_rub": str(Decimal(str(r["product_cogs_rub"]))),
            "component_count": int(r["component_count"]) if r.get("component_count") is not None else None}


def _sorted(rows):
    return sorted(rows, key=lambda r: (r["internal_sku"], r["effective_from"]))


def check_derived(before: list[dict], after: list[dict], effective: list[dict], bundles=BUNDLES) -> dict:
    """after = before, где у интервала, начинавшегося в старом начале, начало сдвинуто на новое; суммы те же."""
    before = _sorted(_norm_interval(r) for r in before)
    after = _sorted(_norm_interval(r) for r in after)
    names = {b.internal_sku for b in bundles}
    untouched_b = [r for r in before if r["internal_sku"] not in names]
    untouched_a = [r for r in after if r["internal_sku"] not in names]
    if untouched_b != untouched_a:
        raise PlanRefused("after-image меняет производную себестоимость других наборов")
    eff = _sorted({"internal_sku": r["internal_sku"], "effective_from": r["effective_from"],
                   "effective_to": r.get("effective_to"),
                   "product_cogs_rub": str(Decimal(str(r["product_cogs_rub"])))} for r in effective)
    report = {}
    for b in bundles:
        bb = [r for r in before if r["internal_sku"] == b.internal_sku]
        aa = [r for r in after if r["internal_sku"] == b.internal_sku]
        if any(r["effective_from"] <= b.new_from <= _end(r["effective_to"]) for r in bb):
            raise PlanRefused(f"{b.internal_sku}: {b.new_from} уже покрыт производной себестоимостью — править нечего")
        starts = [r for r in bb if r["effective_from"] == b.old_from]
        if len(starts) != 1:
            raise PlanRefused(f"{b.internal_sku}: до правки нет единственного интервала с началом {b.old_from}")
        if Decimal(starts[0]["product_cogs_rub"]) != Decimal(b.cogs_rub):
            raise PlanRefused(f"{b.internal_sku}: себестоимость на {b.old_from} = {starts[0]['product_cogs_rub']}, "
                              f"ожидалась {b.cogs_rub}")
        expected = _sorted({**r, "effective_from": b.new_from} if r["effective_from"] == b.old_from else r for r in bb)
        if aa != expected:
            raise PlanRefused(f"{b.internal_sku}: after-image {aa} не равен ожидаемому {expected}")
        eff_b = [{k: r[k] for k in ("internal_sku", "effective_from", "effective_to", "product_cogs_rub")} for r in bb]
        if [r for r in eff if r["internal_sku"] == b.internal_sku] != eff_b:
            raise PlanRefused(f"{b.internal_sku}: V_PRODUCT_COGS_EFFECTIVE (DERIVED_BUNDLE) расходится с V_BUNDLE_COGS_DERIVED")
        report[b.internal_sku] = {"before": bb, "after": aa}
    digest = hashlib.sha256(json.dumps(untouched_b, sort_keys=True).encode()).hexdigest()
    return {"bundles": report, "untouched_rows": len(untouched_b), "untouched_sha256": digest}


def check_sales(sales: list[dict], bundles=BUNDLES) -> dict:
    """Продано (когорта финотчёта WB) с датой заказа в [новое, старое) — ровно ожидаемое число единиц на набор."""
    out = {}
    for b in bundles:
        rows = [r for r in sales if r["internal_sku"] == b.internal_sku]
        bad_nm = sorted({int(r["nm_id"]) for r in rows} - {b.nm_id})
        if bad_nm:
            raise PlanRefused(f"{b.internal_sku}: продажи по чужим nm {bad_nm}")
        if any(not (b.new_from <= r["order_date_msk"] < b.old_from) for r in rows):
            raise PlanRefused(f"{b.internal_sku}: строка продаж вне [{b.new_from}, {b.old_from})")
        units = sum((Decimal(str(r["sold_qty"])) for r in rows), Decimal(0))
        if units != b.affected_units:
            raise PlanRefused(f"{b.internal_sku}: затронуто {units} ед. в [{b.new_from}, {b.old_from}), "
                              f"ожидалось ровно {b.affected_units} — STOP")
        out[b.internal_sku] = {"nm_id": b.nm_id, "units": str(units),
                               "rows": [{"order_date_msk": r["order_date_msk"], "nm_id": int(r["nm_id"]),
                                         "sold_qty": str(Decimal(str(r["sold_qty"])))}
                                        for r in sorted(rows, key=lambda r: r["order_date_msk"])]}
    return out


def transaction_sql(direction: str, corrections=CORRECTIONS, project: str = PROJECT) -> str:
    """Одна транзакция: 4 охраняемых UPDATE, каждый ровно на 1 строку, итог ровно 4 до COMMIT; иначе ROLLBACK."""
    if project != PROJECT:
        raise PlanRefused(f"проект {project!r} не совпадает с {PROJECT!r}")
    if direction not in ("apply", "rollback"):
        raise PlanRefused(f"направление {direction!r}")
    _check_spec(corrections)
    lines = [f"-- {TOOL} {direction}", "DECLARE changed INT64 DEFAULT 0;", "DECLARE n INT64;", "BEGIN",
             "  BEGIN TRANSACTION;"]
    for c in corrections:
        src, dst = (c.old_from, c.new_from) if direction == "apply" else (c.new_from, c.old_from)
        lines += [
            f"  UPDATE {TABLE}",
            f"     SET effective_from = {_sql_date(dst)}",
            f"   WHERE bundle_component_id = {_sql_str(c.bundle_component_id)}",
            f"     AND bundle_internal_sku = {_sql_str(c.bundle_internal_sku)}",
            f"     AND component_internal_sku = {_sql_str(c.component_internal_sku)}",
            f"     AND component_qty = {int(c.component_qty)}",
            f"     AND effective_from = {_sql_date(src)}",
            "     AND effective_to IS NULL;",
            "  SET n = @@row_count;",
            f"  IF n IS NULL OR n != 1 THEN RAISE USING MESSAGE = '{c.bundle_component_id}: expected exactly 1 row';"
            " END IF;",
            "  SET changed = changed + n;",
        ]
    lines += [
        f"  IF changed != {len(corrections)} THEN RAISE USING MESSAGE = 'expected exactly {len(corrections)} rows';"
        " END IF;",
        "  COMMIT TRANSACTION;",
        "EXCEPTION WHEN ERROR THEN",
        "  ROLLBACK TRANSACTION;",
        "  RAISE USING MESSAGE = CONCAT('rolled back: ', @@error.message);",
        "END;",
    ]
    return "\n".join(lines)


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def fingerprint(core: dict) -> str:
    return hashlib.sha256(canonical(core).encode("utf-8")).hexdigest()


def after_rows(before_image: list[dict], corrections=CORRECTIONS) -> list[dict]:
    by_id = {c.bundle_component_id: c for c in corrections}
    out = []
    for r in before_image:
        x = copy.deepcopy(r)
        x["effective_from"] = by_id[r["bundle_component_id"]].new_from
        out.append(x)
    return out


def diff_rows(current: list[dict], expected: list[dict]) -> list[str]:
    cur = {r["bundle_component_id"]: r for r in current}
    msgs = []
    for e in expected:
        c = cur.get(e["bundle_component_id"])
        if c is None:
            msgs.append(f"{e['bundle_component_id']}: строки нет")
        elif canonical(c) != canonical(e):
            keys = sorted(k for k in set(c) | set(e) if c.get(k) != e.get(k))
            msgs.append(f"{e['bundle_component_id']}: расходятся {keys}")
    if len(cur) != len(expected):
        msgs.append(f"строк {len(cur)}, ожидалось {len(expected)}")
    return msgs


def build_plan(src) -> dict:
    """src — источник чтения (LiveSource или офлайн-фикстура тестов)."""
    rows = src.bundle_rows()
    targets, others = check_identity(rows)
    check_no_overlap(others)
    components = check_component_costs(src.cogs_history())
    body = src.derived_view_body()
    derived = check_derived(src.derived_rows(None), src.derived_rows(substitute_body(body)),
                            src.effective_bundle_rows())
    sales = check_sales(src.affected_sales())
    order = {c.bundle_component_id: i for i, c in enumerate(CORRECTIONS)}
    core = {
        "tool": TOOL,
        "project": PROJECT,
        "table": TABLE.strip("`"),
        "corrections": [asdict(c) for c in CORRECTIONS],
        "bundles": [asdict(b) for b in BUNDLES],
        "before_image": sorted(targets, key=lambda r: order[r["bundle_component_id"]]),
        "other_bundle_rows": sorted(others, key=lambda r: str(r.get("bundle_component_id"))),
        "component_cogs": components,
        "derived_view_body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        "derived": derived,
        "affected_sales": sales,
        "apply_sql_sha256": hashlib.sha256(transaction_sql("apply").encode()).hexdigest(),
        "rollback_sql_sha256": hashlib.sha256(transaction_sql("rollback").encode()).hexdigest(),
    }
    return {"fingerprint": fingerprint(core), "plan": core}


# ───────────────────────────── чтение BigQuery (только SELECT) ─────────────────────────────

def _in_list(values) -> str:
    return ", ".join(_sql_str(v) for v in sorted(set(values)))


class LiveSource:
    def __init__(self, bq: ReadOnlyBigQuery):
        self.bq = bq

    def bundle_rows(self) -> list[dict]:
        sql = (f"SELECT TO_JSON_STRING(t) AS j FROM {TABLE} t\n"
               f"WHERE bundle_internal_sku IN ({_in_list(b.internal_sku for b in BUNDLES)})\n"
               f"   OR bundle_component_id IN ({_in_list(c.bundle_component_id for c in CORRECTIONS)})")
        return [json.loads(r["j"]) for r in self.bq.query(sql)]

    def target_rows(self) -> list[dict]:
        sql = (f"SELECT TO_JSON_STRING(t) AS j FROM {TABLE} t\n"
               f"WHERE bundle_component_id IN ({_in_list(c.bundle_component_id for c in CORRECTIONS)})")
        return [json.loads(r["j"]) for r in self.bq.query(sql)]

    def cogs_history(self) -> list[dict]:
        sql = (f"SELECT cogs_history_id, internal_sku, CAST(effective_from AS STRING) AS effective_from,\n"
               f"       CAST(effective_to AS STRING) AS effective_to, CAST(product_cogs_rub AS STRING) AS product_cogs_rub\n"
               f"FROM {COGS_HISTORY}\n"
               f"WHERE internal_sku IN ({_in_list(c.component_internal_sku for c in CORRECTIONS)})")
        return self.bq.query(sql)

    def derived_view_body(self) -> str:
        rows = self.bq.query(f"SELECT view_definition FROM `{PROJECT}.evetis_ref.INFORMATION_SCHEMA.VIEWS`\n"
                             f"WHERE table_name = 'V_BUNDLE_COGS_DERIVED'")
        if len(rows) != 1 or not rows[0]["view_definition"]:
            raise PlanRefused("V_BUNDLE_COGS_DERIVED: тело не прочитано")
        return rows[0]["view_definition"]

    def derived_rows(self, body: str | None) -> list[dict]:
        source = DERIVED_VIEW if body is None else f"(\n{body}\n)"
        sql = (f"SELECT internal_sku, CAST(effective_from AS STRING) AS effective_from,\n"
               f"       CAST(effective_to AS STRING) AS effective_to,\n"
               f"       CAST(product_cogs_rub AS STRING) AS product_cogs_rub, component_count\n"
               f"FROM {source}")
        return self.bq.query(sql)

    def effective_bundle_rows(self) -> list[dict]:
        sql = (f"SELECT internal_sku, CAST(effective_from AS STRING) AS effective_from,\n"
               f"       CAST(effective_to AS STRING) AS effective_to, CAST(product_cogs_rub AS STRING) AS product_cogs_rub\n"
               f"FROM {EFFECTIVE_VIEW}\n"
               f"WHERE resolver_lane = 'DERIVED_BUNDLE' AND internal_sku IN ({_in_list(b.internal_sku for b in BUNDLES)})")
        return self.bq.query(sql)

    def affected_sales(self) -> list[dict]:
        cond = "\n   OR ".join(
            f"(m.internal_sku = {_sql_str(b.internal_sku)} AND c.order_date_msk >= {_sql_date(b.new_from)}"
            f" AND c.order_date_msk < {_sql_date(b.old_from)})" for b in BUNDLES)
        sql = (f"SELECT m.internal_sku, c.nm_id, CAST(c.order_date_msk AS STRING) AS order_date_msk,\n"
               f"       CAST(SUM(c.sold_qty) AS STRING) AS sold_qty\n"
               f"FROM {COHORT} c\n"
               f"JOIN {CHANNEL_MAP} m\n"
               f"  ON m.marketplace = 'WB' AND SAFE_CAST(m.marketplace_sku AS INT64) = c.nm_id\n"
               f" AND c.order_date_msk >= m.valid_from AND (m.valid_to IS NULL OR c.order_date_msk <= m.valid_to)\n"
               f"WHERE {cond}\n"
               f"GROUP BY 1, 2, 3")
        return self.bq.query(sql)


# ───────────────────────────── запись (только APPLY / ROLLBACK --apply) ─────────────────────────────

def _call(method: str, url: str, token: str, body: dict | None = None) -> tuple[int, dict]:
    import urllib.error
    import urllib.request
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except json.JSONDecodeError:
            return e.code, {}


def run_transaction(sql: str, token: str, fp: str) -> dict:
    body = {"configuration": {"query": {"query": sql, "useLegacySql": False},
                              "labels": {"purpose": "cogs-bundle-start-fix", "plan_fp": fp[:16]}},
            "jobReference": {"projectId": PROJECT, "location": "EU"}}
    code, job = _call("POST", f"{HOST}/projects/{PROJECT}/jobs", token, body)
    if code != 200:
        raise SystemExit(f"jobs.insert HTTP {code}: {(job.get('error') or {}).get('message', '')[:300]}")
    jid = job["jobReference"]["jobId"]
    for _ in range(300):
        if job.get("status", {}).get("state") == "DONE":
            break
        time.sleep(1)
        _, job = _call("GET", f"{HOST}/projects/{PROJECT}/jobs/{jid}?location=EU", token)
    if job.get("status", {}).get("state") != "DONE":
        raise SystemExit(f"задание {jid} не завершилось — проверить вручную, повтор не запускать")
    err = job.get("status", {}).get("errorResult")
    if err:
        raise SystemExit(f"задание {jid} завершилось ошибкой (транзакция откатана): {err.get('message', '')[:300]}")
    return job


def git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True, cwd=ROOT).stdout.strip()


def require_merged_head() -> str:
    git("fetch", "--quiet", "origin", "main")
    if git("status", "--porcelain"):
        raise SystemExit("--apply: рабочее дерево не чистое")
    head = git("rev-parse", "HEAD")
    if head != git("rev-parse", "origin/main"):
        raise SystemExit("--apply: HEAD не совпадает с origin/main — исполняется только слитый код")
    return head


def manifest_path(raw: str | None) -> Path:
    if not raw:
        raise SystemExit("нужен --manifest-dir")
    p = Path(raw).resolve()
    if p == ROOT or ROOT in p.parents:
        raise SystemExit("--manifest-dir внутри репозитория: манифест загрязнит дерево и заблокирует откат")
    return p


def write_json(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def print_plan(result: dict) -> None:
    core = result["plan"]
    print("before-image (4 строки):")
    for r in core["before_image"]:
        print("  " + canonical(r))
    print(f"прочие строки этих наборов: {len(core['other_bundle_rows'])}")
    print("себестоимость компонентов на [новое, старое]:")
    for e in core["component_cogs"]:
        print(f"  {e['bundle_component_id']} {e['component_internal_sku']}: {e['cogs_history_id']} "
              f"[{e['effective_from']}, {e['effective_to'] or '∞'}] = {e['product_cogs_rub']}")
    print("V_BUNDLE_COGS_DERIVED (= V_PRODUCT_COGS_EFFECTIVE, DERIVED_BUNDLE):")
    for sku, d in core["derived"]["bundles"].items():
        for side in ("before", "after"):
            for r in d[side]:
                print(f"  {side:6} {sku} [{r['effective_from']}, {r['effective_to'] or '∞'}] "
                      f"= {r['product_cogs_rub']} ({r['component_count']} комп.)")
    print(f"прочие наборы: {core['derived']['untouched_rows']} интервалов без изменений "
          f"(sha256 {core['derived']['untouched_sha256'][:16]}…)")
    print("затронутые продажи (когорта финотчёта WB, дата заказа в [новое, старое)):")
    for sku, s in core["affected_sales"].items():
        print(f"  {sku} nm {s['nm_id']}: {s['units']} ед. {[(r['order_date_msk'], r['sold_qty']) for r in s['rows']]}")
    print(f"FINGERPRINT {result['fingerprint']}")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--project", required=True)
    p.add_argument("--token-command")
    p.add_argument("--token-env", default="BQ_DEPLOY_ACCESS_TOKEN")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--rollback", action="store_true")
    p.add_argument("--plan-fingerprint")
    p.add_argument("--manifest-dir")
    p.add_argument("--plan-out", help="записать план (JSON) в файл")
    a = p.parse_args(argv)
    if a.project != PROJECT:
        raise SystemExit(f"проект {a.project!r} не совпадает с {PROJECT!r}")
    _check_spec()
    if a.apply and not a.rollback and not (a.plan_fingerprint and FP_RE.match(a.plan_fingerprint)):
        raise SystemExit("--apply: нужен --plan-fingerprint (sha256 из PLAN)")
    manifest = manifest_path(a.manifest_dir) if (a.apply or a.rollback) else None
    if a.rollback:
        return rollback(a, manifest)
    head = require_merged_head() if a.apply else git("rev-parse", "HEAD")
    if a.apply and manifest.exists() and any(manifest.iterdir()):
        raise SystemExit(f"{manifest}: каталог манифеста не пуст — отказ перезаписи")
    token = resolve_token(a.token_env, a.token_command, os.environ)
    bq = ReadOnlyBigQuery(project=PROJECT, token=token, label_purpose="cogs-bundle-start-fix-plan")
    src = LiveSource(bq)
    print(f"git HEAD {head} · режим {'APPLY' if a.apply else 'PLAN'}")
    try:
        result = build_plan(src)
    except PlanRefused as e:
        print(f"STOP: {e}")
        return 2
    print_plan(result)
    if a.plan_out:
        write_json(Path(a.plan_out), result)
    if not a.apply:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        print(f"PLAN OK · запросов {bq.queries_issued} · байт {bq.bytes_billed} · записи не было")
        return 0
    if result["fingerprint"] != a.plan_fingerprint:
        raise SystemExit(f"отпечаток плана {result['fingerprint']} ≠ --plan-fingerprint — данные изменились, отказ")
    manifest.mkdir(parents=True, exist_ok=True)
    sql = transaction_sql("apply")
    write_json(manifest / "plan.json", {**result, "git_head": head, "apply_sql": sql,
                                        "rollback_sql": transaction_sql("rollback"),
                                        "written_at": dt.datetime.now(dt.timezone.utc).isoformat()})
    write_json(manifest / "before_image.json", result["plan"]["before_image"])
    job = run_transaction(sql, token, result["fingerprint"])
    jid = job["jobReference"]["jobId"]
    print(f"DONE транзакция {jid}")
    readback = verify_state(src, after_rows(result["plan"]["before_image"]),
                            {s: d["after"] for s, d in result["plan"]["derived"]["bundles"].items()})
    write_json(manifest / "readback.json", {"job_id": jid, **readback})
    print(f"readback {'MATCH' if readback['ok'] else 'MISMATCH'} → {manifest}")
    return 0 if readback["ok"] else 3


def verify_state(src, expected_rows: list[dict], expected_derived: dict) -> dict:
    rows = src.target_rows()
    row_diff = diff_rows(rows, expected_rows)
    live = _sorted(_norm_interval(r) for r in src.derived_rows(None) if r["internal_sku"] in expected_derived)
    want = _sorted(r for v in expected_derived.values() for r in v)
    return {"ok": not row_diff and live == want, "row_diff": row_diff, "rows": rows,
            "derived_live": live, "derived_expected": want}


def rollback(a, manifest: Path) -> int:
    stored = json.loads((manifest / "plan.json").read_text(encoding="utf-8"))
    if fingerprint(stored["plan"]) != stored["fingerprint"]:
        raise SystemExit("plan.json: отпечаток не совпадает с содержимым — манифест повреждён")
    if stored["plan"]["project"] != PROJECT or [Correction(**c) for c in stored["plan"]["corrections"]] != list(CORRECTIONS):
        raise SystemExit("plan.json: другой проект или другой список правок")
    if (manifest / "rollback_readback.json").exists():
        raise SystemExit("откат уже выполнялся по этому манифесту")
    before = stored["plan"]["before_image"]
    head = require_merged_head() if a.apply else git("rev-parse", "HEAD")
    token = resolve_token(a.token_env, a.token_command, os.environ)
    src = LiveSource(ReadOnlyBigQuery(project=PROJECT, token=token, label_purpose="cogs-bundle-start-fix-plan"))
    print(f"git HEAD {head} · режим {'ROLLBACK APPLY' if a.apply else 'ROLLBACK PLAN'} · план {stored['fingerprint']}")
    drift = diff_rows(src.target_rows(), after_rows(before))
    if drift:
        raise SystemExit(f"таблица не в состоянии «после правки»: {drift} — откат отклонён")
    print("состояние «после правки» подтверждено; откат вернёт effective_from из before-image:")
    for r in before:
        print("  " + canonical(r))
    if not a.apply:
        return 0
    job = run_transaction(transaction_sql("rollback"), token, stored["fingerprint"])
    jid = job["jobReference"]["jobId"]
    readback = verify_state(src, before, {s: d["before"] for s, d in stored["plan"]["derived"]["bundles"].items()})
    write_json(manifest / "rollback_readback.json", {"job_id": jid, **readback})
    print(f"DONE откат {jid} · readback {'MATCH' if readback['ok'] else 'MISMATCH'}")
    return 0 if readback["ok"] else 3


if __name__ == "__main__":
    sys.exit(main())
