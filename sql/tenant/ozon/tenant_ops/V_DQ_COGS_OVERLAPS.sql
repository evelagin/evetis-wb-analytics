-- DQ: пересекающиеся интервалы себестоимости продавца по одному артикулу (после разрешения
-- открытых интервалов). На пересечении себестоимость считается отсутствующей (не выбирается
-- и не суммируется), пока справочник не исправлен.
CREATE OR REPLACE VIEW `__tenant__.tenant_ops.V_DQ_COGS_OVERLAPS`
OPTIONS(description = 'DQ: пересекающиеся интервалы себестоимости продавца; на пересечении себестоимость не применяется.')
AS
WITH ranked AS (
  SELECT c.internal_sku, c.effective_from, c.effective_to, c.product_cogs_rub, c.loaded_at,
    ROW_NUMBER() OVER (PARTITION BY c.internal_sku
                       ORDER BY c.effective_from, c.loaded_at, c.product_cogs_rub, c.effective_to) AS rid
  FROM `__tenant__.ozon_mart.ECON_TENANT_COGS` c
)
SELECT a.internal_sku, a.effective_from AS first_from, a.effective_to AS first_to, a.product_cogs_rub AS first_cogs_rub,
  b.effective_from AS second_from, b.effective_to AS second_to, b.product_cogs_rub AS second_cogs_rub
FROM ranked a
JOIN ranked b ON b.internal_sku = a.internal_sku AND b.rid > a.rid AND b.effective_from <= a.effective_to;
