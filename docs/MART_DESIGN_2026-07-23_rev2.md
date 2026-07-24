# MART — дизайн-документ (rev2, после аудита)

**Проект:** EVETIS WB Analytics
**Дата:** 2026-07-23 (rev2)
**Статус:** ЧЕРНОВИК. Аудит ChatGPT: APPROVE WITH CHANGES. rev2 закрывает все
4 блокера + правки 5–10 + ответы §9. Кода по-прежнему нет; ни один объект в BQ
не создаётся до визы владельца на rev2.
**Предыдущая версия:** `docs/MART_DESIGN_2026-07-23.md` (rev1).

Правило проекта: перед кодом — что / зачем / риск / контрольные цифры; ключи
доказываем эмпирически. Все числа получены read-only из BQ `wb_raw` 2026-07-23.

---

## 0. Что изменилось против rev1 (короткий diff для аудитора)

| # | Замечание | Как закрыто в rev2 |
|---|---|---|
| Блокер 1 | Реклама смешивала грейны | Разбита на `FACT_ADS_SKU_DAILY` (date×advert×nm) и `FACT_ADS_COSTS_DAILY` (date×advert). Нераспределённый расход — только в `MART_ACCOUNT_DAILY`, не в каждой SKU-строке. |
| Блокер 2 | Нетоварные финансы в якоре | Введена `MART_ACCOUNT_DAILY` (грейн day × cost_category). `MART_SKU_DAILY` — только SKU-метрики. Псевдо-nm_id=0 отменён. |
| Блокер 3 | rrd_id как PK не доказан | **Probe выполнен** (см. §2.4): rrd_id уникален, но ключ факта = `finance_row_key = report_id#rrd_id` (составной, стоит 0, безопаснее). |
| Блокер 4 | Поочерёдный CREATE OR REPLACE | Введён паттерн `__BUILD` → validation → атомарный publish; `mart_run_id`/`built_at`; дашборд видит только последний COMPLETE. |
| Правка 5 | Смысл даты | `day` = дата события своего источника (не единая). Поля `for_pay` разделены: `sales_for_pay_operational` / `finance_for_pay_accounting`. |
| Правка 6 | Возвраты | Две раздельные метрики (операционная из SALES, финансовая из FINANCE), не суммируются. |
| Правка 7 | Снапшот дня | **Probe:** 16.07 нашлось 2 COMPLETE-снапшота. Берём последний COMPLETE дня по `snapshot_ts`, затем сумма складов. TZ = Москва. |
| Правка 8 | SAFE_CAST прячет ошибки | Parse-QC на каждый FACT (`source_nonempty`/`parsed_null`); build не публикуется при нарушении. |
| Правка 9 | Физика таблиц | Заданы partition/cluster (см. §7). |
| Правка 10 | REF переписывает историю | Явно: атрибуты SKU = current-state enrichment; nm_id — стабильный ключ; SCD2 — позже. |
| §9.3 | Расписание | Триггер сдвинут на ~09:00 МСК + freshness-gate по апстриму. |

**Итог модели:** было 5 фактов + 1 якорь. Стало **6 фактов + 2 витрины + run-log**
(+2 таблицы), зато без смешения грейнов и двойного счёта.

---

## 1. Что обнаружено (факты из BQ)

### 1.1. Покрытие входов

| Источник | Вьюха | Даты | Строк |
|---|---|---|---|
| Заказы | `V_WB_ORDERS` | 2026-04-13 → 2026-07-23 | 3 719 |
| Продажи/возвраты | `V_WB_SALES_RETURNS` | 2026-03-30 → 2026-07-23 | 3 499 |
| Финансы (канон) | `V_WB_FINANCE_CANONICAL` | **2024-09-05** → 2026-07-22 | 202 174 |
| Реклама (по nm) | `V_ADV_CAMPAIGN_STATS` | 2026-04-13 → 2026-07-22 | 8 489 |
| Реклама (расход) | `V_ADV_COSTS` | — | по advertId, без nm |
| Остатки | `V_WB_STOCKS_CURRENT` / `WB_STOCKS_SNAPSHOTS` | текущий + история | 148 (тек.) |

**Асимметрия покрытия:** деньги с сентября 2024, операционка (заказы/продажи/
реклама) — только ~90 дней (лимит WB API). Ось дней MART = UNION дат фактов;
где источника нет — NULL/0. Не баг, а природа источников.

### 1.2. Сопоставление SKU

