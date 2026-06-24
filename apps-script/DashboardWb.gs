/**
 * ══════════════════════════════════════════════════════════════
 * EVETIS WB — DashboardWb.gs
 *
 * Управленческий дашборд DASHBOARD_WB ТОЛЬКО по API-данным.
 *
 * Источники (RAW, только API-строки):
 *   • RAW_WB_ORDERS            (source_api == 'WB_API_ORDERS')   — заказы/отмены
 *   • RAW_WB_SALES_RETURNS     (source_api == 'WB_API_SALES')    — выкупы/возвраты
 *   • RAW_WB_ADV_CAMPAIGN_STATS (fullstats, дедуп date+advertId+nmId) — расход рекламы
 *   • SKU_MASTER                                                 — название, target ДРР, cogs
 *
 * НЕ использует: CLEAN_WB_DAILY, Excel-реализации, UNIT_SKU_DAILY.
 * НЕ трогает: RAW-листы, другие листы, меню.
 *
 * Идемпотентно: чистит DASHBOARD_WB и перестраивает с нуля.
 * Период переключается выпадающим списком в шапке (формулы пересчитываются
 * сами, повторный запуск не нужен). Расход рекламы дедуплицируется на лету
 * по ключу date+advertId+nmId, поэтому перекрытие июньских прогонов
 * (period_from 01.06 / 14.06 / 16.06) не задваивает суммы.
 * ══════════════════════════════════════════════════════════════
 */

var DASH_SHEET_       = 'DASHBOARD_WB';
var DASH_TZ_          = 'Europe/Moscow';

var DASH_SRC_ORDERS_  = 'RAW_WB_ORDERS';
var DASH_SRC_SALES_   = 'RAW_WB_SALES_RETURNS';
var DASH_SRC_ADS_     = 'RAW_WB_ADV_CAMPAIGN_STATS';
var DASH_SRC_SKU_     = 'SKU_MASTER';

/** Точка входа: построить дашборд. */
function buildDashboardWb() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sku = dashReadSkuMaster_(ss);
  var spine = dashBuildSpine_(ss, sku);   // массив строк {dateKey, date, sku, name, ...}
  dashRender_(ss, spine, sku);
  try { ss.toast('Дашборд построен: ' + spine.rows.length + ' строк день×SKU', '📊 DASHBOARD_WB', 8); } catch (e) {}
}

/** Меню (по желанию подключить в onOpen). */
function addDashboardWbMenu() {
  SpreadsheetApp.getUi()
    .createMenu('📊 Дашборд WB')
    .addItem('🔄 Построить/обновить DASHBOARD_WB', 'buildDashboardWb')
    .addToUi();
}

// ───────────────────────────────────────────────────────────────
// ЧТЕНИЕ ИСТОЧНИКОВ
// ───────────────────────────────────────────────────────────────

/** Карта SKU_MASTER: по internal_sku и по nmId. */
function dashReadSkuMaster_(ss) {
  var sh = ss.getSheetByName(DASH_SRC_SKU_);
  var map = { bySku: {}, byNm: {}, order: [] };
  if (!sh || sh.getLastRow() < 2) return map;
  var vals = sh.getRange(1, 1, sh.getLastRow(), sh.getLastColumn()).getValues();
  var h = dashHeaderIndex_(vals[0]);
  var iSku = dashPick_(h, ['internal_sku']);
  var iName = dashPick_(h, ['product_name_short', 'product_name', 'product_name_full']);
  var iNm  = dashPick_(h, ['wb_nm_id', 'nmId', 'nm_id']);
  var iCogs = dashPick_(h, ['current_cogs', 'cogs']);
  var iTgt = dashPick_(h, ['target_drr_percent']);
  var iCrit = dashPick_(h, ['critical_drr_percent']);
  for (var r = 1; r < vals.length; r++) {
    var row = vals[r];
    var skuId = iSku >= 0 ? String(row[iSku] || '').trim() : '';
    if (!skuId) continue;
    var rec = {
      sku: skuId,
      name: iName >= 0 ? String(row[iName] || '').trim() : '',
      nm: iNm >= 0 ? String(row[iNm] || '').trim() : '',
      cogs: iCogs >= 0 ? dashNum_(row[iCogs]) : 0,
      targetDrr: iTgt >= 0 ? dashNum_(row[iTgt]) : 0,
      critDrr: iCrit >= 0 ? dashNum_(row[iCrit]) : 0
    };
    map.bySku[skuId] = rec;
    if (rec.nm) map.byNm[rec.nm] = rec;
    map.order.push(skuId);
  }
  return map;
}

