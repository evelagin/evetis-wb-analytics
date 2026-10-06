"""Offline execution of the actual Ozon price CTE/completeness CASE in SQLite.

Only project/date placeholders and table identifiers are replaced. BigQuery IF,
COUNTIF and SAFE_DIVIDE are supplied as local functions, not production calls.
"""
from pathlib import Path
import re
import sqlite3
import json
import pytest
import sqlglot
from sqlglot import exp

ROOT = Path(__file__).resolve().parents[2]


class CountIf:
    def __init__(self):
        self.n = 0

    def step(self, v):
        self.n += bool(v)

    def finalize(self):
        return self.n


def order_price(rows, expected_units):
    text = (ROOT / "cloud/src/loaders/unitka/ozon/bq.ts").read_text()
    cte = text.split("order_prices AS (", 1)[1].split("),\nprice_finance AS (", 1)[0]
    cte = cte.replace("\\`", "`").replace("${project}.ozon_raw.RAW_OZON_POSTINGS_FBO", "postings")
    cte = cte.replace("${from}", "2026-09-20").replace("${to}", "2026-09-20")
    final = re.search(r"CASE WHEN op.units = f.expected_realized_qty.*?END order_reference_price", text, re.S)[0]
    with sqlite3.connect(":memory:") as db:
        db.create_function("IF", 3, lambda c, yes, no: yes if c else no)
        db.create_function("SAFE_DIVIDE", 2, lambda x, y: x / y if x is not None and y else None)
        db.create_aggregate("COUNTIF", 1, CountIf)
        db.execute("CREATE TABLE postings(order_date TEXT, sku TEXT, quantity INT, price_rub REAL, status TEXT)")
        db.executemany("INSERT INTO postings VALUES('2026-09-20', '1991772098', ?, ?, ?)", rows)
        return db.execute("WITH m AS (SELECT '1991772098' marketplace_sku, '305101272' offer_id), "
                          f"op AS ({cte}), f AS (SELECT ? expected_realized_qty) "
                          f"SELECT {final} FROM op CROSS JOIN f", (expected_units,)).fetchone()[0]


def test_exact_posting_price_never_becomes_revenue():
    assert order_price([(1, 1287, "delivered")], 1) == 1287
    text = (ROOT / "cloud/src/loaders/unitka/ozon/bq.ts").read_text()
    assert "f.seller_base_revenue_rub revenue" in text
    assert "b.buyer_amt, b.seller_amt" in text


def test_weighted_multiple_postings_excludes_cancelled_units():
    assert order_price([(1, 1000, "delivered"), (2, 1300, "delivering"), (5, 9999, "cancelled")], 3) == 1200


@pytest.mark.parametrize("rows,expected", [
    ([(1, None, "delivered")], 1),
    ([(1, 1287, "delivered"), (1, None, "delivering")], 2),
    ([(1, 1287, "delivered")], 2),
    ([(1, 0, "delivered")], 1),
    ([(1, -5, "delivered")], 1),
    ([(None, 1287, "delivered")], 1),
    ([(0, 1287, "delivered")], 1),
])
def test_incomplete_or_invalid_price_coverage_fails_closed(rows, expected):
    assert order_price(rows, expected) is None


@pytest.mark.parametrize("unproven,expected", [(1, "PROVISIONAL_PARTIAL"), (0, "ACTUAL")])
def test_actual_completeness_case_executes_with_cis_signature(unproven, expected):
    text = (ROOT / "sql/current/ozon_mart/V_OZON_SKU_PNL_DAILY_OPERATIONAL.sql").read_text()
    case = re.search(r"CASE\s+WHEN.*?END economics_completeness",
                     text.split("-- ── ПОЛНОТА ЭКОНОМИКИ", 1)[1], re.S)[0]
    with sqlite3.connect(":memory:") as db:
        result = db.execute(f"SELECT {case} FROM (SELECT ? buyout_revenue_unproven_qty, "
                            "1 gross_qty, 0 cancelled_qty, 1 commission_not_applicable_qty, 1 realized_qty, "
                            "0 commission_gap_qty, 0 logistics_gap_qty, 0 seller_base_revenue_rub, "
                            "0 in_transit_revenue_rub, NULL commission_estimate_method, 0 log_per_unit, "
                            "0 cogs_missing_qty, 0 in_transit_cogs_missing_qty, 0 in_transit_qty) r", (unproven,)).fetchone()[0]
        assert result == expected


