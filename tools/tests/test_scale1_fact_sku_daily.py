"""SCALE 1 — offline structural tests for ozon_mart.FCT_OZON_SKU_PNL_DAILY and evetis_mart.FACT_SKU_DAILY.

No network, no credentials, no BigQuery. These tests guard the CONTRACT in Git; numeric parity against
production (daily -> monthly, adapters, identities, grain, mapping) lives in
sql/scale1/fact_sku_daily_validation.sql and is read-only SQL run by the owner.
"""
import json
import re
import sys
from pathlib import Path

import sqlglot
from sqlglot import exp

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tools"))
import scale1_predeploy_render as render  # noqa: E402
import validate_current_sql as v  # noqa: E402

P = "project-fa311fc0-4d87-4781-986"
OZON = REPO / "sql/current/ozon_mart"
DAILY = OZON / "FCT_OZON_SKU_PNL_DAILY.sql"
RECOVERY = OZON / "V_OZON_COMMISSION_RECOVERY.sql"
MONTHLY_SKU = OZON / "FCT_OZON_SKU_PNL_MONTHLY.sql"
MONTHLY_STORE = OZON / "FCT_OZON_PNL_MONTHLY.sql"
NEUTRAL = REPO / "sql/current/evetis_mart/FACT_SKU_DAILY.sql"
VALIDATION = REPO / "sql/scale1/fact_sku_daily_validation.sql"

NEUTRAL_SCHEMA = [
    ("fact_date", "DATE"), ("marketplace", "STRING"), ("internal_sku", "STRING"), ("marketplace_sku", "STRING"),
    ("product_name", "STRING"), ("is_bundle", "BOOL"),
    ("orders_qty", "INT64"), ("cancelled_qty", "INT64"), ("sold_qty", "INT64"), ("return_qty", "INT64"),
    ("seller_revenue_rub", "NUMERIC"), ("marketplace_commission_rub", "NUMERIC"), ("logistics_rub", "NUMERIC"),
    ("storage_rub", "NUMERIC"), ("acquiring_rub", "NUMERIC"), ("other_marketplace_costs_rub", "NUMERIC"),
    ("marketplace_costs_total_rub", "NUMERIC"), ("advertising_attributed_rub", "NUMERIC"),
    ("contribution_after_ads_rub", "NUMERIC"), ("cogs_rub", "NUMERIC"), ("contribution_after_cogs_rub", "NUMERIC"),
    ("economics_covered", "BOOL"), ("is_provisional", "BOOL"), ("fact_date_semantics", "STRING"),
    ("source_contract", "STRING"), ("contract_version", "STRING"),
]
TAX_WORDS = re.compile(r"tax|vat|usn|nalog|налог|ндс|усн", re.I)


def facts(path):
    out = []
    f = v.analyze_sql(path.read_text(encoding="utf-8"), path.name, out)
    assert out == [], out
    return f


def body_ast(path):
    return sqlglot.parse_one(facts(path).body, read="bigquery")


def manifest_entry(dataset, name):
    man = json.loads((REPO / f"sql/current/{dataset}/MANIFEST.json").read_text(encoding="utf-8"))
    return man, next(o for o in man["objects"] if o["object_name"] == name)


def recovery_rows(path):
    """{posting_number: amount} of the inline UNNEST([STRUCT(...), (...)]) literal table in a canonical file."""
    body = facts(path).body
    start = body.index("UNNEST([")
    literal = body[start:body.index("])", start)]
    rows = re.findall(r"\(\s*'([^']+)'(?:\s+AS\s+posting_number)?\s*,\s*NUMERIC\s+'([0-9.]+)'", literal)
    return dict(rows), len(rows)


def in_lists(path, column="type_id"):
    """Every `<column> IN (...)` integer list and `<column> = n` in the view body, as a list of frozensets."""
    found = []
    for node in body_ast(path).find_all(exp.In):
        if node.this.name == column:
            found.append(frozenset(int(e.name) for e in node.expressions))
    for node in body_ast(path).find_all(exp.EQ):
        if isinstance(node.this, exp.Column) and node.this.name == column and isinstance(node.expression, exp.Literal):
            found.append(frozenset({int(node.expression.name)}))
    return found


# ------------------------------------------------------------------------------------- repository contract

def test_repository_contract_holds_with_scale1_objects():
    findings, summary = v.validate(REPO)
    assert findings == [], [f"{f.check} {f.subject}: {f.reason}" for f in findings]
    assert summary["datasets"] == ["evetis_mart", "ozon_mart"]


