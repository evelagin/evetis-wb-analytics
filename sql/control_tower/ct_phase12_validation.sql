-- =====================================================================================
-- EVETIS OWNER CONTROL TOWER — PHASE 1.2 — VALIDATION / REGRESSION SUITE (T33–T40)
-- =====================================================================================
-- Назначение : проверить владельческую упрощённость (Phase 1.2): человеческие подписи,
--              отсутствие обрезки «…» и технических кодов в текстах владельца, типизированный
--              финансовый эффект, статусы СРОЧНО/ВНИМАНИЕ/ПО ПЛАНУ/РОСТ, сопоставимые периоды KPI.
-- Запуск     : после ct_phase1_validation.sql (T1–T22) и ct_phase11_validation.sql (T23–T32);
--              bq query --use_legacy_sql=false < sql/control_tower/ct_phase12_validation.sql
-- Читает     : только V_CT_*. Ничего не пишет. Бизнес-логика Phase 1 / 1.1 не менялась.
-- Прогон 10.09.2026: 8/8 PASSED (T33–T38a job_ne4…, T38b–T40 job_ne4FxyU8fcs3izBKAp6jwkPVrzOu); T1–T22 и T23–T32 — PASSED после CALL sp_ct_refresh_daily()
--              (T10 требует свежего CT_ACTUAL_DAILY: Ozon добавил постинг за август после утреннего обновления).
-- =====================================================================================

-- T33. Тексты действий владельца не обрезаны: нет «…» / «...» в what_to_do и why_owner открытых действий
ASSERT (SELECT COUNTIF(what_to_do LIKE '%…' OR what_to_do LIKE '%...' OR why_owner LIKE '%…' OR why_owner LIKE '%...') = 0
        FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_ACTION_QUEUE` WHERE is_open)
  AS 'T33 FAILED: owner action text truncated with ellipsis';

-- T34. Финансовый эффект всегда типизирован: effect_line = «<тип>: <сумма>», тип из словаря
ASSERT (SELECT COUNTIF(financial_effect_rub IS NOT NULL AND (effect_kind_ru IS NULL OR effect_line NOT LIKE CONCAT(effect_kind_ru, ': %'))) = 0
          AND COUNTIF(effect_kind_ru IS NOT NULL AND effect_kind_ru NOT IN ('Потенциальная выручка', 'Потенциальный cash effect', 'Предотвращённый риск списания', 'Риск списания', 'Ожидаемый вклад', 'Перерасход рекламы', 'Стоимость решения', 'Выручка под риском')) = 0
          AND COUNTIF(financial_effect_rub IS NULL AND effect_line IS NOT NULL) = 0
        FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_ACTION_QUEUE`)
  AS 'T34 FAILED: financial effect must carry its type';

-- T35. «Зачем» без технических кодов и без перечня SKU (перечень — в sku_breakdown), у каждого открытого действия
ASSERT (SELECT COUNTIF(is_open AND (why_owner IS NULL OR why_owner = '')) = 0
          AND COUNTIF(is_open AND REGEXP_CONTAINS(why_owner, r'RAW_|V_ADS_|NOT_PROVEN|ABOVE_BREAKEVEN|NEGATIVE_BEFORE_ADS|ETA ASSUMPTION|BUNDLE_PRODUCTION_PLAN|ADVERTISING_ACTIONS')) = 0
          AND COUNTIF(is_open AND REGEXP_CONTAINS(what_to_do, r'RAW_|V_ADS_|BUNDLE_PRODUCTION_PLAN|ADVERTISING_ACTIONS')) = 0
          AND COUNTIF(is_open AND action_type IN ('PULL_FROM_PALLETS', 'REPLENISH_WB', 'REPLENISH_OZON') AND sku = 'PORTFOLIO' AND sku_breakdown IS NOT NULL AND why_owner LIKE '%, %мл %') = 0
        FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_ACTION_QUEUE`)
  AS 'T35 FAILED: why_owner must be one human sentence';

-- T36. Таблица SKU: статусы владельца из словаря и однозначно соответствуют цветам
ASSERT (SELECT COUNTIF(status_ru NOT IN ('СРОЧНО', 'ВНИМАНИЕ', 'ПО ПЛАНУ', 'РОСТ', 'ВНЕ ПЛАНА')) = 0
          AND COUNTIF((status = 'RED') != (status_ru = 'СРОЧНО')) = 0 AND COUNTIF((status = 'YELLOW') != (status_ru = 'ВНИМАНИЕ')) = 0
          AND COUNTIF((status = 'GREEN') != (status_ru = 'ПО ПЛАНУ')) = 0 AND COUNTIF((status = 'BLUE') != (status_ru = 'РОСТ')) = 0
          AND COUNTIF(channel_ru NOT IN ('WB', 'Ozon')) = 0
        FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_SKU_CONTROL`)
  AS 'T36 FAILED: SKU statuses must be СРОЧНО/ВНИМАНИЕ/ПО ПЛАНУ/РОСТ';

