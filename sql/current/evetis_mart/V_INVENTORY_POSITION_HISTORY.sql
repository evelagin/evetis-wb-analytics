-- ============================================================================
-- CANONICAL CURRENT DEFINITION — evetis_mart.V_INVENTORY_POSITION_HISTORY (VIEW)
-- Authoritative Git definition. Rules: sql/current/README.md. Metadata: MANIFEST.json.
-- Origin: PR-PROMO-4 (Git-first, pending_deploy). Contract:
-- docs/promotions/PR_PROMO_4_INVENTORY_SELL_THROUGH_CONTEXT_2026-09-24.md.
--
-- ВТОРОЙ СИСТЕМЫ ЗАПАСОВ НЕТ. Это переименование понятий поверх дневной истории Control
-- Tower: evetis_ref.CT_INVENTORY_SNAPSHOT_DAILY — партиция дня, которую sp_ct_refresh_daily
-- пишет из wb_mart.V_CT_INVENTORY_TRUTH_LIVE (срез ФФ владельца − отгрузки после среза +
-- живые остатки площадок). Ни одна формула запаса здесь не пересчитывается: каждое число —
-- колонка среза, сумма — уже посчитанная Control Tower.
--
-- Понятия (без двойного счёта, тождества проверяет DQ):
--   warehouse_ff_units          ФФ — общий пул для WB и Ozon (правило владельца R5, C0)
--   marketplace_available_units доступно покупателю сейчас = WB FBO + Ozon FBO + FBS
--   in_transit_units            едет с ФФ на площадки (WB + Ozon), уже списано с ФФ
--   inventory_position_units    позиция = ФФ + площадки + в пути = CT sellable_units
--   исключено из позиции: reserved_in_delivery_units (WB, у покупателя в пути),
--   unavailable_claimed_units (WB, претензия/утрата), inbound_not_received_units (партия
--   не получена). Резерв Ozon в API не наблюдается — отдельной колонки нет.
--
-- Классификация Control Tower (status_code) — ПОЛИТИКА, а не факт: пороги зашиты в
-- представлении и владельцем не утверждались. Она сохранена в ct_policy_* и нигде в
-- расчётах этого слоя не используется.
--
-- Грейн: snapshot_date × internal_sku (физические SKU; наборы — в V_BUNDLE_ASSEMBLY_CAPACITY_CURRENT).
-- История неизменна после окончания дня: процедура заменяет только партицию текущего дня.
-- ============================================================================
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.evetis_mart.V_INVENTORY_POSITION_HISTORY`
OPTIONS (description = "PR-PROMO-4. История позиции запаса по дням: одна строка на snapshot_date × физический SKU. Источник — дневной срез Control Tower (CT_INVENTORY_SNAPSHOT_DAILY), формулы запаса не пересчитываются. ФФ, площадки, в пути, позиция и исключённые категории разведены; классификация Control Tower сохранена как политика.")
AS
WITH s AS (
  SELECT *
  FROM `project-fa311fc0-4d87-4781-986.evetis_ref.CT_INVENTORY_SNAPSHOT_DAILY`
),
pm AS (
  SELECT internal_sku, is_bundle
  FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_PRODUCT_MASTER`
),
ch AS (
  SELECT
    internal_sku,
    STRING_AGG(IF(UPPER(marketplace) = 'WB', marketplace_sku, NULL), ',' ORDER BY marketplace_sku) AS wb_nm_ids,
    STRING_AGG(IF(UPPER(marketplace) = 'WB', vendor_code, NULL), ',' ORDER BY vendor_code) AS wb_vendor_codes,
    STRING_AGG(IF(UPPER(marketplace) = 'OZON', marketplace_sku, NULL), ',' ORDER BY marketplace_sku) AS ozon_skus,
    STRING_AGG(IF(UPPER(marketplace) = 'OZON', offer_id, NULL), ',' ORDER BY offer_id) AS ozon_offer_ids,
    LOGICAL_OR(UPPER(marketplace) = 'WB') AS listed_on_wb,
    LOGICAL_OR(UPPER(marketplace) = 'OZON') AS listed_on_ozon
  FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_CHANNEL_MAP`
  WHERE is_current
  GROUP BY internal_sku
)
SELECT
  s.snapshot_date,
  s.snapshot_ts AS inventory_as_of,
  s.internal_sku,
  s.product_name,
  s.product_line,
  CASE
    WHEN pm.internal_sku IS NULL THEN 'NOT_IN_PRODUCT_MASTER'
    WHEN pm.is_bundle THEN 'BUNDLE_IN_COMPONENT_GRAIN'
    ELSE 'CANONICAL_SKU'
  END AS mapping_status,
  ch.wb_nm_ids,
  ch.wb_vendor_codes,
  ch.ozon_skus,
  ch.ozon_offer_ids,
  IFNULL(ch.listed_on_wb, FALSE) AS listed_on_wb,
  IFNULL(ch.listed_on_ozon, FALSE) AS listed_on_ozon,
  -- Понятия запаса
  s.ff_total_units AS warehouse_ff_units,
  s.wb_fbo_live_units AS wb_available_units,
  s.ozon_fbo_units AS ozon_available_units,
  s.fbs_units,
  s.marketplace_units AS marketplace_available_units,
  s.wb_in_transit_units,
  s.ozon_transit_units AS ozon_in_transit_units,
  s.wb_in_transit_units + s.ozon_transit_units AS in_transit_units,
  s.sellable_units AS inventory_position_units,
  s.assembled_bundle_units_on_marketplaces AS marketplace_units_inside_bundle_cards,
  s.wb_to_client_units AS reserved_in_delivery_units,
  s.wb_lost_claimed_units AS unavailable_claimed_units,
  s.inbound_units AS inbound_not_received_units,
  s.inbound_status,
  s.inbound_eta,
  -- Область доступности (какой канал может обслужить единицу)
  'WAREHOUSE_SHARED_POOL' AS warehouse_ff_scope,
  'CHANNEL_SPECIFIC' AS marketplace_available_scope,
  'CHANNEL_COMMITTED' AS in_transit_scope,
  'GLOBAL_PHYSICAL' AS inventory_position_scope,
  -- Свежесть составляющих (факты; статусы свежести — в текущем представлении)
  s.ff_snapshot_date AS ff_anchor_date,
  DATE_DIFF(s.snapshot_date, s.ff_snapshot_date, DAY) AS ff_anchor_age_days,
  s.ff_operational_snapshot + s.ff_pallet_snapshot AS ff_anchor_units,
  s.ff_outflow_since_snapshot AS ff_outflow_since_anchor_units,
  s.wb_stock_as_of,
  s.ozon_stock_as_of,
  -- Существующая классификация Control Tower — политика, не факт
  s.status_code AS ct_policy_status_code,
  s.status_reason AS ct_policy_status_reason,
  'CONTROL_TOWER_HARDCODED_NOT_OWNER_APPROVED' AS ct_policy_status_provenance,
  -- Как Control Tower видел скорость в момент среза (заказы без отмен, окно по CURRENT_DATE)
  s.units_per_day_30d AS ct_units_per_day_30d_at_snapshot,
  s.units_per_day_7d AS ct_units_per_day_7d_at_snapshot,
  s.days_of_stock_total_at_30d_rate AS ct_cover_days_30d_at_snapshot,
  s.expiry_date AS ct_expiry_date_at_snapshot,
  'evetis_ref.CT_INVENTORY_SNAPSHOT_DAILY ← sp_ct_refresh_daily ← wb_mart.V_CT_INVENTORY_TRUTH_LIVE' AS inventory_source
FROM s
LEFT JOIN pm ON pm.internal_sku = s.internal_sku
LEFT JOIN ch ON ch.internal_sku = s.internal_sku
