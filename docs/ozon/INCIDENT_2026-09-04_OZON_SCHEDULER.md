# Инцидент P0 — регулярная загрузка Ozon не запускалась

Дата обнаружения: 2026-09-04, в ходе аудита свежести витрины Stage 3.4C.3.
Начало отказа: 2026-09-03, сразу после изменения планировщиков.
Класс: полная остановка автоматической загрузки домена Ozon.

---

## 1. Что произошло

Все три планировщика Ozon — `ozon-fast`, `ozon-daily`, `ozon-weekly` —
возвращали `PERMISSION_DENIED` на каждой попытке.

```
ozon-fast    2026-09-04T10:00:50Z   status.code = 7
ozon-daily   2026-09-04T03:30:00Z   status.code = 7
ozon-fast    2026-09-04T04:00:07Z   status.code = 7
ozon-fast    2026-09-03T16:00:07Z   status.code = 7
```

За всё окно журнала: **4 попытки, 4 отказа, 0 успехов.**

Последнее успешное выполнение `ozon-runtime-ingest` — 2026-09-03T15:23Z,
и это был ручной запуск Stage 3.3B, а не планировщик. То есть
планировщики в новой конфигурации **не отработали ни разу**.

Отказ был молчаливым для данных: Cloud Run job не стартовал вовсе,
поэтому строки в `ozon_raw.OZON_INGESTION_RUNS` не появлялось, и статуса
`FAILED` там не было. Единственный наблюдаемый признак — возраст
последнего успешного прогона.

## 2. Корневая причина

Все три планировщика вызывали **один общий** job `ozon-runtime-ingest`,
передавая набор сущностей через `overrides.containerOverrides`
на v1-эндпоинте Admin API:

```
POST https://europe-west1-run.googleapis.com/apis/run.googleapis.com/v1/
       namespaces/project-fa311fc0-4d87-4781-986/jobs/ozon-runtime-ingest:run

{"overrides":{"containerOverrides":[{"env":[{"name":"ENTITIES","value":"..."}]}]}}
```

Запуск **с overrides** требует права `run.jobs.runWithOverrides`.

```
roles/run.invoker = run.instances.invoke, run.jobs.run, run.routes.invoke
```

`run.jobs.runWithOverrides` в этой роли **отсутствует**. У
`sa-ozon-scheduler` была только она.

**Контрольное сравнение.** `wb-mart-prod` имеет ровно ту же
`roles/run.invoker` у своей identity и работает штатно — потому что зовёт
v2-эндпоинт `:run` **без тела запроса**. Разница не в правах, а в форме
вызова.

## 3. Решение — наименьшая привилегия

Роль `roles/run.developer` **не выдавалась**. Вместо этого набор
сущностей перенесён из тела запроса в конфигурацию job.

| | Было | Стало |
|---|---|---|
| job | один `ozon-runtime-ingest` на все каденции | три выделенных |
| ENTITIES | в теле запроса планировщика | в env самого job |
| эндпоинт | v1 `apis/.../namespaces/...` | v2 `.../v2/projects/.../jobs/...` |
| тело запроса | `overrides.containerOverrides` | **пустое** |
| нужное право | `run.jobs.runWithOverrides` | `run.jobs.run` |
| роль identity | `roles/run.invoker` (недостаточно) | `roles/run.invoker` (достаточно) |

Каденция, наборы сущностей и lookback **не менялись** — перенесены
один в один.

| Job | ENTITIES | Расписание (МСК) | Планировщик |
|---|---|---|---|
| `ozon-runtime-fast` | `stocks,fbo_postings` | `0 7,13,19 * * *` | `ozon-fast` |
| `ozon-runtime-daily` | `catalog,prices,finance_accrual,ads_campaigns,ads_expense_daily,ads_sku_daily,supplies` | `30 6 * * *` | `ozon-daily` |
| `ozon-runtime-weekly` | `clusters` | `0 5 * * 1` | `ozon-weekly` |

Все три job используют один и тот же образ, ту же identity
`sa-ozon-ingestion`, те же секреты, регион, ресурсы и таймаут.

Исходный код runtime **не изменялся**: diff = 0.

