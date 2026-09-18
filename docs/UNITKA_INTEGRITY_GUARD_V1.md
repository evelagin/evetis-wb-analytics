# UNITKA INTEGRITY GUARD V1

Статус на 18.09.2026: **Phase 1C2A — код, SQL, Terraform и workflow готовы к ревью (PR). В production НИЧЕГО
не применено**: ни вью, ни таблиц, ни процедуры, ни IAM, ни Scheduler, ни env Cloud Run.
Дизайн: Phase 1A (аудит), 1B (дизайн), 1C1 (реализация), 1C1B (архитектура доступа), 1C2A (финализация).
Код: `cloud/src/loaders/unitka/integrity.ts`.

## 0. Правила, которые нельзя забыть

> **DATA_ERROR ≠ ПАДЕНИЕ JOB'А.** `qa_status` — техническое исполнение и сверка записи (как раньше).
> `integrity_status` — можно ли доверять данным. Прогон может честно дать `qa_status = PASS` и
> `integrity_status = DATA_ERROR`; код выхода 0, факты записаны.

> **OBSERVED PRICE ≠ FACTUAL ORDER PRICE.** Цена из наблюдателя цен (`RAW_WB_PRICES`) попадает только в
> `diagnostic_value` с меткой `OBSERVED_PRICE_NOT_FACTUAL_ORDER_PRICE`. Никогда не подставляется в цену заказа
> и не участвует ни в одном финансовом расчёте. Пустая цена остаётся пустой.

> **COGS 252442517 (Крем для рук).** Канон — **231,38 ₽** (`evetis_ref.V_PRODUCT_COGS_EFFECTIVE`,
> PROVEN_DOCUMENT). 240 ₽ в `$R$45` листа — **известное неверное старое значение** (решение владельца D6).
> Исправление в production **намеренно отложено** до доказательства, что Guard ловит `COGS_SOURCE_MISMATCH`.

> **DRY_RUN=1 НЕ ОЗНАЧАЕТ «БЕЗ ЗАПИСИ В ЛИСТ».** `unitka` — не `prodOnly`: при `DRY_RUN=1` `cli.ts` пропускает
> lease, но **вызывает настоящий handler**; с `ENVIRONMENT=prod` и `UNITKA_WRITE_ENABLED=1` он пишет в книгу
> (закреплено тестом). Прогон без записи — **только** `unitka-engine-shadow` или `UNITKA_WRITE_ENABLED=0`.

> **Runtime-учётки Unitka не читают evetis_ref.** Канон COGS приходит из физической копии в wb_mart (§6).

## 1. Архитектура

```
wb_mart.V_UNITKA_INTEGRITY        факты SKU × день (без серьёзности); wb_raw/wb_mart
evetis_ref.V_PRODUCT_COGS_EFFECTIVE  (канон, НЕ меняется)
   ↓ sa-unitka-cogs-pub · Scheduler :50 07–23 МСК · CALL wb_mart.sp_publish_unitka_cogs
wb_mart.UNITKA_COGS_EFFECTIVE       физическая копия (internal_sku × интервал, published_at, отпечаток)
   ↓ wb_mart.V_UNITKA_COGS_CANONICAL  nm × день + snapshot_published_at; wb_raw/wb_mart, БЕЗ evetis_ref
снимок листа                        СПП (AB), формулы AI (COGS), блоки строки 735, $R$45
        │
integrity.ts (чистые правила) → IntegrityIssue[] → integrity_status
        │
qa_json.integrity (wb_ops.UNITKA_ENGINE_RUNS, без смены схемы) · лог-событие unitka_integrity
```

SQL: `sql/unitka/cogs_publication_v1.sql` (+ `_rollback`), `sql/unitka/integrity_v1.sql` (+ `_rollback`).
Terraform: `infra/terraform/unitka_cogs_publication.tf`. Серьёзность решает TypeScript; SQL отдаёт только факты.

Доказательство «runtime без evetis_ref» (18.09.2026): `referenced_tables` реального read-only задания с телом
`V_UNITKA_INTEGRITY` — только `wb_mart` и `wb_raw`; тело `V_UNITKA_COGS_CANONICAL` ссылается только на
`wb_mart.UNITKA_COGS_EFFECTIVE`, `wb_raw.REF_SKU_MASTER` и `wb_mart.V_UNITKA_LAST_CLOSED_DATE` (цепочка
wb_raw/wb_mart). Статический тест `cloud/test/unitka_integrity_sql.test.ts` не даёт границе разрушиться.

