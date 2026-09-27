"""AIE V1 — офлайн-тесты контракта движка рекламных рекомендаций (без сети и BigQuery)."""
from __future__ import annotations

import datetime as dt
import json
import re
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))

import aie_render as R  # noqa: E402

ROOT = TOOLS.parent
CODES = json.loads((ROOT / "sql" / "ads_intel" / "aie_reason_codes_v1.json").read_text(encoding="utf-8"))
AIE_FILES = [ROOT / folder / f"{name}.sql" for _ds, name, folder in R.OBJECTS if (ROOT / folder / f"{name}.sql").exists()]
DECISION = ROOT / "sql" / "current" / "evetis_mart" / "V_AIE_DECISION_CURRENT.sql"
POLICY = ROOT / "sql" / "ads_intel" / "evetis_ref" / "V_AIE_POLICY.sql"
CLOCKED_FILES = [p for p in AIE_FILES if p != POLICY]
# Решения владельца 2026-09-27 (Shadow V1). Всё остальное — NULL: скрытых значений по умолчанию нет.
APPROVED_POLICY = {"p3": 0.90, "p7": 3.0, "k": 14}


def _code_rows():
    return {c["code"]: c for c in CODES["codes"]}


# ── контракт кодов причин ──────────────────────────────────────────────────────
def test_reason_codes_are_unique_and_well_formed():
    codes = [c["code"] for c in CODES["codes"]]
    ranks = [c["rank"] for c in CODES["codes"]]
    assert len(codes) == len(set(codes))
    assert len(ranks) == len(set(ranks))
    for c in CODES["codes"]:
        assert re.fullmatch(r"[A-Z0-9_]+", c["code"]), c["code"]
        assert c["level"] in CODES["levels"], c
        assert c["state"] is None or c["state"] in CODES["states"], c
        assert c["text_ru"], c


def test_every_state_is_reachable_by_some_code():
    reachable = {c["state"] for c in CODES["codes"] if c["state"]}
    assert reachable == set(CODES["states"])


def test_increase_is_only_reachable_through_the_last_level():
    inc = [c for c in CODES["codes"] if c["state"] == "INCREASE"]
    assert [c["code"] for c in inc] == ["ECON_DRR_BELOW_TARGET_CONFIDENT"]
    assert inc[0]["rank"] == max(c["rank"] for c in CODES["codes"] if c["state"])


def test_reason_code_doc_matches_json():
    doc = (ROOT / "docs" / "ads_intel" / "AIE_REASON_CODES_V1.md").read_text(encoding="utf-8")
    for c in CODES["codes"]:
        row = f"| {c['rank']} | `{c['code']}` | {c['level']} | {c['state'] or '—'} |"
        assert row in doc, c["code"]


# ── SQL представлений ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("path", AIE_FILES, ids=lambda p: p.stem)
def test_view_file_is_a_single_read_only_view(path):
    text = path.read_text(encoding="utf-8")
    assert text.count("CREATE OR REPLACE VIEW") == 1
    body = R.view_body(text)
    bare = re.sub(r"--[^\n]*", "", body)
    for word in ("INSERT", "UPDATE", "DELETE", "MERGE", "TRUNCATE", "DROP", "ALTER", "CREATE"):
        assert not re.search(rf"\b{word}\b", bare, re.I), (path.name, word)


@pytest.mark.parametrize("path", CLOCKED_FILES, ids=lambda p: p.stem)
def test_view_has_exactly_one_clock_and_no_other_now(path):
    body = R.view_body(path.read_text(encoding="utf-8"))
    assert body.count("-- @aie:clock:begin") == 1 and body.count("-- @aie:clock:end") == 1, path.name
    outside = R.MARKER.sub("", body)
    bare = re.sub(r"--[^\n]*", "", outside)
    assert not re.search(r"CURRENT_(DATE|TIMESTAMP|DATETIME|TIME)\b", bare), path.name