def operational_db(*, payout=0, status="delivered", price=1287, tariff=.52):
    """Execute both repository view bodies against local persisted-evidence fixtures.

    SQLGlot translates BigQuery syntax only. Table identifiers and the clock are
    replaced; classification, gaps, precedence and monetary SELECTs are unmodified.
    """
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript("""
      CREATE TABLE REF_SKU_CHANNEL_MAP(marketplace TEXT, internal_sku TEXT, marketplace_sku TEXT);
      INSERT INTO REF_SKU_CHANNEL_MAP VALUES('OZON', 'EVT-FS-MOIST-30', '1991772098');
      CREATE TABLE RAW_OZON_POSTINGS_FBO(posting_number TEXT, sku TEXT, status TEXT,
        order_date TEXT, quantity INT, price_rub REAL, payout_rub REAL);
      CREATE TABLE RAW_OZON_FINANCE_ACCRUAL(posting_number TEXT, sku TEXT, event_date TEXT,
        type_id INT, amount_rub REAL, seller_base_price_rub REAL, commission_rub REAL);
      INSERT INTO RAW_OZON_FINANCE_ACCRUAL VALUES
        ('92767357-0024-1', '1991772098', '2026-09-23', 32, -80, NULL, NULL),
        ('92767357-0024-1', '1991772098', '2026-09-23', 98, -25, NULL, NULL);
      CREATE TABLE V_OZON_CIS_BUYOUT(posting_number TEXT, buyout_proceeds_rub REAL);
      CREATE TABLE V_PRODUCT_COGS_EFFECTIVE(internal_sku TEXT, effective_from TEXT,
        effective_to TEXT, product_cogs_rub REAL);
      INSERT INTO V_PRODUCT_COGS_EFFECTIVE VALUES('EVT-FS-MOIST-30', '2026-01-01', NULL, 130.81);
      CREATE TABLE RAW_OZON_ADS_SKU_DAILY(date TEXT, sku TEXT, attributed_spend_rub REAL);
      INSERT INTO RAW_OZON_ADS_SKU_DAILY VALUES('2026-09-20', '1991772098', 336.64);
      CREATE TABLE V_OZON_COMMISSION_POLICY(internal_sku TEXT, effective_from TEXT,
        effective_to TEXT, commission_rate REAL);
      INSERT INTO V_OZON_COMMISSION_POLICY VALUES('EVT-FS-MOIST-30', '2026-07-15', '2026-08-27', .41);
      CREATE TABLE V_OZON_LOGISTICS_ESTIMATOR(internal_sku TEXT, logistics_per_unit_rub REAL, estimator_method TEXT);
      INSERT INTO V_OZON_LOGISTICS_ESTIMATOR VALUES('EVT-FS-MOIST-30', 99, 'SKU_P70_120D');
    """)
    db.execute("INSERT INTO RAW_OZON_POSTINGS_FBO VALUES(?, '1991772098', ?, '2026-09-20', 1, ?, ?)",
               ("92767357-0024-1", status, price, payout))
    if tariff is not None:
        db.execute("INSERT INTO V_OZON_COMMISSION_POLICY VALUES('EVT-FS-MOIST-30', '2026-08-28', '9999-12-31', ?)",
                   (tariff,))
    for view in ["FCT_OZON_SKU_PNL_DAILY", "V_OZON_SKU_PNL_DAILY_OPERATIONAL"]:
        text = (ROOT / f"sql/current/ozon_mart/{view}.sql").read_text().split("\nAS\n", 1)[1]
        text = re.sub(r"`project-fa311fc0-4d87-4781-986\.[^.]+\.([^`]+)`", r"\1", text)
        text = text.replace("CURRENT_DATE('Europe/Moscow')", "DATE '2026-10-04'")
        query = sqlglot.transpile(text, read="bigquery", write="sqlite")[0]
        db.execute(f"CREATE VIEW {view} AS {query}")
    return db


