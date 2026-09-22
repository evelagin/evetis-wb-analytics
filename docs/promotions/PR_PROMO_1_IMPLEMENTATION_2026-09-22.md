# PR-PROMO-1 — слой наблюдения акций маркетплейсов
# Реализация и развёртывание, 2026-09-22

Одна ответственность: **накапливать достоверную историю состояния акций**.
Ни рекомендаций, ни экономики, ни канонического слоя — это PR-PROMO-2 и далее.

`PROMOTION_WRITE_ALLOWED = FALSE`. Ни одной мутации на маркетплейсах.

---

## 1. Результат преflight

| Проверка | Результат |
|---|---|
| `git fetch origin` | выполнен |
| HEAD против `origin/main` | **совпадают побайтово**: `615878f`, 0 ahead / 0 behind |
| Дрейф кода | **нет**. Незакоммиченными были только 4 документа, не относящихся к PR (`docs/WB_LOGISTICS_STRATEGY_EVETIS_2026.md`, `docs/analysis/wb_logistics_2026/`, `docs/ozon/ozon-week5-2026-09-20.md`, `docs/pricing/WB_PRICE_RECOMMENDATIONS_2026-09-21.md`). Они **не тронуты и не закоммичены** |
| Риск перезаписать чужую работу | нет: ни один существующий объект не изменён (см. §18) |
| Production против допущений фаз 0/0.5 | SHA-256 четырёх канонических экономических вью совпали с Git (проверено в фазе 0.5); 10 имён новых таблиц свободны |
| Границы учётных данных read-only | WB: бит 30 (read-only) в JWT, срок до 2027-03-08. Ozon: роль Admin, граница только программная — реализована и проверена тестом (§10) |
| Тесты до изменений | `cloud`: 43 файла, **1043 passed / 19 skipped**. Python: **378 passed / 1 skipped** |

**Устаревшее предупреждение `CLAUDE.md`.** Файл запрещает обычный `terraform apply`
из `main`, «пока работа по загрузчику воронки не слита в `main` через PR». Проверено:
`infra/terraform/wb_funnel_loader.tf` и `secrets.tf` **уже в `main`** (коммит `733026d`).
Предупреждение частично устарело; тем не менее целевой `-target` apply здесь всё равно
применяется — это дешевле, чем доказывать, что полный план чист.

---

## 2. Архитектура

```
WB  (TypeScript, cloud/)                    Ozon (Python, pipelines/ozon/)
────────────────────────────────            ──────────────────────────────────
cloud/src/loaders/promo/                    pipelines/ozon/runtime/promo.py
  constants.ts  контракт + ALLOWED_PATHS      ALLOWED_PATHS (закрытый список)
  slot.ts       логический период             promo_call() — единственный выход в сеть
  wbApi.ts      READ-клиент + buildUrl()      _paged_products / _paged_auto_add
  normalize.ts  чистые функции                _resolve() — резолв SKU
  bq.ts         append-only load              common.append_rows() — append-only load
  index.ts      оркестрация                   promo() — сущность REGISTRY
         │                                              │
         ▼                                              ▼
  wb_raw.RAW_WB_PROMO_*                        ozon_raw.RAW_OZON_PROMO_*
  wb_raw.WB_PROMO_OBSERVATIONS                 ozon_raw.OZON_PROMO_OBSERVATIONS
                                               + ozon_raw.OZON_INGESTION_RUNS (существующий)
```

Изоляция маркетплейсов соблюдена: WB-загрузчик не читает `ozon_*`, Ozon-загрузчик не
читает `wb_*`; общий только `evetis_ref.REF_SKU_CHANNEL_MAP`.

### 2.1 Три решения, отличающиеся от плана фазы 0

| Было в плане | Стало | Почему |
|---|---|---|
| Добавить колонку `marketing_actions_json` в `ozon_raw.RAW_OZON_PRICES` (PR-PROMO-0) | **Существующая таблица не трогается вовсе.** Наблюдатель сам вызывает `/v5/product/info/prices` и пишет две свои таблицы | Полный поиск по репозиторию и по телам ВСЕХ живых вью шести датасетов дал **ноль потребителей** `ozon_actions_exist`, кроме самого производителя и DDL. Значит совместимость ломать нечем — но и трогать боевой загрузчик цен незачем: у него другая каденция (1×/сут против 4×/сут), и правка добавила бы риск регрессии витрины ради нуля выгоды |
| `RAW_*_PROMO_*` пишутся тем же `merge_rows`, что остальной Ozon | **Append-only через детерминированный `job_id`** | `merge_rows` схлопывает по ключу со `snapshot_date`: при 4 снимках в сутки три из четырёх наблюдений исчезли бы. Применён контракт наблюдателя цен WB (PR-1) |
| 8 таблиц | **10 таблиц** | `marketing_actions.actions[]` оказался непустым у 19 из 20 товаров (15–19 элементов), а не пустым, как предполагала фаза 0. Понадобилась длинная проекция + отдельный манифест Ozon |

---

## 3. Endpoints источников

Все — READ. Мутирующих путей в загрузчиках нет физически (§10).

| МП | Метод | Endpoint | Назначение |
|---|---|---|---|
| WB | GET | `dp-calendar-api /api/v1/calendar/promotions` | список акций за окно |
| WB | GET | `dp-calendar-api /api/v1/calendar/promotions/details` | условия, счётчики, лестница бустинга |
| WB | GET | `dp-calendar-api /api/v1/calendar/promotions/nomenclatures` | состав акции — **только для `type != 'auto'`** |
| OZ | GET | `api-seller /v1/actions` | список акций |
| OZ | POST | `api-seller /v1/actions/products` | участники акции |
| OZ | POST | `api-seller /v1/actions/candidates` | кандидаты |
| OZ | POST | `api-seller /v1/actions/auto-add/products/list` | **кого площадка ДОБАВИТ** |
| OZ | POST | `api-seller /v1/actions/auto-add/products/candidates` | **кого МОЖЕТ добавить** |
| OZ | POST | `api-seller /v5/product/info/prices` | `marketing_actions` уровня товара |

**Не загружается:** `/v1/actions/discounts-task/list` — `DEFERRED_PRIVACY_SENSITIVE_SOURCE` (§10.4).

---

## 4. Грейны источников

| Таблица | Грейн | Почему так |
|---|---|---|
| `RAW_WB_PROMO_CALENDAR` | `observation_id × promotion_id` | список и детали склеиваются в одну строку акции |
| `RAW_WB_PROMO_RANGING` | `observation_id × promotion_id × tier_ordinal` | лестница — самостоятельная сущность, а не атрибут |
| `RAW_WB_PROMO_NOMENCLATURE` | `observation_id × promotion_id × nm_id × in_action_requested` | один nm_id приходит в обоих ответах (`inAction=true` и `false`) |
| `RAW_OZON_PROMO_ACTIONS` | `observation_id × action_id` | |
| `RAW_OZON_PROMO_PRODUCTS` | `observation_id × action_id × product_id × membership` | **`membership` в грейне**: участник и кандидат — разные состояния одного товара в одной акции |
| `RAW_OZON_PROMO_AUTO_ADD` | `observation_id × action_id × auto_add_at × product_id × list_kind` | **`list_kind` в грейне**: «добавит» и «может добавить» различаются на 41 % вклада |
| `RAW_OZON_PROMO_PRODUCT_MARKETING` | `observation_id × offer_id` | |
| `RAW_OZON_PROMO_PRODUCT_ACTION` | `observation_id × offer_id × action_ordinal` | порядок источника значим, идентификатора акции Ozon здесь не даёт |
| `WB_PROMO_OBSERVATIONS` | `observation_id` | манифест прогона |
| `OZON_PROMO_OBSERVATIONS` | `observation_id` | манифест прогона |

