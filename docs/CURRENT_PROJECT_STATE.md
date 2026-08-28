# EVETIS WB Analytics — Current Project State

**Last verified:** 2026-08-27
**Repository:** `evelagin/evetis-wb-analytics`
**Branch:** `main`
**Verified HEAD:** `f8ea31e575db45e0bf4ecf5b9afda85724ff981f`
**Purpose:** authoritative human-readable checkpoint for restarting work in a new Claude Code session.

---

## READ THIS FIRST

Before any new development:

1. read `CLAUDE.md`;
2. read this file;
3. run `git fetch origin`;
4. verify `HEAD == origin/main`;
5. verify working tree clean;
6. inspect `CHANGELOG.md` since this checkpoint;
7. only then continue.

Всё, что написано ниже, проверено командами против production 2026-08-27, а не восстановлено
по памяти предыдущей сессии. Исторические значения помечены словом «исторический»;
неподтверждённые вещи названы гипотезами, а не фактами.

---

# Git / Repository State

| поле | значение |
| --- | --- |
| repository | `evelagin/evetis-wb-analytics` |
| remote URL | `https://github.com/evelagin/evetis-wb-analytics.git` |
| local path | `/Users/evgenelagin/Projects/evetis-wb-analytics` |
| branch | `main` |
| HEAD SHA | `889fcfad1faf8c95059cc5218c22060bcb66c81a` |
| origin/main SHA | `889fcfad1faf8c95059cc5218c22060bcb66c81a` |
| ahead | 0 |
| behind | 0 |
| working tree | clean |
| дата проверки | 2026-08-28 |

Последние коммиты `main`:

```
889fcfa  feat(finance): add Stage 3.1C corrected executive finance overlay
9ada18b  feat(finance): add Stage 3.1C advertising billing classification layer
7c74cde  feat(cogs): add Stage 3.1B COGS consumer views
cd166e9  docs(project): checkpoint state after Stage 3.1A
f8ea31e  feat(cogs): add Stage 3.1A product COGS reference layer
7bf0472  docs(project): add CURRENT_PROJECT_STATE checkpoint verified against production
e902183  chore(metabase): add disaster-recovery snapshot for Executive and SKU Performance
05e0303  docs(analytics): restore Stage 4A PR1 rollout record
a033160  feat(analytics): complete Stage 2 SKU performance dashboard
4bcfa5b  feat(dash): брутто-семантика заказов orders_gross_qty + KPI «Отменено» (Stage 1.10B)
```

## Synchronization rule

Локальный репозиторий — рабочая копия. GitHub — удалённая защищённая копия.
Пока изменение не запушено, оно существует в одном экземпляре и теряется вместе с машиной.

Протокол завершения любого законченного этапа:

1. validation;
2. `git status`;
3. commit;
4. push;
5. verify `HEAD == origin/main`;
6. verify ahead = 0, behind = 0;
7. verify working tree clean.

**Claude НЕ синхронизирует GitHub автоматически.** Commit без push остаётся только локально.
Push не выполняется до acceptance владельца, если задание прямо этого не разрешает.

Исторический пример, зачем это правило: 26–27 августа пять законченных коммитов
(Stage 1.5/1.6, 1.8, 1.9, 1.10B, Stage 2) некоторое время существовали только локально
до отдельного push.

---

# Current Production Architecture

```
WB (API + XLSX-отчёты)
   │
   ├─ Google Apps Script  ─────────┐
   └─ Cloud Run loaders  ──────────┤
                                   ▼
                             BigQuery  wb_raw          ← RAW: сырые ответы как есть
                                   │
                                   ▼
                             BigQuery  wb_mart / FACT_* ← канонические факты, дедуп, типы
                                   │
                                   ▼
                             BigQuery  MART_SKU_DAILY   ← витрина, грейн сутки × nm_id
                                   │
                                   ▼
                             BigQuery  V_DASH_*         ← контракт представления
                                   │
                                   ▼
                             Metabase (Docker, localhost:3000)

BigQuery  evetis_ref  ← reference layer: историческая Product COGS.
                        Marketplace-independent, ключ internal_sku.
                        Потребителями (MART / V_DASH / Metabase) НЕ подключён.
```

Что делает каждый слой, простым языком:

* **RAW (`wb_raw`)** — принимает данные WB без интерпретации. Ничего не считает.
* **FACT (`wb_mart.FACT_*`)** — приводит RAW к каноническому виду: типы, ключи, дедупликация.
* **MART (`MART_SKU_DAILY`)** — сводит факты в одну строку на сутки и товар.
* **V_DASH_\*** — публичный контракт для дашбордов: гейты покрытия, NULL вне покрытия,
  никаких ratio-колонок (их считает потребитель как отношение сумм).
* **`evetis_ref`** — справочный слой Product COGS. Ни от чего не зависит и в конвейер
  выше не встроен: витрины его пока не читают. Появился в Stage 3.1A.
* **Metabase** — только читает. В конвейере не участвует, данных не пишет.

Фактический состав на 2026-08-27:

| компонент | состояние |
| --- | --- |
| BigQuery datasets | `wb_raw`, `wb_mart`, `evetis_communications`, **`evetis_ref`** |
| `wb_mart` | 17 базовых таблиц, **23 view**, 2 процедуры (`sp_bootstrap_facts`, `sp_build_mart_sku_daily`) — 40 объектов |
| `MART_SKU_DAILY` | 7 477 строк, `max_day = 2026-08-26` |
| `FACT_ORDERS` | 4 426 строк, до 2026-08-26 |
| `FACT_SALES` | 4 103 строки, до 2026-08-26 |
| `FACT_FINANCE` | 205 457 строк, до 2026-08-25 |
| `FACT_ADS_SKU_DAILY` | 5 767 строк, до 2026-08-26 |
| `REF_COST_MAP` | 19 правил |
| `wb_raw.REF_SKU_MASTER` | 25 SKU, из них 14 — наборы |
| `evetis_ref` | 4 объекта: 2 таблицы + 2 view (Stage 3.1A) |
| `evetis_ref.REF_SKU_COGS_HISTORY` | 17 материализованных интервалов Product COGS |
| `evetis_ref.REF_BUNDLE_COMPONENTS` | 33 строки состава, 14 наборов, стоимостных полей нет |
| `evetis_ref.V_BUNDLE_COGS_DERIVED` | 21 производный интервал COGS набора |
| `evetis_ref.V_PRODUCT_COGS_EFFECTIVE` | unified resolver, 38 интервалов (17 + 21) |
| dashboard views | `V_DASH_KPI_DAILY` (76 колонок), `V_DASH_SKU_DAILY` (60), `V_DASH_COVERAGE_DAILY` (33), `V_DASH_FRESHNESS_HEADER`, `V_DASH_FRESHNESS_BY_CONTRACT` (14), **`V_DASH_FINANCE_CORRECTED_DAILY`** (Stage 3.1C PR2) |
| Google Apps Script | 39 файлов `.gs` в репозитории; слой Google Sheets (справочники, себестоимость) |
| Cloud Run loaders | `cloud/src/loaders/`: `mart`, `stocks` |
| Metabase | v0.63.15.1 OSS, Docker, 2 наших дашборда, 32 карточки |
| GitHub | `main` = `f8ea31e`, плюс ветка `rescue/stash-20260826` |
| Claude skills | 7 `SKILL.md` в `.claude/skills/`, все под версионным контролем |

Design-only и **не** production: спека рекламного экрана Stage 4B (см. раздел
Rescue & Historical Work), Stage 3B Phase B.

🔴 `evetis_ref` — **production**. Stage 3.1B создал consumer-view
(`V_FACT_FINANCE_COGS`, `V_MART_SKU_DAILY_COGS`), но **Product COGS по-прежнему не
входит в Executive**: ни `MART_SKU_DAILY`, ни `V_DASH_KPI_DAILY`, ни
`V_DASH_FINANCE_CORRECTED_DAILY`, ни карточки Metabase его не читают. Все показатели
дашбордов остаются **до себестоимости товара**.

---

# Stage 1 — Executive Dashboard

**Dashboard:** `EVETIS · WB Executive` (Metabase dashboard id 2, коллекция 6 «01 Executive»).
Карточки 40–55, грейн сутки. Источник — `wb_mart.V_DASH_KPI_DAILY`, а для трёх
финансовых карточек (**51**, **53**, **54**) — исправленный слой
`wb_mart.V_DASH_FINANCE_CORRECTED_DAILY` (Stage 3.1C PR3, см. ниже).

Реализовано и работает:

свежесть данных · качество выбранного периода · заказы брутто · отменено · выкупы ·
выручка seller-base · атрибутированный расход на рекламу · ДРР · возвраты ·
к перечислению (net settlement) · доход до себестоимости · результат периода до
себестоимости · рентабельность до себестоимости · структура затрат · таблица SKU ·
динамика по дням.

## Семантика метрик

| метрика | смысл | база даты |
| --- | --- | --- |
| `orders_gross_qty` | все оформленные заказы, включая впоследствии отменённые | дата заказа |
| `orders_qty` | заказы без отмен | дата заказа |
| `canceled_qty` | отменённые заказы | дата заказа |
| `buyouts_qty` | события выкупа | дата продажи (`sale_date`) |

```
contribution_pre_cogs_rub =
    выручка seller-base
  − сбор маркетплейса
  − логистика SKU
  − атрибутированная реклама

period_result_pre_cogs_corrected_rub =
    contribution_pre_cogs_rub
  − (расходы уровня счёта − рекламный биллинг WB)
```

🔴 **Executive использует исправленную финансовую семантику (Stage 3.1C PR3).**
Из расходов уровня счёта исключается класс `AD_BILLING_RECONSTRUCTED`: реклама уже
вычтена как attributed внутри `contribution_pre_cogs_rub`, и повторное вычитание
рекламного биллинга давало двойной счёт. Транзит поставок и неопознанные удержания
остаются расходом. Прежняя величина `period_result_pre_cogs_rub` в
`V_DASH_KPI_DAILY` сохранена как BEFORE и никем на дашборде не показывается.

Метрика защищена двойным гейтом покрытия
(`contribution_covered AND finance_covered`): при одном гейте на сутках без выручки
она молча превратилась бы в «минус расходы уровня счёта». Проценты считаются
**ratio-of-sums** по знаменателю `revenue_base_period_result_rub`, выровненному по
тому же множеству суток, что и числитель.

🔴 **Это НЕ прибыль.** Себестоимость товара, fulfillment, OPEX и налоги в эти
показатели не входят. Слова «прибыль», «маржа», «EBITDA» к метрикам до COGS не
применяются — ни в названиях карточек, ни в разговоре.

---

# Stage 2 — SKU Performance

**Dashboard:** `EVETIS · WB SKU Performance` (Metabase dashboard id 3, коллекция 7).
Карточки 56–71. Источник — `wb_mart.V_DASH_SKU_DAILY`, грейн сутки × `nm_id`.

Статус:

* Stage 2 **завершён** (включая коррекции 2.0.1 и 2.0.2);
* server acceptance — **PASS**;
* visual acceptance — **PASS** (владелец, 2026-08-27);
* закоммичен: `a033160`;
* сохранён на GitHub;
* скрипт отката существует: `tools/stage2_sku_performance_rollback.sh` (проверен `--dry-run`).

