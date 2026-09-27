# AIE V1 — Final Readiness Report

**Дата:** 2026-09-27 · **База:** `origin/main` `73bf86dd965cb15fd176f0bec06a0cb134a3e7f0` · **Ветка:**
`design/ads-intel-engine-v1` (отдельный worktree, не закоммичено, не запушено, не развёрнуто).
**Этап:** Design Finalization + Final Replay. PR-6 не начат: журнала, планировщика и write-путей нет.

## 1. Правила

### 1.1. Утверждены владельцем и реализованы

| # | Решение | Где в коде | Доказательство |
|---|---|---|---|
| P3 | 0.90, Shadow V1 (не политика будущего автопилота) | `evetis_ref.V_AIE_POLICY.p3_confidence` | `AIE_D08`, тест `test_policy_in_git_is_exactly_the_owner_decision` |
| P7 | граница режима — изменение цены продавца не меньше 3 % | `V_AIE_POLICY.p7_price_change_pct`; применяется в `V_AIE_WB_PAIR_EVIDENCE`, `V_AIE_WB_ECON_GUARD`, `V_AIE_OZON_PAIR_EVIDENCE` | `AIE_W08`, тест `test_p7_threshold_is_not_duplicated_as_a_constant` |
| K | текущий набор: WB 9/11, Ozon только RUNNING (INACTIVE исключён, см. §1.3) и ≤ 14 суток с последнего расхода; остальное — `HISTORICAL_EVIDENCE_ONLY` / `SCOPE_CAMPAIGN_INACTIVE` | `V_AIE_POLICY.k_inactive_days`, колонка `universe` | `AIE_D12`, инварианты replay `UNIVERSE_RULE_EXACT`, `HISTORICAL_ROWS_*` |
| P4 | исключений по SKU нет; `PRICE_BELOW_BREAKEVEN` + `PRICING_REVIEW` + разложение цены; цена не меняется | код 501, колонка `routing`, 11 диагностических колонок | `AIE_D13`, тест `test_price_below_breakeven_routes_to_pricing_review_without_sku_exceptions` |
| P6 | realized — граница DECREASE; forward base — ценовой ограничитель; forward conservative — против INCREASE; forward stress — флаг | `limit_drr = realized`, `conservative_guard`, коды 899 и 1012 | тест `test_decision_p6_roles`, инвариант `FORWARD_NEVER_IN_REPLAY` |
| P15 | 0,7/1,3 только в диагностике запросов | `V_AIE_WB_QUERY_CLASS.is_bid_direction_source = FALSE` | `AIE_Q04` |
| Финансы | флаг `FINANCIAL_DATA_MATURE`, без исключения N дней | `V_AIE_WB_ECON_GUARD.financial_data_mature`, `econ_window_end_lag_days`, контекст `FIN_WINDOW_ENDS_AT_FINANCE` | `AIE_D14`, инвариант `FINANCIAL_DATA_NEVER_IMMATURE` |
| Ранее | P9, P10, P11, P12, P14 (атрибут, не фильтр) | без изменений | `AIE_D04`, `AIE_D07` |

### 1.2. Не решены (в Git — `NULL`, скрытых значений нет)

| # | Что | Следствие сейчас | UBR |
|---|---|---|---|
| P1 | целевой ДРР (резерв) | `INCREASE = 0`; кандидат виден только как диагностика `INCREASE_CANDIDATE` | UBR-013 |
| P2 | полоса гистерезиса | не участвует, пока нет P1; 0 п.п. — только в исследовательской сетке | UBR-014 |
| P5 | нижняя и верхняя границы покрытия | состояние `INV_COVER_POLICY_NOT_SET` (или `INV_COVER_UNAVAILABLE`) | UBR-017 |
| P8 | минимальный расход для показа | показываются все пары | UBR-019 |
| P13 | период ожидания | отложен; моделируется только в `aie_backtest` | UBR-020 |
| P14 | семантика `include_in_ads_analysis` | контекст `CTX_INCLUDE_IN_ADS_ANALYSIS_FALSE` | UBR-022 |

