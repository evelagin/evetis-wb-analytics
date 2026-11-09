"""Store P&L WB · «Корректировка эквайринга» в API-слое — исполнение настоящих вью на SQLite-фикстурах.

Карта операций доказывает «Корректировку эквайринга» только для LEGACY (2024-09…2025-01): DIRECT_SKU, for_pay,
зеркальный acquiring_fee не читается. В API-слое WB прислал ту же операцию как возврат части эквайринга продажи:
forPay > 0, acquiringFee = −forPay, docType «Продажа», srid продажи, количество, база и прочие деньги = 0.
Правило: вне периода карты строка этой операции — DIRECT_SKU ТОЛЬКО в этой форме и только из Finance API
(source_layer DAILY/WEEKLY), а «Продажа» того же srid должна быть того же nm (признак acq_api_form, одинаковый
текст в трёх вью); любая иная форма и любая другая операция — PENDING_CLASSIFICATION, как раньше.

Из sql/unitka/store_pnl_v1.sql берутся тексты V_WB_STORE_FINANCE_COHORT_DAILY, V_WB_FINANCE_NEW_OPERATIONS,
V_WB_STORE_FINANCE_COVERAGE и строки карты V_WB_FINANCE_OPERATION_MAP. Подстановки (только то, чего нет в SQLite):
идентификаторы таблиц; DATE(SAFE.TIMESTAMP(order_dt), 'Europe/Moscow') → DATE(order_dt), SAFE.TIMESTAMP(order_dt)
→ order_dt (фикстуры — сутки МСК); FORMAT_DATE / DATE_TRUNC — функции теста. Суммы синтетические.
"""
from pathlib import Path
import json
import re
import sqlite3

import sqlglot
from sqlglot import exp

ROOT = Path(__file__).resolve().parents[2]
SQL = (ROOT / "sql" / "unitka" / "store_pnl_v1.sql").read_text(encoding="utf-8")
P = "project-fa311fc0-4d87-4781-986"
OP = "Корректировка эквайринга"
CHANGED = ("V_WB_STORE_FINANCE_COHORT_DAILY", "V_WB_FINANCE_NEW_OPERATIONS", "V_WB_STORE_FINANCE_COVERAGE")


def views():
    code = "\n".join(line.split("--")[0] for line in SQL.split("\n"))
    return dict(re.findall(r"CREATE OR REPLACE VIEW `[^`]+\.(\w+)` AS\n(.*?);\s*(?=\n|$)", code, re.S))


V = views()


def op_map_rows():
    arr = sqlglot.parse_one(V["V_WB_FINANCE_OPERATION_MAP"], read="bigquery").find(exp.Array)

    def val(e):
        if isinstance(e, (exp.PropertyEQ, exp.Alias)):
            e = e.expression if isinstance(e, exp.PropertyEQ) else e.this
        if isinstance(e, exp.Null):
            return None
        if isinstance(e, exp.Cast):
            return None if isinstance(e.this, exp.Null) else e.this.name
        return e.name

    return [tuple(val(p) for p in item.expressions) for item in arr.expressions]


def lite(body):
    body = (body.replace(f"`{P}.wb_raw.V_WB_FINANCE_CANONICAL`", "canonical")
                .replace(f"`{P}.wb_mart.V_WB_FINANCE_OPERATION_MAP`", "opmap")
                .replace("DATE(SAFE.TIMESTAMP(order_dt), 'Europe/Moscow')", "DATE(order_dt)")
                .replace("SAFE.TIMESTAMP(order_dt)", "order_dt"))
    return sqlglot.transpile(body, read="bigquery", write="sqlite")[0]


COLS = ["row_hash", "_rr_date", "supplier_oper_name", "finance_status", "source_layer", "srid", "order_dt", "wb_nm_id",
        "doc_type_name", "quantity", "retail_price_withdisc_rub", "retail_amount", "for_pay", "acquiring_fee",
        "logistics_amount", "storage_fee", "deduction", "penalty", "acceptance", "additional_payment",
        "rebill_logistics", "compensation_amount", "other_amount", "raw_json"]

NM = 22000001  # синтетический SKU


def row(key, rr, op, srid="s-1", doc="Продажа", fp=0, acq=0, q=0, pwd=0, lg=0, layer="DAILY", odt="2026-10-10T08:00:00",
        nm=NM):
    camel = {"rebillLogisticCost": 0, "ppvzReward": 0, "vw": 0, "vwNds": 0}
    r = dict(row_hash=key, _rr_date=rr, supplier_oper_name=op, finance_status="PROVISIONAL" if layer == "DAILY" else "FINAL",
             source_layer=layer, srid=srid, order_dt=odt, wb_nm_id=nm, doc_type_name=doc, quantity=q,
             retail_price_withdisc_rub=pwd, retail_amount=0, for_pay=fp, acquiring_fee=acq, logistics_amount=lg,
             storage_fee=0, deduction=0, penalty=0, acceptance=0, additional_payment=0, rebill_logistics=0,
             compensation_amount=0, other_amount=0, raw_json=json.dumps(camel))
    return tuple(r[c] for c in COLS)


def sale(key="sale", srid="s-1", nm=NM):
    # продажа 16.10 по заказу 10.10: база 600, for_pay 500, эквайринг 21,6 (3,6 %)
    return row(key, "2026-10-16", "Продажа", srid, fp=500, acq=21.6, q=1, pwd=600, layer="WEEKLY", nm=nm)


def corr(key="corr", **kw):
    # возврат части эквайринга (2,4 п.п.): forPay +14,4 = −acquiringFee
    args = dict(srid="s-1", fp=14.4, acq=-14.4)
    args.update(kw)
    return row(key, "2026-11-05", OP, **args)