/**
 * Собирает массив день×SKU из трёх RAW-источников.
 * @return {{rows:Array, minDate:Date, maxDate:Date, skus:Array}}
 */
function dashBuildSpine_(ss, sku) {
  var acc = {};   // key 'yyyy-MM-dd|sku' -> агрегат
  var skuName = {};

  function bump(dayKey, skuId, name) {
    var k = dayKey + '|' + skuId;
    if (!acc[k]) {
      acc[k] = {
        dateKey: dayKey, sku: skuId,
        ordersQty: 0, ordersRub: 0, cancelQty: 0, cancelRub: 0,
        salesQty: 0, salesRub: 0, returnsQty: 0, returnsRub: 0, adsSpend: 0
      };
    }
    if (name && !skuName[skuId]) skuName[skuId] = name;
    return acc[k];
  }

  // — Заказы —
  var sho = ss.getSheetByName(DASH_SRC_ORDERS_);
  if (sho && sho.getLastRow() > 1) {
    var ov = sho.getRange(1, 1, sho.getLastRow(), sho.getLastColumn()).getValues();
    var oh = dashHeaderIndex_(ov[0]);
    var oSrc = dashPick_(oh, ['source_api']);
    var oDt = dashPick_(oh, ['order_dt']);
    var oSku = dashPick_(oh, ['internal_sku']);
    var oNm = dashPick_(oh, ['wb_nm_id', 'nmId']);
    var oPrice = dashPick_(oh, ['price_with_disc']);
    var oQty = dashPick_(oh, ['quantity']);
    var oCanc = dashPick_(oh, ['is_cancel']);
    var oDup = dashPick_(oh, ['is_duplicate']);
    for (var r = 1; r < ov.length; r++) {
      var row = ov[r];
      if (oSrc >= 0 && String(row[oSrc]) !== 'WB_API_ORDERS') continue;
      if (oDup >= 0 && dashBool_(row[oDup])) continue;
      var dk = dashDayKey_(row[oDt]); if (!dk) continue;
      var skuId = dashResolveSku_(row, oSku, oNm, sku);
      var price = dashNum_(row[oPrice]);
      var qty = oQty >= 0 ? dashNum_(row[oQty]) : 1; if (!qty) qty = 1;
      var a = bump(dk, skuId.sku, skuId.name);
      var canceled = oCanc >= 0 && dashBool_(row[oCanc]);
      if (canceled) { a.cancelQty += qty; a.cancelRub += price; }
      a.ordersQty += qty; a.ordersRub += price;
    }
  }

  // — Продажи/возвраты —
  var shs = ss.getSheetByName(DASH_SRC_SALES_);
  if (shs && shs.getLastRow() > 1) {
    var sv = shs.getRange(1, 1, shs.getLastRow(), shs.getLastColumn()).getValues();
    var sh = dashHeaderIndex_(sv[0]);
    var sSrc = dashPick_(sh, ['source_api']);
    var sDt = dashPick_(sh, ['sale_dt']);
    var sSku = dashPick_(sh, ['internal_sku']);
    var sNm = dashPick_(sh, ['wb_nm_id', 'nmId']);
    var sFin = dashPick_(sh, ['finished_price']);
    var sQty = dashPick_(sh, ['quantity']);
    var sRet = dashPick_(sh, ['is_return']);
    var sDup = dashPick_(sh, ['is_duplicate']);
    for (var r2 = 1; r2 < sv.length; r2++) {
      var row2 = sv[r2];
      if (sSrc >= 0 && String(row2[sSrc]) !== 'WB_API_SALES') continue;
      if (sDup >= 0 && dashBool_(row2[sDup])) continue;
      var dk2 = dashDayKey_(row2[sDt]); if (!dk2) continue;
      var s2 = dashResolveSku_(row2, sSku, sNm, sku);
      var fin = dashNum_(row2[sFin]);
      var qty2 = sQty >= 0 ? dashNum_(row2[sQty]) : 1; if (!qty2) qty2 = 1;
      var a2 = bump(dk2, s2.sku, s2.name);
      var isRet = sRet >= 0 && dashBool_(row2[sRet]);
      if (isRet) { a2.returnsQty += qty2; a2.returnsRub += fin; }
      else { a2.salesQty += qty2; a2.salesRub += fin; }
    }
  }

  // — Реклама (дедуп date+advertId+nmId, last row wins) —
  // В RAW могут лежать старые 7-дневные fullstats и свежий месячный fullstats.
  // Поэтому при дубле берём ПОСЛЕДНЮЮ строку в листе: новые прогоны append-ятся ниже.
  var sha = ss.getSheetByName(DASH_SRC_ADS_);
  if (sha && sha.getLastRow() > 1) {
    var av = sha.getRange(1, 1, sha.getLastRow(), sha.getLastColumn()).getValues();
    var ah = dashHeaderIndex_(av[0]);
    var aDate = dashPick_(ah, ['date']);
    var aAdv = dashPick_(ah, ['advertId']);
    var aNm = dashPick_(ah, ['nmId', 'nm_id']);
    var aSum = dashPick_(ah, ['sum']);
    var aLvl = dashPick_(ah, ['source_level']);

    if (aDate >= 0 && aNm >= 0 && aSum >= 0) {
      var adsDedup = {};   // key -> {dateKey, nm, spend}; перезапись = last row wins
      for (var r3 = 1; r3 < av.length; r3++) {
        var row3 = av[r3];
        if (aLvl >= 0 && String(row3[aLvl]) !== 'nm') continue;  // только nm-уровень
        var dk3 = dashDayKey_(row3[aDate]); if (!dk3) continue;
        var nm = String(row3[aNm] || '').trim(); if (!nm) continue;
        var adv = aAdv >= 0 ? String(row3[aAdv] || '').trim() : '';
        var dedupKey = dk3 + '|' + adv + '|' + nm;
        adsDedup[dedupKey] = { dateKey: dk3, nm: nm, spend: dashNum_(row3[aSum]) };
      }

      for (var adKey in adsDedup) {
        if (!adsDedup.hasOwnProperty(adKey)) continue;
        var ad = adsDedup[adKey];
        if (!ad.spend) continue;
        var rec = sku.byNm[ad.nm];
        var skuId3 = rec ? rec.sku : ('nm:' + ad.nm);
        var name3 = rec ? rec.name : ('nmId ' + ad.nm);
        var a3 = bump(ad.dateKey, skuId3, name3);
        a3.adsSpend += ad.spend;
      }
    }
  }

  // — В массив + границы дат + список SKU —
  var rows = [], minD = null, maxD = null, skuSet = {};
  for (var k in acc) {
    if (!acc.hasOwnProperty(k)) continue;
    var a = acc[k];
    var d = dashParseKey_(a.dateKey);
    a.date = d;
    a.name = skuName[a.sku] || (sku.bySku[a.sku] ? sku.bySku[a.sku].name : a.sku);
    rows.push(a);
    if (!minD || d < minD) minD = d;
    if (!maxD || d > maxD) maxD = d;
    skuSet[a.sku] = a.name;
  }
  rows.sort(function (x, y) { return x.date - y.date || (x.sku < y.sku ? -1 : 1); });

  var skus = [];
  for (var s in skuSet) if (skuSet.hasOwnProperty(s)) skus.push({ sku: s, name: skuSet[s] });
  skus.sort(function (x, y) { return (x.name < y.name ? -1 : 1); });

  return { rows: rows, minDate: minD, maxDate: maxD, skus: skus };
}

