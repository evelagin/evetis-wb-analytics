# STAGE 2 — SKU PERFORMANCE

Дата: 2026-08-27.  Базовый HEAD: `4bcfa5b` (Stage 1.10B).
Статус: **ЗАВЕРШЁН И ПРИНЯТ ВЛАДЕЛЬЦЕМ** (server acceptance + visual acceptance, 2026-08-27).
Документ отражает итоговое состояние после Stage 2, **Stage 2.0.1** (Semantics & UX Correction, §4.1–4.4),
правок по первому кругу визуального QA (§4.5) и **Stage 2.0.2** (Loading QA & Visual Density, §4.6).
Предшественники: Stage 4A PR1/PR2, Stage 1.9, Stage 1.10A/1.10B.

## 1. Что это

Второй управленческий экран: **EVETIS · WB SKU Performance** (dashboard 3, коллекция 7 «02 SKU Performance»).
Отвечает на вопрос «какой товар требует моего решения прямо сейчас»: спрос → отмены → фактическая реализация →
выручка → стоимость поддержки → результат до себестоимости → тренд к предыдущему периоду → проблемные SKU.

## 2. Границы изменения

**BigQuery не изменялся ни одним объектом** ни в Stage 2, ни в Stage 2.0.1.
`sql/dash/dashboard_contract_v2.sql` не правился: всё необходимое уже есть в `wb_mart.V_DASH_SKU_DAILY`
(60 колонок, грейн `day × nm_id`).

Не затронуты: RAW, `FACT_*`, `MART_SKU_DAILY`, `sp_bootstrap_facts`, `sp_build_mart_sku_daily`,
`REF_COST_MAP`, знаковая семантика Stage 1.5, Stage 1.9 `period_result`, Stage 3B, `V_ADV_COSTS`,
deferred-код #116, COGS, налоговая модель. Executive (dashboard 2, cards 40–55) не изменён ни в одном поле;
cards 40 и 41 переиспользованы как dashcards — сами карточки не менялись.

## 3. Зафиксированные семантические правила экрана

### 3.1. Четыре независимые базы дат

| Величина | База даты |
| --- | --- |
| `orders_gross_qty`, `canceled_qty` | дата заказа |
| `buyouts_qty`, `sales_revenue_seller_base_rub`, `returns_qty` | дата выкупа / возврата |
| `ad_spend_attributed_rub` | дата рекламной активности |
| `marketplace_fee_rub`, `logistics_rub`, `net_settlement_rub` | дата финансовой операции |

🔴 **Заказы и выкупы никогда не показываются как одна воронка и не ставятся в отношение.**
Замер 01.08–22.08: WB Funnel даёт 410 когортных выкупов против 443 событийных; поартикульно событийные
выкупы превышают брутто-заказы («Крем УВЛ» 28 против 22, «Набор тоник+сыворотка АКНЕ» 6 против 5).
Отношение даёт 127 % и 120 % — формула ломается арифметически, а не только методологически.
Корректная когортная конверсия требует `order_date` в `FACT_SALES`; его нет.

### 3.2. Остальные правила

1. **Доля отмен разрешена**: `SUM(canceled_qty) / NULLIF(SUM(orders_gross_qty), 0)` — обе величины
   в базе даты заказа. `AVG` посуточных отношений не применяется.
2. **Все ratio — только ratio-of-sums** за период.
3. **Расходы уровня счёта по SKU не разносятся** — источник не даёт связки; они остаются на Executive.
   Поэтому SKU-результат — это `contribution_pre_cogs`, а не `period_result`.
4. **Нет покрытия ≠ ноль.** `IFNULL(...,0)` не применяется ни к одной метрике.
5. **Реклама — attributed spend, не биллинг.** Stage 3B Phase B не выполнен. Замер 01.08–22.08:
   атрибуция 33 534,38 ₽ против биллинга 33 536,00 ₽ — расхождение 1,62 ₽ (0,005 %).
6. **Себестоимости нет.** `REF_COGS` отсутствует в production и в репозитории. Слова «прибыль», «маржа»,
   «EBITDA», «ROI», «unit economics» на экране не используются; в описаниях денежных показателей
   явно повторено: «Себестоимость товара не вычитается. Показатель не является прибылью.»
7. **Именование.** «Доход до себестоимости, ₽» и «Доходность до себестоимости, %» — те же названия,
   что у card 45 и колонок card 52. «Результат до себестоимости» не используется: на Executive это
   card 53 с другой формулой (`contribution − account_level_total`). Одна формула — одно имя.

## 4. Stage 2.0.1 — коррекция семантики и UX

Три дефекта, найденные визуальным и семантическим аудитом уже собранного экрана.

### 4.1. Смешанная база дат на графике динамики

**Было.** Один график «Заказы и выкупы по дням» с двумя линиями: `orders_gross_qty` по дате заказа
и `buyouts_qty` по дате выкупа. Формулы конверсии в нём не было, но сама форма подачи —
две линии в одних осях — читается как воронка и противоречит правилу §3.1.

**Стало.** График разделён на два, каждый в одной базе дат:

* card 65 «Заказы и отмены по дням» — `orders_gross_qty` + `canceled_qty`, обе по дате заказа;
* card 70 «Выкупы по дням» — только `buyouts_qty`, по дате выкупа.

База дат указана в описании каждого графика, в подписи оси X и в заголовке dashcard.

### 4.2. Выбранный период не равен фактически доступному

**Было.** Владелец выбирает 30 суток, но экономический слой может покрывать 29. Переиспользованная
карточка «Качество выбранного периода» показывает покрытие уровня счёта и SKU-слой не описывает.

**Стало.** Новая card 71 «Период и покрытие данных SKU» в самом верху экрана. Показывает
выбранный период, фактические сутки SKU-слоя и покрытие по пяти источникам, помечая знаком ⚠
любой слой, покрывающий не весь диапазон. Замер на `past30days` (27.08.2026):

```
Выбранный период          28.07.2026 — 26.08.2026  (30 сут)
Данные SKU фактически по  28.07.2026 — 26.08.2026  (30 сут)
Заказы 30/30   Продажи 30/30   Реклама 30/30   Финансы 29/30 ⚠   Экономика 29/30 ⚠
```

