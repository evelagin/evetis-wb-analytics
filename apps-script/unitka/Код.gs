/** UNITKA 2.0 R4 v4.2.0 — финальный сентябрьский QA: сверка с BQ + независимый пересчёт формул. */
var SSID='1E4L4JuwfEqr9owhsGkAjb8F24lRpWpWkEyVmSuRxaJg';
var SH='WB_Юнит_2025', QA='ZZ_QA_СЕНТЯБРЬ_2026', VER='unitka2.0/v4.2.0';
var D=[
 ['01.09.2026',737,7875,11,0, 925.94,214,499.03,36.50,11],
 ['02.09.2026',738,9225,11,1,1083.64,203,499   ,34.21,10],
 ['03.09.2026',739,3176, 7,0, 373.20,null,499  ,32.45, 7],
 ['04.09.2026',740,8112, 3,0, 951.47,170,499   ,28.77, 3],
 ['05.09.2026',741,5949, 9,0, 695.89,167,499   ,28.25, 9],
 ['06.09.2026',742,4144, 6,0, 481.53,158,499   ,26.67, 6],
 ['07.09.2026',743,4374, 7,0, 512.29,151,519.14,26.48, 7],
 ['08.09.2026',744, 796, 1,0,  93.16,143,640   ,25.42, 1],
 ['09.09.2026',745,1663, 4,0, 838.99,144,640   ,25.07, 4]];
var M=[
 ['views — Показы','O',2,'wb_mart.MART_SKU_DAILY.views','AUTO / BigQuery','FACT'],
 ['gross orders — Заказы','Q',3,'wb_mart.FACT_ORDERS: SUM(quantity)','AUTO / BigQuery','FACT'],
 ['cancels — Отмены','S',4,'wb_mart.FACT_ORDERS: SUM(IF(is_cancel…))','AUTO / BigQuery','FACT, окно перезабора 21 день'],
 ['ads — Реклама','X',5,'wb_mart.MART_SKU_DAILY.ad_spend','AUTO / BigQuery','FACT'],
 ['stock — Остатки','T',6,'wb_mart.FACT_STOCKS_SNAPSHOT.quantity','AUTO / BigQuery','FACT; 03.09 снимка нет'],
 ['price — Цена (до СПП)','AA',7,'wb_mart.FACT_ORDERS: SUM(price_with_disc*qty)/SUM(qty)','AUTO / BigQuery','FACT'],
 ['storage — Хранение','AG',8,'wb_raw.RAW_WB_PAID_STORAGE','AUTO / BigQuery','FACT, сверка с финотчётом 0,03 ₽']];
