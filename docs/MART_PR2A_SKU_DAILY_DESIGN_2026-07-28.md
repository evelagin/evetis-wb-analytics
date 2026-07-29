# PR-Mart2a — MART_SKU_DAILY (дизайн rev5, после аудита #4)

**Дата:** 2026-07-28  **Статус:** ЧЕРНОВИК на аудит #5. Кода нет. Аудит #4 закрыт: реклама raw+dedup_estimate+flag
(raw авторитетен, estimate не «выручка»); ad_orders raw сохранён (без смены семантики); spine до `build_as_of_date`
(RANGE по UNIX_DATE); unattributed_ads вынесен в MART_ADS_RECON_DAILY (ACCOUNT-док).
**Основа:** `wb_mart` (Mart1 в проде). Витрина читает ТОЛЬКО FACT из `wb_mart` (+REF), не `wb_raw`.
**Решения владельца:** drr_orders И drr_buyouts (основной KPI дашборда = **drr_buyouts**); `cpo` → **`cpo_attributed`**;
бухгалтерский и операционный вклад — РАЗДЕЛЬНО; KPI сейчас, маржа (COGS) — MART v2.

## 0. Правки аудита #1 (все закрыты эмпирикой на живых FACT 28.07)

| # | Замечание | Решение (доказано) |
|---|---|---|
| 1 | FACT_SALES без quantity → SUM(quantity) невозможен | Источник продаж НЕ содержит quantity (`raw_json` без ключа `"quantity"`; 3563 строки=3563 sale_id). **Контракт 1 row = 1 unit**: `buyouts_qty/returns_qty = COUNT(*)`. quantity в FACT_SALES НЕ добавляем. |
| 2 | orders_rub: SUM(pwd) или SUM(pwd*qty)? | В FACT_ORDERS `quantity ≡ 1` (min=max=1, q≠1: 0 строк) → `SUM(price_with_disc)=SUM(price_with_disc*quantity)=3 392 721`. Контракт 1 row=1 unit и для заказов: `orders_qty=COUNT(*)`, `orders_rub=SUM(price_with_disc)`. |
| 3 | ACCOUNT: нельзя суммировать все денежные колонки | Вынесено в дизайн ACCOUNT (rev2): явная mapping-таблица operation→cost_category→authoritative_field→sign + reconciliation-guard. См. `MART_ACCOUNT_COST_CATEGORY_DESIGN_2026-07-28.md`. |
| 4 | cost_category ≠ operation_type_normalized | Там же: храним ОБА поля (raw `operation_type_normalized` + стабильная `cost_category`). |
| 5 | contribution = finance_for_pay − ad_spend только если ads не внутри finance | **Доказано: 0 строк рекламы в finance** (0 из 202 471 по %реклам%/%продвиж%) → WB не вычитает рекламу в for_pay, двойного вычитания нет. |

## 1. Грейн и якорь
- Строка = (day, nm_id), nm_id — сопоставленный SKU (nm_id<>0, sku_match_status='matched'). Несопоставленные → ACCOUNT.
- `day` = дата события источника: orders→order_date, sales→sale_date, ads→date, finance→finance_date (правка 5).
- Якорь = UNION всех (day, nm_id) из FACT_ORDERS/FACT_SALES/FACT_ADS_SKU_DAILY/FACT_FINANCE(nm<>0), LEFT JOIN каждого.
- Атрибуты SKU — LEFT JOIN `REF_SKU_MASTER` (current-state). Универсум ~26 SKU (компактно).
- **Грейн-контракт: 1 row = 1 unit** в orders и sales (доказано §0.1–0.2).

