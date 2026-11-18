# FINAL INTEGRATION GATE: перенос загрузки WB в Cloud Run (10.11.2026)

Состояние на 10.11.2026 вечер. Всё в Draft. **Ничего не слито, не применено и не выкачено.** Scheduler, Apps Script,
Cloud Run production и BigQuery-объекты не менялись. Всё проверялось только чтением: `terraform plan -lock=false`,
`gcloud … describe/list`, SELECT в BigQuery, GET в Artifact Registry.

Предыдущий отчёт: `MIG_INTEGRATION_READINESS_2026-11-10.md`. Этот документ его дополняет и в расхождениях заменяет.

## 0. Что изменилось с отчёта готовности

| # | Находка | Последствие | Где исправлено |
|---|---|---|---|
| 1 | Живой образ `wb-stocks-shadow` (`3a4f08ab…`, сборка 07.11 из `295c4e23`) — это **OCI index**: образ linux/amd64 + attestation. Pre-check #416 принимал только одиночный манифест | Выкат D′ в MIG1B **остановился бы на pre-check** («ожидался одиночный образ»). Проверено на живых данных | #416 → `8944797f`: цепочка index → единственный linux/amd64 → конфиг → `git-sha`. На живом образе и живых Job'ах pre = OK |
| 2 | #414 не покрывает DRO-детектор, сторожа пульса и опрос коммуникаций | Полный plan с #414 всё ещё откатывает 3 живых расписания (п. E) | Draft #424 поверх #414 |
| 3 | CI `cloud` у #414 падает: тест ждёт старое расписание `unitka-cogs-publication` | #414 нельзя слить «зелёным» | #424 (тест обновлён) |
| 4 | F1/F2/F4/F5 нужны всем потокам | Без общего механизма — четыре разных реализации | Общий Draft #423 поверх #416 |
| 5 | 10.11 WB впервые вернул в T6 несколько складов (4 склада, 26 строк на 22 nm) | Shadow этот случай ещё не видел. Снимок 11.11 станет первой проверкой | Условие квалификации D′ (п. A.4) |

## A. MIG1B_RELEASE_READY — остатки в production

**Вердикт: READY к OWNER ACK MIG1B при условии**, что владелец подтверждает новый head #416 `8944797f`. Прежний
`264d327e` выпускать нельзя: на нём упадёт pre-check (находка 1).

### A.1 Head, main, CI, эффекты merge
- **PR #416.** Ветка `mig/stocks-mig1b-0-r2`, head `8944797f`, base `main` = `a3065abc`, `mergeStateStatus` CLEAN.
  CI на `8944797f` — SUCCESS: cloud, infra, ozon, tenancy, wb-communications, sql-current.
  Merge — только с `--match-head-commit 8944797f`.
- **Эффекты merge:**
  - `cloud/**` изменён, поэтому `deploy-shadow` соберёт D′ и обновит **только** `wb-stocks-shadow` и
    `unitka-engine-shadow`;
  - prod-Job'ы, Scheduler, IAM и Terraform merge не трогает: `infra.yml` запускается только вручную.
- **Заморозка `cloud/**` от merge #416 до продвижения D′.** #423 и #424 тоже меняют `cloud/**`, их merge в это окно
  пересоберёт shadow и сорвёт квалификацию. Порядок слияния — п. C.

### A.2 Паритет T5/T6
Ворота паритета: `sql/mig1c_stocks_t5_parity.sql`. Пять дней подряд, 05–09.11:
- T6 22/22, `prod_only = cr_only = qty_diff = attr_diff = 0`;
- T5 212–215 из 212–215, расхождений 0, дублей 0.

Пропуски, не связанные с кодом:
- 02.11 — токен;
- 04.11 — нет снимка Apps Script;
- **10.11 — shadow не стартовал.** Execution `wb-stocks-shadow-tmz6p` 03:30Z: `ContainerPermissionDenied` на чтение
  образа, это время отключённого биллинга. Строки в `LOADER_RUNS` нет.

### A.3 Почему в T6 22 строки, а SKU 25 (проверка данными, SELECT)
- В `REF_SKU_MASTER` (WB) 25 nm. В снимке T6 — 22 nm, на 10.11 — 26 строк: nm × склад.
- Трёх nm нет в отчёте WB:

  | nm | SKU | В отчёте T6 последний раз |
  |---|---|---|
  | 252441968 | набор `EVT-SET-HAND-BODY` | не появлялся с 01.08 |
  | 773170315 | набор `EVT-SET-TON-CREAM-MOIST` | 20.09, остаток 0 |
  | 438775437 | `EVT-FC-MOIST-50` | 21.10, остаток 0 |