Заказы/продажи/остатки ~99 % matched. Финансы 77 % matched — 23 % без nm_id это
нетоварные операции (логистика/хранение/удержания/штрафы/приёмка/эквайринг) и
исторические nm_id. → эти строки идут в `MART_ACCOUNT_DAILY`, не в якорь по SKU.

### 1.3. Реклама: costs — истина, stats — единственный поартикульный сигнал

`V_ADV_COSTS` без `nmId`; `V_ADV_CAMPAIGN_STATS` c `nmId`, но на окне 30 дней его
`sum` покрывает лишь **65,8 %** реального расхода (103 910 против 157 875 ₽; на
90 днях ~93 %). CLAUDE.md запрещает рекламу как точную поартикульную себестоимость.
→ два факта (§2.5–2.6), нераспределённый расход — явной строкой в
`MART_ACCOUNT_DAILY`.

### 1.4. Probe ключа финансов (Блокер 3) — ВЫПОЛНЕН

```
rows_count = 202174 | distinct(rrd_id) = 202174
distinct(report_id#rrd_id) = 202174 | rrd_id NULL = 0
```
Вывод: в canonical `rrd_id` сегодня глобально уникален. Но `report_id#rrd_id`
даёт тот же счёт → составной ключ стоит 0 и защищает от будущих пересечений
(DAILY/WEEKLY, type 1/2, legacy). **Ключ FACT_FINANCE = `finance_row_key =
CONCAT(report_id,'#',rrd_id)`.**

### 1.5. Probe снапшотов остатков (правка 7) — ВЫПОЛНЕН

Найден день **2026-07-16 с 2 COMPLETE-снапшотами**. → нельзя просто агрегировать
все COMPLETE-строки дня. Алгоритм §2.3.

### 1.6. Типы

Деньги в finance/orders/ads — STRING (возможна запятая) → нормализация
`SAFE_CAST(REPLACE(x, ',', '.') AS NUMERIC)` с parse-QC (§6). nm_id → `INT64`.

---

## 2. Целевая модель (rev2)

Шесть фактов + две витрины + run-log в датасете **`wb_mart`**.

### 2.1. FACT_ORDERS
Источник `V_WB_ORDERS` (дедуп srid). Грейн: 1 заказ (srid). Дата: `order_date`.
Поля: `nm_id INT64`, `internal_sku`, `sku_match_status`, `price_with_disc NUMERIC`,
`quantity INT64`, `is_cancel`, `cancel_dt`, склад/регион.

### 2.2. FACT_SALES
Источник `V_WB_SALES_RETURNS` (дедуп sale_id). Грейн: 1 продажа/возврат (sale_id).
Дата: `sale_date`. Поля: `nm_id`, `internal_sku`, `is_return`, `is_realization`,
`operation_type`, суммы NUMERIC, `sales_for_pay_operational` (переименован из
`for_pay`), склад/тип/регион.

### 2.3. FACT_STOCKS_SNAPSHOT
Источник: `RAW_WB_STOCKS` × COMPLETE-снапшоты `WB_STOCKS_SNAPSHOTS`
(НЕ `V_WB_STOCKS_CURRENT`). Грейн: snapshot_date × nm_id × warehouse_id.
**Выбор снапшота дня** (правка 7):
```sql
QUALIFY ROW_NUMBER() OVER (PARTITION BY snapshot_date ORDER BY snapshot_ts DESC) = 1
-- сначала выбрать ОДИН snapshot_id дня, затем брать все его строки и суммировать склады.
-- НЕ делать ROW_NUMBER по SKU — иначе день соберёт строки из разных запусков.
```
`snapshot_date` — по московскому времени. Для якоря сворачиваем к day × nm_id
(остаток на конец дня = сумма по складам последнего снапшота дня).

### 2.4. FACT_FINANCE
Источник: **только** `V_WB_FINANCE_CANONICAL`. Грейн: строка отчёта, ключ
`finance_row_key = report_id#rrd_id` (§1.4). Обязательно: `source_layer`,
`finance_status`, `operation_type_normalized`, `week_start`, `report_type`.
Дата: `finance_date` = `_rr_date`. Деньги (STRING→NUMERIC): `sale_amount`,
`return_amount_rub`, `finance_for_pay_accounting` (переим. из `for_pay`),
`commission_amount`, `logistics_amount`, `storage_fee`, `deduction`, `penalty`,
`acceptance`, `acquiring_fee`, `additional_payment`, `compensation_amount`.
Строки без nm_id сохраняем (нужны для `MART_ACCOUNT_DAILY`).

