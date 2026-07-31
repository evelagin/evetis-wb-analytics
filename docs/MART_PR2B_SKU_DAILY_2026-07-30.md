# PR-Mart2b / PR#81 — MART_SKU_DAILY (первая KPI-витрина). REV5 (re-audit)

**Дата:** 2026-07-30  **Статус:** REV5 — finance-guard разведён на две обязательные категории (commission, logistics). Ожидает финальный diff-review + merge.
**CPM** входит в витрину (стандартный KPI, НЕ deferred).

**GitHub PR #81 body — заменить строку про отложенный CPM на:**
`Additional marketing extensions beyond CPM are deferred to follow-up PRs.`
**Зависит от:** PR-Mart2a (`V_WB_FINANCE_AMOUNTS_LONG_MAPPED` в проде, принят).
**SQL:** `sql/mart/pr_mart2b_sku_daily.sql` (процедура). **Валидация:** `sql/mart/pr_mart2b_sku_daily_validation.sql`.
**Контракты:** `docs/MART_MART2_CONTRACTS_2026-07-28.md` §4, §KPI.

## Правки REV2 по замечаниям аудитора
1. **Fail-closed guard (#1).** Перед build — ASSERT: в `V_WB_FINANCE_AMOUNTS_LONG_MAPPED` (finance_date≤build_as_of)
   нет строк `cost_category IS NULL`. Раньше неизвестная денежная пара молча выпадала из витрины
   (fin-CTE фильтрует `cost_amount_positive IS NOT NULL`). Теперь — явный отказ, «расширить REF_COST_MAP».
2. **Полнота universe (#2).** Добавлено: guard `global_start ≤ build_as_of`; ASSERT `BUILD rows > 0`; ASSERT
   `(active REF nm_id) EXCEPT DISTINCT (BUILD nm_id) = 0` — ловит ПОЛНОСТЬЮ отсутствующий SKU (проверка плотности
   ловила только разрывы внутри присутствующего nm).
3. **Воспроизводимость на дату (#3).** Все source-CTE (ads/ord/sal/fin/finpay/findt) и все reconciliation-ASSERT
   ограничены `source_date ≤ build_as_of`. Особенно finance/LONG_MAPPED — finance исключён из `max_required_source_date`,
   поэтому без явного бонда прогон на прошлую дату затянул бы более поздние финоперации.
4. **NULL-ловушка GREATEST/LEAST (#4, REV2).** Ранее `GREATEST/LEAST` при любом NULL-аргументе вернули бы NULL и guard
   молча бы не сработал.
   (+ Добавлен KPI `cpm = SAFE_DIVIDE(ad_spend, views) × 1000` — стандартный KPI витрины, НЕ deferred.)

### REV3 (re-audit) — остаточный fail-closed blocker
NULL-safe `UNNEST ... WHERE d IS NOT NULL` **молча терпел пустой ОТДЕЛЬНЫЙ** обязательный источник: если, напр., ADS
полностью пуст, `max_required` считался по orders+sales, и BUILD/reconciliation могли опубликовать нулевую рекламную
метрику как валидную. Исправление: **отдельные max-date переменные** `v_orders_max/v_sales_max/v_ads_max` из
`FACT_ORDERS/FACT_SALES/FACT_ADS_SKU_DAILY` + **явный RAISE на каждый пустой**; отдельная проверка непустоты
`V_WB_FINANCE_AMOUNTS_LONG_MAPPED` (finance в `max_required` НЕ входит) с RAISE; и только потом
`v_max_required = GREATEST(v_orders_max, v_sales_max, v_ads_max)` (уже без NULL-аргументов). Dry-run 30.07:
orders_max=2026-07-30, sales_max=2026-07-30, ads_max=2026-07-29, max_required=2026-07-30.

### REV4 (re-audit) — finance-guard сужен до фактически используемого среза
Проверка непустоты finance считала **ВСЕ** строки `LONG_MAPPED`, а витрина (fin-CTE) потребляет только `is_sku_row`.
Класс ошибки: при сбое reference-мэппинга все финстроки уехали бы в ACCOUNT → `v_finance_rows > 0` (guard проходит),
`fin` пуст → commission/logistics по SKU = 0 → reconciliation сравнивает 0 с 0 (тоже is_sku_row) → повреждённая
витрина публикуется. Исправление: `v_finance_sku_rows` считает `WHERE finance_date <= build_as_of AND is_sku_row
AND nm_id IN (active universe)` — ровно тот финансовый источник, что реально использует витрина; RAISE, если 0.
BigQuery по просьбе аудитора не запускался — только diff.

### REV5 (re-audit) — finance-guard разведён на две обязательные категории
`is_sku_row`-guard всё ещё шире двух реально выводимых показателей (`commission_cost_positive`, `logistics_cost_positive`).
Класс ошибки: при сбое mapping конкретной категории (напр. `commission`/`logistics` исчезли, но прочие SKU-строки
остались) общий guard прошёл бы, а соответствующая метрика витрины занулилась → reconciliation сравнил бы 0 с 0.
Исправление: **два отдельных счётчика** `v_finance_commission_rows` и `v_finance_logistics_rows`
(`is_sku_row AND cost_category='commission'/'logistics' AND cost_amount_positive IS NOT NULL AND nm_id IN active-universe`),
каждый с `RAISE` при 0. Validation §1 показывает оба (`finance_commission_rows`, `finance_logistics_rows`, ожидание >0).
Итог: guards доказывают непустоту ВСЕХ обязательных входов витрины — ORDERS, SALES, ADS, SKU commission, SKU logistics,
active universe, отсутствие unknown mapping. BigQuery не запускался.

## Грейн / spine (§4)
Universe = `REF_SKU_MASTER (WB, active, nm_id NOT NULL)` (24 nm). start_date(nm)=LEAST(MIN order/sale/ads/finance по nm,
все ≤build_as_of); fallback=`mart_global_start_date` (авто = глобальный MIN, NULL-safe). `GENERATE_DATE_ARRAY(start, build_as_of)`
→ пропуски=0. Rolling: `RANGE BETWEEN 6/13 PRECEDING` по `UNIX_DATE(day)`. build_as_of — явный параметр, guards fail-closed.

## Колонки (кратко)
Реклама raw+estimate; заказы (orders_* чистые + canceled_*); выкупы (buyouts_* + returns_*); finance per-SKU
(commission/logistics cost_positive + finance_for_pay_accounting); KPI: ctr, **cpm**, cpc, cpo_attributed, blended_cpo,
drr_orders, **drr_buyouts (осн)**, roas, acos; вклад hybrid/settlement (cross-base, НЕ P&L дня); rolling 7/14 +
**drr_buyouts_7d/14d (главный)**, roas_7d/14d, blended_cpo_7d/14d.

## Гейт (fail-closed, в процедуре)
pre: unknown money-pair=0 (#1). Даты: max_required/global_start NULL-safe + RAISE (#4); guards NOT NULL / ≤сегодня /
≥max_required / global_start≤build_as_of (#2). BUILD: rows>0; грейн (day,nm_id) уникален+not-null; **universe EXCEPT BUILD=0** (#2);
0 разрывов spine; MAX(day)==build_as_of; консервация витрина Σ==FACT Σ (universe, ≤build_as_of, #3) по ad_spend/orders_qty/
buyouts_rub; finance commission+logistics==LONG_MAPPED (universe SKU, ≤build_as_of); физика partition=1/cluster=1.

## Read-only dry-run (30.07, build_as_of=2026-07-30)
`max_required=2026-07-30`; `global_start=2024-09-05`; `unknown_window=0`; `universe_missing=0`; `n_rows=6772`;
`gap_nms=0`; Δ(ad_spend/orders/buyouts/finance, date-bounded)=**0/0/0/0**.
Store 14д (universe): ad_spend 54 050, buyouts_rub 231 205, ДРР 23%, ROAS 2.74; 7д ДРР ~25%, ROAS 2.17.

## Оговорки для аудита
- **Universe-эффект:** витрина покрывает 24 активных nm; «сырые» FACT-итоги по ВСЕМ nm чуть выше (14д buyouts 232 570 vs 231 205) — по контракту §4.
- **Finance лагает** → daily-вклад на свежих днях занижен; основной = rolling. **ad_orders_raw** — мульти-тач (~1%).
- **Вклад cross-base** (buyouts по sale_date, commission/logistics по finance_date) — не P&L дня. Маржа (−COGS) — MART v2.

## Apply после merge
`CALL wb_mart.sp_build_mart_sku_daily(CURRENT_DATE('Europe/Moscow'), NULL);` → затем validation-файл §4-§8.
Оркестрация (runWbMartDaily + freshness-gate + триггер ~09:00) — PR-Mart3.
