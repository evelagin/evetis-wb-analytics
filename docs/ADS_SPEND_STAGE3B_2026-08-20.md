# Stage 3B — семантика рекламного расхода: биллинг против атрибуции

**Дата:** 20.08.2026, ревизия 2 (по ревью) · **Статус:** в production НЕ раскатан · **Verdict:** `BLOCKED`

**Что изменилось в ревизии 2**
1. Денежная колонка `ad_spend_unallocated_rub` из `MART_SKU_DAILY` **убрана**. Вместо неё —
   булево `has_unallocated_billing`; сами рубли живут только в reconciliation-объектах,
   где биллинг раскладывается на три взаимоисключающие части.
2. Контракт `billed_complete` **опровергнут данными** и вынесен во внешний coverage-объект.
   Основание — `docs/ADS_COSTS_COVERAGE_CONTRACT_2026-08-20.md`. Пока контракта нет,
   change-set не раскатывается: это и есть причина BLOCKED.

---

## 1. Что решается

У рекламного расхода WB две несовпадающие величины, и обе верны:

| | Источник | Грейн | Смысл |
|---|---|---|---|
| **Биллинг** | `FACT_ADS_COSTS_DAILY.actual_spend_rub` | `(date, advert_id)` | что WB списал с баланса |
| **Атрибуция** | `FACT_ADS_SKU_DAILY.stats_spend_rub` | `(date, advert_id, nm_id)` | что WB отнёс на карточку в статистике |

До этого change-set витрина знала только вторую и называла её `ad_spend`. Экономические
показатели (ДРР, вклад) считались от атрибуции, то есть от величины, которой в бухгалтерии
не существует. На сборке 20.08 расхождение — **9 301,38 ₽ (1,81 %)**.

Change-set **не выбирает победителя**. Он делает обе величины наблюдаемыми, разводит их по
именам и переводит экономику на биллинг, оставив performance-метрики на атрибуции.

---

## 2. Baseline сборки 20.08 (снят до изменений)

| Объект | Строк | max(date) | Сумма |
|---|---|---|---|
| `MART_SKU_DAILY` | 7 302 | 2026-08-19 | `ad_spend` 523 365,38 · buyouts 3 419 298,40 · hybrid −7 777 098,96 |
| `FACT_ADS_SKU_DAILY` | 5 706 | 2026-08-19 | `stats_spend_rub` 523 365,38 (187 кампаний, с 13.04) |
| `FACT_ADS_COSTS_DAILY` | 1 816 | 2026-08-17 | `actual_spend_rub` 514 064,00 (107 кампаний, с 13.04) |

`mart_run_id` — единственный (сборка целостная). Отрицательных сумм нет ни в одном источнике.

**K9 до изменений** (для отката):

```
sp_bootstrap_facts        6eae969bbf4e9f808c95a3b661e089d4add7d29cc666cc37d8b9cd024af43adc   33714
sp_build_mart_sku_daily   81aef4117282efea8f234c472ab55b54cb3729fa59162761fe81660085fb796e   18306
V_ADS_FUNNEL_SKU_28D      008cca3b970397750607c73837b4076b334959e52c205e85f3b82483abb61b6c    3063
V_ADS_SCREEN_SKU          54c1378c6b01fb2e9ac033eb798acc77b9f9533242dbed52b49170c22e53bc86    1956
```

---

## 3. Состав change-set

| Файл | Что | Изменено строк |
|---|---|---|
| `sql/mart/pr_mart1_facts.sql` | +§1.7 `FACT_ADS_SPEND_ALLOC_DAILY`, +§1.8 `FACT_ADS_SPEND_UNALLOC_DAILY`, publish, физика | +176 / −3 (только комментарии «6 таблиц» → «8») |
| `sql/mart/pr_mart2b_sku_daily.sql` | +12 колонок `MART_SKU_DAILY`, +9 гейтов S1–S9 | +156 / −3 (roas/acos переизданы дословно + комментарий; строка `USING`) |
| `sql/mart/ads_spend_reconciliation_v1.sql` | **новый** — `V_ADS_SPEND_RECONCILIATION` + `..._DAILY` | 142 |
| `sql/mart/ads_spend_stage3b_validation.sql` | **новый** — приёмка V1–V12, только `SELECT` | 186 |
| `sql/mart/ads4_funnel_v1.sql` | `mart_ad_spend_rub` → `mart_ad_spend_attributed_rub` | +7 / −3 |
| `sql/mart/dashboard_layer_v1.sql` | то же имя в `V_ADS_SCREEN_SKU` | +1 / −1 |

