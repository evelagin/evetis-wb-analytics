# PR-Mart3b — оркестрация MART (авто-обновление витрины). ПЛАН PR НА АУДИТ. REV3

**Дата:** 2026-08-03
**Статус:** REV3 — APPROVE WITH CHANGES от аудитора получен; закрыты 3 точных противоречия REV2. Новый архитектурный раунд НЕ требуется. **План APPROVED — можно начинать код PR3b-1.**
**Процесс:** план → аудит → код по подшагам (как для Mart3a).

**Зачем этот документ.** Mart3b трогает несколько слоёв (Cloud Run Job, Cloud Scheduler, TS-loader, BigQuery-процедуры/DDL, Terraform, state-machine retry/lease). Здесь — только СОСТАВ PR, файлы, порядок rollout, что проверяется, что тестируется, что считается готовностью. Кода нет.

---

## Правки REV3 (APPROVE WITH CHANGES — 3 точных противоречия + 1 неблокирующее)

1. **[ПРОТИВОРЕЧИЕ 1] `targetDate` добавлен в контракт `LoaderContext`** (было только `+runId`). Теперь оба поля + инвариант `ctx.logicalPeriod === ctx.targetDate`. Handler использует `ctx.targetDate` как `@target_date` для heartbeat и обеих процедур; текущую дату внутри loader НЕ пересчитывает. См. PR3b-1 (файлы) и §5.1.
2. **[ПРОТИВОРЕЧИЕ 2] Устранён конфликт acquisition-guard между PR3b-1 и PR3b-2.** В PR3b-1 mart НЕ подключается к недоказанному generic-lease: production CLI-dispatch для `mart` **feature-flagged OFF**, ручной прогон — через отдельный dedicated entry-point (в обход `runManifest.acquire()`). В PR3b-2 после доказательства 4-state снимается флаг, mart включается в общий CLI + `LOADER_RUNS` acquisition, затем создаётся Cloud Run Job. См. PR3b-1/PR3b-2.
3. **[ПРОТИВОРЕЧИЕ 3] Однозначная семантика `ads_lagged` в строках MART.** Build-level метаданные качества: ОДИНАКОВЫ во всех строках одной публикации (не характеристика каждого исторического `day`). Формула зафиксирована в §5.3. Отдельный per-day контракт (`ads_day_has_source_data`) — НЕ в PR3b-1.
4. **[неблокирующее] Назван точный entry-point ручного прогона** в DoD PR3b-1, чтобы владелец не запускал production-handler произвольным способом. См. PR3b-1 DoD.

**Окончательно принято аудитором:** ACK Варианта Б (per-table atomic + eventual consistency, full-set атомарности нет); `MART_RUNS` в PR3b-1; никакого shadow; ads-lag до rollout; сигнатура процедуры с третьим `in_run_id`; один `targetDate` на прогон; Scheduler PAUSED до controlled rollout; наблюдение 3 суток; SLA 12:00 МСК; `steps_json` — 2 шага; loader `mart` без WB-токена.

---

## Правки REV2 (по замечаниям аудита REV1 — 5 блокеров + доп. блок)

