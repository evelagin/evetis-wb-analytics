# STAGE 4 — Looker Studio MVP / Dashboard Readiness Audit

**Дата:** 20.08.2026
**Режим:** read-only. Выполнены только `SELECT`. Ни один объект не создан, не изменён и не удалён.
**Проект BigQuery:** `project-fa311fc0-4d87-4781-986`, датасеты `wb_raw`, `wb_mart` (регион EU).
**Stage 3B.1 Фаза A:** не тронута. `V_ADV_COSTS_SNAPSHOT` / `V_ADV_COSTS_DAY_COVERAGE` только прочитаны, bootstrap не запускался.

**ВЕРДИКТ: `LOOKER_MVP_READY_FOR_IMPLEMENTATION`** — с четырьмя явными ограничениями по покрытию
и одной страницей (`Stocks & Supplies`), которая выходит в MVP частично. Подробности — §7 и §8.

---

## 1. Inventory: что реально есть в production

### 1.1 Объём хранилища (факт, не оценка)

Весь слой данных — **менее 500 МБ**. Самая большая таблица `wb_raw.RAW_WB_FINANCE` = 324 МБ,
самая большая витрина `wb_mart.FACT_FINANCE` = 99 МБ, `MART_SKU_DAILY` = **4,73 МБ**.
Это определяет весь раздел §5: стоимость запросов в этом проекте — не проблема.

### 1.2 FACT-слой (`wb_mart`)

| объект | грейн | покрытие | строк | размер | свежесть данных | Looker напрямую? |
|---|---|---|---|---|---|---|
| `FACT_ORDERS` | `order_srid` (заказ) | **2026-04-13 … 2026-08-20** | 4 307 | 1,46 МБ | сегодня, лоадер ежечасно | ⚠️ можно, но грейн — строка заказа, не сутки |
| `FACT_SALES` | `sale_id` | **2026-03-30 … 2026-08-19** | 4 001 | 1,46 МБ | D-1, лоадер ежечасно | ⚠️ то же |
| `FACT_FINANCE` | `finance_row_key` | **2024-09-05 … 2026-08-18** | 204 948 | 98,99 МБ | D-2, 4 прогона/сутки | 🔴 нет — 205 тыс. строк ×2 семантики, Looker будет считать неверно |
| `FACT_STOCKS_SNAPSHOT` | `snapshot_date × nm_id × warehouse_key` | **2026-07-16 … 2026-08-20** | 5 706 | 1,23 МБ | сегодня | ✅ да, для графика остатка |
| `FACT_ADS_SKU_DAILY` | `date × advert_id × nm_id` | **2026-04-13 … 2026-08-19** | 5 706 | 1,07 МБ | D-1 | ⚠️ да, но без имён кампаний и SKU |
| `FACT_ADS_COSTS_DAILY` | `date × advert_id` | **2026-04-13 … 2026-08-17** | 1 816 | 0,23 МБ | **D-3, статус `STALE`** | 🔴 нет — это биллинг, Stage 3B не принят |

Все FACT собраны одним прогоном `MART_RUNS`, `built_at = 2026-08-20 04:02 UTC` (07:02 МСК),
`target_date = 2026-08-19`, `status = COMPLETE`, `environment = prod`.

🔴 **`FACT_FINANCE.sale_amount` и `return_amount_rub` пусты полностью** (0 непустых из 204 948).
Выручку с этого объекта брать нельзя. Работают: `finance_for_pay_accounting`, `commission_amount`,
`logistics_amount`, `storage_fee`, `deduction`, `penalty` (204 948 непустых) и
`marketplace_fee_gap_rub` (39 373 непустых — только строки «Продажа»/«Возврат» по SKU).

### 1.3 MART-слой

**`MART_SKU_DAILY`** — 7 302 строки, 4,73 МБ, грейн `day × nm_id`, покрытие **2024-09-05 … 2026-08-19**,
25 SKU, `built_at 2026-08-20 04:04 UTC`.

Это **DENSE spine**: для каждого SKU генерируется непрерывный ряд суток от его первого события
до `build_as_of`, пропуски заполняются `IFNULL(...,0)`. Прямое следствие — §4.

Четыре разные временные базы в одной строке (это контракт, не дефект, но интерфейс обязан это знать):

| группа колонок | база даты |
|---|---|
| `ad_spend`, `views`, `clicks`, `ad_orders_raw`, `ads_revenue_raw_rub` | дата рекламной активности |
| `orders_qty/rub`, `canceled_qty/rub` | **дата заказа** |
| `buyouts_qty/rub`, `returns_qty/rub`, `sales_for_pay_operational` | **дата продажи/возврата** |
| `wb_reward_cost_positive`, `logistics_cost_positive`, `marketplace_fee_rub`, `finance_for_pay_accounting` | **дата финансовой операции (расчёт)** |

Поэтому `hybrid_day_contribution_pre_cogs` = `buyouts_rub − marketplace_fee_rub − logistics_cost_positive − ad_spend`
— это **cross-base величина, а не P&L суток**. Управленчески осмысленна на горизонте от недели.
`settlement_day_contribution_pre_cogs` = `finance_for_pay_accounting − ad_spend` — на базе расчёта.

🔴 **`ad_spend` — АТРИБУТИРОВАННЫЙ расход**, тождественно `FACT_ADS_SKU_DAILY.stats_spend_rub`.
Проверено суммой за всю историю: `MART_SKU_DAILY.ad_spend` = `FACT_ADS_SKU_DAILY.stats_spend_rub`
= **523 365,38 ₽** до копейки. Биллинг за тот же период = **514 064,00 ₽**
(`FACT_ADS_COSTS_DAILY`), разрыв **9 301,38 ₽ = 1,81 %** — тот самый Stage 3B.
Колонок `ad_spend_billed_rub` / `billed_complete` в **выкаченной** витрине **нет**
(код Stage 3B лежит в `sql/mart/pr_mart2b_sku_daily.sql`, но не задеплоен).

**`MART_RUNS` / `V_MART_RUN_LOG`** — 18 прогонов, ежедневно 04:00 UTC = **07:00 МСК**, `target_date = D-1`.
17.08 было 4 подряд `ERROR` до успеха в 13:20 UTC — сборка витрины не гарантированно тихая.

**`REF_COST_MAP`** — 19 строк, справочник мэппинга операций в категории затрат. Служебный.

### 1.4 Dashboard / presentation views (то, что уже есть)

