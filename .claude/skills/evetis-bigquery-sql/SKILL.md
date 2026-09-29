---
name: evetis-bigquery-sql
description: Пишет и проверяет BigQuery Standard SQL для EVETIS: views, tables, procedures, MERGE, partitions, clustering, grain-safe joins, ASSERT и cost-aware запросы. Использовать при работе с SQL в `sql/`, BigQuery-моделями, витринами и изменениями постоянных BigQuery-объектов или данных.
---

# EVETIS BigQuery SQL

## Назначение

Пиши Google BigQuery Standard SQL в стиле существующего проекта EVETIS.

Сначала корректность, grain и сохранение действующего контракта; затем производительность и стоимость.

Не считай `bq query` read-only по имени команды. Эффект определяется SQL.

## Перед SQL

Определи:

- authoritative source и действующий контракт;
- grain источника;
- grain результата;
- ключ результата;
- период и временную семантику;
- downstream consumers;
- partition/clustering, если они применимы;
- является операция чтением или изменяет постоянные объекты/данные.

Не предполагай наличие partition/clustering там, где их нет или где они не нужны.

Если задача зависит от текущего production SQL/object state, не считай Git автоматически равным production.

## Read vs write

Read-only запрос не должен изменять постоянные BigQuery-объекты или данные.

К write относить, в частности, DDL/DML и другие операции, создающие, заменяющие, изменяющие или удаляющие постоянные объекты/данные.

Перед write:

1. установи точный target;
2. определи affected scope;
3. проверь действующий контракт и downstream impact;
4. подготовь validation и rollback;
5. убедись, что write является явной частью задачи и разрешён правилами `CLAUDE.md`.

Не превращай диагностический запрос в materialization, DDL или DML без необходимости.

## BigQuery rules

- Используй backticks для полных имён объектов, когда это соответствует существующему SQL проекта.
- Для деления предпочитай `SAFE_DIVIDE`, если контракт не требует другого поведения; явно определяй семантику нулевого знаменателя.
- Не полагайся на неявные casts для business keys и денежных полей.
- Для DATE/TIMESTAMP явно контролируй временную семантику и timezone.
- Не смешивай UTC и бизнес-день молча.
- Используй partition pruning, когда объект действительно partitioned и фильтр соответствует семантике запроса.
- Избегай `SELECT *` в стабильных production contracts, если схема результата является частью контракта.
- При `CREATE OR REPLACE` сначала определи изменение публичного контракта и downstream impact.
- При `MERGE` ключ сопоставления должен соответствовать доказанному grain.
- Incremental/backfill логика должна учитывать idempotency, late arrivals и повторный запуск.
- Не используй оптимизацию стоимости как основание для изменения бизнес-семантики.

## JOIN safety

Перед JOIN определи cardinality каждой стороны: 1:1, 1:N, N:1 или N:N.

Для потенциально опасного JOIN:

1. установи grain обеих сторон;
2. проверь uniqueness ключа на стороне, которая должна быть `1`;
3. определи ожидаемую cardinality результата;
4. проверь отсутствие неожиданного row multiplication;
5. проверь критичные additive metrics, если JOIN может изменить их.

Many-to-many JOIN без доказанного бизнес-смысла и контроля multiplicity не использовать.

`ANY_VALUE` не является способом исправить недоказанную cardinality.

## Grain и aggregation

Не агрегируй разные бизнес-события до общего grain только ради удобства JOIN.

Перед агрегацией определи:

- какие dimensions сохраняются;
- какие теряются;
- является ли metric additive, semi-additive или non-additive;
- допустимо ли суммирование по времени и другим dimensions.

Marketplace-specific факты WB и Ozon не считать семантически одинаковыми только из-за совпадающих названий колонок.

## Fail-closed invariants

Критичные инварианты должны иметь исполнимую проверку, когда проектный contract/gate этого требует.

Примеры рисков:

- NULL/empty в ключе;
- duplicate grain;
- нарушение referential integrity;
- невозможные даты;
- double count;
- unexpected row multiplication;
- нарушение зарегистрированного financial/data contract.

Не создавай новый `ASSERT` только потому, что такой паттерн существует. Сначала проверь действующие suites, contracts и существующий способ приёмки объекта.

Диагностический SELECT с комментарием «должно быть 0» не заменяет зарегистрированный gate.

## Validation

Для SQL проекта используй существующую систему доказательств вместо собственного универсального checklist.

Основные инструменты:

- `tools/impact_analysis.py` — определить затронутые объекты и обязательные suites;
- `tools/validate_current_sql.py` — проверить контракт `sql/current`, когда он применим;
- `tools/verify_current_sql_live.py` — read-only Git/production parity, когда он применим;
- `tools/run_data_checks.py` — зарегистрированные data-contract checks;
- `tools/verify_task.py` — итоговый Definition of Done.

Не предполагай, что каждый SQL-файл обязан проходить каждый из этих инструментов: применимость определяется архитектурой и impact analysis проекта.

SQL validation tooling требует штатных зависимостей проекта. В текущем репозитории `sqlglot` закреплён через `tools/requirements-sql-ci.txt`; не устанавливай произвольную или глобальную версию ради обхода отсутствующего окружения.

BLOCKED, EMPTY или UNPROVEN не считать PASS.

## Consumption

Для BI и других consumers предпочитай стабильный upstream SQL contract вместо дублирования критичной бизнес-логики в dashboard.

Проектируй dimensions и metrics только на тех разрезах, которые поддерживаются grain источника.

Не оптимизируй SQL под конкретную визуализацию ценой нарушения общего data contract.

## Перед завершением изменения

Для существенного SQL/data-contract изменения зафиксируй:

- изменившийся объект и grain;
- affected scope;
- downstream impact;
- применимые machine gates;
- результат validation;
- production verification, если она требуется;
- rollback для write/deploy изменения.

Для локального read-only анализа этот процесс не раздувать без необходимости.

## Запрещено

- считать `bq query` автоматически read-only;
- выполнять DDL/DML как побочный шаг диагностики;
- использовать `ANY_VALUE` без доказанной однозначности;
- маскировать нарушение grain дополнительным `DISTINCT`;
- смешивать разные временные семантики без явного контракта;
- считать успешное выполнение SQL доказательством корректности данных;
- считать Git автоматически равным production;
- дублировать бизнес-логику BI-слоем;
- зашивать в skill текущие контрольные суммы, ставки, размеры таблиц или другую быстро меняющуюся production-конфигурацию.
