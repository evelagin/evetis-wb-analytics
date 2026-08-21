# Stage 4A · PR2 — design verification (read-only)

**Дата:** 20.08.2026 · **Режим:** read-only, только `SELECT`. Ни одного объекта не создано и не изменено.
**Статус:** проект. **SQL не написан, PR2 не начат — ждёт вашего ACK.**
**Основание:** PR1 закрыт и принят (`docs/STAGE4A_PR1_ROLLOUT_2026-08-20.md`), OBSERVATION-1 принято в PR2.

---

## 0. Что подтверждено замерами перед проектированием

| проверка | результат |
|---|---|
| `MART_SKU_DAILY.marketplace_fee_rub` = `FACT_FINANCE` (matched SKU) | 7 954 656,56 ₽ = 7 954 656,56 ₽, **delta 0,00** |
| `MART_SKU_DAILY.finance_for_pay_accounting` = `FACT_FINANCE` (matched SKU) | 18 870 292,80 ₽ = 18 870 292,80 ₽, **delta 0,00** |
| деньги вне universe витрины (не-matched SKU) | **484,76 ₽** из 7 955 141,32 ₽ = **0,006 %** |
| 🔑 `retail_price_withdisc_rub` = `marketplace_fee_gap_rub + finance_for_pay_accounting` | восстановлено из `FACT_FINANCE`: Продажа **26 796 859,64 ₽** / 39 362 строки против 26 809 715 ₽ / 39 381 строки в `V_WB_FINANCE_SEMANTIC` — разница ровно на 19 строк, тождество на строку держится |
| контроль: `SUM(fee_gap)` только на «Продажа»/«Возврат» | 7 953 111,64 + 2 029,68 = **7 955 141,32** = полный `SUM(fee_gap)` по `FACT_FINANCE` ✅ |
| `V_DATA_FRESHNESS` | 13 слоёв; `data_as_of` = NULL у `ref_sku_master` и `sku_orphans` |

**🔑 Следствие для scope: `V_WB_FINANCE_SEMANTIC` в PR2 не нужен.** Всё берётся из слоя
`wb_mart` (MART + FACT). Это дешевле и снимает зависимость от тяжёлого unpivot-вью
(47 МБ на запрос; коннектор сегодня на нём устойчиво отваливался по таймауту шлюза).

### 🔴 Главный замер — зачем контрибуции наследовать покрытие

| окно | `SUM(hybrid_day_contribution_pre_cogs)` |
|---|---|
| **где покрытие есть** (128 суток, 13.04 … 18.08) | **+1 312 034,95 ₽** |
| где покрытия нет (587 суток) | **−9 089 133,91 ₽** |
| вся история без гейта | **−7 777 098,96 ₽** |

Причина отрицательного числа не в убытке: комиссия и логистика идут с 2024-09,
а выкупы — только с 2026. Витрина честно вычитает расходы из выручки, которой в тех сутках
физически нет. **Без наследования покрытия из PR1 Executive показал бы минус 7,8 млн ₽.**
С наследованием — 1 312 034,95 ₽, и он раскладывается точно:

```
3 405 702,31 (выкупы) − 1 299 419,13 (сбор WB) − 270 885,66 (логистика) − 523 362,57 (реклама)
= 1 312 034,95 ₽ ✅
```

### 🔴 Замер, доказывающий запрет `AVG(ratio)`

За то же покрытое окно, ДРР по выкупам:

```
ratio-of-sums   SUM(ad_spend) / SUM(buyouts_rub)      = 15,37 %   ← контракт
AVG(row ratio)  AVG(ad_spend / buyouts_rub) по строкам = 15,77 %   ← запрещено
разница 0,40 п.п. на 128 сутках × 25 SKU
```

Разрыв невелик на агрегате магазина и растёт на разрежённых SKU. Это не теория — это замер.

---

## 1. Точный scope PR2

**Три вью, все новые, все — только `VIEW`.**

