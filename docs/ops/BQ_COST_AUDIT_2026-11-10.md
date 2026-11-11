# Аудит чтений BigQuery и план экономии — 2026-11-10

Проект `project-fa311fc0-4d87-4781-986` («Evetis analytics»), общий платёжный аккаунт с другими проектами.
Этап 1: **только чтение и план.** Ни одно расписание, задание, вью или скрипт не изменены.
Каждый пункт плана (BQ-xx) требует отдельного ACK владельца.

Источник всех цифр — `region-eu.INFORMATION_SCHEMA.JOBS_BY_PROJECT` (окно 01.10–09.11.2026), если не сказано иное.
Цена — официальная страница cloud.google.com/bigquery/pricing (прочитана через Firecrawl 10.11; Perplexity недоступен — кончилась квота):
EU и europe-west1 — **$6,25 за ТиБ**, первый **1 ТиБ в месяц бесплатно на платёжный аккаунт**,
**минимум 10 МБ за каждую таблицу в запросе и 10 МБ за запрос**.

---

## 1. Что обнаружено

### 1.1. Объём Evetis завышен в исходной оценке в ~1,6 раза

Исходные цифры (6,58 ТиБ за октябрь, 2,06 ТиБ за 1–9 ноября) складывали родительское задание `SCRIPT` и его
дочерние шаги. Родитель показывает в `total_bytes_billed` **сумму байтов своих детей**: в октябре родительские
SCRIPT дают 2,423 ТиБ, дочерние — те же 2,423 ТиБ. Счёт выставляется один раз.

| Период | С дублем (как в исходной оценке) | **Реально оплачено** | Обработано фактически |
|---|---|---|---|
| Октябрь | 6,58 ТиБ | **4,15 ТиБ** (4 254 ГиБ, 123 тыс. заданий) | 1,55 ТиБ |
| 1–9 ноября | 2,06 ТиБ | **1,27 ТиБ** (~148 ГиБ/сутки) | — |

Сутки октября: минимум 97, среднее 139, максимум 254 ГиБ (11.10 — ручные сессии, 155 ГиБ за день).
Регионы US и europe-west1: единичные задания, < 0,1 ГиБ — не влияют.

### 1.2. 63 % оплаченного — минимальная тарификация, а не данные

Из 4 254 ГиБ, оплаченных в октябре, реально прочитано 1 588 ГиБ. Остальное — правило «10 МБ за каждую
таблицу в запросе». Вью раскрывается в базовые таблицы: `SELECT * FROM V_OPS_SHEET_BOM` трогает 21 таблицу →
210 МиБ к оплате при 2,3 МиБ данных. Поэтому главные рычаги — **реже запускать** и **трогать меньше таблиц
за запуск**. Партиционирование и выбор колонок помогают только там, где данные действительно большие
(сборки Executive/SKU V2, ~350 МиБ на шаг).

### 1.3. Деньги: Evetis — не главная причина счёта $77,49

При 4,15 ТиБ BigQuery Evetis за октябрь стоит не больше (4,15 − 1) × $6,25 = **$19,7** без НДС, даже если
бесплатный ТиБ целиком достаётся Evetis. Счёт без НДС — $77,49. Значит, **~$45–58 приходится на другое**:
Heldi (`mpa-t-client-001`), `mpa-platform` или прочие сервисы. Этот аудит их не видит: экспорта биллинга в
BigQuery нет, Budget API на проекте выключен, консоль биллинга во встроенном браузере потребовала входа.

**Вывод (не проверен по счёту):** даже при нулевом BigQuery Evetis уложиться в $40 с НДС можно, только если
остановлены или урезаны Heldi и `mpa-platform`. Текущий темп Evetis (~4,4 ТиБ/мес) при работе одного Evetis
даёт ≈ $21 за BigQuery + $10–12 за прочее ≈ $31–33 без НДС ≈ **$36–39 с НДС** — на самой границе.
План ниже даёт запас: ≈ $19–21 с НДС за весь аккаунт (если остальные проекты остановлены).

**Нужно от владельца:** разбивка счёта за октябрь по проекту и SKU (Billing → Reports → Group by Project, SKU),
либо войти в консоль во встроенном браузере — я сниму её сам, только чтением.

### 1.4. Кто тратит: источник → ГиБ

Темп ноября посчитан на 8,78 суток (09.11 проекты стояли с 18:38 UTC).

| Источник | Как запускается | Окт., ГиБ | Окт., задания | Окт., реально прочитано, ГиБ | Ноя., ГиБ/сутки | Доля окт. |
|---|---|---:|---:|---:|---:|---:|
| `sp_build_executive_v2_daily` | Scheduler `executive-v2-layer-build`, ежечасно 07–23 МСК (17/сут) | 1 022 | 12 121 | 909 | **34,0** | 24 % |
| Лист операций ФФ (`V_OPS_SHEET_*`, 8 вью) | Apps Script `opsTriggerRefresh`, `everyHours(1)` (24/сут), под личным аккаунтом владельца | 945 | 8 182 | 9 | **31,2** | 22 % |
| Детектор DRO (`sp_evaluate_data_health`) | Scheduler `dro-health-eval`, каждые 30 мин (48/сут) | 596 | 18 095 | 5 | **25,1** | 14 % |
| SKU Performance V2 | Scheduler `sku-performance-v2-layer-build`, ежечасно 07–23 (17/сут) | 347 | 9 587 | 225 | **14,3** | 8 % |
| Ручные сессии Claude/Codex (`purpose=…`, MCP) | вручную | 287 | 3 828 | 188 | 2,0 | 7 % |
| Apps Script загрузчики и прочее без метки (`INGEST_RUNS`, поставки, ручные без метки) | триггеры Apps Script / вручную | 272 | 9 800 | 86 | 5,8 | 6 % |
| Cloud Run loaders: витрина, финансы, P&L, воронка, Юнитка | Scheduler `wb-mart-prod` и др. | 243 | 18 095 | 99 | 9,6 | 6 % |
| Control Tower | Scheduler `ct-refresh-prod`, 5/сут | 144 | 2 980 | 17 | 5,2 | 3 % |
| Публикация себестоимости для Юнитки | Scheduler `unitka-cogs-publication`, ежечасно 07–23 | 118 | 10 520 | 0 | 3,8 | 3 % |
| Наблюдатель цен WB | Scheduler `wb-prices-prod`, каждые 20 мин (72/сут) | 74 | 6 753 | 2 | 2,4 | 2 % |
| Журнал `LOADER_RUNS` (DML по 10 МиБ) | все Cloud Run loaders | 56 | 15 477 | 0 | 1,9 | 1 % |
| `wb-ops-health` | Scheduler, каждые 3 ч | 53 | 1 980 | 12 | 1,8 | 1 % |
| Ручные `bq` CLI | вручную | 21 | 695 | 9 | 6,2 | 0,5 % |
| База экономики акций WB + Ozon | Scheduler, 4/сут каждый | 39 | 496 | 20 | 1,6 | 1 % |
| Metabase (`metabase-read-only@`) | открытие дашбордов | 13 | 498 | 7 | 1,6 | 0,3 % |
| РНП v3 (`sa-rnp-refresh`) | Scheduler, 3/сут (с ноября) | — | — | — | 1,0 | — |
| Ozon ingestion, CPO, comms, shadow | Scheduler | 23 | 4 374 | 0 | 0,9 | 0,5 % |
| **Итого** | | **4 254** | **123 481** | **1 588** | **148,4** | |