// ───────────────────────────────────────────────────────────────
// РЕНДЕР
// ───────────────────────────────────────────────────────────────

function dashRender_(ss, spine, sku) {
  var sh = ss.getSheetByName(DASH_SHEET_);
  if (!sh) sh = ss.insertSheet(DASH_SHEET_);
  sh.clear();
  sh.clearConditionalFormatRules();
  try { sh.getDataRange().clearDataValidations(); } catch (e) {}

  // — Служебный массив день×SKU (далеко справа, скрытый) —
  var SPINE_COL = 30;   // AD
  var sHead = ['_date', '_sku', '_name', '_orders_qty', '_orders_rub', '_cancel_qty',
               '_sales_qty', '_sales_rub', '_returns_qty', '_ads_spend'];
  var sData = [sHead];
  for (var i = 0; i < spine.rows.length; i++) {
    var a = spine.rows[i];
    sData.push([a.date, a.sku, a.name, a.ordersQty, a.ordersRub, a.cancelQty,
                a.salesQty, a.salesRub, a.returnsQty, a.adsSpend]);
  }
  sh.getRange(1, SPINE_COL, sData.length, sHead.length).setValues(sData);
  var spineLastRow = sData.length;          // включая шапку
  var L = dashColLetter_;                    // helper буква колонки
  var cDate = L(SPINE_COL), cSku = L(SPINE_COL + 1);
  var cOQ = L(SPINE_COL + 3), cOR = L(SPINE_COL + 4), cCQ = L(SPINE_COL + 5),
      cSQ = L(SPINE_COL + 6), cSR = L(SPINE_COL + 7), cRQ = L(SPINE_COL + 8), cAS = L(SPINE_COL + 9);
  var rng = '$' + cDate + '$2:$' + cDate + '$' + spineLastRow;          // даты
  var rngSku = '$' + cSku + '$2:$' + cSku + '$' + spineLastRow;          // sku
  function fullCol(c) { return '$' + c + '$2:$' + c + '$' + spineLastRow; }

  // — Шапка и фильтр периода —
  sh.getRange('A1').setValue('EVETIS — Дашборд WB (по данным API)').setFontSize(14).setFontWeight('bold');
  sh.getRange('A2').setValue('Период:').setFontWeight('bold');
  var periods = ['Вчера', '7 дней', '14 дней', 'Текущий месяц', 'Весь период'];
  var dv = SpreadsheetApp.newDataValidation().requireValueInList(periods, true).setAllowInvalid(false).build();
  sh.getRange('B2').setDataValidation(dv).setValue('Весь период').setBackground('#FFF7CC').setFontWeight('bold');

  // helper-ячейки границ периода (вычисляются от фильтра и дат массива)
  var minRef = "MIN(" + rng + ")", maxRef = "MAX(" + rng + ")";
  var fromF = '=IFS(' +
    '$B$2="Вчера",TODAY()-1,' +
    '$B$2="7 дней",TODAY()-7,' +
    '$B$2="14 дней",TODAY()-14,' +
    '$B$2="Текущий месяц",DATE(YEAR(TODAY()),MONTH(TODAY()),1),' +
    '$B$2="Весь период",' + minRef + ')';
  var toF = '=IF($B$2="Весь период",' + maxRef + ',IF($B$2="Текущий месяц",TODAY(),TODAY()-1))';
  sh.getRange('D2').setValue('с:').setFontWeight('bold');
  sh.getRange('E2').setFormula(fromF).setNumberFormat('yyyy-mm-dd');
  sh.getRange('F2').setValue('по:').setFontWeight('bold');
  sh.getRange('G2').setFormula(toF).setNumberFormat('yyyy-mm-dd');
  sh.getRange('I2').setValue('обновлено: ' + Utilities.formatDate(new Date(), DASH_TZ_, 'yyyy-MM-dd HH:mm'));

  var from = '$E$2', to = '$G$2';
  function sif(col) { return 'SUMIFS(' + fullCol(col) + ',' + rng + ',">="&' + from + ',' + rng + ',"<="&' + to + ')'; }
  function sifSku(col, skuCellAbsCol, rowNum) {
    return 'SUMIFS(' + fullCol(col) + ',' + rngSku + ',$' + skuCellAbsCol + rowNum + ',' +
           rng + ',">="&' + from + ',' + rng + ',"<="&' + to + ')';
  }

  // — Блок «Итого по магазину за период» —
  sh.getRange('A4').setValue('ИТОГО ПО МАГАЗИНУ ЗА ПЕРИОД').setFontWeight('bold').setBackground('#1E40AF').setFontColor('#FFFFFF');
  var kpiHead = ['Заказы, шт', 'Заказы, ₽', 'Отмены, шт', 'Выкупы, шт', 'Выкупы, ₽', 'Реклама, ₽', 'ДРР к заказам, %', 'Средний чек, ₽'];
  sh.getRange(5, 1, 1, kpiHead.length).setValues([kpiHead]).setFontWeight('bold').setBackground('#DBEAFE');
  var kpi = [
    '=' + sif(cOQ),
    '=' + sif(cOR),
    '=' + sif(cCQ),
    '=' + sif(cSQ),
    '=' + sif(cSR),
    '=' + sif(cAS),
    '=IF(' + sif(cOR) + '=0,0,' + sif(cAS) + '/' + sif(cOR) + '*100)',
    '=IF(' + sif(cOQ) + '=0,0,' + sif(cOR) + '/' + sif(cOQ) + ')'
  ];
  sh.getRange(6, 1, 1, kpi.length).setFormulas([kpi]);
  sh.getRange(6, 1).setNumberFormat('#,##0');
  sh.getRange(6, 2).setNumberFormat('#,##0 ₽');
  sh.getRange(6, 3).setNumberFormat('#,##0');
  sh.getRange(6, 4).setNumberFormat('#,##0');
  sh.getRange(6, 5).setNumberFormat('#,##0 ₽');
  sh.getRange(6, 6).setNumberFormat('#,##0 ₽');
  sh.getRange(6, 7).setNumberFormat('0.0"%"');
  sh.getRange(6, 8).setNumberFormat('#,##0 ₽');

  // — Таблица «по SKU за период» —
  var skuTop = 8;
  sh.getRange(skuTop, 1).setValue('ПО SKU ЗА ПЕРИОД').setFontWeight('bold').setBackground('#1E40AF').setFontColor('#FFFFFF');
  var skuHead = ['Товар', 'SKU', 'Заказы, шт', 'Заказы, ₽', 'Отмены, шт', 'Выкупы, шт', 'Выкупы, ₽', 'Реклама, ₽', 'ДРР, %', 'Цель ДРР, %', 'Флаг'];
  var hr = skuTop + 1;
  sh.getRange(hr, 1, 1, skuHead.length).setValues([skuHead]).setFontWeight('bold').setBackground('#DBEAFE');

  var skuRows = spine.skus;
  for (var j = 0; j < skuRows.length; j++) {
    var rr = hr + 1 + j;
    var skuCell = 'B' + rr;   // SKU id в колонке B
    sh.getRange(rr, 1).setValue(skuRows[j].name);
    sh.getRange(rr, 2).setValue(skuRows[j].sku);
    var oq = sifSku(cOQ, 'B', rr), or = sifSku(cOR, 'B', rr), cq = sifSku(cCQ, 'B', rr),
        sq = sifSku(cSQ, 'B', rr), sr = sifSku(cSR, 'B', rr), as = sifSku(cAS, 'B', rr);
    var tgt = (sku.bySku[skuRows[j].sku] ? sku.bySku[skuRows[j].sku].targetDrr : 0) || 0;
    var f = [
      '=' + oq, '=' + or, '=' + cq, '=' + sq, '=' + sr, '=' + as,
      '=IF(' + or + '=0,0,' + as + '/' + or + '*100)',
      tgt,
      '=IF(I' + rr + '=0,"—",IF(I' + rr + '>J' + rr + ',"🔴 резать",IF(I' + rr + '<J' + rr + '*0.6,"🟢 усиливать","🟡 ок")))'
    ];
    sh.getRange(rr, 3, 1, f.length).setFormulas([f]);
    sh.getRange(rr, 3).setNumberFormat('#,##0');
    sh.getRange(rr, 4).setNumberFormat('#,##0 ₽');
    sh.getRange(rr, 5).setNumberFormat('#,##0');
    sh.getRange(rr, 6).setNumberFormat('#,##0');
    sh.getRange(rr, 7).setNumberFormat('#,##0 ₽');
    sh.getRange(rr, 8).setNumberFormat('#,##0 ₽');
    sh.getRange(rr, 9).setNumberFormat('0.0"%"');
    sh.getRange(rr, 10).setNumberFormat('0"%"');
  }

  // — Таблица «по дням» (вся история, статично) —
  var dayTop = hr + 1 + skuRows.length + 2;
  sh.getRange(dayTop, 1).setValue('ПО ДНЯМ (вся история)').setFontWeight('bold').setBackground('#1E40AF').setFontColor('#FFFFFF');
  var dayHead = ['Дата', 'Заказы, шт', 'Заказы, ₽', 'Отмены, шт', 'Выкупы, шт', 'Выкупы, ₽', 'Реклама, ₽', 'ДРР, %'];
  var dhr = dayTop + 1;
  sh.getRange(dhr, 1, 1, dayHead.length).setValues([dayHead]).setFontWeight('bold').setBackground('#DBEAFE');

  // агрегируем массив по дню
  var byDay = {};
  for (var t = 0; t < spine.rows.length; t++) {
    var a = spine.rows[t]; var dk = a.dateKey;
    if (!byDay[dk]) byDay[dk] = { date: a.date, oq: 0, or: 0, cq: 0, sq: 0, sr: 0, as: 0 };
    var b = byDay[dk];
    b.oq += a.ordersQty; b.or += a.ordersRub; b.cq += a.cancelQty;
    b.sq += a.salesQty; b.sr += a.salesRub; b.as += a.adsSpend;
  }
  var days = [];
  for (var dk2 in byDay) if (byDay.hasOwnProperty(dk2)) days.push(byDay[dk2]);
  days.sort(function (x, y) { return x.date - y.date; });
  var dayData = [];
  for (var dd = 0; dd < days.length; dd++) {
    var b2 = days[dd];
    var drr = b2.or === 0 ? 0 : b2.as / b2.or * 100;
    dayData.push([b2.date, b2.oq, b2.or, b2.cq, b2.sq, b2.sr, b2.as, drr]);
  }
  if (dayData.length) {
    var dr0 = dhr + 1;
    sh.getRange(dr0, 1, dayData.length, dayHead.length).setValues(dayData);
    sh.getRange(dr0, 1, dayData.length, 1).setNumberFormat('yyyy-mm-dd');
    sh.getRange(dr0, 2, dayData.length, 1).setNumberFormat('#,##0');
    sh.getRange(dr0, 3, dayData.length, 1).setNumberFormat('#,##0 ₽');
    sh.getRange(dr0, 4, dayData.length, 1).setNumberFormat('#,##0');
    sh.getRange(dr0, 5, dayData.length, 1).setNumberFormat('#,##0');
    sh.getRange(dr0, 6, dayData.length, 1).setNumberFormat('#,##0 ₽');
    sh.getRange(dr0, 7, dayData.length, 1).setNumberFormat('#,##0 ₽');
    sh.getRange(dr0, 8, dayData.length, 1).setNumberFormat('0.0"%"');
  }

  // — Косметика —
  sh.setColumnWidth(1, 240);
  sh.setColumnWidths(2, 10, 110);
  sh.setFrozenRows(2);
  for (var hideC = SPINE_COL; hideC < SPINE_COL + sHead.length; hideC++) sh.hideColumns(hideC);
  sh.getRange('A2:I2').setBackground('#F3F4F6');
}

