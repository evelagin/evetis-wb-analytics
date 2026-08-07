# PR-Mart3b-2 — mart в generic CLI + run-lease LOADER_RUNS + Cloud Run Job + IAM

Дата: 2026-08-06. Дизайн-контекст: `docs/MART_PR3B_PLAN_2026-08-03.md` (REV3 APPROVED).
Предшественник: PR-Mart3b-1 (`#83` + hotfix `#84`, production acceptance пройден:
`target_date=2026-08-05`, `mart_rows=6916`, один `run_id`/`build_as_of_date`).

## REV2 (аудит): четыре production-блокера закрыты

Оба открытых решения аудитор принял (dataset-level IAM; двойной prod-guard). Закрыты 4 блокера:

1. **Новый Job получал bootstrap-образ.** `deploy-prod.yml` продвигал digest только в `wb-stocks-prod`.
   Теперь шаг «Deploy prod Jobs» гоняет цикл по `wb-stocks-prod` **и** `wb-mart-prod` — тот же
   проверенный digest в оба Job'а (один образ, dispatch по `args[0]`). Без этого `wb-mart-prod`
   остался бы на hello-образе и не исполнял `node dist/cli.js mart`.
2. **`max_retries=1` конфликтовал с lease/MART_RUNS.** Cloud Run task-retry повторяет task в ТОМ ЖЕ
   execution → тот же `CLOUD_RUN_EXECUTION` → тот же `run_id` → вторая строка LOADER_RUNS и
   `MART_RUNS_CONFLICT`. Для `wb-mart-prod` выставлен **`max_retries=0`**: повтор — только новым
   execution в следующем окне Scheduler (новый `run_id`).
3. **`V_MART_RUN_LOG` скрывал lease-only сбои.** LEFT JOIN терял прогоны, где LOADER_RUNS=ERROR, а
   MART_RUNS не записался (отказ IAM / сбой MERGE / падение до терминальной записи). Заменён на
   **`FULL OUTER JOIN`** (loader='mart' фильтруется в CTE до join), `run_id`/`environment`/`target_date`
   через `COALESCE`, добавлены `lease_error_code`/`lease_error_message` и флаг `lease_only_no_mart`
   для алертов PR3b-3.
4. **`DRY_RUN=1` для mart публиковал production.** Ветка DRY_RUN звала handler ДО lease → для mart
   это реальная пересборка `wb_mart`. Теперь для `prodOnly`-загрузчика DRY_RUN **не исполняет
   handler** (проверка регистрации/периода/контекста → `EXIT_OK`). Ядро вынесено в тестируемый
   `runCli(argv, env, deps)`; новый `cli.test.ts` доказывает, что в DRY_RUN+mart `acquire` и
   `martLoader` не вызываются.

Затронутые дополнительно файлы REV2: `.github/workflows/deploy-prod.yml`, `cloud/test/cli.test.ts`
(новый), рефактор `cloud/src/cli.ts` (инъекция `CliDeps`, entry-point только как main-модуль).

## Цель

Убрать ручной `mart_manual.ts` как рабочий процесс: подключить `mart` к общему CLI, чтобы
прогон витрины шёл через тот же execution-guard/lease (LOADER_RUNS), что и остальные загрузчики,
и мог запускаться Cloud Run Job'ом. Планировщик (Cloud Scheduler) — НЕ здесь (PR-Mart3b-3);
до него Job `wb-mart-prod` создаётся, но триггерится только вручную/для валидации.

## Что меняется (diff по областям)

### 1. Регистрация в generic CLI с политикой D-1 — `cloud/src/loaders/registry.ts`, `cli.ts`

Реестр из `Record<string, LoaderHandler>` превращён в `Record<string, LoaderSpec>`, где spec
объявляет **политику логического периода** и флаг `prodOnly`:

| loader | logicalPeriod | prodOnly |
|--------|---------------|----------|
| noop   | сегодня МСК   | —        |
| stocks | сегодня МСК   | —        |
| mart   | **D-1 МСК** (`d1Moscow`) | **true** |