| # | объект | грейн | зачем |
|---|---|---|---|
| 1 | `wb_mart.V_DASH_KPI_DAILY` | `day` | Executive: бизнес-уровень, включая расходы уровня счёта |
| 2 | `wb_mart.V_DASH_SKU_DAILY` | `day × nm_id` | SKU Analytics + drill-down `Business → SKU → Day` |
| 3 | `wb_mart.V_DASH_FRESHNESS_BY_CONTRACT` | `contract_code` | OBSERVATION-1: свежесть **по контракту**, не по всему контуру |

Файлы: `sql/dash/dashboard_contract_v2.sql`, `sql/dash/dashboard_contract_v2_validation.sql`,
`docs/STAGE4A_PR2_DESIGN_2026-08-20.md` (этот документ как контракт).
`dashboard_contract_v1.sql` **не трогается** — PR1 закрыт.

### Почему третий объект, а не колонки в `V_DASH_FRESHNESS_HEADER`

Ваше требование: общий системный минимум сохраняется, «used» — рядом и с явной,
проверяемой семантикой. Два способа:

| вариант | плюсы | минусы |
|---|---|---|
| **A (рекомендуется)** — новая вью `V_DASH_FRESHNESS_BY_CONTRACT`, грейн `contract_code` | PR1-объект не меняется; PR3–PR5 добавляют **строки**, а не колонки; каждый экран читает свою строку; список слоёв виден как данные | +1 объект в PR2 |
| B — колонки `data_as_of_min_used` / `used_layers_list` в существующую шапку | без нового объекта | 🔴 меняет принятый PR1-объект; смысл «used» плывёт с каждым новым PR — сегодня это KPI+SKU, завтра ещё реклама; K9 и приёмка PR1 обесцениваются |

Вариант B делает определение движущимся во времени — то самое, чего контракт избегает.
Беру A. Если предпочитаете B — скажите, переделаю до написания SQL.

---

## 2. Какие `V_DASH_*` входят именно в PR2

Входят: **`V_DASH_KPI_DAILY`, `V_DASH_SKU_DAILY`, `V_DASH_FRESHNESS_BY_CONTRACT`.**

Не входят (подтверждаю явно): `V_DASH_ORDER_COHORT_DAILY` (PR3),
`V_DASH_ADS_SKU_DAILY` и `V_DASH_ADS_QUERY_DAILY` (PR4),
`V_DASH_STOCKS_DAILY`, `V_DASH_STOCK_HEALTH`, `V_DASH_SUPPLIES` (PR5).

---

## 3. Грейн каждой вью

| вью | грейн | ключ уникальности | ожидаемых строк на якоре |
|---|---|---|---|
| `V_DASH_KPI_DAILY` | сутки | `day` | **715** (совпадает с `V_DASH_COVERAGE_DAILY`) |
| `V_DASH_SKU_DAILY` | сутки × SKU | `(day, nm_id)` | **7 302** (=`MART_SKU_DAILY`, ни строкой больше) |
| `V_DASH_FRESHNESS_BY_CONTRACT` | контракт | `contract_code` | **2** в PR2 (`KPI_DAILY`, `SKU_DAILY`) |

🔴 `V_DASH_KPI_DAILY` строится на **календаре из `V_DASH_COVERAGE_DAILY`**, а не на
`MART_SKU_DAILY`: иначе сутки без единой строки витрины исчезли бы из отчёта, а с ними —
и признак «покрытия не было». Календарь — левая сторона, витрина и финансы подвешиваются `LEFT JOIN`.

🔴 `V_DASH_SKU_DAILY` — строгий pass-through грейна витрины. Справочник `REF_SKU_MASTER`
сворачивается до одной строки на `nm_id` **до** join (тот же приём, что в `V_ADS_SCREEN_SKU`),
поэтому fan-out структурно невозможен; `ref_rows_for_nm_id` остаётся видимым.

---

## 4. Additive metrics

Всё ниже суммируется по любому диапазону. Ни одной ratio-колонки в вью нет.

### `V_DASH_KPI_DAILY`

**Торговля (дата заказа / дата продажи):**
`orders_qty`, `orders_revenue_rub`, `canceled_qty`, `canceled_rub`,
`buyouts_qty`, `sales_revenue_seller_base_rub`, `sales_revenue_buyer_paid_rub`,
`returns_qty`, `returns_rub`