def operational_row(db):
    return dict(db.execute("SELECT * FROM V_OZON_SKU_PNL_DAILY_OPERATIONAL WHERE fact_date='2026-09-20'").fetchone())


@pytest.mark.parametrize("payout", [None, 0])
def test_structural_candidate_uses_dated_estimate_without_recognizing_revenue(payout):
    with operational_db(payout=payout) as db:
        row = operational_row(db)
        assert row["seller_base_revenue_rub"] == row["provisional_revenue_rub"] == 0
        assert row["buyout_revenue_unproven_qty"] == 1
        assert row["commission_not_applicable_qty"] == 0
        assert row["commission_missing_qty"] == row["commission_gap_qty"] == 1
        assert row["commission_rub"] == 0
        assert row["commission_estimated_rub"] == pytest.approx(669.24)
        assert row["commission_effective_rub"] == pytest.approx(669.24)
        assert row["commission_estimate_method"] == "COMMISSION_POLICY_TARIFF"
        assert row["commission_state"] == "ESTIMATED"
        assert row["economics_completeness"] == "PROVISIONAL_PARTIAL"
        assert row["logistics_effective_rub"] == 105
        assert row["provisional_cogs_rub"] == 130.81
        assert row["ad_spend_attributed_rub"] == 336.64


@pytest.mark.parametrize("proceeds,reference", [(558.88, 998), (643.44, 1149)])
def test_documented_buyouts_preserve_proceeds_discount_and_not_applicable(proceeds, reference):
    with operational_db(price=reference) as db:
        db.execute("INSERT INTO V_OZON_CIS_BUYOUT VALUES('92767357-0024-1', ?)", (proceeds,))
        row = operational_row(db)
        assert row["seller_base_revenue_rub"] == pytest.approx(proceeds)
        assert row["seller_base_revenue_rub"] != reference
        assert row["buyout_revenue_unproven_qty"] == row["commission_missing_qty"] == 0
        assert row["commission_not_applicable_qty"] == 1
        assert row["commission_state"] == "NOT_APPLICABLE"
        assert row["commission_estimated_rub"] == row["commission_effective_rub"] == 0
        assert row["commission_estimate_method"] is None
        assert row["economics_completeness"] == "ACTUAL"


@pytest.mark.parametrize("stronger", ["buyout_document", "ordinary_actual"])
def test_historical_refresh_replaces_estimate_with_stronger_persisted_evidence(stronger):
    with operational_db() as db:
        first = operational_row(db)
        assert first["commission_effective_rub"] == pytest.approx(669.24)
        if stronger == "buyout_document":
            db.execute("INSERT INTO V_OZON_CIS_BUYOUT VALUES('92767357-0024-1', 558.88)")
        else:
            db.execute("INSERT INTO RAW_OZON_FINANCE_ACCRUAL VALUES"
                       "('92767357-0024-1', '1991772098', '2026-09-24', 1000, 0, 1287, -579.15)")
        later = operational_row(db)
        assert later["commission_estimated_rub"] == later["commission_gap_qty"] == 0
        assert later["commission_estimate_method"] is None
        assert later["buyout_revenue_unproven_qty"] == 0
        assert later["economics_completeness"] == "ACTUAL"
        assert later["seller_base_revenue_rub"] == (558.88 if stronger == "buyout_document" else 1287)
        assert later["commission_state"] == ("NOT_APPLICABLE" if stronger == "buyout_document" else "ACTUAL")
        assert later["commission_effective_rub"] == pytest.approx(0 if stronger == "buyout_document" else 579.15)
        assert later["commission_effective_rub"] == later["commission_rub"]


