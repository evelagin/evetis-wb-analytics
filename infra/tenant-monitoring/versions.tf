# Оповещения выделенного арендатора — отдельный корень Terraform (не infra/tenant).
#
# Почему отдельно: infra/tenant управляет заданием контроллера, расписаниями и runtime,
# а живой контроллер обновляется исполнителем выпуска мимо этого корня (UpdateJob).
# Полный apply infra/tenant ради алертов показал бы дрейф и мог бы откатить контроллер
# или состояние tick. Здесь только канал, лог-метрики и политики Cloud Monitoring:
# apply и destroy этого корня не касаются загрузки данных.
#
# State: gs://mpa-platform-tfstate-777428383056/monitoring/tenants/<tenant_id>
# (не под префиксом tenants/<tenant_id>, чтобы не появиться «рабочим пространством»
# корня арендатора). Образец политик — алерты Юнитки (infra/terraform/ozon_unitka.tf,
# scheduler.tf); отличие — метрики и PromQL вместо log-match: инцидент открывается
# один раз и не повторяется на каждом тике.
terraform {
  required_version = ">= 1.9.0"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 6.20.0, < 8.0.0"
    }
  }
  backend "gcs" {}
}

# Квота и биллинг вызовов — на проект арендатора, как в infra/tenant.
provider "google" {
  project               = var.project_id
  user_project_override = true
  billing_project       = var.project_id
}