@pytest.mark.parametrize("path", AIE_FILES, ids=lambda p: p.stem)
def test_view_never_reads_billing_and_is_deterministic(path):
    body = re.sub(r"--[^\n]*", "", R.view_body(path.read_text(encoding="utf-8")))
    # FIN CONTRACT V2: движок работает на атрибуции; биллинг не читается и по SKU не распределяется.
    assert "FACT_ADS_COSTS_DAILY" not in body and "V_ADV_COSTS" not in body, path.name
    for fn in ("ANY_VALUE", "RAND(", "GENERATE_UUID", "SESSION_USER"):
        assert fn not in body.upper(), (path.name, fn)
    for agg in re.finditer(r"ARRAY_AGG\s*\((.*?)\)\s*(AS|,|\))", body, re.S):
        assert "ORDER BY" in agg.group(1).upper(), (path.name, agg.group(0)[:80])


def test_no_marketplace_write_hosts_in_aie_code():
    hosts = ("advert-api.wildberries.ru", "api-performance.ozon.ru", "api-seller.ozon.ru", "feedbacks-api")
    for path in [*AIE_FILES, ROOT / "tools" / "aie_render.py", *sorted((ROOT / "tools").glob("aie_*.py"))]:
        text = path.read_text(encoding="utf-8")
        for h in hosts:
            assert h not in text, (path.name, h)


# ── рендер ─────────────────────────────────────────────────────────────────────
def test_predeploy_inlines_every_aie_object():
    refs = [R.ref(f"{ds}.{name}") for ds, name, folder in R.OBJECTS if (ROOT / folder / f"{name}.sql").exists()]
    sql = "SELECT 1 FROM " + " , ".join(refs)
    out = R.render_predeploy(sql)
    for r in refs:
        assert r not in out


FAKE_LIVE = {
    "wb_raw.V_ADV_CAMPAIGN_STATS":
        "SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_ADV_CAMPAIGN_STATS` WHERE processed_status = 'raw'",
    "wb_mart.V_ADS_FUNNEL_QUERY_28D":
        "SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_ADS_FUNNEL_QUERY_DAILY` d "
        "JOIN `project-fa311fc0-4d87-4781-986.wb_raw.V_ADV_QUERY_BIDS` b ON TRUE",
    "wb_mart.V_ADS_FUNNEL_QUERY_DAILY":
        "SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_ADV_QUERY_STATS`",
    "wb_raw.V_ADV_QUERY_STATS":
        "SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_ADV_QUERY_STATS` s "
        "JOIN `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_ADV_QUERY_STATS_RUNS` r ON TRUE",
    "wb_raw.V_ADV_QUERY_BIDS":
        "SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_ADV_QUERY_BIDS` s "
        "JOIN `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_ADV_QUERY_BIDS_RUNS` r ON TRUE",
}


def _replay_all(as_of=dt.date(2026, 9, 1)):
    refs = [R.ref(f"{ds}.{name}") for ds, name, folder in R.OBJECTS if (ROOT / folder / f"{name}.sql").exists()]
    return R.render_replay("SELECT 1 FROM " + " , ".join(refs), as_of, FAKE_LIVE)


def test_replay_has_no_clock_leak_and_uses_the_replay_clock():
    out = _replay_all()
    bare = re.sub(r"--[^\n]*", "", out)
    assert not re.search(r"CURRENT_(DATE|TIMESTAMP|DATETIME|TIME)\b", bare)
    assert "'CURRENT' AS run_mode" not in out
    # Встроенные зависимости повторяют свои часы; каждая копия обязана быть часами replay.
    assert out.count("-- @aie:clock:begin") == out.count("DATE '2026-09-01' AS as_of_date") >= len(CLOCKED_FILES)
    assert "TIMESTAMP('2026-09-02 09:00:00', 'Europe/Moscow')" in out


def test_replay_reads_ads_facts_only_as_known_at_knowledge_time():
    out = _replay_all()
    assert R.ref("wb_mart.FACT_ADS_SKU_DAILY") not in out
    assert R.ref("wb_raw.V_ADV_CAMPAIGN_STATS") not in out
    assert out.startswith("WITH aie_pit_fact_ads_sku_daily AS (")
    for raw in R.PIT_RAW:
        for m in re.finditer(re.escape(R.ref(raw)), out):
            tail = out[m.end(): m.end() + 120]
            assert "WHERE TIMESTAMP(SAFE_CAST(load_ts AS DATETIME), 'Europe/Moscow') <=" in tail, raw


def test_replay_empties_current_only_objects():
    out = _replay_all()
    for obj in R.CURRENT_ONLY:
        for m in re.finditer(re.escape(R.ref(obj)), out):
            assert out[m.end(): m.end() + 12] == " WHERE FALSE", obj