1. **[БЛОКЕР 1] Консистентность FACT↔MART зафиксирована — Вариант Б (per-table атомарность).** Явно записан контракт: атомарность гарантируется на уровне каждой отдельной таблицы, FACT может опередить MART при аварии, прямых production-потребителей FACT нет, BI читает только последнюю успешно опубликованную MART, следующая попытка полностью пересобирает слой. Full-set staging-publish в Mart3b НЕ реализуется. См. §1.1. *(требует явного ACK владельца на подтверждающем аудите)*
2. **[БЛОКЕР 2] `MART_RUNS` + минимальный `V_MART_RUN_LOG` перенесены в PR3b-1** — базовый журнал существует одновременно с первым рабочим handler; PR3b-1 стал самодостаточным. См. §2.
3. **[БЛОКЕР 3] Удалены все формулировки `shadow`.** Отдельного `wb_mart_shadow` нет. DoD переписаны на: unit/integration с mock BigQuery → Cloud Run Job развёрнут при Scheduler PAUSED → ручной controlled production run → validation → потребители не переключаются до acceptance. См. §2, §6.
4. **[БЛОКЕР 4] Политика рекламного лага перенесена в PR3b-1** как production-правило (а не rollout-наблюдение). Трёхуровневый контракт `ads_business_max_date` (FALSE / provisional / `ADS_TOO_STALE`→ERROR) зашивается в код PR3b-1, в rollout только проверяется. См. §5.3.
5. **[БЛОКЕР 5] Зафиксирована точная сигнатура процедуры и единый источник истины для даты.** Третий параметр `IN in_run_id STRING`; `target_date` вычисляется в CLI ОДИН раз до acquire, handler его НЕ пересчитывает. См. §5.1.
6. **[ДОП. БЛОК] State machine нельзя объявить переиспользуемой без доказательства.** PR3b-2 обязан либо перечислить точные правки `runManifest.ts`, либо тестами доказать, что текущий код различает 4 состояния по последней попытке за logical_period; отдельно доказать, что stale-recovery ВСТАВЛЯЕТ новую строку с новым `run_id`, а не перезаписывает зависший ран. См. PR3b-2.

**Перестройка разбивки:** PR3b-1 = BigQuery-контракты + исполнимый loader (без Scheduler); PR3b-2 = lease/state-machine + Cloud Run Job; PR3b-3 = Scheduler + alerts; PR3b-4 = controlled rollout. Причина: journal и loader должны жить вместе; Scheduler отделён от state-machine.

---

## 0. Контекст: что уже готово (не переоткрываем)

