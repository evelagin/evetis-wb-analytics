# ── Серверный исполнитель пакетов выпуска и репетиций Heldi ─────────────────────────────────
# Два задания, две личности:
#   heldi-release-exec      sa-heldi-release-exec  — стадии пакета под ACK: UpdateJob/run контроллера,
#                                                    pause/resume tenant-backfill-tick, таблицы tenant_ops/tenant_locks;
#   heldi-release-rehearse  sa-heldi-rehearse      — репетиция на копии: production только читается.
# Нет Scheduler и нет SA-будильника: каждое исполнение запускает владелец (`gcloud run jobs execute … --args=<стадия>`),
# это и есть шаг ACK. max_retries = 0 у обоих заданий: POST с неизвестным исходом НЕ повторяется никогда.
# Журнал — бакет только на добавление (политика хранения, объекты не перезаписываются): каждое событие —
# новый объект journal/<пакет>/<seq>-<тип>.json, создаваемый с ifGenerationMatch=0. Второй одновременный
# исполнитель получает 412 на том же seq и останавливается — это и есть замок «одна копия».
locals {
  who            = "${var.tenant_id} (${var.project_id})"
  controller_job = "tenant-backfill-controller"
  controller_sa  = "sa-backfill-controller@${var.project_id}.iam.gserviceaccount.com"
  bucket         = "${var.project_id}-heldi-release-journal"
  jobs_enabled   = var.package_pins != null
  # Адреса SA известны на плане: ревью плана видит ровно этих участников грантов.
  sa = {
    exec     = "sa-heldi-release-exec@${var.project_id}.iam.gserviceaccount.com"
    rehearse = "sa-heldi-rehearse@${var.project_id}.iam.gserviceaccount.com"
  }
  read_datasets = ["ozon_raw", "ref", "tenant_ops", "tenant_locks"]
  dataset_condition = { for k, ds in { read = local.read_datasets, write = var.exec_write_datasets } : k => join(" || ", flatten([for d in ds : [
    "resource.name == \"projects/${var.project_id}/datasets/${d}\"",
    "resource.name.startsWith(\"projects/${var.project_id}/datasets/${d}/\")",
  ]])) }
  # Объекты бакета: префикс своей личности + сам бакет (list проверяется на уровне бакета).
  object_condition = { for k, p in { exec = ["journal/", "logs/"], rehearse = ["rehearsal/"] } : k => join(" || ", concat(
    ["resource.name == \"projects/_/buckets/${local.bucket}\""],
    [for x in p : "resource.name.startsWith(\"projects/_/buckets/${local.bucket}/objects/${x}\")"],
  )) }
  # Чтение — у обеих личностей. НЕ roles/bigquery.jobUser (dataform.*.create, cloudaicompanion.*, PR #380).
  read_permissions = [
    "bigquery.jobs.create",
    "cloudscheduler.jobs.get",
    "cloudscheduler.jobs.list",
    "logging.logEntries.list",
    "run.executions.get",
    "run.executions.list",
    "run.jobs.get",
    "run.jobs.list",
    "run.operations.get",
  ]
  data_permissions = ["bigquery.datasets.get", "bigquery.tables.get", "bigquery.tables.getData", "bigquery.tables.list"]
  # Запись исполнителя: создать таблицу и дописать строки. Нет tables.delete/update/setIamPolicy, нет datasets.update.
  write_permissions = ["bigquery.tables.create", "bigquery.tables.updateData"]
}

# ── Личности ────────────────────────────────────────────────────────────────────────────────
resource "google_service_account" "exec" {
  project      = var.project_id
  account_id   = "sa-heldi-release-exec"
  display_name = "Heldi release executor"
  description  = "heldi-release-exec: стадии пакета выпуска по OWNER ACK. Только контроллер Heldi, tick и tenant_ops/tenant_locks."
}

resource "google_service_account" "rehearse" {
  project      = var.project_id
  account_id   = "sa-heldi-rehearse"
  display_name = "Heldi rehearsal (read-only)"
  description  = "heldi-release-rehearse: репетиция пакета на копии. Production только читается; пишет лишь rehearsal/ в журнал."
}

