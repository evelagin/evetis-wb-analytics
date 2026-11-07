"""Юнитка WB · V_UNITKA_LOGISTICS_RATES: плечо = поездка (форма строки логистики) — исполнение настоящей вью на SQLite.

Инцидент 23.10.2026 (тихий): у проданного srid экспортной продажи вторая строка «Доставка» — «Услуга сопровождения
экспортных операций» без поездки (deliveryAmount = returnAmount = 0). Прежняя вью считала её вторым плечом: srid
становился отказом и пересечением, а наибольшая сумма (услуга) — прямым плечом; инвариант сходился, ставки
искажались. Правило: плечо — строка с ровно одной поездкой (deliveryAmount + returnAmount = 1); услуга экспорта при
«Продаже» того же srid — SERVICE (вне ставок); любая иная форма — UNKNOWN, вне ставок и fail-closed (прибавляется к
shipments строки магазина, инвариант Engine отклоняет прогон).

Из sql/unitka/engine_v1_views.sql берётся текст V_UNITKA_LOGISTICS_RATES; подстановки — только идентификаторы таблиц.
Суммы синтетические.
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
    # окно [LCD − 29, LCD]: в SQLite — модификатор даты
    return re.sub(r"DATE_SUB\((\w+), '?29'?, DAY\)", r"DATE(\1, '-29 day')", out)


COLS = ["srid", "wb_nm_id", "supplier_oper_name", "logistics_amount", "raw_json", "source_layer", "_rr_date"]
NM = 31000001
LCD = "2026-11-06"


def row(srid, son, amt=0, bonus=None, da=None, ra=None, layer="WEEKLY", nm=NM, rr="2026-10-23"):
    j = {}
    if bonus is not None:
        j["bonusTypeName"] = bonus
    if da is not None:
        j["deliveryAmount"] = da
    if ra is not None:
        j["returnAmount"] = ra
    return (srid, nm, son, amt, json.dumps(j, ensure_ascii=False), layer, rr)


def sale(srid, **kw):
    return row(srid, "Продажа", **kw)


def fwd(srid, amt=50, bonus="К клиенту при продаже", **kw):
    return row(srid, "Доставка", amt, bonus, 1, 0, **kw)


def back(srid, amt=30, bonus="От клиента при отмене", **kw):
    return row(srid, "Доставка", amt, bonus, 0, 1, **kw)


def run(rows):
    db = sqlite3.connect(":memory:")
    db.execute(f"CREATE TABLE canonical ({', '.join(COLS)})")
    db.executemany(f"INSERT INTO canonical VALUES ({', '.join('?' * len(COLS))})", rows)
    db.execute("CREATE TABLE lcd (last_closed_date)")
    db.execute("INSERT INTO lcd VALUES (?)", (LCD,))
    cur = db.execute(lite(view_body()))
    cols = [c[0] for c in cur.description]
    return {r[cols.index("nm_id")]: dict(zip(cols, r)) for r in cur.fetchall()}


def invariant_holds(store):
    return store["shipments"] == ((store["sales"] or 0) + store["refusals"] - (store["sale_refusal_overlap"] or 0)
                                  + (store["return_tail"] or 0) + (store["seller_return"] or 0))


BASE = [sale("s1"), fwd("s1", 50), sale("s2"), fwd("s2", 60), fwd("c1", 40, "К клиенту при отмене"), back("c1", 30)]


def test_baseline_shapes_unchanged():
    out = run(BASE)
    st = out[0]
    assert (st["shipments"], st["refusals"], st["sales"], st["sale_refusal_overlap"]) == (3, 1, 2, 0)
    assert st["forward_sum"] == 150 and st["reverse_sum"] == 30
    assert st["service_rows"] == 0 and st["unclassified_srids"] == 0
    assert invariant_holds(st)


def test_export_service_is_not_a_leg_and_not_a_refusal():
    rows = BASE + [sale("e1"), fwd("e1", 50.6), row("e1", "Доставка", 180, EXPORT, 0, 0)]
    st = run(rows)[0]
    assert (st["shipments"], st["refusals"], st["sales"], st["sale_refusal_overlap"]) == (4, 1, 3, 0)
    assert st["forward_sum"] == 150 + 50.6            # прямое плечо — 50,6, а не услуга 180
    assert st["reverse_sum"] == 30                    # 50,6 не уходит в обратный пул
    assert (st["service_rows"], st["service_sum"], st["unclassified_srids"]) == (1, 180, 0)
    assert invariant_holds(st)


def test_unknown_second_row_of_a_sold_srid_fails_closed_with_amount():
    rows = BASE + [sale("u1"), fwd("u1", 55), row("u1", "Доставка", 77, "Новая услуга WB", 0, 0)]
    st = run(rows)[0]
    assert st["unclassified_srids"] == 1 and st["unclassified_sum"] == 77
    assert st["refusals"] == 1 and st["sale_refusal_overlap"] == 0     # ставки не искажены
    assert st["forward_sum"] == 150 + 55
    assert not invariant_holds(st)                                     # Engine отклонит прогон: не тихо


def test_export_service_without_a_sale_is_unknown():
    st = run(BASE + [row("e2", "Доставка", 180, EXPORT, 0, 0)])[0]
    assert st["unclassified_srids"] == 1 and st["service_rows"] == 0
    assert not invariant_holds(st)


def test_leg_with_missing_or_double_trip_fields_is_unknown():
    for bad in (row("b1", "Доставка", 50, "К клиенту при продаже"),            # нет полей поездки (не LEGACY)
                row("b2", "Доставка", 50, "К клиенту при продаже", 1, 1),      # две поездки в одной строке
                row("b3", "Доставка", -50, "К клиенту при продаже", -1, 0)):   # отрицательная поездка
        st = run(BASE + [sale(bad[0]), bad])[0]
        assert st["unclassified_srids"] == 1, bad
        assert not invariant_holds(st)


def test_real_customer_return_keeps_its_classes():
    rows = BASE + [sale("r1"), fwd("r1", 50), row("r1", "Возврат"), back("r1", 32, "От клиента при возврате")]
    st = run(rows)[0]
    assert (st["refusals"], st["sale_refusal_overlap"]) == (2, 1)     # как и прежде: продажа + возврат — обе группы
    assert st["reverse_sum"] == 30 + 32 and st["unclassified_srids"] == 0
    assert invariant_holds(st)


def test_seller_return_and_return_tail_keep_their_classes():
    rows = BASE + [fwd("x1", 255, "Возврат неопознанного товара (К продавцу)"),
                   row("t1", "Возврат"), back("t1", 26, "От клиента при возврате")]
    st = run(rows)[0]
    assert (st["seller_return"], st["return_tail"], st["unclassified_srids"]) == (1, 1, 0)
    assert invariant_holds(st)


def test_daily_to_weekly_replacement_gives_the_same_rates():
    shape = [sale("e1"), fwd("e1", 50.6), row("e1", "Доставка", 180, EXPORT, 0, 0)]
    daily = run(BASE + [r[:5] + ("DAILY",) + r[6:] for r in shape])[0]
    weekly = run(BASE + shape)[0]
    keys = ["shipments", "refusals", "sale_refusal_overlap", "forward_sum", "reverse_sum", "direct_rate", "reverse_rate",
            "service_rows", "service_sum", "unclassified_srids"]
    assert {k: daily[k] for k in keys} == {k: weekly[k] for k in keys}


def test_legacy_rows_keep_the_old_behaviour():
    rows = [row("L1", "Продажа", layer="LEGACY"), row("L1", "Логистика", 45, layer="LEGACY")]
    st = run(rows)[0]
    assert (st["shipments"], st["unclassified_srids"], st["forward_sum"]) == (1, 0, 45)


def test_only_store_and_affected_sku_rows_carry_service():
    rows = BASE + [sale("e1", nm=NM + 1), fwd("e1", 50.6, nm=NM + 1), row("e1", "Доставка", 180, EXPORT, 0, 0, nm=NM + 1)]
    out = run(rows)
    assert out[NM + 1]["service_rows"] == 1 and out[NM]["service_rows"] == 0
    assert out[NM + 1]["refusals"] == 0