Полный дизайн, SQL всех карточек и протокол приёмки — `docs/STAGE2_SKU_PERFORMANCE_2026-08-27.md`.

Блоки экрана: покрытие данных SKU · KPI · сравнение с предыдущим сопоставимым периодом ·
рейтинг товаров · динамика · требуют внимания · фильтр товара.

## Semantic rules экрана

1. Заказы и отмены — **дата заказа**.
2. Выкупы — **дата продажи**.
3. Конверсия выкупа как `buyouts / orders` **запрещена** до появления cohort-контракта.
4. Все ratio — **ratio-of-sums** за период, не среднее посуточных отношений.
5. Вне покрытия — **NULL**, не искусственный ноль.
6. Реклама на этом экране — `ad_spend_attributed_rub` (атрибуция, не биллинг).

## Почему деление запрещено — измеримый пример

На окне 01.08–22.08.2026 отдельные товары дают **больше выкупов, чем заказов**:
«Крем УВЛ» — 28 выкупов при 22 брутто-заказах, «Набор тоник+сыворотка АКНЕ» — 6 при 5.
Отношение дало бы 127 % и 120 %. Это не ошибка данных: числитель считается по дате выкупа,
знаменатель — по дате заказа, и они описывают разные множества событий.

Тот же эффект на уровне бренда: WB Funnel показывает 410 когортных выкупов, наш контур —
443 событийных. Оба числа верны в своей базе дат.

---

# Finance Semantics & Reconciliation

Сверка «финансовые XLSX-отчёты WB → BigQuery → Metabase» выполнена (Stage 1.4–1.5,
исторический протокол — в `CHANGELOG.md`).

## Исправленный дефект знака

**Было:** `V_WB_FINANCE_AMOUNTS_LONG_MAPPED` применял `ABS()` построчно к записям,
помеченным как `COST`. Для однознакового поля это безвредно; для знакопеременного —
уничтожало знак: кредит превращался в расход, а итог рос на удвоенную сумму противознака.

**Стало (Stage 1.5):** знакопеременные пары переведены в ветку `ADJUSTMENT`,
которая знак сохраняет (`x * field_normalization_sign`).

Проверено SELECT-ом по `wb_mart.REF_COST_MAP` 2026-08-27 — четыре пары несут явную
пометку Stage 1.5:

| `op_key` | `amount_field` | `economic_direction` |
| --- | --- | --- |
| Продажа | `commission_amount` | `ADJUSTMENT` |
| Удержание | `deduction` | `ADJUSTMENT` |
| Пересчет платной приемки | `acceptance` | `ADJUSTMENT` |
| Штраф | `penalty` | `ADJUSTMENT` |

Всего в `REF_COST_MAP` 19 правил.

## Fail-closed guard

`sql/mart/pr_mart2b_sku_daily.sql` (§pre, ~строки 217–234) содержит исполняемый `ASSERT`,
который **запрещает** знакопеременную пару в ABS-ветке `COST`/`CREDIT`. Если такая пара
появится снова, сборка витрины упадёт с явным сообщением, а не посчитает молча неверно.

Контрольные значения Stage 1 (окно 27.07–16.08.2026: выручка 292 302,67 ₽,
контрибуция 81 629,92 ₽, результат периода 33 734,41 ₽, удержания 33 312,68 ₽) —
**исторические benchmark'и**, подтверждённые повторно 2026-08-27 при проверке Executive.

---

# Advertising State

Проверено фактически 2026-08-27:

| проверка | результат |
| --- | --- |
| куда указывает `wb_raw.V_ADV_COSTS` | `SELECT * FROM wb_raw.V_ADV_COSTS_UNION_PREBOOTSTRAP` |
| `FACT_ADS_SPEND_ALLOC_DAILY` | **не существует** (ни в `wb_mart`, ни в `wb_raw`) |
| `FACT_ADS_SPEND_UNALLOC_DAILY` | **не существует** |
| Stage 3B gate | **закрыт**, исполняемый `ASSERT` в `sql/mart/pr_mart1_facts.sql:78` |

Модель подтвердилась:

* **Stage 3B Phase A — подготовлен.**
* **Stage 3B Phase B — НЕ выполнен.**
* `V_ADV_COSTS` ещё не переключён на snapshot-семантику.
* Deferred-код #116 остаётся под гейтом и в production не раскатан.

Простым языком: SKU Performance сейчас показывает **рекламную атрибуцию** —
расход, который WB отнесла к товару. Это не то же самое, что полноценный контракт
распределения оплаченного (billed) расхода по SKU. На контрольном окне 01.08–22.08
разница мала (атрибуция 33 534,38 ₽ против биллинга 33 536,00 ₽, то есть 1,62 ₽ или
0,005 %), но контракт от этого не становится billed-контрактом.

Cutover не начат и не начинается без отдельного согласования.

---

# COGS & Unit Economics — REFERENCE + CONSUMER LAYERS LIVE, EXECUTIVE NOT CONNECTED

## Stage 3.1A — Product COGS Reference Layer

**Status: `CLOSED / ACCEPTED / PUSHED`**
**Commit:** `f8ea31e575db45e0bf4ecf5b9afda85724ff981f`
Контракт слоя: `docs/STAGE3_1A_COGS_REFERENCE_LAYER_2026-08-27.md`.
Сборка: `sql/ref/pr_ref_cogs_history.sql`. Acceptance: `sql/ref/pr_ref_cogs_validation.sql`.
Откат: `tools/stage3_1a_cogs_rollback.sh [--dry-run]`.

Создан датасет `evetis_ref` (location EU) — marketplace-independent справочный слой
Product COGS. Четыре production-объекта:

| объект | тип | содержимое |
| --- | --- | --- |
| `REF_SKU_COGS_HISTORY` | TABLE | 17 материализованных исторических интервалов Product COGS; только самостоятельные стоимостные факты; ключ `internal_sku`; идентификаторов канала в схеме нет |
| `REF_BUNDLE_COMPONENTS` | TABLE | 33 строки состава, 14 определений наборов; стоимостных полей не содержит |
| `V_BUNDLE_COGS_DERIVED` | VIEW | 21 производный интервал; стоимость ФФ-собранного набора вычисляется из действующих Product COGS компонентов; fail-closed, если компонент не разрешается |
| `V_PRODUCT_COGS_EFFECTIVE` | VIEW | unified resolver, 38 интервалов = 17 материализованных + 21 производный |

**Acceptance-факты:** `REF_SKU_COGS_HISTORY` 17 строк · `REF_BUNDLE_COMPONENTS`
33 строки / 14 наборов · `V_BUNDLE_COGS_DERIVED` 21 интервал ·
`V_PRODUCT_COGS_EFFECTIVE` 38 интервалов · проверено 40 110 исторических
finance-событий WB (2024-09-07 … 2026-08-26): `matches = 1` для 40 110 из 40 110,
`matches = 0` → 0, `matches > 1` → 0.

**Регрессия production отсутствует:** `wb_raw` 56 объектов, `wb_mart` 35 объектов,
`MART_SKU_DAILY` 7 477 строк, `REF_COST_MAP` 19 правил — все baseline без изменений.

🔴 **Stage 3.1A создал справочный слой, но НЕ подключил Product COGS к существующим
потребителям.** `MART_SKU_DAILY`, `V_DASH_*` и Metabase его не читают.

### Product COGS — замороженная семантика

Product COGS включает стоимость товара **до поступления на фулфилмент**: закупочную
стоимость, относимые расходы в Китае, таможенные расходы, доставку до фулфилмента.

Product COGS **не включает**: хранение ФФ · сборку на ФФ · доставку ФФ → склад
маркетплейса · расходы кабинета маркетплейса · рекламу · операционные расходы · налог.

🔴 Не смешивать Product COGS и расходы фулфилмента. Это разные слои и разные поля.

### Наборы

Для ФФ-собранных наборов:

```
bundle_product_cogs(date) = Σ component_qty × component_product_cogs(date)
```

Если хотя бы один компонент не разрешается однозначно — Product COGS набора
**unknown / NULL**. Частичная сумма запрещена. Unknown ≠ zero.

### Импортный набор руки+тело

`EVT-SET-HAND-BODY` до 2025-05-01 существовал как импортированный готовый набор:
Product COGS = **429 ₽**, самостоятельная неделимая товарная единица. С 2025-05-01
применяется derived-модель ФФ-сборки. Режимы **не пересекаются** (исполняемый ASSERT).

### Fail-closed ограничения, действующие сейчас

`EVT-HC-BODY-300` **не имеет действующего inventory-интервала после 2026-04-26.**
Следовательно `EVT-SET-HAND-BODY` также не имеет производного интервала после этой даты.

Это **by design**, а не пробел: SKU выбыл, физического остатка нет. Автоматически эти
интервалы **не открывать**. Будущая новая физическая партия крема тела обязана получить
новый самостоятельный COGS-интервал с собственной себестоимостью и датой.

🔴 Owner-current справочные значения **219 ₽** и **459 ₽** не означают наличия
действующего physical inventory interval на текущую дату.

### Management boundaries

Шесть management / reconstructed transitions:

| id | дата | SKU |
| --- | --- | --- |
| T1 | 2025-01-26 | крем рук |
| T2 | 2025-03-10 | крем рук |
| T3 | 2025-11-01 | крем тела |
| T4 | 2025-05-01 | набор руки+тело, смена режима |
| T5 | 2026-03-01 | сыворотка УВЛ |
| T6 | 2026-08-01 | сыворотка АКНЕ |

🔴 **T6 / сыворотка АКНЕ `2026-08-01` — `MANAGEMENT_RECONSTRUCTED_BOUNDARY`, а НЕ
доказанная физическая дата полного перехода партии.** Mixed-batch limitation остаётся
известным ограничением effective-date-модели v1. Не переинтерпретировать эту дату как
physical batch transition.

### Marketplace independence

`evetis_ref` — marketplace-independent. Product identity = **`internal_sku`**.
WB `nm_id` **не является частью схемы COGS history**. WB-специфичное разрешение
выполняется снаружи справочного слоя (в acceptance-запросах и будущих витринах).
При подключении новых каналов должен использоваться отдельный channel mapping layer,
а не изменение COGS-контракта.

---

## Stage 3.1B — COGS Consumer Layer

Коммит `7c74cde`. Контракт: `docs/STAGE3_1B_COGS_CONSUMER_2026-08-27.md`.

Создано два view в `wb_mart`, читающих `evetis_ref.V_PRODUCT_COGS_EFFECTIVE`:

| объект | назначение |
| --- | --- |
| `V_FACT_FINANCE_COGS` | Product COGS на финансовых строках |
| `V_MART_SKU_DAILY_COGS` | Product COGS на грейне сутки × SKU |

🔴 Слой существует, но **потребителями не подключён**: ни `V_DASH_*`, ни Metabase его
не читают. Числа на дашбордах Stage 3.1B не изменил.

---

## Stage 3.1C PR1 — Advertising Billing Classification — **CLOSED**

Коммит `9ada18b`. Контракт: `docs/STAGE3_1C_AD_BILLING_CLASSIFICATION_2026-08-27.md`.

Установлено, что **92,5 %** account-level операции WB «Удержание» является рекламным
биллингом. Создано два view:

| объект | назначение |
| --- | --- |
| `V_WB_DEDUCTIONS_CLASSIFIED` | построчная классификация удержаний WB |
| `V_ADVERTISING_RECONCILIATION_DAILY` | суточная сверка attributed / billed / unallocated |

Классы: `AD_BILLING_RECONSTRUCTED`, `TRANSIT_DEDUCTION`, `UNCLASSIFIED_DEDUCTION`,
`CLASSIFICATION_CONFLICT`.

## Stage 3.1C PR2 — Executive Financial Semantics Correction — **CLOSED**

Коммит `889fcfa`. Контракт: `docs/STAGE3_1C_PR2_EXECUTIVE_SEMANTICS_2026-08-27.md`.
Скрипты: `sql/dash/pr_dash_finance_corrected_v1.sql`, `..._validation.sql`.
Откат: `tools/stage3_1c_pr2_finance_corrected_rollback.sh`.

Создан один overlay-view `wb_mart.V_DASH_FINANCE_CORRECTED_DAILY` (грейн сутки, 1:1 к
`V_DASH_KPI_DAILY`). Исправлены два дефекта:

1. **двойной счёт рекламы** — расход вычитался и как attributed внутри
   `contribution_pre_cogs_rub`, и повторно внутри `account_level_total_rub`;
2. **рассогласованный знаменатель процента** — числитель гейтился покрытием,
   знаменатель брался сырым.

```
account_level_total_corrected_rub    = account_level_total_rub - ad_billing_reconstructed_rub
period_result_pre_cogs_corrected_rub = contribution_pre_cogs_rub - account_level_total_corrected_rub
```

🔴 Исключается **только** `AD_BILLING_RECONSTRUCTED`. `TRANSIT_DEDUCTION`,
`UNCLASSIFIED_DEDUCTION` и `CLASSIFICATION_CONFLICT` остаются расходом.
Ratio-полей в view нет: процент считает потребитель как ratio-of-sums.

## Stage 3.1C PR3 — Metabase Executive Integration — **CLOSED**

Дата 2026-08-28. Контракт:
`docs/STAGE3_1C_PR3_METABASE_EXECUTIVE_INTEGRATION_2026-08-28.md`.
Изменения только в Metabase; BigQuery и семантические объекты не трогались.

Три карточки Executive переключены на `V_DASH_FINANCE_CORRECTED_DAILY`, IDs сохранены:

| card | что стало |
| --- | --- |
| **51** «Структура затрат за период» | операция WB «Удержание» разложена: транзит поставок · прочие и неопознанные · конфликт классификации · рекламный биллинг как контрольная строка |
| **53** «Результат периода до себестоимости» | `period_result_pre_cogs_corrected_rub` |
| **54** «Рентабельность до себестоимости» | ratio-of-sums по `revenue_base_period_result_rub` |

Field filter `{{day}}` перенаправлен с поля Metabase 909 на 2622; `id`/`name` тега не
менялись, поэтому dashboard-маппинги остались валидными. Раскладка dashboard 2 не
менялась. SKU Performance (dashboard 3, card 63) не изменён.

**Acceptance 01–26.08.2026:** BEFORE 37 469,47 ₽ → AFTER **90 344,47 ₽**,
рентабельность **26,93 %**.
**Regression control 01–25.08.2026:** **87 471,25 ₽**, **26,98 %** — совпадает со
снимком PR2. Разница между окнами — штатный data drift (сутки 26.08 получили
финансовое покрытие, eligible 25 → 26), а не изменение финансовой логики.

🔴 Рекламный биллинг **52 875 ₽** — reconciliation/control, **не вторая статья расхода**.
🔴 Transit (2 596,45 ₽) и unclassified (1 714,74 ₽) остаются расходами.
🔴 Product COGS в расчёт **не входит**.

---

## Исторический контекст до Stage 3.1A

Ниже — состояние, зафиксированное до коммита `f8ea31e`. Сохранено как история: оно
объясняет, почему слой строился именно так. **Актуальным состоянием не является.**

На тот момент себестоимость **отсутствовала в BigQuery**: объектов с `COGS` в `wb_mart`,
`wb_raw` и `evetis_communications` — ноль, `REF_COGS` не создан.

Источник себестоимости живёт в слое Google Sheets и обслуживается Apps Script:
поля `current_cogs` («Текущая себестоимость, ₽ (из COST_HISTORY)») и `cogs_valid_from`
(«Дата начала действия себестоимости») присутствуют в `apps-script/Sheetsschema`,
`apps-script/AuditAndNotes`, `apps-script/Protections`; работа с наборами —
в `apps-script/CostManagement` (`fillMissingBundleCogs()`).

Репозиторий фиксировал состояние прямо: «`REF_COGS` в BigQuery ещё нет: `COST_HISTORY`
живёт в листе и требует REF-Sync PR2» (`docs/DASHBOARD_DATA_LAYER_DESIGN_2026-08-15.md` §2.3).
Stage 1.3 был отмечен как незакрытый.

🔴 Этот абзац исторический. Слой Google Sheets остаётся legacy-источником и в новом
контракте source of truth не является: Product COGS берётся из `evetis_ref`.

## KNOWN (на момент до Stage 3.1A)

Разделяю три разных утверждения — их легко перепутать, и цена ошибки здесь высокая.

**1. Что подтверждает репозиторий.** В слое Apps Script существуют схема и логика работы
с себестоимостью: поля `current_cogs` и `cogs_valid_from` объявлены в
`apps-script/Sheetsschema`, описаны в `apps-script/AuditAndNotes`, защищены в
`apps-script/Protections`; наборы обрабатываются в `apps-script/CostManagement`
(`fillMissingBundleCogs()`). Это подтверждает **наличие схемы и логики** — и только.

