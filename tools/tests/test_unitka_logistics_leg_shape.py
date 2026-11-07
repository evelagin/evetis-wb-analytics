"""Юнитка WB · V_UNITKA_LOGISTICS_RATES: плечо = поездка, направление — по поездке — исполнение настоящей вью на SQLite.

Инцидент 23.10.2026 (тихий): у проданного srid экспортной продажи вторая строка «Доставка» — «Услуга сопровождения
экспортных операций» без поездки (deliveryAmount = returnAmount = 0, способ доставки пуст). Прежняя вью считала её
вторым плечом: srid становился отказом и пересечением, а наибольшая сумма (услуга) — прямым плечом.

Правила (Finance API):
  LEG     — ровно одна поездка (deliveryAmount + returnAmount = 1); прямое — deliveryAmount = 1, обратное —
            returnAmount = 1; у srid не больше одного прямого и одного обратного, иначе UNKNOWN;
  SERVICE — точный bonusTypeName экспортного сопровождения, без поездки и способа доставки, при «Продаже» (docType
            «Продажа») и прямом плече ТОГО ЖЕ srid и ТОГО ЖЕ nm — вне ставок;
  UNKNOWN — всё прочее: shipments/sales истинные, ставки строки магазина = NULL — Engine отклоняет прогон явно;
  LEGACY  — как прежде (полей поездки нет, порядок плеч по сумме).

Из sql/unitka/engine_v1_views.sql берётся текст V_UNITKA_LOGISTICS_RATES; подстановки — только идентификаторы таблиц и
DATE_SUB(…, 29 DAY) → DATE(…, '-29 day'). Суммы синтетические.
"""
from pathlib import Path
import json
import re
import sqlite3

import sqlglot

ROOT = Path(__file__).resolve().parents[2]
SQL = (ROOT / "sql" / "unitka" / "engine_v1_views.sql").read_text(encoding="utf-8")
P = "project-fa311fc0-4d87-4781-986"
EXPORT = "Услуга сопровождения экспортных операций"


def view_body():
    code = "\n".join(line.split("--")[0] for line in SQL.split("\n"))
    m = re.search(r"CREATE OR REPLACE VIEW `[^`]+\.V_UNITKA_LOGISTICS_RATES` AS\n(.*?);\s*(?=\n|$)", code, re.S)
    return m.group(1)


def lite(body):
    body = (body.replace(f"`{P}.wb_raw.V_WB_FINANCE_CANONICAL`", "canonical")
                .replace(f"`{P}.wb_mart.V_UNITKA_LAST_CLOSED_DATE`", "lcd"))
    out = sqlglot.transpile(body, read="bigquery", write="sqlite")[0]
    return re.sub(r"DATE_SUB\((\w+), '?29'?, DAY\)", r"DATE(\1, '-29 day')", out)


COLS = ["srid", "wb_nm_id", "supplier_oper_name", "logistics_amount", "raw_json", "source_layer", "_rr_date"]
NM = 31000001
LCD = "2026-11-06"


def row(srid, son, amt=0, bonus=None, da=None, ra=None, layer="WEEKLY", nm=NM, rr="2026-10-23", doc=None, dm=None):
    j = {}
    for k, v in (("bonusTypeName", bonus), ("deliveryAmount", da), ("returnAmount", ra), ("docTypeName", doc),
                 ("deliveryMethod", dm)):
        if v is not None:
            j[k] = v
    return (srid, nm, son, amt, json.dumps(j, ensure_ascii=False), layer, rr)


def sale(srid, **kw):
    return row(srid, "Продажа", doc="Продажа", **kw)


def ret(srid, **kw):
    return row(srid, "Возврат", doc="Возврат", **kw)


def fwd(srid, amt=50, bonus="К клиенту при продаже", **kw):
    return row(srid, "Доставка", amt, bonus, 1, 0, dm="FBW", **kw)


def back(srid, amt=30, bonus="От клиента при отмене", **kw):
    return row(srid, "Доставка", amt, bonus, 0, 1, dm="FBW", **kw)


def service(srid, amt=180, **kw):
    return row(srid, "Доставка", amt, EXPORT, 0, 0, **kw)


