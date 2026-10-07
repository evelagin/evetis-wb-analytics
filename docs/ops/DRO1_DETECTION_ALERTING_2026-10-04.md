# DRO-1 — Detection & Alerting (Data Reliability & Observability v1, P0)

Статус (2026-10-07): **развёрнуто, доверенный прогон доказан (`DRO_TRUSTED_RUN_PROVEN`).** Регулярная оценка
`*/30 * * * *` (Europe/Moscow) включается Gate C — PR `paused = false`, затем targeted apply по owner ACK
(§9.1). До apply планировщик `dro-health-eval` в production на паузе.
Основание: аудит `Claude outputs/DATA_PLATFORM_AUDIT_2026-10-04/` и дизайн
`Claude outputs/DATA_RELIABILITY_OBSERVABILITY_V1_2026-10-04/DESIGN.md` (owner ACK DRO-1, 2026-10-04).

## 1. Проблема

- `wb_ops.OPS_ALERT_EVENT` копит события, но не доставляет их: 12 из 12 событий записаны с `channel='NONE'`.
  Сбой витрины 01.10 (21 ч) и потери снимков ставок 13.09, 20.09, 01.10 до владельца не дошли.
- Проверки ADS-1B срабатывают через 36 ч. Для снимков (ставки, остатки, тарифы) это **после** невосстановимой потери.
- Расширенная проверка (заказы, продажи, Ozon) не запускалась с 22.09, но 24 её строки показывались как HEALTHY.
- Нет машинного ответа на вопрос «каким данным можно доверять сейчас».

## 2. Архитектура

```
журналы запусков + RAW (WB, Ozon, производные)
        │  read-only
        ▼
evetis_health.V_PIPELINE_CONTRACT  ── пороги, слоты, сроки потери (единственное место чисел)
evetis_health.TVF_PIPELINE_PROBE(as_of)       — наблюдение без порогов
evetis_health.TVF_DATA_PERIOD_STATE(as_of)    — состояние данных по суткам (35 дней)
evetis_health.TVF_DATA_HEALTH(as_of)          — run / data / serving status по конвейеру
        │
Scheduler dro-health-eval (каждые 30 мин, sa-ops-health)
        ▼
evetis_health.sp_evaluate_data_health()
   ├─ INSERT  evetis_health.DATA_HEALTH_SNAPSHOT                 (доказательство состояния)
   ├─ CALL    wb_ops.sp_ops_apply_health (scope 'DRO_PIPELINE')  (инциденты, дедуп, события — без изменений ADS-1B)
   ├─ CALL    evetis_health.sp_dispatch_alerts                    (напоминания, тихие часы, пакет, дайджест 09:30)
   │            └─ задание-маркер с метками @@query_label ──► журнал аудита ──► политика Cloud Monitoring ──► email
   └─ задание-пульс (метка heartbeat) ──► лог-метрика ──► сторож (нет пульса 90 мин) ──► email
        ▼
evetis_health.V_DATA_HEALTH_CURRENT   — «каким данным доверять сейчас» одним запросом
evetis_health.V_HEALTH_CHECK_CURRENT  — все проверки (ADS-1B, расширенная, DRO-1) с устареванием
evetis_health.V_DATA_PERIOD_STATE     — периоды: RECOVERED / MISSING_UNRECOVERABLE / PROVISIONAL_STUCK …
```

**Почему без нового runtime.** BigQuery не шлёт письма, но каждое задание попадает в журнал аудита
Cloud Logging вместе с метками. Проверено 2026-10-04 на этом проекте:

- метки видны по пути `protoPayload.metadata.jobChange.job.jobConfig.labels.*`;
- ошибка задания видна в `jobStatus.errorResult`;
- логи data_access идут в `_Default` без исключений;
- дочерние задания скрипта логируются со своим `parentJobName`.

Применимость `@@query_label` к дочерним заданиям скрипта подтверждена официальной документацией BigQuery
(через Context7). На этом проекте она подтверждается первым прогоном (§9).

