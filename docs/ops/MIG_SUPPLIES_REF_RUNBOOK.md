# M5 — перенос поставок WB и REF Sync из Apps Script в Cloud Run

Статус: **КОД И TERRAFORM В DRAFT PR, НИЧЕГО НЕ ПРИМЕНЕНО.** Каждый шаг с эффектом на
production — по отдельному OWNER ACK, Terraform — только после просмотра `plan`.
Общий план: `docs/ops/WB_APPS_SCRIPT_TO_CLOUD_RUN_PLAN_2026-11-10.md` (PR #415), этап M5.

| Поток | Apps Script | Cloud Run | Таблицы |
|---|---|---|---|
| Поставки | `runWbSuppliesDaily`, 02:29 МСК | loader `supplies`, Job `wb-supplies-{shadow,prod}` | `RAW_WB_SUPPLIES`, `RAW_WB_SUPPLIES_GOODS` |
| Справочник | `runRefSync`, ~08:22 МСК | loader `ref-sync`, Job `ref-sync-{shadow,prod}` | `REF_SKU_MASTER_DATA`, `REF_ACTIVE_VERSION`, `REF_SYNC_RUNS`, `REF_SYNC_TABLE_LOG` |

## 1. Диагноз: почему `runWbSuppliesDaily` падает в 100 % запусков

**Вывод (на основании данных, подтверждён косвенно):** с 31.10.2026 прогон умирает на первом
шаге — чтении токена или списке поставок. Причина почти наверняка — **истёкший токен
`WB_TOKEN_SUPPLIES` в Script Properties**: он истёк вместе с остальными токенами Apps Script
в ночь на 31.10. 02.11 владелец заменил токены статистики и рекламы, а этот — нет.

Доказательства (только SELECT и метаданные, 10.11.2026):

| # | Факт | Источник |
|---|---|---|
| 1 | Последний записанный прогон — `DL_20261030_022925`; с 31.10 в `RAW_WB_SUPPLIES` и `_GOODS` нет ни одной строки, осиротевших позиций нет | `GROUP BY run_id` по обеим таблицам |
| 2 | 29.10 и 30.10 в 02:29–02:31 МСК видны задания загрузчика. Первое — `SELECT entity_key, MAX(UNIX_MILLIS(updated_dt))`, затем пары DELETE/LOAD. С 31.10 по 10.11 в 02:28–02:40 МСК **нет ни одного задания** по `RAW_WB_SUPPLIES*`, даже этого SELECT | `region-eu.INFORMATION_SCHEMA.JOBS_BY_PROJECT` |
| 3 | В коде этот SELECT стоит сразу после `wbsToken_()` и `wbsList_()`. Значит, исключение брошено на одном из них | `Wbsuppliesrawloader.gs:576–596` |
| 4 | 31.10 в 02:22 и 02:31 МСК `sales` и `orders` получили `HTTP 401 … token has invalid claims: token is expired`; `ads` упал в 05:07 | `wb_raw.INGEST_RUNS` |
| 5 | 02.11 `ads` и `orders/sales` снова COMPLETE (токены заменены), поставки — нет | `INGEST_RUNS` (первый COMPLETE `ads` после сбоя — 02.11 10:31) |
| 6 | В Script Properties лежат 8 отдельных токенов WB, в том числе `WB_TOKEN_SUPPLIES` | `UNITKA_2_0_HANDOFF_TO_CLAUDE_CODE.md`, раздел о секретах |
| 7 | В Secret Manager секрета `WB_TOKEN_SUPPLIES` нет | `gcloud secrets list` (только имена) |
| 8 | 10.11 у прогона была ещё одна причина: биллинг проекта был отключён примерно с 18:30Z 09.11 до 06:30Z 10.11, то есть и в 02:29 МСК 10.11 | PR #415 |

Что исключено:

| Гипотеза | Почему нет |
|---|---|
| Лимит 6 минут | Последние удачные прогоны шли около 1,5 минут: задания с 02:29:25 по 02:31:11. Бюджет в коде — 240 с. К тому же упор в бюджет даёт `PARTIAL` без исключения |
| Усечение списка на 1000 | За всю историю 136 объектов |
| Схема и типы | При ошибке загрузки в `JOBS` осталось бы задание LOAD с `error_result`. С 31.10 нет ни одного задания |
| Курсор | Суточный прогон курсор не использует, курсор есть только у бэкфилла |

**Не проверено:**
- Текст исключения. Он есть только в журнале выполнений Apps Script, а запускать и читать
  Apps Script запрещено.
- Отличить «токен истёк» (401) от «свойство удалено» (`Нет токена: WB_TOKEN_SUPPLIES`) или
  «токен без категории Поставки» (403) по данным BigQuery нельзя.

Владелец проверяет так: редактор Apps Script → «Выполнения» → `runWbSuppliesDaily` → текст ошибки.

**Последствие:** поставки, созданные и принятые после 30.10, в RAW не попали. Приёмки после
30.10 не видны и в `V_WB_SUPPLIES_DETAIL`. Последний `create_dt` в RAW — 27.10.

### Как загрузчик Cloud Run закрывает эту причину

1. **Один носитель токена — Secret Manager с версиями.** В Script Properties версий нет. Секрет
   `WB_TOKEN_SUPPLIES` создаёт владелец (§5), и Terraform выдаёт доступ к нему отдельно
   каждому SA.
2. **Срок проверяется до обращения к WB.** Срок берётся из claim `exp` токена (JWT); значение
   токена не логируется.
   - Токен истёк → `WB_SUPPLIES_TOKEN_EXPIRED`.
   - До истечения меньше 14 суток → предупреждение `WB_TOKEN_EXPIRING_SOON` с числом дней.
3. **Отказ авторизации получает свой код.** 401/403 → `WB_SUPPLIES_AUTH`. Это детерминированный
   отказ, повторов нет, Job завершается кодом 1. Запись `loader_failed` с кодом ловит алерт.
   В `LOADER_RUNS` пишется ERROR.
   - В Apps Script исключение было видно только в «Выполнениях»: журнала прогона у потока
     не было вовсе.
4. **Тихого PARTIAL нет.** Упор в бюджет даёт ERROR `SUPPLIES_PARTIAL`, а уже собранные объекты
   при этом записаны.

## 2. Семантика (эталон — Apps Script)

### 2.1 Поставки (`cloud/src/loaders/supplies/*`)

| Аспект | Apps Script | Cloud Run |
|---|---|---|
| API | POST `/api/v1/supplies?limit=1000&offset=0` с телом `{}`; GET `/supplies/{id}`; GET `/supplies/{id}/goods?limit=1000&offset=0` | то же (`wbApi.ts`) |
| Успех | только HTTP 200/204 | то же |
| Повторы | 3 попытки на 429/5xx/сеть, паузы 4 с и 8 с | 4 попытки, паузы 4, 8, 16 с (`wbFetch`) |
| Пауза | 900 мс после карточки и после состава | то же |
| Усечение | ≥ 1000 строк в списке или составе → отказ | то же, `WB_SUPPLIES_TRUNCATED` |
| Неразобранный состав | отказ, а не «0 позиций» | то же, `WB_SUPPLIES_SHAPE` |
| Неразобранная карточка | `card = null`, `card_read_ok = FALSE` | то же |
| Ключ | `P:<preorderID>` приоритетнее `S:<supplyID>`; 0 → NULL; объект без обоих пропускается | то же; счётчик пропусков пишется в лог `SUPPLIES_NO_KEY` |
| Watermark | `MAX(UNIX_MILLIS(updated_dt))` по `entity_key` из `RAW_WB_SUPPLIES` | то же из **целевой** шапки контура: prod — боевая, shadow — `__CR` |
| Выбор объектов | новый; `statusID ≠ 5`; `updatedDate` новее (сравниваются моменты, epoch); `factDate` в окне 14 суток МСК | то же (`selectTodo`) |
| Нормализация | `wbsHeaderRow_` / `wbsGoodsRow_`: `raw_json = {list, card}`, `row_hash` = MD5 склейки через «\|» | то же, побайтно (тест `supplies_normalize`) |
| `sku_match_status` | в RAW поставок нет; маппинг SKU делает вью | не применимо |
| run_id | `DL_yyyyMMdd_HHmmss` (МСК) | тот же формат; новый на каждую попытку обработчика |
| Запись | на объект: DELETE (run_id, entity_key) в обеих таблицах → LOAD goods → LOAD header | порциями по 40 объектов: LOAD goods → LOAD header; DML нет (§3) |
| Commit-marker | шапка пишется последней | то же, внутри порции |
| Журнал | нет (только Logger) | `LOADER_RUNS`, loader `supplies`, период — сутки МСК; лог `supplies_plan` и `supplies_written` |
| Бюджет | 240 с, затем тихий `PARTIAL` | 20 мин, затем ERROR `SUPPLIES_PARTIAL` (собранное записано) |

### 2.2 REF Sync (`cloud/src/loaders/refsync/*`)

| Аспект | Apps Script | Cloud Run |
|---|---|---|
| Источник | `getActiveSpreadsheet()` → лист `SKU_MASTER` | книга `1MLOCJEX0UR0Gj9gUcDbTBfPrtAhqQc2ctcsoaepSTiU` «Evetis аналитика 2.0», лист `SKU_MASTER`, `values.get` с UNFORMATTED_VALUE и scope readonly. Id взят из `docs/apps_script/PROJECT_SEPARATION_2026-09-16.md` — это контейнер проекта `10fr6u1Y…`; название сверено метаданными Drive |
| Колонки | 21 обязательная, иначе отказ | то же, `REF_SHEET_SHAPE` |
| Нормализация | `refStr_` / `refInt_` / `refBool_`; пустая строка = нет ни SKU, ни nm_id | то же (тест `refsync`) |
| Публикация | `REF_SYNC_RUNS` STARTED → load DATA → валидация SQL → MERGE указателя → TABLE_LOG → RUNS COMPLETE → retention 10 версий | тот же порядок, те же SQL, параметры вместо склейки |
| Fail-closed | при провале валидации указатель не трогается | то же, `REF_VALIDATION` |
| Журналы | `REF_SYNC_RUNS`, `REF_SYNC_TABLE_LOG` | те же таблицы (их читают DRO и `dashboard_layer_v1`) плюс `LOADER_RUNS` (loader `ref-sync`, часовой слот МСК) |
| run_id | `REF_yyyyMMdd_HHmmss_<8 hex>` | то же |

## 3. Отличия от Apps Script — каждое с обоснованием

1. **Поставки: нет DELETE, запись порциями.**
   - Apps Script удалял `(run_id, entity_key)` перед записью: внутри одного run_id объект мог
     писаться повторно (порционный бэкфилл с курсором).
   - Здесь run_id уникален на попытку, каждая порция пишется одним load job с детерминированным
     jobId. Повтор вызова получает 409, строк не добавляет.
   - Дубль внутри run_id невозможен конструкцией. Гарантия §7.2 проекта сохраняется без DML.
     Пропадает и гонка «поздний DELETE стирает свежий LOAD», которую Apps Script лечил
     ожиданием. Стоимость ниже: около 2 load-заданий на порцию вместо 4 заданий на объект,
     из них 2 DML по 10 МБ минимум.
   - Отказ WB на объекте k: объекты 1…k−1 собраны целиком и коммитятся, объект k остаётся на
     прежнем committed-прогоне. Это тот же исход, что в Apps Script.
2. **Бэкфилл без курсора.** Пустая `__CR` при первом shadow-прогоне означает, что все объекты
   «новые»: полный снимок за один прогон, около 140 объектов, 5–10 минут.
   `runWbSuppliesBackfill` и его Script Properties не переносятся.
3. **Бюджет 20 минут, исход — ERROR.** Лимита 6 минут больше нет. Тихий PARTIAL в Apps Script
   выглядел как успех.
4. **Отдельные коды отказов** `WB_SUPPLIES_AUTH`, `WB_SUPPLIES_TOKEN_EXPIRED`,
   `WB_SUPPLIES_TRUNCATED`, `WB_SUPPLIES_SHAPE` и предупреждение о сроке токена (§1).
5. **Дубль `entity_key` в списке.** Apps Script писал объект дважды в одном run_id, и вторая
   запись затирала первую. Здесь сразу берётся последняя запись с причиной, без двойной записи.
   Отличие видно только по логу `SUPPLIES_DUP_KEY`.
6. **И10 до записи.** Дубль штрихкода внутри объекта виден в логе `SUPPLIES_I10_BARCODE_DUP`.
   Строки пишутся, как в Apps Script, и проверку `wbSuppliesCheckInvariants` это не отменяет.
7. **REF: нет `refEnsureAll_()`.** Apps Script на каждом прогоне создавал таблицы и делал
   `CREATE OR REPLACE VIEW REF_SKU_MASTER` (DDL). Здесь таблицами владеет Terraform: runtime
   DDL не выполняет. Боевое вью уже существует с тем же телом (сверено `tables.get` 10.11).
8. **REF: сбой retention не превращает прогон в ERROR.** Retention — уборка, версия к этому
   моменту уже активна.
   - Отказ записи журнала после активации пишет в `REF_SYNC_RUNS` фактический указатель.
   - Apps Script в этом случае записал бы прежнюю версию как текущую.
9. **REF: даты в листе.** `getValues()` отдаёт дату объектом `Date`, и `String(Date)` даёт длинную
   строку. Sheets API даёт строку в формате книги. Расхождение возможно только если в одной из
   21 колонки стоит дата. По схеме таких колонок нет; паритет (§4) это проверит.
10. **REF: часовой слот вместо ScriptLock.** Ручной прогон после правки листа в другой час
    не упирается в COMPLETE суточного. Одновременные прогоны отсекает lease в `LOADER_RUNS`.
11. **Расписание поставок: 02:29 и догоняющее окно 05:29 МСК.**
    - COMPLETE уже есть → второе окно стоит одного SELECT по `LOADER_RUNS`.
    - Первая попытка упала → второе окно даёт снимок того же дня.

## 4. Порядок выпуска

Shadow → паритет 5 дней → переключение вечером. Каждый шаг — отдельный ACK.

**S0. Предусловия (владелец, §5).**
- Секрет `WB_TOKEN_SUPPLIES` с токеном категории «Поставки».
- Доступ «Читатель» к книге для двух SA REF Sync.
- **Чтобы паритет поставок был возможен вообще,** в Script Properties Apps Script нужно заменить
  `WB_TOKEN_SUPPLIES` тем же новым токеном. Иначе Apps Script так и пишет 0 строк, и QUERY 1
  даёт NO_RUN.

**S1. Terraform (plan → ACK → apply), только shadow:**

```
terraform plan \
  -target=google_bigquery_table.raw_wb_supplies_cr -target=google_bigquery_table.raw_wb_supplies_goods_cr \
  -target=google_bigquery_table_iam_member.shadow_write_supplies_cr \
  -target=google_secret_manager_secret_iam_member.shadow_access_wb_token_supplies \
  -target=google_cloud_run_v2_job.wb_supplies_shadow -target=google_cloud_run_v2_job_iam_member.scheduler_supplies_shadow_invoke \
  -target=google_cloud_scheduler_job.wb_supplies_shadow \
  -target=google_service_account.ref_sync_shadow -target='google_project_iam_member.ref_sync_jobuser["shadow"]' \
  -target=google_bigquery_table.ref_cr -target=google_bigquery_table_iam_member.ref_sync_shadow_write \
  -target='google_service_account_iam_member.ref_sync_actas_deployer["shadow"]' \
  -target='google_service_account_iam_member.ref_sync_actas_terraform["shadow"]' \
  -target=google_cloud_run_v2_job.ref_sync_shadow -target=google_cloud_run_v2_job_iam_member.scheduler_ref_sync_shadow_invoke \
  -target=google_cloud_scheduler_job.ref_sync_shadow
```

В плане не должно быть prod-ресурсов и изменений существующих объектов, только `create`.

**S2. Образ.**
- Merge в `main` → `deploy-shadow.yml` обновит образ `wb-supplies-shadow` и `ref-sync-shadow`.
  До apply шаг пропускается.
- Затем `scheduler-control` → `run-now` для каждого Job'а, проверка логов и `LOADER_RUNS`.
- После этого `resume` shadow-расписаний: 02:39/05:39 и 08:32 МСК.

**S3. Паритет 5 дней подряд.**
- Поставки: `sql/mig_supplies_parity.sql`, QUERY 1. Приёмка — 5 дней без FAIL и NO_RUN, из них
  не меньше 3 EXACT.
- Поставки, до восстановления токена Apps Script: QUERY 3. Ожидание —
  `same_header = same_goods = objects`. Вместо 5 дней QUERY 1 это не засчитывается.
- Справочник: `sql/mig_ref_parity.sql`, те же правила приёмки.
- Контрольные цифры поставок по `__CR` — запросы `wbSuppliesCheckInvariants` с подстановкой
  `__CR`, только SELECT. И1, И3, И7–И10 = 0. Число объектов `__CR` = числу в списке WB.

**S4. Переключение вечером (ACK MIG-M5-CUTOVER).**
1. prod-ресурсы через Terraform plan → apply: Job, IAM, Scheduler остаются PAUSED.
2. `deploy-prod.yml` с digest, прошедшим shadow: обновятся только `wb-supplies-prod` и `ref-sync-prod`.
   Workflow обновляет все prod-Job'ы из своего списка тем же digest. Если это нежелательно,
   выполнить только два `gcloud run jobs update` из шага M5.
3. В Apps Script снять триггеры `runWbSuppliesDaily` и `runRefSync`. Делает владелец в
   редакторе: «Триггеры».
4. `scheduler-control` → `run-now` для `wb-supplies` и `ref-sync` в prod. Проверить:
   - `LOADER_RUNS` (prod) COMPLETE;
   - новый `DL_…` в `RAW_WB_SUPPLIES`;
   - новый `REF_…` в `REF_SYNC_RUNS` и `REF_ACTIVE_VERSION`.
5. `resume` prod-расписаний.

Двойной записи в боевые таблицы нет ни на одном шаге: prod-Scheduler включается только
после снятия триггера Apps Script.

## 5. Что нужно от владельца

1. **Новый токен WB категории «Поставки»** (только чтение).
   - Создать секрет `WB_TOKEN_SUPPLIES` в Secret Manager проекта и положить токен первой версией.
     Значение в чат и в репозиторий не передавать.
   - Тот же токен положить в Script Properties `WB_TOKEN_SUPPLIES`. Это нужно для паритета и
     для того, чтобы до cutover поставки снова грузились.
2. **Доступ к книге «Evetis аналитика 2.0»** (`1MLOCJEX0UR0Gj9gUcDbTBfPrtAhqQc2ctcsoaepSTiU`).
   - Кнопка «Настройки доступа» → добавить по одному адресу с ролью **Читатель**, без уведомления:
     - `sa-ref-sync-shadow@project-fa311fc0-4d87-4781-986.iam.gserviceaccount.com`;
     - `sa-ref-sync-prod@project-fa311fc0-4d87-4781-986.iam.gserviceaccount.com`.
   - Адреса появятся после apply S1/S4. Выдавать после него.
   - ⚠️ Google Sheets не умеет давать доступ к одному листу. «Читатель» видит всю книгу.
   - Ограничения в коде: scope `spreadsheets.readonly`, читается только диапазон `SKU_MASTER`,
     контур проверяет id книги и имя листа.
   - Поэтому выбраны отдельные SA, а не `sa-loaders-prod`: тот обслуживает около 10 prod-Job'ов
     и уже Редактор книги Юнитки.
   - Если и это шире желаемого, есть вариант: отдельная книга-витрина с `IMPORTRANGE` листа
     `SKU_MASTER`. Тогда меняется `REF_SPREADSHEET_ID` и константа в `targets.ts`.
3. **Решение по паритету поставок:**
   - **вариант А** — заменить токен в Apps Script и ждать 5 дней QUERY 1;
   - **вариант Б** — принять QUERY 3 и инварианты `__CR` как достаточное доказательство.
     Apps Script с 31.10 всё равно не пишет.
4. **Решение по пропуску 31.10–cutover.** Первый prod-прогон читает watermark из
   `RAW_WB_SUPPLIES`. Всё, что изменилось после 30.10, попадёт в него само: объекты будут новыми
   или с новым `updatedDate`. Отдельный бэкфилл не нужен.
5. **ACK** на S1 (shadow apply), S2 (resume shadow) и S4 (cutover).

## 6. STOP и откат

**STOP:** `scheduler-control` → `pause` для `wb-supplies` и `ref-sync` в нужном окружении.
Идущий прогон дописывает текущую порцию; незакоммиченная порция вью не видна.

**Откат после cutover:**
1. `pause` prod-расписаний.
2. Вернуть триггеры Apps Script: `runWbSuppliesDaily` в 02:29 МСК; `refInstallDailyTrigger()`
   или ручной триггер `runRefSync`.

Данные откатывать не нужно:
- Поставки: строки Cloud Run — обычные снимки append-only слоя. Вью выбирают последний
  committed run_id, и следующий прогон Apps Script его перекроет.
- REF: следующий прогон Apps Script переключит указатель на свою версию. Срочно вернуть
  прежнюю версию можно одним MERGE `REF_ACTIVE_VERSION` на `active_version_before` из
  `REF_SYNC_RUNS` по отдельному ACK.

Shadow-таблицы `__CR` удаляются Terraform'ом отдельно, после приёмки.

## 7. Что нужно для shadow-выпуска

- **Terraform:** `infra/terraform/wb_supplies_loader.tf`, `infra/terraform/ref_sync_loader.tf`;
  targets — §4 S1. `terraform validate` (`-backend=false`): Success.
- **Секрет:** `WB_TOKEN_SUPPLIES`. Без него plan упадёт на data-источнике, это fail-closed.
- **Доступ:** «Читатель» книги для `sa-ref-sync-shadow`.
- **Образ:** любой из `main` после merge; деплой-шаги добавлены в `deploy-shadow.yml` и
  `deploy-prod.yml` с проверкой существования Job'а.
- **Расписания:** shadow — 02:39/05:39 и 08:32 МСК; prod — 02:29/05:29 и 08:22 МСК. Все
  создаются PAUSED, `scheduler-control` знает `wb-supplies` и `ref-sync`.
- **Алерты:** запись `loader_failed` с непустым `code` пишется, как у остальных загрузчиков.
  Привязка к политике алертов — отдельно, в M0 плана.
