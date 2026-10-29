# Mig1b — боевое переключение остатков WB на Cloud Run (план, НЕ исполнять)

Статус: **черновик, подготовлен 26.10.2026 и ждёт OWNER ACK MIG1B.**
Основания: дизайн rev2 §9 (формальный cutover) и `docs/MIG1_STOCKS_PARITY.md`, разделы Mig1c и «Мониторинг к cutover».

До ACK запрещено:
- отключать Apps Script;
- включать prod Scheduler;
- менять production RAW.

## 0. Где мы сейчас (26.10.2026, проверено чтением)
- `wb-stocks-shadow`: образ `ec0bce74…` (main `69d6926`, код Mig1c), T5 включён, Scheduler ENABLED 06:30 МСК.
- `wb-stocks-prod`:
  - образ `e6ef1d82…` — main `50345ce`, **до Mig1c**: нет T5, `source_api` старый;
  - `args=["noop"]`, Scheduler PAUSED;
  - env без `STOCKS_*`. Это ловушка: загрузчик по умолчанию пишет в `__CR`-таблицы;
  - SA `sa-loaders-prod` имеет право записи только в `LOADER_RUNS`, на чтение — REF и секрет `WB_TOKEN_ANALYTICS`.
- Владельцы настроек `wb-stocks-prod`:
  - Terraform: `args`, SA, `timeout`, `max_retries`;
  - env и образ: **не** Terraform — `ignore_changes`, управляет `deploy-prod.yml`.
- `deploy-prod.yml` продвигает **один digest во все prod-Job** (12, включая Unitka и Ozon). Для переключения одних остатков это лишний охват.
- Боевой загрузчик — Apps Script `runWbStocksSnapshot`, 06:23 МСК. Пишет в `RAW_WB_STOCKS`, `RAW_WB_STOCKS_T5` и `WB_STOCKS_SNAPSHOTS`.

## 1. Предусловия ACK MIG1B
1. **Паритет подписан.** По `sql/mig1c_stocks_t5_parity.sql` нужно 5 последовательных зачтённых дней (EXACT или DATA_DRIFT), из них ≥ 3 EXACT, без FAIL и MISSING. SOURCE_GAP серию прерывает. На 26.10: 24.10 DATA_DRIFT, 25.10 DATA_DRIFT, 26.10 EXACT.
   Особенность детектора: на следующий день после DATA_DRIFT отличается только `availability_gap_prev/delta`, потому что у shadow своя база. Такой день — DATA_DRIFT, хотя данные идентичны. Окно серии может сдвигаться.
2. **DRO-1 видит пропажу детализации T5** (PR Mig1b-0, §2.3). Сейчас `t5_rows_written = 0` при `control_status = OK` даёт COMPLETE, а `V_WB_STOCKS_T5_CURRENT` при этом пуст. Пробел общий с Apps Script; закрывается до cutover.
3. **PR Mig1b-0 слит, CI зелёный.** Production он не меняет (§2).
4. Окно main согласовано с Heldi.

## 2. PR Mig1b-0 — подготовка, без эффекта на production
1. **Terraform** (`infra/terraform/cloud_run_jobs.tf`, `bigquery.tf`):
   - `wb_stocks_prod`: `args = ["stocks"]`. Пока Scheduler PAUSED и ручных запусков нет, это ничего не запускает.
   - `local.prod_stocks_env` объявляется для пути CREATE с боевыми таблицами:
     - `STOCKS_RAW_TABLE=RAW_WB_STOCKS`;
     - `STOCKS_SNAPSHOT_TABLE=WB_STOCKS_SNAPSHOTS`;
     - `STOCKS_T5_TABLE=RAW_WB_STOCKS_T5`;
     - `STOCKS_T5_MODE=on`;
     - `REF_SKU_TABLE`, `WB_ANALYTICS_SECRET`.
     При UPDATE env не применится, его задаёт п. 2.
   - IAM `sa-loaders-prod`: `roles/bigquery.dataEditor` на 3 таблицы — `RAW_WB_STOCKS`, `RAW_WB_STOCKS_T5`, `WB_STOCKS_SNAPSHOTS`. Только табличный уровень.
   - Ожидаемый состав apply: `wb_stocks_prod=update` и три новых IAM-ресурса `=create` для этих таблиц (имена — в PR). Итого 3 create, 1 update, 0 destroy.
2. **Выкат одного Job** — новый вход `deploy-prod.yml`, например `only_job=wb-stocks-prod`. По умолчанию поведение прежнее.
   - Обновляет только `wb-stocks-prod`: образ = проверенный в shadow digest, `--update-env-vars` с `IMAGE_DIGEST`, `GIT_SHA` и боевыми `STOCKS_*` из п. 1.
   - Остальные prod-Job не трогаются.
3. **DRO-1** (`sql/health/dro1_02_probe.sql`, блок `completeness`):
   - условие полноты `stocks_snapshot` = `control_status = 'OK' AND IFNULL(t5_rows_written, 0) > 0`;
   - новая причина `T5_DETAIL_MISSING`;
   - в реестре `dro1_01_contract.sql`: `stocks_snapshot` → источник `WB_API/CLOUD_RUN`, слот 06:30; `stocks_cloudrun` — включить или слить со `stocks_snapshot`.
   - Production-SQL применяется по своему ACK, до cutover.