- Отчёт остатков WB не возвращает карточки без остатка и без движения. Это свойство источника, а не загрузчика:
  Apps Script получает те же 22 nm, паритет EXACT. Расхождения в 3 строки ожидать не нужно.
- **10.11 впервые за историю** в T6 пришли зарубежные склады: Ереван, Бишкек, Гродно плюс «Склад WB РФ».
  - Prod: 1 такой снимок из 69, `distinct_keys` = 26, дублей 0, `control_status` OK.
  - Shadow: таких снимков 0.

### A.4 Квалификация нового digest D′
1. Merge #416 → `deploy-shadow`. В summary — пара (D′, `source_git_sha` = merge-коммит). Readback: у
   `wb-stocks-shadow` образ = D′.
2. **Один штатный** запуск shadow в 06:30 МСК, без ручного запуска. Чистый, если выполнено всё:
   - COMPLETE, `control_status = OK`, `availability_status = OK`;
   - `t5_rows_written > 0`, дублей 0;
   - QUERY 1 за день — EXACT;
   - **если WB снова отдаст несколько складов — ключи T6 nm × склад совпадают 1:1** (п. A.3).

   Иначе STOP.
3. D′ — это OCI index. Pre-check проходит цепочку index → amd64 → конфиг → `git-sha`; на живом index `3a4f08ab`
   проверено: OK.

### A.5 Targeted plan: ровно 3 IAM create + 1 update
Локальный `terraform plan -lock=false` на реальном state, 10.11, на двух деревьях:

| Дерево | Результат |
|---|---|
| #416 | `3 to add, 1 to change, 0 to destroy` |
| #416 + #414 | то же |

- **create:** `google_bigquery_table_iam_member.prod_write_raw_stocks`, `.prod_write_raw_stocks_t5`,
  `.prod_write_stocks_snapshots` — `roles/bigquery.dataEditor` для `sa-loaders-prod` на `wb_raw.RAW_WB_STOCKS`,
  `RAW_WB_STOCKS_T5`, `WB_STOCKS_SNAPSHOTS`.
- **update:** `google_cloud_run_v2_job.wb_stocks_prod`. Поатрибутно только
  `template[0].template[0].containers[0].args[0]: "noop" → "stocks"`. Поля `client`/`client_version` у этого Job'а
  #416 переводит в `ignore_changes`.
- `tools/tf_plan_guard.py` с `expected_changes` (4 адреса): `PLAN GUARD: OK — 4 изменений ровно как ожидалось`.
- **Других ресурсов targeted plan не касается.** Полный plan содержит ещё 20–24 изменения (п. E); `-target` их
  исключает, а guard отклоняет любой лишний адрес.

### A.6 Выкат, pre-check, readback
`deploy-prod.yml`: `only_job=wb-stocks-prod`, `image_digest` = D′, `source_git_sha` = merge #416.

**Pre-check:**
- digest = образ `wb-stocks-shadow`;
- происхождение через index;
- контур текущего `wb-stocks-prod`: проект, `wb_raw`, `sa-loaders-prod`.

**Update** — только `wb-stocks-prod`: образ D′ и боевой env `STOCKS_*`, `STOCKS_T5_MODE=on`, `WB_TOKEN_ANALYTICS`.

**Readback:**
- образ, env, SA;
- `args` выкат не изменил;
- **ни один другой Job не изменился** (образ, generation, env, args).

После #423 к этому добавится общая проверка охвата для всех режимов.

Сейчас: `wb-stocks-prod` — образ `e6ef1d82…`, `args=["noop"]`; Scheduler `wb-stocks-prod` — PAUSED, `30 6 * * *`,
Europe/Moscow.

### A.7 Снятие триггера и порядок Scheduler; один писатель дневного снимка
- Витрина берёт снимок дня как последний COMPLETE по `started_at` (`sql/mart/pr_mart1_facts.sql:232`). У Apps Script
  `snapshot_id` с временем, у Cloud Run — суточный.
