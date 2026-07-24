---
name: migrating-to-gcp
description: >
  Как EVETIS переносит WB-загрузчики с Google Apps Script на Cloud Run Jobs.
  Использовать при работе над миграцией: добавление/порт загрузчика, правки
  Terraform/CI, cutover. Опирается на утверждённый дизайн, не заменяет его.
---

# Миграция на GCP (Cloud Run Jobs) — рабочий контракт

Источник истины — утверждённые дизайн-документы в `docs/`:
`MIGRATION_CLOUDRUN_DESIGN_2026-07-24_rev2.md` + `..._rev2-final-diff_2026-07-24.md`.

## Неизменяемые инварианты
- Загрузчик = **Cloud Run Job** (задача, exit 0/1), не сервис.
- Один общий TS-кодбейс и один образ; загрузчик = `node dist/cli.js <loader>`.
- Execution-guard: ключ **environment × loader_name × logical_period**; статусы
  STARTED/COMPLETE/ERROR; повтор → OK_NO_NEW / ALREADY_RUNNING / RECOVER.
- Секреты — только Secret Manager; конфиг — env; состояние — BQ.
- Аутентификация CI — WIF, без JSON-ключей.
- Terraform создаёт инфраструктуру; runtime не создаёт таблицы.
- shadow физически не пишет в prod (раздельные runtime SA + табличный IAM).
- Один digest: собираем в shadow, тот же digest промоутим в prod (не пересобираем).
- Иерархия retries: HTTP (429/5xx/timeout) / Cloud Run task (0–1) / Scheduler (1–2).
- Регион `europe-west1`; BQ job `location=EU`.

## Порядок миграции загрузчиков
stocks → REF → ads → orders → sales → nightly reconcile → **finance последним**.

## Порядок cutover (обязательно)
old trigger OFF → убедиться, что старый run завершён → new Scheduler ON.
Никакой одновременной записи Apps Script и Cloud Run в один прод-объект.

## Приёмка порта
1. Replay/fixture parity: сохранённый payload → старый нормализатор vs TS → 1:1.
2. Live shadow parity ≥5 дней (снимки ≤5 мин), ≥1 день с реальным изменением.

## Процесс
Файлы кладёт ассистент; commit/push/PR — владелец (GitHub Desktop). План и PR
проходят аудит ChatGPT до мержа. Триггеры/деплой prod — только после приёмки.