---

## 5. Схемы таблиц

Авторитетный DDL: [`sql/promotions/pr_promo1_raw.sql`](../../sql/promotions/pr_promo1_raw.sql).
Все таблицы: `PARTITION BY DATE(observed_at)` (манифесты — по `started_at`),
`CLUSTER BY` ключ сущности.

Общий технический префикс каждой RAW-строки:
`observed_at`, `observation_bucket`, `observation_id`, `environment`, `run_id`
и суффикс `raw_*_json`, `source_endpoint`, `source_payload_hash`, `ingested_at`.

Три правила схемы, действующие во всех таблицах:

1. **NULL ≠ 0.** Пустая строка и отсутствующее значение источника дают NULL.
   Проверено: `freeze_date: ""` у Ozon → `freeze_at IS NULL`, а не 1970 год.
2. **Сырой payload сохраняется целиком.** Незнакомое поле не теряется, даже если
   разбор его не знает: тест `test_unknown_source_field_is_visible_not_lost`.
3. **Имена источника сохраняются там, где семантика не доказана.**
   `price_min_elastic` / `price_max_elastic` не переименованы в «цену при
   минимальном/максимальном бустинге», хотя наблюдения на это указывают:
   официального описания подполей у Ozon нет (открытый вопрос O-2 фазы 0).

---

## 6. Семантика истории

**Append-only. Ни MERGE, ни UPDATE, ни удаления партиций.**

Строка наблюдения неизменна. Состояние акции изменилось — появляется новая строка
с новым `observation_id`, старая остаётся. Наблюдение, в котором ничего не
изменилось, всё равно записывается: отсутствие строки означает «наблюдатель не
отработал», а не «состав не менялся».

Провенанс каждой строки отвечает на вопрос «что EVETIS знал об этой акции в момент T»:

| Поле | Что отвечает |
|---|---|
| `observed_at` | момент **успешного ответа площадки**, не старта прогона |
| `observation_bucket` | слот расписания (логический период) |
| `observation_id` | детерминированный id снимка |
| `run_id` | сквозной id прогона → `LOADER_RUNS` / `OZON_INGESTION_RUNS` |
| `source_endpoint` | какой метод породил строку |
| `source_payload_hash` | стабильный ключ строки снимка |
| `ingested_at` | момент записи в BigQuery — **отличается** от `observed_at` при ретрае |
| `raw_*_json` | payload источника дословно |

Время события источника (`date_start`, `date_end`, `freeze_date`, `auto_add_dates`)
хранится отдельно от времени наблюдения и временем загрузки не подменяется.

Единственный UPDATE в подсистеме — финализация строки **манифеста**. Манифест
описывает прогон, а не состояние маркетплейса; история наблюдений им не правится.

---

## 7. Семантика идемпотентности

Три уровня, каждый закрывает свой класс повтора:

| Уровень | Механизм | Что подавляет |
|---|---|---|
| 1 | `LOADER_RUNS` / execution-guard по `(environment, loader, logical_period)` | повторный запуск слота после `COMPLETE` — handler не вызывается (WB) |
| 2 | **детерминированный `job_id` load-джобы** | повтор слота после ошибки: BigQuery дедуплицирует load по `job_id`, строки не задваиваются |
| 3 | манифест наблюдений | повтор виден как `status = 'REUSED'`, а не как новый снимок |

`job_id = <mp>promo_<environment>_<slot>_<table>`. Ключ идемпотентности — **слот**,
а не прогон: два запуска в одном слоте обращаются к тому же `job_id`.

Партиции не удаляются и не перезаписываются: это противоречило бы append-only.

**Два дефекта, найденные боевой валидацией и исправленные здесь.**

*Второй — грейн манифеста.* Открытие снимка делало безусловный `INSERT`, поэтому
каждый прогон слота оставлял свою строку: после трёх прогонов WB в манифесте было
три строки на один `observation_id`. Сырые данные при этом не дублировались (11
строк, а не 33) — нарушался грейн **телеметрии**, и проверка «дублей грейна нет»
упала бы на собственном манифесте подсистемы. Исправлено на обеих площадках:
`INSERT ... FROM UNNEST([1]) WHERE NOT EXISTS (...)`. Регрессия закрыта тестом.

*Первый — расчёт `REUSED`.* Исход `REUSED`
изначально считался по всем трём таблицам WB. Пустая запись возвращает `LOADED`,
не обращаясь к BigQuery, а состав акций сегодня пуст всегда — значит повтор слота
навсегда оставался бы `COMPLETE`. Данные при этом были целы (11 строк после двух
прогонов, не 22) — врал манифест, то есть именно операционный сигнал.
Исправлено: `REUSED` считается только по таблицам с непустой записью.
Регрессия закрыта тестом `promo_append.test.ts`.

---

## 8. Каденс

**4 наблюдения в сутки: 04:00, 09:00, 14:00, 19:00 UTC = 07/12/17/22 МСК.**

| Проверка обоснования фазы 0 | Итог |
|---|---|
| Частота обновления источника | Подтверждена: повторный вызов WB `details` через 40 минут дал побитово тот же ответ. Внутрисуточная волатильность есть, но не минутная |
| Дата автодобавления Ozon | 21:00 UTC (полночь МСК). **Суточный снимок в 06:30 МСК увидел бы автодобавление уже после того, как оно сработало** — вечерний слот 19:00 UTC даёт 2 часа предупреждения |
| Лимиты | WB: 10 запросов / 6 с на всю категорию; снимок — 2 запроса. Ozon: 50 req/s на Client-Id; снимок — 55 запросов за прогон, растянутых во времени. Запас кратный |
| Стоимость | WB ~8 запросов/сут, Ozon ~220 запросов/сут. Почасовой дал бы ~1320 запросов Ozon в сутки ради величин, которые площадка считает от 30-суточной медианы |

Новых свидетельств против четырёх слотов не найдено — реализовано как предложено.

Часовые пояса: WB-планировщик в `Etc/UTC`, Ozon-планировщик в `Europe/Moscow`
(так устроены остальные планировщики Ozon), но **моменты совпадают**:
04/09/14/19 UTC = 07/12/17/22 МСК. Совпадение проверяется тестом
`promo_wiring.test.ts`, а согласие расписания с логическим периодом — тестом
`test_deployment_contract.py`.

---

## 9. Возможность бэкфилла

Классификация по каждому источнику:

| Таблица | Класс истории | Основание |
|---|---|---|
| `RAW_WB_PROMO_CALENDAR` — поля списка (`id`, `name`, `type`, даты) | **`API_HISTORICAL`** | окно −730 суток вернуло 242 акции до 2024-10-06 |
| `RAW_WB_PROMO_CALENDAR` — поля деталей (счётчики, участие, исключения) | **`OBSERVED_FROM_2026_09_22`** | `/details` по завершённым акциям возвращает 200 и ПУСТОЙ массив |
| `RAW_WB_PROMO_RANGING` | **`OBSERVED_FROM_2026_09_22`** | лестница живёт только в деталях |
| `RAW_WB_PROMO_NOMENCLATURE` | **`NOT_AVAILABLE`** сегодня | метод неприменим к автоакциям; станет `OBSERVED_FROM_<дата>` при первой regular-акции |
| `RAW_OZON_PROMO_ACTIONS` | **`OBSERVED_FROM_2026_09_22`** | `/v1/actions` отдаёт только текущие и будущие |
| `RAW_OZON_PROMO_PRODUCTS` | **`OBSERVED_FROM_2026_09_22`** | состав только на момент запроса |
| `RAW_OZON_PROMO_AUTO_ADD` | **`OBSERVED_FROM_2026_09_22`** | метод смотрит вперёд, не назад |
| `RAW_OZON_PROMO_PRODUCT_MARKETING` / `_ACTION` | **`OBSERVED_FROM_2026_09_22`** | снимок текущего состояния |

