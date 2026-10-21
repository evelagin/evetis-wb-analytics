# PR-Mig1 — Stocks shadow parity (план приёмки)

Пилот миграции: загрузчик остатков WB портирован в Cloud Run Job `wb-stocks-shadow`.
Пишет ТОЛЬКО в теневые `RAW_WB_STOCKS__CR` + `WB_STOCKS_SNAPSHOTS__CR`. Прод-таблицы
и прод-загрузчик Apps Script не трогаются; prod Scheduler не включается.

Приёмка из двух частей (по дизайну rev2): **A. replay-parity (строго 1:1)** +
**B. live shadow-parity ≥5 дней**.

## A. Replay / fixture parity (детерминированно, в CI)
Тест `cloud/test/stocks_normalize.test.ts` прогоняет сохранённый payload
`cloud/test/fixtures/wb_stocks_t6.json` через TS-нормализатор и проверяет 1:1:
схему строк, нормализацию id, дедуп ключа `nm|chrt|wh`, null-policy, метку
агрегатного склада, SKU-match, метрики, список unmatched. Плюс validate:
пустой ответ = ERROR, дубль ключа = ERROR, отрицательные значения = ERROR.
Зелёный `npm test` в CI = structural parity доказана независимо от живых остатков.

Structural-инварианты (совпадают со старым Apps Script `WbStocksSnapshot.gs`):
grain snapshot×nm_id×chrt_id×warehouse_id; ключ `nm|chrt|wh`, distinct==rows, dup==0;
пустой T6 = ERROR; агрегатный склад = warehouseId 0 / «Остальные»; SKU по nmId из
`REF_SKU_MASTER` (не из Sheets); manifest STARTED→COMPLETE/ERROR; пост-COUNT
written==distinct==expected.

## B. Live shadow parity (≥5 последовательных дней)
Shadow-Job идёт по расписанию (~06:30 МСК) параллельно с прод-загрузчиком Apps
Script. Сравниваем снимки С БЛИЗКИМ временем получения (`ABS(Δ snapshot_ts) ≤ 5 мин`),
т.к. остатки меняются между запусками.

### Выбор снимков дня
```sql
-- последний COMPLETE снимок дня в каждом контуре
WITH cr AS (
  SELECT snapshot_id, completed_at
  FROM `PROJECT.wb_raw.WB_STOCKS_SNAPSHOTS__CR`
  WHERE status='COMPLETE' AND DATE(completed_at)=@d
  QUALIFY ROW_NUMBER() OVER (ORDER BY completed_at DESC)=1
),
prod AS (
  SELECT snapshot_id, completed_at
  FROM `PROJECT.wb_raw.WB_STOCKS_SNAPSHOTS`
  WHERE status='COMPLETE' AND DATE(completed_at)=@d
  QUALIFY ROW_NUMBER() OVER (ORDER BY completed_at DESC)=1
)
SELECT cr.snapshot_id cr_id, prod.snapshot_id prod_id,
       TIMESTAMP_DIFF(cr.completed_at, prod.completed_at, MINUTE) AS delta_min
FROM cr CROSS JOIN prod;
```
Количественную parity считаем ТОЛЬКО если `ABS(delta_min) ≤ 5`.

### Quantitative parity по ключу nm|chrt|wh
```sql
WITH cr AS (
  SELECT nm_id, chrt_id, warehouse_id, quantity, in_way_to_client, in_way_from_client
  FROM `PROJECT.wb_raw.RAW_WB_STOCKS__CR` WHERE snapshot_id=@cr_id
),
prod AS (
  SELECT nm_id, chrt_id, warehouse_id, quantity, in_way_to_client, in_way_from_client
  FROM `PROJECT.wb_raw.RAW_WB_STOCKS` WHERE snapshot_id=@prod_id
)
SELECT
  COUNTIF(p.nm_id IS NOT NULL AND c.nm_id IS NOT NULL)        AS matched_keys,
  COUNTIF(p.nm_id IS NULL)                                    AS cr_only,
  COUNTIF(c.nm_id IS NULL)                                    AS prod_only,
  COUNTIF(c.quantity <> p.quantity)                          AS changed_qty,
  SUM(ABS(IFNULL(c.quantity,0) - IFNULL(p.quantity,0)))      AS abs_qty_delta,
  SAFE_DIVIDE(SUM(ABS(IFNULL(c.quantity,0)-IFNULL(p.quantity,0))),
              SUM(IFNULL(p.quantity,0)))                     AS rel_qty_delta
FROM cr c FULL OUTER JOIN prod p USING (nm_id, chrt_id, warehouse_id);
```