| объект | грейн | строк сейчас | пригодность для Looker |
|---|---|---|---|
| `V_DATA_FRESHNESS` | `layer_code` | **13** | ✅ **READY NOW**, подключать как есть |
| `V_ADS_SCREEN_SKU` | `(as_of_date, window_days, nm_id)` | **14** | ⚠️ окно **фиксировано**: только `as_of=2026-08-19`, `window=28` |
| `V_ADS_SCREEN_QUERY` | `(as_of_date, window_days, nm_id, norm_query)` | **1 821** | ⚠️ то же, окна 28 и 90 |
| `V_ADS_FUNNEL_SKU_28D` / `_QUERY_28D` / `_QUERY_90D` | окно | — | ⚠️ фиксированное окно, тяжёлые baseline-расчёты |
| `V_ADS_FUNNEL_QUERY_DAILY` | `day × nm_id × norm_query` | **39 000** (2026-04-13…08-19, 19 SKU) | ✅ **единственный period-driven рекламный источник** |
| `V_ADS_FUNNEL_SIGNALS` | сигнал на запрос | — | ⚠️ окно фиксировано |
| `V_WB_FINANCE_AMOUNTS_LONG_MAPPED` | `finance_row_key × amount_field` | ~245 тыс. | 🔴 нет, но это **единственный источник** хранения/удержаний |
| `V_WB_SUPPLIES_DETAIL` | `supply_id × nm_id` | **497** (108 поставок) | ⚠️ да, но см. §7 BLOCKED |
| `V_WB_SUPPLIES_INTAKE_BY_SKU` | `nm_id` | 25 | ⚠️ агрегат за всю историю, без периода |

🔴 **Главное ограничение presentation layer сегодня: он не period-driven.**
`V_ADS_SCREEN_*` и все `*_28D/_90D` — это снимок одного окна на одну `as_of_date`.
Требование «произвольный Date Range» они **не выполняют и выполнить не могут**.
Period-driven сегодня только: `MART_SKU_DAILY`, `V_ADS_FUNNEL_QUERY_DAILY`,
`FACT_STOCKS_SNAPSHOT`, `FACT_ORDERS`, `FACT_SALES`, `FACT_FINANCE`.

### 1.5 REF SKU

`wb_raw.REF_SKU_MASTER` — вью, **25 SKU, все `active = TRUE`** (`1083392113` доехал, задача REF-Sync закрыта).
Колонки для дашборда: `product_name_short/full`, `category`, `line`, `product_type`, `is_bundle`, `brand`,
`wb_vendor_code`, `barcode`, `volume_ml`, флаги `include_in_pnl / _ads_analysis / _supply_plan / _stock_alerts`,
`data_quality_status`. Свежесть: `ref_sku_master` = `OK`, последний успех 7 ч назад.
Детектор сирот `sku_orphans` = **0** (SKU с продажами вне справочника за 90 суток нет).

🔴 **`REF_COGS` в BigQuery нет.** Себестоимость живёт только в листе `COST_HISTORY`.
Следствие: вся экономика на дашборде — **pre-COGS**. Чистая прибыль и маржинальность — `NOT AVAILABLE`.

---

## 2. Looker MVP: четыре страницы

Общий каркас всех четырёх страниц:

* **один Date Range control** (произвольный диапазон) + встроенное сравнение Looker
  «предыдущий период» (той же длины) — SQL для этого менять не надо, см. §8;
* **шапка «Данные актуальны на …»** — из `V_DASH_FRESHNESS_HEADER`;
* **бейдж покрытия** — «в выбранном периоде N суток без данных по рекламе / заказам» из
  `V_DASH_COVERAGE_DAILY`. Это и есть fail-closed в интерфейсе (§4).

### 2.1 Executive

Источник: `V_DASH_KPI_DAILY` (грейн `day`) + `V_DASH_COVERAGE_DAILY` + `V_DASH_STOCK_HEALTH`.

| KPI | формула | правило агрегации |
|---|---|---|
| Выручка (заказы) | `SUM(orders_rub)` | SUM |
| Выручка (выкупы) | `SUM(buyouts_rub)` | SUM |
| Заказы, шт | `SUM(orders_qty)` | SUM |
| Выкупы, шт | `SUM(buyouts_qty)` | SUM |
| Отмены, шт / ₽ | `SUM(canceled_qty / canceled_rub)` | SUM |
| Возвраты, шт / ₽ | `SUM(returns_qty / returns_rub)` | SUM |
| **Buyout rate** | `SUM(buyouts_qty) / SUM(orders_qty)` | **ratio-of-sums**, не `AVG` |
| Сбор маркетплейса | `SUM(marketplace_fee_rub)` | SUM |
| Вознаграждение WB (vw) | `SUM(wb_reward_cost_positive)` | SUM, справочно |
| Логистика | `SUM(logistics_cost_positive)` | SUM |
| Хранение | `SUM(storage_rub)` | SUM, **только уровень счёта** |
| Удержания | `SUM(deduction_rub)` | SUM, **только уровень счёта** |
| Штрафы / приёмка / эквайринг | `SUM(...)` | SUM |
| Реклама (атрибуция) | `SUM(ad_spend_attributed_rub)` | SUM, `NULL` вне покрытия |
| К перечислению | `SUM(finance_for_pay_accounting)` | SUM |
| Вклад (hybrid, pre-COGS) | `SUM(hybrid_contribution_pre_cogs)` | SUM |
| Вклад, % | `SUM(hybrid_contribution_pre_cogs) / SUM(buyouts_rub)` | **ratio-of-sums** |
| Остаток | `V_DASH_STOCK_HEALTH.stock_qty` | **snapshot, НЕ SUM по дням** |
| ДРР | `SUM(ad_spend_attributed_rub) / SUM(buyouts_rub)` | **ratio-of-sums** |

Плитки «вчера / 7 дней / предыдущие 7 / MTD» строятся **не отдельными вью**, а
тем же `V_DASH_KPI_DAILY` под четырьмя date-range-контролами. Так период остаётся произвольным.

### 2.2 SKU Analytics

Источник: `V_DASH_SKU_DAILY` (грейн `day × nm_id`), группировка колонок — управленческая, не дамп:

* **Sales** — `orders_qty`, `orders_rub`, `canceled_qty`, средняя цена заказа `SUM(orders_rub)/SUM(orders_qty)`
* **Buyouts** — `buyouts_qty`, `buyouts_rub`, `returns_qty`, `buyout_rate`
* **Economics** — `marketplace_fee_rub`, `logistics_cost_positive`, `finance_for_pay_accounting`,
  `hybrid_contribution_pre_cogs`, вклад %
* **Advertising** — `ad_spend_attributed_rub`, `views`, `clicks`, `ad_orders_raw`, `ads_revenue_raw_rub`
* **Efficiency** — CTR, CPC, CPM, CPO, ДРР, ROAS, ACOS — **все ratio-of-sums**
* **Stock** — из `V_DASH_STOCK_HEALTH`: `stock_qty`, `velocity_7d`, `days_of_stock`, `oos_risk`

Drill-down `Business → SKU → Day`: одна иерархия Looker на `V_DASH_SKU_DAILY`
(`category / line → product_name_short → day`). Отдельные вью не нужны — грейн уже самый мелкий.

### 2.3 Advertising

Два источника, соединённые общим `nm_id`:

