# Ozon акции: изоляция этапов + методы v2 — выпуск для EVETIS (только по OWNER ACK)

Статус: **НЕ развёрнуто.**

## Почему отдельная релизная ветка

Production EVETIS: Jobs `ozon-runtime-*` на `ozon-runtime-ingest@sha256:24e3c6d6715fa7b73d30b4270f9863d2b8680b4b02d4874ff1ea12b4fd90fa1b`
(Terraform `infra/terraform/ozon_ingestion.tf`, `local.ozon_runtime_image`). Провенанс доказан 28.10.2026:
Cloud Build `7e1fe4e2-79d1-46d6-a11e-be32f8d2eb1b` (22.09, тег `:08dfb88`); архив исходника сборки в
`gs://…_cloudbuild/source/1790088788.913899-….tgz` побайтно равен `git archive 08dfb88 pipelines/ozon/runtime`.

После 08dfb88 в `pipelines/ozon` 43 коммита, ≥36 — мультиарендность (Heldi). Собирать EVETIS-образ из main —
значит выкатить их без отдельных ворот. Поэтому образ EVETIS собирается из ветки
`release/evetis-ozon-runtime-08dfb88-promo` = 08dfb88 + только `runtime/promo.py` (тот же патч, что в этом PR), тесты,
проба и release gates. Порядок выпуска и приёмка — `pipelines/ozon/RELEASE_EVETIS_PROMO.md` на релизной ветке (единственный
источник; ниже — только поведение).

## Что меняется в поведении

- Этапы после списка акций независимы: участники, кандидаты, автодобавление (запланированное / возможное), marketing.
- Снятый метод (`404`, `400 code 9`) — этап `UNSUPPORTED`; `403/429/5xx`/потолок страниц — `FAILED`. Ни то ни другое
  не «товаров нет»: строки этапа не пишутся, счётчики манифеста — `NULL`.
- Этап атомарен: отказ на одной акции отбрасывает строки этапа по всем акциям.
- Манифест `OZON_PROMO_OBSERVATIONS`: `PARTIAL`, `error_code='PARTIAL_STAGES'`, причина по этапам; успешный путь — байт-в-байт прежний.
- Прогон с `PARTIAL` остаётся красным (`OZON_INGESTION_RUNS.status = FAILED`, причина `PromoPartial: PARTIAL_STAGES …`) —
  ложного зелёного нет. История акций (`V_OZON_PROMO_OBSERVATION_HISTORY`) берёт только COMPLETE/REUSED, поэтому
  частичный снимок потребителям не виден и «пустое автодобавление» не возникает.
- Методы v2 (dev.ozon.ru, ст. 563; docs.ozon.ru/api/seller, сверено 28.10): `/v2/actions/products|candidates` (last_id),
  `/v2/actions/auto-add/products/list|candidates` (offset, `auto_add_date` из `/v1/actions`); ответ без `result`;
  цены Money → `_money()` (без него все цены молча NULL); у auto-add/candidates товар — `id`; `add_mode` bool → строка.
- Если живой ответ v2 разойдётся с документацией — этап станет `FAILED`, снимок `PARTIAL`, прогон красный; ложного
  «нет товаров» нет. Поэтому до сборки — read-only проба формы ответа (шаг 1 манифеста).

## Выпуск (по ACK)

`pipelines/ozon/RELEASE_EVETIS_PROMO.md` (ветка `release/evetis-ozon-runtime-08dfb88-promo`): проба v2 → сборка + G6 →
только `ozon-runtime-promo` → приёмка по данным → откат на 24e3c6d6. Terraform образ Ozon Job не применяет
(`lifecycle.ignore_changes`), поэтому PR пина в `main` — запись для учёта с нулевым plan, а не защита от отката.

## Слияние этого PR в `main` — заблокировано (решение владельца)

1. **tenancy CI**: `backfill_core.implementation_hash` Heldi включает `promo.py` — любая правка promo.py в `main`
   инвалидирует квалификацию Heldi. Обходить хеш ради зелёного CI запрещено. Варианты: (а) вынести promo из
   состава, который квалифицируется для Heldi (архитектурная развязка), или (б) согласованный перевыпуск квалификации Heldi.
2. **ozon compat**: дифференциальный тест совместимости знает только пути v1; переход на v2 требует явного
   допуска «D3: методы акций v2» в контракте совместимости.

До решения `main` и EVETIS-production расходятся по promo.py на этот патч (production = релизная ветка).

## Не входит

- История 14–28.10 по автодобавлению и участию не восстанавливается: методы отдают только текущее состояние.
- `evetis_mart.V_PROMO_OBSERVABILITY_CURRENT` (STALE / NO_VALID_OBSERVATION) — PR #390.
