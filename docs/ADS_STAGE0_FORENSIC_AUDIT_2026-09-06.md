# Stage ADS-0 — Forensic audit рекламного контура Wildberries

**Дата: 06.09.2026 · Режим: READ-ONLY · Production не изменялся**

Проект: `project-fa311fc0-4d87-4781-986` (EU). Датасеты: `wb_raw`, `wb_mart`, `wb_ops`, `evetis_ref`.
Все выводы ниже подтверждены либо SQL по production-данным, либо кодом репозитория. Там, где
доказательства нет, стоит явная пометка **НЕ ДОКАЗАНО**.

---

## 1. Executive Summary

**Рекламный контур существует и работает.** Это не «чистое поле»: ingestion идёт ежедневно
без сбоев, история непрерывна с 13.04.2026, а поверх RAW уже построен слой Ads-4
(`V_ADS_FUNNEL_*`, `V_ADS_SCREEN_*`) с воронкой, baseline'ами, гейтами доказательности,
сигналами и присоединёнными ставками. Экономический слой (`V_PRODUCT_COGS_EFFECTIVE`)
покрывает 25 SKU из 25. Реализовать DASHBOARD A/B/C/E и target/max DRR можно на том,
что уже лежит в BigQuery.

**Но есть четыре факта, которые меняют план работ.**

1. 🔴 **Витрина `MART_SKU_DAILY` сломана с 02.09.2026.** 22 прогона подряд падают на
   одной и той же ASSERT-ошибке. Последний успех — target_date 2026-09-01. Любая
   рекламная витрина, читающая MART (а `V_ADS_FUNNEL_SKU_28D` её читает), сегодня
   недосчитывает 6 239 ₽ рекламного расхода из 36 321 ₽ за 28-дневное окно (−17,2 %).
2. 🔴 **Мониторинг не работает.** `wb_ops.OPS_PIPELINE_REGISTRY` заполнен (14 конвейеров,
   SLA, критичность), но `OPS_HEALTH_STATE`, `OPS_ALERT_EVENT`, `OPS_INCIDENT` — **0 строк**.
   Именно поэтому четырёхдневный отказ витрины никто не увидел.
3. 🔴 **Query-level статистика покрывает только ~35 % рекламного расхода** (доказано:
   224 737 ₽ из 543 817 ₽ за 13.04–05.09). Остальное — размещения без разбивки по
   поисковым запросам. Search Query Intelligence физически не может объяснить весь бюджет.
4. 🔴 **На 28-дневном окне ACTIONABLE-запросов ровно ТРИ.** При текущем расходе
   (~1 500 ₽/сутки) статистики на решение по ключу почти нет. На 90 днях — 18. Дашборд C
   надо строить с окном 90 дней как основным, иначе он будет состоять из «LOW DATA».

**Органических позиций в проекте нет ни в каком виде** — ни таблицы, ни загрузчика,
ни endpoint'а. DASHBOARD D на собственных данных **невозможен**.

**Вердикт: GO с условием.** Начинать надо не с дашбордов, а со Stage ADS-1 —
разблокировать витрину и включить детектор здоровья. См. §18.

---

## 2. Current Architecture

```
WB Advertising API
   ├─ /adv/v1/promotion/count, /api/advert/v2/adverts  → RAW_WB_ADV_CAMPAIGNS      (DIM + raw_json)
   ├─ /adv/v1/upd                                      → RAW_WB_ADV_COSTS          (биллинг списаний)
   ├─ /adv/v2/fullstats                                → RAW_WB_ADV_CAMPAIGN_STATS (day×advert×nm×appType)
   │                                                   → RAW_WB_ADV_BOOSTER_STATS  (day×advert×nm, avg_position)
   ├─ /adv/v0/normquery/stats                          → RAW_WB_ADV_QUERY_STATS    (day×advert×nm×query)
   └─ /adv/v0/normquery/get-bids                       → RAW_WB_ADV_QUERY_BIDS     (snapshot×advert×nm×query)
        │
        │  Apps Script: runWbAdsDaily() — 05:00 МСК, наблюдаемый старт 05:07
        │  (WbAdsDaily.gs:301, WbAdsRawLoader.gs, WbAdsQueryBids.gs, WbAdsQueryStats.gs)
        ▼
   wb_raw.V_ADV_*  ─ дедуп во вью (ROW_NUMBER по ключу, processed_status='raw')
        ▼
   wb_mart.FACT_ADS_SKU_DAILY / FACT_ADS_COSTS_DAILY  ← sp_build_mart_sku_daily (Cloud Run, 07/09/12/16 МСК)
        ▼
   wb_mart.MART_SKU_DAILY  ← 🔴 СЛОМАНА с 02.09.2026
        ▼
   wb_mart.V_ADS_FUNNEL_QUERY_DAILY → _28D / _90D → V_ADS_FUNNEL_SIGNALS → V_ADS_SCREEN_QUERY
                                    → V_ADS_FUNNEL_SKU_28D              → V_ADS_SCREEN_SKU
        ▼
   Metabase: dashboard 2 «WB Executive», dashboard 3 «WB SKU Performance»
             🔴 ни одна из 46 карточек не читает V_ADS_SCREEN_* — слой Ads-4 в Metabase НЕ выведен
```

Порядок вызовов внутри `runWbAdsDaily()` неслучаен и зафиксирован в коде: сначала три
mart-критичных источника (campaigns → costs → fullstats), затем `ingestFinalizeByStatus_`,
и только потом — Ads-3 (ставки, невосстановимый снимок) и Ads-2 (query stats, восстановимый).
`WB_ADS_EXPECTED_SOURCES_ = 3`: сбой ставок или query-статистики **не** блокирует витрину.

---

## 3. Advertising Data Inventory

