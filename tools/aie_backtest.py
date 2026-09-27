#!/usr/bin/env python3
"""AIE V1 (PR-5) — read-only replay/backtest движка рекламных рекомендаций на истории EVETIS.

Что делает. Для каждой даты решения D из диапазона исполняет ТЕ ЖЕ тела представлений из Git
(tools/aie_render.render_replay): часы D, knowledge_ts = D+1 09:00 МСК, реклама — из RAW по load_ts,
объекты только текущего состояния — пусто. Основной прогон — на утверждённой политике из Git
(evetis_ref.V_AIE_POLICY: P3 = 0.90, P7 = 3 %, K = 14; P1, P2, P5, P13 — NULL) без подстановки.
Сетка кандидатов для нерешённых P1/P2/P5 (команда run без --no-grid) — анализ чувствительности, не решение
и не политика: она подставляется в блок @aie:policy только в тексте запроса replay. «Лучший» параметр
автоматически не выбирается: последствия на истории — наблюдательные данные, не эксперимент.

Команды (все — только SELECT через tools/lib/bq_readonly):
  parity     доказать, что реконструкция FACT_ADS_SKU_DAILY из RAW при knowledge_ts = сейчас совпадает
             с production-таблицей до копейки (иначе replay не «та же логика»);
  run        replay решений и классов запросов + наблюдаемые последствия → JSONL/JSON в --out (вне Git);
             --no-grid — только утверждённая политика (final replay);
  report     агрегаты и Owner Decision Pack (markdown) из результатов run;
  final      итог final replay: текущий набор и историческая база, состояния, причины, диагностика,
             инварианты → final_summary.json и final_tables.md в --out.

usage:
  python tools/aie_backtest.py parity --project P --token-command "gcloud auth print-access-token"
  python tools/aie_backtest.py run --project P --token-command "…" --from 2026-07-26 --to 2026-09-25 --out DIR
  python tools/aie_backtest.py run … --no-grid --out DIR && python tools/aie_backtest.py final --out DIR
  python tools/aie_backtest.py report --out DIR
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import aie_render as R  # noqa: E402

DECISION = R.ref("evetis_mart.V_AIE_DECISION_CURRENT")
QUERY_CLASS = R.ref("wb_mart.V_AIE_WB_QUERY_CLASS")

# ── сетка кандидатов (чувствительность, не политика) ──────────────────────────────
# P7 и K в сетке — утверждённые значения из Git (V_AIE_POLICY); варьируются только нерешённые параметры.
# P3: поддерживаемые уровни. P1: доля резерва от лимита. P2: полоса в долях ДРР (0,01 = 1 п.п.; 0 — только
#     исследование, не значение по умолчанию). P5 — нижняя граница покрытия (верхняя не варьируется:
#     одного наблюдения 487 суток мало для порога): 0 — ограничитель выключен; 14 = срок поставки WB 7 +
#     страховой запас 7; 20 = Ozon 10 + 10;
#     45 — целевое покрытие C0 D4 (docs/ops/STAGE_C0_LOGISTICS_MASTER_2026-09-11.md).
GRID_P3 = [0.80, 0.90, 0.95]
GRID_P1 = [0.10, 0.20, 0.30, 0.40, 0.50]
GRID_P2 = [0.0, 0.01, 0.02]
GRID_P5 = [0.0, 14.0, 20.0, 45.0]
GRID_P13 = [0, 3, 7, 14]
GRID_P15 = [(0.7, 1.3), (0.6, 1.4), (0.8, 1.2), (0.5, 1.5)]


BASE = R.git_policy()
BASE_ID = BASE["policy_id"]


def policy_label(p: dict) -> str:
    if p["policy_id"] == BASE_ID:
        return BASE_ID
    if p.get("p1") is None:
        return f"P3={p['p3']}|P1=NULL"
    return f"P3={p['p3']}|P1={p['p1']}|P2={p['p2']}|P5L={p['p5_low']}"


def policy_grid() -> list[dict]:
    """grid[0] — утверждённая политика из Git; дальше кандидаты (короткий policy_id, расшифровка — policy_label)."""
    out = [dict(BASE)]
    fixed = {"p7": BASE["p7"], "k": BASE["k"]}
    raw = []
    for p3 in GRID_P3:
        raw.append({"p3": p3, **fixed})
        for p1 in GRID_P1:
            for p2 in GRID_P2:
                for p5 in GRID_P5:
                    raw.append({"p1": p1, "p2": p2, "p3": p3, "p5_low": p5, **fixed})
    for i, p in enumerate(raw, start=1):
        out.append({"policy_id": f"g{i:03d}", **p})
    return out


DECISION_COLUMNS = (
    "as_of_date, run_mode, policy_id, marketplace, internal_sku, marketplace_sku, campaign_id, universe, campaign_status, "
    "last_spend_date, days_since_last_spend, payment_type, autopilot_strategy, recommendation, primary_reason_code, "
    "reason_codes, proposed_direction, evidence_status, evidence_window_start, effective_days, eff_clicks, eff_orders, "
    "eff_spend_attributed_rub, spend_28d_attributed_rub, economic_state, econ_as_of, financial_data_mature, "
    "econ_window_end_lag_days, sku_buyouts_qty, sku_drr_point, drr_low, drr_high, econ_limit_drr, target_drr, "
    "forward_availability, conservative_guard, increase_candidate, increase_candidate_realized, "
    "current_effective_price_rub, breakeven_price_rub, contribution_before_ads_rub, routing, "
    "stock_status, stock_units, is_bundle, cover_days, inventory_cover_state, p1_reserve_share, p2_band_pp, "
    "p3_confidence, p5_low_cover_days, p5_overstock_cover_days, p7_price_change_pct, p13_cooldown_days, "
    "k_inactive_days, production_grade, input_manifest")

GRID_COLUMNS = "as_of_date, policy_id, marketplace, campaign_id, marketplace_sku, recommendation, primary_reason_code"

QUERY_COLUMNS = (
    "aie_as_of_date, query_as_of_date, query_as_of_aligned, nm_id, internal_sku, norm_query, evidence_status, "
    "spend_attributed_rub, clicks_sum, orders_sum, cpc_rub, cpo_rub, cpc_vs_baseline, cart_cr_vs_baseline, "
    "sku_economic_state, econ_cpo_ceiling_rub, econ_cpc_ceiling_rub, q_insufficient, q_traffic_no_conversion, "
    "q_high_cpc, q_econ_cannot_raise, q_converting, q_possibly_underexposed, primary_query_class, "
    "diag_below_mult, diag_above_mult")


def daterange(a: dt.date, b: dt.date):
    d = a
    while d <= b:
        yield d
        d += dt.timedelta(days=1)


# ── parity ─────────────────────────────────────────────────────────────────────
def parity_sql(live: dict[str, str]) -> str:
    """Реконструкция FACT «на сейчас» против FACT_ADS_SKU_DAILY: каждая строка и каждая мера."""
    today = dt.datetime.now(dt.timezone(dt.timedelta(hours=3))).date()
    now_as_of = today - dt.timedelta(days=1)
    # knowledge_ts = завтра 09:00 МСК, то есть «всё загруженное к этому моменту»: реконструкция обязана
    # совпасть с FACT, собранным из того же RAW.
    pit = R.pit_fact_ads(live, today)
    return f"""
