"""D1 (2026-10-11): сторно реализации без services и контроль денег суток finance_accrual.

Дефект: `_finance_rows` писал экономический блок продукта (цена продавца, комиссия) только
прицепом к первой строке services. Начисление, где у продукта блок commission есть, а
services нет (null) или пуст ([]), давало ноль строк — сторно реализации при возврате
покупателя терялось молча.

Все фикстуры здесь СИНТЕТИЧЕСКИЕ: идентификаторы и суммы выдуманы, структура повторяет
ответ POST /v1/finance/accrual/by-day. Сценарии:
  A  обычная продажа с services;
  B  сторно без services (delivery = null);
  C  сторно с пустым массивом services;
  D  экономический блок И services в одном продукте — деньги не удваиваются;
  E  отрицательная корректировка, которая не сторно продажи;
  F  повторная загрузка — те же строки, ключи и хэши.
"""
from __future__ import annotations

import copy
import json
from decimal import Decimal

import pytest

import common as C

KEYS = ["accrual_id", "type_id", "sku"]
TS = "2026-10-11T06:30:00+03:00"
DAY = "2026-07-15"
TYPES = {"accrual_types": [{"id": 32, "description": "Логистика"},
                           {"id": 59, "description": "Обратная логистика"},
                           {"id": 57, "description": "Корректировка стоимости услуг"},
                           {"id": 1, "description": "Эквайринг"}]}


def money(v):
    return {"amount": str(v), "currency": "RUB"}


def commission(sign, price="1000", comm="410", ratio="0.410000", sale_price="700",
               bonus="300", coinv="0"):
    """Экономический блок: sign=+1 — реализация, −1 — зеркальное сторно."""
    def s(v):
        return str(Decimal(v) * sign)
    return {"seller_price": money(s(price)), "sale_price": money(s(sale_price)),
            "sale_commission": money(str(-Decimal(comm) * sign)),
            "commission": money(str(-Decimal(comm) * sign)),
            "commission_ratio": f'value:"{ratio}"', "sale_amount": money(s(price)),
            "coinvestment": money(s(coinv)), "bonus": money(s(bonus))}


def accrual(aid, total, products, unit="SYN-0001-1", category="POSTING", **extra):
    a = {"accrual_id": aid, "date": DAY, "total_amount": money(total), "unit_number": unit,
         "accrued_category": category,
         "posting": {"delivery_schema": "Fbo", "products": products} if products is not None else None,
         "item_fees": None, "non_item_fee": None, "container_fees": None}
    a.update(extra)
    return a


# A: продажа 1000, комиссия −410, логистика −88 и −25 → total 477
SALE_A = accrual(7000001, "477", [{
    "sku": 555001, "quantity": 1, "commission": commission(+1),
    "delivery": {"total_accrued": money("-113"), "services": [
        {"type_id": 32, "accrued": money("-88")}, {"type_id": 98, "accrued": money("-25")}]}}])
# B: сторно той же продажи, delivery = null → total −590
REVERSAL_B = accrual(7000002, "-590", [{
    "sku": 555001, "quantity": 1, "delivery": None, "commission": commission(-1)}])
# C: сторно, services = [] → total −590
REVERSAL_C = accrual(7000003, "-590", [{
    "sku": 555002, "quantity": 1, "delivery": {"total_accrued": money("0"), "services": []},
    "commission": commission(-1)}], unit="SYN-0002-1")
# D: сторно вместе с обратной логистикой −60 в одном продукте → total −650
REVERSAL_WITH_SERVICE_D = accrual(7000004, "-650", [{
    "sku": 555003, "quantity": 1, "commission": commission(-1),
    "delivery": {"total_accrued": money("-60"), "services": [
        {"type_id": 59, "accrued": money("-60")}]}}], unit="SYN-0003-1")
# E1: отрицательная корректировка услуг уровня магазина — не сторно продажи
CORRECTION_E1 = accrual(7000005, "-12.5", None, unit=None, category="NON_ITEM",
                        non_item_fee={"type_id": 57, "accrued": money("-12.5")})
# E2: перерасчёт логистики отправления со знаком минус, экономики нет
CORRECTION_E2 = accrual(7000006, "-30", [{
    "sku": 555001, "quantity": 1, "commission": None,
    "delivery": {"total_accrued": money("-30"), "services": [
        {"type_id": 32, "accrued": money("-30")}]}}])