| Объект | Строк | Грейн | Дедуп | Статус |
|---|---:|---|---|---|
| `RAW_WB_ADV_CAMPAIGNS` | 22 291 | snapshot × advertId | `V_ADV_CAMPAIGNS` → последний снимок | ✅ 430 кампаний |
| `RAW_WB_ADV_CAMPAIGN_STATS` | 45 585 | date × advertId × nmId × appType × source_level | `V_ADV_CAMPAIGN_STATS` | ✅ 10 455 ключей, 0 дублей |
| `RAW_WB_ADV_COSTS` | 5 498 | updDate × advertId × updNum | `V_ADV_COSTS` (union prebootstrap) | ⚠️ 180 ключей с 2–3 строками |
| `RAW_WB_ADV_QUERY_STATS` | 50 695 | period_from × advert × nm × norm_query | `V_ADV_QUERY_STATS` (по canon-run) | ✅ 40 794 ключа, 0 дублей |
| `RAW_WB_ADV_QUERY_BIDS` | 2 337 | snapshot_date × advert × nm × norm_query | `V_ADV_QUERY_BIDS` | ✅ 23 снимка |
| `RAW_WB_ADV_BOOSTER_STATS` | 4 424 | date × advertId × nmId | `V_ADV_BOOSTER_STATS` | ✅ 1 968 после дедупа |
| `RAW_WB_ADV_SEARCH_CLUSTERS` | 44 | — | `V_ADV_SEARCH_CLUSTERS` | 🔴 **мёртвая**, 1 прогон 11.07 |
| `wb_mart.FACT_ADS_SKU_DAILY` | 5 870 | date × advert_id × nm_id | — | ✅ 0 дублей |
| `wb_mart.FACT_ADS_COSTS_DAILY` | 1 876 | date × advert_id | — | ✅ |
| `wb_mart.V_ADS_SCREEN_QUERY` | 1 596 | as_of × window × nm_id × norm_query | — | ✅ работает |
| `wb_mart.V_ADS_SCREEN_SKU` | — | as_of × window × nm_id | — | ⚠️ зависит от сломанной MART |

Run-логи: `RAW_WB_ADV_COSTS_RUNS`, `RAW_WB_ADV_QUERY_STATS_RUNS`, `RAW_WB_ADV_QUERY_BIDS_RUNS`,
`wb_raw.INGEST_RUNS` (heartbeat `ads`), `wb_mart.MART_RUNS`.
Contract-вью покрытия: `V_ADV_COSTS_DAY_COVERAGE`, `V_ADV_QUERY_STATS_COVERAGE`.

---

## 4. Historical Coverage

| Домен | Min | Max | Суток | Строк | Грейн | Refresh | Статус |
|---|---|---|---:|---:|---|---|---|
| campaign stats (fullstats) | 2026-04-13 | 2026-09-05 | 146 | 10 455 | day×advert×nm×appType | daily 05:07, окно D−7…D−1 | ✅ непрерывно |
| ad costs (биллинг) | 2026-04-13 | 2026-09-05 | 143 | 1 895 | day×advert×updNum | daily, окно D−7…D−1 | ✅ |
| search query stats | 2026-04-13 | 2026-09-05 | **146 / 146** | 40 794 | day×advert×nm×query | daily, окно D−1…D−7 | ✅ разрывов нет |
| query bids (ставки по ключу) | 2026-08-14 | 2026-09-06 | 23 | 2 337 | snapshot×advert×nm×query | daily snapshot | ⚠️ нет 01.09 |
| campaign bids (в `raw_json`) | 2026-07-11 | 2026-09-06 | 49 | 22 291 | snapshot×advertId | daily | 🔴 дыра 13–21.07 |
| booster avg_position | 2026-04-20 | 2026-09-04 | 138 | 1 968 | day×advert×nm | daily | ✅ |
| ad orders/revenue (attributed) | 2026-04-13 | 2026-09-05 | 146 | 5 870 | day×advert×nm | через витрину | ✅ |
| MART_SKU_DAILY (экономика) | 2024-09-05 | **2026-09-01** | — | 7 627 | day×nm_id | 07/09/12/16 МСК | 🔴 **застыла** |
| organic position | — | — | 0 | 0 | — | — | 🔴 **источника нет** |

История query-статистики покрывает **весь** запрошенный период с апреля 2026. Это не
случайность: backfill был выполнен пачками по 100 пар (ADS2_DESIGN §3.2).

---

## 5. Grain Contract

```
V_ADV_CAMPAIGN_STATS   date × advertId × nmId × appType × source_level     10 455 ключей, 0 дублей
V_ADV_QUERY_STATS      period_from × advert_id × nm_id × norm_query        40 794 ключа,  0 дублей
                       ⚠️ period_from = period_to у 100 % строк → это СУТОЧНЫЙ срез
V_ADV_QUERY_BIDS       snapshot_date × advert_id × nm_id × norm_query      снимок, не факт
V_ADV_COSTS            updDate × advertId × updNum                         🔴 180 ключей неуникальны
V_ADV_BOOSTER_STATS    date × advertId × nmId
FACT_ADS_SKU_DAILY     date × advert_id × nm_id                            5 870 ключей, 0 дублей
V_ADS_FUNNEL_QUERY_DAILY  day × nm_id × norm_query  (advert_id свёрнут в ARRAY_AGG)
V_ADS_SCREEN_QUERY     as_of_date × window_days × nm_id × norm_query
```

🔴 **Ключевое свойство `V_ADS_FUNNEL_QUERY_DAILY`:** `advert_id` схлопывается в массив,
а метрики суммируются. Это правильно для вопроса «продаёт ли ключ», но означает, что
**разложить query-метрику обратно по кампаниям в этом слое нельзя**. Для DASHBOARD B
(уровень кампании) источник другой — `V_ADV_CAMPAIGN_STATS`, и смешивать их в одном
join запрещено: получится fan-out.