**Бэкфилл в PR-PROMO-1 не выполнялся — сознательно.** Единственный доступный
(каталог акций WB за 2 года) **не скоропортящийся**: WB отдаёт его и через месяц.
Скоропортящееся (состав, цены, автодобавление Ozon) невосстановимо в принципе, и
именно поэтому наблюдение запущено сегодня. Каталожный бэкфилл уместнее в
PR-PROMO-2 отдельным `observation_id` с явной пометкой, чем смешанным с живым
снимком.

**Историческое покрытие каталога НЕ означает историю состава и цен.** Строка акции
2024 года несёт только её существование и даты — ни `inPromoActionTotal`, ни
лестницы, ни SKU. Различие закреплено колонкой `details_available`.

---

## 10. Граница записи

### 10.1 Wildberries — граница в самом носителе права
JWT токена `WB_PRICES_READ_TOKEN` содержит **бит 30 (read-only)**, срок до
2027-03-08. Записать акцию этим токеном невозможно физически. Наблюдатель акций
использует **тот же** секрет, что наблюдатель цен: второй носитель права записи в
runtime — лишняя поверхность.

### 10.2 Ozon — граница только программная
Ключ EVETIS имеет роль Admin: мутирующие методы акций ему доступны. Поэтому:
`promo_call()` — **единственная** точка выхода в сеть, и она проверяет путь по
закрытому `ALLOWED_PATHS` до формирования запроса.

### 10.3 Механическая проверка
| Тест | Что доказывает |
|---|---|
| `cloud/test/promo_security.test.ts` | ни один из 19 мутирующих путей не встречается в исходниках `promo/` — **даже в комментарии**; нет `method: 'POST'/'PUT'/'PATCH'/'DELETE'`; `ALLOWED_PATHS` состоит ровно из трёх читающих путей |
| `pipelines/ozon/tests/test_promo_security.py` | то же по тексту + **17 параметризованных проверок поведения**: `promo_call()` на запрещённом пути бросает `PromoPathDenied` и не доходит до транспорта |

Тест поймал реальное нарушение дважды: в обоих загрузчиках мутирующий путь
упоминался в документирующем комментарии. Комментарии переписаны.

### 10.4 Персональные данные
`/v1/actions/discounts-task/list` **не вызывается**. Проверяется тестом: ни путь,
ни поля `customer_name`, `first_name`, `last_name`, `patronymic`, `email`,
`user_comment` не встречаются в исходниках. Фикстуры тестов обезличены.

### 10.5 Секреты
Читаются по имени из Secret Manager в память процесса. Не печатаются в логи, не
попадают в строки BigQuery, в тексты ошибок (проверено тестом) и в фикстуры.

---

## 11. Контракт качества данных

Детерминированные проверки, исполненные после развёртывания (§16) — **15 из 15 PASS**:

| Проверка | Ozon | WB |
|---|---|---|
| Endpoint доступен | ✔ | ✔ |
| Грейн уникален | 3 таблицы ✔ | 2 таблицы ✔ |
| Обязательные идентификаторы непусты | ✔ | ✔ |
| **Кандидат и автодобавление не схлопнуты** | ✔ | н/п |
| `date_start ≤ date_end` | ✔ | ✔ |
| Цены неотрицательны | ✔ | н/п |
| Счётчики неотрицательны | н/п | ✔ |
| Лестница бустинга разбирается | н/п | ✔ (26 ступеней, 25–35 %) |
| Покрытие резолва SKU | ✔ 100 % | н/п (нет строк уровня SKU) |
| Пустая метка времени → NULL, не эпоха | ✔ | ✔ |
| Известное ограничение обработано явно | н/п | ✔ (11 авто → 11 `SKIPPED_AUTO_PROMOTION`) |
| Свежесть payload | манифест | манифест |
| ПДн отсутствуют | тест | тест |
| Мутирующие endpoint'ы не используются | тест | тест |
| Схема не усечена молча | `schema_status` | `schema_status` |

**Fail closed** там, где целостность источника недостаточна:
пропажа обязательных полей (`WB_PROMO_SCHEMA_MISSING`), отсутствие
`data.promotions[]` (`WB_PROMO_SHAPE`), невалидный JSON, упор в потолок страниц,
любой не-2xx кроме документированного 422.

**Классификация исходов WB** (требование §7 задания) — 422 не красит пайплайн:

| Класс | Поведение |
|---|---|
| авторизация (401/403) | `WB_PROMO_AUTH`, прогон падает |
| некорректный запрос (400) | `WB_PROMO_BAD_REQUEST`, падает |
| лимит (429) | ретраи, затем `WB_PROMO_RATE_LIMIT`, падает |
| операционный сбой (5xx) | ретраи, затем `WB_PROMO_HTTP`, падает |
| **известное ограничение (422 на автоакции)** | `UNSUPPORTED_422`, **прогон продолжается**, счётчик `capability_gap_count` |
| валидный пустой ответ | `EMPTY`, прогон продолжается |

Для автоакций запрос вообще не делается (`SKIPPED_AUTO_PROMOTION`): он заведомо
вернёт 422, а лимит категории общий на все методы.

---

## 12. Наблюдаемость

Используются существующие конвенции, новая подсистема мониторинга **не создана**.

| Сигнал | Где |
|---|---|
| Последний успешный / последний запущенный прогон | `WB_PROMO_OBSERVATIONS`, `OZON_PROMO_OBSERVATIONS`, `LOADER_RUNS`, `OZON_INGESTION_RUNS` |
| Свежесть источника | `observed_at`, `completed_at` манифестов |
| Число строк | `rows_written` + счётчики по сущностям |
| Статус endpoint'ов | `http_status`, `http_attempts`, `requests`, `retries` |
| Покрытие резолва SKU | `source_products`, `mapped_products`, `unmapped_products`, `unmapped_ids`, `mapping_coverage_pct` |
| Известные ограничения | `capability_gap_count`, `nomenclature_status` |
| Класс ошибки | `error_code`, `error_message` |
| Длительность | `started_at` → `completed_at` |
| `run_id` | во всех строках RAW и в манифестах |

**`wb_ops.OPS_PIPELINE_REGISTRY` намеренно НЕ трогается в этом PR.**
`infra/terraform/ops_health.tf` несёт незакрытую запись о неприменённом плане
(6 to add, 5 to change, из них чужие изменения), а `sql/current/*/MANIFEST.json`
ведёт собственную миграцию канонических определений. Вклиниваться в них ради
телеметрии наблюдателя — создать связанность, которую потом придётся
распутывать. Телеметрия PR-PROMO-1 самодостаточна; точка интеграции —
**PR-PROMO-7**, когда детектор здоровья будет включаться штатно.

---

## 13. Тесты

