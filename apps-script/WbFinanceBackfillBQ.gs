/**
 * ══════════════════════════════════════════════════════════════
 *  WbFinanceBackfillBQ.gs — бэкфилл финансов WB напрямую в BigQuery
 *  (Фаза B миграции). НЕ трогает лист RAW_WB_FINANCE и старый
 *  загрузчик — это параллельный путь. Полностью обратим.
 * ══════════════════════════════════════════════════════════════
 *  Использует:
 *    из WbFinanceApiV1.gs — getFinanceV1Token_, wbFinV1ListAll_,
 *      wbFinV1FetchDetailedAll_, wbFinV1AdaptRow_, normalizeFinanceApiRows_,
 *      getRawFinanceSheet_, buildFinanceRawHeaderMap_,
 *      WB_FIN_V1_BACKFILL_FROM_, WB_FIN_V1_BUDGET_MS_, WB_FIN_V1_SOURCE_TAG_
 *    из WbBigQuery.gs — getBqConfig_, bqEnsureDataset_,
 *      bqEnsureFinanceTable_, bqLoadRows_, bqQuery_, BQ_TABLE_FINANCE_
 *
 *  Порядок запуска:
 *    1) bqCreateFinanceView()
 *    2) wbFinanceBQStats()
 *    3) wbFinanceBackfillAutoStart()
 *    4) после завершения: wbFinanceBQStats()
 * ══════════════════════════════════════════════════════════════
 */

var WB_FIN_BQ_DONE_PROP_   = 'WB_FIN_V1_DONE_BQ';   // прогресс бэкфилла в BQ (отдельно от листа)
var WB_FIN_BQ_VIEW_        = 'V_WB_FINANCE';         // вью с дедупом
var WB_FIN_BQ_LOAD_BATCH_  = 10000;                  // строк на один load-job
var WB_FIN_BQ_TICK_FN_     = 'wbFinanceBackfillAutoTick';


// ═══════════════════════════════════════
//  КОНВЕРТАЦИЯ строка-массив → объект для BQ
// ═══════════════════════════════════════

/** Заголовки листа RAW_WB_FINANCE (index → имя колонки). */
function wbFinBqHeaderNames_(rawSheet, rawLastCol) {
  return rawSheet.getRange(1, 1, 1, rawLastCol).getValues()[0].map(function (v) {
    return String(v || '').trim();
  });
}

/** Преобразует одну строку (массив) в плоский объект {colName: value} + _rr_date. */
function wbFinBqRowToObj_(rowArr, headerNames) {
  var o = {};
  for (var i = 0; i < headerNames.length; i++) {
    var name = headerNames[i];
    if (!name) continue;

    var v = rowArr[i];
    if (v === '' || v === null || v === undefined) continue; // пусто → NULL в BQ

    o[name] = (typeof v === 'string') ? v : String(v);
  }

  // Служебная колонка партиции: строго YYYY-MM-DD, иначе NULL.
  var dk = normalizeDateKey_(o['rr_dt'] || '') || normalizeDateKey_(o['sale_dt'] || '');
  if (dk && /^\d{4}-\d{2}-\d{2}$/.test(dk)) {
    o['_rr_date'] = dk;
  }

  return o;
}

/** Пакетная загрузка объектов в таблицу. */
function wbFinBqLoadBatched_(objs) {
  var total = 0;

  for (var i = 0; i < objs.length; i += WB_FIN_BQ_LOAD_BATCH_) {
    var slice = objs.slice(i, i + WB_FIN_BQ_LOAD_BATCH_);
    total += bqLoadRows_(BQ_TABLE_FINANCE_, slice);
  }

  return total;
}


// ═══════════════════════════════════════
//  ВЬЮ ДЕДУПА
// ═══════════════════════════════════════