def test_fact_build_is_taken_from_the_mart_procedure_not_copied():
    body = R.fact_ads_build_select()
    assert "FROM `wb_raw.V_ADV_CAMPAIGN_STATS`" in body
    assert "ads_revenue_dedup_estimate_rub" in body and "@" not in body


# ── решение (PR-4) ─────────────────────────────────────────────────────────────
def _decision_body():
    if not DECISION.exists():
        pytest.skip("V_AIE_DECISION_CURRENT ещё не реализован")
    return R.view_body(DECISION.read_text(encoding="utf-8"))


def test_policy_view_is_the_only_policy_block_and_has_no_clock():
    text = POLICY.read_text(encoding="utf-8")
    body = R.view_body(text)
    assert body.count("-- @aie:policy:begin") == 1 and body.count("-- @aie:policy:end") == 1
    assert "@aie:clock" not in body
    assert not re.search(r"CURRENT_(DATE|TIMESTAMP|DATETIME|TIME)\b", re.sub(r"--[^\n]*", "", body))
    # Ни одно другое представление AIE не держит собственной политики — только читает V_AIE_POLICY.
    for path in AIE_FILES:
        if path == POLICY:
            continue
        other = R.view_body(path.read_text(encoding="utf-8"))
        assert "@aie:policy" not in other, path.name
        assert "aie_policy AS" not in other, path.name


def test_policy_in_git_is_exactly_the_owner_decision():
    pol = R.git_policy()
    assert pol["policy_id"] == "V1_SHADOW_2026-09-27"
    for key, value in pol.items():
        if key == "policy_id":
            continue
        if key in APPROVED_POLICY:
            assert value == APPROVED_POLICY[key], key
        else:
            assert value is None, f"{key}: решения владельца нет — в Git должен быть NULL"


def test_p7_threshold_is_not_duplicated_as_a_constant():
    # P7 применяется в трёх доменных представлениях и берётся только из V_AIE_POLICY.
    users = ["V_AIE_WB_PAIR_EVIDENCE", "V_AIE_WB_ECON_GUARD", "V_AIE_OZON_PAIR_EVIDENCE"]
    for path in AIE_FILES:
        body = re.sub(r"--[^\n]*", "", R.view_body(path.read_text(encoding="utf-8")))
        if path.stem in users:
            assert "p7_price_change_pct" in body and R.ref("evetis_ref.V_AIE_POLICY") in body, path.name
        if path != POLICY:
            assert not re.search(r"(?<![\w.])3(\.0+)?\s*(AS\s+p7|\)\s*AS\s+p7)", body), path.name
            assert not re.search(r">=\s*3(\.0+)?\b", body), path.name


def test_decision_reads_policy_and_joins_every_policy_grain():
    body = _decision_body()
    assert R.ref("evetis_ref.V_AIE_POLICY") in body
    assert "e.policy_id = w.policy_id" in body
    assert "JOIN pol p ON p.policy_id = x.policy_id" in body


def test_decision_universe_rule_matches_the_owner_decision():
    body = re.sub(r"\s+", " ", _decision_body())
    assert "(w.campaign_status_raw IN ('9', '11')) AS campaign_active_status" in body
    assert "(o.campaign_state = 'RUNNING') AS campaign_active_status" in body
    # Семантика Ozon INACTIVE не доказана — в текущий набор не входит (fail closed, решение владельца 2026-09-27).
    assert not re.search(r"campaign_state\s+IN\s*\([^)]*'INACTIVE'", body)
    # 'INACTIVE' встречается только в тексте объяснения исключения, а не в правиле набора.
    assert body.count("'INACTIVE'") == 1 and "d.campaign_status = 'INACTIVE'" in body
    assert ("IFNULL(x.campaign_active_status IS TRUE AND (p.k_inactive_days IS NULL "
            "OR x.days_since_last_spend <= p.k_inactive_days), FALSE) AS in_current_universe") in body
    assert "IF(d.in_current_universe, 'CURRENT_ACTIONABLE', 'HISTORICAL_EVIDENCE_ONLY') AS universe" in body
    assert "NOT a.in_current_universe AS fired" in body


