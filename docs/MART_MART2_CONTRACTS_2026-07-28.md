# PR-Mart2 — четыре контракта перед кодом (APPROVE WITH CHANGES)

**Дата:** 2026-07-28  **Статус:** контракты v2 (APPROVE WITH CHANGES ×2). Закрыты уточнения: динамический acceptance
(не фикс-сумма), канонический ключ `__NULL__` для NULL-операций, режим `ADJUSTMENT` для корректировок (ABS не годится),
точный spine-predicate. Далее — КОД.
Архитектура принята: 3 витрины (SKU_DAILY / ACCOUNT_DAILY / ADS_RECON_DAILY), finance long-form, mapping (operation×field),
UNKNOWN=0, раздельная finance/ads-консервация, raw+estimate, dense spine, rolling 7/14, drr_buyouts основной,
дневной hybrid-вклад ≠ прибыль, COGS/маржа — следующий этап.

## Контракт 1 — алгоритм `dedup_estimate` (грейн date × advert_id × nm_id)
Терминология: `ads_revenue_raw_rub`/`ad_orders_raw` — **source-faithful attribution metric** (точно воспроизводит WB API;
НЕ доказывает уникальность заказов, НЕ «авторитетная выручка»). dedup — только ОЦЕНКА.
Для группы g=(date,advert_id,nm_id) над строками appType:
- `ads_revenue_raw_rub` = SUM(sum_price).
- `ad_orders_raw` = SUM(orders)  (= существующее поле `orders`, семантику НЕ меняем).
- Пусть NZ = строки appType с sum_price<>0; DV = множество РАЗЛИЧНЫХ ненулевых значений sum_price в g.
- `ads_revenue_dedup_estimate_rub` = Σ по v∈DV ( v )  — одинаковые ненулевые считаем ОДИН раз, разные суммируем;
  строки sum_price=0 вносят 0.
- `ad_orders_dedup_estimate` = Σ по v∈DV ( MAX(orders) среди строк с sum_price=v ) + Σ(orders для строк sum_price=0).
  (коллапс мульти-тач дубля к одному заказу на различное значение выручки; строки без выручки-атрибуции сохраняем.)
- `multitouch_ambiguous_flag` = ( COUNT(NZ) > CARDINALITY(DV) )  — TRUE, если хоть одно ненулевое значение повторилось
  по appType (полное или частичное совпадение). Estimate ОСТАЁТСЯ оценкой (точная дедуп без order_id невозможна).
- `zero_revenue_multiorder_flag` (доп. диагностика, аудит; НЕ блокирует Mart1.1) = TRUE, если в g ≥2 строк appType с
  `sum_price=0 AND orders>0` — ещё одна зона неопределённой атрибуции (заказ без выручки-атрибуции в слое).
**Acceptance — ДИНАМИЧЕСКИЙ** (аудит: фикс-сумму в prod-ASSERT нельзя, завтра изменится):
`ASSERT Σ FACT_ADS_SKU_DAILY.ads_revenue_raw_rub == Σ SAFE_CAST(REPLACE(sum_price,',','.') AS NUMERIC) FROM V_ADV_CAMPAIGN_STATS`
(равенство FACT источнику на момент build). `1 759 917 ₽` и дельта `19 699 ₽ (1,12%)` — ТОЛЬКО исторический baseline 28.07 (док).

## Контракт 2 — имена rolling-метрик (raw/estimate раздельно)
Окна 7/14d по dense календарному spine (Контракт 4). По умолчанию KPI дашборда — по RAW; estimate рядом как диагностика.
Поля: `ads_revenue_raw_7d`, `ads_revenue_raw_14d`, `ads_revenue_dedup_estimate_7d`, `ads_revenue_dedup_estimate_14d`,
`ad_orders_raw_7d`, `ad_orders_raw_14d`, `ad_orders_dedup_estimate_7d`, `ad_orders_dedup_estimate_14d`,
`ad_spend_7d/14d`, `buyouts_rub_7d/14d`, `orders_rub_7d/14d`, `orders_qty_7d/14d`.
Производные (по RAW, SAFE_DIVIDE): `drr_buyouts_7d`=ad_spend_7d/buyouts_rub_7d (**основной**), `roas_7d`=ads_revenue_raw_7d/ad_spend_7d,
`blended_cpo_7d`=ad_spend_7d/orders_qty_7d (и 14d).

## Контракт 3 — полный `REF_COST_MAP` (все денежные пары operation × amount_field, live 28.07)
**Канонический ключ операции** (аудит: NULL не сматчится JOIN): везде — long-form, seed REF_COST_MAP, JOIN, guard —
`op_key = COALESCE(operation_type_normalized, '__NULL__')`. Пары «(null) × field» в таблице ниже = ключ `__NULL__`.

**Три режима направления** (`cost_amount_positive`), + `field_normalization_sign` per field (commission=−1; все прочие=+1):
- **COST** → `+ABS(source_signed_amount)` (всегда затрата);
- **CREDIT** → `−ABS(source_signed_amount)` (всегда кредит/возмещение);
- **ADJUSTMENT** (аудит: для корректировок со смешанным знаком, чтобы ABS не превратил кредит в расход) →
  `source_signed_amount × field_normalization_sign` (ЗНАК СОХРАНЯЕТСЯ: нормальная корректировка → +, реверс → − уменьшает расход).
`source_signed_amount` — всегда сырой WB-знак (для консервации). Роутинг SKU vs ACCOUNT — по nm-match, не по категории.
Правило commission: COST на «Продажа»; CREDIT на «Возмещение*»; ADJUSTMENT на «Коррекция/Корректировка*».

