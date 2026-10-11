"""Ozon D2: план догрузки RAW_OZON_ADS_SKU_DAILY полными сутками МСК — офлайн, синтетика.

Никаких реальных кампаний и сумм: репозиторий публичный. BigQuery не вызывается.
"""
import json
import sys
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

import ozon_ads_sku_msk_backfill as t  # noqa: E402

TS = "2026-10-11T20:00:00Z"
MAP = [{"marketplace": "OZON", "marketplace_sku": "900001", "internal_sku": "SYN-A"},
       {"marketplace": "OZON", "marketplace_sku": "900002", "internal_sku": "SYN-B"},
       {"marketplace": "WB", "marketplace_sku": "900003", "internal_sku": "SYN-C"}]


def row(d, c, s, spend, imp=10, run="rt-old", ts="2026-09-01 03:31:00+00"):
    return {"date": d, "campaign_id": c, "sku": s, "attributed_spend_rub": spend, "impressions": imp,
            "clicks": 1, "cart_adds": 0, "orders": 0, "revenue_promo_rub": 0, "ordered_total_rub": None,
            "drr_promo_pct": None, "drr_total_pct": None, "attribution_status": "ATTRIBUTED_ACTUAL",
            "source_endpoint": t.SOURCE_ENDPOINT, "source_payload_hash": t.source_payload_hash(d, c, s),
            "extracted_at": ts, "ingestion_run_id": run}


def world():
    """Сутки 01.09 потеряли ночь у кампании 11; 02.09 совпадают; у 03.09 нет строки SKU 900003."""
    src = [row("2026-09-01", "11", "900001", 120.50), row("2026-09-01", "11", "900002", 30.00),
           row("2026-09-01", "12", "900001", 5.00),
           row("2026-09-02", "11", "900001", 40.00),
           row("2026-09-03", "12", "900001", 7.00), row("2026-09-03", "12", "900003", 2.00, imp=3)]
    raw = [row("2026-09-01", "11", "900001", 100.00), row("2026-09-01", "11", "900002", 30.00, imp=9),
           row("2026-09-01", "12", "900001", 5.00),
           row("2026-09-02", "11", "900001", 40.00),
           row("2026-09-03", "12", "900001", 7.00)]
    return src, raw


def plan(src, raw, d0="2026-09-01", d1="2026-09-30"):
    return t.build_plan(src, raw, MAP, d0, d1, extracted_at=TS)


def roundtrip(m):
    return json.loads(json.dumps(m, default=t._jsonable))


def test_plan_covers_only_affected_pairs_with_before_and_after_images():
    src, raw = world()
    m = plan(src, raw)
    keys = [(x["date"], x["campaign_id"], x["sku"]) for x in m["rows"]]
    # пара 01.09×11: обе строки (900002 — только счётчик отличается внутри затронутой пары); 12 — не тронута
    assert keys == [("2026-09-01", "11", "900001"), ("2026-09-01", "11", "900002"),
                    ("2026-09-03", "12", "900003")]
    first = m["rows"][0]
    assert first["before"]["attributed_spend_rub"] == Decimal("100.00")
    assert first["before"]["ingestion_run_id"] == "rt-old"
    assert first["after"]["attributed_spend_rub"] == Decimal("120.50")
    assert first["after"]["ingestion_run_id"].startswith("d2-msk-backfill-")
    assert first["after"]["source_payload_hash"] == t.source_payload_hash("2026-09-01", "11", "900001")
    assert m["rows"][2]["before"] is None                       # вставка
    assert m["totals"]["delta_rub"] == Decimal("22.50")
    assert m["totals"]["delta_unmapped_rub"] == Decimal("2.00")  # 900003 в карте только у WB


def test_daily_summary_has_current_source_delta_and_mapping():
    src, raw = world()
    days = {d["date"]: d for d in plan(src, raw)["days"]}
    assert set(days) == {"2026-09-01", "2026-09-03"}
    d1 = days["2026-09-01"]
    assert (d1["raw_attributed_rub"], d1["source_attributed_rub"], d1["delta_rub"]) == (
        Decimal("135.00"), Decimal("155.50"), Decimal("20.50"))
    assert (d1["delta_mapped_rub"], d1["delta_unmapped_rub"]) == (Decimal("20.50"), Decimal(0))
    assert (d1["affected_campaigns"], d1["affected_skus"], d1["rows"]) == (1, 1, 2)