def test_scale1_objects_are_captured_live_at_the_deployment_sha():
    """Deployed 2026-09-20 from main 2260c73 and captured read-only by R2C (PENDING_DEPLOYED_MATCH): production
    body, schema and description equal the canonical files, so the capture equals the canon on all three hashes."""
    for dataset, name in (("ozon_mart", "V_OZON_COMMISSION_RECOVERY"), ("ozon_mart", "FCT_OZON_SKU_PNL_DAILY"),
                          ("evetis_mart", "FACT_SKU_DAILY")):
        _, o = manifest_entry(dataset, name)
        assert o["sync_state"] == "captured_live" and o["canonical_schema_verification"] == "bigquery_verified"
        assert o["capture_main_sha"] == "2260c73588d87194b4b0e90d93efb1352d5ec0c7" and o["captured_at"].startswith("2026-09-20T"), name
        assert o["live_body_sha256_at_capture"] == o["canonical_body_sha256"], name
        assert o["live_schema_sha256_at_capture"] == o["canonical_schema_sha256"], name
        assert o["live_description_sha256_at_capture"] == o["canonical_description_sha256"], name
        assert o["live_schema_at_capture"] == o["canonical_schema"], name


def test_existing_ozon_monthly_views_are_not_touched():
    man, _ = manifest_entry("ozon_mart", "FCT_OZON_SKU_PNL_MONTHLY")
    for o in man["objects"]:
        if o["object_name"] in ("FCT_OZON_SKU_PNL_MONTHLY", "FCT_OZON_PNL_MONTHLY"):
            assert o["sync_state"] == "captured_live"
            assert o["canonical_body_sha256"] == o["live_body_sha256_at_capture"]


# ------------------------------------------------------------------------------------- neutral fact

def test_neutral_schema_contract_is_exactly_v1():
    _, o = manifest_entry("evetis_mart", "FACT_SKU_DAILY")
    assert [(c["column_name"], c["data_type"]) for c in o["canonical_schema"]] == NEUTRAL_SCHEMA
    assert all(c["is_nullable"] == "YES" for c in o["canonical_schema"])
    assert facts(NEUTRAL).output_columns == [n for n, _ in NEUTRAL_SCHEMA]


def test_neutral_fact_reads_only_authoritative_marts_never_raw():
    refs = {(d, n) for _, d, n in facts(NEUTRAL).references}
    assert refs == {("wb_mart", "SKU_PERFORMANCE_V2_DAILY"), ("ozon_mart", "FCT_OZON_SKU_PNL_DAILY"),
                    ("evetis_ref", "REF_PRODUCT_MASTER"), ("evetis_ref", "REF_SKU_CHANNEL_MAP")}
    assert not any(d.endswith("_raw") for d, _ in refs)


def test_validator_policy_keeps_raw_out_of_the_neutral_layer_and_marts_isolated():
    assert v.EXTERNAL_DATASET_POLICY["evetis_mart"] == frozenset({"wb_mart", "ozon_mart", "evetis_ref"})
    assert v.EXTERNAL_DATASET_POLICY["ozon_mart"] == frozenset({"ozon_raw", "evetis_ref"})
    for dataset, allowed in v.EXTERNAL_DATASET_POLICY.items():
        assert "evetis_mart" not in allowed, dataset  # no marketplace mart may read the neutral layer back


def test_neutral_fact_supports_exactly_wb_and_ozon():
    literals = {s.name for s in body_ast(NEUTRAL).find_all(exp.Literal) if s.is_string}
    aliased = {e.this.name for e in body_ast(NEUTRAL).find_all(exp.Alias)
               if e.alias == "marketplace" and isinstance(e.this, exp.Literal)}
    assert aliased == {"WB", "OZON"} and {"WB", "OZON"} <= literals


def test_no_tax_anywhere_in_the_new_contract():
    for path in (NEUTRAL, DAILY):
        f = facts(path)
        assert not [c for c in f.output_columns if TAX_WORDS.search(c)], path.name
        assert not [c.name for c in body_ast(path).find_all(exp.Column) if TAX_WORDS.search(c.name)], path.name


