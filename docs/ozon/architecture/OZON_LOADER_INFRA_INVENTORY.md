# Инвентаризация ingestion-инфраструктуры перед загрузчиком Ozon

Дата: 2026-09-01. Проверено по репозиторию и по живому GCP, без предположений.

## Вывод

**Production-паттерн Cloud Run для WB существует и работает.** Его надо переиспользовать
как инфраструктурный подход, но не как бизнес-логику: домены остаются изолированными.

## Что найдено в репозитории

| Компонент | Путь | Что это |
| --- | --- | --- |
| Загрузчики | `cloud/src/` | TypeScript, Node 20. `cli.ts` выбирает загрузчик аргументом |
| Реестр загрузчиков | `cloud/src/loaders/registry.ts` | `noop`, `stocks` |
| Клиент BigQuery | `cloud/src/bq/client.ts` | общий, `location=EU` |
| Манифест прогонов | `cloud/src/bq/runManifest.ts` | пишет в `wb_raw.LOADER_RUNS` |
| Секреты | `cloud/src/secrets.ts` | `@google-cloud/secret-manager` |
| HTTP с ретраями | `cloud/src/http/wbHttp.ts` | обработка 429, специфична для WB |
| Образ | `cloud/Dockerfile` | один образ на все Jobs, `ENTRYPOINT node dist/cli.js` |
| Инфраструктура | `infra/terraform/` | `service_accounts.tf`, `iam.tf`, `cloud_run_jobs.tf`, `scheduler.tf`, `secrets.tf`, `bigquery.tf`, `wif.tf`, `artifact_registry.tf` |
| Деплой | `.github/workflows/` | `deploy-shadow.yml`, `deploy-prod.yml`, `infra.yml`, `ci.yml`, `scheduler-control.yml` |

Зависимости: `@google-cloud/bigquery ^7.9.0`, `@google-cloud/secret-manager ^5.6.0`.

## Что найдено в живом GCP

**Cloud Run Jobs** (`europe-west1`): `wb-stocks-shadow`, `wb-stocks-prod`, `wb-mart-prod`.
Cloud Run **Services** для ingestion нет — используются именно Jobs.

**Cloud Scheduler** (`europe-west1`): `wb-stocks-shadow`, `wb-stocks-prod`, `wb-mart-prod`, `evetis-wb-poll`.

**Service accounts:**

| SA | Назначение |
| --- | --- |
| `sa-loaders-shadow`, `sa-loaders-prod` | runtime загрузчиков, раздельно по средам |
| `sa-scheduler-shadow`, `sa-scheduler-prod` | право запустить Job, отделено от runtime |
| `sa-deployer` | деплой из GitHub Actions через WIF, **без доступа к значениям секретов** |
| `sa-terraform-plan` | read-only, для PR |
| `sa-terraform-apply` | привилегированный, только из main |
| `metabase-read-only`, `evetis-wb-comms` | вне ingestion |

**GCS:** `evetis-wb-tfstate-37074083763` (состояние Terraform),
`run-sources-project-fa311fc0-4d87-4781-986-europe-west1` (исходники Cloud Build).
**Отдельного staging-бакета для ingestion нет.**

**Artifact Registry** (`europe-west1`): `wb-loaders`, `cloud-run-source-deploy`.

**Настройки проекта:** `project_id = project-fa311fc0-4d87-4781-986`,
`region = europe-west1`, `bq_location = EU`.

## Существующий паттерн, коротко

```
GitHub Actions (WIF, sa-deployer)
      ↓ собирает образ, промоутит immutable digest shadow → prod
Artifact Registry wb-loaders
      ↓
Cloud Run Job (sa-loaders-*), args = имя загрузчика
      ↓ execution guard + LOADER_RUNS манифест
BigQuery wb_raw
      ↑
Cloud Scheduler (sa-scheduler-*) только запускает Job
```

Ценные решения, которые стоит перенять: раздельные runtime и scheduler identity;
deployer без доступа к значениям секретов; манифест прогонов с `run_id`,
`rows_fetched`, `rows_loaded`, `status`; один образ на несколько загрузчиков.

## Что переиспользуем, а что нет

**Переиспользуем подход:** Cloud Run Job вместо Service; `europe-west1`; отдельная
runtime identity; секреты только из Secret Manager; манифест прогонов; BigQuery `location=EU`.

**Не переиспользуем:** код `cloud/src/` целиком. Там `wbHttp`, `REF_SKU_MASTER`,
таблицы `RAW_WB_*` — это бизнес-логика WB. По правилу изоляции домен Ozon не может
на неё опираться. Загрузчик Ozon получает собственный код в `pipelines/ozon/`.

**Не трогаем:** ни один существующий Job, Scheduler, SA, бакет или workflow WB.

## Уточнение блокера B-01

Проверено по каждому API отдельно: `gcloud run`, `gcloud scheduler`, `gcloud iam`,
`gcloud storage`, `gcloud artifacts` из этой среды **работают**.
Не работает **только `bigquery.googleapis.com`** — возвращает HTML-страницу HTTP 403.

Значит блокер узкий: недоступна только data plane BigQuery. Путь
«GCS → Cloud Run → BigQuery» полностью обходит его, потому что запись в BigQuery
выполняется изнутри GCP, а не с этой машины.
