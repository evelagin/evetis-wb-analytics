# Advertising Intelligence Engine — Design Gate V1

**Дата:** 2026-09-27 · **База:** `origin/main` `73bf86dd965cb15fd176f0bec06a0cb134a3e7f0` ·
**Статус:** Design Gate принят владельцем; реализация разрешена только для PR-0 → PR-5.
PR-6 (журнал, планировщик), PR-7 (наблюдение действий), PR-8 (Ozon) и любые рекламные write-пути
**не разрешены**.

## 0. Что это и чего здесь нет

Система ежедневно отвечает на вопрос: «что сегодня сделать с рекламой каждого SKU EVETIS на WB и Ozon,
почему и насколько уверенно». Это **движок рекомендаций**, а не автопилот.

Ответ по каждой паре `marketplace × internal_sku × campaign × marketplace_sku` — одно из состояний
`INCREASE / DECREASE / HOLD / PAUSE_CANDIDATE / INSUFFICIENT_DATA / BLOCKED_BY_GUARDRAIL`, плюс
основная причина, полный список причин и объяснение на русском.

Жёсткие границы V1:

- никаких вызовов рекламных API на запись, изменений ставок, бюджетов, кампаний, размещений, кластеров;
- никаких новых учётных данных с правом записи; **F-18 остаётся блокером любого write-пути**;
- **FIN CONTRACT V2**: атрибутированный расход ≠ биллинг. Движок работает только с атрибуцией
  (`FACT_ADS_SKU_DAILY.stats_spend_rub`), биллинг (`FACT_ADS_COSTS_DAILY`) не читает и по SKU не распределяет;
- метрики называются `PRE_COGS` / `AFTER_PRODUCT_COGS` / «вклад»; это не прибыль;
- отсутствующие данные не превращаются в ноль;
- Ozon не production-grade до PR-8.

Аудит, на котором основан дизайн: Advertising Data & Control Capability Audit от 2026-09-27
(в чате владельца; ключевые цифры перенесены в разделы ниже и в `quality/unresolved_business_rules.json`).

## 1. Решения владельца, зафиксированные для V1

