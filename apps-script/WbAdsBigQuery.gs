/**
 * ══════════════════════════════════════════════════════════════
 *  WbAdsBigQuery.gs — BigQuery-приёмник для рекламных загрузчиков
 *  (Фаза C). Включается флагом; тяга и построители строк не тронуты.
 * ══════════════════════════════════════════════════════════════
 *  Когда флаг WB_ADS_BQ_SINK включён, общие хелперы
 *  wbAdvRawEnsureSheet_ / wbAdvRawAppendRows_ (в WbAdsRawLoader.gs)
 *  пишут не в листы, а в BigQuery. Все 5 источников:
 *    RAW_WB_ADV_CAMPAIGNS, RAW_WB_ADV_CAMPAIGN_STATS,
 *    RAW_WB_ADV_BOOSTER_STATS, RAW_WB_ADV_SEARCH_CLUSTERS, RAW_WB_ADV_COSTS
 *  Таблицы: все колонки STRING, партиция по времени загрузки (DAY),
 *  кластеризация по date/advertId/nmId. Дедуп — во вью.
 *
 *  Порядок запуска (лестница безопасности):
 *    C0) wbAdsBqInit()  — БЕЗ WB API: preflight+флаг, 5 пустых таблиц,
 *        5 вью, счётчики. Падает ДО флага, если доступа нет (fail-closed).
 *    C1) loadWbAdsRawPeriod('YYYY-MM-DD','YYYY-MM-DD') за ОДИН ЗАВЕРШЁННЫЙ
 *        день (не текущий неполный) → wbAdsBqStats() → wbAdsBqCreateViews().
 *    C2) 7 дней тем же вызовом; проверить дедуп-вью.
 *
 *  Backfill истории — НЕ через общий оркестратор и НЕ 90 дней разом.
 *  Оркестратор в одном прогоне делает campaigns→costs→search→fullstats;
 *  search clusters съедают ~2 мин обязательных пауз, и fullstats может
 *  не успеть за 6-мин лимит → PARTIAL (причина — тайм-бюджет и rate-limit,
 *  НЕ перекрытие окон: wbAdsSplitPeriod_ даёт смежные неперекрывающиеся).
 *  Правильный backfill — ПО ИСТОЧНИКАМ:
 *      loadWbAdsCampaignsRaw();                         // один раз
 *      loadWbAdsCostsRaw('2026-04-01','2026-04-30');    // помесячно
 *      loadWbAdsFullstatsRaw('2026-04-01','2026-04-07');// малыми окнами
 *      loadWbAdsSearchClustersRaw('2026-04-01','2026-04-30'); // отдельно (sample)
 *  Размер окна fullstats подобрать после C1/C2.
 *  Откат: wbAdsBqDisable() — снова пишем в листы.
 *
 *  Замечание про search clusters: RAW_WB_ADV_SEARCH_CLUSTERS — это
 *  SAMPLE/диагностика (первые WB_ADS_SEARCH_MAX_PAIRS_RAW_ связок,
 *  без ротации/checkpoint), НЕ полный RAW. Не строить на нём полноту.
 * ══════════════════════════════════════════════════════════════
 */

var WB_ADS_BQ_PROP_ = 'WB_ADS_BQ_SINK';
// Первый прогон: маленький батч (NDJSON собирается в память Apps Script,
// raw_json бывает крупным). После замера можно поднять до 1000–2000.
var WB_ADS_BQ_BATCH_ = 1000;

// Allowlist: рекламный sink пишет ТОЛЬКО в эти таблицы (fail-closed).
var WB_ADS_BQ_TABLES_ = {
  RAW_WB_ADV_CAMPAIGNS: true,
  RAW_WB_ADV_CAMPAIGN_STATS: true,
  RAW_WB_ADV_BOOSTER_STATS: true,
  RAW_WB_ADV_SEARCH_CLUSTERS: true,
  RAW_WB_ADV_COSTS: true
};
function wbAdsBqAssertTable_(tableId) {
  if (!WB_ADS_BQ_TABLES_[tableId]) {
    throw new Error('Запрещённая Ads BQ-таблица: ' + tableId +
      ' (разрешены только RAW_WB_ADV_*)');
  }
}

/** Включён ли BQ-приёмник рекламы. */
function wbAdsBqSinkOn_() {
  return PropertiesService.getScriptProperties().getProperty(WB_ADS_BQ_PROP_) === '1';
}
function wbAdsBqEnable() {
  // Preflight (fail-closed): проверяем доступ/конфиг/round-trip ДО флага.
  // Если что-то не так — bqSelfTest/ensure кинут ошибку, флаг НЕ ставится.
  var c = getBqConfig_();
  bqEnsureDataset_();
  bqSelfTest();
  PropertiesService.getScriptProperties().setProperty(WB_ADS_BQ_PROP_, '1');
  console.log('✅ Рекламный sink → BigQuery ВКЛючён: ' +
    c.projectId + '.' + c.datasetId + '. Загрузчики теперь пишут в BQ.');
}
function wbAdsBqDisable() {
  PropertiesService.getScriptProperties().deleteProperty(WB_ADS_BQ_PROP_);
  console.log('⏹️ Рекламный sink → BigQuery ВЫКЛючён. Загрузчики снова пишут в листы.');
}