Край диапазона (период по сегодня) проверен отдельно: 31 выбранные сутки против 30 фактических,
⚠ на всех пяти слоях.

### 4.3. Сравнение периодов без доказанной сопоставимости

**Было.** Δ и Δ% выводились для всех девяти строк независимо от того, одинаково ли покрыты окна.
29 фактически покрытых суток против 30 давали «−14,1 %» как чистую бизнес-динамику.

**Стало.** Для каждой метрики отдельно считается число фактически покрытых суток в каждом окне
по её собственному гейту. Добавлены колонки «База дат» и «Покрытие тек./пред.».
**Если покрытие окон не совпадает, Δ и Δ% не выводятся — вместо них «н/с».**
Скрытого приведения 29 суток к 30 не выполняется ни в каком виде.

| Метрика | Гейт покрытия |
| --- | --- |
| Заказы, Отменено, Доля отмен | `orders_covered` |
| Выкупы, Выручка | `sales_covered` |
| Реклама | `ads_covered` |
| ДРР | `ads_covered AND sales_covered` |
| Доход и доходность до себестоимости | `contribution_covered` |

Замер на `past30days`: первые семь строк 30/30 сут — Δ выводится; две экономические строки
29/30 сут — помечены «не сопоставимо», Δ и Δ% скрыты.

### 4.5. Правки по результатам визуального QA (первый круг скриншотов)

Скриншоты владельца выявили три дефекта отрисовки — данные и формулы при этом были верны.

* **Карточки 71 и 67 обрезались.** При `size_y = 2` Metabase показывал только шапку таблицы,
  строка значений уходила под нижнюю границу. Высота увеличена до 4. Минимальная рабочая высота
  таблицы в один ряд — 3 (подтверждено картой 41), для многоколоночных шапок берём 4.
* **Карточка 68 показывала 6 строк из 9.** При `size_y = 7` две экономические строки — ровно те,
  ради которых Stage 2.0.1 и делался, с пометкой «не сопоставимо» — оказывались ниже сгиба.
  Высота увеличена до 11: видны все девять строк без прокрутки.
* **Рейтинг и «Требуют внимания» обрезались по ширине.** При `width: fixed` полезная ширина ~1280 px,
  и 11 колонок рейтинга не помещались: колонка «Доход до себестоимости», по которой таблица
  отсортирована, уходила за правый край — владелец видел рейтинг, не видя основания сортировки.
  Дашборду выставлено `width: full`. Колонки не сокращались: их состав задан §6 и §7 задания.
  Executive (dashboard 2) остаётся `fixed` и не затронут.

Строки раскладки после изменения высот пересчитаны заново — наложений и разрывов нет,
последняя строка 58, минимум одна карточка в каждом ряду доходит до колонки 24.

### 4.4. Прочие правки Stage 2.0.1

* **Рейтинг (card 64).** Подтверждена сортировка `SUM(contribution_pre_cogs_rub) DESC`.
  Ratio переведены на `NULLIF(...)` в знаменателе. Скрыты строки SKU, у которых за период
  одновременно **известны и равны нулю** заказы, выкупы, выручка и реклама (22 строки вместо 25
  на окне A). Неизвестное (NULL) не скрывается никогда. При явном выборе такого SKU фильтром
  таблица пуста — ложной строки нулей не создаётся.
* **«Требуют внимания» (card 69).** Правила приведены к трём жёстким объективным условиям
  (см. §7). Несколько причин для одного SKU дают **одну** строку с перечислением через «; » —
  денежные суммы не размножаются. Сортировка: отрицательный доход по возрастанию, затем реклама по убыванию.
* **card 67** упрощена до календарных границ двух окон; покрытие ушло в card 71 и в колонку card 68.
* **Описания** всех денежных и количественных карточек дополнены явной базой дат и оговоркой о себестоимости.
* **Фильтр «Товар»** получил явное `isMultiSelect: true`.

### 4.6. Stage 2.0.2 — диагностика загрузки и вертикальная плотность

**Карточка «Отменено, шт» — дефекта нет.** На скриншотах она показывала серый skeleton вместо значения.
Серверная проверка: 5 запусков подряд с параметрами дашборда — значение `52` во всех пяти, латентность
3,8–6,2 с, ошибок нет. Структурно карточка 57 совпадает с соседними 56 и 58 во всём, кроме имени
агрегируемой колонки: тот же `display: scalar`, та же база 2, те же два field-фильтра (983 и 986),
тот же `scalar.field`, тот же `column_settings`, `cache_ttl` не задан ни у одной, `result_metadata` есть.

Причина skeleton — общая латентность дашборда, а не конкретная карточка. При одновременном запуске
всех восьми KPI (как делает дашборд) разброс времени внутри круга ~1,4 с, и **самая медленная карточка
меняется от круга к кругу**: в первом круге последней финишировала 57 (7,20 с), во втором — 56 (5,66 с).
Отдельно зафиксирован таймаут 30 с на «холодном» запуске карточки 56 — той самой, которая на скриншоте
отрисовалась нормально. Skeleton видит та карточка, которая на момент снимка ещё не вернула результат;
к «Отменено» это отношения не имеет. **Изменений не вносилось.** Кэш результатов сознательно не включался:
это изменение поведения, а не исправление дефекта.

**Вертикальная плотность.** Минимальные высоты сняты эмпирически с двух кругов скриншотов:

| Карточка | Было | Стало | Основание |
| --- | --- | --- | --- |
| 71 Период и покрытие | 4 | **3** | таблица в 1 строку: `h=2` обрезается (круг 1), `h=3` отрисовывается полностью |
| 67 Сравниваемые периоды | 4 | **3** | то же |
| 69 Требуют внимания | 6 | **4** | 1–2 строки типового результата укладываются в 4 |
| 40 Свежесть данных | 3 | 3 | уже минимум для таблицы в 1 строку |
| 41 Качество периода | 3 | 3 | уже минимум |
| 68 Изменение к периоду | 11 | 11 | `h=7` → видно 6 строк из 9, `h=11` → все 9. Линейно строк = (h+1)/1{,}33, для 9 строк ровно 11. Уменьшение вернёт обрезку |

