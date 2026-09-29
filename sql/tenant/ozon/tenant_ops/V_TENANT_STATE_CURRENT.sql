-- Текущее состояние жизненного цикла арендатора по зеркалу TENANT_STATE_EVENTS.
-- Истина — маркеры tenant_locks.S_<seq> (lifecycle_core; tools/tenancy/tenant_lifecycle.py status), это
-- представление — их зеркало для SQL. Текущее — событие с НАИБОЛЬШИМ seq (номер перехода), а не с
-- последним occurred_at: часы владельца и control различаются, порядок задаёт только номер (T5).
-- Строки без seq — формат до T5: текущими бывают, только если нумерованных строк нет вовсе.
-- Повтор строки того же события (тот же event_id — повтор insertAll) схлопывается.
-- journal_status (как lifecycle_core.current_state / audit_history):
--   DUPLICATE — у одного номера разные события (разные event_id);
--   GAP       — номера не идут подряд 1..N (зеркало не получило строку или подлог);
--   NO_SEQ    — есть строки без номера (формат до T5);
--   OK        — иначе. Не OK — состояние по зеркалу недостоверно, сверять с маркерами.
CREATE OR REPLACE VIEW `__tenant__.tenant_ops.V_TENANT_STATE_CURRENT`
OPTIONS(description = 'Текущее состояние арендатора: событие TENANT_STATE_EVENTS с наибольшим seq; journal_status OK / GAP / DUPLICATE / NO_SEQ.')
AS
WITH ev AS (
  SELECT e.tenant_id, e.event_id, e.seq, e.to_state, e.reason_code, e.reason_detail, e.actor, e.run_id, e.occurred_at
  FROM `__tenant__.tenant_ops.TENANT_STATE_EVENTS` e
  QUALIFY ROW_NUMBER() OVER (PARTITION BY e.tenant_id, e.event_id ORDER BY e.occurred_at) = 1
),
stats AS (
  SELECT v.tenant_id, COUNT(v.seq) AS numbered_events, COUNT(DISTINCT v.seq) AS distinct_seq,
    MAX(v.seq) AS max_seq, COUNTIF(v.seq IS NULL) AS unnumbered_events
  FROM ev v
  GROUP BY v.tenant_id
),
cur AS (
  SELECT v.*
  FROM ev v
  QUALIFY ROW_NUMBER() OVER (PARTITION BY v.tenant_id
                             ORDER BY IF(v.seq IS NULL, 0, 1) DESC, v.seq DESC, v.occurred_at DESC, v.event_id DESC) = 1
)
SELECT c.tenant_id, c.to_state AS state, c.reason_code, c.reason_detail, c.actor, c.run_id, c.occurred_at,
  c.seq,
  CASE WHEN s.numbered_events > s.distinct_seq THEN 'DUPLICATE'
       WHEN s.numbered_events > 0 AND s.max_seq <> s.distinct_seq THEN 'GAP'
       WHEN s.unnumbered_events > 0 THEN 'NO_SEQ'
       ELSE 'OK' END AS journal_status
FROM cur c
JOIN stats s ON s.tenant_id = c.tenant_id;