WITH p AS (SELECT * FROM {pit} WHERE `date` <= DATE '{now_as_of}'),
f AS (SELECT * FROM {R.ref('wb_mart.FACT_ADS_SKU_DAILY')} WHERE `date` <= DATE '{now_as_of}'),
j AS (
  SELECT p.`date` AS pd, f.`date` AS fd,
         (p.views IS NOT DISTINCT FROM f.views AND p.clicks IS NOT DISTINCT FROM f.clicks
          AND p.stats_spend_rub IS NOT DISTINCT FROM f.stats_spend_rub AND p.ad_orders_raw IS NOT DISTINCT FROM f.ad_orders_raw
          AND p.ads_revenue_raw_rub IS NOT DISTINCT FROM f.ads_revenue_raw_rub
          AND p.ads_revenue_dedup_estimate_rub IS NOT DISTINCT FROM f.ads_revenue_dedup_estimate_rub
          AND p.ad_orders_dedup_estimate IS NOT DISTINCT FROM f.ad_orders_dedup_estimate
          AND p.multitouch_ambiguous_flag IS NOT DISTINCT FROM f.multitouch_ambiguous_flag) AS same
  FROM p FULL OUTER JOIN f USING (`date`, advert_id, nm_id)
),
raw AS (
  SELECT COUNTIF(load_ts IS NOT NULL AND SAFE_CAST(load_ts AS DATETIME) IS NULL) AS unparsable_load_ts, COUNT(*) AS raw_rows
  FROM {R.ref('wb_raw.RAW_WB_ADV_CAMPAIGN_STATS')}
)
SELECT COUNT(*) AS keys, COUNTIF(pd IS NULL) AS missing_in_pit, COUNTIF(fd IS NULL) AS extra_in_pit,
       COUNTIF(pd IS NOT NULL AND fd IS NOT NULL AND NOT same) AS value_mismatch,
       ANY_VALUE(raw.unparsable_load_ts) AS unparsable_load_ts, ANY_VALUE(raw.raw_rows) AS raw_rows,
       IF(COUNTIF(pd IS NULL) + COUNTIF(fd IS NULL) + COUNTIF(pd IS NOT NULL AND fd IS NOT NULL AND NOT same) = 0
          AND ANY_VALUE(raw.unparsable_load_ts) = 0, 'PASS', 'FAIL') AS status
FROM j CROSS JOIN raw
"""


# ── run ────────────────────────────────────────────────────────────────────────
def run(bq, start: dt.date, end: dt.date, out: Path, query_start: dt.date, with_grid: bool = True) -> None:
    out.mkdir(parents=True, exist_ok=True)
    live = R.fetch_live_bodies(bq)
    grid = policy_grid() if with_grid else policy_grid()[:1]
    meta = {"from": str(start), "to": str(end), "query_from": str(query_start), "policies": len(grid),
            "base_policy": BASE, "with_grid": with_grid,
            "policy_labels": {p["policy_id"]: policy_label(p) for p in grid},
            "knowledge_ts_rule": "D+1 09:00 Europe/Moscow", "generated_at": dt.datetime.now(dt.timezone.utc).isoformat()}
    with (out / "decisions.jsonl").open("w", encoding="utf-8") as fd, (out / "grid.jsonl").open("w", encoding="utf-8") as fg, \
            (out / "queries.jsonl").open("w", encoding="utf-8") as fq:
        for d in daterange(start, end):
            # Утверждённая политика — как в Git, без подстановки блока @aie:policy.
            sql = R.render_replay(f"SELECT {DECISION_COLUMNS} FROM {DECISION}", d, live)
            rows = bq.query(sql, max_results=100000)
            for r in rows:
                fd.write(json.dumps(r, ensure_ascii=False) + "\n")
            grows = query_grid(bq, d, live, grid[1:]) if grid[1:] else []
            for r in grows:
                fg.write(json.dumps(r, ensure_ascii=False) + "\n")
            nq = 0
            if d >= query_start:
                qsql = R.render_replay(f"SELECT {QUERY_COLUMNS} FROM {QUERY_CLASS}", d, live,
                                       diag_cte=R.diag_grid_cte(GRID_P15))
                qrows = bq.query(qsql, max_results=100000)
                nq = len(qrows)
                for r in qrows:
                    fq.write(json.dumps(r, ensure_ascii=False) + "\n")
            print(f"{d}\tdecisions={len(rows)}\tgrid={len(grows)}\tqueries={nq}\tbytes={bq.bytes_billed}", flush=True)
    outcomes = bq.query(outcome_sql(start, end), max_results=100000)
    with (out / "outcomes.jsonl").open("w", encoding="utf-8") as fo:
        for r in outcomes:
            fo.write(json.dumps(r, ensure_ascii=False) + "\n")
    meta["queries_issued"] = bq.queries_issued
    meta["bytes_processed"] = bq.bytes_billed
    (out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def query_grid(bq, d: dt.date, live: dict[str, str], policies: list[dict]) -> list[dict]:
    """Сетка политик компактными строками; при усечённом ответе сетка делится пополам (ответ не бывает неполным)."""
    from lib.bq_readonly import BigQueryError
    sql = R.render_replay(f"SELECT {GRID_COLUMNS} FROM {DECISION} WHERE marketplace = 'WB'", d, live,
                          policy_cte=R.policy_grid_cte(policies))
    try:
        return bq.query(sql, max_results=100000)
    except BigQueryError as e:
        if "усечён" not in str(e) or len(policies) < 2:
            raise
        half = len(policies) // 2
        return query_grid(bq, d, live, policies[:half]) + query_grid(bq, d, live, policies[half:])


def outcome_sql(start: dt.date, end: dt.date) -> str:
    """Наблюдаемые последствия после даты решения (НЕ причинные): следующие 28 суток по SKU WB и
    смена настроек пары в следующие 14 суток. Используются созревшие данные — это намеренно: это исход,
    а не вход решения."""
    return f"""
