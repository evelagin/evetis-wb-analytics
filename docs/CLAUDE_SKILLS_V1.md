# EVETIS Analytics Skills v1

## Purpose

Project-scoped Claude Code skills for finishing EVETIS WB Analytics and preparing the validated BigQuery consumption layer for Looker Studio.

Skills live in `.claude/skills/<name>/SKILL.md`, which is the project discovery path used by Claude Code. The legacy root `skills/` directory is intentionally left unchanged in this PR for rollback/provenance; it is not treated as the active project-skill directory.

## Active v1 set

| Skill | Use for | Origin of methodology |
|---|---|---|
| `evetis-analytics-engineer` | FACT/MART design, grain, lineage, semantic contracts | adapted from analytics-engineering patterns; project-specific |
| `evetis-bigquery-sql` | BigQuery SQL, partitions, clustering, MERGE, ASSERT, join safety | project-specific BigQuery conventions |
| `evetis-data-profile` | profiling new/changed tables before modeling | adapted from Anthropic `explore-data` workflow |
| `evetis-data-validation` | QA before merge/deploy/dashboard | adapted from Anthropic `validate-data` workflow |
| `evetis-wb-domain` | WB semantics, source-of-truth, P&L, ads, SKU/cost/bundles | upgraded from existing EVETIS domain skills |
| `evetis-pipeline-engineer` | loaders, WB API, Cloud Run/Apps Script, manifests, retries, freshness | project-specific production ingestion patterns |
| `evetis-looker-studio` | BI consumption layer and Looker Studio design/QA | project-specific, derived from BI best practices |

## Why these seven

The repository is no longer an early Google Sheets prototype. Production work already includes BigQuery FACT/MART SQL, validation SQL, Cloud/TypeScript components, ingest manifests/freshness and scheduled MART builds. Therefore v1 focuses on completing and protecting the actual current architecture rather than installing broad generic prompt libraries.

## Routing examples

- "Добавь новую MART по SKU" -> `evetis-analytics-engineer` + `evetis-bigquery-sql` + `evetis-data-validation`.
- "WB поменял поле склада" -> `evetis-pipeline-engineer` + `evetis-data-profile` + `evetis-analytics-engineer`.
- "Почему прибыль не сходится" -> `evetis-wb-domain` + `evetis-data-validation`.
- "Подготовь таблицу для Looker" -> `evetis-analytics-engineer` + `evetis-bigquery-sql` + `evetis-looker-studio` + `evetis-data-validation`.
- "Подключаем новый endpoint WB" -> `evetis-pipeline-engineer` + `evetis-data-profile` + `evetis-wb-domain`.

Claude Code may auto-select skills from descriptions; they can also be invoked explicitly by `/skill-name` during testing.

## Integration rule

`CLAUDE.md` remains project governance and is loaded every session. Skills contain task-specific procedures and load on demand. Do not copy the full skill bodies into `CLAUDE.md`.

## Legacy root `skills/`

Current root skills (`apps-script-reviewer`, `google-sheets-architect`, `migrating-to-gcp`, `unit-economics-auditor`, `wb-api-analyst`) are not deleted in v1. Their useful rules have been incorporated where relevant, while obsolete or overly narrow assumptions are not promoted to the active layer.

After v1 is accepted and tested, a separate cleanup PR may archive/remove duplicates. Do not combine cleanup with activation.

## Acceptance test

From repository root in Claude Code:

1. Start/restart Claude Code after the new top-level `.claude/skills` directory exists.
2. Ask a MART-design question and verify `evetis-analytics-engineer` is selected, or invoke `/evetis-analytics-engineer` explicitly.
3. Ask to profile a table and verify `evetis-data-profile`.
4. Ask to validate a SQL/MART change and verify verdict `READY`, `READY_WITH_CAVEATS`, or `BLOCKED` from `evetis-data-validation`.
5. Ask about WB finance/profit semantics and verify the answer distinguishes order/sale/realization/payout/profit and respects canonical finance.
6. Ask to prepare a Looker source and verify RAW is not proposed as the management dashboard source.

## External references reviewed

- Anthropic Claude Code Skills documentation: project skills belong under `.claude/skills/<name>/SKILL.md` and load on demand.
- Anthropic knowledge-work data skills: `explore-data`, `validate-data`, `analyze`.
- borghei/Claude-Skills: `analytics-engineer`, `data-analyst` as methodology references.

The EVETIS files are original adaptations for this repository, not verbatim copies of third-party skills.