# ── Роли ────────────────────────────────────────────────────────────────────────────────────
resource "google_project_iam_custom_role" "read" {
  project     = var.project_id
  role_id     = "heldiRunnerRead"
  title       = "Heldi runner read"
  description = "Исполнитель и репетиция: SELECT, метаданные Run/Scheduler, чтение логов. Без записи."
  permissions = local.read_permissions
  stage       = "GA"
}

resource "google_project_iam_custom_role" "data" {
  project     = var.project_id
  role_id     = "heldiRunnerData"
  title       = "Heldi runner data read"
  description = "Чтение таблиц ozon_raw, ref, tenant_ops, tenant_locks (условие IAM на датасеты)."
  permissions = local.data_permissions
  stage       = "GA"
}

resource "google_project_iam_custom_role" "write" {
  project     = var.project_id
  role_id     = "heldiRunnerWrite"
  title       = "Heldi runner journal write"
  description = "Исполнитель пакета: создать таблицу и дописать строки в tenant_ops/tenant_locks (условие IAM). Без удаления."
  permissions = local.write_permissions
  stage       = "GA"
}

resource "google_project_iam_custom_role" "tick" {
  project     = var.project_id
  role_id     = "heldiRunnerTick"
  title       = "Heldi runner tick pause/resume"
  description = "Исполнитель пакета: pause/resume расписаний (Scheduler без IAM по заданию); код образа трогает только tenant-backfill-tick."
  permissions = ["cloudscheduler.jobs.pause", "cloudscheduler.jobs.resume"]
  stage       = "GA"
}

resource "google_project_iam_custom_role" "controller" {
  project     = var.project_id
  role_id     = "heldiRunnerController"
  title       = "Heldi runner controller update"
  description = "Исполнитель пакета: UpdateJob и run ТОЛЬКО задания tenant-backfill-controller (грант на уровне задания)."
  permissions = ["run.jobs.get", "run.jobs.run", "run.jobs.update"]
  stage       = "GA"
}

resource "google_project_iam_custom_role" "act_as" {
  project     = var.project_id
  role_id     = "heldiRunnerActAs"
  title       = "Heldi runner act as controller SA"
  description = "UpdateJob контроллера сохраняет его SA: нужен actAs ровно на sa-backfill-controller."
  permissions = ["iam.serviceAccounts.actAs"]
  stage       = "GA"
}

# ── Гранты: чтение (обе личности) ───────────────────────────────────────────────────────────
resource "google_project_iam_member" "read" {
  for_each   = local.sa
  project    = var.project_id
  role       = "projects/${var.project_id}/roles/heldiRunnerRead"
  member     = "serviceAccount:${each.value}"
  depends_on = [google_project_iam_custom_role.read, google_service_account.exec, google_service_account.rehearse]
}

# ACL датасетов авторитетны в infra/tenant (T3.3): грант на датасете отсюда стёрся бы его apply.
# Поэтому — уровень проекта с условием IAM на датасеты (как heldiWatchData в infra/tenant-monitoring).
resource "google_project_iam_member" "data" {
  for_each = local.sa
  project  = var.project_id
  role     = "projects/${var.project_id}/roles/heldiRunnerData"
  member   = "serviceAccount:${each.value}"
  condition {
    title       = "heldi-runner-read-datasets"
    description = "Только ozon_raw, ref, tenant_ops, tenant_locks."
    expression  = local.dataset_condition["read"]
  }
  depends_on = [google_project_iam_custom_role.data, google_service_account.exec, google_service_account.rehearse]
}

# ── Гранты на изменение production: ТОЛЬКО на окно выпуска (решение владельца 09.11) ──────────────
# В покое у sa-heldi-release-exec нет ни одного права на изменение production: эти четыре привязки не
# существуют, пока ACK выпуска не задаст `release_window`. С окном каждая привязка несёт условие IAM
# `request.time` в [not_before, expires) — по истечении права гаснут сами, без apply (атрибут времени
# поддерживают все сервисы Google Cloud: docs.cloud.google.com/iam/docs/conditions-attribute-reference,
# прочитано через Firecrawl 09.11). Окно не длиннее 8 ч. Закрывающий apply ACK (без release_window)
# убирает и сами привязки — до следующего выпуска.
# Пауза tick дополнительно ограничена безопасной фазой цикла */3: вызов pause/resume разрешён IAM только
# во 2-й минуте каждого трёхминутного цикла (минута UTC = 1, 4, …, 58; МСК = UTC+3, границы минут совпадают),
# то есть не раньше 60 с после срабатывания tick и не позже 60 с до следующего. «Нет активного исполнения»
# проверяет образ перед вызовом и readback после (см. регламент §4).
locals {
  window = var.release_window
  in_window = local.window == null ? null : join(" && ", [
    "request.time >= timestamp(\"${local.window.not_before}\")",
    "request.time < timestamp(\"${local.window.expires}\")",
  ])
  safe_minute  = "(${join(" || ", [for m in range(1, 60, 3) : "request.time.getMinutes() == ${m}"])})"
  window_title = local.window == null ? null : "heldi-release-${substr(local.window.ack_sha256, 0, 12)}"
  window_note  = local.window == null ? null : "ACK ${local.window.ack_sha256}; ${local.window.not_before} — ${local.window.expires}"
}