def test_unsupported_metrics_are_null_not_zero():
    sql = facts(NEUTRAL).body
    assert "CAST(NULL AS NUMERIC) acquiring_rub, CAST(NULL AS NUMERIC) other_marketplace_costs_rub" in sql  # WB
    assert "CAST(NULL AS INT64) return_qty" in sql                                                          # Ozon
    # COGS fails closed: an unresolved unit makes cogs and the after-COGS contribution NULL, never partial
    assert "IF(d.cogs_missing_qty = 0, d.product_cogs_rub, NULL) cogs_rub" in sql
    assert "IF(d.cogs_missing_qty = 0, d.contribution_after_attributed_ads_rub, NULL) contribution_after_cogs_rub" in sql


def test_wb_adapter_passes_authoritative_columns_through_without_recomputation():
    sql = facts(NEUTRAL).body
    for line in ("t.contribution_before_cogs_rub contribution_after_ads_rub", "t.cogs_rub cogs_rub",
                 "t.contribution_after_cogs_rub contribution_after_cogs_rub", "t.fin_seller_price_rub seller_revenue_rub",
                 "t.ads_attributed_rub advertising_attributed_rub", "t.orders_gross_units orders_qty"):
        assert line in sql, line
    # the only WB arithmetic is the marketplace take on price (revenue minus credited) and the cost total
    assert "t.fin_seller_price_rub - t.credited_for_goods_rub marketplace_commission_rub" in sql


# ------------------------------------------------------------------------------------- Ozon daily fact

def test_ozon_daily_fact_is_full_precision_and_deterministic():
    tree = body_ast(DAILY)
    assert not list(tree.find_all(exp.Round)), "the daily fact must not ROUND: rounding breaks daily->monthly parity"
    assert not list(tree.find_all(exp.CurrentTimestamp, exp.CurrentDate, exp.CurrentDatetime))


def test_ozon_daily_fact_uses_the_same_sources_as_the_monthly_pnl():
    daily = {(d, n) for _, d, n in facts(DAILY).references}
    monthly = {(d, n) for _, d, n in facts(MONTHLY_SKU).references}
    assert daily - monthly == {("ozon_mart", "V_OZON_COMMISSION_RECOVERY")}
    assert monthly - daily == {("evetis_ref", "REF_PRODUCT_MASTER")}  # names live in the neutral fact, not here
    assert not any(d.startswith("wb_") for d, _ in daily)


def test_ozon_cost_buckets_are_identical_to_the_monthly_pnl():
    monthly = set(in_lists(MONTHLY_SKU))
    direct_var, other_direct = frozenset({32, 29, 28, 98, 30, 1, 59, 45, 78, 9, 79}), frozenset({15, 71, 39, 38})
    assert monthly == {direct_var, other_direct}
    daily = in_lists(DAILY)
    assert direct_var in daily and other_direct in daily
    logistics, acquiring, storage = frozenset({32, 29, 28, 98, 30, 59, 45, 78, 9}), frozenset({1}), frozenset({79})
    assert set(daily) == {direct_var, other_direct, logistics, acquiring, storage}
    # the decomposition is a partition of the monthly direct-variable bucket: nothing invented, nothing lost
    assert logistics | acquiring | storage == direct_var
    assert not (logistics & acquiring or logistics & storage or acquiring & storage)


def test_ozon_status_lists_match_the_store_level_monthly_pnl():
    def statuses(path):
        return {frozenset(e.name for e in n.expressions) for n in body_ast(path).find_all(exp.In) if n.this.name == "status"}
    assert statuses(DAILY) == statuses(MONTHLY_STORE) == {frozenset({"delivering", "awaiting_deliver", "awaiting_packaging"})}


def test_commission_recovery_has_one_named_source_equal_to_both_inline_copies():
    shared, n = recovery_rows(RECOVERY)
    assert n == len(shared) == 29 and sum(map(lambda a: round(float(a) * 100), shared.values())) == 963760
    for legacy in (MONTHLY_SKU, MONTHLY_STORE):
        rows, count = recovery_rows(legacy)
        assert count == 29 and rows == shared, legacy.name
    # the daily fact must reference the named source, not carry a fourth copy of the rows
    assert "UNNEST" not in facts(DAILY).body and "0107987069-0115-1" not in DAILY.read_text(encoding="utf-8")


