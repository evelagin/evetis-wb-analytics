-- ============================================================================
-- PR-PROMO-2 · CANONICAL PROMOTION STATE · wb_mart.V_WB_PROMO_OBSERVATION_HISTORY (VIEW)
-- Грейн: observation_id. Одна строка на ГОДНЫЙ снимок календаря акций WB.
--
-- Годный снимок = environment 'prod', id по контракту слота (WBPROMO_prod_YYYYMMDDHHMM),
-- последняя строка манифеста имеет статус COMPLETE или REUSED. ERROR и STARTED не годны:
-- их строки RAW могут быть неполными. Снимок с id вне контракта слота (например, будущий
-- исторический бэкфилл) в каноническую историю не входит, пока для него не введён
-- собственный history_class — иначе он подменил бы текущее состояние.
--
-- observed_at — время ДАННЫХ (строки RAW), а не время последнего прогона слота. Для
-- слотов со статусом REUSED они различаются: манифест переписывает observed_at повтором,
-- строки остаются от первого успешного прогона (T1: 14:01:11 против 15:02:58).
-- Для снимка без строк (пустой календарь) берётся время манифеста — прогон был один.
--
-- Файл вне sql/current: датасет wb_mart не канонизирован (docs/architecture/
-- CANONICAL_COVERAGE.md §3), а частичный манифест дал бы в R2C 70 UNEXPECTED_LIVE.
-- ============================================================================
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_PROMO_OBSERVATION_HISTORY`
OPTIONS (description = "PR-PROMO-2. Годные снимки календаря акций WB: одна строка на observation_id. Годный = prod, id по контракту слота, статус манифеста COMPLETE или REUSED. observed_at — время данных из строк RAW (для REUSED отличается от времени последнего прогона в манифесте); без строк — время манифеста. Основа канонической истории и текущего состояния.")
AS
WITH manifest AS (
  SELECT observation_id, observation_bucket, status, observed_at AS manifest_observed_at
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.WB_PROMO_OBSERVATIONS`
  WHERE environment = 'prod'
    AND REGEXP_CONTAINS(observation_id, r'^WBPROMO_prod_[0-9]{12}$')
  QUALIFY ROW_NUMBER() OVER (PARTITION BY observation_id ORDER BY completed_at DESC, started_at DESC) = 1
),
data_time AS (
  SELECT observation_id, MIN(observed_at) AS data_observed_at, COUNT(DISTINCT promotion_id) AS promotions_observed
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_PROMO_CALENDAR`
  WHERE environment = 'prod'
  GROUP BY observation_id
)
SELECT
  'WB' AS marketplace,
  m.observation_id,
  m.observation_bucket AS observation_slot,
  COALESCE(d.data_observed_at, m.manifest_observed_at) AS observed_at,
  IF(d.data_observed_at IS NOT NULL, 'RAW_ROWS', 'MANIFEST') AS observed_at_basis,
  m.manifest_observed_at,
  'OBSERVED' AS history_class,
  m.status AS observation_status,
  IFNULL(d.promotions_observed, 0) AS promotions_observed,
  CAST(NULL AS INT64) AS catalog_products_observed
FROM manifest m
LEFT JOIN data_time d ON d.observation_id = m.observation_id
WHERE m.status IN ('COMPLETE', 'REUSED')
