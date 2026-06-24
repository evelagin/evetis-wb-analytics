/**
 * ══════════════════════════════════════════════════════════════
 * EVETIS WB — AdsDashboardWb.gs
 *
 * Рекламный дашборд ADS_WB по данным API (fullstats).
 * Выбор периода → итог по кабинету, разрез по каждому SKU и по каждой
 * рекламной кампании (с площадкой и статусом). Главный рычаг прибыли —
 * наблюдать ДРР/CPO по артикулам и кампаниям, гнать ДРР вниз.
 *
 * Источники (RAW, только API):
 *   • RAW_WB_ADV_CAMPAIGN_STATS — метрики (дедуп date+advertId+nmId+appType,
 *       last-row-wins; appType суммируется, ретрай-дубли убираются);
 *   • RAW_WB_ADV_CAMPAIGNS      — название/статус/площадки кампаний (raw_json);
 *   • SKU_MASTER                — название, целевой ДРР.
 *
 * ЗАВИСИТ от DashboardWb.gs (хелперы dash*). НЕ трогает RAW и другие листы.
 * Идемпотентно: чистит ADS_WB и перестраивает. Период — формулы (live).
 * ══════════════════════════════════════════════════════════════
 */

var ADS_SHEET_        = 'ADS_WB';
var ADS_TZ_           = 'Europe/Moscow';
var ADS_SRC_STATS_    = 'RAW_WB_ADV_CAMPAIGN_STATS';
var ADS_SRC_CAMPS_    = 'RAW_WB_ADV_CAMPAIGNS';
var ADS_SRC_SKU_      = 'SKU_MASTER';

/** Точка входа. */
function buildAdsDashboardWb() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sku = dashReadSkuMaster_(ss);
  var camps = adsReadCampaigns_(ss);
  var data = adsBuildSpine_(ss, sku, camps);
  adsRender_(ss, data, sku);
  try { ss.toast('Рекламный дашборд построен: ' + data.spine.length + ' строк, ' +
                 data.campaigns.length + ' кампаний', '📣 ADS_WB', 8); } catch (e) {}
}

/** Меню (по желанию подключить в onOpen). */
function addAdsDashboardWbMenu() {
  SpreadsheetApp.getUi()
    .createMenu('📣 Реклама — дашборд')
    .addItem('🔄 Построить/обновить ADS_WB', 'buildAdsDashboardWb')
    .addToUi();
}

// ───────────────────────────────────────────────────────────────
// Чтение справочника кампаний (advertId → название/статус/площадка)
// ───────────────────────────────────────────────────────────────
function adsReadCampaigns_(ss) {
  var map = {};
  var sh = ss.getSheetByName(ADS_SRC_CAMPS_);
  if (!sh || sh.getLastRow() < 2) return map;
  var vals = sh.getRange(1, 1, sh.getLastRow(), sh.getLastColumn()).getValues();
  var h = dashHeaderIndex_(vals[0]);
  var iAdv = dashPick_(h, ['advertId']);
  var iStatus = dashPick_(h, ['status']);
  var iName = dashPick_(h, ['campName', 'name']);
  var iJson = dashPick_(h, ['raw_json']);
  for (var r = 1; r < vals.length; r++) {
    var row = vals[r];
    var adv = iAdv >= 0 ? String(row[iAdv] || '').trim() : '';
    if (!adv) continue;
    var name = iName >= 0 ? String(row[iName] || '').trim() : '';
    var status = iStatus >= 0 ? String(row[iStatus] || '').trim() : '';
    var placements = '';
    if ((!name || true) && iJson >= 0) {
      try {
        var j = JSON.parse(row[iJson]);
        if (j && j.settings) {
          if (!name && j.settings.name) name = String(j.settings.name).trim();
          var pl = j.settings.placements || {};
          var arr = [];
          if (pl.search) arr.push('поиск');
          if (pl.recommendations) arr.push('рекомендации');
          if (pl.booster) arr.push('бустер');
          placements = arr.join('+');
        }
      } catch (e) {}
    }
    map[adv] = { name: name || ('Кампания ' + adv), status: status, placement: adsPlacement_(name, placements) };
  }
  return map;
}

