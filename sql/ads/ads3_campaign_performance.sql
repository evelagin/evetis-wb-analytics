-- ============================================================================
-- Stage ADS-3 — производительность рекламы на уровне КАМПАНИИ.
-- Дата: 06.09.2026. Док: docs/ADS3_CAMPAIGN_PERFORMANCE_2026-09-06.md
--
-- 🔴 ПОЧЕМУ ЭТО VIEW, А НЕ FACT_ADS_CAMPAIGN_DAILY.
--    В этом проекте FACT_* — материализованные таблицы, которые ЦЕЛИКОМ пересобирает
--    wb_mart.sp_bootstrap_facts на каждом прогоне витрины. Таблица FACT_* вне этого
--    конвейера протухнет в тот же день. Добавить сборку в sp_bootstrap_facts сейчас
--    нельзя: его авторитетный исходник sql/mart/pr_mart1_facts.sql содержит отложенный
--    код Stage 3B (e30f668 / PR #116, §1.7 FACT_ADS_SPEND_ALLOC_DAILY, §1.8
--    FACT_ADS_SPEND_UNALLOC_DAILY) и открывается fail-closed 0-GATE, который запрещает
--    деплой до Phase B cutover. Проверено на проде: FACT_ADS_SPEND_ALLOC_DAILY не
--    существует, развёрнутая процедура на неё не ссылается — гейт закрыт корректно.
--    Поэтому слой отдан как вью: всегда свежий, без оркестрационного долга.
--    Материализация в FACT_ADS_CAMPAIGN_DAILY — один шаг после открытия гейта.
--
-- ── ГРЕЙН: day × advert_id ──────────────────────────────────────────────────
-- Доказано: в V_ADV_CAMPAIGN_STATS ключ (date, advertId, nmId, appType) уникален
-- (10 455 = числу строк); (date, advertId) даёт 3 334 — это и есть целевой грейн.
--
-- ── appType: СУММИРОВАТЬ БЕЗОПАСНО, доказано ────────────────────────────────
-- appType ∈ {0,1,32,64}. Строки 0 — 959 штук, во ВСЕХ метриках нули, и они
-- прекратились 16.07.2026: это пустой артефакт, а НЕ строка-итог. Компоненты
-- дизъюнктны: 323 877 (32) + 197 203 (64) + 22 738 (1) = 543 818 ₽ ≈ 543 817 ₽
-- в FACT_ADS_SKU_DAILY. Двойного счёта при агрегации нет.
-- 🔴 Семантика appType НЕ ДОКАЗАНА — ярлыки desktop/mobile здесь не вводятся.
--
-- ── КОЭФФИЦИЕНТЫ ────────────────────────────────────────────────────────────
-- ctr/cpc/cr в источнике — производные, проверено с нулевым расхождением:
--   ctr = 100 * clicks / views · cpc = sum / clicks · cr = 100 * orders / CLICKS
-- 🔴 Знаменатель WB-шного cr — КЛИКИ, а не корзины. Здесь все коэффициенты
-- пересчитываются из сумм и никогда не усредняются.
-- ============================================================================