* `V_DASH_ADS_SKU_DAILY` — грейн `day × nm_id × advert_id`, с именем кампании;
* `V_DASH_ADS_QUERY_DAILY` — грейн `day × nm_id × norm_query`.

Метрики: `views`, `clicks`, `spend_attributed_rub`, `ad_orders_raw`, `ads_revenue_raw_rub`;
производные CTR / CPC / CPM / ROAS / ACOS / ДРР — ratio-of-sums.

Drill-down `SKU → Campaign → Query`: `V_DASH_ADS_SKU_DAILY` для первых двух уровней,
переход на `V_DASH_ADS_QUERY_DAILY` по `nm_id` (запросная статистика WB **не несёт `advert_id`
однозначно** — на уровне `norm_query` кампании схлопнуты в `advert_ids ARRAY`; это ограничение
источника, а не вью).

🔴 **Stage 3B.** На странице публикуется **только атрибутированный расход**, колонка называется
`spend_attributed_rub` и подписана в интерфейсе «атрибуция WB, не биллинг».
Legacy `MART_SKU_DAILY.ad_spend` переименовывается в presentation-слое в
`ad_spend_attributed_rub` — **это переименование, не пересчёт**. Биллинг (`FACT_ADS_COSTS_DAILY`)
на дашборд **не выводится** до закрытия 3B.1: он `STALE` (D-3), не финален на D+1 и не монотонен.

### 2.4 Stocks & Supplies

| блок | источник | статус |
|---|---|---|
| Текущий остаток | `V_DASH_STOCK_HEALTH` (последний снимок) | ✅ |
| Скорость продаж | `V_DASH_STOCK_HEALTH.velocity_7d/14d/28d` | ✅ |
| Дней остатка | `stock_qty / velocity` | ✅ |
| Риск OOS | порог по `days_of_stock` | ✅ |
| Динамика остатка | `V_DASH_STOCKS_DAILY` | ✅ но история **только с 2026-07-16** |
| Поставки: отправлено | `V_DASH_SUPPLIES.qty_sent` | ✅ |
| Поставки: принято | `V_DASH_SUPPLIES.qty_accepted` | ⚠️ **недостоверно до 2025** |
| Склад назначения / транзита | `dest_/transit_/actual_warehouse_name` | ✅ **имена сохранены** |
| Поставки в пути (incoming) | — | 🔴 **NOT AVAILABLE** |
| Недостачи (sent − accepted) | — | 🔴 **BLOCKED** |

---

## 3. Presentation-layer design: контракт `V_DASH_*`

Правило: **Looker не соединяет FACT, не дедуплицирует, не выбирает источник правды,
не пересчитывает финансовую семантику и не интерпретирует свежесть.**
Единственное, что Looker делает сам, — `SUM()` аддитивных колонок и деление двух сумм
для ratio-метрик (это не бизнес-логика, а способ агрегации; см. §8).

Предлагается **девять** вью. Меньше — нельзя без переноса логики в Looker; больше — избыточно.

---

### 3.1 `V_DASH_COVERAGE_DAILY` — фундамент fail-closed

* **grain:** `day` (одна строка на календарные сутки от `2024-09-05` до `CURRENT_DATE('Europe/Moscow')`)
* **dimensions:** `day`
* **metrics/flags:** `orders_covered BOOL`, `sales_covered BOOL`, `ads_covered BOOL`,
  `finance_covered BOOL`, `finance_final BOOL`, `stocks_covered BOOL`,
  `orders_coverage_start DATE`, `ads_coverage_start DATE`, `finance_last_final_week DATE`
* **source objects:** `FACT_ORDERS`, `FACT_SALES`, `FACT_ADS_SKU_DAILY`, `FACT_FINANCE`,
  `wb_raw.FINANCE_WEEK_STATUS`, `FACT_STOCKS_SNAPSHOT`
* **date semantics:** календарные сутки МСК
* **NULL semantics:** флагов `NULL` нет — только `TRUE`/`FALSE`. Отсутствие строки невозможно.
* **freshness semantics:** пересчитывается на каждый запрос из фактов, отдельного лага нет

Границы, зафиксированные фактически на 20.08.2026:

```
orders_coverage_start   = 2026-04-13   (WB отдаёт статистику ≤ 90 суток; глубже невосстановимо)
sales_coverage_start    = 2026-03-30
ads_coverage_start      = 2026-04-13
stocks_coverage_start   = 2026-07-16   (+ дыра 18–20.07.2026, 3 суток)
finance_coverage_start  = 2024-09-05
finance_last_final_week = 2026-08-10   (неделя 17.08 ещё не финализирована)
```

---

### 3.2 `V_DASH_KPI_DAILY` — Executive

* **grain:** `day` (бизнес-уровень, без SKU)
* **dimensions:** `day`, плюс все флаги покрытия из 3.1 (join по `day`)
* **metrics (все SUM-аддитивные):**
  `orders_qty, orders_rub, canceled_qty, canceled_rub, buyouts_qty, buyouts_rub,
   returns_qty, returns_rub, sales_for_pay_operational,
   marketplace_fee_rub, wb_reward_cost_positive, logistics_cost_positive,
   acquiring_rub, storage_rub, deduction_rub, penalty_rub, acceptance_rub,
   reimbursement_rub, finance_for_pay_accounting,
   ad_spend_attributed_rub, views, clicks, ad_orders_raw, ads_revenue_raw_rub,
   hybrid_contribution_pre_cogs, settlement_contribution_pre_cogs`
* **source objects:** агрегат `MART_SKU_DAILY` по `day` (SKU-часть) **+**
  `V_WB_FINANCE_AMOUNTS_LONG_MAPPED WHERE NOT is_sku_row` (уровень счёта:
  хранение, удержания, приёмка, часть штрафов) **+** `V_DASH_COVERAGE_DAILY`
* **date semantics:** 🔴 **смешанная база, документируется явно** —
  `orders_*` по дате заказа, `buyouts_*`/`returns_*` по дате продажи,
  `*_rub` финансовые и `finance_for_pay_accounting` по дате расчёта,
  `ad_*` по дате рекламной активности. Вклад — cross-base, не P&L суток.
* **NULL semantics:**
  * `ad_spend_attributed_rub`, `views`, `clicks`, `ad_orders_raw`, `ads_revenue_raw_rub`
    = **`NULL`, если `NOT ads_covered`** (сегодня витрина отдаёт там `0` — это и есть дефект §4);
  * `orders_*`/`canceled_*` = `NULL`, если `NOT orders_covered`;
  * `buyouts_*`/`returns_*` = `NULL`, если `NOT sales_covered`;
  * внутри покрытия `0` означает настоящий ноль и остаётся `0`;
  * `hybrid_contribution_pre_cogs` = `NULL`, если `NULL` любое из слагаемых
    (иначе «вклад» вырастет ровно на неизвестный расход).
* **freshness semantics:** колонка `kpi_data_as_of DATE` = `MIN` из
  `MAX(day)` по каждому источнику, участвующему в строке; плюс `mart_built_at TIMESTAMP`.