`h=2` для таблицы в одну строку исключён: в круге 1 при нём обрезались и карточка 71 (7 колонок),
и карточка 67 (2 короткие колонки) — то есть дело не в переносе заголовков, а в самой высоте.

Высота дашборда 58 → 54 строки сетки. Строки всех нижележащих карточек пересчитаны:
перекрытий 0, пустых строк сетки 0. Рейтинг (`h=9`, 22 строки) и графики (`h=6`) не менялись:
таблица на 22 строки прокручивается внутри себя по построению.

## 5. Восстановление границ периода

Field-фильтр Metabase непрозрачен — выбранные даты в SQL недоступны. Границы восстанавливаются из
**плотного календаря** `V_DASH_KPI_DAILY` (2024-09-05 … сегодня, без разрывов):

```sql
WITH b AS (SELECT MIN(day) AS d1, MAX(day) AS d2, COUNT(*) AS len
           FROM `wb_mart.V_DASH_KPI_DAILY` WHERE {{day}})
```

Границы **нельзя** восстанавливать из `V_DASH_SKU_DAILY`: её последние сутки отстают от календаря,
и длина окна молча уменьшилась бы.

🔴 **Ограничение Metabase, найденное экспериментально.** Field-фильтр разворачивается в
**полностью квалифицированное** имя колонки (`` `wb_mart.V_DASH_SKU_DAILY`.product_name_short ``).
При алиасе таблицы (`FROM ... AS s`) запрос падает с `Unrecognized name`. Поэтому во всех карточках
витрина указывается **без алиаса**, а границы прокидываются скалярными подзапросами.

🔴 **Ловушка алиасов в `HAVING`.** BigQuery резолвит `SUM(orders_gross_qty)` в `HAVING` на
SELECT-алиас `orders_gross_qty`, который сам уже агрегат, и падает с `Aggregations of aggregations
are not allowed`. Поэтому фильтр неактивных SKU в card 64 вынесен в `WHERE` поверх CTE `per`.

Карточка сравнения сканирует витрину **один раз** (разметка окон через `IF(day >= d1,'cur','prv')`).
Вариант со скалярными подзапросами на метрику давал 51 с и предупреждение BigQuery
`Number of stages in query is high`; текущий — 6 с.

## 6. Фильтр товара

Параметр `sku` (`string/=`, `isMultiSelect: true`) привязан к `V_DASH_SKU_DAILY.product_name_short`
(Metabase field 986). Технический ключ — `nm_id`; все карточки группируют по `nm_id` и выводят его
колонкой «Артикул WB».

Почему не `nm_id` в самом фильтре: field-фильтр показывает сырые значения колонки, то есть голые числа —
для владельца непригодно, а remapping `nm_id → имя` требует FK на таблицу-справочник, которой в `wb_mart` нет.
Аудит подтвердил строгую биекцию: 25 `nm_id` ↔ 25 имён, коллизий 0 в обе стороны, орфанов 0.
При будущей коллизии имён рейтинг покажет **две строки** с разными `nm_id`, а не сольёт их молча.

Проверено, что мультивыбор агрегируется как **SUM по выбранным `nm_id`**, а не AVG:
два SKU — заказы 12 + 176 = 188, выручка 4 318,00 + 62 218,28 = 66 536,28, доход −675,76 + 15 119,10 = 14 443,34;
три SKU — заказы 225.

## 7. Блок «Требуют внимания»

Только жёсткие объективные условия, **порогов не содержит**:

1. `contribution_pre_cogs_rub < 0 AND revenue > 0` → «Отрицательный доход до себестоимости»;
2. `ad_spend > 0 AND revenue = 0` → «Есть рекламные расходы без выручки»;
3. `orders_gross > 0 AND canceled_qty = orders_gross` → «Все заказы периода отменены».

Отношение выкупов к заказам не используется. `NULL` (нет покрытия) ни под одно условие не подпадает.
Пороговые правила («высокий ДРР», «много отмен») владельцем не заданы и **сознательно не придуманы**.
Замеренное распределение за 01.07–25.08 (25 SKU) для будущего решения: ДРР бренд 18,39 %,
p50 16,1 % / p75 27,8 % / p90 46,9 % / max 67,7 %; доля отмен бренд 10,22 %, p50 11,1 % / p75 19,0 % / p90 22,9 %.

## 8. Раскладка dashboard 3 (сетка 24 колонки)

```
Ширина дашборда: full (не fixed) — 11 колонок рейтинга не помещаются в фиксированные ~1280 px.
Фильтры: [Период — date/all-options, default past30days]  [Товар — string/=, isMultiSelect]

row  0  24×1  ── Актуальность и покрытие данных
row  1  24×3  card 71  Период и покрытие данных SKU          [date]
row  4  12×3  card 40  Свежесть данных                       (переиспользована, без фильтров)
row  4  12×3  card 41  Качество выбранного периода           [date]
row  7  24×1  ── Ключевые показатели за период
row  8   8×3  card 56  Заказы (брутто), шт · дата заказа
row  8   8×3  card 57  Отменено, шт · дата заказа
row  8   8×3  card 58  Выкупы, шт · дата выкупа
row 11   8×3  card 59  Выручка, ₽ · дата выкупа
row 11   8×3  card 60  Реклама, ₽ · attributed
row 11   8×3  card 61  ДРР, %
row 14  12×3  card 62  Доход до себестоимости, ₽
row 14  12×3  card 63  Доходность до себестоимости, %
row 17  24×1  ── Сравнение с предыдущим сопоставимым периодом
row 18  24×3  card 67  Сравниваемые периоды                  [date]
row 21  24×11 card 68  Изменение к предыдущему периоду
row 32  24×1  ── Товары за период
row 33  24×9  card 64  Рейтинг товаров (по доходу до себестоимости)
row 42  24×1  ── Динамика
row 43   8×6  card 65  Заказы и отмены по дням · дата заказа
row 43   8×6  card 70  Выкупы по дням · дата выкупа
row 43   8×6  card 66  Выручка и доход до себестоимости по дням
row 49  24×1  ── Требуют внимания
row 50  24×4  card 69  Требуют внимания

Итоговая высота 54 строки сетки. Перекрытий 0, пустых строк сетки 0.
```