- Два писателя в один день не задвоят строки, но **молча подменят** снимок: какой записан последним, тот и
  выигрывает. Поэтому порядок такой:
  1. **День X** — ручного prod-запуска нет: снимок X пишет Apps Script (06:23).
  2. **Вечер X** — `wbStocksRemoveTrigger()` в редакторе Apps Script. Readback страницы «Триггеры»: нет
     `runWbStocksSnapshot`, остальные на месте.
  3. **Только после этого** — `scheduler-control` resume `wb-stocks-prod`. Readback: ENABLED, `30 6 * * *`.
- **STOP X+1…X+3:** любой снимок Apps Script после X; два COMPLETE за день.

### A.8 Первая проверка prod и откат
- **X+1 06:30.** Ровно один COMPLETE за X+1, у него `control_status = OK` и `t5_rows_written > 0`. Дальше по цепочке:
  - `V_WB_STOCKS_*_CURRENT` непустые;
  - FACT за день есть;
  - Control Tower и DRO-1 зелёные.
- **Откат** — решение владельца, данные не удаляются:
  1. pause `wb-stocks-prod`;
  2. `wbStocksInstallDailyTrigger()`;
  3. если валидного снимка за день нет — однократно `runWbStocksSnapshot()`.
- Слепого повтора prod-запуска нет.

## B. M2_M5_SHADOW_READY — тестовый контур по потокам

**Вердикт: READY к OWNER ACK SHADOW для M2, M3, M4. M5 — NOT READY:** ждёт секрет `WB_TOKEN_SUPPLIES` (п. F).
Включение shadow и пробные запуски — только по отдельному ACK.

### B.1 Общее для всех потоков
- **Включение:** GitHub Variable `MN_SHADOW_ENABLED = 'true'`, затем `infra.yml` **targeted** plan/apply с
  `expected_changes`, только `create`. Любой `update`/`delete` = STOP.