| operation_type_normalized | amount_field | direction | cost_category | Σ signed | строк (non-SKU) |
|---|---|---|---|---|---|
| Продажа | commission_amount | COST | commission | −888 653 | 38699 (1) |
| Продажа | acquiring_fee | COST | acquiring | 433 982 | 38691 (1) |
| Логистика | logistics_amount | COST | logistics | 2 721 548 | 44414 (237) |
| Коррекция логистики | logistics_amount | ADJUSTMENT | logistics | 3 997 | 948 (0) |
| (null) | logistics_amount | COST | logistics | 17 998 | 315 (0) |
| Хранение | storage_fee | COST | storage | 393 426 | 676 (676) |
| (null) | storage_fee | COST | storage | 9 578 | 20 (20) |
| Удержание | deduction | COST | deduction | 4 577 802 | 350 (350) |
| (null) | deduction | COST | deduction | 4 467 | 6 (6) |
| Удержание | additional_payment | COST | deduction | 7 248 | 5 (5) |
| Штраф | penalty | COST | penalty | 21 994 | 13 (5) |
| Платная приемка | acceptance | COST | acceptance | 14 934 | 21 (21) |
| Пересчет платной приемки | acceptance | COST | acceptance | 101 654 | 21 (21) |
| Возмещение издержек по перевозке/складским операциям | commission_amount | CREDIT | reimbursement_logistics | −309 009 | 98155 (57063) |
| Возмещение за выдачу/возврат на ПВЗ | commission_amount | CREDIT | reimbursement_pvz | −127 892 | 17561 (17561) |
| Возврат | commission_amount | COST | commission | −384 | 10 (0) |
| Возврат | acquiring_fee | COST | acquiring | 96 | 10 (0) |
| Коррекция продаж | commission_amount | ADJUSTMENT | commission | −1 156 | 8 (0) |
| Коррекция продаж | acquiring_fee | ADJUSTMENT | acquiring | 5 | 1 (0) |
| (null) | acquiring_fee | COST | acquiring | 6 107 | 253 (0) |
| Корректировка эквайринга | acquiring_fee | ADJUSTMENT | acquiring | −42 | 64 (0) |
| Стоимость участия в программе лояльности | additional_payment | COST | loyalty | 87 | 24 (0) |

Все денежные пары покрыты → 10 категорий: commission, acquiring, logistics, storage, deduction, penalty, acceptance,
reimbursement_logistics, reimbursement_pvz, loyalty. **compensation_amount не несёт денег** (guard SUM(ABS)=0).
Примечание: у пар со смешанным знаком (напр. Корректировка эквайринга −5..+17) direction — доминирующий экономический смысл;
поточечная точность обеспечивается `source_signed_amount` (консервация), `cost_amount_positive` — нормализованное представление.
**Live guard (обязателен):** число money-bearing пар (operation×field) с cost_category IS NULL (нет в REF_COST_MAP) = **0**,
иначе build FAIL (сначала расширить REF_COST_MAP). REF_COST_MAP сидируется SQL в `wb_mart`; позже — REF-версионирование.

## Контракт 4 — `build_as_of_date` + SKU-universe spine (точные определения)
`build_as_of_date` — ЯВНЫЙ параметр процедуры (DATE). Guards (fail-closed, все три):
- `build_as_of_date IS NOT NULL`;
- `build_as_of_date <= CURRENT_DATE('Europe/Moscow')`;
- `build_as_of_date >= max_required_source_date`, где
  **`max_required_source_date = GREATEST( (SELECT MAX(order_date) FROM FACT_ORDERS), (SELECT MAX(sale_date) FROM FACT_SALES),
  (SELECT MAX(date) FROM FACT_ADS_SKU_DAILY) )`** — обязательные операционные источники. **FINANCE НЕ входит** (лагает недельно,
  иначе заблокирует свежий build).

**SKU-universe (точный predicate):** `SELECT nm_id FROM wb_raw.REF_SKU_MASTER WHERE marketplace='WB' AND active=TRUE
AND nm_id IS NOT NULL` (live 28.07 = 24 nm; счёт динамический, не фиксировать).

**start_date(nm) (точная формула):** `first_ev(nm) = LEAST( MIN(order_date), MIN(sale_date), MIN(FACT_ADS_SKU_DAILY.date),
MIN(FACT_FINANCE.finance_date) )` по этому nm (по всем FACT). **Fallback**, если у nm НЕТ активности ни в одном FACT
(есть в REF, но не продавался/не рекламировался): `start_date = mart_global_start_date` — параметр процедуры
(дефолт = `LEAST` глобальных MIN тех же FACT, т.е. общий старт данных). Так «мёртвые» SKU присутствуют в spine с нулями
(видно «никогда не продавался»).

**Spine:** для каждого nm из universe — `GENERATE_DATE_ARRAY(start_date(nm), build_as_of_date)` → dense (day, nm_id);
LEFT JOIN метрик, пропуски = 0. Rolling: `... OVER (PARTITION BY nm_id ORDER BY UNIX_DATE(day) RANGE BETWEEN 6 (или 13) PRECEDING AND CURRENT ROW)`.

## Порядок кода (после этих контрактов)
Mart1.1 (ads_revenue_raw/estimate + ad_orders_estimate + flag) → finance long-form + REF_COST_MAP →
MART_SKU_DAILY → MART_ACCOUNT_DAILY → MART_ADS_RECON_DAILY. Каждый — BUILD→ASSERT→publish, fail-closed, раздельные
reconciliation-guards; изменения контрактов описываются в соответствующем PR.
