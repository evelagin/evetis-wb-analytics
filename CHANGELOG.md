# CHANGELOG.md

## История изменений

### 2026-08-26 — Fix: знаковая нормализация REF_COST_MAP + возврат MART-процедуры на production-совместимую линию (Stage 1.5 / 1.6)

**Что обнаружено (Stage 1.4, acceptance-аудит 27.07–16.08).** Сверка официальных еженедельных финотчётов WB против витрины выявила два расхождения при том, что остальные 8 категорий затрат сходились до копейки. `deduction_rub` в `V_DASH` и на карточке 51 показывал **79 168,68 ₽** против **33 312,68 ₽** по документам WB; `wb_reward_rub` — **47 452,07 ₽** против **42 584,09 ₽**.

**Причина.** `V_WB_FINANCE_AMOUNTS_LONG_MAPPED` применяет `ABS()` **построчно** к записям, помеченным в `REF_COST_MAP` как `COST`. Для однознакового поля это безвредно, для знакопеременного — уничтожает знак: кредит превращается в расход, а итог растёт на `2 × |сумма противознака|`. Семантика подтверждена самим документом WB: у отрицательных «Удержаний» в поле `Виды логистики, штрафов и корректировок ВВ` записано «Возврат неиспользованного остатка аванса за услугу "Баллы за отзывы"» (−2 928,00) и «Добровольная выплата за товары, пострадавшие в результате обстоятельств непреодолимой силы» (−20 000,00) — это деньги в пользу продавца. У отрицательных строк `Продажа/commission_amount` отрицателен и сам процент `Итоговый кВВ без НДС, %`, то есть это штатное отрицательное вознаграждение по конкретной продаже.

**Полный знаковый аудит истории** выявил **4** знакопеременные пары в ABS-ветках, а не заявленные две. Все переведены в уже существующую ветку `ADJUSTMENT` (`x * field_normalization_sign`), которая знак сохраняет — ту же, на которой корректно работала «Коррекция хранения». Формула `LONG_MAPPED` не менялась.

| op_key / amount_field | было | стало | искажение за историю |
| --- | --- | --- | --- |
| Продажа / commission_amount | `COST`, sign `-1` | `ADJUSTMENT`, sign `+1` | 2 231 127,66 ₽ |
| Удержание / deduction | `COST`, sign `1` | `ADJUSTMENT`, sign `1` | 307 917,84 ₽ |
| Пересчет платной приемки / acceptance | `COST`, sign `1` | `ADJUSTMENT`, sign `1` | 4 000,00 ₽ |
| Штраф / penalty | `COST`, sign `1` | `ADJUSTMENT`, sign `1` | 548,00 ₽ |

Остальные 15 правил проверены и не тронуты: `logistics`, `storage`, `reimbursements`, `acquiring`, `loyalty` — либо однознаковые, либо уже в `ADJUSTMENT`.

**Fail-closed guard (`fix #5` в `sp_build_mart_sku_daily`).** Запрещает знакопеременную пару в ветке `COST`/`CREDIT`. Негативный тест на эмуляции дофиксовой карты: ASSERT падает и называет все 4 пары. На текущей карте — 0 нарушений.

**Затронуто.** `wb_mart.REF_COST_MAP` (UPDATE 4 строк), `sp_build_mart_sku_daily` (+ASSERT), пересборка `MART_SKU_DAILY` тем же `build_as_of = 2026-08-25`. `FACT_FINANCE`, `FACT_SALES`, `FACT_ORDERS` и RAW **не трогались** — сырые знаковые значения сохранены. Структура колонок витрины **не менялась**. SQL карточек Metabase не редактировался: карточка 51 исправилась сама как pass-through.

**Контрольные цифры после фикса (27.07–16.08).** Цепочка WB XLSX → `FACT_FINANCE` → `LONG_MAPPED` → `V_DASH` → Metabase даёт delta 0,00 для обеих метрик: удержания **33 312,68**, wb_reward **42 584,09**. Не изменились: заказы 451, выкупы 402, выручка 292 302,67, сбор маркетплейса 132 487,86, логистика 29 605,33, хранение 8 905,24, штрафы 6 320,00, к перечислению 159 814,70, реклама 48 579,56, **контрибуция 81 629,92** (формула PR-B2 не содержит ни `deduction`, ни `wb_reward`). Производные сдвинулись согласованно: `account_level_total` 93 751,51 → 47 895,51, `finance_long_total` 175 639,90 → 124 915,91, `mart_carried` 77 057,40 → 72 189,42. SKU↔KPI: 13 метрик, все Δ 0,00; грейн 525 = 21 × 25, сирот 0.

⚠️ **Историческое последствие, согласованное с владельцем.** `wb_reward` за всю историю меняет знак: +1 400 787,96 → −830 339,71 ₽. Это верно по WB (знак смешан в каждом месяце с 2024-09), метрика не выводится на дашборде 2 и не входит в формулу контрибуции.

---

**Stage 1.6 — deployment drift.** При выкате guard'а обнаружено, что репозиторий и production разошлись: развёрнутая `sp_build_mart_sku_daily` соответствовала линии `8290672` (PR-B2), тогда как HEAD содержал `e30f668` «feat(ads): prepare billed spend allocation contract (#116)». HEAD потребляет `wb_mart.FACT_ADS_SPEND_ALLOC_DAILY`, которой в production **нет**; сам commit-message #116 объявляет change-set «prepared but remains blocked from production deployment until Stage 3B.1 Phase B cutover». Опасность в том, что процедура при этом **создаётся успешно и падает только на `CALL`** — то есть тихо подменяет рабочую витрину неработоспособной.

**Решение.** `sql/mart/pr_mart2b_sku_daily.sql` возвращён на production-совместимую линию `8290672` + guard `fix #5`. Развёрнутое тело процедуры и файл теперь совпадают побайтово (24 570 симв.). Работа Stage 3B **не удалена и не потеряна** — в шапке файла зафиксированы точки восстановления: `git show e30f668`, `docs/ADS_SPEND_STAGE3B_2026-08-20.md`, `sql/mart/pr_mart1_facts.sql §1.7` (производитель таблицы), `ads_spend_reconciliation_v1.sql`, `ads_spend_stage3b_validation.sql`, плюс порядок возврата после Phase B cutover.

**Остаточный дрейф, требующий решения владельца.** `sql/mart/pr_mart1_facts.sql` на HEAD также содержит неразвёрнутую часть #116 (создание `FACT_ADS_SPEND_ALLOC_DAILY` и `FACT_ADS_SPEND_UNALLOC_DAILY`); развёрнутая `sp_bootstrap_facts` от 17.08 их не содержит. Файл самодостаточен и деплоится, но остаётся сознательно gated до Phase B — в этом стейдже не трогался.

**Гигиена.** `.playwright-mcp/` добавлен в `.gitignore` (артефакты браузера вне Git).

### 2026-08-17 — Fix: грейн FACT_STOCKS_SNAPSHOT переведён на `warehouse_key` (ветка fix/mart-stocks-warehouse-key)

**Инцидент.** 17.08 два последовательных прогона `wb-mart-prod` (`wb-mart-prod-b4jdd` 06:00 и `wb-mart-prod-t487f` 08:00) прошли `freshness_gate` = OK и упали внутри `sp_bootstrap_facts` за 31 с с одинаковой ошибкой `FACT_STOCKS_SNAPSHOT: NULL grain/partition`. Витрина стоит на `target_date = 2026-08-15`.

**Причина — не дефект, а несогласованная смена контракта.** 16.08 WB обезличил склад отгрузки, и T6 стал отдавать `warehouseId = -999999`. Правка лоадера от 16.08 (`WbStocksSnapshot.gs`, `wbStocksWarehouse_()`) сознательно трактует это как **сентинел «склад не раскрываем», а не как идентификатор**: сырое значение уходит в новую колонку `warehouse_code STRING`, а `warehouse_id INT64` остаётся **NULL** — чтобы `-999999` не притворялся настоящим id склада. Решение правильное, но producer при этом переехал на новый ключ грейна, не объявив этого, а consumer `sp_bootstrap_facts` (деплой 13.08) остался на `warehouse_id`. Итог: 46 строк (23 за 16.08 + 23 за 17.08) с NULL в грейне, и fail-closed ASSERT завалил сборку.

**Замер источника (read-only, вся история 30 суток):**

| snapshot_date | строк | `warehouse_id IS NULL` | `warehouse_code` пуст | складов |
|---|---|---|---|---|
| 2026-08-17 | 23 | 23 | 0 | 0 |
| 2026-08-16 | 23 | 23 | 0 | 0 |
| 2026-08-15 | 241 | 0 | 241 | 39 |
| ≤ 2026-08-14 | ~240/сут | 0 | все | 30–37 |

**Правка — на стороне consumer-а.** Грейн `FACT_STOCKS_SNAPSHOT`: `snapshot_date × nm_id × warehouse_id (INT64)` → `snapshot_date × nm_id × warehouse_key (STRING)`, где

```sql
warehouse_key = COALESCE(NULLIF(warehouse_code, ''), CAST(warehouse_id AS STRING))
```

Ключ покрывает обе эпохи: до 15.08 включительно — числовой id при пустом `code`, с 16.08 — наоборот. Тип STRING выбран потому, что WB уже прислал непрозрачное значение и может прислать нечисловое; возврат `-999999` в `warehouse_id` через COALESCE был отклонён — он вписал бы выдуманный склад в факт-таблицу навсегда.

**Изменение структуры колонок FACT.** Добавлена `warehouse_key STRING` (позиция 3). `warehouse_id INT64` **остаётся** в таблице как справочная колонка (NULL с 16.08) — на ней построена вся история до 15.08, включая расчёт сгоревших остатков. `CLUSTER BY nm_id, warehouse_key` в BUILD и в publish. Физика (1 partition + 2 cluster) не изменилась, ASSERT через `INFORMATION_SCHEMA` не трогали. Потребителям разреза по складу брать `warehouse_key`.

⚠️ **Сигнатура ошибки изменилась** вместе с типом грейна: `'FACT_STOCKS_SNAPSHOT: NULL grain/partition'` → `'FACT_STOCKS_SNAPSHOT: NULL/empty grain or NULL partition'` (как у FACT_ORDERS/FACT_SALES, где грейн строковый) и `'…(snapshot_date,nm_id,warehouse_id) not unique'` → `'…(snapshot_date,nm_id,warehouse_key) not unique'`. Мониторинг и алерты по тексту ошибки надо перечитать.

**Доказательства до выката (read-only на проде):**

- новый ключ на всей истории: **5 637 строк = 5 637 distinct, 0 NULL** — уникален и не-NULL на обеих эпохах;
- **регрессия истории: 0 расхождений.** FULL JOIN нового BUILD с опубликованной `FACT_STOCKS_SNAPSHOT` по `(snapshot_date, nm_id, warehouse_key)` на партициях ≤ 15.08: 5 591 против 5 591, ни одной строки с расхождением `quantity` / `in_way_to_client` / `in_way_from_client`. Правка добавляет два недостающих дня и **не переписывает историю**;
- все четыре ASSERT блока 1.3 прогнаны на реальных данных и проходят.

**Усиление по итогам аудита (ред. 2, 17.08).** Две правки, добавленные до PR:

1. **`ANY_VALUE` перестал быть предположением.** Однозначность `warehouse_id` внутри `warehouse_key` НЕ следует из построения ключа — несколько RAW-строк теоретически могут нести один `warehouse_code` и разные `warehouse_id`, и `ANY_VALUE` молча станет недетерминированным. Добавлены два ASSERT **на RAW до агрегации** (в BUILD нарушение уже неразличимо): `'FACT_STOCKS_SNAPSHOT: multiple warehouse_id per warehouse_key'` и `'…: multiple descriptive values per warehouse_key (name/region/sku)'` — второй покрывает остальные `ANY_VALUE`-колонки (`warehouse_name`, `region_name`, `internal_sku`, `sku_match_status`). ⚠️ Внутри обязателен `FORMAT('%t', x)`: голый `COUNT(DISTINCT)` игнорирует NULL, и группа `{NULL, 5}` прошла бы как однозначная — а это ровно тот случай, который ловим. Замер на всей истории: **0 нарушений по всем пяти колонкам**; попутно выяснилось, что `max` RAW-строк на группу = **1**, то есть сегодня `GROUP BY` не схлопывает ничего (у наших SKU один `chrt_id`) — агрегация заработает по-настоящему, когда появится размерный ряд.
2. **Проверка 4a стала настоящим гейтом.** Была диагностическим `SELECT` с комментарием «должен быть 0» — то есть описывала себя как защиту, не будучи ею. Теперь `ASSERT … = 0 AS 'STOCKS: warehouse_id and warehouse_code both empty'`. Диагностическая разбивка по датам оставлена рядом, добавлена **4b** — разбивка нарушений по колонкам, чтобы при срабатывании ASSERT сразу видеть виновника. Проверено, что гейт безопасен на полном объёме RAW: 5 907 строк, 36 снапшотов, **все COMPLETE** (fail-closed лоадера держит — частичных записей от упавших снимков в RAW нет), `both_empty = 0`.

⏭️ **Принято, но сознательно отложено: namespace ключа** вида `id:123456` / `code:-999999`. `warehouse_id` и `warehouse_code` — разные пространства идентификаторов, и голая строка теоретически допускает коллизию числового id со строковым code. Сегодня коллизий нет (5 637 = 5 637 distinct), а префикс означает переписывание ключа во всей истории — цена выше пользы, пока WB отдаёт один непрозрачный код. Вернуться, если появится второй. Зафиксировано комментарием в блоке 1.3, чтобы вопрос не потерялся.

**Данные не пострадали.** Fail-closed отработал как задумано: BUILD упал до publish, опубликованная `FACT_STOCKS_SNAPSHOT` осталась целой на 15.08 (5 591 строка, 0 NULL в `warehouse_id`). Откат не требуется. Радиус поражения проверен: единственный потребитель `FACT_STOCKS_SNAPSHOT` вне самой процедуры — `sql/mart/dashboard_layer_v1.sql`, и он берёт только `MAX(snapshot_date)` и `MAX(built_at)`; на `warehouse_id` не завязан никто. `WB_STOCKS_SNAPSHOTS_BAK_20260816` для диагноза не понадобился — это бэкап манифеста (6 строк), схема совпадает с текущей байт в байт, структурное отличие сидит в `RAW_WB_STOCKS`.

