# Stage 4A · PR2 — реализация и read-only validation

**Дата:** 20.08.2026 · **Режим:** SQL написан, проверен **inline через SELECT**.
**Production BigQuery не изменён.** Ни одного DDL не выполнено. Ветка/PR не созданы.
**Итог: 18 гейтов, PASS 18, FAIL 0.** Рекомендация — `APPROVE`.

---

## 1. Точный diff

| файл | действие | размер |
|---|---|---|
| `sql/dash/dashboard_contract_v2.sql` | **новый** — три `CREATE OR REPLACE VIEW` | 3 стейтмента |
| `sql/dash/dashboard_contract_v2_validation.sql` | **новый** — гейты D1–D18 | 18 запросов |
| `docs/STAGE4A_PR2_IMPLEMENTATION_2026-08-20.md` | **новый** — этот протокол | — |

Не тронуто: `dashboard_contract_v1.sql`, `pr_mart2b_sku_daily.sql`, `pr_mart1_facts.sql`,
все загрузчики, `V_DATA_FRESHNESS`, `V_ADS_SCREEN_*`, Stage 3B.1.

---

## 2. 🔴 CORRECTION-2 — найдено при реализации, дизайн был неполон

Дизайн §4 исходил из того, что расходы делятся **на две** группы: SKU-уровень целиком
лежит в витрине, всё прочее — уровень счёта. Замер показал **три**, и одна из них
в дизайне отсутствовала:

| группа | сумма | где живёт |
|---|---|---|
| 1. SKU-уровень, который витрина несёт | **4 105 620,55 ₽** | `logistics` 2 718 375,42 + `wb_reward` 1 387 245,13 |
| 2. **SKU-уровень, которого в витрине нет вообще** | **240 900,49 ₽** | `acquiring` 449 142,73 · `loyalty` 87,40 · `penalty` 1 322,00 · `reimbursement_logistics` −209 900,62 |
| 3. SKU-уровень **вне universe** витрины | **14 711,02 ₽** (184 строки) | `nm_id`, которых нет в активном справочнике |
| 4. Уровень счёта | **5 298 874,47 ₽** | storage, deduction, acceptance, penalty, возмещения, прочее |

Процедура витрины забирает из `LONG_MAPPED` **только** `logistics` и `wb_reward`
(`sql/mart/pr_mart2b_sku_daily.sql`, CTE `fin`). Всё остальное SKU-уровня в витрину
не попадает — и по первоначальному дизайну PR2 **пропало бы с экрана**, потому что
часть уровня счёта берётся по предикату `NOT is_sku_row` и этих строк не видит.
Это прямо нарушало ваше правило «не скрывать деньги».

**Исправление:** группы 2 и 3 выведены отдельными наблюдаемыми колонками
(`acquiring_sku_rub`, `loyalty_sku_rub`, `penalty_sku_rub`, `reimbursement_sku_rub`,
`other_sku_rub`, `sku_costs_outside_universe_rub`).
🔴 **В контрибуцию они не входят** — `acquiring` и `wb_reward` сидят внутри спреда
(контракт v1 REV2.4, PR-B2 §4), вычитать их вторым разом нельзя.

Побочно подтвердилась правота предиката вместо перечисления категорий:
`logistics`, `wb_reward` и `acquiring` имеют строки на **обоих** уровнях
(40 173,28 · 183,72 · 29,76 ₽ на уровне счёта). Перечисление их бы потеряло.

---

## 3. Schema / grain каждой вью

### `V_DASH_KPI_DAILY` — грейн `day`, **715 строк = 715 суток**

Блоки колонок: покрытие (pass-through PR1, 16 колонок) · счётчики периода (8) ·
торговля (10) · реклама-атрибуция (5) · расходы SKU-уровня из витрины (3) ·
расходы уровня счёта (7) · SKU-уровень вне витрины (5) · reconciliation (4) ·
расчёт (3) · экономика (4) · диагностика (5).

