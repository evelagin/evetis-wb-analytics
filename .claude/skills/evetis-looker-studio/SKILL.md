---
name: evetis-looker-studio-builder
description: Build, review, and maintain EVETIS Looker Studio dashboards on top of the governed BigQuery V_DASH contract layer. Use for Executive, Sales, Advertising, SKU, Finance, Stock, and Data Health screens; visual hierarchy; KPI cards; chart selection; freshness/coverage UX; ratio-of-sums; browser-driven Looker Studio implementation; screenshot QA; and dashboard acceptance. Never use this skill to invent business semantics or bypass BigQuery contracts.
version: 1.0.0
project: EVETIS Analytics
owner_scope: internal BI / marketplace analytics
tags: [looker-studio, bigquery, dashboard, bi, evetis, marketplace, visual-design, qa]
---

# EVETIS · Looker Studio Builder

## Mission

Build EVETIS Analytics as a coherent internal data product, not a collection of Google BI widgets.

The dashboard layer is a **thin, governed presentation layer** over BigQuery. It must be visually premium, operationally useful, mathematically correct, and explicit about data quality.

Target feeling: **quiet, premium, dense with meaning, low in visual noise, obvious in hierarchy**. The user should feel they are opening an EVETIS analytics operating system, not a generic Looker Studio report.

## Non-negotiable architecture

Use this order of authority:

1. **Accepted EVETIS business/data contracts in the repository**.
2. **Production `wb_mart.V_DASH_*` views**.
3. **This skill's visual and interaction rules**.
4. Looker Studio capabilities and browser mechanics.

If these conflict, the higher level wins.

Never move unresolved business logic into Looker merely because it is easier to click there.

### Current governed sources

- `wb_mart.V_DASH_COVERAGE_DAILY`
- `wb_mart.V_DASH_FRESHNESS_HEADER`
- `wb_mart.V_DASH_KPI_DAILY`
- `wb_mart.V_DASH_SKU_DAILY`
- `wb_mart.V_DASH_FRESHNESS_BY_CONTRACT`
- `wb_mart.V_DATA_FRESHNESS` only for system/data-health detail where specified

Do not blend `V_DASH_KPI_DAILY` and `V_DASH_SKU_DAILY`: their grains differ.

## Core operating doctrine

### 1. Truth before beauty

Never make an incomplete metric look complete.

- Unknown coverage -> `NULL` / em dash / "нет данных", never artificial zero.
- A derived KPI inherits unknownness from required components.
- Never add `IFNULL(x, 0)`, `COALESCE(x, 0)`, zero-fill, or equivalent presentation logic for business metrics.
- Missing dates in a time series remain visually broken; do not connect through the gap or drop to zero.
- `PROVISIONAL` finance may be displayed only with visible provisional state.
- Contract freshness and system freshness are different truths and must not be collapsed into one status.

### 2. Ratios are ratio-of-sums

Approved Looker ratio pattern:

`SUM(numerator) / SUM(denominator)`

Never use:

- `AVG(row_ratio)`
- average of daily DRR/CTR/ROAS/etc.
- precomputed daily ratio columns as a substitute for a period ratio

Before the first production dashboard build, run the C-RATIO probe in `references/looker-runbook.md`.

If the probe fails: STOP. Diagnose aggregation. Do not redesign BigQuery without an explicit architecture decision.

### 3. Executive hierarchy, not KPI wallpaper

The data contract may expose many KPIs. The screen must not give every KPI equal visual weight.

Use three levels:

- **Level 1 / Hero:** 4-5 metrics that answer "what is happening to the business?"
- **Level 2 / Explanation:** trends and decomposition that answer "why?"
- **Level 3 / Investigation:** SKU table, drill-down, reconciliation, data health

For Executive, default hero priority:

1. `sales_revenue_seller_base_rub` — Выручка (выкупы)
2. `contribution_pre_cogs_rub` — Вклад pre-COGS
3. DRR by buyouts — ratio-of-sums
4. `orders_qty` or `buyouts_qty` depending the visual story
5. `net_settlement_rub` when space permits

Do not present all 26 metrics as 26 equal cards.

### 4. Every KPI needs context

A primary KPI card should normally contain:

- clear Russian business label
- prominent value
- previous-period comparison where valid
- direction/trend cue where business meaning is unambiguous
- optional sparkline if Looker implementation remains clear
- coverage/provisional state when relevant

