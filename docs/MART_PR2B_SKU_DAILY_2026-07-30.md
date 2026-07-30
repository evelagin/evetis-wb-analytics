# PR-Mart2b — MART_SKU_DAILY (первая KPI-витрина)

**Дата:** 2026-07-30  **Статус:** написана + верифицирована read-only на проде. НА АУДИТ ChatGPT.
**Зависит от:** PR-Mart2a (`V_WB_FINANCE_AMOUNTS_LONG_MAPPED` уже в проде, APPROVE).
**SQL:** `sql/mart/pr_mart2b_sku_daily.sql` (процедура). **Валидация:** `sql/mart/pr_mart2b_sku_daily_validation.sql`.
**Контракты:** `docs/MART_MART2_CONTRACTS_2026-07-28.md` §4 (spine, build_as_of), §KPI.

## Что делает
Строит `wb_mart.MART_SKU_DAILY` — грейн **day × nm_id**, dense spine. Процедура
`sp_build_mart_sku_daily(build_as_of_date DATE, mart_global_start_date DATE)`, паттерн Mart1
(BUILD → ASSERT → publish → ASSERT физики), advisory-lock `mart_sku_daily`, MANUAL-ONLY.

## Grain / spine (§4)
- **Universe** = `REF_SKU_MASTER WHERE marketplace='WB' AND active=TRUE AND nm_id IS NOT NULL` (24 nm live).
- **start_date(nm)** = `LEAST(MIN order/sale/ads/finance по nm)`; fallback (нет активности) = `mart_global_start_date`
  (параметр; NULL → авто = глобальный `LEAST` MIN тех же FACT).
- `GENERATE_DATE_ARRAY(start, build_as_of)` → пропуски = 0. Rolling: `RANGE BETWEEN 6/13 PRECEDING` по `UNIX_DATE(day)`.
- **build_as_of_date** — явный параметр. Guards fail-closed: NOT NULL; ≤ CURRENT_DATE('Europe/Moscow');
  ≥ max_required_source_date = `GREATEST(MAX order/sale/ads)` (FINANCE не входит — лагает недельно).

## Колонки
- *Реклама (raw+estimate, вне консервации):* ad_spend(=stats_spend_rub), views, clicks, ad_orders_raw,
  ads_revenue_raw_rub, ads_revenue_dedup_estimate_rub, ad_orders_dedup_estimate.
- *Заказы:* orders_qty/orders_rub (ЧИСТЫЕ, is_cancel=FALSE) + canceled_qty/canceled_rub (решение владельца 30.07).
- *Выкупы:* buyouts_qty/buyouts_rub (is_return=FALSE), sales_for_pay_operational, returns_qty/returns_rub.
- *Finance per-SKU (LONG_MAPPED, is_sku):* commission_cost_positive, logistics_cost_positive, finance_for_pay_accounting.
- *KPI дня:* ctr, cpc, cpo_attributed(=spend/ad_orders_raw), blended_cpo(=spend/orders_qty),
  drr_orders, **drr_buyouts (основной)**, roas, acos.
- *Вклад-до-COGS (cross-base, НЕ «прибыль дня»):*
  `hybrid_day_contribution_pre_cogs` = buyouts_rub − commission − logistics − ad_spend;
  `settlement_day_contribution_pre_cogs` = finance_for_pay_accounting − ad_spend.
- *Rolling 7/14:* ad_spend, ads_revenue_raw, ads_revenue_dedup_estimate, ad_orders_raw/estimate,
  buyouts_rub, orders_rub, orders_qty + производные **drr_buyouts_7d/14d (главный)**, roas_7d/14d, blended_cpo_7d/14d.

## Гейт (fail-closed, в процедуре)
Грейн (day,nm_id) уникален + not-null; **плотность spine (0 разрывов)**; MAX(day)==build_as_of;
консервация витрина Σ == FACT Σ (universe) по ad_spend / orders_qty / buyouts_rub; консервация finance
commission+logistics == LONG_MAPPED (universe, SKU-ветка); физика partition=1/cluster=1.

## Приёмка read-only (30.07, build_as_of=2026-07-30)
`n_rows=6772`; `grain_dupes=0`; `gap_nms=0`; `nm=24`; `max_day=2026-07-30`;
**все дельты консервации = 0** (ad_spend, orders_qty, buyouts_rub, finance commission+logistics).
Store 14д (16–29.07, universe): ad_spend **54 050**, orders_qty **234**, buyouts_rub **231 205**,
ДРР_buyouts 23.4%, ROAS 2.74. Store 7д: ad_spend 26 735, ДРР_buyouts ~25%, ROAS 2.17 — реклама за неделю просела.

## Оговорки для аудита
- **Universe-эффект:** витрина покрывает только 24 активных nm. Раздельные «сырые» FACT-итоги по ВСЕМ nm чуть выше
  (14д buyouts_rub 232 570 vs 231 205; orders 235 vs 234) — разница = активность на неактивных nm, отсекается по контракту (§4).
- **Finance лагает** недельно → daily-вклад на свежих днях занижен; основной управленческий показатель = **rolling** (drr_buyouts_7d).
- **ad_orders_raw** — мульти-тач атрибуция (~1% инфляции), это source-faithful, не «уникальные заказы».
- **Вклад cross-base:** buyouts по sale_date, commission/logistics по finance_date — осознанное несовпадение баз (НЕ P&L дня).
- Маржа (−COGS) — MART v2 после REF PR2 (COGS в BQ).

## Ручной прогон (после APPROVE)
`CALL wb_mart.sp_build_mart_sku_daily(CURRENT_DATE('Europe/Moscow'), NULL);` → далее validation-файл.
Оркестрация (runWbMartDaily + freshness-gate + run-log + триггер ~09:00) — PR-Mart3.