К фильтру `sku` привязаны карточки 56–66, 68–70. Карточки 40, 41, 67, 71 ему не подчиняются:
свежесть и покрытие определяются посуточно на уровне источника, а границы периода от выбора товара не зависят.

## 9. SQL карточек (as deployed)

### card 71 — Период и покрытие данных SKU  ·  `table`

> Выбранный период против фактически доступных суток SKU-слоя и покрытия по каждому источнику. Границы берутся из плотного календаря V_DASH_KPI_DAILY (2024-09-05 … сегодня, без разрывов), фактические сутки — из V_DASH_SKU_DAILY. Знак ⚠ означает, что слой покрывает НЕ весь выбранный диапазон. Отсутствие данных нулём не подменяется. Карточка намеренно НЕ подчиняется фильтру «Товар»: покрытие определяется посуточно на уровне источника, а не на уровне SKU.

```sql
WITH b AS (
  SELECT
    MIN(day) AS d1, MAX(day) AS d2, COUNT(*) AS len,
    COUNTIF(orders_covered)       AS d_orders,
    COUNTIF(sales_covered)        AS d_sales,
    COUNTIF(ads_covered)          AS d_ads,
    COUNTIF(finance_covered)      AS d_finance,
    COUNTIF(contribution_covered) AS d_contrib
  FROM `wb_mart.V_DASH_KPI_DAILY`
  WHERE {{day}}
),
s AS (
  SELECT MIN(day) AS mn, MAX(day) AS mx, COUNT(DISTINCT day) AS d
  FROM `wb_mart.V_DASH_SKU_DAILY`
  WHERE day BETWEEN (SELECT d1 FROM b) AND (SELECT d2 FROM b)
)
SELECT
  CONCAT(FORMAT_DATE('%d.%m.%Y', b.d1), ' — ', FORMAT_DATE('%d.%m.%Y', b.d2),
         '  (', CAST(b.len AS STRING), ' сут)')                                   AS selected_period,
  IF(s.mn IS NULL, 'нет данных',
     CONCAT(FORMAT_DATE('%d.%m.%Y', s.mn), ' — ', FORMAT_DATE('%d.%m.%Y', s.mx),
            '  (', CAST(s.d AS STRING), ' сут)',
            IF(s.d = b.len, '', ' ⚠')))                                           AS sku_data_period,
  CONCAT(CAST(b.d_orders  AS STRING),'/',CAST(b.len AS STRING), IF(b.d_orders =b.len,'',' ⚠')) AS cov_orders,
  CONCAT(CAST(b.d_sales   AS STRING),'/',CAST(b.len AS STRING), IF(b.d_sales  =b.len,'',' ⚠')) AS cov_sales,
  CONCAT(CAST(b.d_ads     AS STRING),'/',CAST(b.len AS STRING), IF(b.d_ads    =b.len,'',' ⚠')) AS cov_ads,
  CONCAT(CAST(b.d_finance AS STRING),'/',CAST(b.len AS STRING), IF(b.d_finance=b.len,'',' ⚠')) AS cov_finance,
  CONCAT(CAST(b.d_contrib AS STRING),'/',CAST(b.len AS STRING), IF(b.d_contrib=b.len,'',' ⚠')) AS cov_economics
FROM b, s

```

### card 56 — Заказы, шт (SKU)  ·  `scalar`

> БАЗА ДАТ: дата заказа. «Заказы, шт» = gross placed orders — все оформленные заказы периода, включая впоследствии отменённые (orders_gross_qty = orders_qty + canceled_qty). Гейт orders_covered; вне покрытия NULL, не 0.

```sql
SELECT SUM(orders_gross_qty) AS orders_gross_qty
FROM `wb_mart.V_DASH_SKU_DAILY`
WHERE {{day}}
  [[AND {{sku}}]]
```

### card 57 — Отменено, шт (SKU)  ·  `scalar`

> БАЗА ДАТ: дата заказа. Отменённые заказы — часть брутто-заказов той же даты-базы. Гейт orders_covered; вне покрытия NULL, не 0.

```sql
SELECT SUM(canceled_qty) AS canceled_qty
FROM `wb_mart.V_DASH_SKU_DAILY`
WHERE {{day}}
  [[AND {{sku}}]]
```

### card 58 — Выкупы, шт (SKU)  ·  `scalar`

> БАЗА ДАТ: ДАТА ВЫКУПА (sale_date). Это СОБЫТИЕ периода, а не когорта по дате заказа. С «Заказами» в одну воронку не сводится и в отношение не ставится: 01.08–22.08 WB Funnel даёт 410 когортных выкупов против 443 событийных, а по отдельным SKU событийные выкупы превышают брутто-заказы. Гейт sales_covered.

```sql
SELECT SUM(buyouts_qty) AS buyouts_qty
FROM `wb_mart.V_DASH_SKU_DAILY`
WHERE {{day}}
  [[AND {{sku}}]]
```

### card 59 — Выручка, ₽ (SKU)  ·  `scalar`

> БАЗА ДАТ: дата выкупа. Выручка в seller-base. Гейт sales_covered; вне покрытия NULL, не 0.

```sql
SELECT SUM(sales_revenue_seller_base_rub) AS revenue_rub
FROM `wb_mart.V_DASH_SKU_DAILY`
WHERE {{day}}
  [[AND {{sku}}]]
```

### card 60 — Реклама, ₽ (SKU)  ·  `scalar`

> БАЗА ДАТ: дата рекламной активности. «Реклама, ₽» = ATTRIBUTED SPEND (расход, атрибутированный на SKU), а НЕ billing spend. Stage 3B Phase B не выполнен, биллинговой разбивки по SKU в production нет. Замер 01.08–22.08: атрибуция 33 534,38 ₽ против биллинга V_ADV_COSTS 33 536,00 ₽ — расхождение 1,62 ₽ (0,005 %). Гейт ads_covered.

```sql
SELECT SUM(ad_spend_attributed_rub) AS ad_spend_rub
FROM `wb_mart.V_DASH_SKU_DAILY`
WHERE {{day}}
  [[AND {{sku}}]]
```