Отдельный ПЕРЕ-bootstrap не нужен: FACT-таблицы собираются `CREATE OR REPLACE`, штатный прогон `wb-mart-prod` пересоздаст таблицу с новой схемой.

Файлы: `sql/mart/pr_mart1_facts.sql` (блок 1.3, publish, шапка), `sql/mart/pr_mart1_validation.sql` (проверка 4 переведена на новый ключ + новая проверка 4a: `warehouse_id` и `warehouse_code` не должны быть пустыми одновременно НИКОГДА).

🔴 **Урок метода: смена ключа грейна на стороне producer-а — это изменение публичного контракта, даже если новая колонка добавлена аддитивно.** Лоадер 16.08 добавил `warehouse_code` и обнулил `warehouse_id`, ничего не сломав у себя — все его собственные проверки прошли (23/23/23). Сломался потребитель через сутки, и поймал это не producer, а ASSERT витрины. Правило: при смене ключа грейна в RAW обязательно проходить по списку потребителей грейна до выката, а не после.

### 2026-08-11 — PR-Mart3b-3: Cloud Scheduler витрины `wb-mart-prod` (ветка feat/mart3b-3-scheduler)

Последний недостающий элемент облачной цепочки витрины. `wb-mart-prod` проходил зелёным с 10.08 (`MART_RUNS` COMPLETE, target_date 2026-08-09, mart_rows 7012, `lease_only_no_mart=FALSE`, `exit(0)`), но запускался **вручную** — то есть `MART_SKU_DAILY` была свежа ровно до дня последнего ручного запуска.

`scheduler.tf`: `google_cloud_scheduler_job "wb_mart_prod"` — OAuth от `sa-scheduler-prod`, POST на Run Admin API `…/jobs/wb-mart-prod:run`. **Имя обязано быть `wb-mart-prod`**: `scheduler-control.yml` собирает имя как `"<loader>-<environment>"`. Создаётся `paused = true` + `ignore_changes = [paused]` — конфиг принадлежит Terraform, состояние pause/resume принадлежит `scheduler-control.yml`, иначе очередной apply мог бы остановить рабочий загрузчик.

**Расписание `0 7,9,12,16 * * *` Europe/Moscow выбрано по данным, а не по интуиции.** Новый `sql/mart3/pr_mart3b3_freshness_readiness.sql` прогоняет freshness-gate (LATEST-ATTEMPT) по `V_INGEST_HEARTBEAT` для каждого часа МСК за 7 полных суток 03–09.08.2026. Результат опроверг исходное предположение «источники подтягиваются позже»: источники готовы уже к **~05:11 МСК** (лимитирует ads: старт 05:07 → COMPLETE 05:11), часы 06:00–10:00 и 12:00–23:00 зелёные 7/7, а **11:00 — единственный красный час в сутках, 0/7**. Причина: hourly-loader sales стартует в 10:22 и падал **7 раз из 7** с WB HTTP 429 «Limited by global limiter, per seller»; семантика LATEST-ATTEMPT превращает это в ровно часовое окно красноты 10:2x→11:2x (следующая успешная попытка — 11:22). Поэтому 10:00/11:00 исключены, а окна разнесены по обе стороны отказа: 07:00 основное (с зазором от Apps Script-окна триггера ads `atHour(5)`, который может сработать в любой момент 05:00–06:00), 09:00 резерв до отказа sales, 12:00 первый безопасный час после восстановления, 16:00 глубокий фолбэк.

