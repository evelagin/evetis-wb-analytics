-- ═══════════════════════════════════════════════════════════════════════
-- Откат Stage 3.4D.2.
--
-- Порядок обратный установке: сначала витрины, потом RAW.
--
-- ⚠️ Откат RAW-слоя НЕОБРАТИМО теряет тарифные компоненты: блок
-- commissions приходит только в текущем ответе API, историю за прошлые
-- дни Ozon не отдаёт. Снимок 2026-09-06 после DROP не восстановится.
--
-- ⚠️ Отдельно от SQL: образ Cloud Run. Все четыре job переведены на
-- sha256:43fb3a10601b99381571858210bb34e50cd792bca0c6fae72c550672db885422.
-- Возврат к прежнему образу:
--   IMG=europe-west1-docker.pkg.dev/project-fa311fc0-4d87-4781-986/\
--   cloud-run-source-deploy/ozon-runtime-ingest@sha256:a7ce446a661e612bab7f756f103ec8bcf41688429c1da9361096fae108639409
--   for J in ozon-runtime-daily ozon-runtime-fast ozon-runtime-weekly ozon-runtime-ingest; do
--     gcloud run jobs update $J --region=europe-west1 --image=$IMG
--   done
-- Прежний образ сохраняет прежнее поведение prices: одно поле
-- sales_percent_fbo, RAW_OZON_PRICE_COMMISSIONS не пишется.
-- ═══════════════════════════════════════════════════════════════════════

-- ── 1. Витрины Stage 3.4D.2 ───────────────────────────────────────────
-- Порядок важен: экономика ссылается на тариф и на здоровье источника,
-- сравнение схем ссылается на экономику.
DROP VIEW IF EXISTS `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_SKU_FBO_FBS_COMPARISON_CURRENT`;
DROP VIEW IF EXISTS `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_SKU_FORWARD_ECONOMICS_CURRENT`;
DROP VIEW IF EXISTS `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_SKU_CURRENT_TARIFF`;
DROP VIEW IF EXISTS `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_TARIFF_CHANGE_LOG`;
DROP VIEW IF EXISTS `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_TARIFF_SOURCE_HEALTH`;

-- Объекты Stage 3.4C (FCT_OZON_PNL_MONTHLY, FCT_OZON_SKU_PNL_MONTHLY,
-- V_OZON_LIFETIME_PNL, V_OZON_MART_FRESHNESS, V_OZON_SKU_UNIT_ECONOMICS_CURRENT)
-- этот откат НЕ трогает: Stage 3.4D.2 их не изменял.

-- ── 2. Длинная проекция компонент тарифа ──────────────────────────────
DROP TABLE IF EXISTS `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_PRICE_COMMISSIONS`;

-- ── 3. Колонки RAW_OZON_PRICES ────────────────────────────────────────
-- BigQuery не умеет DROP COLUMN на партиционированной таблице через
-- ALTER в один приём для NUMERIC-полей с данными — команды ниже
-- выполняются по одной и физически освобождают место не сразу.
-- Осмысленно только если расширение признано ошибкой: сами по себе
-- лишние NULLABLE-колонки ничего не ломают и ничего не стоят.
ALTER TABLE `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_PRICES`
  DROP COLUMN IF EXISTS currency_code,
  DROP COLUMN IF EXISTS retail_price_rub,
  DROP COLUMN IF EXISTS vat_rate,
  DROP COLUMN IF EXISTS auto_action_enabled,
  DROP COLUMN IF EXISTS auto_add_to_ozon_actions_enabled,
  DROP COLUMN IF EXISTS volume_weight_l,
  DROP COLUMN IF EXISTS sales_percent_fbs,
  DROP COLUMN IF EXISTS sales_percent_rfbs,
  DROP COLUMN IF EXISTS sales_percent_fbp,
  DROP COLUMN IF EXISTS fbo_direct_flow_trans_min_rub,
  DROP COLUMN IF EXISTS fbo_direct_flow_trans_max_rub,
  DROP COLUMN IF EXISTS fbo_deliv_to_customer_rub,
  DROP COLUMN IF EXISTS fbo_return_flow_rub,
  DROP COLUMN IF EXISTS fbs_first_mile_min_rub,
  DROP COLUMN IF EXISTS fbs_first_mile_max_rub,
  DROP COLUMN IF EXISTS fbs_direct_flow_trans_min_rub,
  DROP COLUMN IF EXISTS fbs_direct_flow_trans_max_rub,
  DROP COLUMN IF EXISTS fbs_deliv_to_customer_rub,
  DROP COLUMN IF EXISTS fbs_return_flow_rub,
  DROP COLUMN IF EXISTS ozon_index_min_price_rub,
  DROP COLUMN IF EXISTS ozon_index_value,
  DROP COLUMN IF EXISTS self_marketplaces_index_min_price_rub,
  DROP COLUMN IF EXISTS self_marketplaces_index_value,
  DROP COLUMN IF EXISTS commissions_json,
  DROP COLUMN IF EXISTS commissions_field_count,
  DROP COLUMN IF EXISTS commissions_unknown_fields;

-- ── 4. Код runtime ────────────────────────────────────────────────────
-- git revert коммита Stage 3.4D.2 по pipelines/ozon/runtime/entities.py
-- вернёт функцию prices() к записи в одну таблицу.
