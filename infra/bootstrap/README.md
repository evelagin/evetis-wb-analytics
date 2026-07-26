# infra/bootstrap

Одноразовый модуль: создаёт GCS-bucket под remote state основного Terraform И
выдаёт Terraform-SA доступ к объектам этого бакета (state r/w + `.tflock`).
Свой state — локальный. Запускает человек-админ (Owner).

## Порядок
1. Первичный прогон (bucket): без SA-email'ов (они ещё не созданы) — биндинги
   пропускаются `count`-гардом:
   ```
   terraform init
   terraform apply -var project_id=<PROJECT_ID> -var state_bucket=<UNIQUE_BUCKET>
   ```
2. После создания SA основным модулем — повторный прогон с email'ами (из
   `terraform output` основного модуля), чтобы создать bucket-IAM:
   ```
   terraform apply -var project_id=<PROJECT_ID> -var state_bucket=<UNIQUE_BUCKET> \
     -var terraform_plan_sa_email=<sa-terraform-plan@...> \
     -var terraform_apply_sa_email=<sa-terraform-apply@...>
   ```

Роль на бакете — `roles/storage.objectUser` (bucket-level, НЕ project). Основной
модуль `infra/terraform` bucket-IAM НЕ трогает → CI-identity не рефрешит IAM бакета.

Инициализация backend основного модуля:
```
cd ../terraform
terraform init -backend-config="bucket=<UNIQUE_BUCKET>" -backend-config="prefix=evetis/wb-cloud"
```
