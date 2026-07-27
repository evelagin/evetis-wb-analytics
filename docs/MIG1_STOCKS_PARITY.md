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
