# Tenancy T5 — активация арендатора: учётные данные, привязка, возможности, backfill, сверка

Дата: 2026-09-28. Статус: **аудит T5-A выполнен, блокеры runtime T5-G исправлены (PR), остальное —
проект до решений владельца.** Реальных учётных данных нет, Scheduler на паузе, EVETIS не менялся.

Источники фактов:
- **Swagger Seller API и Performance API от 2026-09-28.** Сняты Playwright с
  `docs.ozon.ru/api/{seller,performance}/swagger.json`: 481 и 47 путей. Прямой запрос уходит в цикл
  JS-редиректов (anti-bot), Firecrawl получает 403.
- **Снимки от 2026-08-31** из `gs://evetis-audit-evidence-37074083763/ozon/audit_2026-08-30/raw/api/`.
  Хеши совпали с `MANIFEST.csv`.
- **Живая история EVETIS:** `docs/ozon/OZON_API_HISTORY_LIMITS_2026-09-03.md`.
- **Код:** `pipelines/ozon/runtime/*`, `tools/tenancy/*`, `sql/tenant/ozon/tenant_ops/*`.

## A. Находки аудита

| # | Блокер из ACK | Факт (код / API) | Статус |
|---|---|---|---|
| 1 | Performance > 62 дней | Swagger: «Лимит на количество дней в выгрузке — 62», до 10 кампаний в отчёте, 1 одновременная выгрузка, `min(активные кампании × 240, 2000)` выгрузок за 24 ч. Окно длиннее 62 дней — 0 строк при HTTP 200 (замер EVETIS). | **исправлено** (строгий режим) |
| 2 | CSV одной кампании | Swagger `/statistics/report`: одна кампания → CSV, несколько → ZIP `<id>.csv`. Runtime разбирал только ZIP и молча терял партию из одной кампании. | **исправлено** |
| 3 | `/daily` → NULL | `ads_expense_daily`: не-200 на `/statistics/daily` давал показы, клики и заказы `NULL` при статусе OK. | **исправлено** |
| 4 | цены без потолка | `/v5/product/info/prices`: цикл без потолка и без проверки повтора курсора; `total` не сверялся. | **исправлено** |
| 5 | каталог > 1000 | `/v3/product/list` — один запрос; при `total > 1000` сущность падала. `/v3/product/info/list` принимает ≤ 1000 id. `/v1/analytics/stocks` принимает **≤ 100 SKU**, а runtime слал все сразу. `result.total` отключат **23.11.2026** (нужен `total_items`). | **исправлено** |
| 6 | SINCE > UNTIL | Пустой прогон со статусом OK; для FBO окно уходило в API как есть. | **исправлено** (строгий режим, выход 2 до API) |
| 7 | FBO UTC/МСК | Окно `…T00:00:00.000Z … T23:59:59.000Z`: секундный разрыв на стыке окон. `order_date = created_at[:10]` — сутки UTC, а контракт покрытия T4 — сутки МСК. Период > 1 года → `PERIOD_IS_TOO_LONG`. | **исправлено** (граница `.999Z`, проверка года); сутки МСК — в покрытии (§H), RAW не меняется |
| 8 | `backfill_start_date` | Такой переменной в runtime нет. Окно — `SINCE/UNTIL` или окно ретроспективы сущности. Планировщика бэкфилла нет. | проект §I |
| 9 | память | Сущность копит все строки окна в списке и пишет одним MERGE. Для бэкфилла память ограничивает только размер отрезка. | проект §I (ограниченные отрезки) |
| 10 | чекпойнты | `BACKFILL_CHECKPOINTS` есть (T4), но никем не пишется. | проект §J |
| 11 | возможности | `CAPABILITY_PROFILE` есть (T4), никем не пишется. `/v1/roles` отдаёт роли, методы и **срок действия ключа**. | проект §G |
| 12 | identity | Runtime **не читает** привязку: 0 обращений к `SELLER_BINDING` и `V_SELLER_BINDING_STATUS`. У runtime SA нет доступа к `tenant_ops`. | проект §F — **блокер ворот** |
| 13 | DQ | Представления `V_DQ_*` есть, ворот READY нет. | проект §K |
| 14 | устаревания API | Все методы runtime на месте и без `deprecated`. **13.10.2026 отключат `/v1/actions/products` и `/v1/actions/candidates`** (промо EVETIS, у client_001 промо выключено). `total` → `total_items` 23.11.2026. | отдельная задача по EVETIS; `total_items` — исправлено |
| 15 | справочник типов | Не-200 на `/v1/finance/accrual/types` — все начисления молча `UNKNOWN`. | **исправлено** (строгий режим) |