def test_decision_p6_roles():
    body = re.sub(r"\s+", " ", _decision_body())
    # realized — единственная граница DECREASE/HOLD; прогнозные базы — только ограничители и флаги.
    assert "x.limit_realized AS limit_drr" in body
    assert "d.limit_drr IS NOT NULL" in body
    assert "f.drr_high < f.limit_forward_conservative THEN 'PASS'" in body
    assert "a.c_below_target_raw AND a.conservative_guard != 'PASS'" in body
    assert "limit_forward_stress" in body and "'ECON_STRESS_RISK_FLAG'" in body
    stress_uses = [m.start() for m in re.finditer("limit_forward_stress", body)]
    assert all("ECON_STRESS_RISK_FLAG" in body[max(0, i - 400): i + 200] or "AS limit_forward_stress" in body[i - 60: i + 30]
               or "d.limit_forward_stress," in body[i - 5: i + 25] for i in stress_uses)


def test_increase_candidate_is_diagnostic_only():
    rows = _code_rows()
    assert rows["INCREASE_CANDIDATE"]["state"] is None
    body = _decision_body()
    assert "AS increase_candidate" in body
    assert "increase_candidate" not in re.search(r"STRUCT\('ECON_DRR_BELOW_TARGET_CONFIDENT'.*?AS fired\)", body, re.S).group(0)


def test_price_below_breakeven_routes_to_pricing_review_without_sku_exceptions():
    rows = _code_rows()
    assert rows["PRICE_BELOW_BREAKEVEN"]["state"] == "PAUSE_CANDIDATE"
    assert rows["PRICING_REVIEW"]["state"] is None
    body = _decision_body()
    for col in ("current_effective_price_rub", "breakeven_price_rub", "contribution_before_ads_rub", "commission_rub",
                "commission_pct", "acquiring_rub", "acquiring_pct", "logistics_rub", "logistics_pct",
                "product_cogs_rub", "product_cogs_pct"):
        assert f"AS {col}," in body, col
    assert "'PRICING_REVIEW', NULL) AS routing" in body
    assert not re.search(r"internal_sku\s*(=|IN)\s*\(?'EVT-", body), "исключений по SKU нет (P4)"
    assert "ECON_NEGATIVE_ALL_AVAILABLE_BASES" not in body and "POLICY_P4" not in body


def test_inventory_states_and_bundles():
    body = re.sub(r"\s+", " ", _decision_body())
    for st in ("INV_COVER_UNAVAILABLE", "INV_COVER_POLICY_NOT_SET", "INV_LOW_COVER", "INV_NORMAL_COVER",
               "INV_OVERSTOCK_CONTEXT"):
        assert f"'{st}'" in body, st
    assert "WHEN d.is_bundle IS TRUE OR d.cover_days IS NULL THEN 'INV_COVER_UNAVAILABLE'" in body


def test_financial_maturity_is_a_flag_not_an_n_day_exclusion():
    econ = (ROOT / "sql" / "ads_intel" / "wb_mart" / "V_AIE_WB_ECON_GUARD.sql").read_text(encoding="utf-8")
    body = re.sub(r"--[^\n]*", "", R.view_body(econ))
    assert "(w.econ_as_of <= b.finance_known_through) AS financial_data_mature" in body
    assert not re.search(r"DATE_SUB\(c\.as_of_date, INTERVAL [1-9]\d* DAY\) AS econ_as_of", body)
    assert "financial_data_mature" in _decision_body()


def test_decision_codes_and_ranks_match_the_contract():
    body = _decision_body()
    rows = _code_rows()
    used = set(re.findall(r"STRUCT\('([A-Z0-9_]+)' AS code, (\d+) AS rank, (CAST\(NULL AS STRING\)|'[A-Z_]+') AS state",
                          body))
    assert used, "коды причин не найдены"
    assert {c for c, _r, _s in used} == set(rows), "SQL и контракт расходятся по составу кодов"
    for code, rank, state in used:
        assert code in rows, code
        assert int(rank) == rows[code]["rank"], code
        expected = "CAST(NULL AS STRING)" if rows[code]["state"] is None else f"'{rows[code]['state']}'"
        assert state == expected, code


def test_decision_emits_delta_as_not_derivable():
    body = _decision_body()
    assert "CAST(NULL AS FLOAT64) AS proposed_delta" in body
    assert "'DELTA_NOT_DERIVABLE' AS delta_basis" in body