resource "google_project_iam_member" "write" {
  count   = local.window == null ? 0 : 1
  project = var.project_id
  role    = "projects/${var.project_id}/roles/heldiRunnerWrite"
  member  = "serviceAccount:${local.sa.exec}"
  condition {
    title       = "${local.window_title}-write"
    description = "Только ${join(", ", var.exec_write_datasets)}; ${local.window_note}"
    expression  = "(${local.dataset_condition["write"]}) && ${local.in_window}"
  }
  depends_on = [google_project_iam_custom_role.write, google_service_account.exec, google_service_account.rehearse]
}

# Cloud Scheduler не даёт IAM на уровне задания и НЕ поддерживает resource.name в условиях IAM
# (docs.cloud.google.com/iam/docs/conditions-resource-attributes, Firecrawl 09.11). Поэтому pause/resume —
# на проекте; сужение по времени (окно выпуска + безопасная минута цикла) — условием, по заданию —
# кодом образа (только …/jobs/tenant-backfill-tick:pause|resume) и наблюдением (heldi-health-eval FAIL
# ORDINARY_SCHEDULER_NOT_PAUSED). update/delete/create/run расписаний нет.
resource "google_project_iam_member" "tick" {
  count   = local.window == null ? 0 : 1
  project = var.project_id
  role    = "projects/${var.project_id}/roles/heldiRunnerTick"
  member  = "serviceAccount:${local.sa.exec}"
  condition {
    title       = "${local.window_title}-tick"
    description = "Безопасная минута цикла */3; ${local.window_note}"
    expression  = "${local.in_window} && ${local.safe_minute}"
  }
  depends_on = [google_project_iam_custom_role.tick, google_service_account.exec, google_service_account.rehearse]
}

# Неавторитетный member на задании контроллера (infra/tenant тоже ведёт members, не политику). Если политика
# задания Cloud Run не примет условную привязку, apply упадёт (fail-closed): тогда — решение владельца,
# без молчаливого расширения на проект.
resource "google_cloud_run_v2_job_iam_member" "controller" {
  count    = local.window == null ? 0 : 1
  project  = var.project_id
  location = var.region
  name     = local.controller_job
  role     = "projects/${var.project_id}/roles/heldiRunnerController"
  member   = "serviceAccount:${local.sa.exec}"
  condition {
    title       = "${local.window_title}-controller"
    description = local.window_note
    expression  = local.in_window
  }
  depends_on = [google_project_iam_custom_role.controller, google_service_account.exec, google_service_account.rehearse]
}

resource "google_service_account_iam_member" "act_as" {
  count              = local.window == null ? 0 : 1
  service_account_id = "projects/${var.project_id}/serviceAccounts/${local.controller_sa}"
  role               = "projects/${var.project_id}/roles/heldiRunnerActAs"
  member             = "serviceAccount:${local.sa.exec}"
  condition {
    title       = "${local.window_title}-act-as"
    description = local.window_note
    expression  = local.in_window
  }
  depends_on = [google_project_iam_custom_role.act_as, google_service_account.exec]
}

# ── Журнал: бакет только на добавление ──────────────────────────────────────────────────────
resource "google_storage_bucket" "journal" {
  project                     = var.project_id
  name                        = local.bucket
  location                    = "EUROPE-WEST1"
  storage_class               = "STANDARD"
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false
  versioning { enabled = true }
  # Не заблокирована: снять её может только владелец проекта. Пока действует — объект нельзя
  # удалить или перезаписать (в т.ч. самому исполнителю), поэтому журнал — только добавление.
  retention_policy {
    retention_period = var.journal_retention_days * 86400
    is_locked        = false
  }
  labels = { tenant = var.tenant_id, purpose = "release-journal" }
}

