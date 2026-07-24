# PR-Mig0 — разовая настройка GCP (чек-лист владельца)

Всё автоматизируемое делает Terraform. Ниже — только то, что руками делает
владелец-админ. Токены и ключи в git НЕ попадают.

## 0. Предпосылки
- Проект: `project-fa311fc0-4d87-4781-986` (номер `37074083763`), BQ dataset `wb_raw` (EU).
- Установлены `gcloud` и `terraform` локально; роль **Owner** на проекте (только для
  ПЕРВОГО apply — дальше через WIF).

## 1. Первый `terraform apply` (bootstrap, человек-админ)
Chicken-egg: WIF и `sa-terraform` создаёт сам Terraform, поэтому первый прогон —
локально под Owner.
```
gcloud auth application-default login
cd infra/terraform
cp terraform.tfvars.example terraform.tfvars   # заполнить github_repo = OWNER/evetis-wb-analytics
terraform init
terraform apply
```
Создаёт: APIs, Artifact Registry, WIF pool/provider, 6 сервис-аккаунтов, IAM
(least-privilege), пустые секреты, таблицы `LOADER_RUNS` + `*__CR`, Cloud Run Jobs
`wb-stocks-shadow/prod` (bootstrap-образ, args=noop), Scheduler (PAUSED).

## 2. Значения токенов WB → Secret Manager (руками, значения не в git)
```
printf '%s' "$WB_TOKEN_ANALYTICS" | gcloud secrets versions add WB_TOKEN_ANALYTICS --data-file=-
printf '%s' "$WB_TOKEN_ADS"       | gcloud secrets versions add WB_TOKEN_ADS       --data-file=-
printf '%s' "$WB_TOKEN_FINANCE"   | gcloud secrets versions add WB_TOKEN_FINANCE   --data-file=-
```

## 3. GitHub — переменные и окружения
Repo → Settings → Secrets and variables → Actions → **Variables** (не Secrets — это не тайны):
- `GCP_PROJECT_ID` = project-fa311fc0-4d87-4781-986
- `GCP_REGION` = europe-west1
- `AR_REPO` = wb-loaders
- `WIF_PROVIDER` = output `workload_identity_provider` из terraform
- `DEPLOYER_SA` = output `deployer_service_account`
- `TERRAFORM_SA` = output `terraform_service_account`

Repo → Settings → **Environments**:
- `production` — добавить **Required reviewers** (approval для deploy-prod и prod scheduler-control).
- `infra` — добавить Required reviewers (approval для `terraform apply` и shadow scheduler-control).

Ключей JSON нет — аутентификация только через WIF.

## 4. Дальше (следующие PR, уже без рук в Console)
- `deploy-shadow.yml` — авто по push в main (cloud/**): собирает образ, пушит по
  digest, деплоит shadow Job. Digest — в summary.
- `deploy-prod.yml` — вручную, с approval, промоушен ТОГО ЖЕ digest в prod.
- `scheduler-control.yml` — pause/resume/run-now.
- `infra.yml` — plan на PR, apply вручную с approval.

## Инварианты (проверяемы в ревью)
- deployer БЕЗ `secretAccessor` и БЕЗ Scheduler Admin;
- shadow SA пишет только `*__CR` и `LOADER_RUNS`, читает `REF_SKU_MASTER`;
- prod Scheduler создаётся PAUSED (включается только на cutover);
- prod образ не пересобирается — промоушен digest;
- ключей JSON в GitHub нет.