**Telegram и `ops-notifier` не используются**: исключение из правила F-18 не дано.
`wb-communications` не задействован.

**Изоляция маркетплейсов.** `evetis_health` — общий слой наблюдаемости. Он читает только журналы и
метаданные свежести обоих доменов, бизнес-фактов не производит и не смешивает. Писать в `wb_*` / `ozon_*`
он не может: тест разрешает только `wb_ops.OPS_ALERT_EVENT` и таблицы `evetis_health`.

## 3. Объекты

| Объект | Тип | Файл |
|---|---|---|
| `evetis_health` | датасет | `infra/terraform/evetis_health.tf` |
| `V_PIPELINE_CONTRACT` | VIEW (литерал, 40 конвейеров) | `sql/health/dro1_01_contract.sql` |
| `TVF_PIPELINE_PROBE(as_of)` | TABLE FUNCTION | `dro1_02_probe.sql` |
| `TVF_DATA_PERIOD_STATE(as_of)`, `TVF_DATA_HEALTH(as_of)` | TABLE FUNCTION | `dro1_03_evaluate.sql` |
| `DATA_HEALTH_SNAPSHOT`, `ALERT_DISPATCH_LOG` | TABLE (partition by date, expiry 400 д) | `dro1_04_tables.sql` |
| `f_label_value`, `sp_dispatch_alerts`, `sp_evaluate_data_health` | FUNCTION, PROCEDURE | `dro1_05_procedures.sql` |
| `V_DATA_HEALTH_CURRENT`, `V_HEALTH_CHECK_CURRENT`, `V_DATA_PERIOD_STATE` | VIEW | `dro1_06_read_model.sql` |
| откат | — | `dro1_99_rollback.sql` |
| Scheduler `dro-health-eval`, метрика `dro_detector_heartbeat`, 4 политики | Terraform | `evetis_health.tf` |
| права `sa-ops-health`: dataEditor `evetis_health`, dataViewer `ozon_raw`, `evetis_ref` | Terraform (уровень датасетов) | `evetis_health.tf` |

`wb_ops` не меняется: `sp_ops_apply_health`, таблицы и вью ADS-1B используются как есть.

## 4. Модель состояния

| Измерение | Grain | Значения |
|---|---|---|
| `run_status` | последний запуск | SUCCESS · PARTIAL · FAILED · ABANDONED (RUNNING дольше `stale_run_minutes`) · RUNNING · SKIPPED · UNKNOWN |
| `data_status` | период (сутки) / текущий целевой период | COMPLETE · RECOVERED · PROVISIONAL · PROVISIONAL_STUCK · PENDING (слот наступил, идёт допуск) · MISSING · MISSING_UNRECOVERABLE · INCOMPLETE · NOT_EXPECTED (день без данных, покрытый успешным окном) · UNKNOWN |
| `serving_status` | конвейер сейчас | HEALTHY · DEGRADED · STALE · BLOCKED (потеря неизбежна или произошла) · UNKNOWN (не оценивается, проба пуста, детектор устарел) |

**Ключевое правило:** исторический FAILED запуска влияет на `serving_status` только через `data_status`
своего периода. Журнал запусков не переписывается.

**Порядок причин** (`reason_code`):

1. `CHECK_NOT_IMPLEMENTED` / `NO_DATA_OBSERVED`.
2. Для слотовых конвейеров:
   - `DATA_LOSS_CONFIRMED` → `DATA_LOSS_IMMINENT` → `SLOT_MISSED` → `SLOT_LATE`.
3. Для возрастных конвейеров:
   - `FRESHNESS_STALE` → `FRESHNESS_LATE`.
4. `RUN_FAILED` / `RUN_PARTIAL`.
5. Причины полноты: `CONTROL_MISMATCH` / `COVERAGE_LOW`.
6. `MISSING_DATES_RECOVERABLE`.
7. `OK`.

**Пороги** — только из контракта. Тест запрещает числовые литералы, кроме 0, в CTE-классификаторах.

## 5. Покрытие реестра

