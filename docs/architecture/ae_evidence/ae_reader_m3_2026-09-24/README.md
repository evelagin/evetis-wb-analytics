# M3 — `sa-ae-reader` (AE_V1_RUNBOOK.md §2) — 2026-09-24

**Итог: `AE_READER_IDENTITY_FAILED_ROLLED_BACK`.** M3 применён по ACK и **откатан**, откатан только
M3. Причина: по эффективным правам SA не был read-only. M1/M2 целы.

## Предпроверки

- `origin/main` = `a0a138c0` — потомок `be2d78b0`. Анализ влияния новых коммитов (PR #166 Gate 10,
  PR #168 PROMO-3): `autonomy.tf`, `wif.tf`, `tools/autonomy/`, workflows не менялись. Новые ресурсы
  `sa-promo-econ-*` в план M3 не попадают. Новый набор `promo_economics` читает `wb_mart`/`ozon_mart`,
  оба входят в восемь датасетов.
- Живой WIF: `wif_check --live --phase wif-hardening` → 25/25, утечек 0.
- Достаточность чтения (статически): все 15 наборов-ворот, детектор здоровья (`wb_ops`, `ozon_raw`),
  `check_runtime_access` (`datasets.get`) и `verify_current_sql_live` (`tables.get`) укладываются в
  восемь датасетов.
- План M3: `terraform_m3_plan.txt` — `16 to add, 0 to change, 0 to destroy`, ровно задокументированный
  состав.
- Состояние до изменений: `pre_project_iam.json`, `pre_dataset_acl.json` (с etag). Откат подготовлен
  заранее: `rollback_m3.sh`.

## Применение (08:20–08:21 МСК)

`terraform_m3_apply.txt` — `16 added`. Изменилось ровно это:

- проект: `+ roles/bigquery.jobUser`, `+ roles/bigquery.resourceViewer`, `+ roles/logging.viewer`,
  ничего не удалено;
- датасеты `wb_raw`, `wb_mart`, `wb_ops`, `evetis_ref`, `evetis_ops`, `evetis_mart`, `ozon_raw`,
  `ozon_mart`: по одной записи `READER`, ничего не удалено;
- политика SA: 4 × `workloadIdentityUser` по `job_workflow_ref` (watch, gate, engineer, test);
- в политиках других SA `sa-ae-reader` не упомянут; публичных (`allUsers`/`allAuthenticatedUsers`)
  привязок в проекте и восьми датасетах нет.

Строгий `wif_check --live` (без `--phase`) при созданном SA → **25/25, код 0**
(`with_reader_wif_check_live_strict.json`): A6/A7/B1/B2 → только `sa-ae-reader`, C1 (ревьюер) → ничего,
D1–D6 (кандидаты) → ничего, A1–A5 без изменений.

## Почему откат

Роли раскрыты до permissions (`role_definitions.jsonl`, `effective_permissions.json`). API Policy
Analyzer и Troubleshooter в проекте выключены, включать их без разрешения не стал. Итог:

- 32 проверенных права мутации отсутствуют: DDL/DML (`tables.create/updateData/update/delete`),
  создание и замена вью (`tables.create/update`), `routines.*`, `datasets.update/setIamPolicy`,
  `resourcemanager.projects.setIamPolicy`, `iam.serviceAccounts.getAccessToken/actAs/signJwt`, Cloud Run,
  Scheduler, Secret Manager, запись в GCS;
- `bigquery.tables.createSnapshot/replicateData/export` без права записи в цель не работают: у SA
  нет записи ни в один датасет;
- **но `roles/bigquery.jobUser` на 2026-09-24 содержит `dataform.repositories.create`,
  `dataform.folders.create` и `geminidataanalytics.locations.chat`, а Dataform API в проекте
  включён.** SA может создавать в проекте ресурсы — репозитории и папки кода BigQuery Studio.
  Путь к изменению данных production не найден: у сервисного агента Dataform нет ролей и записей
  в ACL, `actAs` у SA нет. Однако это право создания вне утверждённого дизайна («ни одной роли
  записи»). По правилу ACK «неожиданные привилегии → откатить только M3».

Проверка `tools/autonomy/iam_check.py` на снимке M3 «как применён» (`m3_as_applied_iam_snapshot.json`)
выдаёт ровно эти три находки.

## Откат (08:25 МСК)

План `terraform_m3_destroy_plan.txt` — `16 to destroy`, чужих ресурсов 0. Применение
`terraform_m3_destroy_apply.txt` — `16 destroyed`. После отката:

- SA не существует; политика IAM проекта и ACL восьми датасетов **совпадают с исходными**
  (`after_rollback_*`);
- `iam_check --live` по всем 50 датасетам и всем SA — следов нет (`after_rollback_iam_check_live.json`);
- `wif_check --live --phase wif-hardening` → 25/25, утечек 0;
- Terraform plan M1/M2 — `No changes` (`terraform_post_m12_plan.txt`); M3 — `16 to add`
  (`terraform_post_m3_plan.txt`): конфигурация в `main`, ресурсов нет — ожидаемо.

## Попутное реальное доказательство M2

В том же окне соседние потоки работали через новые привязки M2:

- `infra.yml` apply из `main` (07:33/07:34 UTC, PROMO-3) — `sa-terraform-apply` авторизован по
  `workflow_ref …/infra.yml@refs/heads/main`;
- `deploy-shadow` (push в `main`, 08:15 UTC) и `deploy-prod` (dispatch из `main`, 08:17 UTC), оба
  success — `sa-deployer` авторизован по `…/deploy-shadow.yml@main` и `…/deploy-prod.yml@main`.

Легитимные deploy и infra не заблокированы.

## Исправленный дизайн (в Git, НЕ применён)

`infra/terraform/autonomy.tf`: вместо `roles/bigquery.jobUser` — пользовательская роль
`aeBigQueryJobRunner` с `bigquery.jobs.create` и `bigquery.config.get`. Остальное без изменений.
Read-only plan: `terraform_fixed_design_plan.txt` — `17 to add`. Проверка после применения —
`python -m tools.autonomy.iam_check --live`, код 0, затем `wif_check --live` без `--phase`.

## Аудит мутаций

- мои: только перечисленные выше IAM и ACL операции M3 и отката (журнал аудита 08:20–08:25 МСК);
  BigQuery-заданий, меняющих данные, от меня нет;
- чужие: развёртывание PROMO-3 (DDL под учётной записью владельца, 10:33 МСК) и его `infra.yml`
  apply; штатные загрузчики и сборки слоёв (`sa-loaders-prod`, `sa-exec-v2-layer`, `sa-sku-v2-layer`,
  `sa-unitka-cogs-pub`, Apps Script `INGEST_RUNS`); deploy после слияния Gate 10.