Прочее, проверенное и не требующее изменений:
- лимит Seller API — 50 запросов/с на Client-Id, runtime делает около 1 запроса/с. При 429 приходит
  `Retry-After`; runtime использует свой backoff 3–48 с, что допустимо;
- `last_id` у `/v1/finance/accrual/by-day` живёт 15 минут, пагинация суток укладывается;
- начисления документированы с 2022-01-01;
- у ключа бывает ограничение по IP. У Cloud Run нет фиксированного IP, поэтому ключ с
  IP-ограничением работать не будет: проверяется в `VALIDATING`.

## B. Доказательства по API Ozon (2026-09-28)

| Метод | Существенное |
|---|---|
| `POST /v3/posting/fbo/list` | период ≤ 1 года (`PERIOD_IS_TOO_LONG`), `limit` ≤ 100, пагинация `cursor`/`has_next` |
| `POST /v1/finance/accrual/by-day` | одни сутки на запрос, самая ранняя дата — 2022-01-01, `last_id` живёт 15 мин |
| `POST /v3/product/list` | `limit` ≤ 1000, `last_id` непуст и на последней странице, `total` → `total_items` (23.11.2026) |
| `POST /v3/product/info/list` | ≤ 1000 идентификаторов в сумме |
| `POST /v5/product/info/prices` | `limit` ≤ 1000, `cursor`, `total` → `total_items` |
| `POST /v1/analytics/stocks` | `skus` ≤ 100 |
| `POST /v1/seller/info` | `company.{inn, ogrn, name, legal_name, ownership_form, tax_system}`, `subscription.{is_premium, type ∈ PREMIUM, PREMIUM_LITE, PREMIUM_PLUS, PREMIUM_PRO}` |
| `POST /v1/roles` | `expires_at` ключа, `roles[].{name, methods[]}` |
| Performance `POST /api/client/token` | `client_id` (вида `…@advertising.performance.ozon.ru`) + `client_secret`, токен на 1800 с |
| `POST /api/client/statistics` | `from/to` (RFC 3339) **или** `dateFrom/dateTo` (ГГГГ-ММ-ДД), даты группируются по Москве; ≤ 62 суток; ≤ 10 кампаний |
| `GET /api/client/statistics/expense`, `/daily` | `dateFrom/dateTo`; есть JSON-варианты `…/json` |

## C–D. Найденные и исправленные блокеры (PR `feat/tenancy-t5-runtime-blockers`)

Правило, как в T2: **закрытый отказ включается только флагом арендатора
`STRICT_PAGE_CAPS=1`**. У EVETIS флага нет, и дифференциальный тест T2 (`test_differential_compat.py`)
это подтверждает. Исключение — исправления, которые только превращают прежний отказ в успех на тех же
данных: пагинация каталога, партии `info/list` и `stocks`. Их первый запрос для EVETIS не изменился.

- каталог: страницы `/v3/product/list` до `total_items|total`. Застревание (страница без новых
  товаров, повтор курсора) или потолок — `PaginationError`; `info/list` — партиями по 1000. В строгом
  режиме: нет `total` — отказ, карточек меньше, чем товаров, — отказ;
- остатки: партиями по 100 SKU;
- цены: потолок 2000 страниц, повтор курсора — отказ (всегда). В строгом режиме число товаров должно
  равняться `total_items`;
