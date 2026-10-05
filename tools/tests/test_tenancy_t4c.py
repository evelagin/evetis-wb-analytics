"""Tenancy T4-c — пакет SQL арендатора: состав, семантика, отсутствие эмпирики EVETIS.

Офлайн. Компиляция в BigQuery проверяется отдельно командой
`python tools/tenancy/sql_package.py dryrun <tenant_id>` (живой dry-run, 0 байт).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
import sqlglot
from sqlglot import exp

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from tools.tenancy import sql_package as SP  # noqa: E402
from tools.tenancy import synthetic as SY  # noqa: E402

C1 = SY.fixture_contract("client_001")


def _order():
    order, findings = SP.load_package(SP.PACKAGE_DIR, C1)
    assert findings == [], findings
    return {(o.dataset_key, o.name): o for o in order}


def _sql(ds, name):
    return (SP.PACKAGE_DIR / ds / f"{name}.sql").read_text(encoding="utf-8")


def test_package_has_the_designed_objects_and_only_package_layers():
    objs = _order()
    mart = {n for ds, n in objs if ds == "ozon_mart"}
    ops = {n for ds, n in objs if ds == "tenant_ops"}
    assert {"NORM_OZON_POSTING_LINE", "NORM_OZON_ACCRUAL", "FACT_OZON_SALES_DAILY",
            "FACT_OZON_SKU_ECONOMICS_DAILY", "FACT_OZON_STORE_COSTS_DAILY", "SNAP_OZON_STOCK",
            "SNAP_OZON_PRICE", "DIM_OZON_PRODUCT", "ECON_TENANT_COGS", "DIM_OZON_ACCRUAL_TYPE"} <= mart
    assert {"V_SELLER_BINDING_STATUS", "V_ENTITY_COVERAGE", "V_DQ_UNRESOLVED_ACCRUALS",
            "V_DQ_SETTLEMENT_ANOMALIES", "V_DQ_COGS_OVERLAPS", "V_TENANT_STATE_CURRENT"} <= ops


def test_accrual_taxonomy_is_complete_unique_and_class_bounded():
    tree = sqlglot.parse_one(_sql("ozon_mart", "DIM_OZON_ACCRUAL_TYPE"), read="bigquery")
    ids, classes = [], set()
    for st in tree.find_all(exp.Struct):
        vals = {e.this.name: e.expression for e in st.expressions if isinstance(e, exp.PropertyEQ)}
        ids.append(int(vals["type_id"].this))
        classes.add(vals["accrual_class"].this)
    assert len(ids) == 33 and len(set(ids)) == 33
    assert classes == {"PROMOTION_BILLING", "PROMOTION_SERVICES", "SUBSCRIPTION", "LOGISTICS", "LAST_MILE",
                       "STORAGE", "ACQUIRING", "RETURN_LOGISTICS", "CANCELLATION_COST",
                       "OTHER_MARKETPLACE_COST", "COMPENSATION"}


def test_cogs_comes_only_from_tenant_reference_data():
    objs = _order()
    users = {k for k, o in objs.items() if ("ref", "REF_COGS") in o.refs}
    assert users == {("ozon_mart", "ECON_TENANT_COGS")}


@pytest.mark.parametrize("name", ["FACT_OZON_SKU_ECONOMICS_DAILY", "NORM_OZON_POSTING_SETTLEMENT",
                                  "FACT_OZON_SALES_DAILY", "FACT_OZON_STORE_COSTS_DAILY"])
def test_economic_facts_contain_no_calibration_constants(name):
    tree = sqlglot.parse_one(_sql("ozon_mart", name), read="bigquery")
    nums = {lit.this for lit in tree.find_all(exp.Literal) if not lit.is_string}
    assert nums <= {"0", "1"}, (name, nums)


def test_marketplace_day_is_moscow_not_utc():
    s = _sql("ozon_mart", "NORM_OZON_POSTING_LINE")
    assert "DATE(p.created_at, 'Europe/Moscow') AS order_date_msk" in s
    assert "p.order_date," not in s                       # UTC-дата RAW наружу не выдаётся


@pytest.mark.parametrize("marker", ["V_OZON_CIS_BUYOUT", "V_PRODUCT_COGS_EFFECTIVE", "REF_COST_BATCH",
                                    "REF_SKU_COGS_HISTORY", "FCT_OZON_SKU_PNL", "V_OZON_SKU_CURRENT_TARIFF",
                                    "sp_unit", "buyout_proceeds"])
def test_no_evetis_mart_objects_or_heuristics_in_package(marker):
    for p in SP.PACKAGE_DIR.glob("*/*.sql"):
        assert marker not in p.read_text(encoding="utf-8"), (p.name, marker)


def test_real_package_renders_identically_for_two_tenants_except_project(tmp_path, monkeypatch):
    from tools.tenancy import registry as R
    monkeypatch.setattr(R, "terraform_inputs", lambda t: SY.fixture_contract(t))
    a = SP.render("client_001", tmp_path / "a")
    b = SP.render("client_002", tmp_path / "b")
    assert [o["name"] for o in a["objects"]] == [o["name"] for o in b["objects"]]
    for o in a["objects"]:
        ta = (tmp_path / "a" / o["dataset"] / f"{o['name']}.sql").read_text(encoding="utf-8")
        tb = (tmp_path / "b" / o["dataset"] / f"{o['name']}.sql").read_text(encoding="utf-8")
        assert ta.replace("mpa-t-client-001", "P") == tb.replace("mpa-t-client-002", "P"), o["name"]


def test_compile_queries_are_self_contained_for_offline_contract():
    qs = SP.compile_queries(C1, live_tables=set())
    assert len(qs) == len(_order())
    for name, q in qs:
        assert SP.TEMPLATE_PROJECT not in q and "mpa-t-client-001" not in q, name   # всё — CTE-заглушки
        sqlglot.parse_one(q, read="bigquery")


def test_semantics_document_lists_every_package_object():
    doc = (REPO / "docs" / "architecture" / "TENANT_SQL_SEMANTICS.md").read_text(encoding="utf-8")
    for (ds, name) in _order():
        assert f"`{ds}.{name}`" in doc, name


# ═══════════════════════════════════════ ревью T4: исполнение финансовых представлений
from tools.tests import tenancy_sql_harness as H  # noqa: E402


def _taxonomy() -> dict[int, dict]:
    tree = sqlglot.parse_one(_sql("ozon_mart", "DIM_OZON_ACCRUAL_TYPE"), read="bigquery")
    out = {}
    for st in tree.find_all(exp.Struct):
        vals = {e.this.name: e.expression.this for e in st.expressions if isinstance(e, exp.PropertyEQ)}
        out[int(vals["type_id"])] = vals
    return out


ACC_COLS = ["event_date", "accrual_id", "type_id", "operation_name", "accrued_category", "posting_number", "sku",
            "amount_rub", "commission_rub", "seller_base_price_rub", "buyer_paid_price_rub", "ozon_bonus_rub",
            "ozon_coinvestment_rub", "extracted_at", "ingestion_run_id"]
LINE_COLS = ["posting_number", "sku", "order_date_msk", "quantity", "price_rub", "is_delivered", "is_cancelled"]
COGS_COLS = ["internal_sku", "effective_from", "effective_to", "product_cogs_rub"]


def acc(aid, type_id, amount, posting="P1", sku="S1", sp=None, cm=None, day="2026-01-10", ext="t1"):
    return (day, aid, type_id, None, None, posting, sku, amount, cm, sp, None, None, None, ext, "r")


def line(posting="P1", sku="S1", qty=1, price=1100, delivered=True, day="2026-01-05"):
    return (posting, sku, day, qty, price, 1 if delivered else 0, 0 if delivered else 1)


def _finance(accruals, lines=None, cogs=None):
    tax = _taxonomy()
    db = H.database({
        "RAW_OZON_FINANCE_ACCRUAL": (ACC_COLS, accruals),
        "DIM_OZON_ACCRUAL_TYPE": (["type_id", "ozon_type_name", "accrual_class", "classification_basis"],
                                  [(i, v["ozon_type_name"], v["accrual_class"], v["classification_basis"])
                                   for i, v in tax.items()]),
        "NORM_OZON_POSTING_LINE": (LINE_COLS, lines if lines is not None else [line()]),
        "DIM_OZON_PRODUCT": (["sku", "internal_sku", "sku_joinable"], [("S1", "A1", 1), ("S2", "A2", 1)]),
        "ECON_TENANT_COGS": (COGS_COLS, cogs if cogs is not None else [("A1", "2026-01-01", "9999-12-31", 200)]),
    })
    for name in ("NORM_OZON_ACCRUAL", "NORM_OZON_POSTING_SETTLEMENT"):
        H.materialize(db, "ozon_mart", name)
    econ = H.query(db, "ozon_mart", "FACT_OZON_SKU_ECONOMICS_DAILY")
    store = H.query(db, "ozon_mart", "FACT_OZON_STORE_COSTS_DAILY")
    norm = H.query(db, "ozon_mart", "NORM_OZON_ACCRUAL")
    return econ, store, norm


SALE = [acc(1, 32, -100, sp=1000, cm=-300), acc(2, 1, -15)]


def test_contribution_adds_ozon_signed_commission_and_costs():
    econ, store, _ = _finance(SALE)
    (r,) = econ
    assert (r["seller_revenue_rub"], r["commission_rub"], r["logistics_rub"], r["acquiring_rub"]) == (1000, -300, -100, -15)
    assert r["contribution_pre_cogs_rub"] == 585                      # 1000 − 300 − 100 − 15
    assert r["product_cogs_rub"] == 200 and r["contribution_after_cogs_rub"] == 385
    assert (r["revenue_basis"], r["taxonomy_status"], r["cogs_coverage"]) == ("SETTLED", "CLASSIFIED", "COMPLETE")
    assert store == []


@pytest.mark.parametrize("extra,why", [
    ([acc(3, 999, 50), acc(4, 998, -50)], "неизвестные типы, в сумме ноль"),
    ([acc(3, 999, -7)], "неизвестный тип"),
    ([acc(3, 32, None)], "пустая сумма известного типа"),
])
def test_unresolved_accruals_block_the_result(extra, why):
    (r,), _, _ = _finance(SALE + extra)
    assert r["taxonomy_status"] == "UNRESOLVED", why
    assert r["contribution_pre_cogs_rub"] is None and r["contribution_after_cogs_rub"] is None, why


def test_unknown_type_with_zero_amount_is_visible_but_not_blocking():
    (r,), _, norm = _finance(SALE + [acc(3, 999, 0)])
    assert r["contribution_pre_cogs_rub"] == 585 and r["unresolved_accruals"] == 0
    assert [n["accrual_class"] for n in norm if n["type_id"] == 999] == ["UNCLASSIFIED"]


@pytest.mark.parametrize("accruals,basis", [
    ([acc(2, 1, -15)], "NOT_SETTLED"),                                              # нет экономического блока
    ([acc(1, 32, -100, sp=1000, cm=-300), acc(2, 29, -5, sp=1000, cm=-300)], "NOT_SETTLED"),  # два блока
    ([acc(1, 32, -100, sp=1000, cm=None)], "NOT_SETTLED"),                           # блок без комиссии
])
def test_unsettled_postings_publish_no_result(accruals, basis):
    (r,), _, _ = _finance(accruals)
    assert r["revenue_basis"] == basis and r["unsettled_postings"] == 1
    assert r["contribution_pre_cogs_rub"] is None and r["contribution_after_cogs_rub"] is None
    for col in ("commission_rub", "logistics_rub", "acquiring_rub", "return_logistics_rub", "promotion_rub",
                "other_costs_rub"):
        assert r[col] is None, col                                     # до расчёта — не ноль


def test_duplicate_raw_rows_are_counted_once():
    (r,), _, _ = _finance(SALE + [acc(2, 1, -15, ext="t0")])            # повтор ключа, старая выгрузка
    assert r["acquiring_rub"] == -15 and r["contribution_pre_cogs_rub"] == 585


def test_late_correction_is_attributed_to_the_order_day():
    (r,), _, _ = _finance(SALE + [acc(3, 32, -20, day="2026-02-20")])
    assert r["order_date_msk"] == "2026-01-05" and r["logistics_rub"] == -120 and r["contribution_pre_cogs_rub"] == 565


@pytest.mark.parametrize("cogs,coverage", [
    ([], "NOT_AVAILABLE"),
    ([("A1", "2026-01-01", "9999-12-31", 200), ("A1", "2026-01-03", "9999-12-31", 250)], "NOT_AVAILABLE"),
])
def test_missing_or_ambiguous_cogs_never_guesses_or_duplicates(cogs, coverage):
    (r,), _, _ = _finance(SALE, cogs=cogs)
    assert r["delivered_units"] == 1 and r["seller_revenue_rub"] == 1000     # строка не размножена
    assert r["cogs_coverage"] == coverage and r["product_cogs_rub"] is None
    assert r["contribution_pre_cogs_rub"] == 585 and r["contribution_after_cogs_rub"] is None


def test_partial_cogs_coverage_publishes_no_after_cogs_result():
    econ, _, _ = _finance(SALE + [acc(5, 32, -100, posting="P2", sp=1000, cm=-300)],
                          lines=[line(), line(posting="P2", day="2026-01-05")],
                          cogs=[("A1", "2026-01-01", "2026-01-04", 200)])
    (r,) = econ
    assert r["cogs_coverage"] == "NOT_AVAILABLE" and r["contribution_after_cogs_rub"] is None


def test_returned_delivery_publishes_no_result():
    (r,), _, _ = _finance(SALE + [acc(3, 59, -60)])
    assert r["postings_with_return_costs"] == 1 and r["contribution_pre_cogs_rub"] is None


def test_every_accrual_lands_exactly_once_in_economics_or_store_costs():
    accruals = SALE + [
        acc(10, 54, -40), acc(11, 25, 700), acc(12, 79, -8),              # продвижение, компенсация, хранение по доставленному
        acc(20, 6, -80, posting="P9"),                                    # отменённое отправление
        acc(21, 32, -30, posting="PX"),                                   # отправление вне загруженной истории
        acc(22, 32, -5, posting="P1", sku="S2"),                          # доставленное отправление, другой SKU
        acc(30, 52, -1990, posting=None, sku=None),                       # подписка магазина
        acc(31, 79, -12, posting=None, sku="S1"),                         # хранение товара
        acc(32, 999, -3, posting=None, sku=None),                         # неизвестный тип уровня магазина
    ]
    econ, store, norm = _finance(accruals, lines=[line(), line(posting="P9", delivered=False)])
    econ_costs = sum(r[c] for r in econ for c in ("logistics_rub", "acquiring_rub", "return_logistics_rub",
                                                  "promotion_rub", "other_costs_rub", "unclassified_rub"))
    total = sum(n["amount_rub"] for n in norm)
    assert econ_costs + sum(s["amount_rub"] for s in store) == total
    assert sum(r["commission_rub"] for r in econ) + sum(s["commission_rub"] or 0 for s in store) == -300
    scopes = {s["cost_scope"] for s in store}
    assert scopes == {"POSTING_NOT_DELIVERED", "POSTING_NOT_LOADED", "POSTING_SKU_UNMATCHED", "STORE", "SKU"}
    (r,) = econ
    assert r["promotion_rub"] == -40 and r["other_costs_rub"] == 700 - 8
    assert [s["unresolved_accruals"] for s in store if s["accrual_class"] == "UNCLASSIFIED"] == [1]


# ═══════════════════════════════════════ продажи, товар, себестоимость
def test_sales_with_missing_quantity_or_price_are_null_not_understated():
    rows = H.run("ozon_mart", "FACT_OZON_SALES_DAILY", {"NORM_OZON_POSTING_LINE": (LINE_COLS, [
        line(), line(posting="P2", price=None)])})
    (r,) = rows
    assert r["ordered_units"] == 2 and r["ordered_value_rub"] is None and r["delivered_value_rub"] is None


def test_every_cancelled_status_counts_as_cancelled():
    assert "STARTS_WITH(p.status, 'cancelled') AS is_cancelled" in _sql("ozon_mart", "NORM_OZON_POSTING_LINE")


CAT = ["sku", "product_id", "offer_id", "name", "is_archived", "status_name", "snapshot_date", "extracted_at"]
MAP = ["internal_sku", "marketplace", "marketplace_sku", "valid_from", "valid_to", "is_current"]
MASTER = ["internal_sku", "product_name", "brand", "category", "is_bundle", "valid_from", "valid_to"]


@pytest.mark.parametrize("mapping,status,internal", [
    ([], "UNMAPPED", None),
    ([("A1", "OZON", "11", "2026-01-01", None, 1)], "MAPPED", "A1"),
    ([("A1", "OZON", "11", "2026-01-01", None, 1), ("A2", "OZON", "11", "2026-01-01", None, 1)], "AMBIGUOUS", None),
    ([("A1", "OZON", "11", "2026-01-01", None, 1), ("A2", "OZON", "11", "2026-03-01", None, 1)], "MAPPED", "A2"),
])
def test_product_mapping_is_never_guessed(mapping, status, internal):
    rows = H.run("ozon_mart", "DIM_OZON_PRODUCT", {
        "RAW_OZON_CATALOG": (CAT, [("11", 1, "o1", "n", 0, "ok", "2026-01-01", "t")]),
        "REF_SKU_CHANNEL_MAP": (MAP, mapping), "REF_PRODUCT_MASTER": (MASTER, [])})
    assert [(r["mapping_status"], r["internal_sku"]) for r in rows] == [(status, internal)]


REFC = ["internal_sku", "effective_from", "effective_to", "product_cogs_rub", "cost_basis", "source", "loaded_at"]


def test_open_cogs_intervals_run_until_the_next_one_and_overlaps_surface_in_dq():
    db = H.database({"REF_COGS": (REFC, [
        ("A1", "2026-01-01", None, 100, None, "s", "t1"), ("A1", "2026-06-01", None, 120, None, "s", "t1"),
        ("A2", "2026-01-01", "2026-12-31", 50, None, "s", "t1"), ("A2", "2026-03-01", "2026-04-30", 60, None, "s", "t1")])})
    H.materialize(db, "ozon_mart", "ECON_TENANT_COGS")
    got = {(r["internal_sku"], r["effective_from"]): r["effective_to"] for r in H.query(db, "ozon_mart", "ECON_TENANT_COGS")}
    assert got[("A1", "2026-01-01")] == "2026-05-31" and got[("A1", "2026-06-01")] == "9999-12-31"
    assert got[("A2", "2026-01-01")] == "2026-12-31"                           # явный конец не трогаем
    overlaps = H.query(db, "tenant_ops", "V_DQ_COGS_OVERLAPS")
    assert [(o["internal_sku"], o["second_from"]) for o in overlaps] == [("A2", "2026-03-01")]


# ═══════════════════════════════════════ привязка к кабинету
BIND = ["binding_id", "api", "identity_fingerprint", "status", "confirmed_at", "confirmed_by", "revoked_at",
        "source_observation_id", "notes"]
OBS = ["observation_id", "api", "identity_fingerprint", "observed_at", "company_ogrn"]
SRC = ("o0", "SELLER", "f1", "2026-01-01")


def conf(bid, fp="f1", at="2026-01-02", src="o0"):
    return (bid, "SELLER", fp, "CONFIRMED", at, "op", None, src)


def revk(bid, at="2026-02-01", fp="f1", confirmed="2026-01-02"):
    return (bid, "SELLER", fp, "REVOKED", confirmed, "op", at, "o0")


def binding_sql(tables):
    normalized = {}
    for table, (cols, rows) in tables.items():
        fixed = []
        for row in rows:
            row = list(row) + [None] * (len(cols) - len(row))
            # Existing test labels are synthetic v1 fingerprints, not malformed markers.
            fp = row[2]
            if isinstance(fp, str) and fp.startswith('f') and fp[1:].isdigit():
                row[2] = fp[1:] * 64
            fixed.append(tuple(row))
        normalized[table] = (cols, fixed)
    return H.run("tenant_ops", "V_SELLER_BINDING_STATUS", normalized)


@pytest.mark.parametrize("bindings,observations,expected", [
    ([], [SRC], "UNBOUND"),
    ([conf("b1")], [SRC], "NOT_OBSERVED"),                                    # только наблюдение до подтверждения
    ([conf("b1")], [SRC, ("o1", "SELLER", "f1", "2026-03-01")], "BOUND"),
    ([conf("b1")], [SRC, ("o1", "SELLER", "f2", "2026-03-01")], "MISMATCH"),
    ([conf("b1")], [SRC, ("o1", "SELLER", "f1", "2026-02-01"), ("o2", "SELLER", "f2", "2026-03-01")], "MISMATCH"),
    ([conf("b1")], [SRC, ("o1", "SELLER", "f1", "2026-03-01"), ("o2", "SELLER", "f2", "2026-03-01")], "MISMATCH"),
    ([conf("b1")], [SRC, ("o1", "SELLER", None, "2026-03-01")], "MISMATCH"),
    ([conf("b1"), revk("b2")], [SRC, ("o1", "SELLER", "f1", "2026-03-01")], "UNBOUND"),
    ([conf("b1"), ("b2", "SELLER", "f1", "REVOKED", "2026-01-02", "op", None, "o0")],   # отзыв без revoked_at, та же минута
     [SRC, ("o1", "SELLER", "f1", "2026-03-01")], "UNBOUND"),
    ([conf("b1"), revk("b2"), conf("b3", fp="f3", at="2026-02-15", src="o5")],
     [SRC, ("o5", "SELLER", "f3", "2026-02-10"), ("o6", "SELLER", "f3", "2026-03-01")], "BOUND"),
    ([conf("b1", src="o-missing")], [SRC, ("o1", "SELLER", "f1", "2026-03-01")], "INVALID_BINDING"),
    ([conf("b1", fp="f9")], [SRC, ("o1", "SELLER", "f9", "2026-03-01")], "INVALID_BINDING"),  # источник с другим отпечатком
    ([conf("b1"), conf("b2", fp="f2")], [SRC, ("o1", "SELLER", "f1", "2026-03-01")], "INVALID_BINDING"),
    ([conf("b1", at="2099-01-01")], [SRC, ("o1", "SELLER", "f1", "2099-02-01")], "INVALID_BINDING"),
    ([conf("b1"), revk("b2"), conf("b3", at="2099-01-01")], [SRC, ("o1", "SELLER", "f1", "2099-02-01")], "INVALID_BINDING"),
    ([conf("b1"), ("b2", "SELLER", "f1", "PENDING", "2026-02-01", "op", None, "o0")],
     [SRC, ("o1", "SELLER", "f1", "2026-03-01")], "BOUND"),                   # недопустимый статус не событие
])
def test_seller_binding_status_fails_closed(bindings, observations, expected):
    rows = binding_sql({
        "SELLER_BINDING": (BIND, bindings), "SELLER_IDENTITY_OBSERVATIONS": (OBS, observations)})
    assert [r["binding_status"] for r in rows if r["api"] == "SELLER"] == [expected]


def test_seller_binding_is_order_independent():
    bindings = [conf("b1"), ("b2", "SELLER", "f1", "REVOKED", "2026-01-02", "op", None, "o0")]
    obs = [SRC, ("o1", "SELLER", "f1", "2026-03-01")]
    for b in (bindings, bindings[::-1]):
        rows = binding_sql({
            "SELLER_BINDING": (BIND, b), "SELLER_IDENTITY_OBSERVATIONS": (OBS, obs)})
        assert [r["binding_status"] for r in rows] == ["UNBOUND"]


# ═══════════════════════════════════════ таксономия: провенанс и расширяемость
def test_taxonomy_carries_official_names_and_classification_basis():
    tax = _taxonomy()
    assert len(tax) == 33
    assert {v["classification_basis"] for v in tax.values()} == {"OZON_TYPE_NAME", "PLATFORM_INTERPRETATION"}
    assert all(v["ozon_type_name"] for v in tax.values())
    assert "attribution_scope" not in _sql("ozon_mart", "DIM_OZON_ACCRUAL_TYPE")   # уровень — из строки, не из эмпирики


def test_taxonomy_provenance_is_documented_type_by_type():
    doc = (REPO / "docs" / "architecture" / "TENANT_SQL_SEMANTICS.md").read_text(encoding="utf-8")
    rows = {int(m.group(1)): (m.group(2), m.group(3), m.group(4))
            for m in re.finditer(r"^\| (\d+) \| (\w+) \| [^|]+ \| (\w+) \| (\w+) \|$", doc, re.M)}
    tax = _taxonomy()
    assert set(rows) == set(tax)
    for i, v in tax.items():
        assert rows[i] == (v["ozon_type_name"], v["accrual_class"], v["classification_basis"]), i


def test_classification_happens_only_in_the_taxonomy_view():
    for p in SP.PACKAGE_DIR.glob("*/*.sql"):
        if p.stem != "DIM_OZON_ACCRUAL_TYPE":
            assert not re.search(r"type_id\s*(=|IN)\s*\(?\s*\d", p.read_text(encoding="utf-8")), p.name


@pytest.mark.parametrize('notes,observed_ogrn,expected', [
    ({'identity_version':'ozon-seller-core-v2','owner_confirmation':True,'legal_evidence':{'ogrn':''}}, '', 'BOUND'),
    ({'identity_version':'ozon-seller-core-v2','owner_confirmation':True,'legal_evidence':{'ogrn':'synthetic-legal'}}, 'synthetic-legal', 'BOUND'),
    ({'identity_version':'ozon-seller-core-v2','owner_confirmation':True,'legal_evidence':{'ogrn':'synthetic-legal'}}, '', 'MISMATCH'),
    ({'identity_version':'ozon-seller-core-v2','owner_confirmation':True,'legal_evidence':{'ogrn':'synthetic-legal'}}, 'changed', 'MISMATCH'),
    ({'identity_version':'ozon-seller-core-v2','owner_confirmation':'true','legal_evidence':{'ogrn':''}}, '', 'INVALID_BINDING'),
    ({'identity_version':'future','owner_confirmation':True,'legal_evidence':{'ogrn':''}}, '', 'INVALID_BINDING'),
    ({}, '', 'INVALID_BINDING'),
])
def test_binding_sql_v2_owner_and_legal_evidence(notes,observed_ogrn,expected):
    import json
    fp='ozon-seller-core-v2:sha256:'+('a'*64)
    binding=('b','SELLER',fp,'CONFIRMED','2026-01-02','OPERATOR:synthetic',None,'o0',json.dumps(notes))
    obs=[('o0','SELLER',fp,'2026-01-01',''),('o1','SELLER',fp,'2026-03-01',observed_ogrn)]
    result=binding_sql({'SELLER_BINDING':(BIND,[binding]),'SELLER_IDENTITY_OBSERVATIONS':(OBS,obs)})
    assert result[0]['binding_status']==expected


def test_binding_sql_unknown_and_cross_version_never_bound():
    for bound_fp,live_fp,expected in [
        ('future:sha256:'+('a'*64),'future:sha256:'+('a'*64),'INVALID_BINDING'),
        ('a'*64,'ozon-seller-core-v2:sha256:'+('a'*64),'MISMATCH'),
    ]:
        row=('b','SELLER',bound_fp,'CONFIRMED','2026-01-02','OPERATOR:synthetic',None,'o0',None)
        obs=[('o0','SELLER',bound_fp,'2026-01-01',''),('o1','SELLER',live_fp,'2026-03-01','')]
        result=binding_sql({'SELLER_BINDING':(BIND,[row]),'SELLER_IDENTITY_OBSERVATIONS':(OBS,obs)})
        assert result[0]['binding_status']==expected


@pytest.mark.parametrize('linked', [True,False])
def test_binding_sql_performance_requires_v2_seller_link(linked):
    import json
    sfp='ozon-seller-core-v2:sha256:'+('a'*64);pfp='b'*64
    notes={'identity_version':'ozon-seller-core-v2','owner_confirmation':True,'legal_evidence':{'ogrn':''}}
    pn={'binding_protocol':'ozon-seller-core-v2','owner_confirmation':True,'seller_binding_fingerprint':sfp}
    rows=[('bs','SELLER',sfp,'CONFIRMED','2026-01-02','OPERATOR:synthetic',None,'os',json.dumps(notes)),
          ('bp','PERFORMANCE',pfp,'CONFIRMED','2026-01-02','OPERATOR:synthetic',None,'op',json.dumps(pn) if linked else None)]
    obs=[('os','SELLER',sfp,'2026-01-01',''),('op','PERFORMANCE',pfp,'2026-01-01',None),
         ('ss','SELLER',sfp,'2026-03-01',''),('sp','PERFORMANCE',pfp,'2026-03-01',None)]
    result=binding_sql({'SELLER_BINDING':(BIND,rows),'SELLER_IDENTITY_OBSERVATIONS':(OBS,obs)})
    assert {r['api']:r['binding_status'] for r in result}=={'SELLER':'BOUND','PERFORMANCE':'BOUND' if linked else 'INVALID_BINDING'}


def test_product_dimension_retains_skuless_products_and_never_resurrects_old_sku():
    rows = H.run('ozon_mart', 'DIM_OZON_PRODUCT', {
        'RAW_OZON_CATALOG': (CAT, [
            ('11', '1', 'offer-one', 'old', 0, 'ok', '2026-09-30', 'a'),
            (None, '1', 'offer-one', 'archived', 1, 'archived', '2026-10-01', 'b'),
            (None, '2', 'offer-two', 'archived', 1, 'archived', '2026-10-01', 'b'),
            ('33', '3', 'offer-three', 'active', 0, 'ok', '2026-10-01', 'b')]),
        'REF_SKU_CHANNEL_MAP': (MAP, [('A1', 'OZON', '11', '2026-01-01', None, 1)]),
        'REF_PRODUCT_MASTER': (MASTER, [])})
    assert len(rows) == 3
    by_id = {r['product_id']: r for r in rows}
    assert by_id['1']['sku'] is None and by_id['2']['sku'] is None
    assert not by_id['1']['sku_joinable'] and by_id['1']['internal_sku'] is None
    assert by_id['3']['sku_joinable']
    assert not [r for r in rows if r['sku_joinable'] and r['sku'] == '11']


def test_ambiguous_catalog_sku_never_multiplies_product_fact_join():
    rows = H.run('ozon_mart', 'DIM_OZON_PRODUCT', {
        'RAW_OZON_CATALOG': (CAT, [('11', '1', 'a', 'a', 0, 'ok', '2026-10-01', 'a'),
                                 ('11', '2', 'b', 'b', 1, 'archived', '2026-10-01', 'a')]),
        'REF_SKU_CHANNEL_MAP': (MAP, [('A1', 'OZON', '11', '2026-01-01', None, 1)]),
        'REF_PRODUCT_MASTER': (MASTER, [])})
    assert len(rows) == 2
    assert all(not r['sku_joinable'] and r['internal_sku'] is None and r['mapping_status'] == 'AMBIGUOUS' for r in rows)
