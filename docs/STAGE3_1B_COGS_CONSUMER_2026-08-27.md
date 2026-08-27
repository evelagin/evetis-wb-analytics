# STAGE 3.1B PR1 — Product COGS Consumer Layer

**Дата:** 2026-08-27
**База:** HEAD `cd166e95a2269a1353a701a94cc5364219cd07b8`
**Маршрут:** R3 — consumer-layer, FACT-слой не изменяется
**Предшественник:** Stage 3.1A (`evetis_ref`: 17 / 33 / 21 / 38) — не изменён
**Скрипты:** `sql/mart/pr_cogs_consumer_v1.sql`, `sql/mart/pr_cogs_consumer_validation.sql`
**Откат:** `tools/stage3_1b_cogs_consumer_rollback.sh [--dry-run]`

---

## 1. Что это

Два `VIEW`, подключающие marketplace-independent слой себестоимости `evetis_ref`
к WB-контуру. Ни один существующий объект не изменён.

| объект | грейн | назначение |
| --- | --- | --- |
| `wb_mart.V_FACT_FINANCE_COGS` | `finance_row_key` (событие реализации) | разрешение Product COGS на событиях `FACT_FINANCE` |
| `wb_mart.V_MART_SKU_DAILY_COGS` | `day × nm_id` | overlay над `MART_SKU_DAILY`, соединяется по `(day, nm_id)` |

## 2. Почему consumer-слой, а не FACT

Аудит 27.08 показал: `MART_SKU_DAILY` считает выкупы из `FACT_SALES`, а источник
`RAW_WB_SALES_RETURNS` физически начинается **2026-03-30**. Финансовые события —
с 2024-09-07. Поэтому разрешение COGS на грейне витрины покрывает лишь ~11 %
единиц жизненного цикла, и Product COGS обязан разрешаться на событиях финансов.

Пронос `srid` в `FACT_FINANCE` (маршрут R2, OD-1 = B) заблокирован расхождением
`sql/mart/pr_mart1_facts.sql` ↔ production: файл в репозитории содержит §1.7
Stage 3B, которого нет в развёрнутой `sp_bootstrap_facts`. Публикация файла
создала бы объекты Stage 3B. Расхождение зарегистрировано отдельно —
`docs/TECHDEBT_FACT_DEPLOY_DRIFT_2026-08-27.md`. Поэтому `srid` берётся из
canonical-слоя на consumer-границе (маршрут R3, owner ACK).

## 3. Owner decisions, реализованные здесь

| # | решение | реализация |
| --- | --- | --- |
| OD-1 = B | возврат сторнирует Product COGS **исходной продажи**, найденной по `srid` | `cogs_effective_date` возврата = дата исходной продажи; `cogs_return_basis = 'ORIGINAL_SALE'` |
| OD-2 = C | две явно разделённые серии, не смешивать | `*_operational_*` (выкупы `FACT_SALES`) и `*_settlement_*` (события `FACT_FINANCE`) |
| OD-3 = B | отдельное COGS-покрытие | `cogs_covered`, `settlement_cogs_covered` + `cogs_covered_qty` / `cogs_uncovered_qty` |
| OD-4 = A | терминология | `contribution_after_product_cogs_rub`, `settlement_contribution_after_product_cogs_rub`. `gross_margin_*`, `profit`, `unit_profit`, `*_pct` — запрещены |

## 4. Fail-closed контракт

| статус | условие | последствие |
| --- | --- | --- |
| `RESOLVED` | ровно одно совпадение resolver | метрики вычисляются |
| `NOT_APPLICABLE` | в строке витрины нет COGS-требующих единиц | Product COGS = **0**: это отсутствие события, а не неизвестность. Число событий выведено отдельными колонками, поэтому ноль доказуем |
| `UNMAPPED_SKU` | `nm_id` вне `REF_SKU_MASTER` | все after-COGS метрики **NULL** |
| `UNKNOWN_NO_INTERVAL` | 0 совпадений | **NULL** |
| `RETURN_LINK_MISSING` / `RETURN_LINK_AMBIGUOUS` / `RETURN_LINK_UNRESOLVED` | исходная продажа не разрешается ровно в одну | **NULL** |
| `CONTRACT_VIOLATION_MULTI` | > 1 совпадения | **NULL** + FAIL валидации, выкат блокируется |

Запрещено: `COALESCE(cogs, 0)`, forward-fill, last known value, `legacy_cogs_rub`,
частичная сумма компонентов набора, неявное `product_cogs + ff_cost`.

## 5. Mapping канал → SKU

Только `JOIN` к актуальному `wb_raw.REF_SKU_MASTER` по `nm_id`.
Сохранённые `FACT_*.internal_sku` / `sku_match_status` **не используются**:
аудит нашёл 8 строк `FACT_SALES` (`nm_id 1083392113`) с `internal_sku IS NULL`
при живом маппинге. Проверено на выкате: `unmapped_events = 0`.
Исправление самих FACT-колонок — отдельный DQ-тикет, в этот PR не входит.