Fan-out проверен и отсутствует: в `V_ADS_SCREEN_QUERY` справочник сведён к одной строке
на `nm_id` через `ROW_NUMBER()` **до** join, знаменатели (`sku_totals`) агрегированы до join,
сигналы свёрнуты один-к-одному.

---

## 6. Metric Coverage Matrix

### Campaign level

| Метрика | Статус | Источник |
|---|---|---|
| campaign/ad ID, name, type, status | ✅ AVAILABLE | `V_ADV_CAMPAIGNS` |
| start/end/change time | ✅ AVAILABLE | `raw_json.timestamps.*` (не извлечено в колонки) |
| SKU / nmId | ✅ AVAILABLE | `V_ADV_CAMPAIGN_STATS.nmId` |
| daily spend | ✅ AVAILABLE | `sum` (fullstats) + `updSum` (биллинг) |
| impressions, clicks | ✅ AVAILABLE | `views`, `clicks` |
| CTR, CPC | ✅ AVAILABLE (сырьё) / 🔴 **не переносятся в FACT** | `ctr`, `cpc` в RAW/VIEW |
| CPM | 🟡 DERIVABLE | `spend/views×1000`; в `MART_SKU_DAILY.cpm` уже есть |
| add-to-cart (atbs) | ⚠️ PARTIAL — есть в VIEW, 🔴 **теряется в FACT_ADS_SKU_DAILY** | `V_ADV_CAMPAIGN_STATS.atbs` |
| orders, order qty (shks) | ✅ / 🔴 `shks` теряется в FACT | `orders`, `shks` |
| attributed revenue | ✅ AVAILABLE | `FACT_ADS_SKU_DAILY.ads_revenue_raw_rub` (+ dedup-оценка) |
| CR | 🟡 DERIVABLE | по знаменателю (клики / корзины / показы) |
| DRR / ACoS / ROAS / CPO | ✅ AVAILABLE | `MART_SKU_DAILY` (`drr_orders`, `drr_buyouts`, `roas`, `acos`, `cpo_attributed`, `blended_cpo`, + 7d/14d) |
| bid (кампанийная ставка) | 🟡 PARTIAL — в `raw_json.nm_settings[].bids_kopecks.{search,recommendations}`, **не извлечена** | `V_ADV_CAMPAIGNS` |
| placement (search / recommendations) | 🟡 PARTIAL — **конфигурация**, не факт показов | `raw_json.settings.placements` |
| payment_type (cpm/cpc), bid_type (unified/manual) | 🟡 PARTIAL — в `raw_json`, колонка `payment_type` пуста у всех 430 | `V_ADV_CAMPAIGNS` |
| appType | ⚠️ значения 0/1/32/64 присутствуют, **семантика НЕ ДОКАЗАНА** | контракт v1 §P3 всё ещё OPEN |

**Фактический состав кампаний (доказано запросом):** все 430 — `type=9`; статусы 7/9/11;
`cpm` — 402, `cpc` — 28; `unified` — 218, `manual` — 212. У `unified` включены оба
размещения, у `manual` — только `search`.

### Search query level

| Метрика | Статус |
|---|---|
| norm_query, clicks, cpc, avg_pos, atbs, orders, shks, spend, currency | ✅ AVAILABLE |
| views, ctr, cpm | ⚠️ PARTIAL — **только CPM-кампании**; 22 983 строки из 40 794 (56 %) имеют `views IS NULL` |
| bid по ключу | ⚠️ PARTIAL — только `manual`+`cpm`, 10 nm_id, 90 запросов, с 14.08 |
| position / rank (рекламная) | 🟡 `avg_pos` есть на уровне запроса, `avg_position` — на уровне SKU (booster) |
| organic position | 🔴 **MISSING** |

🔴 `views`/`ctr`/`cpm` = NULL у CPC — это **свойство модели оплаты WB**, а не пропуск.
Подставлять нули запрещено (контракт v1 §1.1).

---

## 7. Search Query Intelligence Readiness

**Готовность: ВЫСОКАЯ по инфраструктуре, НИЗКАЯ по объёму статистики.**

Что есть: непрерывная суточная история 146 суток, 22 983 уникальных запроса, 19 nm_id,
слой Ads-4 с baseline'ами (SKU-ex-self → STORE-ex-self), гейтами доказательности и восемью
сигналами, уже сведённый в `V_ADS_SCREEN_QUERY`.

Гейты доказательности, действующие в проде:

| Гейт | Порог |
|---|---|
| `can_compare_ctr` | views ≥ 1000 |
| `can_judge_cart_cr` | clicks ≥ 60 |
| `can_judge_order_cr` | clicks ≥ 40 |
| `can_judge_order_carts` | atbs ≥ 40 |
| `can_compare_cpc` | clicks ≥ 20 |
| `evidence_status` | ACTIONABLE ≥ 40 кликов · OBSERVATIONAL ≥ 10 кликов или ≥ 1000 показов · иначе INSUFFICIENT |

🔴 **Фактическое распределение на 05.09.2026:**

| Окно | ACTIONABLE | OBSERVATIONAL | INSUFFICIENT | Расход ACTIONABLE |
|---|---:|---:|---:|---:|
| 28 дней | **3** | 13 | 330 | 1 195 ₽ |
| 90 дней | **18** | 108 | 1 124 | 16 710 ₽ |

**Следствие для дашборда C.** Классификация SCALE / DEFEND / OPTIMIZE / WASTE / LOW DATA
строится, но на 28 днях она даст три решаемых строки. **Основное окно дашборда C должно
быть 90 дней**, 28 дней — вторым слоем для тренда. Порогов ниже действующих вводить нельзя:
40 кликов при CR ≈ 6 % — это уже всего 2–3 заказа, доверительный интервал шире эффекта.