- FBO: в строгом режиме граница `T23:59:59.999Z`, окно ≤ 365 суток;
- начисления: в строгом режиме не-200 справочника типов — отказ;
- реклама по дням: в строгом режиме не-200 `/daily` — отказ;
- реклама по SKU, строгий путь:
  - окна по **датам МСК** (`dateFrom/dateTo`, ≤ 60 суток), моменты UTC не отправляются;
  - периметр — только кампании «Оплата за клик» (`advObjectType = SKU`); оплата за заказ и
    баннеры не заказываются (их отчёт по SKU пуст всегда); расход у кампании неизвестного типа или
    вне реестра — отказ;
  - **каждая пара «CPC-кампания × сутки» с ненулевым расходом** (`/statistics/expense` по суткам)
    обязана иметь строки SKU за эти сутки, иначе отказ — это ловит и «0 строк при OK»;
  - проверяются колонки CSV расхода; нечисловой расход — отказ;
  - CSV одной кампании разбирается; файл ZIP чужой кампании или строка вне окна — отказ;
  - замер EVETIS 2026-09-28 (только чтение):

    | Проверка | Результат |
    |---|---|
    | сутки CPC с расходом, у которых есть строки SKU | 2191 из 2191 |
    | пары «кампания × месяц» SEARCH_PROMO / ALL_SKU_PROMO со строками SKU | 0 из 12 |
    | пары CPC, где сумма SKU совпала с расходом в пределах 1 % | 95 из 148 |

    Поэтому сверка сумм — предупреждение DQ, а не отказ загрузки;
- окно прогона (строгий режим) проверяется по **фактическому окну каждой сущности**:
  - `UNTIL` без `SINCE` и отрицательный `LOOKBACK_OVERRIDE` — отказ;
  - дата — только `YYYY-MM-DD`, ISO-неделя отвергается;
- `total`/`total_items`:
  - без флага сохраняется прежний приоритет `total`;
  - в строгом режиме расхождение полей — отказ;
  - дубли товара внутри страницы считаются один раз;
  - карточки сверяются по набору id, а не по числу;
- окно прогона: в строгом режиме SINCE/UNTIL — только `YYYY-MM-DD`, SINCE ≤ UNTIL, не позже
  сегодняшних суток МСК; иначе выход 2 до первого обращения к API.

## E. Модель учётных данных

| Секрет (Secret Manager, `mpa-t-client-001`) | Класс | Для чего |
|---|---|---|
| `ozon-seller-client-id` | идентификатор | заголовок `Client-Id` Seller API |
| `ozon-seller-api-key` | **секрет** | заголовок `Api-Key` |
| `ozon-perf-client-id` | идентификатор (RFC 6749 §2.2) | `client_id` Performance API (сервисный аккаунт «Настройки → API-ключи → Performance API») |
| `ozon-perf-client-secret` | **секрет** | `client_secret`; из него — токен (секрет, 1800 с) |

- **Вставка:** только владелец, только из stdin: `gcloud secrets versions add <id> --data-file=-`.
  Не в аргументах, не в истории оболочки, не в Git, артефактах, state или журналах. Terraform версиями
  не управляет (И1).
- **Роль ключа Seller** проверяется в `VALIDATING` через `/v1/roles`:
  - обязательные методы = методы включённых сущностей + `/v1/seller/info` + `/v1/roles`; нет
    обязательного — сущность недоступна (BLOCKING для core);
  - в ключе есть методы записи — **решение D2** (рекомендую BLOCKING: runtime только читает; у
    EVETIS ключ Admin с 464 методами — прежняя находка);
  - `expires_at` < 30 суток — WARNING, истёк — BLOCKING.
- **Ротация:** новая версия, старая выключается; runtime читает `latest`, после ротации —
  переход в `VALIDATING`.
- **Вырезание (T2.2):** Api-Key, `client_secret` и токен регистрируются как секреты. Ответ
  `/v1/roles` ключа не содержит. ИНН/ОГРН/название — не секреты, но в журналы не пишутся, только
  отпечаток (§F). Состязательная проверка новых путей — тест `test_strict_ads_messages_carry_no_credentials`;
  для `lifecycle` — такая же при реализации.

## F. Протокол identity продавца