### 2.5. FACT_ADS_SKU_DAILY (новый, Блокер 1)
Источник `V_ADV_CAMPAIGN_STATS`. Грейн: `date × advert_id × nm_id`.
Метрики: `views`, `clicks`, `orders`, `stats_spend_rub` (из `sum`).
Это поартикульный **директивный** сигнал, НЕ точная себестоимость.

### 2.6. FACT_ADS_COSTS_DAILY (новый, Блокер 1)
Источник `V_ADV_COSTS`. Грейн: `date × advert_id` (агрегируем updNum-строки дня).
Метрика: `actual_spend_rub`. Это истина по сумме расхода.
Производная: `unattributed_spend(date) = Σ actual_spend − Σ stats_spend` по дню/
кампании → идёт в `MART_ACCOUNT_DAILY` (не в SKU-строки).

### 2.7. MART_SKU_DAILY (якорь, только SKU)
Грейн: **day × nm_id** (+ атрибуты SKU из `REF_SKU_MASTER`, LEFT JOIN,
current-state enrichment — правка 10). Несопоставленный nm — метка
«не сопоставлено», строки не выкидываем. `day` = дата события своего источника
(правка 5). Метрики v1:
- заказы: `orders_qty`, `orders_rub` (order_date);
- выкупы: `buyouts_qty`, `buyouts_rub` (sale_date, is_return=false);
- возвраты операционные: `returns_qty`, `returns_retail_rub` (FACT_SALES);
- возвраты финансовые: `finance_return_amount_rub` (FACT_FINANCE) — **отдельно**,
  не суммировать с операционными (правка 6);
- реклама атрибутированная: `ads_stats_spend_rub` (по nm);
- финансы по SKU: `commission_rub`, `logistics_rub` (только nm-строки),
  `finance_for_pay_accounting`;
- остаток: `stock_qty_eod`.

**Прибыль/маржа = НЕ считаем в v1** (см. §4). ДРР как расход/выручка — можно.

### 2.8. MART_ACCOUNT_DAILY (новый, Блокер 2)
Грейн: **day × cost_category**. Сюда — всё, что не относится к SKU:
- финансовые суммы без nm_id (хранение, удержания, штрафы, приёмка, эквайринг);
- нераспределённый рекламный расход `unattributed_spend`;
- прочие account-level начисления.
Дневной итог бизнеса = `Σ MART_SKU_DAILY + MART_ACCOUNT_DAILY`. Псевдо-SKU «не
распределено» внутри якоря — отменён. (Позже опционально presentation-view с
`entity_type IN ('SKU','ACCOUNT')`, но физически грейны не смешиваем.)

### 2.9. MART_LOADER_RUNS
Run-log: `mart_run_id`, `built_at`, стадии, rows, status (для publish-гейта §5).

---

## 3. Ключевые решения (подтверждены аудитом)

Датасет `wb_mart` (одобрено). Материализация таблицами full-refresh (одобрено,
но через BUILD→publish, §5). Финансы только из canonical со статусами. Реклама:
costs — истина, stats — директивно. PNL → v2. Возвраты — две метрики. Даты —
пособытийные. REF — current-state enrichment.

---

## 4. Границы v1 (PNL заблокирован) → v2

MART v1 = все ингредиенты прибыли, кроме прибыли. Блокеры: (1) нет себестоимости
в BQ (`COST_HISTORY` в Sheets, ждёт REF Sync PR2); (2) не решена база выручки
for_pay (§8 архитектуры, вариант A/B; данные для recon копятся). Наборы
(`is_bundle`) в v1 не раскладываем. Прибыль/маржа — MART v2 после PR2 + for_pay.

---

## 5. Материализация: BUILD → validation → publish (Блокер 4)

Каждый прогон:
1. Строит staging-набор `*__BUILD`: `FACT_ORDERS__BUILD`, `FACT_SALES__BUILD`,
   `FACT_STOCKS_SNAPSHOT__BUILD`, `FACT_FINANCE__BUILD`, `FACT_ADS_SKU_DAILY__BUILD`,
   `FACT_ADS_COSTS_DAILY__BUILD`, `MART_SKU_DAILY__BUILD`, `MART_ACCOUNT_DAILY__BUILD`.
   Во всех: `mart_run_id`, `built_at`.
2. Прогоняет проверки §8 на BUILD.
3. Только при успехе — атомарный publish `CREATE OR REPLACE TABLE prod AS SELECT *
   FROM build`. Порядок: FACT → MART_SKU_DAILY → MART_ACCOUNT_DAILY → run=COMPLETE.
4. До последнего шага дашборд видит предыдущий полностью согласованный MART.