Хранение и удержания — **только уровень счёта**, проверено:
`storage` 722 строки / 411 172 ₽ и `deduction` 371 строка / **4 933 014 ₽** — все с `is_sku_row = FALSE`.
🔴 Разносить их по SKU нельзя ни в вью, ни в Looker: связки «SKU → расход» источник не даёт.

---

### 3.3 `V_DASH_SKU_DAILY` — SKU Analytics

* **grain:** `day × nm_id`
* **dimensions:** `day`, `nm_id`, `internal_sku`, `product_name_short`, `product_name_full`,
  `category`, `line`, `product_type`, `brand`, `is_bundle`, `sku_status`, `include_in_pnl`,
  + флаги покрытия
* **metrics:** аддитивная часть `MART_SKU_DAILY` (без единой ratio-колонки — см. §8):
  `orders_qty, orders_rub, canceled_qty, canceled_rub, buyouts_qty, buyouts_rub,
   returns_qty, returns_rub, sales_for_pay_operational,
   ad_spend_attributed_rub, views, clicks, ad_orders_raw, ads_revenue_raw_rub,
   ads_revenue_dedup_estimate_rub, ad_orders_dedup_estimate,
   wb_reward_cost_positive, logistics_cost_positive, marketplace_fee_rub,
   finance_for_pay_accounting, hybrid_contribution_pre_cogs, settlement_contribution_pre_cogs`
* **source objects:** `MART_SKU_DAILY` ⟕ `REF_SKU_MASTER` (свёрнут до одной строки на `nm_id`
  ДО join — как в `V_ADS_SCREEN_SKU`, fan-out структурно невозможен) ⟕ `V_DASH_COVERAGE_DAILY`
* **date semantics / NULL semantics:** как в 3.2
* **freshness semantics:** `mart_built_at`, `build_as_of_date` — pass-through из витрины
* 🔴 **колонки `ctr, cpm, cpc, cpo_attributed, blended_cpo, drr_orders, drr_buyouts, roas, acos`
  и все `*_7d/_14d` в эту вью НЕ переносятся.** Они посуточные и в произвольном диапазоне
  дают неверный ответ при любой агрегации, кроме пересчёта из сумм.

---

### 3.4 `V_DASH_ADS_SKU_DAILY` — Advertising, уровень кампании

* **grain:** `day × nm_id × advert_id`
* **dimensions:** `day`, `nm_id`, `internal_sku`, `product_name_short`, `advert_id`,
  `campaign_name`, `campaign_type`, `campaign_status`, `payment_type`
* **metrics:** `views, clicks, spend_attributed_rub, ad_orders_raw, ads_revenue_raw_rub`
* **source objects:** `FACT_ADS_SKU_DAILY` ⟕ `wb_raw.V_ADV_CAMPAIGNS` (430 кампаний;
  🔴 `advertId` там **STRING** — приведение типа делается в вью, не в Looker) ⟕ `REF_SKU_MASTER`
* **date semantics:** дата рекламной активности
* **NULL semantics:** `NULL` вне `ads_covered`; `campaign_name` `NULL` → подставляется
  `'кампания ' || advert_id`, чтобы строка не исчезла из отчёта
* **freshness semantics:** `ads_data_as_of = MAX(date)`, лаг до 29 ч (§6)

### 3.5 `V_DASH_ADS_QUERY_DAILY` — Advertising, уровень запроса

* **grain:** `day × nm_id × norm_query`
* **dimensions:** `day`, `nm_id`, `internal_sku`, `product_name_short`, `norm_query`,
  `advert_count`, `advert_ids` (массив — справочно, не для join)
* **metrics:** `views_sum, clicks_sum, atbs_sum, orders_sum, shks_sum, spend_sum,
  spend_on_imp, avg_pos_x_views, avg_pos_x_clicks, rows_with_impressions`
* **source objects:** `wb_mart.V_ADS_FUNNEL_QUERY_DAILY` ⟕ `REF_SKU_MASTER`
* **date semantics:** дата запроса
* **NULL semantics:** запрос без показов в сутки строки не имеет — это **разрежённый** грейн,
  и это правильно: «нет строки» ≠ «ноль показов по этому запросу»
* **freshness semantics:** `ads_query_stats` — свой журнал, порог SLA **осознанно не задан**
  (`success_age_is_sla = FALSE`); интерфейс обязан показывать часы **без** зелёного статуса
* 🔴 `avg_pos_*` — **взвешенные суммы, а не средние**: средняя позиция считается
  `SUM(avg_pos_x_views)/SUM(views_sum)`. `AVG(avg_pos_x_views)` бессмысленно.

### 3.6 `V_DASH_STOCKS_DAILY` — динамика остатка

* **grain:** `snapshot_date × nm_id × warehouse_key`
* **dimensions:** `snapshot_date`, `nm_id`, `internal_sku`, `product_name_short`,
  `warehouse_key`, `warehouse_name`, `region_name`, `is_latest_snapshot BOOL`
* **metrics:** `quantity`, `in_way_to_client`, `in_way_from_client`
* **source objects:** `FACT_STOCKS_SNAPSHOT` ⟕ `REF_SKU_MASTER`
* **date semantics:** **снимок на дату**, не поток
* **NULL semantics:** суток 18–20.07.2026 в данных **нет** — строк не будет.
  🔴 График обязан рисовать разрыв, а не соединять точки прямой: иначе интерфейс
  выдумает остаток за трое суток. Реализация: отдельная колонка `has_snapshot BOOL`
  на плотном календаре, либо явный «пропуск» в настройках графика.
* **freshness semantics:** `stocks` в `V_DATA_FRESHNESS` = `OK`, `data_as_of = 2026-08-20`
* 🔴 **`warehouse_name` обезличен WB с 13–16.08.2026** («Склад WB РФ»). Разрез по складам
  в свежих сутках нерабочий — подпись обязательна, иначе владелец решит, что товар «уехал».

### 3.7 `V_DASH_STOCK_HEALTH` — остаток / скорость / дней остатка

* **grain:** `nm_id` (**as-of последний снимок; НЕ period-driven по построению**)
* **dimensions:** `nm_id`, `internal_sku`, `product_name_short`, `category`, `line`,
  `include_in_stock_alerts`, `stock_as_of_date`
* **metrics:** `stock_qty`, `in_way_to_client`, `in_way_from_client`,
  `velocity_7d`, `velocity_14d`, `velocity_28d` (шт/сутки, **average-per-day**),
  `days_of_stock_7d/14d/28d`, `oos_risk STRING`, `velocity_basis STRING`
* **source objects:** `FACT_STOCKS_SNAPSHOT` (последняя дата) + `MART_SKU_DAILY`
  (окна скорости) + `REF_SKU_MASTER`