### 1.3. Ozon `INACTIVE` — исключён (Commit/PR Gate, fail closed)

В проекте нет контракта, документа или проверенного кода, который доказывал бы, что `INACTIVE` — рабочее
рекламное состояние Ozon. Аудит 2026-08-30 (`docs/ozon/audit_2026-08-30/LOG.md`, §1.6.2) относит
`INACTIVE`-кампании к группе «не работают». `OZON_INCREMENTAL_CONTRACT_V1.md` и
`OZON_API_HISTORY_LIMITS_2026-09-03.md` упоминают `INACTIVE` только в историческом периметре расхода.
Поэтому в текущий набор входит только `RUNNING`. `INACTIVE` уходит в историческую базу с причиной
`SCOPE_CAMPAIGN_INACTIVE`, а объяснение явно говорит: «семантика INACTIVE не подтверждена — исключено,
fail closed». Replay Ozon повторён на 62 датах. Изменились ровно 33 строки (текущий набор → исторический:
30 с автопилотом, 3 без снимка остатков), остальные поля совпали. На текущей выдаче 27.09 все 14 пар
Ozon — `RUNNING`, итог не изменился.

### 1.4. Fail-closed инварианты политики (тесты)

P1, P2, P5 (нижняя), P5 (верхняя), P13 = `NULL` в Git. Ни одно представление AIE не подставляет вместо
них `IFNULL` / `COALESCE` / `CASE … THEN <число>`. INCREASE структурно требует `P1 IS NOT NULL`, `P2 IS NOT NULL`
и состояния запаса NORMAL/OVERSTOCK, которое без P5 недостижимо. `INCREASE_CANDIDATE` не входит в
`proposed_direction`. Ozon `production_grade = FALSE` в трёх местах. Во всех 7 представлениях нет
`EXTERNAL_QUERY`, `CALL`, `EXECUTE IMMEDIATE`, `EXPORT/LOAD DATA`, `REMOTE`, `CONNECTION`, URL и DML.
Мутационная проверка: подстановка `IFNULL(p1, 0.2)` и возврат `INACTIVE` в правило — оба ловятся тестами.

## 2. Final replay 2026-07-26 … 2026-09-25

62 даты решения, 3 154 строки, политика `V1_SHADOW_2026-09-27` из Git без подстановки,
`knowledge_ts = D+1 09:00 МСК`, реклама WB — из RAW по `load_ts`. 74 SELECT-запроса, 6,57 ГБ.

### 2.1. Текущий набор рекомендаций и историческая база

| Площадка | Набор | Решений | Пар | Состояния |
|---|---|---:|---:|---|
| WB | CURRENT_ACTIONABLE | 1 076 | 26 | INSUFFICIENT_DATA 741 · HOLD 120 · PAUSE_CANDIDATE 120 · DECREASE 94 · BLOCKED 1 |
| WB | HISTORICAL_EVIDENCE_ONLY | 1 056 | 59 | BLOCKED 1 056 (`SCOPE_CAMPAIGN_INACTIVE`): статус 7 — 855, статус 11 — 175, статус 9 — 26 |
| Ozon | CURRENT_ACTIONABLE | 291 | 14 | BLOCKED 291 (`CTRL_OZON_AUTOPILOT` 280, `DQ_STOCK_SNAPSHOT_MISSING` 11) |
| Ozon | HISTORICAL_EVIDENCE_ONLY | 731 | 31 | BLOCKED 731 (`SCOPE_CAMPAIGN_INACTIVE`), из них 96 — состояние `INACTIVE` |

Статус 7: 855 решений, из них 24 с расходом в день решения. В текущий набор не попало ни одно.

### 2.2. WB, текущий набор — по состояниям

**DECREASE — 94 решения, 8 пар** (все `ECON_DRR_ABOVE_BREAKEVEN_CONFIDENT`, P3 = 0.90):