Ручной аналитический контур закрыт и подтверждён на реальных данных:
RAW ✔ → FACT ✔ (PR#78) → ADS FACT ✔ (PR#79) → FINANCE LONG ✔ (PR#80) → `MART_SKU_DAILY` ✔ (PR#81, 6796 строк / 24 SKU) → INGEST heartbeat ✔ (PR#82 / Mart3a).

Heartbeat (`wb_raw.INGEST_RUNS` + `wb_raw.V_INGEST_HEARTBEAT`) подтверждён 03.08 на данных за 2026-08-02: orders/sales/ads все `covers_target = TRUE`, `latest_status = COMPLETE`.

**Единственный оставшийся разрыв:** `MART_SKU_DAILY` собирается ТОЛЬКО вручную (`sp_build_mart_sku_daily`, MANUAL-ONLY, lock `mart_sku_daily`). Mart3b закрывает именно это.

**Мастер-дизайн, к которому обязан быть согласован план:** `docs/MART3_ORCHESTRATION_DESIGN_2026-07-31.md` (гейт-контракт REV5, тело APPROVED).

---

## 1. Фиксированные решения (из мастер-дизайна, НЕ пересматриваем)

1. **fail-closed** — при любой неопределённости источников build не запускается; витрина не трогается.
2. **FACT + MART в одном job** — один Cloud Run прогон делает `sp_bootstrap_facts` (FACT) → `sp_build_mart_sku_daily` (MART) под сквозным `run_id`.
3. **Один успешный D-1 build в день.** `target_date = logical_period = build_as_of = DATE_SUB(CURRENT_DATE('Europe/Moscow'), INTERVAL 1 DAY)`.
4. **Ads-свежесть — через heartbeat**; бизнес-лаг рекламы маркируется/гейтится (см. §5.3).
5. **Finance — вне гейта** (недельный лаг — норма); её heartbeat пишется только для наблюдения.
6. **Гейт свежести = LATEST-ATTEMPT** (не `EXISTS COMPLETE`, не `COUNTIF(COMPLETE)`); отсутствующий loader → `FALSE` через LEFT JOIN (fail-closed).

### 1.1 Контракт консистентности FACT↔MART (БЛОКЕР 1 — Вариант Б, ЗАФИКСИРОВАНО)

Mart3b **НЕ** реализует атомарную публикацию всего набора FACT+MART одним переключением. Принят Вариант Б со следующим явным контрактом:

```text
- Атомарность гарантируется ТОЛЬКО на уровне каждой отдельной таблицы
  (каждая процедура публикует свою таблицу через BUILD→ASSERT→CREATE OR REPLACE).
- При аварии между шагами FACT может опередить MART:
  FACT_* новый, MART_SKU_DAILY старый, run = ERROR.
- Это ДОПУСТИМО, потому что:
  * прямых production-потребителей слоя FACT нет;
  * BI (Looker Studio) читает ТОЛЬКО последнюю успешно опубликованную MART_SKU_DAILY;
  * следующая попытка (retry / следующий фаер) ПОЛНОСТЬЮ пересобирает слой FACT→MART
    под новым run_id, приводя набор в согласованное состояние.
- Поэтому «fail-closed» здесь означает: freshness-gate + сама MART fail-closed;
  межтабличная согласованность всего набора — eventual, не атомарная.
```

Следствие для наблюдения: в `MART_RUNS` при ERROR фиксируется, на каком шаге упал прогон (`steps_json`), чтобы было видно, опередил ли FACT MART. **Требует явного ACK владельца на подтверждающем аудите** (это единственное ослабление ранее заявленной атомарности набора).

---

## 2. Разбивка на PR (перестроена в REV2)

Порядок мержа строгий: 1 → 2 → 3 → 4. Каждый PR самодостаточен: проходит CI и визу аудитора.

### PR3b-1 — BigQuery-контракты + исполнимый loader (без Scheduler)

**Цель.** Появляется всё, что нужно для одного ручного end-to-end прогона витрины из кода: контракты BigQuery (правки процедуры, `MART_RUNS`, минимальный `V_MART_RUN_LOG`), loader `mart` с freshness-gate и ads-lag-политикой, сквозной `run_id`, тесты. Scheduler и Cloud Run Job — НЕ здесь.

**Файлы (создание/правка):**

- `cloud/src/loaders/mart/` (новый): handler по паттерну `stocks/index.ts` + модуль freshness-gate (SQL latest-attempt против `V_INGEST_HEARTBEAT`, параметры `@loaders`, `@target_date`) + запись `MART_RUNS` в обеих ветках.
- `cloud/src/loaders/registry.ts`: `+ mart: martLoader` (регистрация допустима; production CLI-dispatch для mart — под feature-flag OFF, см. ниже).
- `cloud/src/loaders/types.ts`: **+ `runId: string` И `targetDate: string` (YYYY-MM-DD, D-1 Europe/Moscow) в `LoaderContext`.** Инвариант `ctx.logicalPeriod === ctx.targetDate`:
  ```ts
  export interface LoaderContext {
    config: AppConfig;
    logger: Logger;
    logicalPeriod: string;
    runId: string;
    targetDate: string; // YYYY-MM-DD, D-1 Europe/Moscow; ctx.logicalPeriod === ctx.targetDate
  }
  ```
- `cloud/src/cli.ts`: пробросить уже вычисляемые `runId` и `targetDate` в handler (сейчас передаются только `config, logger, logicalPeriod`). Handler берёт `@target_date = ctx.targetDate` для heartbeat-гейта и обеих процедур; текущую дату внутри loader НЕ пересчитывает.
- `cloud/src/mart_manual.ts` (новый, dedicated entry-point) — единственный санкционированный ручной прогон в PR3b-1: строит `ctx` (в т.ч. `targetDate`, `runId`) и вызывает `martLoader` НАПРЯМУЮ, **в обход** `runManifest.acquire()`. Нужен, потому что generic run-lease ещё не доказан (появится в PR3b-2).
- `cloud/src/config.ts`: WB/stocks-переменные — опциональны для mart-прогона; loader `mart` НЕ требует `WB_TOKEN_ANALYTICS` (обязательны только `GCP_PROJECT_ID`, `BQ_RAW_DATASET`, `ENVIRONMENT`).
- `sql/mart/pr_mart2b_sku_daily.sql`: правки процедуры (см. §5.1–§5.3).
- `sql/mart3/pr_mart3b_mart_runs.sql` (новый): DDL `wb_mart.MART_RUNS` + минимальный `V_MART_RUN_LOG` (полная версия JOIN с `LOADER_RUNS` — в PR3b-2, когда появится loader-lease; здесь `V_MART_RUN_LOG` = проекция `MART_RUNS`, чтобы PR был самодостаточен).
- `sql/mart3/pr_mart3b_validation.sql` (новый): validation-запросы §1..§N.
- `cloud/test/`: unit/integration с mock BigQuery.

**Ключевые контракты:** один `ctx.runId` → в обе процедуры → в `MART_RUNS.run_id`. Handler делает только доменную работу (gate → процедуры → `MART_RUNS`); run-lease появится в PR3b-2. Пустой/битый gate-ответ (не 3 строки / любой NULL) → FAIL (fail-closed). **mart НЕ подключается к generic run-lease в этом PR:** production CLI-dispatch для `mart` под feature-flag **OFF** (например, `MART_CLI_ENABLED=0`), так что регистрация в `registry.ts` не активирует ещё-не-доказанный `runManifest.acquire()` для mart. Единственный рабочий путь запуска mart в PR3b-1 — dedicated entry-point `cloud/src/mart_manual.ts` (в обход acquire).

**Тесты (mock BigQuery):** handler тестируется НАПРЯМУЮ (без generic manifest acquisition); gate OK (3×COMPLETE); gate FAIL (не-COMPLETE / нет loader / не 3 строки / NULL); zero-row день (`rows_loaded=0` ок); проброс `run_id` в обе процедуры; инвариант `ctx.logicalPeriod === ctx.targetDate`; ads-lag: FALSE / provisional / `ADS_TOO_STALE`→ERROR (три ветки §5.3); запись `MART_RUNS` в ветках COMPLETE и ERROR (одна строка на `run_id`).

**Definition of Done PR3b-1:** зелёный CI + все unit/integration зелёные (mock BQ); локальный `DRY_RUN=1` прогон handler; **один ручной controlled прогон в prod через названный entry-point** — `npm run mart:manual -- --target-date=<D-1>` (обёртка над `cloud/src/mart_manual.ts`; **это единственный санкционированный способ**, произвольный запуск production-handler запрещён): витрина собрана, `MART_RUNS` заполнена, `ads_lagged`/`ads_business_max_date` корректны, sanity `build_as_of = D-1`. Cloud Run Job/Scheduler ещё нет. Потребители не переключаются.

---

### PR3b-2 — lease / state machine + Cloud Run Job

**Цель.** Защита от двойного/зависшего прогона и упаковка в Cloud Run Job. Никакого нового кода витрины.

**Файлы:**
- `cloud/src/cli.ts` / `cloud/src/bq/runManifest.ts`: **явные** правки под mart-lease (см. ниже) ИЛИ тесты, доказывающие, что текущий код уже поддерживает контракт.
- `cloud/src/cli.ts`: **снять feature-flag** (`MART_CLI_ENABLED`) — mart включается в общий CLI-dispatch + `LOADER_RUNS` acquisition path (порядок: доказать 4-state → снять флаг → создать Job). Dedicated entry-point `mart_manual.ts` из PR3b-1 остаётся для ручных прогонов/бэкфилла.
- Terraform: Cloud Run **Job** для loader `mart` + IAM (§4). Scheduler ещё НЕ создаётся.
- `sql/mart3/pr_mart3b_mart_runs.sql`: расширить `V_MART_RUN_LOG` до `LOADER_RUNS(loader='mart')` JOIN `MART_RUNS` USING(`run_id`).

**State machine (по состоянию ключа `environment × loader='mart' × logical_period=D-1` в `LOADER_RUNS`):**

| Состояние ключа | Решение |
|---|---|
| `COMPLETE` | **skip** (EXIT_OK) — день собран |
| `ERROR` | **retry разрешён** — новая попытка захватывает lease |
| `STARTED`, свежий (age < TTL) | **lease занят** → skip `ALREADY_RUNNING` |
| `STARTED`, старше TTL (**stale**) | **recovery** — ВСТАВЛЯЕТСЯ НОВАЯ строка `STARTED` со своим `run_id` |

**[ДОП. БЛОК] Обязательство PR3b-2 — доказать, а не объявить.** В PR явно фиксируется одно из двух:
- **(а)** список точечных правок `runManifest.ts` (если текущая логика не различает все 4 состояния по последней попытке за `logical_period`); ЛИБО
- **(б)** тесты, показывающие, что `isActive()`/`acquire()` уже: (1) skip при `COMPLETE`; (2) retry при `ERROR`; (3) `ALREADY_RUNNING` при свежем `STARTED`; (4) при stale `STARTED` (age > TTL) **INSERT новой строки с новым `run_id`**, а не UPDATE зависшей.
Отдельный обязательный тест: **stale-recovery не перезаписывает** зависший ран (проверка, что старая `STARTED`-строка остаётся, добавляется новая).

**Инварианты:** `TTL (MART_LEASE_TTL) = 30 мин` (= `DEFAULT_STALE_STARTED_MS`); **`max_build_duration < TTL < scheduler_interval`** = `build(~мин) < 30 < 60`. **Два замка, не путать:** run-lease в `wb_raw.LOADER_RUNS` (оркестрация) vs внутрипроцедурный `wb_mart._MART_BOOTSTRAP_LOCK` (`lock_id='mart_sku_daily'`, остаётся как есть).

**Definition of Done PR3b-2:** Cloud Run Job развёрнут; **Scheduler отсутствует/не создан**; все 4 перехода state-machine доказаны (правки runManifest перечислены ИЛИ тесты зелёные), включая stale-recovery-без-перезаписи; замер длительности Job подтверждает `build < TTL`.

---

### PR3b-3 — Scheduler + alerts

**Цель.** Расписание и наблюдаемость сбоев. Кода витрины/loader нет.

**Файлы:**
- Terraform: **один** Cloud Scheduler job `mart-daily-prod`, `schedule = "0 9,10,11 * * *"`, `time_zone = "Europe/Moscow"`. Создаётся **PAUSED**.
- Alerts: (1) `status='ERROR'` в `MART_RUNS`/`V_MART_RUN_LOG`; (2) **SLA-alert в 12:00 МСК** — если за D-1 нет строки `COMPLETE` (последний фаер в 11:00 уже прошёл).

**Definition of Done PR3b-3:** Scheduler создан и **PAUSED**; оба алерта срабатывают на искусственных условиях (ERROR-строка; отсутствие COMPLETE к 12:00); operational validation зелёная.

---

### PR3b-4 — controlled rollout

**Цель.** Ввести в прод по чек-листу; без нового кода — infra state + операционные проверки.

**Последовательность (Scheduler PAUSED до последнего шага):**
1. Ручной запуск Job (Scheduler PAUSED).
2. **Validation:** консервация (расхождение с ручной сборкой) = 0; плотность spine; грейн (1 строка = SKU×день); один `run_id` по цепочке FACT→MART→MART_RUNS.
3. **Same-day skip:** второй ручной фаер того же дня → `COMPLETE`→skip, витрина не пересобрана.
4. **Freshness failure:** «уронить» источник (latest attempt ≠ COMPLETE) → `ERROR`, алерт, витрина не тронута.
5. **Ads provisional И ads too-stale:** проверить обе ветки §5.3 (лаг 1 день → provisional build; лаг >1 / NULL → `ADS_TOO_STALE`→ERROR, MART не публикуется).
6. **Stale lease recovery:** эмулировать зависший `STARTED` > TTL → следующий фаер подхватывает новой строкой.
7. **`duration_ms < TTL`** подтверждён на проде.
8. **Scheduler → RESUME.**
9. **Наблюдение 3 последовательных суток:** ежедневно ровно один `COMPLETE` D-1, `V_MART_RUN_LOG` чистый.

**Definition of Done PR3b-4 (= готовность всего Mart3b):** Scheduler ON; 3 суток подряд один `COMPLETE` D-1/день без ручного вмешательства; воспроизведены same-day-skip, freshness-failure, ads provisional + too-stale, stale-recovery; инвариант длительности подтверждён; алерты/лог дают наблюдаемость. Далее — Looker Studio (вне Mart3b).

---

## 3. Freshness-gate — контракт (LATEST-ATTEMPT, не переоткрываем)

Loader `mart` читает ТОЛЬКО `wb_raw.V_INGEST_HEARTBEAT` (`INGEST_RUNS` ∪ `LOADER_RUNS` prod). Обязательные loader: `orders`, `sales`, `ads`. finance — вне гейта.

```sql
WITH required AS (SELECT l AS loader_name FROM UNNEST(@loaders) AS l),
     latest AS (
       SELECT loader_name, status, completed_at,
              ROW_NUMBER() OVER (PARTITION BY loader_name
                                 ORDER BY started_at DESC, run_id DESC) AS rn
       FROM `wb_raw.V_INGEST_HEARTBEAT`
       WHERE loader_name IN UNNEST(@loaders)
         AND logical_period = @target_date
         AND started_at    >= TIMESTAMP(DATE_ADD(@target_date, INTERVAL 1 DAY), 'Europe/Moscow')
     )
SELECT r.loader_name,
       COALESCE(l.status = 'COMPLETE' AND l.completed_at IS NOT NULL, FALSE) AS covers_target
FROM required r
LEFT JOIN (SELECT * FROM latest WHERE rn = 1) l USING (loader_name);
```

Гейт проходит только если `covers_target = TRUE` для ВСЕХ `@loaders`. Whitelist успеха задаётся на стороне записи heartbeat (orders/sales = `OK`,`OK_NO_CHANGES`; ads = `OK`,`STALE`) — Mart3b его НЕ дублирует. Снимок гейта per-source → `MART_RUNS.freshness_json` (`{loader,last_complete_at,covers_target,business_max_date}`).

> ⚠️ Freshness-gate (ingestion COMPLETE за target_date) и ads-lag-политика (business-покрытие `FACT_ADS`, §5.3) — **два разных контроля**. Первый: догрузился ли ads-loader. Второй: до какой даты реально доехали данные рекламы.

---

## 4. IAM / permissions (из мастер-дизайна)

- **`wb_raw`: READ** — источники (`RAW_WB_*`, `V_WB_*`, `V_ADV_*`), `REF_SKU_MASTER`, `INGEST_RUNS`, `V_INGEST_HEARTBEAT`; **WRITE** — `LOADER_RUNS` (guard/lease в датасете `wb_raw`).
- **`wb_mart`: READ+WRITE** — `FACT_*`, `MART_*`, `*__BUILD`, `REF_COST_MAP`, `V_WB_FINANCE_AMOUNTS_LONG*`, `MART_RUNS`, `_MART_BOOTSTRAP_LOCK`.
- **проект:** `roles/bigquery.jobUser` (jobs.create), location **EU**.

---

## 5. Контракты кода витрины и вызова (БЛОКЕР 4, 5 — ЗАФИКСИРОВАНО)

### 5.1 Точная сигнатура процедуры + вызов handler + источник истины даты

Финальная сигнатура (третий параметр):

```sql
CREATE OR REPLACE PROCEDURE `wb_mart.sp_build_mart_sku_daily`(
  IN in_build_as_of_date DATE,
  IN in_global_start_date DATE,
  IN in_run_id STRING
)
```

Вызов из handler (проект `project-fa311fc0-4d87-4781-986`):

```sql
CALL `project-fa311fc0-4d87-4781-986.wb_mart.sp_bootstrap_facts`(@run_id);

CALL `project-fa311fc0-4d87-4781-986.wb_mart.sp_build_mart_sku_daily`(
  @target_date,   -- build_as_of = D-1
  NULL,           -- global_start (дефолт витрины)
  @run_id
);
```

`in_run_id`: используется как `mart_run_id`; fallback `GENERATE_UUID()` при `''`/`NULL`.

**Единый источник истины для даты (ОБЯЗАТЕЛЬНО):**

```text
CLI ДО acquisition guard вычисляет targetDate = D-1 Europe/Moscow;
logicalPeriod = FORMAT_DATE(targetDate);          -- ключ LOADER_RUNS
тот же targetDate передаётся в handler и в CALL;
handler D-1 ПОВТОРНО НЕ пересчитывает.
```

Иначе — рассинхрон между `LOADER_RUNS.logical_period` и фактическим `build_as_of`.

### 5.2 Прочие правки процедуры

1. **Убрать** внутренний guard `build_as_of < max_required_source_date → RAISE` (несовместим с D-1/бэкфиллом); оставить `v_max_required` как sanity-строку в лог/`steps_json`. **Верхний guard `build_as_of > CURRENT_DATE('Europe/Moscow') → RAISE` СОХРАНЯЕТСЯ.**
2. `sp_bootstrap_facts` — без изменений (уже принимает `in_run_id`).
3. Внутрипроцедурный lock `_MART_BOOTSTRAP_LOCK` (`lock_id='mart_sku_daily'`) — оставить как есть.

### 5.3 Ads-lag policy — production-правило (в PR3b-1, НЕ в rollout)

`ads_business_max_date = (SELECT MAX(date) FROM wb_mart.FACT_ADS_SKU_DAILY WHERE date <= @build_as_of)`. Трёхуровневый build-level контракт относительно `target_date`:

```text
ads_business_max_date >= target_date
   → ads_lagged = FALSE, build разрешён;

ads_business_max_date = target_date - 1
   → ads_lagged = TRUE, build разрешён как PROVISIONAL;

ads_business_max_date < target_date - 1  ИЛИ  NULL
   → ERROR / ADS_TOO_STALE, MART НЕ публикуется (fail-closed).
```

Реализация: RAISE `ADS_TOO_STALE` в процедуре (у неё есть доступ к `FACT_ADS_SKU_DAILY`); handler маппит в `status='ERROR'`, `error_code='ADS_TOO_STALE'`, пишет `MART_RUNS`.

**Семантика колонок в `MART_SKU_DAILY` (ПРОТИВОРЕЧИЕ 3 — ЗАФИКСИРОВАНО): build-level метаданные качества, ОДИНАКОВЫ во всех строках одной публикации** (не характеристика каждого исторического `day`). Вычисляются один раз на build и проставляются во все строки:

```sql
ads_business_max_date = v_ads_business_max_date,
ads_lagged            = (v_ads_business_max_date = DATE_SUB(v_build_as_of, INTERVAL 1 DAY))
```

Тогда: все строки одной публикации имеют одинаковые `ads_business_max_date`/`ads_lagged`; `MART_RUNS` и `MART_SKU_DAILY` полностью согласованы (снимок = строки); BI показывает предупреждение о provisional-снимке; при следующем rebuild (реклама догрузилась) флаг обновится. ASSERT: `COUNTIF(ads_business_max_date > build_as_of)=0` и `COUNT(DISTINCT ads_business_max_date)<=1` (единый снимок). Ветка `< target_date-1`/NULL до публикации не доходит (уже `ADS_TOO_STALE`→ERROR).

> Если позже понадобится качество рекламы отдельно ПО КАЖДОМУ историческому дню — это другой контракт, отдельная колонка `ads_day_has_source_data`; **в PR3b-1 её НЕ добавляем.**

**В rollout (§PR3b-4.5) этот контракт ПРОВЕРЯЕТСЯ, а не принимается впервые.**

---

## 6. `MART_RUNS` / `V_MART_RUN_LOG` (создаётся в PR3b-1, JOIN расширяется в PR3b-2)

`wb_mart.MART_RUNS` (типизированная таблица, отдельно от `LOADER_RUNS`):

| колонка | тип | смысл |
|---|---|---|
| `run_id` | STRING | сквозной id (Cloud Run → LOADER_RUNS → FACT → MART → сюда) |
| `environment` / `target_date` | STRING / DATE | среда и D-1 |
| `status` | STRING | `COMPLETE` \| `ERROR` |
| `started_at` / `completed_at` / `duration_ms` | TIMESTAMP / INT64 | тайминги job |
| `freshness_json` | STRING (JSON) | per-source снимок гейта |
| `ads_lagged` | BOOL | provisional-реклама на target_date |
| `ads_business_max_date` | DATE | покрытие ads на момент build |
| `steps_json` | STRING (JSON) | шаги (v1: `sp_bootstrap_facts`, `sp_build_mart_sku_daily`) |
| `mart_rows` | INT64 | `COUNT(*) MART_SKU_DAILY` после publish |
| `git_sha` / `image_digest` | STRING | версия кода/образа |
| `error_code` / `error_message` | STRING | при ERROR (в т.ч. `ADS_TOO_STALE`, шаг падения) |

`V_MART_RUN_LOG`: в PR3b-1 — проекция `MART_RUNS` (самодостаточность); в PR3b-2 расширяется до `LOADER_RUNS(loader='mart')` JOIN `MART_RUNS` USING(`run_id`). Запись `MART_RUNS` — из handler в ОБЕИХ ветках (COMPLETE/ERROR). `steps_json` v1 — два верхнеуровневых шага (внутренние BUILD/ASSERT/publish контролируются самими процедурами).

---

## 7. Матрица тестов Mart3b (сводно)

| Сценарий | PR | Ожидаемое |
|---|---|---|
| freshness OK (3×COMPLETE) | 3b-1 | build выполняется, `run_id` сквозной |
| freshness FAIL (не-COMPLETE/нет loader/не 3/NULL) | 3b-1 | fail-closed ERROR, MART не тронута |
| zero-row день | 3b-1 | build проходит, `rows_loaded=0` ок |
| ads >= target_date | 3b-1 | `ads_lagged=FALSE`, build |
| ads = target_date-1 | 3b-1 | `ads_lagged=TRUE`, provisional build |
| ads < target_date-1 / NULL | 3b-1 | `ADS_TOO_STALE`→ERROR, MART не публикуется |
| `MART_RUNS` в обеих ветках | 3b-1 | одна строка на `run_id` |
| skip при COMPLETE (same-day) | 3b-2/4 | повторный фаер → skip |
| `ALREADY_RUNNING` (свежий STARTED) | 3b-2 | нет параллельного build |
| stale recovery (STARTED>TTL) | 3b-2 | НОВАЯ строка/`run_id`, старая не перезаписана |
| retry после ERROR | 3b-2 | новая попытка захватывает lease |
| ERROR-alert / SLA-alert 12:00 | 3b-3 | срабатывают |
| `duration_ms < TTL` | 3b-4 | замер на проде |

---

## 8. Ответы на открытые вопросы (зафиксированы аудитором)

1. **Ads >1 дня** → реализовать в **PR3b-1** (production-правило), в rollout только проверить. ✔ §5.3.
2. **SLA-alert** → **12:00 МСК** (после последнего фаера 11:00). ✔ PR3b-3.
3. **Наблюдение** → **3 последовательных дня**. ✔ PR3b-4.9.
4. **`steps_json` v1** → два верхнеуровневых шага (`sp_bootstrap_facts`, `sp_build_mart_sku_daily`). ✔ §6.
5. **WB token** → loader `mart` НЕ читает и НЕ требует `WB_TOKEN_ANALYTICS`. ✔ §2 (config).

---

## 9. Границы (не входит в Mart3b)

- Looker Studio — следующий этап сразу после Mart3b.
- Новые витрины (`MART_ACCOUNT_DAILY` #10, `MART_ADS_RECON_DAILY` #11, MART v2 маржа — блокер REF PR2 COGS) — позже.
- Схему `INGEST_RUNS`/heartbeat (Mart3a) не трогаем — только читаем `V_INGEST_HEARTBEAT`.
- Схему `LOADER_RUNS` (манифест миграции) не меняем структурно — используем существующий guard (правки runManifest в PR3b-2 — только если нужно для 4-state контракта, доказательно).
- Full-set staging-publish FACT+MART — сознательно НЕ реализуется (Вариант Б, §1.1).

---

*Связано:* `docs/MART3_ORCHESTRATION_DESIGN_2026-07-31.md` (мастер-дизайн, гейт REV5), `docs/MART_PR3A_INGEST_RUNS_2026-07-31.md` (образец PR-дока), `docs/MART_MART2_CONTRACTS_2026-07-28.md` (контракты витрин).
