"""Регрессия production-трансформации finance_accrual на реальных ответах Ozon.

Фикстура fixtures/finance_accrual_sample.json — 25 начислений из аудита
2026-09-16: по одному на каждую комбинацию ветвей emit() (23 сигнатуры из 1 428
начислений), идентификаторы псевдонимизированы, суммы и структура не тронуты.

Независимый контроль — поле total_amount самого Ozon. На всех 1 428 начислениях
аудита выполняется:
    Σ emitted amount_rub = total_amount − (commission.sale_amount + commission.sale_commission)
(выручка продажи в amount_rub не пишется — она в колонках экономического блока).

Полный архив (92 дня, вне Git) проверяется отдельным тестом, если задана
переменная OZON_AUDIT_FINANCE_DIR.
"""

from __future__ import annotations

import glob
import json
import os
from collections import Counter, defaultdict

import pytest

import common as C
from conftest import FIXTURES

KEYS = ["accrual_id", "type_id", "sku"]
TS = "2026-09-16T06:30:00+03:00"


def load_sample():
    return json.loads((FIXTURES / "finance_accrual_sample.json").read_text(encoding="utf-8"))


def fake_by_day(days, types, pages_per_day=1):
    """Транспорт accrual/by-day; при pages_per_day=2 день режется на две страницы."""
    def seller_post(path, body):
        if path == "/v1/finance/accrual/types":
            return 200, types
        acc = days.get(body["date"], [])
        if pages_per_day == 1 or len(acc) < 2:
            return 200, {"accruals": acc, "last_id": ""}
        half = len(acc) // 2
        if not body["last_id"]:
            return 200, {"accruals": acc[:half], "last_id": f"{body['date']}-p2"}
        return 200, {"accruals": acc[half:], "last_id": ""}
    return seller_post


def ozon_control(accrual):
    """Сумма, которую должны дать строки начисления, по данным самого Ozon."""
    total = float(accrual["total_amount"]["amount"])
    for p in (accrual.get("posting") or {}).get("products") or []:
        c = p.get("commission") or {}
        if c:
            total -= float((c.get("sale_amount") or {}).get("amount") or 0)
            total -= float((c.get("sale_commission") or {}).get("amount") or 0)
    return total


def run(entities, captured_merges, monkeypatch, days, types, frm, to, pages_per_day=1):
    monkeypatch.setattr(entities, "seller_post", fake_by_day(days, types, pages_per_day))
    entities.finance_accrual("rt-regression", TS, frm, to)
    (table, rows, keys, kw), = captured_merges
    return table, rows, keys, kw


def span(days):
    return min(days), max(days)


# ------------------------------------------------------------ фикстура
def test_sample_covers_every_emit_branch():
    s = load_sample()
    acc = [a for d in s["days"].values() for a in d]
    cats = Counter(a["accrued_category"] for a in acc)
    assert set(cats) == {"NON_ITEM", "ITEM", "POSTING"}
    assert any(a.get("non_item_fee") for a in acc)
    assert any(len({f["sku"] for f in (a.get("item_fees") or {}).get("fees") or []}) > 1
               for a in acc), "нет начисления ITEM с несколькими SKU"
    prods = [p for a in acc for p in (a.get("posting") or {}).get("products") or []]
    assert any(p.get("commission") for p in prods)
    assert any(not p.get("commission") for p in prods)
    assert any(not ((p.get("delivery") or {}).get("services")) for p in prods), \
        "нет продукта без services"
    assert any(a.get("unit_number") is None for a in acc)


def test_sample_regression(entities, captured_merges, monkeypatch):
    s = load_sample()
    table, rows, keys, kw = run(entities, captured_merges, monkeypatch,
                                s["days"], s["accrual_types"], *span(s["days"]))

    # грейн и режим
    assert table == "RAW_OZON_FINANCE_ACCRUAL"
    assert keys == KEYS
    assert kw == {"on_duplicate_key": "reject"}

    # эталон, снятый с кода 0abc4c4 на этой фикстуре: 29 строк, −1 508.97 ₽
    assert len(rows) == 29
    assert len({C.merge_key(r, keys) for r in rows}) == len(rows)
    assert round(sum(r["amount_rub"] or 0 for r in rows), 2) == -1508.97

    # независимый контроль Ozon — по каждому начислению
    by_acc = defaultdict(float)
    for r in rows:
        by_acc[r["accrual_id"]] += r["amount_rub"] or 0
    for accruals in s["days"].values():
        for a in accruals:
            assert by_acc[a["accrual_id"]] == pytest.approx(ozon_control(a), abs=0.005), \
                a["accrual_id"]

    # проверка партии проходит: повторов ключа нет
    cols = sorted({c for r in rows for c in r})
    assert C.validate_merge_batch(table, rows, keys, cols, **kw) == 0