* **date semantics:** остаток — **end-of-period snapshot**; скорость — среднесуточная за окно
* **NULL semantics:** `days_of_stock = NULL` при `velocity = 0` (не `∞`, не 999);
  `oos_risk = 'НЕТ ДАННЫХ'` при `velocity = NULL`
* **freshness semantics:** `stock_as_of_date` выносится на карточку рядом с числом
* ⚠️ Скорость считать по **выкупам** (`buyouts_qty`, дата продажи) — они и есть списание
  со склада. Заказы дадут завышенную скорость на величину невыкупа.

### 3.8 `V_DASH_SUPPLIES` — поставки

* **grain:** `supply_id × nm_id`
* **dimensions:** `supply_id`, `fact_date`, `nm_id`, `internal_sku`, `product_name_short`,
  `vendor_code`, `barcode`, `dest_warehouse_name`, `transit_warehouse_name`,
  `actual_warehouse_name`, `via_transit`, `box_type_id`, `status_id`
* **metrics:** `qty_sent`, `qty_accepted`, `acceptance_cost`, `paid_acceptance_coefficient`
* **source objects:** `wb_mart.V_WB_SUPPLIES_DETAIL` ⟕ `REF_SKU_MASTER`
* **date semantics:** `fact_date` — дата фактической приёмки
* **NULL semantics:** 🔴 `qty_accepted` отдаётся **вместе с флагом `accepted_is_reliable BOOL`**.
  Факт: 2024 — 21 поставка, отправлено 13 920, принято **0** (все 47 строк `accepted_quantity_zero`);
  2025 — 24 368 / 14 720; 2026 — 6 500 / 6 228. Ноль 2024 года — **не недостача, а отсутствие поля**.
  При `accepted_is_reliable = FALSE` вью отдаёт `qty_accepted = NULL`, а не `0`.
* **freshness semantics:** 🔴 **суточный триггер `runWbSuppliesDaily()` не установлен**.
  Последний снимок `2026-08-18 21:04 UTC`, последняя поставка `2026-07-28`.
  У слоя **нет строки в `V_DATA_FRESHNESS`** — светофор его не видит.

### 3.9 `V_DASH_FRESHNESS_HEADER` + pass-through `V_DATA_FRESHNESS`

* **grain:** 1 строка (глобальная шапка)
* **metrics:** `data_as_of_min DATE`, `worst_status STRING` (`OK`/`STALE`/`ERROR`),
  `layers_ok INT64`, `layers_stale INT64`, `layers_error INT64`,
  `header_text STRING` («Данные актуальны на 19.08.2026; реклама — на 17.08.2026»)
* **source objects:** `V_DATA_FRESHNESS`
* **NULL semantics:** `UNMAPPED` → `ERROR` (fail-closed), как в контракте 1
* Детальная плашка на всех страницах — прямой pass-through `V_DATA_FRESHNESS` (13 строк).

---

## 4. Историческое покрытие и fail-closed семантика

### 4.1 Что показал факт

Разложение `MART_SKU_DAILY` по эпохам (запрос от 20.08.2026):

| период | суток | строк | `ad_spend IS NULL` | `ad_spend = 0` | `ad_spend > 0` | `orders_qty > 0` | `buyouts_qty > 0` | есть finance |
|---|---|---|---|---|---|---|---|---|
| до 2026-03-30 | 571 | 4 034 | **0** | **4 034** | 0 | **0** | 0 | 4 034 |
| 2026-03-30 … 04-12 | 14 | 228 | **0** | **228** | 0 | **0** | 1 | 228 |
| с 2026-04-13 | 129 | 3 040 | **0** | 1 168 | 1 872 | 1 437 | 1 470 | 3 040 |

🔴 **`NULL` в витрине не встречается ни разу.** DENSE spine заполняет пропуски нулём
(`IFNULL(a.ad_spend,0)` и т.д. — `sql/mart/pr_mart2b_sku_daily.sql`, CTE `joined`).

### 4.2 Почему это ломает дашборд

Пользователь выбирает Date Range `01.01.2026 – 20.08.2026`. Витрина честно отдаёт:

* реклама за январь–апрель = **0 ₽** — хотя реклама тогда, возможно, была,
  а данных о ней у нас нет и никогда не будет;
* заказы за тот же период = **0 шт** — хотя финансовые отчёты за эти сутки есть
  и деньги там ненулевые;
* следствие: **ДРР периода занижается**, ROAS завышается, `blended_cpo` рушится,
  а вклад считается «как будто реклама была бесплатной».

Это ровно тот класс ошибки, который в проекте уже ловили дважды
(`V_ADV_COSTS_DAY_COVERAGE`: «правило `day <= MAX(date)` опровергнуто данными»;
`billed_complete = FALSE` → billed-KPI строго `NULL`, не `0`).

### 4.3 Предлагаемая fail-closed семантика

**Правило 1 — ноль только внутри покрытия.**
Вне окна покрытия метрика = `NULL`, никогда не `0`.
`0` внутри покрытия — утверждение «расхода не было», и оно проверяемо.

**Правило 2 — производная наследует неизвестность.**
Если любой вход ratio или разности `NULL`, результат `NULL`.
ДРР периода, в котором есть сутки без покрытия рекламы, **не считается вовсе** —
он не «примерно верный», он неверный.

**Правило 3 — неизвестность обязана быть видимой.**
`SUM()` в Looker молча игнорирует `NULL`, и сумма за 2024–2026 будет выглядеть законченной.
Поэтому рядом с каждым блоком выводится счётчик из `V_DASH_COVERAGE_DAILY`:

```
дни в периоде без покрытия рекламы   = COUNT_DISTINCT(day WHERE NOT ads_covered)
дни в периоде без покрытия заказов   = COUNT_DISTINCT(day WHERE NOT orders_covered)
финансовая неделя не финализирована  = MAX(day) > finance_last_final_week
```

Ненулевой счётчик красит блок и добавляет подпись
«период выходит за границу покрытия: реклама с 13.04.2026». Числа при этом **не прячутся** —
прячется только их интерпретация как полных.

**Правило 4 — отсутствие строки в coverage = `FALSE`.**
Тот же принцип, что в контракте 3B.1: пока полнота не доказана, она не предполагается.

**Правило 5 — граница по умолчанию.**
Date Range по умолчанию открывается на `[ads_coverage_start … CURRENT_DATE]`,
то есть с **13.04.2026**. Пользователь может уйти глубже — но уже осознанно и с бейджем.

---

## 5. Performance / cost

Измерено фактически, не оценено.

| запрос | `totalBytesProcessed` | `totalBytesBilled` |
|---|---|---|
| Разложение `MART_SKU_DAILY` по эпохам | 409 КБ | 10 МБ |
| Все 7 покрытий FACT одним UNION | 5,6 МБ | 73 МБ |
| `V_DATA_FRESHNESS` целиком | 12,8 МБ | 210 МБ |
| `V_ADS_SCREEN_SKU` + `_QUERY` + funnel daily одним UNION | 8,9 МБ | 84 МБ |
| `V_WB_FINANCE_AMOUNTS_LONG_MAPPED` полный скан по категориям | 47 МБ | 47 МБ |
| `V_WB_SUPPLIES_DETAIL` целиком | 35 КБ | 21 МБ |