**2. Что подтверждает BigQuery.** Product identity: **25 из 25** — 25 `nm_id` в фактах
против 25 строк `wb_raw.REF_SKU_MASTER`, орфанов 0, коллизий имён 0 в обе стороны.
Из них 14 помечены `is_bundle = TRUE`.

🔴 **Product identity 25/25 НЕ означает COGS mapping 25/25.** Это разные вещи: первое —
что товар опознан, второе — что у него есть корректная себестоимость на нужную дату.
Полнота живого источника себестоимости репозиторием **не подтверждена**.

**3. Последний доступный срез Stage 1.3** (замер со слов владельца, в репозитории
не зафиксирован, поэтому — **исторический ориентир, а не проверенный факт**):

```
22 MATCHED   2 AMBIGUOUS   1 MISSING
```

То есть у трёх SKU из 25 сопоставление себестоимости на момент того замера не было
однозначным.

**Следствие.** Перед любым внедрением COGS требуется **повторная проверка живого
источника** — срез выше устарел и заново не снимался. Переносить его в статус текущего
состояния нельзя.

Прочее, что было известно **на тот момент** (всё три пункта устранены или изменены
в Stage 3.0.3B / 3.1A — см. блок Stage 3.1A выше):

* ~~исторический контракт себестоимости неполон~~ — восстановлен, 17 интервалов;
* ~~COGS не довезён в BigQuery production~~ — `evetis_ref` создан и запушен (`f8ea31e`);
* историческую прибыль по-прежнему считать нельзя, но причина другая: Product COGS
  есть и заморожен, однако он не подключён к витринам, а слоя расходов фулфилмента
  и налоговой модели не существует.

## UNRESOLVED

Закрыто в Stage 3.0.3 / 3.0.3B / 3.1A:

* ~~историчность себестоимости~~ — 17 интервалов с датами и lineage;
* ~~правило расчёта себестоимости наборов~~ — derived из компонентов, сборка отдельно;
* ~~источник истины для текущей и исторической себестоимости~~ — `evetis_ref`.

Остаётся открытым:

* правила возврата себестоимости при возврате товара;
* отнесение расходов уровня счёта (решение: по SKU **не** распределять);
* слой расходов фулфилмента (`ff_cost_rub`) — не спроектирован;
* налоговая модель;
* разложение landed-cost (закупка в CNY, курс, логистика, таможня) — первичных
  документов по контрактам RU01–RU04 в системе нет;
* подключение Product COGS к витринам и дашбордам.

🔴 Значения Product COGS больше не provisional: они подтверждены владельцем и
заморожены контрактом. Provisional остаются **даты** реконструированных границ —
они помечены `is_reconstructed = TRUE` вместе с окнами.

---

# Cohort Conversion — Deferred

* `orders_gross_qty` считается по **дате заказа**;
* `buyouts_qty` считается по **дате продажи**;
* поэтому обычное деление одного на другое запрещено;
* корректный buyout rate требует привязки каждой продажи к исходному заказу;
* `FACT_SALES` такой связи (`order_date`) сейчас не несёт;
* production-контракта для когорты нет.

Проектировать его сейчас не нужно — это отдельная задача с собственным согласованием.

---

# Metabase State & Recovery

Проверено 2026-08-28:

| параметр | значение |
| --- | --- |
| version | **v0.63.15.1** (OSS, 2026-08-25, `ab5ffba`) |
| container | `metabase`, image `metabase/metabase:latest` |
| Docker volume | `metabase-data` → `/metabase-data` (named volume) |
| application DB | **H2**, `MB_DB_FILE=/metabase-data/metabase.db` |
| подключение к данным | database id 2 «EVETIS BigQuery» (`bigquery-cloud-sdk`) |
| collections | 5 «EVETIS Analytics», 6 «01 Executive», 7 «02 SKU Performance» |
| dashboards | 2 Executive, 3 SKU Performance |
| cards | 40–71, все 32 на месте; 51/53/54 переключены на исправленный финансовый слой (Stage 3.1C PR3) |
| snapshot в Git | `metabase/`, `baseline_commit` = `889fcfad1faf8c95059cc5218c22060bcb66c81a`, sha256 всех 37 файлов сверены |

## Два независимых слоя восстановления

**1. Physical backup — копия application DB, вне Git.**

```
~/Backups/evetis-metabase/metabase-h2-20260827-112124.tar.gz   1 147 213 Б
sha256  fa5285cc8e32b3463a18b823e060ad30ec2c82b2cb5d2e265e70727cdbcbb6f2
```

Снята при остановленном контейнере (H2 нельзя копировать под записью).
Восстанавливает Metabase целиком: объекты, пользователей, права, настройки.

🔴 **Этот архив — чувствительный инфраструктурный артефакт.** Он содержит учётные записи
пользователей, права доступа и настройки инстанса, поэтому его **нельзя коммитить в Git
и нельзя публиковать** — ни в репозитории, ни в переписке, ни в облачных папках общего
доступа. Место хранения — только локальный каталог вне репозитория. Версионируемым
слоем восстановления служит `metabase/` с определениями, где ничего подобного нет.

**2. Logical snapshot — определения в GitHub.**

Каталог `metabase/` в репозитории: 3 коллекции, 2 дашборда, 32 карточки, `manifest.json`
с ID, связями и sha256 каждого файла, `README.md` с runbook. Фактический состав совпал
с ожидаемым; подгонки не потребовалось.

