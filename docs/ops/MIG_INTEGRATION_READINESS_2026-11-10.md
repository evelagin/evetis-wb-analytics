# Integration readiness: перенос загрузки WB в Cloud Run (10.11.2026)

Статус: **ОТЧЁТ, ничего не применено.** До отдельного OWNER ACK нет merge, Terraform apply, изменений Scheduler,
Cloud Run production или Apps Script. Все проверки ниже — только чтение: SELECT, метаданные, `terraform plan -lock=false`,
журнал выполнений Apps Script.

PR в составе: #416 (M1 остатки), #421 (потребители журнала), #420 (M2 заказы/продажи/сверка), #419 (M3 реклама),
#418 (M4 финансы), #417 (M5 поставки/справочник). План верхнего уровня — `WB_APPS_SCRIPT_TO_CLOUD_RUN_PLAN_2026-11-10.md`.

## 1. Восстановление поставок и токены

### 1.1 Ошибка Apps Script с 31.10 — подтверждена
Журнал выполнений проекта `evetis-wb-analitics`, фильтр «Сбой выполнения», запуск `runWbSuppliesDaily` 09.11 02:29 МСК:
- `HTTP 401 post https://supplies-api.wildberries.ru/api/v1/supplies?limit=1000&offset=0`;
- текст ответа: `invalid API access token: token has invalid claims: token is expired`;
- стек: `runWbSuppliesDaily:582 → wbsList_:273 → wbsFetch_:93`.

Запуски 04–10.11 — все «Сбой выполнения» за 2–6 с. Это совпадает с отсутствием заданий BigQuery у загрузчика с 31.10:
он падает на первом запросе к WB.

**Вывод: истёк `WB_TOKEN_SUPPLIES` в Script Properties.** Последняя поставка в RAW создана 27.10; поставок,
созданных или принятых после 30.10, в BigQuery нет.

### 1.2 Инструкция владельцу: новый токен «Поставки»
1. **Выпустить токен.** Кабинет продавца WB → Профиль → Настройки → Доступ к API → Создать токен. Категория
   **«Поставки»**, только чтение — запись загрузчику не нужна. Скопировать токен один раз.
2. **Apps Script** (восстанавливает загрузку сейчас): редактор проекта `evetis-wb-analitics` → Настройки проекта
   (шестерёнка) → Свойства скрипта → `WB_TOKEN_SUPPLIES` → «Изменить» → вставить → Сохранить. Значение больше нигде не вводить.
3. **Secret Manager** (нужен Cloud Run M5): Console → Security → Secret Manager → Create secret, имя
   **`WB_TOKEN_SUPPLIES`**, регион автоматический, значение — тот же токен. Создавать от своей учётной записи.
   Сервисные аккаунты получат доступ через Terraform (M5).
4. **Сообщить мне «готово».** Я запущу `runWbSuppliesDaily` из редактора и проверю по журналу выполнений и BigQuery
   (только SELECT), что поставки с 27.10 загрузились. Окно перечитки — 14 суток по `factDate`; поставки, незавершённые
   на 30.10, перечитаются по правилу «в RAW не завершён» (M5).

Токен нигде не печатается, не передаётся в чат и не коммитится.

### 1.3 Сроки и права остальных токенов в Secret Manager
Чтение значения секрета, даже с разбором только полей `exp` и `s` в памяти, заблокировано правилом безопасности
сессии («Credential Materialization»). Обхода не было. Что известно из метаданных и журналов:

| Секрет | Последняя версия | Факт | Вывод |
|---|---|---|---|
| `WB_TOKEN_STATISTICS` | v1, 28.09 | копия токена Apps Script, который истёк 30.10 23:22Z (401 у orders/sales); 02.11 в Script Properties заменён, в Secret Manager — нет | **почти наверняка истёк**; до M2-SHADOW положить действующий токен новой версией |
| `WB_TOKEN_ADS` | v1, 26.07 | Cloud Run его пока не использует; Apps Script берёт `WB_TOKEN_PROMOTION` из Script Properties | не доказан; проверить в кабинете |
| `WB_TOKEN_FINANCE` | v1, 26.07 | Cloud Run пока не использует | не доказан; проверить в кабинете |
| `WB_TOKEN_SUPPLIES` | нет | — | создать (п. 1.2) |