**Ограничение покрытия (доказано):** query-level расход = 224 737 ₽ против 543 817 ₽
расхода на уровне кампаний за тот же период = **41,3 %**, и доля падает:

| Период | camp spend | query spend | доля | camp clicks | query clicks |
|---|---:|---:|---:|---:|---:|
| 04–05.2026 | 242 016 | 125 566 | 51,9 % | 17 562 | 5 818 |
| 06–07.2026 | 249 269 | 80 629 | 32,3 % | 17 823 | 4 219 |
| 08–09.2026 | 52 532 | 18 542 | 35,3 % | 5 771 | 1 506 |

🔴 Это **не дефект загрузчика**: `V_ADV_QUERY_STATS_COVERAGE` показывает, что в scope
запроса попадает ~100 % суточного биллинга (coverage_ratio среднее 1,031, 134 из 146 суток
≥ 0,95). WB просто не отдаёт разбивку по поисковым запросам для не-поисковых размещений.
**В дашборде C обязана быть видимая метрика `ctr_coverage_spend_share` / доля объяснённого
бюджета**, иначе владелец решит, что видит весь расход.

---

## 8. Organic vs Paid Readiness

🔴 **NOT READY. Данных нет.**

Проверено: grep по всему репозиторию (`*.gs`, `*.sql`, `*.ts`, `*.py`, `*.md`) даёт только
методологические предупреждения в `ARCHITECTURE_EVETIS_ANALYTICS_v2.md` и строку
контракта v1: «Органические показы/клики — источника нет».

Что есть близкого и чем это **не** является:
* `RAW_WB_ADV_BOOSTER_STATS.avg_position` (138 суток, диапазон 2–890) — это позиция
  **рекламного** буста, day × advert × nm, без разбивки по запросам.
* `V_ADV_QUERY_STATS.avg_pos` — средняя **рекламная** позиция по ключу.
* «Общие заказы − рекламные заказы» органикой называть запрещено (`ARCHITECTURE §151`) —
  атрибуция WB не гарантирует непересечение.

**Варианты закрытия (решение владельца, не моё):**
1. Внешний источник. В сессии доступен MCP `sellmonitor` с методом
   `marketplace_analytics_get_product_search_query_positions`. Это **сторонний парсер**,
   не первоисточник WB; при использовании его надо помечать как `source=EXTERNAL_ESTIMATE`
   и никогда не смешивать с WB-фактом в одной колонке.
2. Собственный snapshot выдачи WB по списку ключей. Это отдельный ingestion, вне текущего
   контура, и он даёт позицию только с даты запуска — истории не будет.
3. Отказаться от дашборда D на этом этапе.

Рекомендация: **вынести DASHBOARD D из объёма первой очереди.**

---

## 9. Bid Management Readiness

**READY частично — и это две разные истории.**

**A. Ставка по поисковому запросу** (`normquery/get-bids`): 23 суточных снимка
14.08–06.09 (нет 01.09), 100 строк/сутки, 17 кампаний, 10 nm_id, 90 запросов.
Из 107 троек (advert × nm × query) **9 меняли ставку** за период наблюдения — динамика
есть, но разреженная. Ставки возвращаются только для `manual`+`cpm` (доказано probe v2/v4);
пустой ответ для `unified` — норма, а не пропуск.
🔴 Истории до 14.08.2026 не существует и получить её нельзя: endpoint'а истории у WB нет.

**B. Ставка кампании** (`raw_json.nm_settings[].bids_kopecks.{search,recommendations}`):
🔴 **лежит в BigQuery с 11.07.2026 в 22 291 снимке и НЕ извлечена ни в одну колонку.**
Это самый дешёвый прирост в проекте: одна вью поверх существующего RAW даёт 49 суток
истории кампанийных ставок по обоим размещениям + `payment_type` + `bid_type` +
`placements` + `timestamps.updated`. DDL — одна вью, ingestion не трогается.
Ограничение: дыра 13–21.07 (интерполировать через разрыв запрещено).

Пример фактического содержимого (из production):
`{"bids_kopecks":{"recommendations":10000,"search":90000},"nm_id":438775437,"subject":{"id":357,"name":"кремы"}}`

**Вывод:** decision-support по ставкам строится. Автоматических API-writes не будет —
это зафиксировано и не обсуждается на этом этапе.

---

## 10. Economic Integration

**Authoritative COGS: `evetis_ref.V_PRODUCT_COGS_EFFECTIVE`** — существует, актуален.
Покрытие: **25 из 25** WB-SKU, интервалы с 2024-09-07 по открытую дату,
`cogs_provenance_status ∈ {PROVEN_DOCUMENT, DERIVED_FROM_COMPONENTS, DERIVED_OR_TRANSFORMED}`.
Связка: `wb_raw.REF_SKU_MASTER (nm_id ↔ internal_sku)` → `V_PRODUCT_COGS_EFFECTIVE (internal_sku)`.

**Потребитель уже построен:** `wb_mart.V_MART_SKU_DAILY_COGS` даёт на грейне `day × nm_id`
поля `net_product_cogs_operational_rub`, `contribution_after_product_cogs_rub`,
`cogs_resolution_status` и явную оговорку в `economics_note`.

🔴 **Критично для терминологии.** Формула контрибуции (`pr_mart2b_sku_daily.sql:356`):

```
hybrid_day_contribution_pre_cogs = buyouts_rub − marketplace_fee_rub − logistics_cost_positive − ad_spend
```

То есть **реклама уже вычтена**, а `contribution_after_product_cogs_rub` — это
**contribution after ads and product COGS**. В неё **не входят**: хранение, штрафы,
приёмка и прочие удержания кабинета (WB не даёт их связки с SKU), фулфилмент, OPEX, налог.
Называть это «прибылью» **запрещено**. Корректное имя — `contribution_after_ads_and_product_cogs`.

**Экономические пределы DRR считаются. Доказательство (01.08–01.09.2026):**