Вывод: **самый тяжёлый объект слоя — 47 МБ на запрос.** Бесплатный лимит BigQuery — 1 ТБ/месяц.
Даже при 200 обращениях в сутки к самой тяжёлой вью это ~280 ГБ/мес — внутри бесплатного лимита.

**Решение: все девять `V_DASH_*` остаются обычными `VIEW`. Materialized/dashboard table — не нужны.**
Преждевременная материализация здесь стоила бы дороже, чем сканирование: появился бы
второй источник правды и расписание его обновления.

Два исключения, за которыми стоит наблюдать, но **не оптимизировать сейчас**:

1. `V_ADS_FUNNEL_QUERY_28D/_90D` и `V_ADS_SCREEN_QUERY` — тяжёлые по CPU
   (634 000 slot-ms на один запрос против 14 000 у `MART_SKU_DAILY`): внутри baseline-расчёты
   с оконными функциями. В MVP они **не используются** (не period-driven), поэтому вопрос не стоит.
   Если позже понадобится «экран сигналов» — тогда и мерить.
2. `V_DASH_KPI_DAILY` читает `V_WB_FINANCE_AMOUNTS_LONG_MAPPED` (47 МБ) на каждый рендер.
   Порог для пересмотра: **если счёт BigQuery превысит 1 ТБ/мес** — тогда материализовать
   суточный агрегат уровня счёта (он крошечный, ~700 строк/год).

Настройка Looker: включить **кеш 12 часов** (данные обновляются раз в сутки в 07:00 МСК —
чаще перечитывать нечего) и **отключить** авто-refresh.

---

## 6. Online freshness: реальная задержка каждого KPI

Замеры на 20.08.2026, `V_DATA_FRESHNESS` + `INGEST_RUNS` + `MART_RUNS`.

| KPI | источник | расписание | `data_as_of` | реальная задержка | как показывать |
|---|---|---|---|---|---|
| Заказы (шт/₽) | `FACT_ORDERS` | **ежечасно** (посл. 12:31 UTC) | **2026-08-20** | **≤ 1 ч** | «сегодня, обновлено в HH:MM» |
| Продажи / выкупы | `FACT_SALES` | **ежечасно** | 2026-08-19 | ≤ 1 ч + D-1 | «по 19.08» |
| Остатки | `FACT_STOCKS_SNAPSHOT` | ежесуточно ~06:30 МСК | **2026-08-20** | ≤ 24 ч | «снимок на 20.08» |
| Реклама: показы/клики/атрибуция | `FACT_ADS_SKU_DAILY` | **1 прогон/сутки ~05:07 МСК** | 2026-08-19 | **до 29 ч** | «реклама по 19.08» |
| Реклама: запросы | `V_ADS_FUNNEL_QUERY_DAILY` | тот же прогон + ручные доборы | 2026-08-19 | до 29 ч, **SLA не задан** | часы **без** зелёного бейджа |
| Реклама: биллинг | `FACT_ADS_COSTS_DAILY` | 1/сутки | **2026-08-17** | **до 3 суток, статус `STALE`** | на дашборд **не выводить** |
| Финансы (сборы, логистика, к перечислению) | `FACT_FINANCE` | 4×/сутки (07:26/12:24/18:35/23:58 МСК) | 2026-08-18 | ≤ 6 ч, но **неделя закрывается до 9 суток** | «расчёт по 18.08, последняя закрытая неделя — 10.08» |
| Все витринные KPI | `MART_SKU_DAILY` | ежесуточно **07:00 МСК**, `target_date = D-1` | 2026-08-19 | **≥ 24 ч** | «витрина собрана 20.08 в 07:04 за 19.08» |
| Поставки | `V_WB_SUPPLIES_DETAIL` | 🔴 **триггера нет** | снимок 18.08 | **не ограничена** | «данные вручную от 18.08» + предупреждение |

🔴 **Ключевой вывод по онлайновости.** Заказы в BigQuery доступны с задержкой ≤ 1 часа,
но **на дашборде они появятся только назавтра**, потому что все KPI идут через `MART_SKU_DAILY`
с `target_date = D-1`. Если владельцу нужно «заказы сегодня» — это отдельный
KPI-блок поверх `FACT_ORDERS` напрямую, с явной подписью «оперативно, вне витрины»
и **без** участия в контрибуции и ДРР.

Общая шапка страницы (`V_DASH_FRESHNESS_HEADER`):

```
Данные актуальны на 19.08.2026 · витрина собрана 20.08 07:04 МСК
Заказы: сегодня ≤1 ч · Остатки: 20.08 · Реклама: 19.08 · Финансы: 18.08 (закрытая неделя 10.08)
⚠️ 1 слой STALE: рекламный биллинг (17.08) — на дашборде не используется
```

Плюс раскрывающаяся плашка со всеми 13 строками `V_DATA_FRESHNESS`
(`layer_code`, `status`, `data_age_days`, `success_age_hours`, `status_reason`).
🔴 Колонку `success_age_hours` для `ads_query_stats` показывать **серым**, а не зелёным:
`success_age_is_sla = FALSE` — порог осознанно не задан.

---

## 7. Gap analysis

### READY NOW — можно вывести в Looker сегодня, без единой строки нового SQL

| объект | что даёт |
|---|---|
| `wb_mart.MART_SKU_DAILY` | вся страница SKU Analytics и Executive, если ограничить период `≥ 13.04.2026` |
| `wb_mart.V_DATA_FRESHNESS` | плашка свежести, 13 слоёв |
| `wb_mart.FACT_STOCKS_SNAPSHOT` | график остатка и текущий остаток |
| `wb_raw.REF_SKU_MASTER` | 25 SKU, названия, категории, линейки, флаги |
| `wb_mart.V_ADS_SCREEN_SKU` / `_QUERY` | готовый рекламный экран — **но только окно 28/90 дней на 19.08** |
| `wb_mart.V_ADS_FUNNEL_QUERY_DAILY` | реклама по запросам с произвольным периодом |
| `wb_mart.FACT_ADS_SKU_DAILY` | реклама по кампаниям с произвольным периодом (без имён) |

### NEEDS DASHBOARD VIEW — данные есть, нужна presentation-вью

