# EVETIS Analytics Visual System v1

## Product character

EVETIS Analytics should feel like a premium internal SaaS product: restrained, confident, analytical, and modern.

Design target references conceptually: contemporary fintech/SaaS admin surfaces, high data-ink ratio, strong information hierarchy, minimal ornament.

## Canvas and grid

- 8 px base grid.
- 24-32 px page edge.
- 16 px minimum component gutter.
- 32-48 px between semantic sections.
- Same-row cards share exact height and baseline.
- Avoid nested card-on-card framing.

## Header

Use compact product identity, not a giant report title.

Left:
- `EVETIS / ANALYTICS`
- page: `Executive · Пульс магазина`

Right / control zone:
- date range
- category/SKU filter where useful
- screen freshness
- system freshness

Keep header analytical; no oversized brand artwork.

## KPI anatomy

Hero KPI:

1. label
2. large number
3. valid previous-period comparison
4. optional concise secondary line
5. coverage/provisional cue only if relevant

Visual emphasis comes from number size and whitespace, not saturation.

Supporting KPI may be smaller and grouped by business theme.

### Suggested Hero set for Executive

- Выручка (выкупы)
- Вклад pre-COGS
- ДРР по выкупам
- Заказы / Выкупы (choose based on story)
- К перечислению (optional fifth)

The full 26-KPI contract belongs below hero level or in contextual bands, not as 26 equal boxes.

## Sections

Recommended Executive sequence:

1. Header + data health
2. Hero pulse
3. Business trend
4. Economics decomposition
5. Advertising efficiency
6. Trading efficiency
7. SKU leaderboard / drill-down
8. footnotes / limitations

## Chart styling

### Line charts

- primary series: one accent
- comparison: neutral gray, thinner/lower contrast
- subtle or removed gridlines
- direct labels where practical
- missing coverage = visible gap
- no area fill unless it encodes meaningful magnitude and remains legible

### Bars

- sorted descending for categorical rankings
- baseline zero
- one accent for highlighted/current; neutral for context
- direct value labels where space permits

### Stacked economics

Only stack additive components with unambiguous signs/meaning.
Do not force unrelated costs into a decorative composition.

### Tables

"Subtract, don't add":

- avoid heavy full-cell grid
- clear header hierarchy
- restrained row separators
- right-align numeric values
- use conditional formatting only on actionable variance/efficiency columns
- default sort by seller-base sales revenue descending

## Semantic state system

- OK/current = green + explicit `OK`
- stale/provisional = amber + explicit label
- error/negative business state = red only when semantically adverse
- unknown/unavailable = gray + `—` / `нет данных`

Never use only color to communicate the state.

## Freshness component

Always preserve the two-state concept:

### Данные экрана
Prominent. Answers whether the actual Executive data contract is current.

### Система
Secondary but always visible. Answers whether any layer in the broader analytics system is stale/error.

Do not merge them into one "worst status" badge.

## Typography

Prefer a clean system sans-serif available reliably in Looker Studio.
Use limited type sizes and weights.
Use tabular-looking numbers where font support allows.

Hierarchy:
- product/page title: restrained
- hero metric: strongest
- section heading: medium
- visual title: medium
- caption/footnote: small but legible

## Density

A premium dashboard is not an empty dashboard, but density must be structured.

Use the 5-second test:
After five seconds, a viewer should identify:

1. business direction
2. contribution state
3. advertising efficiency
4. whether data can be trusted

If not, reduce equal-weight elements and increase hierarchy.

## Forbidden visual patterns

- gauges/speedometers
- 3D
- radar/spider
- rainbow categorical abuse
- pie charts for complex cost structures
- gradients as decoration
- glassmorphism purely for trendiness
- large shadows on every tile
- giant icons inside KPI cards
- bright colored tile backgrounds for all KPIs
- raw BigQuery field names on screen
- more than two decimal places where decision-making does not need them

