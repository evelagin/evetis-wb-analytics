# EVETIS Looker Studio QA Checklist v1

## A. Semantic truth

- [ ] Every visual uses an approved `V_DASH_*` source or explicitly approved diagnostics source.
- [ ] KPI_DAILY and SKU_DAILY are not blended.
- [ ] Revenue labels distinguish seller-base / orders / buyer-paid / settled / net settlement where needed.
- [ ] Calendar buyout ratio is explicitly labeled non-cohort and may exceed 100%.
- [ ] Contribution is labeled pre-COGS, never profit/margin.
- [ ] Advertising spend is labeled attribution, not billing.
- [ ] Account-level costs are not allocated to SKU.

## B. Aggregation

- [ ] C-RATIO probe PASS.
- [ ] Every ratio uses `SUM(numerator)/SUM(denominator)`.
- [ ] No `AVG(row_ratio)`.
- [ ] No business metric uses `IFNULL(...,0)` / `COALESCE(...,0)`.
- [ ] Ratio denominator zero renders no-data, not infinity/zero.

## C. Coverage and freshness

- [ ] Missing coverage displays `—` / no data, not zero.
- [ ] Time-series gaps remain gaps.
- [ ] Coverage warning is visible when selected range crosses missing days.
- [ ] Contribution provisional days are visible.
- [ ] Contract freshness and system freshness are simultaneously visible.
- [ ] System stale reason is displayed when stale.

## D. Visual hierarchy

- [ ] 4-5 hero KPIs dominate, not all metrics equally.
- [ ] Main insight is clear within five seconds.
- [ ] Hero values have comparison context when valid.
- [ ] Supporting metrics are visually quieter.
- [ ] Detail table is below analysis, not above hero context.
- [ ] No giant logo or decorative header consumes analytical space.

## E. Visual quality

- [ ] 8px grid alignment is visually consistent.
- [ ] Equal cards have equal dimensions and spacing.
- [ ] One primary accent dominates the screen.
- [ ] Semantic colors encode meaning only.
- [ ] No rainbow, 3D, gauges, radar, decorative gradients, or excessive shadows.
- [ ] Gridlines/borders are restrained.
- [ ] Numbers use consistent localization and precision.
- [ ] Raw BigQuery field names are not visible to the owner.

## F. Charts and tables

- [ ] Time -> line; category ranking -> sorted horizontal bar where appropriate.
- [ ] Bar/column axes do not truncate baseline misleadingly.
- [ ] Legends removed when direct labels are clearer.
- [ ] SKU table sorted by seller-base sales revenue descending by default.
- [ ] SKU table omits account-level costs and explains why.
- [ ] Conditional formatting is sparse and actionable.

## G. Accessibility

- [ ] Text contrast is readable.
- [ ] Important states are not communicated by color alone.
- [ ] Font sizes remain legible at normal viewing scale.
- [ ] Controls are obvious and usable.
- [ ] If Looker supports alt text/accessibility metadata for the visual, use it for key visuals.

## H. Acceptance tests for current Executive baseline

Use current accepted Stage 4B Build Spec values as the acceptance anchor for the stated date.

Core tests:

- [ ] ratio-of-sums probe passes
- [ ] default-range totals reconcile to BigQuery control query
- [ ] DRR and contribution % match control
- [ ] calendar buyout ratio may be >100% and is not rendered as an error
- [ ] coverage counters match contract
- [ ] provisional counter matches contract
- [ ] both freshness states match sources
- [ ] selecting an early unsupported range does not render ad/sales/order unknowns as zeros
- [ ] time-series gap visible
- [ ] SKU count and drill-down behavior match source

## I. Final review

- [ ] Screenshot at 100% scale looks like one coherent product.
- [ ] Screenshot at reduced scale still reveals hierarchy.
- [ ] A naive viewer can say what changed and whether data is trustworthy after 60 seconds.
- [ ] Every component earns its space.
- [ ] Any known limitation is visible or documented.