def test_sample_economics_block_emitted_once_per_product(entities, captured_merges, monkeypatch):
    s = load_sample()
    _, rows, _, _ = run(entities, captured_merges, monkeypatch,
                        s["days"], s["accrual_types"], *span(s["days"]))
    econ = Counter((r["accrual_id"], r["sku"]) for r in rows
                   if r["seller_base_price_rub"] is not None)
    assert econ and all(n == 1 for n in econ.values())


def test_pagination_split_does_not_change_result(entities, captured_merges, monkeypatch):
    s = load_sample()
    _, one_page, _, _ = run(entities, captured_merges, monkeypatch,
                            s["days"], s["accrual_types"], *span(s["days"]))
    captured_merges.clear()
    _, two_pages, _, _ = run(entities, captured_merges, monkeypatch,
                             s["days"], s["accrual_types"], *span(s["days"]), pages_per_day=2)
    strip = lambda rs: sorted(json.dumps(r, sort_keys=True, default=str) for r in rs)
    assert strip(one_page) == strip(two_pages)


def test_overlapping_page_is_rejected_not_collapsed(entities, captured_merges, monkeypatch):
    """Если Ozon вернёт одно начисление на двух страницах, строки совпадут побайтно.
    В финансах это не схлопывается молча: вдруг это два реальных начисления."""
    s = load_sample()
    day = next(d for d, acc in s["days"].items() if acc)
    days = {day: s["days"][day] + s["days"][day][:1]}
    table, rows, keys, kw = run(entities, captured_merges, monkeypatch,
                                days, s["accrual_types"], day, day)
    cols = sorted({c for r in rows for c in r})
    with pytest.raises(C.MergeKeyConflictError, match="режим reject"):
        C.validate_merge_batch(table, rows, keys, cols, **kw)


def test_same_key_different_amount_fails_closed(entities, captured_merges, monkeypatch):
    """Гипотеза аудита: несколько services одного type_id на один SKU. Сейчас такого
    в данных нет; если появится — загрузка падает, а не теряет сумму."""
    s = load_sample()
    acc = next(a for d in s["days"].values() for a in d
               if any(((p.get("delivery") or {}).get("services"))
                      for p in (a.get("posting") or {}).get("products") or []))
    acc = json.loads(json.dumps(acc))
    svc = acc["posting"]["products"][0]["delivery"]["services"]
    svc.append({"type_id": svc[0]["type_id"], "accrued": {"amount": -1.23, "currency": "RUB"}})
    day = acc["date"][:10] if acc.get("date") else next(iter(s["days"]))
    table, rows, keys, kw = run(entities, captured_merges, monkeypatch,
                                {day: [acc]}, s["accrual_types"], day, day)
    cols = sorted({c for r in rows for c in r})
    with pytest.raises(C.MergeKeyConflictError) as e:
        C.validate_merge_batch(table, rows, keys, cols, **kw)
    msg = str(e.value)
    assert "RAW_OZON_FINANCE_ACCRUAL" in msg and "rows=2" in msg and "режим reject" in msg
    assert "-1.23" not in msg                                   # сумма
    assert str(acc["accrual_id"]) not in msg                    # значения ключа слияния
    assert str(acc["posting"]["products"][0]["sku"]) not in msg
    assert str(acc.get("unit_number")) not in msg               # идентификатор отправления
    assert f"key_fp={C.merge_key_fingerprint(C.merge_key(rows[0], keys))}" in msg


# ------------------------------------------------------------ полный архив
AUDIT_DIR = os.environ.get("OZON_AUDIT_FINANCE_DIR")


@pytest.mark.skipif(not AUDIT_DIR, reason="OZON_AUDIT_FINANCE_DIR не задан: архив аудита вне Git")
def test_full_audit_archive(entities, captured_merges, monkeypatch):
    days = {}
    for f in sorted(glob.glob(os.path.join(AUDIT_DIR, "accrual_by_day_*.json"))):
        d = json.load(open(f, encoding="utf-8"))
        days[d["date"]] = d["accruals"]
    types = json.load(open(os.path.join(AUDIT_DIR, "accrual_types.json"), encoding="utf-8"))["data"]
    table, rows, keys, kw = run(entities, captured_merges, monkeypatch, days, types, *span(days))

    accruals = [a for acc in days.values() for a in acc]
    assert (len(days), len(accruals)) == (92, 1428)
    assert len(rows) == 1702
    assert len({C.merge_key(r, keys) for r in rows}) == 1702
    assert round(sum(r["amount_rub"] or 0 for r in rows), 2) == -131192.33
    by_acc = defaultdict(float)
    for r in rows:
        by_acc[r["accrual_id"]] += r["amount_rub"] or 0
    mismatches = [a["accrual_id"] for a in accruals
                  if abs(by_acc[a["accrual_id"]] - ozon_control(a)) > 0.005]
    assert mismatches == []
    cols = sorted({c for r in rows for c in r})
    assert C.validate_merge_batch(table, rows, keys, cols, **kw) == 0
