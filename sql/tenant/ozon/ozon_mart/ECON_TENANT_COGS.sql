-- Себестоимость, которую вводит продавец (ref.REF_COGS), с интервалами действия.
-- Пусто — себестоимости нет: прибыль после себестоимости не публикуется (NOT_AVAILABLE), а не 0.
CREATE OR REPLACE VIEW `__tenant__.ozon_mart.ECON_TENANT_COGS`
OPTIONS(description = 'Себестоимость единицы от продавца с интервалом действия (effective_to NULL = открыт).')
AS
SELECT c.internal_sku, c.effective_from, COALESCE(c.effective_to, DATE '9999-12-31') AS effective_to,
  c.product_cogs_rub, c.cost_basis, c.source, c.loaded_at
FROM `__tenant__.ref.REF_COGS` c
QUALIFY ROW_NUMBER() OVER (PARTITION BY c.internal_sku, c.effective_from ORDER BY c.loaded_at DESC) = 1;