/** Площадка кампании по названию/настройкам. */
function adsPlacement_(name, placements) {
  var n = String(name || '').toLowerCase();
  if (n.indexOf('поиск') !== -1) return 'Поиск';
  if (n.indexOf('полки') !== -1) return 'Полки';
  if (n.indexOf('рекоменд') !== -1) return 'Рекомендации';
  if (n.indexOf('клик') !== -1) return 'Оплата за клик';
  if (placements) return placements;
  return '—';
}

/** Текст статуса кампании по коду WB. */
function adsStatusText_(code) {
  var m = { '4': 'готова', '7': '⏹ завершена', '8': 'отказана', '9': '🟢 идёт', '11': '⏸ пауза' };
  return m[String(code)] || String(code || '—');
}

// ───────────────────────────────────────────────────────────────
// Сбор спайна: (date, sku, advertId) → метрики
// ───────────────────────────────────────────────────────────────
function adsBuildSpine_(ss, sku, camps) {
  var sh = ss.getSheetByName(ADS_SRC_STATS_);
  var acc = {};          // 'dateKey|sku|advert' -> агрегат
  var skuName = {};
  var minD = null, maxD = null;
  var campSeen = {};     // advert -> {sku, ...} для списка кампаний

  if (sh && sh.getLastRow() > 1) {
    var av = sh.getRange(1, 1, sh.getLastRow(), sh.getLastColumn()).getValues();
    var h = dashHeaderIndex_(av[0]);
    var iAdv = dashPick_(h, ['advertId']);
    var iDate = dashPick_(h, ['date']);
    var iType = dashPick_(h, ['appType']);
    var iNm = dashPick_(h, ['nmId', 'nm_id']);
    var iViews = dashPick_(h, ['views']);
    var iClicks = dashPick_(h, ['clicks']);
    var iAtbs = dashPick_(h, ['atbs']);
    var iOrders = dashPick_(h, ['orders']);
    var iSum = dashPick_(h, ['sum']);
    var iSumP = dashPick_(h, ['sum_price']);
    var iLvl = dashPick_(h, ['source_level']);

    // 1) дедуп по date+advert+nm+appType (last row wins)
    var dedup = {};
    for (var r = 1; r < av.length; r++) {
      var row = av[r];
      if (iLvl >= 0 && String(row[iLvl]) !== 'nm') continue;
      var dk = dashDayKey_(row[iDate]); if (!dk) continue;
      var nm = String(row[iNm] || '').trim(); if (!nm) continue;
      var adv = String(row[iAdv] || '').trim();
      var appT = iType >= 0 ? String(row[iType] || '').trim() : '';
      dedup[dk + '|' + adv + '|' + nm + '|' + appT] = {
        dk: dk, nm: nm, adv: adv,
        views: dashNum_(row[iViews]), clicks: dashNum_(row[iClicks]),
        atbs: dashNum_(row[iAtbs]), orders: dashNum_(row[iOrders]),
        spend: dashNum_(row[iSum]), adrev: dashNum_(row[iSumP])
      };
    }

    // 2) агрегируем в (date, sku, advert)
    for (var k in dedup) {
      if (!dedup.hasOwnProperty(k)) continue;
      var d = dedup[k];
      var rec = sku.byNm[d.nm];
      var skuId = rec ? rec.sku : ('nm:' + d.nm);
      var nameS = rec ? rec.name : ('nmId ' + d.nm);
      skuName[skuId] = nameS;
      var key = d.dk + '|' + skuId + '|' + d.adv;
      if (!acc[key]) acc[key] = { dk: d.dk, sku: skuId, adv: d.adv,
        views: 0, clicks: 0, atbs: 0, orders: 0, spend: 0, adrev: 0 };
      var a = acc[key];
      a.views += d.views; a.clicks += d.clicks; a.atbs += d.atbs;
      a.orders += d.orders; a.spend += d.spend; a.adrev += d.adrev;
      campSeen[skuId + '|' + d.adv] = { sku: skuId, skuName: nameS, adv: d.adv };
    }
  }

  // спайн-массив + границы дат
  var spine = [];
  for (var kk in acc) {
    if (!acc.hasOwnProperty(kk)) continue;
    var a = acc[kk];
    var dt = dashParseKey_(a.dk);
    if (!minD || dt < minD) minD = dt;
    if (!maxD || dt > maxD) maxD = dt;
    spine.push([dt, a.sku, a.adv, a.views, a.clicks, a.atbs, a.orders, a.spend, a.adrev]);
  }
  spine.sort(function (x, y) { return x[0] - y[0]; });

  // список SKU (по расходу — позже сортируем формулами; здесь по имени)
  var skus = [];
  for (var s in skuName) if (skuName.hasOwnProperty(s)) skus.push({ sku: s, name: skuName[s] });
  skus.sort(function (x, y) { return (x.name < y.name ? -1 : 1); });

  // список кампаний (sku, advert) с метаданными
  var campaigns = [];
  for (var ck in campSeen) {
    if (!campSeen.hasOwnProperty(ck)) continue;
    var c = campSeen[ck];
    var meta = camps[c.adv] || { name: 'Кампания ' + c.adv, status: '', placement: '—' };
    campaigns.push({ sku: c.sku, skuName: c.skuName, adv: c.adv,
      campName: meta.name, placement: meta.placement, status: adsStatusText_(meta.status) });
  }
  campaigns.sort(function (x, y) { return (x.skuName < y.skuName ? -1 : (x.skuName > y.skuName ? 1 : (x.adv < y.adv ? -1 : 1))); });

  return { spine: spine, skus: skus, campaigns: campaigns, minDate: minD, maxDate: maxD };
}