- **Доказательство** (репетиция #425, read-only plan на реальном state):
  - все флаги false — полный plan совпадает с #416 + #414 + #424 до адреса: потоки без флагов не добавляют ничего;
  - `M2/M3/M4_SHADOW_ENABLED = true` — **+80 create / 0 update / 0 destroy**;
  - ни одного prod-адреса;
  - 7 Scheduler, все `paused = true`;
  - 7 Job'ов на `cloudrun/container/hello`, образ поставит `deploy-shadow` после merge.
- **Prod-Job'ы и prod-Scheduler shadow не трогает.** Prod-флаги остаются false. `deploy-prod` в режиме потока до
  cutover только предупреждает: Job'а нет.
- **LOADER_RUNS:**
  - shadow пишет `environment = 'shadow'`;
  - `V_INGEST_HEARTBEAT` берёт только `environment = 'prod'`;
  - гейт витрины (`mart/bq.ts`) и детекторы shadow не видят.
- **Запись** — только `__CR`. `targets.ts` потока проверяет контур ↔ таблицы до любой записи, иначе `*_TARGET_MISMATCH`.
- **Токены:**
  - срок проверяется до первого запроса (`*_TOKEN_EXPIRED`);
  - 401/403 → `*_AUTH`, детерминированный отказ без повторов;
  - значения токена нет в тексте ошибки и в логе.
- **Лимит WB «на продавца» общий с Apps Script.** Минуты shadow-расписаний сдвинуты от триггеров Apps Script.
  Первые сутки контролировать 429 в обоих контурах.
- **Отключение shadow** (флаг → false) удаляет и таблицы `__CR` вместе с данными (`deletion_protection = false`).
  Делать только после сохранения отчёта паритета.

### B.2 По потокам

| | M2 заказы/продажи/сверка (#420) | M3 реклама (#419) | M4 финансы (#418) | M5 поставки + REF (#417) |
|---|---|---|---|---|
| SA | `sa-wb-statistics-shadow` | `sa-wb-ads-shadow` | `sa-wb-finance-shadow` | `sa-wb-supplies-shadow`, `sa-ref-sync-shadow` |
| Пишет | `RAW_WB_ORDERS__CR`, `RAW_WB_SALES_RETURNS__CR`, `LOADER_RUNS` | 9 × `RAW_WB_ADV_*__CR`, `LOADER_RUNS` | `RAW_WB_FINANCE__CR`, `FINANCE_LOADER_RUNS__CR`, `FINANCE_REPORT_LOADS__CR`, `FINANCE_WEEK_STATUS__CR`, `FINANCE_WEEK_RECON__CR`, `LOADER_RUNS` | `RAW_WB_SUPPLIES__CR`, `RAW_WB_SUPPLIES_GOODS__CR`, `REF_*__CR` |
| Читает | `REF_SKU_MASTER`, `_DATA`, `REF_ACTIVE_VERSION` | `RAW_WB_ADV_CAMPAIGN_STATS`, `RAW_WB_ADV_COSTS`, `_RUNS`, `V_ADV_CAMPAIGN_STATS`, `V_ADV_COSTS`, `V_ADV_COSTS_SNAPSHOT` | — | лист SKU_MASTER: Sheets API, `spreadsheets.readonly`, `UNFORMATTED_VALUE` |
| Секрет (`secretAccessor` только на свой) | `WB_TOKEN_STATISTICS` — v1 от 28.09, **почти наверняка истёк** | `WB_TOKEN_ADS` — v1 от 26.07; Apps Script работает на `WB_TOKEN_PROMOTION` | `WB_TOKEN_FINANCE` — v1 от 26.07 | `WB_TOKEN_SUPPLIES` — **нет** |
| Scheduler shadow (МСК, PAUSED) | orders `8 1,7,13,19`; sales `52 0,6,12,18`; reconcile `50 4` | ads `40 5,7`; ads-query `5 6,8`; audit `30 4 * * 0` | `5 8,13,19` | supplies `39 2,5`; ref `32 8` |
| Watermark / перечитка | MAX `last_change_date` инкрементальных строк своей таблицы. Ручной импорт и сверка его не двигают. Старт — с `statistics_shadow_*_bootstrap_watermark` = MAX боевой таблицы; пусто → ERROR, а не «с начала времён». Сверка — окно 7 сут. | окно D−1 МСК; повтор — следующим окном Scheduler с новым `run_id`; аудит D−46…D−8 | очередь в `FINANCE_REPORT_LOADS__CR`: DISCOVERED → COMPLETE, stale-recovery; mutex с backfill | 14 суток по `factDate` + правило «в RAW не завершён» |
| Гейты свежести | гейт витрины по `orders`/`sales`, LATEST-ATTEMPT после полуночи МСК D+1. Shadow в него не входит. **#421** переводит DRO и freshness на `V_INGEST_HEARTBEAT` до cutover | `ads` — в том же гейте | свой журнал `FINANCE_LOADER_RUNS`, в `INGEST_RUNS` не входил | `REF_SYNC_RUNS`; у поставок журнала свежести не было |
| Паритет | `sql/mig_m2_statistics_parity.sql`: 5 дней без FAIL, ≥3 EXACT | `sql/mig_ads_parity.sql`: costs EXACT все 5 дней | `sql/mig_finance_parity.sql`: понедельник WEEKLY в окне | `sql/mig_supplies_parity.sql`, `sql/mig_ref_parity.sql` |

### B.3 Скрытые зависимости Apps Script
Аудит `apps-script/ingestion/` (read-only, 10.11). Учитываются **при cutover**, на shadow не влияют.
1. **Реклама: catch-up `runWbAdsDailyCatchUp`** (≈06:15 и 08:15) смотрит только `INGEST_RUNS`. После перевода `ads`
   на `LOADER_RUNS` он не найдёт попытку и запустит **вторую полную загрузку** в Apps Script. Снимать вместе с
   `runWbAdsDaily`. Без лока и без `INGEST_RUNS` остаётся ещё `loadWbAdsCostsAudit`, его тоже снимать.
2. **Продажи и ночная сверка — только вместе.** Общий слот Sales API `WB_SALES_API_LAST_REQUEST_AT_MS` защищает от
   429. Заказы — на том же токене статистики.
3. **Финансы.** Пейсер `WB_FIN_API_LAST_REQ_MS` общий с ручной legacy-загрузкой из меню и с `runWbDailyRefresh`.
   После cutover ручной путь отключить.
4. **Разметка SKU.**
   - Apps Script ставит `internal_sku`/`sku_match_status` по **живому листу** SKU_MASTER, M2 — по `REF_SKU_MASTER`.
     После дневной правки листа до 07:30 возможен дрейф атрибута в паритете; это объяснимый, не FAIL.
   - После cutover REF должен идти **раньше** загрузчиков, которым нужен SKU.
5. **Общий ScriptLock Apps Script.** Финансы 07:26 держат лок до ≈4,5 мин, а REF 07:30 ждёт 1 с и бывает
   SKIPPED_LOCKED. В Cloud Run лока нет, порядок задаётся расписанием.
6. **Watermark'и Script Properties** `WB_ORDERS_/WB_SALES_LAST_CHANGE_WATERMARK`. После cutover пункты меню
   «инкремент/bootstrap» убрать: иначе появится второй писатель со старым watermark.
7. **Ручные пути в те же RAW:** `runWbDailyRefresh`, `backfillWbOperationalPilot*`, пункты меню Ads RAW. Отключить
   или явно оставить ручными.
8. **Контракты журналов** перенесены 1:1:
   - реклама: данные → маркер `RAW_WB_ADV_COSTS_RUNS`;
   - поставки: goods → header;
   - остатки: `availability_*` предыдущего снимка;
   - REF: `REF_ACTIVE_VERSION` + prune 10 версий.
9. **Вью не пересоздаются.**
   - `wbAdsBqCreateViews()` и `bqCreateFinanceView()` никто не вызывает автоматически. **Не запускать вручную**:
     первая откатит `V_ADV_COSTS` → снапшот, вторая снимет фильтр WEEKLY.
   - Cloud Run REF не делает `CREATE OR REPLACE VIEW REF_SKU_MASTER`: вью владеет Terraform.
10. **Дашборды Sheets из триггеров не пересобираются.** Читают листы, которые давно не пополняются, — снятие
    триггеров ничего нового не ломает. Лист-журналы `IMPORT_LOG_*`, `WB_ADS_STATUS` перестанут пополняться;
    читателей в коде нет.

### B.4 REF Sync: права на Google Sheets
- `sa-ref-sync-shadow` получает **«Читатель»** книги «Evetis аналитика 2.0». Выдаёт владелец вручную в UI Sheets,
  без уведомления; в Terraform этого нет.
- ⚠️ Google Sheets не даёт доступ к одному листу: «Читатель» видит **всю книгу**. Загрузчик читает только SKU_MASTER
  (`values.get`, scope `spreadsheets.readonly`), но право шире. Для prod — второй SA, `sa-ref-sync-prod`, тоже
  «Читатель».
- ID книги задан в env Job'а в `infra/terraform/ref_sync_loader.tf` (`REF_SPREADSHEET_ID`), в коде загрузчика его нет.

### B.5 Порядок ACK SHADOW
1. Владелец: новые версии `WB_TOKEN_STATISTICS`, `WB_TOKEN_ADS`, `WB_TOKEN_FINANCE` (если истекли), п. F.
2. Merge #423 → #421 → #420 → #419 → #418, по одному, каждый после полного CI, **после** продвижения D′ (ACK A).
3. Для каждого потока: `MN_SHADOW_ENABLED = true` → targeted plan, только create → apply → readback.
4. `scheduler-control`:
   - `run-now` один раз на shadow-Job;
   - проверить COMPLETE в `LOADER_RUNS` (`environment = 'shadow'`) и строки в `__CR`;
   - `resume`.
5. 5 суток паритета, ежедневный отчёт. Cutover каждого потока — отдельный ACK.


## C. MERGE_INTEGRATION_RECEIPT

### C.1 Головы (10.11 вечер) и стек

| PR | Ветка | Head | Base | Назначение |
|---|---|---|---|---|
| #414 | `chore/bq-cost-schedules-2026-11-10` | `a21b5142` | `main` | расписания экономии (не Draft; CI `cloud` красный — чинит #424) |
| #424 | `chore/bq-cost-schedules-drift-fixup` | `15c280ce` | #414 | DRO, сторож пульса, `evetis-wb-poll` = live; тест cogs. CI 11/11 SUCCESS |
| #416 | `mig/stocks-mig1b-0-r2` | `8944797f` | `main` | M1 остатки (+ OCI index). CI 6/6 SUCCESS |
| #423 | `mig/common-deploy-guards` | `06fa6fbf` | #416 | F1/F2/F4/F5, защита `all`, проверка охвата. CI 6/6 SUCCESS |
| #421 | `mig/ingest-heartbeat-consumers` | `3af84880` | #416 | DRO/freshness → `V_INGEST_HEARTBEAT` |
| #420 | `mig/m2-statistics` | `a1d5ef5e` | #423 | M2 |
| #419 | `mig/m3-ads` | `3249416d` (дерево = `a35cee03`; пустой коммит — пересинхронизация PR на GitHub) | #423 | M3 |
| #418 | `mig/m4-finance` | `636ecf9b` | #423 | M4 |
| #417 | `mig/m5-supplies-ref` | `9bf0002d` | #423 | M5 |
| #422 | `mig/integration-2026-11-10` | `8cb337f8` | — | прежняя репетиция; **заменена #425**, не сливать |
| #425 | `mig/integration-final-2026-11-10` | `911d2df7` | — | **финальная репетиция, не сливать** |

### C.2 Порядок слияния (каждый шаг — свой OWNER ACK, merge-коммит, `--match-head-commit`)
1. **#424 → ветка #414, затем #414 → `main`** (ACK расписаний). Меняет `cloud/test`, поэтому запустится
   `deploy-shadow`: пересборка образа без изменения кода. Делать **до** merge #416.
2. **#416 → `main`** (ACK A, MIG1B). Заморозка `cloud/**` до продвижения D′.
3. После продвижения D′: **#423** → **#421** (DDL #421 — отдельно, до cutover M2/M3) → **#420** → **#419** →
   **#418**. По одному, каждый после полного CI на новом head. GitHub сам перенацелит base на `main`.
4. **#417** — после секрета `WB_TOKEN_SUPPLIES` (п. F).

Каждый merge в `main` с `cloud/**` запускает `deploy-shadow`. Он обновляет только shadow-Job'ы; prod не трогает.

### C.3 Репетиция #425: что проверено
- **Объединение:** `main` `a3065abc` + #424 + #423 + #421 + #420 + #419 (`a35cee03`) + #418 + #417.
  - Конфликты только на стыках потоков: `CHANGELOG.md`, `registry.ts`, `config.ts`, `variables.tf`,
    `scheduler-control.yml` (options), `deploy-shadow.yml` (шаги). Разрешены объединением обеих сторон.
  - `deploy-prod.yml`, `infra.yml`, `tools/deploy_scope_check.py`, `tools/stocks_prod_deploy_check.py` — побайтно
    равны #423.
- **Без потерь** (скрипт сравнения, 128 файлов):
  - 102 файла с одним владельцем побайтно равны его голове;
  - в 26 общих файлах все добавленные строки на месте. Исключения ожидаемые:
    - списки `options` заменены объединённым — он содержит все пункты каждого потока;
    - 9 строк #421 — старый текст #416, переписанный #423.
- **Локально:**
  - tsc — 0 ошибок;
  - lint — 0 ошибок, 1 старое предупреждение;
  - vitest — **102 файла, 2373 passed / 19 skipped**;
  - `terraform fmt -check` — OK, `validate` — OK;
  - pytest (Python 3.12) — **5905 passed, 0 failed** (24 мин 51 с).
- **Режимы `only_job`:** `test_deploy_prod_scope.py` проходит все 6 режимов. В каждом обновляются ровно Job'ы
  режима, в `wb-stocks-prod` — ни одного Job'а M2–M5. Плюс негативные мутации.
- **Terraform на репетиции:** все флаги false — полный plan = #416 + #414 + #424; shadow M2–M4 — +80 create, п. B.1.
- **Сосуществование Apps Script и Cloud Run:**
  - shadow пишет только `__CR`, в `LOADER_RUNS` с `environment = 'shadow'`;
  - потребители читают `V_INGEST_HEARTBEAT`, там только prod;
  - prod-Job'ов M2–M5 нет, пока prod-флаг false;
  - снятие триггеров — только при cutover потока (п. B.3).
- **GitHub CI #425 на `911d2df7`: 11/11 SUCCESS** — cloud ×2, infra ×2, ozon ×2, tenancy ×2, wb-communications ×2, sql-current.

### C.4 Известное
- `tools/tests/test_cloud_controller.py::test_packaged_docker_copy_closure_resolves_canonical_registry` (Heldi)
  падает по таймауту 60 с при ≥4 параллельных pytest. Отдельно проходит за 57–59 с, у самого предела. Код
  миграции его не трогает. Нестабильный тест — отдельная задача, не блокер.
- Тесты tenancy требуют Python 3.12.


## D. PRODUCTION_SAFETY_MATRIX

| Действие | Что затрагивает | Защита (fail-closed) | Доказательство |
|---|---|---|---|
| `deploy-prod` `only_job=all` | все prod-Job'ы режима all | первый шаг до токена требует фразу `PROMOTE-ALL-PROD`; проверка охвата после | `test_deploy_prod_scope.py::test_guard_*` |
| `deploy-prod` `only_job=wb-stocks-prod` | только `wb-stocks-prod` | pre-check происхождения (index) и контура; readback «остальные не изменились»; проверка охвата | живой pre OK на `3a4f08ab`; тесты #416 |
| `deploy-prod` режим потока M2–M5 | только Job'ы потока; до cutover их нет — шаг предупреждает | статический тест: режим ↔ шаги; рантайм: снимок до/после, только Job'ы режима, каждый на digest | `test_each_mode_touches_exactly_its_jobs`; негативные тесты (Heldi, Ozon, shadow, пропуск, создание/удаление, args/SA) |
| `deploy-shadow` (merge `cloud/**`) | `wb-stocks-shadow`, `unitka-engine-shadow`; после потоков — их shadow-Job'ы | prod не трогает; образ по digest | `deploy-shadow.yml` |
| `infra.yml apply` | только targets | `expected_changes` + тот же сохранённый план (`tf_plan_guard.py`) | п. A.5 |
| не нацеленный apply | **запрещён** | на `main` — откат 10 расписаний экономии (п. E) | планы п. E |
| флаги контуров | ресурсы shadow/prod потока | `mN_{shadow,prod}_enabled` = false, пока GitHub Variable не `'true'`; сейчас ни одна не задана | `test_stream_enable_flags.py`; `gh variable list` |
| загрузчик shadow | только `__CR` | `targets.ts` до любой записи: контур ↔ таблицы, иначе `*_TARGET_MISMATCH` | тесты потоков |
| токен WB | загрузчик | срок до первого запроса (`*_TOKEN_EXPIRED`); 401/403 → `*_AUTH`, без повторов; значения токена нет в тексте и логе | `wbToken.test.ts`, тесты потоков |
| DRY_RUN | загрузчик | ровно `=== '1'`, как `cli.ts`; prodOnly пропускаются, без lease и `LOADER_RUNS` | тесты M3/M4 |
| Apps Script | триггеры | снятие — вручную, в день cutover потока, readback страницы «Триггеры»; `wbAdsBqCreateViews()` — никогда | ранбуки потоков |
| Heldi | — | не затрагивается: ни один Job Heldi не входит ни в один режим `only_job` | `deploy_scope_check.py` |
| веб-Юнитка | — | миграция её не блокирует: отдельный контур, не в режимах выката | — |

## E. Сохранение расписаний экономии #414

### E.1 Три дерева против живого GCP и того же state (10.11, `terraform plan -lock=false`, только чтение)

| Дерево | Полный plan |
|---|---|
| `main` `a3065abc` | 3 add / **24 change** / 0 destroy |
| #414 `a21b5142` | 3 add / **20 change** |
| #414 + #424 | 3 add / 16 change |
| #416 + #414 + #424 | 6 add / 16 change: +3 IAM M1 и `args` у `wb_stocks_prod` |

### E.2 Все 24 изменения `main`, по атрибутам

**A. Откат экономии 10.11.** Live = экономия, в коде `main` — прежние значения.

| Ресурс | live → код `main` | Чинит |
|---|---|---|
| `ct_refresh_prod` | `40 9` → `40 7,9,12,16,19` | #414 |
| `exec_v2_layer_build` | `45 12` → `10 7-23` (+ `retry_count`) | #414 |
| `sku_v2_layer_build` | `30 12` → `20 7-23` (+ `retry_count`) | #414 |
| `unitka_cogs_publication` | `50 9` → `50 7-23` (+ `retry_count`) | #414 |
| `wb_mart_prod` (Scheduler) | `0 9,12` → `0 7,9,12,16` | #414 |
| `wb_prices_prod` | `0 */4` → `*/20` | #414 |
| `ops_health_prod` | `paused: true → false` | #414 |
| `dro_health_eval` | `0 4,10,16,22` → `*/30` | **#424** (в #414 нет) |
| политика `dro_detector_watchdog[0]` | `25200s`, «7 часов» → `5400s`, «90 минут» | **#424** |

**B. Старый дрейф, не экономия.**

| Ресурс | live → код | Решение |
|---|---|---|
| `wb_comms_poll` (`evetis-wb-poll`) | `0 8-23` (live с 09.10) → `0 8,11,14,17,20` | **#424**: код = live. Если целевое — 5 раз в день, нужна отдельная правка live |

**C. Косметика, поведение не меняется.**

| Ресурсы | Изменение | Почему не дрейф поведения | Решение |
|---|---|---|---|
| 12 Scheduler: `ozon-runtime-promo`, `ozon_store_pnl_prod`, `promo_econ_snapshot` ×2, `unitka_engine_prod` ×2, `unitka_engine_shadow` ×2, `unitka_store_pnl_prod`, `wb_funnel_prod`, `wb_paid_storage_prod`, `wb_promo_prod`; и ещё 3 из группы A | `retry_count: null → 0` | API не возвращает нулевое значение по умолчанию, поэтому plan показывает это изменение при каждом запуске | оставить |
| `wb_mart_prod` (Job), `wb_stocks_prod` (Job) | `client`/`client_version: "gcloud" → null` — метка инструмента последней записи | не конфигурация | `ignore_changes`: #424 и #416 |

Сверх 24 обновлений — **3 create** политик Юнитки (`unitka_owner_attention`, `unitka_publication_blocked`,
`unitka_writer_config`) с `enabled = false`. Это решение 28.10 «минимальное включение», писем не будет.

### E.3 План сохранения
1. **До слияния #414 любой apply — только targeted с `expected_changes`.** Не нацеленный apply из `main` вернёт 10
   прежних значений (A).
2. Слить #424 в ветку #414, затем #414 в `main`, со своим ACK. Это код-только: `infra.yml` вручную, но `cloud/test`
   изменён, поэтому `deploy-shadow` пересоберёт образ. **Слить до merge #416 или после продвижения D′**, не в окне
   заморозки.
3. После слияния полный plan `main` = 16 косметических + 3 выключенные политики, откатов 0. Проверка — повторный
   read-only plan, все `update` только `retry_count`.
4. Targeted plan M1 от слияния #414 не зависит: 3 + 1 в обоих деревьях (п. A.5).

## F. Поставки: восстановление токена (делает владелец) и проверка полноты

**Сейчас:** `WB_TOKEN_SUPPLIES` в Script Properties истёк 31.10 (Apps Script: 401 «token is expired»). В Secret
Manager такого секрета нет. В `RAW_WB_SUPPLIES`:
- 136 поставок;
- последняя создана **27.10**;
- последний снимок **29.10 23:29Z**;
- незавершённых 26 (из них после 15.09 — `41788803` от 27.10 и `41543253` от 05.10).

**Последовательность.** Токен никто, кроме владельца, не видит и не вводит. Загрузки и догона нет до «готово» от
владельца.
1. **Кабинет WB** → Профиль → Настройки → Доступ к API → «Создать токен». Категория **«Поставки»**, только чтение.
   Срок — максимальный. Скопировать один раз.
2. **Apps Script** (восстанавливает текущую загрузку): редактор `evetis-wb-analitics` → Настройки проекта →
   Свойства скрипта → `WB_TOKEN_SUPPLIES` → «Изменить» → вставить → «Сохранить».
3. **Secret Manager** (нужен M5): Console → Security → Secret Manager → «Create secret», имя **`WB_TOKEN_SUPPLIES`**,
   значение — тот же токен. Создавать от своей учётной записи. Доступ сервисным аккаунтам даст Terraform M5.
4. **Сообщить «готово».** Только после этого, по разрешению владельца:
   - один запуск `runWbSuppliesDaily` из редактора Apps Script;
   - проверка журнала выполнений — «Выполнено», без 401.

**Проверка полноты после восстановления** (только SELECT):
- **(а)** Последний снимок `RAW_WB_SUPPLIES` — сегодня; ошибок 401 в журнале нет.
- **(б)** Поставки, созданные после 27.10, есть в RAW: сверка со списком кабинета WB за 28.10–сегодня (по номерам).
  Сверяет владелец, это единственный внешний источник.
- **(в)** Незавершённые на 29.10 поставки перечитаны: у каждой свежий `snapshot_ts` и актуальный `status_id`, у
  принятых `is_completed = true` и `fact_dt`.
- **(г)** У каждой новой и перечитанной поставки есть строки `RAW_WB_SUPPLIES_GOODS` по тому же `run_id`/`entity_key`.
  Header без goods = FAIL.
- **(д)** `V_WB_SUPPLIES_DETAIL` (только принятые, status 5) показывает поставки, принятые после 14.10.
- **(е)** Если (б) или (в) не сходятся — STOP и разбор; повторного догона без разрешения нет.

**M5 shadow** строится только после п. 3: targeted plan (только create), затем ACK. До этого plan M5 намеренно
падает на отсутствии секрета.