### Контрольные суммы и манифест
```sql
SELECT
  (SELECT SUM(quantity) FROM `...RAW_WB_STOCKS__CR` WHERE snapshot_id=@cr_id)   AS cr_sum_all,
  (SELECT SUM(quantity) FROM `...RAW_WB_STOCKS`     WHERE snapshot_id=@prod_id) AS prod_sum_all;
-- манифест __CR: status=COMPLETE, duplicate_keys=0, written_rows=distinct_keys=expected_rows.
-- распределение sku_match_status в __CR ≈ прод (доля matched/not_found).
```

## Критерии приёмки
1. Replay parity (A) — зелёный в CI.
2. ≥5 последовательных дней shadow snapshot = COMPLETE, `duplicate_keys=0`,
   `written=distinct=expected`.
3. ≥1 день с реальным изменением остатков (не статичный кабинет).
4. На снимках `≤5 мин`: `cr_only`/`prod_only` малы и объяснимы, `rel_qty_delta`
   мала и объясняется временем; расхождения не структурные.
5. structural parity 100% (схема/ключи/дедуп/SKU-match/агрегатный склад).
Без replay-теста live-окно поднять до 7 дней.

## Runbook (владелец)
- Деплой shadow: `deploy-shadow.yml` соберёт образ (со `stocks`) и обновит Job.
- Запуск: `scheduler-control.yml` → resume **shadow** (или `run-now` вручную N дней).
  **Prod Scheduler НЕ включать.** Прод-таблицы не трогаются.
- Наблюдение: манифест `WB_STOCKS_SNAPSHOTS__CR` + parity-SQL выше.
- По зелёной приёмке — отдельный **PR-Mig1b** (cutover: отключить Apps Script-триггер
  остатков → переключить prod-Job на прод-таблицы → включить prod Scheduler), строго
  по формальному порядку cutover из дизайна.

---

# PR-Mig1c — T5, контроль T5/T6 и детектор доступности (21.10.2026)

## Что добавлено
Cloud Run Job `wb-stocks-shadow` повторяет весь боевой `wbStocksSnapshotCore_`
(`apps-script/ingestion/WbStocksSnapshot.gs:374-533`), а не только T6:

- **T5 warehouse_remains** — задача create → status (20 опросов по 9 с) → download
  (`cloud/src/loaders/stocks/wbApi.ts`, `fetchStocksT5`). Неблокирующий: сбой T5 →
  `control_status = availability_status = 'T5_UNAVAILABLE'`, снимок T6 пишется.
- **Детализация T5** → `RAW_WB_STOCKS_T5__CR` (грейн snapshot × nm × barcode × techSize × склад,
  `row_type` WAREHOUSE / AGGREGATE / PSEUDO_TOTAL / PSEUDO_TO_CLIENT / PSEUDO_FROM_CLIENT).
  Best-effort: потеря детализации не валит T6, `t5_rows_written = 0`.
- **Контроль T5↔T6** (`t5_control_sum`, `t5_wb_rf_sum`, `t5_named_sum`, `control_status` OK/MISMATCH
  с допуском 2, `control_delta`) и **детектор доступности** (`t6_comparable`, `availability_gap`,
  `_prev`, `_delta`, `_ratio`, `availability_status` OK / DEGRADED / WAREHOUSE_DROP / DATA_ERROR /
  NO_BASELINE). Чистое ядро — `cloud/src/loaders/stocks/t5.ts`, пороги — 1:1 из Apps Script.
- База детектора (`availability_gap_prev`) читается из **своей** manifest-таблицы
  (`WB_STOCKS_SNAPSHOTS__CR` в shadow): контуры не смешиваются.
- `source_api` T6 выровнен с прод: `WB_API_STOCKS` (было `WB_API_ANALYTICS_STOCKS`).
- `warehouses_count` считается по ключу склада (код, иначе имя), как в Apps Script;
  добавлен `anonymized_warehouse_rows`.
- Повтор уже COMPLETE-снимка больше не затирает `control_status` значением `REUSED`
  (DRO-1 читает любой статус кроме `OK` как `CONTROL_MISMATCH`).

