# DEPLOY — EVETIS WB Communications

Вариант A: тот же GCP-проект, что и Marketplace Analytics, полная изоляция ресурсов.
Все команды содержат плейсхолдеры — **реальные секреты вставляются только в
Secret Manager**, не в код и не в git.

> **Публикация в WB по умолчанию ВЫКЛЮЧЕНА** (`WB_PUBLISH_ENABLED=false`). Не
> включайте её до сверки endpoint по live Swagger. Реального деплоя-с-публикацией
> не делать без явного подтверждения.

```bash
# ── 0. Переменные (подставьте свои) ────────────────────────────────────────
export PROJECT_ID="project-fa311fc0-4d87-4781-986"
export REGION="europe-west1"          # Cloud Run region
export BQ_LOCATION="EU"                # BigQuery multi-region
export FS_LOCATION="eur3"             # Firestore location (НЕ равно BQ "EU")
export SERVICE="evetis-wb-communications"
export SA="evetis-wb-comms"
export SA_EMAIL="${SA}@${PROJECT_ID}.iam.gserviceaccount.com"

gcloud config set project "$PROJECT_ID"
```

## Обязательные секреты (fail-closed)

Сервис не работает без них:
- **`EVETIS_SCHEDULER_SECRET`** — обязателен для `/poll` (без него эндпоинт возвращает 503).
- **`EVETIS_TELEGRAM_BOT_TOKEN`** — обязателен для отправки карточек и кнопок (перевыпустить в BotFather).
- `EVETIS_TELEGRAM_WEBHOOK_SECRET`, `EVETIS_WB_API_TOKEN`, `EVETIS_OPENAI_API_KEY`.

`WB_PUBLISH_ENABLED` остаётся **false** до сверки WB publish endpoint по live Swagger.
Реальный деплой с публикацией — только после явного подтверждения.

## 1. Включить API

```bash
gcloud services enable \
  run.googleapis.com cloudscheduler.googleapis.com firestore.googleapis.com \
  bigquery.googleapis.com secretmanager.googleapis.com \
  artifactregistry.googleapis.com cloudbuild.googleapis.com
```

## 2. Service account + минимальные роли

```bash
gcloud iam service-accounts create "$SA" --display-name="EVETIS WB Communications"

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${SA_EMAIL}" --role="roles/datastore.user"
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${SA_EMAIL}" --role="roles/bigquery.jobUser"
```

## 3. Секреты (значения вставляете вы)

Всего пять: WB, OpenAI, Telegram webhook secret, **scheduler secret**, Telegram bot token.

```bash
# Уже созданы: EVETIS_WB_API_TOKEN, EVETIS_OPENAI_API_KEY, EVETIS_TELEGRAM_WEBHOOK_SECRET.
# Нужно создать ещё два:
printf '%s' 'НОВЫЙ_TELEGRAM_BOT_TOKEN' | gcloud secrets create EVETIS_TELEGRAM_BOT_TOKEN --data-file=-
openssl rand -hex 32                    | gcloud secrets create EVETIS_SCHEDULER_SECRET   --data-file=-

for S in EVETIS_OPENAI_API_KEY EVETIS_WB_API_TOKEN EVETIS_TELEGRAM_BOT_TOKEN \
         EVETIS_TELEGRAM_WEBHOOK_SECRET EVETIS_SCHEDULER_SECRET; do
  gcloud secrets add-iam-policy-binding "$S" \
    --member="serviceAccount:${SA_EMAIL}" --role="roles/secretmanager.secretAccessor"
done
```

## 4. Firestore — проверить перед созданием

Firestore location задаётся ОДИН раз и **не** равен BigQuery `EU`. Сначала
проверяем, есть ли база; создаём только если её нет (без «молчаливого» `|| true`):

```bash
if gcloud firestore databases list --format='value(name)' | grep -q '.'; then
  echo "Firestore database already exists — location НЕ меняем."
else
  gcloud firestore databases create --location="$FS_LOCATION"
fi
```

## 5. BigQuery dataset/таблицы (best-effort хелпер)

