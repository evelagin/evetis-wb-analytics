-- ============================================================================
-- CANONICAL CURRENT DEFINITION — ozon_mart.V_OZON_STORE_PNL_MONTHLY (VIEW)
-- Git-first (sync_state pending_deploy): в production объекта ещё нет. Rules: sql/current/README.md.
-- D3 «Ozon Магазин P&L» — единственная власть по управленческой прибыли магазина Ozon за месяц.
-- Строится сверху вниз из V_OZON_STORE_ACCOUNT_LEDGER: каждая строка RAW финансов попадает ровно в одну
-- колонку (карта V_OZON_FINANCE_OPERATION_MAP), экономический блок — в выручку и комиссию.
-- L1–L4 (FCT_OZON_PNL_MONTHLY) не переопределяются: L4 — справочная колонка l4_fct_reference_rub с мостом.
-- Деньги: «+» в колонках расходов — расход (вычитается), компенсации и выручка — доход. Суммы без ROUND.
--   V  store_contribution_pre_tax_rub   = B − C − D − E − F − G − H − I − J − K − L − M − N − O − P − Q − R − S − T + U + X
--   W  management_tax_rub               = 0,02 × B (управленческий резерв, ровно один раз; X несёт налог внутри)
--   Y  management_net_store_profit_rub  = V − W
--   Z  management_net_store_margin      = Y ÷ (B + provisional_revenue_rub)
--   AA sku_contribution_rub             = B − C − D − E … − K − атрибуция CPC − атрибуция CPO − W (как Юнитка)
--   AB ads_adjustment_rub               = атрибуция CPC + атрибуция CPO − L − M − N
--   AC unattributed_account_net_rub     = U − (O + P + Q + R + S + T)
--   Тождество I1: Y = AA + AB + AC + X (атрибуция сокращается — не может войти дважды или потеряться).
-- Себестоимость: V_PRODUCT_COGS_EFFECTIVE на дату заказа × проданные единицы; сторно (−1) себестоимость
-- не уменьшает (решение владельца 12.10: возврат не сторнируется автоматически) — справочно в AF.
-- Состояние месяца, свежесть и закрытие — из V_OZON_STORE_MONTH_STATE; значения не-FINANCIAL_COMPLETE
-- месяцев — PARTIAL/PROVISIONAL, а не окончательная прибыль.
-- ============================================================================
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_STORE_PNL_MONTHLY`
OPTIONS (description = "Управленческий P&L магазина Ozon (D3), зерно = календарный месяц P&L; единственная власть по Management Net Store Profit. Из V_OZON_STORE_ACCOUNT_LEDGER: каждая строка финансов ровно в одной колонке. V = B-C-D-...-T+U+X; W = 0,02 x B; Y = V - W (management_net_store_profit_rub); Z = Y / (B + провизорная выручка). AA - вклад SKU после атрибутированной рекламы и налога, AB - поправка рекламы (атрибуция - биллинг), AC - кабинет; Y = AA + AB + AC + X. L1-L4 не переопределяются: l4_fct_reference_rub и мост l4_bridge_rub. financial_state != FINANCIAL_COMPLETE - значения PARTIAL/PROVISIONAL, не окончательная прибыль.")
AS
WITH
led AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_STORE_ACCOUNT_LEDGER`),
cogs AS (
  SELECT internal_sku, effective_from, IFNULL(effective_to, DATE '9999-12-31') effective_to, product_cogs_rub
  FROM `project-fa311fc0-4d87-4781-986.evetis_ref.V_PRODUCT_COGS_EFFECTIVE`),
units AS (
  SELECT l.pnl_month m, l.econ_direction, l.source, l.quantity, c.product_cogs_rub
  FROM led l
  LEFT JOIN cogs c ON c.internal_sku = l.internal_sku
                  AND l.order_date_msk BETWEEN c.effective_from AND c.effective_to
  WHERE l.econ_direction IS NOT NULL OR l.source = 'CIS_DOCUMENT'),
cogs_m AS (
  SELECT m,
    SUM(IF(econ_direction = 'SALE' OR source = 'CIS_DOCUMENT', quantity * product_cogs_rub, 0)) product_cogs_rub,
    SUM(IF(econ_direction = 'REVERSAL', ABS(quantity) * product_cogs_rub, 0)) returned_units_cogs_not_reversed_rub
  FROM units GROUP BY 1),
