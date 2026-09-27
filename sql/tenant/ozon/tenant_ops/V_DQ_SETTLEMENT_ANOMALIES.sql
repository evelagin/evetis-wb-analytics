-- DQ: отправления, чей экономический блок не позволяет считать выручку и комиссию: блоков
-- больше одного или блок неполный (нет цены продавца или комиссии). Такие отправления
-- считаются нерассчитанными, и результат их строки не публикуется.
CREATE OR REPLACE VIEW `__tenant__.tenant_ops.V_DQ_SETTLEMENT_ANOMALIES`
OPTIONS(description = 'DQ: отправления с несколькими или неполными экономическими блоками (результат не публикуется).')
AS
SELECT s.posting_number, s.sku, s.first_settlement_date, s.last_settlement_date, s.economic_blocks,
  s.incomplete_economic_blocks
FROM `__tenant__.ozon_mart.NORM_OZON_POSTING_SETTLEMENT` s
WHERE s.economic_blocks > 1 OR s.incomplete_economic_blocks > 0;
