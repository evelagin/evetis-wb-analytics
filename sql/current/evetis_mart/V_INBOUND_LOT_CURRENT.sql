-- ============================================================================
-- CANONICAL CURRENT DEFINITION — evetis_mart.V_INBOUND_LOT_CURRENT (VIEW)
-- Authoritative Git definition. Rules: sql/current/README.md. Metadata: MANIFEST.json.
-- Origin: PR-PLAN-1 (Git-first, pending_deploy). Contract:
-- docs/plan/PR_PLAN_1_SALES_PLAN_TRAJECTORY_2026-09-25.md.
--
-- Текущее состояние каждой партии поступления = последнее событие inbound_id
-- (evetis_ref.INBOUND_LOT_EVENT). Партия ВИДНА всегда, но в базовую траекторию запаса входит,
-- только если доказаны SKU, количество, состояние и дата:
--   ELIGIBLE_CONFIRMED        ORDER_CONFIRMED | IN_PRODUCTION | PRODUCED | READY_FOR_SHIPMENT |
--                             IN_TRANSIT, без блокера, eta_status = CONFIRMED, дата не раньше
--                             даты запаса → входит в месяц даты ETA;
--   RECEIVED_NOT_IN_POSITION  RECEIVED после даты среза ФФ, на котором стоит позиция запаса →
--                             входит в текущий месяц количеством приёмки;
-- и не входит:
--   RECEIVED_IN_POSITION      приёмка не позже среза ФФ — единицы уже в позиции (не дважды);
--   EXCLUDED_CANCELLED, EXCLUDED_HYPOTHETICAL (PLANNED | HYPOTHETICAL),
--   BLOCKED                   есть блокер (например AWAITING_PAYMENT);
--   ETA_UNKNOWN               даты нет; ETA_NOT_CONFIRMED — дата только оценочная;
--   ETA_OVERDUE               подтверждённая дата уже прошла, а приёмки нет.
-- Порядок проверки — как в списке. Дата модели Control Tower (CT_INVENTORY_SNAPSHOT_DAILY.
-- inbound_eta) показана рядом как ct_model_inbound_eta и в расчёт не входит никогда.
--
-- Грейн: inbound_id.
-- ============================================================================
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.evetis_mart.V_INBOUND_LOT_CURRENT`
OPTIONS (description = "PR-PLAN-1. Партии поступления: последнее событие, состояние, блокер, дата и её статус, доказательство. inclusion_status объясняет, входит ли партия в базовую траекторию: только подтверждённая дата без блокера или приёмка после среза ФФ. Дата модели Control Tower — только для сравнения.")
AS
WITH ev AS (
  SELECT
    e.*,
    COUNT(*) OVER (PARTITION BY e.inbound_id) AS events_total,
    MIN(e.recorded_at) OVER (PARTITION BY e.inbound_id) AS first_recorded_at
  FROM `project-fa311fc0-4d87-4781-986.evetis_ref.INBOUND_LOT_EVENT` e
  QUALIFY ROW_NUMBER() OVER (PARTITION BY e.inbound_id ORDER BY e.recorded_at DESC) = 1
),
snap AS (
  -- Последняя партиция среза Control Tower: дата запаса, срез ФФ и дата модели по SKU.
  SELECT internal_sku, snapshot_date, ff_snapshot_date, inbound_units, inbound_eta
  FROM `project-fa311fc0-4d87-4781-986.evetis_ref.CT_INVENTORY_SNAPSHOT_DAILY`
  WHERE TRUE
  QUALIFY snapshot_date = MAX(snapshot_date) OVER ()
),
j AS (
  SELECT
    ev.*,
    s.snapshot_date AS inventory_as_of_date,
    s.ff_snapshot_date AS ff_anchor_date,
    s.inbound_units AS ct_model_inbound_units,
    s.inbound_eta AS ct_model_inbound_eta,
    CASE
      WHEN ev.lot_state = 'CANCELLED' THEN 'EXCLUDED_CANCELLED'
      WHEN ev.lot_state IN ('PLANNED', 'HYPOTHETICAL') THEN 'EXCLUDED_HYPOTHETICAL'
      WHEN ev.lot_state = 'RECEIVED' AND s.ff_snapshot_date IS NULL THEN 'RECEIVED_POSITION_UNKNOWN'
      WHEN ev.lot_state = 'RECEIVED' AND ev.received_date <= s.ff_snapshot_date THEN 'RECEIVED_IN_POSITION'
      WHEN ev.lot_state = 'RECEIVED' THEN 'RECEIVED_NOT_IN_POSITION'
      WHEN ev.blocker IS NOT NULL THEN 'BLOCKED'
      WHEN ev.eta_status = 'UNKNOWN' OR ev.eta_date IS NULL THEN 'ETA_UNKNOWN'
      WHEN ev.eta_status != 'CONFIRMED' THEN 'ETA_NOT_CONFIRMED'
      WHEN s.snapshot_date IS NULL OR ev.eta_date < s.snapshot_date THEN 'ETA_OVERDUE'
      ELSE 'ELIGIBLE_CONFIRMED'
    END AS inclusion_status
  FROM ev
  LEFT JOIN snap s USING (internal_sku)
)
SELECT
  inbound_id,
  internal_sku,
  quantity,
  lot_state,
  blocker,
  eta_date,
  eta_status,
  received_date,
  received_quantity,
  expiry_batch_id,
  evidence_class,
  evidence_ref,
  note,
  recorded_at AS state_recorded_at,
  recorded_by AS state_recorded_by,
  first_recorded_at,
  events_total,
  inventory_as_of_date,
  ff_anchor_date,
  inclusion_status,
  inclusion_status IN ('ELIGIBLE_CONFIRMED', 'RECEIVED_NOT_IN_POSITION') AS in_base_trajectory,
  CASE inclusion_status
    WHEN 'ELIGIBLE_CONFIRMED' THEN quantity
    WHEN 'RECEIVED_NOT_IN_POSITION' THEN received_quantity
  END AS trajectory_units,
  CASE inclusion_status
    WHEN 'ELIGIBLE_CONFIRMED' THEN DATE_TRUNC(eta_date, MONTH)
    WHEN 'RECEIVED_NOT_IN_POSITION' THEN DATE_TRUNC(inventory_as_of_date, MONTH)
  END AS trajectory_month,
  IF(lot_state IN ('ORDER_CONFIRMED', 'IN_PRODUCTION', 'PRODUCED', 'READY_FOR_SHIPMENT', 'IN_TRANSIT')
     AND inclusion_status NOT IN ('ELIGIBLE_CONFIRMED'), quantity, 0) AS committed_units_not_in_trajectory,
  ct_model_inbound_units,
  ct_model_inbound_eta,
  'CT_MODEL_DATE_FOR_COMPARISON_ONLY' AS ct_model_inbound_contract
FROM j