def run(rows):
    db = sqlite3.connect(":memory:")
    db.execute(f"CREATE TABLE canonical ({', '.join(COLS)})")
    db.executemany(f"INSERT INTO canonical VALUES ({', '.join('?' * len(COLS))})", rows)
    db.execute("CREATE TABLE lcd (last_closed_date)")
    db.execute("INSERT INTO lcd VALUES (?)", (LCD,))
    cur = db.execute(lite(view_body()))
    cols = [c[0] for c in cur.description]
    return {r[cols.index("nm_id")]: dict(zip(cols, r)) for r in cur.fetchall()}


def invariant_holds(st):
    return st["shipments"] == ((st["sales"] or 0) + st["refusals"] - (st["sale_refusal_overlap"] or 0)
                               + (st["return_tail"] or 0) + (st["seller_return"] or 0))


def engine_accepts(st):
    """plan.ts: нет ставок магазина → INVARIANT_FAIL; затем равенство инварианта."""
    return st["direct_rate"] is not None and st["reverse_rate"] is not None and invariant_holds(st)


BASE = [sale("s1"), fwd("s1", 50), sale("s2"), fwd("s2", 60), fwd("c1", 40, "К клиенту при отмене"), back("c1", 30)]


def test_baseline_shapes_unchanged():
    st = run(BASE)[0]
    assert (st["shipments"], st["refusals"], st["sales"], st["sale_refusal_overlap"]) == (3, 1, 2, 0)
    assert st["forward_sum"] == 150 and st["reverse_sum"] == 30
    assert st["service_rows"] == 0 and st["unclassified_srids"] == 0
    assert engine_accepts(st)


# ── 1. направление плеча — по поездке ─────────────────────────────────────────────────────────────────────────────
def test_reverse_leg_more_expensive_than_forward_goes_to_reverse_pool():
    st = run(BASE + [fwd("c2", 20, "К клиенту при отмене"), back("c2", 35)])[0]
    assert st["forward_sum"] == 150 + 20 and st["reverse_sum"] == 30 + 35   # по сумме было бы 185 / 50
    assert st["refusals"] == 2 and engine_accepts(st)


def test_forward_leg_more_expensive_than_reverse():
    st = run(BASE + [fwd("c2", 45, "К клиенту при отмене"), back("c2", 25)])[0]
    assert st["forward_sum"] == 150 + 45 and st["reverse_sum"] == 30 + 25
    assert engine_accepts(st)


def test_real_sale_with_later_customer_return():
    rows = BASE + [sale("r1"), fwd("r1", 50), ret("r1"), back("r1", 32, "От клиента при возврате")]
    st = run(rows)[0]
    assert (st["refusals"], st["sale_refusal_overlap"]) == (2, 1)
    assert st["forward_sum"] == 150 + 50 and st["reverse_sum"] == 30 + 32
    assert engine_accepts(st)


def test_customer_refusal_pair():
    st = run(BASE)[0]
    assert st["refusals"] == 1 and st["reverse_sum"] == 30


def test_single_return_tail_is_reverse():
    st = run(BASE + [ret("t1"), back("t1", 26, "От клиента при возврате")])[0]
    assert st["return_tail"] == 1 and st["reverse_sum"] == 30 + 26 and st["forward_sum"] == 150
    assert engine_accepts(st)


def test_legacy_without_trip_fields_keeps_amount_order():
    rows = [row("L1", "Продажа", layer="LEGACY"), row("L1", "Логистика", 45, layer="LEGACY"),
            row("L2", "Логистика", 20, layer="LEGACY"), row("L2", "Логистика", 35, layer="LEGACY")]
    st = run(rows)[0]
    assert (st["shipments"], st["refusals"], st["unclassified_srids"]) == (2, 1, 0)
    assert st["forward_sum"] == 45 + 35 and st["reverse_sum"] == 20     # LEGACY: прямое — наибольшее, как прежде


def test_two_forward_legs_of_one_srid_are_unknown():
    st = run(BASE + [sale("d1"), fwd("d1", 50), fwd("d1", 52)])[0]
    assert st["unclassified_srids"] == 1 and not engine_accepts(st)


