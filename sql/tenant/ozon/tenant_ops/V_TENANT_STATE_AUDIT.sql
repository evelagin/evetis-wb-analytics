-- Аудит журнала состояний арендатора (T5): каждое событие TENANT_STATE_EVENTS против автомата
-- lifecycle_core.EDGES (рёбра и исполнители сверяет тест с кодом). Журнал только дописывается;
-- переход без валидатора (или вставка мимо control) видна здесь как VIOLATION:
--   CHAIN — from_state не равен состоянию предыдущего события (прыжок, параллельная запись);
--   EDGE  — такого ребра в автомате нет (например, VALIDATING → READY);
--   ACTOR — ребро есть, но исполнитель не допустим (например, READY от OPERATOR).
-- Порядок событий — как lifecycle_core.ordered: occurred_at, затем event_id.
-- first_violation_at — момент первой ошибки журнала: всё после неё недостоверно.
CREATE OR REPLACE VIEW `__tenant__.tenant_ops.V_TENANT_STATE_AUDIT`
OPTIONS(description = 'Аудит журнала состояний: каждое событие против автомата T5 (CHAIN, EDGE, ACTOR); OK или VIOLATION.')
AS
WITH edges AS (
  SELECT CAST(NULL AS STRING) AS from_state, 'CREDENTIALS_PENDING' AS to_state, 'OPERATOR' AS actor_class
  UNION ALL SELECT 'CREDENTIALS_PENDING', 'VALIDATING', 'OPERATOR'
  UNION ALL SELECT 'VALIDATING', 'CAPABILITY_DISCOVERY', 'CONTROL'
  UNION ALL SELECT 'CAPABILITY_DISCOVERY', 'READY_FOR_BACKFILL', 'CONTROL'
  UNION ALL SELECT 'READY_FOR_BACKFILL', 'BACKFILLING', 'OPERATOR'
  UNION ALL SELECT 'BACKFILLING', 'RECONCILING', 'CONTROL'
  UNION ALL SELECT 'RECONCILING', 'READY', 'CONTROL'
  UNION ALL SELECT 'RECONCILING', 'BACKFILLING', 'CONTROL'
  UNION ALL SELECT 'SUSPENDED', 'VALIDATING', 'OPERATOR'
  UNION ALL SELECT 'READY', 'VALIDATING', 'OPERATOR'
  UNION ALL SELECT 'CREDENTIALS_PENDING', 'SUSPENDED', 'OPERATOR'
  UNION ALL SELECT 'CREDENTIALS_PENDING', 'SUSPENDED', 'CONTROL'
  UNION ALL SELECT 'VALIDATING', 'SUSPENDED', 'OPERATOR'
  UNION ALL SELECT 'VALIDATING', 'SUSPENDED', 'CONTROL'
  UNION ALL SELECT 'CAPABILITY_DISCOVERY', 'SUSPENDED', 'OPERATOR'
  UNION ALL SELECT 'CAPABILITY_DISCOVERY', 'SUSPENDED', 'CONTROL'
  UNION ALL SELECT 'READY_FOR_BACKFILL', 'SUSPENDED', 'OPERATOR'
  UNION ALL SELECT 'READY_FOR_BACKFILL', 'SUSPENDED', 'CONTROL'
  UNION ALL SELECT 'BACKFILLING', 'SUSPENDED', 'OPERATOR'
  UNION ALL SELECT 'BACKFILLING', 'SUSPENDED', 'CONTROL'
  UNION ALL SELECT 'RECONCILING', 'SUSPENDED', 'OPERATOR'
  UNION ALL SELECT 'RECONCILING', 'SUSPENDED', 'CONTROL'
  UNION ALL SELECT 'READY', 'SUSPENDED', 'OPERATOR'
  UNION ALL SELECT 'READY', 'SUSPENDED', 'CONTROL'
),
ev AS (
  SELECT e.tenant_id, e.event_id, e.occurred_at, e.from_state, e.to_state, e.actor, e.run_id, e.reason_code,
    IF(STRPOS(e.actor, ':') > 0, SUBSTR(e.actor, 1, STRPOS(e.actor, ':') - 1), e.actor) AS actor_class,
    LAG(e.to_state) OVER (PARTITION BY e.tenant_id ORDER BY e.occurred_at, e.event_id) AS previous_state,
    ROW_NUMBER() OVER (PARTITION BY e.tenant_id ORDER BY e.occurred_at, e.event_id) AS seq
  FROM `__tenant__.tenant_ops.TENANT_STATE_EVENTS` e
),
checked AS (
  SELECT v.*,
    COALESCE(v.from_state, '-') = COALESCE(v.previous_state, '-') AS chain_ok,
    EXISTS (SELECT 1 FROM edges g
            WHERE COALESCE(g.from_state, '-') = COALESCE(v.from_state, '-') AND g.to_state = v.to_state) AS edge_ok,
    EXISTS (SELECT 1 FROM edges g
            WHERE COALESCE(g.from_state, '-') = COALESCE(v.from_state, '-') AND g.to_state = v.to_state
              AND g.actor_class = v.actor_class) AS actor_ok
  FROM ev v
),
judged AS (
  SELECT c.*,
    CASE WHEN NOT c.chain_ok THEN 'CHAIN' WHEN NOT c.edge_ok THEN 'EDGE' WHEN NOT c.actor_ok THEN 'ACTOR' END AS violation
  FROM checked c
)
SELECT j.tenant_id, j.seq, j.event_id, j.occurred_at, j.previous_state, j.from_state, j.to_state, j.actor_class,
  j.run_id, j.reason_code, IF(j.violation IS NULL, 'OK', 'VIOLATION') AS audit_status, j.violation,
  MIN(IF(j.violation IS NULL, NULL, j.occurred_at)) OVER (PARTITION BY j.tenant_id) AS first_violation_at
FROM judged j;