def test_channel_map_join_key_is_exactly_the_key_whose_uniqueness_the_identity_gate_proves():
    """Join-cardinality guard. The daily fact joins RAW postings / finance / ads to REF_SKU_CHANNEL_MAP on
    (marketplace = 'OZON', marketplace_sku) and nothing else, with NO is_current / validity filter — so every
    historical mapping row takes part. Such a join cannot multiply source rows if and only if no
    (marketplace, marketplace_sku) occurs more than once in the WHOLE map. That is gate
    `fanout_risk_identifier_rows` of V05; this test pins both halves so they cannot drift apart:
    a different join key, or a gate that stops counting, fails here."""
    joins = [j for j in body_ast(DAILY).find_all(exp.Join)
             if isinstance(j.this, exp.Table) and j.this.name == "REF_SKU_CHANNEL_MAP"]
    assert len(joins) == 4  # postings, posting-linked costs, sku-only costs, ads
    for j in joins:
        alias = j.this.alias
        on = j.args["on"]
        conds = list(on.flatten()) if isinstance(on, exp.And) else [on]
        assert len(conds) == 2 and all(isinstance(c, exp.EQ) for c in conds), on.sql()
        map_side = {}
        for c in conds:
            col = next(x for x in (c.this, c.expression) if isinstance(x, exp.Column) and x.table == alias)
            map_side[col.name] = c.expression if col is c.this else c.this
        assert set(map_side) == {"marketplace", "marketplace_sku"}, on.sql()
        assert isinstance(map_side["marketplace"], exp.Literal) and map_side["marketplace"].name == "OZON"
        assert isinstance(map_side["marketplace_sku"], exp.Column) and map_side["marketplace_sku"].name == "sku"

    gate = check_blocks()["V05_PRODUCT_IDENTITY_GATE"]
    # uniqueness of the join key over the WHOLE map: no marketplace / is_current filter before the GROUP BY
    assert ("(SELECT COUNT(*) FROM (SELECT 1 FROM cm GROUP BY marketplace, marketplace_sku HAVING COUNT(*) > 1)) "
            "fanout_risk_identifier_rows") in gate
    assert "cm AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_CHANNEL_MAP`)" in gate
    # exactly one = at most one (above) + at least one (no consumed RAW row is left unmapped)
    status = gate[gate.index("SELECT g.*, IF("):]
    for counter in ("fanout_risk_identifier_rows", "conflicting_identifier_mappings", "ozon_posting_rows_unmapped",
                    "ozon_finance_sku_rows_unmapped", "ozon_ads_rows_unmapped"):
        assert counter in status, counter  # fail-closed: every counter is part of the PASS condition
    assert "= 0,\n  'PASS', 'FAIL') status" in status


# ------------------------------------------------------------------------------------- validation SQL + renderer

def check_blocks():
    text = VALIDATION.read_text(encoding="utf-8")
    blocks = re.split(r"(?m)^-- @check ", text)[1:]
    return {b.partition("\n")[0].strip(): b.partition("\n")[2] for b in blocks}


def test_validation_file_is_read_only_selects():
    blocks = check_blocks()
    assert len(blocks) == 11 and all(re.fullmatch(r"V\d\d_[A-Z0-9_]+", k) for k in blocks)
    for cid, sql in blocks.items():
        statements = sqlglot.parse(sql, read="bigquery")
        assert len(statements) == 1 and isinstance(statements[0], exp.Select), cid
        assert not list(statements[0].find_all(exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Create, exp.Drop,
                                               exp.Alter, exp.Command)), cid
        assert "status" in [s.alias_or_name for s in statements[0].selects], cid


def test_predeploy_render_inlines_every_pending_object_and_stays_a_select():
    # Nothing is pending after the 2026-09-20 deployment, so the tool is a no-op on the real repository ...
    assert render.pending_bodies() == {}
    sample = next(iter(check_blocks().values()))
    assert render.render(sample, {}) == sample
    # ... and its inlining logic is still exercised on the same three objects, as if they were pending.
    bodies = {f"`{P}.{p.parent.name}.{p.stem}`": facts(p).body for p in (RECOVERY, DAILY, NEUTRAL)}
    for cid, sql in check_blocks().items():
        rendered = render.render(sql, bodies)
        assert not any(ref in rendered for ref in bodies), cid
        assert "evetis_mart" not in rendered, cid  # the dataset does not exist before deployment
        statements = sqlglot.parse(rendered, read="bigquery")
        assert len(statements) == 1 and isinstance(statements[0], exp.Select), cid
        assert "CREATE" not in re.sub(r"--[^\n]*", "", rendered).upper().split(), cid
