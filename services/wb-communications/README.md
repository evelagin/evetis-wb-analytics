# EVETIS WB Communications

Автоматизация ответов на отзывы Wildberries для бренда **EVETIS**. Заменяет два
workflow n8n одним сервисом на Google Cloud Run: приём неотвеченных отзывов →
генерация проекта ответа через OpenAI → подтверждение в Telegram → публикация в
WB. Полностью изолирован от проекта **EVETIS Marketplace Analytics** (Вариант A:
тот же GCP-проект, отдельные ресурсы, имена, IAM, dataset и секреты).

> Система **не зависит от n8n** и не публикует ответы автоматически — каждый
> ответ проходит ручное подтверждение в Telegram.

## Возможности (MVP)

- `POST /poll` — тянет неотвеченные отзывы WB, генерирует ответ, шлёт карточку в
  Telegram. **Идемпотентно**: один отзыв никогда не отправляется дважды.
- Кнопки в Telegram: **✅ Опубликовать · ✏️ Изменить · 🔄 Перегенерировать ·
  ⏭ Пропустить** (+ 📋 Показать полностью для длинных отзывов).
- Настоящее редактирование в Telegram (ForceReply → `final_answer`, `ai_answer`
  сохраняется для аудита; `/cancel`, таймаут, защита от чужого пользователя).
- Атомарная публикация: `pending_approval → publishing → published/publish_failed`.
  Повторное нажатие «Опубликовать» **не публикует дважды**.
- Полная state machine: устаревшие кнопки (skip/edit/regenerate на уже
  опубликованном/пропущенном) отклоняются транзакционно.
- Edit/regenerate — заблокированные промежуточные состояния (`EDITING` /
  `REGENERATING`) с lock-token и проверкой версии: конкурентные publish/skip/
  повторный draft не могут «протолкнуть» устаревший ответ обратно в pending.
- Crash-recovery: все in-flight состояния держатся на lease; зависшее после сбоя
  безопасно восстанавливается. Прерванная публикация НЕ переотправляется
  вслепую (идемпотентность WB PATCH не подтверждена) — запрашивается ручная сверка.
- Firestore infra-ошибки (ServiceUnavailable/DeadlineExceeded/…) → transient,
  webhook отвечает 5xx, Telegram-update не теряется (есть attempts + poison-guard).
- Firestore — оперативное состояние и идемпотентность. BigQuery — история и
  аналитика: доставка событий через **Firestore outbox** (at-least-once, дедуп
  по детерминированному `event_id`; не exactly-once).
- Секреты только в Secret Manager. В коде/логах их нет.

**Безопасность эндпоинтов** (сервис публичный, чтобы Telegram достучался до
webhook): `/telegram-webhook` — секретный заголовок Telegram; `/poll` —
обязательный `X-Scheduler-Secret` (fail-closed); `/admin/*` — не регистрируются
в production без `ADMIN_TOKEN`; `/health` — без секретов.

**Публикация в WB — fail-closed:** `WB_PUBLISH_ENABLED=false` по умолчанию;
реальная отправка включается только явной переменной после сверки endpoint.

Публичные вопросы и личные чаты WB заложены в модель (`entity_type`), но в MVP не
реализованы, чтобы не задерживать запуск отзывов.

## Архитектура

```
Cloud Scheduler ──OIDC──▶ POST /poll ──▶ WB Feedbacks API (GET)
                                     ├──▶ OpenAI (Responses API)
                                     ├──▶ Firestore (claim, idempotency)
                                     ├──▶ Telegram (карточка + кнопки)
                                     └──▶ BigQuery (events)

Telegram ──webhook(secret)──▶ POST /telegram-webhook ──▶ Firestore (транзакции)
                                                     ├──▶ WB (publish answer)
                                                     └──▶ BigQuery (events)
```

Слой оркестрации (`app/services/pipeline.py`) написан как чистые функции над
набором зависимостей `Deps`, поэтому вся бизнес-логика тестируется без сети и без
GCP (`MemoryRepository` + фейковые клиенты).

## Структура

```
app/
  main.py                # FastAPI, роуты
  config.py              # env + Secret Manager (лениво)
  dependencies.py        # сборка Deps (продакшн-клиенты)
  routes/                # health, poll, telegram_webhook, admin
  services/              # wb, openai, telegram, firestore repo, bigquery, prompts, pipeline
  domain/                # models, statuses, exceptions
  utils/                 # logging (с редакцией секретов), security, text, retry
prompts/reviews/         # system_v1.txt + user_v1.txt (промпт EVETIS, версионируется)
scripts/                 # set_telegram_webhook, smoke_test, compare_openai_models
tests/                   # text, callbacks, idempotency, wb_client, openai_client
```

## Локальный запуск

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env        # заполните секреты ТОЛЬКО локально

# офлайн-проверка всей логики (без сети и секретов):
python -m scripts.smoke_test
pytest -q

# поднять сервис локально:
uvicorn app.main:app --reload --port 8080
curl localhost:8080/health
```

## Конфигурация

Несекретные параметры — переменные окружения (см. `.env.example`). Секреты —
Secret Manager: `EVETIS_OPENAI_API_KEY`, `EVETIS_WB_API_TOKEN`,
`EVETIS_TELEGRAM_BOT_TOKEN`, `EVETIS_TELEGRAM_WEBHOOK_SECRET`,
`EVETIS_SCHEDULER_SECRET`.

Модель OpenAI — переменная `OPENAI_MODEL` (по умолчанию `gpt-4.1-mini`).
Сменить модель = одна переменная окружения после A/B
(`scripts/compare_openai_models.py`).

## Деплой

Пошагово — в [`DEPLOY.md`](./DEPLOY.md).

## Важные оговорки

- **WB publish endpoint** (`WB_ANSWER_METHOD`/`WB_ANSWER_PATH`) вынесен в
  конфиг: сверьте `PATCH /api/v1/feedbacks {id,text}` с актуальным официальным
  Swagger WB перед первой реальной публикацией.
- Отзывы / публичные вопросы / личные чаты WB — три разные сущности; в один
  `/chat` не объединяются.
- Старые токены WB и Telegram, попавшие в экспорт n8n, считаются
  скомпрометированными — перевыпустите (Telegram: тот же бот, новый токен).
