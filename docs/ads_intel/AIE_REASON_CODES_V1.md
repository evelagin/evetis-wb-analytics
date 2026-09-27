# AIE — контракт кодов причин V1

**Источник истины:** `sql/ads_intel/aie_reason_codes_v1.json` (версия 1.1.0). Этот файл — его читаемая копия; тест
`tools/tests/test_aie.py` проверяет, что таблица ниже и SQL совпадают с JSON.

Правила:

- `state` — состояние, которое код определяет, если он основной. Пусто — контекстный код.
- Основная причина строки — код с минимальным `rank` среди кодов с непустым `state`.
- В строку пишутся все сработавшие коды (`reason_codes`), включая контекстные.
- Коды `POLICY_*` означают отсутствие решения владельца, а не значение по умолчанию.
- Политика — `evetis_ref.V_AIE_POLICY` (решения владельца 2026-09-27): P3 = 0.90, P7 = 3 %, K = 14;
  P1, P2, P5 (нижняя и верхняя границы), P13 — NULL. Пока P1 = NULL, код `ECON_DRR_BELOW_TARGET_CONFIDENT`
  (единственный путь к INCREASE) недостижим; `INCREASE_CANDIDATE` — только диагностика.
- `SCOPE_CAMPAIGN_INACTIVE` отделяет текущий набор рекомендаций (`universe = CURRENT_ACTIONABLE`) от
  исторической базы доказательств (`HISTORICAL_EVIDENCE_ONLY`); строки не удаляются.
- Роли баз (P6): realized — граница DECREASE; forward base — `PRICE_BELOW_BREAKEVEN` / `PRICING_REVIEW`;
  forward conservative — `ECON_CONSERVATIVE_GUARD_BLOCKS_INCREASE`; forward stress — `ECON_STRESS_RISK_FLAG`.