| SKU | Кампания | Решений | Период |
|---|---|---:|---|
| EVT-FT-ACNE-150 | 37755348 | 24 | 09.08–05.09 |
| EVT-HC-HAND-300 | 37669244 | 22 | 10.08–09.09 |
| EVT-HC-AMBER-300 | 37736048 | 17 | 28.07–13.08 |
| EVT-HC-HAND-300 | 38581331 | 15 | 26.07–09.08 |
| EVT-HC-HAND-300 | 40042162 | 7 | 10.09–16.09 |
| EVT-FT-MOIST-150 | 37563883 | 5 | 11.08–23.09 |
| EVT-FT-ACNE-150 | 37727911 | 3 | 10.08–24.09 |
| EVT-SET-MOIST-TONIC-SERUM | 37792295 | 1 | 28.07 |

Калибровка при «любом изменении цены» давала 89; P7 = 3 % добавил 5 за счёт более длинных окон.

**PAUSE_CANDIDATE — 120 решений, 8 пар:** `INV_ZERO_STOCK` 84, `WASTE_ZERO_ORDERS_ADS4` 36.
Больше всего — EVT-SET-4PC-ACNE (34), EVT-SET-MOIST-TONIC-SERUM (24), EVT-SET-TON-SER-CREAM-ACNE (19),
EVT-HC-AMBER-300 (16).

**HOLD — 120 решений, 7 пар:** `POLICY_P1_NOT_SET` 103 (ДРР уверенно ниже фактической границы, цели нет),
`ALLOCATION_NOT_WORST_PAIR` 17.

**INSUFFICIENT_DATA — 741:** `EVID_INSUFFICIENT` 288, `EVID_OBSERVATIONAL_ONLY` 220,
`EVID_NO_EFFECTIVE_WINDOW` 129, `EVID_INTERVAL_STRADDLES_BREAKEVEN` 104.

**BLOCKED_BY_GUARDRAIL — 1:** `ECON_BASIS_UNAVAILABLE`, EVT-FC-ACNE-50 22.09 (нет выкупной выручки в окне).

### 2.3. Диагностика

- **INCREASE = 0** на всех 3 154 строках. **Диагностический `INCREASE_CANDIDATE` = 0** в replay по
  построению: осторожная прогнозная база в истории не воспроизводима (`conservative_guard = NOT_REPLAYABLE`
  на всех 1 076 строках). Кандидатов «ниже фактической границы» (`increase_candidate_realized`) — 103
  решения, 6 пар: EVT-FC-ACNE-50/37727999, EVT-FC-MOIST-50/36047250, EVT-FS-MOIST-30/37741306,
  EVT-FT-ACNE-150/37755348, EVT-SET-4PC-ACNE/37727876, EVT-SET-TON-CREAM-ACNE/39035441.
- **Ценовой ограничитель:** `PRICE_BELOW_BREAKEVEN` = 0 и `PRICING_REVIEW` = 0 в replay — прогнозная
  base-экономика в истории не используется (P6). Контекст `ECON_REALIZED_NEGATIVE_BEFORE_ADS` — 105 строк;
  на WB это EVT-HC-HAND-300, EVT-HC-CHERRY-300, EVT-SET-MOIST-TONIC-SERUM. Паузы он не определяет.
- **Запас не наблюдается** (`INV_COVER_UNAVAILABLE`) — 2 814 из 3 154: наборы — 1 058 (13 SKU), до
  2026-09-10 снимка покрытия нет — 1 756. С 10.09 у единичных SKU покрытие есть всегда. Остальные 340 —
  `INV_COVER_POLICY_NOT_SET`. `INV_LOW_COVER` / `INV_OVERSTOCK_CONTEXT` = 0: порогов нет.
- **Финансы:** `financial_data_mature = TRUE` на всех 2 132 строках WB; окно отстаёт от as_of на 1 день в
  70 решениях, в остальных 2 062 — нет.

## 3. Текущая выдача на 27.09 (as_of 26.09, predeploy)

