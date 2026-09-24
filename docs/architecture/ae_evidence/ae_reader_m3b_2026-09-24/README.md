# M3 (повторно, исправленный дизайн) — `sa-ae-reader` — 2026-09-24

**Итог: `AE_READER_IDENTITY_VERIFIED`.** Первое применение откатано ([../ae_reader_m3_2026-09-24/](../ae_reader_m3_2026-09-24/README.md)).
M4–M6, Anthropic, `autonomy-state`, Engineer, Reviewer, Watcher, ввод в эксплуатацию и UBR-011 не выполнялись.

## Git

- PR #169 из `560f4d54`: только `autonomy.tf` (пользовательская роль вместо `jobUser`), `iam_check.py`,
  тесты, документы и доказательства. CI — 9/9 success. `main` на момент слияния не менялся (`a0a138c0`).
- Слит: `main` = `e5b7bbcafe6d8fbaa717cdb2490dfc5e203057ba`. В `main` есть `aeBigQueryJobRunner`,
  `"roles/bigquery.jobUser"` не назначается.

## План и применение

- `terraform_m3b_plan.txt` — `17 to add, 0 to change, 0 to destroy`, посторонних ресурсов 0. Назначения:
  4 × `iam.workloadIdentityUser`, 8 × `bigquery.dataViewer`, `bigquery.resourceViewer`, `logging.viewer`,
  пользовательская роль (`bigquery.jobs.create`, `bigquery.config.get`).
- Применён сохранённый план: `terraform_m3b_apply.txt` — `17 added`, 08:49 МСК.
- Состояние до изменений для отката: `pre_project_iam.json`, `pre_dataset_acl.json`. Откат —
  targeted destroy тех же 6 адресов (см. `../ae_reader_m3_2026-09-24/rollback_m3.sh`, плюс
  `google_project_iam_custom_role.ae_job_runner` и `google_project_iam_member.ae_reader_job_runner`).

## Эффективные права

`python -m tools.autonomy.iam_check --live` → **PASS, код 0** (`post_iam_check_live.json`, снимок —
`post_iam_snapshot.json`): находок нет, недостающего чтения нет.

- роли проекта: `aeBigQueryJobRunner` (живьём ровно `bigquery.config.get`, `bigquery.jobs.create`),
  `bigquery.resourceViewer`, `logging.viewer`; на 8 датасетах — только `READER`; в политиках других SA
  не упомянут;
- 66 эффективных прав (`effective_permissions.json`). Нет ни одного из запрещённых ACK: DDL/DML,
  создание/изменение/удаление таблиц, вью и routines, Dataform, Gemini, IAM, `actAs`, выпуск токенов,
  Cloud Run, Scheduler, Secret Manager, GCS, Cloud Build, Artifact Registry;
- похожие на запись: `bigquery.jobs.create` — разрешённое исключение. Ещё
  `tables.createSnapshot`, `tables.export`, `models.export`, `tables.replicateData` из `dataViewer`
  без права записи в цель не работают: у SA нет `tables.create` ни в одном датасете и нет записи в GCS;
- diff: проект — ровно +3 привязки, удалений 0; 8 датасетов — ровно +1 `READER` каждый, удалений 0.

## WIF

`python -m tools.autonomy.wif_check --live` (строгий, без `--phase`) → **25/25, код 0, утечек 0**
(`post_wif_check_live_strict.json`):

| Job | Результат |
|---|---|
| Watcher (A6), Gate (A7), Engineer (B1), Test (B2) | только `sa-ae-reader` |
| Reviewer (C1) | ничего |
| ветки `ae/*` (D1–D6) | ничего |
| двойник файла (E1), переиспользуемый вызов (E5) | ничего |
| deploy-prod/deploy-shadow@main, infra@main, scheduler-control@main, infra plan@feature | свои SA (M1/M2 целы) |

## Реальный тест чтения: `NOT_EXECUTABLE_AT_M3`

Токен `sa-ae-reader` выдаётся только job'ам AE из `main`. `autonomy-watch` пропускается без переменной
`AE_READER_SA` (M4), `autonomy-run` — без `AE_ENABLED`. Выдать владельцу право действовать от имени
SA — это изменение IAM вне M3. Права ради теста не расширялись. Первое реальное чтение — `M6`
(ручной `autonomy-watch`, `allow_dispatch=false`).

## Сходимость

- targeted plan M3 — `No changes` (`terraform_post_m3_plan.txt`);
- targeted plan M1/M2 — `No changes` (`terraform_post_m12_plan.txt`).

## Аудит (окно с 08:40 UTC)

- административные события — только мои, ровно 17 операций M3 (08:49 МСК): `CreateServiceAccount`,
  `CreateRole`, 2 × `SetIamPolicy` проекта, 8 × `PatchDataset`, 4 × `SetIAMPolicy` на SA. Посторонних
  изменений IAM нет;
- заданий BigQuery от `sa-ae-reader` — 0; изменения данных в окне — только штатные `sa-loaders-prod` и
  `sa-unitka-cogs-pub`; WB/Ozon не затронуты;
- переменных `AE_*` нет, ветки `autonomy-state` нет; прогоны — только CI PR #169 и `sql-current` на push.