def test_mixed_actual_and_candidate_commission_counts_each_posting_once():
    with operational_db() as db:
        db.execute("INSERT INTO RAW_OZON_POSTINGS_FBO VALUES"
                   "('ordinary', '1991772098', 'delivered', '2026-09-20', 1, 1287, 707.85)")
        db.execute("INSERT INTO RAW_OZON_FINANCE_ACCRUAL VALUES"
                   "('ordinary', '1991772098', '2026-09-24', 1000, 0, 1287, -579.15)")
        row = operational_row(db)
        assert row["gross_qty"] == 2
        assert row["commission_rub"] == 579.15
        assert row["commission_estimated_rub"] == pytest.approx(669.24)
        assert row["commission_effective_rub"] == pytest.approx(1248.39)
        assert row["commission_gap_qty"] == row["commission_missing_qty"] == 1
        assert row["commission_not_applicable_qty"] == 0
        assert row["economics_completeness"] == "PROVISIONAL_PARTIAL"


def test_ordinary_in_transit_tariff_stays_estimated():
    with operational_db(status="delivering") as db:
        row = operational_row(db)
        assert row["buyout_revenue_unproven_qty"] == 0
        assert row["seller_base_revenue_rub"] == 0
        assert row["provisional_revenue_rub"] == 1287
        assert row["commission_rub"] == 0
        assert row["commission_effective_rub"] == pytest.approx(669.24)
        assert row["commission_state"] == "ESTIMATED"
        assert row["economics_completeness"] == "PROVISIONAL_COMPLETE"


def test_missing_dated_tariff_cannot_become_not_applicable_or_actual():
    with operational_db(tariff=None) as db:
        row = operational_row(db)
        assert row["commission_state"] == "UNKNOWN"
        assert row["commission_estimate_method"] is None
        assert row["economics_completeness"] == "PROVISIONAL_PARTIAL"


def adapter_row(db):
    """Actual Unitka adapter SELECT; only dialect/table/date placeholders replaced.

    JSON STRUCT aggregation is translated structurally from the SQL AST, including
    BigQuery boolean JSON representation. Financial/classification CASEs are untouched.
    """
    db.execute("ALTER TABLE REF_SKU_CHANNEL_MAP ADD COLUMN offer_id TEXT")
    db.execute("UPDATE REF_SKU_CHANNEL_MAP SET offer_id='305101272'")
    db.execute("ALTER TABLE RAW_OZON_ADS_SKU_DAILY ADD COLUMN impressions INT DEFAULT 1192")
    db.execute("ALTER TABLE RAW_OZON_ADS_SKU_DAILY ADD COLUMN clicks INT DEFAULT 28")
    db.execute("ALTER TABLE RAW_OZON_FINANCE_ACCRUAL ADD COLUMN buyer_paid_price_rub REAL")
    text = (ROOT / "cloud/src/loaders/unitka/ozon/bq.ts").read_text().split("return `", 1)[1].split("`.trim();", 1)[0]
    text = text.replace("\\`", "`").replace("${project}", "project-fa311fc0-4d87-4781-986")
    text = text.replace("${from}", "2026-09-20").replace("${to}", "2026-09-20")
    text = re.sub(r"`project-fa311fc0-4d87-4781-986\.[^.]+\.([^`]+)`", r"\1", text)
    tree = sqlglot.parse_one(text, read="bigquery")
    for node in list(tree.find_all(exp.JSONFormat)):
        struct = node.this.this
        assert isinstance(node.this, exp.ArrayAgg) and isinstance(struct, exp.Struct)
        fields = []
        for column in struct.expressions:
            assert isinstance(column, exp.Column)
            fields.append(exp.Literal.string(column.name))
            fields.append(sqlglot.parse_one("JSON(CASE WHEN documented_buyout_present THEN 'true' ELSE 'false' END)")
                          if column.name == "documented_buyout_present" else column.copy())
        node.replace(exp.Anonymous(this="JSON_GROUP_ARRAY", expressions=[exp.Anonymous(this="JSON_OBJECT", expressions=fields)]))
    # BigQuery resolves output names; SQLite needs positional ordering here.
    tree.set("order", exp.Order(expressions=[exp.Ordered(this=exp.Literal.number(i)) for i in (1, 2)]))
    return dict(db.execute(tree.sql(dialect="sqlite")).fetchone())