**RAW и загрузчики не тронуты.** Существующие шесть FACT — не изменены ни на байт.

### 3.1 Правило распределения

Внутри `(date, advert_id)`:

```
billed_alloc_rub = actual_spend_rub × stats_sku / Σ stats_кампании
```

Если `Σ stats = 0` (или строк статистики нет) — сумма **не размазывается ни на кого** и
целиком уходит в `FACT_ADS_SPEND_UNALLOC_DAILY` с причиной `ZERO_STATS_SPEND` / `NO_STATS_ROWS`.

Построчного округления нет. Деление NUMERIC даёт scale 9, поэтому нано-рублёвый остаток
относится на **якорную строку** (максимальный `stats_spend_rub`, тай-брейк `nm_id ASC`).
Инвариант `Σ billed_alloc = actual` выполняется **точно по построению** — ASSERT проверяет
логику распределения, а не арифметику NUMERIC.

### 3.2 Новые колонки `MART_SKU_DAILY` (12)

`ad_spend_attributed_rub` · `ad_spend_billed_rub` · `billed_complete` ·
**`has_unallocated_billing`** · `billing_allocation_complete` · `roas_attributed` · `acos_attributed` ·
`drr_orders_billed` · `drr_buyouts_billed` · `roas_billed` ·
`hybrid_day_contribution_pre_cogs_billed` · `settlement_day_contribution_pre_cogs_billed`

🔴 Денежной колонки остатка в витрине НЕТ. Natural grain остатка — `(date, advert_id)` и сутки;
повторённая во всех строках дня сумма завышалась бы в `SUM()` по SKU ровно в число SKU.
Рубли — только в `V_ADS_SPEND_RECONCILIATION[_DAILY]`, где списание раскладывается так:

```
actual_spend_rub =  billed_valid_sku_rub             -- дошло до SKU витрины
                 +  billed_outside_sku_universe_rub  -- дошло до nm_id вне REF_SKU_MASTER
                 +  billed_no_allocation_basis_rub   -- делить было не на что (Σ stats = 0)
```

`ad_spend` и все существующие KPI — **не переименованы и не переопределены**, значения до
копейки прежние. Статус legacy/deprecated.

### 3.3 Два флага полноты

* **`billed_complete`** — закрыты ли сутки по биллингу. 🔴 Берётся **исключительно** из
  `wb_raw.V_ADV_COSTS_DAY_COVERAGE`; витрина этот флаг не вычисляет. Правило
  `day <= MAX(FACT_ADS_COSTS_DAILY.date)` опровергнуто — см. §6. Нет строки покрытия → `FALSE`.
* **`billing_allocation_complete`** — удалось ли распределить весь опубликованный биллинг
  до SKU (`unallocated = 0`). **NULL**, когда `billed_complete = FALSE`: распределять ещё
  нечего, и `TRUE` было бы ложным успехом.

На сутках с `billed_complete = FALSE` все billed-KPI строго **NULL, а не 0**. Ноль означал бы
«реклама была бесплатной»: ДРР этих суток схлопнулся бы в ноль, а вклад вырос бы на всю
сумму нераспределённого расхода.

---

## 4. Что показала проверка на реальных данных

Код прогнан read-only на сборке 20.08 (сами объекты не создавались; тела динамического SQL
выполнены как обычные `SELECT`).

**Распределение** — 3 237 пар `(date, advert_id)`:

