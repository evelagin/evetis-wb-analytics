# EVETIS — спецификация движка решений по акциям
# `PROMO_DECIDE_V1`, 2026-09-22. READ-ONLY.

Решение детерминировано и воспроизводимо из BigQuery. LLM не участвует в арифметике и не
может изменить ни один порог — он может только объяснить уже принятое решение словами.

---

## 1. Состояния рекомендации

| Состояние | Когда |
|---|---|
| `ENTER` | не участвуем, площадка принимает, экономика проходит политику, остатки позволяют |
| `STAY` | участвуем, экономика продолжает проходить политику |
| `EXIT` | участвуем, но экономика больше не проходит, либо остатки не выдержат, либо выгоднее сохранить товар под ближайшую акцию |
| `WATCH` | экономика на границе, или акция ещё не началась, или решение зависит от нераскрытой площадкой величины |
| `NOT_ELIGIBLE` | площадка не предлагает этот SKU в эту акцию, либо не выполнено требование по остатку |
| `INSUFFICIENT_DATA` | нет истории, нужной для оценки правдоподобия требуемого роста, а экономика неоднозначна |
| `BLOCKED_ECONOMICS` | отсутствует обязательный экономический вход (см. `PROMOTION_ECONOMICS_SPEC` §1.2) |

`BLOCKED_ECONOMICS` **никогда** не деградирует до `WATCH`: незнание экономики — это не
«наблюдаем», это «считать нечем».

---

## 2. Порядок вычисления (первое сработавшее выигрывает)

```
1. economics_status = 'BLOCKED'                      → BLOCKED_ECONOMICS
2. NOT is_participating AND NOT is_candidate
   AND NOT is_auto_add_eligible                      → NOT_ELIGIBLE
3. NOT meets_min_stock                               → NOT_ELIGIBLE
4. lifecycle_stage = 'UPCOMING'                      → WATCH
5. participating:
     5a. contribution_pct(STAY, WORST) < 0           → EXIT
     5b. contribution_pct(STAY, EXPECTED) < floor    → EXIT
     5c. stock_out_before_promo_end
         AND NOT inbound_covers_gap                  → EXIT
     5d. better_upcoming_promo_conflict              → EXIT
     5e. иначе                                       → STAY
6. не участвует:
     6a. required_price_rub IS NULL                  → WATCH  (весь WB сегодня)
     6b. contribution_pct(ENTER, WORST) < 0          → WATCH
     6c. contribution_pct(ENTER, EXPECTED) < floor   → WATCH
     6d. uplift_plausibility = 'IMPLAUSIBLE'         → WATCH
     6e. uplift_plausibility = 'UNKNOWN'
         AND required_sales_uplift_pct > 0           → INSUFFICIENT_DATA
     6f. stock_out_before_promo_end
         AND NOT inbound_covers_gap                  → WATCH
     6g. иначе                                       → ENTER
```

Асимметрия намеренная: **вход требует доказательства, выход — нет**. Ошибка «не вошли»
стоит недополученного объёма; ошибка «вошли» стоит вклада по каждой проданной единице и
не отменяется задним числом.

Шаг 6e — причина, по которой `INSUFFICIENT_DATA` вообще существует как рекомендация:
если акция требует роста продаж, а истории для оценки правдоподобия нет, движок не имеет
права ни рекомендовать, ни отговаривать. Если же роста не требуется
(`required_sales_uplift_pct ≤ 0`), отсутствие истории не мешает — вход выгоден
арифметически.

---

## 3. Reason codes

Все reason codes попадают в `V_PROMO_SKU_DECISION.reason_codes` (массив) и в
`wb_ops.OPS_INCIDENT.reason_code` — одно и то же пространство имён, без синонимов.

### Блокирующие
`PROMO_COGS_MISSING` · `PROMO_LOGISTICS_MISSING` · `PROMO_COMMISSION_UNRELIABLE` ·
`PROMO_UPSTREAM_ECONOMICS_BLOCKED` · `PROMO_TAKE_RATE_INVALID`