Разбивка исходной строки «личный аккаунт владельца 1,51 ТиБ»: лист операций ФФ 945 ГиБ, ручные сессии 287,
Apps Script/ручные без метки 272, `bq` CLI 21. Metabase идёт под своим SA, а не под личным аккаунтом владельца.

### 1.5. Топ-30 нормализованных запросов (октябрь, по `total_bytes_billed`)

Ключ — `query_info.query_hashes.normalized_literals`. Колонки: запусков в октябре; МиБ к оплате за запуск;
МиБ реально прочитано за запуск; ГиБ за октябрь; ГиБ в сутки в ноябре. Топ-30 = 2 811 ГиБ из 4 254 (66 %).

| # | Источник | Запрос | Тип | Запусков | МиБ оплачено/запуск | МиБ прочитано/запуск | ГиБ окт. | ГиБ/сут нояб. |
|---|---|---|---|---:|---:|---:|---:|---:|
| 1 | dro-health-eval (30 мин) | `CREATE OR REPLACE TEMP TABLE _dro_eval AS SELECT * FROM evetis_health.TVF_DATA_HEALTH(v_ts)…` | CREATE_TABLE_AS_SELECT | 1173 | 330 | 3.3 | 378.0 | 15.86 |
| 2 | executive-v2-layer | `CREATE TEMP TABLE b AS WITH k AS ( SELECT day, orders_covered, sales_covered, ads_covered, fina…` | CREATE_TABLE_AS_SELECT | 527 | 352 | 351.7 | 180.2 | 6.04 |
| 3 | sku-performance-v2-layer | `CREATE TEMP TABLE b AS WITH u AS ( SELECT nm_id, ANY_VALUE(internal_sku) AS internal_sku, ANY_V…` | CREATE_TABLE_AS_SELECT | 528 | 351 | 350.8 | 179.9 | 6.04 |
| 4 | executive-v2-layer | `ASSERT (SELECT COUNTIF(v.period_result_eligible IS DISTINCT FROM b.period_result_eligible) + CO…` | ASSERT | 527 | 329 | 328.1 | 168.2 | 5.62 |
| 5 | executive-v2-layer | `ASSERT (SELECT COUNTIF(v.settlement_eligible IS DISTINCT FROM b.settlement_eligible) + COUNTIF(…` | ASSERT | 527 | 328 | 327.0 | 167.7 | 5.6 |
| 6 | executive-v2-layer | `ASSERT (SELECT COUNTIF(v.ads_billing_covered IS DISTINCT FROM b.ads_billing_covered) + COUNTIF(…` | ASSERT | 527 | 326 | 325.3 | 166.8 | 5.58 |
| 7 | executive-v2-layer | `ASSERT (SELECT COUNTIF(v.logistics_sold_rub IS DISTINCT FROM b.logistics_sold_rub) + COUNTIF(v.…` | ASSERT | 527 | 321 | 320.4 | 164.3 | 5.49 |
| 8 | AppsScript: лист операций ФФ | `SELECT * FROM evetis_ops.V_OPS_SHEET_BOM…` | SELECT | 745 | 210 | 2.3 | 152.8 | 5.05 |
| 9 | AppsScript: лист операций ФФ | `SELECT * FROM evetis_ops.V_OPS_SHEET_BUNDLES…` | SELECT | 745 | 210 | 2.3 | 152.8 | 5.05 |
| 10 | AppsScript: лист операций ФФ | `SELECT * FROM evetis_ops.V_OPS_SHEET_PLAN…` | SELECT | 746 | 200 | 2.4 | 145.7 | 4.8 |
| 11 | AppsScript: лист операций ФФ | `SELECT * FROM evetis_ops.V_OPS_SHEET_PICK…` | SELECT | 744 | 200 | 2.3 | 145.3 | 4.8 |
| 12 | AppsScript: лист операций ФФ | `SELECT * FROM evetis_ops.V_OPS_SHEET_FF_TASK…` | SELECT | 744 | 200 | 2.3 | 145.3 | 4.8 |
| 13 | AppsScript: лист операций ФФ | `SELECT * FROM evetis_ops.V_OPS_SHEET_META…` | SELECT | 743 | 90 | 0.8 | 65.3 | 2.16 |
| 14 | AppsScript: лист операций ФФ | `SELECT * FROM evetis_ops.V_OPS_SHEET_FF_STOCK…` | SELECT | 743 | 80 | 0.1 | 58.0 | 1.92 |
| 15 | AppsScript: лист операций ФФ | `SELECT * FROM evetis_ops.V_OPS_SHEET_SHIPMENTS…` | SELECT | 743 | 80 | 0.1 | 58.0 | 1.92 |
| 16 | executive-v2-layer | `ASSERT (SELECT COUNTIF(v.cohort_orders IS DISTINCT FROM b.cohort_orders) + COUNTIF(v.buyout_ord…` | ASSERT | 527 | 110 | 58.6 | 56.6 | 1.85 |
| 17 | executive-v2-layer | `ASSERT (SELECT COUNTIF(v.orders_covered IS DISTINCT FROM b.orders_covered) + COUNTIF(v.sales_co…` | ASSERT | 527 | 100 | 63.0 | 51.5 | 1.68 |
| 18 | sku-performance-v2-layer | `ASSERT (SELECT SUM(cogs_rub) FROM b) = (SELECT SUM(c.net_product_cogs_operational_rub) FROM wb_mart.V_MART_SKU_DAILY_COGS…` | ASSERT | 341 | 150 | 67.3 | 50.0 | 2.52 |
| 19 | ct-refresh | `CREATE TEMP TABLE cand AS SELECT * FROM wb_mart.V_CT_ACTION_CANDIDATES…` | CREATE_TABLE_AS_SELECT | 118 | 430 | 71.1 | 49.6 | 1.82 |
| 20 | Ручные сессии (`purpose=job-clean-scan-readonly`) | разовый скан истории заданий | SELECT | 51 | 566 | 565.5 | 28.2 | 1.0 |
| 21 | ct-refresh | `CREATE TEMP TABLE new_actual AS SELECT d, marketplace, internal_sku, sales_mode, component_coun…` | CREATE_TABLE_AS_SELECT | 118 | 245 | 69.2 | 28.0 | 1.06 |
| 22 | unitka-cogs-publication | `CREATE TEMP TABLE s AS SELECT internal_sku, effective_from, … FROM evetis_ref.V_PRODUCT_COGS_EFFECTIVE` | CREATE_TABLE_AS_SELECT | 526 | 50 | 0.0 | 25.7 | 0.83 |
| 23 | unitka-cogs-publication | `SELECT STRUCT<INT64>((SELECT COUNT(*) FROM evetis_ref.V_PRODUCT_COGS_EFFECTIVE…` | SELECT | 526 | 50 | 0.0 | 25.7 | 0.83 |
| 24 | ct-refresh | `SELECT STRUCT<STRING>(( SELECT CONCAT('mart=', … MART_RUNS … V_DASH_SKU_DAILY…` (отпечаток свежести) | SELECT | 155 | 170 | 0.8 | 25.7 | 0.85 |
| 25 | Cloud Run loaders: журнал | `UPDATE wb_raw.LOADER_RUNS SET status=@status, completed_at=CUR…` | UPDATE | 2615 | 10 | 0.0 | 25.5 | 0.84 |
| 26 | wb-ops-health (3 ч) | `SELECT STRUCT<ARRAY<STRUCT<scope STRING, scope_id STRING, health_status STRING, …` | SELECT | 248 | 100 | 48.1 | 24.2 | 0.8 |
| 27 | dro-health-eval | `UPDATE wb_ops.OPS_INCIDENT t SET state = 'RESOLVED', …` | UPDATE | 1172 | 20 | 0.0 | 22.9 | 0.94 |
| 28 | dro-health-eval | `CREATE OR REPLACE TEMP TABLE _eval AS SELECT r.*, p.prev_health, …` | CREATE_TABLE_AS_SELECT | 1172 | 20 | 0.0 | 22.9 | 0.94 |
| 29 | dro-health-eval | `CREATE OR REPLACE TEMP TABLE _prev AS SELECT scope, scope_id, health_status AS prev_health FROM…` | CREATE_TABLE_AS_SELECT | 1172 | 20 | 1.4 | 22.9 | 0.94 |
| 30 | dro-health-eval | `INSERT INTO wb_ops.OPS_ALERT_EVENT (alert_event_id, incident_id, alert_type, …` | INSERT | 1172 | 20 | 0.0 | 22.9 | 0.94 |

