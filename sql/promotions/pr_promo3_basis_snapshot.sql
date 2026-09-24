-- ============================================================================
-- PR-PROMO-3 · НЕИЗМЕННЫЙ БАЗИС ЭКОНОМИКИ АКЦИЙ: DDL двух append-only таблиц и двух процедур.
--
-- Зачем. Канонические форвардные вью (wb_mart.V_WB_SKU_FORWARD_ECONOMICS_CURRENT, WB_FE_V1;
-- ozon_mart.V_OZON_SKU_FORWARD_ECONOMICS_CURRENT, FORWARD_MODELLED) считаются от «сейчас»:
-- скользящие окна логистики и эквайринга, последний тариф, COGS на CURRENT_DATE, последняя
-- цена. Экономика акции, посчитанная вью поверх них, молча переписывала бы прошлое при
-- каждом новом COGS или тарифе. Поэтому на каждом слоте наблюдения акций (04/09/14/19 UTC)
-- процедура КОПИРУЕТ строки канонических вью как есть — без пересчёта — в append-only
-- таблицу. Экономика акции считается только из этих снимков.
--
-- Где. Таблицы — в *_raw, а не в *_mart: ozon_mart под каноническим контрактом sql/current
-- состоит только из VIEW (таблица стала бы UNEXPECTED_LIVE в R2C), а канонические вью
-- ozon_mart по политике изоляции читают только ozon_raw и evetis_ref. WB — симметрично.
-- Имена без префикса RAW_: это не ответ площадки, а снимок собственной канонической
-- экономики, как манифесты *_PROMO_OBSERVATIONS.
--
-- Процедуры идемпотентны по слоту: повтор того же слота ничего не вставляет. Историю не
-- изменяют и не удаляют. Юнитку не читают и не меняют (решение владельца 2026-09-24).
-- Откат: sql/promotions/pr_promo3_rollback.sql.
-- ============================================================================

CREATE TABLE IF NOT EXISTS `project-fa311fc0-4d87-4781-986.wb_raw.WB_PROMO_ECONOMICS_BASIS_SNAPSHOT`
(
  snapshot_slot                        STRING    NOT NULL OPTIONS(description="Слот наблюдения акций YYYY-MM-DDTHH:MM (UTC), к которому относится снимок. Ключ сопоставления с observation_bucket."),
  snapshot_id                          STRING    NOT NULL OPTIONS(description="WBECON_prod_YYYYMMDDHHMM."),
  captured_at                          TIMESTAMP NOT NULL OPTIONS(description="Момент копирования канонической вью (время знания)."),
  trigger                              STRING    OPTIONS(description="scheduler | deploy | manual."),
  internal_sku                         STRING    OPTIONS(description="Дословно из V_WB_SKU_FORWARD_ECONOMICS_CURRENT."),
  nm_id                                INT64,
  product_name_short                   STRING,
  is_bundle                            BOOL,
  seller_effective_price_rub           NUMERIC   OPTIONS(description="Цена продавца (база выручки WB_FE_V1). БАЗОВАЯ цена сценариев."),
  price_observed_at                    TIMESTAMP,
  price_freshness                      STRING,
  cogs_rub                             NUMERIC,
  cogs_source                          STRING,
  cogs_effective_from                  DATE,
  effective_commission_pct             NUMERIC,
  commission_source                    STRING,
  commission_tariff_freshness          STRING,
  acquiring_p50_pct                    FLOAT64,
  acquiring_p90_pct                    FLOAT64,
  logistics_expected_rub               NUMERIC,
  logistics_p90_rub                    NUMERIC,
  logistics_sample_size                INT64,
  contribution_before_ads_pre_tax_rub  FLOAT64   OPTIONS(description="Канонический вклад при базовой цене, дословно. Эталон сверки."),
  break_even_before_ads_pre_tax_base   FLOAT64,
  break_even_before_ads_pre_tax_stress FLOAT64,
  economics_status                     STRING,
  economics_confidence                 STRING,
  blocked_reason                       STRING,
  tax_model_status                     STRING,
  economics_model_version              STRING
)
PARTITION BY DATE(captured_at)
CLUSTER BY internal_sku
OPTIONS(description="PR-PROMO-3. Append-only снимки канонической форвардной экономики WB (WB_FE_V1) на каждый слот наблюдения акций. Строки скопированы из wb_mart.V_WB_SKU_FORWARD_ECONOMICS_CURRENT без пересчёта. Не изменять и не удалять: из них воспроизводится экономика акций в прошлом.");

