# PR-Mart3a — журнал ранов загрузчиков (heartbeat для MART). REV6 (аудит)

**Дата:** 2026-08-03  **Статус:** REV6 — закрыт 1 блокер REV5 (validation §8 считал `COUNTIF(COMPLETE)`, а не latest-attempt). Ожидает применения + 1–2 суток наблюдения.

## Правки REV6 (1 блокер наблюдения)
1. **Validation §8 — latest-attempt по дням (та же семантика, что §7).** Было: `COUNTIF(status='COMPLETE') AS complete_runs`
   — ранний COMPLETE + поздний ERROR (`00:10 COMPLETE → 08:00 ERROR`) давал `complete_runs=1` (ложная зелень),
   хотя последняя попытка дня упала; §7 при этом даёт `FALSE` — критерии расходились. Стало: плотный спайн дней
   (`GENERATE_DATE_ARRAY(D-7..D-1)`) `CROSS JOIN` обязательные загрузчики `LEFT JOIN` последняя попытка за
   `(logical_period × loader)` (`ROW_NUMBER() ... ORDER BY started_at DESC, run_id DESC`, `rn=1`) →
   `latest_complete = status='COMPLETE' AND completed_at IS NOT NULL`. Отсутствующий день/loader остаётся строкой
   `FALSE`, а не исчезает. **Критерий готовности к Mart3b:** `latest_complete = TRUE` для всех трёх на КАЖДЫЙ день.
   Проверено на BQ синтетическим mock: false-green день `00:10 COMPLETE → 08:00 ERROR` теперь `latest_complete=FALSE`,
   отсутствующий loader и пустой день → `FALSE`. PR-нота (шаг наблюдения) синхронизирована.

## Правки REV5 (1 production-блокер)
1. **Ads: fail-open при вычислении `overall` до whitelist.** Было (в исходном `runWbAdsDaily`, до Mart3a):
   `hasError = results.some(x => x && x.status && x.status !== 'OK' && x.status !== 'PARTIAL')`. Результат `null`,
   `{}` или объект без `status` **не** считался ошибкой → `hasError=false`, `hasPartial=false` → `overall` = `OK`/`STALE`
   → whitelist пишет `COMPLETE` → Mart3 пропустил бы сборку, хотя один источник рекламы не подтвердил успех.
   Тот же класс fail-open, что закрывали для неизвестных статусов, но **раньше whitelist**, на этапе `overall`.
   **Правка:** итог вынесен в чистую функцию `wbAdsOverallStatus_(results, stale)` (offline, без API/BQ) со строгой
   валидацией: не массив / не ровно 3 результата / любой `null`/`{}`/без `status` / неизвестный статус → **`ERROR`**.
   `OK+OK+OK → OK` (или `STALE` при `stale===true`); `≥1 PARTIAL` без иных ошибок → `PARTIAL`.
   **Регрессионный self-test `wbAdsSelfTestOverallStatus()`** (14 кейсов, включая `[OK,null,OK]`, `[OK,{},OK]`,
   `[OK,UNKNOWN,OK]`, мало/много результатов, регистр, приоритет ERROR/PARTIAL над STALE) — прогнан offline в node,
   14/14 OK. Генератор `apply_mart3a_patches.py` расширен режимом `replace` и **воспроизводит** правку из чистого
   исходника (повторный прогон не откатит фикс); все 4 `.gs` — `node --check` зелёный.

## Правки REV4 (1 production-блокер)
1. **Freshness-gate: LATEST-ATTEMPT вместо «существует хоть один COMPLETE».** Было: `EXISTS(... status='COMPLETE'
   AND logical_period=target ...)` — находил ЛЮБОЙ ранний COMPLETE и пропускал сборку, даже если последняя попытка
   загрузчика за те же сутки упала. Сценарий: `00:10 COMPLETE → 08:00 ERROR/PARTIAL → 09:00 build` собрал бы витрину
   на неполных данных (для hourly orders/sales — ночной успех мог не захватить поздние правки WB, а catch-up упал).
   Стало: берём **последнюю попытку** за `logical_period` (`ROW_NUMBER() ... ORDER BY started_at DESC, run_id DESC`,
   `rn=1`) и требуем, чтобы **именно она** была `COMPLETE`. Обязательный список источников — слева через `LEFT JOIN`:
   полностью отсутствующий загрузчик даёт `covers_target=FALSE`, а не исчезает из результата (fail-closed).
   Синхронизированы **три места** одного публичного контракта: `MART3_ORCHESTRATION_DESIGN` (контракт гейта),
   `pr_mart3a_ingest_runs.sql` (блок 3 — эталон для Mart3b), `pr_mart3a_validation.sql` §7.
   `started_at >= полночь(target+1)` ⟹ `completed_at >= полночь` (строже и безопаснее): ран, стартовавший до полуночи,
   относится к другому `logical_period` и в набор не попадает.
   **Правило контракта одной строкой:** *latest attempt for loader + logical_period must be COMPLETE.*

   *Неблокирующее (принято к сведению):* (а) `trigger_type` в трёх entry-point сейчас всегда `SCHEDULED` даже при
   ручном запуске из редактора — на freshness не влияет (гейт смотрит status/logical_period/started_at); поправлю
   диагностику manual/scheduled позже, отдельным мелким PR; (б) нумерация validation упорядочена 1→11.

