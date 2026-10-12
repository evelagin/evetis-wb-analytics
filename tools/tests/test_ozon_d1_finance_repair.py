"""ozon_d1_finance_repair: план ремонта RAW_OZON_FINANCE_ACCRUAL (без сети, синтетические данные).

Страницы accrual/by-day и «живой RAW» здесь выдуманы; структура повторяет ответ Ozon и колонки RAW.
"""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ozon_d1_finance_repair as R  # noqa: E402

TYPES = {32: "Логистика", 1: "Эквайринг", 59: "Обратная логистика"}


@pytest.fixture(scope="module")
def E():
    """Единственный сборщик строк; после модуля имена runtime убираются из sys.modules."""
    before_mods, before_path = set(sys.modules), list(sys.path)
    mod = R.load_builder()
    yield mod
    for name in set(sys.modules) - before_mods:
        sys.modules.pop(name, None)
    sys.path[:] = before_path


def m(v):
    return {"amount": str(v), "currency": "RUB"}


def comm(sign, price=1000, c=410):
    return {"seller_price": m(price * sign), "sale_price": m(700 * sign), "sale_commission": m(-c * sign),
            "commission": m(-c * sign), "commission_ratio": 'value:"0.410000"', "sale_amount": m(price * sign),
            "coinvestment": m(0), "bonus": m(300 * sign)}


def sale(aid, pn, sku, day):
    return {"accrual_id": aid, "date": day, "total_amount": m(502), "unit_number": pn, "accrued_category": "POSTING",
            "posting": {"products": [{"sku": sku, "quantity": 1, "commission": comm(+1),
                                      "delivery": {"services": [{"type_id": 32, "accrued": m(-88)}]}}]},
            "item_fees": None, "non_item_fee": None, "container_fees": None}


def reversal(aid, pn, sku, day):
    return {"accrual_id": aid, "date": day, "total_amount": m(-590), "unit_number": pn, "accrued_category": "POSTING",
            "posting": {"products": [{"sku": sku, "quantity": 1, "delivery": None, "commission": comm(-1)}]},
            "item_fees": None, "non_item_fee": None, "container_fees": None}


DAYS = {"2026-01-10": [sale(101, "111-1-1", 9001, "2026-01-10")],
        "2026-01-11": [],
        "2026-01-12": [reversal(102, "111-1-1", 9001, "2026-01-12")]}


def write_pages(tmp_path, days=DAYS):
    idx = []
    (tmp_path / "pages").mkdir(exist_ok=True)
    for d, acc in days.items():
        f = tmp_path / "pages" / f"{d}_p1.json"
        f.write_text(json.dumps({"accruals": acc, "last_id": ""}), encoding="utf-8")
        idx.append({"date": d, "page": 1, "last_id_in": "", "last_id_out": "", "final": True,
                    "evidence_file": f"pages/{d}_p1.json", "sha256": hashlib.sha256(f.read_bytes()).hexdigest(),
                    "fetched_at": "2026-10-11T20:00:00.000000Z"})
    p = tmp_path / "index.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in idx), encoding="utf-8")
    return p


def live_from(E, days, drop_types=()):
    """«Живой RAW» = то, что записал бы старый разбор: всё, кроме строк с type_id в drop_types."""
    out = []
    for d, acc in days.items():
        for r in E._finance_rows(copy.deepcopy(acc), TYPES, d, "rt-old", "2026-01-01T00:00:00Z"):
            if r["type_id"] in drop_types:
                continue
            out.append({"event_date": d, "accrual_id": str(r["accrual_id"]), "type_id": str(r["type_id"]),
                        "sku": r["sku"], "posting_number": r["posting_number"], "amount_rub": str(r["amount_rub"]),
                        "sp": None if r["seller_base_price_rub"] is None else str(r["seller_base_price_rub"]),
                        "cm": None if r["commission_rub"] is None else str(r["commission_rub"]),
                        "ingestion_run_id": "rt-old"})
    return out


