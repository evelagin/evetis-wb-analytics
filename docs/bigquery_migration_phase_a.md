# BigQuery migration — Phase A

## Цель

Вынести RAW-хранилище из Google Sheets в BigQuery, оставив Apps Script как слой загрузки данных из WB API.

На этой фазе существующие листы и загрузчики не меняются. Добавляется только базовый слой доступа к BigQuery и self-test.

## Архитектура

Apps Script → BigQuery RAW tables → SQL views / marts → Looker Studio / thin Google Sheets control panel.

## GCP / BigQuery конфиг

Script Properties:

- `BQ_PROJECT_ID` — точный GCP Project ID.
- `BQ_DATASET` — по умолчанию `wb_raw`.
- `BQ_LOCATION` — по умолчанию `EU`.

Project ID не хранится в репозитории. В `bqSaveConfig_ONE_TIME()` оставлен плейсхолдер `PASTE_EXACT_GCP_PROJECT_ID_HERE`, который нужно заменить вручную в редакторе Apps Script перед первым запуском.

## Новый файл

`apps-script/WbBigQuery.gs`

Содержит:

- чтение/сохранение BigQuery-конфига;
- создание датасета;
- создание таблицы `RAW_WB_FINANCE`;
- batch load через `NEWLINE_DELIMITED_JSON`;
- SQL query helper;
- `bqSelfTest()` для проверки доступа без касания боевых данных.

## Безопасность

- Существующие RAW-листы в Google Sheets не меняются.
- Существующие WB-загрузчики не переключаются на BigQuery на этой фазе.
- `bqSelfTest()` создаёт и удаляет только временную таблицу `_selftest`.
- Боевая таблица `RAW_WB_FINANCE` создаётся только отдельным запуском `bqCreateFinanceTable()`.
- В репозиторий не коммитится реальный GCP Project ID.

## Схема RAW_WB_FINANCE

На Phase A RAW-таблица создаётся с колонками, близкими к текущей схеме листа `RAW_WB_FINANCE`.

Большинство полей хранятся как `STRING`, чтобы не терять исходное сырьё при переносе. Типизация денег, дат и идентификаторов будет выполняться в SQL-витринах / marts.

Дополнительно добавляется `_rr_date DATE` для партиционирования по дате операции.

## Порядок запуска в Apps Script

1. Скопировать `apps-script/WbBigQuery.gs` в редактор Apps Script.
2. Включить Advanced Google Service: BigQuery API.
3. Убедиться, что Apps Script связан с нужным GCP Project Number.
4. В `bqSaveConfig_ONE_TIME()` заменить `PASTE_EXACT_GCP_PROJECT_ID_HERE` на точный Project ID из GCP Console.
5. Запустить `bqSaveConfig_ONE_TIME()`.
6. Запустить `bqShowConfig()`.
7. Запустить `bqSelfTest()`.

Ожидаемый результат:

`✅ SELF-TEST OK: доступ, создание, загрузка и чтение работают.`

После успешного self-test можно запускать:

`bqCreateFinanceTable()`

## Следующая фаза

Phase B:

- добавить конвертер строк `RAW_WB_FINANCE` → BigQuery rows;
- перенаправить новый Finance API v1 loader в BigQuery;
- выполнить backfill финансов без лимита ячеек Google Sheets;
- сверить суммы `for_pay`, `retail_amount`, `logistics_amount` с текущим RAW и WB list totals.