### Недоступность
`PROMO_NOT_OFFERED_BY_MARKETPLACE` · `PROMO_MIN_STOCK_NOT_MET` ·
`PROMO_SKU_LEVEL_DATA_UNAVAILABLE` (WB-автоакции) · `PROMO_SKU_UNRESOLVED`

### Экономика
`PROMO_BELOW_CONTRIBUTION_FLOOR` · `PROMO_BELOW_BREAK_EVEN_EXPECTED` ·
`PROMO_BELOW_BREAK_EVEN_WORST` · `PROMO_NEGATIVE_CONTRIBUTION` ·
`PROMO_CONTRIBUTION_IMPROVES` (акционная цена ≥ текущей) ·
`PROMO_NO_UPLIFT_REQUIRED` · `PROMO_UPLIFT_IMPLAUSIBLE` · `PROMO_UPLIFT_OPTIMISTIC` ·
`PROMO_UPLIFT_UNKNOWN`

### Остатки
`PROMO_STOCKOUT_RISK` · `PROMO_STOCKOUT_COVERED_BY_INBOUND` · `PROMO_EXCESS_STOCK` ·
`PROMO_EXPIRY_PRESSURE`

### Жизненный цикл и конкуренция акций
`PROMO_STARTS_SOON` · `PROMO_ENDS_SOON` · `PROMO_FREEZE_ACTIVE` ·
`PROMO_AUTO_ADD_SCHEDULED` · `PROMO_AUTO_ADD_PRICE_CUT` · `PROMO_AUTO_ADD_PRICE_NEUTRAL` ·
`PROMO_BETTER_UPCOMING_AVAILABLE` · `PROMO_INVENTORY_CONFLICT_WITH_NEXT` ·
`PROMO_BOOST_TIER_WITHIN_REACH`

### Наблюдаемость
`PROMO_PRICE_OBSERVATION_STALE` · `PROMO_STATE_OBSERVATION_STALE` ·
`PROMO_SCHEMA_DRIFT`

---

## 4. Жизненный цикл акции

| Этап | Условие | Сроки, которые он несёт |
|---|---|---|
| `UPCOMING` | `starts_at > now` и SKU не заявлен | `days_until_start` |
| `ELIGIBLE` | площадка предлагает SKU, акция ещё не началась | дедлайн заявки |
| `ENTER_WINDOW` | акция идёт, SKU кандидат, `freeze_at` не наступил | `days_until_freeze` |
| `ACTIVE` | SKU участвует, акция идёт | `days_until_end` |
| `EXIT_WINDOW` | SKU участвует, `freeze_at` наступил | выход ограничен правилами площадки |
| `ENDED` | `ends_at < now` | триггер расчёта `FACT_PROMO_SKU_WINDOW` |
| `POST_PROMO` | `ends_at < now ≤ ends_at + 14 сут` | окно восстановления цены |

### 4.1 Дедлайны, установленные правилами площадок

| Дедлайн | Источник | Класс |
|---|---|---|
| `freeze_date` Ozon: нельзя повышать цены, менять состав, уменьшать количество | `CONFIRMED_DOCS` | жёсткий |
| `auto_add_dates` Ozon: момент, после которого состав меняется без нашего участия | `CONFIRMED_API` | жёсткий |
| WB: автоакция применяет скидки автоматически; исключить товар можно **до старта**, снять скидку — в любой день акции | `CONFIRMED_DOCS` (описание акции 2940) | жёсткий |
| WB: снижение цены со скидкой больше чем вдвое блокируется, снижение более чем на треть уводит в карантин | `docs/pricing/WB_PRICE_RECOMMENDATIONS_2026-09-21.md` | жёсткий, ограничивает восстановление цены после акции |
| Ozon: цену товара можно менять не чаще 10 раз в час | `CONFIRMED_DOCS` | технический |

`action_deadline` строки решения = ближайший из применимых дедлайнов; `days_to_deadline`
и `deadline_kind` выводятся рядом.

### 4.2 Конкуренция между акциями