# E3: возврат эквайринга по SKU (item_fees), положительный
ACQUIRING_E3 = accrual(7000007, "4.5", None, category="ITEM",
                       item_fees={"fees": [{"sku": 555001, "fees": [
                           {"type_id": 1, "accrued": money("4.5")}]}]})

ALL = [SALE_A, REVERSAL_B, REVERSAL_C, REVERSAL_WITH_SERVICE_D, CORRECTION_E1, CORRECTION_E2,
       ACQUIRING_E3]


def by_day(days, calls=None):
    def seller_post(path, body):
        if calls is not None:
            calls.append((path, json.loads(json.dumps(body))))
        if path == "/v1/finance/accrual/types":
            return 200, TYPES
        return 200, {"accruals": copy.deepcopy(days.get(body["date"], [])), "last_id": ""}
    return seller_post


def rows_of(E, chunk, control=None):
    types = {x["id"]: x["description"] for x in TYPES["accrual_types"]}
    return E._finance_rows(copy.deepcopy(chunk), types, DAY, "rt-d1", TS, control=control)


def econ_money(rows):
    """Деньги экономического блока так, как их видит FCT: цена × кол-во + комиссия."""
    return sum(Decimal(str(r["seller_base_price_rub"])) * (r["quantity"] or 1)
               + Decimal(str(r["commission_rub"] or 0))
               for r in rows if r["seller_base_price_rub"] is not None)


def amount(rows):
    return sum(Decimal(str(r["amount_rub"])) for r in rows)


# ───────────────────────────────────────────────────────────── A–E по одному
def test_a_normal_sale_unchanged(entities):
    rows = rows_of(entities, [SALE_A])
    assert [(r["type_id"], r["amount_rub"]) for r in rows] == [(32, -88.0), (98, -25.0)]
    assert rows[0]["seller_base_price_rub"] == 1000.0 and rows[0]["commission_rub"] == -410.0
    assert rows[1]["seller_base_price_rub"] is None                 # блок ровно один раз
    assert all(r["quantity"] is None for r in rows)                 # прежнее поведение
    assert all(r["type_id"] > 0 for r in rows)
    assert amount(rows) + econ_money(rows) == Decimal("477")


@pytest.mark.parametrize("acc", [REVERSAL_B, REVERSAL_C], ids=["B_services_null", "C_services_empty"])
def test_b_c_reversal_without_services_survives(entities, acc):
    ctl = {}
    rows = rows_of(entities, [acc], control=ctl)
    assert len(rows) == 1
    r = rows[0]
    assert r["type_id"] == entities.FIN_ECON_RETURN_TYPE_ID == -1
    assert r["operation_name"] == entities.FIN_ECON_TYPE_NAMES[-1]
    assert r["amount_rub"] == 0.0                                   # не NULL: сумма услуги = 0
    assert r["seller_base_price_rub"] == -1000.0                    # знак источника сохранён
    assert r["commission_rub"] == 410.0
    assert r["buyer_paid_price_rub"] == -700.0 and r["ozon_bonus_rub"] == -300.0
    assert r["commission_ratio"] == 0.41
    assert r["quantity"] == 1
    assert r["posting_number"] == acc["unit_number"] and r["sku"] == str(acc["posting"]["products"][0]["sku"])
    assert r["unit_number_meaning"] == "posting_number"
    assert econ_money(rows) == Decimal("-590") == Decimal(acc["total_amount"]["amount"])
    assert ctl == {"source": Decimal("-590"), "parsed": Decimal("-590")}


def test_d_economics_and_services_no_double_money(entities):
    ctl = {}
    rows = rows_of(entities, [REVERSAL_WITH_SERVICE_D], control=ctl)
    assert [(r["type_id"], r["amount_rub"]) for r in rows] == [(59, -60.0)]
    assert rows[0]["seller_base_price_rub"] == -1000.0              # блок прицепом, как раньше
    assert not [r for r in rows if r["type_id"] < 0]                # отдельной строки нет
    assert amount(rows) + econ_money(rows) == Decimal("-650")
    assert ctl["source"] == ctl["parsed"] == Decimal("-650") and "unbalanced" not in ctl


@pytest.mark.parametrize("acc", [CORRECTION_E1, CORRECTION_E2, ACQUIRING_E3],
                         ids=["E1_store_correction", "E2_logistics_recalc", "E3_acquiring_refund"])
