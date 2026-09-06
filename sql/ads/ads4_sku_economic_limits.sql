-- ============================================================================
-- Stage ADS-4 — экономический потолок рекламы на уровне SKU.
-- Дата: 06.09.2026. Док: docs/ADS4_SKU_ECONOMIC_LIMITS_2026-09-06.md
--
-- Отвечает на один вопрос: сколько рекламы товар выдерживает, прежде чем его
-- вклад станет отрицательным. Это ПОТОЛОК, а не операционная цель.
--
-- 🔴 ТЕРМИНОЛОГИЯ. Ни одно поле здесь не является прибылью.
--    contribution_before_ads_rub = buyouts_rub − marketplace_fee_rub
--                                − logistics_cost_positive − net_product_cogs_rub
--    contribution_after_ads_and_product_cogs_rub = contribution_before_ads_rub − ad_spend_rub
--    НЕ включено: хранение, штрафы, приёмка и прочие удержания уровня счёта
--    (WB не даёт их связки с SKU), фулфилмент, OPEX, налоги.
--    Поэтому это НЕ прибыль, НЕ EBITDA, НЕ операционный результат и НЕ итоговая маржа.
--    Следствие: max_ad_drr_breakeven — ОПТИМИСТИЧНАЯ верхняя граница.
--
-- 🔴 ЦЕЛЕВОГО DRR ЗДЕСЬ НЕТ. ADS-0 доказал, что единый допустимый DRR по ассортименту
--    невалиден: безубыточный DRR разбегается от отрицательного до +28 %. Ни 10 %, ни
--    15 %, ни «70 % от максимума», ни зелёный/жёлтый/красный не вводятся. Операционная
--    цель — предмет ADS-7 Decision Engine, а не этого слоя.
--
-- 🔴 ОТРИЦАТЕЛЬНАЯ ЭКОНОМИКА НЕ ОБРЕЗАЕТСЯ. Если contribution_before_ads < 0, товар
--    убыточен ДО всякой рекламы, и max_ad_drr_breakeven выходит отрицательным. Это не
--    ошибка формулы, а факт: положительного допустимого рекламного бюджета у такого
--    товара не существует. Значение показывается как есть, без клампа в ноль.
--
-- 🔴 БЕЗ FALLBACK-СЕБЕСТОИМОСТИ. Если COGS покрыта не полностью, экономический потолок
--    равен NULL, а не нулю, и состояние помечается MISSING_COGS.
--
-- ── ГРЕЙН: as_of_date × window_days × nm_id ─────────────────────────────────
-- as_of_date = последний ЗАКРЫТЫЙ день. Текущие незакрытые сутки МСК не участвуют.
-- Окна: 7 / 28 / 90 закрытых суток. Логика одна, окна разворачиваются из массива.
--
-- ── ИСТОЧНИКИ (проверено) ───────────────────────────────────────────────────
--   wb_mart.MART_SKU_DAILY        грейн day × nm_id, 7 727 строк = 7 727 ключей
--   wb_mart.V_MART_SKU_DAILY_COGS тот же грейн 1:1, fan-out нет
--   evetis_ref.V_PRODUCT_COGS_EFFECTIVE  25/25 SKU, интервальная (COGS меняется во времени)
--   wb_raw.REF_SKU_MASTER         nm_id ↔ internal_sku, 25/25
-- Рекламный расход берётся из MART_SKU_DAILY.ad_spend — того же источника, что
-- сверен в ADS-1A и ADS-3; отдельного join к FACT_ADS_SKU_DAILY нет, поэтому
-- рассинхронизации между экономикой и рекламой возникнуть не может.
--
-- ── НАБОРЫ ──────────────────────────────────────────────────────────────────
-- 14 из 25 SKU — наборы, и у ВСЕХ есть явная себестоимость набора в справочнике
-- (cogs_origin_type = FF_ASSEMBLED_DERIVED либо IMPORTED_FINISHED_SET). Компонентная
-- экономика здесь не собирается: используется готовое значение уровня набора.
--
-- ── ВОЗВРАТЫ ────────────────────────────────────────────────────────────────
-- Числитель — buyouts_rub (валовая реализованная выручка выкупов, никогда < 0).
-- Себестоимость берётся СОВМЕСТИМОЙ: net_product_cogs = buyouts_qty × unit_cogs
-- − reversal_cogs, где reversal привязан к дате ИСХОДНОЙ продажи через srid.
-- Возвраты в MART живут отдельными полями (returns_qty/returns_rub, отрицательные)
-- и в базис выручки не входят — поэтому и их себестоимость вычитается только через
-- reversal-механизм, а не повторно.
-- ============================================================================