### `V_DASH_SKU_DAILY` — грейн `day × nm_id`, **7 302 строки = 7 302 уникальных ключа**

25 SKU · `MAX(ref_rows_for_nm_id) = 1` · сирот **0** · строк без названия **0**.
Расходов уровня счёта здесь нет по построению; `account_level_excluded_note`
объясняет это прямо в данных.

### `V_DASH_FRESHNESS_BY_CONTRACT` — грейн `contract_code`, **2 строки**

| contract_code | слоёв | `data_as_of_min_used` | `worst_status_used` | без даты |
|---|---|---|---|---|
| `KPI_DAILY` | 5 | **2026-08-18** | **OK** | — |
| `SKU_DAILY` | 6 | **2026-08-18** | **OK** | `ref_sku_master` |

---

## 4. Результаты D1–D18

| # | проверка | измерено | вердикт |
|---|---|---|---|
| D1 | грейн KPI = календарь PR1 | 715 = 715, дублей 0 | **PASS** |
| D2 | грейн SKU = витрина | 7 302 = 7 302, ключей 7 302 | **PASS** |
| D3 | fan-out справочника | `MAX(ref_rows) = 1`, сирот 0, без названия 0 | **PASS** |
| D4 | SKU-часть KPI = агрегат SKU-вью | шесть величин совпали до копейки | **PASS** |
| D5 | сбор и выплата = `FACT_FINANCE` (matched) | 7 954 656,56 и 18 870 292,80, delta 0,00 | **PASS** |
| D6 | 🔴 **сумма частей = целое** | 4 105 620,55 + 240 900,49 + 14 711,02 + 5 298 874,47 = **9 660 106,53**, остаток **0,00** | **PASS** |
| D7 | 🔴 **контрибуция наследует покрытие** | **1 312 034,95 ₽** на 128 сутках; протечек 0; витрина без гейта дала бы **−7 777 098,96 ₽** | **PASS** |
| D8 | NULL propagation | протечек в KPI — **0**, в SKU — **0** (по пяти группам метрик) | **PASS** |
| D9 | нет `IFNULL(...,0)` / `COALESCE(...,0)` | всего 1 `IFNULL(...,0)` на три вью, и это разрешённое исключение `ref_rows_for_nm_id`; `COALESCE(...,0)` — 0 | **PASS** |
| D10 | нет ratio-колонок | 0 совпадений с закрытым списком запрещённых имён; `AVG(` в коде — **0** | **PASS** |
| D11 | `AVG(ratio) ≠ ratio-of-sums` предъявлено | **15,37 %** против **15,77 %** | **PASS** |
| D12 | PROVISIONAL наблюдаем | 13–19.08 → **2**, закрытый период → **0**, противоречий 0 | **PASS** |
| D13 | signed settled + отдельная корректировка | signed **26 789 919,64**, unsigned **26 803 799,64**, смещение **13 880,00 = 2 × 6 940,00** (11 строк «Возврат»); корректировка **1 834,88** на 8 строках отдельно | **PASS** |
| D14 | карта контрактов | 2 строки, опечаток 0, `ref_sku_master` назван в списке «без даты» | **PASS** |
| D15 | contract-свежесть не подменяет системную | системный **2026-08-17 / STALE** ≤ контрактный **2026-08-18 / OK** | **PASS** |
| D16 | baseline «до» | 7 302 / 523 365,38 ₽ · 14 · 1 821 · 1 816 · 13 слоёв · PR1 715 строк · `V_DASH*` = **ровно 2** | **PASS** |
| D17 | K9 | **N/A pre-deploy** — объектов в BigQuery нет; хэши файла посчитаны (§9) | **N/A** |
| D18 | Stage 3B.1 не задет | `V_ADV_COSTS` 0 · `FACT_ADS_COSTS_DAILY` 0 · `billed` 0 · `V_ADS_SCREEN` 0 · `V_ADS_FUNNEL` 0 · `V_WB_FINANCE_SEMANTIC` 0 | **PASS** |