`better_upcoming_promo_conflict` истинно, когда одновременно:
1. существует будущая акция той же площадки, где `contribution_per_unit(ENTER)` выше
   текущей на ≥ 15 %;
2. её `starts_at` ≤ `expected_stock_out_date` текущей;
3. `available_units` не хватает на обе при требуемых объёмах.

Это единственное место, где движок сравнивает акции между собой, и оно даёт
`PROMO_INVENTORY_CONFLICT_WITH_NEXT` — не «выйди», а «выйти выгоднее, вот почему».

### 4.3 Лестница бустинга

`PROMO_BOOST_TIER_WITHIN_REACH` выставляется, когда добавление N товаров переводит
портфель на следующую ступень `ranging[]`. Пример с живых данных: акция WB 2940 —
участие 20 % (1 наш товар из 5), бустинг 25 %; ступень 50 % даёт 30 %. То есть один
дополнительный товар меняет бустинг всего портфеля в акции. Это решение **портфельного
уровня**, оно живёт в `V_PROMO_PORTFOLIO_STATE` и не притворяется решением по SKU.

---

## 5. Класс доверия решения

| Класс | Условие |
|---|---|
| `HIGH` | экономика `economics_confidence='HIGH'`, наблюдения свежие, `uplift_plausibility ∈ {NOT_REQUIRED, PLAUSIBLE, IMPLAUSIBLE}` |
| `MEDIUM` | экономика `MEDIUM`, либо `uplift_plausibility='OPTIMISTIC'`, либо одно устаревшее наблюдение |
| `LOW` | экономика `LOW`, либо дрейф схемы, либо `sku_resolution_status='RESOLVED_BY_OFFER_ID'` |
| `NONE` | `BLOCKED_ECONOMICS` или `uplift_plausibility='UNKNOWN'` при требуемом росте |

Класс доверия **не влияет** на состояние рекомендации — он влияет на то, попадёт ли
строка в алерт и как она выглядит на радаре. Смешивать их нельзя: иначе низкое доверие
начнёт молча превращать `EXIT` в `WATCH`.

---

## 6. Политика: проверка «пола 20 %»

Владелец обсуждал пол 20 %. Что именно найдено в репозитории:

**`docs/pricing/WB_PRICE_RECOMMENDATIONS_2026-09-21.md:19`**
> «вклад = цена × 0,5415 − логистика − хранение − COGS (комиссия 42,25 %, эквайринг 3,6 %).
> Пол — вклад ≥ 20 %.»

Разбор, поле за полем:

| Вопрос | Ответ | Класс |
|---|---|---|
| К какой метрике применяется 20 %? | к **вкладу** = `цена × (1 − take) − логистика − хранение − COGS` | `CONFIRMED_EXISTING_CODE` |
| Доля от чего? | от **цены продавца** (не от цены покупателя, не от выручки после СПП) | `CONFIRMED_EXISTING_CODE` |
| До или после рекламы? | **до**. Реклама в той же записке ограничивается отдельно («ДРР ≤ 10–12 %») | `CONFIRMED_EXISTING_CODE` |
| До или после налога? | **до**; налога в формуле нет | `CONFIRMED_EXISTING_CODE` |
| Площадка | формулировка дана для **WB** | `CONFIRMED_EXISTING_CODE` |
| Жёсткий или целевой? | сформулирован как «пол» рядом с «шаг ±10 % в неделю» — то есть **жёсткий** | `LIKELY` |
| Какой случай — expected или worst? | **не указано** | `UNPROVEN` |
| Есть ли аналог на Ozon? | да, но чуть иной: `V_OZON_SKU_FORWARD_ECONOMICS_CURRENT` классифицирует `margin_expected_pct ≥ 20` как `HEALTHY`, `≥ 30` как `SCALE_CANDIDATE`, `< 10` как `WATCH`; есть готовая лестница `target_price_{10,15,20,25}pct_*` | `CONFIRMED_EXISTING_CODE` |