def test_e_corrections_are_not_sale_reversals(entities, acc):
    ctl = {}
    rows = rows_of(entities, [acc], control=ctl)
    assert rows and all(r["type_id"] > 0 for r in rows)
    assert all(r["seller_base_price_rub"] is None for r in rows)
    assert amount(rows) == Decimal(acc["total_amount"]["amount"])
    assert ctl["source"] == ctl["parsed"] and "unbalanced" not in ctl


def test_sale_without_services_is_kept_as_sale_not_reversal(entities):
    acc = accrual(7000010, "590", [{"sku": 555004, "quantity": 2, "delivery": None,
                                    "commission": commission(+1)}])
    rows = rows_of(entities, [acc])
    assert [(r["type_id"], r["operation_name"], r["quantity"]) for r in rows] == [
        (-2, entities.FIN_ECON_TYPE_NAMES[-2], 2)]


@pytest.mark.parametrize("bad", [
    {"sale_amount": money("-1000"), "seller_price": money("1000")},   # цена и реализация врозь
    {"sale_amount": money("-1000"), "seller_price": money("-1000"),
     "commission": money("-410")},                                      # сторно с удержанием
])
def test_inconsistent_economics_fails_closed_without_amounts(entities, bad):
    c = commission(-1)
    c.update(bad)
    acc = accrual(7000011, "-590", [{"sku": 555005, "quantity": 1, "delivery": None, "commission": c}])
    with pytest.raises(entities.FinanceSemanticsError) as e:
        rows_of(entities, [acc])
    assert "1000" not in str(e.value) and "7000011" not in str(e.value) and "555005" not in str(e.value)


def test_zero_economics_without_services_emits_nothing(entities):
    c = commission(+1, price="0", comm="0", sale_price="0", bonus="0")
    rows = rows_of(entities, [accrual(7000012, "0", [{"sku": 1, "delivery": None, "commission": c}])])
    assert rows == []


# ───────────────────────────────────────────────────────── ключи и идемпотентность
def test_keys_unique_and_compatible_with_reject_mode(entities):
    rows = rows_of(entities, ALL)
    cols = sorted({c for r in rows for c in r})
    assert len({C.merge_key(r, KEYS) for r in rows}) == len(rows)
    assert C.validate_merge_batch("RAW_OZON_FINANCE_ACCRUAL", rows, KEYS, cols,
                                  on_duplicate_key="reject") == 0
    # синтетические типы не пересекаются со справочником Ozon (там только положительные id)
    assert {r["type_id"] for r in rows if r["type_id"] < 0} == {-1}


def test_f_repeated_ingestion_is_byte_identical(entities, captured_merges, monkeypatch):
    monkeypatch.setattr(entities, "seller_post", by_day({DAY: ALL}))
    entities.finance_accrual("rt-d1", TS, DAY, DAY)
    entities.finance_accrual("rt-d1", TS, DAY, DAY)
    (_, first, k1, kw1), (_, second, k2, kw2) = captured_merges
    assert k1 == k2 == KEYS and kw1 == kw2 == {"on_duplicate_key": "reject"}
    dump = lambda rs: [json.dumps(r, sort_keys=True, default=str) for r in rs]
    assert dump(first) == dump(second)
    assert [C.merge_key(r, KEYS) for r in first] == [C.merge_key(r, KEYS) for r in second]
    assert [r["source_payload_hash"] for r in first] == [r["source_payload_hash"] for r in second]
    # хэш строки сторно зависит от денег блока: другая сумма — другой хэш
    rev = next(r for r in first if r["accrual_id"] == 7000002)
    changed = copy.deepcopy(REVERSAL_B)
    changed["posting"]["products"][0]["commission"] = commission(-1, price="999")
    changed["total_amount"] = money("-589")
    assert rows_of(entities, [changed])[0]["source_payload_hash"] != rev["source_payload_hash"]