WITH days AS (SELECT d FROM UNNEST(GENERATE_DATE_ARRAY(DATE '{start}', DATE '{end}')) d),
m AS (
  SELECT c.day, c.nm_id, m.buyouts_rub, m.marketplace_fee_rub, m.logistics_cost_positive, m.ad_spend,
         c.net_product_cogs_operational_rub AS net_cogs, c.cogs_covered
  FROM {R.ref('wb_mart.V_MART_SKU_DAILY_COGS')} c
  JOIN {R.ref('wb_mart.MART_SKU_DAILY')} m ON m.day = c.day AND m.nm_id = c.nm_id
),
fwd AS (
  SELECT days.d AS as_of_date, m.nm_id,
         COUNT(*) AS days_observed,
         SUM(m.ad_spend) AS next28_ad_spend_attributed_rub,
         SUM(m.buyouts_rub) AS next28_buyouts_rub,
         IF(COUNTIF(NOT m.cogs_covered) = 0,
            SUM(m.buyouts_rub) - SUM(m.marketplace_fee_rub) - SUM(m.logistics_cost_positive) - SUM(m.net_cogs), NULL)
           AS next28_contribution_before_ads_rub
  FROM days JOIN m ON m.day > days.d AND m.day <= DATE_ADD(days.d, INTERVAL 28 DAY)
  GROUP BY 1, 2
),
cfg AS (
  SELECT snapshot_ts, advert_id, nm_id,
         TO_JSON_STRING(STRUCT(search_bid_kopecks, recommendations_bid_kopecks, placement_search_enabled,
                               placement_recommendations_enabled, payment_type_raw, bid_type_raw, status_raw)) AS k
  FROM {R.ref('wb_raw.V_ADV_CAMPAIGN_CONFIG_SNAPSHOT')}
  WHERE nm_id IS NOT NULL
),
chg AS (
  SELECT advert_id, nm_id, DATE(snapshot_ts, 'Europe/Moscow') AS change_date
  FROM (SELECT *, LAG(k) OVER (PARTITION BY advert_id, nm_id ORDER BY snapshot_ts) AS pk FROM cfg)
  WHERE pk IS NOT NULL AND pk != k
),
act AS (
  SELECT days.d AS as_of_date, CAST(c.advert_id AS STRING) AS campaign_id, CAST(c.nm_id AS STRING) AS marketplace_sku,
         MIN(c.change_date) AS first_config_change_after
  FROM days JOIN chg c ON c.change_date > days.d AND c.change_date <= DATE_ADD(days.d, INTERVAL 14 DAY)
  GROUP BY 1, 2, 3
)
SELECT 'SKU' AS kind, CAST(f.as_of_date AS STRING) AS as_of_date, CAST(f.nm_id AS STRING) AS marketplace_sku,
       CAST(NULL AS STRING) AS campaign_id, f.days_observed, f.next28_ad_spend_attributed_rub, f.next28_buyouts_rub,
       f.next28_contribution_before_ads_rub, CAST(NULL AS DATE) AS first_config_change_after