/**
 * Гарантирует BQ-таблицу рекламы (все колонки STRING).
 * Если таблицы нет — создаёт. Если есть — аудит схемы и аддитивное
 * расширение (как для листов): добавляет недостающие STRING NULLABLE,
 * обрывает запуск при несовместимом типе существующей колонки.
 * Пустой catch НЕ используем: отличаем 404 от прочих ошибок (№1).
 * @return {boolean} true если таблица была создана заново.
 */
function wbAdvBqEnsureTable_(tableId, headers) {
  wbAdsBqAssertTable_(tableId);
  var c = getBqConfig_();
  bqEnsureDataset_();

  var table = null;
  try {
    table = BigQuery.Tables.get(c.projectId, c.datasetId, tableId);
  } catch (e) {
    var code = Number(e && (e.code || e.statusCode));
    var msg = String((e && e.message) || e);
    var notFound = (code === 404) || (msg.indexOf('Not found') >= 0) || (msg.indexOf('notFound') >= 0);
    if (!notFound) {
      throw new Error('Не удалось проверить BQ-таблицу ' + tableId + ': ' + msg);
    }
  }

  if (!table) return wbAdvBqCreateTable_(tableId, headers);
  wbAdvBqAuditAndExtendSchema_(tableId, table, headers);
  return false;
}

/** Создаёт рекламную BQ-таблицу (STRING-колонки, ingestion-time партиция). */
function wbAdvBqCreateTable_(tableId, headers) {
  var c = getBqConfig_();
  var fields = headers.map(function (h) { return { name: h, type: 'STRING', mode: 'NULLABLE' }; });
  var cluster = [];
  var cand = ['date', 'advertId', 'nmId', 'advert_id', 'nm_id'];
  for (var i = 0; i < cand.length && cluster.length < 4; i++) {
    if (headers.indexOf(cand[i]) >= 0) cluster.push(cand[i]);
  }
  var req = {
    tableReference: { projectId: c.projectId, datasetId: c.datasetId, tableId: tableId },
    schema: { fields: fields },
    timePartitioning: { type: 'DAY' } // партиция по времени загрузки (поле не нужно)
  };
  if (cluster.length) req.clustering = { fields: cluster };
  BigQuery.Tables.insert(req, c.projectId, c.datasetId);
  console.log('✅ BQ таблица создана: ' + tableId +
    (cluster.length ? ' (кластер: ' + cluster.join(',') + ')' : ''));
  return true;
}

/**
 * Аудит СХЕМЫ КОЛОНОК существующей таблицы: добавляет недостающие
 * колонки (STRING NULLABLE) аддитивно; обрывает запуск, если колонка с
 * тем же именем есть, но НЕ STRING (несовместимый контракт RAW).
 * NB: партиционирование и clustering НЕ проверяются — таблицы создаёт
 * наш wbAdvBqCreateTable_(), метаданные заведомо верные. Проверку
 * метаданных можно добавить позже (бэклог).
 */
function wbAdvBqAuditAndExtendSchema_(tableId, table, headers) {
  var c = getBqConfig_();
  var existing = (table.schema && table.schema.fields) || [];
  var byName = {};
  for (var i = 0; i < existing.length; i++) byName[existing[i].name] = existing[i];

  var missing = [];
  for (var h = 0; h < headers.length; h++) {
    var name = headers[h];
    var f = byName[name];
    if (!f) { missing.push({ name: name, type: 'STRING', mode: 'NULLABLE' }); continue; }
    if (String(f.type).toUpperCase() !== 'STRING') {
      throw new Error('BQ-таблица ' + tableId + ': колонка ' + name +
        ' имеет тип ' + f.type + ', ожидался STRING. Расширение прервано.');
    }
  }
  if (!missing.length) return;

  var newFields = existing.concat(missing);
  BigQuery.Tables.patch(
    { schema: { fields: newFields } }, c.projectId, c.datasetId, tableId
  );
  console.log('  BQ таблица ' + tableId + ': добавлены колонки → ' +
    missing.map(function (m) { return m.name; }).join(', '));
}

