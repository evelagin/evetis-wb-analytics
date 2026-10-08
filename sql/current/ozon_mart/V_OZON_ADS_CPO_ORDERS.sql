-- ============================================================================
-- CANONICAL CURRENT DEFINITION — ozon_mart.V_OZON_ADS_CPO_ORDERS (VIEW) · sync_state: pending_deploy
-- Phase B (OWNER ACK 2026-10-08): «Оплата за заказ» (CPO) Ozon по заказам. Rules: sql/current/README.md.
-- Док: docs/finance/OZON_CPO_PHASE_B_2026-10-08.md.
--
-- Каноническая строка CPO-заказа поверх ozon_raw.RAW_OZON_ADS_CPO_ORDERS (загрузчик ozon-cpo-orders).
-- RAW хранит только факты отчёта; всё производное вычисляется здесь на каждом чтении, поэтому
-- поздняя загрузка отправления или правка справочника лечит строку сама (без перезагрузки RAW).
--
-- ДВЕ АТРИБУЦИИ, не смешиваются:
--   финансовая  — ЗАКАЗАННЫЙ (оплаченный) SKU: ordered_sku → ordered_internal_sku. Это не «продано»:
--                 расход списан и за отменённый заказ (order_cancelled = TRUE остаётся расходом);
--   маркетинговая — ПРОДВИГАЕМЫЙ SKU: promoted_sku → promoted_internal_sku (V_OZON_ADS_CPO_PROMOTED_DAILY).
-- Заказанный ≠ продвигаемый в 57 строках из 315 (5 584,95 ₽, история 2025-05…2026-10).
--
-- ДАТЫ, не смешиваются:
--   charge_date    — «Дата» отчёта: дата списания (= дата биллинга кампании и финансов type 54 до задержки проводки);
--   order_date_msk — сутки МСК создания отправления (RAW_OZON_POSTINGS_FBO по (order_id, SKU));
--   business_date  — дата Юнитки: order_date_msk, а если отправление не найдено (FBS, ещё не загружено) —
--                    charge_date с business_date_basis = 'CHARGE_DATE_FALLBACK' (видно, не молча).
--
-- КАМПАНИЯ — по справочнику кампаний: payment_type = 'CPO' и adv_object_type = семейство отчёта.
-- type_id финансов CPO НЕ определяет: в 2025-05…08 type 54 нёс и CPC-кампании (14933060, 14957140,
-- 14984361, 15010239). Одна CPO-кампания семейства → RESOLVED. Если их станет несколько (пересоздание),
-- строка разрешается ПО СУТКАМ: единственная CPO-кампания семейства с биллингом в дату списания →
-- RESOLVED_BY_BILLING_DAY; иначе AMBIGUOUS (видна, в SKU не идёт, Юнитка поднимает OZON_CPO_UNRESOLVED).
-- Так появление второй кампании не обнуляет историю.
--
-- Несопоставленный SKU не отбрасывается: строка остаётся, ordered_mapping_status = 'UNMAPPED',
-- её расход в SKU-слой не попадает и остаётся на уровне магазина (V_OZON_ADS_CPO_RESIDUAL_DAILY).
-- Сторно в источнике не наблюдалось; отрицательная строка, если появится, проходит как есть.
-- Internal dependencies: none.
-- ============================================================================
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_ADS_CPO_ORDERS`
OPTIONS (description = "Оплата за заказ (CPO) Ozon, зерно = строка отчёта по заказам (семейство x дата списания x заказ x заказанный SKU x продвигаемый SKU). Финансовая атрибуция - ЗАКАЗАННЫЙ SKU (ordered_*), маркетинговая - ПРОДВИГАЕМЫЙ SKU (promoted_*). charge_date - дата списания; business_date - сутки МСК заказа (order_date_msk), при ненайденном отправлении - charge_date с business_date_basis = CHARGE_DATE_FALLBACK. Отменённый заказ сохраняет расход. Кампания - по справочнику (payment_type CPO + adv_object_type семейства; при нескольких - по биллингу в дату списания, RESOLVED_BY_BILLING_DAY), не по type_id финансов. Несопоставленный SKU остаётся строкой со статусом UNMAPPED. Итог CPO - биллинг кампании; сверка - V_OZON_ADS_CPO_RESIDUAL_DAILY.")
AS
WITH cpo_camp AS (
  SELECT DISTINCT campaign_id, adv_object_type report_family
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_ADS_CAMPAIGNS`
  WHERE payment_type = 'CPO'),
