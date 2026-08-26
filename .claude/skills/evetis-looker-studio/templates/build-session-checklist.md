# EVETIS Looker build session checklist

## Before browser actions

- [ ] Read latest relevant Build Spec.
- [ ] Confirm report/page target.
- [ ] Confirm source view and grain.
- [ ] Confirm metric aggregation and coverage counter.
- [ ] Confirm whether ratio-of-sums.
- [ ] Confirm freshness source.
- [ ] Run C-RATIO if not already accepted for the report.

## Build

- [ ] Establish grid/header before metrics.
- [ ] Build hero band first.
- [ ] Screenshot and review hero band.
- [ ] Add analytical explanation charts.
- [ ] Screenshot and review.
- [ ] Add supporting metrics.
- [ ] Add SKU investigation surface.
- [ ] Configure missing-data behavior.
- [ ] Add data-health/freshness states.

## Final QA

- [ ] Control-period values reconcile.
- [ ] No unknown -> zero conversion.
- [ ] Ratio-of-sums verified.
- [ ] Comparison suppressed/warned when previous coverage incomplete.
- [ ] Screen/system freshness both visible.
- [ ] Provisional visible.
- [ ] No account-level cost allocation to SKU.
- [ ] No visual anti-patterns.
- [ ] Screenshot passes 5-second hierarchy test.
- [ ] Return READY / NEEDS_FIX with evidence.