A naked number is not a finished KPI.

If previous-period coverage is incomplete, suppress semantic delta and show a neutral warning instead of a misleading percentage.

### 5. Visual hierarchy through typography and spacing

Use an 8 px spatial grid.

Recommended desktop canvas: 1440-1600 px wide or equivalent Looker custom canvas that keeps one-screen executive comprehension.

Tokens:

- page edge: 24-32 px
- component gutter: 16 px minimum
- section gap: 32-48 px
- card internal padding: 16-24 px
- hero metric values: visually dominant, approximately 32-40 px where Looker permits
- supporting labels: 12-14 px
- section labels: 14-16 px medium/semibold
- page/product title: approximately 24-30 px, restrained

Alignment must be arithmetic, not "by eye". Equal peers use equal width, height, baseline, and spacing.

### 6. Premium-minimal color system

EVETIS Analytics uses **one primary accent per screen** and semantic colors only for meaning.

Default roles:

- background: near-white / warm-neutral white
- primary text: near-black
- secondary text: muted neutral gray
- primary accent: one EVETIS brand-compatible accent selected and then locked for the report
- positive: semantic green only when "higher/better" is actually true
- negative: semantic red only for genuine adverse state
- warning/provisional/stale: amber
- unavailable/unknown: neutral gray

Never use color to decorate empty space.

Never use a different bright color for every metric.

Never rely on red/green alone; pair with text, arrow, icon, or status word.

If exact EVETIS brand palette is not available in repository/project assets, do not invent one permanently. Use restrained neutral styling and request/derive approved tokens later.

### 7. Chart grammar

Preferred:

- time trend -> line chart
- categorical comparison -> sorted horizontal bar
- composition over time -> stacked column/area only when components are additive and legible
- exact lookup / multi-metric detail -> table
- cohort -> heatmap (future PR3 surface)

Avoid or prohibit:

- 3D charts
- gauge / speedometer / dial
- radar/spider
- rainbow sequential palettes
- pie charts with many slices
- dual-axis unless explicitly justified and documented
- decorative bubbles for precise comparisons

Axes must not visually exaggerate magnitude. Bar/column baselines start at zero unless an explicit approved exception exists.

### 8. Martini-glass screen flow

The Executive page is mixed explanatory + exploratory.

Top: author-driven summary and current state.
Middle: reason/explanation charts.
Bottom: reader-driven exploration and SKU drill-down.

Do not make the user configure five filters before seeing the business state.

### 9. Data Health is a product feature

EVETIS explicitly surfaces trust metadata.

Always show separately:

- **Данные экрана** — contract-specific freshness from `V_DASH_FRESHNESS_BY_CONTRACT`
- **Система** — global freshness from `V_DASH_FRESHNESS_HEADER`

A green screen contract beside a stale system state is valid and informative.

Coverage, provisional finance, and data gaps are not implementation details to hide; they are part of the product experience.

### 10. Thin Looker layer

Allowed in Looker:

- formatting
- filtering
- approved `SUM(a)/SUM(b)` ratio fields
- date-range control
- previous-period comparison
- drill-down configuration
- chart-specific presentation settings

Not allowed without new data-contract approval:

- redefining revenue
- allocating account-level costs to SKU
- reconstructing missing history
- transforming unknown to zero
- creating profit/margin before COGS contract exists
- calling calendar buyout ratio a cohort buyout rate
- silently substituting billing ad spend for attributed ad spend

## EVETIS semantic guardrails

### Revenue

Keep distinct:

- orders revenue
- seller-base sales revenue
- buyer-paid sales revenue
- settled revenue
- net settlement

Never label two different bases simply "Выручка" without qualifier in the same context.

For Executive primary revenue use the approved seller-base sales revenue field.

### Buyout

`buyout_ratio_calendar_ops = SUM(buyouts_qty)/SUM(orders_qty)` is an operational calendar ratio, not a cohort buyout rate.

It may exceed 100%. That is not an error.

UI label must make the limitation explicit. True cohort buyout rate belongs to PR3.

### Advertising

Current Executive ad spend is WB attribution, not billing.

Include a concise note such as:

`Расход по атрибуции WB, не по биллингу.`

Do not use Stage 3B.1 billing as if it were the same semantic source.

### Economics

`contribution_pre_cogs` is contribution before COGS.

Never rename it "profit", "margin", or "net profit".

Account-level costs such as storage/deductions must not be invented at SKU level.