// ───────────────────────────────────────────────────────────────
// Рендер ADS_WB
// ───────────────────────────────────────────────────────────────
function adsRender_(ss, data, sku) {
  var sh = ss.getSheetByName(ADS_SHEET_);
  if (!sh) sh = ss.insertSheet(ADS_SHEET_);
  sh.clear();
  sh.clearConditionalFormatRules();
  try { sh.getDataRange().clearDataValidations(); } catch (e) {}
  try { var ef = sh.getFilter(); if (ef) ef.remove(); } catch (e) {}

  var SEP = dashArgSep_(sh);
  var L = dashColLetter_;

  // — Спайн (скрытый, далеко справа) —
  var SC = 40; // AN
  var sHead = ['_date', '_sku', '_advert', '_views', '_clicks', '_atbs', '_orders', '_spend', '_adrev'];
  var sData = [sHead].concat(data.spine.length ? data.spine : [['', '', '', '', '', '', '', '', '']]);
  sh.getRange(1, SC, sData.length, sHead.length).setValues(sData);
  var last = sData.length;
  var cDate = L(SC), cSku = L(SC + 1), cAdv = L(SC + 2);
  var cViews = L(SC + 3), cClicks = L(SC + 4), cAtbs = L(SC + 5), cOrders = L(SC + 6), cSpend = L(SC + 7), cAdrev = L(SC + 8);
  function rng(c) { return '$' + c + '$2:$' + c + '$' + last; }
  var rDate = rng(cDate), rSku = rng(cSku), rAdv = rng(cAdv);

  // — Шапка + фильтр периода —
  sh.getRange('A1').setValue('EVETIS — Рекламный дашборд WB (по API)').setFontSize(14).setFontWeight('bold');
  sh.getRange('A2').setValue('Период:').setFontWeight('bold');
  var periods = ['Вчера', '7 дней', '14 дней', 'Текущий месяц', 'Весь период'];
  var dv = SpreadsheetApp.newDataValidation().requireValueInList(periods, true).setAllowInvalid(false).build();
  sh.getRange('B2').setDataValidation(dv).setValue('Весь период').setBackground('#FFF7CC').setFontWeight('bold');

  var minRef = 'MIN(' + rDate + ')', maxRef = 'MAX(' + rDate + ')';
  var fromF = '=IFS(' +
    '$B$2="Вчера"' + SEP + 'TODAY()-1' + SEP +
    '$B$2="7 дней"' + SEP + 'TODAY()-7' + SEP +
    '$B$2="14 дней"' + SEP + 'TODAY()-14' + SEP +
    '$B$2="Текущий месяц"' + SEP + 'DATE(YEAR(TODAY())' + SEP + 'MONTH(TODAY())' + SEP + '1)' + SEP +
    '$B$2="Весь период"' + SEP + minRef + ')';
  var toF = '=IF($B$2="Весь период"' + SEP + maxRef + SEP +
            'IF($B$2="Текущий месяц"' + SEP + 'TODAY()' + SEP + 'TODAY()-1))';
  sh.getRange('D2').setValue('с:').setFontWeight('bold');
  sh.getRange('E2').setFormula(fromF).setNumberFormat('yyyy-mm-dd');
  sh.getRange('F2').setValue('по:').setFontWeight('bold');
  sh.getRange('G2').setFormula(toF).setNumberFormat('yyyy-mm-dd');
  sh.getRange('I2').setValue('обновлено: ' + Utilities.formatDate(new Date(), ADS_TZ_, 'yyyy-MM-dd HH:mm'));

  var from = '$E$2', to = '$G$2';
  function sif(c, extra) {
    return 'SUMIFS(' + rng(c) + SEP + rDate + SEP + '">="&' + from + SEP + rDate + SEP + '"<="&' + to + (extra || '') + ')';
  }
  // KPI-формулы из базовых сумм
  function block(spendF, viewsF, clicksF, atbsF, ordersF, adrevF) {
    return {
      spend: spendF, views: viewsF, clicks: clicksF, atbs: atbsF, orders: ordersF, adrev: adrevF,
      ctr: 'IF(' + viewsF + '=0' + SEP + '0' + SEP + clicksF + '/' + viewsF + '*100)',
      cpc: 'IF(' + clicksF + '=0' + SEP + '0' + SEP + spendF + '/' + clicksF + ')',
      cr: 'IF(' + clicksF + '=0' + SEP + '0' + SEP + ordersF + '/' + clicksF + '*100)',
      cpo: 'IF(' + ordersF + '=0' + SEP + '0' + SEP + spendF + '/' + ordersF + ')',
      drr: 'IF(' + adrevF + '=0' + SEP + '0' + SEP + spendF + '/' + adrevF + '*100)'
    };
  }

  // — Блок 1: итог по кабинету —
  sh.getRange('A4').setValue('ИТОГО ПО КАБИНЕТУ ЗА ПЕРИОД').setFontWeight('bold').setBackground('#7C2D12').setFontColor('#FFFFFF');
  var kHead = ['Расход, ₽', 'Показы', 'Клики', 'CTR, %', 'CPC, ₽', 'Корзины', 'Заказы', 'CR, %', 'CPO, ₽', 'Выручка рекл., ₽', 'ДРР, %'];
  sh.getRange(5, 1, 1, kHead.length).setValues([kHead]).setFontWeight('bold').setBackground('#FFEDD5');
  var b = block(sif(cSpend), sif(cViews), sif(cClicks), sif(cAtbs), sif(cOrders), sif(cAdrev));
  sh.getRange(6, 1, 1, kHead.length).setFormulas([[
    '=' + b.spend, '=' + b.views, '=' + b.clicks, '=' + b.ctr, '=' + b.cpc,
    '=' + b.atbs, '=' + b.orders, '=' + b.cr, '=' + b.cpo, '=' + b.adrev, '=' + b.drr
  ]]);
  adsFmt_(sh, 6, [['#,##0 ₽'], ['#,##0'], ['#,##0'], ['0.0"%"'], ['#,##0 ₽'], ['#,##0'], ['#,##0'], ['0.0"%"'], ['#,##0 ₽'], ['#,##0 ₽'], ['0.0"%"']]);

  // — Блок 2: по SKU —
  var skuTop = 8;
  sh.getRange(skuTop, 1).setValue('ПО SKU ЗА ПЕРИОД').setFontWeight('bold').setBackground('#7C2D12').setFontColor('#FFFFFF');
  var sHead2 = ['Товар', 'SKU', 'Расход, ₽', 'Показы', 'Клики', 'CTR, %', 'CPC, ₽', 'Корзины', 'Заказы', 'CPO, ₽', 'ДРР, %', 'Цель ДРР, %', 'Флаг'];
  var hr = skuTop + 1;
  sh.getRange(hr, 1, 1, sHead2.length).setValues([sHead2]).setFontWeight('bold').setBackground('#FFEDD5');
  for (var i = 0; i < data.skus.length; i++) {
    var rr = hr + 1 + i;
    var skuId = data.skus[i].sku;
    var crit = SEP + rSku + SEP + '$B' + rr;   // доп. условие по SKU (sku в колонке B)
    var sb = block(sif(cSpend, crit), sif(cViews, crit), sif(cClicks, crit), sif(cAtbs, crit), sif(cOrders, crit), sif(cAdrev, crit));
    var tgt = (sku.bySku[skuId] ? sku.bySku[skuId].targetDrr : 0) || 0;
    sh.getRange(rr, 1).setValue(data.skus[i].name);
    sh.getRange(rr, 2).setValue(skuId);
    sh.getRange(rr, 3, 1, 9).setFormulas([[
      '=' + sb.spend, '=' + sb.views, '=' + sb.clicks, '=' + sb.ctr, '=' + sb.cpc,
      '=' + sb.atbs, '=' + sb.orders, '=' + sb.cpo, '=' + sb.drr
    ]]);
    sh.getRange(rr, 12).setValue(tgt);
    sh.getRange(rr, 13).setFormula(
      '=IF(K' + rr + '=0' + SEP + '"—"' + SEP + 'IF(K' + rr + '>L' + rr + SEP + '"🔴 резать"' + SEP +
      'IF(K' + rr + '<L' + rr + '*60/100' + SEP + '"🟢 усиливать"' + SEP + '"🟡 ок")))');
    adsFmt_(sh, rr, [null, null, ['#,##0 ₽'], ['#,##0'], ['#,##0'], ['0.0"%"'], ['#,##0 ₽'], ['#,##0'], ['#,##0'], ['#,##0 ₽'], ['0.0"%"'], ['0"%"'], null]);
  }

  // — Блок 3: по кампаниям (с фильтром) —
  var cTop = hr + 1 + data.skus.length + 2;
  sh.getRange(cTop, 1).setValue('ПО КАМПАНИЯМ ЗА ПЕРИОД (фильтруйте по SKU)').setFontWeight('bold').setBackground('#7C2D12').setFontColor('#FFFFFF');
  var cHead = ['Товар', 'SKU', 'advertId', 'Кампания', 'Площадка', 'Статус', 'Расход, ₽', 'Показы', 'Клики', 'CTR, %', 'CPC, ₽', 'Корзины', 'Заказы', 'CPO, ₽', 'ДРР, %'];
  var chr = cTop + 1;
  sh.getRange(chr, 1, 1, cHead.length).setValues([cHead]).setFontWeight('bold').setBackground('#FFEDD5');
  for (var j = 0; j < data.campaigns.length; j++) {
    var cr = chr + 1 + j;
    var cmp = data.campaigns[j];
    // условие по SKU (B) и advertId (C)
    var critC = SEP + rSku + SEP + '$B' + cr + SEP + rAdv + SEP + '$C' + cr;
    var cb = block(sif(cSpend, critC), sif(cViews, critC), sif(cClicks, critC), sif(cAtbs, critC), sif(cOrders, critC), sif(cAdrev, critC));
    sh.getRange(cr, 1, 1, 6).setValues([[cmp.skuName, cmp.sku, cmp.adv, cmp.campName, cmp.placement, cmp.status]]);
    sh.getRange(cr, 7, 1, 9).setFormulas([[
      '=' + cb.spend, '=' + cb.views, '=' + cb.clicks, '=' + cb.ctr, '=' + cb.cpc,
      '=' + cb.atbs, '=' + cb.orders, '=' + cb.cpo, '=' + cb.drr
    ]]);
    adsFmt_(sh, cr, [null, null, null, null, null, null, ['#,##0 ₽'], ['#,##0'], ['#,##0'], ['0.0"%"'], ['#,##0 ₽'], ['#,##0'], ['#,##0'], ['#,##0 ₽'], ['0.0"%"']]);
  }
  // нативный фильтр на таблицу кампаний
  if (data.campaigns.length) {
    sh.getRange(chr, 1, data.campaigns.length + 1, cHead.length).createFilter();
  }

  // — Косметика —
  sh.setColumnWidth(1, 240);
  sh.setColumnWidth(4, 260);
  sh.setColumnWidths(2, 2, 110);
  sh.setColumnWidths(5, 11, 95);
  sh.setFrozenRows(2);
  for (var hc = SC; hc < SC + sHead.length; hc++) sh.hideColumns(hc);
  sh.getRange('A2:I2').setBackground('#F3F4F6');
}

/** Форматы по колонкам строки: arr[i] = [numFmt] или null. */
function adsFmt_(sh, row, arr) {
  for (var i = 0; i < arr.length; i++) {
    if (arr[i]) sh.getRange(row, i + 1).setNumberFormat(arr[i][0]);
  }
}