| # | Решение | Как реализовано |
|---|---|---|
| P7 | **(финализация 2026-09-27)** Граница режима — относительное изменение `seller_effective_price` не меньше 3 %. Прежнее решение «любое изменение» заменено по итогам калибровки (медиана окна 5 → 11 суток, доля доказательных строк 21,6 → 24,5 %) | `evetis_ref.V_AIE_POLICY.p7_price_change_pct = 3.0`, единственное место значения; WB — `ABS(delta_seller_effective_price_pct) ≥ p7`, Ozon — `100·|Δprice_rub / prev| ≥ p7`; `NULL` = любое изменение. Проверка `AIE_W08` |
| K | **(2026-09-27)** Текущий набор рекомендаций — пары с активным статусом (WB 9/11, Ozon только RUNNING: семантика `INACTIVE` в проекте не доказана — исключён, fail closed, Commit/PR Gate 2026-09-27) и не больше 14 суток с последнего расхода (P90 перерывов расхода = 14). Остальные — историческая база доказательств: не удаляются, `universe = HISTORICAL_EVIDENCE_ONLY`, основная причина `SCOPE_CAMPAIGN_INACTIVE`. Кампания с расходом в день перехода в статус 7 сохраняет день в истории, но текущих рекомендаций не порождает | `V_AIE_POLICY.k_inactive_days = 14`; `last_spend_date`, `days_since_last_spend` в доказательной базе; колонка `universe`; проверка `AIE_D12` |
| P3 | **(2026-09-27)** 0.90 для Shadow V1; не неизменяемая политика будущего автопилота | `V_AIE_POLICY.p3_confidence = 0.90` |
| P4 | **(2026-09-27)** Исключений по SKU нет. Отрицательная прогнозная base-экономика до рекламы → `PAUSE_CANDIDATE` с основной причиной `PRICE_BELOW_BREAKEVEN`, разложение цены и маршрут `PRICING_REVIEW`; цена не меняется, pricing write API не вызывается | код 501, колонки `current_effective_price_rub`, `breakeven_price_rub`, `contribution_before_ads_rub`, `commission/acquiring/logistics/product_cogs_{rub,pct}`, `routing`; проверка `AIE_D13` |
| P6 | **(2026-09-27)** Роли баз: realized — граница доказательности DECREASE; forward base — текущий структурный и ценовой ограничитель; forward conservative — ограничитель против INCREASE; forward stress — флаг риска. Прогнозная экономика в историческом replay не используется | `limit_drr = realized`; `conservative_guard` + `ECON_CONSERVATIVE_GUARD_BLOCKS_INCREASE`; `ECON_STRESS_RISK_FLAG` |
| P15 | **(2026-09-27)** 0,7 / 1,3 — только диагностика запросов; направление ими не определяется | `V_AIE_WB_QUERY_CLASS.is_bid_direction_source = FALSE` |
| Финансы | **(2026-09-27)** Искусственного исключения N дней нет; явный контракт `FINANCIAL_DATA_MATURE`: конец фактического окна не позже последней даты с финансами | `V_AIE_WB_ECON_GUARD.financial_data_mature`, `econ_window_end_lag_days`, контекст `FIN_WINDOW_ENDS_AT_FINANCE`; проверка `AIE_D14`. Утренний дефект `V_ADS_SKU_ECONOMIC_LIMITS` — вне AIE, `TECH_DEBT.md` P2-10 |
| P9 | Кампании Ozon под `TARGET_BIDS` / `TARGET_CIR` неуправляемы в V1 | `BLOCKED_BY_GUARDRAIL`, причина `CTRL_OZON_AUTOPILOT`; изменение CPC не рекомендуется |
| P10 | Статус WB 11 сам по себе не означает отсутствие активности | активность дня = фактический расход; статус хранится как контекст `CTX_WB_STATUS_PAUSED_WITH_SPEND` |
| P11 | `proposed_delta = NULL`, `delta_basis = DELTA_NOT_DERIVABLE` | константы в `V_AIE_DECISION_CURRENT` |
| P12 | Будущий журнал — в `evetis_ref` | в PR-0…PR-5 журнал **не создаётся** |
| P14 | `include_in_ads_analysis` не фильтр | контекст `CTX_INCLUDE_IN_ADS_ANALYSIS_FALSE`; семантика — UBR-022 |

Не решены (`quality/unresolved_business_rules.json`): P1 целевой ДРР (UBR-013), P2 гистерезис
(UBR-014; 0 п.п. — только в исследовательском replay), P5 нижняя и верхняя границы покрытия (UBR-017),
P8 минимальный расход (UBR-019), P13 период ожидания (UBR-020, отложен), семантика P14 (UBR-022).
В `V_AIE_POLICY` они `NULL`.

**Пока P1 IS NULL — инвариант `COUNT(recommendation = 'INCREASE') = 0`.** Кандидат на повышение виден
только как диагностика: флаг `increase_candidate` (интервал ниже фактической границы с доверием P3 и, в
текущем режиме, ниже осторожной прогнозной базы) и контекстный код `INCREASE_CANDIDATE`; состояние строки
при этом `HOLD` / `POLICY_P1_NOT_SET`. Скрытых значений по умолчанию нет.

**Запас (P5).** Состояния покрытия: `INV_COVER_UNAVAILABLE` (наборы — всегда, пока нет покрытия с учётом
компонентов; нет снимка покрытия, в replay — до 2026-09-10), `INV_COVER_POLICY_NOT_SET` (порогов нет),
`INV_LOW_COVER`, `INV_NORMAL_COVER`, `INV_OVERSTOCK_CONTEXT` (контекст, не причина). Порог overstock
из единственного наблюдения (487 суток) не выводится.

## 2. Архитектура

```
 домен WB (wb_mart: wb_raw, wb_ops, evetis_ref)          домен Ozon (ozon_mart: ozon_raw, evetis_ref)
  V_AIE_WB_PAIR_EVIDENCE   пара кампания × nm              V_AIE_OZON_PAIR_EVIDENCE  кампания × sku
  V_AIE_WB_ECON_GUARD      nm: фактическая + прогнозная    V_AIE_OZON_ECON_GUARD     sku
  V_AIE_WB_QUERY_CLASS     nm × norm_query (диагностика)
                     └──── evetis_mart (нейтрально, только VIEW) ────┘
                      V_AIE_DECISION_CURRENT  иерархия L0–L8, состояние, причины, объяснение
```

