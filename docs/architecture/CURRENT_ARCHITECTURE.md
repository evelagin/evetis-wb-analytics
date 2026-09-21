# Текущая архитектура EVETIS Analytics

**Снимок:** 2026-09-21, собран только чтением против production `project-fa311fc0-4d87-4781-986` (EU)
и рабочего дерева на `origin/main` = `821a45e`.
**Машиночитаемая форма:** [`system_inventory.json`](system_inventory.json) — им же порождены
[`SYSTEM_INVENTORY.md`](SYSTEM_INVENTORY.md) и [`DATA_LINEAGE.md`](DATA_LINEAGE.md).

Этот документ отвечает на вопрос «как система устроена **сейчас**». Чем она должна стать —
[`TARGET_ARCHITECTURE.md`](TARGET_ARCHITECTURE.md). Исторические `ARCHITECTURE.md`,
`DATA_MODEL.md`, `PROJECT_RULES.md` в корне репозитория описывают первоначальный
Google-Sheets-контур и верны только как история.

---

## 1. Что это за система

Управленческая аналитика одного продавца (бренд EVETIS) на двух маркетплейсах. Собирает
заказы, продажи, возвраты, финансовые отчёты, рекламу, остатки, тарифы, цены и себестоимость;
выдаёт суточную экономику по SKU, сводный результат периода, операционный контур фулфилмента
и рекомендации по поставкам.

Масштаб — не «большие данные», а **высокая семантическая сложность**: 283 объекта BigQuery,
из них 150 вью и 486 рёбер зависимостей, при ~1,0 ГБ хранилища, 1,67 млн строк и ~25 активных SKU. Стоимость
ошибки здесь не в вычислениях, а в неверной трактовке денежной строки маркетплейса.

## 2. Пять исполняемых сред

Система работает не в одном рантайме, а в пяти. Это главный факт её архитектуры: любая задача,
затрагивающая «загрузку», должна сначала ответить, **какая из сред** её выполняет.

| Среда | Что делает | Где код | Контроль |
|---|---|---|---|
| **Apps Script** | Production-загрузка WB: заказы, продажи и возвраты, финансы, реклама, остатки, синхронизация справочника | `apps-script/**` (108 `.gs`) | ⛔ вне CI, без тестов, развёртывание через браузер |
| **Cloud Run Jobs (TS)** | Витрина `MART_SKU_DAILY`, наблюдатель цен, тарифы, воронка, платное хранение, Unitka Engine, shadow-остатки | `cloud/src/**` | CI: typecheck, lint, vitest, docker build; деплой по digest |
| **Cloud Run Jobs (Py)** | Загрузка Ozon Seller/Performance API (11 сущностей) | `pipelines/ozon/**` | CI: compileall, pytest, docker build |
| **Cloud Run Service** | Ответы на отзывы и вопросы WB — **единственный канал записи** во внешний маркетплейс | `services/wb-communications/**` | CI + обязательный преддеплойный гейт `deploy/preflight_env.py` |
| **BigQuery процедуры** | Сборка витрин и слоёв: `sp_build_mart_sku_daily`, `sp_build_executive_v2_daily`, `sp_build_sku_performance_v2_daily`, `sp_ct_refresh_daily`, `sp_publish_unitka_cogs`, `sp_evaluate_pipeline_health`, `sp_ops_*` | `sql/**` | частично; каноническое Git-определение есть только у вью `ozon_mart` и `evetis_mart` |

Оркестратор один — **Cloud Scheduler** (20 заданий). Половина из них дёргает Cloud Run Job,
половина — напрямую BigQuery `jobs.insert` с телом `CALL ...`. Плюс невидимый отсюда шестой
планировщик: **триггеры Apps Script**, чьё расписание задано в коде и подтверждено только
наблюдением прогонов (`OPS_PIPELINE_REGISTRY.cadence_definition_source = CODE`).

## 3. Поток данных

```
WB API ──Apps Script──────────────┐
WB API ──Cloud Run (TS)───────────┤
                                  ├──► wb_raw (42 табл., 35 вью)
Google Sheets (справочник) ───────┘            │
                                               ├──► wb_mart (27 табл., 70 вью) ──► Metabase
Ozon Seller/Performance ──Cloud Run (Py)──► ozon_raw (15 табл.) ──► ozon_mart (13 вью)
                                               │                          │
Ручной ввод владельца ───────────────► evetis_ref (27 табл.) ◄────────────┘
                                               │
                                       evetis_mart.FACT_SKU_DAILY  (нейтральный суточный факт)
                                               │
                                       evetis_ops (журнал остатков ФФ) ──► Google Sheets (лист операций)
                                       wb_ops   (реестр конвейеров, здоровье, инциденты)
```

**Нейтральный слой `evetis_mart` появился 2026-09-20** (SCALE 1) и пока имеет один объект:
`FACT_SKU_DAILY`, зерно `fact_date × marketplace × internal_sku`. Он не считает экономику
заново — он приводит `wb_mart.SKU_PERFORMANCE_V2_DAILY` и `ozon_mart.FCT_OZON_SKU_PNL_DAILY`
к одному контракту. Это первый и пока единственный кирпич будущей канонической модели.

## 4. Изоляция маркетплейсов

WB и Ozon — два жёстко разделённых домена. Общий только `evetis_ref` (товарный мастер,
соответствие SKU, себестоимость, составы наборов). Правило закреплено машинно:
`EXTERNAL_DATASET_POLICY` в `tools/validate_current_sql.py` не даёт объекту `ozon_mart`
сослаться на `wb_*`, а нейтральному `evetis_mart` — читать RAW любой площадки.