```
учётные данные вставлены → VALIDATING:
  /v1/roles (роли, срок) → /v1/seller/info → токен Performance + /api/client/campaign
  → tenant_ops.SELLER_IDENTITY_OBSERVATIONS (строка на API; отпечаток; run_id)
→ оператор: tenant_binding.py show   (маскированные ИНН/ОГРН, название, Client-Id, отпечаток)
→ оператор сверяет с ожидаемым продавцом вне системы
→ tenant_binding.py confirm --api seller --observation <id> --fingerprint <fp>
  → ref.SELLER_BINDING (CONFIRMED)
→ Performance: доказательство — все SKU кампаний ⊆ каталог привязанного Seller → confirm --api performance
→ только теперь CAPABILITY_DISCOVERY
```

- **Отпечаток Seller** = sha256 канонического JSON `{client_id, inn, ogrn}`. **Performance** —
  sha256 `{perf_client_id}` + доля SKU кампаний, найденных в каталоге. Ключи в отпечаток не входят.
- **Каждый прогон runtime:** сам вызывает `/v1/seller/info`, считает отпечаток и сравнивает с
  действующей привязкой (`V_SELLER_BINDING_STATUS` = BOUND). Не совпало, привязки нет или несколько
  разных отпечатков в последнем наблюдении — **выход до записи данных**. Это закрывает подмену ключа
  между подтверждением и загрузкой.
- `confirm` отказывает, если:
  - в последнем наблюдении больше одного отпечатка;
  - наблюдению больше 24 ч;
  - отпечаток не совпал;
  - наблюдение другого API.

  Журнал только дописывается, отзыв — `revoke`.
- **Где хранится identity:** только в проекте арендатора (`tenant_ops`, `ref`). В `tenant.json`
  полей identity нет, схема их запрещает (T4, тест).

## G. Протокол обнаружения возможностей

Строка `CAPABILITY_PROFILE` на `(api, capability)`:
- `status` ∈ AVAILABLE / UNAVAILABLE / DENIED / NOT_APPLICABLE / UNKNOWN;
- `evidence_kind` ∈ LIVE_CALL / ROLE_LIST / DOCUMENTED;
- `http_status`, `evidence_json`, `run_id`.

Документация сама по себе — никогда не AVAILABLE.

| capability | Проба (только чтение, минимальная) |
|---|---|
| `seller.method:<path>` | `/v1/roles` (ROLE_LIST) |
| `seller.subscription` | `/v1/seller/info` → `subscription.type`: это и есть «Premium» (tier наблюдается, не со слов владельца) |
| `seller.key_expiry` | `/v1/roles.expires_at` |
| `seller.fbo_postings` | `/v3/posting/fbo/list`, последние 30 суток, `limit=1` |
| `seller.fbs_activity` | `/v3/posting/fbs/list`, одна страница, если роль разрешает. Отправления FBS при контракте FBO-only → WARNING (данные FBS не грузятся); не разрешено → UNKNOWN |
| `seller.finance_accrual` | `/v1/finance/accrual/by-day` за вчера |
| `seller.catalog / prices / stocks / supplies / clusters` | одна страница каждого |
| `perf.token / campaigns / expense / async_report` | токен; реестр; `/expense` за 7 суток; один отчёт по одной кампании с расходом за 62 суток (если такой нет — UNKNOWN) |

Сводка: `V_CAPABILITY_CURRENT` — последний статус на пару (новое представление пакета SQL, §O).

## H. Алгоритм границ истории

Строка `HISTORY_BOUNDARIES` на сущность, поля T4. `backfill_to` = **завершено по**: двигается только
вперёд и только после чекпойнта DONE (§J).

- `api_documented_from`:
  - Swagger: начисления 2022-01-01, остальные NULL;
  - Performance UI: с 2019-01-01 (справка) — DOCUMENTED, не VERIFIED.
- `EARLIEST_AVAILABLE` для исторических сущностей:
  1. нижняя граница поиска `L` = `api_documented_from`, иначе 2019-01-01;
  2. помесячный скан от `L` до сегодня:
     - FBO — окно месяца, `limit=1`;
     - начисления — 3 дня в месяце (5/15/25), как в аудите EVETIS;
     - реклама — `/expense` за месяц;
  3. первый непустой месяц M → посуточное уточнение M и M−1;
  4. `first_observed_activity` = первые сутки с данными (VERIFIED);
  5. `api_verified_from` = самая ранняя дата, на которую API ответил 200 (приём окна ≠ данные).
