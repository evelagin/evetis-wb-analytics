# PR-Mig0 — разовая настройка GCP (чек-лист владельца)

Всё автоматизируемое делает Terraform. Ниже — только ручные шаги владельца-админа.
Токены и ключи в git НЕ попадают. Аутентификация CI — WIF, без JSON-ключей.

## 0. Предпосылки
- Проект `project-fa311fc0-4d87-4781-986` (номер `37074083763`), BQ `wb_raw` (EU).
- Локально `gcloud` + `terraform`, роль **Owner** (только для bootstrap и первого apply).

## 1. Bootstrap remote state (создать GCS-bucket под tfstate)
Remote state ОБЯЗАТЕЛЕН: иначе локальный apply и CI разойдутся по состоянию.
```
cd infra/bootstrap
terraform init
terraform apply -var project_id=project-fa311fc0-4d87-4781-986 -var state_bucket=<UNIQUE_BUCKET_NAME>
```
`<UNIQUE_BUCKET_NAME>` — глобально уникальное имя (напр. `evetis-wb-tfstate-<rnd>`).

## 2. Первый apply основного модуля (под Owner, backend = bucket из шага 1)
```
cd ../terraform
cp terraform.tfvars.example terraform.tfvars   # проверить github_repo = evelagin/evetis-wb-analytics
terraform init -backend-config="bucket=<UNIQUE_BUCKET_NAME>" -backend-config="prefix=evetis/wb-cloud"
terraform apply
```
Создаёт: APIs, Artifact Registry, WIF pool/provider, 7 сервис-аккаунтов
(deployer, terraform-plan, terraform-apply, loaders-shadow/prod, scheduler-shadow/prod),
IAM (least-privilege; apply-SA с actAs на runtime/scheduler SAs), пустые секреты,
`LOADER_RUNS` + `*__CR`, Cloud Run Jobs (bootstrap-образ, args=noop), Scheduler (PAUSED).
State уходит в GCS.

## 3. Значения токенов WB → Secret Manager (руками, не в git)
```
printf '%s' "$WB_TOKEN_ANALYTICS" | gcloud secrets versions add WB_TOKEN_ANALYTICS --data-file=-
printf '%s' "$WB_TOKEN_ADS"       | gcloud secrets versions add WB_TOKEN_ADS       --data-file=-
printf '%s' "$WB_TOKEN_FINANCE"   | gcloud secrets versions add WB_TOKEN_FINANCE   --data-file=-
```

## 4. GitHub — Variables (Settings → Secrets and variables → Actions → Variables)
Это не тайны, поэтому Variables (не Secrets):
- `GCP_PROJECT_ID` = project-fa311fc0-4d87-4781-986
- `GCP_PROJECT_NUMBER` = 37074083763
- `GCP_REGION` = europe-west1
- `BQ_RAW_DATASET` = wb_raw
- `AR_REPO` = wb-loaders
- `TF_STATE_BUCKET` = <UNIQUE_BUCKET_NAME> (из шага 1)
- `WIF_PROVIDER` = output `workload_identity_provider`
- `DEPLOYER_SA` = output `deployer_service_account`
- `TERRAFORM_PLAN_SA` = output `terraform_plan_service_account`
- `TERRAFORM_APPLY_SA` = output `terraform_apply_service_account`

## 5. GitHub — Environments (Settings → Environments)
- `production` — **Required reviewers** (approval для deploy-prod и prod scheduler-control).
- `infra` — Required reviewers (approval для `terraform apply` и shadow scheduler-control).

## 6. Дальше (без рук в Console)
- `deploy-shadow.yml` — авто по push в main (cloud/**): образ по digest → shadow Job;
  в summary печатается ПАРА (image_digest, source_git_sha).
- `deploy-prod.yml` — вручную с approval; на вход `image_digest` + `source_git_sha`
  из summary shadow; валидирует digest и промоутит ТОТ ЖЕ образ.
- `scheduler-control.yml` — pause/resume/run-now.
- `infra.yml` — plan на PR (read-only SA) / apply вручную с approval (privileged SA).

## Инварианты (проверяемы в ревью)
- deployer БЕЗ secretAccessor и БЕЗ Scheduler Admin;
- terraform-apply отделён от terraform-plan; WIF-доступ к apply-SA только из main;
- apply-SA имеет actAs на runtime/scheduler SAs;
- shadow-SA пишет только `*__CR` и `LOADER_RUNS`, читает `REF_SKU_MASTER`;
- prod Scheduler PAUSED; `paused` исключён из Terraform drift (ignore_changes);
- prod образ не пересобирается — промоушен digest + source sha сборки;
- remote state в GCS; ключей JSON в GitHub нет.