money AS (
  SELECT pnl_month m,
    SUM(IF(is_econ_block OR source = 'CIS_DOCUMENT', seller_base_rub, 0)) seller_base_revenue_rub,
    -SUM(commission_rub) commission_rub,
    -SUM(IF(pnl_column = 'E', amount_rub, 0)) logistics_rub,
    -SUM(IF(pnl_column = 'F', amount_rub, 0)) last_mile_rub,
    -SUM(IF(pnl_column = 'G', amount_rub, 0)) return_logistics_rub,
    -SUM(IF(pnl_column = 'H', amount_rub, 0)) acquiring_rub,
    -SUM(IF(pnl_column = 'I', amount_rub, 0)) sku_storage_rub,
    -SUM(IF(pnl_column = 'J', amount_rub, 0)) sku_promotion_rub,
    -SUM(IF(pnl_column = 'K', amount_rub, 0)) other_direct_rub,
    -SUM(IF(pnl_column = 'L', amount_rub, 0)) cpc_billing_rub,
    -SUM(IF(pnl_column = 'M', amount_rub, 0)) cpo_billing_rub,
    -SUM(IF(pnl_column = 'N', amount_rub, 0)) store_ads_rub,
    -SUM(IF(pnl_column = 'O', amount_rub, 0)) crossdock_rub,
    -SUM(IF(pnl_column = 'P', amount_rub, 0)) processing_rub,
    -SUM(IF(pnl_column = 'Q', amount_rub, 0)) fbo_placement_rub,
    -SUM(IF(pnl_column = 'R', amount_rub, 0)) insurance_rub,
    -SUM(IF(pnl_column = 'S', amount_rub, 0)) other_store_costs_rub,
    -SUM(IF(pnl_column = 'T', amount_rub, 0)) premium_rub,
    SUM(IF(pnl_column = 'U', amount_rub, 0)) marketplace_compensation_rub,
    SUM(IF(pnl_column IS NULL, money_rub, 0)) unconsumed_rub,
    SUM(IF(date_basis = 'ORDER_MONTH' AND source = 'RAW', amount_rub, 0)) order_basis_amount_rub
  FROM led GROUP BY 1),
booking AS (
  SELECT booking_month m,
    SUM(IF(date_basis = 'ORDER_MONTH' AND source = 'RAW', amount_rub, 0)) booking_basis_amount_rub,
    SUM(IF(type_id = 52, amount_rub, 0)) premium_booking_amount_rub
  FROM led GROUP BY 1),
prem_service AS (
  SELECT pnl_month m, SUM(amount_rub) premium_service_amount_rub
  FROM led WHERE type_id = 52 GROUP BY 1),
ads AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_STORE_ADS_RECON_MONTHLY`),
st AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_STORE_MONTH_STATE`),
fct AS (
  SELECT month m, l4_marketplace_contribution_profit_known_rub l4
  FROM `project-fa311fc0-4d87-4781-986.ozon_mart.FCT_OZON_PNL_MONTHLY`),
months AS (
  SELECT m FROM money UNION DISTINCT SELECT pnl_month FROM st),
j AS (
  SELECT months.m pnl_month,
    IFNULL(mo.seller_base_revenue_rub, 0) seller_base_revenue_rub,
    IFNULL(cm.product_cogs_rub, 0) product_cogs_rub,
    IFNULL(mo.commission_rub, 0) commission_rub,
    IFNULL(mo.logistics_rub, 0) logistics_rub,
    IFNULL(mo.last_mile_rub, 0) last_mile_rub,
    IFNULL(mo.return_logistics_rub, 0) return_logistics_rub,
    IFNULL(mo.acquiring_rub, 0) acquiring_rub,
    IFNULL(mo.sku_storage_rub, 0) sku_storage_rub,
    IFNULL(mo.sku_promotion_rub, 0) sku_promotion_rub,
    IFNULL(mo.other_direct_rub, 0) other_direct_rub,
    IFNULL(mo.cpc_billing_rub, 0) cpc_billing_rub,
    IFNULL(mo.cpo_billing_rub, 0) cpo_billing_rub,
    IFNULL(mo.store_ads_rub, 0) store_ads_rub,
    IFNULL(mo.crossdock_rub, 0) crossdock_rub,
    IFNULL(mo.processing_rub, 0) processing_rub,
    IFNULL(mo.fbo_placement_rub, 0) fbo_placement_rub,
    IFNULL(mo.insurance_rub, 0) insurance_rub,
    IFNULL(mo.other_store_costs_rub, 0) other_store_costs_rub,
    IFNULL(mo.premium_rub, 0) premium_rub,
    IFNULL(mo.marketplace_compensation_rub, 0) marketplace_compensation_rub,
    IFNULL(st.provisional_open_rub, 0) provisional_open_rub,
    IFNULL(st.provisional_revenue_rub, 0) provisional_revenue_rub,
    IFNULL(ads.cpc_attributed_rub, 0) cpc_attributed_rub,
    IFNULL(ads.cpo_attributed_rub, 0) cpo_attributed_rub,
    IFNULL(ads.cpc_residual_rub, 0) cpc_residual_rub,
    IFNULL(ads.cpo_residual_rub, 0) cpo_residual_rub,
    IFNULL(ads.ads_attribution_days_missing, 0) ads_attribution_days_missing,
    IFNULL(mo.unconsumed_rub, 0) unconsumed_rub,
    IFNULL(mo.order_basis_amount_rub, 0) - IFNULL(bk.booking_basis_amount_rub, 0) delta_basis_rub,
    IFNULL(ps.premium_service_amount_rub, 0) - IFNULL(bk.premium_booking_amount_rub, 0) delta_premium_rub,
    fct.l4 l4_fct_reference_rub,
    IFNULL(cm.returned_units_cogs_not_reversed_rub, 0) returned_units_cogs_not_reversed_rub,
    st.financial_state, st.month_closure_status, st.source_freshness_state, st.known_source_defects,
    st.finance_watermark_date, st.provisional_units, st.in_transit_units
  FROM months
  LEFT JOIN money mo ON mo.m = months.m
  LEFT JOIN cogs_m cm ON cm.m = months.m
  LEFT JOIN booking bk ON bk.m = months.m
  LEFT JOIN prem_service ps ON ps.m = months.m
  LEFT JOIN ads ON ads.pnl_month = months.m
  LEFT JOIN st ON st.pnl_month = months.m
  LEFT JOIN fct ON fct.m = months.m),
