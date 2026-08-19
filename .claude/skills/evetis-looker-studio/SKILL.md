---
name: evetis-looker-studio
description: Проектирует consumption layer и Looker Studio dashboard для EVETIS WB Analytics поверх валидированных BigQuery MART/views. Использовать при подготовке dashboard_layer, KPI, фильтров SKU/период/регион/кампания, графиков и проверке соответствия Looker источникам истины.
---

# EVETIS Looker Studio

## Цель

Looker Studio — presentation/decision layer, а не место, где заново изобретается бизнес-логика EVETIS.

## Перед созданием отчёта

Для каждого элемента dashboard зафиксируй:
- business question;
- metric definition;
- authoritative MART/view;
- grain;
- available dimensions;
- allowed filters;
- freshness SLA;
- caveats.

## Принцип слоя данных

Предпочитай BigQuery consumption-ready views/tables из `sql/mart/` и dashboard layer.

Не подключай RAW непосредственно к управленческому dashboard, если только это не отдельный технический diagnostic screen.

Критичные KPI рассчитывай upstream в SQL. Calculated fields Looker оставляй для лёгкого presentation logic, а не для конкурирующей версии P&L, DRR или маржи.

## Dashboard v1 structure

Рекомендуемый каркас:
1. Executive overview — продажи, заказы, возвраты, финансовый факт, расходы, прибыль, маржа.
2. SKU performance — выбор SKU/nmId и динамика ключевых метрик.
3. Advertising — spend, impressions/views, clicks, CTR, CPC/CPM, orders/conversion только в доказанном grain.
4. Profitability — components of P&L, contribution/margin, cost history effects.
5. Inventory — current stocks, stock cover/sales velocity, availability and replenishment signals.
6. Geography/warehouse — только на доступном стабильном географическом grain.
7. Data health — last successful ingest/MART build и freshness flags.

## Filters

Глобальные фильтры допустимы только если все charts имеют совместимый dimension contract. Типовые фильтры:
- date range;
- internal_sku/product;
- nm_id;
- campaign/advert where relevant;
- region/warehouse where supported.

Не делай глобальный filter, который silently исключает charts из-за отсутствующей dimension.

## Metric governance

Для каждой KPI card:
- название;
- формула upstream;
- currency/unit;
- sign convention;
- date basis;
- comparison basis;
- final/provisional status if relevant.

Особо маркируй estimate vs fact.

## Performance

- Используй partition-friendly date filters.
- Не создавай тяжёлые many-to-many blends в Looker.
- Избегай повторных вычислений одних и тех же KPI в нескольких charts.
- Если dashboard требует сложного JOIN/aggregation, перенеси его в BigQuery mart/view.

## QA before release

Сверь минимум 3 контрольных периода и 3 SKU между Looker и прямым BigQuery запросом.
Проверь:
- totals;
- filter behavior;
- partial dates;
- NULL labels;
- metric signs;
- data freshness;
- визуальное отсутствие misleading axes/scales.

Любое несовпадение Looker и MART сначала диагностируй, а не исправляй локальной формулой в отчёте.