Что видно по сборкам:

- **Executive V2** за один запуск: CTAS 352 МиБ + шесть `ASSERT`, которые сверяют результат `b` с исходными
  вью, пересчитывая их заново (4 × ~325 + 110 + 100 МиБ). Сверки — **76 % стоимости запуска** (1,47 из 1,94 ГиБ).
- **Детектор DRO** `TVF_PIPELINE_PROBE` трогает 33 таблицы журналов и RAW → 330 МиБ к оплате при 3,3 МиБ
  данных, плюс ~8 шагов записи инцидентов по 20 МиБ. Итого ~0,5 ГиБ за запуск, 48 раз в сутки.
- **Наблюдатель цен** и **журналы** (`LOADER_RUNS`, `INGEST_RUNS`, `WB_PRICES_OBSERVATIONS`): десятки тысяч
  одиночных DML по 10 МиБ минимума. 43 тыс. заданий `sa-loaders-prod` из исходной оценки — это они.

## 2. Потребители: что строится и кто это читает (JOBS за 26.09–09.11, 45 дней)

`referenced_tables` содержит только базовые таблицы, вью раскрываются. Поэтому потребителей вью я искал по
тексту запросов (окно 27.10–09.11, только Metabase / РНП / Юнитка / владелец без метки).

| Строится (кем, как часто) | Кто читает, кроме самого писателя | Вывод |
|---|---|---|
| `wb_mart.EXECUTIVE_V2_DAILY` (17/сут) | Metabase, дашборд 2: 164 запроса за 45 дней; за последние 2 недели открыт **один день** (06.11) | 17 сборок в сутки при чтении раз в несколько дней |
| `wb_mart.SKU_PERFORMANCE_V2_DAILY` (17/сут) | РНП v3 (10 запросов), ручные сессии, Metabase 1 раз (08.11) | то же |
| `wb_mart.UNITKA_COGS_EFFECTIVE` (17/сут) | Юнитка (`sa-loaders-prod`): ~3 чтения в сутки, утренний прогон 10:00 и резерв 12:30 МСК | нужна 1–2 раза в сутки |
| `evetis_ref.CT_ACTUAL_DAILY`, `CT_INVENTORY_SNAPSHOT_DAILY` (5/сут) | Metabase (план продаж, ~80 запросов), лист операций ФФ через `V_OPS_SHEET_*` | нужны, но не 5 раз в сутки |
| `evetis_ref.CT_OWNER_ACTION_QUEUE`, `CT_ACTION_STATUS_LOG` (5/сут) | Metabase: 3 и 1 запрос, последний — **11.10** | очередь действий никто не открывает месяц; её шаг `V_CT_ACTION_CANDIDATES` — самый дорогой в CT (430 МиБ) |
| `wb_raw.WB_PROMO_ECONOMICS_BASIS_SNAPSHOT`, `ozon_raw.OZON_PROMO_ECONOMICS_BASIS_SNAPSHOT` (4/сут) | только DRO (проверка свежести) и ручные сессии; последнее ручное чтение 28.10 | **production-потребителя нет** |
| `wb_raw.WB_PRICES_OBSERVATIONS`, `WB_TARIFF_OBSERVATIONS` | только DRO и ручные сессии | журналы; данные цен (`RAW_WB_PRICES`) читают другие |
| `evetis_health.DATA_HEALTH_SNAPSHOT`, `ALERT_DISPATCH_LOG`, `wb_ops.OPS_INCIDENT/OPS_ALERT_EVENT/OPS_HEALTH_STATE` | никто | механизм оповещений: пишет и отправляет, читать не обязан. Не кандидат на отключение, но кандидат на реже |
| `wb_mart.*__BUILD`, `_*_LOCK` | никто | промежуточные таблицы сборки — так задумано |

