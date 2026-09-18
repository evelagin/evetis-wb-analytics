WITH
universe AS (
  SELECT DISTINCT nm_id
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.REF_SKU_MASTER`
  WHERE marketplace = 'WB' AND active AND nm_id IS NOT NULL
),
src AS (
  SELECT
    CONCAT(s.report_id, '#', s.rrd_id)                                  AS finance_row_key,
    s._rr_date                                                          AS finance_date,
    s.report_id,
    s.rrd_id,
    s.source_layer,
    s.finance_status,
    s.supplier_oper_name,
    NULLIF(TRIM(s.srid), '')                                            AS srid,
    SAFE_CAST(s.wb_nm_id AS INT64)                                      AS nm_id,
    s.sku_match_status,
    SAFE_CAST(REPLACE(s.retail_price_withdisc_rub, ',', '.') AS NUMERIC) AS seller_price_rub,
    SAFE_CAST(REPLACE(s.for_pay, ',', '.') AS NUMERIC)                  AS for_pay_rub,
    s.marketplace_fee_gap_rub,
    COALESCE(SAFE_CAST(JSON_VALUE(s.raw_json, '$.retailAmount')  AS NUMERIC),
             SAFE_CAST(JSON_VALUE(s.raw_json, '$.retail_amount') AS NUMERIC)) AS buyer_paid_rub,
    SAFE_CAST(JSON_VALUE(s.raw_json, '$.vw')           AS NUMERIC)      AS vw,
    SAFE_CAST(JSON_VALUE(s.raw_json, '$.vwNds')        AS NUMERIC)      AS vw_nds,
    SAFE_CAST(JSON_VALUE(s.raw_json, '$.acquiringFee') AS NUMERIC)      AS acquiring_fee,
    SAFE_CAST(JSON_VALUE(s.raw_json, '$.ppvzReward')   AS NUMERIC)      AS ppvz_reward
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_FINANCE_SEMANTIC` s
  WHERE s.supplier_oper_name IN ('Продажа', 'Возврат')
),
flagged AS (
  SELECT
    src.*,
    (src.vw IS NOT NULL AND src.vw_nds IS NOT NULL AND src.acquiring_fee IS NOT NULL
     AND src.ppvz_reward IS NOT NULL AND src.buyer_paid_rub IS NOT NULL
     AND src.seller_price_rub IS NOT NULL AND src.for_pay_rub IS NOT NULL)  AS price_chain_available
  FROM src
)
SELECT
  f.finance_row_key,
  f.finance_date,
  f.report_id,
  f.rrd_id,
  f.source_layer,
  f.finance_status,
  f.supplier_oper_name,
  f.srid,
  f.nm_id,
  -- Тот же гейт SKU, что у MART_SKU_DAILY.marketplace_fee_rub (finpay CTE).
  COALESCE(f.nm_id > 0 AND f.sku_match_status = 'matched', FALSE)          AS is_sku_row,
  (COALESCE(f.nm_id > 0 AND f.sku_match_status = 'matched', FALSE)
   AND f.nm_id IN (SELECT nm_id FROM universe))                             AS in_mart_universe,
  f.price_chain_available,
  f.seller_price_rub,
  f.buyer_paid_rub,
  f.for_pay_rub,
  f.marketplace_fee_gap_rub,
  -- СПП: цена продавца минус сумма, оплаченная покупателем (retailAmount).
  (f.seller_price_rub - f.buyer_paid_rub)                                   AS spp_rub,
  IF(f.price_chain_available, f.vw,            NULL)                        AS wb_remuneration_rub,
  IF(f.price_chain_available, f.vw_nds,        NULL)                        AS wb_remuneration_vat_rub,
  IF(f.price_chain_available, f.acquiring_fee, NULL)                        AS acquiring_rub,
  IF(f.price_chain_available, f.ppvz_reward,   NULL)                        AS pvz_reward_rub,
  -- Остаток тождества. Ожидается |x| ≤ 0,01 на строку (округление vw).
  IF(f.price_chain_available,
     (f.seller_price_rub - f.for_pay_rub)
     - ((f.seller_price_rub - f.buyer_paid_rub) + f.vw + f.vw_nds + f.acquiring_fee + f.ppvz_reward),
     NULL)                                                                  AS price_chain_rounding_rub
FROM flagged f