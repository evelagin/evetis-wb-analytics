-- Классификация типов начислений Ozon (таксономия платформы v1, 33 типа).
-- type_id — идентификаторы Ozon; класс — их смысл (логистика, эквайринг, продвижение…).
-- Тип, которого здесь нет, НЕ относится в «прочее»: он даёт UNCLASSIFIED и останавливает
-- публикацию финансового результата дня (tenant_ops.V_DQ_UNCLASSIFIED_ACCRUALS).
-- attribution_scope: POSTING — начисление по отправлению; SKU — по товару без отправления;
-- STORE — на уровне магазина.
CREATE OR REPLACE VIEW `__tenant__.ozon_mart.DIM_OZON_ACCRUAL_TYPE`
OPTIONS(description = 'Классификация типов начислений Ozon: type_id → класс и уровень атрибуции. Таксономия платформы v1.')
AS
SELECT t.type_id, t.accrual_class, t.attribution_scope, 1 AS taxonomy_version
FROM UNNEST([
    STRUCT(75 AS type_id, 'PROMOTION_BILLING' AS accrual_class, 'STORE' AS attribution_scope),
    STRUCT(41 AS type_id, 'PROMOTION_BILLING' AS accrual_class, 'STORE' AS attribution_scope),
    STRUCT(33 AS type_id, 'PROMOTION_BILLING' AS accrual_class, 'STORE' AS attribution_scope),
    STRUCT(54 AS type_id, 'PROMOTION_BILLING' AS accrual_class, 'STORE' AS attribution_scope),
    STRUCT(47 AS type_id, 'PROMOTION_SERVICES' AS accrual_class, 'STORE' AS attribution_scope),
    STRUCT(96 AS type_id, 'PROMOTION_SERVICES' AS accrual_class, 'STORE' AS attribution_scope),
    STRUCT(116 AS type_id, 'PROMOTION_SERVICES' AS accrual_class, 'SKU' AS attribution_scope),
    STRUCT(74 AS type_id, 'PROMOTION_SERVICES' AS accrual_class, 'SKU' AS attribution_scope),
    STRUCT(48 AS type_id, 'PROMOTION_SERVICES' AS accrual_class, 'SKU' AS attribution_scope),
    STRUCT(52 AS type_id, 'SUBSCRIPTION' AS accrual_class, 'STORE' AS attribution_scope),
    STRUCT(32 AS type_id, 'LOGISTICS' AS accrual_class, 'POSTING' AS attribution_scope),
    STRUCT(12 AS type_id, 'LOGISTICS' AS accrual_class, 'STORE' AS attribution_scope),
    STRUCT(29 AS type_id, 'LAST_MILE' AS accrual_class, 'POSTING' AS attribution_scope),
    STRUCT(28 AS type_id, 'LAST_MILE' AS accrual_class, 'POSTING' AS attribution_scope),
    STRUCT(98 AS type_id, 'LAST_MILE' AS accrual_class, 'POSTING' AS attribution_scope),
    STRUCT(30 AS type_id, 'LAST_MILE' AS accrual_class, 'POSTING' AS attribution_scope),
    STRUCT(79 AS type_id, 'STORAGE' AS accrual_class, 'SKU' AS attribution_scope),
    STRUCT(46 AS type_id, 'STORAGE' AS accrual_class, 'STORE' AS attribution_scope),
    STRUCT(1 AS type_id, 'ACQUIRING' AS accrual_class, 'POSTING' AS attribution_scope),
    STRUCT(59 AS type_id, 'RETURN_LOGISTICS' AS accrual_class, 'POSTING' AS attribution_scope),
    STRUCT(45 AS type_id, 'RETURN_LOGISTICS' AS accrual_class, 'POSTING' AS attribution_scope),
    STRUCT(78 AS type_id, 'RETURN_LOGISTICS' AS accrual_class, 'POSTING' AS attribution_scope),
    STRUCT(9 AS type_id, 'RETURN_LOGISTICS' AS accrual_class, 'POSTING' AS attribution_scope),
    STRUCT(6 AS type_id, 'CANCELLATION_COST' AS accrual_class, 'POSTING' AS attribution_scope),
    STRUCT(15 AS type_id, 'OTHER_MARKETPLACE_COST' AS accrual_class, 'POSTING' AS attribution_scope),
    STRUCT(71 AS type_id, 'OTHER_MARKETPLACE_COST' AS accrual_class, 'POSTING' AS attribution_scope),
    STRUCT(39 AS type_id, 'OTHER_MARKETPLACE_COST' AS accrual_class, 'POSTING' AS attribution_scope),
    STRUCT(38 AS type_id, 'OTHER_MARKETPLACE_COST' AS accrual_class, 'POSTING' AS attribution_scope),
    STRUCT(77 AS type_id, 'OTHER_MARKETPLACE_COST' AS accrual_class, 'STORE' AS attribution_scope),
    STRUCT(76 AS type_id, 'OTHER_MARKETPLACE_COST' AS accrual_class, 'STORE' AS attribution_scope),
    STRUCT(57 AS type_id, 'OTHER_MARKETPLACE_COST' AS accrual_class, 'STORE' AS attribution_scope),
    STRUCT(25 AS type_id, 'COMPENSATION' AS accrual_class, 'STORE' AS attribution_scope),
    STRUCT(10 AS type_id, 'COMPENSATION' AS accrual_class, 'STORE' AS attribution_scope)
]) AS t;