CREATE OR REPLACE VIEW `wb_mart.V_ADS_SKU_ECONOMIC_LIMITS`
OPTIONS (description = 'Stage ADS-4. Экономический ПОТОЛОК рекламы по SKU: max_ad_drr_breakeven = contribution_before_ads / buyouts_rub. Окна 7/28/90 закрытых суток. НЕ прибыль: хранение, штрафы, приёмка, фулфилмент, OPEX и налоги не включены, поэтому потолок — оптимистичная верхняя граница. Целевого DRR здесь нет (ADS-7). Отрицательная экономика не обрезается. Без COGS потолок = NULL.') AS
WITH bounds AS (
  SELECT LEAST(MAX(day), DATE_SUB(CURRENT_DATE('Europe/Moscow'), INTERVAL 1 DAY)) AS as_of_date
  FROM `wb_mart.MART_SKU_DAILY`
),
base AS (
  SELECT
    c.day, c.nm_id, c.internal_sku, c.is_bundle,
    m.buyouts_qty, m.buyouts_rub, m.returns_qty, m.returns_rub,
    m.marketplace_fee_rub, m.logistics_cost_positive, m.ad_spend,
    c.net_product_cogs_operational_rub AS net_cogs,
    c.cogs_resolution_status,
    c.cogs_covered
  FROM `wb_mart.V_MART_SKU_DAILY_COGS` c
  JOIN `wb_mart.MART_SKU_DAILY` m ON m.day = c.day AND m.nm_id = c.nm_id
),
win AS (SELECT w AS window_days FROM UNNEST([7, 28, 90]) w),
agg AS (
  SELECT
    b.as_of_date, w.window_days, x.nm_id, x.internal_sku, x.is_bundle,
    SUM(x.buyouts_qty)                                     AS buyouts_qty,
    SUM(x.buyouts_rub)                                     AS buyouts_rub,
    SUM(x.returns_qty)                                     AS returns_qty,
    SUM(x.returns_rub)                                     AS returns_rub,
    SUM(x.marketplace_fee_rub)                             AS marketplace_fee_rub,
    SUM(x.logistics_cost_positive)                         AS logistics_cost_positive,
    SUM(x.ad_spend)                                        AS ad_spend_rub,
    -- Себестоимость суммируется ТОЛЬКО если покрыта на всех сутках окна.
    IF(COUNTIF(NOT x.cogs_covered) = 0, SUM(x.net_cogs), NULL) AS net_product_cogs_rub,
    COUNTIF(NOT x.cogs_covered)                            AS days_without_cogs,
    COUNT(*)                                               AS sku_days
  FROM base x
  CROSS JOIN bounds b
  CROSS JOIN win w
  WHERE x.day >  DATE_SUB(b.as_of_date, INTERVAL w.window_days DAY)
    AND x.day <= b.as_of_date
  GROUP BY 1,2,3,4,5
),
calc AS (
  SELECT a.*,
    a.buyouts_rub - a.marketplace_fee_rub - a.logistics_cost_positive - a.net_product_cogs_rub
                                                           AS contribution_before_ads_rub
  FROM agg a
)
SELECT
  c.as_of_date, c.window_days,
  c.internal_sku, c.nm_id, r.product_name_short, c.is_bundle,
  -- ── компоненты экономики ──
  c.buyouts_qty, c.buyouts_rub, c.returns_qty, c.returns_rub,
  c.marketplace_fee_rub, c.logistics_cost_positive, c.net_product_cogs_rub,
  c.ad_spend_rub,
  -- ── вклад ──
  c.contribution_before_ads_rub,
  c.contribution_before_ads_rub - c.ad_spend_rub           AS contribution_after_ads_and_product_cogs_rub,
  -- ── потолок и фактика ──
  SAFE_DIVIDE(c.ad_spend_rub, NULLIF(c.buyouts_rub, 0))                     AS actual_ad_drr,
  SAFE_DIVIDE(c.contribution_before_ads_rub, NULLIF(c.buyouts_rub, 0))      AS max_ad_drr_breakeven,
  SAFE_DIVIDE(c.contribution_before_ads_rub, NULLIF(c.buyouts_rub, 0))
    - SAFE_DIVIDE(c.ad_spend_rub, NULLIF(c.buyouts_rub, 0))                 AS drr_headroom_pp,
  -- Не обрезается: при отрицательном вкладе до рекламы оба поля отрицательны,
  -- и это честный сигнал «положительного допустимого бюджета не существует».
  c.contribution_before_ads_rub                                             AS max_ad_spend_breakeven_rub,
  c.contribution_before_ads_rub - c.ad_spend_rub                            AS ad_spend_headroom_rub,
  -- ── фактическое состояние: только следствие арифметики, без стратегии ──
  CASE
    WHEN c.net_product_cogs_rub IS NULL                     THEN 'MISSING_COGS'
    WHEN IFNULL(c.buyouts_rub, 0) <= 0                      THEN 'NO_REVENUE'
    WHEN c.contribution_before_ads_rub < 0                  THEN 'NEGATIVE_BEFORE_ADS'
    WHEN IFNULL(c.ad_spend_rub, 0) = 0                      THEN 'NO_AD_SPEND'
    WHEN c.ad_spend_rub > c.contribution_before_ads_rub     THEN 'ABOVE_BREAKEVEN'
    ELSE                                                         'BELOW_BREAKEVEN'
  END                                                                       AS economic_state,
  (c.days_without_cogs = 0)                                                 AS cogs_fully_covered,
  c.days_without_cogs, c.sku_days,
  'BEFORE_ADS_AFTER_PRODUCT_COGS_EXCL_ACCOUNT_LEVEL_COSTS'                  AS economics_basis,
  'Не включены: хранение, штрафы, приёмка, прочие удержания уровня счёта, фулфилмент, OPEX, налог. Это НЕ прибыль; потолок — верхняя граница.' AS economics_note
FROM calc c
LEFT JOIN `wb_raw.REF_SKU_MASTER` r ON r.nm_id = c.nm_id;