## 4. Судьба `ozon-runtime-ingest`

Job сохранён и не изменялся. Его назначение теперь — **ручная,
полная и специальная загрузка** (историческая догрузка через
`SINCE`/`UNTIL`, точечный перезапуск одной сущности). Планировщик его
больше не вызывает, поэтому неоднозначности между ним и тремя
регулярными job нет.

Право `roles/run.invoker` у `sa-ozon-scheduler` на него оставлено:
отзывать его — отдельное решение, к устранению инцидента не относящееся.

## 5. IAM

**До**

```
sa-ozon-scheduler:
  ozon-runtime-ingest        roles/run.invoker
  на уровне проекта          ролей нет
```

**После**

```
sa-ozon-scheduler:
  ozon-runtime-ingest        roles/run.invoker   (без изменений)
  ozon-runtime-fast          roles/run.invoker   (добавлено)
  ozon-runtime-daily         roles/run.invoker   (добавлено)
  ozon-runtime-weekly        roles/run.invoker   (добавлено)
  на уровне проекта          ролей нет           (без изменений)
```

Не выдавались: `roles/run.developer`, admin Cloud Run, admin service
accounts, admin Secret Manager, широкие роли BigQuery.

`sa-ozon-ingestion` не менялась: `bigquery.dataEditor`,
`bigquery.dataViewer`, `bigquery.jobUser` на проекте и
`secretmanager.secretAccessor` на четырёх секретах `EVETIS_OZON_*`.

## 6. Infrastructure as Code

Добавлен `infra/terraform/ozon_ingestion.tf`: три job, три планировщика,
три привязки `run.invoker`. `terraform validate` — Success.

**Импорт выполнен 2026-09-04.** Девять ресурсов переведены под управление
Terraform; продакшен при этом не изменялся — импорт трогает только state.

| Адрес | Живой ресурс |
|---|---|
| `google_cloud_run_v2_job.ozon_runtime["ozon-runtime-fast"]` | `…/jobs/ozon-runtime-fast` |
| `google_cloud_run_v2_job.ozon_runtime["ozon-runtime-daily"]` | `…/jobs/ozon-runtime-daily` |
| `google_cloud_run_v2_job.ozon_runtime["ozon-runtime-weekly"]` | `…/jobs/ozon-runtime-weekly` |
| `google_cloud_scheduler_job.ozon_runtime["ozon-runtime-fast"]` | `…/jobs/ozon-fast` |
| `google_cloud_scheduler_job.ozon_runtime["ozon-runtime-daily"]` | `…/jobs/ozon-daily` |
| `google_cloud_scheduler_job.ozon_runtime["ozon-runtime-weekly"]` | `…/jobs/ozon-weekly` |
| `google_cloud_run_v2_job_iam_member.ozon_scheduler_invoke["ozon-runtime-fast"]` | `…/ozon-runtime-fast roles/run.invoker sa-ozon-scheduler` |
| `google_cloud_run_v2_job_iam_member.ozon_scheduler_invoke["ozon-runtime-daily"]` | `…/ozon-runtime-daily roles/run.invoker sa-ozon-scheduler` |
| `google_cloud_run_v2_job_iam_member.ozon_scheduler_invoke["ozon-runtime-weekly"]` | `…/ozon-runtime-weekly roles/run.invoker sa-ozon-scheduler` |

**HCL приведён к продакшену, а не наоборот.** Импорт вскрыл две разницы,
и обе устранены правкой кода:

| Разница | Решение |
|---|---|
| `deletion_protection`: продакшен `true`, HCL было `false` | HCL → `true`. Продакшен — эталон, ослаблять защиту ради единообразия с WB нельзя |
| метки `domain`/`stage` лежат на уровне `template`, а HCL объявлял их на уровне job | HCL → `template.labels`, как их положил `gcloud run jobs create --labels` |

**Остаточный дрейф после нормализации — только провайдерский:**

```
- client         = "gcloud" -> null
- client_version = "577.0.0" -> null
```