FROM fwd f
UNION ALL
SELECT 'PAIR', CAST(a.as_of_date AS STRING), a.marketplace_sku, a.campaign_id, NULL, NULL, NULL, NULL, a.first_config_change_after
FROM act a
"""


# ── report ─────────────────────────────────────────────────────────────────────
def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def num(v):
    return None if v is None else float(v)


def pct(a, b):
    return "—" if not b else f"{100 * a / b:.1f} %"


def simulate_cooldown(seq: list[tuple[str, str]], n: int) -> list[str]:
    """P13: после смены направляющего состояния противоположное не выдаётся n суток (post-processing)."""
    out, last_state, last_change = [], None, None
    for day, state in seq:
        d = dt.date.fromisoformat(day)
        effective = state
        if (n > 0 and last_state in ("INCREASE", "DECREASE") and state in ("INCREASE", "DECREASE")
                and state != last_state and last_change is not None and (d - last_change).days < n):
            effective = last_state
        if effective != last_state:
            last_change = d
        last_state = effective
        out.append(effective)
    return out


def flips_and_durations(states: list[str]) -> tuple[int, int, list[int]]:
    """Переключения INCREASE↔DECREASE (напрямую или через не-направляющие дни) и длины серий состояний."""
    flips, directional, durations, run_len = 0, None, [], 0
    for i, s in enumerate(states):
        if s in ("INCREASE", "DECREASE"):
            if directional is not None and s != directional:
                flips += 1
            directional = s
        if i > 0 and s == states[i - 1]:
            run_len += 1
        else:
            if i > 0:
                durations.append(run_len)
            run_len = 1
    if states:
        durations.append(run_len)
    return flips, len(states), durations


def aggregate(out: Path) -> dict:
    rows = load_jsonl(out / "decisions.jsonl")          # утверждённая политика, все колонки, WB и Ozon
    grid_rows = load_jsonl(out / "grid.jsonl")          # сетка кандидатов, компактно, только WB
    qrows = load_jsonl(out / "queries.jsonl")
    outcomes = load_jsonl(out / "outcomes.jsonl")
    meta = json.loads((out / "meta.json").read_text(encoding="utf-8"))
    labels = meta["policy_labels"]
    unset = [r for r in rows if r["policy_id"] == BASE_ID]
    by_policy = defaultdict(list)
    for r in unset:
        if r["marketplace"] == "WB":
            by_policy[BASE_ID].append(r)
    for r in grid_rows:
        by_policy[r["policy_id"]].append(r)
    agg: dict = {"meta": {k: v for k, v in meta.items() if k != "policy_labels"}, "rows_total": len(rows),
                 "grid_rows_total": len(grid_rows), "policy_labels": labels}

    def dist(rs, key):
        c = Counter(r[key] for r in rs)
        return dict(sorted(c.items(), key=lambda kv: -kv[1]))

    # 1–10: распределения на утверждённой политике (то, что V1 выдаёт как есть) — по площадкам.
    for mp in ("WB", "OZON"):
        rs = [r for r in unset if r["marketplace"] == mp]
        agg[f"unset_{mp}"] = {
            "decisions": len(rs),
            "pairs": len({(r["campaign_id"], r["marketplace_sku"]) for r in rs}),
            "days": len({r["as_of_date"] for r in rs}),
            "states": dist(rs, "recommendation"),
            "primary_reasons": dist(rs, "primary_reason_code"),
            "spend_by_state": {s: round(sum(num(r["spend_28d_attributed_rub"]) or 0 for r in rs if r["recommendation"] == s) / 1, 2)
                               for s in {r["recommendation"] for r in rs}},
            "evidence_status": dist(rs, "evidence_status"),
        }
    # 3: доля пар, получивших хотя бы одну доказательную (не INSUFFICIENT и не DQ) рекомендацию за период.
    wb_unset = [r for r in unset if r["marketplace"] == "WB"]
    pairs = defaultdict(list)
    for r in wb_unset:
        pairs[(r["campaign_id"], r["marketplace_sku"])].append(r)
    agg["coverage_unset_wb"] = {
        "pairs": len(pairs),
        "pairs_ever_actionable_evidence": sum(1 for v in pairs.values() if any(x["evidence_status"] == "ACTIONABLE" and int(x["effective_days"]) > 0 for x in v)),
        "pairs_ever_state_not_insufficient": sum(1 for v in pairs.values() if any(x["recommendation"] != "INSUFFICIENT_DATA" for x in v)),
    }
    # 11: чувствительность по сетке (только WB — Ozon fail-closed).
    sens = []
    for pid, rs in by_policy.items():
        wb = [r for r in rs if r["marketplace"] == "WB"]
        c = Counter(r["recommendation"] for r in wb)
        sens.append({"policy_id": pid, "label": labels.get(pid, pid), "n": len(wb), **{k: c.get(k, 0) for k in
                     ("INCREASE", "DECREASE", "HOLD", "PAUSE_CANDIDATE", "INSUFFICIENT_DATA", "BLOCKED_BY_GUARDRAIL")},
                     "blocked_by_policy": sum(1 for r in wb if str(r["primary_reason_code"]).startswith("POLICY_")),
                     "straddle_breakeven": sum(1 for r in wb if r["primary_reason_code"] == "EVID_INTERVAL_STRADDLES_BREAKEVEN"),
                     "inv_blocked_increase": sum(1 for r in wb if r["primary_reason_code"] in ("INV_LOW_COVER", "INV_COVER_UNAVAILABLE")),
                     "decrease_pairs": len({(r["campaign_id"], r["marketplace_sku"]) for r in wb if r["recommendation"] == "DECREASE"}),
                     "increase_pairs": len({(r["campaign_id"], r["marketplace_sku"]) for r in wb if r["recommendation"] == "INCREASE"})})
    agg["sensitivity"] = sorted(sens, key=lambda x: x["policy_id"])
    # 12 + P13: переключения и длительности состояний; период ожидания как post-processing.
    flip = {}
    for pid, rs in by_policy.items():
        seqs = defaultdict(list)
        for r in rs:
            if r["marketplace"] == "WB":
                seqs[(r["campaign_id"], r["marketplace_sku"])].append((r["as_of_date"], r["recommendation"]))
        for n in GRID_P13:
            tot_flips, tot_days, durs = 0, 0, []
            for seq in seqs.values():
                seq.sort()
                states = simulate_cooldown(seq, n)
                f, days, dd = flips_and_durations(states)
                tot_flips += f
                tot_days += days
                durs += dd
            durs.sort()
            flip[f"{pid}|P13={n}"] = {"flips": tot_flips, "pair_days": tot_days,
                                      "median_state_run_days": durs[len(durs) // 2] if durs else None,
                                      "p90_state_run_days": durs[int(len(durs) * 0.9)] if durs else None}
    agg["flip"] = flip
    # Наблюдаемые последствия (не причинные) для DECREASE-кандидатов сетки.
    sku_next = {(o["as_of_date"], o["marketplace_sku"]): o for o in outcomes if o["kind"] == "SKU"}
    pair_next = {(o["as_of_date"], o["campaign_id"], o["marketplace_sku"]): o for o in outcomes if o["kind"] == "PAIR"}
    obs = defaultdict(lambda: Counter())
    for pid, rs in by_policy.items():
        for r in rs:
            if r["marketplace"] != "WB" or r["recommendation"] not in ("DECREASE", "HOLD", "INCREASE"):
                continue
            nx = sku_next.get((r["as_of_date"], r["marketplace_sku"]))
            acted = (r["as_of_date"], r["campaign_id"], r["marketplace_sku"]) in pair_next
            k = f"{r['recommendation']}|config_changed_14d={acted}"
            obs[pid][k + "|n"] += 1
            if nx and nx["next28_contribution_before_ads_rub"] is not None and nx["next28_buyouts_rub"] not in (None, "0"):
                buy = num(nx["next28_buyouts_rub"])
                ad = num(nx["next28_ad_spend_attributed_rub"]) or 0
                cb = num(nx["next28_contribution_before_ads_rub"])
                if buy:
                    obs[pid][k + "|next28_drr_above_next28_limit"] += int(ad / buy > cb / buy)
                    obs[pid][k + "|with_outcome"] += 1
    agg["observational_outcomes"] = {k: dict(v) for k, v in obs.items()}
    # Инварианты replay (PASS/FAIL), проверяются на всей истории, а не на одном дне.
    p1_of = {p: ("P1=NULL" in lab or (lab == BASE_ID and BASE["p1"] is None)) for p, lab in labels.items()}
    keys = Counter((r["as_of_date"], r["policy_id"], r["campaign_id"], r["marketplace_sku"]) for r in grid_rows)
    ukeys = Counter((r["as_of_date"], r["marketplace"], r["campaign_id"], r["marketplace_sku"]) for r in unset)
    inv = {
        "BASE_NO_INCREASE_WHILE_P1_NULL": BASE["p1"] is not None or not any(r["recommendation"] == "INCREASE" for r in unset),
        "OZON_NEVER_DIRECTIONAL": not any(r["marketplace"] == "OZON" and r["recommendation"] in ("INCREASE", "DECREASE", "HOLD")
                                          for r in unset),
        "OZON_NEVER_PRODUCTION_GRADE": not any(r["marketplace"] == "OZON" and str(r["production_grade"]).lower() == "true"
                                               for r in unset),
        "INCREASE_ONLY_WITH_P1": not any(r["recommendation"] == "INCREASE" and p1_of.get(r["policy_id"], False)
                                         for r in grid_rows + unset),
        "ONE_DECISION_PER_PAIR_DAY_POLICY": all(v == 1 for v in keys.values()) and all(v == 1 for v in ukeys.values()),
        "GRID_COVERS_SAME_PAIRS_AS_BASE": not grid_rows or (
            {(d, c, s) for (d, _p, c, s) in keys} == {(d, c, s) for (d, mp, c, s) in ukeys if mp == "WB"}),
        "EVERY_ROW_HAS_PRIMARY_REASON": all(r["primary_reason_code"] for r in unset) and all(r["primary_reason_code"] for r in grid_rows),
        "REPLAY_MANIFEST_PRESENT": all(str(r["input_manifest"]).startswith("ads:PIT_RAW_LOAD_TS") for r in unset),
    }
    agg["invariants"] = {k: ("PASS" if v else "FAIL") for k, v in inv.items()}
    # 13: запросы WB.
    qa = defaultdict(Counter)
    for q in qrows:
        key = f"{q['diag_below_mult']}/{q['diag_above_mult']}"
        qa[key]["rows"] += 1
        qa[key][q["primary_query_class"]] += 1
        for f in ("q_high_cpc", "q_traffic_no_conversion", "q_converting", "q_possibly_underexposed", "q_econ_cannot_raise"):
            qa[key][f] += int(str(q[f]).lower() == "true")
        qa[key]["spend_rub"] += num(q["spend_attributed_rub"]) or 0
        qa[key]["aligned"] += int(str(q["query_as_of_aligned"]).lower() == "true")
    agg["queries"] = {k: dict(v) for k, v in qa.items()}
    agg["queries_days"] = len({q["aie_as_of_date"] for q in qrows})
    # 14: подозрительные случаи — ранжирование по расходу, без порогов.
    susp = []
    for r in wb_unset:
        codes = r["reason_codes"] or []
        spend = num(r["spend_28d_attributed_rub"]) or 0
        if r["primary_reason_code"] == "INV_ZERO_STOCK":
            susp.append(("INV_ZERO_STOCK_WITH_SPEND", spend, r))
        if r["primary_reason_code"] == "PRICE_BELOW_BREAKEVEN":
            susp.append(("PRICE_BELOW_BREAKEVEN", spend, r))
        if "REGIME_PRICE_CHANGE" in codes and int(r["effective_days"]) <= 1:
            susp.append(("P7_WINDOW_COLLAPSED", spend, r))
        if r["primary_reason_code"] == "WASTE_ZERO_ORDERS_ADS4":
            susp.append(("WASTE_ZERO_ORDERS", spend, r))
    by_kind = defaultdict(list)
    for kind, spend, r in susp:
        by_kind[kind].append((spend, r))
    agg["suspicious"] = {}
    for kind, items in by_kind.items():
        items.sort(key=lambda x: -x[0])
        seen, top = set(), []
        for spend, r in items:
            key = (r["campaign_id"], r["marketplace_sku"])
            if key in seen:
                continue
            seen.add(key)
            top.append({"as_of_date": r["as_of_date"], "internal_sku": r["internal_sku"], "campaign_id": r["campaign_id"],
                        "nm_id": r["marketplace_sku"], "spend_28d": round(spend, 2), "primary": r["primary_reason_code"],
                        "effective_days": r["effective_days"], "evidence": r["evidence_status"]})
            if len(top) == 8:
                break
        agg["suspicious"][kind] = {"rows": len(items), "pairs": len({(r["campaign_id"], r["marketplace_sku"]) for _, r in items}), "top_by_spend": top}
    return agg


STATES = ("INCREASE", "DECREASE", "HOLD", "PAUSE_CANDIDATE", "INSUFFICIENT_DATA", "BLOCKED_BY_GUARDRAIL")


def _pid(agg, label):
    for pid, lab in agg["policy_labels"].items():
        if lab == label:
            return pid
    raise KeyError(label)


def pack_tables(agg: dict) -> str:
    """Таблицы Owner Decision Pack из aggregate.json — числа не переписываются руками."""
    sens = {s["policy_id"]: s for s in agg["sensitivity"]}
    L = []

    def table(head, rows):
        L.append("| " + " | ".join(head) + " |")
        L.append("|" + "|".join("---:" if i else "---" for i in range(len(head))) + "|")
        for r in rows:
            L.append("| " + " | ".join(str(x) for x in r) + " |")
        L.append("")

    for mp in ("WB", "OZON"):
        u = agg[f"unset_{mp}"]
        L.append(f"#### {mp}: {u['decisions']} решений, {u['pairs']} пар, {u['days']} дат (утверждённая политика {BASE_ID})")
        L.append("")
        table(["Состояние", "Решений", "Доля", "Атрибутированный расход 28 сут, ₽ (сумма по решениям)"],
              [(s, n, pct(n, u["decisions"]), f"{u['spend_by_state'].get(s, 0):,.0f}".replace(",", " "))
               for s, n in u["states"].items()])
        table(["Основная причина", "Решений", "Доля"],
              [(c, n, pct(n, u["decisions"])) for c, n in u["primary_reasons"].items()])
    cov = agg["coverage_unset_wb"]
    L.append("#### Покрытие WB")
    L.append("")
    table(["Показатель", "Значение"],
          [("пар кампания × SKU за период", cov["pairs"]),
           ("пар, хоть раз имевших доказательную выборку (ACTIONABLE, окно > 0)", f"{cov['pairs_ever_actionable_evidence']} ({pct(cov['pairs_ever_actionable_evidence'], cov['pairs'])})"),
           ("пар, хоть раз получивших состояние, отличное от INSUFFICIENT_DATA", f"{cov['pairs_ever_state_not_insufficient']} ({pct(cov['pairs_ever_state_not_insufficient'], cov['pairs'])})")])
    L.append("#### P3 при P1 = NULL (INCREASE недостижим по построению; P7 и K — утверждённые)")
    L.append("")
    rows = []
    for p3 in GRID_P3:
        s = sens[_pid(agg, f"P3={p3}|P1=NULL")]
        rows.append((p3, s["n"], *(s[k] for k in STATES), s["straddle_breakeven"], s["decrease_pairs"]))
    s = sens[BASE_ID]
    rows.append((BASE_ID, s["n"], *(s[k] for k in STATES), s["straddle_breakeven"], s["decrease_pairs"]))
    table(["P3", "решений", *STATES, "интервал пересекает лимит", "пар с DECREASE"], rows)
    L.append("#### P1 × P3 (P2 = 0, P5 = 0 — ограничители выключены, чтобы увидеть чистый эффект P1)")
    L.append("")
    rows = []
    for p3 in GRID_P3:
        for p1 in GRID_P1:
            s = sens[_pid(agg, f"P3={p3}|P1={p1}|P2=0.0|P5L=0.0")]
            rows.append((p3, p1, s["INCREASE"], s["increase_pairs"], s["HOLD"], s["DECREASE"], s["BLOCKED_BY_GUARDRAIL"]))
    table(["P3", "P1 (резерв)", "INCREASE", "пар с INCREASE", "HOLD", "DECREASE", "BLOCKED"], rows)
    L.append("#### P2 (полоса) при P3 = 0.90, P5 = 0")
    L.append("")
    rows = []
    for p1 in GRID_P1:
        rows.append((p1, *(sens[_pid(agg, f"P3=0.9|P1={p1}|P2={p2}|P5L=0.0")]["INCREASE"] for p2 in GRID_P2)))
    table(["P1", *(f"INCREASE при P2={p2}" for p2 in GRID_P2)], rows)
    L.append("#### P5 (нижняя граница покрытия) при P3 = 0.90, P2 = 0 (исследование)")
    L.append("")
    rows = []
    for p1 in GRID_P1:
        cells = []
        for p5 in GRID_P5:
            s = sens[_pid(agg, f"P3=0.9|P1={p1}|P2=0.0|P5L={p5}")]
            cells.append(f"{s['INCREASE']} / {s['inv_blocked_increase']}")
        rows.append((p1, *cells))
    table(["P1", *(f"P5L={p5}: INCREASE / удержано запасом" for p5 in GRID_P5)], rows)
    L.append("#### P13 (период ожидания): переключения INCREASE ↔ DECREASE и длительность состояний")
    L.append("")
    reps = [BASE_ID, *(f"P3={p3}|P1=NULL" for p3 in GRID_P3),
            *(f"P3=0.9|P1={p1}|P2=0.0|P5L=0.0" for p1 in (0.1, 0.3, 0.5))]
    rows = []
    for lab in reps:
        pid = BASE_ID if lab == BASE_ID else _pid(agg, lab)
        for n in GRID_P13:
            f = agg["flip"][f"{pid}|P13={n}"]
            rows.append((lab, n, f["flips"], f["pair_days"], f["median_state_run_days"], f["p90_state_run_days"]))
    table(["Политика-кандидат", "P13, сут", "переключений", "пар-дней", "медиана серии, сут", "p90 серии, сут"], rows)
    L.append("#### Наблюдаемые последствия (НЕ причинные): ДРР следующих 28 суток выше их же безубыточности")
    L.append("")
    rows = []
    for lab in [f"P3={p3}|P1=NULL" for p3 in GRID_P3] + ["P3=0.9|P1=0.2|P2=0.0|P5L=0.0"]:
        o = agg["observational_outcomes"].get(_pid(agg, lab), {})
        for st in ("DECREASE", "HOLD", "INCREASE"):
            for acted in (True, False):
                k = f"{st}|config_changed_14d={acted}"
                n = o.get(k + "|n", 0)
                if n:
                    w = o.get(k + "|with_outcome", 0)
                    a = o.get(k + "|next28_drr_above_next28_limit", 0)
                    rows.append((lab, st, "да" if acted else "нет", n, w, f"{a} ({pct(a, w)})"))
    table(["Политика-кандидат", "Состояние", "Настройки менялись ≤14 сут", "решений", "с исходом", "ДРР выше лимита"], rows)
    L.append("#### P15: классы поисковых запросов WB при разных множителях (строк × дней)")
    L.append("")
    rows = []
    for k, v in sorted(agg["queries"].items()):
        rows.append((k, v.get("rows", 0), v.get("q_high_cpc", 0), v.get("q_traffic_no_conversion", 0), v.get("q_converting", 0),
                     v.get("q_possibly_underexposed", 0), v.get("q_econ_cannot_raise", 0), v.get("Q_INSUFFICIENT", 0)))
    table(["0,7/1,3 → вариант", "строк", "HIGH_CPC", "TRAFFIC_NO_CONV", "CONVERTING", "UNDEREXPOSED", "ECON_CANNOT_RAISE", "INSUFFICIENT"], rows)
    L.append("#### Случаи для ручного разбора (ранжирование по расходу, без порогов)")
    L.append("")
    for kind, v in agg["suspicious"].items():
        L.append(f"**{kind}** — {v['rows']} решений, {v['pairs']} пар.")
        L.append("")
        table(["Дата", "SKU", "Кампания", "nm", "Расход 28 сут, ₽", "Эфф. дней", "Доказательность"],
              [(x["as_of_date"], x["internal_sku"], x["campaign_id"], x["nm_id"], f"{x['spend_28d']:,.0f}".replace(",", " "),
                x["effective_days"], x["evidence"]) for x in v["top_by_spend"]])
    return "\n".join(L)


# ── final replay ───────────────────────────────────────────────────────────────
def _true(v) -> bool:
    return str(v).lower() == "true"


def final_summary(out: Path) -> dict:
    """Итог final replay на утверждённой политике: наборы, состояния, причины, диагностика, инварианты."""
    rows = [r for r in load_jsonl(out / "decisions.jsonl") if r["policy_id"] == BASE_ID]
    meta = json.loads((out / "meta.json").read_text(encoding="utf-8"))
    res: dict = {"meta": {k: v for k, v in meta.items() if k != "policy_labels"}, "rows": len(rows),
                 "dates": len({r["as_of_date"] for r in rows})}

    def dist(rs, key):
        c = Counter(str(r[key]) for r in rs)
        return dict(sorted(c.items(), key=lambda kv: (-kv[1], kv[0])))

    def block(rs):
        return {"decisions": len(rs), "pairs": len({(r["campaign_id"], r["marketplace_sku"]) for r in rs}),
                "states": dist(rs, "recommendation"), "primary_reasons": dist(rs, "primary_reason_code")}

    for mp in ("WB", "OZON"):
        for uni in ("CURRENT_ACTIONABLE", "HISTORICAL_EVIDENCE_ONLY"):
            res[f"{mp}|{uni}"] = block([r for r in rows if r["marketplace"] == mp and r["universe"] == uni])
    cur = [r for r in rows if r["marketplace"] == "WB" and r["universe"] == "CURRENT_ACTIONABLE"]
    for st in ("DECREASE", "PAUSE_CANDIDATE", "HOLD", "INSUFFICIENT_DATA", "BLOCKED_BY_GUARDRAIL"):
        rs = [r for r in cur if r["recommendation"] == st]
        pairs = defaultdict(list)
        for r in rs:
            pairs[(r["internal_sku"], r["campaign_id"], r["marketplace_sku"])].append(r)
        res[f"WB_current|{st}"] = {
            **block(rs),
            "pairs_detail": [{"internal_sku": k[0], "campaign_id": k[1], "nm_id": k[2], "decisions": len(v),
                              "first": min(x["as_of_date"] for x in v), "last": max(x["as_of_date"] for x in v),
                              "primary": dist(v, "primary_reason_code")}
                             for k, v in sorted(pairs.items(), key=lambda kv: -len(kv[1]))][:25],
        }
    ic = [r for r in rows if _true(r["increase_candidate"])]
    icr = [r for r in cur if _true(r["increase_candidate_realized"])]
    res["increase_candidate"] = {"decisions": len(ic), "pairs": len({(r["campaign_id"], r["marketplace_sku"]) for r in ic}),
                                 "realized_only_decisions_current": len(icr),
                                 "realized_only_pairs_current": sorted({f"{r['internal_sku']}|{r['campaign_id']}" for r in icr}),
                                 "conservative_guard": dist(cur, "conservative_guard")}
    res["price_guard"] = {
        "primary_PRICE_BELOW_BREAKEVEN": sum(1 for r in rows if r["primary_reason_code"] == "PRICE_BELOW_BREAKEVEN"),
        "routing_PRICING_REVIEW": sum(1 for r in rows if r["routing"] == "PRICING_REVIEW"),
        "ctx_ECON_REALIZED_NEGATIVE_BEFORE_ADS": sum(1 for r in rows if "ECON_REALIZED_NEGATIVE_BEFORE_ADS" in (r["reason_codes"] or [])),
        "realized_negative_pairs_wb": sorted({r["internal_sku"] for r in rows if r["marketplace"] == "WB"
                                              and "ECON_REALIZED_NEGATIVE_BEFORE_ADS" in (r["reason_codes"] or [])}),
        "forward_availability": dist(rows, "forward_availability"),
    }
    res["inventory"] = {
        "cover_state_all": dist(rows, "inventory_cover_state"),
        "cover_state_wb_current": dist(cur, "inventory_cover_state"),
        "bundle_decisions": sum(1 for r in rows if _true(r["is_bundle"])),
        "bundle_skus": sorted({r["internal_sku"] for r in rows if _true(r["is_bundle"])}),
        "unobserved_non_bundle_skus": sorted({r["internal_sku"] for r in rows if r["inventory_cover_state"] == "INV_COVER_UNAVAILABLE"
                                              and not _true(r["is_bundle"])}),
    }
    wb = [r for r in rows if r["marketplace"] == "WB"]
    res["finance"] = {"financial_data_mature": dist(wb, "financial_data_mature"),
                      "econ_window_end_lag_days": dist(wb, "econ_window_end_lag_days")}
    st7 = [r for r in rows if r["marketplace"] == "WB" and r["campaign_status"] == "7"]
    res["scope"] = {
        "status_7_decisions": len(st7),
        "status_7_with_spend_on_decision_day": sum(1 for r in st7 if r["last_spend_date"] == r["as_of_date"]),
        "status_7_in_current": sum(1 for r in st7 if r["universe"] == "CURRENT_ACTIONABLE"),
        "wb_historical_by_status": dist([r for r in wb if r["universe"] == "HISTORICAL_EVIDENCE_ONLY"], "campaign_status"),
        "decrease_all_universes": sum(1 for r in rows if r["recommendation"] == "DECREASE"),
    }
    k = BASE["k"]
    keys = Counter((r["as_of_date"], r["marketplace"], r["campaign_id"], r["marketplace_sku"]) for r in rows)
    active = {"WB": ("9", "11"), "OZON": ("RUNNING",)}
    inv = {
        "INCREASE_ZERO_WHILE_P1_NULL": BASE["p1"] is not None or not any(r["recommendation"] == "INCREASE" for r in rows),
        "INCREASE_CANDIDATE_IS_NOT_INCREASE": not any(_true(r["increase_candidate"]) and r["recommendation"] == "INCREASE" for r in rows),
        "OZON_NEVER_DIRECTIONAL": not any(r["marketplace"] == "OZON" and r["recommendation"] in ("INCREASE", "DECREASE", "HOLD") for r in rows),
        "OZON_NEVER_PRODUCTION_GRADE": not any(r["marketplace"] == "OZON" and _true(r["production_grade"]) for r in rows),
        "ONE_DECISION_PER_PAIR_DAY": all(v == 1 for v in keys.values()),
        "EVERY_ROW_HAS_PRIMARY_REASON": all(r["primary_reason_code"] for r in rows),
        "UNIVERSE_RULE_EXACT": all(
            (r["universe"] == "CURRENT_ACTIONABLE") == (r["campaign_status"] in active[r["marketplace"]]
                                                       and r["days_since_last_spend"] is not None
                                                       and (k is None or int(r["days_since_last_spend"]) <= k))
            for r in rows),
        "HISTORICAL_ROWS_CARRY_SCOPE_CODE": all(("SCOPE_CAMPAIGN_INACTIVE" in (r["reason_codes"] or [])) == (r["universe"] == "HISTORICAL_EVIDENCE_ONLY")
                                                for r in rows),
        "HISTORICAL_ROWS_NEVER_ACTIONABLE": not any(r["universe"] == "HISTORICAL_EVIDENCE_ONLY"
                                                    and r["recommendation"] not in ("BLOCKED_BY_GUARDRAIL",) for r in rows),
        "BUNDLES_COVER_UNAVAILABLE": all(r["inventory_cover_state"] == "INV_COVER_UNAVAILABLE" for r in rows if _true(r["is_bundle"])),
        "FINANCIAL_DATA_NEVER_IMMATURE": not any(str(r["financial_data_mature"]).lower() == "false" for r in rows),
        "POLICY_IS_GIT_POLICY": all(r["policy_id"] == BASE_ID and num(r["p3_confidence"]) == BASE["p3"]
                                    and num(r["p7_price_change_pct"]) == BASE["p7"] and int(r["k_inactive_days"]) == BASE["k"]
                                    and r["p1_reserve_share"] is None and r["p2_band_pp"] is None
                                    and r["p5_low_cover_days"] is None and r["p5_overstock_cover_days"] is None
                                    and r["p13_cooldown_days"] is None for r in rows),
        "REPLAY_MANIFEST_PRESENT": all(str(r["input_manifest"]).startswith("ads:PIT_RAW_LOAD_TS") for r in rows),
        "FORWARD_NEVER_IN_REPLAY": all(r["forward_availability"] in (None, "SKIPPED_NOT_REPLAYABLE") for r in rows),
    }
    res["invariants"] = {k2: ("PASS" if v else "FAIL") for k2, v in inv.items()}
    return res


def final_tables(res: dict) -> str:
    L = []

    def table(head, rows):
        L.append("| " + " | ".join(head) + " |")
        L.append("|" + "|".join("---:" if i else "---" for i in range(len(head))) + "|")
        for r in rows:
            L.append("| " + " | ".join(str(x) for x in r) + " |")
        L.append("")

    m = res["meta"]
    L.append(f"Период {m['from']} — {m['to']}, дат {res['dates']}, строк {res['rows']}, политика {BASE_ID}.")
    L.append("")
    for key in ("WB|CURRENT_ACTIONABLE", "WB|HISTORICAL_EVIDENCE_ONLY", "OZON|CURRENT_ACTIONABLE", "OZON|HISTORICAL_EVIDENCE_ONLY"):
        b = res[key]
        L.append(f"#### {key}: {b['decisions']} решений, {b['pairs']} пар")
        L.append("")
        table(["Состояние", "Решений", "Доля"], [(s, n, pct(n, b["decisions"])) for s, n in b["states"].items()])
        table(["Основная причина", "Решений"], list(b["primary_reasons"].items()))
    for st in ("DECREASE", "PAUSE_CANDIDATE", "HOLD"):
        b = res[f"WB_current|{st}"]
        L.append(f"#### WB, текущий набор, {st}: {b['decisions']} решений, {b['pairs']} пар")
        L.append("")
        if b["pairs_detail"]:
            table(["SKU", "Кампания", "nm", "Решений", "Первая", "Последняя", "Основные причины"],
                  [(x["internal_sku"], x["campaign_id"], x["nm_id"], x["decisions"], x["first"], x["last"],
                    ", ".join(f"{k} {v}" for k, v in x["primary"].items())) for x in b["pairs_detail"]])
    return "\n".join(L)


def main(argv: list[str]) -> int:
    if not argv:
        sys.stderr.write(__doc__)
        return 2
    cmd = argv[0]
    opt = lambda n: R._opt(argv, n)  # noqa: E731
    if cmd in ("parity", "run"):
        bq = R.make_bq(opt("--project") or R.PROJECT, opt("--token-command"), opt("--token-env"), "aie-backtest-readonly")
        if cmd == "parity":
            rows = bq.query(parity_sql(R.fetch_live_bodies(bq)))
            print(json.dumps(rows, ensure_ascii=False))
            return 0 if rows and rows[0]["status"] == "PASS" else 1
        run(bq, dt.date.fromisoformat(opt("--from")), dt.date.fromisoformat(opt("--to")), Path(opt("--out")),
            dt.date.fromisoformat(opt("--query-from") or "2026-08-14"), with_grid="--no-grid" not in argv)
        return 0
    if cmd == "final":
        res = final_summary(Path(opt("--out")))
        (Path(opt("--out")) / "final_summary.json").write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
        (Path(opt("--out")) / "final_tables.md").write_text(final_tables(res), encoding="utf-8")
        print(json.dumps(res["invariants"], ensure_ascii=False, indent=2))
        return 0 if all(v == "PASS" for v in res["invariants"].values()) else 1
    if cmd == "report":
        agg = aggregate(Path(opt("--out")))
        (Path(opt("--out")) / "aggregate.json").write_text(json.dumps(agg, ensure_ascii=False, indent=2), encoding="utf-8")
        (Path(opt("--out")) / "pack_tables.md").write_text(pack_tables(agg), encoding="utf-8")
        print(json.dumps({k: v for k, v in agg.items() if k in ("meta", "rows_total", "grid_rows_total", "invariants",
                                                                  "unset_WB", "unset_OZON", "coverage_unset_wb")},
                         ensure_ascii=False, indent=2))
        return 0
    sys.stderr.write(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