**Границы доказательства зафиксированы в дизайн-документе:** история `INGEST_RUNS` начинается 02.08 (PR#82), поэтому окно наблюдения — 7 суток, а не запрошенные аудитом 14–30; **ads делает ровно одну попытку в сутки** (hourly-ретраев нет), поэтому при падении ads ни одно окно дня витрину не спасёт — это задача алертов (PR-Mart3b-5) и ретрая ads-триггера, а не расписания; эффект PR #87 в проде **не подтверждён** — отказ sales 10:22 воспроизводился и 10.08, контроль — запрос Q4 в новом SQL.

`retry_config.retry_count=1` безопасен и НЕ дублирует `max_retries=0` у Job'а: вызов `:run` асинхронный (200 = execution создан, исход прогона Scheduler не видит), поэтому retry Scheduler повторяет только неудавшийся *вызов API*; дубль-execution отсекается lease по свежему STARTED. `max_retries` Job'а остаётся 0: task-retry повторяет task в ТОМ ЖЕ execution → тот же `run_id` → `MART_RUNS_CONFLICT`.

`iam.tf`: `roles/run.invoker` для `sa-scheduler-prod` на Job `wb-mart-prod` — поресурсно. Без этого биндинга Scheduler молча падал бы по 403. Новых проектных ролей не выдаётся: `roles/cloudscheduler.admin` у `sa-terraform-apply` уже есть.

`scheduler-control.yml`: входы переведены в `type: choice`; добавлен **fail-fast первым шагом** — комбинация `loader=wb-mart` + `environment != prod` отвергается ДО WIF-аутентификации. `mart` помечен `prodOnly` в реестре, Terraform создаёт только `wb-mart-prod`, поэтому `wb-mart-shadow` не существует никогда; без проверки оператор получал невнятный `NOT_FOUND` от gcloud уже после того, как workflow взял привилегированный `sa-terraform-apply`.

В приёмку добавлен конкурентный тест lease: параллельный `gcloud run jobs execute` без `--wait` обязан дать `guard_skip reason=ALREADY_RUNNING`, а повтор после завершения — `reason=COMPLETE`. Снятие паузы — PR-Mart3b-4; алерты ERROR/SLA — PR-Mart3b-5. Дизайн и критерии приёмки — `docs/MART_PR3B3_SCHEDULER_2026-08-11.md`.

### 2026-07-16 — Фаза E: production-загрузчик остатков (ветка phase-e-stocks-loader)

Первый шаг операционной видимости. Источник — **T6** `stocks-report/wb-warehouses` (плоский снимок, grain `snapshot × nmId × chrtId × warehouseId`); **T5** `warehouse_remains` — НЕблокирующий контроль суммы физ. остатков. Период — `today..today` Europe/Moscow (B2 доказал: `currentPeriod` не влияет на снимок). Ключ доказан probe: 155 строк = 155 distinct = 0 дублей; Σфиз T6=T5=4565.

Новый `WbStocksBigQuery.gs` (BQ-слой): `RAW_WB_STOCKS` (append-only, партиция `_snapshot_date`, кластер `nm_id/warehouse_id`; **без** `processed_status/error_message` — ошибка снимка живёт в manifest) + manifest `WB_STOCKS_SNAPSHOTS` (источник истины: STARTED/COMPLETE/ERROR + метрики) + `V_WB_STOCKS_CURRENT` (строки RAW только последнего **COMPLETE** снимка `ORDER BY completed_at DESC, snapshot_id DESC`). Нативные типы (TIMESTAMP UTC ISO, DATE бизнес-дня МСК, `unmatched_nm_ids` JSON-строкой). **C3:** свой детерминированный load-wrapper `wbStocksBqLoadDeterministic_` — batch jobId `STOCK_<snapshot_id>_BATCH_<n>`; при дубликате jobId не вставляет заново, а ждёт DONE. Дополнительно (аудит PR#62): при ЛЮБОЙ ошибке `Jobs.insert` (timeout/сетевая неопределённость) перед retry/throw проверяем существование job по детерминированному jobId (`wbStocksBqJobExists_` → `Jobs.get`): job есть → не дублируем; точно not-found → повтор; проверить не смогли → fail closed. Это критично и для `MANIFEST_START` (иначе STARTED-строка могла записаться, а `_manifestStarted` остаться false → никогда не финализируется). **C2:** DML-хелпер `wbStocksBqDml_` — сперва дожидается `jobComplete=true` (`getQueryResults` по jobId) и проверяет `errorResult` (`Jobs.get`), и только потом читает `numDmlAffectedRows` (Jobs.query может вернуть `jobComplete=false`); финал `wbStocksBqManifestFinalize_` — `UPDATE ... WHERE snapshot_id=? AND status='STARTED'` с обязательной проверкой `==1`. Общие `bqLoadRows_/bqQuery_` НЕ трогали. `wbStocksBqInitC0()` — smoke без WB API (sink+таблицы+вью, fail-closed rollback).

Новый `WbStocksSnapshot.gs` (оркестрация): `runWbStocksSnapshot()`+ядро по порядку **C1–C2**: общий `ScriptLock` (в одном Apps Script проекте другого нет; суточный каденс это допускает) → sink ON? (**OFF → runtime ERROR без manifest**) → ensure RAW+manifest → `snapshot_id=STOCK_SNAP_yyyyMMdd_HHmmss_<uuid8>`+`started_at` → **manifest STARTED (до fetch)** → T6 fetch (429→пауза 21с) → **валидация всего пакета до записи** (типы nmId>0/chrtId/warehouseId≥0/quantity·inWay≥0 целые; ключ уникален; **пустой T6 = ERROR**) → нормализация + SKU-привязка по nmId (unmatched → пустой `internal_sku`, список в manifest) → T5-контроль (не блокирует: `OK`/`MISMATCH`+delta/`T5_UNAVAILABLE`) → RAW append → **пост-COUNT** (`COUNT(*)==expected` И `COUNT(DISTINCT nm|chrt|wh)==expected` И `COUNTIF(snapshot_id NULL/'')==0`) → manifest COMPLETE/ERROR. Статусы `OK`/`SKIPPED_LOCKED`/`ERROR`; частичная запись → manifest НЕ COMPLETE → VIEW остаётся на прошлом снимке. Дневной триггер `wbStocksInstallDailyTrigger()` `everyDays(1).atHour(6).nearMinute(30)` (~06:30 МСК, разнесён от hourly), идемпотентно `0→1/1→1/2+→1`. Sheet `IMPORT_LOG_STOCKS` — только best-effort (книга у лимита; VIEW/monitor от него не зависят).

Легаси листовой `WbStocksLoader` НЕ трогали (BQ-first, отдельные файлы). Продажи/заказы/финансы/рекламу и их RAW/вью — не затрагивали. `node --check` пройден. Apps Script/триггер — после ручной приёмки: `wbStocksBqInitC0()` → `runWbStocksSnapshot()` (R1) → повтор (R2) → fail-closed (R4 T6/R5 T5/R6 mismatch) → `wbStocksInstallDailyTrigger()`.

### 2026-07-16 — B2: проверка семантики периода T6 в probe (ветка test/stocks-t6-period-semantics)

Аудит плана загрузчика остатков принял направление (канон T6, контроль T5), но выставил блокер B2: до production нельзя фиксировать `currentPeriod` T6 на тестовых датах мая — совпадение Σфиз с текущим T5 (4565) само по себе не доказывает семантику. `WbStocksProbe`: добавлен Drive-независимый `probeWbStocksT6Periods()` — гоняет T6 три раза (A `2026-05-18..24`, B сегодня, C последние 7 дней) и сравнивает rows · distinctKey · Σquantity(all/физ) · Σв пути · uniqueNmId. Вердикт в лог: идентичны → период не влияет (берём безопасное окно); различаются → период влияет (выбрать окно под текущие остатки, сверить Σфиз с T5); не-200 на «сегодня» → окно ограничено. Хелперы `stocksProbeT6Metrics_`/`stocksProbeT6Fetch_`; Σфиз считается без агрегатного склада `warehouseId=0/«Остальные»`. Токен не логируется, harness не тронут. Меню дополнено пунктом «T6 период-семантика (B2)». `node --check` пройден. Также фиксируется блокер B1 (manifest-таблица `WB_STOCKS_SNAPSHOTS` + VIEW по последнему COMPLETE) — сворачивается в финальный план перед кодом загрузчика.

### 2026-07-16 — Fail-open console-probe остатков (ветка fix/stocks-probe-drive-failopen)

Диагностика источника остатков (T5 `warehouse_remains` / T6 `stocks-report/wb-warehouses`) блокировалась ещё ДО WB API: `probeWbStocksTestOnly()` идёт через `wbApiTestPrepare_()`, который при недоступной Drive-папке (`Permission denied while enabling APIs: drive`) возвращает `BLOCKED` и не доходит до T5/T6. Для диагностики Drive не нужен.

`WbStocksProbe`: добавлен **Drive-независимый** `probeWbStocksConsole(opts)` — обращается к WB напрямую и всё выводит в `console.log`: endpoint, HTTP, тип ответа, число строк, ключи первой строки, 1–3 примера (без токена), уникальные nmId/barcode/склад, `SUM(quantity)`, физ. и псевдо-склады («всего»/«в пути»), ошибки API. Сохранение полного JSON в Drive — ОПЦИОНАЛЬНО (`{saveJson:true}`) и НЕ блокирует: недоступный Drive → только WARNING. Ошибка самого WB API по-прежнему даёт ERROR/PARTIAL. Токен/заголовки не логируются. Переиспользует Drive-free хелперы `WbApiTest*` (токен/HTTP/task/парсинг) — harness `WbApiTestRunner/Utils/Config` НЕ изменён. Точка повторного запуска: `probeWbStocksConsole()` (без аргументов — Drive не трогается). Меню `addWbStocksProbeMenu` дополнено пунктом console-probe. Старый `probeWbStocksTestOnly()` не тронут. `node --check` пройден.

**Живой прогон 16.07:** оба источника OK, физ-остаток сошёлся T5=T6=4565, uniqueNmId=23, unmatched=1. T5 warehouse_remains — 23 строки со вложенным `warehouses[]` (нужно разворачивать); T6 stocks-report/wb-warehouses — 155 плоских строк `nmId·chrtId·warehouseId·warehouseName·regionName·quantity·inWayToClient·inWayFromClient`. **Решение: канон — T6 (плоский, есть стабильный warehouseId + регион), T5 — контроль общей суммы.** Добавлена диагностика ключа T6: distinct `nmId|chrtId|warehouseId`, дубли ключа, quantity>0/=0, спец-склад `warehouseId=0/«Остальные»` (строк+Σ), список unmatched nmId (по T5 и T6). Прогнать перед RAW-загрузчиком: ожидаем distinct key=155, duplicate=0.

### 2026-07-14 — Фаза D2c: Sales Night Reconciliation + общий rate-limit guard (ветка phase-d2c-sales-night-reconcile)

Ночная пересверка продаж/возвратов — закрывает eventual-consistency Sales API (строка может всплыть позже с `lastChangeDate < watermark`, hourly её не увидит) и пропуски от сбоев/429. Это правильное место для **range-wide дедупа** (в hourly он сознательно НЕ делается). watermark пересверка **не двигает** — им владеет hourly.

**Эмпирика ключа дедупа (BigQuery-коннектор, 14.07):** RAW `WB_API_SALES` = 3421 строк → distinct `sale_id` **3345** = distinct `sale_id|TO_HEX(MD5(raw_json))` **3345** (76 физических дублей — точные повторы состояния, идемпотентность). Окно 7 дней (`last_change_date >= 2026-07-07T00:00:00`): 253 строки → **177** уникальных state-ключей (тривиально в память). `last_change_date` — фикс-ISO с `T` → лексикографическое `>=` хронологически корректно (как watermark). Вывод: range-wide ключ состояния `sale_id|md5(raw_json)` доказан; пересверка дописывает только отсутствующие состояния и точные повторы не плодит.

**Обязательное дополнение по аудиту (ChatGPT) — общий 65-сек API cooldown для ОБОИХ путей.** Один `ScriptLock` защищает от одновременности, но НЕ от двух последовательных запросов Sales API в пределах минуты (доказанный runtime 429: hourly завершился и через ~15 сек стартовал nightly — снова вызвал API). Решение: Script Property `WB_SALES_API_LAST_REQUEST_AT_MS` + `wbSalesApiAcquireRequestSlot_()` (`WbSalesReconcile.gs`), вызывается ОБОИМИ путями под уже удерживаемым общим ScriptLock, непосредственно перед `fetchSalesApiData_`: если с прошлой **попытки** < 65 000 мс → `SKIPPED_RATE_LIMIT` без HTTP; иначе timestamp пишется **ДО** fetch (даже 429/500 = попытка к API, участвует в лимите) и выполняется ровно один запрос.

Новый файл `WbSalesReconcile.gs`:
- `runWbSalesNightReconcile()` + ядро `wbSalesNightReconcileCore_()` — под тем же общим `ScriptLock`, что hourly (`tryLock`→`finally releaseLock`); параллельный запуск = **SKIPPED_LOCKED**. Требует `WB_SALES_BQ_SINK=ON` (иначе ERROR, RAW неизменен).
- Окно: `WB_SALES_RECONCILE_DAYS` (Script Property, дефолт **7**, валидируется 1..90); `wbSalesReconcileFromLcd_()` = полночь МСК `(сегодня − N)` в формате `YYYY-MM-DDT00:00:00` (МСК фиксирован UTC+3 без DST). Одна строка — и API `dateFrom`, и `fromLcd` для BQ-набора; проходит ту же строгую `salesValidWatermark_` ДО SQL.
- Один fail-closed запрос, `noWindow`; та же **валидация ВСЕХ сырых API-строк ДО нормализации** (`saleID/date/lastChangeDate`) + страховка `rows.length===data.length`.
- **Внутрипакетный дедуп по STATE-ключу `sale_id|md5(raw_json)` — оставляем ВСЕ различные состояния** (в ОТЛИЧИЕ от hourly last-wins по `sale_id`: цель пересверки — залатать каждое отсутствующее состояние). Одинаковый state-ключ в пакете → одна строка. **Только после этого** сравнение с BQ-набором.
- Range-wide набор существующих состояний за окно — новый `wbSalesBqStateKeysSince_(fromLcd)`; append **только** тех `sale_id|state`, которых в RAW нет → `gaps_filled`. `rows_written == gaps_filled`. Каноничность — во `V_WB_SALES_RETURNS` (last-wins по `sale_id`), поэтому даже если пересверка дольёт старое состояние — вью остаётся distinct `sale_id`.
- Статусы: `OK` / `OK_NO_GAPS` / `SKIPPED_LOCKED` / `SKIPPED_RATE_LIMIT` / `ERROR`.
- Лог в `IMPORT_LOG_SALES_RETURNS` (тот же расширенный контракт `IMPORT_LOG_SALES_HEADERS_`): `load_id` префикс `SALE_RECON_`, `period_from/period_to` = окно, `rows_imported == rows_written`, **`watermark_before == watermark_after`** (авто-доказательство, что пересверка watermark не двигает). **`gaps_filled` (отсутствующие состояния) фиксируется ДО append, а `rows_written` подтверждается ТОЛЬКО после успешного `appendSalesRows_`** — при падении append: `status=ERROR, gaps_filled=N, rows_written=0` (запись не подтверждена); при успехе `gaps_filled == rows_written`. ⚠️ **Оговорка по семантике:** поле `rows_after_boundary_dedup` в контексте reconcile означает «строк после range-wide state-dedup» (в hourly — после граничного дедупа); контракт колонок не меняли ради совместимости журнала.
- `wbSalesReconcileStatus()` — окно/fromLcd, watermark hourly, sink, размер набора состояний, last Sales API attempt.
- Идемпотентная установка ночного триггера `wbSalesReconcileInstallNightlyTrigger()` — `everyDays(1).atHour(4).nearMinute(20)` (≈ **04:20 МСК**: снижает шанс близости к hourly; сам guard дополнительно страхует от 429); `0→создать 1; 1→ничего; 2+→дедуп до 1`. `wbSalesReconcileRemoveNightlyTrigger()`. Трогают ТОЛЬКО обработчик `runWbSalesNightReconcile` (hourly/Orders/Finance/Ads не затрагиваются). Требует часовой пояс проекта Apps Script = `Europe/Moscow`. Триггер ставит владелец после ручной приёмки.

`WbSalesReturnsBigQuery.gs`: добавлен `wbSalesBqStateKeysSince_(fromLcd)` — DISTINCT `sale_id, TO_HEX(MD5(raw_json))` где `last_change_date >= fromLcd` (fail-safe фильтры `source_api`/`sale_id`/`raw_json`, экранирование литерала). Отличие от `wbSalesBqBoundaryStateKeys_`: диапазон `>=` за окно, а не равенство границе.

`WbSalesIncremental.gs` (минимальная врезка в принятый D2c): перед `fetchSalesApiData_` добавлен вызов общего `wbSalesApiAcquireRequestSlot_()`; при отказе — `SKIPPED_RATE_LIMIT`, watermark не тронут (следующий час догонит). Доп-статус отражён в шапке файла.

НЕ трогалось: RAW-схема/`V_WB_SALES_RETURNS`, consumer adapter (D2b), Finance, Ads, PNL. `node --check` пройден. Apps Script/триггер — после PR/merge и ручной приёмки (acceptance R1–R6: первый прогон → `OK_NO_GAPS`/малый `gaps_filled`, `watermark_before==after`; повтор после 65 сек → `OK_NO_GAPS`, RAW не растёт; guard <65 сек → `SKIPPED_RATE_LIMIT` без HTTP; sink OFF → ERROR; общий lock → `SKIPPED_LOCKED`; установщик триггера 0→1/1→1/2+→1).

### 2026-07-13 — Фаза D2c: watermark-инкремент продаж + hourly-триггер (ветка phase-d2c-sales-watermark)

Автоматизация уже принятого D2a: продажи/возвраты дособираются ежечасно по образцу боевого D1.2 (заказы). Bootstrap-якорь (контрольная цифра, не хардкод): `MAX(last_change_date)` в RAW = `2026-07-12T20:14:45` (сверено коннектором: 3395 API-строк, 0 пустых, 0 вне формата, min `2026-04-13T07:03:54`). Sales хранит `last_change_date` уже с `T` → на границе сравниваем напрямую (без замены пробела, в отличие от Orders).

Новый файл `WbSalesIncremental.gs`:
- `WB_SALES_LAST_CHANGE_WATERMARK` (Script Property); `salesValidWatermark_` (строгий `YYYY-MM-DDThh:mm:ss[.frac]` + календарная корректность).
- `wbSalesIncrementalBootstrap()` — из `MAX(last_change_date)` RAW под ScriptLock; **не перезаписывает** существующее, пустой RAW/битый формат → ERROR (без fallback).
- `runWbSalesIncremental()` + ядро `wbSalesIncrementalCore_()` — весь цикл под одним `ScriptLock` (`tryLock`→`finally releaseLock`); параллельный запуск = **SKIPPED_LOCKED** (не ошибка данных, watermark не трогается). Требует `WB_SALES_BQ_SINK=ON` и валидный watermark. Один fail-closed запрос `fetchSalesApiData_(token, dateFrom=watermark)` (1 req/min, без пагинации/retry); `noWindow` (без фильтра `sale_dt`).
- Порядок безопасности (правки по 2-му статическому аудиту): (1) **валидация ВСЕХ сырых API-строк ДО нормализации** — `saleID` непустой, `date` валидна, `lastChangeDate` валиден; хоть одна плохая строка → ERROR с её номером, пакет отклонён (нормализатор молча роняет строки без даты `if(!day)continue` — валидация после него пропустила бы дефект и сдвинула watermark); страховка `rows.length === data.length` после нормализации. (2) `candidate` = max `lastChangeDate` пакета, строго валиден и `≥ watermark_before` (иначе ERROR). (3) **STATE-ключ вместо `row_hash`:** граница/дедуп по `sale_id | md5(raw_json)` (полное состояние). `row_hash` считается лишь из `srid/nmId/sale_dt/sale_id/operation_type` и НЕ меняется при изменении цены/склада/скидки → давал бы ложный дубль. MD5 согласован (Apps Script `salesMd5_` lowercase hex ↔ BQ `TO_HEX(MD5(raw_json))` над тем же `raw_json`). (4) **внутрипакетный last-wins по `sale_id`** (max `lastChangeDate`, tie-break по state-hash) вместо слабого `seen[row_hash]`, который схлопывал разные состояния и оставлял первое. Граница: дописываются строки `> watermark` и граничные (`== watermark`) с новым `sale_id|state`, которого ещё нет в RAW. Append → `rows_written`; watermark двигаем **только** если `candidate > watermark_before` и только после успешного append.
- **Контракт гарантий (точный at-least-once) — две РАЗНЫЕ гарантии:**
  - *Граница watermark:* строки `last_change_date == watermark` защищены state-ключом `sale_id|md5(raw_json)` — уже сохранённое состояние повторно не пишется, новое состояние с тем же timestamp не теряется. Здесь state-key даёт физическую идемпотентность.
  - *Ошибка после append:* при сбое после успешного append (напр. `setProperty` упал) RAW уже дополнен, watermark прежний. На повторе строки СТРОГО новее watermark (`> wm`) будут append-нуты СНОВА (в append-only RAW дубли допустимы). Каноническая идемпотентность — во `V_WB_SALES_RETURNS` (last-wins по `sale_id`); успешный повтор двигает watermark. Range-wide дедуп RAW сознательно НЕ делаем (вью уже канонична). Неверная формулировка «те же строки отсекутся по state-key» убрана — state-key отсекает только граничные (`== watermark`).
- **VIEW tie-break (C2):** в `ROW_NUMBER() OVER(PARTITION BY sale_id ORDER BY …)` добавлен финальный `TO_HEX(MD5(raw_json)) DESC` — при равных `last_change_date/loaded_at/load_id` выбор состояния детерминирован и согласован с внутрипакетным last-wins.
- **Boundary query самодостаточен (C3):** `wbSalesBqBoundaryStateKeys_` фильтрует `sale_id IS NOT NULL AND TRIM(sale_id)<>'' AND raw_json IS NOT NULL` (fail-safe к старым/ручным строкам).
- Статусы: `OK` / `OK_NO_CHANGES` / `SKIPPED_LOCKED` / `ERROR` (искусственный PARTIAL не вводим; упор в лимит строк ответа → ERROR, ничего не пишем).
- `wbSalesIncrementalStatus()` — watermark, sink, RAW `MAX(last_change_date)`, `V_WB_SALES_RETURNS` count.
- Идемпотентная установка триггера `wbSalesIncrementalInstallHourlyTrigger()` (0→создать 1; 1→ничего; 2+→удалить дубли, оставить 1) и `wbSalesIncrementalRemoveTrigger()` — трогают ТОЛЬКО обработчик `runWbSalesIncremental` (Orders/Finance/Ads не затрагиваются). Триггер ставит владелец после ручной приёмки.

`WbSalesReturnsBigQuery.gs`: добавлены `wbSalesBqMaxLastChange_()` (MAX last_change_date среди API-строк) и `wbSalesBqBoundaryStateKeys_(watermark)` (`sale_id|TO_HEX(MD5(raw_json))` на границе). `WbSalesReturnsLoader`: `IMPORT_LOG_SALES_HEADERS_` расширен (аддитивно) полями `watermark_before/after`, `api_rows_received`, `rows_after_boundary_dedup`, `rows_written`, `duration_ms`. RAW-схема НЕ менялась (state-hash считается на лету из существующего `raw_json`).

Ключ дедупа состояния — `sale_id | md5(raw_json)`; финальное состояние — во `V_WB_SALES_RETURNS` (last-wins по `sale_id`). Вью получила лишь **финальный детерминированный tie-break** (C2, поведение при неравных ключах не меняется) — нужно один раз пересоздать `wbSalesBqCreateViews()`. НЕ трогалось: consumer adapter (D2b), RAW-схема, Finance, Ads, PNL. `node --check` трёх файлов пройден. Триггер и Apps Script — после PR/merge и ручной приёмки: пересоздать вью → `wbSalesIncrementalBootstrap()` → прогон1 → прогон2 → fail-closed → `wbSalesIncrementalInstallHourlyTrigger()`.

### 2026-07-13 — Read-only аудит ёмкости книги (ветка diag/workbook-cell-audit)

D2b принят: parity зелёный (SHEET 2813 = BQ 2813, keys=933, все нули), флаг `BIGQUERY`, `buildCleanWbDaily()` собран из `V_WB_SALES_RETURNS` (3319 продаж, 0 ошибок). `buildDashboardWb()` упал с `dashRender_:247 — количество ячеек в книге превысит 10 000 000` — это **лимит Google Sheets**, не дефект D2b (произошло уже ПОСЛЕ успешного чтения из BQ). Google Drive-коннектор: книга 13.8 МБ, но `read_file_content` для книги такого размера возвращает пусто и не видит аллокацию сетки (пустые ячейки), которая и упирается в лимит.

Добавлен новый файл `WbWorkbookAudit.gs` — read-only `wbWorkbookCellAudit()` (ничего не пишет/не удаляет): по каждому листу logs сетку `maxRows×maxColumns`, использованный `lastRow×lastCol` и «пустой резерв» (сетка−использовано), сортировка по сетке; итог по книге в % от 10 млн и остаток.

**Результат аудита (41 лист):** книга на **100%** — сетка 9 997 687 / 10 000 000, свободно 2 313 ячеек (потому и упал дашборд). Крупнейший потребитель — `RAW_WB_FINANCE` 6 676 946 (67%, 90228×74, заполнен целиком → структурно уходит только в Фазе E после подтверждения, что PNL читает BQ). **Пустой резерв по книге = 1 628 512 ячеек** (сетка−заполнено) — убирается без потери данных. Отдельный кандидат на удаление — лист-бэкап `BACKUP_PR12_RAW_WB_FINANCE_20260617_163216` (564 916). Вывод: освобождение ячеек — предпосылка и для UNIT/PNL (PNL_TOTAL сейчас пуст, книга у потолка), не только для дашборда.

Добавлена guarded `wbWorkbookTrimEmptyReserve_(opts)` — срезает пустой резерв (строки ниже lastRow / столбцы правее lastCol, запас `margin=2`), в данные не заходит, per-sheet `try/catch`, **по умолчанию dry-run** (только лог). Реальное применение — `{dryRun:false}`; опция `skip:[...]`. Порядок: (1) удалить лист-бэкап + `wbWorkbookTrimEmptyReserve_({dryRun:false})` → ~2.1 млн ячеек (книга ~78%); (2) прогнать UNIT/PNL на BIGQUERY; (3) `buildDashboardWb()`. Долгосрочно Dashboard → D5 (web-app на MART), финансовый RAW из Sheets → Фаза E.

### 2026-07-13 — D2b: parity ограничен честной базой (source_api + boundary) (ветка fix/d2b-diag-date-normalization)

Архитектурное решение владельца: **legacy-import ранних продаж не делаем**. Оперативная история Sales API начинается с **2026-04-13** (нижний край сплошного покрытия BQ), полная финансовая история — в модуле Finance (с 2024-09-05, 201 211 строк). 522 API-строки листа за 01–12.04 — случайный неполный хвост (нет января/февраля/марта/2025/2024), их перенос не делает историю полной и не нужен. 10 строк `source_api=TEST` — не реальные события WB API. 90-дн. лимит — только на ПОЛУЧЕНИЕ из WB; уже сохранённое в BQ не исчезает, история накапливается вперёд через watermark+перехлёст+append+дедуп во VIEW (D2c).

`wbSalesParityAggregate_` теперь считает **честную общую базу**: пропускает `source_api !== 'WB_API_SALES'` (тест/legacy) и строки `sale_dt < WB_SALES_BQ_BOUNDARY_` (2026-04-13); `is_duplicate`-пропуск и авторитетный `is_return` сохранены; добавлен счётчик `rows`. `wbSalesConsumerParityTest_` вызывает агрегат с boundary и логирует фильтр (`source_api=WB_API_SALES AND sale_dt >= 2026-04-13`), печатает `SHEET/BQ API rows>=boundary`. Константа `WB_SALES_BQ_BOUNDARY_` вынесена к остальным константам вверху файла. Меняется только `wbSalesConsumerSource.gs` (функции parity), reader-контракт и потребители не тронуты.

Ожидание acceptance: SHEET API rows>=boundary = 2813, BQ = 2813, `quantity/money/missing = 0`. Сверено коннектором: `V_WB_SALES_RETURNS` в `[2026-04-13, 2026-06-23]` = **2813 строк, 21 SKU, 0 возвратов**. После зелёного parity → cutover (`wbSalesConsumerUseBigQuery()` → `buildCleanWbDaily()`/`buildDashboardWb()` → UNIT/PNL) → D2c.

### 2026-07-13 — D2b диагностика hotfix: нормализация дат (ветка fix/d2b-diag-date-normalization)

Первый прогон `wbSalesConsumerSourceClassification()` дал ложный `missing unique=0` и пустые бакеты: `getValues()` отдаёт `sale_dt` из листа как JS `Date`, а `String(date).substring(0,10)` = «Sat May 23», что не проходит `^\d{4}-\d{2}-\d{2}$` → все 3345 строк пропускались (дамп `EVT-HC-BODY-300` работал, т.к. без фильтра по дате). Бизнес-факт при этом получен: BODY-300 — `source_api=TEST`, `sale_id` пустой, `last_change_date` пустой, `operation_type=Продажа`, `quantity=2` → сценарий C подтверждён (тестовый/legacy-хвост, не запись WB Sales API).

Фикс только в `wbSalesConsumerSourceClassification()`/`wbSalesDiagDumpRow_`: `sale_dt` и `last_change_date` нормализуются через существующий `wbSalesDateStr_()` (Date → `yyyy-MM-ddTHH:mm:ss`), день = первые 10 символов. Дамп теперь печатает нормализованные даты. Production-reader/parity/flag не тронуты, поведение адаптера не менялось. `WB_SALES_CONSUMER_SOURCE=SHEET`, cutover запрещён. После фикса повторный запуск `wbSalesConsumerSourceClassification()` даст реальные бакеты Период×source_api и долю ранних API-строк vs non-API хвоста.

### 2026-07-13 — D2b диагностика: классификация источников листа (read-only, ветка diag/d2b-sales-source-classification)

Первый runtime-parity после legacy-hotfix показал: гипотеза дублей НЕ подтвердилась (`duplicate rows total=0`), расхождение имеет иную природу. Доказано по BigQuery-коннектору (read-only): BQ RAW = **100% `source_api=WB_API_SALES`** (3395 строк); сплошное покрытие начинается с **2026-04-13** (03-30 = 1 строка `noWindow`-артефакт, 31.03–12.04 = 0); `EVT-HC-BODY-300`/`nmId=252442341` отсутствует в BQ полностью. Лист (3345 строк, 01.04–23.06) содержит раннюю историю (01–12.04, вне backfill и уже вне 90-дн. retention WB API) и строки вида `operation_type=Продажа`/`quantity=2` — вероятные legacy/не-API записи. Корневой разрыв: вью фильтрует `source_api='WB_API_SALES'`, а SHEET-reader/parity — нет (старый `DashboardWb` не-API строки отбрасывал).

Добавлена **только read-only** функция `wbSalesConsumerSourceClassification()` (production-reader/parity/flag НЕ трогает; читает сырой лист напрямую — нужен `sale_id`, которого нет в каноническом контракте). Выводит в лог: бакеты Период(до/с `2026-04-13`) × source_api(`WB_API_SALES`/пусто/иной) с rows/uniq keys/qty/amountKop/мин-макс `sale_dt`/`last_change_date`; агрегаты missing-ключей (до/с boundary, с API/empty/other-строкой); построчный дамп missing (предел 300) и все строки `EVT-HC-BODY-300` с полями `sale_id/source_api/last_change_date/vendor/barcode/oper/qty/fin`. Цель — расклассифицировать 141 missing-ключ для выбора: (1) одноразовый legacy-import подтверждённых API-строк в BQ (`RAW_WB_SALES_RETURNS_LEGACY`), (2) правила для не-API строк, (3) canonical view с provenance (`record_origin`), (4) финальный parity и cutover. Production-cutover запрещён, `WB_SALES_CONSUMER_SOURCE=SHEET`, historical boundary пока не внедряется.

### 2026-07-13 — D2b hotfix: legacy-семантика продаж в parity (ветка fix/d2b-sales-parity-legacy-semantics)

Follow-up после первого runtime-parity: он корректно провалился и остановил cutover. Диагноз подтверждён по BigQuery (`RAW_WB_SALES_RETURNS`/`V_WB_SALES_RETURNS`): по спорным ключам `raw_rows = uniq_sale_id = view`, `missing_key=0` — вью НЕ теряет валидных продаж и не схлопывает их. Лишние строки — на стороне замороженного листа: у него есть колонка `is_duplicate` (loader D2a её сохранял), которой во вью нет концептуально. Прежний SHEET-reader безусловно ставил `is_duplicate=false`/`quantity=1`, из-за чего legacy-дубли (их старый `DashboardWb` исключал по `is_duplicate`) считались продажами → лист давал больше строк на более коротком периоде (SHEET 3345 строк 2026-04-01…06-23 vs BQ 3319 строк 2026-03-30…07-12; примеры: HAND-300 05-23 лист 9 / BQ 8, CHERRY-300 05-23 лист 3 / BQ 2).

Scope только D2b (loader/RAW/VIEW/watermark/триггеры не трогаются). Меняется только `WbSalesConsumerSource.gs`; `DashboardWb.gs`/`Cleanwbdaily` — без изменений. `node --check` пройден.

- **SHEET-reader — реальная legacy-семантика:** `wbSalesReadSheetCanonical_` ищет колонки `quantity`(`quantity`/`qty`) и `is_duplicate`(`is_duplicate`/`isduplicate`) и подставляет их нормализованные значения; синтез `1`/`false` — только при отсутствии колонки. BQ-reader без изменений (во вью полей нет → `quantity=1`, `is_duplicate=false` корректны для дедуп last-state).
- **parity исправлен содержательно:** `wbSalesParityAggregate_` пропускает строки `is_duplicate === true` (сравнение «лист без дублей» vs BQ; на BQ-стороне фильтр безвреден), `qty = Math.abs(quantity) || 1`, возврат — по авторитетному каноническому `is_return` (без повторной деривации из `operation_type`).
- **публичные обёртки (репозиторий = Apps Script):** `wbSalesConsumerParityTest()` → `wbSalesConsumerParityTest_()`; диагностика публична без завершающего `_`.
- **read-only диагностика `wbSalesConsumerParityDiagnostics()`** (флаг/данные не трогает, только console.log): наличие колонок `is_duplicate`/`quantity`, всего дублей и дублей в overlap, non-dup строки, BQ-строки; missing/qty/money **до и после** исключения дублей; до 20 спорных ключей с разбивкой листа raw/dup/non-dup vs BQ; явный дамп исходных строк `EVT-HC-BODY-300` в overlap (для проверки, все ли они дубли).

Acceptance hotfix (флаг остаётся `SHEET`): `wbSalesConsumerParityDiagnostics()` → анализ BODY-300 → `wbSalesConsumerParityTest()`. Ожидание после фикса: `keys>0`, `quantity mismatch=0`, `money mismatch=0`, `missing keys=0`. Если после исключения дублей остаётся только BODY-300 — разобрать его строки: если все дубли → ок; если нет — отдельно выяснить пробел в BQ, cutover не выполнять до объяснения каждой разницы. `WB_SALES_CONSUMER_SOURCE=BIGQUERY` не включаем до зелёного parity.

### 2026-07-13 — Фаза D2b: cutover потребителей продаж на BigQuery (вариант B, код до acceptance)

Снятие полу-переключённого состояния D2a: `WB_SALES_BQ_SINK=ON` пишет в BigQuery, но `DashboardWb`/`Cleanwbdaily` читали замороженный лист `RAW_WB_SALES_RETURNS`. Выбран **вариант B** — прямой cutover потребителей на вью через ЕДИНЫЙ адаптер с feature-flag и мгновенным откатом. Мост BQ→лист (A) и dual-write (C) отклонены. Ветка `phase-d2b-sales-consumer-cutover`; `node --check` трёх файлов пройден; commit/push — за владельцем. Флаг по умолчанию `SHEET` до parity и ручной приёмки.

Границы D2b (НЕ трогалось): loader D2a, watermark, триггеры, RAW-схема, SQL вью `V_WB_SALES_RETURNS`, finance, ads, MART, PNL-формулы, общий `bqQuery_`.

Новый файл `WbSalesConsumerSource.gs`:
- feature-flag `WB_SALES_CONSUMER_SOURCE = SHEET | BIGQUERY` (Script Property, **default SHEET**). Функции `wbSalesConsumerUseBigQuery()`/`wbSalesConsumerUseSheet()`/`wbSalesConsumerSourceStatus()`.
- `readCanonicalSalesRows_({fromDate,toDate,allowEmpty})` — единственный слой чтения. Возвращает 2D `[header, ...rows]` (форма `getValues()`), потребители используют именные пикеры без изменений.
- **Единый нормализованный 12-колоночный контракт для ОБОИХ источников** (одинаковые логические типы): `sale_dt`/`last_change_date` string `yyyy-MM-ddTHH:mm:ss`; `internal_sku`/`wb_nm_id`/`wb_vendor_code`/`barcode`/`operation_type`/`source_api` string (**`wb_nm_id` — строка**, INT64 может выходить за Number); `finished_price`/`quantity` number; `is_return`/`is_duplicate` boolean. `quantity=1` и `is_duplicate=false` синтезируются централизованно в обоих режимах (в вью/листе их нет).
- **BIGQUERY:** явный SELECT только нужных полей в каноническом порядке + `1 AS quantity`, `FALSE AS is_duplicate`; обязательный partition-filter `WHERE _sale_date >= DATE '<from>'`; детерминированный `ORDER BY sale_dt, sale_id`. Дата — **валидированный литерал** (regex `^\d{4}-\d{2}-\d{2}$`), НЕ query-параметр (`bqQuery_` не расширялся).
- **Нижняя граница истории — постоянная стартовая дата** `WB_SALES_CONSUMER_FROM` (Script Property, fallback константа `2024-09-01`), НЕ скользящее окно — ранняя история не исчезает из пересчитываемых витрин.
- **FAIL-CLOSED:** в BIGQUERY-режиме ошибка запроса ИЛИ пустой результат при `allowEmpty=false` → исключение с явным префиксом `[SALES ADAPTER]`. Молчаливого отката на SHEET НЕТ. Чтение у потребителей идёт ДО очистки витрин → исключение прерывает сборку, данные целы. `allowEmpty=true` — только для parity/узких проверок.
- `salesHeaderMapFromRow_()` — карта {имя→индекс} из канонической шапки для `Cleanwbdaily` (совместима с общим `findCol_`).
- `wbSalesConsumerParityTest_()` — read-only сверка SHEET vs BIGQUERY по date×SKU на **точной границе пересечения** `[max(min), min(max)]` (печатает `SHEET rows/min/max`, `BQ rows/min/max`, `comparison from/to`). Деньги — целые копейки `Math.round(x*100)`. Критерии приёмки: `quantity mismatch=0`, `money mismatch (копейки)=0`, `missing keys=0`.

Правки потребителей (минимальные, только блок чтения; downstream-логика без изменений):
- `DashboardWb.gs` (`dashBuildSpine_`): вместо `getSheetByName(DASH_SRC_SALES_)`+`getRange().getValues()` → `var sv = readCanonicalSalesRows_({allowEmpty:false})`.
- `Cleanwbdaily` (секция 3): вместо прямого листа+`getHeaderMap_(srSheet)`+`readSheetData_(srSheet)` → `readCanonicalSalesRows_({allowEmpty:false})`, шапка через `salesHeaderMapFromRow_(srValues[0])`, данные `srValues.slice(1)`.

Сверено коннектором на момент правок: `V_WB_SALES_RETURNS` = 3319 строк = 3319 уник. `sale_id`, возвратов 0, SKU 21, диапазон 2026-03-30…2026-07-12; `sale_dt`/`last_change_date` уже в формате `yyyy-MM-ddTHH:mm:ss`.

**Правки по статическому ревью (2026-07-13):**
- **feature flag fail-closed:** `wbSalesConsumerSource_()` возвращает SHEET только при отсутствии/пустом свойстве; любое иное значение (опечатка/повреждение) → исключение, а не молчаливый SHEET (иначе после cutover — внешне корректная устаревшая отчётность).
- **единый контракт fromDate/toDate:** SHEET-режим теперь тоже применяет границы (по первым 10 символам `sale_dt`, симметрично BQ); `from` по умолчанию = `2024-09-01`, не «вся история». Публичный контракт адаптера больше не источник-зависимый.
- **проверка формы ответа BQ:** перед маппингом каждой строки — контроль `Array.isArray(f) && f.length === 12`, иначе контролируемый `[SALES ADAPTER] Invalid BigQuery row shape` (вместо технического TypeError).
- **`Cleanwbdaily`:** каноническое `is_return` — первый источник истины при детекции возврата; `operation_type` и знак — fallback.

**Правки по 2-му раунду ревью (2026-07-13):**
- **`allowEmpty:false` симметричен и в SHEET:** пустой лист/отсутствие листа И пустой результат после фильтрации → исключение `[SALES ADAPTER] empty Sheet …` (раньше SHEET молча возвращал пустое → опасный rollback: `DashboardWb` перестроил бы витрину без продаж). Теперь единый контракт: production-чтение с `allowEmpty=false` не отдаёт пустые продажи ни в одном режиме.
- **`is_return` действительно авторитетно:** в `Cleanwbdaily` при наличии канонической колонки её значение решает полностью (явный `false` не переопределяется `operation_type`/знаком); fallback — только при отсутствии поля (легаси-лист без `is_return`).

Acceptance (порядок, флаг остаётся SHEET): `wbSalesConsumerSourceStatus()` → `wbSalesConsumerParityTest_()` зелёный (проверить ненулевое число comparison keys) → `wbSalesConsumerUseBigQuery()` → `buildCleanWbDaily()` → `buildDashboardWb()` → сверка UNIT/PNL → тест неправильного флага (ожидать исключение) → откат `wbSalesConsumerUseSheet()`. Флаг держать ≥1–2 недели. Отдельно: hotfix D2a `fix/d2a-bq-type-aliases` коммитится независимо.

### 2026-07-12 — Фаза D2a: продажи/возвраты в BigQuery (RAW+view, код до acceptance)

Порт `WbSalesReturnsLoader` в BigQuery по образцу заказов (Фаза D1). Флаг `WB_SALES_BQ_SINK` по умолчанию ВЫКЛ; листовое поведение при выключенном sink сохранено. Триггер, watermark-инкремент и cutover потребителей (DashboardWb/Cleanwbdaily/UNIT/PNL) в этот PR НЕ входят (D2b/D2c). `node --check` обоих файлов пройден в окружении ассистента (на машине владельца node не установлен — синтаксис перепроверяется отдельно).

**Эмпирика (2 read-only probe живого Sales API, 2026-05-28 и 2026-04-13):** 3319 строк/90д, `distinct saleID = distinct srid = raw_rows`, пустых `saleID/srid/lastChangeDate`=0, дублей `saleID`=0, конфликтов `saleID↔srid/nmId/date`=0, cap 80000 не достигнут, префиксы `S=3319/R=0`, `orderType` отсутствует, min `date` (2026-03-30) < `dateFrom` (2026-04-13) → фильтрация по потоку изменений. **Доказано: event_key = `saleID`** (заполнен 100%, уникален, стабилен, без версий). Возвраты у EVETIS ≈0 (в финотчёте 16 за ~22 мес) — return-ветка остаётся валидируемой на входе (постконтроль при первом реальном `R`).

Новый файл `WbSalesReturnsBigQuery.gs`:
- флаг `WB_SALES_BQ_SINK`; `wbSalesBqInitC0()` (C0 fail-closed: preflight→таблица→вью→счётчики, при ошибке sink гасится), `wbSalesBqEnable()/Disable()`, `wbSalesBqStats()` (RAW / MISSING_EVENT_KEY / уник. sale_id во вью), `wbSalesBqValidateViews()`.
- таблица `RAW_WB_SALES_RETURNS` — **типизированная** схема (STRING по умолчанию + `INT64`: raw_row_number/income_id/wb_nm_id; `NUMERIC`: total_price/discount_percent/spp/payment_sale_amount/price_with_disc/finished_price/for_pay; `BOOL`: is_supply/is_realization/is_return) + служебная `_sale_date DATE`. Партиция по `_sale_date`, кластер `sale_id, srid, wb_nm_id`. Аудит схемы типо-чувствительный (несовместимый тип → обрыв), строгая проверка партиции по `_sale_date`; `raw_json` (STRING) хранит оригинал ответа.
- `wbSalesBqAppendRows_` — приведение значений к типам колонки (пустые опускаются → NULL), `_sale_date` из `sale_dt[0:10]`, батч 2000.
- вью `V_WB_SALES_RETURNS` — дедуп **last-wins по `sale_id`**: `PARTITION BY sale_id ORDER BY SAFE_CAST(REPLACE(last_change_date,'T',' ') AS TIMESTAMP) DESC, loaded_at DESC, load_id DESC`; `WHERE source_api='WB_API_SALES' AND TRIM(sale_id)<>'' AND processed_status<>'MISSING_EVENT_KEY'`. Продажа и потенциальный возврат одного `srid` НЕ схлопываются (у них разные `sale_id`); `row_hash` в ключе НЕ используется.
- **Хотфикс (C0 в облаке, canonicalization типов):** первый `wbSalesBqInitC0()` создал `RAW_WB_SALES_RETURNS` корректно, но аудит схемы упал — BigQuery API отдаёт **канонические** имена типов (`INTEGER`/`BOOLEAN`/`FLOAT`/`RECORD`), а сравнение шло с SQL-алиасами (`INT64`/`BOOL`/…): `raw_row_number тип INTEGER, ожидался INT64`. Это один тип. Добавлен `wbSalesBqCanonicalType_()`; в `wbSalesBqEnsureTable_` обе проверки типов (колонки + `_sale_date`) сравнивают канонические формы, текст ошибки показывает исходный тип и canonical. Self-test `wbSalesBqTypeAliasSelfTest()` (без BQ). Таблица была создана пустой, данные НЕ писались, sink fail-closed выключился — повторный `wbSalesBqInitC0()` теперь принимает существующую таблицу, создаёт вью и завершает C0. View SQL / loader / поведение sink не менялись.

Изменения в `WbSalesReturnsLoader` (аддитивно, под флагом):
- константа `SALES_RAW_HEADERS_` (40 колонок, каноническая для BQ; `_sale_date` служебная, добавляется в append) и `WB_SALES_API_ROWS_CAP_=80000`.
- `normalizeSalesApiRows_`: добавлены `g_number, income_id, warehouse_type, payment_sale_amount, price_with_disc, is_supply, is_realization, raw_json`; **раздельные** `region_name`(regionName) и `oblast_okrug_name`(oblastOkrugName) — не смешивать (урок Orders); `operation_type` → `SALE`/`RETURN`; `processed_status='MISSING_EVENT_KEY'` при пустом `saleID`. Листовые поля `order_dt/quantity/is_duplicate` сохранены (в BQ-схеме отсутствуют, при sink игнорируются). `orderType` не добавлен (в контракте нет).
- **`noWindow` (критично, найдено на ревью):** `normalizeSalesApiRows_` получил параметр `opts.noWindow`. Sales API — change-feed по `lastChangeDate`, поэтому поздно изменённая старая продажа приходит с `sale_dt < dateFrom` (probe: `date` 2026-03-30 при `dateFrom` 2026-04-13). При sink ON нормализация вызывается с `{noWindow:true}` — локальный фильтр `sale_dt ∈ [from,to]` снят, весь change-feed сохраняется; при sink OFF (legacy sheet) прежнее окно по `sale_dt` сохранено. Контрольные суммы при sink считаются тем же `noWindow:true` (`aggregateSalesRowArray_(...,{noWindow:true})`), чтобы поздние изменения попадали в диагностику. Будущий watermark (D2c) — тоже всегда noWindow.
- Mock-проверка `wbSalesNoWindowSelfTest()` (без API/листов/BQ): строка `sale_dt=2026-03-30`, `lastChangeDate=2026-04-13T07:03:54`, `dateFrom=2026-04-13` → sink ON нормализуется и в sums (1/1), sink OFF отбрасывается (0).
- `fetchSalesApiData_` переписан: **пагинация снята**, один fail-closed запрос (rate limit Sales API 1 req/min; при объёме EVETIS весь диапазон в одном ответе). HTTP≠200/битый JSON/не-массив → `ERROR`; `arr.length>=80000` → `PARTIAL` (граница обрезана, ничего не пишем); пустой `[]` → `ok` с 0 строк.
- **rate limit (найдено на ревью):** `salesHttpGet_` теперь = **ровно один `UrlFetchApp.fetch` без retry** (`wbFetchWithRetry_` убран — при 1 req/min повтор через 20 сек снова словил бы 429 и жёг лимит выполнения). Удалены неиспользуемые константы `WB_SALES_API_MAX_PAGES_/PAGE_PAUSE_/RETRY_429_/RETRY_PAUSE_MS_` (осталась `ROLLING_DAYS_` для rolling-14). 429/5xx → `ERROR`, повтор — вручную оператором ≥65 сек. Комментарии секции FETCH обновлены (не «пагинация/429-backoff»).
- ядро `importWbSalesReturnsFromApiInternal_`: при `!ok` статус `PARTIAL`/`ERROR` (fail-closed, строки не пишутся); контрольные суммы при sink — из памяти (`aggregateSalesRowArray_`).
- sink-ветки: `getRawSalesSheet_` (заглушка `_bqSink`, `wbSalesBqEnsureTable_`), `buildSalesRawHeaderMap_` (hMap из `SALES_RAW_HEADERS_`), `clearSalesOwnPeriod_` (no-op при sink — append-only), `appendSalesRows_` (массивы→объекты→`wbSalesBqAppendRows_`).

Не включено (следующие фазы): **D2b** — cutover `DashboardWb`/`Cleanwbdaily` на `V_WB_SALES_RETURNS`; **D2c** — `WB_SALES_LAST_CHANGE_WATERMARK`, инкремент (граница `sale_id+row_hash`), hourly-триггер. Флаг `WB_SALES_BQ_SINK` включать вручную только на приёмке.

### 2026-07-12 — Фаза D1.2: watermark-инкремент заказов (код, до acceptance-прогонов)

Отдельная операционная семантика поверх period/backfill. Триггер НЕ создаётся, `runWbDailyRefresh` не трогается. `node --check` обоих файлов — ОК.

Новое (`WbOrdersLoader`):
- `WB_ORDERS_LAST_CHANGE_WATERMARK` (Script Property) — полное `lastChangeDate` в формате WB (RFC3339 с `T`).
- `wbOrdersIncrementalBootstrap()` — берёт `MAX(last_change_date)` из RAW (`source_api='WB_API_ORDERS'`), строго проверяет формат, восстанавливает `T` вместо первого пробела, записывает свойство **только если его нет** (существующее молча не перезаписывает; контрольное значение не хардкодится).
- `runWbOrdersIncremental()` (+ ядро `wbOrdersIncrementalCore_`) — под `LockService.getScriptLock()` на весь цикл; требует `WB_ORDERS_BQ_SINK=1` и валидный watermark; запрос по точному watermark `flag=0`; **без фильтра `order_dt`**; строгая валидация `srid`(trim)/даты/`lastChangeDate` каждой строки И `cursor_end` ДО любой ветки; at-least-once — watermark двигается ТОЛЬКО после полного успешного append; при `PARTIAL`/`ERROR` ничего не пишет и watermark не двигает; overlap арифметически не уменьшается.
- **Безопасная граница watermark (защита от потери строк при секундной точности):** no-change определяется НЕ только по timestamp. Помимо строк `lastChangeDate > watermark`, дописываются граничные строки (`== watermark`), пары `srid+row_hash` которых ещё нет в RAW (helper `wbOrdersBqBoundaryKeys_`). Итог: новая `srid` на уже зафиксированной временной точке НЕ теряется; watermark продвигается только при наличии строк строго новее (`candidate > watermark_before`); при только-граничных новых строках — они пишутся, watermark остаётся (боевого размножения дублей нет, т.к. на след. прогоне пары уже в RAW). `candidate < watermark_before` или невалидный `candidate` → `ERROR`.
- `wbOrdersIncrementalStatus()` — текущий watermark, состояние sink, `RAW MAX(last_change_date)`, `V_WB_ORDERS COUNT` (`wbOrdersBqViewCount_`).
- `ordersValidWatermark_` — строгий формат `YYYY-MM-DDTHH:mm:ss[.fraction]` (дата без времени и пробел отклоняются) **+ календарная корректность** (год 0001–9999, месяц 1–12, день с учётом високосного года, часы 0–23, мин/сек 0–59): отсекает синтаксически похожий мусор вроде `2026-99-99T99:99:99` и `0000-01-01T00:00:00`. Применяется к watermark_before, cursor_end, bootstrap и `lastChangeDate` каждой строки.
- **Инвариант пустого `cursor_end`:** пустой кандидат допустим ТОЛЬКО при настоящем пустом ответе (`data.length===0` и `api_rows_received===0`) → `OK_NO_CHANGES`; непустой пакет без `cursor_end` → `ERROR` (иначе строки писались бы, а watermark стоял → бесконечный повтор диапазона).
- **Диагностика `watermark_after`:** сразу после чтения/валидации watermark ставится `watermark_after = watermark_before`, поэтому при `PARTIAL`/`ERROR` после чтения лог показывает `before == after` (checkpoint не сдвинут), а не пустое `after`; при успешном продвижении перезаписывается на `candidate`.
- `wbOrdersIncrementalBootstrap` — под ScriptLock, требует `WB_ORDERS_BQ_SINK=1`; существующий watermark строго валидируется (валидный → `OK_EXISTS`, битый → `ERROR`, авто-перезаписи нет); записываемое значение проверяется строгим форматом.
- `api_rows_received` = реальная сумма строк ответа API (`fetchOrdersApiData_` считает до дедупа по srid), а не `data.length` после схлопывания. `load_id` (`ORD_INC_yyyyMMdd_HHmmss`) **сквозной** — создаётся в самом начале `runWbOrdersIncremental` (до lock/sink/watermark/API), присутствует в result при всех статусах (`OK`/`OK_NO_CHANGES`/`PARTIAL`/`ERROR`); в лог пишется `r.load_id` без fallback-константы.
- Статусы: `OK` / `OK_NO_CHANGES` / `PARTIAL` / `ERROR`. Поля результата: status, mode, load_id, watermark_before, watermark_candidate, watermark_after, pages_fetched, api_rows_received, rows_appended, unique_srid, started_at, finished_at, duration_ms, error_message.
- В меню «📦 Заказы WB» +3 пункта (bootstrap / инкремент / статус) для ручной приёмки. Rolling-14 по-прежнему помечен ⚠️ и на триггер не ставится.

Изменено аддитивно (поведение period/backfill НЕ меняется):
- `fetchOrdersApiData_` возвращает доп. поля `cursor_start`, `cursor_end` (точное `lastChangeDate` последней строки, формат WB), `pages`, `partial`, `reached_end`, `api_rows_received` (реальная сумма строк ответа до дедупа). HTTP/JSON-ошибка после ≥1 успешной страницы теперь классифицируется `PARTIAL` (на 1-й странице — `ERROR`).
- **Признак завершения при неподвижной границе исправлен (критично для инкремента):** при включительном `>=` WB всегда отдаёт граничную строку с тем же `lastChangeDate`, поэтому `nextCursor === cursor` — норма, а не обрыв. Решение теперь по лимиту строк, НЕ по `progress`: ниже лимита → штатный дренаж (`reachedEnd`, `ok:true`); на лимите → `PARTIAL` (граница могла обрезаться); непустой ответ без `cursor_end` → `PARTIAL`. Раньше `progress>0` на границе (в начале прогона `byKey` пуст → все граничные srid локально «новые») ошибочно давал `PARTIAL`, из-за чего второй прогон не доходил до `OK_NO_CHANGES`, а новая srid на граничной секунде — до `wbOrdersBqBoundaryKeys_`. Проверено через сам `fetchOrdersApiData_`: одна граничная строка / A+B на секунде → `ok:true`; хвост без `lastChangeDate` и 80000 строк на границе → `PARTIAL`.
- `normalizeOrdersApiRows_` получил параметр `opts.noWindow` (пропуск фильтра `order_dt` для инкремента); строка без даты заказа пропускается.
- `IMPORT_LOG_ORDERS_HEADERS_` 14 → 19: аддитивно `mode, watermark_before, watermark_after, pages_fetched, api_rows_received`. Первые 14 колонок не переставлены; `ensureImportLogOrdersSheet_` дописывает новые колонки существующему листу, исторические строки не трогает.

Новое (`WbOrdersBigQuery.gs`): `wbOrdersBqMaxLastChangeDate_()` — `MAX(last_change_date)` из RAW для bootstrap; `wbOrdersBqBoundaryKeys_(lastChangeStorage)` — DISTINCT `srid,row_hash` на границе для безопасной обработки граничных строк; `wbOrdersBqViewCount_()` — COUNT из `V_WB_ORDERS` для статуса.

Ограничение (документируется): fetch хранит только последнее наблюдённое состояние `srid` за прогон — D1.2 даёт *latest observed state per srid*, полный event log — это D1.1 (`raw_json`, отказ от fetch-схлопывания). Переход `is_cancel=false→true` в рабочей RAW искусственно НЕ провоцируем: до триггера — изолированный SQL/CTE-тест без записи, после запуска — постконтроль на естественной отмене.

### 2026-07-12 — Фаза D1: прогон C0 → C1 → backfill выполнен и проверен в облаке (migration/backfill ПРИНЯТ)

Документальная фиксация фактического прогона (код не менялся). Проверки — через BigQuery-коннектор в `project-fa311fc0-4d87-4781-986.wb_raw`.

**C0 `wbOrdersBqInit()` — успешно.** Создана `RAW_WB_ORDERS` (партиция `_order_date`, кластер `wb_nm_id, srid`), создана и подтверждена вью `V_WB_ORDERS`. Начальные счётчики 0/0.

**C1 за 2026-07-10 — успешно.** Импортировано 21 строка, 21 уникальный `srid`, 12 `nmId`, 0 отмен, 1 `nmId` не найден в SKU_MASTER. В облаке: RAW=21, VIEW=21; `last_change_date` заполнен и парсится в TIMESTAMP (диапазон 2026-07-10…2026-07-11 — изменение может приходить позже `order_dt`); `_order_date` у всех = 2026-07-10.

**Повторный C1 за тот же день — эмпирическое подтверждение ключа.** RAW=42, VIEW осталось 21, все 21 `srid` встретились ровно по два раза (2 `load_id`). Это подтверждает append-only модель и ключ дедупликации `srid` (RAW растёт, VIEW стабильна). ⚠️ Выбор нового состояния при переходе `is_cancel=false → true` пока НЕ наблюдался (в тестовых прогонах состояние не менялось) — проверяется на watermark-инкременте (D1.2).

**Backfill `importWbOrdersFromApi('2026-04-13','2026-07-12')` — успешно.** Период = **91 календарный день включительно**. Импортировано **3500 уникальных строк по `srid`** (товарные строки / единицы заказа; это НЕ 3500 уникальных покупательских заказов — связка по `gNumber` не проверялась). Одна страница ответа (3552 получено, слив по пустому массиву `[]`). В облаке: RAW=3542 (с учётом двух тестовых прогонов, 3 `load_id`); `V_WB_ORDERS`=3500 = число уникальных `srid`; отмен в VIEW=285; покрытие **91/91 день без пропусков**; пустых `srid`=0; непарсимых `last_change_date`=0; уникальных `nmId`=22. Флаг `WB_ORDERS_BQ_SINK` остаётся включён.

**Статус.** D1 принята как **миграция и историческая загрузка**. Операционный ежедневный инкремент ещё НЕ реализован; `importWbOrdersFromApiRolling14Days` на триггер ставить нельзя (фильтрует по `order_dt` → теряет позднюю отмену старого заказа). Следующий технический этап — **D1.2 watermark-инкремент**. Отдельным бэклогом остаётся **D1.1** (`raw_json` + полная схема, `regionName` вместо `oblastOkrugName`, мск-таймзона для аналитики).

### 2026-07-12 — Фаза D1: fail-closed на ошибке ответа WB + PARTIAL в лог (3-й аудит)

3-й аудит подтвердил корректность контракта WB и поймал fail-open блокер целостности.

Блокер (`fetchOrdersApiData_`, `WbOrdersLoader`):
- `JSON.parse` в `catch` возвращал `arr = []`, а пустой массив = штатный конец → повреждённый/не-массивный ответ WB (битый JSON, HTML-ошибка с HTTP 200, объект вместо массива, обрезанный ответ) принимался за **полную** выгрузку с `ok:true`. Исправлено: ошибка разбора → `{ok:false, partial: pages>0}`; ответ не `Array` → `{ok:false}`. Штатным концом остаётся ТОЛЬКО настоящий пустой массив `[]`.

PARTIAL в результат/лог (`importWbOrdersFromApiInternal_`):
- при `!fetched.ok` статус теперь `PARTIAL` (упор в лимит/курсор/битый JSON) vs `ERROR` (жёсткая), в существующей колонке `status` лога `IMPORT_LOG_ORDERS`; в `error_message` добавлено число полученных страниц; в result — поля `partial`, `pages_fetched`. При любом `!ok` строки НЕ записываются (fail-closed). Теперь фраза «увидишь PARTIAL» соответствует интерфейсу.

Синхронизированы 3 устаревших комментария (backfill «окнами» → одним проходом; «lastChangeDate не сохраняется» → сохраняется; «партиция не проверяется» → строгая проверка). `node --check` обоих файлов — ОК.

Отложено в **D1.1** (после успешного C1, отдельным решением — не раздувать патч): RAW не полностью «сырой» — нет `raw_json` (страховка от расширения схемы, разбор спорных строк без повторного запроса), `totalPrice`, `finishedPrice`, `discountPercent`, `spp`, `warehouseType`, `incomeID`, `isSupply`, `isRealization`; `region_name` сейчас заполнен `oblastOkrugName` (федеральный округ), тогда как WB отдаёт отдельно `regionName` (регион) — для витрины продаж по регионам семантически неверно; `last_change_date` хранится как мск-время (UTC+3) без зоны, `SAFE_CAST(... AS TIMESTAMP)` в BQ трактует как UTC — для дедупа неважно (сдвиг одинаков), но для аналитики абсолютное время на 3 ч неверно, приводить через мск-зону.

### 2026-07-12 — Фаза D1: исправление семантики пагинации/backfill заказов (2-й аудит)

Повторный внешний аудит подтвердил предыдущий hardening и выявил архитектурный дефект пагинации. По документации WB (`/api/v1/supplier/orders`, `flag=0`): `dateFrom` = `lastChangeDate`, возвращаются записи с `lastChangeDate >= dateFrom` (лимит ответа ~80 000 строк); пагинация — **полным значением `lastChangeDate` последней строки**; конец — **пустой массив `[]`**; параметра `dateTo` у эндпоинта НЕТ.

`fetchOrdersApiData_` (`WbOrdersLoader`):
- курсор пагинации теперь = точное `lastChangeDate` **последней строки** ответа (было: `max()` по странице с `replace('T',' ').substring(0,19)` — обрезка мс/формата расширяла границу и вызывала повтор страниц).
- сравнение версий last-wins по srid — на сырых строках `lastChangeDate` (без обрезки).
- признаки завершения приведены к контракту: пустой массив → конец; страница без новых/обновлённых srid **и ниже лимита строк** → дренаж; курсор не двигается при непустом ответе **или** упор в лимит страниц → `{ok:false, partial:true}` (не «красивый» OK). Введена `WB_ORDERS_API_ROWS_CAP_ = 80000` для отличия дренажа от упора в лимит строк.

Backfill (инструкция, не код): 90 дней делать **одним проходом** `importWbOrdersFromApi('<начало 90д>','<сегодня>')` — окна по `dateTo` объём ответа WB не уменьшают (эндпоинт всё равно отдаёт всё от `dateFrom` до «сейчас»), только многократно перетягивают историю и повышают риск упора в лимит. Client-side фильтр по `order_dt` в `normalizeOrdersApiRows_` оставлен — он и вырезает целевое окно.

Ежедневный инкремент (помечено в коде, реализация — до включения триггера): `importWbOrdersFromApiRolling14Days` фильтрует по `order_dt` — для инкремента НЕВЕРНО (поздняя отмена заказа старше окна не обновит состояние). Заменить на watermark-режим: `dateFrom` = последний обработанный `lastChangeDate`, без фильтра изменений по `order_dt` (дедуп по srid во вью выберет последнее состояние).

Строгая проверка партиции (`WbOrdersBigQuery.gs`): `wbOrdersBqEnsureTable_` у существующей таблицы теперь падает, если она не партиционирована по `_order_date` (patch колонку добавляет, но партицию не создаёт). Синхронизирован устаревший комментарий шапки (`loaded_at DESC` → `last_change_date`). Оба файла прошли `node --check`.

### 2026-07-12 — Фаза D1: hardening заказов по внешнему аудиту (до C0/C1)

Правки перед первым прогоном. Финансы/реклама/CLEAN/UNIT/PNL не затронуты.

Структура колонок (`WbOrdersLoader`):
- `ORDERS_RAW_HEADERS_`: 28 → **29 колонок**, добавлена `last_change_date` **в конец** (время изменения заказа на стороне WB). Легаси-лист RAW_WB_ORDERS физически имеет 28 колонок — append в конец её игнорирует (в sheet-режиме `set('last_change_date')` = no-op); в BQ схема патчится аддитивно. Порядок первых 28 колонок не тронут.

Устойчивый last-wins (`WbOrdersLoader`):
- `fetchOrdersApiData_` переписан: внутрипакетный дедуп по srid был **first-wins** (`if (!seenSrid[key]) all.push(o)`) → теперь **last-wins** (для каждой srid держим версию с максимальной `lastChangeDate`). Раньше поздняя версия одной srid отбрасывалась ещё до RAW — вью не могла бы это исправить.
- нормализация сохраняет `last_change_date` (`T`→пробел для чистого `SAFE_CAST` в BQ).

Полнота backfill (`WbOrdersLoader`):
- упор в лимит `WB_ORDERS_API_MAX_PAGES_` (30) больше не возвращает `ok:true`. Введён флаг `reachedEnd`, отличающий штатное завершение (пустой ответ / нет прогресса / курсор не двигается) от обрыва по лимиту → `{ok:false, partial:true}` с сообщением «сузьте окно». Иначе обрезанный хвост давал «красивый» OK.

Валидация периода (`WbOrdersLoader`):
- добавлена проверка `dateFrom <= dateTo` в ядре и в промпте меню.

Дедуп-вью (`WbOrdersBigQuery.gs`):
- `V_WB_ORDERS`: первичный ключ сортировки `SAFE_CAST(last_change_date AS TIMESTAMP) DESC`, затем `loaded_at DESC, load_id DESC` (tie-break). NULL last_change_date уходит вниз (штатно для BigQuery DESC).
- `wbOrdersBqEnsureTable_`: у существующей таблицы теперь аудируется и служебная `_order_date` (должна быть DATE; если нет — добавляется аддитивно). Раньше проверялись только STRING-колонки из headers → таблица могла существовать без партиционной колонки, append падал бы.

Отложено в бэклог (не блокеры D1): контрольные суммы прогона считаются по пакету, не по дедуп-вью — для acceptance брать `COUNT(*)`/`COUNT(DISTINCT srid)` из `V_WB_ORDERS` в облаке; `IMPORT_LOG_ORDERS` временно в Sheets (диагностика); сопоставление SKU при загрузке через `SKU_MASTER` (стратегически — в SQL-вью, чтобы правка справочника пересчитывала старые RAW). Оба файла прошли `node --check`.

### 2026-07-12 — Фаза D1: Orders → BigQuery (порт)

Аудит `WbOrdersLoader` и порт заказов в BigQuery по образцу рекламы (BigQuery-first).

Новый файл `WbOrdersBigQuery.gs`:
- флаг `WB_ORDERS_BQ_SINK`; `wbOrdersBqEnable()` с preflight (self-test+ensure dataset, fail-closed); `wbOrdersBqDisable()`; `wbOrdersBqInit()` — C0 без WB API (флаг+таблица+вью+счётчики, rollback флага при ошибке).
- `wbOrdersBqEnsureTable_()` — 404-aware + аддитивное расширение схемы; таблица `RAW_WB_ORDERS` = STRING-колонки + `_order_date DATE` (партиция по ДАТЕ ЗАКАЗА, кластер wb_nm_id/srid).
- `wbOrdersBqAppendRows_()` — append-only, `_order_date` из order_dt; batch 2000; allowlist только RAW_WB_ORDERS.
- `wbOrdersBqCreateViews()` — `V_WB_ORDERS`: дедуп по **srid, last-wins** (`ORDER BY loaded_at DESC`), фильтр source_api='WB_API_ORDERS'. Заказы мутируют (заказ→отмена) → нужно последнее состояние; row_hash как ключ НЕ годится (включает is_cancel → задвоение).
- `wbOrdersBqStats()`, `wbOrdersBqAssertViews_()`.

Правки под флагом в `WbOrdersLoader` (тяга/нормализация НЕ тронуты):
- добавлена константа `ORDERS_RAW_HEADERS_` (канонический порядок 28 колонок — при sink листа нет).
- `getRawOrdersSheet_` → при sink возвращает заглушку (`_bqSink`, getName, getLastColumn) и гарантирует BQ-таблицу; `buildOrdersRawHeaderMap_` → из константы; `clearOrdersOwnPeriod_` → no-op (append-only, дедуп во вью); `appendOrdersRows_` → массивы→объекты→BQ; контрольные суммы при sink считаются из памяти (`aggregateOrdersRowArray_`).

Запуск: C0 `wbOrdersBqInit()` (редактор) → C1 `importWbOrdersFromApi` за 1 день (меню «Заказы WB → за период…») → `wbOrdersBqStats()` → проверка в облаке (в т.ч. эмпирическая проверка ключа srid: COUNT vs COUNT DISTINCT srid, srid с меняющимся is_cancel) → backfill 90 дней окнами. ⚠️ Глубина заказов = потолок API ~90 дней. Лист `RAW_WB_ORDERS` остаётся legacy. Финансы/реклама/CLEAN/UNIT/PNL не затронуты.

### 2026-07-11

BigQuery migration — Phase C (реклама), hardening по внешнему аудиту перед запуском.

Что изменено (`apps-script/WbAdsBigQuery.gs`):
- `wbAdvBqEnsureTable_()` переписан: отличает 404 от прочих ошибок (убран «пустой catch»), для существующей таблицы делает аудит схемы и АДДИТИВНОЕ расширение (недостающие колонки → STRING NULLABLE через `Tables.patch`), обрывает запуск при несовместимом типе; выделены `wbAdvBqCreateTable_()` / `wbAdvBqAuditAndExtendSchema_()`;
- `wbAdsBqCreateViews()` теперь создаёт дедуп-вью для ВСЕХ 5 таблиц (было 2): `V_ADV_CAMPAIGNS` (ключ advertId), `V_ADV_CAMPAIGN_STATS` (date+advertId+nmId+appType+source_level, только `processed_status='raw'`), `V_ADV_BOOSTER_STATS` (date+advertId+nmId), `V_ADV_SEARCH_CLUSTERS` (period+связка+norm_query), `V_ADV_COSTS` (updNum, при пустом — составной ключ). Сортировка дедупа: `SAFE_CAST(load_ts AS TIMESTAMP) DESC, run_id DESC`;
- `wbAdsBqEnable()` — preflight (fail-closed): `getBqConfig_` + `bqEnsureDataset_` + `bqSelfTest` ДО установки флага;
- добавлены allowlist `WB_ADS_BQ_TABLES_` и `wbAdsBqAssertTable_()` (вызываются в ensure/append);
- `WB_ADS_BQ_BATCH_` 10000 → 1000 (payload NDJSON с крупным raw_json);
- `wbAdsBqStats()` больше не прячет реальные ошибки под «(нет таблицы)».

Что изменено (`apps-script/WbAdsRawLoader.gs`):
- UI-сообщение оркестратора теперь показывает верное назначение (BigQuery vs листы) при включённом sink;
- `RAW_WB_ADV_SEARCH_CLUSTERS` явно задокументирован как SAMPLE (первые N связок, без ротации) — не источник полноты;
- в `loadWbAdsRawPeriod()` добавлено предупреждение при периоде > 31 дня (backfill — помесячно).

Замечания аудита, ОТЛОЖЕННЫЕ в бэклог (не блокеры первого прогона):
- уникальность `updNum` в `RAW_WB_ADV_COSTS` проверить фактическим запросом (в один день по кампании возможно несколько операций) до утверждения ключа `V_ADV_COSTS`;
- отдельная `V_ADV_CAMPAIGN_NO_STATS` при необходимости (сейчас маркеры остаются только в RAW);
- партиция ingestion-time во вью не используется — для витрин фильтровать по бизнес-дате/`_PARTITIONDATE`.

Порядок запуска (лестница): C0 `wbAdsBqEnable()` (preflight) → C1 один день + `wbAdsBqStats()` + `wbAdsBqCreateViews()` → C2 7 дней → backfill ПОМЕСЯЧНО. Откат: `wbAdsBqDisable()`. Финконтур (RAW_WB_FINANCE/V_WB_FINANCE), CLEAN/UNIT/PNL и daily refresh не затронуты.

Правки по ВТОРОМУ раунду аудита (тот же день):
- **Блокер C0:** `wbAdsBqCreateViews()` падал бы на отсутствующей RAW-таблице (таблицы создаются лениво загрузчиками; при отсутствии кампаний 7/9/11 или связок advertId+nmId часть таблиц не появляется). Добавлен `wbAdsBqEnsureAllTables_()` (гарантирует 5 пустых таблиц из констант заголовков) — вызывается в начале `wbAdsBqCreateViews()`. Добавлен `wbAdsBqInit()` — настоящий C0 БЕЗ WB API (enable+ensure+views+stats).
- **Блокер `V_ADV_CAMPAIGNS`:** свежая строка `count_only` (временный сбой /adverts) вытесняла полноценную `raw` (название/товары/даты). В `makeView()` добавлен параметр `orderPrefix`; для кампаний приоритет `raw` (0) над `count_only` (1) перед сортировкой по load_ts.
- **Backfill по источникам:** инструкция в заголовке и handoff переписана — историю грузить НЕ общим оркестратором (он тратит бюджет на паузы search clusters до fullstats), а по источникам: `loadWbAdsCampaignsRaw()` один раз, `loadWbAdsCostsRaw` помесячно, `loadWbAdsFullstatsRaw` малыми окнами, clusters отдельно.
- Исправлена неверная формулировка: `wbAdsSplitPeriod_()` даёт СМЕЖНЫЕ НЕперекрывающиеся окна; причина PARTIAL — тайм-бюджет и rate-limit, не перекрытие.
- `load_ts` подтверждён: `wbAdsNow_()` → `'yyyy-MM-dd HH:mm:ss'` — валидный timestamp-литерал BigQuery, `SAFE_CAST(load_ts AS TIMESTAMP)` парсит корректно (проверить и на реальных строках в C1).
- Комментарий аудита схемы сужен: проверяются только колонки/типы, партиция и clustering — нет (бэклог).

Утилиты backfill по источникам (для C1/истории, 2026-07-12):
- `WbAdsRawLoader.gs`: `loadWbAdsCostsBackfill90()` (расходы 2026-04-13…2026-07-11, 90 завершённых дней), prompt-обёртки `loadWbAdsCostsRawPeriodPrompt()` и `loadWbAdsFullstatsRawPeriodPrompt()` — вызывают напрямую BQ-совместимые загрузчики (без replace-slice по листам).
- `Menu v2`: в «Реклама WB» добавлены пункты «RAW: только расходы за период…» и «RAW: только fullstats за период…».
- Примечание: пункт «fullstats за месяц» тоже пишет в BQ (внутри зовёт loadWbAdsFullstatsRaw), но перед загрузкой делает лишний deleteRows по старым листам — для backfill предпочтительны новые prompt-пункты «за период».
- Backfill не требует пересоздания вью: V_ADV_* читают живой RAW; `wbAdsBqCreateViews()` нужен только при изменении SQL вью.

Backfill рекламы 90 дней ЗАВЕРШЁН и проверен (2026-07-12):
- fullstats: покрытие 13.04–11.07 полное, 90/90 дней, пропусков нет; расход по SKU ≈ 395 170 ₽, 24 SKU, 7995 строк во `V_ADV_CAMPAIGN_STATS`. Грузили 14-дневными окнами (~200 сек каждое, OK).
- costs (upd): `V_ADV_COSTS` ≈ 425 106 ₽ за период. Сходимость с fullstats: costs на ~7,5% больше (вся реклама с баланса vs привязанное к SKU) — ожидаемо, связывает рекламу с удержанием 4,56 млн из финотчёта.
- campaigns: 426 уникальных. Осталось (низкий приоритет): search clusters (sample) — не грузили.

Результат C1 (первый реальный прогон, проверено в облаке 2026-07-12):
- Все 5 RAW-таблиц и 5 вью созданы; sink работает. Прогон был 7-дневный оркестратором (2 запуска: первый отменён после campaigns+costs, второй прошёл до fullstats PARTIAL).
- Дедуп подтверждён: CAMPAIGNS 852→426 (2×426), CAMPAIGN_STATS фильтр `raw` 256 против 363 no_stats-маркеров.
- `load_ts` (`'yyyy-MM-dd HH:mm:ss'`): `SAFE_CAST(... AS TIMESTAMP)` = 0 NULL — сортировка дедупа корректна.
- **НАЙДЕН и ИСПРАВЛЕН дефект `V_ADV_COSTS`:** `updNum` НЕ уникален (2 различных значения на 272 строки — это номер документа, общий для многих кампаний), ключ на updNum схлопывал 272→2. Заменено на `TO_HEX(SHA256(COALESCE(raw_json,'')))` → 154 строки (честная гранулярность, расход upd ≈ 38 324 ₽ за неделю). Живая вью пересоздана; код синхронизирован. updNum как ключ НЕ использовать.
- **fullstats PARTIAL** (пропущено 26 advertId по тайм-бюджету) — подтверждает: историю грузить ПО ИСТОЧНИКАМ малыми окнами, не оркестратором.

Правки по ТРЕТЬЕМУ раунду аудита (тот же день):
- **C0 fail-closed:** `wbAdsBqInit()` обёрнут в try/catch — при ошибке на любом шаге ПОСЛЕ включения флага (частичное создание таблиц/вью) вызывается `wbAdsBqDisable()` (rollback), чтобы загрузчик не писал в недоинициализированный контур.
- **C0 проверка вью:** добавлен `wbAdsBqAssertViews_()` — подтверждает, что все 5 объектов существуют и являются VIEW (не просто «вызов не бросил исключение»).
- **`V_ADV_COSTS` NULL-safe:** append пишет пустые значения как NULL, а `CONCAT` с NULL даёт NULL → разные операции схлопнулись бы в одну. Резервный ключ заменён на `TO_HEX(SHA256(COALESCE(raw_json,'')))` — сохраняет фактическую гранулярность до подтверждения уникальности `updNum` в C1.

### 2026-07-10

BigQuery migration — Phase A.

Что изменено:
- добавлен новый файл `apps-script/WbBigQuery.gs`;
- добавлен базовый слой доступа к BigQuery: конфиг через Script Properties, создание датасета, создание `RAW_WB_FINANCE`, batch load через `NEWLINE_DELIMITED_JSON`, SQL query helper;
- добавлен `bqSelfTest()` — безопасная проверка доступа через временную таблицу `_selftest`;
- добавлена документация `docs/bigquery_migration_phase_a.md`.

Правила безопасности:
- реальный GCP Project ID не коммитится в репозиторий;
- существующие Google Sheets RAW-листы и WB-загрузчики на этой фазе не меняются;
- `bqSelfTest()` удаляет только временную таблицу `_selftest`;
- `RAW_WB_FINANCE` создаётся только отдельным запуском `bqCreateFinanceTable()`.

Следующий шаг: после успешного `bqSelfTest()` перейти к Phase B — переносу финансового RAW/backfill в BigQuery.

### 2026-06-28

Фаза 0, шаг 1 — миграция финансов на API «Финансы»: файл-разведчик (только чтение).

Что изменено:
- добавлен новый файл `apps-script/WbFinanceApiV1.gs` — диагностический модуль БЕЗ записи в листы;
- `showWbFinanceV1ReportsList()` — POST `sales-reports/list`, выводит в лог фактические периоды всех доступных `reportId` (проверка реальной глубины по кабинету);
- `wbFinanceV1DetailedSample()` — POST `sales-reports/detailed/{reportId}`, выводит реальные имена полей (camelCase) и первую строку для проверки string-сумм;
- предусловие: токен категории «Финансы» в Script Property `WB_TOKEN_FINANCE`;
- база `finance-api.wildberries.ru`, пагинация detailed по `rrdid` (как в старом методе).

Что НЕ менялось:
- `RAW_WB_FINANCE` и любые другие листы (модуль только читает API и пишет в логи);
- старый загрузчик `Wbfinanceloader` (reportDetailByPeriod) — работает до 15.07.2026, выводим из использования позже;
- CLEAN/UNIT/PNL, заказы, продажи, реклама, остатки, хранение.

Подтверждено разведкой (29.06): `list` отдал 189 недельных отчётов с глубиной **2024-09-02 … 2026-06-21**; `detailed/{reportId}` отдаёт строки и за сентябрь 2024. Снята полная camelCase-схема полей.

Добавлен продакшн-загрузчик (в том же файле, переиспользует конвейер `Wbfinanceloader`):
- `wbFinV1AdaptRow_` — новые camelCase-поля → старые имена `FINANCE_API_FIELD_MAP_` + парсинг string-сумм в числа (логистика-деньги = `deliveryService`, не `deliveryAmount`);
- `wbFinV1FetchDetailedAll_` — пагинация detailed по `rrdId`;
- `wbFinV1ImportReport_` — запись одного reportId в `RAW_WB_FINANCE` через `normalizeFinanceApiRows_`, с **replace-slice ПО reportId** (`wbFinV1ClearOwnReport_` удаляет только строки `source_api=WB_API_FIN_V1` И `report_id=reportId`, затем пишет fresh — не по периоду, т.к. на неделю бывает несколько reportId);
- `wbFinanceV1ImportOneReportTest` — импорт ОДНОГО отчёта + контрольные суммы рядом с недельными итогами из `list` (самопроверка маппинга); UNMAPPED-диагностика по `sellerOperName`.

Правила безопасности: токен строго `WB_TOKEN_FINANCE` (без fallback); запись запрещена, если в RAW нет колонок `source_api`/`report_id` (иначе откат невозможен); `row_hash` детерминированный = `WB_API_FIN_V1|reportId|rrdId`; HTTP 204 в пагинации = штатное завершение. Запись additive и обратима (откат — удалить строки с `source_api=WB_API_FIN_V1`). Старый загрузчик не тронут.

Проверка на одном отчёте (757272781, 15–21.06.2026): наши detailed-суммы совпали с недельными итогами `list` точь-в-точь (forPay 4396.37, retail 4849.01, логистика 943.99), UNMAPPED нет.

Добавлен резюмируемый бэкфилл по всем reportId (сен 2024 → сейчас):
- `wbFinV1ListAll_`, `wbFinanceV1Backfill` (бюджет времени 4.5 мин, прогресс в Script Property `WB_FIN_V1_DONE` после каждого отчёта, запуск повторно до «ЗАВЕРШЁН»), `wbFinanceV1BackfillStatus`, `wbFinanceV1BackfillReset`.
- `wbFinV1ImportReport_` принимает готовый `skuIndex` (строится 1 раз на прогон).

Принятая стратегия источника финансов: **единый API**. После бэкфилла — сверка недель перекрытия Excel↔API, затем удаление Excel-строк (`source_api=DRIVE_XLSX_REPORT`) → один источник. Шаг сверки/удаления — отдельно. См. `docs/phase0_finance_migration_tz.md`.

### 2026-06-29 (исправление бэкфилла)

Прогон бэкфилла падал: per-report `deleteRow`-очистка по всему листу на разросшемся RAW давала `IllegalStateException` и «Exceeded maximum execution time» (один большой отчёт не укладывался в 6 мин).

Переписан `wbFinanceV1Backfill`: **один скан листа за прогон** (`wbFinV1BuildSeenRrdSet_`), дедуп строго по безопасному ключу **`reportId|rrdId`**, запись только новых строк, БЕЗ очистки по периоду. Это убирает таймаут, `IllegalStateException` и риск задвоения при повторных/прерванных запусках. `row_hash` теперь = `WB_API_FIN_V1|reportId|rrdId`. Добавлена read-only диагностика `wbFinanceV1CheckDuplicates` (строк/уник. reportId|rrdId/дубли/суммы). Старый `wbFinV1ImportReport_` (single-report тест с replace-slice по report_id) оставлен.

### 2026-06-24

Рекламный дашборд ADS_WB v1 (по API fullstats).

Что изменено:
- добавлен новый файл `AdsDashboardWb.gs` с функцией `buildAdsDashboardWb()` (зависит от хелперов `DashboardWb.gs`);
- лист `ADS_WB` (был пустой) наполняется: фильтр периода, итог по кабинету, разрез по каждому SKU, разрез по каждой кампании;
- источники только API: `RAW_WB_ADV_CAMPAIGN_STATS` (дедуп `date+advertId+nmId+appType`, last-row-wins, площадки суммируются), `RAW_WB_ADV_CAMPAIGNS` (название/статус/площадки из raw_json), `SKU_MASTER`;
- метрики: расход, показы, клики, CTR, CPC, корзины, заказы, CR, CPO, выручка с рекламы, ДРР; целевой ДРР и флаг «резать/усиливать/ок»;
- на таблице кампаний включён нативный фильтр (отбор по SKU кликом);
- формулы локаль-safe (через `dashArgSep_`);
- перед построением `ADS_WB` очищаются проверки данных по всему листу, чтобы старые validations в дальних скрытых колонках не блокировали запись служебного массива.

Что НЕ менялось:
- RAW-листы;
- CLEAN/UNIT/PNL;
- финансы, заказы, продажи, остатки, хранение;
- существующие загрузчики и DashboardWb.gs.

Контроль (весь период 01.04–23.06.2026): расход ≈ 380к (сверка с кабинетным «История затрат» 380 532).

### 2026-06-24

Новый управленческий дашборд DASHBOARD_WB только по API-данным.

Что изменено:
- добавлен новый файл `DashboardWb.gs` с функцией `buildDashboardWb()`;
- лист `DASHBOARD_WB` (был пустой) наполняется: фильтр периода (выпадающий список), итог по магазину, таблица по SKU, таблица по дням;
- источники строго API: `RAW_WB_ORDERS` (WB_API_ORDERS), `RAW_WB_SALES_RETURNS` (WB_API_SALES), `RAW_WB_ADV_CAMPAIGN_STATS` (fullstats, дедуп date+advertId+nmId), `SKU_MASTER`;
- расход рекламы дедуплицируется по `date+advertId+nmId`, при дубле берётся ПОСЛЕДНЯЯ строка (last row wins) — новые месячные прогоны fullstats лежат ниже старых недельных, поэтому перекрытие июньских прогонов не задваивается;
- служебный массив день×SKU пишется в скрытые колонки того же листа (расчётные данные, не RAW);
- формулы собираются под локаль таблицы: разделитель аргументов (`,`/`;`) определяется пробной формулой `dashArgSep_`, десятичные литералы убраны (`*0.6` → `*60/100`) — иначе в ru-локали все формулы давали `#ERROR!`;
- ключ дедупа рекламы расширен до `date+advertId+nmId+appType`: fullstats отдаёт расход по площадкам (appType 1/32/64) отдельными строками, их надо суммировать — без appType расход занижался (~130к вместо ~471к);
- «Цель ДРР» (число) и «Флаг» пишутся отдельными `setValue`/`setFormula`, а не в общий `setFormulas` с числом — иначе колонки J/K давали `#ERROR!`.

Что НЕ менялось:
- RAW-листы (заказы/продажи/реклама/финансы/остатки/хранение);
- CLEAN_WB_DAILY, UNIT_SKU_DAILY, PNL;
- Excel-реализации;
- меню (точка входа `addDashboardWbMenu` не подключается автоматически).

Контрольные суммы (весь период 01.04–23.06.2026): заказы 3558 шт / 3 098 389 ₽, выкупы 3335 / 1 746 992 ₽, отмены 276, реклама (дедуп) ≈ 470 904 ₽.

### 2026-06-24

Исправлен replace-slice в месячных рекламных RAW-загрузчиках.

Что изменено:
- в `WbAdsClustersJob.gs` replace-slice теперь удаляет строки через `deleteRows`, а не очищает ячейки через `clearContent`;
- в `WbAdsFullstatsMonth.gs` replace-slice также переведён на `deleteRows`;
- RAW-листы больше не получают пустые разрывы внутри данных;
- CSV/gviz-чтение больше не должно обрываться на первой пустой строке;
- данные других периодов и TEST-строки не трогаются.

Что НЕ менялось:
- структура RAW-листов;
- CLEAN/UNIT/PNL;
- финансы;
- заказы;
- продажи/возвраты;
- остатки;
- хранение.

### 2026-06-24

Месячный WB Ads fullstats с идемпотентным пересбором периода.

Что изменено:
- добавлен новый файл `WbAdsFullstatsMonth.gs`;
- добавлены точки входа fullstats за текущий месяц и выбранный период;
- перед сбором выполняется replace-slice по `period_from`/`period_to` для fullstats-строк;
- чистятся только строки `adv/v3/fullstats` и `no_stats` за выбранный период;
- данные других периодов и TEST-строки не трогаются;
- добавлены пункты меню в «🏷️ EVETIS WB → 📊 Реклама WB → 📈 fullstats за месяц».

Что НЕ менялось:
- существующий сборщик `loadWbAdsFullstatsRaw`;
- CLEAN/UNIT/PNL;
- финансы;
- заказы;
- продажи/возвраты;
- остатки;
- хранение;
- структура RAW-листов.

### 2026-06-24

Полная пакетная загрузка WB Ads search clusters за месяц.

Что изменено:
- добавлен новый загрузчик `WbAdsClustersJob.gs`;
- search clusters теперь можно собирать по всем advertId+nmId парам, а не только sample 20;
- добавлен служебный скрытый лист `_ADS_CLUSTERS_JOB` для хранения прогресса;
- добавлен cursor/progress: сбор можно продолжать пачками без упора в 6-минутный лимит Apps Script;
- добавлен replace-slice по `period_from`/`period_to` для `RAW_WB_ADV_SEARCH_CLUSTERS`, чтобы повторный сбор месяца не плодил дубли;
- добавлены пункты меню в «🏷️ EVETIS WB → 📊 Реклама WB → 🧩 Кластеры за месяц».

Что НЕ менялось:
- CLEAN/UNIT/PNL;
- финансы;
- заказы;
- продажи/возвраты;
- остатки;
- хранение;
- структура `RAW_WB_ADV_SEARCH_CLUSTERS`.

### 2026-06-23

Консолидация меню в единое «🏷️ EVETIS WB» (только UI, без бизнес-логики, листов и данных).

Что изменено:
- `onOpen()` (Menu v2) теперь строит ОДНО top-level меню с подменю: 💰 Финансы WB, 📦 Заказы WB, 💳 Продажи WB, 📦 Остатки WB, 📦 Хранение WB, 📊 Реклама WB, 📊 Расчёты, ⚙️ Обслуживание + быстрые пункты «🔧 Полная настройка», «🔄 Обновить WB API (WB Daily)», «🩺 Диагностика», «ℹ️ О системе»;
- убраны вызовы отдельных строителей top-level меню (`addWbDailyRefreshMenu`, `addWbOrdersLoaderMenu`, `addWbSalesReturnsLoaderMenu`, `addUnitSkuDailyMenu`, `addWbOperationalPilotMenu`, `addStorageFolderLoaderMenu`) — их пункты перенесены в подменю. Сами функции оставлены в файлах;
- меню «📦 Хранение WB» теперь доступно как подменю (раньше уезжало в «…» и было не видно);
- исправлено битое подменю «💰 Финансы WB»: прежние пункты ссылались на несуществующие функции (`previewWbFinanceApi`, `loadWbFinanceLastWeek`/`Period`/`FullHistory`/`HistoryChunked`, `auditRawWbFinance`); перевязано на реальные функции (`importWbFinanceFromApiRolling14Days`, `listWbFinanceReportsInDrive`, `buildCleanWbDailyWithFinance`, `buildMonthlyUnitReportWithFinance`, `auditRawWbFinanceColumns`, `verifyRawWbFinanceImport`).

Зачем:
- множество отдельных top-level меню переполняли панель Google Sheets и прятались в «…» (из-за этого кнопка хранения была недоступна);
- часть пунктов «Финансы WB» не работала (ссылки на отсутствующие функции).

Какие листы затронуты:
- никакие. Изменения только в построении меню. Все целевые функции существуют (проверено: 48 пунктов, 0 битых ссылок), синтаксис проверен `node --check`.

Как проверить:
- перезагрузить таблицу → в шапке одно меню «🏷️ EVETIS WB»; отдельных WB Daily/Заказы/Продажи/Хранение/Pilot/UNIT больше нет;
- открыть «📦 Хранение WB → 🔄 Обновить хранение WB» — запускается `updateWbStorageData`;
- кликнуть пункты «💰 Финансы WB» — ошибки «функция не найдена» больше нет.

### 2026-06-22

Техническая стабильность загрузчиков WB API (без изменения бизнес-логики, структуры листов и данных).

Что изменено:
- добавлен единый HTTP-helper `wbFetchWithRetry_` (+ разбор `Retry-After` в `wbRetryAfterMs_`) в `utils.gs`: ретраи на HTTP 429 и 5xx (500/502/503/504), экспоненциальный backoff с верхней границей, уважение заголовка `Retry-After`, ограничение числа повторов, логирование попыток без вывода токена;
- загрузчики переведены на общий helper: заказы (`fetchOrdersApiData_`), продажи/возвраты (`salesHttpGet_`), остатки (`stocksFetch429_` → делегирует helper), реклама (`wbAdsHttp_`), хранение (`Wbstoragefolderloader`: create/status/download). Прежние внешние контракты функций и форматы строк сохранены;
- устранён дубль глобальной функции `importWbStorageFromApiRolling7Days`: диагностическая копия в `Wbstorageapidiag` переименована в приватную `importWbStorageFromApiRolling7DaysDiag_`; боевая реализация в `Wbstoragefolderloader` и пункт меню не изменены;
- исправлен debug-lookup в `utils.gs` (`debugSkuMatching`): `skuIndex.byNmId` → `skuIndex.byNm` в соответствии с контрактом `buildSkuIndex_` (`byNm`/`byBarcode`/`byVendor`). Сам `buildSkuIndex_` не менялся.

Зачем:
- раньше обработка 429/5xx была непоследовательной (у заказов и рекламы ретраев почти не было) — заказы и реклама могли «тихо» недогружаться при лимитах WB;
- дубль имени функции в общем глобальном пространстве Apps Script приводил к тому, что одна реализация перетирала другую;
- debug-функция обращалась к несуществующему ключу индекса и всегда показывала «NOT FOUND».

Какие листы затронуты:
- никакие. Изменения только в коде загрузки/ретраев и отладки. Имена колонок, формат записываемых строк, логика replace-slice и `source_api` не менялись. RAW/CLEAN/UNIT/PNL и справочники не затрагиваются.

Как проверить:
- запустить «📦 Заказы WB → Обновить заказы (rolling 14)»: в логах — строки `[Orders] …` при лимитах; число строк `WB_API_ORDERS` за период не растёт при повторном прогоне (идемпотентность сохранена);
- прогнать загрузчики продаж/остатков/рекламы/хранения — данные пишутся как прежде, при 429/5xx в логах видны паузы и повторы;
- проверить, что пункт меню «🌐 API rolling 7 days» по-прежнему запускает боевую загрузку хранения.

### 2026-06-04

Создана базовая структура проекта EVETIS WB Analytics:
- добавлены проектные инструкции;
- описана архитектура;
- создана папка apps-script;
- создана папка skills;
- зафиксированы базовые правила работы с Claude и Codex.