CREATE TABLE IF NOT EXISTS `project-fa311fc0-4d87-4781-986.ozon_raw.OZON_PROMO_ECONOMICS_BASIS_SNAPSHOT`
(
  snapshot_slot                STRING    NOT NULL OPTIONS(description="Слот наблюдения акций YYYY-MM-DDTHH:MM (UTC)."),
  snapshot_id                  STRING    NOT NULL OPTIONS(description="OZECON_prod_YYYYMMDDHHMM."),
  captured_at                  TIMESTAMP NOT NULL OPTIONS(description="Момент копирования канонической вью."),
  trigger                      STRING,
  internal_sku                 STRING    OPTIONS(description="Дословно из V_OZON_SKU_FORWARD_ECONOMICS_CURRENT."),
  offer_id                     STRING,
  ozon_sku                     STRING,
  product_name                 STRING,
  product_type                 STRING,
  economics_mode               STRING,
  tariff_snapshot_at           TIMESTAMP,
  tariff_effective_at          DATE,
  seller_base_price            NUMERIC   OPTIONS(description="marketing_seller_price из /v5/product/info/prices — база комиссии Ozon. БАЗОВАЯ цена сценариев."),
  current_management_cogs_rub  NUMERIC,
  cogs_basis                   STRING,
  cogs_effective_from          DATE      OPTIONS(description="Начало действия COGS на дату снимка (evetis_ref.V_PRODUCT_COGS_EFFECTIVE, тот же предикат, что у канона)."),
  commission_pct               NUMERIC,
  commission_rub               NUMERIC,
  acquiring_rub                NUMERIC,
  last_mile_rub                NUMERIC,
  logistics_min_rub            NUMERIC,
  logistics_expected_rub       NUMERIC,
  logistics_max_rub            NUMERIC,
  logistics_expected_basis     STRING,
  contribution_expected        NUMERIC   OPTIONS(description="Канонический EXPECTED при базовой цене, дословно. Эталон сверки."),
  contribution_worst_case      NUMERIC   OPTIONS(description="Канонический WORST (максимальная логистика), дословно."),
  break_even_price_expected    NUMERIC,
  break_even_price_worst       NUMERIC,
  forward_economics_ready      BOOL,
  tariff_freshness_status      STRING,
  expected_mode_status         STRING
)
PARTITION BY DATE(captured_at)
CLUSTER BY internal_sku
OPTIONS(description="PR-PROMO-3. Append-only снимки канонической форвардной экономики Ozon (FORWARD_MODELLED) на каждый слот наблюдения акций. Строки скопированы из ozon_mart.V_OZON_SKU_FORWARD_ECONOMICS_CURRENT без пересчёта. Не изменять и не удалять.");

