# STEP 5A — production hardening контура ADS → MART → UNITKA

Ветка `hardening/step5a-production`, база `origin/main = e2bf05854cda17d063cc7a206c2f56de2389bb8a`.
Контекст отказа: `docs/` + память проекта `ads-timeout-dag-incident-2026-09-15`.

## Что чинится

15.09.2026 `runWbAdsDaily` упёрлась в жёсткий 6-минутный лимит Apps Script
(360,875 c, «Exceeded maximum execution time»). Критичный путь загрузился,
но `ingestFinalizeByStatus_` не выполнился: при hard kill `finally` не отрабатывает.
`INGEST_RUNS` остался `STARTED` → LATEST-ATTEMPT freshness-гейт витрины не открылся →
`MART_SKU_DAILY` не собралась → `LAST_CLOSED_DATE` замер на 13.09 → Юнитка не закрыла 14.09.
Тот же отказ уже был 11.09 — в `INGEST_RUNS` осталось 4 вечных `STARTED`.

## Механизмы

| # | Механизм | Где | Что гарантирует |
|---|---|---|---|
| 1 | Deadline guard | `WbAdsRawLoader.gs` | операция не начинается, если пауза + запрос + резерв финализации не помещаются в стену |
| 2 | Stale-run reaper | `IngestRunLog.gs` | мёртвый `STARTED` переводится в `ERROR/STALE_RUN_TIMEOUT`, никогда в `COMPLETE` |
| 3 | Catch-up | `WbAdsDaily.gs` | повтор за D−1, только если LATEST-ATTEMPT не `COMPLETE` |
| 4 | Trigger provenance | `WbAdsDaily.gs` | `SCHEDULED` / `MANUAL` / `CATCHUP` различимы в журнале |
| 5 | Phase B costs | `WbAdsRawLoader.gs` | текст файла приведён к фактическому окну D−14 … D−1 |
| 6 | Tail clamp | `WbAdsQueryBids.gs`, `WbAdsQueryStats.gs` | необязательный хвост не может убить execution после критичного пути |
| 7 | Detectors | `sql/ops/step5a_detectors.sql` | 5 read-only проверок наблюдаемости |

## Резервы времени выведены из контракта, а не из прототипа

Фактический путь HTTP рекламы: `wbAdsHttp_` (`WbAdsProbe.gs`) → `wbFetchWithRetry_` (`Utils.gs`).

```
maxRetries  = WB_ADS_MAX_RETRY_429_ = 3        → до 4 попыток
baseDelayMs = WB_ADS_RETRY_BASE_MS_ = 20000
maxDelayMs  = 60000   ← ДЕФОЛТ wbFetchWithRetry_, вызовом НЕ переопределён
пауза n-й попытки = min(60000, 20000 * 2^(n-1)) → 20 c, 40 c, 60 c
Retry-After имеет приоритет и тоже ограничен 60 c
```

⇒ худший случай ОДНОГО вызова = **120 000 мс только на sleep** + 4 × RTT (до 180 c, если
каждый ответ отдаёт Retry-After у потолка).

🔴 Прототипные 45 000 мс «request reserve» **отвергнуты**: они не покрывают даже первую
паузу ретрая (20 c) вместе с медленным ответом. Но и покрыть худший случай нельзя:
резерв в 120–180 c отсекал бы `fullstats` почти каждый день, а `fullstats` mart-критичен.

Принятое решение — defence in depth, а не один гейт:

| Константа | Значение | Откуда |
|---|---|---|
| `WB_ADS_HARD_WALL_MS_` | 360 000 | лимит Apps Script |
| `WB_ADS_FINALIZE_RESERVE_MS_` | 45 000 | путь ПОСЛЕ последней операции: `wbAdsDailyFreshness_` ~5 c + `wbAdsDailyWriteStatus_` ~3 c + `ingestRunQuery_` (`timeoutMs: 30000`) = 38 c + запас. Прототипные 35 000 не покрывали даже один таймаут BQ-запроса |
| `WB_ADS_REQUEST_RESERVE_MS_` | 30 000 | один ответ WB; наблюдавшийся максимум 15.09 ≈ 25 c |
| `WB_ADS_RETRY_SLEEP_WORST_MS_` | 120 000 | документируется; **в гейте не используется** |

Остаточный риск — ретрай внутри уже допущенной операции — закрывают механизмы 2 и 3,
а не гейт. Полностью ограниченный worst case требует передать `maxDelayMs`/`maxRetries`
в `wbAdsHttp_` (`WbAdsProbe.gs`) — вне scope Step 5A, вынесено в техдолг.