### card 61 — ДРР, % (SKU)  ·  `scalar`

> Ratio-of-sums за период: SUM(ad_spend_attributed_rub) / SUM(sales_revenue_seller_base_rub). AVG посуточных ДРР не применяется. Числитель — attributed spend, не биллинг. Знаменатель — выручка по дате выкупа.

```sql
SELECT SAFE_DIVIDE(SUM(ad_spend_attributed_rub), SUM(sales_revenue_seller_base_rub)) AS drr
FROM `wb_mart.V_DASH_SKU_DAILY`
WHERE {{day}}
  [[AND {{sku}}]]
```

### card 62 — Доход до себестоимости, ₽ (SKU)  ·  `scalar`

> contribution_pre_cogs: выручка продавца за вычетом сбора маркетплейса, логистики SKU и атрибутированной рекламы. Расходы уровня счёта (хранение, удержания, приёмка) по SKU не разносятся — источник не даёт связки с SKU, они остаются на Executive. Себестоимость товара не вычитается. Показатель не является прибылью. REF_COGS в production отсутствует. Гейт contribution_covered.

```sql
SELECT SUM(contribution_pre_cogs_rub) AS contribution_rub
FROM `wb_mart.V_DASH_SKU_DAILY`
WHERE {{day}}
  [[AND {{sku}}]]
```

### card 63 — Доходность до себестоимости, % (SKU)  ·  `scalar`

> Ratio-of-sums за период: SUM(contribution_pre_cogs_rub) / SUM(sales_revenue_seller_base_rub). AVG посуточных отношений не применяется. Себестоимость товара не вычитается. Показатель не является прибылью. Это не маржа.

```sql
SELECT SAFE_DIVIDE(SUM(contribution_pre_cogs_rub), SUM(sales_revenue_seller_base_rub)) AS contribution_to_revenue
FROM `wb_mart.V_DASH_SKU_DAILY`
WHERE {{day}}
  [[AND {{sku}}]]
```

### card 67 — Сравниваемые периоды (SKU)  ·  `table`

> Календарные границы двух сравниваемых окон одинаковой длины. Фактическое покрытие каждого окна по метрикам показано в карточке «Сравнение с предыдущим периодом» колонкой «Покрытие тек./пред.», покрытие текущего периода по слоям — в карточке «Период и покрытие данных SKU». Карточка не подчиняется фильтру «Товар»: границы периода от выбора товара не зависят.

```sql
WITH b AS (
  SELECT MIN(day) AS d1, MAX(day) AS d2, COUNT(*) AS len
  FROM `wb_mart.V_DASH_KPI_DAILY`
  WHERE {{day}}
)
SELECT
  CONCAT(FORMAT_DATE('%d.%m.%Y', d1), ' — ', FORMAT_DATE('%d.%m.%Y', d2),
         '  (', CAST(len AS STRING), ' сут)')                                        AS cur_period,
  CONCAT(FORMAT_DATE('%d.%m.%Y', DATE_SUB(d1, INTERVAL len DAY)), ' — ',
         FORMAT_DATE('%d.%m.%Y', DATE_SUB(d1, INTERVAL 1 DAY)),
         '  (', CAST(len AS STRING), ' сут)')                                        AS prv_period
FROM b
```

### card 68 — Сравнение с предыдущим периодом (SKU)  ·  `table`

> Текущий период против предыдущего той же длины, непосредственно перед ним. Границы восстанавливаются из плотного календаря V_DASH_KPI_DAILY. У КАЖДОЙ МЕТРИКИ СВОЯ БАЗА ДАТ И СВОЁ ПОКРЫТИЕ — колонка «Покрытие тек./пред.» показывает число фактически покрытых суток в каждом окне. Если покрытие окон не совпадает, Δ и Δ% НЕ выводятся: вместо них «н/с» (не сопоставимо). Скрытого приведения 29 суток к 30 не выполняется ни в каком виде. Для ratio (доля отмен, ДРР, доходность) изменение считается как разность ratio-of-sums двух периодов и выражается в процентных пунктах; относительное Δ% для них не выводится как бессмысленное. Отношение выкупов к заказам не вычисляется.

