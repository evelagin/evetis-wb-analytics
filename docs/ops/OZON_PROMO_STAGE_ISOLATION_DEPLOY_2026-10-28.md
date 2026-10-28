# Ozon акции: изоляция этапов наблюдателя — план выпуска для EVETIS (только по OWNER ACK)

Статус: **НЕ развёрнуто.**

## Почему отдельная релизная ветка

Production EVETIS: Jobs `ozon-runtime-*` на `ozon-runtime-ingest@sha256:24e3c6d6715fa7b73d30b4270f9863d2b8680b4b02d4874ff1ea12b4fd90fa1b`
(Terraform `infra/terraform/ozon_ingestion.tf`, `local.ozon_runtime_image`). Провенанс доказан 28.10.2026:
Cloud Build `7e1fe4e2-79d1-46d6-a11e-be32f8d2eb1b` (22.09, тег `:08dfb88`); архив исходника сборки в
`gs://…_cloudbuild/source/1790088788.913899-….tgz` побайтно равен `git archive 08dfb88 pipelines/ozon/runtime`.

После 08dfb88 в `pipelines/ozon` 43 коммита, ≥36 — мультиарендность (Heldi). Собирать EVETIS-образ из main —
значит выкатить их без отдельных ворот. Поэтому образ EVETIS собирается из ветки
`release/evetis-ozon-runtime-08dfb88-promo` = 08dfb88 + только `runtime/promo.py` и его тест (тот же патч, что в этом PR).

## Что меняется в поведении

- Этапы после списка акций независимы: участники, кандидаты, автодобавление (запланированное / возможное), marketing.
- Снятый метод (`404`, `400 code 9`) — этап `UNSUPPORTED`; `403/429/5xx`/потолок страниц — `FAILED`. Ни то ни другое
  не «товаров нет»: строки этапа не пишутся, счётчики манифеста — `NULL`.
- Этап атомарен: отказ на одной акции отбрасывает строки этапа по всем акциям.
- Манифест `OZON_PROMO_OBSERVATIONS`: `PARTIAL`, `error_code='PARTIAL_STAGES'`, причина по этапам; успешный путь — байт-в-байт прежний.
- Прогон с `PARTIAL` остаётся красным (`OZON_INGESTION_RUNS.status = FAILED`, причина `PromoPartial: PARTIAL_STAGES …`) —
  ложного зелёного нет. История акций (`V_OZON_PROMO_OBSERVATION_HISTORY`) берёт только COMPLETE/REUSED, поэтому
  частичный снимок потребителям не виден и «пустое автодобавление» не возникает.
- Ожидаемое после выпуска до перехода на v2: `auto_add_scheduled=UNSUPPORTED(404)`, остальные этапы — свежие данные в RAW.

## Выпуск (по ACK)

1. `gcloud builds submit pipelines/ozon/runtime --tag europe-west1-docker.pkg.dev/project-fa311fc0-4d87-4781-986/cloud-run-source-deploy/ozon-runtime-ingest:<sha релизной ветки>`
   из чистого checkout релизной ветки; записать digest.
2. Только `ozon-runtime-promo`: `gcloud run jobs update ozon-runtime-promo --image <digest>`. Остальные три Ozon Job
   остаются на 24e3c6d6 (изменение затрагивает только сущность promo, но выпуск — минимальный).
3. PR в `infra/terraform/ozon_ingestion.tf`: отдельный digest для promo (иначе следующий `terraform apply` откатит).
4. Приёмка по данным: в ближайший слот `OZON_PROMO_OBSERVATIONS` — `PARTIAL` с перечнем этапов, свежие строки
   `RAW_OZON_PROMO_ACTIONS/PRODUCTS/PRODUCT_MARKETING` на этот observation_id, 0 строк SCHEDULED; ошибка Job — `PromoPartial`.
5. Откат: `gcloud run jobs update ozon-runtime-promo --image …@sha256:24e3c6d6…`. Данные не мигрируются, записи
   PARTIAL-снимков append-only и не читаются историей.

## Не входит (следующие шаги)

- Переход на `/v2/actions/auto-add/products/list` и `/v2/actions/products|candidates` (dev.ozon.ru, статья 563,
  17.09.2026; v1 отключены 19.10). Схема ответа v2 (формат Money, пагинация offset/limit) не проверена на живом
  ответе — нужен захват образца ответа по ACK, затем адаптер с тестом на нём.
- `evetis_mart.V_PROMO_OBSERVABILITY_CURRENT`: Ozon показан `OBSERVED/COMPLETE` при возрасте наблюдения 14.10 —
  отдельное исправление статуса свежести.
- История 14–28.10 по автодобавлению не восстанавливается: метод отдаёт только текущее состояние.