Все 27 строк `wb_ops.OPS_PIPELINE_REGISTRY` присутствуют (`in_registry = TRUE`), ещё 13 — вне реестра.
Итого 40, из них 34 оцениваются.

Не оцениваются (`NOT_EVALUATED`, без алертов, в read model честное UNKNOWN):

| Конвейер | Причина |
|---|---|
| `ads_search_clusters` | ручной |
| `finance_backfill` | нет журнала |
| `sales_reconcile` | нет журнала |
| `stocks_cloudrun` | выключен в реестре |
| `ff_stock` | DRO-2 |
| `sales_plan` | DRO-2 |

## 6. Backtest на production-данных (read-only, 2026-10-04)

Команда: `python tools/dro1_health.py --token-command "gcloud auth print-access-token" backtest`.

Тела объектов подставляются в SELECT, момент `as_of` фиксируется, запросы уходят через `tools/lib/bq_readonly.py`.
Итог: 18 запросов, 56 МБ обработано, **23 / 23 PASS**.

| Случай | Момент (МСК) | Конвейер | Результат | serving / data | Severity | Опоздание, мин | До потери, мин |
|---|---|---|---|---|---|---|---|
| Реклама 30.09, прогон ERROR | 01.10 07:00 | ads_daily | SLOT_MISSED | STALE / MISSING | HIGH | 105 | 8 535 |
| Витрина 01.10 | 01.10 08:00 | mart | SLOT_LATE | DEGRADED / MISSING | MEDIUM | 30 | — |
| Витрина 01.10 | 01.10 10:00 | mart | SLOT_MISSED | STALE / MISSING | CRITICAL | 150 | — |
| Реклама 30.09 сейчас | 04.10 | ads_daily | OK | HEALTHY / COMPLETE | INFO | 0 | — |
| Ставки 13.09 | 13.09 07:00 | ads_query_bids | SLOT_LATE | DEGRADED / MISSING | HIGH | 105 | 1 020 |
| Ставки 13.09 | 13.09 16:30 | ads_query_bids | DATA_LOSS_IMMINENT | BLOCKED / MISSING | CRITICAL | 675 | 450 |
| Ставки 13.09 | 14.09 00:30 | ads_query_bids | DATA_LOSS_CONFIRMED | BLOCKED / MISSING_UNRECOVERABLE | CRITICAL | 1 155 | −30 |
| Ставки 20.09 | 20.09 07:00 | ads_query_bids | SLOT_LATE | DEGRADED / MISSING | HIGH | 105 | 1 020 |
| Ставки 20.09 | 20.09 16:30 | ads_query_bids | DATA_LOSS_IMMINENT | BLOCKED / MISSING | CRITICAL | 675 | 450 |
| Ставки 01.10 | 01.10 07:00 | ads_query_bids | SLOT_LATE | DEGRADED / MISSING | HIGH | 105 | 1 020 |
| Ставки 01.10 | 01.10 16:30 | ads_query_bids | DATA_LOSS_IMMINENT | BLOCKED / MISSING | CRITICAL | 675 | 450 |
| Остатки WB 03.09 | 03.09 08:00 | stocks_snapshot | SLOT_LATE | DEGRADED / MISSING | HIGH | 90 | 960 |
| Остатки WB 03.09 | 03.09 16:30 | stocks_snapshot | DATA_LOSS_IMMINENT | BLOCKED / MISSING | CRITICAL | 600 | 450 |
| Остатки WB 03.09 | 04.09 00:30 | stocks_snapshot | DATA_LOSS_CONFIRMED | BLOCKED / MISSING_UNRECOVERABLE | CRITICAL | 1 080 | −30 |

Периоды «сейчас»:

| Период | Ожидалось | Получено | Итог |
|---|---|---|---|
| ads_daily 30.09 | RECOVERED | RECOVERED | PASS |
| mart 30.09 | RECOVERED | RECOVERED | PASS |
| ads_query_bids 13.09 | MISSING_UNRECOVERABLE | MISSING_UNRECOVERABLE | PASS |
| ads_query_bids 20.09 | MISSING_UNRECOVERABLE | MISSING_UNRECOVERABLE | PASS |
| ads_query_bids 01.10 | MISSING_UNRECOVERABLE | MISSING_UNRECOVERABLE | PASS |
| stocks_snapshot 03.09 | MISSING_UNRECOVERABLE | MISSING_UNRECOVERABLE | PASS |

Read model (таблица снимков подменена результатом TVF):

| Проверка | Результат | Итог |
|---|---|---|
| Свежий снимок | 34 / 34 оцениваемых HEALTHY, ни одного DETECTOR_STALE | PASS |
| Снимок 2 ч назад | 34 / 34 UNKNOWN / DETECTOR_STALE, ни одного HEALTHY | PASS |
| Расширенная проверка (возраст 17 708 мин ≈ 12,3 сут) | 24 / 24 UNKNOWN / DETECTOR_STALE; 7 проверок ADS-1B — HEALTHY | PASS |

**Вывод.** Каждая известная потеря снимка обнаруживается:

- первым WARN за 16–17 ч до срока: 07:00–08:00 при сроке 00:00;
- затем CRITICAL за 7,5 ч до срока.

Сейчас H4 ловит такую потерю через 36 ч, уже после срока. Простой витрины 01.10 был бы CRITICAL в 10:00
того же дня, а фактически он длился 21 ч. Текущее состояние: 0 ложных срабатываний.

**Ограничения backtest:**

- `PROVISIONAL_STUCK` списаний (`V_ADV_COSTS_DAY_COVERAGE`) читается «на сегодня», а не на `as_of`.
- Если строку перезагрузили позже, её `loaded_at` при реконструкции прошлого момента может оказаться
  «слишком новым». На живом прогоне (`as_of = now`) это не влияет.

## 7. Тесты

`tools/tests/test_dro1_health.py` (входит в CI `sql-current`: `pytest -q tools/tests`), 41 тест.

- **Контракт.** Совпадение с 27 ID реестра. Согласованность порогов. Для снимков WARN не позже 16:00
  (≥ 8 ч до потери). ФФ и план не оцениваются.
- **Классификаторы — тот же SQL-текст в sqlite.**
  - Нормальный конвейер.
  - Реклама 30.09: RECOVERED и HEALTHY.
  - Снимок до срока (WARN и IMMINENT) и после срока (UNRECOVERABLE).
  - Восстановимый источник с задержкой: STALE, а не UNRECOVERABLE.
  - Допуск PENDING.
  - RUN_FAILED при свежих данных.
  - CONTROL_MISMATCH.
  - LOW-критичность.
  - NOT_EVALUATED.
  - Пустая проба.
  - PROVISIONAL.
  - Все 7 правил периодов.
  - Устаревание детектора: свежий / 61 мин / 12 суток / никогда не запускался.
  - Устаревшая расширенная проверка → UNKNOWN.
- **Безопасность.**
  - Читающие объекты рендерятся в чистый SELECT (`assert_read_only`).
  - Процедуры пишут только в разрешённые таблицы, без DELETE / MERGE / DROP.
  - Метки — валидные метки BigQuery.
  - Секретов в файлах нет.
- **Terraform.**
  - Существующий канал, без нового канала.
  - Без ролей проекта.
  - Маркеры и `dro_kind` совпадают с SQL.
  - Политики под `count` по `unitka_alert_email`.

## 8. Оповещения

| Политика | Условие | Тихие часы 23–07 МСК |
|---|---|---|
| `dro_alert_batch` | задание-маркер `dro_kind=alert` (один пакет за прогон) | детектор отправляет ночью только CRITICAL с `DATA_LOSS_IMMINENT` / `DATA_LOSS_CONFIRMED`. Остальные HIGH / CRITICAL ждут 07:00, MEDIUM — дайджеста |
| `dro_digest` | маркер `dro_kind=digest`, первый прогон ≥ 09:30 МСК, раз в сутки | — |
| `dro_detector_failed` | ошибка задания с `dro_component=detector` или маркер `dispatch_error` | всегда |
| `dro_detector_watchdog` | нет пульса `dro_detector_heartbeat` 90 мин | всегда |