## Включение без зависимости от порядка выката
`deploy-shadow.yml` перевыкатывает образ на КАЖДЫЙ merge в `cloud/**`. Поэтому T5 управляется
`STOCKS_T5_MODE` (по умолчанию `off` — поведение до Mig1c, прежний набор колонок manifest).
`on` и `STOCKS_T5_TABLE` выставляет Terraform **тем же apply**, что создаёт `RAW_WB_STOCKS_T5__CR`
и добавляет колонки в `WB_STOCKS_SNAPSHOTS__CR`. Откат без пересборки — `STOCKS_T5_MODE = "off"`.

## Доказательства до развёртывания
1. **Replay 1:1 с исполняемым Apps Script** (`cloud/test/stocks_t5_apps_script_parity.test.ts`):
   один сохранённый ответ (T6 + задача T5) проходит через `wbStocksSnapshotCore_` в `node:vm`
   и через `stocksLoader`. Совпадают все строки T6, все строки T5 и 26 метрик manifest; сценарии —
   три значения базы детектора, недоступный T5, `STOCKS_T5_MODE=off`.
2. **Реальные снимки** (вне репозитория, только чтение): прод-снимки 08–21.10.2026 восстановлены
   в ответы WB (T6 — из `raw_json`, T5 — сборкой строк по товару) и пропущены через TS-нормализаторы:
   14/14 дней — T6 и T5 построчно равны прод-RAW (21–23 и 198–207 строк), 17 полей manifest
   (суммы T5, контроль, детектор, счётчики) без расхождений.
3. Сверочный SQL `sql/mig1c_stocks_t5_parity.sql` проверен самосравнением prod↔prod: 8/8 дней EXACT.

## Известные расхождения источников (не алгоритма) — разбирать до FAIL
- **SKU:** Apps Script берёт привязку из листа `SKU_MASTER`, Cloud Run — из `REF_SKU_MASTER`
  (синхронизация листа → BQ ежедневно ~08:22 МСК). Правка листа после синхронизации даст
  `t5_attr_diff`/`t6_attr_diff` до следующего `runRefSync` — это FAIL дня с объяснением, не дефект порта.
- **Один токен и один кабинет:** prod (06:23) и shadow (06:30) создают по задаче T5 под одним
  `WB_TOKEN_ANALYTICS`. 429 на T5 Cloud Run повторяет как Apps Script (пауза 21 с, до 3 раз),
  таймаут запроса T5 — 30 с, общий бюджет T5 — 8 мин, затем `T5_UNAVAILABLE` (Job не убивается
  по таймауту 1800 с и не оставляет manifest в STARTED).
- **Повтор в те же сутки:** детализация T5 пишется load job с id на сутки — повтор не дублирует
  строки; если первая попытка T5 уже загружена, контроль считается по свежему T5, а в логе
  `stocks_t5_detail_reused`. Если load job детализации дня упал, повтор в те же сутки её не
  запишет (`t5_rows_written = 0`) — видно в manifest.

## Пять дней паритета (критерии приёмки Mig1c)
Запуск: shadow-расписание 06:30 МСК (уже ENABLED), prod Apps Script 06:23 МСК. Отчёт —
QUERY 1 из `sql/mig1c_stocks_t5_parity.sql`, ежедневно после 06:35 МСК.

| # | Критерий | Порог |
|---|---|---|
| 1 | 5 последовательных дней со снимком на обеих сторонах | нет `MISSING` |
| 2 | Структурный паритет T6: ключи, SKU, склад, `source_api`, `is_aggregate_warehouse` | `t6_prod_only = t6_cr_only = t6_attr_diff = 0` каждый день |
| 3 | Структурный паритет T5: у совпавших ключей `row_type`, SKU, `vendor_code`, `volume`, `source_api` | `t5_attr_diff = 0`, дублей грейна 0, `t5_one_sided_unexplained = 0` |
| 4 | Структурные метрики manifest (строки T6, ключи, nm, склады, unmatched) | `manifest_struct_diff` пуст |
| 5 | Точный паритет количеств, контрольных сумм и статусов | не меньше 3 дней из 5 `EXACT`; остальные — только `DATA_DRIFT` |
| 6 | Shadow-инварианты | `LOADER_RUNS` shadow COMPLETE, `duplicate_keys = 0`, `written = distinct = expected` (QUERY 2 из `sql/mig1_stocks_parity.sql`) |
| 7 | Статусы контроля | `cr_control` и `cr_avail` равны прод в каждом `EXACT`-дне; день с `T5_UNAVAILABLE` на любой стороне = `SOURCE_GAP`: не FAIL, но и не засчитывается в 5 дней |

