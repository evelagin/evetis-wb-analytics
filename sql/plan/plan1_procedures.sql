-- ============================================================================
-- PR-PLAN-1 · процедуры записи плана продаж и поступлений (evetis_ref). Контракт:
-- docs/plan/PR_PLAN_1_SALES_PLAN_TRAJECTORY_2026-09-25.md
--
-- Запускаются ТОЛЬКО вручную (CALL из консоли или tools/plan_cli.py). Расписаний, сервисных
-- аккаунтов и автоматического утверждения нет. Каждая процедура либо записывает всё, либо
-- ничего (транзакция + RAISE), и после записи сверяет результат с выводом
-- evetis_mart.V_PLAN_VERSION_STATUS: хеш содержимого считается ТЕМ ЖЕ выражением, что и в
-- представлении, а при расхождении транзакция откатывается.
--
--   sp_plan_register_legacy_ct        регистрирует версию Control Tower (SET_2026-09-09) как
--                                     MODEL_SCENARIO: шапка + снимок BOM + допущения модели;
--                                     строки не копируются (адаптер V_PLAN_LINE_MONTHLY_ALL)
--   sp_plan_propose_observed_run_rate создаёт версию SYSTEM_PROPOSED методом
--                                     OBSERVED_RUN_RATE_30D_V1 (статус DRAFT)
--   sp_plan_submit                    DRAFT → PROPOSED (событие с точным хешем)
--   sp_plan_approve                   PROPOSED → APPROVED: только по решению владельца, с
--                                     ожидаемым хешем, месяцем начала действия и основанием
--   sp_plan_close                     WITHDRAWN (предложенная) | REVOKED (утверждённая)
--   sp_inbound_record                 событие партии поступления (перечисления проверяются)
--
-- Процедуры не трогают Control Tower (CT_*), C1 (evetis_ops), Юнитку и экономику.
-- Откат: sql/plan/plan1_rollback.sql.
-- ============================================================================

-- ─────────────────────────────────────────────────────────────── legacy SET → MODEL_SCENARIO
CREATE OR REPLACE PROCEDURE `project-fa311fc0-4d87-4781-986.evetis_ref.sp_plan_register_legacy_ct`(
  p_ct_plan_version STRING, p_created_by STRING, p_evidence_ref STRING)