```
contribution_before_ads = buyouts_rub − marketplace_fee − logistics − net_product_COGS
max_ad_drr (break-even) = contribution_before_ads / buyouts_rub
```

| internal_sku | nm_id | Выкупы ₽ | Вклад до рекламы ₽ | max DRR | факт DRR | Реклама ₽ |
|---|---:|---:|---:|---:|---:|---:|
| EVT-HC-HAND-300 | 252442517 | 85 886 | **−16 833** | **−19,6 %** | 16,0 % | 13 766 |
| EVT-FS-ACNE-30 | 305101361 | 31 853 | 8 127 | 25,5 % | 24,2 % | 7 700 |
| EVT-FT-ACNE-150 | 535581675 | 18 402 | 3 440 | 18,7 % | **28,5 %** | 5 251 |
| EVT-FC-ACNE-50 | 438775617 | 26 391 | 6 145 | 23,3 % | 18,1 % | 4 781 |
| EVT-EP-ENZYME-75 | 535580776 | 11 339 | 2 445 | 21,6 % | 21,1 % | 2 397 |
| EVT-HC-AMBER-300 | 593111985 | 7 552 | 1 128 | 14,9 % | **30,2 %** | 2 279 |
| EVT-FC-MOIST-50 | 438775437 | 21 429 | 5 971 | 27,9 % | 9,3 % | 1 984 |
| EVT-FS-MOIST-30 | 305101272 | 19 840 | 4 987 | 25,1 % | 9,9 % | 1 970 |
| EVT-SET-4PC-ACNE | 868597351 | 43 010 | 9 382 | 21,8 % | 4,2 % | 1 824 |
| EVT-FT-MOIST-150 | 535581674 | 9 174 | 1 195 | 13,0 % | **19,6 %** | 1 800 |
| EVT-SET-TON-CREAM-ACNE | 952068582 | 6 238 | 598 | 9,6 % | **16,1 %** | 1 006 |
| EVT-HC-CHERRY-300 | 593111986 | 10 120 | 728 | 7,2 % | 5,3 % | 540 |

**Единый допустимый DRR по ассортименту применять нельзя** — это теперь не тезис, а
измерение: break-even DRR разбегается от **−19,6 % до +27,9 %**. У `EVT-HC-HAND-300`
товар убыточен **до** всякой рекламы (вклад до рекламы отрицателен), поэтому для него
любой рекламный рубль увеличивает убыток, и `max_ad_drr` не определён как положительное
число. Пять SKU из двенадцати расходуют выше своего break-even.

⚠️ **Оговорка к таблице:** хранение и удержания кабинета в знаменателе не учтены (источник
не даёт SKU-связки), поэтому `max_ad_drr` здесь — **оптимистичная верхняя граница**.
`target_ad_drr` обязан оставлять резерв; конкретный процент резерва — решение владельца,
я его не выдумываю.

---

## 11. Data Quality Findings

**F-1 🔴 BLOCKER. `MART_SKU_DAILY` не собирается с 02.09.2026.**
22 прогона в статусе ERROR (последний 06.09 ~07:00 МСК), 25 COMPLETE, последний успех —
`target_date = 2026-09-01`. Ошибка одна и та же:
> `SKU_DAILY §pre: unknown money-pair (cost_category IS NULL) в LONG_MAPPED — расширить REF_COST_MAP`
> `at [wb_mart.sp_build_mart_sku_daily:101:3]`

Причина установлена запросом — две новые пары `(op_key, amount_field)` в отчётах WB **с 01.09.2026**:

| op_key | amount_field | строк | сумма ₽ | период |
|---|---|---:|---:|---|
| Возмещение издержек по перемещению и операционной обработке товара | `commission_amount` | 125 | −424,50 | 01–05.09 |
| Доставка | `logistics_amount` | 85 | +5 063,12 | 01–05.09 |

ASSERT сработал правильно (fail-closed) — витрина не собрала мусор. Лечится расширением
`wb_mart.REF_COST_MAP`, но это **отдельное решение по семантике удержаний**, не рекламная задача.

**F-2 🔴 BLOCKER. Мониторинг не наблюдает.**
`OPS_PIPELINE_REGISTRY` = 14 строк (включая `ads_daily`, `ads_costs`, `ads_query_bids`,
`ads_query_stats` с SLA и критичностью), `OPS_METRIC_COVERAGE` = 8, но
`OPS_HEALTH_STATE` = **0**, `OPS_ALERT_EVENT` = **0**, `OPS_INCIDENT` = **0**.
Реестр есть, детектора нет. Четырёхдневный отказ витрины прошёл незамеченным.

**F-3 🔴 Последствие F-1 для рекламы, измерено.**
`V_ADS_FUNNEL_SKU_28D.mart_ad_spend_rub` = **30 078 ₽**, тогда как
`FACT_ADS_SKU_DAILY` за то же окно (08.08–05.09) = **36 321 ₽**. Недостача **6 239 ₽ (17,2 %)** —
ровно расход 02–05.09, которого нет в застывшей витрине. Значит `query_spend_share_of_total`
и вся экономика в `V_ADS_SCREEN_SKU` сегодня **завышают долю query-расхода**.

**F-4 ⚠️ `V_ADV_COSTS`: 180 неуникальных ключей `(updDate, advertId, updNum)`** —
176 ключей по 2 строки и 4 по 3, суммарно 89 385 ₽. Одно из двух: либо WB отдаёт несколько
позиций на один документ списания (тогда это норма и ключ грейна надо расширить), либо это
дубли перечитывания. **НЕ ДОКАЗАНО** — требует проверки перед тем, как строить биллинговую
сверку на этом ключе. На итог это не влияет: `FACT_ADS_COSTS_DAILY` агрегирует по
`date × advert_id`, а сверка camp-vs-billed сходится (см. F-6).

