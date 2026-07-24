# EVETIS WB Analytics — облачные загрузчики (`/cloud`)

**PR-Mig0 — фундамент.** Бизнес-логики загрузки НЕТ (только `noop`). Реальный
пилот `wb-stocks` — в PR-Mig1. См. утверждённый дизайн:
`docs/MIGRATION_CLOUDRUN_DESIGN_2026-07-24_rev2.md` + `..._rev2-final-diff_...md`.

## Модель
Каждый загрузчик — **Cloud Run Job** (задача по расписанию, exit 0/1), НЕ сервис.
Один общий образ; загрузчик выбирается командой `node dist/cli.js <loader>`.
Оркестрация: `config → execution-guard (environment × loader × logical_period) →
loader → finalize`.

## Локально
```
npm ci
npm run typecheck
npm run lint
npm test
DRY_RUN=1 GCP_PROJECT_ID=x BQ_RAW_DATASET=wb_raw ENVIRONMENT=shadow node dist/cli.js noop
```

## Инварианты
- runtime НЕ создаёт таблицы (их создаёт Terraform);
- секреты — только Secret Manager (не env, не в коде);
- shadow физически не пишет в prod;
- prod не пересобирает образ — продвигается тот же digest.