```sql
WITH b AS (
  SELECT MIN(day) AS d1, MAX(day) AS d2, COUNT(*) AS len
  FROM `wb_mart.V_DASH_KPI_DAILY`
  WHERE {{day}}
),
-- Покрытие по слоям, отдельно для каждого окна. Считается по календарю KPI,
-- потому что он плотный: 2024-09-05 … сегодня без разрывов.
cov AS (
  SELECT
    IF(day >= (SELECT d1 FROM b), 'cur', 'prv') AS w,
    COUNTIF(orders_covered)                        AS d_orders,
    COUNTIF(sales_covered)                         AS d_sales,
    COUNTIF(ads_covered)                           AS d_ads,
    COUNTIF(ads_covered AND sales_covered)         AS d_adsrev,
    COUNTIF(contribution_covered)                  AS d_contrib,
    COUNT(*)                                       AS d_total
  FROM `wb_mart.V_DASH_KPI_DAILY`, b
  WHERE day BETWEEN DATE_SUB(b.d1, INTERVAL b.len DAY) AND b.d2
  GROUP BY w
),
cv AS (
  SELECT
    MAX(IF(w='cur',d_orders, NULL)) AS c_d_orders,  MAX(IF(w='prv',d_orders, NULL)) AS p_d_orders,
    MAX(IF(w='cur',d_sales,  NULL)) AS c_d_sales,   MAX(IF(w='prv',d_sales,  NULL)) AS p_d_sales,
    MAX(IF(w='cur',d_ads,    NULL)) AS c_d_ads,     MAX(IF(w='prv',d_ads,    NULL)) AS p_d_ads,
    MAX(IF(w='cur',d_adsrev, NULL)) AS c_d_adsrev,  MAX(IF(w='prv',d_adsrev, NULL)) AS p_d_adsrev,
    MAX(IF(w='cur',d_contrib,NULL)) AS c_d_contrib, MAX(IF(w='prv',d_contrib,NULL)) AS p_d_contrib
  FROM cov
),
win AS (
  SELECT
    IF(day >= (SELECT d1 FROM b), 'cur', 'prv') AS w,
    orders_gross_qty, canceled_qty, buyouts_qty,
    sales_revenue_seller_base_rub, ad_spend_attributed_rub, contribution_pre_cogs_rub
  FROM `wb_mart.V_DASH_SKU_DAILY`, b
  WHERE day BETWEEN DATE_SUB(b.d1, INTERVAL b.len DAY) AND b.d2
    [[AND {{sku}}]]
),
agg AS (
  SELECT w,
    SUM(orders_gross_qty) AS ord, SUM(canceled_qty) AS can, SUM(buyouts_qty) AS buy,
    SUM(sales_revenue_seller_base_rub) AS rev, SUM(ad_spend_attributed_rub) AS ads,
    SUM(contribution_pre_cogs_rub) AS con
  FROM win GROUP BY w
),
p AS (
  SELECT
    MAX(IF(w='cur',ord,NULL)) AS c_ord, MAX(IF(w='prv',ord,NULL)) AS p_ord,
    MAX(IF(w='cur',can,NULL)) AS c_can, MAX(IF(w='prv',can,NULL)) AS p_can,
    MAX(IF(w='cur',buy,NULL)) AS c_buy, MAX(IF(w='prv',buy,NULL)) AS p_buy,
    MAX(IF(w='cur',rev,NULL)) AS c_rev, MAX(IF(w='prv',rev,NULL)) AS p_rev,
    MAX(IF(w='cur',ads,NULL)) AS c_ads, MAX(IF(w='prv',ads,NULL)) AS p_ads,
    MAX(IF(w='cur',con,NULL)) AS c_con, MAX(IF(w='prv',con,NULL)) AS p_con
  FROM agg
),
m AS (
  SELECT ord AS sort_ord, metric, basis, unit, cv_, pv_, cd, pd
  FROM p, cv, UNNEST([
    STRUCT(1 AS ord, 'Заказы, шт' AS metric, 'дата заказа' AS basis, 'n' AS unit,
           CAST(c_ord AS NUMERIC) AS cv_, CAST(p_ord AS NUMERIC) AS pv_,
           c_d_orders AS cd, p_d_orders AS pd),
    (2,'Отменено, шт',              'дата заказа','n',   CAST(c_can AS NUMERIC), CAST(p_can AS NUMERIC), c_d_orders, p_d_orders),
    (3,'Доля отмен',                'дата заказа','pct', SAFE_DIVIDE(c_can,c_ord), SAFE_DIVIDE(p_can,p_ord), c_d_orders, p_d_orders),
    (4,'Выкупы, шт',                'дата выкупа','n',   CAST(c_buy AS NUMERIC), CAST(p_buy AS NUMERIC), c_d_sales, p_d_sales),
    (5,'Выручка, ₽',                'дата выкупа','n',   c_rev, p_rev,                                   c_d_sales, p_d_sales),
    (6,'Реклама, ₽',                'дата рекламы','n',  c_ads, p_ads,                                   c_d_ads, p_d_ads),
    (7,'ДРР',                       'реклама / выручка','pct', SAFE_DIVIDE(c_ads,c_rev), SAFE_DIVIDE(p_ads,p_rev), c_d_adsrev, p_d_adsrev),
    (8,'Доход до себестоимости, ₽', 'экономика','n',     c_con, p_con,                                   c_d_contrib, p_d_contrib),
    (9,'Доходность до себестоимости','экономика','pct',  SAFE_DIVIDE(c_con,c_rev), SAFE_DIVIDE(p_con,p_rev), c_d_contrib, p_d_contrib)
  ])
)
SELECT
  metric,
  basis,
  IF(unit='pct', FORMAT('%.1f %%', cv_*100), REPLACE(FORMAT('%\'.0f', cv_), ',', ' ')) AS cur_txt,
  IF(unit='pct', FORMAT('%.1f %%', pv_*100), REPLACE(FORMAT('%\'.0f', pv_), ',', ' ')) AS prv_txt,
  IF(cd = pd,
     IF(unit='pct', FORMAT('%+.1f п.п.', (cv_-pv_)*100), REPLACE(FORMAT('%+\'.0f', cv_-pv_), ',', ' ')),
     'н/с') AS delta_txt,
  IF(cd = pd AND unit <> 'pct', SAFE_DIVIDE(cv_-pv_, ABS(NULLIF(pv_,0))), NULL) AS delta_pct,
  CONCAT(CAST(cd AS STRING),' / ',CAST(pd AS STRING),' сут',
         IF(cd = pd, '', ' — не сопоставимо')) AS comparability
FROM m ORDER BY sort_ord

```

### card 64 — Рейтинг товаров за период (SKU)  ·  `table`

> Сортировка: SUM(contribution_pre_cogs_rub) DESC. БАЗЫ ДАТ РАЗНЫЕ И НЕ СМЕШИВАЮТСЯ: «Заказы (брутто)» и «Отменено» — дата заказа, «Выкупы» — дата выкупа, «Реклама» — дата рекламной активности, «Выручка» — дата выкупа. Отношение выкупов к заказам не вычисляется. Все ratio — ratio-of-sums за период: доля отмен = SUM(canceled)/NULLIF(SUM(orders_gross),0), ДРР = SUM(ads)/NULLIF(SUM(revenue),0), доходность = SUM(contribution)/NULLIF(SUM(revenue),0). AVG посуточных отношений не применяется. Строки SKU, у которых за период одновременно известны и равны нулю заказы, выкупы, выручка и реклама, из рейтинга скрыты — из модели SKU при этом не удаляются. Неизвестное (NULL, нет покрытия) не скрывается никогда. Себестоимость товара не вычитается: показатель не является прибылью.