def test_pagination_split_does_not_change_reversal_rows(entities, captured_merges, monkeypatch):
    pages = {"": (ALL[:3], "p2"), "p2": (ALL[3:], "")}
    def seller_post(path, body):
        if path == "/v1/finance/accrual/types":
            return 200, TYPES
        chunk, nxt = pages[body["last_id"]]
        return 200, {"accruals": copy.deepcopy(chunk), "last_id": nxt}
    monkeypatch.setattr(entities, "seller_post", seller_post)
    entities.finance_accrual("rt-d1", TS, DAY, DAY)
    monkeypatch.setattr(entities, "seller_post", by_day({DAY: ALL}))
    entities.finance_accrual("rt-d1", TS, DAY, DAY)
    (_, split, _, _), (_, whole, _, _) = captured_merges
    dump = lambda rs: sorted(json.dumps(r, sort_keys=True, default=str) for r in rs)
    assert dump(split) == dump(whole)


# ───────────────────────────────────────────────────────── контроль денег суток
def test_day_control_passes_silently_on_balanced_day(entities, captured_merges, monkeypatch):
    logged = []
    monkeypatch.setattr(entities, "log", lambda **kw: logged.append(kw))
    monkeypatch.setattr(entities, "seller_post", by_day({DAY: ALL}))
    res = entities.finance_accrual("rt-d1", TS, DAY, DAY)
    assert res["received"] == len(captured_merges[0][1])
    assert logged == []                       # чистые сутки не меняют вывод журнала


def _unparsed_container_fee():
    a = copy.deepcopy(SALE_A)
    a["accrual_id"] = 7000020
    a["container_fees"] = {"fees": [{"type_id": 12, "accrued": money("-7")}]}
    a["total_amount"] = money("470")
    return a


def test_day_control_mismatch_is_loud_and_fails_entity_after_write(entities, captured_merges,
                                                                   monkeypatch):
    """Обычный режим EVETIS: строки, которые разобраны верно, пишутся (MERGE идемпотентен),
    но сущность завершается отказом — статус прогона не может остаться OK."""
    logged = []
    monkeypatch.setattr(C, "STRICT_PAGE_CAPS", False)
    monkeypatch.setattr(entities, "log", lambda **kw: logged.append(kw))
    monkeypatch.setattr(entities, "seller_post", by_day({DAY: ALL + [_unparsed_container_fee()]}))
    with pytest.raises(entities.FinanceControlError) as e:
        entities.finance_accrual("rt-d1", TS, DAY, DAY)
    assert len(captured_merges) == 1                      # записано до отказа
    assert DAY in str(e.value) and "7000020" not in str(e.value)
    assert logged == [{"event": "finance_control_mismatch", "entity": "finance_accrual",
                       "day": DAY, "accruals_unbalanced": 1, "residual_rub": "-7.00"}]


def test_day_control_mismatch_strict_mode_refuses_before_write(entities, captured_merges,
                                                               monkeypatch):
    monkeypatch.setattr(C, "STRICT_PAGE_CAPS", True)
    monkeypatch.setattr(entities, "seller_post", by_day({DAY: [_unparsed_container_fee()]}))
    with pytest.raises(entities.StrictLimitError, match="не сходятся"):
        entities.finance_accrual("rt-d1", TS, DAY, DAY)
    assert captured_merges == []


def test_old_parser_would_have_failed_the_control(entities):
    """Регрессия: без D1 сторно давало 0 строк, и контроль это видит."""
    ctl = {}
    rows = rows_of(entities, [REVERSAL_B], control=ctl)
    assert rows                                            # теперь строка есть
    without = {"source": Decimal("-590"), "parsed": Decimal(0)}   # то, что дал бы старый разбор
    assert without["source"] != without["parsed"] and ctl["source"] == ctl["parsed"]


def test_accruals_without_total_amount_are_counted_not_failed(entities):
    """Ответ без total_amount сверить нечем: счётчик, а не отказ (так устроены сценарии
    различающего прогона compat/, где total_amount нет)."""
    ctl = {}
    acc = {"accrual_id": "A1", "accrued_category": "OTHER", "unit_number": 1,
           "non_item_fee": {"type_id": 1, "accrued": {"amount": "1.5"}}}
    rows_of(entities, [acc], control=ctl)
    assert ctl == {"no_total": 1}


def test_backfill_call_signature_unchanged(entities):
    """Тенантный бэкфилл зовёт _finance_rows пятью позиционными аргументами."""
    types = {x["id"]: x["description"] for x in TYPES["accrual_types"]}
    rows = entities._finance_rows(copy.deepcopy([REVERSAL_B]), types, DAY, "rt", TS)
    assert [r["type_id"] for r in rows] == [-1]
