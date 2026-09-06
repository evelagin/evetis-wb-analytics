# Stage ADS-1B — детектор здоровья конвейеров WB

**06.09.2026 · объекты BigQuery развёрнуты и проверены · автозапуск ожидает решения владельца**

SQL: `sql/ops/ads1b_health_detector.sql` · Инфра: `infra/terraform/ops_health.tf`

---

## 1. Что было до этапа

`wb_ops` существует с Stage 3.1A и содержит проработанный контракт: реестр из 14 конвейеров
с SLA и критичностью, три таблицы жизненного цикла с говорящими `OPTIONS.description`.

Работал он при этом **нулевым образом**:

| Таблица | Строк до ADS-1B |
|---|---:|
| `OPS_PIPELINE_REGISTRY` | 14 |
| `OPS_METRIC_COVERAGE` | 8 |
| `OPS_HEALTH_STATE` | **0** |
| `OPS_ALERT_EVENT` | **0** |
| `OPS_INCIDENT` | **0** |

🔴 **Отдельная находка: весь датасет `wb_ops` не имел ни строки в репозитории** — ни DDL,
ни дизайна, ни доменов значений. Тот же класс дефекта, что Stage 1.6 нашёл в
`sp_build_mart_sku_daily`, а Stage ADS-1A — в `REF_COST_MAP`. Схема задавала *структуру*
(поля, ключи, партиционирование) и *намерение* (в описаниях), но **не задавала значения**:
что такое `health_status`, какие бывают `reason_code`, `alert_type`, `state`, как строятся
`condition_fingerprint` и `incident_id`. Всё это пришлось завести здесь — и это
зафиксировано как новый контракт, а не как «использование существующего».

## 2. Архитектура

```
OPS_PIPELINE_REGISTRY  (SLA, критичность, каденс — источник порогов)
        │
        ▼
sp_evaluate_pipeline_health()      ← вычисляет H0–H6 по живым данным
        │  ARRAY<STRUCT<...>>
        ▼
sp_ops_apply_health(results, ts)   ← чистое применение переходов состояния
        ├──► OPS_HEALTH_STATE   (append-only снимки)
        ├──► OPS_INCIDENT       (MERGE: открыть / продлить / закрыть)
        └──► OPS_ALERT_EVENT    (только на переходах)
        │
        ▼
V_OPS_CURRENT_HEALTH · V_OPS_ACTIVE_INCIDENTS   ← dashboard-ready
```

Разделение на две процедуры сделано не для красоты: оно даёт **детерминированный
lifecycle-тест на изолированном scope** без порчи production (§6).

## 3. Домены значений (заводятся ADS-1B)

| Поле | Значения |
|---|---|
| `health_status` | `HEALTHY` · `UNHEALTHY` |
| `verification_status` | `OBSERVED` — проверка выполнена по живым данным |
| `commissioning_status` | `ACTIVE` |
| `scope` | `PIPELINE_CHECK` (production) · `TEST_LIFECYCLE` (фикстура) |
| `scope_id` | `<pipeline_id>/<check_id>` |
| `incident.state` | `OPEN` · `RESOLVED` |
| `alert_type` | `OPEN` · `RECOVERY` |
| `alert.state` | `RECORDED` |
| `condition_fingerprint` | `SHA256(scope\|scope_id\|reason_code\|subject_key)` |
| `incident_id` | `<fingerprint[0:16]>#<occurrence_seq>` |
| `alert_event_id` | `<incident_id>\|<alert_type>` |

🔴 **`CLOSED_UNRECOVERABLE` намеренно не реализован.** Описание таблицы его предусматривает,
но он требует политики `recovery_deadline_ts` («сколько ждём, прежде чем признать потерю
необратимой»), а её нет. Заводить срок наугад для невосстановимых снимков ставок нельзя.
Оставлено следующему этапу; `is_data_loss`/`is_recoverable` уже заполняются из реестра,
так что данные для этого решения копятся.

🔴 **Доставка алертов НЕ реализована — и это видно в данных, а не спрятано.**
Схема `OPS_ALERT_EVENT` предполагает доставщика (`channel`, `recipient_mask`, `sent_at`,
`attempt_count`, `next_retry_at`, семантика AT_LEAST_ONCE). ADS-1B доставку не строит
(прямо исключено из периметра), поэтому события пишутся как `state='RECORDED'`,
`channel='NONE'`, `recipient_mask='n/a'`. Это честное «событие зафиксировано, никому не
отправлено». Будущий доставщик должен забирать строки в состоянии `RECORDED`.
**Пока алерт — это запись в таблице, а не оповещение.** Выбор канала и получателя —
решение владельца.