**F-5 🔴 `V_ADVERTISING_RECONCILIATION_DAILY` сравнивает несравнимое.**
Суточный `ad_spend_attributed_rub` сопоставляется с `ad_spend_billed_rub` из финансовых
отчётов WB, которые приходят **недельными пачками**. Результат: 29 суток из 36 в августе
имеют `billed = 0` и отрицательную «недостачу», а в дни отчётов — скачок (16.08: attributed
19,13 ₽ против billed 15 652 ₽). **Дневная сверка биллинга бессмысленна по построению**;
сверять надо на неделе отчёта WB или накопленным итогом.

**F-6 ✅ Сверка «кампании ↔ биллинг рекламного кабинета» сходится.**
Отношение `campaign_stats.sum / adv_costs.updSum`: 1,024 (04–05) → 1,015 (06–07) → 1,003 (08–09).
Это здоровый источник и правильная основа для контроля расхода.

**F-7 ⚠️ Потери на переходе VIEW → FACT.** `atbs`, `shks`, `canceled`, `ctr`, `cpc`, `cr`
есть в `V_ADV_CAMPAIGN_STATS`, но отсутствуют в `FACT_ADS_SKU_DAILY`. `appType`
схлопывается. Для дашборда B (диагностика «низкий CTR / низкий CR») это критично.

**F-8 ⚠️ Атрибуция: multitouch.** 142 строки из 5 870 (2,4 %) помечены
`multitouch_ambiguous_flag`. `ads_revenue_raw_rub` = 2 101 724 ₽, dedup-оценка = 1 962 121 ₽
(−6,6 %); заказы 2 437 → 2 271. 🔴 **Обе величины нельзя смешивать в одной колонке дашборда**:
raw пригоден для сравнения кампаний между собой, dedup — для сопоставления с торговой выручкой.

**F-9 ⚠️ Семантика `appType` (0/1/32/64) НЕ ДОКАЗАНА** с 13.08.2026 (контракт v1, P3 = OPEN).
До сверки с кабинетом никаких срезов «по площадкам» строить нельзя.

**F-10 ⚠️ `RAW_WB_ADV_SEARCH_CLUSTERS` мертва** — 44 строки, один прогон 11.07.2026.
Заменена на `RAW_WB_ADV_QUERY_STATS`. Кандидат на депрекацию (не на удаление).

**F-11 ⚠️ Ads-2 и Ads-3 выполняются в остатке 6-минутного бюджета Apps Script**
после mart-критичного пути. Это осознанное решение (сбой ставок не должен ронять витрину),
но означает: при разрастании кампаний снимок ставок начнёт молча пропадать. Собственный
heartbeat у `ads_query_bids` в реестре объявлен обязательным — детектора нет (см. F-2).

**F-12 ⚠️ Metabase не видит рекламный слой Ads-4.** Из 46 карточек ни одна не читает
`V_ADS_SCREEN_QUERY` / `V_ADS_SCREEN_SKU`. Реклама представлена четырьмя скалярами
(46 «Реклама, ₽», 47 «ДРР», 79 «нераспределено», 85 «биллинг WB») и колонками в таблице SKU.

---

## 12. Missing Data / Gaps

| # | Чего нет | Можно ли получить | Стоимость |
|---|---|---|---|
| G-1 | Органическая позиция по запросу | ❌ не из WB API продавца | внешний источник или свой snapshot выдачи |
| G-2 | Кампанийные ставки как колонки | ✅ **уже в BigQuery**, в `raw_json` | 1 вью, ingestion не трогаем |
| G-3 | `payment_type` / `bid_type` / `placements` как колонки | ✅ там же | та же вью |
| G-4 | `atbs`/`shks`/`ctr`/`cpc`/`appType` в FACT | ✅ есть в VIEW | расширение `sp_build_mart_sku_daily` |
| G-5 | История ставок по ключу до 14.08.2026 | ❌ невосстановимо | — |
| G-6 | Семантика `appType` | 🟡 сверка с кабинетом WB вручную | 1 час владельца |
| G-7 | Разбивка расхода search / recommendations по факту | ❌ API не даёт | известна только конфигурация |
| G-8 | Query-разбивка для ~65 % расхода | ❌ WB не отдаёт | принять как ограничение и показывать |
| G-9 | Хранение/штрафы в разрезе SKU | ❌ WB не даёт связки | `max_ad_drr` остаётся верхней границей |

---

## 13. Proposed BigQuery Architecture

Принцип: **бизнес-логика в BigQuery, Metabase читает готовую семантику.** Новых RAW-таблиц
не требуется — весь первый этап строится вьюхами поверх существующего.

**Слой 1 — извлечение из `raw_json` (новое, дёшево):**
```
wb_raw.V_ADV_CAMPAIGN_CONFIG_SNAPSHOT
  грейн: snapshot_date × advert_id × nm_id
  из RAW_WB_ADV_CAMPAIGNS.raw_json:
    payment_type, bid_type, placement_search, placement_recommendations,
    bid_search_kopecks, bid_reco_kopecks, subject_id, subject_name,
    settings_updated_at, campaign_status
  ⚠️ дыра 13–21.07.2026 не интерполируется, помечается флагом gap_before
```

**Слой 2 — рекламный факт (расширение существующего):**
```
wb_mart.FACT_ADS_SKU_DAILY   + atbs, shks, canceled  (G-4)
wb_mart.FACT_ADS_CAMPAIGN_DAILY  (новая)
  грейн: date × advert_id
  spend, views, clicks, atbs, orders, shks, canceled, attributed_revenue
  + campaign_type, payment_type, bid_type, status на дату
```