**Как проверить без раскрытия.** В кабинете WB → Доступ к API у каждого токена видны категории и дата окончания.
Владелец сверяет:
- статистика → «Статистика»;
- реклама → «Продвижение»;
- финансы → «Финансы»;
- поставки → «Поставки».

Срок у всех должен быть не ближе месяца. Безопасный автоматический контроль — после shadow-apply: загрузчик M5 сам
проверяет `exp` в рантайме и пишет `WB_TOKEN_EXPIRING_SOON` за 14 суток. Для M2–M4 такую же проверку стоит добавить
отдельной правкой (п. 6, F5).

## 2. Граф слияния

### 2.1 Пересечения
Общие файлы:
- `CHANGELOG.md` (все);
- `cloud/src/loaders/registry.ts` (M2–M5);
- `.github/workflows/deploy-shadow.yml`, `deploy-prod.yml`, `scheduler-control.yml` (M1 и M2–M5);
- `cloud/src/config.ts` (M2, M4);
- `infra/terraform/variables.tf` (M2, M4);
- `.github/workflows/infra.yml` (M3).

#421 построен на ветке #416 и содержит её коммиты.

### 2.2 Репетиция объединения
Ветка `mig/integration-2026-11-10`: `origin/main` a3065abc + #416 + #421 + #420 + #419 + #418 + #417, merge-коммитами.

Конфликты и как разрешены:
- `CHANGELOG.md` — объединение записей.
- `registry.ts`, `scheduler-control.yml` (список `options`) — аддитивное объединение.
- `config.ts`, `variables.tf` — обе стороны конфликта были незакрытыми блоками с общим `}`. Простая склейка сломала бы
  синтаксис, поэтому каждый блок закрыт явно.
- `deploy-prod.yml`, `deploy-shadow.yml` — то же самое для шагов с циклом `for … done`. Слияние выполнено пошагово:
  шаги ветки вставлены целиком, YAML проверен.

Результат объединённого дерева:
- `tsc` 0;
- `lint` 0;
- `vitest` 98 файлов, 2 295 passed / 19 skipped;
- полный `pytest tools/tests` — см. §2.5.

### 2.3 Дефекты, видимые только в объединении (исправить до merge)
| # | Что | Где | Исправление |
|---|---|---|---|
| F1 | Шаги M3 и M5 в `deploy-prod.yml` не знают входа `only_job` из #416: выкат «только `wb-stocks-prod`» обновил бы и `wb-ads-prod`/`wb-supplies-prod`/`ref-sync-prod`, если они созданы. **Подтверждено тестом** `test_mig1b0_stocks_prod` | #419, #417 | эталон в репетиции 8cb337f8: свой режим `only_job` на поток + условие шага |
| F2 | Для prod-Job M2 (`wb-orders/sales/sales-reconcile-prod`) и M4 (`wb-finance-prod`) нет шага в `deploy-prod.yml` — на cutover образ пришлось бы ставить вручную | #420, #418 | эталон в репетиции 8cb337f8 |
| F3 | `DRY_RUN` в M3 и M4 разбирался с `trim()`, а в `cli.ts` — точно: при `' 1'` cli писал бы COMPLETE без записи данных (ложный зелёный гейт) | #419, #418 | **исправлено**: 5e81aa7c, 5aef2425 (тесты 73/73, 95/95) |
| F4 | Разный способ включения флагов: M3 — переменная репозитория через `TF_VAR` в `infra.yml`; M2/M4/M5 — смена default отдельным PR. У M2–M4 shadow-ресурсы без флага | все | единый способ: `TF_VAR_<поток>_{shadow,prod}_enabled` из переменных репозитория в `infra.yml`, флаг shadow у всех |
| F5 | Проверка `exp` токена в рантайме есть только у M5 | #420, #419, #418 | общий помощник из M5 для всех потоков |

F1, F2 и F4 трогают файлы из #416, поэтому вносятся при обновлении каждой ветки от main после слияния #416 (§2.4).