| потребность | чего не хватает | вью |
|---|---|---|
| Fail-closed по покрытию | границ покрытия как данных | `V_DASH_COVERAGE_DAILY` |
| Executive: хранение, удержания, приёмка, штрафы | их **нет** в `MART_SKU_DAILY` | `V_DASH_KPI_DAILY` |
| SKU-таблица с названиями и группировкой | join к `REF_SKU_MASTER` | `V_DASH_SKU_DAILY` |
| Реклама с именами кампаний | `V_ADV_CAMPAIGNS.advertId` — STRING, нужно приведение | `V_DASH_ADS_SKU_DAILY` |
| Реклама по запросам с названиями SKU | join к `REF_SKU_MASTER` | `V_DASH_ADS_QUERY_DAILY` |
| Дней остатка, риск OOS, скорость | нигде не считается | `V_DASH_STOCK_HEALTH` |
| Разрыв 18–20.07 в остатках | плотный календарь + флаг | `V_DASH_STOCKS_DAILY` |
| Поставки с названиями и флагом достоверности | флага нет | `V_DASH_SUPPLIES` |
| Шапка «Данные актуальны на …» | скалярная свёртка | `V_DASH_FRESHNESS_HEADER` |

### BLOCKED — бизнес-семантика ещё не принята

| что | почему заблокировано |
|---|---|
| **Рекламный расход по биллингу** | Stage 3B выкат заблокирован до 3B.1; разрыв с атрибуцией 9 301,38 ₽ = 1,81 %; `FACT_ADS_COSTS_DAILY` в статусе `STALE` (D-3). На дашборде — **только атрибуция**, подписанная как атрибуция. |
| **Недостачи поставок (`sent − accepted`)** | 2024 год: принято 0 при отправленных 13 920 — это отсутствие поля, а не недостача. Пока граница `acceptedQuantity` не принята, метрика «недостача» не публикуется. |
| **Что считать «выручкой» на Executive** | три кандидата: заказы (дата заказа), выкупы (дата продажи), к перечислению (дата расчёта). Дают три разных числа за один период. Нужно решение владельца, какое — главное. |
| **Buyout rate** | `SUM(buyouts_qty)/SUM(orders_qty)` за календарный период — не когортная величина: числитель и знаменатель относятся к разным заказам. Нужно решение: календарный (просто, приблизительно) или когортный (правильно, требует новой вью по `srid`). |
| **Вклад как P&L суток** | `hybrid_day_contribution_pre_cogs` — cross-base конструкция. Нужно согласовать, на каком горизонте её показывать (предложение: не мельче недели). |
| **Чистая прибыль / маржинальность** | `REF_COGS` в BigQuery нет, себестоимость только в листе `COST_HISTORY`. Вся экономика — `pre_cogs`, и это должно быть подписано на экране, а не подразумеваться. |

### NOT AVAILABLE — данных нет вообще

| что | причина | восстановимо? |
|---|---|---|
| Заказы и продажи до 2026-03/04 | WB отдаёт статистику **не глубже 90 суток** | 🔴 нет, никогда |
| Реклама до **13.04.2026** | то же ограничение | 🔴 нет |
| Остатки до **2026-07-16** | загрузчик запущен тогда | 🔴 нет |
| Остатки за **18–20.07.2026** | 3 суток отсутствуют в снимках | 🔴 нет |
| Поставки в пути / ожидаемые | все 108 поставок имеют `status_id = 5` (завершена); `acceptance_resolution` пуст во всех 497 строках | ⏭️ возможно, другим методом API |
| Поимённый склад в остатках с 13–16.08.2026 | WB обезличил (`Склад WB РФ`) | 🔴 нет |
| Хранение / удержания / приёмка **по SKU** | источник даёт их только на уровне счёта (`is_sku_row = FALSE`) | 🔴 нет |
| `FACT_FINANCE.sale_amount`, `return_amount_rub` | колонки существуют, заполнены на 0 % | ⏭️ вопрос к лоадеру, отдельно от Stage 4 |
| Выручка/заказы в 2024–2025 в разрезе «заказано» | есть только реализация из финотчёта | 🔴 нет |

---

## 8. Period-driven семантика (дополнение к Stage 4)

### 8.1 Правило агрегации для каждой метрики

| метрика | правило | формула для произвольного диапазона |
|---|---|---|
| `orders_qty/rub`, `canceled_*`, `buyouts_*`, `returns_*` | **SUM** | `SUM(x)` |
| `ad_spend_attributed_rub`, `views`, `clicks`, `ad_orders_raw`, `ads_revenue_raw_rub` | **SUM** | `SUM(x)` |
| `marketplace_fee_rub`, `logistics_*`, `wb_reward_*`, `storage`, `deduction`, `penalty`, `acceptance`, `acquiring` | **SUM** | `SUM(x)` |
| `finance_for_pay_accounting` | **SUM** | `SUM(x)` |
| `hybrid_/settlement_contribution_pre_cogs` | **SUM** | `SUM(x)` |
| **Buyout rate** | **ratio-of-sums** | `SUM(buyouts_qty)/SUM(orders_qty)` |
| **CTR** | **ratio-of-sums** | `SUM(clicks)/SUM(views)` |
| **CPC** | **ratio-of-sums** | `SUM(spend)/SUM(clicks)` |
| **CPM** | **ratio-of-sums** | `SUM(spend)/SUM(views)*1000` |
| **CPO (attributed)** | **ratio-of-sums** | `SUM(spend)/SUM(ad_orders_raw)` |
| **Blended CPO** | **ratio-of-sums** | `SUM(spend)/SUM(orders_qty)` |
| **ДРР (заказы / выкупы)** | **ratio-of-sums** | `SUM(spend)/SUM(orders_rub)` · `SUM(spend)/SUM(buyouts_rub)` |
| **ROAS / ACOS** | **ratio-of-sums** | `SUM(ads_revenue_raw_rub)/SUM(spend)` и обратное |
| **Средняя цена заказа** | **weighted average = ratio-of-sums** | `SUM(orders_rub)/SUM(orders_qty)` |
| **Вклад, %** | **ratio-of-sums** | `SUM(contribution)/SUM(buyouts_rub)` |
| **Средняя позиция в выдаче** | **weighted average** | `SUM(avg_pos_x_views)/SUM(views_sum)` |
| **Остаток** | **end-of-period snapshot** | значение на `MAX(snapshot_date)` в диапазоне; **по суткам НЕ суммируется**, по SKU/складам внутри одних суток — суммируется |
| **Товар в пути** | **end-of-period snapshot** | то же |
| **Скорость продаж** | **average-per-day** | `SUM(buyouts_qty) / (число суток покрытия в диапазоне)` |
| **Дней остатка** | **производная от двух правил** | `stock_eop / velocity`; `NULL` при `velocity = 0` |
| **Средний расход в день** | **average-per-day** | `SUM(spend) / (число суток покрытия)` — 🔴 делитель = **покрытые** сутки, не календарные |

🔴 **`AVG()` не применяется ни к одной ratio-метрике.** Единственные законные `AVG` —
это average-per-day, и там делитель считается явно из `V_DASH_COVERAGE_DAILY`, а не из `COUNT(*)`.

### 8.2 Три обязательных представления любого диапазона