```bash
GCP_PROJECT_ID="$PROJECT_ID" BIGQUERY_LOCATION="$BQ_LOCATION" \
python -c "from app.config import get_settings; from app.services.bigquery_repository import BigQueryRepository; BigQueryRepository(get_settings()).ensure_dataset_and_tables()"

bq add-iam-policy-binding \
  --member="serviceAccount:${SA_EMAIL}" --role="roles/bigquery.dataEditor" \
  "${PROJECT_ID}:evetis_communications"
```

## 6. Деплой Cloud Run

Сервис **публично доступен** (`--allow-unauthenticated`), потому что Telegram
должен достучаться до `/telegram-webhook`. Защита распределена по эндпоинтам:

| Endpoint | Защита |
|---|---|
| `/telegram-webhook` | заголовок `X-Telegram-Bot-Api-Secret-Token` (= `EVETIS_TELEGRAM_WEBHOOK_SECRET`) |
| `/poll` | обязательный заголовок `X-Scheduler-Secret` (= `EVETIS_SCHEDULER_SECRET`), fail-closed |
| `/admin/*` | в production не регистрируются без `ADMIN_TOKEN`; при наличии — требуют `X-Admin-Token` |
| `/health` | без секретов |

```bash
gcloud run deploy "$SERVICE" \
  --source . --region "$REGION" --service-account "$SA_EMAIL" \
  --allow-unauthenticated \
  --set-env-vars "GCP_PROJECT_ID=${PROJECT_ID},GCP_REGION=${REGION},BIGQUERY_LOCATION=${BQ_LOCATION},FIRESTORE_LOCATION=${FS_LOCATION},OPENAI_MODEL=gpt-4.1-mini,PROMPT_VERSION=reviews_v1,TELEGRAM_CHAT_ID=302044578,TELEGRAM_ALLOWED_USER_IDS=302044578,WB_PUBLISH_ENABLED=false,ENVIRONMENT=production"

export SERVICE_URL="$(gcloud run services describe "$SERVICE" --region "$REGION" --format='value(status.url)')"
echo "$SERVICE_URL"
```

> Чтобы временно включить `/admin/*` для диагностики — добавьте
> `ADMIN_TOKEN=<случайная строка>` в `--set-env-vars` и шлите её в заголовке
> `X-Admin-Token`. По завершении диагностики уберите переменную.

## 7. Cloud Scheduler → /poll (через shared secret header)

```bash
gcloud scheduler jobs create http evetis-wb-reviews-poll \
  --location "$REGION" \
  --schedule "*/10 * * * *" \
  --uri "${SERVICE_URL}/poll" \
  --http-method POST \
  --headers "X-Scheduler-Secret=$(gcloud secrets versions access latest --secret=EVETIS_SCHEDULER_SECRET)"
```

Секрет попадает в конфигурацию задания Scheduler (доступна только владельцам
проекта), а не в код. Интервал (`--schedule`) меняется здесь.

## 8. Telegram webhook

```bash
TELEGRAM_WEBHOOK_URL="${SERVICE_URL}/telegram-webhook" GCP_PROJECT_ID="$PROJECT_ID" \
python -m scripts.set_telegram_webhook
```

## 9. Smoke-тест на проде (без включения публикации)

```bash
curl "${SERVICE_URL}/health"

# poll с обязательным секретом:
curl -X POST -H "X-Scheduler-Secret: $(gcloud secrets versions access latest --secret=EVETIS_SCHEDULER_SECRET)" \
  "${SERVICE_URL}/poll"
```

## Порядок проверки перед первым реальным ответом

1. `python -m scripts.smoke_test` и `pytest -q` — зелёные.
2. (Опц.) включить `ADMIN_TOKEN`, прогнать `/admin/test-openai`, `/admin/test-wb`,
   `/admin/test-telegram`, затем убрать токен.