**Metabase, что открывали за 27.10–09.11:** `V_DASH_EXECUTIVE_V2_DAILY` (87 запросов, 1 день), три вью плана
продаж в `evetis_mart` и `ozon_mart.V_OZON_FINANCE_NEW_OPERATIONS` (по 15 запросов, 2 дня), по одному запросу
к Ozon P&L, Юнитке и акциям 08.11. Всё вместе — 1,6 ГиБ в сутки, экономить здесь нечего.
**Разбивка по карточкам Metabase не сделана:** Metabase локальный и не запущен (MCP `metabase` — ECONNREFUSED).
Поднимать его ради аудита я не стал.

**Лист операций ФФ** читает 8 вью `V_OPS_SHEET_*` каждый час. За ними 21 базовая таблица: `evetis_ops.CT_*`,
`evetis_ref.CT_*`, RAW поставок и остатков WB/Ozon. Данные ФФ меняются редко: запись владельца выключена,
переезд с Usend идёт с 03.11.

## 3. Что предлагается: план по пунктам

Порядок — по «экономия ГиБ в сутки / риск». Экономия считается от темпа ноября **148,4 ГиБ/сутки ≈ 4,41 ТиБ/мес**.
Прогноз в $ — только BigQuery Evetis, с НДС 16 %, при условии, что бесплатный ТиБ достаётся Evetis
(то есть Heldi и platform BigQuery не тратят).

### Ярус 1 — только расписания (логика и экономика витрин не меняются)

| ID | Изменение | ГиБ/сут | Риск | Затронуто |
|---|---|---:|---|---|
| **BQ-01** | Лист операций ФФ: триггер `everyHours(1)` → `everyHours(6)` (4 в сутки) + ручное «Обновить» из меню. **Или отключить совсем**, если лист после переезда с Usend не нужен | −26,0 (или −31,2) | Низкий: лист отстаёт до 6 ч | `apps-script/operations/OpsMain.gs` (`opsEnsureRefreshTrigger_`, стр. 114) + Код.gs; триггер в проекте Apps Script |
| **BQ-02** | Executive V2: `10 7-23 * * *` → `15 7,9,12,16,22 * * *` МСК (5 в сутки, через 15 мин после `wb-mart-prod`; витрина собирается за 3–4 мин) | −24,0 | Низкий: цифры из витрины не устаревают (витрина и так 4 раза в сутки); покрытие из RAW (реклама, заголовок свежести) отстаёт до 4–6 ч. Ночной разрыв 22:15→07:15 = 9 ч < порога DRO WARN 600 мин | `infra/terraform/executive_v2_layer.tf` (`schedule`), Scheduler `executive-v2-layer-build` |
| **BQ-03** | Детектор DRO: `*/30 * * * *` → `0 */2 * * *` (12 в сутки). Компромисс — ежечасно: −12,6. **Вместе с этим** поднять окно политики «Нет пульса dro_detector_heartbeat» с 90 мин (`duration = 5400s`) до 150 мин при шаге 2 ч (или 90 → 100 мин при шаге 1 ч), иначе она будет срабатывать постоянно | −18,8 | Средний: тревога о сбое приходит до 2 ч позже. Пороги контракта — от 45 мин (цены) до суток; WARN по ценам при 2-часовом шаге всё равно сработает, просто позже | Scheduler `dro-health-eval` + политика мониторинга. Terraform `infra/terraform/evetis_health.tf` есть только в ветках (коммит `0aa9fff9`), в `main` его нет — сначала сверить live с источником |
| **BQ-04** | SKU Performance V2: `20 7-23 * * *` → `25 7,9,12,16,22 * * *` (5 в сутки) | −10,1 | Низкий, как BQ-02 | `infra/terraform/sku_performance_v2_layer.tf` |
| **BQ-05** | Публикация себестоимости Юнитки: `50 7-23 * * *` → `50 9,12 * * *` МСК (перед утренним прогоном 10:00 и резервом 12:30) | −3,4 | Низкий: себестоимость меняется только при правке владельцем; правка, сделанная после 12:50, попадёт в Юнитку на следующий день | `infra/terraform/unitka_cogs_publication.tf` |
| **BQ-06** | Control Tower: `40 7,9,12,16,19 * * *` → `40 7,16 * * *` (2 в сутки) | −3,1 | Низкий: план продаж и лист ФФ отстают до 9 ч. Порог DRO WARN 1560 мин — с запасом | Scheduler `ct-refresh-prod` (Terraform: `ct_refresh.tf`, сверить) |
| **BQ-07** | Наблюдатель цен WB: `*/20` → `0 * * * *` (24 в сутки) | −1,6 | Низкий/средний: реже точки истории цен. **Требует правки контракта DRO** (`wb_prices_observer` WARN 45 / FAIL 90 мин → 90/150), иначе ложные тревоги | Scheduler `wb-prices-prod`, `evetis_health.V_PIPELINE_CONTRACT` |
| **BQ-08** | База экономики акций WB/Ozon: 4 → 2 в сутки, или пауза до появления потребителя | −0,8 (пауза −1,6) | Низкий: production-читателей нет. **Требует правки контракта DRO** (`promo_basis_*` WARN 420 мин) | Scheduler `promo-econ-basis-wb/ozon`, `V_PIPELINE_CONTRACT` |