**Слой 3 — экономические пределы (новое, ключевое для §9 ТЗ):**
```
wb_mart.V_ADS_SKU_ECONOMIC_LIMITS
  грейн: as_of_date × window_days × nm_id
  buyouts_rub, marketplace_fee, logistics, net_product_cogs,
  contribution_before_ads_rub,
  max_ad_drr_breakeven      = contribution_before_ads / buyouts_rub
  target_ad_drr             = max_ad_drr × (1 − reserve_share)   -- reserve задаёт владелец
  actual_drr_buyouts, drr_headroom_pp,
  economics_basis = 'BEFORE_ADS_AFTER_PRODUCT_COGS_EXCL_CABINET_COSTS'
  economics_note  = 'хранение, штрафы, приёмка, OPEX и налог НЕ включены — верхняя граница'
```

**Слой 4 — decision engine:**
```
wb_mart.V_WB_AD_DECISION_ENGINE
  грейн: as_of_date × window_days × entity_type × entity_key
  entity_type ∈ {'QUERY','SKU','CAMPAIGN'}
  поля из ТЗ §10 + evidence_status + recommendation + recommendation_reason + confidence
```

**Слой 5 — витрина дня:**
```
wb_mart.V_ADS_CONTROL_TOWER
  плоский список «на что смотреть сегодня» с bucket ∈
  {NEEDS_ATTENTION, SCALE, WASTE, BID_CANDIDATE, DATA_PROBLEM}
```

Ничего из `V_ADS_FUNNEL_*` / `V_ADS_SCREEN_*` не переписывается — эти вью уже работают и
проходят проверку на fan-out. Decision engine строится **поверх** них.

---

## 14. Proposed Metabase Dashboards

Новая коллекция `03 · Advertising`, чтобы не трогать замороженные dashboard 2 и 3
(Executive объявлен UX-FROZEN в CHANGELOG 28.08).

| Дашборд | Источник | Фильтры | Готовность |
|---|---|---|---|
| **A · Реклама: обзор** | `MART_SKU_DAILY` + `FACT_ADS_CAMPAIGN_DAILY` | период, SKU, кампания, тип | ✅ после ADS-1 |
| **B · Кампании** | `FACT_ADS_CAMPAIGN_DAILY` + `V_ADV_CAMPAIGN_CONFIG_SNAPSHOT` | период, статус, payment_type | после ADS-2 |
| **C · Поисковые запросы** | `V_ADS_SCREEN_QUERY` | **окно 90 дн. по умолчанию**, SKU, evidence_status, сигнал | ✅ уже сейчас |
| **D · Органика vs реклама** | — | — | 🔴 **отложен, источника нет** |
| **E · Ставки** | `V_ADV_QUERY_BIDS` + `V_ADV_CAMPAIGN_CONFIG_SNAPSHOT` | SKU, кампания | после ADS-2 |
| **F · Advertising Control Tower** | `V_ADS_CONTROL_TOWER` | без фильтров, «открыл утром» | после ADS-7 |

Обязательные элементы на A и C (иначе дашборд врёт по умолчанию):
* «Объяснено поисковыми запросами: X ₽ из Y ₽ (Z %)» — доля покрытия;
* «Данные актуальны на: …» с явным разрывом, если витрина отстаёт;
* разделение `attributed revenue (raw)` и `(dedup estimate)` разными карточками.

---

## 15. Decision Engine Design

Рекомендация выдаётся **только** при `evidence_status = ACTIONABLE`. Для OBSERVATIONAL
выдаётся наблюдение без действия, для INSUFFICIENT — `INSUFFICIENT_DATA`. Экономический
порог берётся из `V_ADS_SKU_ECONOMIC_LIMITS`, а не из константы.

| Рекомендация | Условие | Обоснование |
|---|---|---|
| `PAUSE_OR_EXCLUDE` | `orders = 0 AND spend > 0` при ACTIONABLE | нет заказов при достаточной выборке |
| `PAUSE_ECONOMIC` | `max_ad_drr ≤ 0` | товар убыточен до рекламы (случай HAND-300) |
| `DECREASE_BID` | `actual_drr > max_ad_drr` | расход выше точки безубыточности |
| `OPTIMIZE` | `target_drr < actual_drr ≤ max_ad_drr` | продаёт, но съедает резерв |
| `SCALE` | `actual_drr < target_drr` И `order_cr ≥ baseline` И есть запас объёма | прибыльно и есть куда расти |
| `HOLD` | всё в норме, значимых сигналов нет | |
| `TEST_LOWER_BID` | `cpc_vs_baseline > 1.3` при нормальном CR | цена трафика выше своей же базы |
| `INSUFFICIENT_DATA` | не ACTIONABLE | |

`confidence` = функция от (`evidence_status`, `funnel_monotonic`, `baseline_level`,
`days_with_spend`, свежесть `bid_snapshot_offset_days`). Строка с `funnel_monotonic = FALSE`
рекомендацию не получает — воронка противоречива.

🔴 `ORGANIC_STRONG` и `DEFEND` в первую очередь **не входят**: обе требуют органической
позиции, которой нет. Вводить их на прокси-признаке запрещено.

---

## 16. Refresh & Monitoring Strategy

Менять текущее расписание в первой очереди **не нужно** — оно доказано данными
(`sql/mart3/pr_mart3b3_freshness_readiness.sql`, 7 суток наблюдений).

| Конвейер | Сейчас | Предложение |
|---|---|---|
| `ads_daily` (campaigns+costs+fullstats) | daily 05:00 МСК, окно D−7…D−1 | без изменений |
| `ads_query_bids` | daily после родителя | без изменений; **добить пропуск 01.09 нельзя** |
| `ads_query_stats` | daily после ставок, окно D−1…D−7 | без изменений |
| `mart` | 07/09/12/16 МСК | без изменений **после** разблокировки |
| health detector | **не запущен** | 🔴 включить: писать в `OPS_HEALTH_STATE` / `OPS_ALERT_EVENT` |