/** Создаёт/обновляет вью V_WB_FINANCE — последняя версия строки по report_id|rrd_id. */
function bqCreateFinanceView() {
  var c = getBqConfig_();
  var raw = '`' + c.projectId + '.' + c.datasetId + '.' + BQ_TABLE_FINANCE_ + '`';
  var view = '`' + c.projectId + '.' + c.datasetId + '.' + WB_FIN_BQ_VIEW_ + '`';

  var sql =
    'CREATE OR REPLACE VIEW ' + view + ' AS\n' +
    'SELECT * EXCEPT(_rn) FROM (\n' +
    '  SELECT *, ROW_NUMBER() OVER (\n' +
    '    PARTITION BY report_id, rrd_id ORDER BY loaded_at DESC\n' +
    '  ) AS _rn\n' +
    '  FROM ' + raw + '\n' +
    '  WHERE rrd_id IS NOT NULL AND rrd_id != ""\n' +
    ')\nWHERE _rn = 1';

  bqQuery_(sql);
  console.log('✅ Вью создан/обновлён: ' + c.datasetId + '.' + WB_FIN_BQ_VIEW_);
}


// ═══════════════════════════════════════
//  БЭКФИЛЛ
// ═══════════════════════════════════════

/**
 * Внутренний прогон: обрабатывает отчёты, пишет в BQ.
 * testOneOnly=true — обработать только первый ещё-не-готовый отчёт и выйти.
 */
