"""Store P&L WB · возврат неопознанного товара продавцу — исполнение настоящих вью на SQLite-фикстурах.

Инцидент 05.11.2026: WB вернул продавцу обезличенный при приёмке товар. Финотчёт несёт плечо «Логистика»/«Доставка»
с bonusTypeName «Возврат неопознанного товара (К продавцу)» на служебном nm «неопознанный товар» с датой «заказа».
Это не заказ покупателя: в когорте заказа плечо висело сиротой (UNKNOWN_COST_PRESENT, вне прибыли).
Решение: плечо — расход магазина UNIDENTIFIED_RETURN в журнале кабинета (месяц проводки), из когорты исключено,
в покрытии — свой потребитель ACCOUNT_LEDGER_LOGISTICS. Маржа SKU и клиентская логистика не меняются.

Из sql/unitka/store_pnl_v1.sql берутся тексты V_WB_STORE_FINANCE_COHORT_DAILY, V_WB_STORE_ACCOUNT_LEDGER,
V_WB_STORE_FINANCE_COVERAGE и строки карты V_WB_FINANCE_OPERATION_MAP. Подстановки (только то, чего нет в SQLite):
идентификаторы таблиц; DATE(SAFE.TIMESTAMP(order_dt), 'Europe/Moscow') → DATE(order_dt), SAFE.TIMESTAMP(order_dt) → order_dt
(фикстуры — сутки МСК);
CTE months (UNNEST … WITH OFFSET) → VALUES; DATE_TRUNC(…, MONTH) и DATE(y, m, d) — функции теста, DATE_SUB(…, 1 MONTH) → DATE(…, '-1 month'); хвост журнала после CTE l (маскировка номера документа компенсации
за утрату, дата отчёта) → SELECT * FROM l. CTE other и l — дословно. Суммы синтетические.
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
UNID = "Возврат неопознанного товара (К продавцу)"


def views():
    code = "\n".join(line.split("--")[0] for line in SQL.split("\n"))
    return dict(re.findall(r"CREATE OR REPLACE VIEW `[^`]+\.(\w+)` AS\n(.*?);\s*(?=\n|$)", code, re.S))


V = views()


def op_map_rows():
    """Строки карты операций — из текста вью (STRUCT первой строкой, дальше кортежи)."""
    tree = sqlglot.parse_one(V["V_WB_FINANCE_OPERATION_MAP"], read="bigquery")
    arr = tree.find(exp.Array)

    def val(e):
        if isinstance(e, (exp.PropertyEQ, exp.Alias)):
            e = e.expression if isinstance(e, exp.PropertyEQ) else e.this
        if isinstance(e, exp.Null):
            return None
        if isinstance(e, exp.Cast):
            return None if isinstance(e.this, exp.Null) else e.this.name
        return e.name

    rows = []
    for item in arr.expressions:
        parts = item.expressions
        rows.append(tuple(val(p) for p in parts))
    assert all(len(r) == 6 for r in rows)
    return rows


def lite(body):
    body = (body.replace(f"`{P}.wb_raw.V_WB_FINANCE_CANONICAL`", "canonical")
                .replace(f"`{P}.wb_mart.V_WB_FINANCE_OPERATION_MAP`", "opmap")
                .replace(f"`{P}.wb_mart.V_WB_DEDUCTIONS_CLASSIFIED`", "deductions")
                .replace("DATE(SAFE.TIMESTAMP(order_dt), 'Europe/Moscow')", "DATE(order_dt)")
                .replace("SAFE.TIMESTAMP(order_dt)", "order_dt"))
    out = sqlglot.transpile(body, read="bigquery", write="sqlite")[0]
    # «минус месяц» минимального платежа: в SQLite — модификатор даты
    return re.sub(r"DATE_SUB\((DATE_TRUNC\('MONTH', \w+\)), '1', MONTH\)", r"DATE(\1, '-1 month')", out)


def ledger_body():
    b = V["V_WB_STORE_ACCOUNT_LEDGER"]
    months = re.search(r"months AS \(.*?WITH OFFSET i\),", b, re.S)
    assert months, "CTE months не найден"
    names = ['январь', 'февраль', 'март', 'апрель', 'май', 'июнь', 'июль', 'август', 'сентябрь', 'октябрь',
             'ноябрь', 'декабрь']
    vals = " UNION ALL ".join(f"SELECT '{n}' AS name, {i} AS i" for i, n in enumerate(names))
    b = b.replace(months.group(0), f"months AS ({vals}),")
    tail = b.index("\nSELECT x.* EXCEPT")
    return b[:tail] + "\nSELECT * FROM l"


COLS = ["row_hash", "_rr_date", "supplier_oper_name", "finance_status", "source_layer", "srid", "order_dt", "wb_nm_id",
        "quantity", "retail_price_withdisc_rub", "retail_amount", "for_pay", "logistics_amount", "storage_fee",
        "deduction", "penalty", "acceptance", "additional_payment", "rebill_logistics", "compensation_amount",
        "other_amount", "acquiring_fee", "report_period_from", "report_period_to", "raw_json"]


def row(key, rr, op, layer="DAILY", srid="", order_dt=None, nm=None, logistics=0, penalty=0, for_pay=0, bonus=None, **kw):
    camel = {"rebillLogisticCost": 0, "ppvzReward": 0, "vw": 0, "vwNds": 0}
    if bonus is not None:
        camel["bonusTypeName"] = bonus
    r = dict(row_hash=key, _rr_date=rr, supplier_oper_name=op,
             finance_status="PROVISIONAL" if layer == "DAILY" else "FINAL", source_layer=layer, srid=srid,
             order_dt=order_dt, wb_nm_id=nm, quantity=kw.get("quantity", 0), retail_price_withdisc_rub=kw.get("pwd", 0),
             retail_amount=0, for_pay=for_pay, logistics_amount=logistics, storage_fee=0, deduction=0, penalty=penalty,
             acceptance=0, additional_payment=0, rebill_logistics=0, compensation_amount=0, other_amount=0,
             acquiring_fee=0, report_period_from=rr, report_period_to=rr, raw_json=json.dumps(camel, ensure_ascii=False))
    return tuple(r[c] for c in COLS)


ORPHAN_NM = 99000001   # синтетический служебный nm «неопознанный товар»
CLIENT_NM = 11000001   # синтетический SKU клиента


def incident(layer="DAILY", prefix="d"):
    """Три плеча возврата обезлички (по 100 ₽), штраф за разворот поставки 500 ₽, обычная логистика клиента 40 ₽."""
    return [
        row(f"{prefix}1", "2026-11-05", "Доставка", layer, "s-u1", "2026-11-02T10:00:00", ORPHAN_NM, 100, bonus=UNID),
        row(f"{prefix}2", "2026-11-05", "Доставка", layer, "s-u2", "2026-11-02T11:00:00", ORPHAN_NM, 100, bonus=UNID),
        row(f"{prefix}3", "2026-11-05", "Доставка", layer, "s-u3", "2026-11-03T09:00:00", ORPHAN_NM, 100, bonus=UNID),
        row(f"{prefix}4", "2026-11-05", "Штраф", layer, "supply-1", None, 0, 0, penalty=500,
            bonus="Разворот поставки, поставка supply-1"),
        row(f"{prefix}5", "2026-11-05", "Доставка", layer, "s-c1", "2026-11-04T12:00:00", CLIENT_NM, 40,
            bonus="К клиенту при продаже"),
    ]


def run(rows):
    db = sqlite3.connect(":memory:")
    db.create_function("DATE_FROM_PARTS", 3, lambda y, m, d: f"{int(y):04d}-{int(m):02d}-{int(d):02d}" if None not in (y, m, d) else None)
    db.create_function("DATE_TRUNC", 2, lambda unit, d: (d[:7] + "-01") if d and unit == "MONTH" else None)
    db.create_function("REGEXP_EXTRACT", 3, lambda s, p, g: (re.search(p, s).group(g) if s and re.search(p, s) else None))
    db.execute(f"CREATE TABLE canonical ({', '.join(COLS)})")
    db.executemany(f"INSERT INTO canonical VALUES ({', '.join('?' * len(COLS))})", rows)
    db.execute("CREATE TABLE opmap (supplier_oper_name, treatment, category, note, valid_from, valid_to)")
    db.executemany("INSERT INTO opmap VALUES (?, ?, ?, ?, ?, ?)", op_map_rows())
    db.execute("CREATE TABLE deductions (finance_row_key, finance_date, bonus_type_name, deduction_class, "
               "deduction_amount_rub, srid, transit_supply_id)")
    out = {}
    for name, body in (("cohort", V["V_WB_STORE_FINANCE_COHORT_DAILY"]), ("ledger", ledger_body()),
                       ("coverage", V["V_WB_STORE_FINANCE_COVERAGE"])):
        cur = db.execute(lite(body))
        cols = [c[0] for c in cur.description]
        out[name] = [dict(zip(cols, r)) for r in cur.fetchall()]
    return out


def ledger_by(out, category):
    return [r for r in out["ledger"] if r["category"] == category]


def test_map_parser_reads_the_real_map():
    m = {r[0]: r for r in op_map_rows()}
    assert m["Доставка"][1] == "DIRECT_SKU" and m["Штраф"][1] == "ACCOUNT_LEVEL"
    assert m["Компенсация ущерба"][4:] == ("2024-12-01", "2025-02-28")


def test_unidentified_return_leaves_the_cohort_and_becomes_a_store_cost_of_the_booking_month():
    out = run(incident())
    orphan = [r for r in out["cohort"] if r["nm_id"] == ORPHAN_NM]
    assert sum(r["logistics_rub"] for r in orphan) == 0
    client = [r for r in out["cohort"] if r["nm_id"] == CLIENT_NM]
    assert [r["logistics_rub"] for r in client] == [40]          # логистика клиента не тронута
    led = ledger_by(out, "UNIDENTIFIED_RETURN")
    assert len(led) == 3 and sum(r["amount_rub"] for r in led) == 300
    assert {r["treatment"] for r in led} == {"ACCOUNT_LEVEL"}
    assert {r["service_month"] for r in led} == {"2026-11-01"}   # заказ 02–03.11, проводка 05.11 → ноябрь
    assert {r["service_period_basis"] for r in led} == {"BOOKING_MONTH"}


def test_coverage_consumes_every_rouble_and_names_the_consumer():
    out = run(incident())
    by_key = {r["finance_row_key"]: r for r in out["coverage"]}
    assert {by_key[k]["consumer"] for k in ("d1", "d2", "d3")} == {"ACCOUNT_LEDGER_LOGISTICS"}
    assert by_key["d5"]["consumer"] == "COHORT" and by_key["d4"]["consumer"] == "ACCOUNT_LEDGER"
    assert sum(r["unconsumed_abs_rub"] for r in out["coverage"]) == 0


def test_only_the_exact_operation_type_is_reclassified():
    rows = [
        row("x1", "2026-11-05", "Доставка", "DAILY", "s-x1", "2026-11-02T10:00:00", ORPHAN_NM, 100,
            bonus="Возврат брака (К продавцу)"),
        row("x2", "2026-11-05", "Доставка", "DAILY", "s-x2", "2026-11-02T10:00:00", ORPHAN_NM, 100,
            bonus="Возврат неопознанного товара"),
        row("x3", "2026-11-05", "Доставка", "DAILY", "s-x3", "2026-11-02T10:00:00", ORPHAN_NM, 100, bonus=None),
        # сторно-строка WB «Коррекция логистики» с тем же типом не поглощается молча: остаётся в когорте (видна)
        row("x4", "2026-11-05", "Коррекция логистики", "DAILY", "s-x4", "2026-11-02T10:00:00", ORPHAN_NM, -100,
            bonus=UNID),
    ]
    out = run(rows)
    assert ledger_by(out, "UNIDENTIFIED_RETURN") == []
    assert sum(r["logistics_rub"] for r in out["cohort"]) == 200
    assert {r["consumer"] for r in out["coverage"]} == {"COHORT"}


def test_penalty_of_the_same_supply_is_counted_once():
    out = run(incident())
    pen = ledger_by(out, "PENALTY")
    assert [r["amount_rub"] for r in pen] == [500]
    keys = [(r["finance_row_key"], r["category"]) for r in out["ledger"]]
    assert len(keys) == len(set(keys)) and len({k for k, _ in keys}) == len(keys)  # каждая строка финотчёта — одна строка журнала
    assert sum(r["amount_rub"] for r in out["ledger"]) == 800                       # 300 возврата + 500 штрафа


def test_daily_to_weekly_replacement_does_not_double():
    """Канон финотчёта отдаёт неделю ОДНИМ слоем: DAILY до окончательного недельного, затем WEEKLY.
    Вью построчны и суммируют по месяцу проводки — замена слоя меняет ключи, но не деньги и не число строк."""
    before = run(incident("DAILY", "d"))
    after = run(incident("WEEKLY", "w"))

    def money(out):
        return {(r["category"], r["service_month"]): r["amount_rub"] for r in out["ledger"]}

    def total(out, cat):
        return sum(r["amount_rub"] for r in out["ledger"] if r["category"] == cat)

    assert total(before, "UNIDENTIFIED_RETURN") == total(after, "UNIDENTIFIED_RETURN") == 300
    assert len(ledger_by(before, "UNIDENTIFIED_RETURN")) == len(ledger_by(after, "UNIDENTIFIED_RETURN")) == 3
    assert set(money(before)) == set(money(after))
    assert sum(r["unconsumed_abs_rub"] for r in after["coverage"]) == 0
    assert {r["finance_status"] for r in after["coverage"]} == {"FINAL"}


def test_reversal_keeps_its_sign_and_lands_in_its_own_booking_month():
    rows = incident() + [
        row("r1", "2026-12-07", "Доставка", "DAILY", "s-u1", "2026-11-02T10:00:00", ORPHAN_NM, -100, bonus=UNID)]
    out = run(rows)
    led = ledger_by(out, "UNIDENTIFIED_RETURN")
    nov = sum(r["amount_rub"] for r in led if r["service_month"] == "2026-11-01")
    dec = sum(r["amount_rub"] for r in led if r["service_month"] == "2026-12-01")
    assert (nov, dec) == (300, -100)          # ноябрь не переписывается, сторно уменьшает расход декабря
    assert sum(r["unconsumed_abs_rub"] for r in out["coverage"]) == 0


def test_wb_compensation_outside_the_proven_period_fails_closed_and_is_not_netted():
    rows = incident() + [
        row("c1", "2026-12-07", "Компенсация ущерба", "DAILY", "lost:s-u1", None, ORPHAN_NM, 0, for_pay=100)]
    out = run(rows)
    assert ledger_by(out, "COMPENSATION_INCOME") == []
    assert sum(r["amount_rub"] for r in ledger_by(out, "UNIDENTIFIED_RETURN")) == 300
    comp = [r for r in out["coverage"] if r["finance_row_key"] == "c1"]
    assert comp[0]["treatment"] == "PENDING_CLASSIFICATION" and comp[0]["consumer"] == "NONE"
    assert comp[0]["unconsumed_abs_rub"] == 100   # блокирует закрытие месяца до решения владельца


def test_engine_and_client_logistics_do_not_read_the_changed_views():
    changed = ("V_WB_STORE_FINANCE_COHORT_DAILY", "V_WB_STORE_ACCOUNT_LEDGER", "V_WB_STORE_FINANCE_COVERAGE")
    engine_sql = ["engine_v1_views.sql", "reconcile_v1.sql", "cancel_class_v1.sql", "integrity_v1.sql",
                  "refusals_v1.sql"]
    for f in engine_sql:
        text = (ROOT / "sql" / "unitka" / f).read_text(encoding="utf-8")
        assert not any(v in text for v in changed), f
    for src in (ROOT / "cloud" / "src" / "loaders" / "unitka").glob("*.ts"):
        text = src.read_text(encoding="utf-8")
        assert not any(v in text for v in changed), src.name
