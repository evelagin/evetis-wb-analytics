/*
 * EVETIS OWNER CONTROL TOWER — Phase 1.2 · Metabase build script (OWNER UX SIMPLIFICATION)
 * ------------------------------------------------------------------------------------------
 * Run in the browser console on the Metabase origin (http://localhost:3000) while logged in
 * as the owner. Same-origin `fetch` uses the session cookie; nothing else is needed.
 * Idempotent: cards are matched by fixed id (97–116) or by the key stored in the card
 * description («EVETIS Control Tower Phase 1.x · <key>») inside collection 9 and UPDATED in place;
 * new cards are created once. Dashboards 5 (Owner Home), 6 (Sales Plan), «ДЕТАЛИ», «DAILY BRIEF»
 * are re-laid out.
 *
 * Phase 1.2 changes vs tools/metabase_ct_phase11_build.js:
 *   · labels: «Карточки» → «Проданные позиции, шт.», «Флаконы» → «Физические единицы, шт.»,
 *     «План продаж / Факт продаж / Выполнение плана / Прогноз месяца / Вклад / Операционный результат»;
 *   · KPI (Trend) cards: context vs plan AND vs the same weekday a week ago; stock vs season start / plan;
 *   · Owner Home order: KPI → A План месяца → B Требует решения владельца → C Исполнить сегодня →
 *     D Главные риски → E Крем для рук → F Запас и деньги; shipments/bundles/freshness → Details;
 *   · actions: no «…» truncation, «Зачем» one sentence (why_owner), typed effect (effect_line);
 *   · Sales Plan: period navigation (Сегодня | 7 дней | Текущий месяц | Сезон) + channel (WB | Ozon)
 *     as static-list dashboard filters; order: период → текущий месяц по неделям → топ-12 SKU →
 *     каналы → сезон → таблица SKU × канал (9 columns, statuses СРОЧНО/ВНИМАНИЕ/ПО ПЛАНУ/РОСТ);
 *   · Daily Brief: six section cards instead of one giant table.
 *
 * Usage:   await ctBuild.all()          — everything
 *          await ctBuild.cards()        — cards only
 *          await ctBuild.layouts()      — dashboard layouts only
 * Rollback: run tools/metabase_ct_phase11_build.js (restores Phase 1.1 cards + layouts), then
 *           tools/ct_phase12_rollback.sh for BigQuery views.
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
  const TONE = (col) => rag(col, [['срочно', RAG.RED], ['внимание', RAG.YELLOW], ['рост', RAG.BLUE], ['ок', RAG.GREEN], ['СРОЧНО', RAG.RED], ['ВНИМАНИЕ', RAG.YELLOW], ['РОСТ', RAG.BLUE], ['ПО ПЛАНУ', RAG.GREEN], ['ВНЕ ПЛАНА', RAG.GREY], ['RED', RAG.RED], ['YELLOW', RAG.YELLOW], ['GREEN', RAG.GREEN], ['BLUE', RAG.BLUE], ['● OK', RAG.GREEN], ['● STALE', RAG.RED], ['● ERROR', RAG.RED], ['LOW', RAG.YELLOW], ['MEDIUM', RAG.BLUE], ['HIGH', RAG.GREEN]]);
  // Канал приходит из фильтра человеческим словом («WB» / «Ozon»), в данных — 'WB' / 'OZON' → UPPER().
  const TAGS = { marketplace: { name: 'marketplace', 'display-name': 'Канал', type: 'text', required: false }, sales_mode: { name: 'sales_mode', 'display-name': 'Соло/набор', type: 'text', required: false }, sku: { name: 'sku', 'display-name': 'SKU', type: 'text', required: false }, product_line: { name: 'product_line', 'display-name': 'Линия', type: 'text', required: false } };
  const TAGS_PERIOD = { ...TAGS, period: { name: 'period', 'display-name': 'Период', type: 'text', required: true, default: 'Текущий месяц' } };
  const FILT = '[[AND marketplace = UPPER({{marketplace}})]] [[AND sales_mode = {{sales_mode}}]] [[AND internal_sku = {{sku}}]] [[AND product_line = {{product_line}}]]';
  const FILT_P = '[[AND p.marketplace = UPPER({{marketplace}})]] [[AND p.sales_mode = {{sales_mode}}]] [[AND p.internal_sku = {{sku}}]] [[AND m.product_line = {{product_line}}]]';
  // Границы периода для фильтра «Период»: Сегодня (неполный день) | 7 дней (7 закрытых дней) | Текущий месяц (с 1-го по вчера) | Сезон (с первого планового дня по вчера)
  const PERIOD_CTE = "pr AS (SELECT {{period}} AS p, CASE {{period}} WHEN 'Сегодня' THEN CURRENT_DATE() WHEN '7 дней' THEN DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY) WHEN 'Сезон' THEN (SELECT MIN(d) FROM wb_mart.V_CT_PLAN_VS_ACTUAL_DAILY WHERE in_plan) ELSE DATE_TRUNC(CURRENT_DATE(), MONTH) END AS d0, IF({{period}} = 'Сегодня', CURRENT_DATE(), DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY)) AS d1)";
  const RU_MONEY = (x) => "CASE WHEN ABS(" + x + ") >= 1000000 THEN CONCAT(IF(" + x + " < 0, '−', '+'), FORMAT('%.1f', ABS(" + x + ") / 1000000), ' млн ₽') WHEN ABS(" + x + ") >= 1000 THEN CONCAT(IF(" + x + " < 0, '−', '+'), CAST(CAST(ROUND(ABS(" + x + ") / 1000) AS INT64) AS STRING), ' тыс ₽') ELSE CONCAT(IF(" + x + " < 0, '−', '+'), CAST(CAST(ROUND(ABS(" + x + ")) AS INT64) AS STRING), ' ₽') END";
  const money = (col, suffix = ' ₽', decimals = 0) => ({ column_settings: cs({ [col]: { number_style: 'decimal', decimals, suffix } }) });
  // Trend-карточка с двумя сравнениями: против плана и против сопоставимого периода (тот же день недели неделю назад).
  const trend = (col, planCol, label, suffix, decimals = 0, flip = false, prevCol = null, prevLabel = null) => ({
    'scalar.field': col, 'scalar.compact_primary_number': false, 'scalar.switch_positive_negative': flip,
    'scalar.comparisons': [{ id: 'c1', type: 'anotherColumn', column: planCol, label }, ...(prevCol ? [{ id: 'c2', type: 'anotherColumn', column: prevCol, label: prevLabel }] : [])],
    column_settings: cs({ [col]: { number_style: 'decimal', decimals, suffix }, [planCol]: { number_style: 'decimal', decimals, suffix }, ...(prevCol ? { [prevCol]: { number_style: 'decimal', decimals, suffix } } : {}) })
  });
  const link = (text, col) => ({ view_as: 'link', link_text: text, link_url: '{{' + col + '}}' });

  // ── Card catalogue ─────────────────────────────────────────────────────────────
  // Карточка действия: приоритет · что сделать (без обрезки, перенос) · сколько · куда/кому · срок · зачем (одна фраза) · тип эффекта и сумма.
  const ACTION_COLS = (verb) => ({
    'table.columns': ['p', 'what', 'qty', 'who', 'deadline', 'why', 'effect', 'done', 'progress'].map(n => ({ name: n, enabled: true })),
    column_settings: cs({ p: { column_title: 'P' }, what: { column_title: verb, text_wrapping: true }, qty: { column_title: 'Сколько' }, who: { column_title: 'Куда / кому', text_wrapping: true }, deadline: { column_title: 'Срок' }, why: { column_title: 'Зачем', text_wrapping: true }, effect: { column_title: 'Эффект', text_wrapping: true }, done: { column_title: 'Готово', ...link('✅', 'done') }, progress: { column_title: 'Начать', ...link('▶', 'progress') } }),
    'table.column_formatting': PRI('p'),
    // ⚠ Metabase масштабирует подсказки ширин под ширину карточки, но клампит колонку снизу 60 px —
    // подсказка < 60 даёт горизонтальный overflow (замерено: 11 px). Все подсказки ≥ 60.
    'table.column_widths': [60, 300, 90, 140, 80, 300, 170, 80, 80]
  });
  const ACTION_SQL = (lane, limit) => "SELECT priority_short AS p, what_to_do AS what, qty_ru AS qty, executor_ru AS who, deadline_ru AS deadline, why_owner AS why, IFNULL(effect_line, '—') AS effect, done_url AS done, progress_url AS progress FROM wb_mart.V_CT_ACTION_QUEUE WHERE is_open AND lane = '" + lane + "' ORDER BY lane_rank LIMIT " + limit;

  // Компактная строка свежести: одна строка, домен = колонка; ● = свежо, ◔ = стареет (снимок не сегодняшний), ✖ = STALE/ERROR.
  const FRESH_DOMS = [['WB_SALES', 'wb_sales', 'WB SALES'], ['OZON_SALES', 'ozon_sales', 'OZON SALES'], ['WB_STOCK', 'wb_stock', 'WB STOCK'], ['OZON_STOCK', 'ozon_stock', 'OZON STOCK'], ['FF_STOCK', 'ff_stock', 'FF STOCK'], ['WB_ADS', 'wb_ads', 'WB ADS'], ['OZON_ADS', 'ozon_ads', 'OZON ADS']];
  const FRESH_STRIP_SQL = "WITH f AS (SELECT domain, CONCAT(CASE WHEN status != 'OK' THEN '✖' WHEN age_days >= 1 AND domain IN ('FF_STOCK','OZON_STOCK','WB_STOCK','OZON_SALES') THEN '◔' ELSE '●' END, ' ', CASE WHEN status != 'OK' THEN status WHEN age_days = 0 THEN 'today' WHEN age_days = 1 THEN '1 day' ELSE CONCAT(CAST(age_days AS STRING), ' days') END) AS v FROM wb_mart.V_CT_FRESHNESS) SELECT " + FRESH_DOMS.map(d => "MAX(IF(domain = '" + d[0] + "', v, NULL)) AS " + d[1]).join(', ') + " FROM f";
  const CARDS = [
    // — status line —
    { key: 'status_line', name: 'CONTROL TOWER · обновление', display: 'scalar', sql: "SELECT CONCAT('CONTROL TOWER UPDATED AT: ', IFNULL(ct_last_ok_msk, '—'), ' МСК ● ', ct_refresh_status, IF(ct_warn_today > 0, ' ⚠ витрина продаж за вчера не собрана', '')) FROM " + H, vs: {} },
    { key: 'fresh_strip', name: 'Свежесть источников', display: 'table', sql: FRESH_STRIP_SQL, vs: { column_settings: cs(Object.fromEntries(FRESH_DOMS.map(d => [d[1], { column_title: d[2] }]))), 'table.column_formatting': [{ columns: FRESH_DOMS.map(d => d[1]), type: 'single', operator: 'contains', value: '✖', color: RAG.RED, highlight_row: false }, { columns: FRESH_DOMS.map(d => d[1]), type: 'single', operator: 'contains', value: '◔', color: RAG.YELLOW, highlight_row: false }, { columns: FRESH_DOMS.map(d => d[1]), type: 'single', operator: 'contains', value: '●', color: RAG.GREEN, highlight_row: false }] } },
    // — KPI row —
    { id: 97, key: 'kpi_sales', name: 'Продажи вчера, ₽', display: 'smartscalar', sql: 'SELECT yesterday_date AS d, gmv_yesterday AS v, plan_gmv_yesterday AS plan, gmv_lw AS lw FROM ' + H, vs: trend('v', 'plan', 'план дня', ' ₽', 0, false, 'lw', 'неделю назад') },
    { id: 98, key: 'kpi_units', name: 'Физические единицы вчера, шт.', display: 'smartscalar', sql: 'SELECT yesterday_date AS d, units_yesterday AS v, plan_units_yesterday AS plan, units_lw AS lw FROM ' + H, vs: trend('v', 'plan', 'план дня', ' шт.', 0, false, 'lw', 'неделю назад') },
    { id: 99, key: 'kpi_contrib', name: 'Вклад вчера, ₽ (не прибыль)', display: 'smartscalar', sql: 'SELECT yesterday_date AS d, contribution_yesterday AS v, plan_contribution_yesterday AS plan, contribution_lw AS lw FROM ' + H, vs: trend('v', 'plan', 'план дня', ' ₽', 0, false, 'lw', 'неделю назад') },
    { id: 100, key: 'kpi_drr', name: 'ДРР вчера, %', display: 'smartscalar', sql: 'SELECT yesterday_date AS d, drr_yesterday_pct AS v, plan_drr_yesterday_pct AS plan, drr_lw_pct AS lw FROM ' + H, vs: trend('v', 'plan', 'план', ' %', 1, true, 'lw', 'неделю назад') },
    { id: 101, key: 'kpi_plan', name: 'Выполнение плана месяца, %', display: 'gauge', sql: 'SELECT mtd_attainment_pct FROM ' + H, vs: { 'gauge.segments': [{ min: 0, max: 70, color: RAG.RED, label: 'отставание' }, { min: 70, max: 90, color: RAG.YELLOW, label: 'риск' }, { min: 90, max: 150, color: RAG.GREEN, label: 'по плану' }], ...money('mtd_attainment_pct', ' %') } },
    { id: 102, key: 'kpi_stock', name: 'Остаток товара, шт.', display: 'smartscalar', sql: 'SELECT as_of AS d, inventory_remaining_units AS v, opening_inventory_units - CAST(ROUND(plan_units_mtd) AS INT64) AS plan, opening_inventory_units AS lw FROM ' + H, vs: trend('v', 'plan', 'план на сегодня', ' шт.', 0, true, 'lw', 'начало сезона 09.09') },
    // — plan vs actual —
    { id: 103, key: 'month_summary', name: 'План месяца · план продаж, факт, прогноз', display: 'table', sql:
      // Один скан Owner Home + UNNEST(STRUCT) — UNION ALL из 10 веток превышает лимит сложности плана BigQuery.
      "SELECT l.ord, l.k AS `Показатель`, l.v AS `Значение`, l.tone AS `Статус` FROM " + H + " h, UNNEST([" +
      " STRUCT(1 AS ord, 'План продаж · прошедшие плановые дни' AS k, CONCAT(CAST(CAST(ROUND(plan_cards_mtd) AS INT64) AS STRING), ' поз. · ', CAST(CAST(ROUND(plan_units_mtd) AS INT64) AS STRING), ' физ. ед.') AS v, '' AS tone)," +
      " STRUCT(2, 'Факт продаж · те же дни', CONCAT(CAST(cards_ordered_plan_days_mtd AS STRING), ' поз. · ', CAST(units_plan_days_mtd AS STRING), ' физ. ед.'), '')," +
      " STRUCT(3, 'Выполнение плана', CONCAT(CAST(CAST(ROUND(mtd_attainment_pct) AS INT64) AS STRING), ' %'), CASE WHEN mtd_attainment_pct >= 90 THEN 'ПО ПЛАНУ' WHEN mtd_attainment_pct >= 70 THEN 'ВНИМАНИЕ' ELSE 'СРОЧНО' END)," +
      " STRUCT(4, 'План продаж · весь месяц', CONCAT(CAST(CAST(ROUND(plan_cards_month) AS INT64) AS STRING), ' поз. · ', CAST(CAST(ROUND(plan_units_month) AS INT64) AS STRING), ' физ. ед.'), '')," +
      " STRUCT(5, 'Прогноз месяца', CONCAT(CAST(CAST(ROUND(forecast_eom_cards_plan_days) AS INT64) AS STRING), ' поз. (', CAST(CAST(ROUND(forecast_eom_attainment_pct) AS INT64) AS STRING), ' % плана)'), CASE WHEN forecast_eom_attainment_pct >= 90 THEN 'ПО ПЛАНУ' WHEN forecast_eom_attainment_pct >= 70 THEN 'ВНИМАНИЕ' ELSE 'СРОЧНО' END)," +
      " STRUCT(6, 'Нужно в день / факт в день', CONCAT(FORMAT('%.1f', required_daily_velocity_remaining), ' / ', FORMAT('%.1f', actual_daily_velocity_plan_days), ' поз.'), IF(actual_daily_velocity_plan_days >= required_daily_velocity_remaining, 'ПО ПЛАНУ', 'СРОЧНО'))," +
      " STRUCT(7, 'Надёжность прогноза', CONCAT(forecast_confidence, ' (', forecast_confidence_ru, ')'), forecast_confidence)," +
      " STRUCT(8, 'Почему такая надёжность', forecast_confidence_reason, '')," +
      " STRUCT(9, 'Вклад · плановые дни (после площадки, рекламы и себестоимости; не прибыль)', CONCAT(CAST(CAST(ROUND(contribution_plan_days_mtd) AS INT64) AS STRING), ' ₽ (план ', CAST(CAST(ROUND(plan_contribution_mtd) AS INT64) AS STRING), ' ₽)'), IF(contribution_plan_days_mtd < 0, 'СРОЧНО', IF(contribution_plan_days_mtd >= plan_contribution_mtd, 'ПО ПЛАНУ', 'ВНИМАНИЕ')))," +
      " STRUCT(10, 'Операционный результат · месяц (вклад − OPEX)', CONCAT(CAST(CAST(ROUND(operating_result_mtd) AS INT64) AS STRING), ' ₽ · OPEX ', CAST(CAST(ROUND(opex_allocated_mtd) AS INT64) AS STRING), ' ₽'), IF(operating_result_mtd < 0, 'СРОЧНО', 'ПО ПЛАНУ'))" +
      "]) l ORDER BY l.ord",
      vs: { 'table.columns': [{ name: 'ord', enabled: false }, { name: 'Показатель', enabled: true }, { name: 'Значение', enabled: true }, { name: 'Статус', enabled: true }], column_settings: cs({ 'Показатель': { text_wrapping: true }, 'Значение': { text_wrapping: true } }), 'table.column_formatting': TONE('Статус'), 'table.column_widths': [230, 290, 90] } },
    { id: 104, key: 'month_cum', name: 'Текущий месяц · нарастающим итогом, проданные позиции', display: 'line', sql:
      "WITH d AS (SELECT d AS day, SUM(target_cards) AS t, SUM(IF(is_past AND d < CURRENT_DATE(), IFNULL(actual_cards, 0), 0)) AS a, MAX(IF(is_past AND d < CURRENT_DATE(), 1, 0)) AS past FROM wb_mart.V_CT_PLAN_VS_ACTUAL_DAILY WHERE month = DATE_TRUNC(CURRENT_DATE(), MONTH) GROUP BY 1) SELECT day AS `Дата`, ROUND(SUM(t) OVER (ORDER BY day)) AS `План`, IF(past = 1, SUM(a) OVER (ORDER BY day), NULL) AS `Факт` FROM d ORDER BY day",
      vs: { 'graph.dimensions': ['Дата'], 'graph.metrics': ['План', 'Факт'], 'graph.y_axis.auto_split': false, 'graph.x_axis.title_text': '', 'graph.y_axis.title_text': 'проданные позиции, шт.', series_settings: { 'План': { color: '#A989C5', 'line.style': 'dashed' }, 'Факт': { color: '#509EE3' } } } },
    // — actions —
    { key: 'decisions', name: 'Требует решения владельца · топ-5', display: 'table', sql: ACTION_SQL('DECISION', 5), vs: ACTION_COLS('Что решить') },
    { id: 105, key: 'execution', name: 'Исполнить сегодня · фулфилмент и площадки · топ-7', display: 'table', sql: ACTION_SQL('EXECUTION', 7), vs: ACTION_COLS('Что сделать') },
    { key: 'open_count', name: 'Открытых действий', display: 'scalar', sql: "SELECT CONCAT(CAST(open_actions AS STRING), ' открыто · ', CAST(open_decisions AS STRING), ' решений · ', CAST(open_executions AS STRING), ' на исполнении · ', CAST(done_actions_mtd AS STRING), ' закрыто в этом месяце') FROM " + H, vs: {} },
    { id: 106, key: 'attention', name: 'Главные риски · топ-5', display: 'table', sql: "SELECT CASE severity WHEN 'RED' THEN 'СРОЧНО' WHEN 'YELLOW' THEN 'ВНИМАНИЕ' WHEN 'BLUE' THEN 'РОСТ' ELSE 'ПО ПЛАНУ' END AS tone, headline AS what, CASE WHEN financial_effect_rub IS NULL THEN '—' ELSE CONCAT(CASE alert_type WHEN 'EXPIRY' THEN 'Риск списания' WHEN 'DRR' THEN 'Перерасход рекламы за 7 дн.' WHEN 'OVERSTOCK' THEN 'Потенциальный cash effect' ELSE 'Эффект' END, ': ', " + RU_MONEY('financial_effect_rub') + ") END AS effect FROM wb_mart.V_CT_ATTENTION ORDER BY severity_rank, sort_rank, metric_value LIMIT 5",
      vs: { column_settings: cs({ tone: { column_title: 'Статус' }, what: { column_title: 'Что происходит', text_wrapping: true }, effect: { column_title: 'Эффект', text_wrapping: true } }), 'table.column_formatting': TONE('tone'), 'table.column_widths': [110, 560, 320] } },
    // — hand cream —
    { key: 'hc_pace', name: 'Темп продаж к нужному, %', display: 'gauge', sql: 'SELECT SAFE_DIVIDE(hc_actual_units_per_day_7d, hc_required_units_per_day) * 100 AS pace FROM ' + H, vs: { 'gauge.segments': [{ min: 0, max: 60, color: RAG.RED, label: 'спишем' }, { min: 60, max: 90, color: RAG.YELLOW, label: 'риск' }, { min: 90, max: 150, color: RAG.GREEN, label: 'успеваем' }], ...money('pace', ' %') } },
    { key: 'hc_stock', name: 'Остаток крема для рук, шт.', display: 'scalar', sql: 'SELECT hc_stock_units FROM ' + H, vs: { 'scalar.compact_primary_number': false, ...money('hc_stock_units', ' шт.') } },
    { key: 'hc_days', name: 'До срока годности, дней', display: 'scalar', sql: 'SELECT hc_days_to_expiry FROM ' + H, vs: { 'scalar.compact_primary_number': false, ...money('hc_days_to_expiry', ' дн.') } },
    { key: 'hc_writeoff', name: 'Спишется при текущем темпе, шт.', display: 'scalar', sql: 'SELECT CAST(ROUND(hc_projected_residual_at_expiry) AS INT64) AS v FROM ' + H, vs: { 'scalar.compact_primary_number': false, ...money('v', ' шт.') } },
    { id: 108, key: 'hc_table', name: 'Крем для рук · контроль', display: 'table', sql:
      "SELECT l.ord, l.k AS `Показатель`, l.v AS `Значение`, l.tone AS `Статус` FROM " + H + " h, UNNEST([" +
      " STRUCT(1 AS ord, 'Нужно продавать, чтобы распродать к сроку' AS k, CONCAT(FORMAT('%.1f', hc_required_units_per_day), ' шт./день') AS v, '' AS tone)," +
      " STRUCT(2, 'Фактический темп', CONCAT(FORMAT('%.1f', hc_actual_units_per_day_7d), ' шт./день за 7 дн. · ', FORMAT('%.1f', hc_actual_units_per_day_30d), ' за 30 дн.'), CASE hc_status WHEN 'RED' THEN 'СРОЧНО' WHEN 'YELLOW' THEN 'ВНИМАНИЕ' ELSE 'ПО ПЛАНУ' END)," +
      " STRUCT(3, 'Продано в этом месяце', CONCAT(CAST(hc_units_mtd AS STRING), ' шт. (отдельно и в наборах)'), '')," +
      " STRUCT(4, 'Риск списания (по себестоимости)', CONCAT(CAST(CAST(ROUND(hc_writeoff_risk_rub / 1000) AS INT64) AS STRING), ' тыс ₽'), IF(hc_writeoff_risk_rub > 0, 'СРОЧНО', 'ПО ПЛАНУ'))," +
      " STRUCT(5, 'Программа HC-B', 'WB 640 → 590 (окт) → 540 (ноя) → 490 (дек, пол 450); Ozon 973 → 895 → 850 → 799; реклама 6 → 17 %; наборы руки + Cherry/Amber', '')" +
      "]) l ORDER BY l.ord",
      vs: { 'table.columns': [{ name: 'ord', enabled: false }, { name: 'Показатель', enabled: true }, { name: 'Значение', enabled: true }, { name: 'Статус', enabled: true }], column_settings: cs({ 'Значение': { text_wrapping: true } }), 'table.column_formatting': TONE('Статус'), 'table.column_widths': [230, 380, 80] } },
    // — inventory —
    { key: 'inv_units', name: 'Остаток товара, шт.', display: 'scalar', sql: 'SELECT inventory_remaining_units FROM ' + H, vs: { 'scalar.compact_primary_number': false, ...money('inventory_remaining_units', ' шт.') } },
    { key: 'inv_value', name: 'Деньги в товаре, млн ₽', display: 'scalar', sql: 'SELECT ROUND(inventory_remaining_value_rub / 1000000, 2) AS v FROM ' + H, vs: { 'scalar.compact_primary_number': false, ...money('v', ' млн ₽', 2) } },
    { key: 'inv_conv', name: 'Запас распродан, % (цель сезона 53,9 %)', display: 'progress', sql: 'SELECT ROUND(stock_reduction_pct, 1) AS v, ROUND(season_plan_stock_reduction_pct, 1) AS goal FROM ' + H, vs: { 'progress.goal': 53.9, 'progress.color': RAG.BLUE, ...money('v', ' %', 1) } },
    { key: 'inv_resid', name: 'Останется к 31.03.2027, шт.', display: 'scalar', sql: 'SELECT CAST(ROUND(projected_residual_31_03_units_at_plan) AS INT64) AS v FROM ' + H, vs: { 'scalar.compact_primary_number': false, ...money('v', ' шт.') } },
    { id: 107, key: 'inv_risks', name: 'Запас · главные риски · топ-5', display: 'table', sql:
      "SELECT product_name AS sku, sellable_units AS units, ROUND(sellable_value_rub / 1000) AS value_k, CAST(ROUND(days_of_stock_total_at_plan_rate) AS INT64) AS days_plan, expiry_date AS expiry, CASE status WHEN 'RED' THEN 'СРОЧНО' WHEN 'YELLOW' THEN 'ВНИМАНИЕ' WHEN 'BLUE' THEN 'РОСТ' ELSE 'ПО ПЛАНУ' END AS tone, status_reason AS why FROM wb_mart.V_CT_INVENTORY_TRUTH WHERE status != 'GREEN' ORDER BY CASE status WHEN 'RED' THEN 0 WHEN 'YELLOW' THEN 1 ELSE 2 END, days_to_expiry, sellable_value_rub DESC LIMIT 5",
      vs: { column_settings: cs({ sku: { column_title: 'Товар' }, units: { column_title: 'Остаток, шт.', number_style: 'decimal', decimals: 0 }, value_k: { column_title: 'Себестоимость, тыс ₽', number_style: 'decimal', decimals: 0 }, days_plan: { column_title: 'Запаса дней по плану', number_style: 'decimal', decimals: 0 }, expiry: { column_title: 'Срок годности', date_style: 'D.M.YYYY' }, tone: { column_title: 'Статус' }, why: { column_title: 'Почему', text_wrapping: true } }), 'table.column_formatting': TONE('tone'), 'table.column_widths': [150, 100, 130, 130, 110, 100, 300] } },
    // — shipments & bundles —
    { id: 111, key: 'shipments', name: 'Что отгружать · WB и Ozon', display: 'table', sql: "SELECT marketplace AS mp, product_name AS sku, marketplace_units AS on_mp, CAST(ROUND(cover_days_at_plan_rate) AS INT64) AS cover, recommended_ship_units AS ship, CASE status WHEN 'RED' THEN 'срочно' WHEN 'YELLOW' THEN 'внимание' WHEN 'BLUE' THEN 'рост' ELSE 'ок' END AS tone FROM wb_mart.V_CT_SUPPLY_NEED WHERE recommended_ship_units > 0 ORDER BY cover_days_at_plan_rate LIMIT 6",
      vs: { column_settings: cs({ mp: { column_title: 'Канал' }, sku: { column_title: 'SKU' }, on_mp: { column_title: 'На площадке' }, cover: { column_title: 'Дней покрытия' }, ship: { column_title: 'Отгрузить, фл.' }, tone: { column_title: ' ' } }), 'table.column_formatting': TONE('tone'), 'table.column_widths': [60, 150, 100, 110, 110, 80] } },
    { id: 110, key: 'bundles', name: 'Наборы · сборка и наличие', display: 'table', sql: "SELECT product_name AS bundle, marketplace_cards_live AS live, assembly_need_next_45d AS need45, assemblable_now_ff AS can_now, CASE status WHEN 'RED' THEN 'срочно' WHEN 'YELLOW' THEN 'внимание' WHEN 'BLUE' THEN 'рост' ELSE 'ок' END AS tone FROM wb_mart.V_CT_BUNDLE_STATUS WHERE plan_cards_next_45d > 0 OR cards_ordered_mtd > 0 ORDER BY plan_cards_next_45d DESC LIMIT 6",
      vs: { column_settings: cs({ bundle: { column_title: 'Набор', text_wrapping: true }, live: { column_title: 'На площадках' }, need45: { column_title: 'Собрать на 45 дн.' }, can_now: { column_title: 'Можно собрать сейчас' }, tone: { column_title: ' ' } }), 'table.column_formatting': TONE('tone'), 'table.column_widths': [200, 100, 120, 140, 80] } },
    // — freshness compact —
    { id: 109, key: 'freshness', name: 'Свежесть данных', display: 'table', sql: "SELECT domain_ru AS src, CONCAT('● ', status) AS st, CASE age_days WHEN 0 THEN 'сегодня' WHEN 1 THEN 'вчера' ELSE CONCAT(CAST(age_days AS STRING), ' дн. назад') END AS age FROM wb_mart.V_CT_FRESHNESS WHERE domain NOT LIKE 'CT_%' ORDER BY sort_order",
      vs: { column_settings: cs({ src: { column_title: 'Источник' }, st: { column_title: ' ' }, age: { column_title: 'Актуально' } }), 'table.column_formatting': TONE('st'), 'table.column_widths': [260, 90, 120] } },
    // — details dashboard —
    { key: 'd_actions_all', name: 'Все действия · очередь и история', display: 'table', sql: "SELECT CASE status WHEN 'OPEN' THEN 'открыто' WHEN 'IN_PROGRESS' THEN 'в работе' WHEN 'DONE' THEN 'готово' WHEN 'CANCELLED' THEN 'отменено' ELSE 'снято правилом' END AS st, lane_ru AS lane, priority_short AS p, what_to_do AS what, qty_ru AS qty, executor_ru AS who, deadline_ru AS deadline, effect_line AS effect, why_owner AS why, sku_breakdown AS skus, reason_text AS full_reason, owner_note AS note, FORMAT_TIMESTAMP('%d.%m %H:%M', status_updated_at, 'Europe/Moscow') AS updated, done_url AS done, progress_url AS progress, cancel_url AS cancel, action_id AS id FROM wb_mart.V_CT_ACTION_QUEUE ORDER BY status_rank, queue_rank LIMIT 200",
      vs: { column_settings: cs({ st: { column_title: 'Статус' }, lane: { column_title: 'Дорожка' }, p: { column_title: 'P' }, what: { column_title: 'Что сделать', text_wrapping: true }, qty: { column_title: 'Сколько' }, who: { column_title: 'Куда / кому', text_wrapping: true }, deadline: { column_title: 'Срок' }, effect: { column_title: 'Эффект', text_wrapping: true }, why: { column_title: 'Зачем', text_wrapping: true }, skus: { column_title: 'Разбивка по SKU', text_wrapping: true }, full_reason: { column_title: 'Полное обоснование', text_wrapping: true }, note: { column_title: 'Заметки', text_wrapping: true }, updated: { column_title: 'Изменено' }, done: { column_title: 'Готово', ...link('✅', 'done') }, progress: { column_title: 'Начать', ...link('▶', 'progress') }, cancel: { column_title: 'Отменить', ...link('✖', 'cancel') }, id: { column_title: 'ID' } }), 'table.column_formatting': [...PRI('p'), ...rag('st', [['открыто', RAG.YELLOW], ['в работе', RAG.BLUE], ['готово', RAG.GREEN], ['отменено', RAG.GREY], ['снято правилом', RAG.GREY]])] } },
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
    { key: 'brief_title', name: 'Сводка дня', display: 'scalar', sql: "SELECT CONCAT('EVETIS — план на сегодня · ', FORMAT_DATE('%d.%m.%Y', CURRENT_DATE())) AS t", vs: {} },
    { key: 'brief_lines', name: 'Ежедневная сводка владельца · полная таблица', display: 'table', sql: "SELECT section AS sec, line_text AS line, CASE tone WHEN 'RED' THEN 'СРОЧНО' WHEN 'YELLOW' THEN 'ВНИМАНИЕ' WHEN 'BLUE' THEN 'РОСТ' WHEN 'GREEN' THEN 'ПО ПЛАНУ' ELSE '' END AS tone FROM wb_mart.V_CT_DAILY_BRIEF_LINES ORDER BY section_ord, line_ord",
      vs: { column_settings: cs({ sec: { column_title: 'Раздел' }, line: { column_title: ' ', text_wrapping: true }, tone: { column_title: ' ' } }), 'table.column_formatting': TONE('tone'), 'table.column_widths': [200, 1000, 90] } },
    // Phase 1.2: сводка дня как шесть коротких секций (одна карточка = одна секция)
    ...[[1, 'brief_yesterday', 'Вчера'], [2, 'brief_month', 'План месяца'], [3, 'brief_deviations', 'Главные отклонения'], [4, 'brief_today', 'Сегодня сделать'], [5, 'brief_decisions', 'Решения владельца'], [6, 'brief_data', 'Данные']].map(([ord, key, title]) => ({
      key, name: title, display: 'table', sql: "SELECT CASE tone WHEN 'RED' THEN 'СРОЧНО' WHEN 'YELLOW' THEN 'ВНИМАНИЕ' WHEN 'BLUE' THEN 'РОСТ' WHEN 'GREEN' THEN 'ПО ПЛАНУ' ELSE '' END AS tone, line_text AS line FROM wb_mart.V_CT_DAILY_BRIEF_LINES WHERE section_ord = " + ord + " ORDER BY line_ord",
      vs: { column_settings: cs({ tone: { column_title: 'Статус' }, line: { column_title: ' ', text_wrapping: true } }), 'table.column_formatting': TONE('tone'), 'table.column_widths': [100, ord <= 2 ? 560 : 1250] }
    })),
    // — sales plan —
    // Phase 1.2: продажи за выбранный период (фильтр «Период» + «Канал»)
    { key: 'period_summary', name: 'Продажи за период · по каналам · проданные позиции, шт. (план и «факт по плану» — только плановые дни, с 09.09)', display: 'table', tags: 'period', sql:
      "WITH " + PERIOD_CTE + ", x AS (SELECT p.marketplace AS mp, SUM(IF(p.in_plan, p.target_cards, 0)) AS plan_c, SUM(IF(p.in_plan, IFNULL(p.actual_cards, 0), 0)) AS fact_c, SUM(IFNULL(p.actual_cards, 0)) AS fact_all, SUM(IFNULL(p.actual_physical_units, 0)) AS units, SUM(IFNULL(p.actual_gmv, 0)) AS gmv, SUM(IFNULL(p.actual_contribution, 0)) AS contrib FROM wb_mart.V_CT_PLAN_VS_ACTUAL_DAILY p LEFT JOIN evetis_ref.REF_PRODUCT_MASTER m ON m.internal_sku = p.internal_sku, pr WHERE p.d BETWEEN pr.d0 AND pr.d1 " + FILT_P + " GROUP BY 1), t AS (SELECT 'Все каналы' AS mp, SUM(plan_c) plan_c, SUM(fact_c) fact_c, SUM(fact_all) fact_all, SUM(units) units, SUM(gmv) gmv, SUM(contrib) contrib, 0 AS ord FROM x UNION ALL SELECT IF(mp = 'OZON', 'Ozon', mp), plan_c, fact_c, fact_all, units, gmv, contrib, IF(mp = 'WB', 1, 2) FROM x) " +
      "SELECT t.mp AS mp, CONCAT(pr.p, ': ', FORMAT_DATE('%d.%m', pr.d0), IF(pr.d0 = pr.d1, IF(pr.p = 'Сегодня', ' (день не закрыт)', ''), CONCAT(' – ', FORMAT_DATE('%d.%m', pr.d1)))) AS period, CAST(ROUND(plan_c) AS INT64) AS plan_c, fact_c, IF(plan_c > 0, CONCAT(CAST(CAST(ROUND(SAFE_DIVIDE(fact_c, plan_c) * 100) AS INT64) AS STRING), ' %'), '—') AS att, fact_all, units, CAST(ROUND(gmv) AS INT64) AS gmv, CAST(ROUND(contrib) AS INT64) AS contrib, CASE WHEN plan_c = 0 THEN '' WHEN SAFE_DIVIDE(fact_c, plan_c) >= 0.9 THEN 'ПО ПЛАНУ' WHEN SAFE_DIVIDE(fact_c, plan_c) >= 0.7 THEN 'ВНИМАНИЕ' ELSE 'СРОЧНО' END AS tone FROM t, pr ORDER BY ord",
      // ⚠ BigQuery не допускает запятые/точки в алиасах — человеческие заголовки задаются через column_title.
      // План и «факт по плану» считаются только по плановым дням (план стартовал 09.09), «продано всего» — по всем дням периода.
      // Заголовки таблиц Metabase не переносятся — единицы вынесены в название карточки, заголовки короткие.
      vs: { column_settings: cs({ mp: { column_title: 'Канал' }, period: { column_title: 'Период' }, plan_c: { column_title: 'План продаж' }, fact_c: { column_title: 'Факт по плану' }, att: { column_title: 'Выполнение плана' }, fact_all: { column_title: 'Продано всего' }, units: { column_title: 'Физ. единицы', number_style: 'decimal', decimals: 0 }, gmv: { column_title: 'Выручка, ₽', number_style: 'decimal', decimals: 0 }, contrib: { column_title: 'Вклад, ₽', number_style: 'decimal', decimals: 0 }, tone: { column_title: 'Статус' } }), 'table.column_formatting': TONE('tone'), 'table.column_widths': [100, 200, 120, 130, 150, 130, 120, 110, 100, 100] } },
    // Phase 1.2: топ-12 SKU текущего месяца — таблица вместо диаграммы (план, факт, % плана, запас дней, статус)
    { key: 'sp_top_sku_table', name: 'Топ-12 SKU · текущий месяц · проданные позиции, шт.', display: 'table', tags: true, sql:
      "WITH s AS (SELECT product_name, SUM(plan_cards_month) AS plan_m, SUM(plan_cards_mtd) AS plan_mtd, SUM(actual_cards_mtd) AS fact, SUM(forecast_eom_cards) AS fc, MIN(cover_days) AS cover, MIN(CASE status WHEN 'RED' THEN 0 WHEN 'YELLOW' THEN 1 WHEN 'GREEN' THEN 2 WHEN 'BLUE' THEN 3 ELSE 4 END) AS worst, STRING_AGG(IF(status IN ('RED', 'YELLOW'), CONCAT(channel_ru, ': ', status_reason), NULL), '; ') AS why FROM wb_mart.V_CT_SKU_CONTROL WHERE plan_cards_month > 0 " + FILT + " GROUP BY 1) " +
      "SELECT product_name AS sku, ROUND(plan_mtd, 1) AS plan_mtd, fact AS fact_mtd, IF(plan_mtd > 0, CONCAT(CAST(CAST(ROUND(SAFE_DIVIDE(fact, plan_mtd) * 100) AS INT64) AS STRING), ' %'), '—') AS att, CAST(ROUND(fc) AS INT64) AS forecast, CAST(ROUND(plan_m) AS INT64) AS plan_month, IFNULL(CAST(CAST(ROUND(cover) AS INT64) AS STRING), '—') AS cover, CASE worst WHEN 0 THEN 'СРОЧНО' WHEN 1 THEN 'ВНИМАНИЕ' WHEN 2 THEN 'ПО ПЛАНУ' WHEN 3 THEN 'РОСТ' ELSE 'ВНЕ ПЛАНА' END AS tone, IFNULL(why, '') AS why FROM s ORDER BY plan_m DESC LIMIT 12",
      vs: { column_settings: cs({ sku: { column_title: 'SKU', text_wrapping: true }, plan_mtd: { column_title: 'План продаж' }, fact_mtd: { column_title: 'Факт продаж' }, att: { column_title: 'Выполнение плана' }, forecast: { column_title: 'Прогноз месяца' }, plan_month: { column_title: 'План месяца' }, cover: { column_title: 'Запаса дней' }, tone: { column_title: 'Статус' }, why: { column_title: 'Почему', text_wrapping: true } }), 'table.column_formatting': TONE('tone'), 'table.column_widths': [200, 120, 120, 150, 140, 120, 110, 100, 240] } },
    // Phase 1.2: каналы текущего месяца — таблица рядом с диаграммой
    { key: 'channels_month', name: 'WB и Ozon · текущий месяц · проданные позиции, шт.', display: 'table', tags: true, sql:
      "WITH c AS (SELECT marketplace, SUM(plan_cards_mtd) plan_mtd, SUM(actual_cards_mtd) fact, SUM(forecast_eom_cards) fc, SUM(plan_cards_month) plan_m FROM wb_mart.V_CT_SKU_CONTROL WHERE 1 = 1 " + FILT + " GROUP BY 1) SELECT IF(marketplace = 'OZON', 'Ozon', marketplace) AS mp, ROUND(plan_mtd, 1) AS plan_mtd, fact AS fact_mtd, IF(plan_mtd > 0, CONCAT(CAST(CAST(ROUND(SAFE_DIVIDE(fact, plan_mtd) * 100) AS INT64) AS STRING), ' %'), '—') AS att, CAST(ROUND(fc) AS INT64) AS forecast, CAST(ROUND(plan_m) AS INT64) AS plan_month, CASE WHEN plan_mtd = 0 THEN '' WHEN SAFE_DIVIDE(fact, plan_mtd) >= 0.9 THEN 'ПО ПЛАНУ' WHEN SAFE_DIVIDE(fact, plan_mtd) >= 0.7 THEN 'ВНИМАНИЕ' ELSE 'СРОЧНО' END AS tone FROM c ORDER BY 1",
      vs: { column_settings: cs({ mp: { column_title: 'Канал' }, plan_mtd: { column_title: 'План' }, fact_mtd: { column_title: 'Факт' }, att: { column_title: '% плана' }, forecast: { column_title: 'Прогноз' }, plan_month: { column_title: 'План мес.' }, tone: { column_title: 'Статус' } }), 'table.column_formatting': TONE('tone'), 'table.column_widths': [80, 90, 90, 100, 90, 100, 100] } },
    { id: 112, key: 'sp_season', name: 'Сезон · нарастающим итогом, проданные позиции', display: 'line', tags: true, sql:
      "WITH b AS (SELECT p.d AS day, SUM(p.target_cards) AS t, SUM(IF(p.is_past AND p.d < CURRENT_DATE(), IFNULL(p.actual_cards, 0), 0)) AS a, MAX(IF(p.is_past AND p.d < CURRENT_DATE(), 1, 0)) AS past FROM wb_mart.V_CT_PLAN_VS_ACTUAL_DAILY p LEFT JOIN evetis_ref.REF_PRODUCT_MASTER m ON m.internal_sku = p.internal_sku WHERE 1 = 1 " + FILT_P + " GROUP BY 1) SELECT day AS `Дата`, ROUND(SUM(t) OVER (ORDER BY day)) AS `План`, IF(past = 1, SUM(a) OVER (ORDER BY day), NULL) AS `Факт` FROM b ORDER BY day",
      vs: { 'graph.dimensions': ['Дата'], 'graph.metrics': ['План', 'Факт'], 'graph.y_axis.auto_split': false, 'graph.x_axis.title_text': '', 'graph.y_axis.title_text': 'проданные позиции, шт.', series_settings: { 'План': { color: '#A989C5', 'line.style': 'dashed' }, 'Факт': { color: '#509EE3' } } } },
    { key: 'sp_season_summary', name: 'Сезон · итоги', display: 'table', sql:
      "SELECT l.ord, l.k AS `Показатель`, l.v AS fact, l.p AS plan FROM " + H + " h, UNNEST([" +
      " STRUCT(1 AS ord, 'Проданные позиции, шт.' AS k, CAST(season_cards AS STRING) AS v, CAST(CAST(ROUND(season_plan_cards) AS INT64) AS STRING) AS p)," +
      " STRUCT(2, 'Физические единицы, шт.', CAST(season_units_ordered AS STRING), CAST(CAST(ROUND(season_plan_units) AS INT64) AS STRING))," +
      " STRUCT(3, 'Выручка (заказы), ₽', CAST(CAST(ROUND(season_gmv) AS INT64) AS STRING), CAST(CAST(ROUND(season_plan_gmv) AS INT64) AS STRING))," +
      " STRUCT(4, 'Вклад (после площадки, рекламы и себестоимости; не прибыль), ₽', CAST(CAST(ROUND(season_contribution) AS INT64) AS STRING), CAST(CAST(ROUND(season_plan_contribution) AS INT64) AS STRING))," +
      " STRUCT(5, 'Постоянные расходы (OPEX), ₽', CAST(CAST(ROUND(season_opex) AS INT64) AS STRING), CAST(CAST(ROUND(season_plan_opex) AS INT64) AS STRING))," +
      " STRUCT(6, 'Операционный результат (вклад − OPEX), ₽', CAST(CAST(ROUND(season_operating_result) AS INT64) AS STRING), CAST(CAST(ROUND(season_plan_operating_result) AS INT64) AS STRING))," +
      " STRUCT(7, 'Остаток товара на конец сезона, шт.', CAST(CAST(ROUND(projected_residual_31_03_units_at_plan) AS INT64) AS STRING), CAST(CAST(ROUND(season_plan_closing_units) AS INT64) AS STRING))" +
      "]) l ORDER BY l.ord",
      vs: { 'table.columns': [{ name: 'ord', enabled: false }, { name: 'Показатель', enabled: true }, { name: 'fact', enabled: true }, { name: 'plan', enabled: true }], column_settings: cs({ 'Показатель': { text_wrapping: true }, fact: { column_title: 'Факт с 09.09' }, plan: { column_title: 'План сезона' } }), 'table.column_widths': [230, 115, 115] } },
    { id: 113, key: 'sp_weeks', name: 'Текущий месяц · план продаж и факт по неделям', display: 'bar', tags: true, sql:
      "SELECT CONCAT('с ', FORMAT_DATE('%d.%m', DATE_TRUNC(p.d, WEEK(MONDAY)))) AS `Неделя`, ROUND(SUM(p.target_cards)) AS `План`, SUM(IF(p.is_past AND p.d < CURRENT_DATE(), IFNULL(p.actual_cards, 0), 0)) AS `Факт` FROM wb_mart.V_CT_PLAN_VS_ACTUAL_DAILY p LEFT JOIN evetis_ref.REF_PRODUCT_MASTER m ON m.internal_sku = p.internal_sku WHERE p.month = DATE_TRUNC(CURRENT_DATE(), MONTH) " + FILT_P + " GROUP BY 1, DATE_TRUNC(p.d, WEEK(MONDAY)) ORDER BY DATE_TRUNC(p.d, WEEK(MONDAY))",
      vs: { 'graph.dimensions': ['Неделя'], 'graph.metrics': ['План', 'Факт'], 'graph.y_axis.auto_split': false, 'graph.x_axis.title_text': '', 'graph.y_axis.title_text': 'проданные позиции, шт.', series_settings: { 'План': { color: '#A989C5' }, 'Факт': { color: '#509EE3' } } } },
    { id: 115, key: 'sp_mp_month', name: 'WB и Ozon · текущий месяц · диаграмма', display: 'bar', tags: true, sql:
      "SELECT channel_ru AS `Канал`, ROUND(SUM(plan_cards_mtd)) AS `План продаж (прошедшие дни)`, SUM(actual_cards_mtd) AS `Факт продаж`, ROUND(SUM(plan_cards_month)) AS `План месяца` FROM wb_mart.V_CT_SKU_CONTROL WHERE 1 = 1 " + FILT + " GROUP BY 1 ORDER BY 1",
      vs: { 'graph.dimensions': ['Канал'], 'graph.metrics': ['План продаж (прошедшие дни)', 'Факт продаж'], 'graph.y_axis.auto_split': false, 'graph.x_axis.title_text': '', 'graph.y_axis.title_text': 'проданные позиции, шт.', series_settings: { 'План продаж (прошедшие дни)': { color: '#A989C5' }, 'Факт продаж': { color: '#509EE3' }, 'План месяца': { color: '#D1C4E9' } } } },
    { key: 'sp_mp_season', name: 'WB и Ozon · сезон', display: 'bar', tags: true, sql:
      "SELECT p.marketplace AS `Канал`, ROUND(SUM(p.target_cards)) AS `План сезона`, SUM(IF(p.is_past AND p.d < CURRENT_DATE(), IFNULL(p.actual_cards, 0), 0)) AS `Факт с начала сезона` FROM wb_mart.V_CT_PLAN_VS_ACTUAL_DAILY p LEFT JOIN evetis_ref.REF_PRODUCT_MASTER m ON m.internal_sku = p.internal_sku WHERE 1 = 1 " + FILT_P + " GROUP BY 1 ORDER BY 1",
      vs: { 'graph.dimensions': ['Канал'], 'graph.metrics': ['План сезона', 'Факт с начала сезона'], 'graph.y_axis.auto_split': false, 'graph.x_axis.title_text': '', 'graph.y_axis.title_text': 'проданные позиции, шт.', series_settings: { 'План сезона': { color: '#A989C5' }, 'Факт с начала сезона': { color: '#509EE3' } } } },
    { id: 114, key: 'sp_top_sku', name: 'Top-12 SKU · текущий месяц (план и факт за прошедшие плановые дни)', display: 'row', tags: true, sql:
      "WITH s AS (SELECT product_name, SUM(plan_cards_month) AS plan_m, SUM(plan_cards_mtd) AS plan_mtd, SUM(actual_cards_mtd) AS fact FROM wb_mart.V_CT_SKU_CONTROL WHERE 1 = 1 " + FILT + " GROUP BY 1), r AS (SELECT *, ROW_NUMBER() OVER (ORDER BY plan_m DESC) AS rn FROM s) SELECT IF(rn <= 12, product_name, 'Остальные SKU') AS `SKU`, ROUND(SUM(plan_mtd), 1) AS `План`, SUM(fact) AS `Факт`, MIN(rn) AS ord FROM r GROUP BY 1 ORDER BY ord",
      vs: { 'graph.dimensions': ['SKU'], 'graph.metrics': ['План', 'Факт'], 'graph.max_categories_enabled': false, 'graph.max_categories': 20, 'graph.y_axis.auto_split': false, 'graph.x_axis.title_text': '', 'graph.y_axis.title_text': '', series_settings: { 'План': { color: '#A989C5' }, 'Факт': { color: '#509EE3' } } } },
    // Phase 1.2: ровно 9 колонок — SKU · Канал · План MTD · Факт MTD · % плана · Прогноз месяца · Запас дней · Статус · Почему
    { id: 116, key: 'sp_sku_control', name: 'Топ-20 SKU × канал · таблица контроля · текущий месяц · проданные позиции, шт.', display: 'table', tags: true, sql:
      "SELECT product_name AS sku, channel_ru AS mp, ROUND(plan_cards_mtd, 1) AS plan_mtd, actual_cards_mtd AS fact_mtd, IF(plan_cards_mtd > 0, CONCAT(CAST(CAST(ROUND(attainment_pct) AS INT64) AS STRING), ' %'), '—') AS att, CAST(ROUND(forecast_eom_cards) AS INT64) AS forecast, IFNULL(CAST(CAST(ROUND(cover_days) AS INT64) AS STRING), '—') AS cover, status_ru AS tone, status_reason AS why FROM wb_mart.V_CT_SKU_CONTROL WHERE (plan_cards_month > 0 OR actual_cards_month_to_date_all > 0) " + FILT + " ORDER BY plan_cards_month DESC, marketplace LIMIT 20",
      vs: { column_settings: cs({ sku: { column_title: 'SKU', text_wrapping: true }, mp: { column_title: 'Канал' }, plan_mtd: { column_title: 'План продаж' }, fact_mtd: { column_title: 'Факт продаж' }, att: { column_title: 'Выполнение плана' }, forecast: { column_title: 'Прогноз месяца' }, cover: { column_title: 'Запаса дней' }, tone: { column_title: 'Статус' }, why: { column_title: 'Почему', text_wrapping: true } }), 'table.column_formatting': TONE('tone'), 'table.column_widths': [200, 70, 120, 120, 150, 140, 110, 100, 260] } },
    { key: 'd_sku_full', name: 'SKU × канал · полная таблица контроля', display: 'table', sql:
      "SELECT product_name AS sku, channel_ru AS mp, kind_ru AS kind, ROUND(plan_cards_mtd, 1) AS plan_mtd, actual_cards_mtd AS fact_mtd, IF(plan_cards_mtd > 0, CONCAT(CAST(CAST(ROUND(attainment_pct) AS INT64) AS STRING), ' %'), '—') AS att, CAST(ROUND(forecast_eom_cards) AS INT64) AS forecast, CAST(ROUND(plan_cards_month) AS INT64) AS plan_month, actual_units_mtd AS units, ROUND(actual_gmv_mtd) AS gmv, ROUND(actual_contribution_mtd) AS contrib, stock_units AS stock, IFNULL(CAST(CAST(ROUND(cover_days) AS INT64) AS STRING), '—') AS cover, status_ru AS tone, status_reason AS why FROM wb_mart.V_CT_SKU_CONTROL WHERE (plan_cards_month > 0 OR actual_cards_month_to_date_all > 0) ORDER BY plan_cards_month DESC, marketplace LIMIT 200",
      vs: { column_settings: cs({ sku: { column_title: 'SKU' }, mp: { column_title: 'Канал' }, kind: { column_title: 'Тип' }, plan_mtd: { column_title: 'План продаж MTD, поз.' }, fact_mtd: { column_title: 'Факт продаж MTD, поз.' }, att: { column_title: 'Выполнение плана' }, forecast: { column_title: 'Прогноз месяца, поз.' }, plan_month: { column_title: 'План месяца, поз.' }, units: { column_title: 'Физ. ед. MTD, шт.' }, gmv: { column_title: 'Выручка MTD, ₽', number_style: 'decimal', decimals: 0 }, contrib: { column_title: 'Вклад MTD, ₽', number_style: 'decimal', decimals: 0 }, stock: { column_title: 'На площадке, шт.' }, cover: { column_title: 'Запаса дней' }, tone: { column_title: 'Статус' }, why: { column_title: 'Почему' } }), 'table.column_formatting': TONE('tone') } }
  ];

  const legacyQuery = (sql, tags) => ({ database: DB, type: 'native', native: { query: sql, 'template-tags': tags === 'period' ? TAGS_PERIOD : (tags ? TAGS : {}) } });
  const IDS = {};
  async function cards() {
    const existing = await mb('/collection/' + COLL + '/items?models=card');
    // Карточки Phase 1.1/1.2 узнаются по ключу в описании («EVETIS Control Tower Phase 1.x · <key>»); имена могут меняться.
    // ⚠ Только по ключу из описания — совпадение по имени опасно (в Phase 1.2 «План месяца» = и секция сводки,
    // и прежнее имя gauge-карточки 101; поиск по имени перезаписал бы чужую карточку).
    const byKey = {};
    for (const c of existing.data || []) { const m = /Phase 1\.\d · (\w+)$/.exec(c.description || ''); if (m) byKey[m[1]] = c.id; }
    for (const c of CARDS) {
      const payload = { name: c.name, display: c.display, collection_id: COLL, visualization_settings: c.vs || {}, dataset_query: legacyQuery(c.sql, c.tags), description: 'EVETIS Control Tower Phase 1.2 · ' + c.key };
      let id = c.id || byKey[c.key];
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
  // Фильтры Sales Plan: «Период» и «Канал» — фиксированные списки с человеческими значениями (кнопок в Metabase нет,
  // static-list = простейший нативный выбор); SKU / Линия / Соло-набор — вторичные.
  const STATIC = (values) => ({ values_source_type: 'static-list', values_source_config: { values } });
  const PARAMS = [
    { id: 'p_period', name: 'Период', sectionId: 'string', slug: 'period', type: 'string/=', default: ['Текущий месяц'], required: true, ...STATIC(['Сегодня', '7 дней', 'Текущий месяц', 'Сезон']) },
    { id: 'p_marketplace', name: 'Канал (Все / WB / Ozon)', sectionId: 'string', slug: 'marketplace', type: 'string/=', ...STATIC(['WB', 'Ozon']) },
    { id: 'p_sales_mode', name: 'Товар / набор', sectionId: 'string', slug: 'sales_mode', type: 'string/=', ...STATIC(['SOLO', 'BUNDLE']) },
    { id: 'p_product_line', name: 'Линия', sectionId: 'string', slug: 'product_line', type: 'string/=' },
    { id: 'p_sku', name: 'SKU', sectionId: 'string', slug: 'sku', type: 'string/=' }];
  const pmFor = (key, withPeriod = false) => [...(withPeriod ? ['period'] : []), 'marketplace', 'sales_mode', 'product_line', 'sku'].map(t => ({ card_id: IDS[key], parameter_id: 'p_' + t, target: ['variable', ['template-tag', t]] }));

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

    // — Owner Home (5) — Phase 1.2: KPI → A План месяца → B Решения → C Исполнить → D Риски → E Крем для рук → F Запас
    const SP = '/dashboard/6';
    const home = [
      card('status_line', 0, 0, 9, 3), card('fresh_strip', 0, 9, 15, 3, { 'card.title': '' }),
      // 6 KPI в два ряда по три (8 колонок): Trend с двумя сравнениями при 3–5 колонках сжимает число до «34k» и режет подписи.
      card('kpi_sales', 3, 0, 8, 4), card('kpi_units', 3, 8, 8, 4), card('kpi_contrib', 3, 16, 8, 4), card('kpi_drr', 7, 0, 8, 4), card('kpi_plan', 7, 8, 8, 4), card('kpi_stock', 7, 16, 8, 4),
      text(11, 0, 24, 1, '**Подробнее →** [План продаж: неделя, топ SKU, каналы, сезон](' + SP + ') · [Сводка дня](' + BRIEF + ') · [Детали: все действия, история, запас, отгрузки, наборы, свежесть](' + DET + ')'),
      // Высоты подобраны визуальным QA 10.09.2026 (1440×900): 0 горизонтальных overflow, 0 внутренних прокруток.
      heading(12, 'A · План месяца'),
      card('month_summary', 13, 0, 12, 11), card('month_cum', 13, 12, 12, 11),
      heading(24, 'B · Требует решения владельца'),
      card('decisions', 25, 0, 24, 8),
      heading(33, 'C · Исполнить сегодня'),
      card('execution', 34, 0, 24, 11),
      text(45, 0, 10, 2, '**Все действия, история и статусы →** [Открыть детали](' + DET + ')'),
      card('open_count', 45, 10, 14, 2),
      heading(47, 'D · Главные риски'), card('attention', 48, 0, 24, 6),
      heading(54, 'E · Крем для рук — жёсткий срок годности 31.12.2026'),
      card('hc_pace', 55, 0, 5, 3), card('hc_stock', 55, 5, 5, 3), card('hc_days', 55, 10, 5, 3), card('hc_writeoff', 55, 15, 9, 3),
      card('hc_table', 58, 0, 24, 6),
      heading(64, 'F · Запас и деньги'),
      card('inv_units', 65, 0, 5, 3), card('inv_value', 65, 5, 5, 3), card('inv_conv', 65, 10, 8, 3), card('inv_resid', 65, 18, 6, 3),
      card('inv_risks', 68, 0, 24, 7),
      text(75, 0, 24, 1, 'Отгрузки, наборы, свежесть источников, журнал обновления → [Детали](' + DET + ')')
    ];
    const r5 = await mb('/dashboard/5', 'PUT', { width: 'full', dashcards: home, parameters: [] });
    if (!r5.dashcards) throw new Error('home layout: ' + JSON.stringify(r5).slice(0, 200));

    // — Sales Plan (6) — Phase 1.2: период → текущий месяц по неделям → топ-12 SKU → каналы → сезон → таблица SKU × канал
    const sp = [
      text(0, 0, 24, 1, '← [Owner Home](/dashboard/5) · Фильтры сверху: **Период** (Сегодня | 7 дней | Текущий месяц | Сезон) действует на блок «Продажи за период»; **Канал** (Все | WB | Ozon) — на все блоки. Остальное — текущий месяц и сезон.'),
      heading(1, 'Продажи за период'),
      card('period_summary', 2, 0, 24, 5, {}, pmFor('period_summary', true)),
      heading(7, 'A · Текущий месяц · план продаж и факт по неделям'),
      card('sp_weeks', 8, 0, 12, 11, {}, pmFor('sp_weeks')), card('month_summary', 8, 12, 12, 11),
      heading(19, 'B · Топ-12 SKU текущего месяца'),
      card('sp_top_sku_table', 20, 0, 24, 13, {}, pmFor('sp_top_sku_table')),
      heading(33, 'C · Каналы · WB и Ozon · текущий месяц'),
      card('sp_mp_month', 34, 0, 12, 6, {}, pmFor('sp_mp_month')), card('channels_month', 34, 12, 12, 6, {}, pmFor('channels_month')),
      heading(40, 'D · Сезон 09.09.2026 – 31.03.2027 · нарастающим итогом'),
      card('sp_season', 41, 0, 15, 7, {}, pmFor('sp_season')), card('sp_season_summary', 41, 15, 9, 7),
      heading(48, 'E · SKU × канал · таблица контроля (топ-20; все SKU — в Деталях)'),
      card('sp_sku_control', 49, 0, 24, 15, {}, pmFor('sp_sku_control')),
      text(64, 0, 24, 1, 'Все SKU × канал, выручка, вклад, запас · диаграмма топ-SKU и каналы за сезон → [Детали Control Tower](' + DET + ')')
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
      heading(60, 'SKU × канал · полная таблица контроля'), card('d_sku_full', 61, 0, 24, 20),
      heading(81, 'Диаграммы, убранные с владельческих экранов (Phase 1.2)'),
      card('sp_top_sku', 82, 0, 12, 8), card('sp_mp_season', 82, 12, 12, 8),
      card('shipments', 90, 0, 8, 7), card('bundles', 90, 8, 8, 7), card('freshness', 90, 16, 8, 7),
      heading(97, 'Ежедневная сводка · полная таблица'), card('brief_lines', 98, 0, 24, 16)
    ];
    const r8 = await mb(DET, 'PUT', { width: 'full', dashcards: det, parameters: [] });
    if (!r8.dashcards) throw new Error('details layout: ' + JSON.stringify(r8).slice(0, 200));

    // — Daily brief — Phase 1.2: шесть коротких секций
    const br = [
      card('brief_title', 0, 0, 24, 2),
      card('brief_yesterday', 2, 0, 12, 5), card('brief_month', 2, 12, 12, 5),
      card('brief_deviations', 7, 0, 24, 5),
      card('brief_today', 12, 0, 24, 6),
      card('brief_decisions', 18, 0, 24, 5),
      card('brief_data', 23, 0, 24, 4),
      text(27, 0, 24, 1, '← [EVETIS OWNER HOME](/dashboard/5) · [План продаж](/dashboard/6) · [Детали (полная таблица сводки, все действия)](' + DET + ')')
    ];
    const r9 = await mb(BRIEF, 'PUT', { width: 'full', dashcards: br, parameters: [] });
    if (!r9.dashcards) throw new Error('brief layout: ' + JSON.stringify(r9).slice(0, 200));
    return { detailsId, briefId };
  }

  async function all() { await cards(); return layouts(); }
  window.ctBuild = { cards, layouts, all, IDS, CARDS, mb };
})();
