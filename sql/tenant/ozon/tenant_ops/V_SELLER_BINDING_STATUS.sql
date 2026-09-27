-- Привязка к кабинету продавца по каждому API: журнал подтверждений оператора (ref, runtime его
-- только читает) против последнего наблюдения runtime (tenant_ops). Загрузка допустима только
-- при BOUND; всё остальное — fail closed.
-- Журнал привязок только дописывается. Действует последнее допустимое событие (CONFIRMED или
-- REVOKED) по времени события (revoked_at, иначе confirmed_at); при равном времени побеждает
-- REVOKED. INVALID_BINDING: действующее подтверждение не ссылается на наблюдение того же API с
-- тем же отпечатком, датировано будущим или в тот же момент подтверждены разные отпечатки.
-- Наблюдение засчитывается, только если сделано не раньше подтверждения; несколько наблюдений
-- в последний момент с разными отпечатками или пустой отпечаток — MISMATCH.
CREATE OR REPLACE VIEW `__tenant__.tenant_ops.V_SELLER_BINDING_STATUS`
OPTIONS(description = 'Статус привязки к кабинету продавца: BOUND, UNBOUND, MISMATCH, NOT_OBSERVED, INVALID_BINDING. Загрузка — только при BOUND.')
AS
WITH events AS (
  SELECT b.api, b.binding_id, b.identity_fingerprint, b.status, b.confirmed_at, b.confirmed_by,
    b.revoked_at, b.source_observation_id, COALESCE(b.revoked_at, b.confirmed_at) AS event_at
  FROM `__tenant__.ref.SELLER_BINDING` b
  WHERE b.status IN ('CONFIRMED', 'REVOKED')
),
latest AS (
  SELECT e.*
  FROM events e
  QUALIFY ROW_NUMBER() OVER (PARTITION BY e.api
                             ORDER BY e.event_at DESC, IF(e.status = 'REVOKED', 0, 1), e.binding_id DESC) = 1
),
same_moment AS (
  SELECT e.api, COUNT(DISTINCT e.identity_fingerprint) AS confirmed_fingerprints
  FROM events e
  JOIN latest l ON l.api = e.api AND l.event_at = e.event_at
  WHERE e.status = 'CONFIRMED'
  GROUP BY e.api
),
bound AS (
  SELECT l.api, l.binding_id, l.identity_fingerprint, l.confirmed_at, l.confirmed_by,
    l.event_at > CURRENT_TIMESTAMP() AS is_future,
    IFNULL(sm.confirmed_fingerprints, 0) > 1 AS is_conflicting,
    EXISTS (
      SELECT 1 FROM `__tenant__.tenant_ops.SELLER_IDENTITY_OBSERVATIONS` src
      WHERE src.observation_id = l.source_observation_id AND src.api = l.api
        AND src.identity_fingerprint = l.identity_fingerprint
    ) AS has_source_observation
  FROM latest l
  LEFT JOIN same_moment sm ON sm.api = l.api
  WHERE l.status = 'CONFIRMED' AND l.revoked_at IS NULL
),
last_seen AS (
  SELECT o.api, MAX(o.observed_at) AS observed_at
  FROM `__tenant__.tenant_ops.SELLER_IDENTITY_OBSERVATIONS` o
  GROUP BY o.api
),
observed AS (
  SELECT o.api, o.observed_at, MAX(o.observation_id) AS observation_id,
    COUNT(DISTINCT o.identity_fingerprint) AS fingerprints,
    COUNTIF(o.identity_fingerprint IS NULL) AS empty_fingerprints,
    MAX(o.identity_fingerprint) AS identity_fingerprint
  FROM `__tenant__.tenant_ops.SELLER_IDENTITY_OBSERVATIONS` o
  JOIN last_seen s ON s.api = o.api AND s.observed_at = o.observed_at
  GROUP BY o.api, o.observed_at
),
apis AS (SELECT api FROM events UNION DISTINCT SELECT api FROM observed)
SELECT a.api, b.binding_id, b.confirmed_at, b.confirmed_by, o.observation_id, o.observed_at,
  CASE
    WHEN b.binding_id IS NULL THEN 'UNBOUND'
    WHEN b.is_future OR b.is_conflicting OR NOT b.has_source_observation THEN 'INVALID_BINDING'
    WHEN o.observed_at IS NULL OR o.observed_at < b.confirmed_at THEN 'NOT_OBSERVED'
    WHEN o.fingerprints = 1 AND o.empty_fingerprints = 0 AND o.identity_fingerprint = b.identity_fingerprint THEN 'BOUND'
    ELSE 'MISMATCH'
  END AS binding_status
FROM apis a
LEFT JOIN bound b ON b.api = a.api
LEFT JOIN observed o ON o.api = a.api;