1. **Итог периода** — `SUM` / ratio-of-sums по таблице выше.
2. **Среднее в день** — только там, где имеет бизнес-смысл: расход, заказы, выкупы,
   выручка, скорость продаж. **Не применяется** к остатку, buyout rate, ДРР, ROAS, вкладу %.
3. **Сравнение с предыдущим периодом той же длины** — встроенная функция Looker Studio
   «Comparison date range → Previous period». **SQL для этого менять не надо.**
   🔴 Условие честности: предыдущий период тоже проверяется на покрытие.
   Если сравниваемый период выходит за `ads_coverage_start`, дельта помечается
   «сравнение недостоверно: N суток без данных» и не окрашивается в зелёный/красный.

### 8.3 Почему произвольный Date Range работает без изменения SQL

Все девять `V_DASH_*` имеют **суточный (или снимочный) грейн и колонку-дату**.
Looker передаёт выбранный диапазон как `WHERE day BETWEEN ...` — предикат уходит в BigQuery,
вью не содержит ни одного зашитого `7`/`14`/`30`. Именно поэтому в §3.3 из
`V_DASH_SKU_DAILY` исключены готовые `*_7d/_14d` — они бы конкурировали с фильтром периода
и давали два разных ответа на один вопрос.

Единственное осознанное исключение — `V_DASH_STOCK_HEALTH` (§3.7): «дней остатка»
по построению относится к «сейчас», а не к выбранному периоду. Это подписывается
на карточке словами «на 20.08.2026», а не прячется.

---

## 9. Минимальный implementation plan

Цель — рабочий Looker **не дожидаясь Stage 3B**. Ни один шаг ниже от Stage 3B не зависит.

### Шаг 0 — сегодня, 0 строк SQL, ~2 часа

Подключить Looker Studio к трём объектам и собрать две страницы:

* `MART_SKU_DAILY` → Executive + SKU Analytics;
* `V_DATA_FRESHNESS` → плашка свежести;
* `FACT_STOCKS_SNAPSHOT` → график остатка.

Ограничения этого шага, которые надо принять сознательно:
Date Range по умолчанию **с 13.04.2026**; все ratio-метрики — calculated fields
`SUM(a)/SUM(b)`; хранения и удержаний нет; имён SKU нет (только `nm_id`).

**Это уже работающий онлайн-дашборд на production-данных.** Дальше — улучшения, а не старт.

### Шаг 1 — PR #1: fail-closed (полдня)

`V_DASH_COVERAGE_DAILY` + `V_DASH_FRESHNESS_HEADER`.
После него дашборд перестаёт врать на периодах глубже 13.04.2026.
**Это самый важный PR всего Stage 4** — до него дашборд опаснее своего отсутствия.

### Шаг 2 — PR #2: основной контракт (день)

`V_DASH_SKU_DAILY` + `V_DASH_KPI_DAILY`.
Появляются названия SKU, категории, хранение, удержания, приёмка, штрафы,
и `NULL` вместо `0` вне покрытия. Executive и SKU переводятся на них.

### Шаг 3 — PR #3: реклама (день)

`V_DASH_ADS_SKU_DAILY` + `V_DASH_ADS_QUERY_DAILY`.
Страница Advertising с drill-down `SKU → Campaign → Query`, только атрибуция.

### Шаг 4 — PR #4: склад и поставки (день)

`V_DASH_STOCKS_DAILY` + `V_DASH_STOCK_HEALTH` + `V_DASH_SUPPLIES`.
Страница Stocks & Supplies в объёме §2.4 — без «в пути» и без «недостач».

### Параллельно, вне очереди Stage 4

* 🔴 поставить суточный триггер `runWbSuppliesDaily()` и **завести слой `supplies`
  в `V_DATA_FRESHNESS`** — сейчас светофор его не видит вовсе;
* ⏭️ `REF_COGS` в BigQuery (REF-Sync PR2) — снимает `pre_cogs` со всей экономики;
* ⏭️ разобраться, почему `FACT_FINANCE.sale_amount` пуст на 100 %.

### Порядок утверждения

Согласно правилу проекта: **UI не реализуется до утверждения dashboard contract.**
Утверждению подлежат: §3 (девять вью с их grain / NULL / freshness),
§4.3 (пять правил fail-closed), §8.1 (таблица агрегации) и три открытых вопроса из BLOCKED
(что такое «выручка», как считать buyout rate, на каком горизонте показывать вклад).

---

## 10. Как проверить результат

Контрольные цифры, снятые сегодня; после реализации `V_DASH_*` они обязаны совпасть до копейки.

| гейт | проверка | эталон на 20.08.2026 |
|---|---|---|
| L1 | `SUM(V_DASH_SKU_DAILY.ad_spend_attributed_rub)` за всю историю | **523 365,38 ₽** |
| L2 | то же = `SUM(FACT_ADS_SKU_DAILY.stats_spend_rub)` | delta = **0,00** |
| L3 | `SUM(V_DASH_KPI_DAILY.*)` = `SUM(MART_SKU_DAILY.*)` по каждой SKU-колонке | delta = **0,00** |
| L4 | строк в `V_DASH_SKU_DAILY` = строк в `MART_SKU_DAILY` | **7 302** |
| L5 | SKU в `V_DASH_SKU_DAILY` | **25**, ни одного `NULL` в `product_name_short` |
| L6 | `COUNTIF(ad_spend_attributed_rub IS NULL)` за период до 13.04.2026 | **4 262** (= 4 034 + 228), было `0` |
| L7 | `COUNTIF(ad_spend_attributed_rub = 0)` за период до 13.04.2026 | **0** |
| L8 | `SUM(V_DASH_KPI_DAILY.storage_rub)` | **411 172 ₽** |
| L9 | `SUM(V_DASH_KPI_DAILY.deduction_rub)` | **4 933 014 ₽** |
| L10 | `V_DASH_SUPPLIES`: `SUM(qty_sent)` / `COUNT(DISTINCT supply_id)` | **44 788 / 108** |
| L11 | `V_DASH_SUPPLIES`: `qty_accepted IS NULL` за 2024 | **47 строк из 47** |
| L12 | `V_DASH_STOCKS_DAILY`: суток со снимком в 2026-07-16…2026-08-20 | **33 из 36**, отсутствуют 18–20.07 |
| L13 | `V_DASH_STOCK_HEALTH`: `stock_qty` итого на последний снимок | **1 262 шт** |
| L14 | `V_DASH_FRESHNESS_HEADER.layers_stale` | **2** (`ads_costs`, `fact_ads_costs_daily`) |
| L15 | `V_DASH_COVERAGE_DAILY`: `ads_covered = FALSE` суток до 13.04.2026 | все, без исключения |
| L16 | ratio-метрика в Looker за 2 суток ≠ среднее двух суточных ratio | проверка на любом SKU с разным объёмом по суткам |

Риск поломки production при реализации — **нулевой**: все девять объектов
создаются как новые `VIEW`, ни одна существующая таблица или вью не меняется.
Откат = `DROP VIEW`.