## 2. Каталог метрик (формулы)
Операционные:
- `orders_qty` = COUNT(*) FACT_ORDERS; `orders_rub` = SUM(price_with_disc).
- `buyouts_qty` = COUNTIF(is_return=false); `buyouts_rub` = SUM(IF(NOT is_return, price_with_disc, 0)) (FACT_SALES).
- `returns_qty_op` = COUNTIF(is_return=true); `returns_rub_op` = SUM(IF(is_return, price_with_disc, 0)) — операц. возвраты.
Реклама (FACT_ADS_SKU_DAILY, агрегируем advert_id→nm): `ad_views`, `ad_clicks`, `ad_spend_rub`(=stats_spend_rub) —
по-площадочные, аддитивны. Атрибутивные (raw+estimate, §4): `ad_orders_raw`, `ad_orders_dedup_estimate`,
`ads_revenue_raw_rub`, `ads_revenue_dedup_estimate_rub`, `multitouch_ambiguous_flag`.
KPI (все через SAFE_DIVIDE; по умолчанию по RAW):
- `ctr` = ad_clicks/ad_views; `cpc` = ad_spend_rub/ad_clicks; **`cpo_attributed`** = ad_spend_rub/ad_orders_raw;
- `cr_ad` = ad_orders_raw/ad_clicks; `roas` = ads_revenue_raw_rub/ad_spend_rub; `acos` = ad_spend_rub/ads_revenue_raw_rub;
- `drr_orders` = ad_spend_rub/orders_rub; **`drr_buyouts`** = ad_spend_rub/buyouts_rub (основной KPI дашборда).
  Контроль live: Σspend 446 091 / Σad_revenue_raw 1 759 917 → ACOS≈25.3%, ROAS≈3.94 (по raw).
