# EXECUTIVE V2 · PHASE C2 — материализованный дневной слой

Дата: 2026-09-17. Предыдущая фаза: `docs/EXECUTIVE_V2_PHASE_C_PERFORMANCE_2026-09-16.md` (аудит и прототип).

**Это только performance refactor.** Ни один metric contract, знак, формула, правило покрытия или
финальности не менялся. Реклама в результате — биллинг по дате услуги, возмещения WB — MEMO,
минимальный платёж — расход, COGS по дате, когортный выкуп — прежний контракт. KI-1/KI-2/KI-3 не трогались.

## 1. Что сделано

| Объект | Тип | Назначение |
| --- | --- | --- |
| `wb_mart.EXECUTIVE_V2_DAILY` | таблица, 743 строки, грейн `day` | детерминированные значения канонических `V_DASH_*` (pass-through, 73 колонки; view — 84) |
| `wb_mart.EXECUTIVE_V2_BUILD_LOG` | таблица | журнал сборок: `STARTED` / `SUCCESS` / `FAILED` / `SKIPPED_LOCKED` |
| `wb_mart._EXECUTIVE_V2_BUILD_LOCK` | таблица, 1 строка | замок против наложения сборок (зависший — старше 90 мин) |
| `wb_mart.sp_build_executive_v2_daily(trigger)` | процедура | TEMP → ASSERT → транзакционная подмена |
| `wb_mart.V_DASH_EXECUTIVE_V2_DAILY` | view | контракт Metabase: календарь и признаки момента чтения + свежесть |
| `sa-exec-v2-layer`, `executive-v2-layer-build` | Terraform | Cloud Scheduler → BigQuery `CALL`, каждый час в :10, 07:10–23:10 МСК |
| карточки 187–217 «… · слой C2» | Metabase, коллекция 6 | копии 31 карточки dashboard 2 на слое |
| dashboard 2 | Metabase | переключён на копии; id, раскладка, заголовки, фильтр — прежние |

Файлы: `sql/dash/executive_v2_daily_v1.sql`, `sql/dash/executive_v2_daily_validation.sql`,
`sql/rollback/executive_v2_daily_2026-09-17/R_DROP_LAYER.sql`, `infra/terraform/executive_v2_layer.tf`,
`infra/terraform/iam.tf` (actAs), `tools/metabase_exec_v2_c2_switch.py`,
`metabase/rollback/exec_v2_phase_c2_before/` (снимок Phase B + соответствие карточек),
`docs/executive_v2_c2_evidence/` (plan, apply, эквивалентность).

## 2. Детерминированное против момента чтения

В таблицу пишется только то, что не зависит от `CURRENT_DATE` / `CURRENT_TIMESTAMP`. Во view пересчитываются
**теми же выражениями, что в канонических view**:

| Признак | Канонический источник | Во view |
| --- | --- | --- |
| календарь до сегодня | `V_DASH_COVERAGE_DAILY` | `GENERATE_DATE_ARRAY(MIN(day), CURRENT_DATE('Europe/Moscow'))` |
| `is_current_day` | `day = today` | `day >= today OR day >= DATE(layer_built_at)` ¹ |
| `ads_billing_is_final` | `covered AND billed_complete` | `covered AND stable_ok AND DATE_DIFF(today, day) >= settle_days` |
| `exec_provisional_ads_billing_day`, `executive_incomplete/provisional/final_day` | `V_DASH_EXECUTIVE_ECONOMICS_DAILY` | те же `IF(...)` |
| `cohort_provisional_day` | `V_DASH_BUYOUT_COHORT_DAILY` | `NULL` без когорты, иначе `unresolved + conflict = 0 AND NOT current` |

¹ Второе условие срабатывает только между полуночью и первой сборкой нового дня (00:00–07:10): сутки,
снятые до своего окончания, не выдаются за полные. При сборке «сегодня» условие совпадает с каноническим.
Совпадение пересчёта с каноническими статусами проверяет ASSERT A6 при каждой сборке.

`stable_ok = stable_reads >= contract_n_stable` и `settle_days` материализуются; равенство
`billed_complete = requested_ok AND age_days >= settle AND stable_ok` проверено на 157 днях, 0 расхождений.

## 3. Сборка и отказоустойчивость

Порядок `sp_build_executive_v2_daily`:
1. журнал `STARTED`;
2. замок (`UPDATE … WHERE NOT is_running OR stale`; 0 строк → `SKIPPED_LOCKED`, выход);
3. `CREATE TEMP TABLE` из канонических view — production-таблица не затронута;
4. ASSERT до подмены:
   - A1 — `day` уникален и не NULL;
   - A2 — календарь без дыр;
   - A3 — последний день = сегодня (МСК), ровно одни текущие сутки = канонические;
   - A4 — тождества: прибыль = до COGS − COGS, выплата = товар + удержания, сумма post-sale-статей, цепочка
     цены продавца, отмены = к клиенту + от клиента, разбиение когорты;
   - A5 — повторное **построчное** чтение каждого из шести канонических view (все взятые колонки, включая
     одноимённые колонки CORRECTED/ECONOMICS, которые слой хранит одной копией; по одному view на запрос);
   - A6 — пересчитанные статусы момента чтения = канонические;