def test_second_plan_after_apply_is_empty_and_keys_are_unique():
    src, raw = world()
    m = roundtrip(plan(src, raw))
    after = t.simulate_apply(raw, m)
    keys = [(r["date"], r["campaign_id"], r["sku"]) for r in after]
    assert len(keys) == len(set(keys))
    assert plan(src, after)["rows"] == []


def test_plan_is_deterministic_and_fingerprint_survives_json():
    src, raw = world()
    a, b = plan(src, raw), plan(list(reversed(src)), list(reversed(raw)))
    assert a["fingerprint"] == b["fingerprint"]
    loaded = roundtrip(a)
    assert t.fingerprint(loaded) == a["fingerprint"]
    loaded["rows"][0]["after"]["attributed_spend_rub"] = "999.00"
    assert t.fingerprint(loaded) != a["fingerprint"]


def test_plan_refuses_deletion_and_duplicate_keys():
    src, raw = world()
    with pytest.raises(t.PlanError, match="нужно удаление"):
        plan([r for r in src if r["sku"] != "900002"], raw)
    with pytest.raises(t.PlanError, match="дважды"):
        plan(src + [src[0]], raw)


def test_plan_respects_scope_dates():
    src, raw = world()
    m = plan(src, raw, "2026-09-02", "2026-09-30")
    assert {x["date"] for x in m["rows"]} == {"2026-09-03"}


def test_apply_is_dry_run_without_exact_confirmation(tmp_path, capsys):
    src, raw = world()
    m = plan(src, raw)
    f = tmp_path / "m.json"
    f.write_text(json.dumps(m, default=t._jsonable), encoding="utf-8")
    assert t.main(["apply", "--manifest", str(f), "--fingerprint", m["fingerprint"],
                   "--confirm", "APPLY-wrong"]) == 0
    out = capsys.readouterr()
    assert "MERGE `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_ADS_SKU_DAILY`" in out.out
    assert f"--confirm APPLY-{m['fingerprint'][:12]}" in out.err


def test_apply_refuses_foreign_fingerprint(tmp_path):
    src, raw = world()
    m = plan(src, raw)
    f = tmp_path / "m.json"
    f.write_text(json.dumps(m, default=t._jsonable), encoding="utf-8")
    with pytest.raises(SystemExit, match="отпечаток не совпал"):
        t.main(["apply", "--manifest", str(f), "--fingerprint", "0" * 64,
                "--confirm", "APPLY-000000000000"])


def test_apply_sql_guards_before_and_after_images_in_one_transaction():
    src, raw = world()
    m = roundtrip(plan(src, raw))
    sql = t.apply_sql(m, "p")
    assert sql.index("BEGIN TRANSACTION") < sql.index("ASSERT") < sql.index("MERGE") < sql.rindex("ASSERT") \
        < sql.index("COMMIT TRANSACTION")
    assert "NUMERIC '100.00'" in sql and "NUMERIC '120.50'" in sql and "'rt-old'" in sql
    assert sql.count("STRUCT(") == 2 * len(m["rows"])
    rb = t.apply_sql(m, "p", rollback=True)
    # откат: ожидается образ ПОСЛЕ, восстанавливается образ ДО, вставленная строка удаляется
    assert "откат D2" in rb and "DELETE FROM `p.ozon_raw.RAW_OZON_ADS_SKU_DAILY`" in rb
    pre_rb = rb.split("CREATE TEMP TABLE d2_post")[0]
    assert "NUMERIC '120.50'" in pre_rb and "NUMERIC '100.00'" not in pre_rb


def test_check_sql_is_read_only():
    src, raw = world()
    sql = t.check_sql(roundtrip(plan(src, raw)), "p")
    assert sql.lstrip().startswith("WITH")
    for word in ("MERGE", "INSERT", "UPDATE", "DELETE", "CREATE", "BEGIN"):
        assert word not in sql.upper()


def test_unsafe_literal_is_refused():
    src, raw = world()
    src[5]["sku"] = "900003'; DROP"           # вставляемая строка: план строится, SQL — нет
    with pytest.raises(t.PlanError, match="недопустимый символ"):
        t.apply_sql(roundtrip(plan(src, raw)), "p")