Раньше cli вычислял `logicalPeriod = dailyPeriodMoscow()` (current-day) для ВСЕХ — это и был
блокер #3 PR3b-1 (mart'у нужен D-1). Теперь период — часть реестра; инвариант
`ctx.targetDate === ctx.logicalPeriod` сохранён (оба = `spec.logicalPeriod()`).

`cli.ts` дополнительно отклоняет `prodOnly`-загрузчик вне `ENVIRONMENT=prod` **до** `acquire`
(не плодя строку LOADER_RUNS для заведомо неразрешённого прогона).

### 2. Fail-closed самозащита loader'а — `cloud/src/loaders/mart/index.ts`

`martLoader` бросает `MART_ENV`, если `environment !== 'prod'` (сразу после `CTX_INVARIANT`).
Второй рубеж к cli-гварду: loader — тот компонент, что ПУБЛИКУЕТ production `wb_mart`, поэтому
самозащищается независимо от точки входа (generic cli / mart_manual / будущий вызов). Прежний
`mart_manual.ts` со своим `assertManualRunAllowed` не трогаем — он остаётся для ручных прогонов.

### 3. Cloud Run Job `wb-mart-prod` — `infra/terraform/cloud_run_jobs.tf`, `variables.tf`

`google_cloud_run_v2_job.wb_mart_prod`: тот же образ (dispatch по `args=["mart"]` в cli),
`service_account = loaders_prod`, `ENVIRONMENT=prod`, `LOADER_NAME=mart`, **`max_retries=0`**,
`timeout=1800s`. `ignore_changes` на image (как у `wb_stocks_shadow`) — digest продвигает деплой.
Добавлена `var.mart_dataset` (default `wb_mart`). Планировщик и его invoker-binding — в PR3b-3.