## 4. Проверки

| check_id | Конвейер | Правило | Порог | Severity | Источник порога |
|---|---|---|---|---|---|
| `H1_MART_REPEATED_FAILURE` | mart | ≥2 ERROR подряд после последнего COMPLETE на текущем `target_date` | ≥2 | CRITICAL | registry `criticality` |
| `H2_MART_LAG` | mart | `MAX(MART_SKU_DAILY.day)` против последней закрытой даты (D−1 МСК) | > 2 сут | CRITICAL | registry `expected_business_lag_days=2` |
| `H3_ADS_INGESTION_STALE` | ads_daily | время с последнего `COMPLETE` в `INGEST_RUNS` | > 36 ч | HIGH | registry `freshness_sla_minutes=2160` |
| `H4_BID_SNAPSHOT_MISSING` | ads_query_bids | время с последнего успешного снимка ставок | > 36 ч | HIGH | registry `freshness_sla_minutes=2160` |
| `H5_QUERY_COVERAGE_DEGRADED` | ads_query_stats | `coverage_ratio` на последней закрытой дате | < 0,90 | MEDIUM | статистика истории, §5 |
| `H6_FINANCE_UNKNOWN_PAIRS` | finance | пары `(op_key, amount_field)` вне `REF_COST_MAP` | > 0 | HIGH | урок ADS-1A |
| `H0_DETECTOR_HEARTBEAT` | health_detector | отметка последнего прогона (сам себя не оценивает) | — | MEDIUM | §7 |

🔴 **H1 отличается от буквальной формулировки ТЗ, и это обнаружено на практике.**
Правило «≥2 ERROR на один `target_date` без последующего COMPLETE» по всей истории даёт
**вечное срабатывание**: `target_date` 02–04.09 никогда не получат COMPLETE, потому что
`sp_build_mart_sku_daily` делает полную пересборку и конвейер уходит на новый `target_date`.
Данные за те сутки при этом восстановлены. Первый bootstrap с буквальным правилом дал
`UNHEALTHY` на заведомо здоровом проде — ложное срабатывание было снято, состояние
переинициализировано. Операционно проверка отвечает на вопрос «сломан ли конвейер **сейчас**»,
поэтому берётся последний по времени `target_date` и считаются ERROR в сегменте **после
последнего COMPLETE**. Раннее обнаружение при этом не теряется (§6). Устаревание данных
за прошлые сутки ловит H2, а не H1.

🔴 **H5 не использует `query_spend / total_ad_spend`.** По находке B-2 из ADS-0 query-разбивка
покрывает лишь ~35 % рекламного бюджета — это свойство API WB, а не дефект. Используется
существующий `coverage_ratio`, измеряющий покрытие **scope, доступного query-источнику**.

## 5. Обоснование порога H5

Распределение `coverage_ratio` по всей истории (143 суток со значением; ещё 3 суток
`NULL` — там нулевой биллинг, знаменателя нет, такие сутки в проверку не входят):

| min | p01 | p05 | p10 | median | p95 | max |
|---:|---:|---:|---:|---:|---:|---:|
| 0,8211 | 0,8409 | 0,9353 | 1,0011 | 1,0110 | 1,1481 | 2,6433 |

Ниже 0,90 — **4 суток из 143 (2,8 %)**; ниже 0,80 — ноль. Порог 0,90 лежит чуть ниже p05,
даёт ~3 % срабатываний в год и каждое из них — реально неполные сутки (0,82–0,89),
восстановимые ручным backfill (`ads_query_stats.is_backfillable = true`). Порог принят.

## 6. Исторический replay на реальном инциденте

Read-only, по фактическим `MART_RUNS` за 02–06.09.2026. Исторические ops-таблицы не менялись.

| Проверка | Условие стало истинным | Что создал бы детектор | Severity | Раньше фактического обнаружения |
|---|---|---|---|---:|
| **H1** | **2026-09-03 06:04 UTC (09:04 МСК)** | `UNHEALTHY` на `mart/H1`, инцидент `MART_REPEATED_ERROR`, subject `target_date=2026-09-02`, alert OPEN | CRITICAL | **77,6 ч** |
| **H2** | 2026-09-05 (сутки МСК), lag=3 сут при допуске 2 | `UNHEALTHY` на `mart/H2`, инцидент `MART_LAG_EXCEEDED` | CRITICAL | 35,7 ч |