/** Грузит массив объектов-строк в BQ-таблицу (все значения → STRING). */
function wbAdvBqAppendRows_(tableId, rowObjs) {
  wbAdsBqAssertTable_(tableId);
  if (!rowObjs || !rowObjs.length) return 0;
  var norm = [];
  for (var i = 0; i < rowObjs.length; i++) {
    var o = rowObjs[i], out = {};
    for (var k in o) {
      if (!o.hasOwnProperty(k)) continue;
      var v = o[k];
      if (v === '' || v === null || v === undefined) continue;
      out[k] = (typeof v === 'string') ? v : String(v);
    }
    norm.push(out);
  }
  var total = 0;
  for (var j = 0; j < norm.length; j += WB_ADS_BQ_BATCH_) {
    total += bqLoadRows_(tableId, norm.slice(j, j + WB_ADS_BQ_BATCH_));
  }
  return total;
}


/**
 * Гарантирует все 5 пустых RAW-таблиц рекламы (идемпотентно).
 * Нужно, потому что загрузчики создают таблицы лениво: если за период
 * нет кампаний 7/9/11 или связок advertId+nmId, часть таблиц не появится,
 * и wbAdsBqCreateViews() упадёт на отсутствующей. Заголовки берём из
 * констант WB_ADV_RAW_*_HEADERS_ (WbAdsRawLoader.gs).
 */
function wbAdsBqEnsureAllTables_() {
  wbAdvBqEnsureTable_('RAW_WB_ADV_CAMPAIGNS', WB_ADV_RAW_CAMPAIGNS_HEADERS_);
  wbAdvBqEnsureTable_('RAW_WB_ADV_CAMPAIGN_STATS', WB_ADV_RAW_CAMPAIGN_STATS_HEADERS_);
  wbAdvBqEnsureTable_('RAW_WB_ADV_BOOSTER_STATS', WB_ADV_RAW_BOOSTER_STATS_HEADERS_);
  wbAdvBqEnsureTable_('RAW_WB_ADV_SEARCH_CLUSTERS', WB_ADV_RAW_SEARCH_CLUSTERS_HEADERS_);
  wbAdvBqEnsureTable_('RAW_WB_ADV_COSTS', WB_ADV_RAW_COSTS_HEADERS_);
  console.log('✅ Все 5 RAW_WB_ADV_* таблиц гарантированы.');
}

/**
 * C0 — технический smoke-тест БЕЗ обращения к WB API:
 * включает sink (с preflight), создаёт 5 пустых таблиц и 5 вью, проверяет
 * вью и печатает счётчики. Fail-closed: если ЛЮБОЙ шаг после включения
 * флага упал (частичное создание таблиц/вью), sink автоматически
 * выключается — чтобы загрузчик не писал в недоинициализированный контур.
 * После успеха — C1: loadWbAdsRawPeriod за один завершённый день.
 */
function wbAdsBqInit() {
  try {
    wbAdsBqEnable();           // preflight: self-test + ensure dataset, затем флаг
    wbAdsBqEnsureAllTables_(); // 5 пустых таблиц
    wbAdsBqCreateViews();      // 5 дедуп-вью
    wbAdsBqAssertViews_();     // подтвердить, что все 5 — именно VIEW
    wbAdsBqStats();            // счётчики (все по 0 — норм для C0)
    console.log('✅ C0 готов: таблицы и вью созданы. Дальше C1 — один день.');
  } catch (e) {
    wbAdsBqDisable();          // rollback флага при любой ошибке C0
    console.error('❌ C0 не завершён. Рекламный BQ-sink автоматически ВЫКЛючен: ' +
      String((e && e.message) || e));
    throw e;
  }
}

/** Подтверждает, что все 5 рекламных объектов существуют и являются VIEW. */
function wbAdsBqAssertViews_() {
  var c = getBqConfig_();
  var views = ['V_ADV_CAMPAIGNS', 'V_ADV_CAMPAIGN_STATS', 'V_ADV_BOOSTER_STATS',
    'V_ADV_SEARCH_CLUSTERS', 'V_ADV_COSTS'];
  for (var i = 0; i < views.length; i++) {
    var t = BigQuery.Tables.get(c.projectId, c.datasetId, views[i]);
    if (!t.view) throw new Error(views[i] + ': объект существует, но не является VIEW');
  }
  console.log('✅ Все 5 рекламных вью подтверждены.');
}

/**
 * Дедуп-вью для ВСЕХ 5 RAW-таблиц (№2). append-only RAW при повторных
 * прогонах даёт копии строк — вью оставляют последнюю по load_ts.
 * Порядок сортировки: SAFE_CAST(load_ts AS TIMESTAMP) DESC (устойчиво к
 * формату строки, №6), run_id как тай-брейк.
 */
