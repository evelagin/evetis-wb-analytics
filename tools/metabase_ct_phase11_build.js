/*
 * EVETIS OWNER CONTROL TOWER — Phase 1.1 · Metabase build script
 * ---------------------------------------------------------------
 * Run in the browser console on the Metabase origin (http://localhost:3000) while logged in
 * as the owner. Same-origin `fetch` uses the session cookie; nothing else is needed.
 * Idempotent: existing cards 97–116 are UPDATED in place (ids preserved), new cards are
 * created once and remembered by name inside collection 9; dashboards 5/6 are re-laid out,
 * dashboards «EVETIS CONTROL TOWER · ДЕТАЛИ» and «EVETIS DAILY BRIEF» are created if missing.
 *
 * Usage:   await ctBuild.all()          — everything
 *          await ctBuild.cards()        — cards only
 *          await ctBuild.layouts()      — dashboard layouts only
 * Rollback: tools/ct_phase11_rollback.sh (Metabase part restores dashboards from metabase/ snapshot).
 */
(function () {
  const COLL = 9, DB = 2;
  const RAG = { RED: '#ED6E6E', YELLOW: '#F9CF48', GREEN: '#84BB4C', BLUE: '#509EE3', GREY: '#949AAB' };
  const H = 'wb_mart.V_CT_OWNER_HOME';
  const mb = async (path, method = 'GET', body) => {
    const r = await fetch('/api' + path, { method, headers: { 'Content-Type': 'application/json' }, body: body ? JSON.stringify(body) : undefined });
    const t = await r.text(); try { return JSON.parse(t); } catch (e) { return { status: r.status, text: t.slice(0, 300) }; }
  };
  const cs = (m) => Object.fromEntries(Object.entries(m).map(([k, v]) => ['["name","' + k + '"]', v]));
  const rag = (col, map) => map.map(([val, color]) => ({ columns: [col], type: 'single', operator: '=', value: val, color, highlight_row: false }));
  const PRI = (col) => rag(col, [['P0', RAG.RED], ['P1', RAG.YELLOW], ['P2', RAG.YELLOW], ['P3', RAG.BLUE], ['P4', RAG.GREY]]);
  const TONE = (col) => rag(col, [['срочно', RAG.RED], ['внимание', RAG.YELLOW], ['рост', RAG.BLUE], ['ок', RAG.GREEN], ['RED', RAG.RED], ['YELLOW', RAG.YELLOW], ['GREEN', RAG.GREEN], ['BLUE', RAG.BLUE], ['● OK', RAG.GREEN], ['● STALE', RAG.RED], ['● ERROR', RAG.RED], ['LOW', RAG.YELLOW], ['MEDIUM', RAG.BLUE], ['HIGH', RAG.GREEN]]);
  const TAGS = { marketplace: { name: 'marketplace', 'display-name': 'Канал', type: 'text', required: false }, sales_mode: { name: 'sales_mode', 'display-name': 'Соло/набор', type: 'text', required: false }, sku: { name: 'sku', 'display-name': 'SKU', type: 'text', required: false }, product_line: { name: 'product_line', 'display-name': 'Линия', type: 'text', required: false } };
  const FILT = '[[AND marketplace = {{marketplace}}]] [[AND sales_mode = {{sales_mode}}]] [[AND internal_sku = {{sku}}]] [[AND product_line = {{product_line}}]]';
  const FILT_P = '[[AND p.marketplace = {{marketplace}}]] [[AND p.sales_mode = {{sales_mode}}]] [[AND p.internal_sku = {{sku}}]] [[AND m.product_line = {{product_line}}]]';
  const money = (col, suffix = ' ₽', decimals = 0) => ({ column_settings: cs({ [col]: { number_style: 'decimal', decimals, suffix } }) });
  const trend = (col, planCol, label, suffix, decimals = 0, flip = false) => ({
    'scalar.field': col, 'scalar.compact_primary_number': false, 'scalar.switch_positive_negative': flip,
    'scalar.comparisons': [{ id: 'c1', type: 'anotherColumn', column: planCol, label }],
    column_settings: cs({ [col]: { number_style: 'decimal', decimals, suffix }, [planCol]: { number_style: 'decimal', decimals, suffix } })
  });
  const link = (text, col) => ({ view_as: 'link', link_text: text, link_url: '{{' + col + '}}' });

  // ── Card catalogue ─────────────────────────────────────────────────────────────
  const ACTION_COLS = (verb) => ({
    'table.columns': ['p', 'what', 'qty', 'who', 'deadline', 'why', 'effect', 'done', 'progress'].map(n => ({ name: n, enabled: true })),
    column_settings: cs({ p: { column_title: 'P' }, what: { column_title: verb, text_wrapping: true }, qty: { column_title: 'Кол-во' }, who: { column_title: 'Куда / кому', text_wrapping: true }, deadline: { column_title: 'Срок' }, why: { column_title: 'Почему', text_wrapping: true }, effect: { column_title: 'Эффект' }, done: { column_title: 'Готово', ...link('✅', 'done') }, progress: { column_title: 'В работе', ...link('▶', 'progress') } }),
    'table.column_formatting': PRI('p'),
    // ⚠ Metabase масштабирует подсказки ширин под ширину карточки, но клампит колонку снизу 60 px —
    // подсказка < 60 даёт горизонтальный overflow (замерено: 11 px). Все подсказки ≥ 60.
    'table.column_widths': [60, 250, 90, 140, 80, 260, 95, 70, 80]
  });
  const ACTION_SQL = (lane, limit) => "SELECT priority_short AS p, what_to_do AS what, qty_ru AS qty, executor_ru AS who, deadline_ru AS deadline, why_short AS why, effect_ru AS effect, done_url AS done, progress_url AS progress FROM wb_mart.V_CT_ACTION_QUEUE WHERE is_open AND lane = '" + lane + "' ORDER BY lane_rank LIMIT " + limit;

  // Компактная строка свежести: одна строка, домен = колонка; ● = свежо, ◔ = стареет (снимок не сегодняшний), ✖ = STALE/ERROR.
  const FRESH_DOMS = [['WB_SALES', 'wb_sales', 'WB SALES'], ['OZON_SALES', 'ozon_sales', 'OZON SALES'], ['WB_STOCK', 'wb_stock', 'WB STOCK'], ['OZON_STOCK', 'ozon_stock', 'OZON STOCK'], ['FF_STOCK', 'ff_stock', 'FF STOCK'], ['WB_ADS', 'wb_ads', 'WB ADS'], ['OZON_ADS', 'ozon_ads', 'OZON ADS']];
  const FRESH_STRIP_SQL = "WITH f AS (SELECT domain, CONCAT(CASE WHEN status != 'OK' THEN '✖' WHEN age_days >= 1 AND domain IN ('FF_STOCK','OZON_STOCK','WB_STOCK','OZON_SALES') THEN '◔' ELSE '●' END, ' ', CASE WHEN status != 'OK' THEN status WHEN age_days = 0 THEN 'today' WHEN age_days = 1 THEN '1 day' ELSE CONCAT(CAST(age_days AS STRING), ' days') END) AS v FROM wb_mart.V_CT_FRESHNESS) SELECT " + FRESH_DOMS.map(d => "MAX(IF(domain = '" + d[0] + "', v, NULL)) AS " + d[1]).join(', ') + " FROM f";
  const CARDS = [
    // — status line —
    { key: 'status_line', name: 'CONTROL TOWER · обновление', display: 'scalar', sql: "SELECT CONCAT('CONTROL TOWER UPDATED AT: ', IFNULL(ct_last_ok_msk, '—'), ' МСК ● ', ct_refresh_status, IF(ct_warn_today > 0, ' ⚠ витрина продаж за вчера не собрана', '')) FROM " + H, vs: {} },
    { key: 'fresh_strip', name: 'Свежесть источников', display: 'table', sql: FRESH_STRIP_SQL, vs: { column_settings: cs(Object.fromEntries(FRESH_DOMS.map(d => [d[1], { column_title: d[2] }]))), 'table.column_formatting': [{ columns: FRESH_DOMS.map(d => d[1]), type: 'single', operator: 'contains', value: '✖', color: RAG.RED, highlight_row: false }, { columns: FRESH_DOMS.map(d => d[1]), type: 'single', operator: 'contains', value: '◔', color: RAG.YELLOW, highlight_row: false }, { columns: FRESH_DOMS.map(d => d[1]), type: 'single', operator: 'contains', value: '●', color: RAG.GREEN, highlight_row: false }] } },
    // — KPI row —
    { id: 97, key: 'kpi_sales', name: 'Продажи вчера', display: 'smartscalar', sql: 'SELECT yesterday_date AS d, gmv_yesterday AS v, plan_gmv_yesterday AS plan FROM ' + H, vs: trend('v', 'plan', 'план дня', ' ₽') },
    { id: 98, key: 'kpi_units', name: 'Флаконы', display: 'smartscalar', sql: 'SELECT yesterday_date AS d, units_yesterday AS v, plan_units_yesterday AS plan FROM ' + H, vs: trend('v', 'plan', 'план дня', ' фл.') },
    { id: 99, key: 'kpi_contrib', name: 'Вклад вчера', display: 'smartscalar', sql: 'SELECT yesterday_date AS d, contribution_yesterday AS v, plan_contribution_yesterday AS plan FROM ' + H, vs: trend('v', 'plan', 'план дня', ' ₽') },
    { id: 100, key: 'kpi_drr', name: 'ДРР вчера', display: 'smartscalar', sql: 'SELECT yesterday_date AS d, drr_yesterday_pct AS v, plan_drr_yesterday_pct AS plan FROM ' + H, vs: trend('v', 'plan', 'план', ' %', 1, true) },
    { id: 101, key: 'kpi_plan', name: 'План месяца', display: 'gauge', sql: 'SELECT mtd_attainment_pct FROM ' + H, vs: { 'gauge.segments': [{ min: 0, max: 70, color: RAG.RED, label: 'отставание' }, { min: 70, max: 90, color: RAG.YELLOW, label: 'риск' }, { min: 90, max: 150, color: RAG.GREEN, label: 'по плану' }], ...money('mtd_attainment_pct', ' %') } },
    { id: 102, key: 'kpi_stock', name: 'Остаток, фл.', display: 'smartscalar', sql: 'SELECT as_of AS d, inventory_remaining_units AS v, opening_inventory_units AS plan FROM ' + H, vs: trend('v', 'plan', 'открытие сезона 09.09', ' фл.') },
    // — plan vs actual —
    { id: 103, key: 'month_summary', name: 'Текущий месяц · план, факт, прогноз', display: 'table', sql:
      "WITH h AS (SELECT * FROM " + H + ") SELECT ord, k AS `Показатель`, v AS `Значение`, tone AS `Статус` FROM (" +
      " SELECT 1 AS ord, 'План на прошедшие плановые дни' AS k, CONCAT(CAST(CAST(ROUND(plan_cards_mtd) AS INT64) AS STRING), ' карт. · ', CAST(CAST(ROUND(plan_units_mtd) AS INT64) AS STRING), ' фл.') AS v, '' AS tone FROM h" +
      " UNION ALL SELECT 2, 'Факт за те же дни', CONCAT(CAST(cards_ordered_plan_days_mtd AS STRING), ' карт. · ', CAST(units_plan_days_mtd AS STRING), ' фл.'), '' FROM h" +
      " UNION ALL SELECT 3, 'Выполнение плана', CONCAT(CAST(CAST(ROUND(mtd_attainment_pct) AS INT64) AS STRING), ' %'), CASE WHEN mtd_attainment_pct >= 90 THEN 'ок' WHEN mtd_attainment_pct >= 70 THEN 'внимание' ELSE 'срочно' END FROM h" +
      " UNION ALL SELECT 4, 'План месяца', CONCAT(CAST(CAST(ROUND(plan_cards_month) AS INT64) AS STRING), ' карт. · ', CAST(CAST(ROUND(plan_units_month) AS INT64) AS STRING), ' фл.'), '' FROM h" +
      " UNION ALL SELECT 5, 'Прогноз на конец месяца', CONCAT(CAST(CAST(ROUND(forecast_eom_cards_plan_days) AS INT64) AS STRING), ' карт. (', CAST(CAST(ROUND(forecast_eom_attainment_pct) AS INT64) AS STRING), ' % плана)'), CASE WHEN forecast_eom_attainment_pct >= 90 THEN 'ок' WHEN forecast_eom_attainment_pct >= 70 THEN 'внимание' ELSE 'срочно' END FROM h" +
      " UNION ALL SELECT 6, 'Нужно в день / факт в день', CONCAT(FORMAT('%.1f', required_daily_velocity_remaining), ' / ', FORMAT('%.1f', actual_daily_velocity_plan_days), ' карт.'), IF(actual_daily_velocity_plan_days >= required_daily_velocity_remaining, 'ок', 'срочно') FROM h" +
      " UNION ALL SELECT 7, 'Уверенность прогноза', forecast_confidence, forecast_confidence FROM h" +
      " UNION ALL SELECT 8, 'Почему', forecast_confidence_reason, '' FROM h" +
      " UNION ALL SELECT 9, 'Вклад за плановые дни', CONCAT(CAST(CAST(ROUND(contribution_plan_days_mtd) AS INT64) AS STRING), ' ₽ (план ', CAST(CAST(ROUND(plan_contribution_mtd) AS INT64) AS STRING), ' ₽)'), IF(contribution_plan_days_mtd < 0, 'срочно', IF(contribution_plan_days_mtd >= plan_contribution_mtd, 'ок', 'внимание')) FROM h" +
      " UNION ALL SELECT 10, 'Операционный результат MTD (вклад − OPEX)', CONCAT(CAST(CAST(ROUND(operating_result_mtd) AS INT64) AS STRING), ' ₽ · OPEX ', CAST(CAST(ROUND(opex_allocated_mtd) AS INT64) AS STRING), ' ₽'), IF(operating_result_mtd < 0, 'срочно', 'ок') FROM h" +
      ") ORDER BY ord",
      vs: { 'table.columns': [{ name: 'ord', enabled: false }, { name: 'Показатель', enabled: true }, { name: 'Значение', enabled: true }, { name: 'Статус', enabled: true }], column_settings: cs({ 'Показатель': { text_wrapping: true }, 'Значение': { text_wrapping: true } }), 'table.column_formatting': TONE('Статус'), 'table.column_widths': [190, 330, 80] } },
    { id: 104, key: 'month_cum', name: 'Текущий месяц · нарастающим итогом, карточки', display: 'line', sql:
      "WITH d AS (SELECT d AS day, SUM(target_cards) AS t, SUM(IF(is_past AND d < CURRENT_DATE(), IFNULL(actual_cards, 0), 0)) AS a, MAX(IF(is_past AND d < CURRENT_DATE(), 1, 0)) AS past FROM wb_mart.V_CT_PLAN_VS_ACTUAL_DAILY WHERE month = DATE_TRUNC(CURRENT_DATE(), MONTH) GROUP BY 1) SELECT day AS `Дата`, ROUND(SUM(t) OVER (ORDER BY day)) AS `План`, IF(past = 1, SUM(a) OVER (ORDER BY day), NULL) AS `Факт` FROM d ORDER BY day",
      vs: { 'graph.dimensions': ['Дата'], 'graph.metrics': ['План', 'Факт'], 'graph.y_axis.auto_split': false, 'graph.x_axis.title_text': '', 'graph.y_axis.title_text': 'карточки', series_settings: { 'План': { color: '#A989C5', 'line.style': 'dashed' }, 'Факт': { color: '#509EE3' } } } },
    // — actions —
    { key: 'decisions', name: 'Требует решения владельца', display: 'table', sql: ACTION_SQL('DECISION', 5), vs: ACTION_COLS('Что решить') },
    { id: 105, key: 'execution', name: 'Исполнение · фулфилмент и площадки', display: 'table', sql: ACTION_SQL('EXECUTION', 8), vs: ACTION_COLS('Что сделать') },
    { key: 'open_count', name: 'Открытых действий', display: 'scalar', sql: "SELECT CONCAT(CAST(open_actions AS STRING), ' открыто · ', CAST(open_decisions AS STRING), ' решений · ', CAST(open_executions AS STRING), ' на исполнении · ', CAST(done_actions_mtd AS STRING), ' закрыто в этом месяце') FROM " + H, vs: {} },
    { id: 106, key: 'attention', name: 'Требует внимания', display: 'table', sql: "SELECT CASE severity WHEN 'RED' THEN 'срочно' WHEN 'YELLOW' THEN 'внимание' WHEN 'BLUE' THEN 'рост' ELSE 'ок' END AS tone, headline AS what, ROUND(financial_effect_rub) AS effect FROM wb_mart.V_CT_ATTENTION ORDER BY severity_rank, sort_rank, metric_value LIMIT 6",
      vs: { column_settings: cs({ tone: { column_title: ' ' }, what: { column_title: 'Что происходит', text_wrapping: true }, effect: { column_title: 'Эффект, ₽', number_style: 'decimal', decimals: 0 } }), 'table.column_formatting': TONE('tone'), 'table.column_widths': [90, 460, 110] } },
    // — hand cream —
    { key: 'hc_pace', name: 'Крем для рук · темп к нужному', display: 'gauge', sql: 'SELECT SAFE_DIVIDE(hc_actual_units_per_day_7d, hc_required_units_per_day) * 100 AS pace FROM ' + H, vs: { 'gauge.segments': [{ min: 0, max: 60, color: RAG.RED, label: 'спишем' }, { min: 60, max: 90, color: RAG.YELLOW, label: 'риск' }, { min: 90, max: 150, color: RAG.GREEN, label: 'успеваем' }], ...money('pace', ' %') } },
    { key: 'hc_stock', name: 'Крем для рук · остаток', display: 'scalar', sql: 'SELECT hc_stock_units FROM ' + H, vs: { 'scalar.compact_primary_number': false, ...money('hc_stock_units', ' фл.') } },
    { key: 'hc_days', name: 'До срока годности 31.12.2026', display: 'scalar', sql: 'SELECT hc_days_to_expiry FROM ' + H, vs: { 'scalar.compact_primary_number': false, ...money('hc_days_to_expiry', ' дн.') } },
    { key: 'hc_writeoff', name: 'Спишется при текущем темпе', display: 'scalar', sql: 'SELECT CAST(ROUND(hc_projected_residual_at_expiry) AS INT64) AS v FROM ' + H, vs: { 'scalar.compact_primary_number': false, ...money('v', ' фл.') } },
    { id: 108, key: 'hc_table', name: 'Крем для рук · контроль', display: 'table', sql:
      "WITH h AS (SELECT * FROM " + H + ") SELECT ord, k AS `Показатель`, v AS `Значение`, tone AS `Статус` FROM (" +
      " SELECT 1 AS ord, 'Нужно продавать, чтобы выйти в ноль' AS k, CONCAT(FORMAT('%.1f', hc_required_units_per_day), ' фл./день') AS v, '' AS tone FROM h" +
      " UNION ALL SELECT 2, 'Фактический темп', CONCAT(FORMAT('%.1f', hc_actual_units_per_day_7d), ' фл./день за 7 дн. · ', FORMAT('%.1f', hc_actual_units_per_day_30d), ' за 30 дн.'), hc_status FROM h" +
      " UNION ALL SELECT 3, 'Продано в этом месяце', CONCAT(CAST(hc_units_mtd AS STRING), ' фл. (соло и в наборах)'), '' FROM h" +
      " UNION ALL SELECT 4, 'Риск списания по себестоимости', CONCAT(CAST(CAST(ROUND(hc_writeoff_risk_rub / 1000) AS INT64) AS STRING), ' тыс ₽'), IF(hc_writeoff_risk_rub > 0, 'RED', 'GREEN') FROM h" +
      " UNION ALL SELECT 5, 'Программа HC-B', 'WB 640 → 590 (окт) → 540 (ноя) → 490 (дек, пол 450); Ozon 973 → 895 → 850 → 799; реклама 6 → 17 %; наборы руки + Cherry/Amber', '' FROM h" +
      ") ORDER BY ord",
      vs: { 'table.columns': [{ name: 'ord', enabled: false }, { name: 'Показатель', enabled: true }, { name: 'Значение', enabled: true }, { name: 'Статус', enabled: true }], column_settings: cs({ 'Значение': { text_wrapping: true } }), 'table.column_formatting': TONE('Статус'), 'table.column_widths': [230, 380, 80] } },
    // — inventory —
    { key: 'inv_units', name: 'Остаток товара', display: 'scalar', sql: 'SELECT inventory_remaining_units FROM ' + H, vs: { 'scalar.compact_primary_number': false, ...money('inventory_remaining_units', ' фл.') } },
    { key: 'inv_value', name: 'По себестоимости', display: 'scalar', sql: 'SELECT ROUND(inventory_remaining_value_rub / 1000000, 2) AS v FROM ' + H, vs: { 'scalar.compact_primary_number': false, ...money('v', ' млн ₽', 2) } },
    { key: 'inv_conv', name: 'Запас продан, % (цель сезона)', display: 'progress', sql: 'SELECT ROUND(stock_reduction_pct, 1) AS v, ROUND(season_plan_stock_reduction_pct, 1) AS goal FROM ' + H, vs: { 'progress.goal': 53.9, 'progress.color': RAG.BLUE, ...money('v', ' %', 1) } },
    { key: 'inv_resid', name: 'Прогноз остатка 31.03.2027 по плану', display: 'scalar', sql: 'SELECT CAST(ROUND(projected_residual_31_03_units_at_plan) AS INT64) AS v FROM ' + H, vs: { 'scalar.compact_primary_number': false, ...money('v', ' фл.') } },
    { id: 107, key: 'inv_risks', name: 'Запас · главные риски', display: 'table', sql:
      "SELECT product_name AS sku, sellable_units AS units, ROUND(sellable_value_rub / 1000) AS value_k, CAST(ROUND(days_of_stock_total_at_plan_rate) AS INT64) AS days_plan, expiry_date AS expiry, CASE status WHEN 'RED' THEN 'срочно' WHEN 'YELLOW' THEN 'внимание' WHEN 'BLUE' THEN 'рост' ELSE 'ок' END AS tone, status_reason AS why FROM wb_mart.V_CT_INVENTORY_TRUTH WHERE status != 'GREEN' ORDER BY CASE status WHEN 'RED' THEN 0 WHEN 'YELLOW' THEN 1 ELSE 2 END, days_to_expiry, sellable_value_rub DESC LIMIT 6",
      vs: { column_settings: cs({ sku: { column_title: 'SKU' }, units: { column_title: 'Остаток, фл.', number_style: 'decimal', decimals: 0 }, value_k: { column_title: 'тыс ₽', number_style: 'decimal', decimals: 0 }, days_plan: { column_title: 'Дней по плану', number_style: 'decimal', decimals: 0 }, expiry: { column_title: 'Срок годности', date_style: 'D.M.YYYY' }, tone: { column_title: ' ' }, why: { column_title: 'Почему', text_wrapping: true } }), 'table.column_formatting': TONE('tone'), 'table.column_widths': [140, 100, 80, 110, 110, 90, 400] } },
    // — shipments & bundles —
    { id: 111, key: 'shipments', name: 'Что отгружать · WB и Ozon', display: 'table', sql: "SELECT marketplace AS mp, product_name AS sku, marketplace_units AS on_mp, CAST(ROUND(cover_days_at_plan_rate) AS INT64) AS cover, recommended_ship_units AS ship, CASE status WHEN 'RED' THEN 'срочно' WHEN 'YELLOW' THEN 'внимание' WHEN 'BLUE' THEN 'рост' ELSE 'ок' END AS tone FROM wb_mart.V_CT_SUPPLY_NEED WHERE recommended_ship_units > 0 ORDER BY cover_days_at_plan_rate LIMIT 6",
      vs: { column_settings: cs({ mp: { column_title: 'Канал' }, sku: { column_title: 'SKU' }, on_mp: { column_title: 'На площадке' }, cover: { column_title: 'Дней покрытия' }, ship: { column_title: 'Отгрузить, фл.' }, tone: { column_title: ' ' } }), 'table.column_formatting': TONE('tone'), 'table.column_widths': [60, 150, 100, 110, 110, 80] } },
    { id: 110, key: 'bundles', name: 'Наборы · сборка и наличие', display: 'table', sql: "SELECT product_name AS bundle, marketplace_cards_live AS live, assembly_need_next_45d AS need45, assemblable_now_ff AS can_now, CASE status WHEN 'RED' THEN 'срочно' WHEN 'YELLOW' THEN 'внимание' WHEN 'BLUE' THEN 'рост' ELSE 'ок' END AS tone FROM wb_mart.V_CT_BUNDLE_STATUS WHERE plan_cards_next_45d > 0 OR cards_ordered_mtd > 0 ORDER BY plan_cards_next_45d DESC LIMIT 6",
      vs: { column_settings: cs({ bundle: { column_title: 'Набор', text_wrapping: true }, live: { column_title: 'На площадках' }, need45: { column_title: 'Собрать на 45 дн.' }, can_now: { column_title: 'Можно собрать сейчас' }, tone: { column_title: ' ' } }), 'table.column_formatting': TONE('tone'), 'table.column_widths': [200, 100, 120, 140, 80] } },
    // — freshness compact —
    { id: 109, key: 'freshness', name: 'Свежесть данных', display: 'table', sql: "SELECT domain_ru AS src, CONCAT('● ', status) AS st, CASE age_days WHEN 0 THEN 'сегодня' WHEN 1 THEN 'вчера' ELSE CONCAT(CAST(age_days AS STRING), ' дн. назад') END AS age FROM wb_mart.V_CT_FRESHNESS WHERE domain NOT LIKE 'CT_%' ORDER BY sort_order",
      vs: { column_settings: cs({ src: { column_title: 'Источник' }, st: { column_title: ' ' }, age: { column_title: 'Актуально' } }), 'table.column_formatting': TONE('st'), 'table.column_widths': [260, 90, 120] } },
    // — details dashboard —
    { key: 'd_actions_all', name: 'Все действия · очередь и история', display: 'table', sql: "SELECT CASE status WHEN 'OPEN' THEN 'открыто' WHEN 'IN_PROGRESS' THEN 'в работе' WHEN 'DONE' THEN 'готово' WHEN 'CANCELLED' THEN 'отменено' ELSE 'снято правилом' END AS st, lane_ru AS lane, priority_short AS p, what_to_do AS what, qty_ru AS qty, executor_ru AS who, deadline_ru AS deadline, effect_ru AS effect, why_short AS why, owner_note AS note, FORMAT_TIMESTAMP('%d.%m %H:%M', status_updated_at, 'Europe/Moscow') AS updated, done_url AS done, progress_url AS progress, cancel_url AS cancel, action_id AS id FROM wb_mart.V_CT_ACTION_QUEUE ORDER BY status_rank, queue_rank LIMIT 200",
      vs: { column_settings: cs({ st: { column_title: 'Статус' }, lane: { column_title: 'Дорожка' }, p: { column_title: 'P' }, what: { column_title: 'Что сделать', text_wrapping: true }, qty: { column_title: 'Кол-во' }, who: { column_title: 'Куда / кому', text_wrapping: true }, deadline: { column_title: 'Срок' }, effect: { column_title: 'Эффект' }, why: { column_title: 'Почему', text_wrapping: true }, note: { column_title: 'Заметки', text_wrapping: true }, updated: { column_title: 'Изменено' }, done: { column_title: 'Готово', ...link('✅', 'done') }, progress: { column_title: 'В работе', ...link('▶', 'progress') }, cancel: { column_title: 'Отменить', ...link('✖', 'cancel') }, id: { column_title: 'ID' } }), 'table.column_formatting': [...PRI('p'), ...rag('st', [['открыто', RAG.YELLOW], ['в работе', RAG.BLUE], ['готово', RAG.GREEN], ['отменено', RAG.GREY], ['снято правилом', RAG.GREY]])] } },
    { key: 'd_status_log', name: 'Журнал смены статусов', display: 'table', sql: "SELECT FORMAT_TIMESTAMP('%d.%m.%Y %H:%M', changed_at, 'Europe/Moscow') AS ts, action_id AS id, old_status AS old_s, new_status AS new_s, changed_by AS who, owner_note AS note FROM evetis_ref.CT_ACTION_STATUS_LOG ORDER BY changed_at DESC LIMIT 100",
      vs: { column_settings: cs({ ts: { column_title: 'Когда' }, id: { column_title: 'ID' }, old_s: { column_title: 'Было' }, new_s: { column_title: 'Стало' }, who: { column_title: 'Кто' }, note: { column_title: 'Заметка', text_wrapping: true } }) } },
    { key: 'd_freshness', name: 'Свежесть данных · подробно', display: 'table', sql: "SELECT domain_ru AS src, data_as_of AS as_of, FORMAT_TIMESTAMP('%d.%m %H:%M', data_as_of_ts, 'Europe/Moscow') AS ts, age_days AS age, sla_days AS sla, status AS st, source FROM wb_mart.V_CT_FRESHNESS ORDER BY sort_order",
      vs: { column_settings: cs({ src: { column_title: 'Домен' }, as_of: { column_title: 'Данные по', date_style: 'D.M.YYYY' }, ts: { column_title: 'Время (МСК)' }, age: { column_title: 'Дней назад' }, sla: { column_title: 'SLA, дн.' }, st: { column_title: 'Статус' }, source: { column_title: 'Источник' } }), 'table.column_formatting': rag('st', [['OK', RAG.GREEN], ['STALE', RAG.RED], ['ERROR', RAG.RED]]) } },
    { key: 'd_refresh_log', name: 'Журнал обновления Control Tower', display: 'table', sql: "SELECT FORMAT_TIMESTAMP('%d.%m %H:%M:%S', run_ts, 'Europe/Moscow') AS ts, step, status AS st, rows_affected AS rows_n, message AS msg, ROUND(duration_ms / 1000, 1) AS sec FROM evetis_ref.CT_REFRESH_LOG ORDER BY run_ts DESC LIMIT 60",
      vs: { column_settings: cs({ ts: { column_title: 'Когда (МСК)' }, step: { column_title: 'Шаг' }, st: { column_title: 'Статус' }, rows_n: { column_title: 'Строк' }, msg: { column_title: 'Сообщение', text_wrapping: true }, sec: { column_title: 'Сек.' } }), 'table.column_formatting': rag('st', [['OK', RAG.GREEN], ['SKIP', RAG.GREY], ['WARN', RAG.YELLOW], ['ERROR', RAG.RED]]) } },
    { key: 'd_inventory', name: 'Запас по SKU · подробно', display: 'table', sql: "SELECT product_name AS sku, ff_total_units AS ff, wb_fbo_live_units AS wb, ozon_fbo_units AS ozon, ozon_transit_units AS transit, fbs_units AS fbs, sellable_units AS total, ROUND(sellable_value_rub) AS value_rub, CAST(ROUND(days_of_stock_total_at_plan_rate) AS INT64) AS days_plan, expiry_date AS expiry, inbound_units AS inbound, wb_lost_claimed_units AS lost, status AS st, status_reason AS why FROM wb_mart.V_CT_INVENTORY_TRUTH ORDER BY sellable_value_rub DESC",
      vs: { column_settings: cs({ sku: { column_title: 'SKU' }, ff: { column_title: 'ФФ' }, wb: { column_title: 'WB' }, ozon: { column_title: 'Ozon' }, transit: { column_title: 'В пути Ozon' }, fbs: { column_title: 'FBS' }, total: { column_title: 'Итого, фл.' }, value_rub: { column_title: 'По себестоимости, ₽', number_style: 'decimal', decimals: 0 }, days_plan: { column_title: 'Дней по плану' }, expiry: { column_title: 'Срок годности', date_style: 'D.M.YYYY' }, inbound: { column_title: 'Ожидается' }, lost: { column_title: 'Потеряно WB' }, st: { column_title: 'Статус' }, why: { column_title: 'Почему', text_wrapping: true } }), 'table.column_formatting': rag('st', [['RED', RAG.RED], ['YELLOW', RAG.YELLOW], ['BLUE', RAG.BLUE], ['GREEN', RAG.GREEN]]) } },
    { key: 'd_supply', name: 'Потребность в отгрузке · подробно', display: 'table', sql: "SELECT marketplace AS mp, product_name AS sku, marketplace_units AS on_mp, in_transit_units AS transit, ff_total_units AS ff, ROUND(plan_units_per_day, 2) AS plan_day, ROUND(units_per_day_30d, 2) AS fact_day, CAST(ROUND(cover_days_at_plan_rate) AS INT64) AS cover_plan, CAST(ROUND(cover_days_at_30d_rate) AS INT64) AS cover_fact, gap_units_45d AS gap45, recommended_ship_units AS ship, need_code AS need, status AS st FROM wb_mart.V_CT_SUPPLY_NEED ORDER BY marketplace, cover_days_at_plan_rate",
      vs: { column_settings: cs({ mp: { column_title: 'Канал' }, sku: { column_title: 'SKU' }, on_mp: { column_title: 'На площадке' }, transit: { column_title: 'В пути' }, ff: { column_title: 'На ФФ' }, plan_day: { column_title: 'План/день' }, fact_day: { column_title: 'Факт/день (30 дн.)' }, cover_plan: { column_title: 'Покрытие по плану, дн.' }, cover_fact: { column_title: 'Покрытие по факту, дн.' }, gap45: { column_title: 'Дефицит 45 дн.' }, ship: { column_title: 'Отгрузить' }, need: { column_title: 'Код' }, st: { column_title: 'Статус' } }), 'table.column_formatting': rag('st', [['RED', RAG.RED], ['YELLOW', RAG.YELLOW], ['BLUE', RAG.BLUE], ['GREEN', RAG.GREEN]]) } },
    { key: 'd_bundles', name: 'Наборы · подробно', display: 'table', sql: "SELECT product_name AS bundle, components AS comp, wb_cards_live AS wb, ozon_cards_live AS ozon, cards_ordered_mtd AS mtd, ROUND(plan_cards_mtd, 1) AS plan_mtd, ROUND(plan_cards_next_45d) AS plan45, assembly_need_next_45d AS need45, assemblable_now_ff AS can_now, limiting_component AS limiting, next_assembly_week_start AS next_week, next_assembly_qty AS next_qty, status_code AS code, status AS st FROM wb_mart.V_CT_BUNDLE_STATUS ORDER BY plan_cards_next_45d DESC",
      vs: { column_settings: cs({ bundle: { column_title: 'Набор', text_wrapping: true }, comp: { column_title: 'Состав', text_wrapping: true }, wb: { column_title: 'WB' }, ozon: { column_title: 'Ozon' }, mtd: { column_title: 'Заказано MTD' }, plan_mtd: { column_title: 'План MTD' }, plan45: { column_title: 'План 45 дн.' }, need45: { column_title: 'Собрать 45 дн.' }, can_now: { column_title: 'Можно сейчас' }, limiting: { column_title: 'Ограничивает' }, next_week: { column_title: 'Ближайшая сборка', date_style: 'D.M.YYYY' }, next_qty: { column_title: 'Кол-во' }, code: { column_title: 'Код' }, st: { column_title: 'Статус' } }), 'table.column_formatting': rag('st', [['RED', RAG.RED], ['YELLOW', RAG.YELLOW], ['BLUE', RAG.BLUE], ['GREEN', RAG.GREEN]]) } },
    { key: 'd_attention', name: 'Требует внимания · все сигналы', display: 'table', sql: "SELECT severity AS st, alert_type AS typ, headline AS what, detail, ROUND(financial_effect_rub) AS effect FROM wb_mart.V_CT_ATTENTION ORDER BY severity_rank, sort_rank, metric_value LIMIT 40",
      vs: { column_settings: cs({ st: { column_title: 'Статус' }, typ: { column_title: 'Тип' }, what: { column_title: 'Что происходит', text_wrapping: true }, detail: { column_title: 'Подробно', text_wrapping: true }, effect: { column_title: 'Эффект, ₽', number_style: 'decimal', decimals: 0 } }), 'table.column_formatting': rag('st', [['RED', RAG.RED], ['YELLOW', RAG.YELLOW], ['BLUE', RAG.BLUE], ['GREEN', RAG.GREEN]]) } },
    // — daily brief —
    { key: 'brief_title', name: 'Сводка дня', display: 'scalar', sql: "SELECT CONCAT('EVETIS · что сегодня делать · ', FORMAT_DATE('%d.%m.%Y', CURRENT_DATE())) AS t", vs: {} },
    { key: 'brief_lines', name: 'Ежедневная сводка владельца', display: 'table', sql: "SELECT section AS sec, line_text AS line, CASE tone WHEN 'RED' THEN 'срочно' WHEN 'YELLOW' THEN 'внимание' WHEN 'BLUE' THEN 'рост' WHEN 'GREEN' THEN 'ок' ELSE '' END AS tone FROM wb_mart.V_CT_DAILY_BRIEF_LINES ORDER BY section_ord, line_ord",
      vs: { column_settings: cs({ sec: { column_title: 'Раздел' }, line: { column_title: ' ', text_wrapping: true }, tone: { column_title: ' ' } }), 'table.column_formatting': TONE('tone'), 'table.column_widths': [200, 1000, 90] } },
    // — sales plan —
    { id: 112, key: 'sp_season', name: 'Сезон · нарастающим итогом, карточки', display: 'line', tags: true, sql:
      "WITH b AS (SELECT p.d AS day, SUM(p.target_cards) AS t, SUM(IF(p.is_past AND p.d < CURRENT_DATE(), IFNULL(p.actual_cards, 0), 0)) AS a, MAX(IF(p.is_past AND p.d < CURRENT_DATE(), 1, 0)) AS past FROM wb_mart.V_CT_PLAN_VS_ACTUAL_DAILY p LEFT JOIN evetis_ref.REF_PRODUCT_MASTER m ON m.internal_sku = p.internal_sku WHERE 1 = 1 " + FILT_P + " GROUP BY 1) SELECT day AS `Дата`, ROUND(SUM(t) OVER (ORDER BY day)) AS `План`, IF(past = 1, SUM(a) OVER (ORDER BY day), NULL) AS `Факт` FROM b ORDER BY day",
      vs: { 'graph.dimensions': ['Дата'], 'graph.metrics': ['План', 'Факт'], 'graph.y_axis.auto_split': false, 'graph.x_axis.title_text': '', 'graph.y_axis.title_text': 'карточки', series_settings: { 'План': { color: '#A989C5', 'line.style': 'dashed' }, 'Факт': { color: '#509EE3' } } } },
    { key: 'sp_season_summary', name: 'Сезон · итоги', display: 'table', sql:
      "WITH h AS (SELECT * FROM " + H + ") SELECT ord, k AS `Показатель`, v AS fact, p AS plan FROM (" +
      " SELECT 1 AS ord, 'Карточки' AS k, CAST(season_cards AS STRING) AS v, CAST(CAST(ROUND(season_plan_cards) AS INT64) AS STRING) AS p FROM h" +
      " UNION ALL SELECT 2, 'Флаконы', CAST(season_units_ordered AS STRING), CAST(CAST(ROUND(season_plan_units) AS INT64) AS STRING) FROM h" +
      " UNION ALL SELECT 3, 'Деньги (GMV), ₽', CAST(CAST(ROUND(season_gmv) AS INT64) AS STRING), CAST(CAST(ROUND(season_plan_gmv) AS INT64) AS STRING) FROM h" +
      " UNION ALL SELECT 4, 'Вклад (после площадки, рекламы и COGS), ₽', CAST(CAST(ROUND(season_contribution) AS INT64) AS STRING), CAST(CAST(ROUND(season_plan_contribution) AS INT64) AS STRING) FROM h" +
      " UNION ALL SELECT 5, 'OPEX распределённый, ₽', CAST(CAST(ROUND(season_opex) AS INT64) AS STRING), CAST(CAST(ROUND(season_plan_opex) AS INT64) AS STRING) FROM h" +
      " UNION ALL SELECT 6, 'Операционный результат (вклад − OPEX), ₽', CAST(CAST(ROUND(season_operating_result) AS INT64) AS STRING), CAST(CAST(ROUND(season_plan_operating_result) AS INT64) AS STRING) FROM h" +
      " UNION ALL SELECT 7, 'Остаток товара на конец сезона, фл.', CAST(CAST(ROUND(projected_residual_31_03_units_at_plan) AS INT64) AS STRING), CAST(CAST(ROUND(season_plan_closing_units) AS INT64) AS STRING) FROM h" +
      ") ORDER BY ord",
      vs: { 'table.columns': [{ name: 'ord', enabled: false }, { name: 'Показатель', enabled: true }, { name: 'fact', enabled: true }, { name: 'plan', enabled: true }], column_settings: cs({ 'Показатель': { text_wrapping: true }, fact: { column_title: 'Факт с 09.09' }, plan: { column_title: 'План сезона' } }), 'table.column_widths': [300, 130, 130] } },
    { id: 113, key: 'sp_weeks', name: 'Текущий месяц · по неделям', display: 'bar', tags: true, sql:
      "SELECT CONCAT('с ', FORMAT_DATE('%d.%m', DATE_TRUNC(p.d, WEEK(MONDAY)))) AS `Неделя`, ROUND(SUM(p.target_cards)) AS `План`, SUM(IF(p.is_past AND p.d < CURRENT_DATE(), IFNULL(p.actual_cards, 0), 0)) AS `Факт` FROM wb_mart.V_CT_PLAN_VS_ACTUAL_DAILY p LEFT JOIN evetis_ref.REF_PRODUCT_MASTER m ON m.internal_sku = p.internal_sku WHERE p.month = DATE_TRUNC(CURRENT_DATE(), MONTH) " + FILT_P + " GROUP BY 1, DATE_TRUNC(p.d, WEEK(MONDAY)) ORDER BY DATE_TRUNC(p.d, WEEK(MONDAY))",
      vs: { 'graph.dimensions': ['Неделя'], 'graph.metrics': ['План', 'Факт'], 'graph.y_axis.auto_split': false, 'graph.x_axis.title_text': '', 'graph.y_axis.title_text': 'карточки', series_settings: { 'План': { color: '#A989C5' }, 'Факт': { color: '#509EE3' } } } },
    { id: 115, key: 'sp_mp_month', name: 'WB и Ozon · текущий месяц', display: 'bar', tags: true, sql:
      "SELECT marketplace AS `Канал`, ROUND(SUM(plan_cards_mtd)) AS `План за прошедшие дни`, SUM(actual_cards_mtd) AS `Факт`, ROUND(SUM(plan_cards_month)) AS `План месяца` FROM wb_mart.V_CT_SKU_CONTROL WHERE 1 = 1 " + FILT + " GROUP BY 1 ORDER BY 1",
      vs: { 'graph.dimensions': ['Канал'], 'graph.metrics': ['План за прошедшие дни', 'Факт'], 'graph.y_axis.auto_split': false, 'graph.x_axis.title_text': '', 'graph.y_axis.title_text': 'карточки', series_settings: { 'План за прошедшие дни': { color: '#A989C5' }, 'Факт': { color: '#509EE3' }, 'План месяца': { color: '#D1C4E9' } } } },
    { key: 'sp_mp_season', name: 'WB и Ozon · сезон', display: 'bar', tags: true, sql:
      "SELECT p.marketplace AS `Канал`, ROUND(SUM(p.target_cards)) AS `План сезона`, SUM(IF(p.is_past AND p.d < CURRENT_DATE(), IFNULL(p.actual_cards, 0), 0)) AS `Факт с начала сезона` FROM wb_mart.V_CT_PLAN_VS_ACTUAL_DAILY p LEFT JOIN evetis_ref.REF_PRODUCT_MASTER m ON m.internal_sku = p.internal_sku WHERE 1 = 1 " + FILT_P + " GROUP BY 1 ORDER BY 1",
      vs: { 'graph.dimensions': ['Канал'], 'graph.metrics': ['План сезона', 'Факт с начала сезона'], 'graph.y_axis.auto_split': false, 'graph.x_axis.title_text': '', 'graph.y_axis.title_text': 'карточки', series_settings: { 'План сезона': { color: '#A989C5' }, 'Факт с начала сезона': { color: '#509EE3' } } } },
    { id: 114, key: 'sp_top_sku', name: 'Top-12 SKU · текущий месяц (план и факт за прошедшие плановые дни)', display: 'row', tags: true, sql:
      "WITH s AS (SELECT product_name, SUM(plan_cards_month) AS plan_m, SUM(plan_cards_mtd) AS plan_mtd, SUM(actual_cards_mtd) AS fact FROM wb_mart.V_CT_SKU_CONTROL WHERE 1 = 1 " + FILT + " GROUP BY 1), r AS (SELECT *, ROW_NUMBER() OVER (ORDER BY plan_m DESC) AS rn FROM s) SELECT IF(rn <= 12, product_name, 'Остальные SKU') AS `SKU`, ROUND(SUM(plan_mtd), 1) AS `План`, SUM(fact) AS `Факт`, MIN(rn) AS ord FROM r GROUP BY 1 ORDER BY ord",
      vs: { 'graph.dimensions': ['SKU'], 'graph.metrics': ['План', 'Факт'], 'graph.max_categories_enabled': false, 'graph.max_categories': 20, 'graph.y_axis.auto_split': false, 'graph.x_axis.title_text': '', 'graph.y_axis.title_text': '', series_settings: { 'План': { color: '#A989C5' }, 'Факт': { color: '#509EE3' } } } },
    { id: 116, key: 'sp_sku_control', name: 'Top-20 SKU × канал · таблица контроля (текущий месяц)', display: 'table', tags: true, sql:
      "SELECT product_name AS sku, marketplace AS mp, ROUND(plan_cards_mtd, 1) AS plan_mtd, actual_cards_mtd AS fact_mtd, CAST(ROUND(attainment_pct) AS INT64) AS att, CAST(ROUND(forecast_eom_cards) AS INT64) AS forecast, CAST(ROUND(plan_cards_month) AS INT64) AS plan_month, CAST(ROUND(cover_days) AS INT64) AS cover, CASE status WHEN 'RED' THEN 'срочно' WHEN 'YELLOW' THEN 'внимание' WHEN 'BLUE' THEN 'рост' WHEN 'GREEN' THEN 'ок' ELSE '—' END AS tone, status_reason AS why FROM wb_mart.V_CT_SKU_CONTROL WHERE (plan_cards_month > 0 OR actual_cards_month_to_date_all > 0) " + FILT + " ORDER BY plan_cards_month DESC, marketplace LIMIT 20",
      vs: { column_settings: cs({ sku: { column_title: 'SKU' }, mp: { column_title: 'Канал' }, plan_mtd: { column_title: 'План MTD' }, fact_mtd: { column_title: 'Факт MTD' }, att: { column_title: '% плана' }, forecast: { column_title: 'Прогноз' }, plan_month: { column_title: 'План месяца' }, cover: { column_title: 'Запас, дн.' }, tone: { column_title: 'Статус' }, why: { column_title: 'Почему' } }), 'table.column_formatting': TONE('tone'), 'table.column_widths': [200, 70, 90, 90, 80, 90, 110, 100, 100, 200] } },
    { key: 'd_sku_full', name: 'SKU × канал · полная таблица контроля', display: 'table', sql:
      "SELECT product_name AS sku, marketplace AS mp, ROUND(plan_cards_mtd, 1) AS plan_mtd, actual_cards_mtd AS fact_mtd, CAST(ROUND(attainment_pct) AS INT64) AS att, CAST(ROUND(forecast_eom_cards) AS INT64) AS forecast, CAST(ROUND(plan_cards_month) AS INT64) AS plan_month, CAST(ROUND(cover_days) AS INT64) AS cover, CASE status WHEN 'RED' THEN 'срочно' WHEN 'YELLOW' THEN 'внимание' WHEN 'BLUE' THEN 'рост' WHEN 'GREEN' THEN 'ок' ELSE '—' END AS tone, status_reason AS why FROM wb_mart.V_CT_SKU_CONTROL WHERE (plan_cards_month > 0 OR actual_cards_month_to_date_all > 0) ORDER BY plan_cards_month DESC, marketplace LIMIT 200",
      vs: { column_settings: cs({ sku: { column_title: 'SKU' }, mp: { column_title: 'Канал' }, plan_mtd: { column_title: 'План MTD' }, fact_mtd: { column_title: 'Факт MTD' }, att: { column_title: '% плана' }, forecast: { column_title: 'Прогноз' }, plan_month: { column_title: 'План месяца' }, cover: { column_title: 'Запас, дн.' }, tone: { column_title: 'Статус' }, why: { column_title: 'Почему' } }), 'table.column_formatting': TONE('tone') } }
  ];

  const legacyQuery = (sql, withTags) => ({ database: DB, type: 'native', native: { query: sql, 'template-tags': withTags ? TAGS : {} } });
  const IDS = {};
  async function cards() {
    const existing = await mb('/collection/' + COLL + '/items?models=card');
    const byName = Object.fromEntries((existing.data || []).map(c => [c.name, c.id]));
    for (const c of CARDS) {
      const payload = { name: c.name, display: c.display, collection_id: COLL, visualization_settings: c.vs || {}, dataset_query: legacyQuery(c.sql, !!c.tags), description: 'EVETIS Control Tower Phase 1.1 · ' + c.key };
      let id = c.id || byName[c.name];
      let res;
      if (id) res = await mb('/card/' + id, 'PUT', payload); else res = await mb('/card', 'POST', { ...payload, type: 'question' });
      if (!res.id) { console.error('card failed', c.key, res); throw new Error('card ' + c.key); }
      IDS[c.key] = res.id;
    }
    return IDS;
  }

  // ── Layouts ─────────────────────────────────────────────────────────────────────
  const text = (row, col, w, h, md, opts = {}) => ({ id: -(row * 100 + col + 1), card_id: null, row, col, size_x: w, size_y: h, series: [], parameter_mappings: [], visualization_settings: { virtual_card: { name: null, display: 'text', visualization_settings: {}, dataset_query: {}, archived: false }, text: md, 'text.align_vertical': 'middle', 'dashcard.background': false, ...opts } });
  const heading = (row, text_) => ({ id: -(row * 100 + 50), card_id: null, row, col: 0, size_x: 24, size_y: 1, series: [], parameter_mappings: [], visualization_settings: { virtual_card: { name: null, display: 'heading', visualization_settings: {}, dataset_query: {}, archived: false }, text: text_, 'dashcard.background': false } });
  const card = (key, row, col, w, h, vs = {}, pm = []) => ({ id: -(row * 100 + col + 1), card_id: IDS[key], row, col, size_x: w, size_y: h, series: [], parameter_mappings: pm, visualization_settings: vs });
  const PARAMS = [{ id: 'p_marketplace', name: 'Канал', sectionId: 'string', slug: 'marketplace', type: 'string/=' }, { id: 'p_sales_mode', name: 'Соло / набор', sectionId: 'string', slug: 'sales_mode', type: 'string/=' }, { id: 'p_product_line', name: 'Линия', sectionId: 'string', slug: 'product_line', type: 'string/=' }, { id: 'p_sku', name: 'SKU', sectionId: 'string', slug: 'sku', type: 'string/=' }];
  const pmFor = (key) => ['marketplace', 'sales_mode', 'product_line', 'sku'].map(t => ({ card_id: IDS[key], parameter_id: 'p_' + t, target: ['variable', ['template-tag', t]] }));

  async function ensureDashboard(name, opts) {
    const items = await mb('/collection/' + COLL + '/items?models=dashboard');
    const found = (items.data || []).find(d => d.name === name);
    if (found) return found.id;
    const d = await mb('/dashboard', 'POST', { name, collection_id: COLL, ...opts });
    return d.id;
  }

  async function layouts() {
    const detailsId = await ensureDashboard('EVETIS CONTROL TOWER · ДЕТАЛИ', { description: 'Drill-down: полная очередь действий и история, свежесть, запас, отгрузки, наборы, журнал обновления.' });
    const briefId = await ensureDashboard('EVETIS DAILY BRIEF', { description: 'Ежедневная сводка владельца: что было вчера, план, отклонения, что делать сегодня, что требует решения.' });
    const DET = '/dashboard/' + detailsId, BRIEF = '/dashboard/' + briefId;

    // — Owner Home (5) —
    const home = [
      // Ряды подобраны визуальным QA 10.09.2026 (1440×900, full width): ни одна таблица не имеет
      // горизонтального overflow и внутренней вертикальной прокрутки (проверка ctQA в консоли).
      card('status_line', 0, 0, 9, 3), card('fresh_strip', 0, 9, 15, 3, { 'card.title': '' }),
      card('kpi_sales', 3, 0, 5, 3), card('kpi_units', 3, 5, 3, 3), card('kpi_contrib', 3, 8, 5, 3), card('kpi_drr', 3, 13, 3, 3), card('kpi_plan', 3, 16, 4, 3), card('kpi_stock', 3, 20, 4, 3),
      heading(6, 'Что делать сегодня'),
      card('decisions', 7, 0, 24, 8), card('execution', 15, 0, 24, 15),
      text(30, 0, 16, 2, '**Все действия, история и статусы →** [Открыть детали](' + DET + ') · **Сводка дня →** [Ежедневный бриф](' + BRIEF + ')'),
      card('open_count', 30, 16, 8, 2),
      heading(32, 'План и факт'),
      card('month_cum', 33, 0, 12, 10), card('month_summary', 33, 12, 12, 10),
      heading(43, 'Требует внимания'), card('attention', 44, 0, 24, 6),
      heading(50, 'Крем для рук — жёсткий срок годности 31.12.2026'),
      card('hc_pace', 51, 0, 5, 3), card('hc_stock', 51, 5, 5, 3), card('hc_days', 51, 10, 5, 3), card('hc_writeoff', 51, 15, 9, 3),
      card('hc_table', 54, 0, 24, 6),
      heading(60, 'Запас в деньги'),
      card('inv_units', 61, 0, 5, 3), card('inv_value', 61, 5, 5, 3), card('inv_conv', 61, 10, 8, 3), card('inv_resid', 61, 18, 6, 3),
      card('inv_risks', 64, 0, 24, 6),
      heading(70, 'Отгрузки и наборы'),
      card('shipments', 71, 0, 12, 7), card('bundles', 71, 12, 12, 7),
      heading(78, 'Свежесть данных'),
      card('freshness', 79, 0, 10, 8),
      text(79, 10, 14, 1, 'Подробные метки времени и журнал обновления → [Детали](' + DET + ')')
    ];
    const r5 = await mb('/dashboard/5', 'PUT', { width: 'full', dashcards: home, parameters: [] });
    if (!r5.dashcards) throw new Error('home layout: ' + JSON.stringify(r5).slice(0, 200));

    // — Sales Plan (6) —
    const sp = [
      heading(0, 'A · Траектория сезона'),
      card('sp_season', 1, 0, 15, 7, {}, pmFor('sp_season')), card('sp_season_summary', 1, 15, 9, 7),
      heading(8, 'B · Текущий месяц'),
      card('sp_weeks', 9, 0, 12, 10, {}, pmFor('sp_weeks')), card('month_summary', 9, 12, 12, 10),
      heading(19, 'C · Каналы'),
      card('sp_mp_month', 20, 0, 12, 5, {}, pmFor('sp_mp_month')), card('sp_mp_season', 20, 12, 12, 5, {}, pmFor('sp_mp_season')),
      heading(25, 'D · SKU'),
      card('sp_top_sku', 26, 0, 24, 8, {}, pmFor('sp_top_sku')),
      heading(34, 'E · Таблица контроля SKU (все SKU — в Деталях)'),
      card('sp_sku_control', 35, 0, 24, 15, {}, pmFor('sp_sku_control')),
      text(50, 0, 24, 1, 'Все SKU × канал, GMV, вклад, запас → [Детали Control Tower](' + DET + ')')
    ];
    const r6 = await mb('/dashboard/6', 'PUT', { width: 'full', dashcards: sp, parameters: PARAMS });
    if (!r6.dashcards) throw new Error('sales plan layout: ' + JSON.stringify(r6).slice(0, 200));

    // — Details —
    const det = [
      text(0, 0, 24, 1, '← [EVETIS OWNER HOME](/dashboard/5) · [EVETIS SALES PLAN](/dashboard/6) · [Ежедневный бриф](' + BRIEF + ')'),
      heading(1, 'Действия · вся очередь и история'), card('d_actions_all', 2, 0, 24, 12), card('d_status_log', 14, 0, 24, 6),
      heading(20, 'Запас'), card('d_inventory', 21, 0, 24, 6),
      heading(27, 'Отгрузки'), card('d_supply', 28, 0, 24, 8),
      heading(36, 'Наборы'), card('d_bundles', 37, 0, 24, 7),
      heading(44, 'Сигналы'), card('d_attention', 45, 0, 24, 8),
      heading(53, 'Данные и обновление'), card('d_freshness', 54, 0, 12, 6), card('d_refresh_log', 54, 12, 12, 6),
      heading(60, 'SKU × канал · полная таблица контроля'), card('d_sku_full', 61, 0, 24, 20)
    ];
    const r8 = await mb(DET, 'PUT', { width: 'full', dashcards: det, parameters: [] });
    if (!r8.dashcards) throw new Error('details layout: ' + JSON.stringify(r8).slice(0, 200));

    // — Daily brief —
    const br = [card('brief_title', 0, 0, 24, 2), card('brief_lines', 2, 0, 24, 16), text(18, 0, 24, 1, '← [EVETIS OWNER HOME](/dashboard/5) · [Детали](' + DET + ')')];
    const r9 = await mb(BRIEF, 'PUT', { width: 'full', dashcards: br, parameters: [] });
    if (!r9.dashcards) throw new Error('brief layout: ' + JSON.stringify(r9).slice(0, 200));
    return { detailsId, briefId };
  }

  async function all() { await cards(); return layouts(); }
  window.ctBuild = { cards, layouts, all, IDS, CARDS };
})();