**Прогноз после каждого шага (накопительно):**

| После | ГиБ/сутки | ТиБ/мес | BigQuery Evetis, $/мес с НДС |
|---|---:|---:|---:|
| сейчас | 148,4 | 4,41 | 24,7 |
| BQ-01 | 122,4 | 3,63 | 19,1 |
| BQ-02 | 98,4 | 2,92 | 13,9 |
| BQ-03 | 79,6 | 2,36 | 9,9 |
| BQ-04 | 69,5 | 2,06 | 7,7 |
| BQ-05 | 66,1 | 1,96 | 7,0 |
| BQ-06 | 63,0 | 1,87 | 6,3 |
| BQ-07 | 61,4 | 1,82 | 6,0 |
| BQ-08 | 60,6 | 1,80 | 5,8 |

Цель «≤ 3 ТиБ/мес» достигается уже после **BQ-01 + BQ-02**. Весь ярус 1 — **≈ 1,8 ТиБ/мес (−59 %)**.
Если бесплатный ТиБ съедают другие проекты аккаунта, худший случай для Evetis — 1,8 × $6,25 × 1,16 ≈ $13/мес.

### Ярус 2 — изменения кода (отдельный ACK, тесты, контрольные суммы)

| ID | Изменение | ГиБ/сут (после яруса 1) | Риск |
|---|---|---:|---|
| **BQ-11** | Executive V2: тяжёлые сверки `ASSERT v ↔ b` (стр. 311–398 `sql/dash/executive_v2_daily_v1.sql`) выполнять в одной полной сборке в сутки (параметр `in_trigger = 'scheduler_full'`), в остальных — только дешёвые (стр. 266–279). Запуск 1,94 → ~0,47 ГиБ | −4,6 | **Средний:** ослабляет защиту экономики в промежуточных сборках. Это изменение сборщика `V_DASH_*` → нужны контрольные суммы (см. §5) |
| **BQ-12** | Лист ФФ: 8 запросов → 1 (один `SELECT` со всеми вью в JSON-колонках) или одна материализованная таблица-снимок, которую пишет CT. Запуск 1,27 ГиБ → ~0,21 ГиБ | −3,2 | Низкий/средний: правка Apps Script (production) |
| **BQ-13** | DRO: `TVF_PIPELINE_PROBE` — 33 таблицы → свежесть по журналам прогонов (`LOADER_RUNS`, `INGEST_RUNS`, `MART_RUNS`…) плюс одно чтение `INFORMATION_SCHEMA.PARTITIONS` вместо прямых сканов RAW | −4,0 | Средний: меняется доказательная база детектора |
| **BQ-14** | Журналы через бесплатные load-задания / Storage Write API вместо DML по 10 МиБ (`LOADER_RUNS`, `WB_PRICES_OBSERVATIONS`, `INGEST_RUNS` у Apps Script); у цен — 4 DML на наблюдение → 1 `MERGE` | −2,5 | Средний: DML-семантика статусов (`STARTED → COMPLETE`) уходит в append-only |
| **BQ-15** | «Пропустить, если вход не изменился»: Executive/SKU V2/CT сначала сравнивают отпечаток (`MART_RUNS.completed_at` + журналы RAW, ~20–30 МиБ) с отпечатком прошлой сборки и выходят без пересборки. Позволит вернуть ежечасный график почти бесплатно | ≈0 при ярусе 1; −20 вместо BQ-02/04, если нужен ежечасный | Низкий/средний |
| **BQ-16** | CT: убрать шаг `V_CT_ACTION_CANDIDATES` → `CT_OWNER_ACTION_QUEUE` (430 МиБ, очередь не открывали с 11.10) или выполнять раз в сутки | −0,7 | Низкий |

Ярус 2 целиком → **≈ 45 ГиБ/сутки ≈ 1,3 ТиБ/мес**.

Чего **не** предлагаю: партиционирование и `require_partition_filter` на RAW/FACT. Крупные сканы (~350 МиБ)
делают только сборки Executive/SKU V2 — они читают всю историю по построению. Остальное — минимальная
тарификация, партиции её не уменьшают. Инкрементальная сборка Executive/SKU V2 (только последние N дней)
дала бы ещё ~5 ГиБ/сутки, но меняет экономику витрин — отдельная задача после яруса 1.

## 4. Защитные ограничения (предлагаемые значения)