```sql
WITH per AS (
  SELECT
    nm_id,
    product_name_short                    AS product,
    SUM(orders_gross_qty)                 AS orders_gross_qty,
    SUM(canceled_qty)                     AS canceled_qty,
    SUM(buyouts_qty)                      AS buyouts_qty,
    SUM(sales_revenue_seller_base_rub)    AS revenue_rub,
    SUM(ad_spend_attributed_rub)          AS ad_spend_rub,
    SUM(contribution_pre_cogs_rub)        AS contribution_rub
  FROM `wb_mart.V_DASH_SKU_DAILY`
  WHERE {{day}}
    [[AND {{sku}}]]
  GROUP BY nm_id, product
)
SELECT
  nm_id,
  product,
  orders_gross_qty,
  canceled_qty,
  SAFE_DIVIDE(canceled_qty, NULLIF(orders_gross_qty, 0))    AS canceled_share,
  buyouts_qty,
  revenue_rub,
  ad_spend_rub,
  SAFE_DIVIDE(ad_spend_rub, NULLIF(revenue_rub, 0))         AS drr,
  contribution_rub,
  SAFE_DIVIDE(contribution_rub, NULLIF(revenue_rub, 0))     AS contribution_to_revenue
FROM per
WHERE
  -- Скрываем ТОЛЬКО доказанно неактивные в периоде SKU (все четыре величины известны и равны нулю).
  -- Неизвестное (NULL, нет покрытия) НЕ скрывается: отсутствие данных не равно отсутствию активности.
     COALESCE(orders_gross_qty, 0) <> 0
  OR COALESCE(buyouts_qty, 0)      <> 0
  OR COALESCE(revenue_rub, 0)      <> 0
  OR COALESCE(ad_spend_rub, 0)     <> 0
  OR orders_gross_qty IS NULL
  OR buyouts_qty      IS NULL
  OR revenue_rub      IS NULL
  OR ad_spend_rub     IS NULL
ORDER BY contribution_rub DESC

```

### card 65 — Заказы и отмены по дням (SKU)  ·  `line`

> БАЗА ДАТ: ДАТА ЗАКАЗА для обеих серий. «Заказы» — брутто (orders_gross_qty = orders_qty + canceled_qty), «Отменено» — часть этих же заказов. Обе метрики измерены в одной дате-базе, поэтому их допустимо читать вместе. Выкупы на этом графике сознательно отсутствуют: они живут в базе даты выкупа и отдельным графиком. Гейт orders_covered; сутки вне покрытия дают NULL и разрыв линии, а не ноль.

```sql
SELECT
  day,
  SUM(orders_gross_qty) AS orders_gross_qty,
  SUM(canceled_qty)     AS canceled_qty
FROM `wb_mart.V_DASH_SKU_DAILY`
WHERE {{day}}
  [[AND {{sku}}]]
GROUP BY day
ORDER BY day
```

### card 70 — Выкупы по дням (SKU)  ·  `line`

> БАЗА ДАТ: ДАТА ВЫКУПА (sale_date). СОБЫТИЕ, а не когорта. Этот график намеренно отделён от заказов: заказы измеряются по дате заказа, и совмещение двух баз в одной воронке создаёт ложное впечатление конверсии. Отношение выкупов к заказам НЕ вычисляется нигде на экране. Замер 01.08–22.08: WB Funnel даёт 410 когортных выкупов против 443 событийных, а по отдельным SKU событийные выкупы превышают брутто-заказы (Крем УВЛ: 28 против 22). Гейт sales_covered.

```sql
SELECT
  day,
  SUM(buyouts_qty) AS buyouts_qty
FROM `wb_mart.V_DASH_SKU_DAILY`
WHERE {{day}}
  [[AND {{sku}}]]
GROUP BY day
ORDER BY day
```

### card 66 — Выручка и доход до себестоимости по дням (SKU)  ·  `combo`

> БАЗЫ ДАТ: выручка — дата выкупа, доход до себестоимости — cross-base величина (сводит выручку по дате выкупа, комиссию и логистику по дате финансовой операции, рекламу по дате рекламной активности), поэтому управленчески осмысленна на горизонте от недели. Обе величины в ₽ и на одной оси — dual-axis не требуется. Сутки вне покрытия дают NULL и разрыв, а не ноль. Себестоимость товара не вычитается. Показатель не является прибылью.

```sql
SELECT
  day,
  SUM(sales_revenue_seller_base_rub) AS revenue_rub,
  SUM(contribution_pre_cogs_rub)     AS contribution_rub
FROM `wb_mart.V_DASH_SKU_DAILY`
WHERE {{day}}
  [[AND {{sku}}]]
GROUP BY day
ORDER BY day
```

### card 69 — Требуют внимания (SKU)  ·  `table`

> ТОЛЬКО ЖЁСТКИЕ ОБЪЕКТИВНЫЕ УСЛОВИЯ, ни одного порога: (1) доход до себестоимости < 0 при выручке > 0; (2) расход на рекламу > 0 при нулевой выручке; (3) все заказы периода отменены (canceled = orders_gross > 0). Отношение выкупов к заказам не используется. Пороговые правила («высокий ДРР», «много отмен») владельцем не заданы и сознательно не придуманы; ДРР выводится справочно и фильтром не является. NULL (нет покрытия) ни под одно условие не подпадает: неизвестно ≠ проблема. SKU, подходящий под несколько условий, даёт ОДНУ строку с перечислением причин через «; » — денежные суммы не размножаются. Сортировка: сначала отрицательный доход по возрастанию, затем расход на рекламу по убыванию.

```sql
WITH per AS (
  SELECT
    nm_id,
    product_name_short                    AS product,
    SUM(orders_gross_qty)                 AS orders_gross_qty,
    SUM(canceled_qty)                     AS canceled_qty,
    SUM(sales_revenue_seller_base_rub)    AS revenue_rub,
    SUM(ad_spend_attributed_rub)          AS ad_spend_rub,
    SUM(contribution_pre_cogs_rub)        AS contribution_rub
  FROM `wb_mart.V_DASH_SKU_DAILY`
  WHERE {{day}}
    [[AND {{sku}}]]
  GROUP BY nm_id, product
),
flagged AS (
  SELECT
    per.*,
    ARRAY_TO_STRING(ARRAY(
      SELECT r FROM UNNEST([
        IF(contribution_rub < 0 AND revenue_rub > 0,           'Отрицательный доход до себестоимости', NULL),
        IF(ad_spend_rub > 0 AND revenue_rub = 0,               'Есть рекламные расходы без выручки',   NULL),
        IF(orders_gross_qty > 0 AND canceled_qty = orders_gross_qty, 'Все заказы периода отменены',    NULL)
      ]) AS r WHERE r IS NOT NULL
    ), '; ') AS reason
  FROM per
)
SELECT
  nm_id, product, reason,
  orders_gross_qty, canceled_qty, revenue_rub, ad_spend_rub,
  SAFE_DIVIDE(ad_spend_rub, NULLIF(revenue_rub, 0)) AS drr,
  contribution_rub
FROM flagged
WHERE reason <> ''
ORDER BY
  IF(contribution_rub < 0, 0, 1) ASC,
  contribution_rub ASC,
  ad_spend_rub DESC

```