**Одно расхождение, требующее решения владельца.** Записка по WB включает **хранение**
в формулу вклада, а `WB_FE_V1` и `V_OZON_SKU_FORWARD_ECONOMICS_CURRENT` хранение
**исключают** (`docs/pricing/PR2_PHASE_A_DISCOVERY.md:231`: «хранение не входит в
маргинальный пол цены»). Поэтому «20 % по записке» и «20 % по витрине» — **разные числа**.
Движок не выбирает между ними сам: до решения владельца `REF_PROMO_POLICY` не наполняется,
а `V_PROMO_SKU_DECISION` работает в режиме `policy_status = 'NOT_SET'`, выдавая
`WATCH` вместо `ENTER` и `STAY` вместо `EXIT`, но **выдавая все числа** — вклад, пол
безубыточности, требуемый рост. Решение остаётся у человека, движок не молчит.

---

## 7. Состав строки рекомендации

`evetis_mart.V_PROMO_SKU_DECISION`:

`marketplace, promotion_id, promotion_name, promotion_type, action_type, internal_sku,
marketplace_sku, product_name, recommendation, reason_codes[], decision_confidence,
policy_status, contribution_baseline_rub, contribution_promo_rub,
contribution_promo_pct, contribution_delta_rub, break_even_price_rub,
required_price_rub, max_allowed_price_rub, max_ad_spend_per_unit_rub,
break_even_drr_pct, boost_current_pct, boost_max_pct,
required_sales_uplift_pct, uplift_status, uplift_plausibility,
observed_difference_pct_p50, evidence_class,
available_units, baseline_days_of_cover, promo_days_of_cover,
expected_stock_out_date, inbound_eta, meets_min_stock,
lifecycle_stage, starts_at, ends_at, days_until_start, days_until_end,
freeze_at, next_auto_add_at, action_deadline, deadline_kind, days_to_deadline,
next_promotion_id, next_promotion_starts_at, next_promotion_contribution_rub,
blocked_reason, observed_at, economics_model_version, decision_model_version,
tax_model_status, generated_at`

Ни одного поля, которое нельзя вывести из перечисленных источников.

---

## 8. Жизненный цикл алертов — в существующей подсистеме

Новая подсистема уведомлений **не создаётся**. Используется
`wb_ops.OPS_INCIDENT` + `wb_ops.OPS_ALERT_EVENT`:
дедупликация по `condition_fingerprint`, повторы по `reminder_bucket`, доставка по
`channel`, маскирование получателя в `recipient_mask`.

`scope = 'PROMO'`, `scope_id = '<marketplace>|<promotion_id>|<internal_sku>'`
(или `'<marketplace>|<promotion_id>'` для портфельных).

| Алерт | Условие | Severity | Дедуп |
|---|---|---|---|
| `PROMO_NEW_ELIGIBLE` | SKU впервые стал кандидатом акции, проходящей политику | INFO | акция × SKU |
| `PROMO_STARTS_SOON` | `days_until_start ≤ 3` и рекомендация `ENTER` | WARN | акция × SKU |
| `PROMO_ELIGIBILITY_LOST` | был кандидатом/участником, перестал | WARN | акция × SKU |
| `PROMO_MAX_PRICE_CHANGED` | `max_action_price` изменилась на ≥ 5 % от прошлого наблюдения | WARN | акция × SKU |
| `PROMO_PRICE_NO_LONGER_QUALIFIES` | текущая цена > `max_action_price` у участника | **CRIT** | акция × SKU |
| `PROMO_FLOOR_BREACHED` | у участника `contribution_pct(STAY)` ушёл ниже пола | **CRIT** | акция × SKU |
| `PROMO_STOCK_INSUFFICIENT` | `meets_min_stock` стал FALSE у участника | WARN | акция × SKU |
| `PROMO_ENDS_SOON` | `days_until_end ≤ 2` у участника | INFO | акция × SKU |
| `PROMO_AUTO_ADD_DETECTED` | появилась строка `SCHEDULED` с `add_mode='AUTO'` | WARN | акция × дата × SKU |
| `PROMO_AUTO_ADD_PRICE_CUT` | то же и `action_price_to_auto_add < текущей цены` | **CRIT** | акция × дата × SKU |
| `PROMO_AUTO_ADD_BELOW_FLOOR` | то же и вклад по автоцене ниже пола | **CRIT** | акция × дата × SKU |
| `PROMO_UNEXPECTED_STATE_CHANGE` | участие исчезло без нашего действия | **CRIT** | акция × SKU |
| `PROMO_FREEZE_STARTED` | у акции появился `freeze_date` | WARN | акция |
| `PROMO_PORTFOLIO_BOOST_TIER` | одна дополнительная позиция даёт следующую ступень бустинга WB | INFO | акция |
| `PROMO_SCHEMA_DRIFT` | `schema_status != 'OK'` | WARN | площадка |
| `PROMO_OBSERVATION_STALE` | наблюдение старше SLA | WARN | площадка |