| ID | Ограничение | Значение | Комментарий |
|---|---|---|---|
| **BQ-21** | Бюджет на платёжный аккаунт | **$34,50 без НДС** (≈ $40 с НДС); оповещения на 50 / 90 / 100 % факта + 100 % прогноза | Бюджет **не останавливает** расходы, только оповещает. Нужно включить `billingbudgets.googleapis.com` и права Billing Account Administrator. Проще всего завести в консоли самому владельцу |
| **BQ-22** | Квота проекта «Query usage per day» | **сейчас 260 ГиБ** (максимум октября — 254) → **150 ГиБ** после BQ-01…04 → **120 ГиБ** через 2 недели стабильной работы | При превышении **все** запросы проекта падают до конца суток (по тихоокеанскому времени), включая загрузчики. Поэтому ступенями и с запасом ×2 к норме. 120 ГиБ/сутки ограничивают худший месяц ~3,6 ТиБ ≈ $16 |
| **BQ-23** | Квота «Query usage per day per user» (одна на все идентичности) | **сейчас 100 ГиБ** (сборка Executive — 34, личный аккаунт владельца — 48 в среднем) → **40 ГиБ** после яруса 1 | Ловушка: под личным аккаунтом владельца работают и Apps Script-загрузчики (production), и ручные сессии. Тяжёлая ручная сессия (как 11.10, 155 ГиБ) остановит загрузки до конца суток. Альтернатива — ручные сессии под отдельным SA |
| **BQ-24** | `maximumBytesBilled` в заданиях | Scheduler-задания (`executive_v2_layer.tf`, `sku_performance_v2_layer.tf`, `unitka_cogs_publication.tf`, CT, DRO): **2 ГиБ**; Apps Script лист ФФ: **0,5 ГиБ** на запрос; ручные хелперы: 10 ГиБ | Самый большой одиночный шаг за 40 дней — 0,42 ГиБ у SA, 1,15 ГиБ у ручных. **Проверить на тесте** семантику для `SCRIPT`: относится ли лимит к каждому шагу или к сумме. И учесть: на `INFORMATION_SCHEMA` лимит сравнивается с верхней оценкой (9 ГиБ при фактических 240 МиБ) |

## 5. Как проверить результат

**Контроль объёма (до/после, по каждому пункту).** Узкий запрос к истории заданий, ~20–40 МиБ за неделю:

```sql
SELECT DATE(creation_time) d,
  IFNULL((SELECT ANY_VALUE(CONCAT(l.key,'=',l.value)) FROM UNNEST(labels) l
          WHERE l.key IN ('evetis-job','dro_component','component')), user_email) src,
  COUNT(*) jobs, ROUND(SUM(total_bytes_billed)/POW(1024,3),1) gib
FROM `region-eu`.INFORMATION_SCHEMA.JOBS_BY_PROJECT
WHERE creation_time >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY)
  AND job_type = 'QUERY' AND statement_type <> 'SCRIPT'   -- без родителей SCRIPT, иначе дубль
GROUP BY 1, 2 ORDER BY 1, 4 DESC
```

Ожидаемые значения по `src` в ГиБ/сутки, до → после: `executive-v2-layer` 34 → ~10;
`evetis-operations-sheet` 31 → ~5; DRO 25 → ~6; `sku-performance-v2-layer` 14 → ~4;
`unitka-cogs-publication` 3,8 → ~0,4; `ct-refresh` 5,2 → ~2. Проект целиком: 148 → ~60.

**Контроль, что ничего не сломалось (ярус 1):**

- `EXECUTIVE_V2_BUILD_LOG`, `SKU_PERFORMANCE_V2_BUILD_LOG`, `UNITKA_COGS_PUBLISH_LOG`, `CT_REFRESH_LOG`:
  все прогоны после изменения `COMPLETE`, их ровно 5 / 5 / 2 / 2 в сутки.
- DRO: в `V_DATA_HEALTH_CURRENT` нет новых WARN/FAIL по `executive_v2`, `sku_performance_v2`, `ct_refresh`
  (и по `wb_prices_observer`/`promo_basis_*` после правки контракта для BQ-07/08).
- Юнитка: утренний прогон 10:00 МСК читает `UNITKA_COGS_EFFECTIVE` с `published_at` того же дня.
- Экономика витрин не меняется: ярус 1 трогает только время запуска. Встроенные `ASSERT` сборщиков
  продолжают выполняться в каждом прогоне.

**Контрольные суммы для BQ-11** (единственный пункт, затрагивающий защиту `V_DASH_*`): до и после — за
окно 01.07–вчера `SUM` по `period_result_pre_cogs_corrected_rub`, `ad_spend_financial_rub`,
`settlement_seller_price_rub`, `logistics_sold_rub`, `cohort_orders` в `EXECUTIVE_V2_DAILY` против
`V_DASH_FINANCE_CORRECTED_DAILY` / `V_DASH_SETTLEMENT_DAILY` / `V_DASH_EXECUTIVE_BREAKDOWN_DAILY` /
`V_DASH_BUYOUT_COHORT_DAILY`. Расхождение должно быть 0,00 ₽ и 0 строк.

**Откат.** Ярус 1 — вернуть прежний `schedule` в Terraform/Scheduler (старые значения — в §3 и в
`gcloud scheduler jobs list`) и `everyHours(1)` в Apps Script. Данные не теряются: сборки полные, следующий
запуск восстанавливает состояние. Ярус 2 — откат кода через PR + прежний DDL процедур (живые определения
снять в `sql/rollback/` до деплоя).

## 6. Открытые вопросы к владельцу

1. **Состав счёта за октябрь** по проектам и SKU — от него зависит, достижимы ли $40 одной оптимизацией Evetis (§1.3).
2. **Лист операций ФФ после переезда с Usend** — нужен ли он вообще? Если нет, BQ-01 = отключить триггер (−31 ГиБ/сутки).
3. **Допустимая задержка тревог DRO:** 2 ч (BQ-03 как предложено) или 1 ч (−12,6 вместо −18,8)?
4. **Свежесть Executive/SKU V2:** хватает ли 5 сборок в сутки? Если нужен ежечасный график — тогда BQ-15 вместо BQ-02/04.
5. Terraform DRO (`infra/terraform/evetis_health.tf`, коммит `0aa9fff9`) есть только в ветках, в `main` его
   нет. Перед BQ-03 решить, где правится расписание и окно пульса: перенести файл в `main` или править через `gcloud`.

## 7. Как делался аудит

- Только `SELECT` через REST `www.googleapis.com/bigquery/v2/.../queries`. Клиент отказывал не-SELECT,
  делал dry-run, ставил `maximumBytesBilled` и метку `purpose=bq-cost-audit-2026-11`.
