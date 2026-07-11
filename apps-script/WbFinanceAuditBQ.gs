/**
 * ══════════════════════════════════════════════════════════════
 *  WbFinanceAuditBQ.gs — аудит финансовых данных в BigQuery
 *  (проверка Фазы B). Только ЧТЕНИЕ из вью V_WB_FINANCE.
 * ══════════════════════════════════════════════════════════════
 *  Цель: убедиться, что каждый рубль расхода на месте и понятен —
 *  комиссия, эквайринг, логистика, хранение, штрафы, удержания.
 *  Плюс проверка качества данных.
 *
 *  Логика комиссии (по договорённости) — НЕ фиксировать константой,
 *  моделировать по периодам:
 *    комиссия с эквайрингом (gross) = retail_amount − for_pay
 *    чистая комиссия (без эквайринга) = retail_amount − for_pay − acquiring_fee
 *    (эквайринг прибавляем к перечислению, вычитаем из цены покупки)
 *  Комиссию считаем ТОЛЬКО по продажам (doc_type_name='Продажа'),
 *  возвраты — отдельно.
 *
 *  Функции (после backfill 193/193):
 *    wbFinAuditQuality()             — качество данных
 *    wbFinAuditCommissionByMonth()   — модель комиссии по месяцам (продажи)
 *    wbFinAuditByMonth()             — расходы по статьям по месяцам
 *    wbFinAuditByOper()              — по типам операций WB
 *    wbFinAuditBySku()               — по SKU (заготовка маржи)
 * ══════════════════════════════════════════════════════════════
 */

/** SQL-выражение: строка-число WB → FLOAT64 (убираем пробелы, запятую→точку). */
function wbNum_(col) {
  return 'SUM(SAFE_CAST(REPLACE(REPLACE(IFNULL(' + col + ',"0")," ",""),",",".") AS FLOAT64))';
}

/** Полное имя вью с дедупом. */
function wbFinViewFq_() {
  var c = getBqConfig_();
  return '`' + c.projectId + '.' + c.datasetId + '.' + WB_FIN_BQ_VIEW_ + '`';
}

/** Печать строк результата как TSV с заголовком. */
function wbFinPrintRows_(res, header) {
  console.log(header.join('\t'));
  for (var i = 0; i < res.rows.length; i++) {
    var f = res.rows[i].f;
    var line = [];
    for (var j = 0; j < f.length; j++) line.push(f[j].v === null ? '' : f[j].v);
    console.log(line.join('\t'));
  }
}


/** Качество данных: строки RAW vs вью, уник. nm_id, пропуски, парсинг, период. */
function wbFinAuditQuality() {
  var c = getBqConfig_();
  var raw = '`' + c.projectId + '.' + c.datasetId + '.' + BQ_TABLE_FINANCE_ + '`';
  var v = wbFinViewFq_();
  var sql =
    'SELECT\n' +
    '  (SELECT COUNT(*) FROM ' + raw + ') AS raw_rows,\n' +
    '  (SELECT COUNT(*) FROM ' + v + ') AS view_rows,\n' +
    '  (SELECT COUNT(DISTINCT wb_nm_id) FROM ' + v + ') AS uniq_nm,\n' +
    '  (SELECT COUNTIF(rrd_id IS NULL OR rrd_id="") FROM ' + v + ') AS empty_rrd,\n' +
    '  (SELECT COUNTIF(rr_dt IS NULL OR rr_dt="") FROM ' + v + ') AS empty_rr_dt,\n' +
    '  (SELECT COUNTIF(_rr_date IS NULL) FROM ' + v + ') AS null_part_date,\n' +
    '  (SELECT COUNTIF(internal_sku IS NULL OR internal_sku="") FROM ' + v + ') AS unmatched_sku,\n' +
    '  (SELECT COUNTIF(money_parse_status IS NOT NULL AND money_parse_status!="" AND money_parse_status!="OK") FROM ' + v + ') AS money_parse_bad,\n' +
    '  (SELECT MIN(_rr_date) FROM ' + v + ') AS min_date,\n' +
    '  (SELECT MAX(_rr_date) FROM ' + v + ') AS max_date';
  var res = bqQuery_(sql);
  wbFinPrintRows_(res, ['raw_rows', 'view_rows', 'uniq_nm', 'empty_rrd', 'empty_rr_dt',
    'null_part_date', 'unmatched_sku', 'money_parse_bad', 'min_date', 'max_date']);
  console.log('Ожидаем: raw_rows ≥ view_rows (разница = схлопнутые дубли); empty_rrd=0; money_parse_bad=0.');
}


/**
 * МОДЕЛЬ КОМИССИИ по месяцам — только продажи (doc_type_name='Продажа').
 * comm_gross = retail − for_pay (с эквайрингом);
 * comm_pure  = retail − for_pay − acquiring (чистая, без эквайринга).
 * Показывает, как менялась комиссия во времени (НЕ фиксируем константу).
 */
