-- Текущее состояние жизненного цикла арендатора — последнее событие автомата.
CREATE OR REPLACE VIEW `__tenant__.tenant_ops.V_TENANT_STATE_CURRENT`
OPTIONS(description = 'Текущее состояние арендатора (последнее событие TENANT_STATE_EVENTS).')
AS
SELECT e.tenant_id, e.to_state AS state, e.reason_code, e.reason_detail, e.actor, e.run_id, e.occurred_at
FROM `__tenant__.tenant_ops.TENANT_STATE_EVENTS` e
QUALIFY ROW_NUMBER() OVER (PARTITION BY e.tenant_id ORDER BY e.occurred_at DESC, e.event_id DESC) = 1;
