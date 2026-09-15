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


## PRE-PR REVIEW (найдено и исправлено до PR)

| # | Severity | Дефект | Исправление |
|---|---|---|---|
| 1 | HIGH | `wbAdsInstallCatchUpTriggers` при `have === 1` доливал ещё два и оставлял ТРИ триггера | установщик приводит состояние к ровно двум из любого стартового: сносит свои `runWbAdsDailyCatchUp` и создаёт заново; чужие триггеры не трогает |
| 2 | HIGH | TOCTOU: решение catch-up принималось ДО ожидания ScriptLock (до 30 c). Конкурент мог за это время закрыться `COMPLETE`, catch-up запускал полный дублирующий прогон и, упав в `PARTIAL`, **закрывал уже открытый freshness-гейт** | `runWbAdsDailyCore_(triggerType, recheckAfterLock)` — под локом решение перепроверяется, при изменении прогон отменяется и lock освобождается |
| 3 | MEDIUM | детектор `STALE_INGEST_RUN` вечно давал `CRITICAL` на 4 исторических `STARTED`, которые закрываются только отдельным approval | строки старше 24 ч → `HIGH (historical)`, свежие остаются `CRITICAL`. На живых данных `CRITICAL` сейчас 0 строк |

Негативный контроль: оба новых теста (12, 13) падают на `2f060aa` и проходят после фикса.

### Граничные значения deadline guard (прогнаны на реальной функции)

```
wall=360000  finalize=45000  request=30000  →  mustEndBy = t0 + 315000
pause = 21 c (fullstats):
  elapsed 263,999 c → true     (need t+314,999 ≤ t+315)
  elapsed 264,000 c → true     (граница включительно, условие <=)
  elapsed 264,001 c → false
pause учитывается: при elapsed 264 c и pause 0 → need t+294 c; при pause 21 c → need t+315 c
```

### Reaper при некорректной activation (прогнано, считались вызовы UPDATE)

| activationTs | status | reaped | UPDATE-вызовов |
|---|---|---|---|
| `not-a-timestamp` | `FAILED` | 0 | **0** |
| `''` | `DISABLED_NO_ACTIVATION_TS` | 0 | **0** |
| `'   '` | `FAILED` | 0 | **0** |
| в будущем | `NOTHING_TO_REAP` | 0 | **0** |
| ровно `= started_at` | `REAPED` | 1 | 1 |
| на 1 мс позже `started_at` | `NOTHING_TO_REAP` | 0 | **0** |
| порог 5 мин (< floor) | `FAILED` | 0 | **0** |

Ни в одном некорректном случае UPDATE не выполняется. Граница `started_at >= activation`
включительная — задокументировано. `TIMESTAMP(@activation)` без указания пояса трактуется
BigQuery как UTC: значение свойства задавать в ISO-8601 с `Z`.

### D−14: нагрузка на WB API (эмпирика по `RAW_WB_ADV_COSTS_RUNS`)

Окно costs запрашивается ОДНИМ запросом, пока ≤ 31 суток (`wbAdsSplitPeriod_`), поэтому
7 → 14 суток **не меняет число HTTP-запросов**: было 1, стало 1.

| суток в окне | прогонов | строк в среднем | строк/сутки | сек в среднем | сек максимум |
|---|---|---|---|---|---|
| 7 | 19 | 37,2 | 5,3 | 5,7 | 11,0 |
| 25 | 1 | 142 | 5,7 | 6,6 | 6,6 |
| 30 | 9 | 464,7 | 15,5 | 6,0 | 10,2 |
| 31 | 3 | 552 | 17,8 | 7,8 | 14,2 |

Длительность практически не зависит от ширины окна — она определяется ответом WB, а не
объёмом. Ожидание для 14 суток: ~75 строк, те же 3–14 c. На фоне критичного пути 230–300 c
это ≤ 1 % и к стене 360 c не приближает. Плюс deadline guard теперь гарантирует, что даже
при деградации прогон уйдёт в `PARTIAL` с закрытым манифестом, а не умрёт.

## Production writes, которые потребуются ПОЗЖЕ (сейчас не выполнены)

1. Вставка 5 файлов в Apps Script `evetis-wb-analitics` (ручное копирование — воспроизводимого
   деплоя нет).
2. Script Property `INGEST_REAPER_ACTIVATION_TS` = момент выката в ISO-8601 UTC.
   Без неё reaper выключен.
3. `wbAdsInstallCatchUpTriggers()` — два триггера 06:15 и 08:15 МСК.
4. Отдельным решением: закрытие 4 исторических `STARTED` (в Step 5A НЕ делается).


## Rollback — честная формулировка

🔴 Обнулять `WB_ADS_REQUEST_RESERVE_MS_` / `WB_ADS_FINALIZE_RESERVE_MS_` — это
**АВАРИЙНОЕ СНЯТИЕ ЗАЩИТЫ, а не безопасный режим работы.** Проверено на реальной функции:
при `reserve = 0` и `elapsed = 339 c` (ровно профиль прогона, который 15.09 убил execution)
`wbAdsCanStartOp_` возвращает **true**, то есть операция допускается и поведение
возвращается к доинцидентному. Остаётся только сырая стена 360 c без запаса на финализацию —
именно та конфигурация, в которой манифест остаётся `STARTED` навсегда.

Применять только если гейт ошибочно блокирует штатную загрузку, и только как временную меру
с последующим разбором. Безопасные пути отката, в порядке предпочтения:

1. Выключить reaper — удалить Script Property `INGEST_REAPER_ACTIVATION_TS` (код не трогается).
2. Выключить catch-up — удалить два триггера `runWbAdsDailyCatchUp`.
3. Откатить конкретный коммит `git revert` — коммиты не пересекаются по хункам.
4. Вставить в Apps Script предыдущие версии файлов (md5 зафиксированы в
   `step5a_base_regression.txt`, воспроизводимы из Drive-экспорта).
5. И только в крайнем случае — обнуление резервов, с осознанием, что это возвращает
   режим, в котором произошёл инцидент.