## 5. Наблюдаемость: что есть

- `wb_ops.OPS_PIPELINE_REGISTRY` — 16 конвейеров с криминальностью, SLA свежести, порогом
  устаревания, источником журнала прогонов и признаками `is_snapshot_only` / `is_backfillable`
  / `missed_run_data_loss`. Это лучшая часть текущей наблюдаемости: реестр честно отличает
  «пропуск можно добрать» от «сутки потеряны навсегда».
- `wb_ops.sp_evaluate_pipeline_health` — детектор, запускается каждые 3 часа, ведёт
  `OPS_HEALTH_STATE`, `OPS_ALERT_EVENT`, `OPS_INCIDENT` и вью `V_OPS_CURRENT_HEALTH`.
- `ozon_raw.OZON_INGESTION_RUNS` — журнал прогонов Ozon по сущностям (статус, строки, ошибки).
- Журналы сборок: `wb_mart.MART_RUNS`, `EXECUTIVE_V2_BUILD_LOG`, `SKU_PERFORMANCE_V2_BUILD_LOG`,
  `wb_ops.UNITKA_ENGINE_RUNS`, `wb_raw.LOADER_RUNS` / `INGEST_RUNS` / `FINANCE_LOADER_RUNS`.

## 6. Наблюдаемость: чего нет

Измерено 2026-09-21 против `OPS_PIPELINE_REGISTRY` и `OPS_HEALTH_STATE`:

- **11 из 16 зарегистрированных конвейеров не имеют ни одной проверки здоровья**, в том числе
  оба CRITICAL — `orders` и `sales`. Проверки существуют только для `mart`, `finance`,
  `ads_daily`, `ads_query_bids`, `ads_query_stats` (плюс сердцебиение самого детектора).
- **Ozon не представлен в реестре вообще.** 11 сущностей грузятся ежедневно и ведут собственный
  журнал, но платформенный детектор здоровья о них не знает. `ozon_mart.V_OZON_MART_FRESHNESS`
  существует — его никто не вычисляет.
- Нет единого статуса платформы: ответ на вопрос «система здорова?» собирается вручную из
  нескольких вью и журналов.
- Доставки оповещений нет: `OPS_ALERT_EVENT` наполняется, но никуда не уходит.

## 7. Контроль соответствия Git и production

Production дважды опережал репозиторий (`REF_COST_MAP`, `V_PRODUCT_COGS_EFFECTIVE`), поэтому
в сентябре 2026 был построен канонический слой:

- `sql/current/<dataset>/<OBJECT>.sql` — одно текущее определение на объект + `MANIFEST.json`
  с хешами тела, схемы и описания (`canonical_hash_v1`);
- `tools/validate_current_sql.py` — 18 офлайн-проверок контракта (C1–C18), в CI `sql-current`;
- `tools/verify_current_sql_live.py` — сверка с production **только чтением**, статусы
  `MATCH` / `*_DRIFT` / `METADATA_UNPROVEN` / `PENDING_*`.

**Покрытие — 14 вью из 150.** Для остальных 136 авторитетным определением остаётся сам
production: изменение в BigQuery мимо Git не будет замечено ни человеком, ни агентом.

## 8. Доставка и инфраструктура

- Terraform: 132 ресурса, `infra/terraform/**`, apply — только через workflow `infra` с ручным
  подтверждением окружения.
- Образы: сборка в `deploy-shadow`, промоушен в prod **тем же digest**. Проверено: все 14
  Cloud Run Jobs закреплены по digest, ни одного mutable-тега.
- Разделение прав: отдельные SA для plan и apply, WIF вместо ключей.
- CI: `ci.yml` (cloud / wb-communications / ozon / infra), `sql-current.yml`. Оба без
  облачных креденшелов.

## 9. Потребители

Metabase (сервисная учётка `metabase-read-only@`) за 30 суток читал **165 объектов**. Топ:
`V_DASH_KPI_DAILY`, `V_DASH_EXECUTIVE_V2_DAILY`, `V_DASH_SKU_DAILY`,
`V_DASH_SKU_PERFORMANCE_V2_DAILY`, `V_DASH_FINANCE_CORRECTED_DAILY`. Второй потребитель —
Google Sheets: контракт листа операций (`evetis_ops.V_OPS_SHEET_*`) и книга «Юнитка».

Объекты `ozon_mart`, кроме двух новых от SCALE 1, последний раз читались 2026-09-06 — дашбордов Ozon нет; его экономика
сегодня доходит до владельца через разовые выгрузки, а не через витрину.

## 10. Границы и опасные места

- `services/wb-communications` открыт в интернет (F-18 в `docs/ops/SECURITY_BACKLOG.md`) и
  блокирует любую новую автоматизацию с правом записи, включая репрайсер.
- Живая конфигурация этого сервиса расходится с `deploy/env.production.yaml`: деплой с
  `--env-vars-file` **молча** выключит публикацию в WB.
- `Apps Script wbAdsBqCreateViews()` запускать нельзя — функция безусловно пересобирает
  `V_ADV_COSTS` и откатит cutover рекламного биллинга.
- Обычный `terraform apply` из `main` предложит удалить ресурсы загрузчика воронки: они
  попали в production из feature-веток. Допустимы только целевые apply с просмотренным планом.
- Рекламный расход в витрине SKU — **атрибуция**, не биллинг (расхождение +1,76 % за
  апрель–сентябрь 2026). Контракт: `docs/FIN_CONTRACT_V2_2026-09-16.md`.
