---
name: evetis-data-profile
description: Профилирует новые или изменившиеся источники и таблицы EVETIS перед аналитикой и моделированием: schema, grain, keys, freshness, NULL, duplicates, distributions, gaps, referential integrity и schema drift. Использовать перед подключением нового marketplace/API source, изменением data contract или при подозрении на изменение данных.
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

Особое внимание уделяй business identifiers, marketplace product/SKU keys, transaction/event ids, campaign/promotion ids, warehouse keys и датам.

Не предполагай одинаковую семантику идентификаторов WB и Ozon. Конкретные ключи определяй по действующему marketplace/data contract.

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

- Finance: перед трактовкой revenue, deductions, advertising, costs или profit найди действующий financial/data contract; не смешивай факты с разной recognition semantics.
- Marketplace: профилируй WB и Ozon в их собственной семантике до cross-marketplace normalization.
- Stocks: устанавливай фактическую семантику warehouse и inventory identifiers; не предполагай стабильность ключа между источниками или версиями producer.
- Ads: не объявляй campaign-level расход точным SKU-level фактом без доказанной связи.
- Promotions/prices: различай observed state, eligibility/candidate state и proposed/modelled action.
- Bundles/cost: проверяй effective date и соответствие состава набора действующему контракту.

## Output

Выдай:
1. таблица и source;
2. доказанный grain;
3. keys и их качество;
4. freshness/date coverage;
5. data quality findings с severity High/Medium/Low;
6. join risks;
7. schema drift;
8. пригодность источника для конкретного предполагаемого consumer/use case;
9. unresolved assumptions и ограничения;
10. следующие проверки перед изменением кода.

Не исправляй найденную проблему в том же шаге без отдельного анализа impact.