def add_posting(db, posting, quantity=1, price=1287, actual=None, status="delivered", proceeds=None):
    db.execute("INSERT INTO RAW_OZON_POSTINGS_FBO VALUES(?, '1991772098', ?, '2026-09-20', ?, ?, ?)",
               (posting, status, quantity, price, None if actual is None else actual * .48))
    if actual is not None:
        db.execute("INSERT INTO RAW_OZON_FINANCE_ACCRUAL VALUES(?, '1991772098', '2026-09-24', 1000, 0, ?, ?)",
                   (posting, actual, -actual * .52))
    if proceeds is not None:
        db.execute("INSERT INTO V_OZON_CIS_BUYOUT VALUES(?, ?)", (posting, proceeds))


def test_actual_adapter_exact_mixed_counterexample_population_and_price_basis():
    with operational_db() as db:
        add_posting(db, "ordinary", actual=1287)
        row = adapter_row(db)
        assert row["revenue"] == 1287  # reference never becomes recognized revenue
        assert row["buyer_amt"] is None
        assert row["operational_expected_qty"] == 2
        assert row["operational_actual_qty"] == row["operational_provisional_qty"] == 1
        assert row["operational_reference_covered_qty"] == 1
        assert row["operational_actual_basis_rub"] == row["operational_reference_basis_rub"] == 1287
        assert row["operational_basis_rub"] == 2574
        assert row["commission"] == pytest.approx(1338.48)
        assert row["commission"] / row["operational_basis_rub"] == pytest.approx(.52)
        assert row["economics_completeness"] == "PROVISIONAL_PARTIAL"
        witness = json.loads(row["operational_basis_units_json"])
        assert {u["basis_source"] for u in witness} == {"ACTUAL_FINANCE", "REFERENCE"}
        assert len({(u["posting_number"], u["marketplace_sku"]) for u in witness}) == 2


@pytest.mark.parametrize("provisional_price", [None, 0, -1])
def test_adapter_incomplete_provisional_remainder_fails_closed(provisional_price):
    with operational_db(price=provisional_price) as db:
        add_posting(db, "ordinary", actual=1287)
        row = adapter_row(db)
        assert row["revenue"] == 1287
        assert row["operational_basis_rub"] is None
        assert row["operational_actual_qty"] == row["operational_provisional_qty"] == 1
        assert row["operational_reference_covered_qty"] == 0


def test_adapter_actual_units_do_not_require_reference_prices():
    with operational_db(price=None) as db:
        db.execute("INSERT INTO RAW_OZON_FINANCE_ACCRUAL VALUES"
                   "('92767357-0024-1', '1991772098', '2026-09-24', 1000, 0, 1287, -669.24)")
        row = adapter_row(db)
        assert row["order_reference_price"] is None
        assert row["operational_actual_qty"] == 1
        assert row["operational_provisional_qty"] == 0
        assert row["operational_basis_rub"] == 1287


def test_adapter_multiple_populations_weighting_uses_stronger_prices_once():
    with operational_db() as db:
        db.execute("DELETE FROM RAW_OZON_POSTINGS_FBO")  # local fixture only
        add_posting(db, "actual-A", quantity=2, price=900, actual=1100)
        add_posting(db, "actual-B", quantity=1, price=1400, actual=1400)
        add_posting(db, "reference-C", quantity=3, price=1300)
        add_posting(db, "reference-D", quantity=1, price=1000)
        row = adapter_row(db)
        assert row["operational_actual_qty"] == 3
        assert row["operational_provisional_qty"] == row["operational_reference_covered_qty"] == 4
        assert row["operational_actual_basis_rub"] == 3600
        assert row["operational_reference_basis_rub"] == 4900
        assert row["operational_basis_rub"] == 8500
        assert row["operational_basis_rub"] / row["operational_expected_qty"] == pytest.approx(8500 / 7)
        assert row["revenue"] == 3600


