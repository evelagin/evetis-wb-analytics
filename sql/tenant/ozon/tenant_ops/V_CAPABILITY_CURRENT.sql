-- Текущий профиль возможностей арендатора (T5): последняя строка CAPABILITY_PROFILE по каждой паре
-- (api, capability). Журнал только дописывает control (insertAll); здесь — только чтение.
-- is_ok — ровно lifecycle_core.OK_CAPABILITY: AVAILABLE или NOT_APPLICABLE. Всё прочее
-- (DENIED, UNAVAILABLE, UNKNOWN, нет строки) для READY — не пройдено.
CREATE OR REPLACE VIEW `__tenant__.tenant_ops.V_CAPABILITY_CURRENT`
OPTIONS(description = 'Текущий профиль возможностей: последняя проверка по каждой паре api/capability; is_ok = AVAILABLE или NOT_APPLICABLE.')
AS
SELECT p.api, p.capability, p.status, p.status IN ('AVAILABLE', 'NOT_APPLICABLE') AS is_ok,
  p.evidence_kind, p.http_status, p.notes, p.run_id, p.discovered_at
FROM `__tenant__.tenant_ops.CAPABILITY_PROFILE` p
QUALIFY ROW_NUMBER() OVER (PARTITION BY p.api, p.capability ORDER BY p.discovered_at DESC, p.profile_id DESC) = 1;