def plan(E, tmp_path, days=DAYS, live=None):
    pages = R.load_pages(write_pages(tmp_path, days))
    return R.build_plan(E, pages, TYPES, live if live is not None else live_from(E, days, drop_types=(-1, -2)))


def test_plan_adds_only_proven_reversal_and_conserves_money(E, tmp_path):
    p = plan(E, tmp_path)
    c = p["core"]
    assert c["add"]["rows"] == 1 and c["add"]["type_ids"] == {-1: 1} and c["deletes"] == 0
    assert c["add"]["money_rub"] == "-590.00"
    assert c["add"]["earliest_event_date"] == c["add"]["latest_event_date"] == "2026-01-12"
    assert c["duplicate_key_proof"] == {"plan_keys_unique": True, "plan_keys_in_live_raw": 0}
    assert c["rows_after_repair"] == c["live_raw"]["rows"] + 1
    assert c["conservation"]["2026-01"]["residual"] == "0.00"
    assert c["conservation"]["2026-01"]["source"] == "-88.00"
    assert c["monthly_delta"] == {"2026-01": {"rows": 1, "seller_price_x_qty_rub": "-1000.00",
                                              "commission_rub": "410.00", "money_rub": "-590.00"}}
    (row,) = p["rows"]
    assert row["type_id"] == -1 and row["amount_rub"] == 0.0 and row["ingestion_run_id"] == R.RUN_PREFIX + p["fingerprint"][:16]
    assert set(row) == set(R.COLUMNS)


def test_fingerprint_is_deterministic_and_bound_to_live_state(E, tmp_path):
    a, b = plan(E, tmp_path), plan(E, tmp_path)
    assert a["fingerprint"] == b["fingerprint"] and len(a["fingerprint"]) == 64
    changed = live_from(E, DAYS, drop_types=(-1,))
    changed[0]["ingestion_run_id"] = "rt-other"              # не деньги и не ключ — отпечаток тот же
    assert plan(E, tmp_path, live=changed)["fingerprint"] == a["fingerprint"]
    extra = DAYS | {"2026-01-13": [sale(103, "222-1-1", 9002, "2026-01-13")]}
    assert plan(E, tmp_path, days=extra)["fingerprint"] != a["fingerprint"]


def test_already_present_reversal_is_not_added_again(E, tmp_path):
    p = plan(E, tmp_path, live=live_from(E, DAYS))
    assert p["core"]["add"]["rows"] == 0 and p["core"]["already_present_synthetic"] == 1 and p["rows"] == []


def test_late_published_service_rows_are_reported_not_repaired(E, tmp_path):
    late = {"accrual_id": 150, "date": "2026-01-11", "total_amount": m(-5), "unit_number": "111-1-1",
            "accrued_category": "ITEM", "posting": None, "non_item_fee": None, "container_fees": None,
            "item_fees": {"fees": [{"sku": 9001, "fees": [{"type_id": 1, "accrued": m(-5)}]}]}}
    days = DAYS | {"2026-01-11": [late]}
    p = plan(E, tmp_path, days=days, live=live_from(E, DAYS, drop_types=(-1,)))
    assert p["core"]["add"]["rows"] == 1                    # только сторно
    assert p["core"]["late_published_not_repaired"] == {"rows": 1, "money_rub": "-5.00"}
    assert p["core"]["conservation"]["2026-01"]["residual"] == "0.00"


@pytest.mark.parametrize("fault,msg", [
    ("raw_only", "нет в источнике"),
    ("amount", "amount_rub"),
    ("econ", "экономический блок"),
    ("dup", "повтор ключа"),
])
def test_plan_refuses_unproven_window(E, tmp_path, fault, msg):
    live = live_from(E, DAYS, drop_types=(-1,))
    if fault == "raw_only":
        live.append(dict(live[0], accrual_id="999"))
    elif fault == "amount":
        live[0]["amount_rub"] = "-87"
    elif fault == "econ":
        live[0]["sp"] = "999"
    else:
        live.append(dict(live[0]))
    with pytest.raises(R.PlanRefused, match=msg):
        plan(E, tmp_path, live=live)