### 2.4 Порядок слияния и защита от потерь
Force-push запрещён, поэтому ветки обновляются **merge-коммитом main в ветку PR**. Каждый шаг ниже — только по ACK.

1. **#416 (M1).** Merge с `--match-head-commit`. `deploy-shadow` соберёт образ **D′** и выкатит его в `wb-stocks-shadow`
   и `unitka-engine-shadow`; шаги новых Job пропускаются, Job'ов ещё нет.
   **Заморозка `cloud/**` в main** до продвижения D′ в `wb-stocks-prod` (п. 3): любой следующий merge в `cloud/**`
   пересобрал бы shadow-образ, и квалификация D′ потеряла бы смысл.
2. **#421.** Сменить base на `main`; коммиты #416 уже в main. Повторный CI, merge. `cloud/**` не затронут —
   `deploy-shadow` не запускается.
3. **#420 → #419 → #418 → #417**, по одному. Для каждого:
   - `git merge origin/main` в ветку PR, разрешение по образцу репетиции, F1/F2/F4;
   - push без force;
   - **полный CI на новом head**;
   - merge с `--match-head-commit`.
4. **Контроль отсутствия потерь.** После каждого merge `git diff <ожидаемое дерево> main` по затронутым файлам. Ожидаемое
   дерево — репетиция плюс F1/F2/F4. Расхождение = STOP.

Draft PR репетиции (не для merge) запускает GitHub CI на всём объединённом дереве до любого реального слияния.

### 2.5 Полный pytest объединённого дерева
`pytest tools/tests` на объединённом дереве (до F1/F2): **5 853 passed, 13 failed**, 25 мин.
- **1 падение по делу** — `test_mig1b0_stocks_prod::test_default_mode_steps_unchanged_and_guarded`. Это и есть F1:
  тест поймал шаги M3/M5 без условия `only_job`.
- **12 падений вне миграции** — `test_cloud_controller` и `test_tenancy_deploy` (Heldi). Локальный venv — Python 3.14,
  контроллер требует 3.12 (`production Python 3.12 required`). На Python 3.12 те же тесты на объединённом дереве —
  **70/70 passed**; на чистом main с 3.14 `test_cloud_controller` падает так же.

**F1/F2 реализованы эталоном в репетиции** (8cb337f8):
- `only_job` получил режимы `wb-statistics`, `wb-ads`, `wb-finance`, `wb-supplies-ref`;
- шаги M3/M5 — `if: inputs.only_job == 'all' || inputs.only_job == '<поток>'`;
- для M2 и M4 добавлены шаги выката.

Тест переписан по смыслу: для каждого режима вычисляется множество обновляемых Job.
- `wb-stocks-prod` — ровно `{wb-stocks-prod}`, поток — ровно свои Job.
- Мутация «снять условие у шага рекламы» ловится: режим остатков задел бы 3 Job рекламы.

После правки `test_mig1b0_stocks_prod.py` — 19/19. Эти же изменения переносятся в ветки PR при обновлении от main (§2.4).

GitHub CI на всём объединённом дереве — Draft PR #422 (не для merge).

## 3. M1 — остатки (production)

### 3.1 Паритет на реальных данных — подтверждён
`sql/mig1c_stocks_t5_parity.sql` QUERY 1, только SELECT, окно 12 дней. 05–09.11 — пять дней подряд:
- T6 matched 22/22, `prod_only = cr_only = qty_diff = attr_diff = 0`;
- T5 matched 212–215 из 212–215, `qty_diff = attr_diff = 0`, односторонних ключей 0, дублей 0;
- разрыв снимков 7–8 мин.

Остальные дни: 02.11 нет shadow (токен), 04.11 нет снимка Apps Script (shadow есть), 10.11 нет shadow (биллинг).

**Ограничение:** паритет доказан для образа текущего shadow (`fd6e6cf1`), а не для D′. D′ квалифицируется заново (п. 3.3).