| Площадка | Набор | Состояние | Основная причина | Пар |
|---|---|---|---|---:|
| WB | CURRENT | PAUSE_CANDIDATE | `PRICE_BELOW_BREAKEVEN` — EVT-HC-HAND-300, кампании 37669244 и 40042162 | 2 |
| WB | CURRENT | PAUSE_CANDIDATE | `INV_ZERO_STOCK` — EVT-SET-4PC-ACNE, EVT-SET-MOIST-TONIC-SERUM | 2 |
| WB | CURRENT | INSUFFICIENT_DATA | `EVID_INSUFFICIENT` 6, `EVID_OBSERVATIONAL_ONLY` 3 | 9 |
| WB | HISTORICAL | BLOCKED | `SCOPE_CAMPAIGN_INACTIVE` — EVT-EP-ENZYME-75/39120213 (21 сут без расхода), EVT-HC-AMBER-300/37736048 (17 сут) | 2 |
| Ozon | CURRENT | BLOCKED | `CTRL_OZON_AUTOPILOT` (TARGET_BIDS) | 14 |

EVT-HC-HAND-300, текст для владельца: цена 499,20 ₽ ниже безубыточной 556,84 ₽; вклад до рекламы
−31,50 ₽; комиссия 42,3 %, эквайринг 3,1 %, логистика 14,6 %, себестоимость 46,4 % цены. Маршрут
`PRICING_REVIEW`, флаг `ECON_STRESS_RISK_FLAG`. Цена не меняется.

`production_grade`: WB — TRUE, Ozon — FALSE на всех строках. INCREASE = 0, `INCREASE_CANDIDATE` = 0.
Ozon: у 11 из 14 пар окно 26.09 обнулено сменой цены не меньше 3 % (`EVID_NO_EFFECTIVE_WINDOW`) —
на итог не влияет, основная причина — автопилот.

## 4. Файлы (worktree `evetis-wb-analytics-ads-intel`)

**Новые (16):**
`sql/ads_intel/evetis_ref/V_AIE_POLICY.sql` · `sql/ads_intel/wb_mart/V_AIE_WB_PAIR_EVIDENCE.sql` ·
`…/V_AIE_WB_ECON_GUARD.sql` · `…/V_AIE_WB_QUERY_CLASS.sql` · `sql/current/ozon_mart/V_AIE_OZON_PAIR_EVIDENCE.sql` ·
`…/V_AIE_OZON_ECON_GUARD.sql` · `sql/current/evetis_mart/V_AIE_DECISION_CURRENT.sql` ·
`sql/ads_intel/aie_reason_codes_v1.json` · `sql/ads_intel/aie_validation.sql` · `sql/ads_intel/aie_rollback.sql` ·
`tools/aie_render.py` · `tools/aie_backtest.py` · `tools/tests/test_aie.py` ·
`docs/ads_intel/AIE_DESIGN_GATE_V1_2026-09-27.md` · `docs/ads_intel/AIE_REASON_CODES_V1.md` ·
`docs/ads_intel/AIE_OWNER_DECISION_PACK_V1_2026-09-27.md` (+ этот отчёт).

**Изменённые (9):** `CHANGELOG.md` · `docs/architecture/TECH_DEBT.md` (P2-10) ·
`quality/unresolved_business_rules.json` · `quality/suites.json` · `quality/contract_registry.json` ·
`sql/current/ozon_mart/MANIFEST.json` · `sql/current/evetis_mart/MANIFEST.json` ·
`tools/tests/test_validate_current_sql.py` · `tools/tests/test_scale1_fact_sku_daily.py`.

FACT / MART / DASH, Terraform, Apps Script, `services/` — без изменений.

## 5. Доказательства тестов

| Набор | Результат |
|---|---|
| Проверки данных `ads_intel` (predeploy, живые данные 27.09) | **43/43 PASS**. D13 первый раз упал из-за ошибки самой проверки (NULL-маршрут в `IS DISTINCT FROM`); проверка исправлена, повтор PASS |
| Инварианты final replay (`aie_backtest.py final`) | **14/14 PASS** |
| `tools/tests/test_aie.py` | **69 passed** (в т. ч. fail-closed политики, §1.4) |
| `tools/tests` целиком | **1 500 passed**, 0 failed (262,4 с) |
| `tools/validate_current_sql.py` | OK, 53 объекта, C1–C18 |
| `tools/build_contract_registry.py` | 393 проверки, 16 нерешённых правил |
| Dry-run всех 7 представлений | OK |
| Паритет реконструкции FACT_ADS_SKU_DAILY (PR-5) | 6 112 ключей, 0 расхождений |