Изоляция площадок — `EXTERNAL_DATASET_POLICY` (`tools/validate_current_sql.py`). Итоговое решение
может жить только в `evetis_mart`: доменные витрины читать `evetis_mart` не могут.

| Объект | Датасет | Файл | Состояние |
|---|---|---|---|
| `V_AIE_POLICY` | evetis_ref | `sql/ads_intel/evetis_ref/V_AIE_POLICY.sql` | Git, не развёрнут |
| `V_AIE_WB_PAIR_EVIDENCE` | wb_mart | `sql/ads_intel/wb_mart/V_AIE_WB_PAIR_EVIDENCE.sql` | Git, не развёрнут |
| `V_AIE_WB_ECON_GUARD` | wb_mart | `sql/ads_intel/wb_mart/V_AIE_WB_ECON_GUARD.sql` | Git, не развёрнут |
| `V_AIE_WB_QUERY_CLASS` | wb_mart | `sql/ads_intel/wb_mart/V_AIE_WB_QUERY_CLASS.sql` | Git, не развёрнут |
| `V_AIE_OZON_PAIR_EVIDENCE` | ozon_mart | `sql/current/ozon_mart/…` | `pending_deploy` |
| `V_AIE_OZON_ECON_GUARD` | ozon_mart | `sql/current/ozon_mart/…` | `pending_deploy` |
| `V_AIE_DECISION_CURRENT` | evetis_mart | `sql/current/evetis_mart/…` | `pending_deploy` |

`wb_mart` канонизирован на 0 % (`docs/architecture/CANONICAL_COVERAGE.md`), поэтому WB-представления
лежат по прецеденту PR-PROMO-2/3 — один файл на объект вне `sql/current`.

**Политика — представление `evetis_ref.V_AIE_POLICY`** (финализация 2026-09-27; раньше — CTE `aie_policy`
внутри `V_AIE_DECISION_CURRENT`). Вместо таблицы `evetis_ref.AIE_POLICY` из Design Gate — VIEW с CTE
`aie_policy` между маркерами `@aie:policy`: runtime-таблиц нет, изменение политики — ревью SQL в Git, а не
незаметный UPDATE. Представление, а не CTE решения, потому что P7 применяется в трёх доменных
представлениях, а `wb_mart` / `ozon_mart` не могут читать `evetis_mart`; `evetis_ref` читают все слои.
Все представления AIE несут `policy_id`; replay подставляет сетку кандидатов в тот же блок.

## 3. Иерархия решения

Ограничители сужают множество допустимых действий. Порядок уровней задаёт две вещи: какие входы
должны быть валидны до чтения следующих и какая причина становится основной. В строку пишутся **все**
сработавшие причины (`reason_codes`), основная — `primary_reason_code` = код с минимальным `rank`
среди кодов, определяющих состояние (`sql/ads_intel/aie_reason_codes_v1.json`).