-- T37. Сопоставимые периоды KPI: даты и суммы позавчера / неделю назад заполнены и согласованы с V_CT_ACTUAL_DAILY
ASSERT (SELECT h.dby_date = DATE_SUB(h.yesterday_date, INTERVAL 1 DAY) AND h.lw_date = DATE_SUB(h.yesterday_date, INTERVAL 7 DAY)
          AND h.cards_ordered_lw = (SELECT SUM(cards_ordered) FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_ACTUAL_DAILY` a WHERE a.d = h.lw_date)
          AND h.cards_ordered_dby = (SELECT SUM(cards_ordered) FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_ACTUAL_DAILY` a WHERE a.d = h.dby_date)
          AND h.gmv_lw IS NOT NULL AND h.contribution_lw IS NOT NULL
        FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_OWNER_HOME` h)
  AS 'T37 FAILED: previous comparable period columns';

-- T38. Подписи владельца: «карточки» как единица измерения («20 карточек», «37 карт.») больше не встречаются
--      в текстах Owner Home / сводки / сигналов («одиночная карточка WB» как товарная карточка допустима)
ASSERT (SELECT NOT REGEXP_CONTAINS(LOWER(forecast_line), r'карточ') FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_OWNER_HOME`)
  AS 'T38a FAILED: Owner Home forecast_line still says «карточек»';
ASSERT (SELECT COUNTIF(REGEXP_CONTAINS(LOWER(line_text), r'\d+ (карточ|карт\.)')) = 0 FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_DAILY_BRIEF_LINES`)
  AS 'T38b FAILED: Daily brief still says «карточек»';
ASSERT (SELECT COUNTIF(REGEXP_CONTAINS(LOWER(headline), r'\d+ (карточ|карт\.)')) = 0 FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_ATTENTION`)
  AS 'T38c FAILED: Attention headlines still say «карточек»';

-- T39. Сводка дня: шесть разделов с владельческими названиями, ≤ 7 действий, все действия несут срок и «зачем»
ASSERT (SELECT ARRAY_TO_STRING(ARRAY_AGG(DISTINCT section ORDER BY section), '|') = 'Вчера|Главные отклонения|Данные|План месяца|Решения владельца|Сегодня сделать'
          AND COUNTIF(section_ord IN (4, 5)) <= 7
          AND COUNTIF(section_ord IN (4, 5) AND (line_text NOT LIKE '% · срок: %' OR line_text NOT LIKE '% — %')) = 0
        FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_DAILY_BRIEF_LINES`)
  AS 'T39 FAILED: daily brief sections';

-- T40. Расчёты не изменились: итоги SKU-таблицы, сводки и Owner Home совпадают (регрессия Phase 1.1 T29 + строка сводки)
ASSERT (SELECT s.a = h.cards_ordered_plan_days_mtd AND ABS(s.p - h.plan_cards_mtd) < 0.5
          AND (SELECT line_text FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_DAILY_BRIEF_LINES` WHERE section_ord = 2 AND line_ord = 1)
              LIKE CONCAT('%', CAST(h.cards_ordered_plan_days_mtd AS STRING), ' — выполнение плана ', CAST(CAST(ROUND(h.mtd_attainment_pct) AS INT64) AS STRING), ' %')
        FROM (SELECT SUM(actual_cards_mtd) a, SUM(plan_cards_mtd) p FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_SKU_CONTROL`) s,
             `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_OWNER_HOME` h)
  AS 'T40 FAILED: totals drifted between SKU table, brief and Owner Home';

SELECT 'CT PHASE 1.2 VALIDATION: ALL 8 TESTS PASSED (T33–T40)' AS result, CURRENT_TIMESTAMP() AS checked_at;
