# infra/bootstrap

Одноразовый модуль: создаёт GCS-bucket под remote state основного Terraform.
Свой state — локальный (bucket нельзя хранить в самом себе).

```
cd infra/bootstrap
terraform init
terraform apply -var project_id=<PROJECT_ID> -var state_bucket=<UNIQUE_BUCKET_NAME>
```

Затем в `infra/terraform` инициализируй backend этим bucket'ом:
```
cd ../terraform
terraform init -backend-config="bucket=<UNIQUE_BUCKET_NAME>" -backend-config="prefix=evetis/wb-cloud"
```