### Бюджет до/после (симуляция 431 advertId, харнесс, те же входные данные)

| perCall | base elapsed | base headroom | patched elapsed | patched headroom |
|---|---|---|---|---|
| 3 c | 231 c | 129 c | 231 c (9 вызовов) | 129 c |
| 10 c | 294 c | 66 c | 294 c (9 вызовов) | 66 c |
| 15 c | 339 c | **21 c** ✗ | 267 c (7, stopped) | 93 c |
| 20 c | 343 c | **17 c** ✗ | 302 c (7, stopped) | 58 c |
| 25 c | 337 c | **23 c** ✗ | 291 c (6, stopped) | 69 c |
| 30 c | 321 c | **39 c** ✗ | 270 c (5, stopped) | 90 c |

В норме (3–10 c/запрос) поведение не меняется — все 9 вызовов проходят. При деградации WB
прогон переходит в `PARTIAL` с закрытым манифестом вместо смерти с вечным `STARTED`.

## Reaper: почему он не может открыть гейт

- `UPDATE` повторяет ВСЕ условия отбора (`status='STARTED'`, `source='apps_script'`,
  `started_at < cutoff`, `started_at >= activation`) и ограничивается списком найденных
  `run_id` → `COMPLETE`/`ERROR` неизменяемы, живой run закрыть нельзя, повтор идемпотентен.
- Reaper **никогда** не ставит `COMPLETE`. Freshness-гейт открывает только новый успешный run.
  Это прямо проверяется тестом 06+07: после reaper гейт ВСЁ ЕЩЁ закрыт.
- Порог ≥ 15 мин (`INGEST_STALE_THRESHOLD_FLOOR_MIN_`), меньшее значение отвергается.
- Activation floor — Script Property `INGEST_REAPER_ACTIVATION_TS`. Нет свойства → reaper
  выключен. 4 исторических `STARTED` (11.09 ads, 29.08 sales, 19.08 orders/sales) не трогаются.
- Cloud Run не затрагивается: у `LOADER_RUNS` уже есть собственный lease —
  `BqManifestStore.isActive` со скользящим `DEFAULT_STALE_STARTED_MS = 30 мин`.
  Поэтому shadow-stocks `STARTED` от 27.07 никого не блокирует и остаётся как есть.

## Phase B D−14 — доказательство

- `docs/ADS_COSTS_SNAPSHOT_CONTRACT_2026-08-20.md:77` — `Operational reread = D−14 … D−1`.
- `sql/mart/ads_spend_stage3b_validation.sql:17` — «B4b — переключение `V_ADV_COSTS` на
  `V_ADV_COSTS_SNAPSHOT`; B4a — константа 7 → 14 отдельным коммитом».
- Живой BigQuery: `V_ADV_COSTS` = `SELECT * FROM V_ADV_COSTS_SNAPSHOT` ⇒ **B4b выполнен**,
  canonical-слой уже ждёт 14-суточное окно, а production Apps Script читал 7.
- Определения `V_ADV_COSTS` / `V_ADV_COSTS_SNAPSHOT` / `V_ADV_COSTS_DAY_COVERAGE`
  **не менялись**: K9-сверка репо ↔ live по нормализованному SHA-256 — PASS по всем трём.
- `MART_SKU_DAILY.ad_spend` = `SUM(stats_spend_rub)` из `wb_mart.FACT_ADS_SKU_DAILY`,
  которая строится из `wb_raw.V_ADV_CAMPAIGN_STATS` (**fullstats**), а не из costs `upd`.
  ⇒ переход 7 → 14 не меняет `ad_spend` витрины и, следовательно, рекламу в Юнитке.

## Что НЕ делается в Step 5A

HOT/COLD фильтрация кампаний; миграция ADS в Cloud Run; изменение temporal-семантики
закрытых суток; `UNITKA_CELL_CHANGES`; отключение legacy Unitka; чистка Ozon-триггера;
удаление production probe-файлов; разделение Apps Script-проектов; автоматизация деплоя.

## Production writes, которые потребуются ПОЗЖЕ (сейчас не выполнены)

1. Вставка 5 файлов в Apps Script `evetis-wb-analitics` (ручное копирование — воспроизводимого
   деплоя нет).
2. Script Property `INGEST_REAPER_ACTIVATION_TS` = момент выката в ISO-8601 UTC.
   Без неё reaper выключен.
3. `wbAdsInstallCatchUpTriggers()` — два триггера 06:15 и 08:15 МСК.
4. Отдельным решением: закрытие 4 исторических `STARTED` (в Step 5A НЕ делается).
