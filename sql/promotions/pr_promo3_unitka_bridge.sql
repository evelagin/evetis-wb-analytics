-- ============================================================================
-- PR-PROMO-3 · ЗАМЕР моста «Forward economics ↔ Юнитка WB» на одной цене. ТОЛЬКО ЧТЕНИЕ.
-- Не ворота и не исправление: решение владельца 2026-09-24 — Юнитку не менять, расхождение
-- контрактов документировать. Юнитка здесь только читается (её вью ставок и COGS).
--
-- При одной и той же цене продавца P:
--   FE  = P − P·take_fe − L_fe − C_fe                     (WB_FE_V1, до налога)
--   U   = P − P·AD − AF − 0,02·P − C_u                    (Юнитка, колонка AI: formulas.ts)
--   FE − U = P·(AD − take_fe) + (AF − L_fe) + 0,02·P + (C_u − C_fe)
-- Проверка U01 — тождество замыкается (нет неучтённого слагаемого). Слагаемые — отчётные.
-- ============================================================================

-- @check U01_UNITKA_BRIDGE_CLOSES
WITH fe AS (
  SELECT internal_sku, nm_id, product_name_short, seller_effective_price_rub AS p, cogs_rub AS c_fe,
    (effective_commission_pct + acquiring_p50_pct) / 100 AS take_fe, logistics_expected_rub AS l_fe,
    contribution_before_ads_pre_tax_rub AS fe_contribution
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_SKU_FORWARD_ECONOMICS_CURRENT`
  WHERE economics_status != 'BLOCKED'
),
u AS (
  SELECT nm_id, commission_rate AS ad, logistics_per_unit AS af
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_UNITKA_COMMISSION_RATES`
  WHERE nm_id > 0
),
uc AS (
  SELECT nm_id, canonical_cogs AS c_u
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_UNITKA_COGS_CANONICAL`
  QUALIFY ROW_NUMBER() OVER (PARTITION BY nm_id ORDER BY day DESC) = 1
),
b AS (
  SELECT fe.*, u.ad, u.af, uc.c_u,
    fe.p - fe.p * fe.take_fe - fe.l_fe - fe.c_fe AS fe_unrounded,
    fe.p - fe.p * u.ad - u.af - 0.02 * fe.p - uc.c_u AS unitka_ai
  FROM fe JOIN u USING (nm_id) JOIN uc USING (nm_id)
)
SELECT nm_id, product_name_short, p AS seller_price_rub,
  ROUND(fe_contribution, 2) AS fe_contribution_rub,
  ROUND(unitka_ai, 2) AS unitka_unit_contribution_rub,
  ROUND(fe_unrounded - unitka_ai, 2) AS gap_rub,
  ROUND(p * (ad - take_fe), 2) AS take_component_rub,
  ROUND(af - l_fe, 2) AS logistics_component_rub,
  ROUND(0.02 * p, 2) AS tax_reserve_component_rub,
  ROUND(c_u - c_fe, 2) AS cogs_component_rub,
  IF(ABS((fe_unrounded - unitka_ai) - (p * (ad - take_fe) + (af - l_fe) + 0.02 * p + (c_u - c_fe))) < 0.000001,
     'PASS', 'FAIL') AS status
FROM b
ORDER BY nm_id;