## 6. Acceptance snapshot на момент выката (2026-08-27)

🔴 **Это снимок, а НЕ постоянный инвариант.** `FACT_*` штатно обновляются
прогоном `wb-mart-prod`, поэтому абсолютные числа ниже устаревают by design.
Постоянные проверки в `pr_cogs_consumer_validation.sql` — динамические:
грейн сверяется с текущим источником, а суммы — с независимым пересчётом
на том же снимке.

| величина | значение |
| --- | ---: |
| `V_FACT_FINANCE_COGS` строк | 39 481 |
| из них `RESOLVED` | 39 481 (100,00 %) |
| нетто-единиц | 39 459 |
| событий «Возврат» | 11, все связаны ровно с одной продажей |
| **Product COGS, settlement** | **8 686 474,00 ₽** |
| `V_MART_SKU_DAILY_COGS` строк | 7 477 (1:1 с витриной) |
| из них `RESOLVED` / `NOT_APPLICABLE` | 1 531 / 5 946, непокрытых 0 |
| Product COGS operational (брутто) | 933 994,50 ₽ |
| реверс operational | 145,00 ₽ |
| **Product COGS operational (нетто)** | **933 849,50 ₽** |
| `contribution_after_product_cogs_rub` | −8 683 753,15 ₽ |
| `settlement_contribution_after_product_cogs_rub` | 9 697 006,64 ₽ |

### Эффект OD-1 = B — 4,50 ₽, ровно как предсказал forensic

Аудит 27.08 считал settlement Product COGS по семантике A (дата возврата) и
получил **8 686 478,50 ₽**. Реализованная семантика B даёт **8 686 474,00 ₽**.
Разница **4,50 ₽** — единственный возврат за всю историю, пересекающий границу
партии: 2025-01-29, `EVT-HC-HAND-300`, граница T1 (2025-01-26). Под A он
сторнировал 214,50 ₽, под B сторнирует 219,00 ₽ — себестоимость той партии,
из которой единица была продана 2025-01-22.

Это подтверждение работы контракта, а не расхождение.

## 7. Non-regression на выкате

| проверка | до | после |
| --- | ---: | ---: |
| `MART_SKU_DAILY` строк / дат / nm / колонок | 7 477 / 721 / 25 / 60 | идентично |
| `orders_qty` / `canceled_qty` / `buyouts_qty` / `returns_qty` | 4 031 / 395 / 4 102 / 1 | идентично |
| `buyouts_rub` / `marketplace_fee_rub` / `logistics` / `for_pay` | 3 497 248,80 / 7 990 858,46 / 2 726 147,30 / 18 913 627,33 | идентично |
| `hybrid` / `settlement` pre-COGS | −7 749 903,65 / 18 383 480,64 | идентично |
| `V_DASH_KPI_DAILY`: contribution / покрытых суток / settled revenue | 1 343 046,23 / 135 / 26 869 456,07 | идентично |
| `V_DASH_SKU_DAILY` строк | 7 477 | идентично |
| `evetis_ref` | 4 объекта, 17 / 33 / 21 / 38 | идентично |
| объектов `wb_mart` | 35 | **37** (+2 вью) |
| изменённых из 35 существующих объектов | — | **0** (`last_modified_time` и `row_count` совпали у всех) |

Суточная дельта источников (70 финансовых, 8 продажных, 12 заказных строк)
искусственно не поглощалась: её заберёт штатный прогон `wb-mart-prod`.

## 8. Structural baseline

`sql/ref/pr_ref_cogs_validation.sql`: константа `wb_mart` обновлена **35 → 37**
одновременно с созданием двух объектов. Это обслуживание СТРУКТУРНОГО baseline,
а не переоткрытие Stage 3.1A: экономические инварианты (17 / 33 / 21 / 38,
AC-6…AC-9) не изменены ни одним символом. Проверено: AC-10/AC-11 = PASS.

## 9. Чего этот PR не делает

Не изменяет `FACT_*`, `MART_SKU_DAILY`, `V_DASH_*`, процедуры, загрузчики,
Scheduler, Google Sheets, Apps Script. Не подключает метрики к Metabase —
карточки 40–71 читают только `V_DASH_*` и этих вью не видят. Не начинает
Stage 3B. Не переоткрывает Stage 3.0.3 / 3.0.3B / 3.1A.

## 10. Следующий шаг

Подключение after-COGS к презентационному слою — **отдельный PR**: новые
`V_DASH_*_COGS` (или аддитивные колонки), затем новые карточки Metabase и
metadata sync. До него числа на живых экранах не меняются (AC-13).
