# EVETIS · Looker Studio Builder Skill v1

A project-specific Claude skill for building and reviewing the EVETIS Analytics Looker Studio layer.

## Recommended project placement

Place the folder as:

`.claude/skills/evetis-looker-studio-builder/`

or use the equivalent skills directory supported by the current Claude project setup.

The required entry point is `SKILL.md`. Keep the `references/` directory beside it so Claude can consult detailed contracts, visual rules, the Looker runbook, and QA checklist.

## What this package does

- governs Looker Studio visual design
- enforces EVETIS BigQuery contract semantics
- prevents NULL -> 0 presentation errors
- standardizes ratio-of-sums
- provides the current Executive KPI manifest
- standardizes visual hierarchy and premium-minimal styling
- defines screenshot-driven QA
- provides browser automation guardrails

## What it does not do

- replace BigQuery business logic
- create cohort buyout rate before PR3
- invent COGS/profit
- allocate account-level costs to SKU
- hide missing coverage
- provide a stable official Looker Studio editing API

## Suggested first use

Ask Claude:

`Use the evetis-looker-studio-builder skill and the latest Stage 4B Build Spec. First audit the current Executive page screenshot against the skill. Do not change SQL. Then propose the exact visual corrections in priority order and implement them in Looker Studio only after confirming the C-RATIO probe.`