CREATE OR REPLACE VIEW `wb_mart.V_ADS_CAMPAIGN_DAILY`
OPTIONS (description = 'Stage ADS-3. Производительность рекламы на грейне day × advert_id из V_ADV_CAMPAIGN_STATS (агрегация по appType — компоненты дизъюнктны). Коэффициенты пересчитаны из сумм. Конфигурация приклеена as-of (последний снимок НЕ ПОЗЖЕ дня) с явными config_snapshot_date/config_age_days. raw и dedup-атрибуция разделены именами.') AS
WITH parsed AS (
  SELECT
    SAFE.PARSE_DATE('%Y-%m-%d', SUBSTR(`date`, 1, 10))            AS d,
    SAFE_CAST(advertId AS INT64)                                  AS advert_id,
    SAFE_CAST(nmId AS INT64)                                      AS nm_id,
    SAFE_CAST(views AS INT64)                                     AS views,
    SAFE_CAST(clicks AS INT64)                                    AS clicks,
    SAFE_CAST(atbs AS INT64)                                      AS atbs,
    SAFE_CAST(orders AS INT64)                                    AS orders,
    SAFE_CAST(shks AS INT64)                                      AS shks,
    SAFE_CAST(canceled AS INT64)                                  AS canceled,
    SAFE_CAST(REPLACE(`sum`, ',', '.') AS NUMERIC)                AS spend,
    SAFE_CAST(REPLACE(sum_price, ',', '.') AS NUMERIC)            AS revenue
  FROM `wb_raw.V_ADV_CAMPAIGN_STATS`
),
-- Дедуп повторяет алгоритм FACT_ADS_SKU_DAILY (sp_bootstrap_facts §1.6) ДОСЛОВНО,
-- чтобы кампанийный слой не разошёлся с SKU-слоем: на группе (d, advert, nm)
-- Σ различных ненулевых revenue и Σ MAX(orders) на каждое различное значение.
-- Дедуп определён ВНУТРИ кампании, поэтому суммирование его по nm до кампании
-- детерминировано и владельца атрибуции не выдумывает.
dedup_nm AS (
  SELECT d, advert_id, nm_id,
         SUM(rev_val)            AS revenue_dedup,
         SUM(max_orders_for_val) AS orders_dedup_nz
  FROM (
    SELECT d, advert_id, nm_id, revenue AS rev_val, MAX(orders) AS max_orders_for_val
    FROM parsed WHERE revenue IS NOT NULL AND revenue <> 0
    GROUP BY d, advert_id, nm_id, revenue
  ) GROUP BY d, advert_id, nm_id
),
per_nm AS (
  SELECT p.d, p.advert_id, p.nm_id,
         IFNULL(ANY_VALUE(dd.revenue_dedup), 0)                                            AS revenue_dedup,
         IFNULL(ANY_VALUE(dd.orders_dedup_nz), 0)
           + SUM(IF(IFNULL(p.revenue, 0) = 0, p.orders, 0))                                AS orders_dedup,
         (COUNTIF(p.revenue IS NOT NULL AND p.revenue <> 0)
            > COUNT(DISTINCT IF(p.revenue <> 0, p.revenue, NULL)))                         AS multitouch_ambiguous
  FROM parsed p LEFT JOIN dedup_nm dd USING (d, advert_id, nm_id)
  GROUP BY p.d, p.advert_id, p.nm_id
),
agg AS (
  SELECT
    p.d AS day, p.advert_id,
    COUNT(DISTINCT p.nm_id)                                       AS nm_id_count,
    SUM(p.spend)                                                  AS ad_spend_rub,
    SUM(p.views)                                                  AS views,
    SUM(p.clicks)                                                 AS clicks,
    SUM(p.atbs)                                                   AS atbs,
    SUM(p.orders)                                                 AS ad_orders_raw,
    SUM(p.shks)                                                   AS shks,
    SUM(p.canceled)                                               AS canceled,
    SUM(IFNULL(p.revenue, 0))                                     AS ads_revenue_raw_rub
  FROM parsed p GROUP BY p.d, p.advert_id
),
agg_dedup AS (
  SELECT d AS day, advert_id,
         SUM(revenue_dedup)                                       AS ads_revenue_dedup_estimate_rub,
         SUM(orders_dedup)                                        AS ad_orders_dedup_estimate,
         COUNTIF(multitouch_ambiguous)                            AS nm_multitouch_ambiguous_count
  FROM per_nm GROUP BY d, advert_id
),
-- Кампанийные поля конфигурации константны внутри (snapshot_date, advert_id) —
-- проверено, 0 пар с расхождением, поэтому DISTINCT даёт ровно одну строку и
-- fan-out невозможен.
cfg AS (
  SELECT DISTINCT snapshot_date, advert_id, campaign_name, status_raw, campaign_type_raw,
         payment_type_raw, bid_type_raw, placement_search_enabled, placement_recommendations_enabled
  FROM `wb_raw.V_ADV_CAMPAIGN_CONFIG_DAILY`
)
SELECT
  a.day, a.advert_id, a.nm_id_count,
  -- ── аддитивные метрики ──
  a.ad_spend_rub, a.views, a.clicks, a.atbs, a.ad_orders_raw, a.shks, a.canceled,
  a.ads_revenue_raw_rub,
  -- ── dedup-оценка: ОЦЕНКА, не выручка; имя это говорит ──
  d.ads_revenue_dedup_estimate_rub, d.ad_orders_dedup_estimate, d.nm_multitouch_ambiguous_count,
  -- ── коэффициенты: пересчитаны из сумм, не усреднены ──
  SAFE_DIVIDE(a.clicks, a.views) * 100                            AS ctr_pct,
  SAFE_DIVIDE(a.ad_spend_rub, a.clicks)                           AS cpc_rub,
  SAFE_DIVIDE(a.ad_spend_rub, a.views) * 1000                     AS cpm_rub,
  SAFE_DIVIDE(a.ad_orders_raw, a.clicks) * 100                    AS cr_orders_per_click_pct,
  SAFE_DIVIDE(a.atbs, a.clicks) * 100                             AS cart_rate_per_click_pct,
  SAFE_DIVIDE(a.ad_orders_raw, a.atbs) * 100                      AS order_rate_per_cart_pct,
  SAFE_DIVIDE(a.ad_spend_rub, NULLIF(a.ad_orders_raw, 0))         AS cpo_raw_rub,
  SAFE_DIVIDE(a.ads_revenue_raw_rub, NULLIF(a.ad_spend_rub, 0))   AS roas_raw,
  -- ── конфигурация as-of: последний снимок НЕ ПОЗЖЕ дня ──
  c.snapshot_date                                                 AS config_snapshot_date,
  DATE_DIFF(a.day, c.snapshot_date, DAY)                          AS config_age_days,
  (c.snapshot_date = a.day)                                       AS config_is_same_day,
  c.campaign_name, c.status_raw, c.campaign_type_raw,
  c.payment_type_raw, c.bid_type_raw,
  c.placement_search_enabled, c.placement_recommendations_enabled
FROM agg a
LEFT JOIN agg_dedup d USING (day, advert_id)
-- as-of join. Конфигурации ДО 11.07.2026 не существует вовсе (89 суток
-- производительности), там все поля конфигурации останутся NULL — это честное
-- «неизвестно», а не подстановка. Разрыв 13–21.07 НЕ интерполируется молча:
-- значение переносится вперёд, но перенос ВИДЕН в config_age_days и
-- config_is_same_day, и потребитель сам решает свой порог устаревания.
LEFT JOIN cfg c
  ON c.advert_id = a.advert_id AND c.snapshot_date <= a.day
QUALIFY ROW_NUMBER() OVER (
  PARTITION BY a.day, a.advert_id ORDER BY c.snapshot_date DESC) = 1;
