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

## 1a. Развёртывание 2026-09-28 — инцидент F-19 (только журналирование)

| | |
|---|---|
| Исходник | ветка `sec/wb-comms-telegram-token-redaction`, коммит `c8124ca4ec2439a43e8403c1186c5128f49cf86f` (PR #209) |
| Сборка | Cloud Build `b334eff2-7f1b-4226-b6ed-2776174c0efe` (europe-west1): шаг `pytest` на `python:3.12-slim` с закреплёнными версиями → `docker build` |
| Образ | `europe-west1-docker.pkg.dev/project-fa311fc0-4d87-4781-986/cloud-run-source-deploy/evetis-wb-communications@sha256:21f5a80427c38fafc8c3d806e04b51dfc8dc36682cfdce5c838a4bbbc7227511` |
| Ревизия | `evetis-wb-communications-00026-t9f` (100 % трафика); предыдущая `00025-rq8`, `sha256:0d33bb92…` |
| Способ | `gcloud run services update --image <digest>` — окружение, SA, ingress, IAM не менялись (отпечаток env до/после одинаков) |
| Откат | `gcloud run services update-traffic evetis-wb-communications --region europe-west1 --to-revisions evetis-wb-communications-00025-rq8=100` |

Terraform (`infra/terraform/wb_communications.tf`) по-прежнему указывает прежний digest: `template` в
`ignore_changes`, поэтому apply ревизию не трогает; строка — документация исходного импорта.

## 1b. Развёртывание 2026-09-28 — D-19b (аудируемый публичный вход, 1.6.0)

| | |
|---|---|
| Исходник | ветка `feat/wb-comms-audit-events`, коммит `79bdfaeb7f99e5ab211a348078b70308c34b7a8f` (PR #218), поверх main `b4803cb` (сервис = 1.5.2 `f84f745`) |
| Сборка | Cloud Build `78f3907e-07b0-4220-bff1-091b566995a9`: `pytest` на `python:3.12-slim` с закреплёнными версиями → `docker build` |
| Образ | `…/cloud-run-source-deploy/evetis-wb-communications@sha256:aa55989cb0103ad51e8cb2079e8c10ef3d160298b4d8c8ad99a0b881a70ecd77` |
| Ревизия | `evetis-wb-communications-00032-22j`; до неё `00030-67n` (1.5.2, `c6022b02…`), промежуточная `00031-qz6` (`99216cb6…`, без `request_end`) |
| Способ | `gcloud run services update --image <digest>` — окружение, SA, ingress, `allUsers` не менялись |
| Откат | `gcloud run services update-traffic evetis-wb-communications --region europe-west1 --to-revisions evetis-wb-communications-00030-67n=100` |

## 1c. Развёртывание 2026-09-28 — main 1.6.1 (D-19b канон + v3 SHADOW выключен + 1.5.3)

Запись добавлена задним числом 2026-09-29 по результатам сверки; сама выкатка выполнена 2026-09-28 17:46 UTC.

| | |
|---|---|
| Исходник | `main`, merge-коммит `1e4b912875bbb79e3c2a8ee4c76bde955656cde1` (PR #218 = D-19b 1.6.1 поверх 1.6.0 v3 SHADOW #219 и 1.5.3 WP11 #220) |
| Сборка | Cloud Build `851d81f6-04de-404a-9ebb-5fdb9e35d987`: `pytest` на `python:3.12-slim` → `docker build`, тег `d19b-1.6.1-1e4b912` |
| Доказательство | архив исходников сборки: `SOURCE_SHA` = `1e4b912…`; `diff -r services/wb-communications` архив ↔ `git archive 1e4b912` — различий нет |
| Образ | `…/cloud-run-source-deploy/evetis-wb-communications@sha256:3a8c6c8f2ae3a3baace7e3de83c4c1ae834c357740013d52a1aede5527d22b57` |
| Ревизия | `evetis-wb-communications-00033-x6m` (100 % трафика); до неё `00032-22j` (`aa55989c…`) |
| Способ | только образ: env (47 записей, отпечаток одинаков у 00032 и 00033), SA, ресурсы, maxScale, ingress не менялись. `V3_SHADOW_ENABLED` в окружении нет → v3 выключен (`build_v3` → `None`) |
| Откат | `gcloud run services update-traffic evetis-wb-communications --region europe-west1 --to-revisions evetis-wb-communications-00032-22j=100` |

## 1d. Развёртывание 2026-09-29 — 1.6.2, v3 SHADOW включён (Phase 3 live shadow)

| | |
|---|---|
| Исходник | `main` `3e0466f597a362fd4972df9c8e58be24c2cb8c4d` (PR #223); вне `app/v3`, `knowledge_v3`, `tests/v3` код = 00033 (`1e4b912`) |
| Сборка | Cloud Build `56f5a275-2807-4807-b1b6-28ef877fdf85`: `pytest` на `python:3.12-slim` → `docker build`, тег `v3shadow-3e0466f`; архив исходников: `SOURCE_SHA` = `3e0466f…`, `diff -r` с коммитом пустой |
| Образ | `…/cloud-run-source-deploy/evetis-wb-communications@sha256:dcc5ca5f53c666bb5d220d985c5f93337a4f309530108fbaf27e0f7b3c45a7ef` |
| Ревизия | `evetis-wb-communications-00034-7v4` (100 % трафика, 2026-09-29 06:36 UTC); до неё `00033-x6m` |
| Способ | `gcloud run services update --image <digest> --update-env-vars V3_SHADOW_ENABLED=true`: env 47 → 48 (добавлена только эта переменная), SA, ресурсы, timeout, concurrency, maxScale, ingress, IAM не менялись |
| Smoke | первый плановый `/poll` 2026-09-29 08:00 UTC: 200, 42 с; `v3_shadow` decided 6 / errors 0, строки `run_kind='shadow'` со снимком `ks_v3_20260928T120346_4f4121d4`; событий `mutation_*` от ревизии нет; 106 строк `LOCAL_HISTORICAL_BACKFILL` не изменились |
| Откат | `gcloud run services update-traffic evetis-wb-communications --region europe-west1 --to-revisions evetis-wb-communications-00033-x6m=100` |

## 2. Код в Git ≠ конфигурация в production

**Это главное, что нужно знать перед любым деплоем.**

Образ не пересобирался с 2026-07-23, а ревизий уже 25: ревизии 11…25 меняли
только переменные окружения (`gcloud run services update --update-env-vars`).
Поэтому `deploy/env.production.yaml` в этом каталоге — это состояние на момент
сборки, а не то, что работает сейчас.

Расхождение на 2026-09-08 (read-only снимок Cloud Run). Машиночитаемая версия —
`deploy/env.production.live.yaml`; проверяется тестом `tests/test_deploy_contract.py`
и преддеплойным гейтом `deploy/preflight_env.py`:

| Переменная | `deploy/env.production.yaml` | Живой Cloud Run | Последствие отката |
|---|---|---|---|
| `WB_PUBLISH_ENABLED` | `"false"` | **`true`** | публикация ответов на отзывы ВЫКЛЮЧИТСЯ |
| `WB_QUESTIONS_ENABLED` | `"false"` | **`true`** | опрос вопросов покупателей ОСТАНОВИТСЯ |
| `WB_QUESTION_PUBLISH_ENABLED` | `"false"` | **`true`** | публикация ответов на вопросы ВЫКЛЮЧИТСЯ |
| `COMMUNICATION_ENGINE_V2_ENABLED` | `"false"` | **`true`** | движок v2 перестанет строиться |
| `COMMUNICATION_ENGINE_V2_SHADOW_ONLY` | `"true"` | **`false`** | — |
| `COMMUNICATION_ENGINE_V2_PRIMARY` | `"false"` | **`true`** | генератор черновиков откатится на `reviews_v1` |
| `WB_QUESTIONS_FIRST_RUN_MAX` | `"20"` | `10` | больше вопросов в первом прогоне |
| `TELEGRAM_CHAT_ID` | `"302044578"` | **`-5578869057`** | согласование ответов ушло бы из рабочей группы в личный чат |
| `TELEGRAM_WEBHOOK_URL` | `""` (литерал) | из Secret Manager | ссылка на секрет заменилась бы пустой строкой, вебхук оборвался бы |

> 🔴 **`gcloud run deploy --env-vars-file deploy/env.production.yaml` перезапишет
> набор переменных целиком и молча остановит публикацию в Wildberries.**
> Именно так `DEPLOY.md` из архива сборки и предлагал деплоить.
>
> **Закрыто на Stage A Closeout (F-19), fail-closed:** перед деплоем обязателен
> `python3 deploy/preflight_env.py` — он читает живой Cloud Run, печатает каждую
> переменную, которая изменилась бы или исчезла, и возвращает ненулевой код,
> пока изменения не подтверждены явным `--accept-changes`. Недоступность живой
> конфигурации — тоже отказ, а не пропуск проверки. `DEPLOY.md` переписан так,
> что команда деплоя идёт только после гейта.
>
> Предпочтительный способ менять конфигурацию остаётся аддитивным:
> `gcloud run services update --update-env-vars …`.

Файл `deploy/env.production.yaml` намеренно оставлен байт в байт таким, каким он
был в собранном образе: приведение его к «живым» значениям — это решение
владельца о конфигурации production, а не техническая правка. Безопасность
обеспечивается гейтом, а не редактированием снимка.

Восьмое расхождение (`TELEGRAM_CHAT_ID`) нашёл сам гейт при первом же запуске —
в отчёте Stage A их было перечислено семь.

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

## 5. Terraform — принято (Stage A Closeout, 2026-09-08)

Ресурсы существовали и создавались вручную, поэтому Terraform их **принял
(import)**, а не создал заново. Выполнено `terraform apply` сохранённого плана:

```
Apply complete! Resources: 12 imported, 4 added, 4 changed, 0 destroyed.
```

Под управлением Terraform теперь: `google_cloud_run_v2_service.wb_communications`,
`google_cloud_run_v2_service_iam_member.wb_communications_public`,
`google_cloud_scheduler_job.wb_comms_poll`, `google_service_account.wb_comms`
и две его проектные роли.

Проверено после apply:
- повторный `terraform plan` → **No changes. Your infrastructure matches the configuration.**
- ревизия сервиса осталась `evetis-wb-communications-00025-rq8` (generation 25) — не заменена;
- `WB_PUBLISH_ENABLED`, `WB_QUESTIONS_ENABLED`, `WB_QUESTION_PUBLISH_ENABLED`, `COMMUNICATION_ENGINE_V2_PRIMARY` = `true`, как и до apply;
- расписание `0 8,11,14,17,20 * * *` Europe/Moscow, состояние ENABLED, URI не изменились;
- заголовок `X-Scheduler-Secret` планировщика на месте (Terraform его игнорирует).

Terraform намеренно не контролирует `template` сервиса, `build_config` и заголовки
планировщика — иначе первый же apply выровнял бы переменные окружения и остановил
публикацию.

## 6. Что НЕ делалось на Stage A

- поведение сервиса не менялось;
- новая ревизия не деплоилась;
- переменные окружения не трогались;
- ни одного тестового отзыва или ответа в Wildberries не отправлено;
- зависимости не обновлялись, рефакторинга нет — только перенос кода 1:1.
