---
name: evetis-wb-domain
description: Применяет доменную модель Wildberries и EVETIS к данным, метрикам и коду: orders, sales, returns, finance, ads, stocks, SKU, costs, bundles, profit and margins. Использовать когда задача требует трактовки WB-полей, выбора источника истины или расчёта бизнес-метрик.
---

# EVETIS Wildberries Domain

## Главный принцип

Сначала определить экономический смысл показателя и его authoritative source, затем писать SQL/код. Не выводить бизнес-смысл только из похожего названия API-поля.

## Сущности

Различай:
- order — оформление заказа;
- sale — выкуп/продажа;
- return — обратное движение;
- WB finance realization/financial fact;
- payout/к перечислению;
- commission;
- logistics;
- storage;
- acquiring;
- deductions/penalties;
- advertising fact;
- advertising campaign statistics;
- COGS;
- tax;
- management profit.

`order`, `sale`, `realization`, `payout` и `profit` не являются взаимозаменяемыми.

## Sources

Следуй актуальным контрактам проекта, а не общим знаниям о WB.

- Orders/Sales/Stocks/Ads API дают операционные факты соответствующего контура.
- Финансовый факт берётся из утверждённого finance canonical/finance pipeline проекта.
- `SKU_MASTER` — товарный mapping.
- `COST_HISTORY` — себестоимость с временной применимостью.
- `BUNDLES` — состав наборов.
- MART/FACT — предпочтительный слой для повторной аналитики и BI после валидации.

Если документация проекта и память модели расходятся, документация/проверенный production-контракт проекта приоритетнее.

## Finance safeguards

- DAILY может быть provisional, WEEKLY final: используй действующую canonical replacement logic, не суммируй слои напрямую.
- Не считай payout чистой прибылью.
- Не меняй знак расходов без проверки реального контракта поля.
- PNL должен быть воспроизводим из источников с контрольными суммами.

## Ads safeguards

Различай:
1. финансовый факт рекламного расхода;
2. статистику кампаний/воронки.

Если source не доказывает точное `SKU -> spend`, нельзя показывать SKU-ad-spend как факт. Допускается оценка только с явной маркировкой и описанием метода.

## SKU and cost

Для SKU всегда проверяй mapping через существующий справочник. Не склеивай `nmId`, barcode, vendorCode и internal_sku как будто они один ключ.

Для себестоимости:
- используй effective history;
- для наборов раскрывай состав через `BUNDLES`;
- не применяй текущую себестоимость к прошлому периоду без доказанного правила.

## Inventory and geography

Регион/склад можно анализировать только на том уровне, который реально присутствует и стабилен в source. При изменениях API-идентификаторов сохраняй историческую сопоставимость через документированный warehouse key contract, а не выдуманный id.

## Перед ответом на бизнес-вопрос

Выведи внутренне цепочку:
`вопрос -> метрика -> definition -> source -> grain -> filters -> validation`.

Если любой элемент не доказан, пометь вывод как ограниченный или останови расчёт.
