-- Товар Ozon: последний снимок каталога + соответствие внутреннему артикулу арендатора и его
-- справочник товаров. Нет соответствия — internal_sku NULL и mapping_status UNMAPPED (не выдумываем).
-- Несколько действующих соответствий с одной датой начала — AMBIGUOUS, internal_sku NULL.
CREATE OR REPLACE VIEW `__tenant__.ozon_mart.DIM_OZON_PRODUCT`
OPTIONS(description = 'Товар Ozon: последний снимок каталога, внутренний артикул арендатора и справочник товаров.')
AS
WITH latest AS (
  SELECT c.sku, c.product_id, c.offer_id, c.name, c.is_archived, c.status_name, c.snapshot_date
  FROM `__tenant__.ozon_raw.RAW_OZON_CATALOG` c
  QUALIFY ROW_NUMBER() OVER (PARTITION BY c.product_id ORDER BY c.snapshot_date DESC, c.extracted_at DESC, c.product_id DESC) = 1
),
sku_owners AS (
  SELECT sku, COUNT(DISTINCT product_id) AS products
  FROM latest WHERE sku IS NOT NULL AND SAFE_CAST(sku AS INT64) > 0
  GROUP BY sku
),
mapping_latest AS (
  SELECT m.marketplace_sku, m.internal_sku
  FROM `__tenant__.ref.REF_SKU_CHANNEL_MAP` m
  WHERE m.marketplace = 'OZON' AND m.is_current
  QUALIFY RANK() OVER (PARTITION BY m.marketplace_sku ORDER BY m.valid_from DESC) = 1
),
mapping AS (
  SELECT ml.marketplace_sku, IF(COUNT(DISTINCT ml.internal_sku) = 1, MAX(ml.internal_sku), NULL) AS internal_sku,
    COUNT(DISTINCT ml.internal_sku) AS candidates
  FROM mapping_latest ml
  GROUP BY ml.marketplace_sku
),
master AS (
  SELECT pm.internal_sku, pm.product_name, pm.brand, pm.category, pm.is_bundle
  FROM `__tenant__.ref.REF_PRODUCT_MASTER` pm
  WHERE pm.valid_to IS NULL
  QUALIFY ROW_NUMBER() OVER (PARTITION BY pm.internal_sku ORDER BY pm.valid_from DESC) = 1
)
SELECT l.sku, l.product_id, l.offer_id, l.name AS ozon_name, l.is_archived, l.status_name,
  l.snapshot_date AS catalog_snapshot_date,
  COALESCE(o.products = 1, FALSE) AS sku_joinable, m.internal_sku,
  CASE WHEN o.products > 1 OR m.candidates > 1 THEN 'AMBIGUOUS' WHEN m.internal_sku IS NULL THEN 'UNMAPPED' ELSE 'MAPPED' END AS mapping_status,
  pm.product_name, pm.brand, pm.category, pm.is_bundle
FROM latest l
LEFT JOIN sku_owners o ON o.sku = l.sku
LEFT JOIN mapping m ON m.marketplace_sku = l.sku AND o.products = 1
LEFT JOIN master pm ON pm.internal_sku = m.internal_sku;