- Подсказка владельца «примерно с 2024» только задаёт порядок скана. Граница из неё не берётся.
- Пустые месяцы между активностью — это отсутствие активности, а не предел API. Предел фиксируется
  только по ответу API (400/`PERIOD_*`) или по документации.
- Снимки (каталог, цены, остатки, кластеры, кампании): `completeness_status = NOT_AVAILABLE` для
  прошлого, `first_data_date` = первый снимок. Поставки: реестр без окна.
- Покрытие по суткам МСК: `DATA_COVERAGE.coverage_date` — сутки МСК.
  - FBO грузится окнами UTC, перекрывающими сутки МСК с запасом одни сутки; MERGE идемпотентен.
    Сутки МСК COMPLETE, только если весь интервал `[D−1 21:00Z, D 21:00Z)` внутри DONE-окон.
  - Реклама: даты отчётов уже МСК.
  - Начисления: сутки запроса.

## I. Архитектура backfill

- **Планировщик** (control, §L) из `HISTORY_BOUNDARIES` и даты отсечки `C` (вчера МСК) строит
  детерминированный план отрезков. Хеш плана пишется в событие перехода.

| Сущность | Отрезок | Семантика дат | Пагинация / потолок | Повтор | Ключ MERGE |
|---|---|---|---|---|---|
| fbo_postings | 7 сут. (адаптивно ÷2 при упоре в 200 стр.) | UTC-моменты `[D T00:00:00.000Z, D+6 T23:59:59.999Z]` | cursor/has_next, 200 стр. × 100 | backoff 429/5xx | posting_number, sku |
| finance_accrual | 7 сут. | сутки запроса | last_id, 60 стр./сутки | то же | accrual_id, type_id, sku (reject) |
| ads_expense_daily | 31 сут. | сутки МСК | — | то же | date, campaign_id |
| ads_sku_daily | 60 сут. | даты МСК | ≤ 10 кампаний, 1 выгрузка одновременно, бюджет выгрузок | то же | date, campaign_id, sku |
| снимки, поставки | — | — | как в runtime | — | как в runtime |

- **Исполнитель** — runtime с флагом `STRICT_PAGE_CAPS=1`, окно `SINCE/UNTIL` — ровно один отрезок.
  Память ограничена отрезком.
- **Бюджет Performance:** выгрузки за 24 ч считаются по журналу. План не превышает
  `min(активные × 240, 2000)`, остаток переносится на следующие сутки.
- **Детерминизм:** план — чистая функция `(границы, C, размеры отрезков)`; повтор даёт тот же план.
- **После отсечки** — обычные инкрементальные прогоны (окна ретроспективы). Перекрытие с бэкфиллом
  безопасно (MERGE).

## J. Семантика чекпойнтов

`BACKFILL_CHECKPOINTS` — журнал версий, только дописывается. Статус отрезка — последняя строка по
`updated_at`.

- `PENDING` (план) → `RUNNING` (аренда 2 ч) → `DONE` | `FAILED` (attempts++).
  - ≥ 5 попыток — `FAILED_PERMANENT`, это BLOCKING DQ.
  - `RUNNING` с истёкшей арендой — снова в работу.
- **Порядок записи:** MERGE данных → строка `OZON_INGESTION_RUNS` (OK, окно, run_id) → проверка
  control (журнал OK, окно совпадает, `StrictLimitError` нет) → `DONE`.
  - Сбой между MERGE и `DONE` — отрезок выполняется повторно, это безопасно (MERGE).
  - `DONE` никогда не пишется до MERGE.
- Уже загруженные отрезки при сбое не откатываются.
- **Идемпотентность:** повтор `DONE`-отрезка ничего не делает, повтор плана — те же `backfill_id`
  (хеш сущности и окна).

## K. Ворота DQ и сверки

