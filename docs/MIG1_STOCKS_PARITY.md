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

## Пять дней паритета (критерии приёмки Mig1c)
Запуск: shadow-расписание 06:30 МСК (уже ENABLED), prod Apps Script 06:23 МСК. Отчёт —
QUERY 1 из `sql/mig1c_stocks_t5_parity.sql`, ежедневно после 06:35 МСК.

| # | Критерий | Порог |
|---|---|---|
| 1 | 5 последовательных дней со снимком на обеих сторонах | нет `MISSING` |
| 2 | Структурный паритет T6: ключи, SKU, склад, `source_api`, `is_aggregate_warehouse` | `t6_prod_only = t6_cr_only = t6_attr_diff = 0` каждый день |
| 3 | Структурный паритет T5: ключи, `row_type`, SKU, `vendor_code`, `volume`, `source_api` | `t5_prod_only = t5_cr_only = t5_attr_diff = 0`, дублей грейна 0 |
| 4 | Структурные метрики manifest (строки, ключи, nm, склады, `t5_rows_written`, unmatched) | `manifest_struct_diff` пуст |
| 5 | Точный паритет количеств, контрольных сумм и статусов | не меньше 3 дней из 5 `EXACT`; остальные — только `DATA_DRIFT` |
| 6 | Shadow-инварианты | `LOADER_RUNS` shadow COMPLETE, `duplicate_keys = 0`, `written = distinct = expected` (QUERY 2 из `sql/mig1_stocks_parity.sql`) |
| 7 | Статусы контроля | `cr_control` и `cr_avail` равны прод в каждом `EXACT`-дне; `T5_UNAVAILABLE` только вместе с прод (`SOURCE_GAP` не засчитывается в 5 дней) |

`DATA_DRIFT` допустим только как следствие изменения остатков между 06:23 и 06:30: ключи и типы
совпали, разошлись количества. Любой `FAIL` обнуляет счётчик дней. Первый день после включения
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
4. Порядок cutover — дизайн rev2 §9: prod Job → ручной прогон → включить Scheduler → снять
   `runWbStocksSnapshot`. Одновременной записи в один прод-снимок нет: Apps Script и Cloud Run
   пишут разные `snapshot_id`, потребители берут последний COMPLETE.