Фактически отказ обнаружен 06.09 около 14:42 МСК, побочно, при работе над доменом Ozon
(`docs/ops/INCIDENT_2026-09-06_WB_MART_FAILURE.md`). H1 поймал бы его **на трое суток раньше**,
на втором подряд отказе, а не на пятнадцатом.

Уточнённое правило H1 (§4) даёт **то же самое время срабатывания**: на 03.09 06:05 UTC
последний `target_date` = 2026-09-02, ERROR после последнего COMPLETE = 2 → `UNHEALTHY`.

## 7. Отказ самого детектора

Рекурсивный мониторинг мониторинга не строится. Минимальное решение: детектор пишет
собственную отметку `health_detector/H0_DETECTOR_HEARTBEAT` с `observed = <время прогона>`.
Остановка детектора видна как **устаревший `last_checked_at`** в `V_OPS_CURRENT_HEALTH`:
если отметка старше нескольких часов, значит прогонов нет. Вторым независимым источником
остаётся история выполнений Cloud Scheduler job.

## 8. Тест жизненного цикла

Изолированный scope `TEST_LIFECYCLE`, синтетическая фикстура, production не затронут.
После снятия доказательств строки удалены.

| Шаг | Вход | health | Инцидентов | Alert-событий |
|---|---|---|---:|---:|
| 1 · 20:00 | HEALTHY | `HEALTHY` [переход] | 0 | 0 |
| 2 · 20:10 | UNHEALTHY | `UNHEALTHY` [переход] | **1 OPEN** | **1 OPEN** |
| 3 · 20:20 | UNHEALTHY | `UNHEALTHY` | 1 (obs=2) | 1 |
| 4 · 20:30 | UNHEALTHY | `UNHEALTHY` | 1 (obs=3) | 1 |
| 5 · 20:40 | HEALTHY | `HEALTHY` [переход] | **1 RESOLVED** | **2** (+RECOVERY) |
| 6 · 20:40 повторно | HEALTHY | `HEALTHY` | 1 RESOLVED | 2 |

Итог: `incidents_total=1`, `incidents_open=0`, `closure_reason=CONDITION_CLEARED`,
`observation_count=3`, alert-событий ровно **2** — `OPEN @20:10` и `RECOVERY @20:40`.
Три подряд `UNHEALTHY` не породили ни второго инцидента, ни шторма алертов;
повторный recovery (шаг 6) не создал дубля и не переоткрыл инцидент.

## 9. Bootstrap production

Первый прогон с буквальным H1 дал ложный `UNHEALTHY` (§4). Правило уточнено, артефакты
удалены, состояние переинициализировано начисто:

**7 проверок, все `HEALTHY`, 0 инцидентов, 0 alert-событий.** Ложных исторических
инцидентов не создано — требование §14 ТЗ выполнено.

## 10. Расписание — НЕ ВКЛЮЧЕНО

`infra/terraform/ops_health.tf`: `terraform validate` — **Success**, `terraform fmt` — чисто.
**`terraform apply` не выполнялся.**

| Параметр | Значение |
|---|---|
| Механизм | Cloud Scheduler → BigQuery `jobs.insert` (тот же resource и паттерн, что `wb-mart-prod`) |
| Job | `wb-ops-health-prod`, region `europe-west1` |
| Каденс | `0 */3 * * *`, `Europe/Moscow` |
| Обоснование каденса | самый короткий осмысленный SLA реестра — `orders.freshness_sla_minutes = 180` (3 ч) |
| Identity | новый SA `sa-ops-health` + jobUser + dataEditor на `wb_ops` + dataViewer на `wb_raw`/`wb_mart` |
| Retry / deadline | `retry_count = 1`, `attempt_deadline = 320s` |
| Начальное состояние | `paused = true` — как у всех job'ов проекта |

**Почему не применено — проверено 06.09.2026, два независимых блокера.**

