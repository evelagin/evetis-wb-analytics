-- Привязка к кабинету продавца по каждому API: подтверждённая оператором запись (ref) против
-- последнего наблюдения runtime (tenant_ops). Загрузка допустима только при BOUND.
-- Журнал привязок только дописывается: действует последняя запись по API; отзыв — новая
-- строка REVOKED, и тогда статус UNBOUND, даже если раньше была CONFIRMED.
CREATE OR REPLACE VIEW `__tenant__.tenant_ops.V_SELLER_BINDING_STATUS`
OPTIONS(description = 'Статус привязки к кабинету продавца: BOUND, UNBOUND, MISMATCH, NOT_OBSERVED.')
AS
WITH latest AS (
  SELECT b.api, b.identity_fingerprint, b.binding_id, b.confirmed_at, b.confirmed_by, b.status, b.revoked_at
  FROM `__tenant__.ref.SELLER_BINDING` b
  QUALIFY ROW_NUMBER() OVER (PARTITION BY b.api ORDER BY COALESCE(b.revoked_at, b.confirmed_at) DESC, b.confirmed_at DESC) = 1
),
bound AS (
  SELECT l.api, l.identity_fingerprint, l.binding_id, l.confirmed_at, l.confirmed_by
  FROM latest l
  WHERE l.status = 'CONFIRMED' AND l.revoked_at IS NULL
),
observed AS (
  SELECT o.api, o.identity_fingerprint, o.observation_id, o.observed_at
  FROM `__tenant__.tenant_ops.SELLER_IDENTITY_OBSERVATIONS` o
  QUALIFY ROW_NUMBER() OVER (PARTITION BY o.api ORDER BY o.observed_at DESC) = 1
),
apis AS (SELECT api FROM latest UNION DISTINCT SELECT api FROM observed)
SELECT a.api, b.binding_id, b.confirmed_at, b.confirmed_by, o.observation_id, o.observed_at,
  CASE
    WHEN b.binding_id IS NULL THEN 'UNBOUND'
    WHEN o.observation_id IS NULL THEN 'NOT_OBSERVED'
    WHEN o.identity_fingerprint = b.identity_fingerprint THEN 'BOUND'
    ELSE 'MISMATCH'
  END AS binding_status
FROM apis a
LEFT JOIN bound b ON b.api = a.api
LEFT JOIN observed o ON o.api = a.api;