function q_(a){ return "'"+SH+"'!"+a; }
function main(){
  var ss=SpreadsheetApp.openById(SSID), q=ss.getSheetByName(QA);
  if(!q) q=ss.insertSheet(QA, ss.getNumSheets());
  q.clear(); q.clearNotes();
  var H=[['SEPTEMBER MASTER PILOT — FINAL QA','','','','','','','',''],
   ['SKU','252442517 Крем для рук','','','','','','',''],
   ['Блок',SH+' · M:AJ · 737–766','','','','','','',''],
   ['Окно факта','01.09.2026 .. LAST_CLOSED_DATE = 09.09.2026','','','','','','',''],
   ['Часовой пояс','Europe/Moscow','','','','','','',''],
   ['Сформировано',new Date(),'','агент',VER,'','','',''],
   ['','','','','','','','',''],
   ['DATE','METRIC','SOURCE','BQ','SHEETS','DELTA','STATUS','SOURCE_TYPE','DATA_QUALITY']];
  q.getRange(1,1,H.length,9).setValues(H);
  q.getRange(1,1,1,9).setFontWeight('bold').setFontSize(13);
  q.getRange(8,1,1,9).setFontWeight('bold').setBackground('#e8eaed');
  q.setFrozenRows(8);
  var rows=[], forms=[], r0=9, r=r0;
  for(var i=0;i<D.length;i++){
    var R=D[i][1];
    for(var m=0;m<M.length;m++){
      var v=D[i][M[m][2]];
      rows.push([D[i][0],M[m][0],M[m][3],(v===null?'':v),'','','',M[m][4],M[m][5]]);
      forms.push(['='+q_(M[m][1]+R),
        (v===null?'':'=IF(E'+r+'="";"";E'+r+'-D'+r+')'),
        (v===null?'="NO SOURCE DATA"':'=IF(E'+r+'="";"НЕ ЗАПОЛНЕНО";IF(ABS(F'+r+')<=0,01;"OK";"MISMATCH"))')]); r++;
    }
    rows.push([D[i][0],'identity: NET = GROSS − CANCELS','wb_mart.MART_SKU_DAILY.orders_qty',D[i][9],'','','','КОНТРОЛЬ','семантика заказов']);
    forms.push(['='+q_('Q'+R)+'-'+q_('S'+R),'=IF(E'+r+'="";"";E'+r+'-D'+r+')','=IF(F'+r+'=0;"OK";"MISMATCH")']); r++;
    // независимый пересчёт формул книги
    rows.push([D[i][0],'формула: оборачиваемость = Остатки / Заказы','пересчёт из T и Q этого же дня','','','','','ФОРМУЛА','самопроверка']);
    forms.push(['='+q_('U'+R),'=IF(E'+r+'="";"";E'+r+'-'+q_('T'+R)+'/'+q_('Q'+R)+')','=IF(ABS(F'+r+')<=0,0001;"OK";"MISMATCH")']); r++;
    rows.push([D[i][0],'формула: вклад/ед = AE − AF − AH − себестоимость','пересчёт из AE, AF, AH и R45','','','','','ФОРМУЛА','самопроверка']);
    forms.push(['='+q_('AI'+R),'=IF(E'+r+'="";"";E'+r+'-('+q_('AE'+R)+'-'+q_('AF'+R)+'-'+q_('AH'+R)+'-'+q_('$R$45')+'))','=IF(ABS(F'+r+')<=0,0001;"OK";"MISMATCH")']); r++;
    rows.push([D[i][0],'формула: вклад дня = Q×AI − реклама − хранение − S×AI + блогеры','пересчёт из Q, AI, X, AG, S, Y','','','','','ФОРМУЛА','самопроверка; отмены не задваиваются']);
    forms.push(['='+q_('W'+R),'=IF(E'+r+'="";"";E'+r+'-('+q_('Q'+R)+'*'+q_('AI'+R)+'-'+q_('X'+R)+'-'+q_('AG'+R)+'-'+q_('S'+R)+'*'+q_('AI'+R)+'+'+q_('Y'+R)+'))','=IF(ABS(F'+r+')<=0,0001;"OK";"MISMATCH")']); r++;
  }
  // MTD
  var MTD=[['MTD','Показы','SUM закрытых дней','O'],['MTD','Заказы','SUM','Q'],['MTD','Отмены','SUM','S'],
           ['MTD','Реклама','SUM','X'],['MTD','Хранение','SUM','AG'],['MTD','Доходность общая','SUM','W']];
  for(var t=0;t<MTD.length;t++){
    rows.push(['ИТОГ',MTD[t][1],MTD[t][2],'','','','','MTD ACTUAL','только закрытые дни']);
    forms.push(['='+q_(MTD[t][3]+'767'),'=IF(E'+r+'="";"";E'+r+'-SUMIF('+q_('$M$737:$M$766')+';"<="&'+q_('$WB$736')+';'+q_(MTD[t][3]+'737:'+MTD[t][3]+'766')+'))','=IF(ABS(F'+r+')<=0,01;"OK";"MISMATCH")']); r++;
  }
  q.getRange(r0,1,rows.length,9).setValues(rows);
  for(var k=0;k<forms.length;k++){
    q.getRange(r0+k,5).setFormula(forms[k][0]);
    if(forms[k][1]) q.getRange(r0+k,6).setFormula(forms[k][1]);
    q.getRange(r0+k,7).setFormula(forms[k][2]);
  }
  q.getRange(r0,4,rows.length,3).setNumberFormat('#,##0.00');
  var f=r+1;
  var F=[['БЕЗ СВЕРКИ С BQ','','','','','','','',''],
   ['','card opens — Переходы','WB /api/v2/nm-report/downloads -> openCardCount','','','','PENDING ACCESS','AUTO (код готов)','403 Report not available; все альтернативные пути 404'],
   ['','add to cart — Корзины','то же -> addToCartCount','','','','PENDING ACCESS','AUTO (код готов)','тот же 403'],
   ['','SPP — СПП %','V_WB_FINANCE_CANONICAL.spp_percent','','','','MANUAL','MANUAL / INFORMATIONAL','привязан к дате выкупа и к покупателю; на выручку продавца не влияет'],
   ['','N — Блогеры + самовыкупы','источника нет','','','','MANUAL','MANUAL',''],
   ['','строка 768 — план месяца','—','','','','MANUAL','MANUAL','владелец впишет свой'],
   ['','commission — Комиссия','42,25 % базовая + 3,07 % эквайринг','','','','OK','MODEL / период','212 выкупов: MAE 2,24 ₽ (0,9 %), MAX 8,5 ₽ (3,4 %)'],
   ['','logistics — Логистика','15 522,43 ₽ / 212 ед, окно 11.08–09.09','','','','OK','MODEL / период','ROLLING_30D_LOGISTICS_ESTIMATE, confidence HIGH'],
   ['','tax reserve — налог (резерв)','2 % управленческое допущение','','','','OK','MODEL','НЕ бухгалтерский налог УСН'],
   ['','','','','','','','',''],
   ['БУДУЩИЕ ДНИ (после LAST_CLOSED_DATE)','','','','','','','',''],
   ['','факт и производные','313 формул обёрнуты в IF(дата>LAST_CLOSED_DATE;"";…)','','','','OK','—','пусто, а не ноль'],
   ['','параметры модели AD и AF','значение есть, шрифт приглушён','','','','OK','MODEL','в MTD не входят']];
  q.getRange(f,1,F.length,9).setValues(F);
  q.getRange(f,1,1,9).setFontWeight('bold');
  q.getRange(f+10,1,1,9).setFontWeight('bold');
  q.setColumnWidth(1,95); q.setColumnWidth(2,330); q.setColumnWidth(3,330); q.setColumnWidth(4,95);
  q.setColumnWidth(5,95); q.setColumnWidth(6,85); q.setColumnWidth(7,130); q.setColumnWidth(8,150); q.setColumnWidth(9,340);
  SpreadsheetApp.flush();
  var res=q.getRange(r0,1,rows.length,9).getValues(), ok=0,nod=0,bad=[];
  for(var i2=0;i2<res.length;i2++){var st=String(res[i2][6]);
    if(st==='OK') ok++; else if(st.indexOf('NO ')===0||st.indexOf('НЕ ')===0) nod++;
    else bad.push(res[i2][0]+' | '+res[i2][1]+' | BQ='+res[i2][3]+' SH='+res[i2][4]+' Δ='+res[i2][5]+' -> '+st);}
  Logger.log('QA: строк '+res.length+' | OK '+ok+' | без источника '+nod+' | MISMATCH '+bad.length+(bad.length?'\n  '+bad.join('\n  '):''));
}