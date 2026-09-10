-- =====================================================================================
-- CONTROL TOWER PHASE 1.1 — НОВЫЕ ОБЪЕКТЫ (evetis_ref.CT_CONFIG, wb_mart.V_CT_*)
-- =====================================================================================
-- Дата: 2026-09-10. База: Phase 1 (075f45a).
-- Применять ПОСЛЕ ct_05_procedures.sql (V_CT_OWNER_HOME из ct_04b ссылается на
-- V_CT_REFRESH_STATUS, поэтому порядок с нуля: ct_07 §1–2 → ct_04b → ct_05 → ct_07 §3–5,
-- см. docs/control_tower/CT_PHASE11_IMPLEMENTATION_2026-09-10.md «Порядок применения»).
--
-- Объекты:
--   §1 evetis_ref.CT_CONFIG           — настройки владельца (URL web-app, расписание, SLA, force)
--   §2 wb_mart.V_CT_REFRESH_STATUS    — «CONTROL TOWER UPDATED AT» + OK / STALE / ERROR
--   §3 wb_mart.V_CT_SKU_CONTROL       — таблица контроля SKU × канал для Sales Plan
--   §4 wb_mart.V_CT_DAILY_BRIEF_LINES — ежедневная сводка построчно (секция × строка)
--   §5 wb_mart.V_CT_DAILY_BRIEF       — та же сводка одним текстом
-- Все объекты новые и аддитивные. Откат: tools/ct_phase11_rollback.sh.
-- Ничего в production (FACT/MART/V_DASH/Stage B/загрузчики) не меняется.
-- =====================================================================================

-- ── §1. Настройки Control Tower ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS `project-fa311fc0-4d87-4781-986.evetis_ref.CT_CONFIG`
(
  config_key   STRING NOT NULL OPTIONS(description="Ключ настройки Control Tower (action_webapp_url, refresh_schedule_msk, refresh_sla_hours, refresh_force, metabase_details_dashboard_url)"),
  config_value STRING          OPTIONS(description="Значение; NULL/пусто = не задано"),
  note         STRING          OPTIONS(description="Кто/зачем"),
  updated_at   TIMESTAMP NOT NULL
)
OPTIONS (description = 'Control Tower Phase 1.1: owner-maintained runtime settings (web-app URL for action status changes, refresh schedule/SLA, one-shot force flag). Additive CT object; rollback = DROP TABLE.');

MERGE `project-fa311fc0-4d87-4781-986.evetis_ref.CT_CONFIG` t
USING (
  SELECT 'action_webapp_url' AS config_key, CAST(NULL AS STRING) AS config_value,
         "URL web-app Apps Script (apps-script/CtOwnerActions.gs → Deploy → Web app → Execute as me, Only myself). Заполняется после деплоя: UPDATE evetis_ref.CT_CONFIG SET config_value = 'https://script.google.com/macros/s/…/exec', updated_at = CURRENT_TIMESTAMP() WHERE config_key = 'action_webapp_url'" AS note
  UNION ALL SELECT 'refresh_schedule_msk', '07:40, 09:40, 12:40, 16:40, 19:40', 'Cloud Scheduler ct-refresh-prod (infra/terraform/ct_refresh.tf), Europe/Moscow'
  UNION ALL SELECT 'refresh_sla_hours', '26', 'Через сколько часов без успешного sp_ct_refresh_daily витрина считается STALE'
  UNION ALL SELECT 'refresh_force', '0', 'Поставить 1, чтобы ближайший sp_ct_refresh_daily пересобрал витрины даже при неизменных источниках; сбрасывается в 0 автоматически'
  UNION ALL SELECT 'metabase_details_dashboard_url', NULL, 'Ссылка на дашборд «EVETIS CONTROL TOWER · ДЕТАЛИ» (drill-down)'
) s ON t.config_key = s.config_key
WHEN NOT MATCHED THEN INSERT (config_key, config_value, note, updated_at) VALUES (s.config_key, s.config_value, s.note, CURRENT_TIMESTAMP());

