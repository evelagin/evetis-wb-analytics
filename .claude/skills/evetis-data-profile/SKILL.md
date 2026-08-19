---
name: evetis-data-profile
description: Профилирует новые или изменившиеся таблицы EVETIS перед аналитикой и моделированием: schema, grain, keys, freshness, NULL, duplicates, distributions, gaps, referential integrity и schema drift. Использовать перед подключением нового WB endpoint/table или при подозрении на изменение данных.
---

# EVETIS Data Profile

## Принцип

Не анализируй и не моделируй незнакомую таблицу до профилирования. Этот workflow адаптирует сильные идеи data exploration под BigQuery и контракты EVETIS.

## 1. Structure

Для таблицы установи:
- назначение и producer;
- число строк и колонок;
- типы колонок;
- предполагаемый grain;
- natural/primary key;
- partition/cluster configuration;
- минимальную и максимальную business date;
- последнюю загрузку и ожидаемую cadence.

## 2. Column profile

Для ключевых колонок проверь:
- NULL и empty rate;
- distinct count/cardinality;
- top values;
- min/max для чисел и дат;
- отрицательные/нулевые значения там, где они подозрительны;
- новые enum/status значения;
- type/format consistency.

Особое внимание: `nmId`/`nm_id`, barcode, vendorCode/internal_sku, srid/rrdId/report_id, advert/campaign ids, warehouse keys и даты.

## 3. Grain proof

Нельзя объявлять grain только по названию таблицы. Докажи его:
- `COUNT(*)`;
- `COUNT(DISTINCT key)` либо GROUP BY составного ключа;
- список duplicate groups;
- NULL по каждому элементу ключа.

Если grain не доказан, пометь его как hypothesis, а не факт.

## 4. Time and freshness

Проверь:
- max event/business date;
- max loaded_at/built_at, если доступно;
- gaps по ожидаемой cadence;
- partial current day;
- late arrivals;
- разницу event time vs load time.

Не сравнивай неполный текущий период с полным предыдущим без явной пометки.

## 5. Relationships

Для предполагаемых joins проверь:
- coverage FK -> dimension;
- unmatched SKU;
- cardinality join keys;
- возможный N:N;
- изменение ключа producer-а относительно downstream consumer-а.

## 6. EVETIS-specific checks

- Финансовые источники: не смешивать DAILY/PROVISIONAL и WEEKLY/FINAL вне утвержденного canonical слоя.
- Stocks: учитывать эволюцию warehouse identifier contract; не предполагать, что старый ключ остаётся non-null.
- Ads: не объявлять campaign-level расход точным SKU-level фактом без доказанной связи.
- Bundles/cost: проверять effective date и соответствие составу набора.

## Output

Выдай:
1. таблица и source;
2. доказанный grain;
3. keys и их качество;
4. freshness/date coverage;
5. data quality findings с severity High/Medium/Low;
6. join risks;
7. schema drift;
8. пригодность: RAW only / FACT-ready / MART-ready / BI-ready;
9. следующие проверки перед изменением кода.

Не исправляй найденную проблему в том же шаге без отдельного анализа impact.