| Проверка | Уровень |
|---|---|
| привязка Seller ≠ BOUND; Performance ≠ BOUND при `ozon_ads` | BLOCKING |
| ключ: нет обязательного метода; истёк; методы записи (D2) | BLOCKING |
| отрезок плана не DONE; `FAILED_PERMANENT`; `StrictLimitError` без успешного повтора | BLOCKING |
| сутки МСК без покрытия между `first_data_date` и отсечкой (исторические сущности) | BLOCKING |
| повтор ключа MERGE в RAW; `MergeKeyConflictError` | BLOCKING |
| тип начисления вне таксономии (`V_DQ_UNRESOLVED_ACCRUALS`) | BLOCKING |
| `FINANCE_SETTLEMENT_MATURITY_DAYS` не задан (D3) | BLOCKING |
| сумма SKU-расхода ≠ расход кампании за окно (НДС, бонусы) | WARNING |
| неоднозначность SKU ↔ offer_id | WARNING |
| отправления FBS при контракте FBO-only | WARNING |
| ключ истекает < 30 суток | WARNING |
| снимочные сущности без истории | INFO |

Результат пишется в `DQ_RESULTS` (run_id). READY невозможен, пока в последнем прогоне DQ есть
BLOCKING.

## L. Автомат состояний

```
CREDENTIALS_PENDING → VALIDATING → CAPABILITY_DISCOVERY → READY_FOR_BACKFILL → BACKFILLING → RECONCILING → READY
                          ↑                                                         ↑______________|
                          |                                                   (DQ нашёл дыры → новые отрезки)
                     SUSPENDED ← (любое: MISMATCH, ключ отозван/истёк, оператор)
```

| Переход | Машинно проверяемое доказательство | Кто |
|---|---|---|
| CREDENTIALS_PENDING → VALIDATING | у 4 секретов ≥ 1 ENABLED-версия (число, не значение) | оператор |
| VALIDATING → CAPABILITY_DISCOVERY | роли ОК; наблюдение этого прогона; Seller BOUND (+ Performance BOUND при рекламе) | control |
| CAPABILITY_DISCOVERY → READY_FOR_BACKFILL | профиль по всем включённым сущностям; границы истории; хеш плана | control |
| READY_FOR_BACKFILL → BACKFILLING | подтверждение плана оператором (хеш) | оператор |
| BACKFILLING → RECONCILING | все отрезки плана DONE | control |
| RECONCILING → READY | прогон DQ без BLOCKING | control |
| RECONCILING → BACKFILLING | прогон DQ с дырами → дополнительные отрезки | control |
| * → SUSPENDED / SUSPENDED → VALIDATING | причина / решение оператора | control, оператор |

Строка события содержит `from, to, occurred_at, actor, run_id, reason_code, evidence_json` (хеши
доказательств). Запись — только функцией `transition()`: текущее состояние из
`V_TENANT_STATE_CURRENT` должно равняться `from`, ребро разрешено, доказательство проходит
валидатор. Повтор с тем же хешем ничего не делает. Прыжок через состояние отвергается.

Независимая проверка — новое представление `V_TENANT_STATE_AUDIT` (§O): недопустимые рёбра в
истории → `INVALID`.

## Решения владельца (до реализации L/F/G/I/J/K)

- **D1 — кто пишет `tenant_ops`.** Рекомендую новый SA арендатора `sa-tenant-control`:
  - тот же образ runtime, точка входа `lifecycle.py`;
  - права: секреты для проб, WRITER `tenant_ops`, READER `ozon_raw`/`ref`, `jobUser`;
  - runtime получает только READER `tenant_ops`, свою привязку проверяет сам.

  Так оцениваемый (runtime) не пишет себе вердикт READY. Альтернатива из плана T4 — runtime
  WRITER `tenant_ops` плюс независимый верификатор. Она проще, но смешивает роли.
- **D2 — методы записи в ключе Seller:** BLOCKING (рекомендую) или WARNING с явным исключением
  владельца.
- **D3 — `FINANCE_SETTLEMENT_MATURITY_DAYS`** обязателен до READY (рекомендую BLOCKING; значение
  задаёт оператор, константа EVETIS 36 не переносится).
- **D4 — право слияния PR T5 и сборки образа runtime-кандидата** из точного коммита `main`
  (release gate T3.2b).
- **D5 — промо EVETIS:** до 13.10.2026 перевести наблюдатель на `/v2/actions/*`. Это отдельные
  ворота EVETIS, задача заведена отдельно.