## Правки REV3 (2 блокера)
1. **Три реально изменённых loader-файла В ПАКЕТЕ** (было: только скрипт). Патчи применены к рабочим файлам
   `apply_mart3a_patches.py`, проверены `node --check` (все 4 `.gs`), `git diff --check` чист. Готовый diff — `docs/MART_PR3A_loaders.diff`.
   Бэкапы `*.pre_mart3a.bak` убраны из `apps-script/` (в `_to_delete/`), в коммит не идут.
2. **Идемпотентность сверяет ОЖИДАЕМЫЙ статус.** Было: `affected=0` + любой терминальный статус → `true` — то есть
   `ingestRunComplete_` по уже-`ERROR` строке ложно возвращал `true` (конфликт состояний). Стало:
   `ingestInterpretFinalize_(affected, runId, expectedStatus)` → `true` только если `actualStatus === expectedStatus`,
   противоположный терминальный статус = КОНФЛИКТ → `false` + лог. Добавлен `ingestSelfTestCrossState()`:
   `COMPLETE→ERROR = false` (строка остаётся COMPLETE), `ERROR→COMPLETE = false` (остаётся ERROR).

**Дизайн:** `docs/MART3_ORCHESTRATION_DESIGN_2026-07-31.md` (REV4, блоки 1–2).
**Файлы PR:** `sql/mart3/pr_mart3a_ingest_runs.sql`, `sql/mart3/pr_mart3a_validation.sql`,
`apps-script/IngestRunLog.gs` (новый), **фактические правки** `apps-script/WbOrdersLoader`,
`apps-script/WbSalesIncremental.gs`, `apps-script/WbAdsDaily.gs`.

## Правки REV2 по замечаниям аудита
1. **Реальные интеграции в три tracked-файла** (было: только инструкция). Патчи внесены в рабочее дерево,
   включая ветки `catch` и early-return. Бэкапы исходников — `*.pre_mart3a.bak` (удалить после ревью).
2. **Whitelist success-статусов.** «Всё, кроме ERROR» ≠ успех: `ads PARTIAL` и `sales SKIPPED_RATE_LIMIT`
   дали бы ложный `COMPLETE`-heartbeat, и Mart3 собрал бы витрину на неполных данных. Введён явный список.
3. **Проверка `numDmlAffectedRows`.** `INSERT` обязан дать ровно 1; финализация: `1` = обновлено,
   `0` = `true` **только** если строка доказанно уже терминальна (проверяется `SELECT`), `>1` = аномалия → `false`.
   Self-тест идемпотентности теперь **доказывает** неизменность строки, а не просто печатает `true`.

## Домены статусов (установлено по коду загрузчиков)
| loader | статусы в коде | успех для heartbeat |
|---|---|---|
| orders | `OK`, `OK_NO_CHANGES`, `PARTIAL`, `ERROR` | **`OK`, `OK_NO_CHANGES`** |
| sales | `OK`, `OK_NO_CHANGES`, `PARTIAL`, `ERROR`, `SKIPPED_LOCKED`, `SKIPPED_RATE_LIMIT` | **`OK`, `OK_NO_CHANGES`** |
| ads | `OK`, `PARTIAL`, `STALE`, `ERROR`, `SKIPPED_LOCKED` | **`OK`, `STALE`** |

`OK_NO_CHANGES` — штатный **zero-row success** (изменений не было, ран успешен).
**`STALE` включён в успех осознанно** (пункт для явного подтверждения аудитором): все источники загрузились,
ран успешен; устаревание данных на стороне WB — это сценарий **ADS_LAGGED**, который Mart3 обрабатывает
**маркером в витрине** (`ads_lagged` / `ads_business_max_date`), а не блокировкой сборки. `PARTIAL` (неполнота
самой загрузки) успехом **не** считается.

## Фактические изменения загрузчиков
Единая точка финализации — `ingestFinalizeByStatus_(runId, loader, status, fetched, loaded, errMsg)`:
применяет whitelist и вызывает `ingestRunComplete_` либо `ingestRunError_` с кодом вида `ADS_PARTIAL`,
`SALES_SKIPPED_RATE_LIMIT`, `ORDERS_UNKNOWN`.

**`WbOrdersLoader` → `runWbOrdersIncremental()`**
- `ingestRunStart_('orders', ingestClosedDayMsk_(), 'SCHEDULED')` — **после** захвата ScriptLock
  (пропуск по локу — это не ран, строку не создаём);
- финализация — после `console.log`, перед `return r`, поля `r.api_rows_received` / **`r.rows_appended`**.

**`WbSalesIncremental.gs` → `runWbSalesIncremental()`**
- `ingestRunStart_('sales', …)` после захвата лока (ранний `SKIPPED_LOCKED` — до этого, строка не создаётся);
- финализация перед `return r`, поля `r.api_rows_received` / `r.rows_written`.

