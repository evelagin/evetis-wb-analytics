# M4 · Финансовые отчёты WB: перенос из Apps Script в Cloud Run — runbook

Статус: **код, Terraform и сверка подготовлены, ничего не применено.** Draft PR, ветка `mig/m4-finance`.
Общий план — `docs/ops/WB_APPS_SCRIPT_TO_CLOUD_RUN_PLAN_2026-11-10.md` (PR #415), этап M4.

| Что | Где |
|---|---|
| Эталон (production) | `apps-script/ingestion/Wbfinancedaily.gs` (PR-Fin1 v1.3.1 + PR-A #91), тик `runWbFinanceDaily` |
| Загрузчик | `cloud/src/loaders/finance/` — loader `finance` (тик) и `finance-backfill` (регистрация WEEKLY периода) |
| Инфраструктура | `infra/terraform/wb_finance_loader.tf` |
| Сверка | `sql/mig_finance_parity.sql` (только SELECT) |
| Тесты | `cloud/test/finance_*.test.ts` |

## 1. Какие модули `.gs` финансов production

Установщики триггеров в Git есть только в двух файлах (`grep newTrigger`): `Wbfinancedaily.gs` и
`WbFinanceBackfillBQ.gs`. По `JOBS_BY_USER` на 21.10 живой поток — только `runWbFinanceDaily`
(фактически 07:26 / 12:24 / 18:36 МСК; по понедельникам приходит WEEKLY). Список живых триггеров из
репозитория не виден — подтверждает его read-only `wbFinanceBackfillAuditTriggers()` (вопрос владельцу, §8).

| Файл | Что это | Вызывается триггером | Решение |
|---|---|---|---|
| `Wbfinancedaily.gs` | контур DAILY/WEEKLY → `RAW_WB_FINANCE` + `FINANCE_*` | **да**: `runWbFinanceDaily` ×3/сут | **переносится** (тик и `wbFinDeepBackfillRegister`) |
| `WbFinanceBackfillBQ.gs` | догон v5 → RAW «мимо orphan guard» | тик `wbFinanceBackfillAutoTick` — только если взведён | **не переносится**: с 12.08 закрыт kill-switch `WB_FIN_BQ_DEPRECATED_=true`, пишущие функции бросают исключение; последняя запись legacy-слоя — 13.07. ⚠️ `bqCreateFinanceView()` kill-switch'ем не закрыт: пересоздал бы `V_WB_FINANCE` старым определением — не запускать |
| `Wbfinanceapiv1.gs` | разведка Finance API v1 и импорт одного отчёта **в лист** | нет (меню) | мёртвый для загрузки; общий с тиком пейсер `WB_FIN_API_LAST_REQ_MS` после M6 не нужен |
| `Wbfinanceloader.gs` | загрузка **в лист** через `reportDetailByPeriod` v5 + XLSX с Drive | нет (меню, `WbDailyRefresh.gs`) | мёртвый: метод v5 отключён WB 15.07.2026 |
| `Wbfinance.gs`, `Wbfinancemonthclose.gs` | листовой слой `CLEAN_WB_DAILY`, юнит-отчёт, закрытие месяца | нет (меню) | исторический слой Sheets, к загрузке не относится |
| `Wbfinanceclosestatus.gs`, `Wbfinanceapidiag.gs`, `Wbfinanceauditbq.gs`, `Wbfinancedailyprobe.gs`, `FindFinanceReturns_TEMP.gs` | read-only диагностика | нет | не переносится |
| `Wbfinanceimportfromdrive.gs` | разовый импорт XLSX мая 2026 в лист | нет | не переносится |

Из `Wbfinancedaily.gs` не переносятся: `wbFinDailyInitC0()` (одноразовый DDL 13.07: ALTER RAW, таблицы контура,
вью `V_WB_FINANCE_COMPLETE` / `V_WB_FINANCE_CANONICAL` / `V_WB_FINANCE`) — ⚠️ DDL этих трёх production-вью живёт
**только** в `.gs`, в `sql/` его нет; перенос — задача M6. `wbFinDailyStatus()` заменяют QUERY 3–4 сверки.
`wbFinInstall/RemoveDailyTriggers()` заменяет Cloud Scheduler.

## 2. Семантика (одинакова в Apps Script и Cloud Run)

- **Источник:** `POST https://finance-api.wildberries.ru/api/finance/v1/sales-reports/list` (`period` daily|weekly,
  `limit` 1000 + offset) и `…/sales-reports/detailed/{reportId}` (`{"rrdId":<курсор>,"limit":100000}`, всегда с `rrdId=0`,
  повтор = полная перезагрузка отчёта). Токен категории «Финансы» в `Authorization` без `Bearer`.
- **Лимит WB:** 1 запрос/мин на продавца на все методы → пейсер ≥ 61 с между любыми запросами.
- **Двухслойная модель:** DAILY (reportType 1/2) = PROVISIONAL, WEEKLY = FINAL. `reportType` строго ∈ {1,2}: любой
  другой тип в list отклоняет весь list (`неподдерживаемый reportType`) — модель молча не расширяется.
- **«Watermark»** — не дата, а состояние в BigQuery: манифест `FINANCE_REPORT_LOADS` (grain = report_id) и календарь
  `FINANCE_WEEK_STATUS`. Script Properties для состояния не используются (у Apps Script там были только C0-маркер и пейсер).
- **Окна discovery:** daily — сегодня−3…сегодня (МСК); weekly — cutover (2026-07-13)…сегодня, только если есть
  нефинальная неделя календаря. Календарь недель: от cutover до последней завершённой недели, типы 1 и 2.
- **Манифест:** DISCOVERED → STARTED → COMPLETE | ERROR; `attempt_count` растёт только на STARTED; ERROR повторяется до
  5 попыток; COMPLETE неизменяем (повторный list лишь сверяет 7 метрик с допуском 0,01 ₽ — расхождение = IMMUTABLE_ANOMALY,
  ERROR прогона); STARTED старше 120 мин → DISCOVERED.
- **Порядок тика:** stale-recovery → календарь недель → PK-assert манифеста → очередь (DISCOVERED + ERROR<5) → discovery
  daily → discovery weekly → PK-assert → свежеоткрытое → финализация недель + RECON → статус.
- **«Пропущено (final week)»:** daily финализированной (неделя × тип) недели не регистрируется.
- **Финализация недели:** WEEKLY COMPLETE ≥ cutover → RECON (Σ daily list-метрик против weekly, OK/WARN) →
  `weekly_final=TRUE`. После этого `V_WB_FINANCE_CANONICAL` замещает daily-строки недели weekly-строками.
- **Загрузка отчёта (fail-closed по ключу, PR-A #91):** STARTED → orphan guard (строки отчёта в RAW от чужого прогона —
  `ORPHANED_RAW_REPORT`; вне партиций периода или с NULL-партицией — `ORPHANED_RAW_OUTSIDE_MANIFEST_PERIOD`) → detailed →
  KEY_CONFLICT (пустой/дубль `rrdId`) → load-job(ы) по 10 000 строк с детерминированным jobId `fin_<report>_<run>_<batch>` →
  post-load «этого прогона» (count = distinct rrd = fetched = loaded) → post-load «всех прогонов» в партициях периода
  (`POSTLOAD_CROSSRUN_MISMATCH`) → COMPLETE только своим `processing_run_id`. Частичная попытка невидима: валидны лишь
  строки `run_id = processing_run_id` при COMPLETE (`V_WB_FINANCE_COMPLETE`). ASSERT «одна строка на rrd_id в CANONICAL»
  остаётся в витрине (`sp_bootstrap_facts`) — загрузчик его не меняет, а сохраняет инварианты, на которых он держится.
- **BigInt:** `reportId/rrdId/giId/shkId` закавычиваются ДО `JSON.parse`, число id в теле сверяется с числом объектов
  (`PARSE_MAPPING_ERROR`); курсор и путь — вербатим.
- **Нормализация и `raw_json`:** 54 поля маппера `finDailyMapRow_` 1:1 (деньги — STRING, конвенция RAW); `raw_json` — JSON
  одного detailed-объекта с BigInt-строками; `row_hash = md5('WB_API_FIN_V1|report_id|rrd_id')`; `_rr_date` = rrDate, иначе
  saleDt, иначе NULL; `report_period`, `report_type`, `run_id` = финансовый run_id; `loaded_at` — ISO UTC.
- **Проверка маппера на живых данных (10.11.2026, локально, данные не сохранялись):** 300 строк нового контура RAW за
  02–09.11 — `mapDetailedRow(JSON.parse(raw_json))` воспроизвёл все 54 поля байт-в-байт (включая `row_hash`, `_rr_date`,
  `raw_json`), прочие 20 колонок у живых строк — NULL. Самосверка `sql/mig_finance_parity.sql` (prod против prod) за 14 суток —
  все сутки EXACT, все 34 недели EXACT.
- **SKU:** `internal_sku`/`sku_match_status` при загрузке **не** ставятся (и у Apps Script тоже) — SKU разрешается в
  `V_WB_FINANCE_SEMANTIC` (nm → barcode → vendor code). Заморозки статуса при загрузке у финансов нет.
- **Схема RAW** (`wb_raw.RAW_WB_FINANCE`, 74 колонки, партиция `_rr_date` DAY, кластер `wb_nm_id, rrd_id`) и таблиц контура
  не меняется. `__CR` — зеркало (`wb_finance_loader.tf`).
- **Журнал прогона:** `FINANCE_LOADER_RUNS` — строка RUNNING при старте (load-job), MERGE при завершении: OK / OK_NO_NEW /
  PARTIAL / ERROR, `reports_discovered/loaded/errors`, `requests_made`. Статус: аномалия или (ошибки и 0 загрузок) → ERROR;
  ошибки или хвост очереди → PARTIAL; 0 загрузок и 0 новых → OK_NO_NEW; иначе OK.
- **Витрина:** guard витрины (`mart/watermark.ts`, #341) хеширует `V_WB_FINANCE_SEMANTIC` целиком, включая `load_id/loaded_at/run_id`.
  Отпечаток остаётся стабильным, потому что каждый отчёт пишется ровно один раз (COMPLETE неизменяем, повтор не дописывает).
  Новый отчёт меняет отпечаток — так и задумано (пересборка D-1). Shadow пишет только `__CR` и отпечаток не трогает.

## 3. Отличия от Apps Script — каждое с обоснованием

| # | Apps Script | Cloud Run | Почему |
|---|---|---|---|
| 1 | общий `ScriptLock` тика и догона (30 с ожидания) | lease `LOADER_RUNS` (cli), период = часовой слот МСК `YYYY-MM-DDTHH`; плюс взаимоисключение `finance` ↔ `finance-backfill` (`mutex.ts`): каждый режим после своей строки STARTED ищет свежую (< 30 мин) STARTED-строку соседа; тик при активной регистрации пропускается (`FINANCE_SKIPPED_LOCKED`, как SKIPPED_LOCKED), регистрация при активном тике — отказ `FINANCE_LOCKED` | lease ключуется по имени загрузчика, общий ключ занял бы слот тика. Схема «запись → проверка» не пропускает оба режима одновременно (может отказать обоим — fail-closed). Ручной запуск в том же часе, что слот, «съедает» слот (COMPLETE) — безвредно |
| 2 | C0-маркер в Script Properties | `assertReady()`: 5 таблиц есть, у RAW есть колонки контура (метаданные, бесплатно) | маркер живёт в Script Properties, Cloud Run его не видит; проверяется то, что маркер гарантировал |
| 3 | персистентный пейсер в Script Properties | пейсер в памяти прогона + повтор 429 с паузой `X-Ratelimit-Retry`/`Retry-After` | прогоны сериализует lease; зазор между двумя прогонами подряд и чужой клиент того же токена (Apps Script в shadow-фазе) закрывает повтор |
| 4 | ответ ≠ 200 → ERROR отчёта сразу; сетевое исключение UrlFetch роняет тик, отчёт висит STARTED до stale-recovery (120 мин) | до 2 повторов на 429/5xx/сеть, каждый через пейсер; после — ERROR отчёта (attempt засчитан) | требование брифа (ретраи 429/5xx с паузами WB); исключение сети больше не роняет весь тик |
| 5 | бюджет тика 270 с, маржа 30 с | 20 мин (`FINANCE_BUDGET_MS`); запрос начинается, только если остаток бюджета > ожидание пейсера + 30 с + таймаут HTTP (120 с) + хвост BigQuery (120 с); budget + таймаут + 5 мин накладных обязаны быть < 30 мин (timeout Job 1800 с и stale lease), иначе `FINANCE_CONFIG_INVALID` до I/O | лимит исполнения Apps Script 6 мин; из-за него понедельничный тик с WEEKLY уходил в PARTIAL (2 раза за 30 сут). Резерв не даёт убить Job между load-job и COMPLETE (строки без владельца → ORPHANED_RAW_REPORT) |
| 6 | финализация недель только в шаге 6; снимок финальных недель — после шага 3 | снимок финальных недель и признак «есть открытые недели» — ДО шага 3; финализация — после цикла очереди (если там дошёл WEEKLY) и в шаге 6; недели, финализированные после очереди, пересчитываются (RECON, `daily_days_loaded`) после шага 5, если там грузились суточные | итог манифеста, недель и RECON — как у Apps Script (снимок «до очереди» = «после очереди» у Apps Script, где до шага 6 ничего не финализируется). Окно «WEEKLY COMPLETE, неделя не финальна» (в нём CANONICAL держит обе копии rrd_id, ASSERT витрины #91): для WEEKLY из очереди кончается с циклом очереди — без discovery и шага 5, то есть по составу короче, чем у Apps Script; для свежего WEEKLY — как у Apps Script, до конца шага 5. По времени окно ограничено бюджетом и не пересекается со слотами витрины: финансы 07:30/12:30/18:30 и кончаются < 30 мин, витрина 07/09/12/16:00 |
| 7 | журнал только `FINANCE_LOADER_RUNS` | тот же журнал + `LOADER_RUNS` (`loader_name='finance'`) | контракт брифа. ERROR тика → LOADER_RUNS ERROR + `loader_failed` с кодом → алерт; PARTIAL → COMPLETE + WARNING `FINANCE_PARTIAL`. В `V_INGEST_HEARTBEAT` у finance `logical_period` = NULL (слот не дата) — гейт витрины финансы не читает |
| 8 | `run_id` `FINDAILY_yyyyMMdd_HHmmss_<цифры>` | `FINDAILY_yyyyMMdd_HHmmss_C<цифры>` | строки/отчёты Cloud Run отличимы для отката без новой колонки; формат никто не разбирает |
| 9 | `trigger_type` AUTO/MANUAL/BACKFILL по функции | AUTO по умолчанию, MANUAL — `FINANCE_TRIGGER_TYPE=MANUAL`, BACKFILL — loader `finance-backfill` | Cloud Run не отличает запуск Scheduler от ручного |
| 10 | период догона — константы в коде | `FINANCE_BACKFILL_FROM/TO` в env исполнения | без правки кода |
| 11 | — | алерт `wb_finance_failed` (prod) | раньше сбой финансов был виден только в DRO-1 |
| 13 | проект Apps Script (один принципал) | свои SA `sa-wb-finance-shadow/prod`: jobUser, `LOADER_RUNS`, только свои 5 таблиц, только секрет `WB_TOKEN_FINANCE`; общие `sa-loaders-*` прав на финансы не получают | минимум прав; утечка прав одного потока не открывает другие |
| 14 | — | повтор COMPLETE-MERGE один раз при транзиентном отказе BigQuery | строки уже в RAW: без повтора отчёт остался бы STARTED со строками и через 120 мин стал бы ORPHANED |
| 12 | shadow нет | shadow: cutover `2026-11-09`, слоты 08:05/13:05/19:05 МСК | не перекачивать WEEKLY с июля; не делить минуту лимита с Apps Script |

Отличий в данных нет: те же таблицы, колонки, ключи, jobId-схема, маппинг и статусы.

**Догон (`WbFinanceBackfillBQ.gs`) отдельным режимом не нужен:** он закрыт kill-switch'ем и писал мимо orphan guard.
Его функцию выполняет `finance-backfill` (= `wbFinDeepBackfillRegister`): регистрирует WEEKLY периода, грузят обычные тики
под всеми проверками.

## 4. Shadow → паритет 5 дней

Предусловия: PR слит (deploy-shadow соберёт образ и обновит `wb-finance-shadow`, если Job уже есть).

1. **Terraform** (план — владельцу, apply — по ACK), `finance_prod_enabled` = false (по умолчанию — prod-ресурсов в
   конфигурации нет вовсе). `-target`: `google_bigquery_table.{raw_wb_finance_cr,finance_loader_runs_cr,finance_report_loads_cr,
   finance_week_status_cr,finance_week_recon_cr}`, `google_service_account.finance_shadow`,
   `google_project_iam_member.finance_jobuser`, `google_bigquery_table_iam_member.finance_write_loader_runs`,
   `google_secret_manager_secret_iam_member.finance_token_access`, `google_service_account_iam_member.finance_actas_deployer`,
   `google_service_account_iam_member.finance_actas_terraform`, `google_bigquery_table_iam_member.finance_shadow_write_cr`,
   `google_cloud_run_v2_job.wb_finance_shadow`, `google_cloud_run_v2_job_iam_member.scheduler_finance_shadow_invoke`,
   `google_cloud_scheduler_job.wb_finance_shadow`. Ожидание плана: 5 таблиц + 1 SA + 5 привязок SA + 5 `__CR` + Job +
   invoker + Scheduler = 19 add, 0 change, 0 destroy.
   Перед apply сверить `FINANCE_CUTOVER` shadow: понедельник недели **перед** стартом shadow.
2. Образ: `deploy-shadow` (workflow_dispatch) — шаг `wb-finance-shadow` продвинет тот же digest.
3. `scheduler-control`: environment=shadow, loader=`wb-finance`, action=resume.
4. Первый день: календарь недель shadow с cutover, WEEKLY этих недель, daily за сегодня−3…сегодня (daily финальных недель
   пропускаются). Проверить `FINANCE_LOADER_RUNS__CR` (QUERY 4) — OK/OK_NO_NEW, `requests_made` разумные.
5. Ежедневно после 19:30 МСК — `sql/mig_finance_parity.sql` QUERY 1–3.
   **Приёмка:** 5 последовательных суток без FAIL, из них ≥ 3 EXACT, QUERY 3 без FAIL, в shadow нет ERROR прогонов.
   Неделя, на которой стартовал shadow, может законно разойтись по `daily_days_loaded` (shadow не грузил суточные до старта).
   Понедельник (WEEKLY) обязан попасть в окно приёмки.

Цена: shadow удваивает запросы к Finance API (~6–10/сут) и BigQuery-запросы финансов (мелкие MERGE по манифесту,
по 10 МБ минимум); RAW `__CR` растёт на объём отчётов (~0,5 МБ/сут при ~1,7 КБ на строку).

## 5. Переключение (вечер дня X, по ACK M4)

1. Дождаться завершения вечернего тика Apps Script (~18:40 МСК, `FINANCE_LOADER_RUNS`: последняя строка ≠ RUNNING).
2. Проверка манифеста (только SELECT): нет STARTED; нет ERROR c `attempt_count=5` без разбора; PK манифеста цел.
3. Apps Script: `wbFinRemoveDailyTriggers()`; затем `wbFinanceBackfillAuditTriggers()` — триггеров финансов 0.
4. Terraform (ACK): включить флаг `finance_prod_enabled = true` (tfvars / `-var` infra.yml), план по prod-ресурсам
   `wb_finance_loader.tf`: `google_service_account.finance_prod[0]`, его привязки (`finance_jobuser["prod"]`,
   `finance_write_loader_runs["prod"]`, `finance_token_access["prod"]`, `finance_actas_deployer["prod"]`,
   `finance_actas_terraform["prod"]`), `finance_prod_write` ×5 (боевые таблицы), `wb_finance_prod[0]`, invoker,
   `google_cloud_scheduler_job.wb_finance_prod[0]` (PAUSED), `google_monitoring_alert_policy.wb_finance_failed[0]`.
   Ожидание: 15 add, 0 change, 0 destroy (shadow не меняется). Флаг после cutover остаётся true; откат флагом
   в false удалит prod-контур — только после возврата на Apps Script (§6).
5. Образ **только в свой Job**: `gcloud run jobs update wb-finance-prod --image <digest, проверенный shadow>
   --update-env-vars IMAGE_DIGEST=…,GIT_SHA=…` (или `deploy-prod.yml only_job=wb-finance-prod`, когда механизм из
   #382/#416 будет в main). `deploy-prod.yml` в этом PR не меняется: общий промоушен двигает 12 prod-Job'ов.
6. Сверить env Job'а (`gcloud run jobs describe wb-finance-prod`): боевые имена 5 таблиц, `FINANCE_CUTOVER=2026-07-13`.
   Ошибка здесь не запишет ничего: `FINANCE_TARGET_MISMATCH` до токена и BigQuery.
7. `scheduler-control`: environment=prod, loader=`wb-finance`, action=resume. Первый прогон — 07:30 МСК.
8. Утро X+1: `FINANCE_LOADER_RUNS` — новые строки с `run_id LIKE 'FINDAILY_%_C%'`, статус OK/OK_NO_NEW; `LOADER_RUNS`
   `environment='prod' AND loader_name='finance'` COMPLETE; витрина за D-1 собралась (guard увидел новую версию финансов).
   Ручного prod-запуска до утра нет.

Shadow после cutover: пауза `wb-finance-shadow`; таблицы `__CR` удалить отдельным PR после недели стабильной работы prod.

## 6. STOP и откат

**STOP** (пауза `wb-finance-prod`, разбор до продолжения):
- `ORPHAN_GUARD` (`ORPHANED_RAW_*`) — строки отчёта без владельца; автоматического «усыновления» нет (как и в Apps Script);
- `POSTLOAD_*`, `KEY_CONFLICT`, `FINANCE_MANIFEST_PK_VIOLATION`, `FINANCE_IMMUTABLE_ANOMALY`;
- ASSERT витрины «CANONICAL содержит дубли rrd_id»;
- `FINANCE_TARGET_MISMATCH`, `FINANCE_SCHEMA_NOT_READY`.

Не STOP: `PARTIAL` (хвост очереди), `DETAILED_FAILED`/`list HTTP 429|5xx` с последующим COMPLETE в следующем слоте.

`finance-backfill` запускать вне слотов (`gcloud run jobs execute wb-finance-<env> --args=finance-backfill
--update-env-vars FINANCE_BACKFILL_FROM=…,FINANCE_BACKFILL_TO=…`): при активном тике он откажет `FINANCE_LOCKED`.

**Откат** (данные контуров совместимы — общий манифест, Apps Script продолжит с того же состояния):
1. `scheduler-control` pause `wb-finance-prod`; дождаться конца текущего исполнения.
2. Apps Script: `wbFinInstallDailyTriggers()` (C0-маркер в Script Properties не тронут, проверка пройдёт).
3. Если Cloud Run записал дефектные отчёты: найти `FINANCE_REPORT_LOADS.processing_run_id LIKE 'FINDAILY_%_C%'`,
   разобрать вручную по образцу PR-A (ручное восстановление) — DML только по отдельному ACK.

## 7. Что нужно для shadow-выпуска

- **Terraform targets** — §4 п.1 (19 ресурсов, prod не затрагивается: под флагом `finance_prod_enabled=false`).
  `terraform validate -backend=false` — Success.
- **Секрет:** `WB_TOKEN_FINANCE` уже в `local.wb_secrets`; accessor выдаётся новым SA (`finance_token_access`).
  Версия v1 от 26.07 — проверить срок действия (без вывода значения) до старта shadow.
- **Расписание:** shadow `5 8,13,19 * * *` Europe/Moscow (PAUSED при создании); prod `30 7,12,18 * * *` (PAUSED).
- **Workflows:** `deploy-shadow.yml` — шаг `wb-finance-shadow` (guarded); `scheduler-control.yml` — loader `wb-finance`.

## 8. Открытые вопросы к владельцу

1. Подтвердить живой список триггеров `wbFinanceBackfillAuditTriggers()` (ожидание: 3 × `runWbFinanceDaily`, 0 backfill).
2. Срок действия токена `WB_TOKEN_FINANCE` (v1 от 26.07) — у `WB_TOKEN_STATISTICS` 02.11 истёк.
3. Дата старта shadow → значение `FINANCE_CUTOVER` shadow (сейчас `2026-11-09`).
4. Статус `RUNNING` в `FINANCE_LOADER_RUNS` (пишут оба контура) в `dashboard_layer_v1.sql` попадает в `UNMAPPED`
   (карта знает только `STARTED`) — существующее поведение, не менялось; исправлять ли отдельно.
5. Алерт по каждому `finance_report_error` (а не только по ERROR прогона) — достаточно ли часового rate-limit.
6. DDL вью `V_WB_FINANCE_COMPLETE/CANONICAL/V_WB_FINANCE` живёт только в `Wbfinancedaily.gs` — перенос в `sql/` (M6).