**Механика доставки:**

- **Дедупликация.** Инцидент ADS-1B с отпечатком `scope|pipeline_id|reason_code|pipeline_id`: даты в
  отпечатке нет.
- **Эскалация.** Смена причины открывает новый инцидент: SLOT_LATE → DATA_LOSS_IMMINENT → DATA_LOSS_CONFIRMED.
- **Напоминания.** CRITICAL — раз в 4 ч, HIGH — раз в 12 ч. Это `alert_type='REMINDER'` с `reminder_bucket`.
- **Восстановление** уходит отдельным письмом, если открытие было отправлено. Иначе — в дайджест.
- **Пакет.** Все события прогона идут одним письмом, поэтому ограничение частоты 5 мин ничего не теряет.
- **Передача ≠ доставка.** В `OPS_ALERT_EVENT` пишется `state='HANDED_OFF'`, `channel='EMAIL_CLOUD_MONITORING'`.
  Само письмо подтверждает только Cloud Monitoring.
- **Сбой доставки после маркера.** Возможен дубль письма, потери нет: AT_LEAST_ONCE, контракт таблицы.

## 9. Деплой (не выполнен; каждый шаг — по ACK)

1. **Terraform, targeted:**
   - `google_bigquery_dataset.evetis_health`;
   - три `google_bigquery_dataset_iam_member.ops_health_*`.

   Через `infra.yml` (`action=plan`, затем `apply` с `targets`). План должен показать ровно 4 to add.
2. **SQL по порядку:** `dro1_01` → `dro1_02` → `dro1_03` → `dro1_04` → `dro1_05` → `dro1_06`.

   Каждый файл — `CREATE OR REPLACE` / `CREATE TABLE IF NOT EXISTS`. Перед этим:

   ```bash
   python tools/dro1_health.py --token-command "gcloud auth print-access-token" backtest
   ```

   Ожидается 23 / 23 PASS.
3. **Проверка после деплоя SQL** (пишет в `evetis_health` и `wb_ops`):
   1. `CALL evetis_health.sp_evaluate_data_health()` вручную.
   2. `SELECT * FROM evetis_health.V_DATA_HEALTH_CURRENT` — 40 строк, 34 оцениваемых, без DETECTOR_*.
   3. В Cloud Logging у дочерних заданий `jobConfig.labels.dro_kind="heartbeat"` — это подтверждает `@@query_label`.
4. **Terraform, targeted:**
   - `google_cloud_scheduler_job.dro_health_eval`;
   - `google_logging_metric.dro_detector_heartbeat`;
   - четыре `google_monitoring_alert_policy.dro_*`.
5. **Контролируемая проверка канала** (D10, отдельный ACK): одно тестовое задание-маркер, письмо подтверждает владелец.
6. **Наблюдение 14 суток.** Точность алертов ≥ 80 %, ≤ 2 неприменимых в неделю.

**Предусловие.** Repo variable `UNITKA_ALERT_EMAIL` задана — канал и 3 существующие политики уже живут
(аудит 2026-10-04). Без неё политики DRO-1 не создаются, а детектор пишет состояние «в тишину».

### 9.1. Доверенный прогон и Gate C (2026-10-07)

Разовый прогон по расписанию: `jobs.run` на приостановленном job отклоняется (`FAILED_PRECONDITION`),
поэтому resume в 06:57 UTC → одно исполнение 07:00:00 UTC (HTTP 200) → pause в 07:00:10 UTC.
Повторных исполнений не было.

