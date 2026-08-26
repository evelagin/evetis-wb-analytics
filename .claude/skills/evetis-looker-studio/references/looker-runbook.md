# Looker Studio Build Runbook · EVETIS

## 0. Preconditions

Before UI work:

- confirm report is the intended EVETIS Analytics report
- confirm BigQuery production views are accepted
- confirm source grain and date field
- confirm no pending semantic decision affects the requested component

## 1. C-RATIO probe

Source: `wb_mart.V_DASH_KPI_DAILY`
Date field: `day`

Create temporary report-level fields with aggregation `Auto`:

`_probe_avg_price = SUM(orders_revenue_rub) / SUM(orders_qty)`

`_probe_drr = SUM(ad_spend_attributed_rub) / SUM(sales_revenue_seller_base_rub)`

`_probe_ctr = SUM(clicks) / SUM(views)`

Date range: 2026-08-01 through 2026-08-07.

Expected as-of the Stage 4B acceptance anchor:

- average order price: 594.73
- DRR: 16.77%
- CTR: 2.4807%

Previous period 2026-07-25 through 2026-07-31:

- average order price: 1009.09
- DRR: 25.72%

If Looker instead behaves like average-of-row-ratios, STOP and diagnose field aggregation. Do not proceed to production ratio cards.

Delete `_probe_*` fields after the test.

## 2. Connect governed sources

Use native BigQuery connector.

- `V_DASH_KPI_DAILY` -> KPI cards and executive charts
- `V_DASH_SKU_DAILY` -> SKU table / drill-down only
- `V_DASH_FRESHNESS_BY_CONTRACT` -> contract freshness
- `V_DASH_FRESHNESS_HEADER` -> global system freshness
- `V_DATA_FRESHNESS` -> diagnostic page/detail only

Never blend KPI_DAILY with SKU_DAILY.

## 3. Create the shell before metrics

Set page canvas, background, margins, grid, header, section anchors, and reusable visual styles first.

Add product shell:

`EVETIS / ANALYTICS`

Page:

`Executive · Пульс магазина`

## 4. Controls

Add:

- Date Range control
- optional category/SKU control where it adds investigation value

Default range begins 2026-04-13 and ends at current date.

Enable previous-period comparison for KPI cards only when previous-period coverage is valid.

## 5. Freshness and honesty strip

Display both:

- contract freshness (`KPI_DAILY`)
- system freshness

Add coverage/provisional warnings using the approved additive counters from the dashboard contract.

Do not implement new coverage logic inside Looker.

## 6. Hero band

Build 4-5 hero KPIs first. Verify alignment and numbers before continuing.

Suggested order:

1. Выручка (выкупы)
2. Вклад pre-COGS
3. ДРР по выкупам
4. Заказы or Выкупы
5. К перечислению (if needed)

Each hero KPI gets prior-period context when trustworthy.

## 7. Explanation band

Preferred first two analytical modules:

- revenue/contribution trend over time
- economics decomposition / cost structure

Then advertising/trading efficiency.

## 8. Supporting KPIs

Use the Executive manifest. Supporting KPIs should be visually quieter than hero cards.

Do not allow the 26-metric contract to become a 26-card wall.

## 9. SKU table

Source: `V_DASH_SKU_DAILY`.

Drill hierarchy:

`category -> product_name_short -> day`

No account-level storage/deductions/acceptance columns in SKU table.

Permanent explanatory footnote: account-level costs are intentionally not allocated to SKU.

## 10. Missing data behavior

For each relevant visual/field:

- missing data -> `—` / no data
- time series -> break line across uncovered dates
- ratio denominator zero -> `—`

Inspect actual Looker rendering; do not assume defaults are safe.

## 11. Screenshot QA loop

Take a screenshot after:

- shell/header
- hero band
- analytical band
- supporting metrics
- SKU table
- final page

Review each against `visual-system.md` and `qa-checklist.md` before adding more.

## 12. Build completion report

Return:

- report/page URL
- sources
- calculated fields
- date/filter settings
- C-RATIO result
- screenshots
- acceptance values
- coverage/provisional/freshness behavior
- divergences from spec
- recommendation READY / NEEDS_FIX

