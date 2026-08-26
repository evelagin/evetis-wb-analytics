# Source inspirations and adaptation notes

This skill is an original EVETIS-specific synthesis. It does not copy any single third-party skill wholesale.

## 1. Looker Studio + BigQuery integration skill

Source family:
- `aiskillstore/marketplace` — `skills/supercent-io/looker-studio-bigquery/SKILL.md`
- mirrors/forks exist in other skill registries

Ideas adapted:
- native BigQuery connector
- thin BI layer over prepared BigQuery data
- date controls and drill-down
- F-pattern / KPI -> trend -> detail information flow
- performance awareness and precomputation

EVETIS override:
- use governed `V_DASH_*` contracts, not ad-hoc custom SQL in Looker
- stricter coverage, NULL, ratio-of-sums, and freshness semantics

## 2. AIO SaaS Dashboard Design Advisor

Source:
- `aiocean/claude-plugins`
- `plugins/aio-design-system/skills/aio-dashboard-design/SKILL.md`

Ideas adapted:
- audience-first and one Big Idea per screen
- Martini Glass explanatory-to-exploratory flow
- data-ink discipline
- semantic color and single-accent philosophy
- KPI context and comparison
- anti-pattern rejection (gauges, 3D, rainbow, misleading encodings)
- accessibility and truthfulness as release criteria
- screenshot/review loop

EVETIS override:
- implementation constrained by Looker Studio capabilities
- data trust metadata (coverage/provisional/freshness) elevated to first-class UI

## 3. Dashboard Designer skill

Source:
- `NickCrew/Claude-Cortex`
- `skills/dashboard-designer/SKILL.md`

Ideas adapted:
- executive dashboard = few headline KPIs
- visual hierarchy top-left
- whitespace and limited chart density
- filters above content
- detail gradient: summary -> context -> investigation
- comparison context required for KPIs

## 4. Power BI Claude Design System

Source:
- `jonathan-pap/PowerBI-Claude-Design`
- `design-system/SKILL.md`

Ideas adapted in platform-neutral form:
- anti-"BI slop" discipline
- 8px grid and arithmetic alignment
- detail gradient
- KPI anatomy
- semantic colors
- restrained tables (subtract visual noise)
- report-level design system rather than per-widget improvisation

Not carried over:
- Power BI-specific theme JSON, DAX, Fluent/Segoe property mappings

## 5. Looker Studio browser automation reference

Source:
- `talandrius/looker-studio-mcp`

Observed capabilities:
- Playwright persistent browser profile
- report creation
- chart insertion
- screenshots
- data-source interaction

EVETIS position:
- useful as automation reference, not as source of business semantics
- browser selectors are fragile and can change when Google updates Looker Studio
- do not automate credential storage/login secrets

## Priority of authority

Third-party patterns are advisory only.

If any external design suggestion conflicts with an accepted EVETIS data contract, the EVETIS contract wins.