🔴 Автоматического импорта в OSS **нет** — проверено: `/api/ee/serialization/export`
отдаёт HTTP 404, `mb git-sync` недоступен, команд import у CLI нет.
Поэтому JSON — источник для реконструкции, а **не** восстановление одной командой:
при пересборке ID присвоит сервер заново, и `parameter_mappings` вместе с числовыми
ID полей в field-фильтрах придётся переотображать. Подробности — `metabase/README.md`.

## Ограничение

H2 — **временное решение**. Файловая БД уязвима к повреждению при некорректном завершении
и не поддерживает онлайн-копирование. Долгосрочно application DB полезно вынести
в PostgreSQL ради `pg_dump` без простоя. **Миграцию сейчас не выполняем** — она меняет
способ запуска контейнера и требует отдельного согласования.

---

# Claude Code Operating Context

Активные project skills (все 7 под версионным контролем, `.claude/skills/*/SKILL.md`):

`evetis-analytics-engineer` · `evetis-bigquery-sql` · `evetis-data-profile` ·
`evetis-data-validation` · `evetis-looker-studio` · `evetis-pipeline-engineer` ·
`evetis-wb-domain`

Замечание: `evetis-looker-studio` описывает Looker Studio, тогда как система работает
на Metabase. Скилл сохранён как исторический; переименование или адаптация — отдельная задача.

## Чем отличаются источники контекста

| источник | что это | авторитетность |
| --- | --- | --- |
| `CLAUDE.md` | правила работы над проектом, читается автоматически | **авторитетный**, в git |
| `.claude/skills/` | предметные инструкции по слоям и SQL | **авторитетный**, в git |
| git history + `docs/` + `CHANGELOG.md` | что и почему сделано, с доказательствами | **авторитетный**, в git |
| этот файл | человекочитаемый чекпоинт состояния | **авторитетный**, в git |
| session memory Claude | заметки между сессиями, вне репозитория | **вспомогательный, не авторитетный** |

🔴 **Не полагайтесь на внутреннюю память сессии.** Её перенос между сессиями не гарантирован,
она не версионируется и на GitHub не попадает. Файлы памяти, если существуют, лежат вне
репозитория (`~/.claude/projects/…/memory/`) и являются **вторичным, неавторитетным**
источником.

Принцип: **репозиторий должен быть достаточен для восстановления проекта даже при полной
потере памяти предыдущей сессии.** Этот файл — основной чекпоинт для такого восстановления.

---

# Rescue & Historical Work

## `rescue/stash-20260826`

Ветка существует локально и на GitHub, указывает на `d38f620` — объект `stash@{0}`
от 26.08.2026 11:47.

🔴 Это **исторический снимок; ветка не предназначена для merge или rebase в `main`**.
Её база — коммит `b018cb3`, который **не содержит ни одного** из коммитов
Stage 1.5/1.8/1.9/1.10B/Stage 2. Объединение способно повторно внести устаревшие версии
файлов и породить конфликты, разрешаемые вручную и с риском потерять принятые контракты.
Правильный способ работы с ней — **только выборочное извлечение отдельных файлов**
(selective extraction), а не слияние истории.

Переносить из неё нельзя:

* `sql/dash/dashboard_contract_v2.sql` — версия из стэша на 65 строк короче и **не содержит**
  `orders_gross_qty` и `period_result_pre_cogs_rub`, то есть откатила бы Stage 1.9 и 1.10B;
* `.gitignore` — отличается только текстом комментария, правило `.playwright-mcp/` в `main` есть.

Уже восстановлено в `main`: `docs/STAGE4A_PR1_ROLLOUT_2026-08-20.md` (коммит `05e0303`) —
он чинил две битые ссылки, одна из которых в `sql/dash/dashboard_contract_v2.sql`.

Остальные исторические файлы **не переносились** в `main` и ждут отдельного решения
владельца — все пути ниже существуют **только внутри ветки `rescue/stash-20260826`**,
в `main` их нет:

* КИЗ-контур: `docs/CHZ_TRUE_API_2026-08-24.md`, `docs/KIZ_RECON_DESIGN_2026-08-24.md`,
  три загрузчика `apps-script/Wb*.gs`;
* спеки Stage 4B Executive и CRATIO;
* расширенный пакет Looker-скилла (7 файлов);
* файлы Ozon и CSV состава наборов.

Читать их можно так, без переключения ветки:
`git show rescue/stash-20260826:<путь>`.

Локальный `git stash` содержит 6 записей и намеренно не удалялся — страховка задвоена.

## Ветка `docs/stage4b-advertising-build-spec`

Ветка `3d34af0` существует **только локально**, на GitHub её нет.
Содержит файл `docs/STAGE4B_ADVERTISING_BUILD_SPEC_2026-08-26.md` (16 997 Б),
которого в `main` нет — читать через `git show 3d34af0:docs/STAGE4B_ADVERTISING_BUILD_SPEC_2026-08-26.md`.

Статус: **design-only / current-with-edits**, не production.

Что в ней актуально: источник — существующий `V_DASH_SKU_DAILY`, новый вью сознательно
не создаётся; ratio только как отношение сумм; зафиксировано, что ROAS ≠ 1/ДРР.
Запрет на конверсию выкупа она не нарушает.

Что требует правок: документ написан до Stage 1.10B и использует `orders_qty` (нетто)
под подписью «Заказы», тогда как принятая семантика — `orders_gross_qty`; и он целится
в Looker Studio, а система работает на Metabase.

---

# Known Gaps / Deferred Work