```
списано WB          514 064,00
  распределено      512 997,00   (100 % внутри universe витрины)
  нераспределено      1 067,00   (2 пары, обе ZERO_STATS_SPEND)
остаток по каждой паре        0   ← ни одного исключения из 3 237
атрибуция           523 365,38
```

1 421 пара имеет атрибуцию без биллинга; пар с биллингом без атрибуции — **ноль**.

**Витрина** — десять гейтов S1, S4–S10 дали ноль нарушений (прогон ревизии 2, coverage-заглушка
как логический контроль): attributed построчно тождествен legacy · посуточная `SUM(billed)` равна
FACT-аллокации по universe · day-level флаги едины внутри суток · `billed_complete` — точная копия
coverage-объекта с fail-closed `IFNULL(...,FALSE)` · `billing_allocation_complete` = `NOT
has_unallocated_billing` на полных сутках и NULL на неполных · billed-KPI NULL на неполных сутках ·
витрина не несёт больше, чем WB списал · `has_unallocated_billing` соответствует остатку суток.
Суток с нераспределённым биллингом — 2.

**Сдвиг вклада.** Сравнение корректно только по суткам с `billed_complete`, иначе
сравнивались бы разные периоды:

```
hybrid_day_contribution_pre_cogs         (полные сутки)  −7 794 350,75
hybrid_day_contribution_pre_cogs_billed  (полные сутки)  −7 783 987,75
                                                  разница  +10 363,00
```

Вклад **растёт**: биллинг меньше атрибуции, значит вычитается меньше. ДРР по SKU,
наоборот, снижается. Направление одинаково для всех SKU и полностью объясняется
отношением `billed / attributed` — проверка V9/V10 в приёмочном скрипте требует, чтобы
`explain_gap` и `delta_check` были нулями у каждой строки.

---

## 5. Порядок раскатки — только после снятия BLOCKED

### 5.1 Исключение конкурентного прогона

`wb-mart-prod` идёт по расписанию `0 7,9,12,16 * * *` Europe/Moscow, прогон ~4 минуты, и он сам
вызывает `sp_bootstrap_facts` + `sp_build_mart_sku_daily`. Раскатка процедур в момент его работы
оставила бы сборку, часть которой построена старым кодом, а часть — новым.

Advisory-lock `_MART_BOOTSTRAP_LOCK` (`facts`, `mart_sku_daily`) защищает от одновременного
**исполнения**, но не от `CREATE OR REPLACE PROCEDURE` посреди чужого прогона. Поэтому lock —
вторая линия, а не основная.

**Основная — pause scheduler'а тем же рычагом, что и `wb-stocks-shadow`:**
`.github/workflows/scheduler-control.yml`, `loader = wb-mart`, `environment = prod`, `action = pause`.
Terraform держит `paused` под `ignore_changes`, поэтому pause через workflow не создаёт drift.

### 5.2 Атомарная последовательность

```
0. scheduler-control.yml → pause wb-mart-prod
1. убедиться, что прогон не идёт:
     SELECT lock_id, is_running FROM wb_mart._MART_BOOTSTRAP_LOCK;          -- оба FALSE
     SELECT status, started_at FROM wb_raw.LOADER_RUNS
      WHERE loader_name='mart' ORDER BY started_at DESC LIMIT 1;            -- COMPLETE
2. deploy sql/mart/pr_mart1_facts.sql  +  sql/mart/pr_mart2b_sku_daily.sql  -- ОДНИМ окном
3. CALL wb_mart.sp_bootstrap_facts('');
4. CALL wb_mart.sp_build_mart_sku_daily(
        DATE_SUB(CURRENT_DATE('Europe/Moscow'), INTERVAL 1 DAY), NULL, '');
5. deploy sql/mart/ads_spend_reconciliation_v1.sql        -- после того, как FACT созданы
6. deploy sql/mart/ads4_funnel_v1.sql, затем dashboard_layer_v1.sql  -- вторая читает первую
7. sql/mart/ads_spend_stage3b_validation.sql — V1–V12
8. scheduler-control.yml → resume wb-mart-prod
```