function wbAdsBqCreateViews() {
  // №1 (повторный аудит): вью падают на отсутствующей RAW-таблице
  // (таблицы создаются лениво загрузчиками). Гарантируем все 5 таблиц.
  wbAdsBqEnsureAllTables_();

  var c = getBqConfig_();
  function fq(t) { return '`' + c.projectId + '.' + c.datasetId + '.' + t + '`'; }

  // Универсальный построитель дедуп-вью.
  // orderPrefix — необязательный SQL-фрагмент приоритета ПЕРЕД load_ts
  // (например, чтобы полноценная строка не вытеснялась маркерной).
  function makeView(viewName, rawTable, partitionExpr, whereClause, orderPrefix) {
    var sql =
      'CREATE OR REPLACE VIEW ' + fq(viewName) + ' AS\n' +
      'SELECT * EXCEPT(_rn) FROM (\n' +
      '  SELECT *, ROW_NUMBER() OVER (\n' +
      '    PARTITION BY ' + partitionExpr + '\n' +
      '    ORDER BY ' + (orderPrefix ? orderPrefix + ',\n      ' : '') +
      'SAFE_CAST(load_ts AS TIMESTAMP) DESC, run_id DESC\n' +
      '  ) AS _rn\n' +
      '  FROM ' + fq(rawTable) + '\n' +
      (whereClause ? '  WHERE ' + whereClause + '\n' : '') +
      ')\nWHERE _rn = 1';
    bqQuery_(sql);
  }

  // 1) Кампании — ключ advertId. Приоритет полноценной строки 'raw' над
  //    'count_only' (№3): при временном сбое /adverts свежий count_only
  //    не должен вытеснять запись с названием/товарами/датами.
  makeView('V_ADV_CAMPAIGNS', 'RAW_WB_ADV_CAMPAIGNS', 'advertId', null,
    "CASE WHEN processed_status = 'raw' THEN 0 WHEN processed_status = 'count_only' THEN 1 ELSE 2 END");

  // 2) fullstats по SKU/дням — ключ включает source_level; только 'raw'
  //    (маркеры no_stats/count_only не пускаем в аналитическую вью, №5).
  makeView('V_ADV_CAMPAIGN_STATS', 'RAW_WB_ADV_CAMPAIGN_STATS',
    '`date`, advertId, nmId, appType, source_level',
    "processed_status = 'raw'");

  // 3) Бустер (поз. в поиске) — ключ date+advertId+nmId.
  makeView('V_ADV_BOOSTER_STATS', 'RAW_WB_ADV_BOOSTER_STATS',
    '`date`, advertId, nmId', null);

  // 4) Search clusters (SAMPLE) — ключ период+связка+ключевой запрос.
  makeView('V_ADV_SEARCH_CLUSTERS', 'RAW_WB_ADV_SEARCH_CLUSTERS',
    'period_from, period_to, advert_id, nm_id, norm_query', null);

  // 5) Расходы upd — дедуп по ХЭШУ raw_json.
  //    ⚠️ C1 (2026-07-12) показал: updNum НЕ уникален — 2 различных
  //    значения на 272 строки (это номер документа, общий для многих
  //    кампаний). Ключ на updNum схлопывал 272 → 2. raw_json даёт 154
  //    различных = фактическую гранулярность операций; одинаковые
  //    перезагрузки → один хэш. updNum как ключ НЕ использовать.
  makeView('V_ADV_COSTS', 'RAW_WB_ADV_COSTS',
    "TO_HEX(SHA256(COALESCE(raw_json, '')))", null);

  console.log('✅ Вью созданы (5): V_ADV_CAMPAIGNS, V_ADV_CAMPAIGN_STATS, ' +
    'V_ADV_BOOSTER_STATS, V_ADV_SEARCH_CLUSTERS, V_ADV_COSTS');
}

/** Сколько строк в каждой рекламной таблице BQ. */
function wbAdsBqStats() {
  var c = getBqConfig_();
  var tabs = ['RAW_WB_ADV_CAMPAIGNS', 'RAW_WB_ADV_CAMPAIGN_STATS', 'RAW_WB_ADV_BOOSTER_STATS',
    'RAW_WB_ADV_SEARCH_CLUSTERS', 'RAW_WB_ADV_COSTS'];
  tabs.forEach(function (t) {
    try {
      var r = bqQuery_('SELECT COUNT(*) AS c FROM `' + c.projectId + '.' + c.datasetId + '.' + t + '`');
      var count = (r && r.rows && r.rows.length) ? r.rows[0].f[0].v : '0';
      console.log(t + ': ' + count);
    } catch (e) {
      var msg = String((e && e.message) || e);
      var notFound = (msg.indexOf('Not found') >= 0) || (msg.indexOf('notFound') >= 0) || (msg.indexOf('404') >= 0);
      if (notFound) console.log(t + ': (таблицы ещё нет)');
      else console.error('❌ ' + t + ': ' + msg); // реальную ошибку не прячем (№8)
    }
  });
}