- Итог по метке (`JOBS_BY_USER`, 10.11): **10 заданий, все `SELECT`, 1,44 ГиБ (≈ $0,01)**. Плюс бесплатные
  метаданные (`routines.get`, `tables.get`, `datasets.list`) и `gcloud scheduler jobs list`.
- Без изменений: Scheduler, Cloud Run, Apps Script, Terraform, DDL/DML. Heldi не затрагивался.
- Не получено: разбивка счёта (нет доступа), разбивка по карточкам Metabase (Metabase не запущен),
  `TABLE_STORAGE` (нет права у личного аккаунта владельца; хранение, по всей видимости, копеечное — не проверено).

## 8. Карта расходов по системам и целевое расписание «два окна» (дополнение 10.11)

Принцип владельца: данные проверяются на следующий день; Юнитка обновляется 2 раза в сутки (10:00 и 12:30 МСК),
РНП — вместе с ней (10:05/12:05/14:05). Значит, всё, что пересобирается ежечасно, можно привязать к двум окнам.

**Главное:** деньги уходят не на выгрузку из API WB/Ozon. Все загрузчики вместе (Apps Script WB, Cloud Run WB,
Ozon) стоят ≈ 7 ГиБ/сутки из 148. Дорого обходятся внутренние пересборки, проверки и лист ФФ, которые
крутятся ежечасно независимо от того, появились ли новые данные.

Значения ниже — ГиБ/сутки в ноябре → после изменения.

| Система | Что делает | Расписание сейчас | Читает | Кормит | Сейчас | Предложение | После |
|---|---|---|---|---|---:|---|---:|
| Executive V2 (`sp_build_executive_v2_daily`) | Таблица управленческого P&L WB по дням: заказы, выручка, выплата WB, логистика, хранение, реклама (биллинг), себестоимость, результат периода, когорты выкупа. 76 % стоимости — шесть `ASSERT`, которые пересчитывают исходные вью, чтобы доказать «таблица = вью» | ежечасно 07–23 (17/сут) | 8 вью `V_DASH_*` поверх `FACT_*`, `MART_SKU_DAILY`, RAW рекламы | Metabase, дашборд 2 «Executive» (карточки 187–217). За 2 недели открыт 1 день | 34,0 | 2/сут: 09:15 и 12:45 | 4,0 |
| Лист «EVETIS OPERATIONS» (Apps Script, лист ФФ) | План отгрузок WB/Ozon, сборка наборов, комплектация, задания ФФ, остаток ФФ, поставки — 11 вью `V_OPS_SHEET_*` | ежечасно, 24/сут | 21 таблица: журнал ФФ `evetis_ops`, CT, RAW поставок и остатков WB/Ozon | Google Sheet операций ФФ | 31,2 | меню «Выключить автообновление», обновлять кнопкой «Обновить данные» (без кода) | ~2,6 (0, если лист не нужен) |
| Детектор DRO | Каждые 30 мин проверяет свежесть 40 потоков данных, ведёт инциденты, шлёт письма | каждые 30 мин (48/сут) | 33 таблицы журналов и RAW | оповещения | 25,1 | 4/сут: 08:00, 10:45, 13:45, 20:00 + окно пульса 90 мин → 13 ч | 2,1 |
| SKU Performance V2 | Таблица экономики по артикулу и дню: заказы, выкупы, выручка, комиссии, логистика, хранение, атрибуция рекламы, себестоимость, вклад | ежечасно 07–23 | `FACT_*`, `MART_SKU_DAILY`, хранение, себестоимость | Metabase SKU Performance, РНП | 14,3 | 2/сут: 09:25 и 12:55 | 1,7 |
| Витрина WB `wb-mart-prod` | Полная пересборка `FACT_*` и `MART_SKU_DAILY` из RAW | 07, 09, 12, 16 | RAW WB | всё выше + Юнитка | ~4,1 | 2/сут: 09:00 и 12:00 | ~2,2 |
| Юнитка WB/Ozon, P&L магазина WB/Ozon, воронка, хранение, тарифы | Cloud Run, пишут в Google Sheets | 10:00 + 12:30; P&L 13:00/13:20; прочее раз в сутки | `FACT_*`, RAW, `UNITKA_COGS_EFFECTIVE` | Юнитка, P&L магазина | ~5,5 | без изменений | ~5,5 |
| Публикация себестоимости для Юнитки | Копия `V_PRODUCT_COGS_EFFECTIVE` → `UNITKA_COGS_EFFECTIVE` | ежечасно 07–23 | справочник себестоимости | Юнитка WB | 3,8 | 1/сут: 09:50 | 0,2 |
| Control Tower | Факт по SKU×каналу, снимок запасов, очередь действий | 5/сут | витрина, RAW Ozon, остатки | Metabase «план продаж», лист ФФ | 5,2 | 1/сут: 09:40 | 1,0 |
| Наблюдатель цен WB | Цена и скидка продавца из Seller API. **СПП Seller API не отдаёт** — частые снимки СПП не ловят | каждые 20 мин (72/сут) | Seller API | история цен | 2,4 + журнал 1,9 | 6/сут | ~0,5 |
| `wb-ops-health` | Старая проверка здоровья, частично дублирует DRO | каждые 3 ч | журналы, витрина | `OPS_HEALTH_STATE` | 1,8 | 2/сут или выключить | ~0,45 |
| База экономики акций WB/Ozon | Снимок цены, себестоимости, комиссии по SKU | 4/сут | цены, себестоимость, тарифы | никто (только DRO) | 1,6 | пауза | 0 (0,4 при 1/сут) |
| Apps Script загрузки WB | Заказы и продажи — ежечасно; реклама — 05:00; финансы, остатки, поставки, справочник | 24/сут заказы и продажи | API WB | RAW WB | ~5,8 | можно 4/сут для заказов и продаж (−0,4); не приоритет | ~5,4 |
| Ozon (fast/daily/promo/weekly/CPO) | Отправления, остатки, финансы, реклама, цены, акции | 3/сут, 1/сут, 4/сут | API Ozon | RAW Ozon | ~0,7 | без изменений | ~0,7 |
| РНП v3 | Сборка блоков РНП | 10:05/12:05/14:05 | RAW Ozon/WB, `SKU_PERFORMANCE_V2_DAILY`, справочники | сайт РНП | 1,0 | без изменений | 1,0 |
| Metabase | Открытие дашбордов | по требованию | вью | дашборды | 1,6 | без изменений | 1,6 |
| Ручные сессии (Claude/Codex, `bq`) | Аудиты и разборы | вручную | разное | — | ~8,2 | лимит 10 ГиБ на запрос, метки | ~8 |
| **Итого** | | | | | **148** | | **≈ 38 ГиБ/сутки ≈ 1,1 ТиБ/мес** |