| Файл | Тестов | Что покрывает |
|---|---|---|
| `cloud/test/promo_slot.test.ts` | 9 | слот, границы суток/месяца/года, 4 различных слота в сутки, детерминизм id |
| `cloud/test/promo_normalize.test.ts` | 21 | разбор календаря и `ranging[]`, **агрегат не размазывается по SKU**, NULL ≠ 0, дрейф схемы, нерезолвленный nm_id не отбрасывается |
| `cloud/test/promo_wbapi.test.ts` | 18 | **поведение на 422**, классификация 400/401/429/5xx, пустой ответ, битый JSON, пропажа массива, токен не в тексте ошибки, allow-list путей |
| `cloud/test/promo_append.test.ts` | 11 | дедупликация по `job_id`, отличие повтора от настоящей ошибки, расчёт `REUSED` по непустым записям |
| `cloud/test/promo_security.test.ts` | 6 | deny-list по исходному тексту, отсутствие мутирующих методов и полей ПДн |
| `cloud/test/promo_wiring.test.ts` | 6 | регистрация загрузчика, **совпадение расписания Terraform с политикой слотов** на обеих площадках |
| `pipelines/ozon/tests/test_promo_observer.py` | 15 | разбор акций, **кандидат ≠ участник**, **SCHEDULED ≠ ELIGIBLE**, сохранение `marketing_actions[]`, разнородность `value`, лживый флаг рядом с истиной, резолв по product_id и offer_id, нерезолвленные не теряются, пустой/битый/ошибочный ответ, потолок страниц, идемпотентность |
| `pipelines/ozon/tests/test_promo_security.py` | 24 | deny-list по тексту + 17 параметризованных проверок поведения `promo_call()`, отсутствие ПДн, отсутствие хардкода секретов |

Итог: **было** `cloud` 43 файла / 1043 теста, Python 378 → **стало** `cloud` 49 файлов /
**1114 тестов**, Python **417**. Все зелёные. Фикстуры обезличены, боевых секретов нет.

---

## 14. План развёртывания и что выполнено

| Шаг | Статус |
|---|---|
| 1. Перечень объектов и файлов | §18 |
| 2. Тесты | ✅ зелёные до и после (локально; в CI шаг `npm test` не достигается из-за L-8) |
| 3. `terraform fmt` + `terraform validate` | ✅ `Success! The configuration is valid` |
| 4. `bq query --dry_run` по DDL | ✅ `Query successfully validated` |
| 5. **Создание 10 таблиц BigQuery** | ✅ **ВЫПОЛНЕНО** |
| 6. Контролируемый прогон наблюдения ×2 | ✅ **ВЫПОЛНЕНО** (§16) |
| 7. `terraform apply -target` для Job'ов и планировщиков | ⛔ **НЕ ВЫПОЛНЕНО — требует владельца** |

**Почему шаг 7 не выполнен.** Единственный штатный путь apply —
`.github/workflows/infra.yml` с `environment: infra` (ручной approval) и
привилегированным `TERRAFORM_APPLY_SA`, чей WIF-условие ограничено `main`.
Approval — действие владельца, и подменить его нечем. Локальный apply непригоден:
у рабочей учётной записи нет `getIamPolicy`, план падает на предсуществующих
ресурсах (зафиксировано в `ops_health.tf`).

Таблицы BigQuery созданы напрямую DDL-скриптом **по действующей конвенции проекта**:
Terraform ими не владеет (`bigquery.tf` описывает лишь несколько WB-таблиц
shadow-контура и права), ровно так же развёрнуты PR-1 и PR-2.

**Целевой план уже выполнен и проверен** (authenticated `terraform plan` через
`infra.yml`, ветка PR, run 35738907075):

```
Plan: 4 to add, 0 to change, 0 to destroy.
  # google_cloud_run_v2_job.ozon_runtime["ozon-runtime-promo"]   will be created
  # google_cloud_run_v2_job.wb_promo_prod                        will be created
  # google_cloud_scheduler_job.ozon_runtime["ozon-runtime-promo"] will be created
  # google_cloud_scheduler_job.wb_promo_prod                     will be created
```

Ни одного `change`, ни одного `destroy`, ни одного постороннего ресурса.

**Команда для владельца** (после слияния PR в `main`):

```
gh workflow run infra.yml -f action=plan  -f targets="google_cloud_run_v2_job.wb_promo_prod,google_cloud_scheduler_job.wb_promo_prod,google_cloud_run_v2_job.ozon_runtime[\"ozon-runtime-promo\"],google_cloud_scheduler_job.ozon_runtime[\"ozon-runtime-promo\"]"
```

Ожидаемый план: **4 to add, 0 to change, 0 to destroy**.
Любой `change`/`destroy` — **STOP, не применять**.
После apply: промоушен образа (`deploy-prod.yml` уже знает про `wb-promo-prod`),
проверка бита 30 в токене и только затем снятие планировщиков с паузы.

---

## 15. План отката

| Что | Как |
|---|---|
| Остановить наблюдение, сохранив историю | `gcloud scheduler jobs pause wb-promo-prod --location=europe-west1` и `... pause ozon-promo ...` |
| Убрать Job'ы и планировщики | `terraform destroy -target=...` теми же адресами, что в §14 |
| Убрать таблицы | [`sql/promotions/pr_promo1_rollback.sql`](../../sql/promotions/pr_promo1_rollback.sql) — 10 `DROP TABLE IF EXISTS`, ни одного существующего объекта |
| Убрать код | revert коммита PR |

⚠️ Откат таблиц **уничтожает историю, которую нельзя восстановить**: у Ozon
исторической выгрузки акций нет вовсе. Если цель — остановить наблюдение, а не
стереть накопленное, достаточно паузы планировщиков.

Ни один существующий production-объект не изменён, поэтому «откатить чужое»
здесь нечего.

---

## 16. Валидация после развёртывания

Контролируемый цикл наблюдения выполнен **дважды на каждой площадке**, на живых
read-only API, из кода репозитория.

### Ozon — снимок `OZPROMO_prod_202609221400`

| Что требовалось доказать | Факт |
|---|---|
| Текущие акции захвачены | **12 акций**, из них `is_participating` — 9 |
| Участвующие товары захвачены | **94 строки** `membership='PARTICIPATING'`, `add_mode='MANUAL'` |
| Кандидаты захвачены | **61 строка** `membership='CANDIDATE'` |
| Автодобавление захвачено и **не схлопнуто** | **19 строк** `SCHEDULED` + **64 строки** `ELIGIBLE`. По акции 4253043: `SCHEDULED` — 1 товар по 3279 ₽, `ELIGIBLE` — 34 строки со средней 1270 ₽. Та самая разница, которую фаза 0 оценила в 41 % вклада |
| `marketing_actions.actions[]` захвачен | **20 товаров**, **278 строк** длинной проекции |
| Лживый флаг виден рядом с истиной | `source_actions_exist_flag = TRUE` у **0** товаров при непустом `actions[]` у **19** |
| Разнородность `value` сохранена | «Акция для складов. Москва и МО» = 1319 (рубли) рядом с «РК. Рассрочка 0-0-12» = 12 (месяцы) |
| Покрытие резолва SKU | **100,0 %**, unmapped = **0** из 155 |

### Wildberries — снимок `WBPROMO_prod_202609221400`