Финансы по SKU — из общего long-form слоя `V_WB_FINANCE_AMOUNTS_LONG` (см. ACCOUNT-док §2–3), nm-matched, направление
из REF_COST_MAP по (операция×поле):
- **Нормализованные costs по (операция×поле)** (аудит #3.1): commission_amount = COST на «Продажа/Коррекция/Возврат»
  (у товарных Σ commission −888 653 на «Продажа»); logistics_amount = COST (полож.). `commission_cost_positive` и
  `logistics_cost_positive` = Σ `cost_amount_positive` из long-form (COST→+ABS). Прямое вычитание signed-полей запрещено
  (знаки полей разные: commission −, logistics +).
- `finance_return_rub` = Σ return_amount_rub (финансовые возвраты — ОТДЕЛЬНО от операционных);
  `finance_for_pay_rub` = Σ finance_for_pay_accounting.
Вклад — ДВЕ вспомогательные колонки, обе смешивают временные базы (аудит #3.2: commission/logistics тоже лагируют
в Finance). НИ ОДНА не называется «дневной прибылью»; **основной управленческий вклад — rolling 7/14d (§6)**:
- **`hybrid_day_contribution_pre_cogs`** = buyouts_rub − commission_cost_positive − logistics_cost_positive − ad_spend_rub
  (микс sale_date/finance_date/ads date — честно помечен hybrid).
- **`settlement_day_contribution_pre_cogs`** = finance_for_pay_accounting − ad_spend_rub (бухгалтерская база; реклама не
  внутри for_pay §0.5; тоже cross-base). Обе — только для закрытых периодов/сверки, не для дневной прибыли.
- Маржа (`− COGS`) — MART v2 после REF Sync PR2.

## 3. Физика / паттерн
Датасет `wb_mart`; PARTITION BY day; CLUSTER BY nm_id. Паттерн BUILD→ASSERT→publish (fail-closed COUNT>0, дедуп
(day,nm_id), not-null grain, физика через IS). Оркестрация — PR-Mart3. Потребителей не подключать до Mart3.

## 4. Follow-up Mart1.1 (перед Mart2a) — RAW сохраняем, estimate помечаем (аудит #4.1–4.3)
sum_price/orders — АТРИБУТИВНЫЕ (мульти-тач): разбор 16 групп (orders=1 в каждом appType при clicks=0 в части;
sum_price=цена товара повторена) → SUM 36 339 vs MAX 16 640, дельта 19 699 ₽ = 1,12%. Точная дедупликация БЕЗ order_id
НЕВОЗМОЖНА (частичные дубли heuristic не ловит) → **raw обязателен, dedup — только estimate, не авторитетная выручка.**
Mart1.1 добавляет в `FACT_ADS_SKU_DAILY` (аддитивно, БЕЗ смены семантики существующих колонок):
- `ads_revenue_raw_rub` = SUM(sum_price) — авторитетно для acceptance (**== raw Σ 1 759 917 ₽**);
- `ads_revenue_dedup_estimate_rub` = SUM с дедупом одинаковых ненулевых sum_price по appType — **ОЦЕНКА**;
- `ad_orders_raw` = существующее `orders` (уже SUM по appType — семантику НЕ меняем);
- `ad_orders_dedup_estimate`; `multitouch_ambiguous_flag` (TRUE, если в группе есть идентичный ненулевой sum_price).
ACOS/ROAS по умолчанию считаются по **raw** (документировано); estimate — альтернатива для чувствительных к дублям.
`ad_revenue_*`/`ad_orders_*` — директивные, ВНЕ любой консервации (ads-консервация — только по `stats_spend`,
ACCOUNT-док). parse-QC на sum_price + пере-bootstrap. quantity в FACT_SALES — НЕ требуется (1 row=1 unit).

## 5. Контрольные цифры приёмки
1. Дедуп: rows == distinct(day, nm_id). 2. Реклама: Σ ad_spend==Σ stats_spend (446 091); Σ ad_revenue==Σ sum_price (1 759 917).
3. Заказы/выкупы контрольного дня == живая книга «Evetis аналитика 2.0». 4. Σ commission/logistics по витрине ==
Σ FACT_FINANCE(nm<>0) на окне. 5. Все KPI конечны (SAFE_DIVIDE), нет inf/NaN. 6. fail-closed: BUILD>0, объём=источнику.

## 6. Rolling KPI на КАЛЕНДАРНОМ spine (аудит #3.5)
⚠️ `ROWS BETWEEN N PRECEDING` считает последние N *строк*, а не календарные дни → при пропущенных днях окно «съезжает».
Поэтому строим **dense spine `calendar_date × active nm_id`**: для каждого nm — все календарные дни от его первого
события ДО **`build_as_of_date`** (аудит #4.4 — ЯВНЫЙ параметр процедуры, напр. CURRENT_DATE('Europe/Moscow') или заданная
дата; НЕ до последнего события — иначе «уснувший» SKU выпадает и rolling врёт). `GENERATE_DATE_ARRAY(first_day, build_as_of_date)`;
LEFT JOIN метрик, **пропущенные дни = 0**. Rolling: `ORDER BY UNIX_DATE(day) RANGE BETWEEN 6 PRECEDING AND CURRENT ROW`
(BQ требует целое для RANGE → UNIX_DATE; окно строго календарное, не ROWS). Спайн — общий базис витрины.
Окна: `ad_spend_7d/14d`, `ad_revenue_7d/14d`, `buyouts_rub_7d/14d`, `orders_rub_7d/14d`, `orders_qty_7d/14d`, `ad_orders_7d/14d`;
производные: `drr_buyouts_7d = ad_spend_7d/buyouts_rub_7d` (**основной KPI**), `roas_7d = ad_revenue_7d/ad_spend_7d`,
`blended_cpo_7d = ad_spend_7d/orders_qty_7d` (и 14d). Всё SAFE_DIVIDE.
`blended_cpo` (переим. из cpo_actual) = ad_spend_rub/orders_qty (по ФАКТИЧЕСКИМ заказам) — рядом с `cpo_attributed`
(= ad_spend_rub/ad_orders, по рекламным). Обе в витрине. `unattributed_ads` тоже с rolling-7d (ACCOUNT-док §5).

## 7. Остаточные вопросы аудитору
- ДРР по выкупам как основной KPI — учитывать лаг: buyouts свежи операционно, «деньги» финализирует finance;
  на дашборде метка свежести (freshness финансов — PR-Mart3).
- Окна rolling: 7 и 14 дней достаточно для v1, или добавить 30d?

## 8. План
PR-Mart1.1 (ads_revenue_rub) → PR-Mart2a (MART_SKU_DAILY) → [cost_category] PR-Mart2b (MART_ACCOUNT_DAILY)
→ PR-Mart3 (оркестратор) → MART v2 (маржа).
