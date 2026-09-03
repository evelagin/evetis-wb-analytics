-- Stage 3.1 — общий справочный слой EVETIS.
-- ADDITIVE: ничего существующего не изменяется и не удаляется.
-- wb_raw.REF_SKU_MASTER остаётся как есть, его потребители не переключаются.
-- Location: EU (совпадает с evetis_ref, wb_raw, wb_mart).

-- ============================================================ REF_PRODUCT_MASTER
-- Grain: 1 строка на internal_sku.
-- Только marketplace-независимые атрибуты товара.
-- НЕ содержит: nm_id, wb_vendor_code, wb_subject_*, include_in_*, status, active.
CREATE TABLE IF NOT EXISTS `project-fa311fc0-4d87-4781-986.evetis_ref.REF_PRODUCT_MASTER`
(
  internal_sku            STRING  NOT NULL OPTIONS(description="Внутренний артикул EVETIS. Логический первичный ключ"),
  canonical_product_name  STRING           OPTIONS(description="Каноническое наименование товара EVETIS, независимое от площадки. НЕ заголовок карточки маркетплейса и не SEO-название"),
  product_name_short      STRING           OPTIONS(description="Короткое внутреннее название"),
  brand                   STRING           OPTIONS(description="Бренд"),
  category                STRING           OPTIONS(description="Внутренняя категория EVETIS. Не категория площадки"),
  product_line            STRING           OPTIONS(description="Продуктовая линейка"),
  product_type            STRING           OPTIONS(description="Тип продукта"),
  is_bundle               BOOL             OPTIONS(description="Признак набора. Определяет расчёт себестоимости через REF_BUNDLE_COMPONENTS"),
  volume                  STRING           OPTIONS(description="Объём как в исходном справочнике. Разложение на value+unit отложено"),
  barcode                 STRING           OPTIONS(description="Штрихкод физического товара"),
  active_from             DATE             OPTIONS(description="Начало действия строки справочника"),
  active_to               DATE             OPTIONS(description="Конец действия строки справочника, NULL = действует"),
  source_system           STRING           OPTIONS(description="Откуда получена строка при миграции"),
  loaded_at               TIMESTAMP        OPTIONS(description="Момент загрузки строки")
)
CLUSTER BY internal_sku
OPTIONS(description="Общий справочник товаров EVETIS. Marketplace-независимый. Используется доменами WB и Ozon. Не содержит идентификаторов и операционных флагов площадок");

-- ========================================================= REF_SKU_CHANNEL_MAP
-- Grain: internal_sku × marketplace × marketplace_sku.
-- Идентификаторы каждой площадки хранятся ЯВНО и никогда не выводятся из другой площадки.
CREATE TABLE IF NOT EXISTS `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_CHANNEL_MAP`
(
  internal_sku           STRING NOT NULL OPTIONS(description="Внутренний артикул EVETIS"),
  marketplace            STRING NOT NULL OPTIONS(description="Код площадки: WB или OZON"),
  marketplace_sku        STRING NOT NULL OPTIONS(description="Идентификатор товара на площадке. Для WB это nm_id, для OZON это ozon_sku. Хранится явной строкой"),
  marketplace_product_id STRING          OPTIONS(description="Внутренний id товара на площадке, если у неё он отдельный. Для OZON это product_id"),
  offer_id               STRING          OPTIONS(description="Артикул продавца на площадке. Для OZON это offer_id"),
  vendor_code            STRING          OPTIONS(description="Артикул продавца в терминах площадки"),
  valid_from             DATE   NOT NULL OPTIONS(description="Начало действия связки"),
  valid_to               DATE            OPTIONS(description="Конец действия связки, NULL = действует"),
  is_current             BOOL   NOT NULL OPTIONS(description="Признак действующей строки"),
  mapping_source         STRING          OPTIONS(description="Чем подтверждена связка"),
  mapping_status         STRING          OPTIONS(description="RESOLVED_FROM_OZON_API | MIGRATED_FROM_WB_REFERENCE"),
  verified_at            DATE            OPTIONS(description="Дата последней проверки связки"),
  loaded_at              TIMESTAMP       OPTIONS(description="Момент загрузки строки")
)
CLUSTER BY marketplace, internal_sku
OPTIONS(description="Связка внутреннего артикула EVETIS с идентификаторами площадок. Единственный разрешённый способ резолва идентификаторов Ozon. Неявный join offer_id = nm_id в production запрещён");