Intraday для расхода **не предлагаю**: рекламный биллинг WB урегулируется до 15 суток
(`V_ADV_COSTS_DAY_COVERAGE.contract_settle_days`), а `runWbAdsDaily` уже перечитывает
D−7…D−1. Внутридневное обновление добавит шум, а не точность.

Минимальный набор алертов первой очереди:
1. `MART_RUNS`: два ERROR подряд по одному `target_date` → инцидент (F-1 был бы пойман 03.09);
2. `INGEST_RUNS.ads`: нет COMPLETE > 26 часов;
3. `RAW_WB_ADV_QUERY_BIDS_RUNS`: нет снимка за сутки (невосстановимо);
4. `V_ADV_QUERY_STATS_COVERAGE.coverage_ratio < 0.9` на закрытых сутках;
5. `MAX(day)` в `MART_SKU_DAILY` отстаёт от `MAX(date)` в `FACT_ADS_SKU_DAILY` > 1 суток.

---

## 17. Implementation Plan

| Stage | Содержание | Затрагивает | Критерий приёмки | Зависит от |
|---|---|---|---|---|
| **ADS-1** 🔴 | Разблокировать витрину: расширить `REF_COST_MAP` двумя парами; включить health detector в `wb_ops` | `wb_mart.REF_COST_MAP`, `wb_ops` | `MART_RUNS` COMPLETE за 05.09; `MART_SKU_DAILY` max(day) = D−1; `OPS_HEALTH_STATE` > 0 строк | — |
| **ADS-2** | `V_ADV_CAMPAIGN_CONFIG_SNAPSHOT` из `raw_json`: ставки, payment_type, bid_type, placements | `wb_raw` (+1 вью) | 430 кампаний, 49 суток, дыра 13–21.07 помечена, 0 дублей на ключе | ADS-1 |
| **ADS-3** | `FACT_ADS_CAMPAIGN_DAILY` + перенос `atbs`/`shks`/`canceled` в `FACT_ADS_SKU_DAILY` | `wb_mart`, `sp_build_mart_sku_daily` | `SUM(spend)` совпадает с `V_ADV_CAMPAIGN_STATS` до копейки; 0 дублей | ADS-2 |
| **ADS-4** | `V_ADS_SKU_ECONOMIC_LIMITS`: `max_ad_drr`, `target_ad_drr`, headroom | `wb_mart` (+1 вью) | 25/25 SKU; `contribution_before_ads` сходится с `V_MART_SKU_DAILY_COGS` + `ad_spend` | ADS-1 |
| **ADS-5** | Metabase: DASHBOARD C на `V_ADS_SCREEN_QUERY`, окно 90 дней, с метрикой покрытия | Metabase | доля объяснённого бюджета видна на экране; ACTIONABLE-строки читаются | ADS-1 |
| **ADS-6** | Metabase: DASHBOARD A + B | Metabase | сверка A с card 46/47 Executive: расхождение 0 | ADS-3 |
| **ADS-7** | `V_WB_AD_DECISION_ENGINE` | `wb_mart` | ни одной рекомендации при `evidence_status ≠ ACTIONABLE`; ручная проверка 10 строк | ADS-4 |
| **ADS-8** | Metabase: DASHBOARD E (ставки) | Metabase | видна дата снимка и `bid_snapshot_offset_days` | ADS-2 |
| **ADS-9** | `V_ADS_CONTROL_TOWER` + DASHBOARD F | `wb_mart`, Metabase | 5 корзин, каждая строка ведёт в C/B/E | ADS-7 |
| **ADS-10** | Алерты 1–5 (§16), CHANGELOG, документация, откат | `wb_ops`, docs | искусственный сбой ловится алертом | ADS-9 |
| **ADS-X** | Органика: решение по источнику, потом DASHBOARD D | — | 🔴 отложен до решения владельца | — |

Отличие от предварительной схемы ТЗ: **ADS-1 не «forensic audit» (он выполнен этим
документом), а разблокировка production.** Строить рекламный дашборд на витрине,
застывшей четыре дня назад, нельзя.

---

## 18. GO / NO-GO

## 🟢 **GO — с одним обязательным условием**

Данных достаточно, чтобы построить **DASHBOARD A, B, C, E**, экономические пределы DRR
и Decision Engine. Инфраструктура зрелая: ingestion не сбоил ни разу за 146 суток,
грейны чистые, дедуп во вьюхах, COGS покрывает 100 % ассортимента, слой Ads-4 уже
проходит проверку на fan-out.

**Условие: сначала Stage ADS-1.** Пока `MART_SKU_DAILY` стоит на 01.09, любая рекламная
витрина с экономикой будет врать на измеренные 17 % расхода, а отсутствие health-детектора
означает, что следующий такой отказ снова обнаружится случайно.

**Первым выполнять: Stage ADS-1.**
Он не рекламный по содержанию и небольшой по объёму, но без него всё остальное строится
на движущемся основании. Рекламная работа по существу начинается с ADS-2.

**Blockers, которые НЕ закрываются на этом этапе и должны быть приняты явно:**

| # | Блокер | Решение |
|---|---|---|
| B-1 | Органическая позиция отсутствует | DASHBOARD D исключён из первой очереди |
| B-2 | Query-разбивка покрывает ~35 % расхода | принять; показывать долю покрытия на экране |
| B-3 | ACTIONABLE-запросов на 28 днях — 3 | окно 90 дней как основное |
| B-4 | Семантика `appType` не доказана | никаких срезов «по площадкам» до сверки с кабинетом |
| B-5 | Истории ставок по ключу до 14.08 нет | невосстановимо, констатируем |
| B-6 | Хранение/штрафы не раскладываются на SKU | `max_ad_drr` = верхняя граница, а не точный порог |

---

**Изменений в production не вносилось. Коммитов нет. Ожидаю подтверждения по Stage ADS-1.**