-- ── §2. Статус обновления Control Tower ─────────────────────────────────────────────
-- Одна строка: последний успешный sp_ct_refresh_daily, последняя попытка, OK / STALE / ERROR
-- (SLA в часах из CT_CONFIG.refresh_sla_hours), предупреждения freshness_gate за сегодня,
-- готовая строка «CONTROL TOWER UPDATED AT: …» для Owner Home.
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_REFRESH_STATUS`
OPTIONS (description = 'Control Tower Phase 1.1: one row — last successful sp_ct_refresh_daily, last attempt, status OK/STALE/ERROR (SLA hours from CT_CONFIG.refresh_sla_hours), schedule windows, warnings today. Feeds the CONTROL TOWER UPDATED AT line on Owner Home.') AS
WITH cfg AS (
  SELECT
    IFNULL(SAFE_CAST(MAX(IF(config_key = 'refresh_sla_hours', config_value, NULL)) AS FLOAT64), 26) AS sla_hours,
    MAX(IF(config_key = 'refresh_schedule_msk', config_value, NULL)) AS schedule_msk
  FROM `project-fa311fc0-4d87-4781-986.evetis_ref.CT_CONFIG`
),
runs AS (
  SELECT run_id, run_ts, status, message
  FROM `project-fa311fc0-4d87-4781-986.evetis_ref.CT_REFRESH_LOG`
  WHERE step = 'sp_ct_refresh_daily'
),
last_ok AS (SELECT run_ts, run_id FROM runs WHERE status = 'OK' ORDER BY run_ts DESC LIMIT 1),
last_any AS (SELECT run_ts, status, message, run_id FROM runs ORDER BY run_ts DESC LIMIT 1),
last_err AS (SELECT run_ts, message FROM runs WHERE status = 'ERROR' ORDER BY run_ts DESC LIMIT 1),
warn AS (
  SELECT COUNTIF(status = 'WARN') AS warn_today, MAX(IF(status = 'WARN', message, NULL)) AS warn_msg
  FROM `project-fa311fc0-4d87-4781-986.evetis_ref.CT_REFRESH_LOG`
  WHERE step = 'freshness_gate' AND DATE(run_ts, 'Europe/Moscow') = CURRENT_DATE('Europe/Moscow')
),
today AS (
  SELECT COUNTIF(status = 'OK') AS ok_today, COUNTIF(status = 'SKIP') AS skip_today, COUNTIF(status = 'ERROR') AS error_today
  FROM runs WHERE DATE(run_ts, 'Europe/Moscow') = CURRENT_DATE('Europe/Moscow')
),
x AS (
  SELECT
    last_ok.run_ts AS last_ok_ts, last_ok.run_id AS last_ok_run_id,
    last_any.run_ts AS last_attempt_ts, last_any.status AS last_attempt_status,
    last_err.run_ts AS last_error_ts, last_err.message AS last_error_message,
    TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), last_ok.run_ts, MINUTE) / 60.0 AS hours_since_ok,
    cfg.sla_hours, cfg.schedule_msk, warn.warn_today, warn.warn_msg, today.ok_today, today.skip_today, today.error_today
  FROM cfg CROSS JOIN warn CROSS JOIN today
  LEFT JOIN last_ok ON TRUE LEFT JOIN last_any ON TRUE LEFT JOIN last_err ON TRUE
)
SELECT
  x.*,
  CASE
    WHEN last_ok_ts IS NULL THEN 'ERROR'
    WHEN last_attempt_status = 'ERROR' AND last_error_ts > last_ok_ts THEN 'ERROR'
    WHEN hours_since_ok > sla_hours THEN 'STALE'
    ELSE 'OK'
  END AS refresh_status,
  FORMAT_TIMESTAMP('%d.%m.%Y %H:%M', last_ok_ts, 'Europe/Moscow') AS last_ok_msk,
  FORMAT_TIMESTAMP('%d.%m %H:%M', last_attempt_ts, 'Europe/Moscow') AS last_attempt_msk,
  CONCAT('CONTROL TOWER UPDATED AT: ', IFNULL(FORMAT_TIMESTAMP('%d.%m.%Y %H:%M', last_ok_ts, 'Europe/Moscow'), '—'), ' МСК',
         CASE
           WHEN last_ok_ts IS NULL THEN ' ● ERROR — ни одного успешного обновления'
           WHEN last_attempt_status = 'ERROR' AND last_error_ts > last_ok_ts THEN CONCAT(' ● ERROR — последняя попытка ', FORMAT_TIMESTAMP('%d.%m %H:%M', last_error_ts, 'Europe/Moscow'), ' упала')
           WHEN hours_since_ok > sla_hours THEN CONCAT(' ● STALE — ', CAST(CAST(ROUND(hours_since_ok) AS INT64) AS STRING), ' ч без обновления')
           ELSE ' ● OK'
         END,
         IF(warn_today > 0, CONCAT(' · ⚠ витрина продаж за вчера не собрана (', warn_msg, ')'), ''),
         ' · расписание ', IFNULL(schedule_msk, 'не задано'), ' МСК') AS status_line,
  CURRENT_TIMESTAMP() AS generated_at
FROM x;

-- ── §3. Таблица контроля SKU × канал (Sales Plan) ───────────────────────────────────
-- Только текущий месяц. is_past ограничен вчерашним днём — как на Owner Home
-- (Ozon отдаёт постинги за сегодня, иначе итоги SKU и Owner Home расходятся).
-- Прогноз EOM = факт за плановые дни + темп 7 дн. × оставшиеся плановые дни — то же
-- определение, что forecast_eom_cards_plan_days на Owner Home.
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_SKU_CONTROL`
OPTIONS (description = 'Control Tower Phase 1.1: SKU × marketplace control table for the current month — plan MTD (plan days only), actual MTD (same days, through yesterday), attainment %, forecast EOM (= actual on plan days + 7-day rate × remaining plan days, same definition as Owner Home), month plan, stock on marketplace, cover days at plan rate, status RED/YELLOW/GREEN/BLUE. Solo stock from V_CT_SUPPLY_NEED, bundle stock from V_CT_BUNDLE_STATUS. Feeds the Sales Plan SKU Control Table; details via drill-down.') AS
WITH pa AS (
  SELECT p.internal_sku, p.marketplace, p.sales_mode, ANY_VALUE(COALESCE(m.product_name_short, m.canonical_product_name, p.internal_sku)) AS product_name, ANY_VALUE(m.product_line) AS product_line,
    SUM(IF(p.is_past AND p.d < CURRENT_DATE() AND p.in_plan, p.target_cards, 0)) AS plan_cards_mtd,
    SUM(IF(p.is_past AND p.d < CURRENT_DATE() AND p.in_plan, IFNULL(p.actual_cards, 0), 0)) AS actual_cards_mtd,
    SUM(IF(p.is_past AND p.d < CURRENT_DATE(), IFNULL(p.actual_cards, 0), 0)) AS actual_cards_month_to_date_all,
    SUM(IF(p.is_past AND p.d < CURRENT_DATE(), IFNULL(p.actual_physical_units, 0), 0)) AS actual_units_mtd,
    SUM(IF(p.is_past AND p.d < CURRENT_DATE(), IFNULL(p.actual_gmv, 0), 0)) AS actual_gmv_mtd,
    SUM(IF(p.is_past AND p.d < CURRENT_DATE(), IFNULL(p.actual_contribution, 0), 0)) AS actual_contribution_mtd,
    SUM(p.target_cards) AS plan_cards_month,
    SUM(p.target_gmv) AS plan_gmv_month,
    SUM(p.target_contribution) AS plan_contribution_month,
    COUNTIF(p.in_plan AND NOT (p.is_past AND p.d < CURRENT_DATE())) AS plan_days_remaining,
    SUM(IF(p.is_past AND p.d < CURRENT_DATE() AND p.d > DATE_SUB(CURRENT_DATE(), INTERVAL 8 DAY), IFNULL(p.actual_cards, 0), 0)) / 7.0 AS cards_per_day_7d
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_PLAN_VS_ACTUAL_DAILY` p
  LEFT JOIN `project-fa311fc0-4d87-4781-986.evetis_ref.REF_PRODUCT_MASTER` m ON m.internal_sku = p.internal_sku
  WHERE p.month = DATE_TRUNC(CURRENT_DATE(), MONTH)
  GROUP BY 1, 2, 3
),
stock AS (
  SELECT marketplace, internal_sku, marketplace_units AS stock_units, in_transit_units, cover_days_at_plan_rate AS cover_days, recommended_ship_units, ff_total_units
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_SUPPLY_NEED`
  UNION ALL
  SELECT 'WB', bundle_sku, wb_cards_live, 0, days_cover_marketplace_at_plan_rate, NULL, assemblable_now_ff FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_BUNDLE_STATUS`
  UNION ALL
  SELECT 'OZON', bundle_sku, ozon_cards_live, ozon_cards_transit_api, days_cover_marketplace_at_plan_rate, NULL, assemblable_now_ff FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_BUNDLE_STATUS`
)
-- Phase 1.2: status_ru (СРОЧНО / ВНИМАНИЕ / ПО ПЛАНУ / РОСТ / ВНЕ ПЛАНА) и channel_ru — для владельческих таблиц без кодов
SELECT x.*,
  CASE x.status WHEN 'RED' THEN 'СРОЧНО' WHEN 'YELLOW' THEN 'ВНИМАНИЕ' WHEN 'BLUE' THEN 'РОСТ' WHEN 'GREEN' THEN 'ПО ПЛАНУ' ELSE 'ВНЕ ПЛАНА' END AS status_ru,
  CASE x.marketplace WHEN 'WB' THEN 'WB' WHEN 'OZON' THEN 'Ozon' ELSE x.marketplace END AS channel_ru,
  IF(x.sales_mode = 'BUNDLE', 'набор', 'товар') AS kind_ru