| Что требовалось доказать | Факт |
|---|---|
| Календарь захвачен | **11 акций** в окне −7…+90 суток |
| Автоакции захвачены | **11 из 11** `type='auto'`, `capability_gap_count = 11` |
| Лестницы бустинга захвачены | **26 ступеней**, бустинг 25–35 % |
| Агрегатное участие захвачено | `SUM(in_promo_total) = 33` как факт уровня акции |
| **Недоступность состава представлена честно** | `RAW_WB_PROMO_NOMENCLATURE` — **0 строк**; `sku_level_data_available = FALSE` у всех 11; `nomenclature_status = 'SKIPPED_AUTO_PROMOTION'` у всех 11. Ни одной выдуманной строки уровня SKU |

### Идемпотентность — повторный прогон того же слота

| Площадка | После 1-го прогона | После 2-го | Манифест |
|---|---|---|---|
| WB `RAW_WB_PROMO_CALENDAR` | 11 строк | **11 строк** (не 22) | `REUSED` |
| WB `RAW_WB_PROMO_RANGING` | 26 строк | **26 строк** | — |
| Ozon, все 5 таблиц | 548 строк | **548 строк**, 5 событий `promo_append_reused` | `REUSED` |

После четырёх прогонов каждой площадки (два до исправлений, два после):
WB — 37 строк RAW и **одна** строка манифеста; Ozon — 548 строк RAW и **одна**
строка манифеста. Грейн манифеста уникален на обеих площадках.

`ingested_at` в повторно «записанных» строках остался от первого прогона —
второй load был отброшен BigQuery по `job_id`, а не переписал строки.

**Оговорка о честности прогона.** Контролируемые циклы запущены с рабочей машины
кодом из репозитория, а не Cloud Run Job'ом (его ещё нет — §14, шаг 7). Отличия:
WB — режим `DRY_RUN=1` (handler исполняется полностью, строка в `LOADER_RUNS` не
пишется: локальный прогон не должен выдавать себя за облачный);
Ozon — библиотека `google-cloud-secretmanager` в этой среде недоступна
(`pip: from versions: none`), поэтому значение секрета взято из **того же**
Secret Manager через `gcloud` CLI. Подменён только транспорт секрета; HTTP,
разбор, резолв и запись — боевой код без изменений.

---

## 17. Известные ограничения

Только фактические, нерешённые:

| # | Ограничение | Класс |
|---|---|---|
| L-1 | **Cloud Run Job и планировщики не созданы.** Наблюдение не возобновится само до `terraform apply` владельцем. Накоплен ровно один снимок каждой площадки | блокирующее для непрерывности |
| L-2 | Состав акций WB по SKU недоступен, пока все акции `auto`. Ограничение контракта площадки, не нашей архитектуры | внешнее |
| L-3 | Семантика `stock` в `/v1/actions/{products,candidates}` не доказана (0 при ненулевом FBO). Поле сохраняется, в решениях не используется | `UNPROVEN`, закроется наблюдением |
| L-4 | Направление `price_min_elastic` / `price_max_elastic` не подтверждено документацией. Имена источника сохранены намеренно | `LIKELY` |
| L-5 | `marketing_actions.actions[].title` — единственный идентификатор акции в этом источнике; числового id Ozon здесь не даёт. Связь с `/v1/actions` — задача PR-PROMO-2 | внешнее |
| L-6 | `wb_ops.OPS_PIPELINE_REGISTRY` не пополнен — см. §12. Точка интеграции: PR-PROMO-7 | осознанное |
| L-7 | Каталожный бэкфилл WB (242 акции за 2 года) не выполнен — см. §9 | осознанное |
| L-8 | **CI-джоб `cloud` красный на `main`, а не из-за этого PR.** `npm run lint` даёт 5 ошибок `no-unused-vars` в `unitka/ozon/{loader,month,monthplan,requests}.ts` и `test/unitka_ozon.test.ts`. Доказано прогоном eslint на чистом worktree `origin/main` (615878f): те же 5 ошибок, 0 из них в файлах PR-PROMO-1. Коммиты, породившие их, уходили в `main` напрямую, без PR, поэтому CI на них не запускался. **Следствие тяжелее самих ошибок:** в `ci.yml` порядок шагов `typecheck → lint → test`, значит `npm test` в CI **не исполнялся** с момента их появления — все 1113 тестов `cloud` проверены только локально. Чинить чужой модуль внутри PR-PROMO-1 — выход за периметр (§1 задания); заведено отдельной задачей | предсуществующее, вне периметра |

---

## 18. Точный перечень production-объектов

### Создано (10 таблиц BigQuery)

| Датасет | Объект |
|---|---|
| `wb_raw` | `RAW_WB_PROMO_CALENDAR` |
| `wb_raw` | `RAW_WB_PROMO_RANGING` |
| `wb_raw` | `RAW_WB_PROMO_NOMENCLATURE` |
| `wb_raw` | `WB_PROMO_OBSERVATIONS` |
| `ozon_raw` | `RAW_OZON_PROMO_ACTIONS` |
| `ozon_raw` | `RAW_OZON_PROMO_PRODUCTS` |
| `ozon_raw` | `RAW_OZON_PROMO_AUTO_ADD` |
| `ozon_raw` | `RAW_OZON_PROMO_PRODUCT_MARKETING` |
| `ozon_raw` | `RAW_OZON_PROMO_PRODUCT_ACTION` |
| `ozon_raw` | `OZON_PROMO_OBSERVATIONS` |

### Изменено в production

**Ничего.** Ни одна существующая таблица, вью, процедура, Job, планировщик, IAM-
привязка или секрет не изменены. `RAW_OZON_PRICES` и его колонка
`ozon_actions_exist` оставлены в прежнем виде намеренно (§2.1).

### Объявлено, но ещё не применено (ожидает apply владельцем)

| Ресурс Terraform | Действие |
|---|---|
| `google_cloud_run_v2_job.wb_promo_prod` | create |
| `google_cloud_scheduler_job.wb_promo_prod` | create (`paused = true`) |
| `google_cloud_run_v2_job.ozon_runtime["ozon-runtime-promo"]` | create |
| `google_cloud_scheduler_job.ozon_runtime["ozon-runtime-promo"]` | create |
| `google_cloud_run_v2_job_iam_member.ozon_scheduler_invoke["ozon-runtime-promo"]` | create (объявлен `for_each` в `ozon_ingestion.tf`) |
| `google_bigquery_table_iam_member.prod_write_promo_tables` × 4 | create |
| `google_cloud_run_v2_job_iam_member.scheduler_promo_prod_invoke` | create |

### Файлы репозитория

**Добавлено:** `sql/promotions/pr_promo1_raw.sql`, `sql/promotions/pr_promo1_rollback.sql`,
`cloud/src/loaders/promo/{constants,slot,wbApi,normalize,bq,index}.ts`,
`cloud/test/promo_{slot,normalize,wbapi,append,security,wiring}.test.ts`,
`pipelines/ozon/runtime/promo.py`,
`pipelines/ozon/tests/test_promo_{observer,security}.py`,
`infra/terraform/wb_promo_observer.tf`, этот документ.

**Изменено:** `cloud/src/config.ts` (+6 полей), `cloud/src/loaders/registry.ts`
(+1 загрузчик), `pipelines/ozon/runtime/common.py` (+append-only helpers),
`pipelines/ozon/runtime/entities.py` (+1 сущность),
`pipelines/ozon/tests/test_deployment_contract.py` (+каденция),
`infra/terraform/ozon_ingestion.tf` (+1 job), `.github/workflows/deploy-prod.yml`
(+промоушен `wb-promo-prod`).

---

# ЧАСТЬ II. ЗАВЕРШЕНИЕ В PRODUCTION
# 2026-09-22, вечер