function wbFinBqRun_(testOneOnly) {
  var tk = getFinanceV1Token_();
  if (!tk) {
    console.log('❌ Нет токена WB_TOKEN_FINANCE');
    return { finished: false, done: 0, total: 0 };
  }

  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var rawSheet = getRawFinanceSheet_(ss);
  if (!rawSheet) {
    console.log('❌ Лист RAW_WB_FINANCE не найден (нужен только для заголовков)');
    return { finished: false, done: 0, total: 0 };
  }

  var rawLastCol = rawSheet.getLastColumn();
  var hMap = buildFinanceRawHeaderMap_(rawSheet, rawLastCol);
  var headerNames = wbFinBqHeaderNames_(rawSheet, rawLastCol);

  if (hMap['source_api'] === undefined || hMap['report_id'] === undefined || hMap['rrd_id'] === undefined) {
    console.log('❌ В заголовках листа нет source_api / report_id / rrd_id — стоп');
    return { finished: false, done: 0, total: 0 };
  }

  // Убедимся, что таблица есть.
  bqEnsureDataset_();
  bqEnsureFinanceTable_();

  var props = PropertiesService.getScriptProperties();
  var done = {};

  try {
    var rd = props.getProperty(WB_FIN_BQ_DONE_PROP_);
    if (rd) done = JSON.parse(rd) || {};
  } catch (e) {
    done = {};
  }

  var today = Utilities.formatDate(new Date(), 'Europe/Moscow', 'yyyy-MM-dd');
  var lst = wbFinV1ListAll_(tk.token, WB_FIN_V1_BACKFILL_FROM_, today);

  if (!lst.ok) {
    console.log('❌ list: ' + lst.error);
    return { finished: false, done: Object.keys(done).length, total: 0 };
  }

  var reports = lst.data;
  reports.sort(function (a, b) {
    return String(a.dateFrom).localeCompare(String(b.dateFrom));
  });

  console.log('Всего отчётов: ' + reports.length + ' | готово (BQ): ' + Object.keys(done).length);

  var skuIndex = (typeof buildSkuIndex_ === 'function') ? buildSkuIndex_(ss) : null;
  var start = Date.now();
  var processed = 0;
  var appended = 0;
  var errs = 0;

  for (var i = 0; i < reports.length; i++) {
    var rep = reports[i];
    var rid = String(rep.reportId);

    if (done[rid]) continue;

    if (!testOneOnly && (Date.now() - start > WB_FIN_V1_BUDGET_MS_)) {
      props.setProperty(WB_FIN_BQ_DONE_PROP_, JSON.stringify(done));
      console.log('⏸️ Бюджет исчерпан. За прогон: отчётов ' + processed + ', строк +' + appended + '. Следующий запуск продолжит с незавершённых reportId.');
      return { finished: false, done: Object.keys(done).length, total: reports.length };
    }

    // Тяга с ретраями: временный сетевой сбой WB не должен ронять весь прогон.
    var fetched = null;

    for (var attempt = 1; attempt <= 3; attempt++) {
      try {
        fetched = wbFinV1FetchDetailedAll_(tk.token, rid);
        break;
      } catch (ex) {
        console.log('⚠️ попытка ' + attempt + '/3, отчёт ' + rid + ': ' + ex);
        if (attempt < 3) Utilities.sleep(3000 * attempt);
      }
    }

    if (!fetched) {
      errs++;
      console.log('✗ ' + rid + ' — сеть недоступна, отчёт НЕ отмечен готовым, повтор на следующем тике');
      continue;
    }

    if (!fetched.ok) {
      errs++;
      console.log('✗ ' + rid + ' (' + rep.dateFrom + '): ' + fetched.error + ' — отчёт НЕ отмечен готовым, повтор на следующем тике');
      continue;
    }

    var adapted = [];
    for (var a = 0; a < fetched.data.length; a++) {
      adapted.push(wbFinV1AdaptRow_(fetched.data[a]));
    }

    var loadId = 'FIN_V1_BF_BQ';
    var loadedAt = Utilities.formatDate(new Date(), 'Europe/Moscow', 'yyyy-MM-dd HH:mm:ss');
    var rows = normalizeFinanceApiRows_(adapted, hMap, rawLastCol, loadId, loadedAt, rep.dateFrom, rep.dateTo, '', skuIndex);

    // Метка источника + детерминированный row_hash.
    for (var rI = 0; rI < rows.length; rI++) {
      rows[rI][hMap['source_api']] = WB_FIN_V1_SOURCE_TAG_;

      var rrd = String(rows[rI][hMap['rrd_id']] || '').trim();
      if (hMap['row_hash'] !== undefined) {
        rows[rI][hMap['row_hash']] = financeMd5_(WB_FIN_V1_SOURCE_TAG_ + '|' + rid + '|' + rrd);
      }
    }

    var objs = [];
    for (var k = 0; k < rows.length; k++) {
      objs.push(wbFinBqRowToObj_(rows[k], headerNames));
    }

    var loaded = wbFinBqLoadBatched_(objs);

    appended += loaded;
    done[rid] = 1;
    processed++;

    props.setProperty(WB_FIN_BQ_DONE_PROP_, JSON.stringify(done));

    console.log('✓ ' + rep.dateFrom + '…' + rep.dateTo + ' [' + rid + '] +' + loaded + ' строк в BQ');

    if (testOneOnly) {
      console.log('🧪 TEST-ONE завершён. Проверьте wbFinanceBQStats().');
      return { finished: false, done: Object.keys(done).length, total: reports.length };
    }
  }

  var doneCount = Object.keys(done).length;
  var finished = doneCount >= reports.length;

  console.log('━━━ БЭКФИЛЛ В BQ: прогресс ' + doneCount + '/' + reports.length +
    ', добавлено за прогон: ' + appended +
    ', ошибок за прогон: ' + errs +
    (finished ? ' | завершено' : ' | не завершено'));

  return { finished: finished, done: doneCount, total: reports.length };
}

/** Прогнать ОДИН отчёт для проверки конвейера. */
function wbFinanceBackfillBQ_TESTONE() {
  wbFinBqRun_(true);
}

/** Полный бэкфилл в BQ вручную. */
function wbFinanceBackfillBQ() {
  wbFinBqRun_(false);
}

/** Сбросить прогресс бэкфилла в BQ. НЕ удаляет данные из BigQuery. */
function wbFinanceBQResetProgress() {
  PropertiesService.getScriptProperties().deleteProperty(WB_FIN_BQ_DONE_PROP_);
  console.log('♻️ Прогресс BQ-бэкфилла сброшен. Данные в таблице не тронуты. Без очистки BQ это может привести к физическим дублям при повторном backfill.');
}