// ───────────────────────────────────────────────────────────────
// ХЕЛПЕРЫ
// ───────────────────────────────────────────────────────────────

function dashHeaderIndex_(headerRow) {
  var h = {};
  for (var c = 0; c < headerRow.length; c++) h[String(headerRow[c]).trim()] = c;
  return h;
}
function dashPick_(h, names) {
  for (var i = 0; i < names.length; i++) if (h[names[i]] !== undefined) return h[names[i]];
  return -1;
}
function dashResolveSku_(row, iSku, iNm, sku) {
  var skuId = iSku >= 0 ? String(row[iSku] || '').trim() : '';
  if (skuId) {
    var rec = sku.bySku[skuId];
    return { sku: skuId, name: rec ? rec.name : skuId };
  }
  var nm = iNm >= 0 ? String(row[iNm] || '').trim() : '';
  var r2 = nm ? sku.byNm[nm] : null;
  if (r2) return { sku: r2.sku, name: r2.name };
  return { sku: nm ? ('nm:' + nm) : 'НЕИЗВЕСТНО', name: nm ? ('nmId ' + nm) : 'НЕИЗВЕСТНО' };
}
function dashNum_(v) {
  if (v === '' || v === null || v === undefined) return 0;
  if (typeof v === 'number') return v;
  var s = String(v).replace(/\s/g, '').replace(',', '.');
  var n = parseFloat(s);
  return isNaN(n) ? 0 : n;
}
function dashBool_(v) {
  if (v === true) return true;
  var s = String(v).trim().toLowerCase();
  return s === 'true' || s === '1' || s === 'да';
}
/** 'yyyy-MM-dd' из Date или строки даты/даты-времени. */
function dashDayKey_(v) {
  if (v instanceof Date) return Utilities.formatDate(v, DASH_TZ_, 'yyyy-MM-dd');
  var s = String(v || '').trim();
  if (!s) return '';
  var m = s.match(/^(\d{4})-(\d{1,2})-(\d{1,2})/);
  if (!m) return '';
  return m[1] + '-' + ('0' + m[2]).slice(-2) + '-' + ('0' + m[3]).slice(-2);
}
/** Date из ключа 'yyyy-MM-dd' (полдень, чтобы избежать сдвига TZ). */
function dashParseKey_(k) {
  var p = k.split('-');
  return new Date(Number(p[0]), Number(p[1]) - 1, Number(p[2]), 12, 0, 0);
}
/** Буква колонки по номеру (1→A). */
function dashColLetter_(n) {
  var s = '';
  while (n > 0) { var m = (n - 1) % 26; s = String.fromCharCode(65 + m) + s; n = Math.floor((n - 1) / 26); }
  return s;
}