Семантика NULL: `factual_order_price NULL` = цены нет (≠ 0 ₽); `canonical_cogs NULL` = в свежей копии нет ровно
одного интервала (≠ COGS 0); `orders_funnel NULL` = строки воронки нет; `storage_value NULL` = дата не покрыта
отчётом, а 0 при покрытой дате — легитимный «хранения нет».

## 2. Правила V1

| Код | Условие (закрытый день ≤ LCD) | Серьёзность | blocking | financial_invalid |
|---|---|---|---|---|
| `PRICE_MISSING_WITH_ORDERS` | (Q > 0 или S > 0) и фактическая цена NULL; SKU с блоком | ERROR | да (строка) | да |
| `PRICE_MISSING_NO_ORDERS` | Q = 0, S = 0, цена NULL | INFO (только счётчик) | нет | нет |
| `COGS_ZERO_OR_MISSING` | копия свежая, и COGS в AI 0 / пусто / формула не по контракту, или канон не ровно один интервал / ≤ 0 (`COGS_CANONICAL_MISSING`) | ERROR при активности SKU в месяце, иначе WARNING (латентно) | при активности | при активности |
| `COGS_SOURCE_MISMATCH` | копия свежая, присутствие в порядке, \|лист − канон\| > 0,005 ₽ | WARNING | нет | нет |
| `COGS_SNAPSHOT_STALE` | последняя успешная публикация копии старше **26 ч** | WARNING | нет | нет |
| `COGS_SNAPSHOT_UNAVAILABLE` | копию не прочитать: нет вью/таблицы, пусто, ошибка запроса, неразборчивое время | WARNING | нет | нет |
| `SKU_WITHOUT_BLOCK` | активный SKU источника без блока в строке 735 | ERROR | да (покрытие) | нет |
| `BLOCK_WITHOUT_SKU` | блок без активного SKU | прежний `BLOCK_MISSING` Engine (fail-closed до записи) | — | — |
| `SPP_MISSING` | Q > 0 и ячейка AB **действительно пуста** (числовой 0 — заполнено) | MANUAL_REQUIRED | нет | нет (неполны ДРР/цена с СПП) |
| `STORAGE_MISSING` | дата не покрыта отчётом хранения; одна issue на дату | D−1 до 12:15 МСК EXPECTED_DELAY, с 12:15 WARNING; D−2 WARNING; старше ERROR | нет | нет |
| `ORDERS_SOURCE_DIVERGENCE` | воронка ≠ FACT_ORDERS | INFO; WARNING при ONLY_FUNNEL/ONLY_FACT или \|Δ\| ≥ 2 | нет | нет |

**Состояния копии COGS** (решения D2/D3): `COGS_VALID` — свежая копия (≤ 26 ч), интервал ровно один, COGS > 0;
`COGS_CANONICAL_MISSING` — свежая копия, для SKU нет ровно одного интервала (внутри `COGS_ZERO_OR_MISSING`);
`COGS_SNAPSHOT_STALE` и `COGS_SNAPSHOT_UNAVAILABLE` — **подавляют все вердикты COGS** в этом прогоне
(устаревшая копия не может ни подтвердить COGS, ни обвинить лист) и поднимают статус минимум до
PASS_WITH_WARNINGS: прогон никогда не кончается PASS, если COGS не проверялся. Пустая копия — UNAVAILABLE,
а не «канона нет». Штатная свежесть копии ≈ 1 ч; **26 ч — порог безопасности, а не целевой SLA.**

Парсер COGS принимает только live-контракт `=IF($<дата><r>>LAST_CLOSED_DATE,"",<AE><r>-<AF><r>-<AH><r>-<COGS>)`
(разделитель `,` или `;`), колонки и строка обязаны совпасть с геометрией блока; COGS — число или разрешённая
ссылка `$R$45`. Иное — `UNRECOGNISED_FORMULA` внутри `COGS_ZERO_OR_MISSING`. Допуск — полкопейки.

Статус: `SYSTEM_ERROR > DATA_ERROR > MANUAL_REQUIRED > PASS_WITH_WARNINGS > PASS`; INFO и EXPECTED_DELAY статус
не поднимают. `SYSTEM_ERROR` — только сбой исполнения (Engine упал до оценки, сломался Guard, исчерпан бюджет).

## 3. Режимы, бюджет, владелец настроек

| Режим `UNITKA_INTEGRITY_MODE` | Поведение |
|---|---|
| `off` (по умолчанию в коде) | Guard не вызывается, ни одного лишнего запроса; поведение = Engine 1.1.0 |
| `observe` (цель для SHADOW) | Оценка, `qa_json.integrity`, лог `unitka_integrity`; DATA_ERROR не бросает; сбой Guard → `SYSTEM_ERROR` в журнале, факт-запись и код выхода не меняются |
| `enforce` (зарезервирован, НЕ включать без решения владельца) | Как observe, но сбой самого Guard → `INTEGRITY_SUBSYSTEM_FAILURE`, exit 1 (после записи и журнала). DATA_ERROR не бросает и здесь |