| Проверка | Результат |
|---|---|
| Доверенная личность | Родительское задание `job_NAU8G4OA8dXH4TygVe0UjMkiGvcK` (SCRIPT, 89 с) и все 24 дочерних — `sa-ops-health`, ошибок 0. Метки `dro_component` / `dro_kind` на месте. Маркеры alert / digest / heartbeat в журнале аудита — от `sa-ops-health` с этим родителем |
| Оценка | Снимок `dro1_20261007T070001_b1ab6c4e`: 40 строк, 34 HEALTHY, 6 не оцениваются, 0 проблемных |
| Инциденты | 4 инцидента 2026-10-05 закрыты `CONDITION_CLEARED`, 4 события RECOVERY переданы. Прежние OPEN не тронуты |
| Письмо пакета | `drob_20261007_1000` (4 восстановления) — инцидент 07:01:45 UTC, письмо получено владельцем |
| Письмо дайджеста | `drod_20261007` — инцидент 07:02:20 UTC, письмо получено владельцем |
| Пульс | Первая точка `dro_detector_heartbeat` — 07:02 UTC |
| Сторож | «Нет пульса 90 минут» — инцидент 08:46:34 UTC (через 104 мин после точки), письмо получено владельцем |

Это закрывает UNPROVEN из §11 по меткам дочерних заданий и каналу доставки.

**Gate C.** `infra/terraform/evetis_health.tf`: `paused = true` → `paused = false`, частота `*/30 * * * *`.

- Ожидаемый план (`infra.yml`, `targets=google_cloud_scheduler_job.dro_health_eval`): 0 to add, 1 to change,
  0 to destroy. Единственное изменение — `paused: true → false`.
- После apply:
  - прогоны в :00 и :30 идут под `sa-ops-health`;
  - точки пульса приходят каждые 30 минут;
  - открытый с 2026-10-07 инцидент сторожа закрывается сам;
  - второго дайджеста за сутки нет.
- Откат — `paused = true` и тот же targeted apply.

`No severity` в письмах (у политик не задан `severity`) — отдельный follow-up, не Gate C.

## 10. Откат

| Слой | Как откатить |
|---|---|
| Scheduler и политики | Terraform: удалить `evetis_health.tf` или поставить `paused = true` и применить targeted |
| Объекты BigQuery | `sql/health/dro1_99_rollback.sql`: вью → процедуры → функция → таблицы → TVF → контракт |
| Датасет | Terraform, после шага выше: `delete_contents_on_destroy = false` защищает от случайного destroy |
| Строки `scope='DRO_PIPELINE'` в `wb_ops` | остаются; на ADS-1B не влияют. Удаление — отдельный деструктивный ACK, запросы приведены в файле отката |

Откат не затрагивает загрузчики, витрины, Юнитку, Ozon runtime и ADS-1B.

## 11. Ограничения

- **Метки дочерних заданий.** То, что `@@query_label` метит дочерние задания процедуры, подтверждено
  документацией BigQuery. На проекте доказано доверенным прогоном 2026-10-07 (§9.1).
- **Канал доставки.** Доказан 2026-10-07 (§9.1): письма пакета, дайджеста и сторожа получены владельцем.
- **Задержка сторожа.** Порог 90 мин + выравнивание: на прогоне 2026-10-07 инцидент открылся через 104 мин
  после последней точки, т.е. ≈ 1 ч 15 мин – 1 ч 45 мин после пропущенного прогона.
- **Полнота без сигнала источника.** У 32 из 34 конвейеров стоит `COMPLETENESS_NOT_MEASURED`: загрузчики
  не пишут признаков полноты. Это P1 дизайна, и для него нужны изменения загрузчиков, включая образ Ozon,
  что вне DRO-1.
- **Юнитка Ozon.** LCD в BigQuery не наблюдаем, проверяется только свежесть успешного прогона.
- **Executive / SKU V2.** Лаг `source_data_through` = D-2 не оценивается (решение D6).
- **Ночная эскалация.** CRITICAL, кроме потери данных, ночью ждёт 07:00, включая витрину. Так задано тихими часами.
- **Реестр.** `V_OPS_CURRENT_HEALTH` в `wb_ops` не менялся. Честная версия — `evetis_health.V_HEALTH_CHECK_CURRENT`.