**Реклама (дата рекламной активности), атрибуция:**
`ad_spend_attributed_rub`, `views`, `clicks`, `ad_orders_raw`, `ads_revenue_raw_rub`

**Расходы уровня SKU (дата расчёта):**
`marketplace_fee_rub`, `wb_reward_rub`, `logistics_rub`

**Расходы уровня СЧЁТА (дата расчёта) — их нет и не может быть в SKU-вью:**
`storage_rub`, `deduction_rub`, `acceptance_rub`, `penalty_account_rub`,
`reimbursement_rub`, `other_account_rub`

**Расчёт:**
`net_settlement_rub`, `sales_revenue_settled_rub`

**Экономика:**
`contribution_pre_cogs_rub` (hybrid), `settlement_contribution_pre_cogs_rub`

**Счётчики для произвольного периода** (наследуются из PR1 join'ом по `day`):
`days_total`, `*_uncovered_days`, `contribution_uncovered_days`,
`provisional_finance_days`, `contribution_provisional_days`

Замеренные величины уровня счёта на якоре: хранение **411 172 ₽** (722 строки, 2024-09-05 … 2026-08-18),
удержания **4 933 014 ₽** (371, 2024-10-01 … 2026-08-16), приёмка **120 588 ₽** (42, по 2025-12-14),
штрафы уровня счёта **27 540 ₽** (6).

### `V_DASH_SKU_DAILY`

Тот же список **минус** блок уровня счёта (его нельзя разнести по SKU — источник не даёт связки)
и минус `sales_revenue_settled_rub`, если пройдёт OPEN-3 вариант «из FACT со знаком».

Плюс измерения: `internal_sku`, `product_name_short`, `product_name_full`, `category`, `line`,
`product_type`, `brand`, `is_bundle`, `sku_status`, `include_in_pnl`, `ref_rows_for_nm_id`.

### 🔴 Чего НЕ переносим из витрины

`ctr, cpm, cpc, cpo_attributed, blended_cpo, drr_orders, drr_buyouts, roas, acos` и **все**
`*_7d/_14d`. Причина одна и та же: посуточное ratio за произвольный диапазон можно только
усреднить, а `AVG(ratio)` — другая величина (замер §0: 15,77 % против 15,37 %).
Предвычисленные окна вдобавок конкурировали бы с пользовательским Date Range.

---

## 5. Ratio-of-sums pairs — закрытый список

Считаются **в Looker** как `SUM(numerator)/SUM(denominator)` из поименованных колонок выше.
Ничего, кроме деления двух сумм, интерфейс не делает (поправка к §3.3, ACK-3).

| метрика | числитель | знаменатель |
|---|---|---|
| CTR | `clicks` | `views` |
| CPC | `ad_spend_attributed_rub` | `clicks` |
| CPM ×1000 | `ad_spend_attributed_rub` | `views` |
| CPO (attributed) | `ad_spend_attributed_rub` | `ad_orders_raw` |
| Blended CPO | `ad_spend_attributed_rub` | `orders_qty` |
| **ДРР по выкупам** | `ad_spend_attributed_rub` | `sales_revenue_seller_base_rub` |
| ДРР по заказам | `ad_spend_attributed_rub` | `orders_revenue_rub` |
| ROAS (attributed) | `ads_revenue_raw_rub` | `ad_spend_attributed_rub` |
| ACOS (attributed) | `ad_spend_attributed_rub` | `ads_revenue_raw_rub` |
| Средняя цена заказа | `orders_revenue_rub` | `orders_qty` |
| Средняя цена выкупа | `sales_revenue_seller_base_rub` | `buyouts_qty` |
| **Вклад, %** | `contribution_pre_cogs_rub` | `sales_revenue_seller_base_rub` |
| Доля отмен | `canceled_qty` | `orders_qty` |
| Доля возвратов | `returns_qty` | `buyouts_qty` |
| Сбор WB, % | `marketplace_fee_rub` | `sales_revenue_seller_base_rub` |
| Логистика на выкуп | `logistics_rub` | `buyouts_qty` |
| **`buyout_ratio_calendar_ops`** | `buyouts_qty` | `orders_qty` |

🔴 Последняя — **operational**, не `buyout_rate`. Настоящий когортный — в PR3.
Имя обязано содержать `_calendar_ops`, чтобы подмены не случилось.

Проверочные значения на покрытом окне (13.04 … 18.08): вклад % = **38,52 %**, ДРР по выкупам = **15,37 %**.

---

## 6. Coverage dependencies

Обе дата-вью **обязаны** join'иться к `V_DASH_COVERAGE_DAILY` по `day` — это и есть
наследование PR1. Гейт D-COV проверяет, что join не потерял и не размножил ни одной строки.

| метрика PR2 | зависит от флага PR1 |
|---|---|
| `orders_qty`, `orders_revenue_rub`, `canceled_*` | `orders_covered` |
| `buyouts_*`, `sales_revenue_seller_base_rub`, `returns_*` | `sales_covered` |
| `ad_spend_attributed_rub`, `views`, `clicks`, `ad_orders_raw`, `ads_revenue_raw_rub` | `ads_covered` |
| `marketplace_fee_rub`, `wb_reward_rub`, `logistics_rub`, `net_settlement_rub`, все расходы уровня счёта, `sales_revenue_settled_rub` | `finance_covered` |
| `contribution_pre_cogs_rub`, `settlement_contribution_pre_cogs_rub` | **`contribution_covered`** |

`contribution_covered` из PR1 = `sales_covered AND ads_covered AND (финансовые строки за сутки есть)`.
Ни один флаг в PR2 **не пересчитывается** — только читается.

---

## 7. NULL propagation

```
N1  Флаг покрытия FALSE ⇒ соответствующие метрики = NULL. Никогда 0.
N2  Внутри покрытия 0 остаётся 0 — это утверждение «событий не было».
N3  contribution_pre_cogs_rub = NULL, если NOT contribution_covered.
    Всё производное (вклад %, вклад на единицу) наследует NULL автоматически,
    потому что ratio считается из сумм, а SUM(NULL) не даёт нуля.
N4  Ни одного IFNULL(...,0) и ни одного COALESCE(...,0) для метрик.
    COALESCE допустим только для булевых флагов покрытия — и только в PR1, где он уже есть.
N5  Сутки календаря без строк витрины остаются в выдаче со всеми метриками NULL —
    исчезновение суток скрыло бы отсутствие покрытия.
N6  Дименсии (product_name_short и т.п.) при отсутствии в справочнике не подменяются:
    NULL остаётся NULL, а ref_rows_for_nm_id показывает, сколько строк справочника нашлось.
```

🔴 Именно N1+N3 превращают −7 777 098,96 ₽ в 1 312 034,95 ₽ (§0). Проверяется гейтом D7.

---

## 8. Freshness semantics — `V_DASH_FRESHNESS_BY_CONTRACT`

* **grain:** `contract_code` — одна строка на контракт экрана.
* **Карта «контракт → слои» живёт CTE-константой внутри вью**, попадает в `view_definition`
  и проверяется K9. Никакой фильтрации в Looker — интерфейс читает готовую строку.
* **columns:** `contract_code`, `layers_used_list`, `layers_used_count`,
  `data_as_of_min_used`, `data_as_of_max_used`, `worst_status_used`,
  `layers_used_without_date_count`, `layers_used_without_date_list`,
  `layers_without_sla_used_list`, `header_text_used`, `generated_at`.
* **Правило дат:** слои без `data_as_of` (`ref_sku_master`, `sku_orphans` — измерения и QC)
  в `data_as_of_min_used` **не участвуют**, но обязаны быть названы в
  `layers_used_without_date_list`. Молча исчезнуть не может ничто.
* **Правило статуса:** та же нормализация `IFNULL(status,'UNKNOWN')` и тот же приоритет
  `OK < STALE < ERROR < прочее`, что в PR1. Статус не пересчитывается.
* **Отношение к системному минимуму:** `V_DASH_FRESHNESS_HEADER` остаётся **как есть**
  и продолжает отдавать worst-case по всему контуру. Два значения имеют разный смысл
  и оба выводятся на экран рядом.

Карта на PR2 и что она даёт на якоре:

| contract_code | слои | `data_as_of_min_used` |
|---|---|---|
| `KPI_DAILY` | `mart_sku_daily`, `finance`, `orders`, `sales`, `fact_ads_sku_daily` | **2026-08-18** |
| `SKU_DAILY` | `mart_sku_daily`, `orders`, `sales`, `fact_ads_sku_daily`, `finance`, `ref_sku_master`* | **2026-08-18** |

\* `ref_sku_master` — без даты, идёт в `layers_used_without_date_list`.

Системный минимум сейчас — **2026-08-17**, и его задаёт `ads_costs` (рекламный биллинг,
Stage 3B заблокирован, на экран не выводится). Разница ровно одни сутки, но природа её
именно та, о которой вы написали: слой, не участвующий в KPI, тянул заголовок на себя.

🔴 Свежесть **не является** покрытием. `data_as_of_min_used` отвечает «насколько свежи
данные контракта», `V_DASH_COVERAGE_DAILY` — «за какие сутки они вообще есть».
Смешивать нельзя, обе величины выводятся раздельно.

---

## 9. Что доступно за произвольный Date Range

Доступно сразу после PR2, без изменения SQL и без зашитых окон:

* все аддитивные метрики §4 — итог периода;
* все ratio §5 — пересчитываются как отношение сумм выбранного диапазона;
* среднее в день — `SUM(x) / SUM(days_total)`, причём знаменатель берётся
  из счётчиков PR1, а не из `COUNT(*)`: делить надо на **покрытые** сутки;
* сравнение с предыдущим периодом той же длины — штатная функция Looker Studio,
  SQL не меняется;
* честность сравнения — через `SUM(*_uncovered_days)` и `SUM(contribution_provisional_days)`
  по обоим периодам: если у сравниваемого окна счётчик ненулевой, дельта помечается недостоверной.

Недоступно принципиально и в PR2 не появится: остаток на конец периода (snapshot, PR5),
когортный buyout (PR3), разрезы по кампаниям и запросам (PR4), прибыль и маржа (нет `REF_COGS`).

---

## 10. Acceptance gates (проект)

| # | проверка | ожидание на якоре 20.08 |
|---|---|---|
| D1 | строк в `V_DASH_KPI_DAILY` = строк в `V_DASH_COVERAGE_DAILY` | 715 = 715, дублей `day` нет |
| D2 | строк в `V_DASH_SKU_DAILY` = строк в `MART_SKU_DAILY` | 7 302 = 7 302, ключ `(day, nm_id)` уникален |
| D3 | fan-out справочника отсутствует | `MAX(ref_rows_for_nm_id) = 1`; SKU без справочника = 0 |
| D4 | SKU-часть KPI = агрегат SKU-вью по суткам | delta 0,00 по каждой аддитивной колонке |
| D5 | `marketplace_fee_rub` и `net_settlement_rub` = `FACT_FINANCE` (matched) | 7 954 656,56 и 18 870 292,80, delta 0,00 |
| D6 | **сумма частей = целое по финансам** — SKU-часть + часть уровня счёта + не-matched остаток = `SUM` по `V_WB_FINANCE_AMOUNTS_LONG_MAPPED` | delta 0,00; остаток не-matched предъявляется числом (**484,76 ₽** по сбору) |
| D7 | 🔴 **контрибуция наследует покрытие** | `SUM(contribution_pre_cogs_rub)` = **1 312 034,95 ₽** на 128 покрытых сутках; на непокрытых — строго `NULL`, не −9 089 133,91 ₽ |
| D8 | ни одного `0` вместо `NULL` вне покрытия | по каждой метрике: строк с `NOT covered AND metric IS NOT NULL` = 0 |
| D9 | ни одного `IFNULL(...,0)` / `COALESCE(...,0)` для метрик | статически по `view_definition`, как C18 |
| D10 | ни одной ratio-колонки в вью | статически: имён из закрытого списка §5 в схеме нет |
| D11 | `AVG(ratio) ≠ ratio-of-sums` предъявлено | 15,77 % против **15,37 %** — гейт фиксирует расхождение, а не прячет его |
| D12 | PROVISIONAL виден | `SUM(contribution_provisional_days)` за 13–19.08 = **2**, за 13.04–12.08 = **0** |
| D13 | `V_DASH_FRESHNESS_BY_CONTRACT` — 2 строки, обе с непустым списком слоёв | `KPI_DAILY` и `SKU_DAILY`, `data_as_of_min_used` = **2026-08-18** |
| D14 | слои без даты названы, а не пропущены | `ref_sku_master` в `layers_used_without_date_list` |
| D15 | системный минимум не изменился | `V_DASH_FRESHNESS_HEADER.data_as_of_min` = **2026-08-17**, объект не тронут |
| D16 | baseline «до = после» | `MART_SKU_DAILY` 7 302 / 523 365,38 ₽ · `V_ADS_SCREEN_SKU` 14 · `V_ADS_SCREEN_QUERY` 1 821 · `FACT_ADS_COSTS_DAILY` 1 816 / 514 064,00 ₽ · `V_DATA_FRESHNESS` 13 строк · оба объекта PR1 без изменений |
| D17 | K9 файл ↔ BigQuery | PASS ×3 (терминатор `;` снимается **после** нормализации — правило из PR1) |
| D18 | Stage 3B.1 не задет | в `view_definition` всех трёх объектов нет ни `V_ADV_COSTS`, ни `billed`, ни `FACT_ADS_COSTS_DAILY` |

---

## 11. Rollback

```sql
DROP VIEW `wb_mart.V_DASH_KPI_DAILY`;
DROP VIEW `wb_mart.V_DASH_SKU_DAILY`;
DROP VIEW `wb_mart.V_DASH_FRESHNESS_BY_CONTRACT`;
```

Плюс `git revert` коммита PR2. Объекты PR1 и все существующие витрины не изменяются,
откатывать в них нечего. Риск для production — нулевой: три новых `VIEW`, потребителей нет.

---

## 12. Что намеренно остаётся за пределами PR2

1. **Когортный `buyout_rate`** — PR3. В PR2 есть только `buyout_ratio_calendar_ops` под своим именем.
2. **Реклама по кампаниям и запросам** — PR4. `V_ADS_SCREEN_SKU` / `V_ADS_SCREEN_QUERY`
   **не дублируются и не переопределяются**: у них свой universe (рекламные SKU, фиксированное
   окно 28/90 суток) и своя приёмочная история S1–S12 + K9. PR2 их не читает вовсе.
3. **Остатки и поставки** — PR5. Snapshot-семантика в PR2 отсутствует полностью.
4. **Прибыль и маржа** — нет `REF_COGS`. Все имена сохраняют `_pre_cogs`; слова «прибыль»
   и «маржа» в контракте и на экране не употребляются.
5. **Биллинговый расход рекламы и всё из Stage 3B/3B.1** — не читается ни одной ссылкой,
   bootstrap не запускается, `V_ADV_COSTS_DAY_COVERAGE` в PR2 не упоминается.
6. **Хранение, удержания, приёмка по SKU** — источник даёт их только уровнем счёта
   (`is_sku_row = FALSE`). Разносить по SKU не будем ни в вью, ни в Looker.
7. **Правки `MART_SKU_DAILY`** — витрина не трогается. Вся правка семантики живёт в `V_DASH_*`.
8. **Сам UI** — экран собирается только после приёмки PR2.

---

## 13. Проверка по вашему списку

| требование | как выполнено |
|---|---|
| никаких `IFNULL(...,0)` для отсутствующего покрытия | правило N4 + статический гейт **D9** |
| никакого `AVG()` для ratio | ratio-колонок в вью нет (**D10**), закрытый список §5, расхождение предъявлено (**D11**) |
| contribution наследует coverage из PR1 | §6 + правило N3 + гейт **D7** (1 312 034,95 против −7 777 098,96) |
| PROVISIONAL остаётся видимым | `finance_is_final` и `contribution_provisional_days` протянуты из PR1, гейт **D12** |
| не дублировать `V_ADS_SCREEN_*` | §12 п. 2, PR2 их не читает; гейт **D16** доказывает неизменность |
| не трогать Stage 3B.1 | §12 п. 5, статический гейт **D18** |
| не строить profit/margin до `REF_COGS` | §12 п. 4, суффикс `_pre_cogs` во всех именах |

---

## 14. Первый реальный экран Looker Studio после PR2

**Экран «Executive · Пульс магазина»** — один экран, один произвольный Date Range,
встроенное сравнение с предыдущим периодом.

**Источники — ровно четыре, все готовые:**

| блок экрана | источник |
|---|---|
| KPI-плитки, графики по суткам | `wb_mart.V_DASH_KPI_DAILY` |
| таблица SKU + drill-down `Business → SKU → Day` | `wb_mart.V_DASH_SKU_DAILY` |
| шапка «Данные актуальны на …» по этому экрану | `wb_mart.V_DASH_FRESHNESS_BY_CONTRACT` (`contract_code = 'KPI_DAILY'`) |
| системный светофор, раскрывающаяся плашка | `wb_mart.V_DASH_FRESHNESS_HEADER` + `wb_mart.V_DATA_FRESHNESS` |

Что на нём будет работать в день сборки:

* **KPI-строка:** выручка по выкупам, заказы, выкупы, доля выкупа (календарная, подписанная),
  сбор WB, логистика, хранение, удержания, реклама (атрибуция), вклад pre-COGS, ДРР, ROAS;
* **графики:** выручка и вклад по суткам, реклама и ДРР по суткам;
* **таблица SKU:** сгруппированная по Sales / Buyouts / Economics / Advertising / Efficiency,
  сортируемая, с drill-down до суток;
* **честность:** бейдж «в выбранном периоде N суток без покрытия рекламы / заказов»
  и «period contains provisional finance» — оба из счётчиков PR1;
* **по умолчанию:** диапазон с **13.04.2026**; глубже выбрать можно, но бейдж загорится.

Чего на нём не будет и почему — подписано прямо на экране: остаток и дней остатка (PR5),
когортный выкуп (PR3), разрезы по кампаниям и запросам (PR4), прибыль и маржа (`REF_COGS`),
биллинговый расход рекламы (Stage 3B.1).

**Это и есть первый рабочий online-дашборд на production-данных** — цель Stage 4,
достигнутая без единой правки витрины и без ожидания Stage 3B.

---

## 15. Что нужно от вас перед написанием SQL

| # | вопрос | рекомендация |
|---|---|---|
| **OPEN-2** | вариант **A** (новая вью `V_DASH_FRESHNESS_BY_CONTRACT`) или **B** (колонки в шапку PR1) | **A** — не трогает принятый объект, масштабируется строками |
| **OPEN-3** | `sales_revenue_settled_rub`: брать из `MART_SKU_DAILY` как `fee + for_pay` (один источник, но 11 строк «Возврат» на 6 940 ₽ = 0,026 % **прибавляются** вместо вычитания — дефект унаследован от витрины) или считать из `FACT_FINANCE` со знаком по `supplier_oper_name` | **из `FACT_FINANCE` со знаком** — корректность важнее числа источников; расхождение с витриной предъявляется гейтом, а не замалчивается |
| **OPEN-4** | «Коррекция продаж» — 8 строк, `fee_gap` пуст, `for_pay` 1 834,88 ₽, базы выручки нет | вынести отдельной колонкой `sales_correction_settlement_rub`, в `sales_revenue_settled_rub` **не включать** и подписать |

**SQL не написан. PR2 не начат.** Жду ACK по scope и по OPEN-2 / OPEN-3 / OPEN-4.

---

### Операционная заметка

Во время этой проверки BigQuery-коннектор устойчиво отдавал 502 на запросах к
`V_WB_FINANCE_AMOUNTS_LONG_MAPPED` (тяжёлый unpivot, ~47 МБ и 13–20 с). Лёгкие запросы
проходили штатно. Один замер — разбивка `is_sku_row` по категориям `logistics`,
`wb_reward` и возмещениям — **снять не удалось** и он не подставлен по памяти.
На дизайн это не влияет: часть уровня счёта определяется **предикатом** `NOT is_sku_row`,
а не перечислением категорий, а гейт **D6** («сумма частей = целое») поймает любую
категорию, которая окажется не там, где ожидалось.