3. Один `poll` → карточка приходит в Telegram; «Изменить»/«Перегенерировать» работают.
4. **Сверить WB publish endpoint** с live Swagger. Дефолт (по текущей WB
   OpenAPI-спеке) — первый ответ на отзыв `POST /api/v1/feedbacks/answer`,
   тело `{"id","text"}`, токен в `Authorization` без «Bearer». Если Swagger
   отличается — переопределить `WB_ANSWER_METHOD`/`WB_ANSWER_PATH` через
   `--update-env-vars` (менять код не нужно). Токен WB должен иметь доступ на
   запись в категории «Вопросы и отзывы».
5. Только после сверки — включить публикацию: `--update-env-vars WB_PUBLISH_ENABLED=true`.
6. Нажать «Опубликовать» на одном отзыве → проверить в кабинете WB.
7. Повторный `poll` — дубля нет; повторное нажатие — второй публикации нет;
   кнопки на уже опубликованном/пропущенном — «отзыв уже обработан».

## Вопросы покупателей WB (entity_type=question)

Вопросы — отдельная сущность со своим WB-эндпоинтом (`GET`/`PATCH /api/v1/questions`)
и **отдельными гейтами**. Контур отзывов не затрагивается. Порядок ввода:

**Шаг 1. Включить приём вопросов (публикация ответов ВЫКЛЮЧЕНА).** Реальные
вопросы начнут приходить в Telegram, но кнопка «Опубликовать» под ними будет
закрыта своим гейтом. Отзывы продолжают публиковаться как раньше.

```bash
gcloud run services update evetis-wb-communications --region=europe-west1 \
  --update-env-vars WB_QUESTIONS_ENABLED=true,WB_QUESTION_PUBLISH_ENABLED=false
```

На первом запуске обрабатывается не более `WB_QUESTIONS_FIRST_RUN_MAX` (20) новых
вопросов за поллинг — исторический бэклог не завалит Telegram, остальные подтянутся
на следующих поллингах (сначала самые свежие; ничего не теряется).

**Шаг 2. Проверить в песочнице WB** (по желанию). Тестовые вопросы создаются через
`POST /api/v1/test/make/questions` и живут 5 дней — можно прогнать полный цикл, не
трогая реальную карточку.

**Шаг 3. Сверить публикацию и включить её отдельно.** Ответ уходит как
`PATCH /api/v1/questions` с телом `{"id","text","state":"wbRu"}` (`wbRu` = ответ
виден покупателям; `none` = покупатель получит ответ, но он не будет опубликован на
сайте). Тот же токен категории «Вопросы и отзывы». После сверки:

```bash
gcloud run services update evetis-wb-communications --region=europe-west1 \
  --update-env-vars WB_QUESTION_PUBLISH_ENABLED=true
```

Откат вопросов — по одному флагу: `WB_QUESTIONS_ENABLED=false` (приём) или
`WB_QUESTION_PUBLISH_ENABLED=false` (только публикация). Вопросы требуют
`COMMUNICATION_ENGINE_V2_ENABLED=true` (у них свой шаблон в движке).

## Откат / пауза

```bash
gcloud scheduler jobs pause evetis-wb-reviews-poll --location "$REGION"
gcloud run services update-traffic "$SERVICE" --region "$REGION" --to-revisions PREV=100
# аварийно отключить публикацию, не трогая приём:
gcloud run services update "$SERVICE" --region "$REGION" --set-env-vars WB_PUBLISH_ENABLED=false
```

## Communication Engine v2 — shadow rollout

v2 работает **только в теневом режиме**: параллельно генерирует ответ, прогоняет
валидаторы и пишет строку сравнения в отдельную таблицу
`communication_engine_shadow`. Он **никогда не публикует в WB** и **не шлёт второе
сообщение в Telegram**. Ошибка v2 изолирована и не ломает `reviews_v1`.

**Шаг 1. Безопасный деплой (v2 ВЫКЛЮЧЕН).** Флаги в `deploy/env.production.yaml`
уже стоят на `false`/`true`, поэтому деплой не включает v2 автоматически:

```bash
# деплой с v2 OFF (поведение полностью прежнее)
gcloud run deploy "$SERVICE" \
  --source . --region "$REGION" --service-account "$SA_EMAIL" \
  --allow-unauthenticated \
  --env-vars-file deploy/env.production.yaml
```

**Шаг 2. Явная миграция схемы shadow-таблицы — ОБЯЗАТЕЛЬНО перед включением тени.**
Идемпотентно: создаёт таблицу, если её нет, и **добавляет `shadow_id`**, если
таблица осталась от версии 1.2.0 (23 колонки без `shadow_id`). Без этого шага
`insert_shadow` может отклоняться BigQuery как запись с неизвестным полем.
Команда fail-fast — при проблеме со схемой возвращает **non-zero** и деплой-пайплайн
должен остановиться:

```bash
GCP_PROJECT_ID="$PROJECT_ID" BIGQUERY_LOCATION="$BQ_LOCATION" \
python -m scripts.migrate_shadow_table
# stdout при успехе: [shadow-migration] OK — {'table_created': ..., 'columns_added': [...]}
# non-zero exit -> схему НЕ трогать дальше, тень НЕ включать, разобраться с BigQuery
```

> Миграция запускается ОТДЕЛЬНОЙ pre-deploy командой, а НЕ на каждом `/poll`.
> `/poll` схему BigQuery не меняет.

**Шаг 3. Включить тень ОТДЕЛЬНО, уже после миграции** (осознанное действие, не
часть выката). Меняет только один флаг, не трогая остальное:

```bash
gcloud run services update "$SERVICE" --region "$REGION" \
  --update-env-vars COMMUNICATION_ENGINE_V2_ENABLED=true,COMMUNICATION_ENGINE_V2_SHADOW_ONLY=true
```

> `COMMUNICATION_ENGINE_V2_SHADOW_ONLY` — **fail-closed** для shadow-режима:
> `false` без `_V2_PRIMARY=true` не поддерживается (движок не строится + CRITICAL-лог).

**Шаг 4 (опционально). Перевести v2 в PRIMARY — генерацию черновиков движком.**
Только после того, как shadow-данные вас устроили. Черновик в Telegram начинает
писать выверенная база + валидаторы (вместо `reviews_v1`); если валидатор что-то
поймал или товар не распознан — на карточке появляется строка «⚠️ Проверка v2».
Ответ по-прежнему подтверждает человек, публикация в WB остаётся выключенной. При
сбое движка на конкретном отзыве — автоматический откат к `reviews_v1` (не хуже
текущего). Включается одним аддитивным флагом:

```bash
gcloud run services update "$SERVICE" --region "$REGION" \
  --update-env-vars COMMUNICATION_ENGINE_V2_ENABLED=true,COMMUNICATION_ENGINE_V2_PRIMARY=true
```

Откат к прежней генерации (reviews_v1) — тоже один флаг:

```bash
gcloud run services update "$SERVICE" --region "$REGION" \
  --update-env-vars COMMUNICATION_ENGINE_V2_PRIMARY=false
```

> В PRIMARY отдельный shadow-прогон не запускается (движок и так — основной ответ).
> `WB_PUBLISH_ENABLED` остаётся `false` независимо от этого флага.

**Откат тени** (мгновенно возвращает прежнее поведение, v2 больше не вызывается):

```bash
gcloud run services update "$SERVICE" --region "$REGION" \
  --update-env-vars COMMUNICATION_ENGINE_V2_ENABLED=false
```

Проверка накопленных теневых данных (v2 vs reviews_v1), без влияния на прод:

```sql
SELECT review_id, product_resolution_method, marketplace_resolution_status,
       needs_manual_moderation, validation_passed, v2_usable, v2_latency_ms
FROM `project-fa311fc0-4d87-4781-986.evetis_communications.communication_engine_shadow`
ORDER BY shadow_at DESC
LIMIT 100;
```

> `WB_PUBLISH_ENABLED` остаётся `false` независимо от v2. Включение тени НЕ
> открывает публикацию и НЕ меняет модерацию — это чистое наблюдение.