**17 PASS · 1 N/A (K9 до выката) · FAIL 0.**

---

## 5. Reconciliation: seller-base / settled / returns / adjustments

| величина | KPI_DAILY | SKU_DAILY | источник-эталон | delta |
|---|---|---|---|---|
| `orders_revenue_rub` | 3 339 705,67 | 3 339 705,67 | `MART.orders_rub` 3 339 705,67 | **0,00** |
| `sales_revenue_seller_base_rub` | 3 418 557,40 | 3 418 557,40 | `MART.buyouts_rub` 3 419 298,40 | **−741,00** ⬅ см. ниже |
| `sales_revenue_buyer_paid_rub` | 2 122 563,55 | — | `FACT_SALES.finished_price`, тот же universe | — |
| контроль выкупов | `buyouts_qty` 3 999 | — | независимый счётчик из `FACT_SALES` **3 999** | **0** |
| `ad_spend_attributed_rub` | 523 365,38 | 523 365,38 | `MART.ad_spend` 523 365,38 | **0,00** |
| `marketplace_fee_rub` | 7 954 656,56 | 7 954 656,56 | `FACT_FINANCE` matched 7 954 656,56 | **0,00** |
| `net_settlement_rub` | 18 870 292,80 | 18 870 292,80 | `FACT_FINANCE` matched 18 870 292,80 | **0,00** |
| `sales_revenue_settled_rub` | **26 789 919,64** | — | signed Продажа − Возврат | **0,00** |
| `sales_adjustment_rub` | **1 834,88** | — | 8 строк «Коррекция продаж» | отдельно |
| `contribution_pre_cogs_rub` | **1 312 034,95** | 1 312 034,95 | покрытое окно, 128 суток | **0,00** |

🔑 **delta −741,00 ₽ по seller-base — не дефект, а работающий fail-closed.**
Это ровно та единственная строка `FACT_SALES` от **2026-03-30**, у которой
`sales_covered = FALSE` (одиночная продажа за 13 суток до начала непрерывного
покрытия, просочившаяся через `flag = 0` по `lastChangeDate`). Витрина её отдаёт,
контракт — гасит. `CORRECTION-1` из PR1 предъявлена в деньгах.

### Возвраты и знак (OPEN-3)

```
signed   (Продажа + , Возврат −)   26 789 919,64 ₽   ← контракт PR2
unsigned (поведение MART)          26 803 799,64 ₽
смещение                               13 880,00 ₽ = 2 × 6 940,00 (11 строк «Возврат»)
```

Смещение измерено, а не унаследовано. `MART_SKU_DAILY` **не правился** — расхождение
живёт в гейте D13 и будет расти вместе с числом возвратов.

### Корректировки (OPEN-4)

`sales_adjustment_rub` = **1 834,88 ₽**, 8 строк.
Source predicate: `FACT_FINANCE WHERE supplier_oper_name = 'Коррекция продаж'`,
величина `finance_for_pay_accounting`. У этих строк `marketplace_fee_gap_rub` пуст,
базы выручки нет — поэтому в `sales_revenue_settled_rub` они **не входят** и
выручкой/прибылью не считаются до отдельного бизнес-решения.

---

## 6. Covered vs uncovered contribution

| окно | `contribution_pre_cogs_rub` |
|---|---|
| контракт PR2, покрытые сутки (128) | **+1 312 034,95 ₽** |
| витрина без гейта, вся история (715) | **−7 777 098,96 ₽** |
| из них непокрытая часть (587 суток) | **−9 089 133,91 ₽** |

Разложение покрытого окна сходится точно:

```
3 405 702,31 (выкупы) − 1 299 419,13 (сбор WB) − 270 885,66 (логистика) − 523 362,57 (реклама)
= 1 312 034,95 ₽
```

Вклад в процентах (ratio-of-sums, покрытое окно): **38,52 %**.

---

## 7. Доказательство NULL propagation

Замер по всем 715 суткам KPI и всем 7 302 строкам SKU:

| проверка | KPI | SKU |
|---|---|---|
| `NOT orders_covered` и заказ не `NULL` | **0** | **0** |
| `NOT sales_covered` и выкуп/выручка не `NULL` | **0** | **0** |
| `NOT ads_covered` и расход/показы/клики не `NULL` | **0** | **0** |
| `NOT finance_covered` и сбор/выплата/хранение/settled не `NULL` | **0** | **0** |
| `NOT contribution_covered` и вклад не `NULL` | **0** | **0** |
| строк SKU без строки покрытия (join потерял) | — | **0** |

Плюс структурное доказательство: `IFNULL(...,0)` встречается во всех трёх вью **один раз**,
и это `IFNULL(r.ref_rows_for_nm_id, 0)` — счётчик строк справочника, а не метрика;
ноль там означает «строк справочника нет» и это утверждение, а не подмена. `COALESCE(...,0)` — **0 раз**.

Отдельно: 714 суток из 715 имеют строку витрины; единственная без неё — **2026-08-20**,
сегодняшние сутки за границей `build_as_of_date = 2026-08-19`. Колонки
`has_mart_row` и `mart_build_covered` делают это различие видимым, а не молчаливым.

---

## 8. Доказательство отсутствия ratio-колонок

Статический аудит текста всех трёх вью:

```
ratio-колонок в AS-именах (ctr|cpc|cpm|cpo|roas|acos|drr|_rate|_ratio|_pct|_percent|_share|_7d|_14d|_28d):  0
AVG( в коде:                                                                                                0
```

Гейт D10 повторяет ту же проверку по `INFORMATION_SCHEMA.COLUMNS` после выката —
по фактической схеме, а не по тексту. Два независимых способа, один вывод.

Закрытый список из 17 разрешённых ratio-of-sums пар зафиксирован в
`docs/STAGE4A_PR2_DESIGN_2026-08-20.md` §5 и считается **только** в Looker.
🔴 `buyout_ratio_calendar_ops` (= `SUM(buyouts_qty)/SUM(orders_qty)`) остаётся
operational-метрикой и `buyout_rate` не называется. Настоящий когортный — PR3.

---

## 9. Freshness contract mapping

Карта живёт CTE-константой `contract_layers` внутри `V_DASH_FRESHNESS_BY_CONTRACT`,
попадает в `view_definition` и проверяется K9 и гейтом D14. В Looker фильтрации нет —
интерфейс читает готовую строку.

```
KPI_DAILY → mart_sku_daily, finance, orders, sales, fact_ads_sku_daily
SKU_DAILY → mart_sku_daily, finance, orders, sales, fact_ads_sku_daily, ref_sku_master
```

| | системная шапка PR1 | контракт `KPI_DAILY` |
|---|---|---|
| дата | **2026-08-17** | **2026-08-18** |
| статус | **STALE** | **OK** |
| что задаёт | `ads_costs` — рекламный биллинг, на экран не выводится | `finance` — реально питает KPI |

Оба значения существуют одновременно и выводятся рядом: системное — «худшее по всему
контуру», контрактное — «худшее по тому, из чего собран этот экран».
Два fail-closed правила: слой без `data_as_of` в минимум не входит, но **назван**
(`ref_sku_master`); слой, объявленный в карте, но отсутствующий в источнике, получает
`UNKNOWN` с наивысшим приоритетом и попадает в `layers_declared_but_absent_list`.

---

## 10. Baseline существующих объектов (до выката)

| объект | значение |
|---|---|
| `MART_SKU_DAILY` | 7 302 строки · `SUM(ad_spend)` 523 365,38 ₽ |
| `V_ADS_SCREEN_SKU` / `V_ADS_SCREEN_QUERY` | 14 / 1 821 |
| `FACT_ADS_COSTS_DAILY` | 1 816 |
| `V_DATA_FRESHNESS` | 13 строк |
| `V_DASH_COVERAGE_DAILY` (PR1) | 715 строк |
| `V_DASH_FRESHNESS_HEADER` (PR1) | `data_as_of_min` 2026-08-17, `worst_status` STALE |
| объектов `V_DASH*` в BigQuery | **ровно 2** — оба из PR1 |