`DATA_DRIFT` допустим только как следствие изменения остатков между 06:23 и 06:30: у совпавших
ключей типы и SKU равны, разошлись количества, либо ключ T5 есть лишь на одной стороне.

**Односторонние ключи T5 раскрываются и объясняются, а не принимаются на веру.** WB не отдаёт в T5
строки с нулевым остатком (на 21.10 нулевых строк 0 из 204), поэтому продажа или поступление в эти
7 минут МОГУТ изменить состав ключей — но это не доказательство для конкретного ключа. Ключ
засчитывается как дрейф, только если движение того же товара видно НЕЗАВИСИМО от него:
(a) у того же `nm|barcode|techSize` изменилось количество на совпавшем ключе, или (b) в T6 по тому же
`nm_id` изменились остатки или потоки. Иначе — `t5_one_sided_unexplained > 0` и день `FAIL`.
QUERY 1 показывает `t5_one_sided`, `t5_one_sided_qty` (суммарное влияние в штуках) и
`t5_one_sided_unexplained`; QUERY 3 — каждый такой ключ: сторона, тип строки, SKU, количества,
какие изменения того же товара его объясняют. QUERY 3 — обязательное приложение к каждому
`DATA_DRIFT`-дню.

Проверка правила на живых данных (только чтение, подстановка прод вместо shadow):
самосравнение prod↔prod за 8 дней — 0 односторонних ключей, все `EXACT`; сравнение каждого
дня со снимком СЛЕДУЮЩИХ суток (дрейф 24 ч, на порядки больше 7 минут) — 18 односторонних ключей
T5 за 7 пар дней, все объяснены движением того же товара; единственный `FAIL` (16.10↔17.10) — из-за
появления новых ключей T6, как и положено.

Счёт приёмки неизменен: **5 последовательных зачтённых дней** (EXACT или DATA_DRIFT), из них
**не менее 3 EXACT**, без `FAIL` и `MISSING`; `SOURCE_GAP` не засчитывается и прерывает
последовательность. Любой `FAIL` обнуляет счётчик дней. Первый день после включения
T5 даёт `NO_BASELINE` в shadow (базы ещё нет) — он засчитывается по критериям 1–4 и 6, но не в 5.
Без `EXACT`-дней приёмку не подписывать: значит, снимки систематически расходятся во времени и
критерий 5 не проверен.

## Мониторинг к cutover (Mig1b) — что уже эквивалентно и что нужно до отключения Apps Script
Потребители журнала остатков (проверено по `sql/**` 21.10.2026):
- DRO-1 `stocks_snapshot` — свежесть по слоту 06:30 (`WB_STOCKS_SNAPSHOTS`) и полнота по
  `control_status` (`sql/health/dro1_02_probe.sql:61,260`); FACT_STOCKS_SNAPSHOT
  (`sql/mart/pr_mart1_facts.sql:229`); `dashboard_layer_v1.sql:171`; `V_WB_STOCKS_CURRENT`,
  `V_WB_STOCKS_T5_CURRENT` → Control Tower, лист EVETIS OPERATIONS; `store_inventory_loss_v1.sql`.
- Prod Cloud Run пишет те же таблицы и те же статусы, поэтому эти сигналы продолжаются без правок.
  `INGEST_RUNS` остатки не использовали никогда — его перевод для stocks не нужен.

Отличия, которые нужно учесть в Mig1b:
1. `snapshot_id` в Cloud Run один на сутки (`STOCK_SNAP_prod_YYYYMMDD`), повтор после ERROR
   переводит ту же строку manifest в STARTED → история ERROR остаётся только в `LOADER_RUNS`
   (её читает журнал отказов DRO-1, `dro1_07_run_failure_ledger.sql`). Остановка загрузки
   ловится свежестью DRO-1 (нет COMPLETE к 08:00 МСК).
2. **Пробел, общий с Apps Script:** если пропала только детализация T5 (`t5_rows_written = 0`
   при `control_status = 'OK'`), DRO-1 его не видит, а `V_WB_STOCKS_T5_CURRENT` становится пустой.
   До отключения Apps Script — добавить в полноту DRO-1 условие `t5_rows_written > 0` (отдельный PR,
   production-SQL, по ACK).
3. Реестр DRO-1: строку `stocks_cloudrun` (`DISABLED_IN_REGISTRY`) и источник `stocks_snapshot`
   (`WB_API/APPS_SCRIPT`) привести к факту в том же релизе, что cutover.