BEGIN
  DECLARE v_now TIMESTAMP DEFAULT CURRENT_TIMESTAMP();
  DECLARE v_lines INT64;
  DECLARE v_bom INT64;
  DECLARE v_sha STRING;
  DECLARE v_status STRING;

  IF p_ct_plan_version IS NULL OR IFNULL(TRIM(p_created_by), '') = '' OR IFNULL(TRIM(p_evidence_ref), '') = '' THEN
    RAISE USING MESSAGE = 'PLAN: версия Control Tower, автор и основание обязательны';
  END IF;
  IF (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.evetis_ref.CT_PLAN_VERSION` WHERE plan_version = p_ct_plan_version) != 1 THEN
    RAISE USING MESSAGE = CONCAT('PLAN: в CT_PLAN_VERSION нет единственной версии ', p_ct_plan_version);
  END IF;
  IF (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.evetis_ref.PLAN_VERSION` WHERE plan_version = p_ct_plan_version) > 0 THEN
    RAISE USING MESSAGE = CONCAT('PLAN: версия ', p_ct_plan_version, ' уже зарегистрирована — шапки не переписываются');
  END IF;

  -- Строки — тем же выражением, что адаптер legacy в V_PLAN_LINE_MONTHLY_ALL.
  CREATE TEMP TABLE _lines AS
  SELECT c.month, UPPER(c.marketplace) AS marketplace, c.internal_sku,
    ROUND(CAST(SUM(c.target_cards) AS NUMERIC), 4) AS planned_cards
  FROM `project-fa311fc0-4d87-4781-986.evetis_ref.CT_SEASON_PLAN_MONTHLY` c
  WHERE c.plan_version = p_ct_plan_version
  GROUP BY c.month, UPPER(c.marketplace), c.internal_sku;

  -- Снимок BOM: действующие на дату регистрации строки состава для наборов этой версии.
  CREATE TEMP TABLE _bom AS
  SELECT b.bundle_internal_sku AS bundle_sku, b.component_internal_sku AS component_sku, b.component_qty,
    b.bundle_component_id AS source_row_id
  FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_BUNDLE_COMPONENTS` b
  WHERE b.bundle_internal_sku IN (SELECT DISTINCT internal_sku FROM _lines)
    AND b.effective_from <= CURRENT_DATE() AND (b.effective_to IS NULL OR b.effective_to >= CURRENT_DATE());

  SET v_lines = (SELECT COUNT(*) FROM _lines);
  SET v_bom = (SELECT COUNT(*) FROM _bom);
  SET v_sha = (
    SELECT TO_HEX(SHA256(CONCAT(
      IFNULL((SELECT STRING_AGG(CONCAT(CAST(month AS STRING), '|', marketplace, '|', internal_sku, '|', CAST(planned_cards AS STRING)), '\n'
                                ORDER BY CONCAT(CAST(month AS STRING), '|', marketplace, '|', internal_sku, '|', CAST(planned_cards AS STRING)))
              FROM _lines), ''),
      '\n#BOM\n',
      IFNULL((SELECT STRING_AGG(CONCAT(bundle_sku, '|', component_sku, '|', CAST(component_qty AS STRING)), '\n'
                                ORDER BY CONCAT(bundle_sku, '|', component_sku, '|', CAST(component_qty AS STRING)))
              FROM _bom), '')))));

  BEGIN
    BEGIN TRANSACTION;
    INSERT INTO `project-fa311fc0-4d87-4781-986.evetis_ref.PLAN_VERSION`
      (plan_version, plan_kind, plan_name, method_id, method_version, parent_plan_version, source_kind, source_ref,
       horizon_from, horizon_to, marketplaces, basis_sales_as_of, basis_inventory_as_of, basis_inventory_freshness,
       line_count, bom_basis_rows, content_sha256, created_at, created_by, notes)
    SELECT v.plan_version, 'MODEL_SCENARIO', CONCAT('Модельный сценарий Control Tower ', v.plan_version), 'LEGACY_CT_MODEL',
      v.assumption_version, NULL, 'LEGACY_CT_SEASON_PLAN_MONTHLY', v.plan_version,
      v.horizon_from, v.horizon_to, 'WB,OZON', NULL, v.opening_inventory_as_of, NULL,
      v_lines, v_bom, v_sha, v_now, p_created_by,
      CONCAT('Legacy: строки не копируются, читаются из CT_SEASON_PLAN_MONTHLY; хеш фиксирует их на ', CAST(v_now AS STRING),
             '. Утверждён быть не может. Основание: ', p_evidence_ref)
    FROM `project-fa311fc0-4d87-4781-986.evetis_ref.CT_PLAN_VERSION` v
    WHERE v.plan_version = p_ct_plan_version;

    INSERT INTO `project-fa311fc0-4d87-4781-986.evetis_ref.PLAN_BOM_BASIS`
      (plan_version, bundle_sku, component_sku, component_qty, source_row_id, captured_at)
    SELECT p_ct_plan_version, bundle_sku, component_sku, component_qty, source_row_id, v_now FROM _bom;

    -- Допущения модели (аудит SET_2026-09-09, 2026-09-25): видны рядом с версией, в новый план не наследуются.
    INSERT INTO `project-fa311fc0-4d87-4781-986.evetis_ref.PLAN_ASSUMPTION`
      (plan_version, assumption_id, assumption_type, scope_marketplace, scope_sku, scope_month, value_numeric, value_text,
       unit, evidence_class, source, created_at)
    SELECT p_ct_plan_version, a.id, a.t, a.mp, a.sku, NULL, a.v, a.txt, a.u, 'LEGACY_MODEL',
      CONCAT('model/ct.py; ', p_evidence_ref), v_now
    FROM UNNEST([
      STRUCT('L01' AS id, 'SEASONALITY' AS t, CAST(NULL AS STRING) AS mp, CAST(NULL AS STRING) AS sku, CAST(NULL AS NUMERIC) AS v,
             'Сезонные множители по месяцам модели Control Tower; не доказаны фактом' AS txt, 'multiplier' AS u),
      STRUCT('L02', 'LEGACY_MULTIPLIER', 'OZON', NULL, NUMERIC '1.25', 'Ozon = WB × 1,25 (допущение модели)', 'multiplier'),
      STRUCT('L03', 'LEGACY_MULTIPLIER', NULL, NULL, NUMERIC '2.5', 'Наборы × 2,5 в пиковые месяцы (допущение модели)', 'multiplier'),
      STRUCT('L04', 'LEGACY_MULTIPLIER', NULL, NULL, NUMERIC '1.1', 'Наборы × 1,1 вне пика (допущение модели)', 'multiplier'),
      STRUCT('L05', 'PROGRAM', NULL, 'EVT-HC-HAND-300', NULL, 'Программа HC-B: распродажа крема для рук к 31.12.2026 (политика, не утверждена в PLAN-1)', NULL),
      STRUCT('L06', 'INBOUND', NULL, 'EVT-FC-MOIST-50', NUMERIC '5000', 'Партия 8 в октябре 2026 (ETA модели 20.10.2026, не подтверждена)', 'units'),
      STRUCT('L07', 'INBOUND', NULL, 'EVT-FC-ACNE-50', NUMERIC '3000', 'Дозаказ 3 000 в декабре 2026 (допущение модели)', 'units'),
      STRUCT('L08', 'CALENDAR', NULL, NULL, NUMERIC '0.7333', 'Сентябрь 2026 масштабирован 22/30 (30-дневный месяц модели)', 'ratio')
    ]) a;

    SET v_status = (SELECT lifecycle_status FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_PLAN_VERSION_STATUS`
                    WHERE plan_version = p_ct_plan_version);
    IF v_status IS DISTINCT FROM 'MODEL_SCENARIO' THEN
      RAISE USING MESSAGE = CONCAT('PLAN: после записи статус ', IFNULL(v_status, '∅'), ' вместо MODEL_SCENARIO — откат');
    END IF;
    COMMIT TRANSACTION;
  EXCEPTION WHEN ERROR THEN
    ROLLBACK TRANSACTION;
    RAISE USING MESSAGE = @@error.message;
  END;

  SELECT plan_version, plan_kind, lifecycle_status, line_rows_actual, bom_rows_actual, content_sha256, integrity_ok
  FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_PLAN_VERSION_STATUS` WHERE plan_version = p_ct_plan_version;
END;

-- ─────────────────────────────────────────────────────────────── предложение системы
-- Метод OBSERVED_RUN_RATE_30D_V1:
--   темп(карточка, площадка) = заказанные карточки без отмен за 30 полных суток, оканчивающихся
--     sales_as_of (V_CT_ACTUAL_DAILY), / 30;
--   план месяца = ROUND(темп × все календарные дни месяца, 1). Горизонт — с месяца, следующего за
--     sales_as_of днём, по p_horizon_to (последний месяц — дни до p_horizon_to).
--   Вселенная — текущие карточки REF_SKU_CHANNEL_MAP (WB, OZON), нулевой темп → строка 0.
--   Нет сезонности, множителей Ozon/наборов, программ распродажи и предполагаемых поставок.
CREATE OR REPLACE PROCEDURE `project-fa311fc0-4d87-4781-986.evetis_ref.sp_plan_propose_observed_run_rate`(
  p_horizon_to DATE, p_created_by STRING, OUT p_plan_version STRING)
BEGIN
  DECLARE v_now TIMESTAMP DEFAULT CURRENT_TIMESTAMP();
  DECLARE v_sales_as_of DATE;
  DECLARE v_from DATE;
  DECLARE v_inv_date DATE;
  DECLARE v_inv_fresh STRING;
  DECLARE v_lines INT64;
  DECLARE v_bom INT64;
  DECLARE v_sha STRING;
  DECLARE v_status STRING;
  DECLARE v_missing STRING;

  IF p_horizon_to IS NULL OR IFNULL(TRIM(p_created_by), '') = '' THEN
    RAISE USING MESSAGE = 'PLAN: конец горизонта и автор обязательны';
  END IF;

  -- sales_as_of — та же формула, что в V_SKU_SELL_THROUGH_CURRENT (паритет — DQ P09).
  SET v_sales_as_of = (
    SELECT LEAST(MAX(IF(marketplace = 'WB', d, NULL)), MAX(IF(marketplace = 'OZON', d, NULL)), DATE_SUB(DATE(MAX(refreshed_at)), INTERVAL 1 DAY))
    FROM `project-fa311fc0-4d87-4781-986.evetis_ref.CT_ACTUAL_DAILY`);
  IF v_sales_as_of IS NULL THEN
    RAISE USING MESSAGE = 'PLAN: нет фактов продаж CT_ACTUAL_DAILY';
  END IF;
  SET v_from = DATE_TRUNC(DATE_ADD(v_sales_as_of, INTERVAL 1 DAY), MONTH);
  IF p_horizon_to < v_from THEN
    RAISE USING MESSAGE = CONCAT('PLAN: конец горизонта ', CAST(p_horizon_to AS STRING), ' раньше начала ', CAST(v_from AS STRING));
  END IF;
  SET (v_inv_date, v_inv_fresh) = (
    SELECT AS STRUCT MAX(inventory_as_of_date), IF(LOGICAL_AND(inventory_freshness_status = 'FRESH'), 'FRESH', 'STALE')
    FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_SKU_SELL_THROUGH_CURRENT`);
  SET p_plan_version = CONCAT('SP-OBS30-', FORMAT_TIMESTAMP('%Y%m%dT%H%M%S', v_now));

  CREATE TEMP TABLE _uni AS
  SELECT DISTINCT UPPER(m.marketplace) AS marketplace, m.internal_sku,
    IF(IFNULL(p.is_bundle, FALSE), 'BUNDLE', 'SOLO') AS sales_mode, p.internal_sku IS NULL AS not_in_master
  FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_CHANNEL_MAP` m
  LEFT JOIN `project-fa311fc0-4d87-4781-986.evetis_ref.REF_PRODUCT_MASTER` p USING (internal_sku)
  WHERE m.is_current AND UPPER(m.marketplace) IN ('WB', 'OZON');
  SET v_missing = (SELECT STRING_AGG(internal_sku, ', ') FROM _uni WHERE not_in_master);
  IF v_missing IS NOT NULL THEN
    RAISE USING MESSAGE = CONCAT('PLAN: карточек нет в REF_PRODUCT_MASTER: ', v_missing);
  END IF;

  CREATE TEMP TABLE _rate AS
  SELECT u.marketplace, u.internal_sku, u.sales_mode,
    CAST(IFNULL(SUM(a.cards_ordered), 0) AS NUMERIC) / 30 AS cards_per_day
  FROM _uni u
  LEFT JOIN `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_ACTUAL_DAILY` a
    ON UPPER(a.marketplace) = u.marketplace AND a.internal_sku = u.internal_sku
   AND a.d BETWEEN DATE_SUB(v_sales_as_of, INTERVAL 29 DAY) AND v_sales_as_of
  GROUP BY u.marketplace, u.internal_sku, u.sales_mode;

  CREATE TEMP TABLE _lines AS
  SELECT mo AS month, r.marketplace, r.internal_sku, r.sales_mode,
    ROUND(r.cards_per_day * (DATE_DIFF(LEAST(LAST_DAY(mo), p_horizon_to), mo, DAY) + 1), 1) AS planned_cards
  FROM _rate r
  CROSS JOIN UNNEST(GENERATE_DATE_ARRAY(v_from, DATE_TRUNC(p_horizon_to, MONTH), INTERVAL 1 MONTH)) AS mo;

  CREATE TEMP TABLE _bom AS
  SELECT b.bundle_internal_sku AS bundle_sku, b.component_internal_sku AS component_sku, b.component_qty,
    b.bundle_component_id AS source_row_id
  FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_BUNDLE_COMPONENTS` b
  WHERE b.bundle_internal_sku IN (SELECT internal_sku FROM _uni WHERE sales_mode = 'BUNDLE')
    AND b.effective_from <= CURRENT_DATE() AND (b.effective_to IS NULL OR b.effective_to >= CURRENT_DATE());
  SET v_missing = (SELECT STRING_AGG(DISTINCT internal_sku, ', ') FROM _uni
                   WHERE sales_mode = 'BUNDLE' AND internal_sku NOT IN (SELECT bundle_sku FROM _bom));
  IF v_missing IS NOT NULL THEN
    RAISE USING MESSAGE = CONCAT('PLAN: у наборов нет действующего состава: ', v_missing);
  END IF;

  SET v_lines = (SELECT COUNT(*) FROM _lines);
  SET v_bom = (SELECT COUNT(*) FROM _bom);
  SET v_sha = (
    SELECT TO_HEX(SHA256(CONCAT(
      IFNULL((SELECT STRING_AGG(k, '\n' ORDER BY k) FROM (
                SELECT CONCAT(CAST(month AS STRING), '|', marketplace, '|', internal_sku, '|', CAST(ROUND(planned_cards, 4) AS STRING)) AS k
                FROM _lines)), ''),
      '\n#BOM\n',
      IFNULL((SELECT STRING_AGG(k, '\n' ORDER BY k) FROM (
                SELECT CONCAT(bundle_sku, '|', component_sku, '|', CAST(component_qty AS STRING)) AS k FROM _bom)), '')))));

  BEGIN
    BEGIN TRANSACTION;
    INSERT INTO `project-fa311fc0-4d87-4781-986.evetis_ref.PLAN_VERSION`
      (plan_version, plan_kind, plan_name, method_id, method_version, parent_plan_version, source_kind, source_ref,
       horizon_from, horizon_to, marketplaces, basis_sales_as_of, basis_inventory_as_of, basis_inventory_freshness,
       line_count, bom_basis_rows, content_sha256, created_at, created_by, notes)
    VALUES (p_plan_version, 'SYSTEM_PROPOSED',
      CONCAT('Наблюдаемый темп 30 дней до ', CAST(v_sales_as_of AS STRING)), 'OBSERVED_RUN_RATE_30D_V1', 'v1', NULL,
      'PLAN_LINE_MONTHLY', NULL, v_from, p_horizon_to, 'WB,OZON', v_sales_as_of, v_inv_date, v_inv_fresh,
      v_lines, v_bom, v_sha, v_now, p_created_by,
      'Предложение системы: наблюдаемый темп × календарные дни. Не прогноз спроса и не утверждённый план.');

    INSERT INTO `project-fa311fc0-4d87-4781-986.evetis_ref.PLAN_LINE_MONTHLY`
      (plan_version, month, marketplace, internal_sku, sales_mode, planned_cards, line_basis, created_at)
    SELECT p_plan_version, month, marketplace, internal_sku, sales_mode, planned_cards, 'OBSERVED_RUN_RATE_30D × CALENDAR_DAYS', v_now
    FROM _lines;

    INSERT INTO `project-fa311fc0-4d87-4781-986.evetis_ref.PLAN_BOM_BASIS`
      (plan_version, bundle_sku, component_sku, component_qty, source_row_id, captured_at)
    SELECT p_plan_version, bundle_sku, component_sku, component_qty, source_row_id, v_now FROM _bom;

    INSERT INTO `project-fa311fc0-4d87-4781-986.evetis_ref.PLAN_ASSUMPTION`
      (plan_version, assumption_id, assumption_type, scope_marketplace, scope_sku, scope_month, value_numeric, value_text,
       unit, evidence_class, source, created_at)
    SELECT p_plan_version, id, t, mp, sku, NULL, v, txt, u, ev, src, v_now
    FROM UNNEST([
      STRUCT('M01' AS id, 'METHOD' AS t, CAST(NULL AS STRING) AS mp, CAST(NULL AS STRING) AS sku, CAST(NULL AS NUMERIC) AS v,
             'Темп заказов без отмен за 30 полных суток × календарные дни месяца; ROUND до 0,1 карточки' AS txt,
             CAST(NULL AS STRING) AS u, 'ASSUMPTION' AS ev, 'PR-PLAN-1 §9' AS src),
      STRUCT('M02', 'SEASONALITY', NULL, NULL, NUMERIC '1', 'Сезонность не применяется: не доказана фактом', 'multiplier', 'ASSUMPTION', 'PR-PLAN-1 §9'),
      STRUCT('M03', 'CHANNEL_SCOPE', NULL, NULL, NULL, 'Каналы WB и Ozon — каждый со своим наблюдаемым темпом; Ozon × 1,25 не наследуется', NULL, 'ASSUMPTION', 'PR-PLAN-1 §9'),
      STRUCT('M04', 'CALENDAR', NULL, NULL, NULL,
             CONCAT('Настоящие дни месяцев (28/29/30/31); горизонт ', CAST(v_from AS STRING), ' — ', CAST(p_horizon_to AS STRING)), NULL, 'FACT', 'PR-PLAN-1 §10'),
      STRUCT('M05', 'INBOUND', NULL, NULL, NULL, 'Поставки и производство в план продаж не заложены; поступления — отдельный реестр', NULL, 'ASSUMPTION', 'PR-PLAN-1 §11'),
      STRUCT('M06', 'INVENTORY_BASIS', NULL, NULL, NULL,
             CONCAT('Запас на ', IFNULL(CAST(v_inv_date AS STRING), '∅'), ': ', v_inv_fresh, '. План продаж от запаса не зависит.'), NULL, 'FACT',
             'evetis_mart.V_SKU_SELL_THROUGH_CURRENT')
    ])
    UNION ALL
    SELECT p_plan_version, CONCAT('R-', marketplace, '-', internal_sku), 'OBSERVED_RATE', marketplace, internal_sku, NULL,
      cards_per_day, CONCAT('Карточек в день, 30 суток по ', CAST(v_sales_as_of AS STRING)), 'cards/day', 'FACT',
      'wb_mart.V_CT_ACTUAL_DAILY', v_now
    FROM _rate;

    SET v_status = (SELECT lifecycle_status FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_PLAN_VERSION_STATUS`
                    WHERE plan_version = p_plan_version);
    IF v_status IS DISTINCT FROM 'DRAFT' THEN
      RAISE USING MESSAGE = CONCAT('PLAN: после записи статус ', IFNULL(v_status, '∅'), ' вместо DRAFT — откат');
    END IF;
    COMMIT TRANSACTION;
  EXCEPTION WHEN ERROR THEN
    ROLLBACK TRANSACTION;
    RAISE USING MESSAGE = @@error.message;
  END;

  SELECT plan_version, plan_kind, lifecycle_status, line_rows_actual, bom_rows_actual, planned_cards_total, content_sha256, integrity_ok
  FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_PLAN_VERSION_STATUS` WHERE plan_version = p_plan_version;
END;

-- ─────────────────────────────────────────────────────────────── события жизненного цикла
-- Общее ядро: проверка и запись одного события с хешем версии. Отказ = RAISE, записи нет.
CREATE OR REPLACE PROCEDURE `project-fa311fc0-4d87-4781-986.evetis_ref.sp_plan_event_core`(
  p_plan_version STRING, p_event STRING, p_expected_sha STRING, p_effective_from_month DATE,
  p_actor STRING, p_reference STRING, p_note STRING)
BEGIN
  DECLARE v STRUCT<plan_kind STRING, lifecycle_status STRING, integrity_ok BOOL, content_sha256 STRING, recomputed_sha256 STRING,
                   approvable_now BOOL, horizon_from DATE, horizon_to DATE>;
  DECLARE v_after STRING;

  IF p_event NOT IN ('PROPOSED', 'APPROVED', 'WITHDRAWN', 'REVOKED') THEN
    RAISE USING MESSAGE = CONCAT('PLAN: неизвестное событие ', IFNULL(p_event, '∅'));
  END IF;
  IF IFNULL(TRIM(p_actor), '') = '' THEN
    RAISE USING MESSAGE = 'PLAN: кто принимает решение — обязательно';
  END IF;
  SET v = (SELECT AS STRUCT plan_kind, lifecycle_status, integrity_ok, content_sha256, recomputed_sha256, approvable_now,
                  horizon_from, horizon_to
           FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_PLAN_VERSION_STATUS` WHERE plan_version = p_plan_version);
  IF v.plan_kind IS NULL THEN
    RAISE USING MESSAGE = CONCAT('PLAN: версии ', IFNULL(p_plan_version, '∅'), ' нет');
  END IF;
  IF v.plan_kind = 'MODEL_SCENARIO' AND p_event IN ('PROPOSED', 'APPROVED') THEN
    RAISE USING MESSAGE = CONCAT('PLAN: ', p_plan_version, ' — модельный сценарий, его нельзя предложить или утвердить');
  END IF;
  IF NOT v.integrity_ok THEN
    RAISE USING MESSAGE = CONCAT('PLAN: целостность версии нарушена (хеш шапки ', v.content_sha256, ', пересчёт ', v.recomputed_sha256, ')');
  END IF;
  IF p_expected_sha IS DISTINCT FROM v.content_sha256 THEN
    RAISE USING MESSAGE = CONCAT('PLAN: ожидаемый хеш ', IFNULL(p_expected_sha, '∅'), ' не совпадает с содержимым ', v.content_sha256);
  END IF;

  IF p_event = 'PROPOSED' AND v.lifecycle_status != 'DRAFT' THEN
    RAISE USING MESSAGE = CONCAT('PLAN: предложить можно только DRAFT, сейчас ', v.lifecycle_status);
  END IF;
  IF p_event = 'APPROVED' THEN
    IF NOT v.approvable_now THEN
      RAISE USING MESSAGE = CONCAT('PLAN: утвердить можно только действующее PROPOSED, сейчас ', v.lifecycle_status);
    END IF;
    IF IFNULL(TRIM(p_reference), '') = '' THEN
      RAISE USING MESSAGE = 'PLAN: основание утверждения (где и когда владелец дал ACK) обязательно';
    END IF;
    IF p_effective_from_month IS NULL OR p_effective_from_month != DATE_TRUNC(p_effective_from_month, MONTH)
       OR p_effective_from_month < DATE_TRUNC(CURRENT_DATE(), MONTH)
       OR p_effective_from_month < DATE_TRUNC(v.horizon_from, MONTH) OR p_effective_from_month > v.horizon_to THEN
      RAISE USING MESSAGE = CONCAT('PLAN: месяц начала действия ', IFNULL(CAST(p_effective_from_month AS STRING), '∅'),
                                   ' — не первое число, в прошлом или вне горизонта');
    END IF;
  ELSEIF p_effective_from_month IS NOT NULL THEN
    RAISE USING MESSAGE = 'PLAN: месяц начала действия допустим только у APPROVED';
  END IF;
  IF p_event = 'WITHDRAWN' AND v.lifecycle_status != 'PROPOSED' THEN
    RAISE USING MESSAGE = CONCAT('PLAN: отозвать предложение можно только у PROPOSED, сейчас ', v.lifecycle_status);
  END IF;
  IF p_event = 'REVOKED' AND v.lifecycle_status NOT IN ('APPROVED', 'SUPERSEDED') THEN
    RAISE USING MESSAGE = CONCAT('PLAN: снять утверждение можно только у APPROVED/SUPERSEDED, сейчас ', v.lifecycle_status);
  END IF;

  BEGIN
    BEGIN TRANSACTION;
    INSERT INTO `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SALES_PLAN_APPROVAL`
      (plan_version, approval_status, approved_scope, approved_by, approved_at, approval_reference, note, recorded_at, recorded_by,
       content_sha256, effective_from_month)
    VALUES (p_plan_version, p_event, 'PLAN_VERSION_MONTH_CHANNEL_CARD', p_actor, CURRENT_TIMESTAMP(), p_reference, p_note,
            CURRENT_TIMESTAMP(), SESSION_USER(), v.content_sha256, p_effective_from_month);
    SET v_after = (SELECT lifecycle_status FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_PLAN_VERSION_STATUS`
                   WHERE plan_version = p_plan_version);
    IF v_after IS DISTINCT FROM CASE p_event WHEN 'PROPOSED' THEN 'PROPOSED' WHEN 'APPROVED' THEN 'APPROVED'
                                              WHEN 'WITHDRAWN' THEN 'WITHDRAWN' ELSE 'REVOKED' END
       AND NOT (p_event = 'APPROVED' AND v_after = 'SUPERSEDED') THEN
      RAISE USING MESSAGE = CONCAT('PLAN: событие не подействовало (статус ', IFNULL(v_after, '∅'), ') — откат');
    END IF;
    COMMIT TRANSACTION;
  EXCEPTION WHEN ERROR THEN
    ROLLBACK TRANSACTION;
    RAISE USING MESSAGE = @@error.message;
  END;

  SELECT plan_version, lifecycle_status, content_sha256, approved_by, approved_at, effective_from_month,
    effective_to_month_exclusive, events_total, events_ignored
  FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_PLAN_VERSION_STATUS` WHERE plan_version = p_plan_version;
END;

CREATE OR REPLACE PROCEDURE `project-fa311fc0-4d87-4781-986.evetis_ref.sp_plan_submit`(
  p_plan_version STRING, p_expected_sha STRING, p_submitted_by STRING, p_note STRING)
BEGIN
  CALL `project-fa311fc0-4d87-4781-986.evetis_ref.sp_plan_event_core`(
    p_plan_version, 'PROPOSED', p_expected_sha, NULL, p_submitted_by, NULL, p_note);
END;

-- Утверждение — только решение владельца: вызывать после того, как владелец увидел содержимое
-- версии и дал ACK на ЭТОТ хеш. Процедура сама ничего не выбирает и не вызывается расписанием.
CREATE OR REPLACE PROCEDURE `project-fa311fc0-4d87-4781-986.evetis_ref.sp_plan_approve`(
  p_plan_version STRING, p_expected_sha STRING, p_effective_from_month DATE, p_approved_by STRING, p_reference STRING)
BEGIN
  CALL `project-fa311fc0-4d87-4781-986.evetis_ref.sp_plan_event_core`(
    p_plan_version, 'APPROVED', p_expected_sha, p_effective_from_month, p_approved_by, p_reference, NULL);
END;

CREATE OR REPLACE PROCEDURE `project-fa311fc0-4d87-4781-986.evetis_ref.sp_plan_close`(
  p_plan_version STRING, p_event STRING, p_expected_sha STRING, p_closed_by STRING, p_reference STRING)
BEGIN
  IF p_event NOT IN ('WITHDRAWN', 'REVOKED') THEN
    RAISE USING MESSAGE = 'PLAN: sp_plan_close принимает только WITHDRAWN или REVOKED';
  END IF;
  CALL `project-fa311fc0-4d87-4781-986.evetis_ref.sp_plan_event_core`(
    p_plan_version, p_event, p_expected_sha, NULL, p_closed_by, p_reference, NULL);
END;

-- ─────────────────────────────────────────────────────────────── поступления
-- Событие партии: новое событие того же inbound_id заменяет прежнее в V_INBOUND_LOT_CURRENT,
-- история остаётся. Партия попадает в базовую траекторию только при доказанных SKU, количестве,
-- состоянии и подтверждённой дате (правило — в представлении, не здесь).
CREATE OR REPLACE PROCEDURE `project-fa311fc0-4d87-4781-986.evetis_ref.sp_inbound_record`(
  p_inbound_id STRING, p_internal_sku STRING, p_quantity INT64, p_lot_state STRING, p_blocker STRING,
  p_eta_date DATE, p_eta_status STRING, p_received_date DATE, p_received_quantity INT64, p_expiry_batch_id STRING,
  p_evidence_class STRING, p_evidence_ref STRING, p_recorded_by STRING, p_note STRING)
BEGIN
  DECLARE v_prev_sku STRING;

  IF IFNULL(TRIM(p_inbound_id), '') = '' OR IFNULL(TRIM(p_evidence_ref), '') = '' OR IFNULL(TRIM(p_recorded_by), '') = '' THEN
    RAISE USING MESSAGE = 'INBOUND: inbound_id, основание и автор обязательны';
  END IF;
  IF (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.evetis_ref.REF_PRODUCT_MASTER`
      WHERE internal_sku = p_internal_sku AND NOT IFNULL(is_bundle, FALSE)) != 1 THEN
    RAISE USING MESSAGE = CONCAT('INBOUND: ', IFNULL(p_internal_sku, '∅'), ' — не физический SKU справочника');
  END IF;
  SET v_prev_sku = (SELECT ANY_VALUE(internal_sku) FROM `project-fa311fc0-4d87-4781-986.evetis_ref.INBOUND_LOT_EVENT`
                    WHERE inbound_id = p_inbound_id);
  IF v_prev_sku IS NOT NULL AND v_prev_sku != p_internal_sku THEN
    RAISE USING MESSAGE = CONCAT('INBOUND: партия ', p_inbound_id, ' уже относится к ', v_prev_sku);
  END IF;
  IF p_quantity IS NULL OR p_quantity <= 0 THEN
    RAISE USING MESSAGE = 'INBOUND: количество должно быть > 0';
  END IF;
  IF p_lot_state NOT IN ('PLANNED', 'HYPOTHETICAL', 'ORDER_CONFIRMED', 'IN_PRODUCTION', 'PRODUCED', 'READY_FOR_SHIPMENT',
                         'IN_TRANSIT', 'RECEIVED', 'CANCELLED') THEN
    RAISE USING MESSAGE = CONCAT('INBOUND: неизвестное состояние ', IFNULL(p_lot_state, '∅'));
  END IF;
  IF p_blocker IS NOT NULL AND NOT REGEXP_CONTAINS(p_blocker, r'^[A-Z][A-Z_]*$') THEN
    RAISE USING MESSAGE = 'INBOUND: блокер — код ВЕРХНИМ_РЕГИСТРОМ, например AWAITING_PAYMENT';
  END IF;
  IF p_eta_status NOT IN ('CONFIRMED', 'ESTIMATED', 'UNKNOWN') THEN
    RAISE USING MESSAGE = CONCAT('INBOUND: неизвестный статус даты ', IFNULL(p_eta_status, '∅'));
  END IF;
  IF (p_eta_status = 'UNKNOWN') != (p_eta_date IS NULL) THEN
    RAISE USING MESSAGE = 'INBOUND: дата задаётся только при статусе CONFIRMED/ESTIMATED и обязательна для них';
  END IF;
  IF p_evidence_class NOT IN ('OWNER_FACT', 'DOCUMENT', 'SUPPLIER_CONFIRMATION', 'HYPOTHESIS') THEN
    RAISE USING MESSAGE = CONCAT('INBOUND: неизвестный класс доказательства ', IFNULL(p_evidence_class, '∅'));
  END IF;
  IF p_evidence_class = 'HYPOTHESIS' AND p_lot_state NOT IN ('PLANNED', 'HYPOTHETICAL', 'CANCELLED') THEN
    RAISE USING MESSAGE = 'INBOUND: гипотеза не может подтверждать заказ, производство или отгрузку';
  END IF;
  IF p_lot_state = 'RECEIVED' AND (p_received_date IS NULL OR p_received_quantity IS NULL OR p_received_quantity < 0) THEN
    RAISE USING MESSAGE = 'INBOUND: для RECEIVED нужны дата и количество приёмки';
  END IF;
  IF p_lot_state != 'RECEIVED' AND (p_received_date IS NOT NULL OR p_received_quantity IS NOT NULL) THEN
    RAISE USING MESSAGE = 'INBOUND: дата и количество приёмки допустимы только у RECEIVED';
  END IF;
  IF p_expiry_batch_id IS NOT NULL AND (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.evetis_ref.CT_EXPIRY_BATCH`
      WHERE batch_id = p_expiry_batch_id AND internal_sku = p_internal_sku) = 0 THEN
    RAISE USING MESSAGE = CONCAT('INBOUND: партии сроков ', p_expiry_batch_id, ' нет для ', p_internal_sku);
  END IF;

  INSERT INTO `project-fa311fc0-4d87-4781-986.evetis_ref.INBOUND_LOT_EVENT`
    (inbound_id, internal_sku, quantity, lot_state, blocker, eta_date, eta_status, received_date, received_quantity,
     expiry_batch_id, evidence_class, evidence_ref, recorded_at, recorded_by, note)
  VALUES (p_inbound_id, p_internal_sku, p_quantity, p_lot_state, p_blocker, p_eta_date, p_eta_status, p_received_date,
          p_received_quantity, p_expiry_batch_id, p_evidence_class, p_evidence_ref, CURRENT_TIMESTAMP(), p_recorded_by, p_note);

  SELECT * FROM `project-fa311fc0-4d87-4781-986.evetis_mart.V_INBOUND_LOT_CURRENT` WHERE inbound_id = p_inbound_id;
END;