FROM (
SELECT pa.*, s.stock_units, s.in_transit_units, s.cover_days, s.recommended_ship_units, s.ff_total_units,
  SAFE_DIVIDE(pa.actual_cards_mtd, pa.plan_cards_mtd) * 100 AS attainment_pct,
  pa.actual_cards_mtd + pa.cards_per_day_7d * pa.plan_days_remaining AS forecast_eom_cards,
  SAFE_DIVIDE(pa.actual_cards_mtd + pa.cards_per_day_7d * pa.plan_days_remaining, pa.plan_cards_month) * 100 AS forecast_attainment_pct,
  CASE
    WHEN pa.plan_cards_month = 0 AND pa.actual_cards_month_to_date_all = 0 THEN 'GREY'
    WHEN (s.stock_units IS NOT NULL AND s.stock_units + IFNULL(s.in_transit_units, 0) <= 0 AND pa.plan_cards_month > 0) THEN 'RED'
    WHEN SAFE_DIVIDE(pa.actual_cards_mtd, pa.plan_cards_mtd) < 0.7 OR (s.cover_days IS NOT NULL AND s.cover_days < 7 AND pa.plan_cards_month > 0) THEN 'RED'
    WHEN SAFE_DIVIDE(pa.actual_cards_mtd, pa.plan_cards_mtd) < 0.9 OR (s.cover_days IS NOT NULL AND s.cover_days < 14 AND pa.plan_cards_month > 0) THEN 'YELLOW'
    WHEN SAFE_DIVIDE(pa.actual_cards_mtd, pa.plan_cards_mtd) > 1.2 THEN 'BLUE'
    ELSE 'GREEN'
  END AS status,
  CASE
    WHEN pa.plan_cards_month = 0 AND pa.actual_cards_month_to_date_all = 0 THEN 'вне плана'
    WHEN (s.stock_units IS NOT NULL AND s.stock_units + IFNULL(s.in_transit_units, 0) <= 0 AND pa.plan_cards_month > 0) THEN 'нет на площадке'
    WHEN s.cover_days IS NOT NULL AND s.cover_days < 7 AND pa.plan_cards_month > 0 THEN 'запаса < 7 дней'
    WHEN SAFE_DIVIDE(pa.actual_cards_mtd, pa.plan_cards_mtd) < 0.7 THEN 'сильно ниже плана'
    WHEN s.cover_days IS NOT NULL AND s.cover_days < 14 AND pa.plan_cards_month > 0 THEN 'запаса < 14 дней'
    WHEN SAFE_DIVIDE(pa.actual_cards_mtd, pa.plan_cards_mtd) < 0.9 THEN 'ниже плана'
    WHEN SAFE_DIVIDE(pa.actual_cards_mtd, pa.plan_cards_mtd) > 1.2 THEN 'выше плана — рост'
    ELSE 'по плану'
  END AS status_reason,
  CURRENT_DATE() AS as_of
FROM pa LEFT JOIN stock s ON s.marketplace = pa.marketplace AND s.internal_sku = pa.internal_sku) x;