**Неизменность окружения:** основной checkout (`feat/gate10-ozon-own-lcd-env`) совпадает со снимком до
работы; в BigQuery нет ни одного объекта `%AIE%` в `wb_mart`, `ozon_mart`, `evetis_mart`, `evetis_ref`;
все задания с меткой `aie-*` — только SELECT (плюс 5 неудачных запросов без типа). DML того же дня под
учётной записью владельца — это загрузчики `wb_raw` (Apps Script), к AIE не относятся.

## 6. Remediation вне AIE

1. **`TECH_DEBT.md` P2-10** — утренняя сборка `V_ADS_SKU_ECONOMIC_LIMITS` около 07:00 до прихода финансов
   около 07:24; завышение лимита ДРР при одном дне без финансов P90 5,5 п.п.; страдает ADS_REVIEW в
   Control Tower. Нужен отдельный scope и ACK.
2. **Покрытие наборов с учётом компонентов** — без него у наборов всегда `INV_COVER_UNAVAILABLE`.
3. **P2-5 и P2-6 (Ozon)** и снимок текущих ставок Ozon — условие `production_grade` для Ozon (PR-8).
4. **F-18** — блокер любого write-пути, в том числе будущего PR-6/PR-7.

## 7. Что будет создано в BigQuery при развёртывании (только по отдельному ACK)

Только 7 VIEW. Таблиц, процедур, расписаний, журнала, учётных данных — нет. Порядок:

| # | Объект | Читает |
|---|---|---|
| 1 | `evetis_ref.V_AIE_POLICY` | ничего |
| 2 | `wb_mart.V_AIE_WB_PAIR_EVIDENCE` | 1, FACT_ADS_SKU_DAILY, снимки настроек, цены, остатки |
| 3 | `wb_mart.V_AIE_WB_ECON_GUARD` | 1, MART_SKU_DAILY, финансы, V_WB_SKU_FORWARD_ECONOMICS_CURRENT |
| 4 | `wb_mart.V_AIE_WB_QUERY_CLASS` | 3, V_ADS_FUNNEL_QUERY_28D |
| 5 | `ozon_mart.V_AIE_OZON_PAIR_EVIDENCE` | 1, ozon_raw |
| 6 | `ozon_mart.V_AIE_OZON_ECON_GUARD` | ozon_mart |
| 7 | `evetis_mart.V_AIE_DECISION_CURRENT` | 1, 2, 3, 5, 6, V_DATA_FRESHNESS, V_INVENTORY_POSITION_HISTORY |

После развёртывания: `aie_validation.sql` на живых объектах (без predeploy), сверка тел с Git, перевод
записей MANIFEST из `pending_deploy` в `captured_live` отдельным PR. Развёртывание — целевое
`bq query` / `CREATE OR REPLACE VIEW` по файлам. `terraform apply` и `deploy-prod` из `main` не нужны
и запрещены (CLAUDE.md).

## 8. План отката

1. `sql/ads_intel/aie_rollback.sql` — 7 × `DROP VIEW IF EXISTS` в обратном порядке зависимостей
   (решение → Ozon → запросы WB → экономика WB → доказательства WB → политика). Тест
   `test_rollback_drops_every_aie_object_in_reverse_dependency_order` сверяет порядок с `aie_render.OBJECTS`.
2. Потребителей у представлений AIE нет: дашборды, процедуры и витрины их не читают. Данные не
   затрагиваются — AIE не пишет таблиц.
3. Откат Git — не сливать ветку; worktree удаляется вместе с веткой `design/ads-intel-engine-v1`.
4. Контроль после отката: запрос к `INFORMATION_SCHEMA.TABLES` по `%AIE%` в четырёх датасетах возвращает 0 строк.

## 9. Статус

Готово к ревью владельцем. Не сделано и не разрешено: PR-6, журнал, планировщик, commit, push, deploy.
