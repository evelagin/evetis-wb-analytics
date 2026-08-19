---
name: evetis-bigquery-sql
description: Пишет и проверяет BigQuery SQL для EVETIS WB Analytics: partitions, clustering, MERGE, views, procedures, ASSERT, grain-safe joins и cost-aware запросы. Использовать при любой работе с SQL в `sql/`, BigQuery FACT/MART/views и dashboard layer.
---

# EVETIS BigQuery SQL

## Назначение

Пиши Google BigQuery Standard SQL в стиле существующего проекта EVETIS. Сначала корректность и сохранение контракта, затем оптимизация.

## Перед SQL

Определи:
- source table/view;
- grain источника;
- grain результата;
- ключ результата;
- partition column;
- cluster columns;
- период и timezone;
- downstream consumer;
- какие суммы/row counts должны сохраниться.

## BigQuery правила

- Используй backticks для полных имен таблиц и views.
- Для деления предпочитай `SAFE_DIVIDE` и явно определяй поведение при нулевом знаменателе.
- Не полагайся на неявные casts для business keys и денежных полей.
- Для дат/времени явно контролируй DATE/TIMESTAMP и timezone; не смешивай UTC и московский бизнес-день молча.
- Фильтруй partition как можно раньше.
- Избегай `SELECT *` в production contracts, если схема результата важна.
- При `CREATE OR REPLACE` сначала докажи сохранение исторического контракта.
- При `MERGE` natural key должен совпадать с доказанным grain.
- Для incremental/backfill логики учитывай late arrivals и idempotency.
- Денежные суммы сверяй независимо хотя бы двумя способами на контрольном периоде.

## JOIN safety

Перед JOIN определи cardinality каждой стороны: 1:1, 1:N, N:1 или N:N.

Для потенциально опасного JOIN:
1. посчитай строки и distinct grain до JOIN;
2. проверь uniqueness join key на стороне, которая должна быть 1;
3. выполни JOIN;
4. снова проверь rows/distinct grain и денежные суммы;
5. при неожиданном росте строк остановись и объясни причину.

## ASSERT pattern

Критические бизнес-инварианты должны блокировать publish, когда существующая процедура поддерживает fail-closed подход. Типовые инварианты:
- key IS NOT NULL / не пуст;
- grain unique;
- неизвестные SKU не превышают допустимый контракт;
- нет невозможных дат;
- canonical/final выбор не создаёт double count;
- суммы контрольного периода сходятся в установленном допуске.

Диагностический SELECT с комментарием «должно быть 0» не считается gate.

## Производительность для Looker

При подготовке таблиц для Looker Studio:
- не заставляй Looker сканировать RAW;
- выдавай consumption-ready MART/view;
- сохраняй понятные dimensions и additive/semi-additive metrics;
- избегай тяжёлых dashboard-level calculated fields для основных KPI;
- проектируй фильтрацию по date, SKU/nm_id, campaign, region/warehouse только там, где grain реально поддерживает этот разрез.

## Проверка перед коммитом

Всегда выдай:
- ожидаемый affected row set;
- проверки grain/NULL/duplicates;
- контрольные суммы;
- impact на partitions/clustering;
- downstream consumers;
- rollback plan.