def test_plan_refuses_reversal_without_original_sale(E, tmp_path):
    days = {"2026-01-12": [reversal(102, "111-1-1", 9001, "2026-01-12")]}
    with pytest.raises(R.PlanRefused, match="без исходной продажи"):
        plan(E, tmp_path, days=days)


def test_plan_refuses_unbalanced_day(E, tmp_path):
    days = copy.deepcopy(DAYS)
    days["2026-01-10"][0]["container_fees"] = {"fees": [{"type_id": 12, "accrued": m(-7)}]}
    days["2026-01-10"][0]["total_amount"] = m(495)
    with pytest.raises(R.PlanRefused, match="контроль денег"):
        plan(E, tmp_path, days=days)


def test_pages_tamper_and_gaps_are_refused(E, tmp_path):
    idx = write_pages(tmp_path)
    (tmp_path / "pages" / "2026-01-10_p1.json").write_text('{"accruals": [], "last_id": ""}')
    with pytest.raises(R.PlanRefused, match="sha256"):
        R.load_pages(idx)
    other = tmp_path / "gap"
    other.mkdir()
    gap = {k: v for k, v in DAYS.items() if k != "2026-01-11"}
    with pytest.raises(R.PlanRefused, match="не подряд"):
        R.load_pages(write_pages(other, gap))


def test_apply_sql_is_insert_only_and_rejects_unsafe_values(E, tmp_path):
    p = plan(E, tmp_path)
    names = set(E.FIN_ECON_TYPE_NAMES.values())
    run_id = p["rows"][0]["ingestion_run_id"]
    sql = R.apply_sql(p["rows"], run_id, names)
    assert "WHEN NOT MATCHED THEN INSERT" in sql and "WHEN MATCHED" not in sql
    assert "DELETE" not in sql and "UPDATE" not in sql and "ASSERT" in sql
    bad = copy.deepcopy(p["rows"])
    bad[0]["posting_number"] = "1'); DROP TABLE x; --"
    with pytest.raises(R.PlanRefused):
        R.apply_sql(bad, run_id, names)
    bad = copy.deepcopy(p["rows"])
    bad[0]["operation_name"] = "Логистика"
    with pytest.raises(R.PlanRefused):
        R.apply_sql(bad, run_id, names)


def test_rollback_deletes_exactly_plan_keys_of_repair_run(E, tmp_path):
    p = plan(E, tmp_path)
    run_id = p["rows"][0]["ingestion_run_id"]
    sql = R.rollback_sql([list(R.key(r)) for r in p["rows"]], run_id)
    assert f"ingestion_run_id = '{run_id}'" in sql and "type_id < 0" in sql and "STRUCT(102 AS a, -1 AS t, '9001' AS s)" in sql
    with pytest.raises(R.PlanRefused):
        R.rollback_sql([[102, -1, "9001"]], "rt-production-run")


def test_cli_guards(tmp_path):
    with pytest.raises(SystemExit, match="plan-fingerprint"):
        R.main(["--project", R.PROJECT, "--apply"])
    with pytest.raises(SystemExit, match="не совпадает"):
        R.main(["--project", "other-project"])
    with pytest.raises(SystemExit, match="внутри репозитория"):
        R.outside_repo(str(R.ROOT / "x.jsonl"), "--pages-index")


def test_ingestion_window_guard():
    utc = dt.timezone.utc
    assert not R.outside_ingestion_window(dt.datetime(2026, 10, 12, 3, 30, tzinfo=utc))
    assert R.outside_ingestion_window(dt.datetime(2026, 10, 12, 4, 30, tzinfo=utc))
    assert R.outside_ingestion_window(dt.datetime(2026, 10, 12, 3, 14, tzinfo=utc))


