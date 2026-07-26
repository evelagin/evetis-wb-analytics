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

## Migration for existing Mig0 installation (перенос bucket-IAM main → bootstrap)

Для УЖЕ развёрнутой Mig0 два ресурса bucket-IAM находятся в remote state ОСНОВНОГО
модуля. Обычный `plan/apply` после удаления из `infra/terraform` предложит
**уничтожить реальные grants** → CI потеряет доступ к backend. Порядок переноса
владения (детерминированно, через `import` — НЕ через apply-адопт):

1. **НЕ запускать** `terraform apply` в `infra/terraform` сразу после merge.

2. Удалить записи из remote state основного модуля (реальные IAM grants остаются):
   ```
   cd infra/terraform
   terraform state rm \
     google_storage_bucket_iam_member.tfstate_plan \
     google_storage_bucket_iam_member.tfstate_apply
   ```

3. Подготовить значения для `infra/bootstrap` (в `terraform.tfvars` или через `-var`):
   ```
   project_id               = project-fa311fc0-4d87-4781-986
   state_bucket             = evetis-wb-tfstate-37074083763
   terraform_plan_sa_email  = sa-terraform-plan@project-fa311fc0-4d87-4781-986.iam.gserviceaccount.com
   terraform_apply_sa_email = sa-terraform-apply@project-fa311fc0-4d87-4781-986.iam.gserviceaccount.com
   ```

4. Импортировать существующие grants в bootstrap state (`terraform init` в bootstrap
   уже выполнен; адрес обязательно с `[0]` из-за `count`):
   ```
   cd ../bootstrap
   terraform import \
     'google_storage_bucket_iam_member.tfstate_plan[0]' \
     'b/evetis-wb-tfstate-37074083763 roles/storage.objectUser serviceAccount:sa-terraform-plan@project-fa311fc0-4d87-4781-986.iam.gserviceaccount.com'
   terraform import \
     'google_storage_bucket_iam_member.tfstate_apply[0]' \
     'b/evetis-wb-tfstate-37074083763 roles/storage.objectUser serviceAccount:sa-terraform-apply@project-fa311fc0-4d87-4781-986.iam.gserviceaccount.com'
   ```

5. Проверки:
   - bootstrap: `terraform plan` → **No changes**
   - main:      `terraform plan` → **No changes**
   - GitHub:    Actions → infra → plan → **green**

Для существующего проекта используем ЯВНЫЙ `import` (не `apply`), чтобы перенос
владения bindings был детерминированным.