| # | пробел | STATUS | почему не сделано | блокер | следующий безопасный шаг |
| --- | --- | --- | --- | --- | --- |
| 1 | Историческая себестоимость (COGS) | **закрыто** (Stage 3.0.3B + 3.1A, `f8ea31e`) | контракт восстановлен форензикой и вынесен в `evetis_ref` | — | подключение к витринам — отдельный gate |
| 2 | Правило себестоимости наборов | **закрыто** | derived из Product COGS компонентов на дату; сборка — отдельный слой | — | действий не требуется |
| 3 | Реальная прибыль и маржа | заблокировано | Product COGS есть, но не подключён; нет слоя расходов ФФ и налоговой модели | подключение COGS к витринам, `ff_cost_rub`, налоговая модель | не считать до их закрытия |
| 3a | Подключение Product COGS к MART / V_DASH / Metabase | не начато | Stage 3.1A сознательно остановлен на справочном слое | отдельный design/read-only gate + ACK владельца | спроектировать потребление, не трогая контракт |
| 3b | Слой расходов фулфилмента (`ff_cost_rub`) | не начато | ledger ФФ отсутствует, лист `FULFILLMENT` пуст | выгрузка операций ФФ | оценить объём отдельно |
| 4 | Stage 3B Phase B (billed ads по SKU) | подготовлено, не раскатано | сознательный fail-closed гейт | cutover `V_ADV_COSTS` → snapshot | отдельный этап с ACK |
| 5 | Cohort buyout conversion | отложено | `FACT_SALES` не несёт `order_date` | изменение FACT-слоя | проектировать отдельно |
| 6 | Metabase → PostgreSQL | не начато | H2 работает, миграция меняет запуск | согласование простоя | оценить объём отдельно |
| 7 | Память проекта вне git | частично | session memory не версионируется | — | этот файл закрывает основную часть риска |
| 8 | Старые битые ссылки в документации | 9 штук | давние, к текущей работе не относятся | — | чинить при касании файлов |
| 9 | Пороги блока «Требуют внимания» | не заданы | пороги не выдумываются | решение владельца | распределения уже замерены |
| 10 | Отставание метаданных Metabase | **устранено** | пересинхронизировалось при рестарте 27.08 | — | действий не требуется |

Ни один пункт не является обязательным следующим шагом сам по себе — зависимости
указаны явно, и порядок определяет владелец.

---

# Non-Negotiable Semantic Rules

* unknown ≠ zero;
* ratios = ratio-of-sums, не среднее посуточных отношений;
* gross orders ≠ net orders;
* отмены — отдельная величина, не поправка к заказам;
* order-date ≠ sale-date ≠ finance-date;
* seller-base revenue ≠ buyer-paid revenue;
* net settlement ≠ поступление на счёт;
* attributed ad spend ≠ billed ad spend;
* расход уровня счёта ≠ расход уровня SKU;
* contribution pre-COGS ≠ прибыль;
* period result pre-COGS ≠ прибыль;
* COGS обязана быть исторически корректной;
* себестоимость наборов нельзя выдумать;
* Product COGS ≠ расходы фулфилмента — это разные слои, неявно они не складываются;
* Product COGS набора = сумма компонентов на дату; частичная сумма запрещена,
  нерешаемый компонент даёт NULL, а не ноль;
* management-граница ≠ доказанная физическая дата смены партии;
* ключ COGS-контракта — `internal_sku`; `nm_id` маркетплейса ключом не является;
* legacy-значения себестоимости — audit lineage, а не источник расчёта;
* Stage 3B не обходит свой гейт;
* никаких молчаливых подстановок значений по умолчанию;
* deferred-код не раскатывается из `main` без исполняемого гейта;
* сверка repo ↔ production до любого выката;
* принятое изменение обязано быть закоммичено и запушено.

---

# Current Stage Status & Next Step

## CLOSED

* **Stage 3.0.3 / 3.0.3B** — форензика исторической себестоимости и нормализованный
  контракт Product COGS;
* **Stage 3.1A** — Product COGS Reference Layer, коммит `f8ea31e`;
* **Stage 3.1B** — COGS Consumer Layer, коммит `7c74cde`;
* **Stage 3.1C PR1** — Advertising Billing Classification, коммит `9ada18b`;
* **Stage 3.1C PR2** — Executive Financial Semantics Correction, коммит `889fcfa`;
* **Stage 3.1C PR3** — Metabase Executive Integration, 2026-08-28.

## CURRENT PRODUCTION STATE

**Executive использует исправленную финансовую семантику.** Карточки 51 / 53 / 54
читают `wb_mart.V_DASH_FINANCE_CORRECTED_DAILY`: реклама не вычитается дважды,
проценты считаются ratio-of-sums по выровненному знаменателю. Контрольные величины на
01–26.08.2026 — результат периода 90 344,47 ₽, рентабельность 26,93 %.

`evetis_ref` является **production reference source** для Product COGS, а Stage 3.1B
дал consumer-view `V_FACT_FINANCE_COGS` и `V_MART_SKU_DAILY_COGS`.

🔴 Но **Product COGS ещё НЕ входит в Executive**. Его не читают:

* `MART_SKU_DAILY`;
* `V_DASH_KPI_DAILY` и `V_DASH_FINANCE_CORRECTED_DAILY`;
* карточки Metabase.

Поэтому все показатели дашбордов остаются **до себестоимости товара**, и это не прибыль.

**Stage 3B — не начат.** Остаётся отдельным gated-направлением со своим исполняемым
гейтом (`sql/mart/pr_mart1_facts.sql:78`) и автоматически не запускается.

## NEXT

Следующий бизнес-этап после этого чекпоинта — **подключение Product COGS к
управленческой экономике и к Executive**: как витрина берёт цену на дату, как ведёт
себя fail-closed при отсутствии интервала, какие метрики после COGS появляются и как
они называются.

Этап должен начинаться **отдельным design / read-only gate**. Этот чекпоинт его не
открывает, Stage 3B не начинает и новых production-изменений не вносит.