У ресурса `google_cloud_run_v2_job` аргументов `client` / `client_version`
нет — это computed-поля происхождения, которые gcloud проставляет при
создании, а Terraform выразить не может. Ровно тот же дрейф показывает
давно управляемый Terraform `wb_stocks_shadow` (`client_version = "568.0.0"`),
то есть это известное поведение провайдера, а не след инцидента.
Применение обнулило бы метаданные и ничего функционального не затронуло бы.

⚠️ **`terraform plan` целиком с этой машины не выполняется.** Refresh
ресурсов `google_bigquery_table` (`LOADER_RUNS`, `RAW_WB_STOCKS__CR`,
`WB_STOCKS_SNAPSHOTS__CR`) падает с HTTP 403 — тот же запрет data plane
BigQuery, из-за которого здесь не работают `bq` и REST. Ограничение
рабочего места, а не конфигурации. План по импортированной области
получен через `-target` на три Ozon-ресурса и валиден.

⚠️ **Предсуществующий дрейф WB, не связанный с инцидентом:**
`google_cloud_run_v2_job.wb_stocks_shadow` показывает в плане обновление
на месте (те же `client` / `client_version` → null). Не трогался.

🔴 **Что по-прежнему вне IaC.** Импорт намеренно ограничен девятью
ресурсами. Вне Terraform остаются `sa-ozon-ingestion`, `sa-ozon-scheduler`,
job `ozon-runtime-ingest`, датасеты `ozon_raw` и `ozon_mart`, секреты
`EVETIS_OZON_*`. Это дрейф, существовавший до инцидента; его закрытие —
отдельное решение владельца.

## 7. Откат

Исправление обратимо тремя командами на планировщик — вернуть прежний
URI и тело:

```bash
BODY_FAST='{"overrides":{"containerOverrides":[{"env":[{"name":"ENTITIES","value":"stocks,fbo_postings"}]}]}}'
V1=https://europe-west1-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/project-fa311fc0-4d87-4781-986/jobs/ozon-runtime-ingest:run
gcloud scheduler jobs update http ozon-fast --location=europe-west1 --uri="$V1" --message-body="$BODY_FAST"
# аналогично ozon-daily и ozon-weekly со своими наборами ENTITIES
# У job включена deletion_protection — сначала снять, иначе delete откажет:
for J in ozon-runtime-fast ozon-runtime-daily ozon-runtime-weekly; do
  gcloud run jobs update "$J" --region=europe-west1 --no-deletion-protection
  gcloud run jobs delete "$J" --region=europe-west1 --quiet
done
# И убрать из state, иначе Terraform будет считать их существующими:
cd infra/terraform
for K in ozon-runtime-fast ozon-runtime-daily ozon-runtime-weekly; do
  terraform state rm "google_cloud_run_v2_job.ozon_runtime[\"$K\"]" \
                     "google_cloud_scheduler_job.ozon_runtime[\"$K\"]" \
                     "google_cloud_run_v2_job_iam_member.ozon_scheduler_invoke[\"$K\"]"
done
```

Откат возвращает систему в неработающее состояние и осмыслен только
если новая схема окажется хуже. Данные при откате не теряются:
`ozon_raw` пишется идемпотентным MERGE, job не хранит состояния.

Снимок конфигурации до изменения сохранён в артефактах прогона:
`sched-ozon-{fast,daily,weekly}.yaml`, `job-ozon-runtime-ingest.yaml`,
`iam-job-before.yaml`, `iam-project-before.txt`.

## 8. Что этот инцидент говорит о наблюдаемости

Отказ **не был виден** ни одному существующему контролю: job не
стартовал, строк в `OZON_INGESTION_RUNS` не появлялось, статуса `FAILED`
не возникало. Молчание выглядело как отсутствие событий.

Поймал его контракт свежести `ozon_mart.V_OZON_MART_FRESHNESS`,
введённый Stage 3.4C.3, — по **возрасту последнего успеха**, а не по
наличию ошибки. Это ограничение записано в самом SQL и должно
сохраняться при любой правке контракта.

Отдельного алерта на состояние планировщиков по-прежнему нет.
Предложение: следить за `status.code` заданий Cloud Scheduler или за
`agent_decision_gate` витрины. Это выходит за периметр инцидента и
требует отдельного решения владельца.
