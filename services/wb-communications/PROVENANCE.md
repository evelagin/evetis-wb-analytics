# PROVENANCE — evetis-wb-communications

Файл создан на этапе **Stage A / A2** (2026-09-08) и фиксирует, откуда взят код в
этом каталоге и как он связан с тем, что реально работает в production.

До Stage A сервис существовал только в GCP: исходников в репозитории не было,
Terraform им не управлял, ревью изменений было невозможно. Это была находка
**F-01 (CRITICAL)** аудита 2026-09-08.

---

## 1. Цепочка происхождения — доказана полностью

```
GCS source archive
  gs://run-sources-project-fa311fc0-4d87-4781-986-europe-west1/
      services/evetis-wb-communications/1784800037.566966-c6268d35da1c415d9f424c4dcf162a56.zip
  generation 1784800038589046
        │
        ▼  Cloud Build  e7f0e35c-e2a8-4c3d-be0d-89f3c3db9841
           регион europe-west1 · SUCCESS · 2026-07-23T09:47:19Z
        │
        ▼  Artifact Registry
           europe-west1-docker.pkg.dev/project-fa311fc0-4d87-4781-986/
           cloud-run-source-deploy/evetis-wb-communications
           @sha256:0d33bb926944c5a1ef958ce46dfa9198b126de895948d6e4647ec1a94425bca5
           создан 2026-07-23 09:48, тег `latest`
        │
        ▼  Cloud Run Service  evetis-wb-communications  (europe-west1)
           ревизия evetis-wb-communications-00025-rq8  ← работает сейчас
        │
        ▼  Cloud Scheduler  evetis-wb-poll
           POST https://evetis-wb-communications-…run.app/poll
           0 8,11,14,17,20 * * *   Europe/Moscow
```

Ни одной стрелки со статусом UNKNOWN: digest, который исполняется в ревизии
`00025-rq8`, буквально указан в `results.images[].digest` сборки `e7f0e35c`, а
исходный архив этой сборки — единственный вход.

### Контрольные суммы восстановления

| Что | Значение |
|---|---|
| SHA-256 архива сборки (`.zip`) | `a37d0998f211a53543548f28e7dcbfc5eb16e5ef4262109ab4ff2ce87346c0bc` |
| Файлов в архиве (без `__pycache__`) | 145 |
| Манифест дерева в Git (SHA-256 от `find \| sort \| shasum`) | `92dc32b8ce699ceb23e893245495308c0805ace90f7871bfb240522cfbe6362e` |
| `diff -r` архив ↔ `services/wb-communications` | различий нет |

Из архива исключён единственный артефакт `__pycache__/*.pyc` — байткод не влияет
на сборку образа (`Dockerfile` ставит зависимости и копирует исходники).

---

## 2. Код в Git ≠ конфигурация в production

**Это главное, что нужно знать перед любым деплоем.**

Образ не пересобирался с 2026-07-23, а ревизий уже 25: ревизии 11…25 меняли
только переменные окружения (`gcloud run services update --update-env-vars`).
Поэтому `deploy/env.production.yaml` в этом каталоге — это состояние на момент
сборки, а не то, что работает сейчас.

Расхождение на 2026-09-08 (read-only снимок Cloud Run):

| Переменная | `deploy/env.production.yaml` | Живой Cloud Run | Последствие отката |
|---|---|---|---|
| `WB_PUBLISH_ENABLED` | `"false"` | **`true`** | публикация ответов на отзывы ВЫКЛЮЧИТСЯ |
| `WB_QUESTIONS_ENABLED` | `"false"` | **`true`** | опрос вопросов покупателей ОСТАНОВИТСЯ |
| `WB_QUESTION_PUBLISH_ENABLED` | `"false"` | **`true`** | публикация ответов на вопросы ВЫКЛЮЧИТСЯ |
| `COMMUNICATION_ENGINE_V2_ENABLED` | `"false"` | **`true`** | движок v2 перестанет строиться |
| `COMMUNICATION_ENGINE_V2_SHADOW_ONLY` | `"true"` | **`false`** | — |
| `COMMUNICATION_ENGINE_V2_PRIMARY` | `"false"` | **`true`** | генератор черновиков откатится на `reviews_v1` |
| `WB_QUESTIONS_FIRST_RUN_MAX` | `"20"` | `10` | больше вопросов в первом прогоне |