# objectCreator: создать новый объект (перезапись требовала бы storage.objects.delete — её нет).
resource "google_storage_bucket_iam_member" "creator" {
  for_each = local.sa
  bucket   = google_storage_bucket.journal.name
  role     = "roles/storage.objectCreator"
  member   = "serviceAccount:${each.value}"
  condition {
    title       = "heldi-runner-${each.key}-prefix"
    description = each.key == "exec" ? "journal/ и logs/" : "rehearsal/"
    expression  = local.object_condition[each.key]
  }
  depends_on = [google_service_account.exec, google_service_account.rehearse]
}

resource "google_storage_bucket_iam_member" "viewer" {
  for_each = local.sa
  bucket   = google_storage_bucket.journal.name
  role     = "roles/storage.objectViewer"
  member   = "serviceAccount:${each.value}"
  condition {
    title       = "heldi-runner-${each.key}-prefix"
    description = each.key == "exec" ? "journal/ и logs/" : "rehearsal/"
    expression  = local.object_condition[each.key]
  }
  depends_on = [google_service_account.exec, google_service_account.rehearse]
}

# ── Задания (второй apply: появляются вместе с пинами пакета) ────────────────────────────────
resource "google_cloud_run_v2_job" "exec" {
  count               = local.jobs_enabled && var.exec_image != null ? 1 : 0
  project             = var.project_id
  location            = var.region
  name                = "heldi-release-exec"
  deletion_protection = false
  template {
    task_count  = 1
    parallelism = 1
    template {
      service_account = local.sa.exec
      timeout         = var.exec_timeout
      max_retries     = 0
      containers {
        image   = var.exec_image
        command = ["python", "-I", "-m", "heldi_runner.entry"]
        dynamic "env" {
          for_each = {
            RUNNER_MODE       = "EXEC"
            TENANT_ID         = var.tenant_id
            PROJECT_ID        = var.project_id
            EXPECTED_IDENTITY = local.sa.exec
            JOURNAL_BUCKET    = google_storage_bucket.journal.name
            PACKAGE           = var.package_pins.package
            ACK_SHA256        = var.package_pins.ack_sha256
            PACKAGE_SHA256    = var.package_pins.package_sha256
            EXECUTOR_SOURCE   = var.package_pins.executor_source
          }
          content {
            name  = env.key
            value = env.value
          }
        }
        resources {
          limits = { cpu = "1000m", memory = "2Gi" }
        }
      }
    }
  }
  depends_on = [google_project_iam_member.read, google_project_iam_member.data,
  google_storage_bucket_iam_member.creator, google_storage_bucket_iam_member.viewer]
}

resource "google_cloud_run_v2_job" "rehearse" {
  count               = local.jobs_enabled && var.rehearse_image != null ? 1 : 0
  project             = var.project_id
  location            = var.region
  name                = "heldi-release-rehearse"
  deletion_protection = false
  template {
    task_count  = 1
    parallelism = 1
    template {
      service_account = local.sa.rehearse
      timeout         = var.rehearse_timeout
      max_retries     = 0
      containers {
        image   = var.rehearse_image
        command = ["python", "-I", "-m", "heldi_runner.entry"]
        dynamic "env" {
          for_each = {
            RUNNER_MODE       = "REHEARSE"
            TENANT_ID         = var.tenant_id
            PROJECT_ID        = var.project_id
            EXPECTED_IDENTITY = local.sa.rehearse
            JOURNAL_BUCKET    = google_storage_bucket.journal.name
            PACKAGE           = var.package_pins.package
            ACK_SHA256        = var.package_pins.ack_sha256
            PACKAGE_SHA256    = var.package_pins.package_sha256
            EXECUTOR_SOURCE   = var.package_pins.executor_source
          }
          content {
            name  = env.key
            value = env.value
          }
        }
        resources {
          limits = { cpu = "1000m", memory = "2Gi" }
        }
      }
    }
  }
  depends_on = [google_project_iam_member.read, google_project_iam_member.data,
  google_storage_bucket_iam_member.creator, google_storage_bucket_iam_member.viewer]
}