def test_query_class_is_diagnostic_only():
    path = ROOT / "sql" / "ads_intel" / "wb_mart" / "V_AIE_WB_QUERY_CLASS.sql"
    body = re.sub(r"--[^\n]*", "", R.view_body(path.read_text(encoding="utf-8")))
    for word in ("recommendation", "'INCREASE'", "'DECREASE'", "proposed_delta", "bid_direction'"):
        assert word not in body, word
    assert "FALSE AS is_bid_direction_source" in body


def test_policy_grid_is_typed_and_replaces_the_git_policy_only_in_replay():
    grid = R.policy_grid_cte([{"policy_id": "G1", "p1": None, "p3": 0.9, "p7": 3.0, "k": 14},
                              {"policy_id": "G2", "p1": 0.2, "p2": 0.01, "p3": 0.95, "p5_low": 14, "p7": 3.0, "k": 14}])
    out = R.render_replay(f"SELECT * FROM {R.ref('evetis_mart.V_AIE_DECISION_CURRENT')}", dt.date(2026, 9, 1),
                          FAKE_LIVE, policy_cte=grid)
    assert "'V1_SHADOW_2026-09-27'" not in out
    assert "('G2', 0.2, 0.01, 0.95, 14.0, CAST(NULL AS FLOAT64), 3.0, CAST(NULL AS INT64), 14)" in out
    assert ("('G1', CAST(NULL AS FLOAT64), CAST(NULL AS FLOAT64), 0.9, CAST(NULL AS FLOAT64), CAST(NULL AS FLOAT64), "
            "3.0, CAST(NULL AS INT64), 14)") in out
    # Без сетки replay исполняет политику из Git как есть.
    plain = R.render_replay(f"SELECT * FROM {R.ref('evetis_mart.V_AIE_DECISION_CURRENT')}", dt.date(2026, 9, 1), FAKE_LIVE)
    assert "'V1_SHADOW_2026-09-27'" in plain


def test_cooldown_simulation_holds_direction_for_n_days():
    import aie_backtest as B
    seq = [("2026-09-01", "DECREASE"), ("2026-09-02", "INCREASE"), ("2026-09-05", "INCREASE"), ("2026-09-06", "HOLD")]
    assert B.simulate_cooldown(seq, 0) == ["DECREASE", "INCREASE", "INCREASE", "HOLD"]
    assert B.simulate_cooldown(seq, 3) == ["DECREASE", "DECREASE", "INCREASE", "HOLD"]
    flips, days, runs = B.flips_and_durations(["DECREASE", "HOLD", "INCREASE", "INCREASE"])
    assert (flips, days, runs) == (1, 4, [1, 1, 2])


def test_backtest_grid_never_becomes_policy():
    import aie_backtest as B
    grid = B.policy_grid()
    assert grid[0] == R.git_policy()
    assert all(p["policy_id"] != grid[0]["policy_id"] for p in grid[1:])
    # Сетка варьирует только нерешённые параметры; утверждённые P7 и K фиксированы, P6 больше не параметр.
    assert all(p["p7"] == 3.0 and p["k"] == 14 and "p6" not in p for p in grid[1:])
    # сетка не пишется ни в Git-тело решения, ни в какую-либо таблицу
    body = R.view_body(DECISION.read_text(encoding="utf-8")) if DECISION.exists() else ""
    for p in grid[1:]:
        assert p["policy_id"] not in body


def test_rollback_drops_every_aie_object_in_reverse_dependency_order():
    text = (ROOT / "sql" / "ads_intel" / "aie_rollback.sql").read_text(encoding="utf-8")
    dropped = re.findall(r"^DROP VIEW IF EXISTS `project-fa311fc0-4d87-4781-986\.(\w+\.\w+)`;$", text, re.M)
    assert dropped == [f"{ds}.{name}" for ds, name, _f in reversed(R.OBJECTS)]
    bare = re.sub(r"--[^\n]*", "", text)
    assert not re.search(r"\b(DROP TABLE|DELETE|TRUNCATE|UPDATE|INSERT|MERGE)\b", bare, re.I)


# ── fail-closed инварианты политики (Commit/PR Gate 2026-09-27) ────────────────
UNRESOLVED = {"p1": "p1_reserve_share", "p2": "p2_band_pp", "p5_low": "p5_low_cover_days",
              "p5_overstock": "p5_overstock_cover_days", "p13": "p13_cooldown_days"}