k AS (
  SELECT j.*,
    seller_base_revenue_rub - product_cogs_rub - commission_rub - logistics_rub - last_mile_rub
      - return_logistics_rub - acquiring_rub - sku_storage_rub - sku_promotion_rub - other_direct_rub
      - cpc_billing_rub - cpo_billing_rub - store_ads_rub - crossdock_rub - processing_rub
      - fbo_placement_rub - insurance_rub - other_store_costs_rub - premium_rub
      + marketplace_compensation_rub + provisional_open_rub store_contribution_pre_tax_rub,
    seller_base_revenue_rub * 0.02 management_tax_rub
  FROM j)
SELECT
  pnl_month,
  seller_base_revenue_rub, product_cogs_rub, commission_rub, logistics_rub, last_mile_rub,
  return_logistics_rub, acquiring_rub, sku_storage_rub, sku_promotion_rub, other_direct_rub,
  cpc_billing_rub, cpo_billing_rub, store_ads_rub,
  crossdock_rub, processing_rub, fbo_placement_rub, insurance_rub, other_store_costs_rub, premium_rub,
  marketplace_compensation_rub,
  provisional_open_rub,
  store_contribution_pre_tax_rub,
  management_tax_rub,
  store_contribution_pre_tax_rub - management_tax_rub management_net_store_profit_rub,
  SAFE_DIVIDE(store_contribution_pre_tax_rub - management_tax_rub,
              NULLIF(seller_base_revenue_rub + provisional_revenue_rub, 0)) management_net_store_margin,
  seller_base_revenue_rub - product_cogs_rub - commission_rub - logistics_rub - last_mile_rub
    - return_logistics_rub - acquiring_rub - sku_storage_rub - sku_promotion_rub - other_direct_rub
    - cpc_attributed_rub - cpo_attributed_rub - management_tax_rub sku_contribution_rub,
  cpc_attributed_rub + cpo_attributed_rub - cpc_billing_rub - cpo_billing_rub - store_ads_rub ads_adjustment_rub,
  cpc_billing_rub + cpo_billing_rub + store_ads_rub - cpc_attributed_rub - cpo_attributed_rub unattributed_advertising_rub,
  crossdock_rub + processing_rub + fbo_placement_rub + insurance_rub + other_store_costs_rub + premium_rub account_level_expenses_rub,
  marketplace_compensation_rub - (crossdock_rub + processing_rub + fbo_placement_rub + insurance_rub
    + other_store_costs_rub + premium_rub) unattributed_account_net_rub,
  l4_fct_reference_rub,
  store_contribution_pre_tax_rub - IFNULL(l4_fct_reference_rub, 0) l4_bridge_rub,
  delta_basis_rub, delta_premium_rub,
  store_contribution_pre_tax_rub - IFNULL(l4_fct_reference_rub, 0) - delta_basis_rub - delta_premium_rub l4_bridge_other_rub,
  returned_units_cogs_not_reversed_rub,
  financial_state,
  CASE
    WHEN ads_attribution_days_missing > 0 THEN 'PARTIAL_ADS_UNAVAILABLE'
    WHEN month_closure_status = 'OPEN' THEN 'PROVISIONAL_OPEN'
    WHEN ABS(cpc_residual_rub) <= 0.50 THEN 'SAFE_EXACT'
    ELSE 'PARTIAL_RESIDUAL' END ads_attribution_state,
  source_freshness_state,
  month_closure_status,
  known_source_defects,
  finance_watermark_date,
  provisional_revenue_rub, provisional_units, in_transit_units,
  cpc_attributed_rub, cpo_attributed_rub, cpc_residual_rub, cpo_residual_rub,
  unconsumed_rub
FROM k;
