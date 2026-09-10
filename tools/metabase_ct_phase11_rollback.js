/*
 * EVETIS OWNER CONTROL TOWER — Phase 1.1 · Metabase ROLLBACK to the Phase 1 state (10.09.2026, commit 075f45a)
 * ----------------------------------------------------------------------------------------------------------
 * Run in the browser console on http://localhost:3000 (owner session). Restores:
 *   • cards 97–116 — name / display / SQL / visualization settings exactly as Phase 1 created them;
 *   • dashboards 5 (EVETIS OWNER HOME) and 6 (EVETIS SALES PLAN) — Phase 1 layout, width "fixed", filters of 6;
 *   • archives every card and dashboard created by Phase 1.1 (found by description prefix and by name).
 * BigQuery objects are rolled back separately by tools/ct_phase11_rollback.sh.
 * Usage: await ctRollback.all()
 */
(function () {
  const P = 'project-fa311fc0-4d87-4781-986';
  const mb = async (path, method = 'GET', body) => {
    const r = await fetch('/api' + path, { method, headers: { 'Content-Type': 'application/json' }, body: body ? JSON.stringify(body) : undefined });
    const t = await r.text(); try { return JSON.parse(t); } catch (e) { return { status: r.status, text: t.slice(0, 300) }; }
  };
  const RAG = { RED: '#ED6E6E', YELLOW: '#F9CF48', GREEN: '#84BB4C', BLUE: '#509EE3' };
  const single = (col, val, color) => ({ columns: [col], type: 'single', operator: '=', value: val, color, highlight_row: false });
  const contains = (col, val, color) => ({ columns: [col], type: 'single', operator: 'contains', value: val, color, highlight_row: false });
  const statusFmt = (col, withBlue = true, withGreen = true) => [single(col, 'RED', RAG.RED), single(col, 'YELLOW', RAG.YELLOW)].concat(withBlue ? [single(col, 'BLUE', RAG.BLUE)] : []).concat(withGreen ? [single(col, 'GREEN', RAG.GREEN)] : []);
  const num = (col, suffix, decimals = 0) => ({ 'scalar.compact_primary_number': false, column_settings: { ['["name","' + col + '"]']: { number_style: 'decimal', decimals, suffix } } });
  const H = '`' + P + '.wb_mart.V_CT_OWNER_HOME`';
  const PVA = '`' + P + '.wb_mart.V_CT_PLAN_VS_ACTUAL_DAILY`';
  const RPM = '`' + P + '.evetis_ref.REF_PRODUCT_MASTER`';
  const F = ' [[AND p.marketplace = {{marketplace}}]] [[AND p.sales_mode = {{sales_mode}}]] [[AND p.internal_sku = {{sku}}]] [[AND m.product_line = {{product_line}}]]';
  const TAGS = { marketplace: { name: 'marketplace', 'display-name': 'Канал', type: 'text', required: false }, sales_mode: { name: 'sales_mode', 'display-name': 'Соло/набор', type: 'text', required: false }, sku: { name: 'sku', 'display-name': 'SKU', type: 'text', required: false }, product_line: { name: 'product_line', 'display-name': 'Линия', type: 'text', required: false } };

  const PHASE1 = {
    97: { name: 'Вчера · заказано, ₽', display: 'scalar', sql: 'SELECT gmv_yesterday FROM ' + H, vs: num('gmv_yesterday', ' ₽') },
    98: { name: 'Вчера · флаконов', display: 'scalar', sql: 'SELECT units_yesterday FROM ' + H, vs: num('units_yesterday', ' фл.') },
    99: { name: 'Вчера · вклад, ₽', display: 'scalar', sql: 'SELECT contribution_yesterday FROM ' + H, vs: num('contribution_yesterday', ' ₽') },
    100: { name: 'Вчера · ДРР, %', display: 'scalar', sql: 'SELECT drr_yesterday_pct FROM ' + H, vs: num('drr_yesterday_pct', ' %', 1) },
    101: { name: 'Сентябрь · выполнение плана, %', display: 'scalar', sql: 'SELECT mtd_attainment_pct FROM ' + H, vs: num('mtd_attainment_pct', ' %') },
    102: { name: 'Запас · остаток, фл.', display: 'scalar', sql: 'SELECT inventory_remaining_units FROM ' + H, vs: num('inventory_remaining_units', ' фл.') },
    103: { name: 'План vs факт · текущий месяц', display: 'table', sql: "WITH h AS (SELECT * FROM " + H + ")\nSELECT * FROM (\n  SELECT 1 AS ord, 'Карточки' AS `Показатель`,\n    ROUND(plan_cards_mtd) AS `План за плановые дни`, CAST(cards_ordered_plan_days_mtd AS FLOAT64) AS `Факт за те же дни`,\n    ROUND(mtd_attainment_pct) AS `Процент плана`, ROUND(plan_cards_month) AS `План месяца`,\n    ROUND(forecast_eom_cards_plan_days) AS `Прогноз конца месяца`, ROUND(required_daily_velocity_remaining,1) AS `Нужно в день`,\n    CASE WHEN mtd_attainment_pct >= 90 THEN 'GREEN' WHEN mtd_attainment_pct >= 70 THEN 'YELLOW' ELSE 'RED' END AS `Статус` FROM h\n  UNION ALL\n  SELECT 2, 'Флаконы', ROUND(plan_units_mtd), CAST(units_plan_days_mtd AS FLOAT64), ROUND(mtd_attainment_units_pct), ROUND(plan_units_month),\n    ROUND(forecast_eom_units_plan_days), NULL,\n    CASE WHEN mtd_attainment_units_pct >= 90 THEN 'GREEN' WHEN mtd_attainment_units_pct >= 70 THEN 'YELLOW' ELSE 'RED' END FROM h\n  UNION ALL\n  SELECT 3, 'GMV, руб', ROUND(plan_gmv_mtd), CAST(gmv_plan_days_mtd AS FLOAT64), ROUND(mtd_attainment_gmv_pct), ROUND(plan_gmv_month),\n    ROUND(forecast_eom_gmv_plan_days), NULL,\n    CASE WHEN mtd_attainment_gmv_pct >= 90 THEN 'GREEN' WHEN mtd_attainment_gmv_pct >= 70 THEN 'YELLOW' ELSE 'RED' END FROM h\n  UNION ALL\n  SELECT 4, 'Вклад, руб', ROUND(plan_contribution_mtd), ROUND(contribution_plan_days_mtd), ROUND(mtd_attainment_contribution_pct), ROUND(plan_contribution_month),\n    NULL, NULL,\n    CASE WHEN contribution_plan_days_mtd >= plan_contribution_mtd THEN 'GREEN' WHEN contribution_plan_days_mtd >= 0 THEN 'YELLOW' ELSE 'RED' END FROM h\n  UNION ALL\n  SELECT 5, 'Операционный результат, руб', ROUND(plan_operating_result_month * SAFE_DIVIDE(plan_cards_mtd, plan_cards_month)), ROUND(contribution_plan_days_mtd - opex_allocated_mtd),\n    NULL, ROUND(plan_operating_result_month), NULL, NULL,\n    CASE WHEN contribution_plan_days_mtd - opex_allocated_mtd >= 0 THEN 'GREEN' ELSE 'RED' END FROM h\n) ORDER BY ord",
      vs: { 'table.columns': ['Показатель', 'План за плановые дни', 'Факт за те же дни', 'Процент плана', 'План месяца', 'Прогноз конца месяца', 'Нужно в день', 'Статус'].map(n => ({ name: n, enabled: true })).concat([{ name: 'ord', enabled: false }]), 'table.column_formatting': statusFmt('Статус', false) } },
    104: { name: 'Нарастающий итог · план vs факт (месяц)', display: 'line', sql: "WITH d AS (\n  SELECT d AS day, SUM(target_cards) AS t, SUM(IF(is_past, IFNULL(actual_cards, 0), 0)) AS a, MAX(IF(is_past, 1, 0)) AS past\n  FROM " + PVA + " WHERE month = DATE_TRUNC(CURRENT_DATE(), MONTH) GROUP BY 1)\nSELECT day AS `Дата`,\n  ROUND(SUM(t) OVER (ORDER BY day)) AS `План нарастающим`,\n  IF(past = 1, SUM(a) OVER (ORDER BY day), NULL) AS `Факт нарастающим`\nFROM d ORDER BY day",
      vs: { 'graph.dimensions': ['Дата'], 'graph.x_axis.title_text': '', 'graph.y_axis.title_text': 'Карточки нарастающим итогом', series_settings: { 'План нарастающим': { color: '#A989C5', 'line.style': 'dashed' }, 'Факт нарастающим': { color: '#509EE3' } }, 'graph.y_axis.auto_split': false, 'graph.metrics': ['План нарастающим', 'Факт нарастающим'] } },
    105: { name: 'Действия на сегодня', display: 'table', sql: "SELECT priority AS `Приоритет`, action_type_ru AS `Тип`,\n  CONCAT(IFNULL(NULLIF(marketplace, 'ALL'), 'все каналы'),\n    IF(sku IN ('PORTFOLIO','BUNDLES','FF','DATA','BATCH-05') OR sku IS NULL, '', CONCAT(' · ', sku_name)),\n    IF(qty IS NULL OR qty = 0, '', CONCAT(' · ', CAST(CAST(qty AS INT64) AS STRING), ' шт'))) AS `Что и где`,\n  deadline AS `Срок`, ROUND(financial_effect_rub) AS `Эффект руб`,\n  SUBSTR(reason_text, 1, 120) AS `Основание`, action_id AS `ID`\nFROM `" + P + ".wb_mart.V_CT_ACTION_QUEUE` WHERE is_open ORDER BY queue_rank LIMIT 8",
      vs: { 'table.column_formatting': [contains('Приоритет', 'P0', RAG.RED), contains('Приоритет', 'P1', RAG.YELLOW), contains('Приоритет', 'P3', RAG.BLUE)] } },
    106: { name: 'Требует внимания', display: 'table', sql: "SELECT severity AS `Статус`, alert_type AS `Тип`, headline AS `Что происходит`,\n  ROUND(financial_effect_rub) AS `Эффект руб`\nFROM `" + P + ".wb_mart.V_CT_ATTENTION` ORDER BY severity_rank, sort_rank, metric_value LIMIT 10",
      vs: { 'table.column_formatting': statusFmt('Статус', true, false) } },
    107: { name: 'Запас в деньги', display: 'table', sql: "WITH h AS (SELECT * FROM " + H + ")\nSELECT * FROM (\n  SELECT 1 AS ord, 'Открытие сезона (09.09)' AS `Этап`, opening_inventory_units AS `Флаконы`, ROUND(opening_inventory_value_rub) AS `Рубли по себестоимости`, '' AS `Комментарий` FROM h\n  UNION ALL SELECT 2, 'Продано с открытия', -season_units_ordered, -ROUND(cogs_released_since_opening), 'COGS выпущен в деньги' FROM h\n  UNION ALL SELECT 3, 'Остаток сейчас', inventory_remaining_units, ROUND(inventory_remaining_value_rub), CONCAT('Конверсия запаса в деньги: ', CAST(ROUND(inventory_cash_conversion_pct,2) AS STRING), ' % (план сезона ', CAST(ROUND(season_plan_cash_conversion_pct,1) AS STRING), ' %)') FROM h\n  UNION ALL SELECT 4, 'Партия 8 в пути (не в остатке)', inbound_open_units, NULL, 'ETA ФФ ~20.10, срок годности +3 года' FROM h\n  UNION ALL SELECT 5, 'Потерянный сток WB (не продаваемый)', wb_lost_claimed_units, NULL, 'Отдельно, в claim/recovery' FROM h\n  UNION ALL SELECT 6, 'Прогноз остатка на 31.03.2027 (по плану)', CAST(ROUND(projected_residual_31_03_units_at_plan) AS INT64), NULL, CONCAT('При текущем темпе 30 дн.: ', CAST(CAST(ROUND(projected_residual_31_03_units_at_30d_rate) AS INT64) AS STRING), ' фл.') FROM h\n) ORDER BY ord",
      vs: { 'table.columns': ['Этап', 'Флаконы', 'Рубли по себестоимости', 'Комментарий'].map(n => ({ name: n, enabled: true })).concat([{ name: 'ord', enabled: false }]) } },
    108: { name: 'Крем для рук · контроль срока', display: 'table', sql: "SELECT * FROM (\n  SELECT 1 AS ord, 'Остаток, фл.' AS `Показатель`, CAST(current_stock_units AS STRING) AS `Значение`, CONCAT(CAST(ROUND(current_stock_value_rub) AS STRING), ' руб. по себестоимости') AS `Комментарий` FROM `" + P + ".wb_mart.V_CT_HAND_CREAM_CONTROL`\n  UNION ALL SELECT 2, 'Дней до 31.12.2026', CAST(days_to_expiry AS STRING), 'Жёсткий срок, продление невозможно' FROM `" + P + ".wb_mart.V_CT_HAND_CREAM_CONTROL`\n  UNION ALL SELECT 3, 'Нужно продавать, фл./день', CAST(ROUND(required_units_per_day,1) AS STRING), 'Чтобы выйти в ноль к сроку' FROM `" + P + ".wb_mart.V_CT_HAND_CREAM_CONTROL`\n  UNION ALL SELECT 4, 'Фактический темп, фл./день', CONCAT(CAST(ROUND(actual_units_per_day_7d,1) AS STRING), ' за 7 дн. / ', CAST(ROUND(actual_units_per_day_30d,1) AS STRING), ' за 30 дн.'), 'Соло плюс через наборы' FROM `" + P + ".wb_mart.V_CT_HAND_CREAM_CONTROL`\n  UNION ALL SELECT 5, 'Продано за месяц, фл.', CAST(units_ordered_mtd AS STRING), CONCAT('в т.ч. через наборы: ', CAST(units_ordered_mtd_via_bundle AS STRING)) FROM `" + P + ".wb_mart.V_CT_HAND_CREAM_CONTROL`\n  UNION ALL SELECT 6, 'Прогноз остатка на срок, фл.', CAST(CAST(ROUND(projected_residual_at_expiry_at_30d_rate) AS INT64) AS STRING), CONCAT('Риск списания ', CAST(ROUND(writeoff_risk_at_30d_rate_rub/1000) AS STRING), ' тыс. руб.') FROM `" + P + ".wb_mart.V_CT_HAND_CREAM_CONTROL`\n  UNION ALL SELECT 7, 'Статус', status, program FROM `" + P + ".wb_mart.V_CT_HAND_CREAM_CONTROL`\n) ORDER BY ord",
      vs: { 'table.columns': ['Показатель', 'Значение', 'Комментарий'].map(n => ({ name: n, enabled: true })).concat([{ name: 'ord', enabled: false }]), 'table.column_formatting': statusFmt('Значение', false) } },
    109: { name: 'Данные актуальны на', display: 'table', sql: "SELECT domain_ru AS `Домен данных`, data_as_of AS `Данные по`, age_days AS `Дней назад`, status AS `Статус`\nFROM `" + P + ".wb_mart.V_CT_FRESHNESS` ORDER BY sort_order",
      vs: { 'table.column_formatting': [single('Статус', 'STALE', RAG.RED), single('Статус', 'ERROR', RAG.RED), single('Статус', 'OK', RAG.GREEN)] } },
    110: { name: 'Наборы · сборка и наличие', display: 'table', sql: "SELECT product_name AS `Набор`, component_count AS `Флаконов в наборе`,\n  marketplace_cards_live AS `На площадках`, cards_ordered_mtd AS `Заказано за месяц`,\n  assembly_need_next_45d AS `Собрать на 45 дней`, assemblable_now_ff AS `Можно собрать сейчас`,\n  next_assembly_week_start AS `Ближайшая сборка`, status AS `Статус`\nFROM `" + P + ".wb_mart.V_CT_BUNDLE_STATUS` WHERE plan_cards_next_45d > 0 OR cards_ordered_mtd > 0 ORDER BY plan_cards_next_45d DESC LIMIT 10",
      vs: { 'table.column_formatting': statusFmt('Статус') } },
    111: { name: 'Что отгружать · WB и Ozon', display: 'table', sql: "SELECT marketplace AS `Канал`, product_name AS `SKU`, marketplace_units AS `На площадке`, in_transit_units AS `В пути`,\n  ROUND(cover_days_at_plan_rate) AS `Дней покрытия`, recommended_ship_units AS `Отгрузить фл`, ff_total_units AS `Есть на ФФ`, status AS `Статус`\nFROM `" + P + ".wb_mart.V_CT_SUPPLY_NEED` WHERE recommended_ship_units > 0 ORDER BY cover_days_at_plan_rate LIMIT 12",
      vs: { 'table.column_formatting': statusFmt('Статус') } },
    112: { name: 'Сезон · нарастающим итогом', display: 'line', tags: true, sql: "WITH b AS (\n SELECT p.d AS day, SUM(p.target_cards) AS t, SUM(IF(p.is_past, IFNULL(p.actual_cards, 0), 0)) AS a, MAX(IF(p.is_past, 1, 0)) AS past\n FROM " + PVA + " p\nLEFT JOIN " + RPM + " m ON m.internal_sku = p.internal_sku\n WHERE 1 = 1" + F + "\n GROUP BY 1)\nSELECT day AS `Дата`, ROUND(SUM(t) OVER (ORDER BY day)) AS `План нарастающим`,\n  IF(past = 1, SUM(a) OVER (ORDER BY day), NULL) AS `Факт нарастающим`\nFROM b ORDER BY day",
      vs: { 'graph.dimensions': ['Дата'], 'graph.metrics': ['План нарастающим', 'Факт нарастающим'], 'graph.y_axis.auto_split': false, series_settings: { 'План нарастающим': { color: '#A989C5', 'line.style': 'dashed' }, 'Факт нарастающим': { color: '#509EE3' } } } },
    113: { name: 'По месяцам · план и факт', display: 'bar', tags: true, sql: "SELECT FORMAT_DATE('%Y-%m', p.month) AS `Месяц`, ROUND(SUM(p.target_cards)) AS `План`,\n  SUM(IF(p.is_past, IFNULL(p.actual_cards, 0), 0)) AS `Факт`\nFROM " + PVA + " p\nLEFT JOIN " + RPM + " m ON m.internal_sku = p.internal_sku\nWHERE 1 = 1" + F + "\nGROUP BY 1 ORDER BY 1",
      vs: { 'graph.dimensions': ['Месяц'], 'graph.metrics': ['План', 'Факт'], 'graph.y_axis.auto_split': false } },
    114: { name: 'Флаконы по SKU · факт и план сезона', display: 'row', tags: true, sql: "SELECT COALESCE(m.product_name_short, m.canonical_product_name, p.internal_sku) AS `SKU`,\n  SUM(IF(p.is_past, IFNULL(p.actual_physical_units, 0), 0)) AS `Факт флаконов`,\n  ROUND(SUM(p.target_cards * p.component_count)) AS `План флаконов за сезон`\nFROM " + PVA + " p\nLEFT JOIN " + RPM + " m ON m.internal_sku = p.internal_sku\nWHERE 1 = 1" + F + "\nGROUP BY 1 ORDER BY `План флаконов за сезон` DESC LIMIT 20",
      vs: { 'graph.dimensions': ['SKU'], 'graph.metrics': ['Факт флаконов', 'План флаконов за сезон'], 'graph.y_axis.auto_split': false } },
    115: { name: 'WB и Ozon', display: 'bar', tags: true, sql: "SELECT p.marketplace AS `Канал`, ROUND(SUM(p.target_cards)) AS `План за сезон`,\n  SUM(IF(p.is_past, IFNULL(p.actual_cards, 0), 0)) AS `Факт с начала сезона`\nFROM " + PVA + " p\nLEFT JOIN " + RPM + " m ON m.internal_sku = p.internal_sku\nWHERE 1 = 1" + F + "\nGROUP BY 1 ORDER BY 1",
      vs: { 'graph.dimensions': ['Канал'], 'graph.metrics': ['План за сезон', 'Факт с начала сезона'], 'graph.y_axis.auto_split': false } },
    116: { name: 'Детально по SKU · текущий месяц', display: 'table', tags: true, sql: "SELECT COALESCE(m.product_name_short, m.canonical_product_name, p.internal_sku) AS `SKU`,\n  p.marketplace AS `Канал`, p.sales_mode AS `Тип`,\n  ROUND(SUM(IF(p.is_past, p.target_cards, 0))) AS `План за прошедшие дни`,\n  SUM(IF(p.is_past, IFNULL(p.actual_cards, 0), 0)) AS `Факт`,\n  ROUND(SAFE_DIVIDE(SUM(IF(p.is_past, IFNULL(p.actual_cards, 0), 0)), SUM(IF(p.is_past, p.target_cards, 0))) * 100) AS `Процент плана`,\n  ROUND(SUM(p.target_cards)) AS `План месяца`,\n  SUM(IF(p.is_past, IFNULL(p.actual_physical_units, 0), 0)) AS `Факт флаконов`,\n  ROUND(SUM(IF(p.is_past, IFNULL(p.actual_gmv, 0), 0))) AS `Факт GMV руб`\nFROM " + PVA + " p\nLEFT JOIN " + RPM + " m ON m.internal_sku = p.internal_sku\nWHERE 1 = 1" + F + "\n  AND p.month = DATE_TRUNC(CURRENT_DATE(), MONTH)\nGROUP BY 1, 2, 3 HAVING `План месяца` > 0 OR `Факт` > 0 ORDER BY `План месяца` DESC LIMIT 40",
      vs: {} }
  };

  const heading = (row, txt) => ({ id: -(row * 100 + 50), card_id: null, row, col: 0, size_x: 24, size_y: 1, series: [], parameter_mappings: [], visualization_settings: { virtual_card: { name: null, display: 'heading', visualization_settings: {}, dataset_query: {}, archived: false }, text: txt, 'dashcard.background': false } });
  const card = (id, row, col, w, h, pm = []) => ({ id: -(row * 100 + col + 1), card_id: id, row, col, size_x: w, size_y: h, series: [], parameter_mappings: pm, visualization_settings: {} });
  const PARAMS = [{ id: 'p_marketplace', name: 'Канал', sectionId: 'string', slug: 'marketplace', type: 'string/=' }, { id: 'p_sales_mode', name: 'Соло / набор', sectionId: 'string', slug: 'sales_mode', type: 'string/=' }, { id: 'p_product_line', name: 'Линия', sectionId: 'string', slug: 'product_line', type: 'string/=' }, { id: 'p_sku', name: 'SKU', sectionId: 'string', slug: 'sku', type: 'string/=' }];
  const pm = (id) => ['marketplace', 'sales_mode', 'product_line', 'sku'].map(t => ({ card_id: id, parameter_id: 'p_' + t, target: ['variable', ['template-tag', t]] }));

  async function cards() {
    for (const [id, c] of Object.entries(PHASE1)) {
      const r = await mb('/card/' + id, 'PUT', { name: c.name, display: c.display, visualization_settings: c.vs, dataset_query: { database: 2, type: 'native', native: { query: c.sql, 'template-tags': c.tags ? TAGS : {} } }, description: null });
      if (!r.id) throw new Error('card ' + id + ': ' + JSON.stringify(r).slice(0, 200));
    }
  }
  async function layouts() {
    const home = [heading(0, '## Вчера'), card(97, 1, 0, 8, 3), card(98, 1, 8, 8, 3), card(99, 1, 16, 8, 3), card(100, 4, 0, 8, 3), card(101, 4, 8, 8, 3), card(102, 4, 16, 8, 3),
      heading(7, '## План и факт'), card(103, 8, 0, 13, 6), card(104, 8, 13, 11, 6),
      heading(14, '## Что делать сегодня'), card(105, 15, 0, 24, 7),
      heading(22, '## Требует внимания'), card(106, 23, 0, 24, 7),
      heading(30, '## Запас в деньги'), card(107, 31, 0, 13, 6), card(108, 31, 13, 11, 6),
      heading(37, '## Отгрузки и наборы'), card(111, 38, 0, 13, 7), card(110, 38, 13, 11, 7),
      heading(45, '## Свежесть данных'), card(109, 46, 0, 13, 6)];
    const r5 = await mb('/dashboard/5', 'PUT', { width: 'fixed', dashcards: home, parameters: [] });
    if (!r5.dashcards) throw new Error('home: ' + JSON.stringify(r5).slice(0, 200));
    const sp = [heading(0, '## Сезон целиком'), card(112, 1, 0, 24, 7, pm(112)),
      heading(8, '## Месяцы и каналы'), card(113, 9, 0, 14, 6, pm(113)), card(115, 9, 14, 10, 6, pm(115)),
      heading(15, '## Физические флаконы по SKU'), card(114, 16, 0, 24, 8, pm(114)),
      heading(24, '## Детально по SKU за текущий месяц'), card(116, 25, 0, 24, 9, pm(116))];
    const r6 = await mb('/dashboard/6', 'PUT', { width: 'fixed', dashcards: sp, parameters: PARAMS });
    if (!r6.dashcards) throw new Error('sales plan: ' + JSON.stringify(r6).slice(0, 200));
  }
  async function archivePhase11() {
    const items = await mb('/collection/9/items?models=card');
    for (const c of (items.data || [])) { if ((c.description || '').startsWith('EVETIS Control Tower Phase 1.1') || c.name.startsWith('__test')) await mb('/card/' + c.id, 'PUT', { archived: true }); }
    const dash = await mb('/collection/9/items?models=dashboard');
    for (const d of (dash.data || [])) { if (['EVETIS CONTROL TOWER · ДЕТАЛИ', 'EVETIS DAILY BRIEF', '__test layout'].includes(d.name)) await mb('/dashboard/' + d.id, 'PUT', { archived: true }); }
  }
  async function all() { await cards(); await layouts(); await archivePhase11(); return 'Phase 1 Metabase state restored'; }
  window.ctRollback = { cards, layouts, archivePhase11, all };
})();
