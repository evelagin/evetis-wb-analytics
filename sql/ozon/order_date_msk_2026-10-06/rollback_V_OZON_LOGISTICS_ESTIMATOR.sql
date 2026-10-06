-- Откат 2026-10-06: определение из main 30f2de9 (= production 2026-10-06 по canonical_body_sha256).
-- ============================================================================
-- CANONICAL CURRENT DEFINITION — ozon_mart.V_OZON_LOGISTICS_ESTIMATOR (VIEW)
-- Git-first object (Gate 8, 2026-09-21): not in production until deployed. Rules:
-- sql/current/README.md. Metadata: MANIFEST.json.
--
-- ОЦЕНЩИК ЛОГИСТИКИ НА РЕАЛИЗОВАННУЮ ЕДИНИЦУ. В отличие от комиссии, логистика не является
-- тарифом: она зависит от кластера, плеча и фактического маршрута, и детерминированного
-- источника у неё нет. Поэтому здесь статистика — но выбранная бэктестом, а не вкусом.
--
-- БЭКТЕСТ (Gate 8, 547 прогнозов out-of-sample, апрель-сентябрь 2026, прогрев 60 наблюдений;
-- оценка строится ТОЛЬКО по наблюдениям строго ДО прогнозируемого; ошибка со знаком,
-- «занижение» = оценка меньше факта = мнимая прибыль сегодня и убыток потом):
--
-- Ставка логистики СМЕНИЛА РЕЖИМ: медиана на единицу держалась около 50 ₽ по февраль 2026
-- и с марта-апреля встала на 78-89 ₽. Поэтому оценщик на ПОЛНОЙ истории систематически
-- переоценивает (p75 по всей истории даёт 105 ₽ там, где факт около 86), и окно наблюдений
-- обязательно. Бэктест сравнивал окна 60/90/120/180/270 суток и полную историю на пяти
-- перцентилях; прогноз строится ТОЛЬКО по наблюдениям строго ДО прогнозируемых суток.
--
--   окно  кв     MAE   смещение  занижений  RMSE      (цель: заказы с 2026-04-01, 607 прогнозов)
--    120 p70   14,08     +1,26     35,7 %  23,63   <- выбран
--     90 p70   14,83     +3,41     34,1 %  24,15
--     60 p65   14,33     +1,33     40,9 %  24,21
--    180 p70   15,60     +3,64     32,8 %  24,28
--    120 p75   16,73     +6,60     28,8 %  25,15
--    все p75   17,08     +3,05     40,4 %  25,92
--    все p70   15,87     -5,61     53,9 %  25,66
--
--   то же на установившемся режиме (цель: заказы с 2026-06-01):
--    120 p70    9,54     +0,42     38,6 %  14,61   <- выбран
--     90 p75    9,84     +0,86     36,1 %  14,73
--     60 p75   10,48     +1,71     33,6 %  14,96
--
-- Выбран SKU_P70_120D: лучший RMSE среди оценщиков БЕЗ систематического занижения в обоих
-- испытаниях, смещение +1,26 ₽ на переходе режима и +0,42 ₽ на установившемся. Оценщики с
-- меньшей MAE (p60, медиана) платят за неё занижением расхода в 50-70 % случаев — ровно той
-- мнимой прибылью сегодня и убытком потом, которую владелец просил не допускать.
--
-- Разброс между SKU реален (медианы от 50 до 107 ₽ на единицу) и отражает вес и габариты,
-- поэтому зерно — SKU. При числе наблюдений меньше MIN_SAMPLES (8) своей статистики нет,
-- и используется общерыночный p70 ТОГО ЖЕ окна: у нового товара нет истории, но расход будет.
--
-- Окно отсчитывается от СЕГОДНЯ, а не от оцениваемых суток: оценка живёт ровно до прихода
-- факта, и пересчитывается она сегодняшним знанием. Пришедший факт вытесняет оценку.
--
-- ВАЖНО: оценщик описывает ЛОГИСТИКУ РЕАЛИЗОВАННЫХ единиц. Логистика по нереализованным
-- (отменённым) отправлениям живёт в «Прочих прямых» и здесь не моделируется.
-- Internal dependencies: none.
-- ============================================================================
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_LOGISTICS_ESTIMATOR`
OPTIONS (description = "Оценщик логистики на реализованную единицу, зерно = internal_sku. Метод SKU_P70_120D: 70-й перцентиль логистики на единицу по своему SKU за скользящие 120 суток, при числе наблюдений < 8 - общерыночный p70 того же окна. Выбран бэктестом out-of-sample по окнам 60/90/120/180/270/вся история на пяти перцентилях: лучший RMSE среди оценщиков без систематического занижения и на переходе режима (смещение +1,26 руб., MAE 14,08, RMSE 23,63), и на установившемся режиме (смещение +0,42 руб., MAE 9,54, RMSE 14,61). Окно обязательно: ставка логистики сменила режим (около 50 руб. на единицу по февраль 2026, 78-89 руб. с марта-апреля), и оценщик на полной истории переоценивает. Оценивает логистику РЕАЛИЗОВАННЫХ единиц; логистика отменённых отправлений в оценщик не входит.")
AS
WITH map AS (
  SELECT DISTINCT internal_sku, marketplace_sku
  FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_CHANNEL_MAP`
  WHERE marketplace = 'OZON'),
post AS (
  SELECT DISTINCT posting_number, sku, order_date, quantity
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_POSTINGS_FBO`
  WHERE status = 'delivered'),
lg AS (
  SELECT posting_number, sku, SUM(-amount_rub) amt
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_FINANCE_ACCRUAL`
  WHERE type_id IN (32, 29, 28, 98, 30, 59, 45, 78, 9) AND posting_number IS NOT NULL
  GROUP BY 1, 2),
obs AS (
  SELECT m.internal_sku, l.amt / p.quantity per_unit
  FROM post p JOIN lg l USING (posting_number, sku) JOIN map m ON m.marketplace_sku = p.sku
  WHERE p.quantity > 0 AND l.amt > 0
    AND p.order_date > DATE_SUB(CURRENT_DATE('Europe/Moscow'), INTERVAL 120 DAY)),
mkt AS (
  SELECT APPROX_QUANTILES(per_unit, 100)[OFFSET(70)] p70,
         APPROX_QUANTILES(per_unit, 100)[OFFSET(50)] p50,
         COUNT(*) n
  FROM obs),
bysku AS (
  SELECT internal_sku, COUNT(*) samples,
         APPROX_QUANTILES(per_unit, 100)[OFFSET(70)] p70,
         APPROX_QUANTILES(per_unit, 100)[OFFSET(50)] p50
  FROM obs GROUP BY 1)
SELECT
  m.internal_sku,
  IFNULL(bysku.samples, 0) samples,
  IF(IFNULL(bysku.samples, 0) >= 8, bysku.p70, mkt.p70) logistics_per_unit_rub,
  IF(IFNULL(bysku.samples, 0) >= 8, 'SKU_P70_120D', 'MARKETPLACE_P70_120D') estimator_method,
  bysku.p50 sku_median_per_unit_rub,
  mkt.p70 marketplace_p70_rub,
  mkt.n marketplace_samples,
  8 min_samples,
  120 window_days
FROM map m
LEFT JOIN bysku USING (internal_sku)
CROSS JOIN mkt