Неизвестное значение → `off` + предупреждение `unitka_integrity_mode_invalid`. `UNITKA_STORAGE_DUE_MSK`
(`HH:MM`, по умолчанию 12:15). `UNITKA_INTEGRITY_BUDGET_MS` (по умолчанию 90 000, допустимо 5–300 с): остаток
бюджета передаётся в BigQuery как **серверный `jobTimeoutMs`** — зависшее задание отменяет сам BigQuery (не
брошенный промис). Исчерпание → `SYSTEM_ERROR / INTEGRITY_TIME_BUDGET_EXCEEDED`; факт-запись не откатывается,
журнал прогона пишется. Чтение `$R$45` из Sheets ограничено таймаутом шлюза (120 с) и делается только при
оставшемся бюджете. Запас: прогоны Engine ≤ 69 с при таймауте Job'а 600 с.

**Владелец runtime-значения режима — deploy-workflow**, не Terraform. У Job'ов `unitka-engine-*`
`lifecycle.ignore_changes` покрывает `env` (провайдер v7: `env` — set, точечно не исключить; см.
`cloud_run_jobs.tf`). `UNITKA_INTEGRITY_MODE = "off"` в `unitka_engine.tf` — только значение при СОЗДАНИИ Job'а;
правка его не меняет существующий Job. `deploy-shadow.yml` ставит `unitka-engine-shadow`
`UNITKA_INTEGRITY_MODE=observe`; `deploy-prod.yml` режим не задаёт → prod `off`. Внимание: `deploy-shadow`
срабатывает на push в `main` с изменениями `cloud/**` — merge этого PR передеплоит shadow-образ с `observe`
(Scheduler'ы shadow на паузе, вью Guard ещё нет → при ручном прогоне будет `SYSTEM_ERROR` в журнале shadow).
Откат режима: `UNITKA_INTEGRITY_MODE=off` (или `--remove-env-vars`) в workflow и передеплой.

Где оценивается: SHADOW — `PRE_WRITE` на текущем листе; PROD — `POST_WRITE` на перечитанном листе.

## 4. Отложено (решение D4)

Журнал issue `wb_ops.UNITKA_QA_ISSUES` и его IAM, алерты, условное форматирование, production-включение Guard —
после октябрьского rollover, отдельными гейтами. Для 1C3 доказательства — только `qa_json.integrity` и логи
`unitka_integrity`. Кода записи журнала в Engine нет (удалён в 1C2A): при любом режиме Guard ничего не пишет,
кроме строки `UNITKA_ENGINE_RUNS`.

## 5. Ячейки статуса WB738 / WB739 — ЗАРЕЗЕРВИРОВАНЫ

Проверка 18.09.2026 (live-экспорт + репозиторий): пусты; нет ссылок из формул, УФ, проверок данных, именованных
диапазонов, слияний; код использует только WB736/WB737. **WB738 = SAFE_TO_RESERVE, WB739 = SAFE_TO_RESERVE**.
В коде — только константы `INTEGRITY_STATUS_CELLS` (`model.ts`); Engine в них не пишет.

## 6. Публикация канонического COGS (sa-unitka-cogs-pub)

Почему не authorized view: доступ к `evetis_ref` управляется `google_bigquery_dataset_iam_member`
(ct_refresh, exec_v2_layer, sku_v2_layer); провайдер запрещает смешивать их с `google_bigquery_dataset_access`
(«Using any of these resources will remove any authorized view permissions»). Почему не логическая вью в
wb_mart: BigQuery проверяет права вызывающего на исходные таблицы. Решение — шаблон Executive V2 / SKU V2:
отдельная учётка читает канон и публикует физическую копию; Unitka читает её штатным доступом к wb_mart.

**Процедура** `wb_mart.sp_publish_unitka_cogs(trigger)`: замок (`_UNITKA_COGS_PUBLISH_LOCK`, зависший > 30 мин) →
staging (TEMP) → ASSERT P1–P8 (канон не пуст; число строк = канону; internal_sku и effective_from не пусты;
COGS > 0; нет `effective_to < effective_from`; нет дублей `(internal_sku, effective_from)`; нет пересечений
интервалов одного SKU) → отпечаток SHA-256 → транзакция DELETE+INSERT → `SUCCESS` в журнале → освобождение
замка. Любая ошибка → ROLLBACK, `FAILED`, копия от последней удачной публикации. Процедура таблиц не создаёт.

**Права `sa-unitka-cogs-pub`** (минимально практичные):

| Операция | Право | Роль | Область | Зачем |
|---|---|---|---|---|
| запуск CALL, TEMP-staging | `bigquery.jobs.create` | `jobUser` | проект (уже нельзя) | задание скрипта |
| вызов процедуры | `bigquery.routines.get` | `dataViewer` | ОДНА процедура (routine IAM) | CALL без доступа к датасету wb_mart |
| чтение канона | `bigquery.tables.getData` на вью и её базовых таблицах | `dataViewer` | датасет `evetis_ref` (`dataset_iam_member`) | логическая вью над несколькими таблицами; тип ресурса как у текущих читателей |
| DELETE+INSERT копии; UPDATE замка; INSERT/UPDATE журнала | `tables.getData`, `tables.updateData`, `tables.get` | `dataEditor` | потаблично: 3 таблицы | `dataViewer` не даёт `updateData`; кастомных ролей в репозитории нет |
| создание таблиц | — | не выдаётся | — | таблицы создаёт миграция §1 |
| метаданные таблиц | `tables.get` | входит в `dataEditor` на таблицу | 3 таблицы | DML |

`sa-loaders-*` доступа к `evetis_ref` не получают. Dataset-wide `dataEditor` на wb_mart не выдаётся.
Scheduler: `unitka-cogs-publication`, `50 7-23 * * *` Europe/Moscow (минута :50 свободна от других Job'ов;
09:50 обновляет копию перед окном Unitka 10:00; расписания не зависят друг от друга).

**Применение (1C2B, гейт владельца):** `cogs_publication_v1.sql` §1–§2 → целевой `terraform apply` ресурсов
`unitka_cogs_publication.tf` + actAs в `iam.tf` (ожидаемо 9 to add, 0 change, 0 destroy) → первый CALL /
первый плановый запуск (доказывает routine IAM; запасной вариант — `metadataViewer` на датасет wb_mart) →
`integrity_v1.sql`.

## 7. Сухой прогон (инструмент для 1C3)

```
cd cloud && npm run build
node scripts/unitka_integrity_dry_run.mjs integrity.json snapshot.json [cogs.json] [--now=ISO] [--cogs-published-at=ISO]
```
Входы — выгрузки read-only SELECT'ов тел вью (`bq query --format=json`) и снимок листа. Ничего не пишет. До
публикации физической копии `cogs.json` эмулируется read-only SELECT'ом тела `V_UNITKA_COGS_CANONICAL`, где
`UNITKA_COGS_EFFECTIVE` заменена каноном (только для офлайн-доказательства; runtime так не работает).

Прогон 18.09.2026 (реальные данные, `--now=2026-09-18T07:01:00Z`): 4 × PRICE_MISSING_WITH_ORDERS (08.09/305101361,
09.09/930334396, 10.09/305101361, 17.09/930334396); свежая копия — COGS_SOURCE_MISMATCH 252442517 (`$R$45=240` ↔
231,38), COGS_ZERO_OR_MISSING 252442341 и 252441968 (WARNING, латентно); SKU_WITHOUT_BLOCK 909951444;
SPP_MISSING 13 (владелец дозаполнил 17.09/252442517); статус DATA_ERROR. Копия 27 ч → только COGS_SNAPSHOT_STALE;
копии нет → только COGS_SNAPSHOT_UNAVAILABLE.

## 8. Раскатка (каждый production-шаг — отдельный гейт владельца)

1C2A код + PR (этот документ) → 1C2B инфраструктура (SQL-миграция публикации, целевой Terraform, первый CALL,
вью Guard) → 1C3 shadow-прогон на истории → **пауза Guard, приоритет — календарь / октябрьский rollover** →
журнал issue и prod observe → УФ на копии → алерты → включение → только затем исправление COGS 252442517.

## 9. Откат

- Режим: `off` в `deploy-shadow.yml` и передеплой (prod уже `off`).
- Вью Guard: `sql/unitka/integrity_v1_rollback.sql`.
- Публикация: пауза Scheduler `unitka-cogs-publication` → `sql/unitka/cogs_publication_v1_rollback.sql`
  (производные объекты; журнал публикаций сохраняется) → целевой destroy `unitka_cogs_publication.tf`.
- Код: образ предыдущего digest; при `off` поведение = 1.1.0 и без отката.
- Бизнес-данные Unitka Guard не меняет — откатывать в листе нечего; истина COGS остаётся в `evetis_ref`.