| rank | Код | Уровень | Состояние | Смысл |
|---:|---|---|---|---|
| 100 | `ID_UNMAPPED_SKU` | L0 | BLOCKED_BY_GUARDRAIL | SKU площадки не сопоставлен с internal_sku в evetis_ref.REF_SKU_CHANNEL_MAP |
| 101 | `ID_AMBIGUOUS_SKU` | L0 | BLOCKED_BY_GUARDRAIL | SKU площадки сопоставлен более чем с одним internal_sku |
| 102 | `SCOPE_CAMPAIGN_TYPE_OUT_OF_SCOPE` | L0 | BLOCKED_BY_GUARDRAIL | Тип кампании вне V1 (WB: не type=9; Ozon: не SKU/CPC) |
| 103 | `SCOPE_CAMPAIGN_INACTIVE` | L0 | BLOCKED_BY_GUARDRAIL | Пара вне текущего набора рекомендаций: статус кампании не активен (WB — не 9/11; Ozon — не RUNNING: семантика INACTIVE в проекте не доказана, поэтому исключена, fail closed) или с последнего расхода прошло больше K суток (V_AIE_POLICY, K = 14). Строка остаётся в исторической базе доказательств (universe = HISTORICAL_EVIDENCE_ONLY); заменяет CTRL_CAMPAIGN_FINISHED |
| 200 | `DQ_FRESHNESS_LAYER_NOT_OK` | L1 | BLOCKED_BY_GUARDRAIL | Слой в wb_mart.V_DATA_FRESHNESS не в статусе OK (только режим CURRENT) |
| 201 | `DQ_ADS_DATA_BEHIND` | L1 | BLOCKED_BY_GUARDRAIL | Статистика рекламы не покрывает дату решения |
| 202 | `DQ_STOCK_SNAPSHOT_MISSING` | L1 | BLOCKED_BY_GUARDRAIL | Нет снимка остатка карточки на дату решения |
| 203 | `DQ_FINANCE_BEHIND_SLA` | L1 | BLOCKED_BY_GUARDRAIL | Финансы известны позже срока V_DATA_FRESHNESS.finance (3 суток) |
| 204 | `DQ_OZON_FRESHNESS_GATE_CLOSED` | L1 | BLOCKED_BY_GUARDRAIL | ozon_mart.V_OZON_MART_FRESHNESS.agent_decision_gate закрыт |
| 205 | `DQ_OZON_ADS_DATA_BEHIND` | L1 | BLOCKED_BY_GUARDRAIL | Статистика рекламы Ozon не покрывает дату решения |
| 206 | `DQ_OZON_P26_SUSPECTED` | L1 | BLOCKED_BY_GUARDRAIL | Сигнатура дефекта P2-6: у кампании есть расход за день, но нет ни одной строки по SKU |
| 301 | `CTRL_OZON_AUTOPILOT` | L2 | BLOCKED_BY_GUARDRAIL | Ставками кампании Ozon управляет автопилот Ozon (TARGET_BIDS / TARGET_CIR) — решение владельца P9 |
| 400 | `INV_ZERO_STOCK` | L3 | PAUSE_CANDIDATE | Остаток карточки на площадке равен нулю: продажа невозможна |
| 500 | `ECON_MISSING_COGS` | L4 | BLOCKED_BY_GUARDRAIL | Себестоимость в окне покрыта не полностью: экономический предел не вычисляется (NULL, не 0) |
| 501 | `PRICE_BELOW_BREAKEVEN` | L4 | PAUSE_CANDIDATE | Структурный ценовой ограничитель (P4, P6): вклад до рекламы отрицателен в прогнозной base и в фактической базе, если она вычислена — текущая цена ниже безубыточной. Пауза рекламы — кандидат; разложение цены в колонках current_effective_price_rub … product_cogs_pct, маршрут PRICING_REVIEW. Цена не меняется, pricing write API не вызывается. Исключений по SKU нет |
| 700 | `EVID_NO_EFFECTIVE_WINDOW` | L6 | INSUFFICIENT_DATA | После обрезки окна по смене режима и дням без остатка не осталось дней |
| 701 | `EVID_INSUFFICIENT` | L6 | INSUFFICIENT_DATA | Ads-4 evidence_status = INSUFFICIENT (clicks < 10 и views < 1000) |
| 702 | `EVID_OBSERVATIONAL_ONLY` | L6 | INSUFFICIENT_DATA | Ads-4 evidence_status = OBSERVATIONAL (clicks < 40): выводы о конверсии запрещены |
| 800 | `WASTE_ZERO_ORDERS_ADS4` | L7 | PAUSE_CANDIDATE | Ads-4 POTENTIAL_WASTE: ACTIONABLE (clicks ≥ 40), расход есть, атрибутированных заказов 0 |
| 801 | `OZON_NOT_PRODUCTION_GRADE` | L7 | BLOCKED_BY_GUARDRAIL | Ozon не production-grade до PR-8 (P2-5, P2-6, снимок ставок): направления ставок не выдаются |
| 803 | `POLICY_P3_NOT_SET` | L7 | BLOCKED_BY_GUARDRAIL | Не задан уровень доверия интервала (P3) |
| 804 | `POLICY_P3_UNSUPPORTED_VALUE` | L7 | BLOCKED_BY_GUARDRAIL | Значение P3 не входит в поддерживаемый набор уровней доверия |
| 805 | `ECON_BASIS_UNAVAILABLE` | L7 | BLOCKED_BY_GUARDRAIL | Фактическая граница ДРР (realized, P6) для строки не вычислена |
| 806 | `ECON_NO_BUYOUTS_IN_WINDOW` | L7 | INSUFFICIENT_DATA | В экономическом окне нет выкупов: интервал ДРР не определён |
| 807 | `EVID_INTERVAL_STRADDLES_BREAKEVEN` | L7 | INSUFFICIENT_DATA | Интервал ДРР пересекает безубыточность: противоположные действия равно допустимы |
| 808 | `ECON_DRR_ABOVE_BREAKEVEN_CONFIDENT` | L7 | DECREASE | Весь интервал ДРР выше экономического предела |
| 809 | `ALLOCATION_NOT_WORST_PAIR` | L7 | HOLD | У SKU несколько кампаний, понижать предлагается кампанию с худшим CPO |
| 810 | `POLICY_P1_NOT_SET` | L7 | HOLD | Интервал ДРР ниже безубыточности, но целевой ДРР (P1) не задан: повышения нет |
| 811 | `EVID_INTERVAL_STRADDLES_TARGET` | L7 | HOLD | Интервал ДРР пересекает цель: повышать не доказано, понижать не нужно |
| 812 | `ECON_DRR_WITHIN_TARGET_BAND` | L7 | HOLD | Интервал ДРР между целью и безубыточностью |
| 899 | `ECON_CONSERVATIVE_GUARD_BLOCKS_INCREASE` | L8 | HOLD | Кандидат на повышение, но осторожная прогнозная база (forward conservative, P6) не подтверждает: верхняя граница интервала ДРР не ниже лимита conservative, или база недоступна / не воспроизводима в replay |
| 900 | `POLICY_P2_NOT_SET` | L8 | BLOCKED_BY_GUARDRAIL | Кандидат на повышение, но полоса гистерезиса (P2) не задана |
| 901 | `POLICY_P5_NOT_SET` | L8 | BLOCKED_BY_GUARDRAIL | Кандидат на повышение, но границы покрытия запасом (P5, нижняя и верхняя) не заданы |
| 902 | `INV_COVER_UNAVAILABLE` | L8 | HOLD | Кандидат на повышение, но покрытие запасом не наблюдается (для наборов — всегда, до покрытия с учётом компонентов) |
| 903 | `INV_LOW_COVER` | L8 | HOLD | Кандидат на повышение, но покрытие ниже нижней границы P5 |
| 904 | `ECON_DRR_BELOW_TARGET_CONFIDENT` | L8 | INCREASE | Весь интервал ДРР ниже цели минус полоса P2, осторожная прогнозная база подтверждает, покрытие не ниже P5. Недостижим, пока P1 не задан |
| 1000 | `DQ_OZON_P25_WINDOW_BIAS` | CTX | — | Дефект P2-5: статистика Ozon по SKU теряет 00:00–03:00 МСК каждого дня |
| 1001 | `DQ_OZON_NOT_POINT_IN_TIME` | CTX | — | RAW Ozon пишется MERGE: в replay значения — последние известные, а не на дату |
| 1002 | `CTRL_OZON_NO_BID_OBSERVED` | CTX | — | Текущая ставка Ozon не собирается (нет снимка v2/products) |
| 1003 | `CTRL_NO_CONFIG_OBSERVED` | CTX | — | Нет снимка настроек кампании для пары |
| 1004 | `CTX_CONFIG_SNAPSHOT_BEHIND` | CTX | — | Последний снимок настроек старше даты решения |
| 1005 | `CTX_WB_STATUS_PAUSED_WITH_SPEND` | CTX | — | Статус 11 в снимке, но в окне есть расход — день рекламно активен (решение владельца P10) |
| 1006 | `CTX_INCLUDE_IN_ADS_ANALYSIS_FALSE` | CTX | — | REF_SKU_MASTER.include_in_ads_analysis = FALSE; фильтром не является (решение владельца P14) |
| 1007 | `ECON_FORWARD_SKIPPED_NOT_REPLAYABLE` | CTX | — | Прогнозная экономика только текущая: в replay не используется |
| 1008 | `ECON_FORWARD_UNAVAILABLE` | CTX | — | Прогнозной экономики для SKU нет или она BLOCKED |
| 1009 | `ECON_REALIZED_NEGATIVE_BEFORE_ADS` | CTX | — | Фактический вклад до рекламы в окне отрицателен (без прогнозной базы паузу не определяет) |
| 1010 | `PRICING_REVIEW` | CTX | — | Прогнозная base-экономика отрицательна до рекламы: вопрос цены, а не ставки. Маршрут для ручного разбора цены; записи цены нет |
| 1011 | `INCREASE_CANDIDATE` | CTX | — | Диагностика: интервал ДРР ниже фактической границы с доверием P3 и (в текущем режиме) ниже осторожной прогнозной базы. Не рекомендация INCREASE: P1 не задан |
| 1012 | `ECON_STRESS_RISK_FLAG` | CTX | — | Флаг риска (P6): прогнозная stress-база отрицательна или точечный ДРР SKU выше её лимита. Направления не определяет |
| 1013 | `INV_OVERSTOCK_CONTEXT` | CTX | — | Покрытие выше верхней границы P5 — контекст для повышения, не причина. Пока верхняя граница P5 не задана, не срабатывает |
| 1014 | `FIN_WINDOW_ENDS_AT_FINANCE` | CTX | — | Экономическое окно заканчивается раньше даты решения — на последнем дне с известными финансами (FINANCIAL_DATA_MATURE); искусственного исключения N дней нет |
| 1100 | `REGIME_BID_CHANGE` | L5 | — | Окно обрезано по смене ставки, размещения или типа оплаты |
| 1101 | `REGIME_PRICE_CHANGE` | L5 | — | Окно обрезано по смене цены продавца не меньше порога P7 (V_AIE_POLICY, 3 %) |
| 1102 | `REGIME_PRICE_UNOBSERVED_PART` | L5 | — | Часть окна до начала наблюдения цен: смена цены там не наблюдаема |
| 1103 | `REGIME_PROMO_CHANGE` | L5 | — | Окно обрезано по смене состояния акции SKU |
| 1104 | `REGIME_PROMO_UNOBSERVABLE_WB` | L5 | — | Состав авто-акций WB по SKU не наблюдается: влияние акции не исключено |
| 1105 | `INV_STOCKOUT_DAYS_EXCLUDED` | L5 | — | Дни без остатка карточки исключены из выборки |

## Шаблон объяснения

`{состояние}: {основная причина словами}; {ключевые числа с окном}; {доказательность}; {ограничители}.`
Числа подставляются из строки решения; порогов, которых нет в политике, в тексте нет.