> 🔴 **`gcloud run deploy --env-vars-file deploy/env.production.yaml` перезапишет
> набор переменных целиком и молча остановит публикацию в Wildberries.**
> Именно так `DEPLOY.md` (файл из архива сборки) и предлагает деплоить.
> Пока это расхождение не решено владельцем, деплоить сервис можно только
> аддитивно: `gcloud run services update --update-env-vars …`.

Файл `deploy/env.production.yaml` намеренно оставлен таким, каким он был в
собранном образе. Приводить его к «живым» значениям — это изменение
production-поведения, а Stage A такого не делает.

---

## 3. Что сервис делает во внешнем мире

Единственный подтверждённый канал записи из всей системы EVETIS наружу.

| Направление | Endpoint | Гейт |
|---|---|---|
| Чтение отзывов | `GET https://feedbacks-api.wildberries.ru/api/v1/feedbacks` | `WB_POLL_BATCH_SIZE`, `WB_MAX_PAGES`, `WB_MAX_ITEMS` |
| **Публикация ответа на отзыв** | `POST /api/v1/feedbacks/answer` | `WB_PUBLISH_ENABLED` + `WB_VERIFY_BEFORE_PUBLISH` |
| Чтение вопросов | `GET /api/v1/questions` | `WB_QUESTIONS_ENABLED` |
| **Публикация ответа на вопрос** | `PATCH /api/v1/questions` | `WB_QUESTION_PUBLISH_ENABLED` |
| Генерация текста | OpenAI, модель `gpt-5.6-terra` | `PROMPT_VERSION`, валидаторы `app/communication_engine/validators/` |
| Согласование | Telegram-бот, `TELEGRAM_ALLOWED_USER_IDS` | ручное подтверждение |
| Журнал | BigQuery `evetis_communications` + Firestore | — |

Оба гейта публикации в коде по умолчанию закрыты
(`app/config.py`: `os.environ.get("WB_PUBLISH_ENABLED", "false")`), решение
принимается в `app/services/pipeline.py:610` и `:614`.

### Аварийное отключение (kill switch)

```bash
gcloud run services update evetis-wb-communications \
  --project=project-fa311fc0-4d87-4781-986 --region=europe-west1 \
  --update-env-vars WB_PUBLISH_ENABLED=false,WB_QUESTION_PUBLISH_ENABLED=false
```

Опрос и генерация черновиков продолжатся, наружу ничего не уйдёт. Полная
остановка — пауза планировщика:

```bash
gcloud scheduler jobs pause evetis-wb-poll \
  --project=project-fa311fc0-4d87-4781-986 --location=europe-west1
```

---

## 4. Инфраструктура

| Ресурс | Значение |
|---|---|
| Cloud Run Service | `evetis-wb-communications`, europe-west1, ingress `all`, CPU 1, память 512Mi |
| Service account | `evetis-wb-comms@project-fa311fc0-4d87-4781-986.iam.gserviceaccount.com` |
| Роли SA | `roles/bigquery.jobUser`, `roles/datastore.user` (проектный уровень) |
| Секреты (Secret Manager, значения не в Git) | `ADMIN_TOKEN`, `OPENAI_API_KEY`, `SCHEDULER_SECRET`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET`, `TELEGRAM_WEBHOOK_URL`, `WB_API_TOKEN` |
| Scheduler | `evetis-wb-poll`, `0 8,11,14,17,20 * * *` Europe/Moscow |
| Terraform | описан в `infra/terraform/wb_communications.tf` через `import`-блоки — см. §5 |

---

## 5. Статус перевода под Terraform

Ресурсы **уже существуют** и создавались вручную, поэтому Terraform обязан их
**принять (import)**, а не создавать заново. В `infra/terraform/wb_communications.tf`
объявлены ресурсы и `import`-блоки; `apply` в рамках Stage A **не выполнялся**.

Причина: `terraform plan` с этой машины падает на сетевом фильтре
(`bigquery.googleapis.com` возвращает HTTP 403 на уровне сети — см. приложение к
отчёту аудита 2026-09-08), поэтому доказать «no destroy / no replacement»
локально невозможно. Принимать ресурсы вслепую запрещено.

**Как завершить:** выполнить `plan` из GitHub Actions (`.github/workflows/infra.yml`,
`action=plan`), убедиться, что в плане нет `destroy` и `forces replacement`, и
только затем `apply`.

---

## 6. Что НЕ делалось на Stage A

- поведение сервиса не менялось;
- новая ревизия не деплоилась;
- переменные окружения не трогались;
- ни одного тестового отзыва или ответа в Wildberries не отправлено;
- зависимости не обновлялись, рефакторинга нет — только перенос кода 1:1.