Каждая отдельная замена атомарна; staging делает атомарным весь набор. Для v1
staging+validation+publish достаточно; presentation-view «по последнему COMPLETE
run» — при желании позже.

## 6. Parse-QC (правка 8)

Для каждого FACT на этапе BUILD:
```
source_nonempty = COUNTIF(source_value IS NOT NULL AND TRIM(source_value) <> '')
parsed_null     = COUNTIF(source_value IS NOT NULL AND TRIM(source_value) <> ''
                          AND SAFE_CAST(REPLACE(source_value,',','.') AS NUMERIC) IS NULL)
```
Приёмка: `parsed_null = 0` (или документированный допуск). Нарушение → build НЕ
публикуется, run=FAILED, алерт.

## 7. Партиционирование и кластеризация (правка 9)

| Таблица | Partition | Cluster |
|---|---|---|
| FACT_ORDERS | order_date | nm_id, internal_sku |
| FACT_SALES | sale_date | nm_id, is_return |
| FACT_FINANCE | finance_date | nm_id, finance_status, operation_type_normalized |
| FACT_STOCKS_SNAPSHOT | snapshot_date | nm_id, warehouse_id |
| FACT_ADS_SKU_DAILY | date | nm_id, advert_id |
| FACT_ADS_COSTS_DAILY | date | advert_id |
| MART_SKU_DAILY | day | nm_id, internal_sku |
| MART_ACCOUNT_DAILY | day | cost_category |

## 8. Контрольные цифры для приёмки

1. **Дедуп фактов:** rows(FACT_*) == distinct(ключ).
2. **Финансы-полнота:** Σ FACT_FINANCE (по метрике) == Σ V_WB_FINANCE_CANONICAL
   (тот же фильтр); строки без nm сохранены.
3. **Реклама-инвариант:** `Σ FACT_ADS_COSTS_DAILY ≥ Σ FACT_ADS_SKU_DAILY` по дню;
   `unattributed_spend ≥ 0`; полный расход = costs (не stats).
4. **Полнота денег:** `Σ MART_SKU_DAILY(fin) + Σ MART_ACCOUNT_DAILY == Σ FACT_FINANCE`
   (ничего не потеряно и не задвоено).
5. **Возвраты:** операционные (SALES) и финансовые (FINANCE) присутствуют как
   разные колонки; нет единой `returns_rub`.
6. **Parse-QC:** §6 = 0.
7. **Покрытие:** операционные метрики min day ≈ 2026-04-13; финансовые до 2024-09.
8. **Сверка с живой книгой** «Evetis аналитика 2.0»: заказы/выкупы контрольного
   дня совпадают.

## 9. Ответы на вопросы rev1 §9 (по аудиту)

1. Датасет — **`wb_mart`** (одобрено; в `wb_raw` не класть).
2. Full-refresh v1 — **да**, но через BUILD→validation→publish (§5).
3. Расписание — **~09:00 МСК** (запас апстриму) + **freshness-gate** перед build:
   последний Finance run OK/OK_NO_NEW; нет зависших загрузок; stocks-снапшот за
   ожидаемую дату есть; входные вьюхи доступны. Одного дневного прогона достаточно.
4. Возвраты — операционные из **FACT_SALES**, финансовые из **FACT_FINANCE**,
   раздельно, не суммируем.
5. Прибыль/маржа — **v2** (после COGS в BQ + revenue-contract + for_pay-recon).

## 10. Открытые вопросы к владельцу (rev2)

1. `cost_category` в `MART_ACCOUNT_DAILY` — по каким статьям резать (хранение,
   удержания, штрафы, приёмка, эквайринг, нераспр. реклама)? Утвердить список.
2. freshness-gate: считать ли отсутствие нового Finance-отчёта (OK_NO_NEW)
   поводом всё равно пересобрать MART (да — операционка/остатки свежие)?

## 11. План PR (после визы на rev2)

- **PR-Mart1:** датасет `wb_mart` + 6 `FACT_*` (BUILD-паттерн, типизация,
  parse-QC, partition/cluster, дедуп-тесты).
- **PR-Mart2:** `MART_SKU_DAILY` + `MART_ACCOUNT_DAILY` (джойны, «не сопоставлено»,
  раздельные возвраты, unattributed_spend, контрольные §8).
- **PR-Mart3:** `runWbMartDaily` + freshness-gate + `MART_LOADER_RUNS` + publish +
  установщик триггера ~09:00 (после приёмки и негативных тестов).
- **v2 (отдельно):** прибыль/маржа — после REF PR2 (себестоимость) и for_pay-recon.

_Код не пишется до визы владельца на rev2._