def test_adapter_documented_buyout_precedes_finance_and_reference():
    with operational_db(price=998) as db:
        db.execute("INSERT INTO V_OZON_CIS_BUYOUT VALUES('92767357-0024-1', 558.88)")
        db.execute("INSERT INTO RAW_OZON_FINANCE_ACCRUAL VALUES"
                   "('92767357-0024-1', '1991772098', '2026-09-24', 1000, 0, 1287, -669.24)")
        add_posting(db, "ordinary", actual=1287)
        row = adapter_row(db)
        assert row["operational_actual_qty"] == 2
        assert row["operational_provisional_qty"] == 0
        assert row["operational_actual_basis_rub"] == pytest.approx(1845.88)
        assert row["operational_reference_basis_rub"] == 0
        assert row["commission_actual"] == pytest.approx(669.24)
        assert row["commission_estimated_rub"] == 0
        assert row["revenue"] == pytest.approx(1845.88)


@pytest.mark.parametrize("stronger", ["finance", "document"])
def test_adapter_later_stronger_evidence_replaces_reference_population(stronger):
    with operational_db() as db:
        if stronger == "finance":
            db.execute("INSERT INTO RAW_OZON_FINANCE_ACCRUAL VALUES"
                       "('92767357-0024-1', '1991772098', '2026-09-24', 1000, 0, 1200, -624)")
        else:
            db.execute("INSERT INTO V_OZON_CIS_BUYOUT VALUES('92767357-0024-1', 558.88)")
        row = adapter_row(db)
        assert row["operational_actual_qty"] == 1
        assert row["operational_provisional_qty"] == row["operational_reference_basis_rub"] == 0
        assert row["operational_basis_rub"] == (1200 if stronger == "finance" else 558.88)
        assert row["commission_estimated_rub"] == 0


def test_adapter_ordinary_in_transit_keeps_reference_provisional_basis():
    with operational_db(status="delivering") as db:
        row = adapter_row(db)
        assert row["operational_actual_qty"] == 0
        assert row["operational_provisional_qty"] == row["operational_reference_covered_qty"] == 1
        assert row["operational_basis_rub"] == 1287
        assert row["economics_completeness"] == "PROVISIONAL_COMPLETE"


@pytest.mark.parametrize("population,expected", [
    ("documented_only", "ACTUAL"),
    ("ordinary_actual_only", "ACTUAL"),
    ("ordinary_provisional_only", "PROVISIONAL_COMPLETE"),
    ("documented_and_provisional", "PROVISIONAL_COMPLETE"),
    ("actual_and_provisional", "PROVISIONAL_COMPLETE"),
    ("unproven_delivered", "PROVISIONAL_PARTIAL"),
    ("cancelled_only", "NO_ECONOMICS"),
])
def test_real_operational_sql_completeness_population_matrix(population, expected):
    with operational_db(status="delivering" if population == "ordinary_provisional_only"
                        else "cancelled" if population == "cancelled_only" else "delivered") as db:
        if population.startswith("documented"):
            db.execute("INSERT INTO V_OZON_CIS_BUYOUT VALUES('92767357-0024-1', 558.88)")
        if population in ("ordinary_actual_only", "actual_and_provisional"):
            db.execute("INSERT INTO RAW_OZON_FINANCE_ACCRUAL VALUES"
                       "('92767357-0024-1', '1991772098', '2026-09-24', 1000, 0, 1287, -669.24)")
        if population in ("documented_and_provisional", "actual_and_provisional"):
            add_posting(db, "ordinary-in-transit", status="delivering")
        row = operational_row(db)
        assert row["economics_completeness"] == expected
        if population == "documented_and_provisional":
            assert row["realized_qty"] == row["commission_not_applicable_qty"] == 1
            assert row["in_transit_qty"] == 1  # exact formerly early-ACTUAL counterexample
            adapted = adapter_row(db)
            assert adapted["operational_actual_qty"] == adapted["operational_provisional_qty"] == 1
            assert adapted["operational_basis_rub"] == pytest.approx(1845.88)
            assert adapted["commission_estimated_rub"] == pytest.approx(669.24)
            assert adapted["logistics_estimated_rub"] == 99
            assert adapted["economics_completeness"] == "PROVISIONAL_COMPLETE"