Ни один объект PR2 в BigQuery не создан. Валидация выполнена подстановкой тел вью
в `SELECT` (inline bodies), поэтому production остался нетронутым.

---

## 11. K9

**Статус: `N/A` до выката** — объектов в BigQuery нет, сравнивать не с чем.
Хэши нормализованного текста файла посчитаны заранее, чтобы после выката сверка
была механической:

```
V_DASH_KPI_DAILY              10 472 симв. · 172 строки · sha256 361e690dd0034597bfeea48aaedf8d30318f6ca6a9a9fce39b1992c874e44e7a
V_DASH_SKU_DAILY               3 858 симв. ·  85 строк  · sha256 990c030502e5f9c3d900b3c8f65c9d8b8840536616424678a075f3019453fbef
V_DASH_FRESHNESS_BY_CONTRACT   2 601 симв. ·  52 строки · sha256 cb0d56ab7899cdf45a33f7c430f871ee1f0df325b86cf6e430705baceacb9b0b
```

🔴 Правило деления файла, подтверждённое в третий раз: **только по
`CREATE OR REPLACE VIEW`**, терминатор `;` снимать **после** нормализации.
В `dashboard_contract_v2.sql` — **4 точки с запятой внутри комментариев**;
деление по `;` сломало бы сверку гарантированно.

---

## 12. Data contract первого экрана · `Executive · Пульс магазина`

Источники: `V_DASH_KPI_DAILY` (KPI и графики) · `V_DASH_SKU_DAILY` (таблица и drill-down) ·
`V_DASH_FRESHNESS_BY_CONTRACT` (шапка экрана) · `V_DASH_FRESHNESS_HEADER` + `V_DATA_FRESHNESS`
(системный светофор). Один произвольный Date Range, штатное сравнение с предыдущим периодом.

### KPI-строка

| KPI | source field(s) | aggregation | coverage / freshness indicator |
|---|---|---|---|
| Выручка (выкупы) | `sales_revenue_seller_base_rub` | `SUM` | `sales_uncovered_days` |
| Выручка (заказы) | `orders_revenue_rub` | `SUM` | `orders_uncovered_days` |
| Заказы, шт | `orders_qty` | `SUM` | `orders_uncovered_days` |
| Выкупы, шт | `buyouts_qty` | `SUM` | `sales_uncovered_days` |
| Отмены, шт / ₽ | `canceled_qty` / `canceled_rub` | `SUM` | `orders_uncovered_days` |
| Возвраты, шт / ₽ | `returns_qty` / `returns_rub` | `SUM` | `sales_uncovered_days` |
| Доля выкупа (**operational**) | `buyouts_qty` / `orders_qty` | **ratio-of-sums** | оба счётчика; подпись «календарная, не когортная» |
| Сбор маркетплейса | `marketplace_fee_rub` | `SUM` | `finance_uncovered_days` + `finance_is_final` |
| Вознаграждение WB | `wb_reward_rub` | `SUM` | то же |
| Логистика | `logistics_rub` | `SUM` | то же |
| Хранение | `storage_rub` | `SUM` | то же · **уровень счёта** |
| Удержания | `deduction_rub` | `SUM` | то же · **уровень счёта** |
| Приёмка | `acceptance_rub` | `SUM` | то же · **уровень счёта** |
| Возмещения | `reimbursement_account_rub` | `SUM` | то же · **уровень счёта** |
| Эквайринг (справочно) | `acquiring_sku_rub` | `SUM` | то же · **в контрибуцию не входит** |
| Реклама (атрибуция) | `ad_spend_attributed_rub` | `SUM` | `ads_uncovered_days` |
| К перечислению | `net_settlement_rub` | `SUM` | `finance_uncovered_days` |
| Реализация (расчёт) | `sales_revenue_settled_rub` | `SUM` | `finance_is_final` |
| Корректировки | `sales_adjustment_rub` | `SUM` | подпись «не выручка» |
| **Вклад pre-COGS** | `contribution_pre_cogs_rub` | `SUM` | `contribution_uncovered_days` + `contribution_provisional_days` |
| Вклад, % | `contribution_pre_cogs_rub` / `sales_revenue_seller_base_rub` | **ratio-of-sums** | то же |
| ДРР по выкупам | `ad_spend_attributed_rub` / `sales_revenue_seller_base_rub` | **ratio-of-sums** | оба счётчика |
| ROAS / ACOS | `ads_revenue_raw_rub` / `ad_spend_attributed_rub` и обратное | **ratio-of-sums** | `ads_uncovered_days` |
| CTR / CPC / CPM | `clicks`/`views` · `ad_spend`/`clicks` · `ad_spend`/`views`×1000 | **ratio-of-sums** | `ads_uncovered_days` |
| Средняя цена заказа | `orders_revenue_rub` / `orders_qty` | **ratio-of-sums** | `orders_uncovered_days` |
| Средний расход в день | `ad_spend_attributed_rub` / `days_total` | **average-per-day**, делитель — покрытые сутки | `ads_uncovered_days` |