## 10. Приёмка

Окно A `2026-08-01 … 2026-08-22`, окно B `2026-07-27 … 2026-08-16`.

| Проверка | Результат |
| --- | --- |
| Грейн `day × nm_id` | 7 477 строк = 7 477 уникальных ключей, дублей 0, `nm_id IS NULL` 0 |
| SKU-слой = KPI-слой | Δ = 0 по 12 аддитивным метрикам на обоих окнах |
| Окно A | заказы 499, отменено 38, выкупы 443, выручка 290 284,24, реклама 33 534,38, доход 92 628,00 |
| Окно B | заказы 497, отменено 46, выкупы 402, выручка 292 302,67, реклама 48 579,56, доход 81 629,92 |
| Тождество | `orders_gross` 497 = `orders_qty` 451 + `canceled` 46 |
| Ratio-of-sums | ДРР 0,166196087 = 48 579,56/292 302,67; доходность 0,279265051; доля отмен 0,092555332 = 46/497 |
| NULL-семантика | 0 утечек внутрь покрытия, 0 значений вне покрытия, 0 ложных нулей |
| Нет buyout conversion | regex-поиск по SQL всех 18 карточек дашборда — 0 совпадений |
| Рейтинг | 22 строки (3 неактивных скрыты), Σ заказов 499, Σ дохода 92 628,00 — сходится с KPI |
| Динамика | 22 точки в каждом из трёх графиков |
| Фильтр SKU | 1 / 2 / 3 SKU — SUM подтверждён вручную; SKU без активности даёт пустой рейтинг, ДРР `NULL` |
| Сопоставимость сравнения | 7 строк 30/30 сут → Δ выводится; 2 строки 29/30 → «н/с» |
| Executive регресс | 12 карточек на окне B — все совпали; 16 определений и dashboard 2 — 0 изменённых полей |
| Pipeline | baseline до/после правок Stage 2.0.1 идентичен |
| Stage 3B | `V_ADV_COSTS → V_ADV_COSTS_UNION_PREBOOTSTRAP`, Phase B = false, `FACT_ADS_SPEND%` = 0 |
| COGS | 0 объектов в трёх датасетах, 0 следов в репозитории |

### Замечание о фоновом приросте данных

Между сборкой Stage 2 и коррекцией Stage 2.0.1 отработал штатный планировщик загрузки:
`FACT_ORDERS` 4 410 → 4 426, `FACT_SALES` 4 090 → 4 103, `FACT_FINANCE` 205 381 → 205 457,
`FACT_ADS_SKU_DAILY` 5 752 → 5 767, `MART_SKU_DAILY` 7 452 → 7 477 (`max_day` 2026-08-25 → 2026-08-26).
sha256 обеих процедур, `REF_COST_MAP` (19) и число вью (18) при этом не изменились — это приход новых
суток, а не изменение пайплайна. Контрольные окна A и B данным приростом не затронуты: все 12 значений
совпали до копейки. Вердикт `PIPELINE_UNCHANGED` для Stage 2.0.1 измеряется относительно baseline,
снятого непосредственно перед правками.

## 10.1. Приёмка владельцем

**Server acceptance — PASS** (2026-08-27). Полный протокол в §10.

**Visual acceptance — APPROVED** (2026-08-27, владелец, третий круг скриншотов). Подтверждено визуально:

* карточка «Период и покрытие данных SKU» отображается полностью;
* покрытие `30/30` и `29/30 ⚠` читается корректно;
* KPI «Отменено» отрисовывается как `52`; ранее наблюдавшийся skeleton принят как transient loading state
  (диагностика — §4.6: дефекта нет, изменений не вносилось);
* сравнение показывает все 9 строк;
* обе строки с неполным покрытием экономики показывают `29 / 30 сут — не сопоставимо`;
* рейтинг помещается по ширине и показывает «Доход до себестоимости, ₽» и «Доходность до себестоимости, %»;
* три графика динамики визуально корректны и разделены по базе дат;
* блок «Требуют внимания» читается корректно;
* текущая минимальная высота карточки 68 принята владельцем.

**Решение владельца:** дополнительную косметическую модификацию footer/table settings не выполнять.
Зазор между последней строкой данных и прижатым к низу футером таблицы — минимум формата Metabase,
а не запас высоты; уменьшение обрезало бы данные.

## 11. Deferred work

1. **Cohort buyout conversion.** Требует `order_date` в `FACT_SALES`.
2. **Stage 3B Phase B** — billed ads spend по SKU.
3. **`REF_COGS`** — без неё нет прибыли, маржи и unit economics.
4. **Пороговые правила блока «Требуют внимания»** — ожидают ACK владельца (§7).
5. **Фильтр товара по `nm_id` с человекочитаемым отображением** — потребует таблицы-справочника и FK.
6. **Метаданные Metabase таблицы 39 отстают на колонку** `orders_gross_qty`: 59 полей против 60.
   На экран не влияет (всё native SQL), но потребует `mb db sync-schema 2` до structured-запросов.
7. **Сравнение при неравном покрытии** сейчас показывает «н/с». Альтернатива — сравнение по одинаковому
   числу полностью покрытых суток — отложена как более сложная и требующая отдельного контракта.

## 12. Откат

Исполняемый скрипт: `tools/stage2_sku_performance_rollback.sh` (проверен `--dry-run`).
Архивирует dashboard 3, карточки 56–71 и коллекцию 7, предварительно убедившись, что Executive не затронут.
Объектов BigQuery не создавалось — откатывать в BigQuery нечего.