CREATE OR REPLACE PROCEDURE `project-fa311fc0-4d87-4781-986.wb_mart.sp_snapshot_wb_promo_economics_basis`(run_trigger STRING)
OPTIONS(description="PR-PROMO-3. Копирует wb_mart.V_WB_SKU_FORWARD_ECONOMICS_CURRENT в wb_raw.WB_PROMO_ECONOMICS_BASIS_SNAPSHOT для текущего слота наблюдения акций. Идемпотентна по слоту, append-only.")
BEGIN
  DECLARE now_ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP();
  DECLARE slot STRING DEFAULT (
    SELECT IF(h IS NULL,
              FORMAT_TIMESTAMP('%Y-%m-%dT19:00', TIMESTAMP_SUB(TIMESTAMP_TRUNC(now_ts, DAY), INTERVAL 1 DAY)),
              FORMAT_TIMESTAMP('%Y-%m-%dT', now_ts) || FORMAT('%02d:00', h))
    FROM (SELECT MAX(x) AS h FROM UNNEST([4, 9, 14, 19]) AS x WHERE x <= EXTRACT(HOUR FROM now_ts))
  );
  INSERT INTO `project-fa311fc0-4d87-4781-986.wb_raw.WB_PROMO_ECONOMICS_BASIS_SNAPSHOT`
    (snapshot_slot, snapshot_id, captured_at, trigger, internal_sku, nm_id, product_name_short, is_bundle,
     seller_effective_price_rub, price_observed_at, price_freshness, cogs_rub, cogs_source, cogs_effective_from,
     effective_commission_pct, commission_source, commission_tariff_freshness, acquiring_p50_pct, acquiring_p90_pct,
     logistics_expected_rub, logistics_p90_rub, logistics_sample_size, contribution_before_ads_pre_tax_rub,
     break_even_before_ads_pre_tax_base, break_even_before_ads_pre_tax_stress, economics_status,
     economics_confidence, blocked_reason, tax_model_status, economics_model_version)
  SELECT slot, 'WBECON_prod_' || REGEXP_REPLACE(slot, r'[-:T]', ''), now_ts, run_trigger,
    internal_sku, nm_id, product_name_short, is_bundle,
    seller_effective_price_rub, price_observed_at, price_freshness, cogs_rub, cogs_source, cogs_effective_from,
    effective_commission_pct, commission_source, commission_tariff_freshness, acquiring_p50_pct, acquiring_p90_pct,
    logistics_expected_rub, logistics_p90_rub, logistics_sample_size, contribution_before_ads_pre_tax_rub,
    break_even_before_ads_pre_tax_base, break_even_before_ads_pre_tax_stress, economics_status,
    economics_confidence, blocked_reason, tax_model_status, economics_model_version
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_SKU_FORWARD_ECONOMICS_CURRENT`
  WHERE NOT EXISTS (
    SELECT 1 FROM `project-fa311fc0-4d87-4781-986.wb_raw.WB_PROMO_ECONOMICS_BASIS_SNAPSHOT` s
    WHERE s.snapshot_slot = slot);
END;

CREATE OR REPLACE PROCEDURE `project-fa311fc0-4d87-4781-986.ozon_mart.sp_snapshot_ozon_promo_economics_basis`(run_trigger STRING)
OPTIONS(description="PR-PROMO-3. Копирует ozon_mart.V_OZON_SKU_FORWARD_ECONOMICS_CURRENT в ozon_raw.OZON_PROMO_ECONOMICS_BASIS_SNAPSHOT для текущего слота наблюдения акций. Идемпотентна по слоту, append-only.")
BEGIN
  DECLARE now_ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP();
  DECLARE slot STRING DEFAULT (
    SELECT IF(h IS NULL,
              FORMAT_TIMESTAMP('%Y-%m-%dT19:00', TIMESTAMP_SUB(TIMESTAMP_TRUNC(now_ts, DAY), INTERVAL 1 DAY)),
              FORMAT_TIMESTAMP('%Y-%m-%dT', now_ts) || FORMAT('%02d:00', h))
    FROM (SELECT MAX(x) AS h FROM UNNEST([4, 9, 14, 19]) AS x WHERE x <= EXTRACT(HOUR FROM now_ts))
  );
  INSERT INTO `project-fa311fc0-4d87-4781-986.ozon_raw.OZON_PROMO_ECONOMICS_BASIS_SNAPSHOT`
    (snapshot_slot, snapshot_id, captured_at, trigger, internal_sku, offer_id, ozon_sku, product_name, product_type,
     economics_mode, tariff_snapshot_at, tariff_effective_at, seller_base_price, current_management_cogs_rub,
     cogs_basis, cogs_effective_from, commission_pct, commission_rub, acquiring_rub, last_mile_rub,
     logistics_min_rub, logistics_expected_rub, logistics_max_rub, logistics_expected_basis,
     contribution_expected, contribution_worst_case, break_even_price_expected, break_even_price_worst,
     forward_economics_ready, tariff_freshness_status, expected_mode_status)
  SELECT slot, 'OZECON_prod_' || REGEXP_REPLACE(slot, r'[-:T]', ''), now_ts, run_trigger,
    f.internal_sku, f.offer_id, f.ozon_sku, f.product_name, f.product_type,
    f.economics_mode, f.tariff_snapshot_at, f.tariff_effective_at, f.seller_base_price, f.current_management_cogs_rub,
    f.cogs_basis, c.effective_from, f.commission_pct, f.commission_rub, f.acquiring_rub, f.last_mile_rub,
    f.logistics_min_rub, f.logistics_expected_rub, f.logistics_max_rub, f.logistics_expected_basis,
    f.contribution_expected, f.contribution_worst_case, f.break_even_price_expected, f.break_even_price_worst,
    f.forward_economics_ready, f.tariff_freshness_status, f.expected_mode_status
  FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_SKU_FORWARD_ECONOMICS_CURRENT` f
  LEFT JOIN (
    SELECT internal_sku, MAX(effective_from) AS effective_from
    FROM `project-fa311fc0-4d87-4781-986.evetis_ref.V_PRODUCT_COGS_EFFECTIVE`
    WHERE CURRENT_DATE() BETWEEN effective_from AND COALESCE(effective_to, DATE '9999-12-31')
    GROUP BY internal_sku) c ON c.internal_sku = f.internal_sku
  WHERE NOT EXISTS (
    SELECT 1 FROM `project-fa311fc0-4d87-4781-986.ozon_raw.OZON_PROMO_ECONOMICS_BASIS_SNAPSHOT` s
    WHERE s.snapshot_slot = slot);
END;