Шаг 2 неделим: новая `sp_build_mart_sku_daily` читает `FACT_ADS_SPEND_ALLOC_DAILY`, которой
до шага 3 не существует. Если раскатать только `pr_mart2b` — ночной прогон упадёт.
Шаги 3–4 обязаны идти подряд: между ними FACT новые, а витрина ещё старая.

**Откат:** восстановить два файла процедур из git и выполнить их (K9-хеши §2), затем
пере-bootstrap. Новые FACT-таблицы можно оставить — их никто не читает после отката
процедур, либо удалить отдельно. `V_ADS_SPEND_RECONCILIATION[_DAILY]` — `DROP VIEW`.
Ни одна существующая колонка не удалена, поэтому потребители отката не заметят.

---

## 6. Почему BLOCKED

Ревью потребовало доказать, что `billed_complete` отличает успешные нулевые сутки от незагруженных.
Доказательство искалось в `RAW_WB_ADV_COSTS` (129 суток, 37 ранов) и опровергло само правило:

* журнала ранов у `adv/v1/upd` **нет** — ран с пустым ответом не оставляет следа, поэтому
  «0 ₽» и «не загрузилось» неразличимы (живой случай: 18.08 и 19.08);
* WB отдаёт строки **вне** запрошенного окна, значит `period_from/period_to` — не утверждение о покрытии;
* биллинг **не финален на D+1 и не монотонен**: 02.08 менялся после семи одинаковых чтений подряд,
  06.08 дозаписался на D+2, 05.08 дал разовый выброс и откатился;
* дедуп по `(advertId, updTime, updSum)` не выражает отзыв записи → `FACT_ADS_COSTS_DAILY`
  завышен на **334,00 ₽** по 4 суткам против последнего ответа WB.

Минимальный coverage contract (журнал ранов → окно шире хвоста дозаписи → снимок последнего
полного ответа → вью `V_ADV_COSTS_DAY_COVERAGE`) описан в
`docs/ADS_COSTS_COVERAGE_CONTRACT_2026-08-20.md`. Зависимость в коде уже прописана: после
появления вью Stage 3B разблокируется без единой правки в MART.

---

## 7. Статус вопросов ревью

**R1 — закрыт.** Денежная колонка убрана из витрины, остался булев `has_unallocated_billing`.
Рубли — в reconciliation-объектах на их natural grain. Гейт S10 связывает флаг с суммой, чтобы
«всё распределено» не стало утверждением ни на чём не основанным.

**R2 — закрыт разложением.** Вместо одной величины «остаток» сверка отдаёт три
взаимоисключающие части (`billed_valid_sku_rub`, `billed_outside_sku_universe_rub`,
`billed_no_allocation_basis_rub`) с инвариантом `residual_rub = 0`. На 20.08:
512 997,00 + 0,00 + 1 067,00 = 514 064,00.

**R3 — принято.** Скользящие окна остались на атрибуции. `drr_buyouts_7d/14d`, `roas_7d/14d`,
`blended_cpo_7d/14d` считаются от legacy `ad_spend`. Billed-версии окон не добавлены:
NULL на неполных сутках протёк бы через окно и обнулил последние 7–14 значений у каждого
SKU. Billed-окна считать только при completeness ВСЕХ суток окна — то есть не раньше, чем
появится coverage-контракт. По той же причине нет `acos_billed` (обратная к `roas_billed`).

**R4 — APPROVED.** Единственное переименование в change-set — `mart_ad_spend_rub` →
`mart_ad_spend_attributed_rub` в `V_ADS_FUNNEL_SKU_28D` и `V_ADS_SCREEN_SKU`. Это колонка
вью, потребителей нет (Looker не построен). Величина не изменилась: доля запросов
по-прежнему считается от атрибуции, потому что query stats и campaign stats — один источник,
а деление на биллинг из другого разреза было бы подлогом.
