-- Себестоимость, которую вводит продавец (ref.REF_COGS), с интервалами действия.
-- Открытый интервал (effective_to NULL) действует до начала следующего интервала того же
-- артикула, последний — без конца. Явно закрытые интервалы не меняются: если они
-- пересекаются, на пересечении себестоимость считается отсутствующей (экономика, DQ
-- tenant_ops.V_DQ_COGS_OVERLAPS). Одинаковые загрузки одного интервала — последняя по
-- loaded_at; при равном loaded_at остаются обе (пересечение → себестоимости нет).
-- Пусто — себестоимости нет: результат после себестоимости не публикуется, а не 0.
CREATE OR REPLACE VIEW `__tenant__.ozon_mart.ECON_TENANT_COGS`
OPTIONS(description = 'Себестоимость единицы от продавца с интервалом действия; открытый интервал — до следующего.')
AS
WITH latest AS (
  SELECT c.internal_sku, c.effective_from, c.effective_to, c.product_cogs_rub, c.cost_basis, c.source, c.loaded_at
  FROM `__tenant__.ref.REF_COGS` c
  QUALIFY RANK() OVER (PARTITION BY c.internal_sku, c.effective_from ORDER BY c.loaded_at DESC) = 1
),
starts AS (
  SELECT DISTINCT l.internal_sku, l.effective_from FROM latest l
),
next_start AS (
  SELECT s.internal_sku, s.effective_from,
    LEAD(s.effective_from) OVER (PARTITION BY s.internal_sku ORDER BY s.effective_from) AS next_from
  FROM starts s
)
SELECT l.internal_sku, l.effective_from,
  COALESCE(l.effective_to, DATE_SUB(n.next_from, INTERVAL 1 DAY), DATE '9999-12-31') AS effective_to,
  l.product_cogs_rub, l.cost_basis, l.source, l.loaded_at
FROM latest l
JOIN next_start n ON n.internal_sku = l.internal_sku AND n.effective_from = l.effective_from;