5. `BEGIN TRANSACTION; DELETE; INSERT; COMMIT` — читатель видит старую или новую версию целиком;
6. журнал `SUCCESS`, замок освобождён. Любая ошибка → `ROLLBACK` (флаг `v_in_tx`), журнал `FAILED`, замок
   освобождён, `RAISE`.

Проверено на production-процедуре (копии с внедрённой ошибкой, после проверки удалены):

| Сценарий | Результат |
| --- | --- |
| ASSERT падает до подмены | таблица 743 строки, прежний `layer_run_id`, прежняя сумма прибыли; `FAILED`; замок свободен |
| ошибка внутри транзакции после `DELETE` | `ROLLBACK_TRANSACTION`, таблица не изменилась; `FAILED`; замок свободен |
| два запуска одновременно | A — `SUCCESS`, B — `SKIPPED_LOCKED` за 3 с |

⚠️ Первый прогон отказа нашёл дефект: `@@transaction_id` недоступен в обработчике исключений — обработчик падал
и оставлял замок. Исправлено флагом `v_in_tx`, замок того прогона освобождён вручную (запись в журнале).

Стоимость сборки (замер 17.09): **~77 с, 1,8 ГБ, 13,1 тыс. slot-с**; сама сборка — 354 МБ, остальное —
повторные чтения A5. 17 сборок в сутки ≈ **31 ГБ и 223 тыс. slot-с в сутки** независимо от числа открытий
дашборда (одно открытие до C2 = 7,5 ГБ, 47 тыс. slot-с). Снижение (пропуск сборки при неизменных источниках) —
не делалось, это отдельное решение.

## 4. Свежесть

Карточка «Свежесть данных» (199): **«Данные по»** = `source_data_through` (`V_DASH_FRESHNESS_HEADER.data_as_of_min`,
дата исходных данных); **«Витрина собрана»** = `finished_at` последней успешной сборки слоя
(журнал по `layer_run_id`), а не время запуска процедуры и не сборка `MART_SKU_DAILY`. Если последняя попытка
`FAILED` или `STARTED` старше 90 минут, view отдаёт `layer_build_alert`, и карточка дописывает
«· ⚠ последняя сборка не удалась» / «· ⚠ сборка зависла». В view также есть `mart_built_at`,
`last_build_status`, `last_build_started_at`, `last_build_error`.

## 5. Оркестрация

`infra/terraform/executive_v2_layer.tf` — прецедент `ct_refresh.tf`. Права минимальные: jobUser; dataViewer на
`wb_mart`, `wb_raw`, `evetis_ref` (состав проверен по `referenced_tables` сборки); dataEditor потаблично на
три таблицы слоя.

Расписание `10 7-23 * * *` Europe/Moscow: витрина `wb-mart-prod` в окнах 07/09/12/16 завершается к :04–:06,
поэтому 07:10 / 09:10 / 12:10 / 16:10 — обязательная пересборка после витрины; остальные часы подхватывают
загрузчики. `retry_count = 0`, наложения исключает замок.

Plan (целевой, из рабочей копии, backend `evetis-wb-tfstate-37074083763/evetis/wb-cloud`):

```
terraform plan -target=google_service_account.exec_v2_layer
  -target=google_project_iam_member.exec_v2_layer_job_user
  -target=google_bigquery_dataset_iam_member.exec_v2_layer_read
  -target=google_bigquery_table_iam_member.exec_v2_layer_write
  -target='google_service_account_iam_member.terraform_apply_actas["exec_v2_layer"]'
  -target=google_cloud_scheduler_job.exec_v2_layer_build
Plan: 10 to add, 0 to change, 0 to destroy.
Apply complete! Resources: 10 added, 0 changed, 0 destroyed.
```

Полный вывод: `docs/executive_v2_c2_evidence/terraform_plan_2026-09-17.txt`, `…_apply_…`. После apply:
принудительный запуск job — `SUCCESS` под `sa-exec-v2-layer` (75 с); плановый 10:10 — `SUCCESS` (77 с).

## 6. Эквивалентность

| Проверка | Объём | Результат |
| --- | --- | --- |
| `executive_v2_daily_validation.sql` — вся история, построчно, EXCEPT DISTINCT в обе стороны, по каждому каноническому view; дни без канонической строки = NULL; свежесть; журнал | 16 ASSERT | **16/16** |
| SQL карточки на канонических view против того же SQL на слое, сырые значения BigQuery, NUMERIC до последнего знака | 30 карточек × 6 периодов | **180/180 идентичны** |
| `mb card query --format-rows`: карточка Phase B против копии C2 | 31 × 6 | **180/186**; 6 различий — только «Витрина собрана» у 159/199 (намеренно) |
| Текст экрана dashboard 2, 31.08–13.09, до и после переключения | 172 строки | идентичны, кроме «Отредактировано…» и «Витрина собрана 07:03 → 10:11» |
| Фильтр периода в браузере после переключения | последние 30 дней; 01–17.09 с текущими сутками | значения = эталону CLI |