| Уровень | Что проверяется | Почему здесь |
|---|---|---|
| L0 | идентичность SKU (`REF_SKU_CHANNEL_MAP`), тип кампании, текущий набор (`SCOPE_CAMPAIGN_INACTIVE`) | все следующие уровни соединяются по `internal_sku`; вне текущего набора строка — только история |
| L1 | свежесть и покрытие: реклама на дату, снимок остатка, финансы в пределах SLA 3 суток, слои `V_DATA_FRESHNESS` (CURRENT), `agent_decision_gate` Ozon, сигнатура P2-6 | L2–L4 читают эти же данные |
| L2 | управляемость: автопилот Ozon (P9) | рекомендация по ставке бессмысленна, если ставку ведёт алгоритм площадки |
| L3 | остаток карточки = 0 → `PAUSE_CANDIDATE` | продажа невозможна, выборка не нужна |
| L4 | себестоимость не покрыта → `BLOCKED`; вклад до рекламы < 0 в прогнозной base и в фактической базе, если она вычислена → `PAUSE_CANDIDATE` / `PRICE_BELOW_BREAKEVEN` + `PRICING_REVIEW` | цена ниже безубыточной — вопрос цены, а не ставки; исключений по SKU нет (P4) |
| L5 | обрезка окна: смена ставки/размещения/оплаты, цены (P7), акции; исключение дней без остатка | выборка должна быть однородной до подсчёта |
| L6 | доказательность Ads-4 на паре: ACTIONABLE ≥ 40 кликов, иначе `INSUFFICIENT_DATA` | пороги Ads-4 выведены (биномиальная / 2σ), см. `docs/ADS4_FUNNEL_MART_DESIGN_2026-08-15.md` §6.2 |
| L7 | `WASTE_ZERO_ORDERS_ADS4` → `PAUSE_CANDIDATE`; интервальный тест ДРР SKU против фактической границы (P3; P6 realized) → `DECREASE` / `HOLD` / кандидат на `INCREASE` | цель — экономика SKU; воронка (CTR, CPC, CR) идёт только в диагностику и сама направление не рождает |
| L8 | кандидат на `INCREASE` проходит осторожную прогнозную базу (P6 conservative), P2 (гистерезис) и P5 (покрытие) | ограничивает только движение вверх |

Структурная пауза L4 в режиме replay не срабатывает: прогнозная экономика только текущая
(`ECON_FORWARD_SKIPPED_NOT_REPLAYABLE`), а одна фактическая база паузу не определяет
(`ECON_REALIZED_NEGATIVE_BEFORE_ADS` — контекст).

## 4. Модель доказательности

**Окна.** Основное — 28 суток (решение владельца для Ads-4 до дизайна; живое распределение
2026-08-30…09-26: на паре WB медиана заказов за 7 суток 1, за 28 — 5; без заказов 49 % / 19 %
наблюдений). 7 суток — только для L1–L3. 3 суток отвергнуты (медиана 0 на всех уровнях).

**Эффективное окно пары** = `(as_of − 27 … as_of]`, начало сдвигается на день после последней границы
режима, дни с нулевым остатком карточки исключаются.

- смена ставки: изменение `(search_bid, reco_bid, placements, payment_type, bid_type)` между соседними
  снимками `V_ADV_CAMPAIGN_CONFIG_DAILY`; окно начинается на следующий день после МСК-даты снимка, в
  котором изменение замечено (смена произошла между снимками, оба дня смешанные);
- смена цены (P7): окно начинается на следующий день после МСК-даты наблюдения изменения;
- до первого наблюдения цены (WB: 2026-09-07) смена цены не наблюдаема → `REGIME_PRICE_UNOBSERVED_PART`.

**Экономическое окно SKU** заканчивается на `econ_as_of = LEAST(as_of, finance_known_through)`:
комиссия и логистика приходят из финансового отчёта с лагом, а `V_ADS_SKU_ECONOMIC_LIMITS` подставляет
0 за дни без отчёта (контракт `MART_SKU_DAILY`). Окно обрезается по смене цены.

**Интервал ДРР SKU** (P3 = 0.90, граница — фактическая, P6). ДРР = атрибутированный расход / выкупы. Неопределённость
задаёт число выкупов `n`; границы Пуассона — приближение Вильсона–Хилферти:
`L(n) = n·(1 − 1/(9n) − z/(3√n))³`, `U(n) = (n+1)·(1 − 1/(9(n+1)) + z/(3√(n+1)))³`,
`drr_low = spend / (p̄·U)`, `drr_high = spend / (p̄·L)`, `p̄ = buyouts_rub / n`. Уровень доверия — P3
из поддерживаемого набора {0.80, 0.90, 0.95}; иначе `POLICY_P3_UNSUPPORTED_VALUE`.

## 5. Replay без заглядывания вперёд

Одно тело SQL для текущего режима и для replay. `tools/aie_render.py` подменяет только:

1. CTE `aie_clock` (маркеры `@aie:clock`): `as_of_date = D`, `knowledge_ts = D+1 09:00 МСК`
   (после загрузки рекламы 05:11 и сборки витрины 07:00), `run_mode = 'REPLAY'`;