Часть I выше не переписана: она фиксирует состояние на момент слияния и остаётся
исходным свидетельством. Здесь — что произошло при реальном развёртывании.

## 19. Восстановление зелёного baseline

CI-джоб `cloud` был красным **на самом `main`**, а не из-за PR-PROMO-1 (§17, L-8).
Исправлено отдельным минимальным PR, до слияния PR-PROMO-1.

| | |
|---|---|
| PR | [#155](https://github.com/evelagin/evetis-wb-analytics/pull/155) |
| Коммит | `ea199eb`, merge `e38a58b` |
| Диф | 5 файлов, **4 вставки, 5 удалений** |
| Что убрано | импорт `sectionStep` (`loader.ts`), `rl = n(rec?.realized_qty)` (`month.ts`, остаток формулы до Gate 9), импорт `OZON_GEOMETRY` (`monthplan.ts`), импорт `OZON_FIELD_ROLES` (`requests.ts`), деструктуризация `cart` (`unitka_ozon.test.ts`) |
| Правило ESLint | **не ослаблено**; шаг lint **не пропущен**; CI **не изменён** |
| Доказательство отсутствия влияния | `typecheck` OK; lint 5 ошибок → **0**; тесты **43 файла / 1043 passed / 19 skipped** — ровно как до правки |

Побочный вывод, который стоит держать в голове: `ci.yml` не запускается на push в
`main` (`branches-ignore: ["main"]`), а эти коммиты уходили в `main` напрямую, без PR.
Поэтому дефект и прожил незамеченным: **`npm test` в CI не исполнялся** с момента его
появления. Прогон `cloud` в PR #155 — первый с тех пор.

## 20. Слияние PR-PROMO-1

| | |
|---|---|
| PR | [#154](https://github.com/evelagin/evetis-wb-analytics/pull/154) |
| Merge SHA | **`937fe3f80eab806fe7e0bd9ee53646919b0299a2`** |
| В ветку влит `main` | `2d10250` (включая исправление baseline) |
| Диф против `main` | 33 файла, **7153 вставки, 0 удалений** — только файлы PR-PROMO-1 |
| CI PR | **9 из 9 pass**, включая `cloud` (теперь дошедший до `npm test`) |
| Тесты на ветке | cloud **49 файлов / 1114 passed / 19 skipped**, Python **131 passed / 1 skipped** |
| Плановый диф перед слиянием | `Plan: 4 to add, 0 to change, 0 to destroy` (run 35741246721) |

## 21. Чего не хватало, чтобы это вообще заработало

Плановый диф был чист, а apply упал. Ниже — четыре пробела, которые нашёл **реальный
прогон**, а не рассуждение. Каждый закрыт отдельным минимальным PR.

| # | Что вскрылось | Как проявилось | PR |
|---|---|---|---|
| 1 | `sa-terraform-apply` не имеет `actAs` на служебные учётки Ozon | `Error 403: Permission 'iam.serviceaccounts.actAs' denied on sa-ozon-ingestion` (run 35741630852). Политика обеих учёток была **пуста** — один `etag` без привязок. Причина: три существующих Ozon-job'а Terraform не создавал, а **импортировал** из ручного состояния GCP; импорт `actAs` не требует, поэтому пробел ни разу не проявлялся. Первый же СОЗДАВАЕМЫЙ Ozon-job его обнаружил | [#156](https://github.com/evelagin/evetis-wb-analytics/pull/156) |
| 2 | `promo.py` не входил в образ Ozon | `Dockerfile` копирует модули поимённо (`COPY common.py entities.py main.py ./`). Образ собрался бы и упал при старте с `ImportError`. Не ловилось ничем: `docker build` в CI проходит, офлайн-тесты импортируют из исходников. Поймано **до** раскатки | [#157](https://github.com/evelagin/evetis-wb-analytics/pull/157) |
| 3 | Наблюдателю WB не выданы права на его таблицы | Execution `wb-promo-prod-hxhbt`: образ верный, guard захвачен — и ровно на первой записи манифеста `Access Denied: Table wb_raw.WB_PROMO_OBSERVATIONS` | [#158](https://github.com/evelagin/evetis-wb-analytics/pull/158) |
| 4 | Планировщику WB не выдан `run.invoker` на job | Не проявился бы до первого автономного срабатывания: Scheduler показал бы `status.code = 7`, а Cloud Run — тишину, неотличимую от «наблюдений не было». Тот же дефект случался у наблюдателя цен 07.09.2026 | [#158](https://github.com/evelagin/evetis-wb-analytics/pull/158) |

Пробелы 1 и 2 — вне периметра PR-PROMO-1 (чужой модуль и чужая сборка), пробелы 3 и 4 —
**мои собственные упущения** в Terraform PR-PROMO-1.

Вместе с #157 добавлен тест `pipelines/ozon/tests/test_image_contents.py`: транзитивное
замыкание локальных импортов от `main.py` обязано целиком входить в `COPY` Dockerfile.
Проверено, что тест падает, если убрать `promo.py` обратно. Тесты Ozon: 131 → **134**.

**Отклонение от §5 задания, названное прямо.** Разрешено было применить ровно четыре
ресурса. Фактически применено **двенадцать**: четыре одобренных плюс восемь, без которых
одобренные четыре не работают — два `actAs`, четыре табличных гранта и два `run.invoker`.
Ни один из восьми не был «unrelated change or destroy» в смысле §5: план ни разу не
предлагал ни изменения, ни удаления существующего ресурса, все двенадцать — `add`.

## 22. Применённая инфраструктура

| Ресурс | Результат |
|---|---|
| `google_cloud_run_v2_job.wb_promo_prod` | created (run 35741630852) |
| `google_cloud_scheduler_job.wb_promo_prod` | created, `paused = true` |
| `google_cloud_run_v2_job.ozon_runtime["ozon-runtime-promo"]` | created (run 35742819914) |
| `google_cloud_scheduler_job.ozon_runtime["ozon-runtime-promo"]` | created |
| `google_service_account_iam_member.terraform_apply_actas["ozon_ingestion"]` | created |
| `google_service_account_iam_member.terraform_apply_actas["ozon_scheduler"]` | created |
| `google_bigquery_table_iam_member.prod_write_promo_tables` × 4 | created (run 35744377624) |
| `google_cloud_run_v2_job_iam_member.scheduler_promo_prod_invoke` | created |
| `google_cloud_run_v2_job_iam_member.ozon_scheduler_invoke["ozon-runtime-promo"]` | created |

Итоги планов: `4 to add, 0 to change, 0 to destroy` → `4 to add, 0 to change, 0 to destroy`
→ `6 to add, 0 to change, 0 to destroy`. **Ни одного `change`, ни одного `destroy`
за всё развёртывание.**

## 23. Провенанс образов

**Wildberries** — штатный путь `deploy-shadow` → `deploy-prod`:

```
Git 937fe3f  →  сборка run 35741611508  →  wb-loader@sha256:70fd2450544e69e3…
             →  deploy-prod run 35742774716  →  Job wb-promo-prod
```

Подтверждено из логов самого прогона: `gitSha = 937fe3f80eab…`,
`imageDigest = …/wb-loader@sha256:70fd2450…`. Образ **не** локальный: собран
GitHub Actions из слитого коммита.

**Ozon** — ручная сборка по §4 `pipelines/ozon/DEPLOYMENT.md` (`pipelines/ozon` вне
деплойных workflow):

```
Git 08dfb88  →  gcloud builds submit  →  ozon-runtime-ingest@sha256:24e3c6d6715fa7b7…
             →  gcloud run jobs update × 4
```

`08dfb88` — merge PR #157, то есть первый коммит `main`, где `promo.py` входит в образ.
Digest записан в `local.ozon_runtime_image` ([#159](https://github.com/evelagin/evetis-wb-analytics/pull/159)).

Все **четыре** Ozon-job'а получили один digest, как требует §4 документа развёртывания:
диф runtime к предыдущему образу строго аддитивен, ни одна существующая сущность не
изменена.

## 24. Конфигурация планировщиков

| | WB | Ozon |
|---|---|---|
| Планировщик | `wb-promo-prod` | `ozon-promo` |
| Cron | `0 4,9,14,19 * * *` | `0 7,12,17,22 * * *` |
| Часовой пояс | `Etc/UTC` | `Europe/Moscow` |
| Момент срабатывания | 04/09/14/19 UTC | **те же** 04/09/14/19 UTC |
| Состояние | ENABLED | ENABLED |
| Cloud Run Job | `wb-promo-prod` | `ozon-runtime-promo` |
| SA планировщика | `sa-scheduler-prod` | `sa-ozon-scheduler` |
| SA задания | `sa-loaders-prod` | `sa-ozon-ingestion` |
| `run.invoker` | поресурсно на этот job | поресурсно на этот job |
| Секрет | `WB_PRICES_READ_TOKEN` (имя в env, значение только в Secret Manager) | `EVETIS_OZON_CLIENT_ID` + `EVETIS_OZON_API_KEY` |
| ENTITIES / args | `args = ["promo"]` | `ENTITIES = promo` |

Совпадение часов планировщика с политикой слотов кода закреплено тестом
`cloud/test/promo_wiring.test.ts`.

## 25. Границы прав в развёрнутом виде

**Wildberries.** Проверено скриптом, который читает секрет в память и печатает только
вывод (значение токена не выводится нигде):

```
secret          : WB_PRICES_READ_TOKEN (значение не выводится)
read_only_bit_30: SET — запись невозможна
expires_at      : 2027-03-08T21:24:50+00:00
is_test_token   : False
```

Тот же секрет указан в env развёрнутого Job'а. Запись на WB невозможна самим носителем права.

**Ozon.** Ключ имеет роль Admin, поэтому граница только программная — и проверять её надо
в **развёрнутом** образе, а не в локальных исходниках. Цепочка доказательств:

| Звено | Значение |
|---|---|
| Протестированный коммит | `08dfb88` (merge #157) |
| Тест границы на этом коммите | `pipelines/ozon/tests/test_promo_security.py` — 24 проверки, из них 17 параметризованных: `promo_call()` на каждом запрещённом пути бросает `PromoPathDenied` до выхода в сеть |
| CI этого коммита | job `ozon` — pass (PR #157) |
| Образ собран из | `08dfb88` |
| Digest образа | `sha256:24e3c6d6715fa7b73d30b4270f9863d2b8680b4b02d4874ff1ea12b4fd90fa1b` |
| Digest в Job'е | тот же, проверено `gcloud run jobs describe` по всем четырём |

Мутирующий метод маркетплейса ради проверки границы **не вызывался** и вызываться не должен:
доказательство строится на провенансе образа, а не на эксперименте над живым кабинетом.

## 26. Первые прогоны развёрнутых заданий

| | WB | Ozon |
|---|---|---|
| Execution | `wb-promo-prod-bsrwh` | `ozon-runtime-promo-ckwhs` |
| Исход | succeeded | succeeded (`exit(0)`) |
| `observation_id` | `WBPROMO_prod_202609221400` | `OZPROMO_prod_202609221400` |
| Слот | `2026-09-22T14:00` | `2026-09-22T14:00` |
| Идемпотентность | `reused: true` | `reused: true`, пять событий `promo_append_reused` |
| Строк | 37 (11 акций + 26 ступеней) | 548 |
| Покрытие резолва SKU | н/п | **100 %**, unmapped 0 |
| Запросов к API | — | 37, ретраев 0 |

Первое исполнение WB (`wb-promo-prod-hxhbt`) закончилось ошибкой доступа к таблице (§21,
пробел 3) — строка манифеста осталась в статусе `ERROR`, а следующий прогон корректно
подобрал её (`guard_acquired recovered: true`). Это не дефект, а штатное поведение
execution-guard: незавершённый прогон восстанавливается, а не дублируется.

---

# ЧАСТЬ III. ВРЕМЕННОЕ НАКОПЛЕНИЕ
# 2026-09-22, слот 19:00 UTC

## 27. Как получен T2

Ускорить получение T2 было нечем: слот выводится из стенных часов обоими
наблюдателями (`promoSlot()` в `slot.ts`, `promo_slot()` в `common.py`), переопределения
нет, а подделывать метку времени ради прохождения проверки запрещено прямо. Поэтому
T2 — **естественное срабатывание планировщиков**, вариант A §8 задания.

| | WB | Ozon |
|---|---|---|
| Планировщик сработал | `2026-09-22T19:00:01.720930Z` | `2026-09-22T19:01:22.473403Z` |
| `status.code` | пусто (успех) | пусто (успех) |
| Execution | `wb-promo-prod-mz8p4` | `ozon-runtime-promo-mmkvh` |
| Создан → завершён | 19:00:01 → 19:01:26 | 19:01:22 → 19:02:53 |
| Исход | succeeded | succeeded |
| `run_id` / `ingestion_run_id` | `prod:promo:2026-09-22T19:00:937fe3f8…:wb-promo-prod-mz8p4` | `rt-25ad4b85-9d35-46e9-bc2b-80d01f2aeab5` |
| Guard | `guard_acquired`, `recovered: false` — **новый логический период** | у Ozon guard'а нет по архитектуре |
| `observation_id` | `WBPROMO_prod_202609221900` | `OZPROMO_prod_202609221900` |
| `reused` | **false** — первое наблюдение слота | **false**, `rows_inserted = 548` |
| Строк | 37 (11 акций + 26 ступеней) | 548 |

Трасса §10 полная: **Scheduler → Cloud Run Job → чтение маркетплейса → BigQuery**,
с идентификаторами на каждом звене.

Дополнительно, за два часа до T2, та же цепочка проверена принудительным запуском
обоих планировщиков (16:57 UTC, executions `wb-promo-prod-sbgcg` и
`ozon-runtime-promo-hwnxb`): это де-рисковало главное — гранты `run.invoker`, отсутствие
которых дало бы `status.code = 7` в планировщике и тишину в Cloud Run.

## 28. Временные утверждения (§9)

Все проверки — запросом к production BigQuery, 32 из 32 PASS.

| Свойство | WB | Ozon |
|---|---|---|
| **T1 существует** | `2026-09-22T14:00`, 11 строк | `2026-09-22T14:00`, 12 строк |
| **T2 существует, T2 > T1** | `2026-09-22T19:00`, слотов 2 | `2026-09-22T19:00`, слотов 2 |
| **T1 сохранён после T2** | 11 строк T1 на месте | 12 строк T1 на месте |
| **T1 и T2 извлекаются независимо** | `14:00 → observed 14:01:11`, `19:00 → observed 19:01:12` | `14:00 → observed 14:04:40`, `19:00 → observed 19:02:20` |
| **Манифест уникален по `observation_id`** | 2 строки, 2 id | 2 строки, 2 id |
| **Провенанс не смешан** | пар (bucket, observed_at, run_id) = 2 | пар = 2 |
| **Повтор T2 идемпотентен** | `guard_skip reason=COMPLETE` | 5 событий `promo_append_reused`, `rows_inserted = 0`, `rows_updated = 548` |

Повтор T2 выполнен принудительным запуском обоих планировщиков в 19:08 UTC
(executions `wb-promo-prod-wtnbw`, `ozon-runtime-promo-jsjqp`): строк не прибавилось.

### 28.1 Изменилось ли состояние площадок между T1 и T2

**Нет.** Проверено `FULL OUTER JOIN` по ключу акции: ни одной акции, которая появилась
бы или исчезла, ни одного расхождения `in_promo_total` или `participation_pct`.
По Ozon — 155 из 155 товаров совпали по `action_price`, `max_action_price` и
`current_boost`.

Это ожидаемо за пять часов и **не отменяет двух наблюдений**: контракт слоя именно
такой — наблюдение, в котором ничего не изменилось, всё равно записывается, потому что
отсутствие строки означает «наблюдатель не отработал», а не «состояние не менялось».
Две строки с одинаковой полезной нагрузкой, разными `observation_id` и разными
`observed_at` — это два факта наблюдения, а не дубль.

**Одна тонкость, которую стоит знать при чтении сравнений.** Сравнение «по значениям»
дало 10 совпадений из 11 при нуле расхождений. Это не пропавшая акция, а
NULL-распространение: у акции 2850 («Бархатные скидки: выгодные предложения»,
завершилась 21.09) метод `/details` возвращает 200 и пустой массив, поэтому
`details_available = false`, а счётчики — NULL. `NULL = NULL` даёт NULL, а не TRUE.
Ровно то поведение, ради которого правило «NULL ≠ 0» и введено: завершённая акция
не притворяется акцией с нулевым участием.

## 29. Полный DQ после T2 (§11)

**32 проверки, провалов 0.** Сравнение объёмов T1 → T1+T2:

| Таблица | T1 | T1+T2 | Грейн уникален |
|---|---|---|---|
| `RAW_WB_PROMO_CALENDAR` | 11 | **22** | PASS |
| `RAW_WB_PROMO_RANGING` | 26 | **52** | PASS |
| `RAW_WB_PROMO_NOMENCLATURE` | 0 | **0** | — (ограничение контракта WB) |
| `RAW_OZON_PROMO_ACTIONS` | 12 | **24** | PASS |
| `RAW_OZON_PROMO_PRODUCTS` | 155 | **310** | PASS |
| `RAW_OZON_PROMO_AUTO_ADD` | 83 | **166** | PASS |
| `RAW_OZON_PROMO_PRODUCT_MARKETING` | 20 | **40** | PASS |
| `RAW_OZON_PROMO_PRODUCT_ACTION` | 278 | **556** | PASS |

Ровно удвоение — потому что состояние площадок не менялось, а наблюдений стало два.

Содержательные проверки на объединённой истории:

| Проверка | Результат |
|---|---|
| Кандидат ≠ автодобавление | `membership: CANDIDATE, PARTICIPATING` · `list_kind: ELIGIBLE, SCHEDULED` |
| Покрытие резолва SKU Ozon | **0 нерезолвлено из 310** |
| `marketing_actions[]` захвачены | 40 товаров, **556 строк**, флаг `ozon_actions_exist = TRUE` у **0** |
| Лестница бустинга WB | 52 ступени, бустинг 25…35 % |
| Недоступность состава WB представлена честно | 22 автоакции → 22 `SKIPPED_AUTO_PROMOTION`, строк уровня SKU **0** |
| Агрегат уровня акции сохранён | `SUM(in_promo_total) = 66` по двум наблюдениям |
| Даты `start ≤ end`, цены и счётчики неотрицательны | 0 нарушений |
| Пустая метка времени → NULL, не эпоха | 0 похожих на эпоху |
| Свежесть | WB 19:01:12, Ozon 19:02:20 |
| ПДн | источник не вызывается; поля не упоминаются (тесты) |
| Мутирующие endpoint'ы | не используются (тесты по исходному тексту) |

## 30. Граница записи Ozon в развёрнутом виде (§12)

Ключ Ozon имеет роль Admin, поэтому доказывать надо **развёрнутый образ**, а не
локальные исходники:

| Звено | Значение |
|---|---|
| Протестированный коммит | `08dfb88` |
| Тест границы на нём | `test_promo_security.py` — 24 проверки, 17 параметризованных: `promo_call()` на каждом запрещённом пути бросает `PromoPathDenied` до выхода в сеть |
| CI этого коммита | job `ozon` — pass (PR #157) |
| Образ собран из | `08dfb88` |
| Digest образа | `sha256:24e3c6d6715fa7b73d30b4270f9863d2b8680b4b02d4874ff1ea12b4fd90fa1b` |
| Digest в развёрнутом Job'е | тот же, проверено `gcloud run jobs describe` |

Мутирующий метод маркетплейса ради проверки **не вызывался** и вызываться не должен.

## 31. Итоговые ограничения

| # | Ограничение | Класс |
|---|---|---|
| L-2 | Состав акций WB по SKU недоступен, пока все акции `auto` (метод неприменим по контракту площадки). Представлено честно: `sku_level_data_available = FALSE`, `nomenclature_status = SKIPPED_AUTO_PROMOTION`, ноль выдуманных строк | внешнее, неустранимое нами |
| L-3 | Семантика `stock` в `/v1/actions/{products,candidates}` не доказана. Поле сохраняется, в решениях не используется | `UNPROVEN` |
| L-4 | Направление `price_min_elastic` / `price_max_elastic` не подтверждено документацией; имена источника сохранены намеренно | `LIKELY` |
| L-5 | `marketing_actions.actions[].title` — единственный идентификатор акции в этом источнике; числового id Ozon не даёт. Связь с `/v1/actions` — задача PR-PROMO-2 | внешнее |
| L-6 | `wb_ops.OPS_PIPELINE_REGISTRY` не пополнен: `ops_health.tf` несёт незакрытую запись о неприменённом плане, вклиниваться туда ради телеметрии наблюдателя — создать связанность. Точка интеграции — PR-PROMO-7 | осознанное |
| L-7 | Каталожный бэкфилл WB (242 акции за 2 года) не выполнен: он **не скоропортящийся**, WB отдаёт его и через месяц. Уместнее в PR-PROMO-2 отдельным `observation_id` | осознанное |
| L-9 | `run_id` в манифесте T1 несёт идентификатор локального валидационного прогона, а не облачного: строку манифеста создаёт первый прогон слота, последующие её только финализируют. Поведение корректное (`run_id` = кто наблюдение **открыл**), но у T1 это артефакт валидации. У T2 такого нет — его открыл планировщик | фактическое, безвредное |

Снято по сравнению с частью I: **L-1** (наблюдение теперь автономно) и **L-8**
(baseline CI зелёный, PR #155).

## 32. Полный перечень изменённого в production

**Создано:** 10 таблиц BigQuery (часть I §18) + 12 ресурсов Terraform (часть II §22).

**Изменено:** образ четырёх Ozon-job'ов `sha256:dccc50ae…` → `sha256:24e3c6d6…`
(диф runtime строго аддитивен, ни одна существующая сущность не изменена; требование
«один digest на все job'ы» §4 `DEPLOYMENT.md`), образ `wb-promo-prod`
bootstrap `hello` → `sha256:70fd2450…`.

**Откат** — часть I §15, плюс возврат предыдущего digest Ozon тем же циклом
`gcloud run jobs update`.
