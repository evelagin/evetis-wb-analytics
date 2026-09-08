# evetis-wb-analytics

Управленческая аналитика бренда EVETIS на Wildberries и Ozon: сбор данных из API
маркетплейсов, витрины в BigQuery, дашборды в Metabase и операционный контроль.

Актуально на 2026-09-08 (Stage A). Историю решений см. в `CHANGELOG.md`.

---

## Из чего состоит система

```
Wildberries API ──┬─► Apps Script (apps-script/)      ─┐
                  │   заказы · продажи · финансы ·     │
                  │   реклама · остатки · справочник   │
                  │                                    ├─► BigQuery
                  ├─► Cloud Run Jobs (cloud/)          │   wb_raw → wb_mart
                  │   витрина · цены · тарифы          │   evetis_ref · wb_ops
                  │                                    │        │
                  └─► Cloud Run Service                │        ▼
                      (services/wb-communications/)    │   Metabase
                      ответы на отзывы и вопросы ──────┘   3 дашборда, 57 карточек
                      ⚠️ единственный канал ЗАПИСИ в WB

Ozon Seller API ────► Cloud Run Jobs (pipelines/ozon/) ──► ozon_raw → ozon_mart
```

**Apps Script — это production, а не legacy.** Заказы, продажи, финансы, реклама и
остатки WB грузятся именно им; Cloud Run отвечает за витрину, цены и тарифы.
Подробности — `docs/CURRENT_PROJECT_STATE.md`.

## Каталоги

| Каталог | Что там |
|---|---|
| `apps-script/` | Загрузчики WB API и DDL RAW-view. Production. |
| `cloud/` | TypeScript Cloud Run Jobs: витрина, наблюдатель цен, тарифы, shadow-остатки. |
| `services/wb-communications/` | Сервис ответов на отзывы и вопросы WB (Python/FastAPI). |
| `pipelines/ozon/` | Python-загрузчики Ozon: runtime и разовый bootstrap. |
| `sql/` | Авторитетные DDL витрин, FACT, семантического слоя, справочников, ops. |
| `infra/terraform/` | Cloud Run, Scheduler, IAM, service accounts, секреты. |
| `metabase/` | Версионированный снимок дашбордов и карточек (второй слой восстановления). |
| `docs/` | Дизайн-документы и записи этапов. |
| `tools/` | Экспорт снимка Metabase, скрипты отката этапов. |
| `.github/workflows/` | CI, деплой shadow → prod по digest, Terraform, управление расписаниями. |

## Быстрые проверки

```bash
cd cloud && npm run typecheck && npm run lint && npm test
```

```bash
cd infra && terraform fmt -check -recursive
```

```bash
docker run --rm -v "$PWD":/src -w /src python:3.12-slim bash -lc "pip install -q -r pipelines/ozon/requirements-dev.txt && python -m pytest -q pipelines/ozon/tests"
```

## Что важно знать до первого изменения

1. `CLAUDE.md` — правила работы над проектом.
2. `docs/CURRENT_PROJECT_STATE.md` — фактическое состояние production.
3. Себестоимость: авторитетный источник `V_PRODUCT_COGS_EFFECTIVE` —
   `sql/ref/stage3_4b1_management_landed_cogs.sql`. Более ранние файлы этапов
   защищены гейтами и упадут, если попытаться понизить контракт.
4. Ни один показатель дашбордов не называется прибылью: контракт метрик —
   `PRE_COGS` и `AFTER_PRODUCT_COGS`, в них нет налогов, OPEX и фулфилмента.

## Исторические документы

`ARCHITECTURE.md`, `DATA_MODEL.md`, `PROJECT_RULES.md` описывают первоначальную
архитектуру на Google Sheets и сохранены как исторические. Действующей системе
они не соответствуют.