camp AS (
  SELECT report_family, ARRAY_AGG(campaign_id ORDER BY campaign_id) ids FROM cpo_camp GROUP BY 1),
billed_day AS (
  SELECT c.report_family, e.date charge_date, ARRAY_AGG(DISTINCT e.campaign_id ORDER BY e.campaign_id) ids
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_ADS_EXPENSE_DAILY` e
  JOIN cpo_camp c ON c.campaign_id = e.campaign_id
  WHERE e.expense_rub != 0
  GROUP BY 1, 2),
post AS (
  SELECT CAST(order_id AS STRING) order_id, sku, MIN(created_at) order_created_at,
         MIN(DATE(created_at, 'Europe/Moscow')) order_date_msk,
         COUNT(DISTINCT posting_number) postings,
         STRING_AGG(DISTINCT status, ',' ORDER BY status) posting_statuses,
         LOGICAL_AND(status = 'cancelled') all_cancelled
  FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_POSTINGS_FBO`
  GROUP BY 1, 2),
m AS (
  SELECT marketplace_sku, MIN(internal_sku) internal_sku, MIN(offer_id) offer_id,
         COUNT(DISTINCT internal_sku) internal_skus
  FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_CHANNEL_MAP`
  WHERE marketplace = 'OZON'
  GROUP BY 1)
SELECT
  r.row_key, r.report_family,
  CASE WHEN ARRAY_LENGTH(c.ids) = 1 THEN c.ids[OFFSET(0)]
       WHEN ARRAY_LENGTH(bd.ids) = 1 THEN bd.ids[OFFSET(0)] END campaign_id,
  CASE WHEN c.ids IS NULL THEN 'NO_CPO_CAMPAIGN'
       WHEN ARRAY_LENGTH(c.ids) = 1 THEN 'RESOLVED'
       WHEN ARRAY_LENGTH(bd.ids) = 1 THEN 'RESOLVED_BY_BILLING_DAY'
       ELSE 'AMBIGUOUS' END campaign_status,
  r.charge_date,
  r.order_id, r.order_number,
  r.ordered_sku,
  IF(mo.internal_skus = 1, mo.internal_sku, NULL) ordered_internal_sku,
  IF(mo.internal_skus = 1, mo.offer_id, NULL) ordered_offer_id,
  r.ordered_offer_id_reported,
  CASE WHEN mo.marketplace_sku IS NULL THEN 'UNMAPPED'
       WHEN mo.internal_skus > 1 THEN 'AMBIGUOUS'
       ELSE 'MAPPED' END ordered_mapping_status,
  r.promoted_sku,
  IF(mp.internal_skus = 1, mp.internal_sku, NULL) promoted_internal_sku,
  IF(mp.internal_skus = 1, mp.offer_id, NULL) promoted_offer_id,
  CASE WHEN mp.marketplace_sku IS NULL THEN 'UNMAPPED'
       WHEN mp.internal_skus > 1 THEN 'AMBIGUOUS'
       ELSE 'MAPPED' END promoted_mapping_status,
  r.ordered_sku != r.promoted_sku ordered_differs_from_promoted,
  r.order_source, r.product_name, r.quantity,
  r.unit_sale_price_rub, r.sale_value_rub, r.rate_pct, r.rate_rub, r.expense_rub,
  p.order_created_at,
  p.order_date_msk,
  IFNULL(p.order_date_msk, r.charge_date) business_date,
  IF(p.order_created_at IS NULL, 'CHARGE_DATE_FALLBACK', 'ORDER_DATE_MSK') business_date_basis,
  p.postings, p.posting_statuses,
  IFNULL(p.all_cancelled, FALSE) order_cancelled,
  r.completeness_status, r.report_uuid, r.requested_from, r.requested_to, r.fetched_at, r.run_id
FROM `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_ADS_CPO_ORDERS` r
LEFT JOIN camp c USING (report_family)
LEFT JOIN billed_day bd ON bd.report_family = r.report_family AND bd.charge_date = r.charge_date
LEFT JOIN post p ON p.order_id = r.order_id AND p.sku = r.ordered_sku
LEFT JOIN m mo ON mo.marketplace_sku = r.ordered_sku
LEFT JOIN m mp ON mp.marketplace_sku = r.promoted_sku;
