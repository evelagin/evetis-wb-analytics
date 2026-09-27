-- Классификация типов начислений Ozon (таксономия платформы v2, 33 типа из 124 в справочнике
-- Ozon /v1/finance/accrual/types). type_id и ozon_type_name — официальные данные Ozon; класс —
-- решение платформы. classification_basis: OZON_TYPE_NAME — класс прямо следует из официального
-- имени типа; PLATFORM_INTERPRETATION — группировка платформы (провенанс —
-- docs/architecture/TENANT_SQL_SEMANTICS.md §5). Уровень атрибуции здесь не задаётся: он
-- берётся из самой строки начисления. Тип, которого здесь нет, НЕ относится в «прочее»: он
-- даёт UNCLASSIFIED, и ненулевая сумма останавливает финансовый результат.
CREATE OR REPLACE VIEW `__tenant__.ozon_mart.DIM_OZON_ACCRUAL_TYPE`
OPTIONS(description = 'Классификация типов начислений Ozon: type_id → класс; официальное имя типа и основание классификации. Таксономия платформы v2.')
AS
SELECT t.type_id, t.ozon_type_name, t.accrual_class, t.classification_basis, 2 AS taxonomy_version
FROM UNNEST([
    STRUCT(75 AS type_id, 'Stencil' AS ozon_type_name, 'PROMOTION_BILLING' AS accrual_class, 'OZON_TYPE_NAME' AS classification_basis),
    STRUCT(41 AS type_id, 'PayPerClick' AS ozon_type_name, 'PROMOTION_BILLING' AS accrual_class, 'OZON_TYPE_NAME' AS classification_basis),
    STRUCT(33 AS type_id, 'Marketing' AS ozon_type_name, 'PROMOTION_BILLING' AS accrual_class, 'OZON_TYPE_NAME' AS classification_basis),
    STRUCT(54 AS type_id, 'Promotion' AS ozon_type_name, 'PROMOTION_BILLING' AS accrual_class, 'OZON_TYPE_NAME' AS classification_basis),
    STRUCT(47 AS type_id, 'PointsForReviews' AS ozon_type_name, 'PROMOTION_SERVICES' AS accrual_class, 'PLATFORM_INTERPRETATION' AS classification_basis),
    STRUCT(96 AS type_id, 'AcceleratedReviewCollection' AS ozon_type_name, 'PROMOTION_SERVICES' AS accrual_class, 'PLATFORM_INTERPRETATION' AS classification_basis),
    STRUCT(116 AS type_id, 'FirstCustomerReview' AS ozon_type_name, 'PROMOTION_SERVICES' AS accrual_class, 'PLATFORM_INTERPRETATION' AS classification_basis),
    STRUCT(74 AS type_id, 'StarsMembership' AS ozon_type_name, 'PROMOTION_SERVICES' AS accrual_class, 'PLATFORM_INTERPRETATION' AS classification_basis),
    STRUCT(48 AS type_id, 'PremiumCashbackIndividualPoints' AS ozon_type_name, 'PROMOTION_SERVICES' AS accrual_class, 'PLATFORM_INTERPRETATION' AS classification_basis),
    STRUCT(52 AS type_id, 'PremiumSubscription' AS ozon_type_name, 'SUBSCRIPTION' AS accrual_class, 'OZON_TYPE_NAME' AS classification_basis),
    STRUCT(32 AS type_id, 'Logistic' AS ozon_type_name, 'LOGISTICS' AS accrual_class, 'OZON_TYPE_NAME' AS classification_basis),
    STRUCT(12 AS type_id, 'CrossDock' AS ozon_type_name, 'LOGISTICS' AS accrual_class, 'OZON_TYPE_NAME' AS classification_basis),
    STRUCT(29 AS type_id, 'LastMileCourier' AS ozon_type_name, 'LAST_MILE' AS accrual_class, 'OZON_TYPE_NAME' AS classification_basis),
    STRUCT(28 AS type_id, 'LastMile' AS ozon_type_name, 'LAST_MILE' AS accrual_class, 'OZON_TYPE_NAME' AS classification_basis),
    STRUCT(98 AS type_id, 'DeliveryToHandoverPlaceByOzon' AS ozon_type_name, 'LAST_MILE' AS accrual_class, 'OZON_TYPE_NAME' AS classification_basis),
    STRUCT(30 AS type_id, 'LastMilePickUpPoint' AS ozon_type_name, 'LAST_MILE' AS accrual_class, 'OZON_TYPE_NAME' AS classification_basis),
    STRUCT(79 AS type_id, 'TemporaryPlacementsAgent' AS ozon_type_name, 'STORAGE' AS accrual_class, 'OZON_TYPE_NAME' AS classification_basis),
    STRUCT(46 AS type_id, 'Placements' AS ozon_type_name, 'STORAGE' AS accrual_class, 'OZON_TYPE_NAME' AS classification_basis),
    STRUCT(1 AS type_id, 'Acquiring' AS ozon_type_name, 'ACQUIRING' AS accrual_class, 'OZON_TYPE_NAME' AS classification_basis),
    STRUCT(59 AS type_id, 'ReturnFlowLogistic' AS ozon_type_name, 'RETURN_LOGISTICS' AS accrual_class, 'OZON_TYPE_NAME' AS classification_basis),
    STRUCT(9 AS type_id, 'ClientReturn' AS ozon_type_name, 'RETURN_LOGISTICS' AS accrual_class, 'OZON_TYPE_NAME' AS classification_basis),
    STRUCT(45 AS type_id, 'PickUpPointReturnAcceptance' AS ozon_type_name, 'RETURN_LOGISTICS' AS accrual_class, 'PLATFORM_INTERPRETATION' AS classification_basis),
    STRUCT(78 AS type_id, 'TemporaryPlacement' AS ozon_type_name, 'RETURN_LOGISTICS' AS accrual_class, 'PLATFORM_INTERPRETATION' AS classification_basis),
    STRUCT(6 AS type_id, 'Cancellation' AS ozon_type_name, 'CANCELLATION_COST' AS accrual_class, 'OZON_TYPE_NAME' AS classification_basis),
    STRUCT(15 AS type_id, 'Disposal' AS ozon_type_name, 'OTHER_MARKETPLACE_COST' AS accrual_class, 'PLATFORM_INTERPRETATION' AS classification_basis),
    STRUCT(71 AS type_id, 'SellerReturns' AS ozon_type_name, 'OTHER_MARKETPLACE_COST' AS accrual_class, 'PLATFORM_INTERPRETATION' AS classification_basis),
    STRUCT(39 AS type_id, 'PackingFee' AS ozon_type_name, 'OTHER_MARKETPLACE_COST' AS accrual_class, 'PLATFORM_INTERPRETATION' AS classification_basis),
    STRUCT(38 AS type_id, 'PackageCost' AS ozon_type_name, 'OTHER_MARKETPLACE_COST' AS accrual_class, 'PLATFORM_INTERPRETATION' AS classification_basis),
    STRUCT(77 AS type_id, 'SupplyInbound' AS ozon_type_name, 'OTHER_MARKETPLACE_COST' AS accrual_class, 'PLATFORM_INTERPRETATION' AS classification_basis),
    STRUCT(76 AS type_id, 'StockInsurance' AS ozon_type_name, 'OTHER_MARKETPLACE_COST' AS accrual_class, 'PLATFORM_INTERPRETATION' AS classification_basis),
    STRUCT(57 AS type_id, 'RealizationReportCorrection' AS ozon_type_name, 'OTHER_MARKETPLACE_COST' AS accrual_class, 'PLATFORM_INTERPRETATION' AS classification_basis),
    STRUCT(25 AS type_id, 'ItemCompensation' AS ozon_type_name, 'COMPENSATION' AS accrual_class, 'OZON_TYPE_NAME' AS classification_basis),
    STRUCT(10 AS type_id, 'Compensation' AS ozon_type_name, 'COMPENSATION' AS accrual_class, 'OZON_TYPE_NAME' AS classification_basis)
]) AS t;
