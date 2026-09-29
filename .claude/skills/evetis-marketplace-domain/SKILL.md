---
name: evetis-marketplace-domain
description: Применяет доменную модель маркетплейсов EVETIS к данным, метрикам и коду: Wildberries, Ozon, orders, sales, returns, cancellations, finance, ads, promotions, prices, stocks, SKU, costs, profit and margins. Использовать когда задача требует трактовки marketplace-полей, выбора authoritative source, сопоставления WB/Ozon или расчёта бизнес-метрик.
---

# EVETIS Marketplace Domain

## Цель

Определяй экономический и операционный смысл показателя до написания SQL, кода или аналитического вывода.

Wildberries и Ozon — разные домены источников. Не переносить семантику поля, события или статуса с одного маркетплейса на другой только из-за похожего названия.

Нормализовать данные между маркетплейсами только после того, как отдельно доказана семантика каждого источника.

## Перед работой

1. Определи маркетплейс и бизнес-вопрос.
2. Найди актуальный authoritative contract или source-of-truth в репозитории.
3. Проверь grain, период, временную семантику и идентификаторы.
4. Определи, является показатель billing, attribution, operational или derived metric.
5. Только после этого выбирай таблицу, API-поле, SQL или формулу.

Не считать документ authoritative только потому, что он новее по дате. Приоритет имеют действующий контракт, deployed/current implementation и подтверждённый production state.

Если источники расходятся, не выбирать один молча: зафиксировать расхождение и определить, какой контракт регулирует конкретный бизнес-вопрос.

## Marketplace boundaries

### Wildberries

Используй WB-контракты и WB-источники для трактовки:

- orders, sales, returns и cancellations;
- finance и marketplace deductions;
- advertising;
- promotions и prices;
- stocks, warehouses и geography;
- WB identifiers и SKU mapping.

Не считать API orders эквивалентом финансово признанной продажи без соответствующего контракта.

### Ozon

Используй Ozon-контракты и Ozon-источники для трактовки:

- postings, orders, cancellations и buyout;
- accruals и financial transactions;
- advertising;
- promotions и prices;
- stocks и fulfillment;
- Ozon identifiers и SKU/product mapping.

Не выводить финансовый смысл Ozon из названия operation/accrual type без проверки действующего financial contract или mapping.

## Cross-marketplace layer

Общий слой EVETIS должен нормализовать бизнес-смысл, а не названия колонок.

Перед объединением WB и Ozon докажи:

- одинаков ли grain;
- одинаково ли определяется событие продажи;
- одинаков ли временной базис;
- одинаково ли учитываются возвраты и отмены;
- сопоставимы ли advertising и promotion semantics;
- одинаков ли экономический смысл revenue, costs, contribution, profit и margin.

Если определения различаются, сохраняй marketplace-specific семантику до слоя, где нормализация явно определена контрактом.

## Finance safeguards

Для финансовых показателей сначала найди действующий финансовый контракт проекта.

Не смешивай:

- billing и attribution;
- cash movement и economic recognition;
- revenue и marketplace payout;
- advertising charge и attributed advertising;
- marketplace deductions разных типов;
- COGS и marketplace expenses;
- текущий полный период и partial period.

Не изобретай отсутствующую бизнес-логику. Не подменяй контракт "разумной" формулой.

### Advertising

Различай:

- финансовый/billing факт рекламного расхода;
- campaign/statistics fact;
- analytical attribution расхода к SKU или другому объекту.

Если source или действующий mapping не доказывает точный `SKU -> spend`, не представляй SKU-level advertising spend как authoritative fact. Derived attribution допустима только с явной семантикой и методом.

## Promotions and prices

Promotion participation, eligibility, auto-add, candidate state, base price, seller price и customer-facing price могут иметь разную семантику по маркетплейсам.

Перед расчётом promotion economics:

1. установи marketplace-specific state;
2. найди действующий promotion/price contract;
3. установи цену, к которой относится расчёт;
4. используй действующую экономическую модель проекта;
5. отделяй наблюдаемое состояние от предложения или сценария.

Не считать proposed action фактически применённым изменением.

## SKU and identity

Не соединяй сущности только по display name.

Используй подтверждённые идентификаторы и mapping проекта. Проверяй направление и кардинальность mapping перед JOIN.

Особенно различай:

- marketplace product identifiers;
- offer/vendor codes;
- seller SKU;
- EVETIS canonical SKU;
- bundle/set и его компоненты.

Для cost/bundle semantics:

- используй действующий cost contract и effective date;
- не применяй текущую себестоимость к историческому периоду без доказанного правила;
- для набора учитывай его компонентный состав по действующему bundle/set mapping;
- не считай стоимость набора простой суммой компонентов, если контракт определяет её иначе.

## Inventory

При анализе остатков различай как минимум:

- marketplace;
- fulfillment model;
- warehouse/location;
- physical stock;
- available stock;
- reserved/in-transit state, если он существует в источнике;
- snapshot time.

Не суммируй разные inventory states как один остаток без явного определения метрики.

## Business question workflow

Перед ответом на бизнес-вопрос сформулируй:

- что именно измеряется;
- за какой период;
- на каком grain;
- какой marketplace/source authoritative;
- какой контракт определяет показатель;
- полный ли период;
- какие известны ограничения данных.

Если вопрос затрагивает несколько контрактов, перечисли их роли вместо выбора одного универсального источника.

## Validation

При изменении кода или данных не придумывай собственный набор regression checks, если проект уже знает зависимости.

Используй существующую систему доказательств:

- `tools/impact_analysis.py` — определить затронутые объекты и обязательные suites;
- `tools/run_data_checks.py` — выполнить зарегистрированные data-contract checks;
- `tools/verify_task.py` — собрать итоговые доказательства Definition of Done.

Используй доступный Python interpreter проекта. Не устанавливай отсутствующие зависимости глобально только ради прохождения gate.

## Запрещено

- переносить семантику WB на Ozon или наоборот без доказательства;
- определять бизнес-смысл только по названию поля;
- считать более новый документ автоматически authoritative;
- смешивать billing и attribution;
- соединять SKU по похожему названию;
- превращать proposed/modelled state в observed/applied state;
- зашивать в skill текущие ставки, контрольные суммы, количества объектов или другие быстро меняющиеся production-факты;
- обходить действующий контракт собственной формулой без явного основания.