def run(rows):
    db = sqlite3.connect(":memory:")
    db.create_function("DATE_TRUNC", 2, lambda unit, d: (d[:7] + "-01") if d and unit == "MONTH" else None)
    db.create_function("FORMAT_DATE", 2, lambda f, d: d[:7] if d else None)
    db.execute(f"CREATE TABLE canonical ({', '.join(COLS)})")
    db.executemany(f"INSERT INTO canonical VALUES ({', '.join('?' * len(COLS))})", rows)
    db.execute("CREATE TABLE opmap (supplier_oper_name, treatment, category, note, valid_from, valid_to)")
    db.executemany("INSERT INTO opmap VALUES (?, ?, ?, ?, ?, ?)", op_map_rows())
    out = {}
    for name, view in (("cohort", "V_WB_STORE_FINANCE_COHORT_DAILY"), ("newops", "V_WB_FINANCE_NEW_OPERATIONS"),
                       ("coverage", "V_WB_STORE_FINANCE_COVERAGE")):
        cur = db.execute(lite(V[view]))
        cols = [c[0] for c in cur.description]
        out[name] = [dict(zip(cols, r)) for r in cur.fetchall()]
    return out


def cov(out, key):
    return [r for r in out["coverage"] if r["finance_row_key"] == key][0]


def assert_pending(out, key="corr"):
    c = cov(out, key)
    assert c["treatment"] == "PENDING_CLASSIFICATION" and c["consumer"] == "NONE" and c["unconsumed_abs_rub"] > 0
    assert any(r["supplier_oper_name"] == c["supplier_oper_name"] and r["treatment"] == "PENDING_CLASSIFICATION"
               for r in out["newops"])


def test_form_rule_text_is_identical_in_the_three_views():
    src = SQL
    blocks = re.findall(r"IFNULL\(supplier_oper_name = 'Корректировка эквайринга'.*?FALSE\) acq_api_form", src, re.S)
    assert len(blocks) == 3 and len(set(blocks)) == 1
    for v in CHANGED:
        assert "acq_api_form" in V[v]


def test_map_period_is_not_extended():
    m = {r[0]: r for r in op_map_rows()}
    assert m[OP][1:3] == ("DIRECT_SKU", "REVENUE_COMMISSION") and m[OP][4:] == ("2024-09-01", "2025-01-31")


def test_proven_form_is_direct_sku_and_the_mirror_is_read_once():
    base = run([sale()])["cohort"][0]
    out = run([sale(), corr()])
    c = out["cohort"][0]
    assert round(c["for_pay_rub"] - base["for_pay_rub"], 2) == 14.4
    assert round(c["wb_take_rub"] - base["wb_take_rub"], 2) == -14.4      # не −28,8: зеркало не читается
    assert (c["sold_qty"], c["seller_base_rub"], c["logistics_rub"]) == (base["sold_qty"], base["seller_base_rub"],
                                                                         base["logistics_rub"])
    k = cov(out, "corr")
    assert (k["treatment"], k["consumer"], k["unconsumed_abs_rub"]) == ("DIRECT_SKU", "COHORT", 0)
    assert not any(r["supplier_oper_name"] == OP for r in out["newops"])


def test_without_the_rule_the_same_row_was_pending():
    out = run([sale(), corr(srid="s-2")])   # нет продажи этого srid
    assert_pending(out)
    assert cov(out, "corr")["unconsumed_abs_rub"] == 28.8                 # |forPay| + |acquiringFee|


def test_broken_mirror_is_pending():
    assert_pending(run([sale(), corr(acq=-10)]))


def test_quantity_or_base_is_pending():
    assert_pending(run([sale(), corr(q=1)]))
    assert_pending(run([sale(), corr(pwd=600)]))


def test_other_document_type_is_pending():
    assert_pending(run([sale(), corr(doc="Возврат")]))


def test_other_money_in_the_row_is_pending():
    assert_pending(run([sale(), corr(lg=5)]))


def test_negative_correction_is_a_new_form_and_pending():
    assert_pending(run([sale(), corr(fp=-14.4, acq=14.4)]))


def test_rule_does_not_spread_to_other_operations():
    other = row("cs", "2026-11-05", "Коррекция продаж", "s-1", fp=14.4, acq=-14.4)
    out = run([sale(), other])
    assert_pending(out, "cs")


def test_legacy_period_behaviour_is_unchanged():
    legacy = row("lg1", "2024-11-15", OP, "s-9", doc=None, fp=-3.0, acq=3.0, odt="2024-11-01T10:00:00", layer="LEGACY")
    out = run([legacy])
    k = cov(out, "lg1")
    assert (k["treatment"], k["consumer"]) == ("DIRECT_SKU", "COHORT")
    assert out["cohort"][0]["for_pay_rub"] == -3.0


def test_only_finance_api_rows_qualify():
    assert_pending(run([sale(), corr(layer="LEGACY")]))   # та же форма, но не из Finance API
    assert_pending(run([sale(), corr(layer=None)]))       # происхождение не указано — fail-closed


def test_weekly_layer_qualifies_like_daily():
    k = cov(run([sale(), corr(layer="WEEKLY")]), "corr")
    assert (k["treatment"], k["consumer"]) == ("DIRECT_SKU", "COHORT")


def test_sale_of_the_same_srid_must_be_the_same_sku():
    other_nm = NM + 1
    assert_pending(run([sale(nm=other_nm), corr()]))       # srid совпал, SKU продажи другой
    assert_pending(run([sale(), corr(nm=None)]))           # у корректировки нет SKU