### 3.2 Merge preflight (до merge #416)
- **Head и CI:** head = утверждённый, CI 11/11 SUCCESS, `mergeStateStatus` CLEAN; main не сдвинулся.
- **Окно:**
  - нет активных исполнений `wb-stocks-shadow`/`unitka-engine-shadow`;
  - не 06:20–06:45 МСК (снимки остатков);
  - не 06:50–07:20Z и 09:20–09:45Z (Engine).
- **Заморозка `cloud/**` объявлена.**

### 3.3 Квалификация D′ (без использования результатов старого образа)
1. Из summary `deploy-shadow` записать пару (D′, `source_git_sha` = merge-коммит #416).
   Readback: у `wb-stocks-shadow` образ = D′.
2. Дождаться **одного штатного** запуска `wb-stocks-shadow` (06:30 МСК). Ручного запуска нет: суточный `snapshot_id`
   и guard.
3. Запуск чистый, если:
   - COMPLETE, `control_status = OK`, `availability_status = OK`;
   - `t5_rows_written > 0`, дублей 0;
   - QUERY 1 за этот день — EXACT или объяснённый DATA_DRIFT (QUERY 3).

   Иначе STOP, продвижения нет.

### 3.4 Targeted Terraform plan (проверено локально 10.11 на объединённом дереве)
Цели:
- `google_cloud_run_v2_job.wb_stocks_prod`;
- `google_bigquery_table_iam_member.prod_write_raw_stocks`, `.prod_write_raw_stocks_t5`, `.prod_write_stocks_snapshots`.

Ожидание: **3 create, 1 update, 0 destroy**. У Job поатрибутно меняется только `args[0]`: `noop` → `stocks`.
`infra.yml` с `expected_changes` под `tools/tf_plan_guard.py`.

⚠️ **Не нацеленный apply запрещён.** Полный план объединённого дерева сегодня показывает 24 изменения существующих
ресурсов, это откат расписаний 10.11 из ещё не слитого PR #414:
- Scheduler: `schedule`, `retry_config`, `paused`;
- поля клиента `wb_mart_prod`;
- условие DRO watchdog.

### 3.5 Выкат только `wb-stocks-prod`
`deploy-prod.yml`: `only_job=wb-stocks-prod`, `image_digest` = **D′**, `source_git_sha` = merge #416.

Pre-check workflow проверяет:
- digest = образ `wb-stocks-shadow`;
- происхождение: sha256 манифеста → конфиг → метка `git-sha`;
- контур prod.

Остальные шаги пропускаются (`if: inputs.only_job == 'all'`). После F1 — и шаги M3/M5.

### 3.6 Readback
- `wb-stocks-prod`: образ D′, `args=["stocks"]`, боевые `STOCKS_*`, `STOCKS_T5_MODE=on`, SA `sa-loaders-prod`.
- IAM на 3 боевые таблицы.
- Scheduler `wb-stocks-prod` в GCP — **PAUSED**.
- Остальные `*-prod` Job — образ и generation не изменились.
- DRO-1 SQL из #416 применён по порядку `DRO1_DETECTION_ALERTING_2026-10-04.md`, ручной `CALL` детектора зелёный.

### 3.7 Переключение без двойной записи
1. **День X, до вечера** — ручного prod-запуска нет: снимок X уже записал Apps Script.
2. **Вечер X (18:00 – 05:30 МСК X+1)** — в редакторе Apps Script `wbStocksRemoveTrigger()`.
   Readback: на странице «Триггеры» нет `runWbStocksSnapshot`, остальные триггеры не тронуты.
   Опционально read-only `wbFinanceBackfillAuditTriggers()`.
3. **Только после этого** — `scheduler-control.yml` resume `wb-stocks-prod`. Readback: ENABLED, `30 6 * * *`, Europe/Moscow.

### 3.8 Первые сутки production и STOP
- **X+1 06:30** — проверки:
  - ровно один COMPLETE за X+1, Apps Script за X+1 нет;
  - `control_status = OK`, `t5_rows_written > 0`;
  - `V_WB_STOCKS_*_CURRENT` непусты;
  - FACT за день есть;
  - Control Tower и DRO-1 зелёные.
- **STOP X+1…X+3:**
  - нет COMPLETE после фактического завершения, ERROR или `STOCKS_TARGET_MISMATCH`;
  - `control_status` ≠ OK, `t5_rows_written = 0`;
  - дубли, пустые CURRENT-вью;
  - в prod-RAW `__CR` или чужой `source_api`;
  - любой снимок Apps Script после X.
- **Откат** (по решению владельца, данные не удаляются):
  1. pause `wb-stocks-prod`;
  2. `wbStocksInstallDailyTrigger()`;
  3. если валидного снимка за день нет — однократно `runWbStocksSnapshot()`.

  Слепого повтора prod-запуска нет.

## 4. M2–M5 — тестовая инфраструктура (shadow)

### 4.1 Точный план (локально, `-lock=false`, только чтение, объединённое дерево)
Не нацеленный план (все prod-флаги false, M5-shadow false): **86 create / 24 update / 0 destroy**.
- 24 update — дрейф из п. 3.4, к миграции не относится.
- 6 create не shadow: 3 IAM M1 и 3 алерт-политики Unitka, которые есть в main, но не применены.

**Нацеленный shadow-план по 80 адресам M2–M4: `80 to add, 0 to change, 0 to destroy`, нарушений 0.**
- Все Scheduler `paused = true`.
- Все Job на `cloudrun/container/hello` — образ поставит `deploy-shadow` после merge.
- У каждого потока свой SA:

| Поток | SA | Таблицы `__CR` | Scheduler (МСК, на паузе) |
|---|---|---|---|
| M2 | `sa-wb-statistics-shadow` | `RAW_WB_ORDERS__CR`, `RAW_WB_SALES_RETURNS__CR` | orders `8 1,7,13,19`; sales `52 0,6,12,18`; reconcile `50 4` |
| M3 | `sa-wb-ads-shadow` | 9 × `RAW_WB_ADV_*__CR` (campaigns, campaign_stats, booster_stats, costs, costs_runs, query_bids(+runs), query_stats(+runs)) | ads `40 5,7`; ads-query `5 6,8`; audit `30 4 * * 0` |
| M4 | `sa-wb-finance-shadow` | `RAW_WB_FINANCE__CR`, `FINANCE_LOADER_RUNS__CR`, `FINANCE_REPORT_LOADS__CR`, `FINANCE_WEEK_STATUS__CR`, `FINANCE_WEEK_RECON__CR` | `5 8,13,19` |
| M5 | `sa-wb-supplies-shadow`, `sa-ref-sync-shadow` | `RAW_WB_SUPPLIES__CR`, `RAW_WB_SUPPLIES_GOODS__CR`, `REF_*__CR` | supplies `39 2,5`; ref `32 8` (из runbook M5) |

Права каждого SA:
- `bigquery.jobUser`;
- `dataEditor` на `LOADER_RUNS` и свои `__CR`;
- чтение нужных источников: M2 — `REF_SKU_MASTER`, `REF_SKU_MASTER_DATA`, `REF_ACTIVE_VERSION`;
  M3 — 6 объектов `V_ADV_*`/`RAW_WB_ADV_*`;
- `secretAccessor` только на свой токен;
- actAs для deployer и terraform-apply.

`sa-loaders-*`, существующие Job, Scheduler, IAM и боевые таблицы shadow-план **не меняет**.

**M5 не входит в план сегодня:** plan падает на отсутствии секрета `WB_TOKEN_SUPPLIES` — так задумано, fail-closed.
Его shadow-план строится после п. 1.2.

### 4.2 Начальный watermark M2
**Prod на cutover.** В `RAW_WB_ORDERS` есть `ORD_INC` (3 800 строк, max `last_change_date` 10.11 09:33) и ручной `ORD_API`
(max 28.09). В `RAW_WB_SALES_RETURNS`:
- `SALE_INC` (2 002, max 10.11 07:06);
- `SALE_API` (max 09.08);
- `SALE_RECON` (max 15.08).

Watermark = MAX по инкрементальным строкам — то же значение, что Script Property Apps Script. Ручной импорт и сверка
его не сдвигают (тесты M2).

**Сценарий «ручной импорт есть, инкрементальных строк нет».** Возможен только в `__CR`, если туда вручную импортировать.
Поведение:
- заказы — watermark = MAX по API-строкам без сверки, как `wb*IncrementalBootstrap` Apps Script;
- только строки сверки → ERROR.

Shadow стартует с пустой `__CR` и переменными `statistics_shadow_*_bootstrap_watermark`. Их значение берётся равным
MAX инкрементальных строк боевой таблицы на момент старта; пусто → ERROR, а не загрузка с начала времён.

### 4.3 По потокам: доказательства, паритет, полнота, запрет двойной записи, STOP
**Реальный прогон загрузчика** (не unit-тесты) возможен только после shadow-apply. Это первый критерий приёмки:
`scheduler-control` → `run-now` каждого shadow-Job, затем COMPLETE в `LOADER_RUNS` (`environment='shadow'`) и строки в `__CR`.
До apply эквивалентность доказана replay-тестами, которые исполняют боевой `.gs` в `node:vm`
(M2 — 21 сценарий, M3 — 5 сценариев × 9 таблиц), и самосверкой SQL паритета prod↔prod.

| Поток | Построчный паритет (SQL) | Полнота и свежесть | Одновременная запись | STOP |
|---|---|---|---|---|
| M2 | `sql/mig_m2_statistics_parity.sql`: по дням и `srid`/`sale_id` + `row_hash`; 5 дней без FAIL, ≥3 EXACT по каждому потоку | COMPLETE в `LOADER_RUNS` за D−1 до 09:00 МСК; MAX `last_change_date` `__CR` ≥ Apps Script − 6 ч | shadow пишет только `__CR`; минуты смещены от триггеров Apps Script (лимит statistics-api на продавца) | FAIL паритета; ERROR 2 слота подряд; 401/403; запись вне `__CR` |
| M3 | `sql/mig_ads_parity.sql` QUERY 1–3: по дням и таблицам; costs EXACT все 5 дней, ≥3 дня EXACT целиком | `ads` COMPLETE за D−1 до 07:00; хвост `ads-query` отдельно | окна 05:40/07:40 против 05:00/06:15/08:15 Apps Script; `V_ADV_*` не меняется | FAIL или MISSING; `ADS_PARTIAL` 2 дня; любая попытка DDL |
| M4 | `sql/mig_finance_parity.sql`: сутки и недели EXACT; понедельник WEEKLY обязательно в окне | `FINANCE_LOADER_RUNS__CR` OK/OK_NO_NEW в 3 слотах; недели финализированы | слоты :05 против :26/:24/:36 Apps Script; lease + mutex с backfill | FAIL; KEY_CONFLICT/ORPHANED; дубль `rrd_id` в каноне `__CR` |
| M5 | `sql/mig_supplies_parity.sql` QUERY 1–4, `sql/mig_ref_parity.sql`: по `entity_key`, `is_completed`, `status_id`; REF — версия = строки листа | одна COMPLETE за сутки; REF активная версия обновлена | 02:39 против 02:29 Apps Script; REF пишет только `REF_*__CR` | FAIL; `WB_SUPPLIES_AUTH`; необъяснённый QUERY 4 |

Сверх этого во всех потоках:
- `loaded_at` в формате `'%Y-%m-%d %H:%M:%S'` МСК — его разбирает `TVF_PIPELINE_PROBE`;
- `run_id` совместим с префиксами, которые разбирают детекторы.

Это проверяет сверка в первый же день shadow.

## 5. Общее здоровье

- **#421 до переключения безопасен.** В `LOADER_RUNS` (prod) строк `orders`/`sales`/`ads` = 0 (SELECT 10.11). Поэтому
  `TVF_PIPELINE_PROBE`, `V_DATA_FRESHNESS` и `sp_evaluate_health_ext` дают на тех же данных тот же результат.
  Живые тела трёх объектов до правки совпадают с Git. `bq --dry_run` 4/4 OK. Shadow в `V_INGEST_HEARTBEAT` не попадает:
  `environment='prod'`.
- **LATEST-ATTEMPT.**
  - Гейт витрины (`mart/bq.ts`) берёт последнюю попытку по `started_at DESC, run_id DESC`, начатую после полуночи МСК
    целевого D+1.
  - `TVF_PIPELINE_PROBE`: `last_run` — последняя по `started_at`; успешные и сбойные периоды через `DISTINCT`. Поэтому
    одновременное существование строк `INGEST_RUNS` и `LOADER_RUNS` за один период **не задваивает** период.
  - `V_DATA_FRESHNESS` — последняя попытка по времени.
- **Риск** один: если триггер Apps Script не снят, его поздняя ERROR станет последней попыткой и закроет гейт.
  - Защита — порядок переключения: снять триггер → readback страницы «Триггеры» → только потом resume Scheduler.
  - STOP: строка `INGEST_RUNS` потока с `started_at` позже переключения.
- **Повисшие STARTED.** Если Cloud Run убит по таймауту, строка остаётся STARTED: `RUNNING` в пробе и `STUCK` в
  `V_DATA_FRESHNESS`, пока не придёт следующая попытка. Гейт следующую попытку видит.
  Таймауты Job выставлены выше бюджетов (M2 900/3600 с, M3 2400 с при дедлайне 1800 с, M4 1800 с при бюджете ≤ 25 мин
  с проверкой). Reaper — отдельная задача.
- **Детекторы Step 5A** (`sql/ops/step5a_detectors.sql`) — разовый read-only скрипт, не объект production. Правка его
  порогов под Cloud Run вынесена в M3/M6.

## 6. Итог: два независимых решения

### A. OWNER ACK MIG1B — production-выпуск остатков
**Разрешает:**
1. Merge **только #416** (head 264d327e или новее, если изменится, с `--match-head-commit`) и объявленную заморозку
   `cloud/**` до п. 4.
2. Автоматический `deploy-shadow` этого merge: только `wb-stocks-shadow` и `unitka-engine-shadow` на D′.
3. Квалификация D′ одним штатным снимком 06:30 МСК (п. 3.3). При нечистом снимке — STOP.
4. `deploy-prod.yml only_job=wb-stocks-prod` с D′ (п. 3.5).
5. `infra.yml` **targeted** plan, затем apply по 4 адресам п. 3.4 с `expected_changes` (3 create / 1 update).
   План показывается до apply.
6. DRO-1 SQL из #416 (`dro1_02_probe.sql`, `dro1_03_evaluate.sql`) по порядку DRO1-документа.
7. Вечером X — `wbStocksRemoveTrigger()` в Apps Script (через браузер владельца) с readback.
   После этого resume Scheduler `wb-stocks-prod`.
8. Контроль X+1…X+3 и откат по п. 3.8.

**Не разрешает:**
- другие PR;
- не нацеленный apply;
- ручной prod-запуск;
- изменение других Job и триггеров.

### B. OWNER ACK SHADOW M2–M5 — тестовая инфраструктура
**Разрешает:**
1. Исправления F1, F2, F4, F5 в ветках #420, #419, #418, #417 и обновление их от main merge-коммитом.
2. Merge #421, затем #420, #419, #418, #417 — по одному, каждый после полного CI на новом head.
   Только после завершения ACK A, когда D′ уже в `wb-stocks-prod`.
3. Автоматические `deploy-shadow` этих merge: shadow-Job получают общий образ.
4. `infra.yml` **targeted** plan/apply только по shadow-адресам:
   - 80 для M2–M4 по списку п. 4.1;
   - адреса M5 — после создания секрета `WB_TOKEN_SUPPLIES`.

   `expected_changes` только `create`; любое `update`/`delete` = STOP.
5. `scheduler-control`: `run-now` по разу на каждый shadow-Job (проверка реального прогона), затем `resume` shadow-Scheduler.
6. Владелец: новые версии токенов (п. 1.2–1.3) и «Читатель» книги «Evetis аналитика 2.0» для `sa-ref-sync-shadow`.
7. 5 суток паритета по п. 4.3 с ежедневным отчётом.

**Не разрешает:**
- любые prod-ресурсы (`*_prod_enabled` остаются false);
- применение DDL #421 (отдельно, до cutover M2/M3);
- снятие триггеров Apps Script;
- prod-деплой;
- cutover.

Cutover каждого потока — отдельный ACK после паритета.