@pytest.mark.parametrize("missing", ["commission_estimate", "logistics_estimate", "cogs"])
def test_missing_material_evidence_cannot_be_provisional_complete_or_actual(missing):
    with operational_db() as db:
        db.execute("INSERT INTO V_OZON_CIS_BUYOUT VALUES('92767357-0024-1', 558.88)")
        add_posting(db, "ordinary-in-transit", status="delivering")
        if missing == "commission_estimate":
            db.execute("DELETE FROM V_OZON_COMMISSION_POLICY")
        elif missing == "logistics_estimate":
            db.execute("DELETE FROM V_OZON_LOGISTICS_ESTIMATOR")
        else:
            db.execute("DELETE FROM V_PRODUCT_COGS_EFFECTIVE")
        assert operational_row(db)["economics_completeness"] == "PROVISIONAL_PARTIAL"


@pytest.mark.parametrize("component,expected", [
    ("cogs", "PROVISIONAL_PARTIAL"),
    ("logistics_estimated", "PROVISIONAL_COMPLETE"),
    ("logistics_unknown", "PROVISIONAL_PARTIAL"),
])
def test_documented_buyout_only_requires_actual_material_components_for_actual(component, expected):
    with operational_db() as db:
        db.execute("INSERT INTO V_OZON_CIS_BUYOUT VALUES('92767357-0024-1', 558.88)")
        if component == "cogs":
            db.execute("DELETE FROM V_PRODUCT_COGS_EFFECTIVE")
        else:
            db.execute("DELETE FROM RAW_OZON_FINANCE_ACCRUAL")
            if component == "logistics_unknown":
                db.execute("DELETE FROM V_OZON_LOGISTICS_ESTIMATOR")
        assert operational_row(db)["economics_completeness"] == expected


def test_historical_refresh_removes_last_provisional_unit_then_becomes_actual():
    with operational_db() as db:
        db.execute("INSERT INTO V_OZON_CIS_BUYOUT VALUES('92767357-0024-1', 558.88)")
        add_posting(db, "ordinary-in-transit", status="delivering")
        first = adapter_row(db)
        assert first["economics_completeness"] == "PROVISIONAL_COMPLETE"
        # Fixture evidence arrives later; production tables are never called or changed.
        db.execute("UPDATE RAW_OZON_POSTINGS_FBO SET status='delivered' WHERE posting_number='ordinary-in-transit'")
        db.execute("INSERT INTO RAW_OZON_FINANCE_ACCRUAL"
                   "(posting_number, sku, event_date, type_id, amount_rub, seller_base_price_rub, commission_rub) VALUES"
                   "('ordinary-in-transit', '1991772098', '2026-09-24', 1000, 0, 1287, -669.24),"
                   "('ordinary-in-transit', '1991772098', '2026-09-24', 32, -80, NULL, NULL),"
                   "('ordinary-in-transit', '1991772098', '2026-09-24', 98, -25, NULL, NULL)")
        # adapter_row adds fixture-only auxiliary columns once; read the actual operational view afterward.
        later = operational_row(db)
        assert later["economics_completeness"] == "ACTUAL"
        assert later["in_transit_qty"] == later["commission_estimated_rub"] == later["logistics_estimated_rub"] == 0
        assert later["seller_base_revenue_rub"] == pytest.approx(1845.88)
        assert later["commission_rub"] == pytest.approx(669.24)