def test_scope_is_limited_to_days_older_than_loading_window(E, tmp_path):
    pages = R.load_pages(write_pages(tmp_path))
    now = dt.datetime(2026, 2, 13, 12, 0, tzinfo=dt.timezone.utc)          # МСК 13.02 − 31 = 13.01
    assert R.default_through(now) == "2026-01-13"
    assert sorted(R.scope_pages(pages, "2026-01-11", now)) == ["2026-01-10", "2026-01-11"]
    with pytest.raises(R.PlanRefused, match="внутри окна загрузки"):
        R.scope_pages(pages, "2026-01-12", dt.datetime(2026, 2, 10, tzinfo=dt.timezone.utc))
    with pytest.raises(R.PlanRefused, match="вне сохранённых"):
        R.scope_pages(pages, "2025-12-31", now)


def test_late_publication_after_scope_does_not_break_plan(E, tmp_path):
    """Поздняя публикация в свежих сутках (RAW там ещё меняется) вне области не роняет план и не
    меняет отпечаток."""
    scoped = {k: v for k, v in DAYS.items() if k <= "2026-01-12"}
    base = plan(E, tmp_path, days=scoped)
    late = tmp_path / "late"
    late.mkdir()
    pages = R.load_pages(write_pages(late, DAYS | {"2026-01-13": [sale(103, "222-1-1", 9002, "2026-01-13")]}))
    now = dt.datetime(2026, 2, 13, 12, 0, tzinfo=dt.timezone.utc)
    p = R.build_plan(E, R.scope_pages(pages, "2026-01-12", now), TYPES, live_from(E, scoped, drop_types=(-1,)))
    assert p["fingerprint"] == base["fingerprint"] and p["core"]["add"]["rows"] == 1


def test_quarantined_accrual_refuses_plan(E, tmp_path):
    days = copy.deepcopy(DAYS)
    days["2026-01-12"][0]["posting"]["products"][0]["commission"]["seller_price"] = m(1000)   # знаки врозь
    with pytest.raises(R.PlanRefused, match="карантин"):
        plan(E, tmp_path, days=days)


def test_readback_compares_content_not_only_keys(E, tmp_path):
    p = plan(E, tmp_path)
    got = [{c: (None if v is None else str(v)) for c, v in r.items()} for r in p["rows"]]
    assert R.readback_diff(got, p["rows"]) == []
    got[0]["seller_base_price_rub"] = "-999"
    assert R.readback_diff(got, p["rows"]) == [R.key(p["rows"][0])]
    assert R.readback_diff([], p["rows"]) == [R.key(p["rows"][0])]


def test_rollback_asserts_exact_count_before_delete(E, tmp_path):
    p = plan(E, tmp_path)
    sql = R.rollback_sql([list(R.key(r)) for r in p["rows"]], p["rows"][0]["ingestion_run_id"])
    assert sql.index("ASSERT") < sql.index("DELETE") and ") = 1 AS 'OZD1_ROLLBACK: по ключам" in sql


def test_tail_after_through_is_reported_softly(E):
    """Хвост после through не блокирует план: несошедшиеся сутки и карантин — только в журнал."""
    bad = copy.deepcopy(DAYS["2026-01-10"])
    bad[0]["container_fees"] = {"fees": [{"type_id": 12, "accrued": m(-7)}]}
    bad[0]["total_amount"] = m(495)
    quarantined = copy.deepcopy(DAYS["2026-01-12"])
    quarantined[0]["posting"]["products"][0]["commission"]["seller_price"] = m(1000)
    tail = {"2026-02-01": {"accruals": bad, "pages": []},
            "2026-02-02": {"accruals": DAYS["2026-01-12"], "pages": []},
            "2026-02-03": {"accruals": quarantined, "pages": []}}
    assert R.tail_report(E, tail, TYPES) == {"days": 3, "synthetic_rows": 1, "unbalanced_days": 1,
                                             "quarantined": 1, "no_total": 0}