Для этого расписания нужно обновить контракты детектора (`evetis_health.V_PIPELINE_CONTRACT`), иначе пойдут
ложные тревоги о «несвежести»:

- `executive_v2` и `sku_performance_v2`: WARN 600 → 1500, FAIL 1020 → 2000 мин;
- `mart`: слот 07:30 → 09:30;
- `wb_prices_observer`: WARN 45/FAIL 90 → 600/720 мин;
- `promo_basis_wb/ozon`: оповещение выключить на время паузы;
- политика пульса DRO: окно 5400 → 46 800 с.

`ct_refresh` (WARN 1560) при 1 сборке в сутки остаётся в пороге.

## 9. Выполнено 10.11.2026 по ACK владельца

| # | Что | Было | Стало | Где изменено |
|---|---|---|---|---|
| 1 | Executive V2 `executive-v2-layer-build` | `10 7-23 * * *` МСК | `45 12 * * *` МСК | Scheduler (live) + `executive_v2_layer.tf` |
| 2 | Лист «EVETIS OPERATIONS», автообновление | ежечасный триггер `opsTriggerRefresh` | выключен через меню таблицы («Автообновление выключено. Обновлять — через меню.») | Apps Script листа ФФ |
| 3 | Детектор DRO `dro-health-eval` | `*/30 * * * *` | `0 4,10,16,22 * * *` МСК (каждые 6 ч; сдвиг сетки — чтобы сводка 09:30 и тревоги не попадали в тихие часы 23–07) | Scheduler (live). Terraform DRO в `main` нет (коммит `0aa9fff9` в ветках) |
| 3a | Политика «DRO-1: детектор здоровья данных не работает» | окно пульса 5400 с (90 мин) | 25 200 с (7 ч) | Cloud Monitoring, policy `17273251700172697474` |
| 4 | SKU Performance V2 `sku-performance-v2-layer-build` | `20 7-23 * * *` | `30 12 * * *` МСК (после витрины 12:00 и хранения 11:45) | Scheduler + `sku_performance_v2_layer.tf` |
| 5 | Витрина `wb-mart-prod` | `0 7,9,12,16 * * *` | `0 9,12 * * *` МСК | Scheduler + `scheduler.tf` |
| 7 | Себестоимость для Юнитки `unitka-cogs-publication` | `50 7-23 * * *` | `50 9 * * *` МСК (порог устаревания у Юнитки — 26 ч) | Scheduler + `unitka_cogs_publication.tf` |
| 8 | Control Tower `ct-refresh-prod` | `40 7,9,12,16,19 * * *` | `40 9 * * *` МСК | Scheduler + `ct_refresh.tf` |
| 9 | Цены WB `wb-prices-prod` | `*/20 * * * *` UTC | `0 */4 * * *` UTC (= 03, 07, 11, 15, 19, 23 МСК) | Scheduler + `wb_prices_observer.tf` |
| 10a | `wb-ops-health-prod` | ENABLED, `0 */3 * * *` | PAUSED | Scheduler + `ops_health.tf` (`paused = true`) |
| 10c | Apps Script `runWbOrdersIncremental`, `runWbSalesIncremental` | «По часам — раз в час» | «По часам — раз в 6 часов» | триггеры проекта `evetis-wb-analitics` |
| — | Контракт DRO `evetis_health.V_PIPELINE_CONTRACT` (версия `dro1-2026-10-04` → `dro1-2026-11-10`) | orders 180/360, sales 240/480, цены 45/90, mart слот 07:30, executive/sku 600/1020 | orders и sales 420/720, цены 300/540, mart слот 09:30, executive/sku 1500/2000, тексты cadence | `tables.patch` (метаданные вью сохранены) |

Без изменений по решению владельца: база экономики акций, Юнитка, РНП, Ozon, ручные сессии.

**Терраформ:** 7 файлов в `infra/terraform` приведены к live (`terraform validate` и `fmt -check` — OK).
Пока они не закоммичены и не слиты в `main`, любой `terraform apply` из `main` вернёт прежние расписания.

**Откат** (снимки «до» — в `docs/ops/bq_cost_rollback_2026-11-10/`):

```bash
P="--project=project-fa311fc0-4d87-4781-986 --location=europe-west1"
gcloud scheduler jobs update http executive-v2-layer-build $P --schedule="10 7-23 * * *"
gcloud scheduler jobs update http sku-performance-v2-layer-build $P --schedule="20 7-23 * * *"
gcloud scheduler jobs update http dro-health-eval $P --schedule="*/30 * * * *"
gcloud scheduler jobs update http wb-mart-prod $P --schedule="0 7,9,12,16 * * *"
gcloud scheduler jobs update http unitka-cogs-publication $P --schedule="50 7-23 * * *"
gcloud scheduler jobs update http ct-refresh-prod $P --schedule="40 7,9,12,16,19 * * *"
gcloud scheduler jobs update http wb-prices-prod $P --schedule="*/20 * * * *"
gcloud scheduler jobs resume wb-ops-health-prod $P
```

(в zsh переменную `$P` подставлять как `${=P}` или писать флаги явно).

Остальное откатывается так:

- контракт DRO — `tables.patch` с `V_PIPELINE_CONTRACT.before.sql`;
- политика пульса — PATCH `conditions` из `alert_policy_dro_heartbeat.before.json`;
- лист ФФ — меню «Включить автообновление (каждый час)»;
- триггеры Apps Script — «Раз в час» в редакторе триггеров.