function wbFinAuditCommissionByMonth() {
  var v = wbFinViewFq_();
  var retail = wbNum_('retail_amount');
  var forpay = wbNum_('for_pay');
  var acq = wbNum_('acquiring_fee');
  var sql =
    'SELECT period_month,\n' +
    '  COUNT(*) AS sale_rows,\n' +
    '  ' + wbNum_('quantity') + ' AS qty,\n' +
    '  ' + retail + ' AS retail,\n' +
    '  ' + forpay + ' AS for_pay,\n' +
    '  ' + acq + ' AS acquiring,\n' +
    '  ROUND(' + retail + ' - ' + forpay + ', 2) AS comm_gross,\n' +
    '  ROUND(SAFE_DIVIDE(' + retail + ' - ' + forpay + ', ' + retail + ') * 100, 1) AS comm_gross_pct,\n' +
    '  ROUND(' + retail + ' - ' + forpay + ' - ' + acq + ', 2) AS comm_pure,\n' +
    '  ROUND(SAFE_DIVIDE(' + retail + ' - ' + forpay + ' - ' + acq + ', ' + retail + ') * 100, 1) AS comm_pure_pct\n' +
    'FROM ' + v + '\n' +
    'WHERE doc_type_name = "Продажа"\n' +
    'GROUP BY period_month ORDER BY period_month';
  var res = bqQuery_(sql);
  wbFinPrintRows_(res, ['month', 'sale_rows', 'qty', 'retail', 'for_pay', 'acquiring',
    'comm_gross', 'gross%', 'comm_pure', 'pure%']);
  console.log('Комиссия менялась по периодам — сверяем pure% с вашим ручным расчётом.');
}


/** Раскладка по месяцам (все операции): выручка, перечисление, расходы по статьям. */
function wbFinAuditByMonth() {
  var v = wbFinViewFq_();
  var sql =
    'SELECT period_month,\n' +
    '  COUNT(*) AS rows,\n' +
    '  ' + wbNum_('retail_amount') + ' AS retail,\n' +
    '  ' + wbNum_('for_pay') + ' AS for_pay,\n' +
    '  ' + wbNum_('acquiring_fee') + ' AS acquiring,\n' +
    '  ' + wbNum_('logistics_amount') + ' AS logistics,\n' +
    '  ' + wbNum_('storage_fee') + ' AS storage,\n' +
    '  ' + wbNum_('penalty') + ' AS penalty,\n' +
    '  ' + wbNum_('deduction') + ' AS deduction,\n' +
    '  ' + wbNum_('additional_payment') + ' AS add_payment,\n' +
    '  ' + wbNum_('rebill_logistics') + ' AS rebill_log\n' +
    'FROM ' + v + '\n' +
    'GROUP BY period_month ORDER BY period_month';
  var res = bqQuery_(sql);
  wbFinPrintRows_(res, ['month', 'rows', 'retail', 'for_pay',
    'acquiring', 'logistics', 'storage', 'penalty', 'deduction', 'add_pay', 'rebill_log']);
}


/** По типам операций WB: где какие деньги (продажа/возврат/логистика/хранение/штраф…). */
function wbFinAuditByOper() {
  var v = wbFinViewFq_();
  var sql =
    'SELECT supplier_oper_name,\n' +
    '  COUNT(*) AS rows,\n' +
    '  ' + wbNum_('retail_amount') + ' AS retail,\n' +
    '  ' + wbNum_('for_pay') + ' AS for_pay,\n' +
    '  ' + wbNum_('logistics_amount') + ' AS logistics,\n' +
    '  ' + wbNum_('storage_fee') + ' AS storage,\n' +
    '  ' + wbNum_('penalty') + ' AS penalty,\n' +
    '  ' + wbNum_('deduction') + ' AS deduction,\n' +
    '  ' + wbNum_('acquiring_fee') + ' AS acquiring\n' +
    'FROM ' + v + '\n' +
    'GROUP BY supplier_oper_name ORDER BY rows DESC';
  var res = bqQuery_(sql);
  wbFinPrintRows_(res, ['oper', 'rows', 'retail', 'for_pay', 'logistics', 'storage', 'penalty', 'deduction', 'acquiring']);
}


/** По SKU: количество, выручка, к перечислению, комиссия(gross) и расходы (заготовка маржи). */
function wbFinAuditBySku() {
  var v = wbFinViewFq_();
  var sql =
    'SELECT IFNULL(internal_sku, "(не сматчен)") AS sku, wb_nm_id,\n' +
    '  ' + wbNum_('quantity') + ' AS qty,\n' +
    '  ' + wbNum_('retail_amount') + ' AS retail,\n' +
    '  ' + wbNum_('for_pay') + ' AS for_pay,\n' +
    '  ROUND(' + wbNum_('retail_amount') + ' - ' + wbNum_('for_pay') + ', 2) AS commission_gross,\n' +
    '  ' + wbNum_('logistics_amount') + ' AS logistics,\n' +
    '  ' + wbNum_('storage_fee') + ' AS storage,\n' +
    '  ' + wbNum_('penalty') + ' AS penalty\n' +
    'FROM ' + v + '\n' +
    'GROUP BY sku, wb_nm_id ORDER BY for_pay DESC';
  var res = bqQuery_(sql);
  wbFinPrintRows_(res, ['sku', 'nm_id', 'qty', 'retail', 'for_pay', 'commission_gross', 'logistics', 'storage', 'penalty']);
}