1. 🔴 **План содержит 4 unrelated изменения**, ни одно не относится к ADS-1B:
   `ozon_runtime["ozon-runtime-daily"]`, `["ozon-runtime-fast"]`, `["ozon-runtime-weekly"]`
   (от **незакоммиченной** правки `infra/terraform/ozon_ingestion.tf` — Stage 3.4D.2/3.4D.3,
   смена `ozon_runtime_image` и сущность `seller_info`) и `wb_stocks_shadow`
   (дрейф: `client`/`client_version` `"gcloud"`/`"568.0.0"` → `null`, job трогали мимо Terraform).
   `terraform apply` применяет всю конфигурацию, а не файл — включение детектора протащило бы
   с собой чужой Ozon-релиз. Прямое STOP-условие.
2. 🔴 **`terraform plan` завершается кодом 1**: 11 предсуществующих ресурсов падают на refresh
   с HTTP 403 `getIamPolicy` у локальной учётки — та же стена 403, что у BigQuery REST с этой
   машины. Ресурсов `ops_health` в ошибках нет; все шесть корректно планируются к созданию:
   **Plan: 6 to add, 4 to change, 0 to destroy** (`destroy = 0` подтверждён).

**Штатный путь** — `.github/workflows/infra.yml` (`workflow_dispatch`, `action=apply`,
WIF + `TERRAFORM_APPLY_SA`, ручной approval через environment `infra`). Инфраструктура
проекта применяется из CI привилегированным SA, а не с ноутбука — поэтому локальная
учётка и получает 403. Порядок: развести unrelated изменения → влить декларацию в main →
запустить workflow `infra` с `action=apply`.

**До apply** детектор запускается вручную: ``CALL `wb_ops.sp_evaluate_pipeline_health`()``.

Контейнер и Cloud Run job не нужны — процедура уже в BigQuery. BigQuery scheduled query
отвергнут: это новый для проекта механизм. Встраивание в `wb-mart-prod` отвергнуто:
нарушило бы fail-open.

## 11. Fail-open

Детектор не вызывается ни из одного конвейера данных и не может их заблокировать:
отдельный scheduler job, отдельный SA, отдельная процедура. Обратное тоже верно —
отказ витрины не мешает детектору работать, что и требуется, чтобы он о ней сообщил.

## 12. Чего этап не делает

Campaign config extraction, bids analytics mart, advertising dashboard, Decision Engine,
Metabase advertising cards, organic rank, BL-8 global parity framework, автоуправление
ставками, API writes. Metabase-карточки не создавались — только dashboard-ready вью.

## 13. Backlog

| # | Находка | Severity | Целевой Stage |
|---|---|---|---|
| BL-8 | Parity-гейт есть только у `pr_mart2a`; остальные seed/DDL-скрипты `sql/` не защищены | HIGH | ADS-10 |
| **BL-9** | **Датасет `wb_ops` не имел представления в репозитории; §0 закрывает это как as-is снимок, но таблицы Stage 3.1A по-прежнему создаются вне репозитория** | **HIGH** | ADS-10 |
| **BL-10** | **Доставка алертов не реализована: события копятся в `RECORDED`, канал и получатель не выбраны.** Рекомендация: сначала исследовать существующий датасет `evetis_communications` (`communication_events`, `communications_current`, `communication_engine_shadow`) и Cloud Run сервис `evetis-wb-communications` — возможно, доставщик уже есть. Параллельную систему доставки не создавать. | **HIGH** | требует решения владельца |
| **BL-11** | **`CLOSED_UNRECOVERABLE` не реализован — нет политики `recovery_deadline_ts` для невосстановимых снимков ставок** | MEDIUM | после BL-10 |
| **BL-12** | **`terraform apply` не выполнен — автозапуска нет.** Два блокера, см. §10: план содержит 4 unrelated изменения (3× Ozon runtime от незакоммиченного `ozon_ingestion.tf` + дрейф `wb_stocks_shadow`), и `terraform plan` падает кодом 1 на 403 `getIamPolicy` у локальной учётки. Штатный путь — workflow `infra`. | **HIGH** | решение владельца |
| **BL-13** | **Внешний watchdog для `wb-ops-health-prod`.** H0 внутри самого детектора не способен обнаружить, что детектор вообще перестал запускаться: если прогонов нет, то и строки H0 не появляется. Сейчас единственное доказательство — control-plane история выполнений Cloud Scheduler. Нужен независимый от детектора наблюдатель. | MEDIUM | после BL-12 |
| BL-1…BL-7 | См. `docs/ADS1A_MART_RECOVERY_2026-09-06.md` §11 | — | — |