2. CTE `aie_policy` в `V_AIE_POLICY` (маркеры `@aie:policy`): только в анализе чувствительности —
   сетка кандидатов для нерешённых параметров; final replay исполняет политику из Git без подстановки;
3. `wb_mart.FACT_ADS_SKU_DAILY` и `wb_raw.V_ADV_CAMPAIGN_STATS` → та же логика сборки
   (`sql/mart/pr_mart1_facts.sql` §1.5, живое тело дедуп-вью) поверх RAW, отфильтрованного
   `TIMESTAMP(load_ts, 'Europe/Moscow') <= knowledge_ts` (`load_ts` пишется по МСК без пояса:
   запуск 02:07 UTC ↔ `load_ts` 05:10);
4. объекты только текущего состояния (`V_WB_SKU_FORWARD_ECONOMICS_CURRENT`, `V_DATA_FRESHNESS`,
   `V_OZON_AGENT_DECISION_INPUT`, `V_OZON_MART_FRESHNESS`) → та же схема, ноль строк.

| Вход | В replay |
|---|---|
| статистика рекламы WB | `PIT_RAW_LOAD_TS` |
| настройки кампаний, ставки | `PIT_SNAPSHOT_TS` (`snapshot_ts ≤ knowledge_ts`) |
| цены WB | `PIT_OBSERVED_AT` с 2026-09-07; раньше `UNOBSERVED` |
| остатки WB | `PIT_SNAPSHOT_DATE` с 2026-07-16 |
| финансы | `DATE_CUTOFF_REPORT_LOADS` (`completed_at ≤ knowledge_ts`); значения с последующей финализацией — `RESTATEMENT_RISK` |
| выкупы | `DATE_CUTOFF` по `econ_as_of`; `RESTATEMENT_RISK` |
| себестоимость | `CURRENT_REFERENCE_RETROACTIVE` — справочник правился на месте, историческое состояние не восстановимо |
| прогнозная экономика, `V_DATA_FRESHNESS`, вход агента Ozon | `SKIPPED_NOT_REPLAYABLE` |
| Ozon RAW | `NOT_POINT_IN_TIME` (MERGE перезаписывает) |

Перечень доступных входов пишется в каждую строку решения (`input_manifest`).

## 6. Поисковые запросы WB (диагностика)

`V_AIE_WB_QUERY_CLASS` поверх `wb_mart.V_ADS_FUNNEL_QUERY_28D` (Ads-4, без дублирования) и экономики SKU.
Классы: `Q_INSUFFICIENT`, `Q_TRAFFIC_NO_CONVERSION`, `Q_HIGH_CPC`, `Q_ECON_CANNOT_RAISE`, `Q_CONVERTING`,
`Q_POSSIBLY_UNDEREXPOSED`. Представление **не является источником направления ставки**: колонок
рекомендации в нём нет, `Q_POSSIBLY_UNDEREXPOSED` — гипотеза (доля показов не наблюдается).
Множители 0,7 / 1,3 — проектное решение Ads-4 (P15, не решено), вынесены в CTE `aie_diag_params`.

## 7. Ozon — режим «закрыто»

Все строки Ozon: `production_grade = FALSE`. Правила P7 и K применяются к Ozon так же, как к WB. Допустимы только `BLOCKED_BY_GUARDRAIL`, `INSUFFICIENT_DATA`
и структурный `PAUSE_CANDIDATE` (нулевой остаток; отрицательный лучший сценарий при открытом
`agent_decision_gate`). Строка, дошедшая до L7, получает `OZON_NOT_PRODUCTION_GRADE`. P2-5 и P2-6 в
этом треке не исправляются; сигнатура P2-6 (расход кампании за день без строк SKU) блокирует пару.

## 8. Проверки и откат

Набор `ads_intel` в `quality/suites.json`, файл `sql/ads_intel/aie_validation.sql`. До развёртывания
выполняется подстановкой тел: `python tools/aie_render.py run predeploy sql/ads_intel/aie_validation.sql …`.
Откат каждого этапа: `DROP VIEW` (потребителей нет); файлы отката — `sql/ads_intel/aie_rollback.sql`.
Развёртывание в production — только по отдельному ACK.