4. Финансы в Mig1b не входят, но их предусловие (два WEEKLY-отчёта на неделях 31.08 и 28.09 —
   разрез по границе месяца, сверка держит один отчёт) зафиксировано для R8 в дизайне rev2 §15
   и повторяется в чек-листе каждого следующего cutover-релиза: Mig1b его не блокирует.
5. Порядок cutover — дизайн rev2 §9: prod Job → ручной прогон → включить Scheduler → снять
   `runWbStocksSnapshot`. Одновременной записи в один прод-снимок нет: Apps Script и Cloud Run
   пишут разные `snapshot_id`, потребители берут последний COMPLETE.

## Ранбук shadow-выпуска Mig1c (OWNER ACK MIG1C-SHADOW)

Ожидаемый состав плана (вход `expected_changes` в `infra.yml`):
```
google_bigquery_table.raw_wb_stocks_t5_cr=create,google_bigquery_table.wb_stocks_snapshots_cr=update,google_bigquery_table_iam_member.shadow_write_raw_t5_cr=create,google_cloud_run_v2_job.wb_stocks_shadow=update
```
`targets`: `google_cloud_run_v2_job.wb_stocks_shadow` — `depends_on` втягивает таблицу T5, manifest и IAM
(проверено локальным `terraform plan -lock=false` 21.10.2026: 2 add, 2 change, 0 destroy; no-op — только
`google_service_account.loaders_shadow` и 12 `google_project_service.enabled[*]`).

1. **Merge #369** — после освобождения main сессией Heldi (#364). `deploy-shadow.yml` сам обновит образ
   `wb-stocks-shadow`; env `STOCKS_T5_MODE` ещё нет → T5 off, поведение прежнее.
   Readback: `gcloud run jobs describe wb-stocks-shadow --region europe-west1 --format=json` — образ = digest
   из сводки deploy-shadow, env без `STOCKS_T5_*`.
2. **Окно 05:30–06:15 МСК** того же или следующего дня (почему — п. 5).
3. **`infra.yml` → plan** (ref `main`): `targets` и `expected_changes` как выше. Ожидается `PLAN GUARD: OK — 4`.
4. **`infra.yml` → apply** (ref `main`, approval environment `infra`): те же входы. Apply заново считает
   план, guard проверяет ИМЕННО его и применяет тот же планфайл; при любом отличии — падение до apply.
5. **Readback** (только чтение):
   - `RAW_WB_STOCKS_T5__CR`: 15 колонок, партиция `_snapshot_date`, кластер `nm_id, warehouse_name`;
   - `WB_STOCKS_SNAPSHOTS__CR`: 32 колонки, первые 21 — в прежнем порядке, число строк не изменилось;
   - IAM таблицы `RAW_WB_STOCKS_T5__CR`: `roles/bigquery.dataEditor` у `sa-loaders-shadow`, prod-SA нет;
   - `wb-stocks-shadow`: env `STOCKS_T5_MODE=on`, `STOCKS_T5_TABLE=RAW_WB_STOCKS_T5__CR`, образ — тот же digest, что в п. 1;
   - `wb-stocks-prod`: `args=["noop"]`, Scheduler `PAUSED` — без изменений.
6. **Один ручной запуск — до 06:30 МСК**: `gcloud run jobs execute wb-stocks-shadow --region europe-west1 --wait`.
   ⚠️ Снимок shadow — один на сутки, а execution guard после COMPLETE завершает повтор как no-op:
   ручной запуск ПОСЛЕ планового 06:30 ничего не проверит. Плановый 06:30 в день ручного запуска
   завершится как no-op — это ожидаемо. Ожидания: `LOADER_RUNS` shadow/stocks COMPLETE;
   `WB_STOCKS_SNAPSHOTS__CR` COMPLETE, `control_status` ∈ {OK, MISMATCH, T5_UNAVAILABLE} (не `NOT_RUN`),
   `availability_status = NO_BASELINE`, `t5_rows_written > 0`; `RAW_WB_STOCKS_T5__CR` = `t5_rows_written` строк.
7. **Откат**: `STOCKS_T5_MODE = "off"` в `cloud_run_jobs.tf` → plan/apply тем же порядком (одно изменение:
   `google_cloud_run_v2_job.wb_stocks_shadow=update`). Таблица и колонки безвредны и остаются.

День ручного запуска — день 1 окна (NO_BASELINE, в EXACT не засчитывается); дни 2–6 — пять дней паритета.