Ни один алерт не разворачивается в этой фазе. Их SQL пишется и проверяется на исторических
снимках, но `OPS_ALERT_EVENT` не наполняется до отдельного решения.

**Замечание о шуме.** `PROMO_AUTO_ADD_PRICE_CUT` при сегодняшних данных не сработал бы ни
разу: все 13 запланированных автодобавлений идут по цене, равной текущей или выше. Это
хороший знак для качества алерта — он не будет кричать по расписанию.

---

## 9. Каденс наблюдения

Односуточный снимок принимать нельзя без обоснования. Обоснование:

| Фактор | WB | Ozon |
|---|---|---|
| Частота публикации новых акций | новые акции появляются каждые 3–7 суток (70 акций за 300 суток окна) | 12 акций, состав меняется редко |
| Горизонт публикации вперёд | +29 суток | до +14 суток, плюс `auto_add_dates` |
| Волатильность состояния SKU | не наблюдается вовсе | `max_action_price` и `current_boost` привязаны к медианной цене за 30 суток → меняются медленно |
| Дедлайны, которые нельзя пропустить | старт автоакции (полночь МСК) | `auto_add_dates` (21:00 UTC = полночь МСК), `freeze_date` |
| Лимит | 10 req / 6 с на всю категорию | 50 req/s на Client-Id |
| Стоимость полного снимка | 2 запроса | ~35 запросов |

**Рекомендация: 4 раза в сутки, `0 4,9,14,19 * * *` UTC (07/12/17/22 МСК).**

Обоснование именно такое, а не суточное и не почасовое:
- дедлайны площадок привязаны к полуночи МСК (21:00 UTC), поэтому **обязателен снимок
  вечером МСК** — он превращает «акция стартовала ночью» в «акция стартует через 2 часа»;
- четыре снимка дают ≤ 5 часов задержки обнаружения — меньше самого короткого
  наблюдаемого окна между публикацией акции WB и её стартом (2940 опубликована минимум
  за 5 суток до старта);
- почасовой снимок дал бы ~840 запросов Ozon в сутки ради величин, которые по построению
  считаются от 30-суточной медианы, — это шум и расход квоты без выигрыша;
- суточный снимок ловил бы `auto_add_date` уже после срабатывания.

**С наблюдателем цен (каждые 20 минут) не объединяется.** Три причины: разные лимиты
площадок, разный характер величины (цена меняется нашими руками, состав акции — руками
площадки), и разные последствия пропуска окна. Общим остаётся образ Cloud Run и
дисциплина манифеста наблюдений.

Отдельно: **точечный снимок за 1 час до каждой `auto_add_date`** — не отдельный
планировщик, а ветка того же job, включаемая, когда до ближайшей даты автодобавления
меньше суток.

---

## 10. Граница записи

```
PROMOTION_READ_ALLOWED  = TRUE
PROMOTION_WRITE_ALLOWED = FALSE
```

Не декларация, а конструкция:

1. **WB.** `WB_PRICES_READ_TOKEN` имеет в JWT бит 30 (read-only) — проверено
   2026-09-22, срок до 2027-03-08. Записать акцию этим токеном невозможно физически.
   Preflight загрузчика обязан проверять бит 30 и **падать**, если его нет: это ровно та
   же логика, по которой `wb-prices-prod` создавался `paused`.
2. **Ozon.** Ключ EVETIS имеет роль Admin, мутирующие методы доступны. Поэтому граница
   переносится в код: HTTP-клиент подсистемы акций экспортирует **только** `promo_get` и
   `promo_post_read`, а список разрешённых путей — константа-allow-list. Любой путь вне
   списка роняет вызов на этапе сборки запроса, а не после ответа.
3. **Deny-list** (из `PROMOTION_DISCOVERY_2026-09-22.md` §7) зашивается в тест: файл
   загрузчика не должен содержать ни одной из этих строк. Тест по исходному тексту, а не
   по поведению, — он ловит опечатку раньше, чем она доедет до площадки.
4. **Секреты.** Читаются в память процесса из Secret Manager, не пишутся в логи, файлы,
   строки BigQuery, отчёты и CI-вывод. `secretAccessor` выдаётся ровно на нужные секреты
   ровно одному SA — по образцу `infra/terraform/wb_prices_observer.tf`.
5. **Scheduler** создаётся `paused`; снятие с паузы — отдельное действие владельца после
   проверки п. 1–3.
6. **ПДн.** `/v1/actions/discounts-task/list` не загружается в V1. Если будет загружаться,
   поля ПДн вырезаются в загрузчике до формирования строки.

---

## 11. Проверки качества данных

`evetis_mart.V_PROMO_HEALTH`, одна строка, по образцу `V_WB_PRICING_ECONOMICS_HEALTH`.

| Проверка | Условие тревоги |
|---|---|
| Свежесть наблюдения | возраст последнего снимка > 2× каденса |
| Покрытие площадок | нет снимка по WB или по Ozon за сутки |
| Полнота пагинации | `COUNT(products) != total` в ответе |
| Пропавшие акции | акция была активна, исчезла из списка до `date_end` |
| Дубли грейна | больше одной строки на ключ слияния |
| Нерезолвленные SKU | `sku_resolution_status = 'UNRESOLVED'` > 0 |
| Некорректные цены | `required_price_rub ≤ 0` или `> max_allowed_price_rub` |
| Некорректные даты | `starts_at > ends_at`, `freeze_at > ends_at`, `auto_add_date` вне окна акции |
| Участник без цены | `is_participating` и `required_price_rub IS NULL` |
| Дрейф схемы | `schema_status != 'OK'` |
| Полнота экономики | доля строк с `BLOCKED_ECONOMICS` |
| Согласованность флага | `ozon_actions_exist = FALSE` при `participating_products_count > 0` — **сегодня срабатывает постоянно**, поэтому заводится как известное расхождение с явным примечанием, а не как новый инцидент |

Сводный статус `GREEN / YELLOW / RED` — по той же схеме, что
`economics_health_status`: `RED` при любой из критических, `YELLOW` при неполноте.

---

## 12. Граница LLM

| Можно LLM | Нельзя LLM |
|---|---|
| Объяснить готовую рекомендацию словами | Вычислить или изменить вклад, пол, требуемый рост |
| Сгруппировать строки радара в связный обзор | Изменить состояние рекомендации |
| Предложить владельцу формулировку вопроса | Снять `BLOCKED_ECONOMICS` |
| Сопоставить решение с контекстом переписки/кабинета | Вызвать любой метод площадки |

Каждая рекомендация обязана воспроизводиться повторным выполнением SQL на тех же снимках.
`decision_model_version` и `economics_model_version` хранятся в строке.

---

## 13. Витрина `V_PROMO_RADAR`

Одна строка = один повод посмотреть. Попадают строки, у которых верно хотя бы одно:
рекомендация ∈ {`ENTER`,`EXIT`}; `days_until_start ≤ 7`; `days_until_end ≤ 3`;
`is_auto_add_scheduled`; сработал любой CRIT-алерт; `BLOCKED_ECONOMICS`.

