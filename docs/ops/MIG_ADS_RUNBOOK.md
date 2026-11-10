# M3 — реклама WB: Apps Script → Cloud Run (runbook)

Статус на 10.11.2026: **код, тесты, Terraform и сверка — в Draft PR. Ничего не применено.**
Не выполнялись: `terraform apply`, деплой, запуск Job, правка Apps Script, обращения к WB API.

Общий план миграции — `docs/ops/WB_APPS_SCRIPT_TO_CLOUD_RUN_PLAN_2026-11-10.md` (PR #415), этап M3.

| Что | Где |
|---|---|
| Загрузчик `ads` (суточный + catch-up, журнал гейта) | `cloud/src/loaders/ads/index.ts` → `makeAdsLoader` |
| Загрузчик `ads-query` (хвост: Ads-3 ставки + Ads-2 query stats) | там же → `makeAdsQueryLoader` |
| Загрузчик `ads-costs-audit` (недельный аудит расходов) | там же → `makeAdsCostsAuditLoader` |
| Источники (campaigns, fullstats, costs, Ads-3, Ads-2) | `cloud/src/loaders/ads/sources.ts` |
| Нормализация строк RAW | `cloud/src/loaders/ads/normalize.ts` |
| Проверка контура shadow/prod | `cloud/src/loaders/ads/targets.ts` |
| Terraform | `infra/terraform/wb_ads_loader.tf` |
| Сверка паритета | `sql/mig_ads_parity.sql` |
| Тесты | `cloud/test/ads_loader.test.ts`, `cloud/test/ads_token.test.ts`, `cloud/test/ads_apps_script_parity.test.ts` |
| Флаги контуров (общая схема M2–M5, PR #423) | `var.m3_shadow_enabled` ← `M3_SHADOW_ENABLED`, `var.m3_prod_enabled` ← `M3_PROD_ENABLED` |

## 1. Семантика эталона (Apps Script) и как она перенесена

Эталон — `apps-script/ingestion/Wbadsdaily.gs`, `WbAdsRawLoader.gs`, `WbAdsProbe.gs`, `Wbadsbigquery.gs`,
`WbAdsQueryBids.gs`, `Wbadsquerystats.gs`, `IngestRunLog.gs`. Код Apps Script не менялся.

### 1.1 Окна запроса (watermark)

У рекламы нет watermark «от последней строки». Каждый прогон перечитывает **скользящие окна от вчерашних суток МСК**:

| Источник | Окно | Эталон |
|---|---|---|
| campaigns | без периода: полный список из `/adv/v1/promotion/count` | `loadWbAdsCampaignsRaw` |
| costs (`/adv/v1/upd`) | D−14 … D−1, одно окно (≤ 31 сут.) | `wbAdsCostsRangeBack_(14, 1)`, Фаза B |
| fullstats (`/adv/v3/fullstats`) | D−7 … D−1, одно окно | `wbAdsLast7Range_` |
| Ads-3 ставки (`get-bids`) | снимок «сейчас» | `loadWbAdsQueryBidsRaw` |
| Ads-2 query stats | 7 суток D−7 … D−1, по суткам, от свежих к старым | `wbAdsQsResolveDays_` |
| аудит расходов | D−45 … D−8, окна по 30 сут. (здесь по умолчанию D−46, см. D7) | `loadWbAdsCostsAudit` |

В Cloud Run окна считаются от `logical_period` (= D−1 МСК, `d1Moscow`), а не от часов внутри прогона.
При одном моменте запуска значения те же; журнал и окна не могут разойтись на полуночи.

### 1.2 Дедупликация и граничные ключи

Загрузчик **только дописывает RAW**. Дедуп и выбор версии делают вью, их загрузчик не трогает:
- `V_ADV_CAMPAIGNS` — по `advertId`, строка `raw` важнее `count_only`, затем `load_ts DESC, run_id DESC`;
- `V_ADV_CAMPAIGN_STATS` — по `(date, advertId, nmId, appType, source_level)`, только `processed_status='raw'`;
- `V_ADV_BOOSTER_STATS` — по `(date, advertId, nmId)`;
- `V_ADV_COSTS` = `V_ADV_COSTS_SNAPSHOT`: снапшот окна `(run_id, window_index)` с commit-marker в
  `RAW_WB_ADV_COSTS_RUNS` (`status='OK' AND http_success='true'`); на дату — последний снапшот по
  `window_completed_at`; ключ записи `(advertId, updTime)`;
- `V_ADV_QUERY_BIDS` — снимок целиком по `(snapshot_date, snapshot_ts, run_id)`;
- `V_ADV_QUERY_STATS` — срез целиком по `(period_from, period_to, run_id)`, только `OK`.

Поэтому повтор прогона не «дублирует» данные в смысле потребителей: он создаёт новый снапшот, и вью берут
последний. В пределах одной загрузки повтор load-job не удваивает строки: `jobId` детерминирован
на `(run_id, таблица, номер загрузки)`, повтор получает 409 и проверяет, что задача DONE.

🔴 `wbAdsBqCreateViews()` **не воспроизведена нигде**: в коде загрузчика нет DDL и записи во вью
(проверяет тест `wbAdsBqCreateViews не воспроизводится`). Вызов этой функции откатил бы cutover на
`V_ADV_COSTS_SNAPSHOT` (CLAUDE.md, `docs/ops/STAGE_B_BOOTSTRAP_RUNBOOK.md` §3).

### 1.3 Нормализация и `raw_json`

Перенесены дословно `wbAdvCampaignRow_`, `wbAdvCollectNmIds_`, `wbAdvFlattenFullstats_`,
`wbAdvCampaignStatNoStatsRow_`, `wbAdvCostRow_`, `wbAdsCostsWriteRunLog_`, `wbAdsQsFlatten_`, строки снимка ставок:
- тот же порядок проверки ключей (`advertId || advertID || id`, `nm || nms`, `sum_price ?? sumPrice` и т.д.);
- `raw_json` — `JSON.stringify` исходного элемента, обрезка до 45 000 символов;
- запись как `wbAdvBqAppendRows_`: `''`/`null`/`undefined` не пишутся (NULL), остальное — `String(v)`;
- `load_ts`, `window_completed_at`, `snapshot_ts` — `'yyyy-MM-dd HH:mm:ss'` **по Москве**, как `wbAdsNow_()`.
  Вью упорядочивают по `SAFE_CAST(... AS TIMESTAMP)`; ISO-время UTC «отстало» бы на 3 ч от строк Apps Script;
- `run_id` — `ADSRAW_yyyyMMdd_HHmmss_CR<хвост execution>` (аудит — `ADSAUDIT_…`). Префикс и позиции даты те же:
  на них опираются `run_id LIKE 'ADSRAW_%'` и разбор времени в `sql/ops/step5a_detectors.sql`.

Эквивалентность проверена исполнением: `ads_apps_script_parity.test.ts` запускает **сам** `runWbAdsDaily()`
из `.gs` в `node:vm` и Cloud Run-загрузчик на одной синтетической ленте WB. Все строки девяти таблиц совпадают
(кроме времени и `run_id`) в пяти сценариях: штатный день, 500 fullstats с дроблением, пустой upd + 429,
сбой Ads-3/Ads-2, сбой count. Исход журнала (COMPLETE с `rows_loaded`, либо ERROR с тем же кодом) — тоже.

### 1.4 Маппинг SKU

В рекламных RAW маппинга SKU нет: колонок `sku_match_status`/`internal_sku` нет ни в одной таблице `RAW_WB_ADV_*`
(живые схемы, `tables.get` 10.11.2026). Связь с SKU строится ниже по течению (`FACT_ADS_SKU_DAILY`).

### 1.5 Схема RAW

Девять таблиц `wb_raw`, все колонки `STRING NULLABLE`. Колонки и порядок — `cloud/src/loaders/ads/constants.ts`,
сверены с живыми таблицами:

| Таблица | Партиция | Кластер |
|---|---|---|
| `RAW_WB_ADV_CAMPAIGNS` | время загрузки, DAY | advertId |
| `RAW_WB_ADV_CAMPAIGN_STATS` | время загрузки, DAY | date, advertId, nmId |
| `RAW_WB_ADV_BOOSTER_STATS` | время загрузки, DAY | date, advertId, nmId |
| `RAW_WB_ADV_COSTS` | время загрузки, DAY | advertId |
| `RAW_WB_ADV_COSTS_RUNS` | нет | нет |
| `RAW_WB_ADV_QUERY_BIDS` / `_RUNS` | время загрузки, DAY | advert_id, nm_id / нет |
| `RAW_WB_ADV_QUERY_STATS` / `_RUNS` | время загрузки, DAY | advert_id, nm_id / нет |

Схема не меняется. Загрузчик не создаёт таблиц и не расширяет схему (`CREATE_NEVER`, `ignoreUnknownValues=false`).

### 1.6 Журнал прогона

| Apps Script | Cloud Run |
|---|---|
| `ingestRunStart_('ads', rng.to)` → `INGEST_RUNS` STARTED | `cli.ts` acquire → `LOADER_RUNS` STARTED, `loader_name='ads'`, `logical_period` = D−1 |
| `ingestFinalizeByStatus_`: OK/STALE → COMPLETE, иначе ERROR `ADS_<статус>` | handler вернулся → COMPLETE; `LoaderError('ADS_PARTIAL'|'ADS_ERROR')` → ERROR с тем же кодом |
| итог — `wbAdsOverallStatus_` по трём источникам | `overallStatus` (порт, те же 14 кейсов self-теста + SKIPPED) |
| `ingestReapStaleRuns_` (15 мин) | lease `cli.ts`: STARTED старше 30 мин не блокирует |
| catch-up: `wbAdsCatchUpDecision_` RUN / SKIP_COMPLETE / SKIP_RUNNING | окна Scheduler + lease: COMPLETE → `guard_skip`, свежий STARTED → ALREADY_RUNNING, ERROR → новый прогон |
| `ingestFinalizeByStatus_` ДО Ads-3/Ads-2 (Wbadsdaily.gs:212-231) | Ads-3/Ads-2 — отдельный загрузчик `ads-query` (свой Job, `loader_name='ads-query'`): журнал `ads` закрывается без хвоста |
| стена 6 мин + резервы Step 5A | дедлайн обработчика (`ADS_DEADLINE_MS` 1800 c / `ADS_QUERY_DEADLINE_MS` 480 c): новые вызовы WB и их повторы после него не начинаются; timeout Job'а = дедлайн + ≥ 420–600 c |
| `WB_ADS_STATUS` (лист Google) | структурный лог Cloud Logging (`ads_source`, `ads_costs_window`, …) |

Гейт витрины (`cloud/src/loaders/mart/bq.ts`) читает `V_INGEST_HEARTBEAT` = `INGEST_RUNS` ∪ `LOADER_RUNS`
(только `environment='prod'`): строка Cloud Run prod-`ads` за D−1, начатая после полуночи МСК, закрывает гейт
так же, как строка Apps Script. Shadow-строки в гейт не попадают.

### 1.7 Повторы и 429

`AdsHttp` (`cloud/src/loaders/ads/http.ts`) повторяет контракт `wbAdsHttp_` → `wbFetchWithRetry_`:
429/500/502/503/504 и сетевые исключения; 4 попытки; пауза `Retry-After`, иначе 20 c → 40 c → 60 c (≤ 60 c);
исчерпанные HTTP-повторы возвращают последний ответ, исчерпанное исключение бросается (источник → FAILED).
Свой модуль, а не `cloud/src/http/wbHttp.ts`: общий клиент не читает `Retry-After` и повторяет любые 5xx.
**401/403** — не как эталон (D11): сразу `WB_ADS_AUTH` без повторов, прогон прерывается.

### 1.8 Лимиты WB Advert API (как в эталоне)

`ids` ≤ 50 на fullstats и adverts; fullstats 3 запроса/мин → пауза 21 c; upd 1 запрос/с → 1,2 c; adverts 1,2 c;
get-bids ≤ 100 пар, 250 мс; normquery/stats ≤ 100 пар, 6,5 c; окно upd и fullstats ≤ 31 сутки.
Статистика — только статусы 7/9/11, порядок 9 → 11 → 7, кратность id сохраняется.
🔴 Лимиты — **на продавца**, а не на токен: shadow и Apps Script не должны работать одновременно (§3).

## 2. Эквивалентность Apps Script → TypeScript

| Apps Script | TypeScript |
|---|---|
| `runWbAdsDaily` / `runWbAdsDailyCore_` (критичная часть) | `makeAdsLoader` (`ads/index.ts`) |
| `runWbAdsDaily` после `ingestFinalizeByStatus_` (Ads-3 + Ads-2) | `makeAdsQueryLoader` (`ads/index.ts`) |
| `runWbAdsDailyCatchUp` + `wbAdsCatchUpDecision_` + триггеры 06:15/08:15 | окна Scheduler 05:00/06:00/08:00 + lease `cli.ts` |
| `wbAdsOverallStatus_` | `overallStatus` (`ads/status.ts`) |
| `wbAdsDailyFreshness_` | `AdsBq.maxStatsDate` + `isStale` |
| `loadWbAdsCampaignsRaw` | `loadCampaigns` |
| `loadWbAdsFullstatsRaw` + `wbAdsFullstatsTryLevel_` | `loadFullstats` + `fullstatsTryLevel` |
| `loadWbAdsCostsRaw` + `wbAdsCostsWriteRunLog_` | `loadCosts` + `writeCostsRunLog` |
| `loadWbAdsQueryBidsRaw` + `wbAdsCollectCpmManualPairs_` | `loadQueryBids` + `collectCpmManualPairs` |
| `loadWbAdsQueryStatsRaw` + `wbAdsQsProcessDay_` | `loadQueryStats` + `queryStatsDay` |
| `wbAdsQsPairsForDay_` / `wbAdsQsCostsByDay_` | `AdsBq.scopePairsForDay` / `AdsBq.costsByDay` |
| `loadWbAdsCostsAudit` | `makeAdsCostsAuditLoader` |
| `wbAdsHttp_` / `wbFetchWithRetry_` | `AdsHttp.request` |
| `wbAdvBqAppendRows_` → `bqLoadRows_` | `AdsBq.append` (`toBqRow` + load-job) |
| `getWbAdsToken_` (Script Properties) | Secret Manager `WB_TOKEN_ADS` |
| `wbAdsBqCreateViews` | **нет и не будет** |
| `WbAdsClustersJob` | **не переносится** (§5) |
| `loadWbAdsCostsBootstrapPass` / `loadWbAdsCostsRecheck` / backfill-обёртки | не переносятся: ручные разовые контуры |

## 3. Отличия от Apps Script — каждое с обоснованием

- **D1. Нет стены 6 минут — есть дедлайн обработчика.** Бюджет fullstats — 900 c от старта
  (`ADS_FULLSTATS_BUDGET_MS`); общий дедлайн `ads` — 1800 c (`ADS_DEADLINE_MS`), timeout Job'а — 2400 c.
  После дедлайна ни один вызов WB не начинается (`withDeadline`), и повтор, не успевающий закончиться до него, тоже
  (`AdsHttp`, `notAfterMs`): перерасход — максимум одна попытка (≤ 60 c), исход попадает в журнал до таймаута.
  В Apps Script fullstats обрывался по стене (инцидент 01.10.2026), прогон уходил в PARTIAL и закрывал гейт.
  В Cloud Run обрыва в штатном объёме нет. Бюджеты Ads-3 (90 c) и Ads-2 (120 c) оставлены как в эталоне —
  ради паритета. Наблюдение: в 3 из 8 последних прогонов Apps Script (03–10.11) Ads-3 закончился FAILED, Ads-2 —
  без OK-суток; причина не разбиралась (предположение — стена Step 5A).
- **D2. Ads-3/Ads-2 — отдельный загрузчик `ads-query`.** Apps Script финализировал heartbeat 'ads' ДО
  необязательной части, чтобы таймаут на хвосте не оставил STARTED. Здесь `cli.ts` закрывает журнал по возврату
  handler, поэтому хвост вынесен в свой Job со своим `loader_name='ads-query'` (prod 05:30/06:30/08:30 МСК).
  Он стартует, только если последняя попытка `ads` за тот же D−1 в том же контуре терминальна (COMPLETE или ERROR —
  эталон гонял хвост и при PARTIAL); иначе `ADS_QUERY_NOT_READY` и следующее окно. Свой `run_id` (`ADSRAW_…_CRQ…`):
  срезы Ads-2 канонизируются по `(period, run_id)`, общий run_id с повтором смешал бы две выдачи.
  Цена: снимок ставок снимается на ~25 мин позже, чем у Apps Script; `ads-query` COMPLETE и при FAILED отдельного
  источника (статусы — в своих run-log), как и в эталоне, где хвост в журнал не входил.
- **D3. Catch-up — окна Scheduler.** Отдельной функции решения нет: тот же LATEST-ATTEMPT делает lease.
  Порог «зависшего» STARTED — 30 мин вместо 15 (константа `cli.ts`, общая для всех загрузчиков).
  Окна 06:00 и 08:00 вместо ~06:15 и ~08:15: у одного cron одна минута.
- **D4. Fullstats не делает «sample»-вызов adverts.** `wbAdsFetchCampaigns_` после count запрашивал первую
  пачку `/api/advert/v2/adverts` ради пар для probe; fullstats его результат не использовал. Строки не меняются
  (доказано replay-тестом), запросов к WB на один меньше.
- **D5. Нет листа `WB_ADS_STATUS` и флага `WB_ADS_BQ_SINK`.** Cloud Run не пишет в Google Sheets; диагностика —
  в структурном логе. Флаг sink не нужен: приёмник всегда BigQuery.
- **D6. Нет токена → прогон падает целиком** (`ADS_TOKEN_UNAVAILABLE`). В Apps Script каждый источник ставил
  BLOCKED и итог был тот же ERROR; разница — нет строки BLOCKED в `RAW_WB_ADV_QUERY_BIDS_RUNS`.
- **D7. Аудит расходов начинается с D−46** (эталон D−45, возвращается `ADS_COSTS_AUDIT_FROM_DAYS=45`).
  Первый день рана `/adv/v1/upd` — краевое прочтение без записей 00:00–03:00 МСК
  (`docs/ops/ADS_COSTS_UTC_EDGE_2026-10-21.md`, PR #370 рекомендует начинать на сутки раньше).
  В prod аудит отказывает без `ADS_COSTS_AUDIT_ENABLED=1` (§6).
- **D8. Строже запись.** Load-job с `ignoreUnknownValues=false` и `CREATE_NEVER`: неизвестная колонка — ошибка,
  а не молчаливый пропуск. Схему Cloud Run не расширяет (Apps Script делал ALTER ADD COLUMN).
- **D9. Свежесть** берётся как `MAX(date)` из целевой RAW с `processed_status='raw'` (в shadow — из `__CR`), а не
  из `V_ADV_CAMPAIGN_STATS`. Значение то же (вью — дедуп этих строк, дедуп не меняет максимум) и влияет только на
  OK ↔ STALE, оба — успех.
- **D10. DRY_RUN=1** — только проверка контура и окон, без токена, WB и записи. Разбор — ровно `=== '1'`, как в
  `cli.ts` (`' 1'`, `'true'` — обычный прогон; иначе загрузчик и `cli.ts` разошлись бы).
- **D11. Токен — общий механизм M2–M5** (`cloud/src/http/wbToken.ts`, PR #423; урок 31.10.2026: истёкший токен поставок
  давал Apps Script 401 и молча глушил загрузку). Во всех трёх режимах после чтения секрета и ДО первого вызова WB:
  истёкший `exp` → `WB_ADS_TOKEN_EXPIRED`, пустой секрет → `WB_ADS_AUTH`, меньше 14 суток → warn
  `WB_TOKEN_EXPIRING_SOON`. Любой 401/403 → `WB_ADS_AUTH` без HTTP-повторов, и источник его **не глотает**:
  прогон падает этим кодом, а не `ADS_ERROR`/`ADS_PARTIAL`/`ADS_AUDIT_FAILED` (в эталоне — FAILED источника).
  Все три кода детерминированы (`classifyFailure`). Значения токена нет ни в ошибке, ни в логе: только имя секрета,
  срок, статус и обрезанная причина WB с вырезанными токеноподобными строками. Цена: окно `upd` с 401 не получает
  строку `ERROR` в `RAW_WB_ADV_COSTS_RUNS` (на снапшот не влияет — он берёт только `OK`).

## 4. Совместимость со снапшотом расходов v1 и v2 (PR #370)

Загрузчик воспроизводит форму данных, на которой стоят обе версии `V_ADV_COSTS_SNAPSHOT` /
`V_ADV_COSTS_DAY_COVERAGE`, и сам их не читает:
- одно окно D−14…D−1 на суточный ран, `window_index='0'`, `period_from/to` — запрошенные даты;
- сначала строки `RAW_WB_ADV_COSTS`, потом маркер `RAW_WB_ADV_COSTS_RUNS` (`status`, `http_success` строчными
  `'true'/'false'`, `window_completed_at` в МСК-формате); маркер пишется и на пустой, и на неуспешный ответ;
- «хвост» следующих суток (`updDate > period_to`) не отбрасывается — сохраняется в RAW и считается в
  `rows_out_of_window`, как в эталоне.

**v1 (живая):** поведение идентично Apps Script, включая известный дефект края: D−14 суточного рана —
краевое прочтение. Ничего не ухудшается и не улучшается.
**v2 (draft):** правило «прочтение d полное, только если ран запросил d−1» работает на тех же строках; хвост
берётся из окна d−1 того же рана. Для суточного рана полные D−13…D−1, как у Apps Script.
Для аудита двух окон (D−46…D−17, D−16…D−8) край только на D−46: стык окон внутри рана v2 закрывает хвостом.
⚠️ Переключение источника — граница по времени: строки Cloud Run и Apps Script упорядочиваются по
`window_completed_at`, поэтому формат времени обязан совпадать (МСК без зоны, §1.3) — совпадает.

## 5. `WbAdsClustersJob` — не переносить

- Пишет `RAW_WB_ADV_SEARCH_CLUSTERS` через replace-slice **листа** (`sheet.deleteRows`). При включённом BQ-sink
  `wbAdvRawEnsureSheet_` возвращает заглушку без `getLastRow` → старт джоба падает. Таблица не менялась с
  июля 2026 (`lastModifiedTime` по `tables.get`), в ней одна выборка за одну пару.
- Триггер «каждые 10 мин» снимает себя сам, если `CLJOB_STATUS != 'running'`; данных не даёт.
- Полнота по запросам уже обеспечена Ads-2 (`normquery/stats` по всем парам суток, `V_ADV_QUERY_STATS`), а
  `ADS2_DESIGN_2026-08-14.md` §4.5 прямо выводит старую таблицу из оборота. В DRO-1 источник помечен `MANUAL`.
- Включать его в суточный прогон незачем: потерь данных нет, потому что данных нет с июля.
Действие владельца (не срочно): проверить в «Триггерах» Apps Script, нет ли `wbAdsClustersJobTriggerTick_`, и
удалить его; удаление файла — cleanup-PR M6.

## 6. Недельный аудит расходов (`ads-costs-audit`)

- Ни разу не запускался в Apps Script (в `RAW_WB_ADV_COSTS_RUNS` нет `ADSAUDIT_`).
- Job `wb-ads-costs-audit-{shadow,prod}`, Scheduler по воскресеньям 04:30 МСК, оба **PAUSED**.
- 🔴 Под v1 аудит вреден: его краевые прочтения (первый день каждого рана) становятся последними снапшотами и
  занижают даты в P&L. Поэтому prod-загрузчик отказывает (`ADS_AUDIT_DISABLED`) без `ADS_COSTS_AUDIT_ENABLED=1`.
- Порядок включения: ACK ADS-UTC-EDGE (PR #370, v2 применён) → `gcloud run jobs update wb-ads-costs-audit-prod
  --update-env-vars ADS_COSTS_AUDIT_ENABLED=1` → resume Scheduler. Shadow-аудит безопасен (пишет только `__CR`).
- Свой `loader_name` (`ads-costs-audit`): строка аудита не может стать «последней попыткой» `ads` для гейта.

## 7. Выпуск: shadow → паритет 5 дней → переключение вечером

### 7.1 Shadow (OWNER ACK M3-SHADOW)

0. Владелец ставит переменную репозитория `M3_SHADOW_ENABLED=true` (infra.yml → `TF_VAR_m3_shadow_enabled`).
   До этого ни одного ресурса M3 в конфигурации нет: оба флага по умолчанию `false`, весь `wb_ads_loader.tf` новый
   (в `origin/main` его нет), поэтому план без флагов по этому файлу — 0 ресурсов.
1. `infra.yml` → plan (`M3_PROD_ENABLED` не задана → `m3_prod_enabled=false`) с `targets`:
   `google_cloud_run_v2_job.wb_ads_shadow,google_cloud_scheduler_job.wb_ads_shadow,google_cloud_run_v2_job_iam_member.scheduler_ads_shadow_invoke`
   Ожидается **39 create, 0 update/delete**: `google_service_account.wb_ads_shadow[0]`; `wb_ads_jobuser["shadow"]`;
   `wb_ads_loader_runs["shadow"]`; 9 `google_bigquery_table.ads_cr[…]`; 9 `wb_ads_shadow_write_cr[…]`;
   6 `wb_ads_read_sources["shadow:…"]`; `wb_ads_token["shadow"]`; `wb_ads_actas_deployer["shadow"]`,
   `wb_ads_actas_terraform["shadow"]`; по 3 Job / invoker / Scheduler (`wb-ads`, `wb-ads-query`, `wb-ads-costs-audit`).
   Ни одного prod-ресурса и ни одного изменения `sa-loaders-*`. Для `expected_changes` (tools/tf_plan_guard.py) — те же
   39 адресов `=create`.
2. Apply тем же планом.
3. Merge PR → `deploy-shadow.yml` обновит образ трёх shadow-Job'ов (только образ: env shadow принадлежит Terraform).
4. `scheduler-control`: `wb-ads` / shadow / `run-now` вне окон Apps Script (не 05:00–05:20, 06:15, 08:15) —
   первый прогон под наблюдением. Проверить `LOADER_RUNS` (`environment='shadow'`, `loader_name='ads'`) и
   строки в девяти `__CR`.
5. `scheduler-control`: `wb-ads-query` / shadow / `run-now` после COMPLETE `ads` (проверить `RAW_WB_ADV_QUERY_*__CR`).
6. `scheduler-control`: `resume` для `wb-ads` (05:40, 07:40 МСК) и `wb-ads-query` (06:05, 08:05) в shadow.

### 7.2 Паритет — 5 дней подряд

Ежедневно после 08:00 МСК — QUERY 1 из `sql/mig_ads_parity.sql`. Критерий:
- 5 последовательных дней без `FAIL` и `MISSING`;
- `costs_verdict = EXACT` все 5 дней;
- не меньше 3 дней `verdict = EXACT`; `DATA_DRIFT` разобран QUERY 3 (дрейф допустим только в метриках свежих суток);
- `SOURCE_GAP` (одна сторона не COMPLETE) день не засчитывает и не обнуляет счёт — разбор вручную;
- `aux_verdict = REVIEW` (Ads-3/Ads-2) переключение не блокирует, но записывается в отчёт приёмки.

### 7.3 Переключение (вечер, после 20:00 МСК; OWNER ACK M3-CUTOVER)

1. Предусловия:
   - паритет 7.2 пройден;
   - **общий PR (готовит координатор миграции; в этом PR файлы не меняются)** переводит на `V_INGEST_HEARTBEAT`
     всех читателей heartbeat `ads` из `INGEST_RUNS`: `sql/mart/dashboard_layer_v1.sql:154-155`
     (`V_DATA_FRESHNESS`, слои `ads_query_bids` / `ads_costs` / `ads_fullstats`), `sql/health/dro1_01_contract.sql:55`
     и пробу DRO-1 `sql/health/dro1_02_probe.sql` (§9, п. 3) — применён и проверен. Без него после переключения
     витрина свежести и DRO-1 покажут рекламу «без прогонов», хотя гейт витрины будет открыт.
2. Владелец ставит переменную репозитория `M3_PROD_ENABLED=true` (infra.yml → `TF_VAR_m3_prod_enabled`).
   До этого ни одного prod-ресурса M3 в конфигурации нет.
3. `infra.yml` plan с `targets`: `google_cloud_run_v2_job.wb_ads_prod,google_cloud_scheduler_job.wb_ads_prod,google_cloud_run_v2_job_iam_member.scheduler_ads_prod_invoke,google_monitoring_alert_policy.wb_ads_failed`
   Ожидается **31 create, 0 update/delete**: `google_service_account.wb_ads_prod[0]`; `wb_ads_jobuser["prod"]`;
   `wb_ads_loader_runs["prod"]`; 9 `wb_ads_prod_write[…]`; 6 `wb_ads_read_sources["prod:…"]`; `wb_ads_token["prod"]`;
   2 actAs; по 3 Job / invoker / Scheduler (все PAUSED); `wb_ads_failed[0]` (если задан `UNITKA_ALERT_EMAIL`, иначе 30).
   Аудит prod создаётся тем же apply, но его Scheduler остаётся PAUSED, а загрузчик без `ADS_COSTS_AUDIT_ENABLED=1`
   отказывает (§6).
4. `deploy-prod.yml` с `only_job=wb-ads` (тот же digest, что проверен в shadow; режим и проверка охвата —
   общий PR #423, `tools/deploy_scope_check.py`) — образ попадёт ТОЛЬКО в `wb-ads-prod`, `wb-ads-query-prod`,
   `wb-ads-costs-audit-prod`, любой другой изменившийся Job валит выкат. Readback: `gcloud run jobs describe wb-ads-prod` — образ = digest, env `ADS_*_TABLE` =
   боевые имена, `ENVIRONMENT=prod`, SA `sa-wb-ads-prod`.
5. Apps Script (владелец, UI «Триггеры»): удалить триггеры `runWbAdsDaily` и оба `runWbAdsDailyCatchUp`
   (или `wbAdsRemoveTrigger()` для суточного; для catch-up функции удаления нет — вручную).
6. `scheduler-control`: `wb-ads` / prod / `run-now`, затем `wb-ads-query` / prod / `run-now` — дымовой прогон за текущие D−1 (запишет новый снапшот тех же
   окон; вью возьмут его как последний). Проверка: `LOADER_RUNS` prod `ads` COMPLETE; в `RAW_WB_ADV_COSTS_RUNS`
   маркер `ADSRAW_…_CR…` с `status='OK'`; `V_INGEST_HEARTBEAT` показывает `cloud_run`.
7. `scheduler-control`: `resume` для `wb-ads` и `wb-ads-query` в prod; `pause` для обоих в shadow (лимиты WB общие).
8. Утро: к 05:15 `LOADER_RUNS` prod `ads` COMPLETE за D−1; в 07:00 `MART_RUNS` COMPLETE (не FRESHNESS_GATE);
   `V_ADV_COSTS_DAY_COVERAGE` — без новых незакрытых дат относительно вчера.

## 8. STOP и откат

**STOP** (переключение не продолжается / откатывается):
- prod `ads` ERROR в двух окнах подряд (05:00 и 06:00) или `MART_RUNS` с `FRESHNESS_GATE` из-за `ads`;
- маркер расходов Cloud Run с `status='OK'`, но `returned_rows` расходится с данными окна;
- в боевой RAW появилась строка без префикса `ADSRAW_`/`ADSAUDIT_` или с `period_*` вне ожидаемых окон;
- любая запись в `__CR` из prod или в боевую таблицу из shadow (проверка контура это исключает — значит дефект).

**Откат** (данные не откатываются — RAW append-only, вью сами берут последний снапшот):
1. `scheduler-control`: `pause` для `wb-ads` и `wb-ads-query` в prod. Полный вывод prod-контура — снять
   `M3_PROD_ENABLED` и apply (destroy только prod-ресурсов M3; боевые RAW не затрагиваются — их Terraform не создавал).
   Полный вывод shadow-контура — снять `M3_SHADOW_ENABLED` и apply: destroy shadow-ресурсов M3, **включая 9 таблиц
   `__CR` с их строками** (`deletion_protection = false`); выгрузить их до этого, если нужны для разбора паритета.
2. Apps Script: `wbAdsInstallDailyTrigger()` и `wbAdsInstallCatchUpTriggers()`.
3. Если D−1 ещё не закрыт: запустить `runWbAdsDaily` вручную. Catch-up Apps Script смотрит только `INGEST_RUNS`
   и Cloud Run-строк не видит — прогон будет, это безопасно (новый снапшот).
4. Строки Cloud Run в боевых RAW остаются: следующий снапшот Apps Script по тем же ключам/окнам их перекрывает.
   Если дефект в строках вне окон (старше D−14 по расходам, D−7 по fullstats) — отдельный разбор с владельцем,
   удаление только по отдельному ACK.

## 9. Что нужно от владельца

1. **Токен.** Apps Script берёт `WB_TOKEN_PROMOTION` из Script Properties; Cloud Run — Secret Manager
   `WB_TOKEN_ADS` (версия 1 от 26.07.2026). Подтвердить, что это действующий токен категории «Продвижение», или
   добавить новую версию секрета тем же токеном (значение не выводить). Проверить можно только запросом к WB —
   в этой работе не делалось. Срок `exp` загрузчик проверяет сам до первого запроса (D11): `WB_ADS_TOKEN_EXPIRED` /
   `WB_ADS_AUTH` в алерте = выпустить новый токен и добавить версию секрета, повтор Job'а не поможет.
2. **ACK M3-SHADOW** на план §7.1 (39 create, только shadow) и переменная `M3_SHADOW_ENABLED=true`.
3. **Читатели heartbeat из `INGEST_RUNS`** — `sql/health/dro1_02_probe.sql` (`ads_daily`, `orders`, `sales`),
   `sql/health/dro1_01_contract.sql:55`, `sql/mart/dashboard_layer_v1.sql:154-155` (`V_DATA_FRESHNESS`:
   `ads_query_bids` / `ads_costs` / `ads_fullstats`). После переключения heartbeat переедет в `LOADER_RUNS`.
   Перевод на `V_INGEST_HEARTBEAT` — **отдельный общий PR (M2/M3, готовит координатор)**, обязательное
   предусловие M3-CUTOVER (§7.3, п. 1). В этом PR эти файлы не меняются.
3a. **Флаги контуров** (общая схема M2–M5, `tools/tests/test_stream_enable_flags.py`): `M3_SHADOW_ENABLED=true` —
   только на ACK M3-SHADOW, `M3_PROD_ENABLED=true` — только на OWNER ACK M3-CUTOVER. Других флагов включения у M3 нет.
4. **Детекторы Step 5A** (`sql/ops/step5a_detectors.sql`): `ADS_RUNTIME_NEAR_LIMIT` мерит fullstats против стены
   Apps Script 300 c (у Cloud Run её нет), а `ADS_TAIL_MISSING` ищет снимки Ads-3/Ads-2 с тем же `run_id`, что у
   fullstats — у Cloud Run хвост пишется своим `run_id` (`…_CRQ…`), детектор покажет MISSING. Обоим нужна правка
   до cutover (тот же общий PR или отдельный).
5. **Аудит расходов** — только после ACK ADS-UTC-EDGE (PR #370); затем `ADS_COSTS_AUDIT_ENABLED=1` и resume.
6. **Триггер `WbAdsClustersJob`** — проверить и удалить, если остался (§5).

## 10. Тесты

```
cd cloud
npx tsc --noEmit -p .
npx vitest run test/ads_loader.test.ts test/ads_token.test.ts test/ads_apps_script_parity.test.ts
npm run lint
```
Все тесты офлайн на синтетике (`cloud/test/ads_fixture.ts`): идентификаторы, суммы и названия выдуманы.