**`WbAdsDaily.gs` → `runWbAdsDaily()`** — четыре ветки:
- `var ingestRunIdAds = null;` в начале;
- **early-return при выключенном `WB_ADS_BQ_SINK`:** открываем ран и сразу пишем `ADS_SINK_OFF` → `ERROR`
  (точный период API ещё не вычислен, берём закрытый день — в норме он совпадает с `rng.to`);
- **успешный путь:** `ingestRunStart_('ads', rng.to, …)` сразу после `rng = wbAdsLast7Range_()`
  (`logical_period` = **целевой период API**, а не дата запуска), финализация по `overall` с суммой `results[].rows`;
- **`catch`:** `ingestRunError_(ingestRunIdAds, 'ADS_EXCEPTION', em)` перед `return`;
- ранний `SKIPPED_LOCKED` (до `try`) строку не создаёт — осознанно.
- **REV5:** `overall` считает чистая `wbAdsOverallStatus_(results, fresh.stale)` (fail-closed): любой
  `null`/`{}`/без-`status`/неизвестный статус/не 3 результата → `ERROR`, а не проскок в `OK`/`STALE` до whitelist.

## Контракты (без изменений с REV1)
Одна строка на run: `INSERT STARTED` → `UPDATE` по `run_id`; `run_id` уникален; `COMPLETE`/`ERROR` терминальны
(`WHERE status='STARTED'`); сбой логгера не скрывает ошибку загрузчика (все функции гасят свои исключения);
DML через `Jobs.query` (не streaming `insertAll`) + NAMED-параметры; `PARTITION BY DATE(started_at)`,
`CLUSTER BY loader_name, status, logical_period`; `logical_period` — тип **DATE**.

## Применение
1. **SQL:** прогнать `sql/mart3/pr_mart3a_ingest_runs.sql` (идемпотентно).
2. **Apps Script:** добавить `IngestRunLog.gs`; перенести правки трёх загрузчиков (diff в PR).
3. **Тесты (из редактора):**
   - `wbAdsSelfTestOverallStatus()` — **REV5**, 14 offline-кейсов расчёта ads-статуса (null/{}/unknown/счётчик → ERROR);
   - `ingestSelfTestComplete()` — **доказательная** идемпотентность: после повторной финализации строка
     не изменилась (`rows_loaded` остался 7) и повтор вернул `true` как «уже терминальна»;
   - `ingestSelfTestZeroRows()` — zero-row success → `COMPLETE`, `rows_loaded=0`;
   - `ingestSelfTestError()` — кавычки/переводы строк в тексте ошибки;
   - `ingestSelfTestStatusWhitelist()` — **`ads PARTIAL` и `sales SKIPPED_RATE_LIMIT` обязаны дать `ERROR`**,
     `orders OK_NO_CHANGES` — `COMPLETE`.
4. **Валидация:** `sql/mart3/pr_mart3a_validation.sql` §1–§6, §10 (`status_domain_bad=0`), §11 (whitelist-регресс).
5. **Наблюдение 1–2 суток:** §7 (`covers_target = TRUE` по orders/sales/ads) и §8 — **latest-attempt по дням**
   (`latest_complete = TRUE` для всех трёх на КАЖДЫЙ день; НЕ `COUNTIF(COMPLETE) > 0` — иначе ранний COMPLETE
   + поздний ERROR дал бы ложную зелень). Готовность к Mart3b подтверждает ПОСЛЕДНЯЯ попытка каждого дня.
   Строки `loader_name='selftest'` в гейт не входят.
6. Удалить бэкапы `apps-script/*.pre_mart3a.bak` после ревью diff.

## Риски и как закрыты
| Риск | Закрытие |
|---|---|
| `UPDATE` по строке в streaming buffer запрещён | DML через `Jobs.query`, не `insertAll` |
| Кавычки/переводы строк в `error_message` | NAMED-параметры |
| Сбой логгера ломает загрузчик | функции логгера гасят свои исключения |
| PARTIAL/SKIPPED дают ложный успех | whitelist + §11 валидации |
| Молчаливый no-op UPDATE (строки нет / чужой run_id) | проверка `numDmlAffectedRows` + `SELECT`-доказательство при 0 |
| Несколько строк на один ран | `run_id` уникален; §2/§5 валидации |
| Загрузчик умер, не финализировав | остаётся `STARTED` → §3 (`stuck_started`); гейт такую строку успехом не считает |
| Пропущенная запись журнала | гейт fail-closed остановит сборку — лучше, чем «тихо неверная» витрина |

## Дальше
После зелёного наблюдения — **PR-Mart3b**: loader `mart` (freshness по `V_INGEST_HEARTBEAT`), `MART_RUNS`,
`V_MART_RUN_LOG`, Terraform (Job + Scheduler `0 9,10,11 * * *` Europe/Moscow + IAM), правки витрины
(`+in_run_id`, снятие guard `>= max_required`, **`ads_lagged` / `ads_business_max_date` в `MART_SKU_DAILY`**).