4. Тесты: env и `args` у prod в плане, guard `expected_changes`, проверка SQL DRO-1 на синтетике (OK / T5_DETAIL_MISSING / MISMATCH).

## 3. Последовательность переключения — без двойной записи
День **X** — первый день после подписанного паритета и слитого Mig1b-0.

1. **X, после 06:35 МСК** (оба снимка дня X уже сделаны):
   - a. Выкат одного Job (§2.2) с digest **D** = текущий образ shadow. Readback: образ = D, env с боевыми таблицами и `STOCKS_T5_MODE=on`.
   - b. `infra.yml`: plan, затем apply с `expected_changes` (§2.1). Readback: `args=["stocks"]`, IAM на 3 таблицы, Scheduler prod **PAUSED**.
   - c. Ручного prod-запуска **нет**. За день X Apps Script уже записал снимок, и ручной запуск дал бы второй снимок того же дня. Свежесть дня X держит Apps Script.
2. **X, вечер, между 18:00 и 05:30 МСК X+1:**
   - a. Владелец в редакторе Apps Script выполняет `wbStocksRemoveTrigger()`.
   - b. Проверка только чтением `wbFinanceBackfillAuditTriggers()`: обработчика `runWbStocksSnapshot` нет. Остальные триггеры не тронуты.
   - c. `scheduler-control.yml`: resume `wb-stocks-prod`. Проверка в GCP: ENABLED, `30 6 * * *`, Europe/Moscow.
   - d. `wb-stocks-shadow` остаётся включён ещё на 3 дня как эталон (Cloud Run против Cloud Run).
3. **X+1, 06:30 МСК** — первый боевой снимок Cloud Run. Apps Script в этот день уже не пишет, поэтому двойной записи нет.

## 4. Контроль первого production-снимка (X+1, после фактического завершения)
- `LOADER_RUNS` prod/stocks: COMPLETE, `rows_fetched = rows_loaded`.
- `WB_STOCKS_SNAPSHOTS`:
  - ровно одна строка за X+1: `STOCK_SNAP_prod_<X+1>`, COMPLETE;
  - строки Apps Script за X+1 **нет**;
  - `control_status = OK`, `t5_rows_written > 0`;
  - `availability_status` — не `NO_BASELINE`: база продолжается из истории Apps Script в той же таблице.
- `RAW_WB_STOCKS` и `RAW_WB_STOCKS_T5` за X+1: строк = manifest, дублей грейна 0, `source_api` = `WB_API_STOCKS` / `WB_API_STOCKS_T5`.
- Сверка prod с shadow за X+1 (QUERY 1 с подстановкой prod-Cloud Run): EXACT или объяснённый DATA_DRIFT.
- Потребители:
  - `V_WB_STOCKS_CURRENT` и `V_WB_STOCKS_T5_CURRENT` показывают новый снимок;
  - `FACT_STOCKS_SNAPSHOT` за X+1 после прогона витрины 07:00;
  - Control Tower: свежесть `WB_STOCK`;
  - лист EVETIS OPERATIONS: `wb_stock_date`;
  - DRO-1 `stocks_snapshot`: свежесть и полнота зелёные.
- Повторить проверку в X+2 и X+3. После трёх чистых дней — пауза `wb-stocks-shadow`. Код Apps Script хранится ещё ≥ 14 дней.

## 5. Критерии остановки и откат
**STOP**, если в X+1–X+3 выполняется любое из условий:
- нет COMPLETE prod до 07:30 МСК после фактического завершения исполнения;
- ERROR;
- `control_status` ∈ {MISMATCH, T5_UNAVAILABLE};
- `t5_rows_written = 0`;
- дубли > 0;
- пустые `V_WB_STOCKS_*_CURRENT`;
- нет `FACT_STOCKS_SNAPSHOT` за день;
- в prod-RAW записан `source_api` или таблица `__CR`.

**Откат** (по решению владельца, без удаления данных):
1. `scheduler-control.yml`: pause `wb-stocks-prod`.
2. Владелец: `wbStocksInstallDailyTrigger()`. Если сегодняшнего валидного снимка нет — однократно `runWbStocksSnapshot()`. Снимок Apps Script начнётся позже и станет текущим для потребителей.
3. Строки Cloud Run в prod-RAW **не удаляются**: append-only, потребители берут последний COMPLETE.
4. По желанию — `args=["noop"]` через Terraform с guard.

Слепого повтора prod-запуска в тот же день нет. Суточный `snapshot_id` Cloud Run и guard делают повтор после COMPLETE no-op. Повтор после ERROR — только отдельным решением.

## 6. Что ACK MIG1B должен назвать явно
- День X.
- Digest D и SHA исходников.
- Точный `expected_changes`.
- Время снятия триггера Apps Script.
- Кто снимает триггер (владелец, в редакторе Apps Script).
- Окно main с Heldi.