`max_retries=0` — обязательно (REV2 блокер #2): встроенный task-retry Cloud Run повторяет task
в ТОМ ЖЕ execution → тот же `CLOUD_RUN_EXECUTION` → тот же `run_id`, что дало бы вторую строку
LOADER_RUNS и `MART_RUNS_CONFLICT`. Повтор витрины разрешён ТОЛЬКО новым execution в следующем
окне Scheduler (09/10/11 МСК) — с новым `run_id`.

Промоушен образа: `deploy-prod.yml` продвигает один проверенный digest в `wb-stocks-prod` **и**
`wb-mart-prod` (REV2 блокер #1) — иначе Job остался бы на bootstrap hello и не исполнял бы `mart`.

### 4. IAM для `loaders_prod` — `infra/terraform/bigquery.tf`

- `wb_mart` → **dataEditor** (dataset-level): процедуры делают `CREATE OR REPLACE TABLE`
  FACT_*/MART_SKU_DAILY (+`__BUILD`, `_MART_BOOTSTRAP_LOCK`), терминальная `MART_RUNS` — `MERGE`
  (mutating DML) + read-back. Insert-only упал бы на первом MERGE (предупреждение из DDL PR3b-1).
- `wb_raw` → **dataViewer** (dataset-level): freshness-gate читает `V_INGEST_HEARTBEAT`
  (→ `INGEST_RUNS`+`LOADER_RUNS`); `sp_bootstrap_facts` читает `V_WB_ORDERS`, `V_WB_SALES_RETURNS`,
  `V_ADV_CAMPAIGN_STATS`, `V_ADV_COSTS`, `V_WB_FINANCE_CANONICAL`, `WB_STOCKS_SNAPSHOTS`,
  `RAW_WB_STOCKS` — и их базовые таблицы (не-authorized вью раскрываются на зависимостях).

### 5. Окно наблюдения — `sql/mart3/pr_mart3b_mart_runs.sql`

`V_MART_RUN_LOG` расширен до `MART_RUNS ⟗ LOADER_RUNS(loader_name='mart')` по `run_id` через
**`FULL OUTER JOIN`** (REV2 блокер #3; `loader_name='mart'` фильтруется в CTE до join'а). Вью
сохраняет ОБЕ стороны одновременно: ручной MART без lease (`lease_*`=NULL); нормальный автопрогон
(обе строки); и — критично для алертов PR3b-3 — **lease-only сбой без `MART_RUNS`** (LOADER_RUNS=ERROR,
бизнес-строка не записана из-за отказа IAM / сбоя MERGE / падения до терминальной записи). `run_id`,
`environment`, `target_date` выводятся через `COALESCE`; добавлены `lease_error_code`/`lease_error_message`
и прямой флаг `lease_only_no_mart`. Схема `MART_RUNS` НЕ меняется — только вью (`CREATE OR REPLACE VIEW`).

### 6. Тесты — `cloud/test/runManifest.test.ts`, `cloud/test/martWiring.test.ts`, `cloud/test/cli.test.ts`

- Усилен recovery-тест: устаревший STARTED → **вставка НОВОЙ строки** (было 1 → стало 2),
  старая строка нетронута, `finalize` адресует только новый `run_id`. Явное доказательство
  «insert-new-row, не update-in-place».
- Новый `martWiring.test.ts`: политика D-1 (mart ≠ current-day), `prodOnly`-флаги,
  `MART_ENV` guard (shadow → бросает + пишет `MART_RUNS` ERROR; prod → проходит env-guard).

## Состояние-машина lease (LOADER_RUNS) для mart

Даёт её существующий (уже покрытый тестами) `BqManifestStore.acquire` — регистрация mart
включает его «бесплатно». По ПОСЛЕДНЕЙ строке за ключ `(prod, mart, D-1)`:

| Состояние         | acquire → | Поведение mart (окна SLA 09/10/11 МСК одного утра, target=D-1) |
|-------------------|-----------|----------------------------------------------------------------|
| COMPLETE          | skip (`COMPLETE`)      | витрина уже собрана за D-1 → пропуск |
| свежий STARTED    | skip (`ALREADY_RUNNING`)| параллельный прогон держит lease |
| ERROR             | acquire (retry)        | предыдущее окно упало (напр. FRESHNESS_GATE) → повтор в следующем |
| устаревший STARTED| acquire (recovery)     | зависший прогон (>30 мин) → НОВАЯ строка STARTED, старая сохранена |

Поскольку `logicalPeriod=D-1` стабилен на протяжении окон 09/10/11 одного календарного утра,
идемпотентность «одна COMPLETE-витрина на D-1» и «ERROR→retry в следующем окне» работают как надо.

## Решения аудита (приняты в REV1)

**D1. Ширина IAM — принято dataset-level** (`wb_mart` dataEditor + `wb_raw` dataViewer): соразмерно
функции (витрина — ролап ВСЕГО канонического слоя) и устойчиво к эволюции процедур.

**D2. Двойной prod-guard — оставлен** (cli `prodOnly` до lease + loader `MART_ENV` внутри handler):
belt-and-suspenders на пути публикации production.

## Порядок применения (ТОЛЬКО после merge и APPROVE диффа)

1. `sql/mart3/pr_mart3b_mart_runs.sql` — `CREATE OR REPLACE VIEW V_MART_RUN_LOG` (идемпотентно; таблица не трогается).
2. Terraform apply: `wb_mart_prod` Job (создаётся на bootstrap hello) + IAM (`prod_edit_mart`, `prod_view_raw`).
3. `deploy-prod.yml` (workflow_dispatch) — продвигает проверенный digest в `wb-stocks-prod` И `wb-mart-prod`.
   ТОЛЬКО после этого шага Job исполняет реальный `node dist/cli.js mart` (до него — hello-образ).
4. Валидация: ручной триггер `wb-mart-prod` (Scheduler ещё нет) → проверить LOADER_RUNS(loader='mart')
   COMPLETE, MART_RUNS COMPLETE того же `run_id`, `V_MART_RUN_LOG.lease_status='COMPLETE'`, `lease_only_no_mart=FALSE`.
5. PR-Mart3b-3: Cloud Scheduler `mart-daily-prod` `0 9,10,11 * * *` Europe/Moscow (PAUSED) + invoker-binding + алерты.

Scheduler в этом PR НЕТ (соответствует «оставить Scheduler пока PAUSED» — сам Scheduler = PR3b-3).