-- ── §4. Ежедневная сводка владельца — построчно ─────────────────────────────────────
-- Секции (Phase 1.2): Вчера / План месяца / Главные отклонения / Сегодня сделать (≤ 4, исполнение) /
-- Решения владельца (≤ 3) / Данные. Итого ≤ 7 действий. Эффект всегда с типом (effect_line), «зачем» — why_owner.
-- V_CT_OWNER_HOME читается ОДИН раз (UNNEST массива STRUCT) — иначе BigQuery
-- разворачивает тяжёлую витрину в каждой ветке UNION ALL и падает по сложности плана.
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_DAILY_BRIEF_LINES`
OPTIONS (description = 'Control Tower Phase 1.1: Daily Owner Brief as lines (section × line). Sections: Вчера / План месяца / Главные отклонения / Сегодня сделать / Решения владельца / Данные. Max 7 actions total (4 execution + 3 decisions). No SQL terms. Built from V_CT_OWNER_HOME (single scan), V_CT_ACTION_QUEUE, V_CT_ATTENTION.') AS
WITH h AS (SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_OWNER_HOME`),
fmt AS (
  SELECT
    h.*,
    REPLACE(FORMAT('%\'d', CAST(ROUND(gmv_yesterday) AS INT64)), ',', ' ') AS gmv_y_s,
    REPLACE(FORMAT('%\'d', CAST(ROUND(plan_gmv_yesterday) AS INT64)), ',', ' ') AS plan_gmv_y_s,
    REPLACE(FORMAT('%\'d', CAST(ROUND(ABS(contribution_yesterday)) AS INT64)), ',', ' ') AS contrib_y_abs_s,
    REPLACE(FORMAT('%\'d', CAST(ROUND(plan_contribution_yesterday) AS INT64)), ',', ' ') AS plan_contrib_y_s,
    REPLACE(FORMAT('%\'d', CAST(ROUND(hc_stock_units) AS INT64)), ',', ' ') AS hc_units_s,
    REPLACE(FORMAT('%\'d', CAST(ROUND(hc_projected_residual_at_expiry) AS INT64)), ',', ' ') AS hc_resid_s
  FROM h
),
home_lines AS (
  SELECT l.*
  FROM fmt, UNNEST([
    STRUCT(1 AS section_ord, 'Вчера' AS section, 1 AS line_ord,
      CONCAT(FORMAT_DATE('%d.%m', yesterday_date), ': продано ', CAST(cards_ordered_yesterday AS STRING), ' позиций / ', CAST(units_yesterday AS STRING), ' физ. ед. на ', gmv_y_s, ' ₽',
             IF(plan_cards_yesterday > 0, CONCAT(' — ', CAST(CAST(ROUND(yesterday_attainment_pct) AS INT64) AS STRING), ' % плана дня (план ', CAST(CAST(ROUND(plan_cards_yesterday) AS INT64) AS STRING), ' поз. / ', plan_gmv_y_s, ' ₽)'), ''),
             '; позавчера ', CAST(cards_ordered_dby AS STRING), ' поз., неделю назад ', CAST(cards_ordered_lw AS STRING), ' поз.') AS line_text,
      IF(plan_cards_yesterday > 0 AND yesterday_attainment_pct < 70, 'RED', IF(plan_cards_yesterday > 0 AND yesterday_attainment_pct < 90, 'YELLOW', 'GREEN')) AS tone),
    STRUCT(1, 'Вчера', 2,
      CONCAT('Вклад ', IF(contribution_yesterday < 0, '−', '+'), contrib_y_abs_s, ' ₽ (план +', plan_contrib_y_s, ' ₽) · ДРР ', FORMAT('%.1f', drr_yesterday_pct), ' % (план ', FORMAT('%.1f', plan_drr_yesterday_pct), ' %) · WB ', CAST(wb_cards_yesterday AS STRING), ' / Ozon ', CAST(ozon_cards_yesterday AS STRING), ' поз.'),
      IF(contribution_yesterday < 0, 'RED', IF(contribution_yesterday < plan_contribution_yesterday, 'YELLOW', 'GREEN'))),
    STRUCT(1, 'Вчера', 3, IF(yesterday_data_complete, 'Данные за вчера полные (WB и Ozon закрыли день)', 'Данные за вчера НЕПОЛНЫЕ — WB или Ozon ещё не закрыли день'), IF(yesterday_data_complete, 'GREEN', 'YELLOW')),
    STRUCT(2, 'План месяца', 1,
      CONCAT('План продаж на прошедшие плановые дни ', CAST(CAST(ROUND(plan_cards_mtd) AS INT64) AS STRING), ' поз., факт продаж ', CAST(cards_ordered_plan_days_mtd AS STRING), ' — выполнение плана ', CAST(CAST(ROUND(mtd_attainment_pct) AS INT64) AS STRING), ' %'),
      IF(mtd_attainment_pct < 70, 'RED', IF(mtd_attainment_pct < 90, 'YELLOW', 'GREEN'))),
    STRUCT(2, 'План месяца', 2,
      CONCAT('План месяца ', CAST(CAST(ROUND(plan_cards_month) AS INT64) AS STRING), ' поз. · прогноз месяца ', CAST(CAST(ROUND(forecast_eom_cards_plan_days) AS INT64) AS STRING), ' (', CAST(CAST(ROUND(forecast_eom_attainment_pct) AS INT64) AS STRING), ' %) · нужно ', FORMAT('%.1f', required_daily_velocity_remaining), ' поз./день, факт ', FORMAT('%.1f', actual_daily_velocity_plan_days), ' поз./день'),
      IF(forecast_eom_attainment_pct < 70, 'RED', IF(forecast_eom_attainment_pct < 90, 'YELLOW', 'GREEN'))),
    STRUCT(2, 'План месяца', 3, CONCAT('Надёжность прогноза: ', forecast_confidence, ' (', forecast_confidence_ru, ') — ', forecast_confidence_reason), IF(forecast_confidence = 'LOW', 'YELLOW', 'GREEN')),
    STRUCT(3, 'Главные отклонения', 1,
      CONCAT('Крем для рук: ', hc_units_s, ' фл., ', CAST(hc_days_to_expiry AS STRING), ' дн. до срока годности; нужно ', FORMAT('%.1f', hc_required_units_per_day), '/день, факт ', FORMAT('%.1f', hc_actual_units_per_day_7d), '/день; риск списания ', hc_resid_s, ' фл. (', CAST(CAST(ROUND(hc_writeoff_risk_rub / 1000) AS INT64) AS STRING), ' тыс ₽ по себестоимости)'),
      hc_status),
    STRUCT(6, 'Данные', 1, ct_status_line, IF(ct_refresh_status = 'OK', 'GREEN', 'RED')),
    STRUCT(6, 'Данные', 2, IF(stale_domains_count = 0, 'Все источники свежие', CONCAT('Устарели: ', stale_domains)), IF(stale_domains_count = 0, 'GREEN', 'YELLOW'))
  ]) AS l
),
other_lines AS (
  SELECT 3 AS section_ord, 'Главные отклонения' AS section, 1 + ROW_NUMBER() OVER (ORDER BY sort_rank, metric_value) AS line_ord, headline AS line_text, severity AS tone
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_ATTENTION` WHERE severity = 'RED' AND alert_type != 'EXPIRY'
  QUALIFY ROW_NUMBER() OVER (ORDER BY sort_rank, metric_value) <= 3
  UNION ALL
  SELECT 4, 'Сегодня сделать', lane_rank,
    CONCAT(priority_short, ' · ', what_to_do, ' → ', executor_ru, ' · срок: ', deadline_ru, IFNULL(CONCAT(' · ', effect_line), ''), ' — ', why_owner), color
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_ACTION_QUEUE` WHERE is_open AND lane = 'EXECUTION' AND lane_rank <= 4
  UNION ALL
  SELECT 5, 'Решения владельца', lane_rank,
    CONCAT(priority_short, ' · ', what_to_do, ' · срок: ', deadline_ru, IFNULL(CONCAT(' · ', effect_line), ''), IF(why_owner IS NULL OR why_owner = '', '', CONCAT(' — ', why_owner))), color
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_ACTION_QUEUE` WHERE is_open AND lane = 'DECISION' AND lane_rank <= 3
)
SELECT section_ord, section, line_ord, line_text, tone, CURRENT_DATE() AS as_of FROM home_lines
UNION ALL
SELECT section_ord, section, line_ord, line_text, tone, CURRENT_DATE() FROM other_lines;

-- ── §5. Ежедневная сводка владельца — одним текстом ────────────────────────────────
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_DAILY_BRIEF`
OPTIONS (description = 'Control Tower Phase 1.1: Daily Owner Brief as one text (newline separated), answering «Что сегодня делать по EVETIS?». Same sources as Owner Home.') AS
SELECT CURRENT_DATE() AS as_of,
  CONCAT('EVETIS · ежедневная сводка владельца · ', FORMAT_DATE('%d.%m.%Y', CURRENT_DATE()), '\n\n',
    STRING_AGG(CONCAT(section, '\n', body), '\n\n' ORDER BY section_ord)) AS brief_text
FROM (
  SELECT section_ord, section, STRING_AGG(CONCAT('• ', line_text), '\n' ORDER BY line_ord) AS body
  FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_DAILY_BRIEF_LINES` GROUP BY 1, 2
);