@pytest.mark.parametrize("key", sorted(UNRESOLVED))
def test_unresolved_policy_is_null_in_git(key):
    assert R.git_policy()[key] is None, f"{key}: решения владельца нет — только NULL"


@pytest.mark.parametrize("path", AIE_FILES, ids=lambda p: p.stem)
def test_no_runtime_default_replaces_unresolved_policy(path):
    body = re.sub(r"--[^\n]*", "", R.view_body(path.read_text(encoding="utf-8")))
    for col in UNRESOLVED.values():
        # Ни IFNULL/COALESCE/NVL, ни CASE WHEN … IS NULL THEN <число> не подставляют значение вместо NULL.
        assert not re.search(rf"(IFNULL|COALESCE|NVL)\s*\([^()]*\b{col}\b", body, re.I), (path.name, col)
        assert not re.search(rf"\b{col}\b\s+IS\s+NULL\s+THEN\s+-?[\d.]+", body, re.I), (path.name, col)
    if path == POLICY:
        return
    # Значения политики берутся только из V_AIE_POLICY; своих литералов нет.
    for col in UNRESOLVED.values():
        assert not re.search(rf"AS\s+{col}\b", body) or path == DECISION, (path.name, col)


def test_increase_requires_p1_structurally():
    body = re.sub(r"\s+", " ", _decision_body())
    # c_below_target_raw — единственный вход в ECON_DRR_BELOW_TARGET_CONFIDENT — требует P1 IS NOT NULL.
    assert "f.p1_reserve_share IS NOT NULL AND f.drr_high < f.target_drr) AS c_below_target_raw" in body
    inc = re.search(r"STRUCT\('ECON_DRR_BELOW_TARGET_CONFIDENT'.*?AS fired\)", body).group(0)
    assert "a.c_below_target_raw" in inc and "a.p2_band_pp IS NOT NULL" in inc
    assert "INV_NORMAL_COVER" in inc  # без P5 состояние NORMAL/OVERSTOCK недостижимо
    # target_drr = limit × (1 − P1): при NULL P1 цель NULL, а не лимит.
    assert "i.limit_drr * (1 - i.p1_reserve_share) AS target_drr" in body
    # INCREASE — единственное состояние кода 904, и оно последнее в иерархии.
    assert _code_rows()["ECON_DRR_BELOW_TARGET_CONFIDENT"]["state"] == "INCREASE"


def test_increase_candidate_is_not_executable():
    body = re.sub(r"\s+", " ", _decision_body())
    assert _code_rows()["INCREASE_CANDIDATE"]["state"] is None
    # Направление строится только из recommendation; флаг кандидата в него не входит.
    direction = re.search(r"CASE IFNULL\(d\.primary_reason\.state, 'BLOCKED_BY_GUARDRAIL'\).*?END AS proposed_direction", body).group(0)
    assert "increase_candidate" not in direction
    assert "CAST(NULL AS FLOAT64) AS proposed_delta" in body


def test_ozon_is_never_production_grade():
    body = re.sub(r"\s+", " ", _decision_body())
    assert "FALSE AS production_grade_data" in body  # ветка Ozon
    assert "(d.marketplace = 'WB' AND d.production_grade_data" in body
    for name in ("V_AIE_OZON_PAIR_EVIDENCE", "V_AIE_OZON_ECON_GUARD"):
        text = (ROOT / "sql" / "current" / "ozon_mart" / f"{name}.sql").read_text(encoding="utf-8")
        assert "FALSE AS production_grade" in re.sub(r"\s+", " ", R.view_body(text)), name


@pytest.mark.parametrize("path", AIE_FILES, ids=lambda p: p.stem)
def test_no_execution_path_in_aie_views(path):
    body = re.sub(r"--[^\n]*", "", R.view_body(path.read_text(encoding="utf-8")))
    for pattern in (r"\bEXTERNAL_QUERY\b", r"\bEXECUTE\s+IMMEDIATE\b", r"\bCALL\b", r"\bML\.", r"\bREMOTE\b",
                    r"\bCONNECTION\b", r"\bEXPORT\s+DATA\b", r"\bLOAD\s+DATA\b", r"https?://", r"\bBEGIN\b",
                    r"\bCREATE\s+TEMP", r"\bSET\s+@@"):
        assert not re.search(pattern, body, re.I), (path.name, pattern)