Колонки для Metabase (порядок — порядок чтения):
SKU · площадка · текущее состояние · текущая акция · ближайшая акция · дней до старта ·
требуемая акционная цена · вклад сейчас · вклад в акции · Δ вклада · требуемый рост
продаж · правдоподобие роста · класс исторических свидетельств · покрытие остатками ·
дней до дедлайна · рекомендация · доверие · причины.

Сортировка по умолчанию — по `days_to_deadline` возрастанию: радар отвечает на вопрос
«что решать сегодня», а не «что интересно вообще».

Дашборд строится в существующем Metabase поверх BigQuery, отдельного канала не создаётся.

---

## 14. Валидация: можно ли ответить на контрольные вопросы

Проверено на живых данных 2026-09-22.

| # | Вопрос | Ozon | WB | Авторитетный источник |
|---|---|---|---|---|
| 1 | Какие акции доступны сегодня? | **да** — 12 | **да** — 10 актуальных | `GET /v1/actions` · `GET /api/v1/calendar/promotions` |
| 2 | Какие SKU EVETIS подходят? | **да**, поимённо | **нет** — только число (`notInPromoActionTotal`) | `/v1/actions/candidates` · `details` |
| 3 | Какие участвуют сейчас? | **да** — 18 SKU в 9 акциях | **нет** поимённо; да агрегатом (1–5 товаров в 9 из 10 акций) | `/v1/actions/products` · `details.inPromoActionTotal` |
| 4 | Какая акционная цена требуется? | **да** — `action_price` / `action_price_to_auto_add` | **нет** | `/v1/actions/products`, `/auto-add/products/list` |
| 5 | Какая цена максимально допустима? | **да** — `max_action_price` | **нет** | `/v1/actions/products` |
| 6 | Сколько SKU зарабатывает по этой цене? | **да** | нет — нечего подставлять | `V_OZON_SKU_FORWARD_ECONOMICS_CURRENT` + `PROMO_ECON_V1` |
| 7 | Какой рост продаж нужен, чтобы сохранить вклад/день? | **да** (пример: +1592 % у 930334396 при автоцене 1130 ₽) | нет | `PROMOTION_ECONOMICS_SPEC` §3 + `V_CT_INVENTORY_TRUTH.units_per_day_30d` |
| 8 | Правдоподобен ли этот рост по истории? | **нет сегодня** — `INSUFFICIENT_DATA`: история цены с 2026-08-31, состояния акций нет вовсе | нет | `FACT_PROMO_SKU_WINDOW` после накопления |
| 9 | Позволяют ли остатки? | **да** | частично: остатки есть, но неизвестно, какие SKU в акции | `V_CT_INVENTORY_TRUTH` |
| 10 | Когда акция заканчивается? | **да** | **да** | `date_end` · `endDateTime` |
| 11 | Какая акция идёт следующей? | **да**, горизонт ~14 суток + `auto_add_dates` | **да**, горизонт 29 суток | оба календаря |
| 12 | То же для WB? | — | **частично**: уровень акции и портфеля — да, уровень SKU — нет | см. выше |
| 13 | На что ответить нельзя и почему | цена покупателя до заказа (скидки за счёт Ozon, баллы, округление — разрыв до −29 %); смысл `stock`; фактический эффект бустинга | состав акции по SKU, плановая акционная цена, СПП — все три закрыты контрактом API, а не нашей архитектурой | `PROMOTION_API_CAPABILITY_MATRIX_2026-09-22.md` §3 |

**Итог валидации.** Из 13 вопросов Ozon отвечает на 10 сегодня, ещё на 1 (№ 8) — через
4–6 недель накопления. WB отвечает на 4 из 13 на уровне SKU и на 7 — на уровне акции и
портфеля. Движок строится в полном объёме для Ozon и в урезанном, но полезном виде для WB:
для WB он отвечает не «входить ли этому SKU», а «что сейчас происходит с нашим портфелем
в акциях, какой бустинг мы получаем и какая ступень достижима».