## Build workflow

When asked to build or modify a Looker Studio surface, follow this sequence.

### Phase A — Read contract

Read the latest relevant repository docs first. At minimum for Executive:

- Stage 4B Executive Build Spec
- Stage 4A PR1/PR2 contract and rollout docs when the change touches semantics

Confirm:

- source view
- grain
- metric field
- aggregation
- coverage dependency
- freshness dependency
- whether ratio or additive

Do not start browser work while any of these are ambiguous.

### Phase B — Design intent

Write one sentence for the screen's Big Idea.

For current Executive baseline:

`Показать владельцу, как магазин продаёт и зарабатывает до COGS, почему результат меняется и насколько данным можно доверять.`

Then choose hero metrics and visual hierarchy. Do not merely mirror source columns.

### Phase C — C-RATIO and source QA

Run C-RATIO probe before relying on Looker calculated ratio fields.

Confirm exact source bindings and date dimension.

### Phase D — Build shell first

Before placing dozens of data visuals, establish:

1. canvas/background
2. product header (`EVETIS / ANALYTICS`)
3. navigation placeholders
4. date/filter zone
5. grid and section geometry
6. freshness/data-health strip
7. reusable KPI card style

Only after visual primitives are consistent should the screen be populated.

### Phase E — Populate in order of importance

1. hero KPIs
2. hero trend / economics explanation
3. supporting KPIs
4. advertising/economics analysis
5. SKU investigation table
6. data-quality warnings and footnotes

### Phase F — Screenshot review loop

After each logical band:

1. take screenshot
2. review at 100% and reduced zoom
3. verify alignment and hierarchy
4. remove visual noise
5. compare with this skill's QA checklist
6. fix before adding the next band

Do not wait until 30 visuals exist before judging design quality.

### Phase G — Acceptance

Run functional and visual QA from `references/qa-checklist.md`.

Return a build report with:

- report/page URL
- source bindings
- calculated fields created
- filters created
- screenshots
- control-period values
- coverage/provisional/freshness behavior
- known limitations
- `READY` / `NEEDS_FIX`

## Browser automation policy

Claude in Chrome / browser automation may execute the Looker UI build, but browser mechanics are subordinate to the contract.

Preferred mechanics:

- reuse existing authenticated browser session
- use stable visible labels/ARIA selectors when possible
- build one component, verify, then duplicate only when source/style mapping is controlled
- screenshot frequently
- stop if the UI state is ambiguous or selector action could modify the wrong source/report

Do not automate Google password entry or store credentials inside project code/skills.

Playwright automation can be introduced later as a helper, but it is not the source of truth and must not encode business semantics.

## Anti-slop rules

A dashboard is rejected if it exhibits any of the following:

- 20+ equal KPI tiles above the fold
- generic Google default styling left largely untouched
- excessive borders/shadows
- rainbow charts
- unnecessary gradients or glass effects
- giant logo taking analytical space
- repeated legends where direct labels would work
- labels that expose raw warehouse field names
- excessive decimal precision
- red/green coloring without semantic direction
- missing comparison context on hero metrics
- missing freshness / coverage state
- layout misalignment visible at normal zoom
- technical diagnostics dominating the executive surface
- visually dense table with heavy gridlines and no hierarchy

## Number formatting

Executive display rounds aggressively while retaining exact data in detail/tooltips where supported.

Examples:

- `1 317 656 ₽` -> `1,32 млн ₽` or `1.318M ₽` only if the report language standard explicitly uses Latin abbreviations; prefer Russian localized format for owner-facing UI
- `0.1531` -> `15,31 %`
- counts -> integer with thin-space grouping
- CPC / average price -> 0-2 decimals depending scale

Use one convention consistently across the report.

## Screen naming

Product shell:

`EVETIS / ANALYTICS`

Page baseline:

`Executive · Пульс магазина`

Future navigation placeholders may include:

`Executive · Sales · Advertising · Products · Finance · Stock · Data Health`

Do not imply a future page is complete if it is not; use disabled/secondary presentation until built.

## Reference files

- `references/executive-contract.yaml` — current Executive component manifest
- `references/visual-system.md` — exact visual language and component anatomy
- `references/looker-runbook.md` — Looker implementation sequence and C-RATIO probe
- `references/qa-checklist.md` — functional, semantic, and visual acceptance
- `references/source-notes.md` — source inspirations and adaptation notes