Периоды: P1 31.08–13.09 · P2 17–30.08 · P3 27.07–23.08 · P4 последние 30 дней (18.08–16.09) · P5 01–16.09 ·
P6 01–17.09 (текущие сутки). Отчёт: `docs/executive_v2_c2_evidence/equivalence_bq_report.json`.

Статусы периодов (одинаково в Phase B и C2):

| Период | Статус | Предв./оконч./неполные |
| --- | --- | --- |
| P1 | 🟡 ПРЕДВАРИТЕЛЬНЫЕ | 11 / 3 / 0 |
| P2 | 🟢 ОКОНЧАТЕЛЬНЫЕ | 0 / 14 / 0 |
| P3 | 🟢 ОКОНЧАТЕЛЬНЫЕ | 0 / 28 / 0 |
| P4 | 🔴 НЕПОЛНЫЕ | 13 / 16 / 1 |
| P5 | 🔴 НЕПОЛНЫЕ | 13 / 2 / 1 |
| P6 | 🔴 НЕПОЛНЫЕ | 13 / 2 / 2 |

Контроль P1 (17.09, оба пути): заказы 202, выкупы 195, выручка 139 439,61, оплачено 112 365,07, реклама (биллинг)
20 748, логистика 13 683,76, хранение 1 817,13, тарифная опция 2 382, прочие 38, результат до COGS 37 535,84,
COGS 43 017,52, **прибыль −5 481,68**, маржа 26,92 % / −3,93 %, к перечислению за товар 76 204,65, удержания
−38 738,89, **выплата 37 465,76**, документы «WB Продвижение» 20 818, выкуп по когортам 92,26 % (155/168, ждут 34).

Отличие от контроля 16.09 (−5 483,68; 12/2/0) — дозагрузка канонических данных, не слой: биллинг рекламы
20 750 → 20 748, заказы 199 → 202, сутки дозрели до окончательных.

## 7. Производительность (браузер, 1440 px, 31.08–13.09, холодная загрузка)

| Метрика | BEFORE (Phase B) | AFTER (C2, dashboard 2) |
| --- | --- | --- |
| Запросов карточек | 37 | 37 |
| Последняя плитка загружена | **66,9 с** | **14,8 с** (повтор 17,9 с; тест-dashboard 14,3 с) |
| HTTP-запрос карточки: медиана / p90 / max | 6,2 / 12,7 / 15,7 с | 1,7 / 2,2 / 2,2 с |
| BigQuery exec: медиана / p90 | 4,67 / 10,0 с | 0,68 / 1,13 с |
| Обработано | 7,548 ГБ | 0,002 ГБ |
| Slot-время | 47 448 slot-с | 163 slot-с |
| Ошибки / cache hits | 0 / 0 | 0 / 0 |

Цель 8–12 с не достигнута на 3–6 с. **Узкое место — не BigQuery**: исполнение 0,58–0,68 с + 0,27 с очереди
заданий, остальные ~0,8 с запроса — Metabase (jobs.insert/poll, сериализация). Фронтенд Metabase держит
не больше 5 запросов параллельно (замер: max concurrency = 5, HTTP/1.1), 37 запросов × ~1,6 с ÷ 5 ≈ 12 с + 1,4 с
до первого запроса. 7 из 37 запросов — одна карточка 164 (цепочка цены на 7 плитках). Ускорение дальше —
только уменьшением числа запросов (объединить плитки 164 в одну карточку) или кешем Metabase; оба пути меняют
раскладку/настройки и не входят в C2.

## 8. Откат

Проверен **до** переключения production на временном dashboard 10 (затем архивирован): раскладка Phase B →
переключение → откат → сравнение со снимком (позиции, card_id, `visualization_settings`, `parameter_mappings`,
параметры) — совпадает полностью; повторное переключение — тоже.

```
python3 tools/metabase_exec_v2_c2_switch.py --rollback     # dashboard 2 → карточки Phase B
gcloud scheduler jobs pause executive-v2-layer-build --location=europe-west1   # при необходимости
```

Откат не трогает канонические `V_DASH_*`; слой может остаться изолированным. Удаление объектов —
`sql/rollback/executive_v2_daily_2026-09-17/R_DROP_LAYER.sql` (после отката Metabase и Terraform).

## 9. Известное, не исправлялось

- Старые валидации `pr_dash_finance_corrected_validation.sql` (C-7), `pr_dash_executive_economics_validation.sql`
  (D-12) — уже отмечены устаревшими 16.09. `pr_dash_settlement_validation.sql` G-11 теперь падает ложно:
  `LIKE '%ratio%'` находит «ope**ratio**ns» / «remune**ratio**n» в колонках Phase 4 (GAP-01/05, view изменён
  16.09 в 16:15). Не связано с C2.
- `D-12` дополнительно разойдётся на 4 объекта C2 в `wb_mart`.
- Карточки Phase B (42–44, 49, 50, 53, 54, 74–76, 157–186) остались вне раскладки как эталон и путь отката.
