-- ============================================================================
-- STAGE 3.4B — КОНТРАКТ ОБНОВЛЕНИЯ REF_SKU_CHANNEL_MAP (Ozon)
-- Дата: 2026-09-04.
--
-- ЗАЧЕМ. Корень дефекта Stage 3.4A: карта идентичностей заполнялась из ТЕКУЩЕГО
--   списка товаров Ozon API. Товар, снятый с продажи до очередного снимка,
--   в карту не попадал и молча выпадал из отчётности. Так потерялись
--   1997315236 (EVT-HC-BODY-300) и 2046027307 (EVT-SET-HAND-BODY) — 417 единиц,
--   18,4 % валового объёма Ozon.
--
-- ПРАВИЛО. Обновление карты — APPEND-PRESERVING. Строки только добавляются и
--   обновляются. DELETE и CREATE OR REPLACE TABLE запрещены: исторические
--   связки, которых уже нет в API, обязаны сохраняться навсегда.
--
-- ПРИОРИТЕТ ИСТОЧНИКОВ (при конфликте выигрывает меньший номер):
--   1. OZON_PRIMARY_DOCUMENT      первичные документы Ozon и ozon_raw
--   2. OWNER_VERIFIED             ручная проверка владельцем
--   3. RESOLVED_FROM_OZON_API     текущий каталог Ozon
--   4. MIGRATED_FROM_WB_REFERENCE наследие справочника WB
--   Строка с более высоким приоритетом не перезаписывается более низким.
--
-- ИНВАРИАНТ. Один marketplace_sku → ровно один internal_sku. Проверяется A-7.
--   Соглашение offer_id = WB nm_id — наблюдаемая метаданная, а НЕ логика резолва.
--   Выводить идентичность Ozon из nm_id запрещено.
-- ============================================================================

MERGE `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_CHANNEL_MAP` T
USING (
  -- Текущий каталог Ozon: последний снимок
  SELECT c.sku AS marketplace_sku, c.offer_id, c.product_id AS marketplace_product_id,
         m.internal_sku
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_CATALOG` c
  JOIN `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_CHANNEL_MAP` m
    ON m.marketplace = 'OZON' AND m.marketplace_sku = c.sku
  WHERE c.snapshot_date = (SELECT MAX(snapshot_date)
                           FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_CATALOG`)
) S
ON T.marketplace = 'OZON' AND T.marketplace_sku = S.marketplace_sku
-- Обновляем только строки, чей источник НЕ приоритетнее каталога.
WHEN MATCHED AND T.mapping_status NOT IN ('OZON_PRIMARY_DOCUMENT', 'OWNER_VERIFIED') THEN UPDATE SET
  offer_id = S.offer_id,
  marketplace_product_id = S.marketplace_product_id,
  is_current = TRUE,
  verified_at = CURRENT_DATE(),
  loaded_at = CURRENT_TIMESTAMP()
WHEN NOT MATCHED THEN INSERT (internal_sku, marketplace, marketplace_sku,
  marketplace_product_id, offer_id, vendor_code, valid_from, valid_to,
  is_current, mapping_source, mapping_status, verified_at, loaded_at)
VALUES (S.internal_sku, 'OZON', S.marketplace_sku, S.marketplace_product_id,
  S.offer_id, NULL, CURRENT_DATE(), NULL, TRUE,
  'Ozon Seller API /v3/product/list + /v3/product/info/list',
  'RESOLVED_FROM_OZON_API', CURRENT_DATE(), CURRENT_TIMESTAMP());
-- Ветки WHEN NOT MATCHED BY SOURCE ... THEN DELETE здесь нет и быть не должно.

-- ====================================================== РЕГРЕССИЯ ОБНОВЛЕНИЯ
-- R-1 исторические идентичности пережили обновление
ASSERT (
  SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_CHANNEL_MAP`
  WHERE marketplace = 'OZON' AND marketplace_sku IN ('1997315236','2046027307')
) = 2 AS 'R-1 исторические идентичности Ozon должны пережить обновление каталога';

-- R-2 их происхождение не перезаписано каталогом
ASSERT (
  SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_CHANNEL_MAP`
  WHERE marketplace = 'OZON' AND marketplace_sku IN ('1997315236','2046027307')
    AND mapping_status = 'OZON_PRIMARY_DOCUMENT' AND is_current = FALSE
) = 2 AS 'R-2 происхождение исторических связок не должно перезаписываться каталогом';

-- R-3 ни одна идентичность не указывает на два товара
ASSERT (
  SELECT COUNT(*) FROM (
    SELECT marketplace_sku FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_CHANNEL_MAP`
    WHERE marketplace = 'OZON'
    GROUP BY marketplace_sku HAVING COUNT(DISTINCT internal_sku) > 1)
) = 0 AS 'R-3 один Ozon SKU не может указывать на несколько internal_sku';

-- R-4 все исторические идентичности из фактов резолвятся
ASSERT (
  SELECT COUNT(*) FROM (
    SELECT DISTINCT p.sku
    FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_POSTINGS_FBO` p
    LEFT JOIN `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_CHANNEL_MAP` m
      ON m.marketplace = 'OZON' AND m.marketplace_sku = p.sku
    WHERE m.internal_sku IS NULL)
) = 0 AS 'R-4 все Ozon SKU из postings должны резолвиться';