# ── 2. SERVICE — только при продаже и прямом плече того же srid И того же nm ──────────────────────────────────────
def test_export_service_is_not_a_leg_and_not_a_refusal():
    st = run(BASE + [sale("e1"), fwd("e1", 50.6), service("e1")])[0]
    assert (st["shipments"], st["refusals"], st["sales"], st["sale_refusal_overlap"]) == (4, 1, 3, 0)
    assert st["forward_sum"] == 150 + 50.6 and st["reverse_sum"] == 30
    assert (st["service_rows"], st["service_sum"], st["unclassified_srids"]) == (1, 180, 0)
    assert engine_accepts(st)


def test_service_with_same_srid_but_other_nm_is_not_confirmed():
    rows = BASE + [sale("e1"), fwd("e1", 50.6), service("e1", nm=NM + 7)]
    st = run(rows)[0]
    assert st["service_rows"] == 0 and st["unclassified_srids"] == 1 and st["unclassified_sum"] == 180
    assert not engine_accepts(st)


def test_service_without_a_sale_or_without_a_forward_leg_is_unknown():
    assert run(BASE + [service("e2")])[0]["unclassified_srids"] == 1
    assert run(BASE + [sale("e3"), service("e3")])[0]["unclassified_srids"] == 1


def test_service_row_with_a_delivery_method_is_unknown():
    rows = BASE + [sale("e1"), fwd("e1", 50.6), row("e1", "Доставка", 180, EXPORT, 0, 0, dm="FBW")]
    assert run(rows)[0]["unclassified_srids"] == 1


# ── 3. UNKNOWN: истинные shipments/sales, явная блокировка ставок ────────────────────────────────────────────────
def test_unknown_second_row_blocks_rates_without_touching_counts():
    clean = run(BASE + [sale("u1"), fwd("u1", 55)])[0]
    st = run(BASE + [sale("u1"), fwd("u1", 55), row("u1", "Доставка", 77, "Новая услуга WB", 0, 0)])[0]
    assert (st["shipments"], st["sales"], st["refusals"]) == (clean["shipments"], clean["sales"], clean["refusals"])
    assert invariant_holds(st)                                          # счётчики правдивы
    assert st["direct_rate"] is None and st["reverse_rate"] is None     # экономика заблокирована явно
    assert (st["unclassified_srids"], st["unclassified_sum"]) == (1, 77)
    assert not engine_accepts(st)


def test_rows_with_missing_or_bad_trip_fields_are_unknown():
    for bad in (row("b1", "Доставка", 50, "К клиенту при продаже"),
                row("b2", "Доставка", 50, "К клиенту при продаже", 1, 1),
                row("b3", "Доставка", -50, "К клиенту при продаже", -1, 0)):
        st = run(BASE + [sale(bad[0]), bad])[0]
        assert st["unclassified_srids"] == 1 and st["direct_rate"] is None, bad


# ── прочее ──────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_seller_return_keeps_its_class():
    st = run(BASE + [fwd("x1", 255, "Возврат неопознанного товара (К продавцу)")])[0]
    assert (st["seller_return"], st["unclassified_srids"]) == (1, 0) and engine_accepts(st)


def test_daily_to_weekly_replacement_gives_the_same_rates():
    shape = [sale("e1"), fwd("e1", 50.6), service("e1"), fwd("c2", 20, "К клиенту при отмене"), back("c2", 35)]
    daily = run(BASE + [r[:5] + ("DAILY",) + r[6:] for r in shape])[0]
    weekly = run(BASE + shape)[0]
    keys = ["shipments", "refusals", "sale_refusal_overlap", "forward_sum", "reverse_sum", "direct_rate", "reverse_rate",
            "service_rows", "service_sum", "unclassified_srids"]
    assert {k: daily[k] for k in keys} == {k: weekly[k] for k in keys}


def test_money_is_conserved():
    rows = BASE + [sale("e1"), fwd("e1", 50.6), service("e1"), fwd("x1", 255, "Возврат брака (К продавцу)"),
                   sale("u1"), fwd("u1", 55), row("u1", "Доставка", 77, "Новая услуга WB", 0, 0)]
    st = run(rows)[0]
    total = sum(r[3] for r in rows if r[2] in ("Логистика", "Доставка"))
    assert round(st["forward_sum"] + st["reverse_sum"] + st["seller_return_sum"] + st["service_sum"]
                 + st["unclassified_sum"], 2) == round(total, 2)