### Графики

Выручка и вклад по суткам · реклама и ДРР по суткам · заказы против выкупов —
все на `V_DASH_KPI_DAILY`, ось X = `day`, диапазон произвольный.

### Таблица SKU (`V_DASH_SKU_DAILY`), группы колонок

**Sales** `orders_qty`, `orders_revenue_rub`, `canceled_qty`, ср. цена ·
**Buyouts** `buyouts_qty`, `sales_revenue_seller_base_rub`, `returns_qty`, доля выкупа ·
**Economics** `marketplace_fee_rub`, `logistics_rub`, `net_settlement_rub`, `contribution_pre_cogs_rub`, вклад % ·
**Advertising** `ad_spend_attributed_rub`, `views`, `clicks`, `ad_orders_raw`, `ads_revenue_raw_rub` ·
**Efficiency** CTR, CPC, CPM, CPO, ДРР, ROAS, ACOS — все ratio-of-sums.
Drill-down `category / line → product_name_short → day` — одна иерархия Looker.

### Индикаторы честности на экране

```
Данные контракта KPI_DAILY актуальны на 18.08.2026        ← V_DASH_FRESHNESS_BY_CONTRACT
Система в целом: 17.08.2026, STALE (ads_costs)            ← V_DASH_FRESHNESS_HEADER
В периоде N суток без покрытия рекламы / заказов          ← *_uncovered_days
Период содержит предварительные финансовые сутки          ← contribution_provisional_days > 0
Экономика pre-COGS: вклад, а не прибыль                   ← economics_basis / economics_note
```

Диапазон по умолчанию — **с 13.04.2026**. Глубже выбрать можно, бейджи загорятся.

### Чего на экране нет и почему — подписано прямо там

Остаток и дней остатка (PR5) · когортный `buyout_rate` (PR3) · кампании и запросы (PR4) ·
прибыль и маржа (нет `REF_COGS`) · биллинговый расход рекламы (Stage 3B.1).

---

## 13. Рекомендация

# `APPROVE`

17 PASS, 1 N/A (K9 до выката), FAIL 0. Production не изменён.

Главный результат — D7: контрибуция за покрытое окно **+1 312 034,95 ₽** вместо
**−7 777 098,96 ₽**, которые витрина отдаёт без гейта. Второй по важности — D6:
четыре группы расходов закрывают финансовый слой **без остатка**, и это поймало
реальный пробел дизайна (CORRECTION-2, 240 900,49 ₽, которые иначе исчезли бы с экрана).

**Жду ACK на выкат PR2 в BigQuery и post-deploy приёмку** (повторный прогон D1–D18
на выкаченных объектах + закрытие D17/K9 в основной форме).
Ветка и PR — за вами. PR3 не начат.
