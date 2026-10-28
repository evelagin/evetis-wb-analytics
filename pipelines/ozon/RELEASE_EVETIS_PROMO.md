# EVETIS Ozon runtime — выпуск «акции: изоляция этапов + методы v2»

Ветка `release/evetis-ozon-runtime-08dfb88-promo`. Отдельный квалифицированный выпуск EVETIS: **не** из `main`
(в `main` после 08dfb88 — 43 коммита `pipelines/ozon`, ≥36 мультиарендность; правка promo.py в `main` по дизайну
инвалидирует квалификацию Heldi — `backfill_core.implementation_hash`). Хеш Heldi не трогается.

## Провенанс базы
- Production: Jobs `ozon-runtime-daily|fast|promo|weekly` на `ozon-runtime-ingest@sha256:24e3c6d6…fa1b`
  (Terraform `infra/terraform/ozon_ingestion.tf`, `local.ozon_runtime_image`).
- Cloud Build `7e1fe4e2-79d1-46d6-a11e-be32f8d2eb1b` (22.09.2026, тег `:08dfb88`); архив исходника сборки побайтно
  равен `git archive 08dfb88 pipelines/ozon/runtime` (проверено 28.10.2026).

## Состав (относительно 08dfb88)
- Образ: только `runtime/promo.py` — изоляция этапов (UNSUPPORTED/FAILED, PARTIAL, красный прогон) и методы v2
  (`/v2/actions/products|candidates` — last_id; `/v2/actions/auto-add/products/list|candidates` — offset, обязательная
  `auto_add_date`; ответ без обёртки `result`; цены Money `{amount, currency}` → `_money()`; у auto-add/candidates
  идентификатор товара `id`; `add_mode` bool → `'true'/'false'`). `/v1/actions` (список акций) не меняется.
- Без `_money()` ответ v2 молча дал бы NULL во всех ценах — это ловят тесты `test_promo_observer.py` (Money, обе
  пагинации, null-Money, отсутствие дрейфа схемы).
- Вне образа: тесты promo, read-only проба `tools/ozon/promo_v2_probe.py` + тест, `.github/workflows/ci.yml` (пины
  actions), этот файл и `release_gate_evetis.sh`.
- Секреты, датасеты, таблицы, схемы — прежние (`EVETIS_OZON_CLIENT_ID`, `EVETIS_OZON_API_KEY`, `ozon_raw`).

## Изоляция от Heldi
- `main` не меняется: `pipelines/ozon/runtime/promo.py` в `main` остаётся прежним, поэтому
  `backfill_core.implementation_hash` и квалификация Heldi (tenancy CI) не затрагиваются.
- В образ не попадает ни один модуль мультиарендности (G3: 7 файлов = база 08dfb88).
- Выпуск меняет один ресурс EVETIS — Job `ozon-runtime-promo` в проекте EVETIS. Задания, секреты, датасеты и
  Scheduler Heldi не трогаются; остальные три Ozon Job EVETIS остаются на 24e3c6d6.

## Release gates (`pipelines/ozon/release_gate_evetis.sh`)
G1 база · G2 в образе изменён только promo.py · G3 состав файлов образа = база (нет модулей арендатора) ·
G4 нет мутирующих путей/ПДн · G5 офлайн-тесты Ozon · G6 исходник Cloud Build = git archive HEAD (после сборки).
28.10: G1–G5 OK (165 passed, 1 skipped); G6 — после сборки. Выпуск — только с зелёными G1–G6 и CI на том же
коммите, что собирается.

## Порядок выпуска (каждый шаг — отдельный OWNER ACK)
1. **Проба формы ответа v2** (только чтение, ≤ 19 запросов: GET `/v1/actions` + на ≤ 3 акции 2 POST + на ≤ 2 даты
   автодобавления 2 POST, `limit` 5): `python3 tools/ozon/promo_v2_probe.py --execute` из чистого checkout HEAD этой
   ветки. Ключи — из Secret Manager учёткой владельца, не печатаются; ответы — в
   `~/EVETIS_AUDIT_REPORTS/ozon_promo_v2_probe/<ts>/`, не в Git. Принять: ответы 200,
   `wrapped_in_result = false`, цены — Money, у auto-add/candidates есть `id`. Иначе — правка адаптера и тестов,
   сборки нет.
2. **Сборка** из чистого checkout HEAD релизной ветки:
   `gcloud builds submit pipelines/ozon/runtime --tag europe-west1-docker.pkg.dev/project-fa311fc0-4d87-4781-986/cloud-run-source-deploy/ozon-runtime-ingest:<sha>`;
   записать digest и URI архива исходника; `release_gate_evetis.sh <gs://…tgz>` → G6 OK.
3. **Только `ozon-runtime-promo`**: `gcloud run jobs update ozon-runtime-promo --region europe-west1 --image <…@sha256:digest>`.
   Остальные три Ozon Job остаются на 24e3c6d6. Terraform этот шаг не откатит: образ Ozon Job — в
   `lifecycle.ignore_changes` (`infra/terraform/ozon_ingestion.tf`, «образ продвигается отдельно»). PR в `main` с отдельным
   digest promo нужен **для учёта** (Git = production), его plan обязан показать 0 изменений.
4. **Приёмка по данным** (следующий слот 07/12/17/22 МСК):
   - `OZON_INGESTION_RUNS(promo)` SUCCESS, либо `PromoPartial` с перечнем этапов;
   - `OZON_PROMO_OBSERVATIONS` COMPLETE (или PARTIAL с причинами);
   - свежие строки `RAW_OZON_PROMO_ACTIONS/PRODUCTS/AUTO_ADD/PRODUCT_MARKETING` на этот observation_id с ненулевыми ценами
     (`price_rub`, `action_price_rub` не NULL у участников);
   - ни одного observation_id со статусом PARTIAL в `ozon_mart.V_OZON_PROMO_SKU_STATE_HISTORY`;
   - DRO-1 `ozon_promo` → HEALTHY после COMPLETE; `V_PROMO_OBSERVABILITY_CURRENT` (после #390) — Ozon не STALE.
5. **Откат**: `gcloud run jobs update ozon-runtime-promo --region europe-west1 --image …@sha256:24e3c6d6…`.
   Данные append-only, не мигрируются; PARTIAL-снимки историей не читаются.

## Чего выпуск не восстанавливает
Историю автодобавления и участия за 14–28.10: методы отдают только текущее состояние.
