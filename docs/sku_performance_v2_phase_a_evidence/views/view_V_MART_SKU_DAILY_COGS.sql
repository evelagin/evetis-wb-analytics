WITH
map AS (
  SELECT nm_id, internal_sku, is_bundle
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.REF_SKU_MASTER`
),
cogs AS (
  SELECT internal_sku, effective_from,
         COALESCE(effective_to, DATE '9999-12-31') AS effective_to,
         product_cogs_rub
  FROM `project-fa311fc0-4d87-4781-986.evetis_ref.V_PRODUCT_COGS_EFFECTIVE`
),
sales_src AS (
  SELECT sale_id, NULLIF(TRIM(srid), '') AS srid, is_return, _sale_date AS sale_date
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_SALES_RETURNS`
),
sales_srid_self AS (
  SELECT sale_id, ANY_VALUE(srid) AS srid FROM sales_src GROUP BY sale_id
),
sales_sale_by_srid AS (
  SELECT srid, COUNT(*) AS sale_match_count, MIN(sale_date) AS sale_date
  FROM sales_src WHERE NOT is_return AND srid IS NOT NULL GROUP BY srid
),
ret_ev AS (
  SELECT
    fs.sale_id, fs.sale_date AS day, fs.nm_id, m.internal_sku,
    IFNULL(l.sale_match_count, 0) AS link_n,
    l.sale_date                   AS orig_sale_date
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.FACT_SALES` fs
  LEFT JOIN map m                 ON m.nm_id   = fs.nm_id
  LEFT JOIN sales_srid_self s     ON s.sale_id = fs.sale_id
  LEFT JOIN sales_sale_by_srid l  ON l.srid    = s.srid
  WHERE fs.is_return
),
ret_res AS (
  SELECT
    r.sale_id, r.day, r.nm_id,
    ANY_VALUE(r.link_n)           AS link_n,
    COUNT(v.internal_sku)         AS mc,
    ANY_VALUE(v.product_cogs_rub) AS uc
  FROM ret_ev r
  LEFT JOIN cogs v
         ON v.internal_sku = r.internal_sku
        AND IF(r.link_n = 1, r.orig_sale_date, NULL)
            BETWEEN v.effective_from AND v.effective_to
  GROUP BY 1,2,3
),
ret_day AS (
  SELECT
    day, nm_id,
    COUNT(*)                                                     AS reversal_events,
    COUNTIF(link_n = 1 AND mc = 1)                               AS reversal_resolved_events,
    IF(COUNTIF(link_n = 1 AND mc = 1) = COUNT(*), SUM(uc), NULL) AS reversal_cogs_rub
  FROM ret_res GROUP BY day, nm_id
),
stl AS (
  SELECT
    finance_date AS day, nm_id,
    COUNT(*)                                                                                       AS settlement_events,
    IF(COUNTIF(cogs_resolution_status = 'RESOLVED') = COUNT(*), SUM(product_cogs_signed_rub), NULL) AS settlement_cogs_rub
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_FACT_FINANCE_COGS`
  GROUP BY finance_date, nm_id
),
base AS (
  SELECT
    d.day, d.nm_id, m.internal_sku, m.is_bundle,
    d.buyouts_qty, d.returns_qty, d.buyouts_rub, d.returns_rub,
    d.hybrid_day_contribution_pre_cogs, d.settlement_day_contribution_pre_cogs,
    d.build_as_of_date, d.built_at
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.MART_SKU_DAILY` d
  LEFT JOIN map m ON m.nm_id = d.nm_id
),
base_res AS (
  SELECT
    b.day, b.nm_id, b.internal_sku, b.is_bundle,
    b.buyouts_qty, b.returns_qty, b.buyouts_rub, b.returns_rub,
    b.hybrid_day_contribution_pre_cogs, b.settlement_day_contribution_pre_cogs,
    b.build_as_of_date, b.built_at,
    COUNT(v.internal_sku)         AS cogs_match_count,
    ANY_VALUE(v.product_cogs_rub) AS unit_cogs_raw
  FROM base b
  LEFT JOIN cogs v
         ON v.internal_sku = b.internal_sku
        AND b.day BETWEEN v.effective_from AND v.effective_to
  GROUP BY 1,2,3,4,5,6,7,8,9,10,11,12
),
flagged AS (
  SELECT
    br.*,
    rd.reversal_events, rd.reversal_resolved_events, rd.reversal_cogs_rub,
    st.settlement_events, st.settlement_cogs_rub,
    CASE
      WHEN br.buyouts_qty = 0 AND br.returns_qty = 0            THEN 'NOT_APPLICABLE'
      WHEN br.internal_sku IS NULL                              THEN 'UNMAPPED_SKU'
      WHEN br.cogs_match_count > 1                              THEN 'CONTRACT_VIOLATION_MULTI'
      WHEN br.cogs_match_count = 0                              THEN 'UNKNOWN_NO_INTERVAL'
      WHEN br.returns_qty > 0 AND rd.reversal_cogs_rub IS NULL  THEN 'RETURN_LINK_UNRESOLVED'
      ELSE 'RESOLVED'
    END AS cogs_resolution_status
  FROM base_res br
  LEFT JOIN ret_day rd ON rd.day = br.day AND rd.nm_id = br.nm_id
  LEFT JOIN stl     st ON st.day = br.day AND st.nm_id = br.nm_id
)
SELECT
  day,
  nm_id,
  internal_sku,
  is_bundle,
  buyouts_qty,
  returns_qty,
  buyouts_rub,
  returns_rub,
  hybrid_day_contribution_pre_cogs,
  settlement_day_contribution_pre_cogs,
  cogs_match_count,
  IF(cogs_resolution_status = 'RESOLVED', unit_cogs_raw, NULL)          AS unit_product_cogs_rub,
  cogs_resolution_status,
  (cogs_resolution_status IN ('RESOLVED', 'NOT_APPLICABLE'))            AS cogs_covered,
  IF(cogs_resolution_status IN ('RESOLVED', 'NOT_APPLICABLE'), buyouts_qty + returns_qty, 0) AS cogs_covered_qty,
  IF(cogs_resolution_status IN ('RESOLVED', 'NOT_APPLICABLE'), 0, buyouts_qty + returns_qty) AS cogs_uncovered_qty,
  CASE cogs_resolution_status
    WHEN 'NOT_APPLICABLE' THEN NUMERIC '0'
    WHEN 'RESOLVED'       THEN buyouts_qty * unit_cogs_raw
    ELSE NULL END                                                       AS product_cogs_operational_rub,
  CASE cogs_resolution_status
    WHEN 'NOT_APPLICABLE' THEN NUMERIC '0'
    WHEN 'RESOLVED'       THEN IFNULL(reversal_cogs_rub, NUMERIC '0')
    ELSE NULL END                                                       AS product_cogs_reversal_operational_rub,
  CASE cogs_resolution_status
    WHEN 'NOT_APPLICABLE' THEN NUMERIC '0'
    WHEN 'RESOLVED'       THEN buyouts_qty * unit_cogs_raw - IFNULL(reversal_cogs_rub, NUMERIC '0')
    ELSE NULL END                                                       AS net_product_cogs_operational_rub,
  IFNULL(reversal_events, 0)                                            AS reversal_events,
  IFNULL(reversal_resolved_events, 0)                                   AS reversal_resolved_events,
  IF(settlement_events IS NULL, NUMERIC '0', settlement_cogs_rub)       AS product_cogs_settlement_rub,
  IFNULL(settlement_events, 0)                                          AS settlement_cogs_event_count,
  (settlement_events IS NULL OR settlement_cogs_rub IS NOT NULL)        AS settlement_cogs_covered,
  IF(cogs_resolution_status IN ('RESOLVED', 'NOT_APPLICABLE'),
     hybrid_day_contribution_pre_cogs
       - CASE cogs_resolution_status
           WHEN 'NOT_APPLICABLE' THEN NUMERIC '0'
           ELSE buyouts_qty * unit_cogs_raw - IFNULL(reversal_cogs_rub, NUMERIC '0')
         END,
     NULL)                                                              AS contribution_after_product_cogs_rub,
  IF(settlement_events IS NULL OR settlement_cogs_rub IS NOT NULL,
     settlement_day_contribution_pre_cogs
       - IF(settlement_events IS NULL, NUMERIC '0', settlement_cogs_rub),
     NULL)                                                              AS settlement_contribution_after_product_cogs_rub,
  'AFTER_PRODUCT_COGS'                                                  AS economics_basis,
  'Product COGS = закупка + Китай + таможня + доставка ДО фулфилмента. НЕ включены: операции ФФ, расходы кабинета WB, OPEX, налог. Это НЕ валовая маржа и НЕ прибыль.'
                                                                        AS economics_note,
  build_as_of_date,
  built_at                                                              AS mart_built_at,
  CURRENT_TIMESTAMP()                                                   AS generated_at
FROM flagged