// ═══════════════════════════════════════
//  АВТО-РЕЖИМ
// ═══════════════════════════════════════

/** Запуск авто-бэкфилла: ставит триггер каждые 5 минут и делает первый чанк сразу. */
function wbFinanceBackfillAutoStart() {
  wbFinanceBackfillAutoStop();
  ScriptApp.newTrigger(WB_FIN_BQ_TICK_FN_).timeBased().everyMinutes(5).create();
  console.log('▶️ Авто-бэкфилл запущен: тик каждые 5 минут. Можно закрыть вкладку.');
  wbFinanceBackfillAutoTick();
}

/** Один тик планировщика: чанк + проверка завершения. С защитой от наложения. */
function wbFinanceBackfillAutoTick() {
  var lock = LockService.getScriptLock();

  if (!lock.tryLock(1000)) {
    console.log('⏭️ Пропуск тика: предыдущий ещё выполняется');
    return;
  }

  try {
    var st = wbFinBqRun_(false);

    if (st && st.finished) {
      wbFinanceBackfillAutoStop();
      console.log('🎉 Авто-бэкфилл завершён: ' + st.done + '/' + st.total + '. Триггер удалён. Запустите wbFinanceBQStats().');
    } else if (st) {
      console.log('… прогресс ' + st.done + '/' + st.total + '. Следующий тик через 5 минут.');
    }
  } finally {
    lock.releaseLock();
  }
}

/** Остановка авто-бэкфилла: удаляет все триггеры тика. */
function wbFinanceBackfillAutoStop() {
  var trs = ScriptApp.getProjectTriggers();
  var n = 0;

  for (var i = 0; i < trs.length; i++) {
    if (trs[i].getHandlerFunction() === WB_FIN_BQ_TICK_FN_) {
      ScriptApp.deleteTrigger(trs[i]);
      n++;
    }
  }

  if (n) console.log('⏹️ Удалено триггеров: ' + n);
}


// ═══════════════════════════════════════
//  КОНТРОЛЬНЫЕ ЦИФРЫ
// ═══════════════════════════════════════

/** Считает строки/уникальные/суммы из BQ: сырьё и дедуп-вью. */
function wbFinanceBQStats() {
  var c = getBqConfig_();
  var raw = '`' + c.projectId + '.' + c.datasetId + '.' + BQ_TABLE_FINANCE_ + '`';
  var view = '`' + c.projectId + '.' + c.datasetId + '.' + WB_FIN_BQ_VIEW_ + '`';

  var num = function (col) {
    return 'ROUND(SUM(SAFE_CAST(REPLACE(REPLACE(' + col + ', " ", ""), ",", ".") AS FLOAT64)), 2)';
  };

  var sqlRaw = 'SELECT COUNT(*) AS total, ' +
    'COUNT(DISTINCT CONCAT(IFNULL(report_id,""),"|",IFNULL(rrd_id,""))) AS uniq ' +
    'FROM ' + raw;

  var r1 = bqQuery_(sqlRaw);
  var f1 = r1.rows.length ? r1.rows[0].f : null;

  if (f1) {
    console.log('RAW: строк ' + f1[0].v + ' | уник. report|rrd ' + f1[1].v);
  }

  var sqlView = 'SELECT COUNT(*) AS cnt, ' +
    num('for_pay') + ' AS sum_for_pay, ' +
    num('retail_amount') + ' AS sum_retail, ' +
    num('logistics_amount') + ' AS sum_logistics, ' +
    'MIN(_rr_date) AS min_d, MAX(_rr_date) AS max_d ' +
    'FROM ' + view;

  var r2 = bqQuery_(sqlView);
  var f2 = r2.rows.length ? r2.rows[0].f : null;

  if (f2) {
    console.log('VIEW (дедуп): строк ' + f2[0].v +
      ' | Σ for_pay ' + f2[1].v +
      ' | Σ retail_amount ' + f2[2].v +
      ' | Σ logistics_amount ' + f2[3].v +
      ' | период ' + f2[4].v + '…' + f2[5].v);
  }
}
