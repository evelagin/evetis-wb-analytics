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
  RAW_WB_ADV_COSTS: true,
  // Stage 3B.1: журнал окон-снапшотов расходов (grain run_id × window_index).
  RAW_WB_ADV_COSTS_RUNS: true,
  // Ads-3: снимок ставок по поисковым кластерам + его run-log.
  RAW_WB_ADV_QUERY_BIDS: true,
  RAW_WB_ADV_QUERY_BIDS_RUNS: true,
  // Ads-2: суточные срезы статистики по поисковым запросам + их run-log.
  RAW_WB_ADV_QUERY_STATS: true,
  RAW_WB_ADV_QUERY_STATS_RUNS: true
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
  // Stage 3B.1: журнал ранов расходов — commit-marker каждого окна.
  wbAdvBqEnsureTable_('RAW_WB_ADV_COSTS_RUNS', WB_ADV_RAW_COSTS_RUNS_HEADERS_);
  // Ads-3 (append-only снимок ставок) + его run-log.
  wbAdvBqEnsureTable_('RAW_WB_ADV_QUERY_BIDS', WB_ADV_RAW_QUERY_BIDS_HEADERS_);
  wbAdvBqEnsureTable_('RAW_WB_ADV_QUERY_BIDS_RUNS', WB_ADV_RAW_QUERY_BIDS_RUNS_HEADERS_);
  // Ads-2 (append-only суточные срезы query-level статистики) + их run-log.
  wbAdvBqEnsureTable_('RAW_WB_ADV_QUERY_STATS', WB_ADV_RAW_QUERY_STATS_HEADERS_);
  wbAdvBqEnsureTable_('RAW_WB_ADV_QUERY_STATS_RUNS', WB_ADV_RAW_QUERY_STATS_RUNS_HEADERS_);
  console.log('✅ Все 10 RAW_WB_ADV_* таблиц гарантированы.');
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
    'V_ADV_SEARCH_CLUSTERS', 'V_ADV_COSTS', 'V_ADV_QUERY_BIDS',
    'V_ADV_QUERY_STATS', 'V_ADV_QUERY_STATS_COVERAGE'];
  for (var i = 0; i < views.length; i++) {
    var t = BigQuery.Tables.get(c.projectId, c.datasetId, views[i]);
    if (!t.view) throw new Error(views[i] + ': объект существует, но не является VIEW');
  }
  console.log('✅ Все 8 рекламных вью подтверждены.');
}

/**
 * Дедуп-вью для RAW-таблиц (№2). append-only RAW при повторных
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

  // 5) Расходы upd — дедуп по БИЗНЕС-КЛЮЧУ списания (advertId, updTime, updSum).
  //    🔴 Прежний ключ TO_HEX(SHA256(raw_json)) НЕ РАБОТАЛ и молча раздувал расход.
  //    Причина: raw_json содержит поля, которые к самому списанию не относятся и
  //    меняются между загрузками, — `updNum` (WB присваивает номер документа
  //    позже: сначала 0, потом настоящий) и `advertStatus` (статус КАМПАНИИ на
  //    момент загрузки, 9 ↔ 11). Скользящее окно 7 дней перезагружает один и тот
  //    же день до семи раз; стоит статусу измениться или номеру появиться — хэш
  //    другой, и то же самое списание попадает во вью ещё раз.
  //    Замер 14.08.2026: RAW 4 478 строк → вью 2 484 вместо 1 938; расход завышен
  //    на 123 915 ₽ (июль +102 157, +88%; август +21 758, +77%). Апрель–июнь
  //    совпали до рубля — там номера уже были присвоены, а статусы не менялись,
  //    поэтому дефект и не был виден раньше.
  //    ⚠️ Урок C1 (2026-07-12) остаётся в силе: `updNum` САМ ПО СЕБЕ ключом быть
  //    не может — это номер документа, общий для многих кампаний (2 значения на
  //    272 строки). Но из этого не следовало, что ключом должен стать весь
  //    raw_json: правильный ответ — три поля, идентифицирующие именно платёж.
  //    Безопасность ключа проверена эмпирически: групп с двумя строками одного
  //    (advertId, updTime, updSum) ВНУТРИ одного run_id — 0. В одном ответе API
  //    такая пара не встречается никогда, схлопывать нечего; все дубли —
  //    межпрогонные, то есть именно перезагрузки.
  //    ORDER BY у makeView (load_ts DESC, run_id DESC) оставляет САМУЮ СВЕЖУЮ
  //    строку — с уже присвоенным updNum и актуальным статусом кампании.
  makeView('V_ADV_COSTS', 'RAW_WB_ADV_COSTS',
    'advertId, updTime, updSum', null);

  // 6) Ads-3: ставки по кластерам — APPEND-ONLY ИСТОРИЯ.
  //    🔴 makeView здесь НЕ ПОДХОДИТ ПРИНЦИПИАЛЬНО, и дело не только в ключе.
  //    Дедуп по (snapshot_date, advert_id, nm_id, norm_query) канонизирует КАЖДЫЙ
  //    запрос НЕЗАВИСИМО. Если во втором прогоне дня ставка по ключу снята, строки
  //    для него во втором снимке нет — и вью подставит строку из ПЕРВОГО снимка.
  //    Получится гибрид: набор ставок, которого не существовало ни в один момент
  //    времени. Для таблицы снимков это тихая порча данных.
  //    Канонизация обязана быть НА УРОВНЕ СНИМКА: выбрать один снимок за день —
  //    ключ (snapshot_date, snapshot_ts, run_id), а не один snapshot_ts, потому что
  //    ts имеет точность до секунды и два execution могут его разделить, — и
  //    показать строки ТОЛЬКО этого снимка целиком.
  //    Выбор снимка дня: сначала успешные и полные (OK), затем неполные (PARTIAL);
  //    внутри группы — самый поздний, тай-брейк по run_id. Статус выбранного снимка
  //    отдаётся колонкой snapshot_status, чтобы потребитель видел, полон ли день.
  wbAdsBqCreateQueryBidsView_(fq);

  // 7) Ads-2: суточные СРЕЗЫ query-level статистики — тоже append-only.
  //    🔴 makeView здесь неприменим по той же причине, что и в п.6, но по другому
  //    поводу: probe v5 показал, что два обращения за ОДИН И ТОТ ЖЕ период вернули
  //    разное число строк (61 против 44) при идентичных агрегатах. Причина не
  //    установлена и здесь не постулируется. Построчный дедуп склеил бы две
  //    retrieval-версии в набор запросов, не существовавший ни в одном ответе API.
  //    Канонизация — на уровне СРЕЗА целиком, ключ (period_from, period_to, run_id).
  wbAdsBqCreateQueryStatsView_(fq);

  // 8) Ads-2: витрина НЕопубликованного. Нужна потому, что п.7 публикует только OK:
  //    без неё непокрытые сутки превратились бы в тихую дыру.
  wbAdsBqCreateQueryStatsCoverageView_(fq);

  console.log('✅ Вью созданы (8): V_ADV_CAMPAIGNS, V_ADV_CAMPAIGN_STATS, ' +
    'V_ADV_BOOSTER_STATS, V_ADV_SEARCH_CLUSTERS, V_ADV_COSTS, V_ADV_QUERY_BIDS, ' +
    'V_ADV_QUERY_STATS, V_ADV_QUERY_STATS_COVERAGE');
}

/**
 * V_ADV_QUERY_STATS — канонический СРЕЗ query-level статистики за сутки (Ads-2).
 *
 * 🔴 ПУБЛИКУЕТСЯ ТОЛЬКО status = 'OK'. PARTIAL и FAILED остаются в RAW и в run-log
 *    как диагностический материал, но официальной версией периода не становятся
 *    никогда. Причина: PARTIAL означает, что часть пар или пачек не доехала, и
 *    опубликовать его значит отдать заниженные цифры под видом полных — причём
 *    молча, потому что по отсутствующим строкам недостача не видна. Отсутствие
 *    периода заметно, тихая недостача — нет. Fail-closed.
 *
 * 🔴 Ключ каноники = (period_from, period_to, run_id). Один прогон на период,
 *    строки только этого прогона. run_id уникален на execution и потому является
 *    настоящим идентификатором retrieval-версии; load_ts несёт лишь время.
 *
 * Внутренний дедуп идёт ВНУТРИ выбранного run_id: схлопывать строки между
 * прогонами нельзя, это пересекло бы границу среза.
 *
 * slice_status оставлен в выдаче, хотя сейчас всегда равен 'OK': потребитель не
 * должен гадать, фильтровано ли представление, а будущее расширение набора
 * публикуемых статусов обязано быть видимым изменением, а не молчаливым.
 */
function wbAdsBqCreateQueryStatsView_(fq) {
  var sql =
    'CREATE OR REPLACE VIEW ' + fq('V_ADV_QUERY_STATS') + ' AS\n' +
    'WITH canon AS (\n' +
    '  SELECT period_from, period_to, run_id, status AS slice_status\n' +
    '  FROM (\n' +
    '    SELECT period_from, period_to, run_id, status,\n' +
    '           ROW_NUMBER() OVER (\n' +
    '             PARTITION BY period_from, period_to\n' +
    '             ORDER BY SAFE_CAST(load_ts AS TIMESTAMP) DESC,\n' +
    '                      run_id DESC\n' +
    '           ) AS _rn\n' +
    '    FROM ' + fq('RAW_WB_ADV_QUERY_STATS_RUNS') + '\n' +
    "    WHERE http_success = 'TRUE' AND status = 'OK'\n" +
    '  )\n' +
    '  WHERE _rn = 1\n' +
    ')\n' +
    'SELECT * EXCEPT(_dup) FROM (\n' +
    '  SELECT s.*, c.slice_status,\n' +
    '         ROW_NUMBER() OVER (\n' +
    '           PARTITION BY s.run_id, s.period_from, s.period_to,\n' +
    '                        s.advert_id, s.nm_id, s.norm_query\n' +
    '           ORDER BY s.load_ts DESC\n' +
    '         ) AS _dup\n' +
    '  FROM ' + fq('RAW_WB_ADV_QUERY_STATS') + ' s\n' +
    '  JOIN canon c\n' +
    '    ON s.period_from = c.period_from\n' +
    '   AND s.period_to   = c.period_to\n' +
    '   AND s.run_id      = c.run_id\n' +
    "  WHERE s.processed_status = 'raw'\n" +
    ')\n' +
    'WHERE _dup = 1';
  bqQuery_(sql);
}

/**
 * V_ADV_QUERY_STATS_COVERAGE — что НЕ опубликовано и почему (Ads-2, спека §4.4).
 *
 * Вселенная суток берётся из V_ADV_CAMPAIGN_STATS, а НЕ из run-log. Иначе сутки,
 * которые вообще ни разу не запрашивались, не попали бы в отчёт — а это ровно тот
 * случай, который надо видеть в первую очередь: не «попробовали и не вышло», а
 * «не пробовали». Отсюда LEFT JOIN: строки без попыток остаются с нулями.
 *
 * coverage_ratio = scope_spend_rub / day_spend_costs_rub — какую долю реально
 * потраченных за сутки денег мы вообще пытались объяснить.
 * ⚠️ Осмысленно только после исправления ключа дедупа V_ADV_COSTS: до него
 *    знаменатель завышен (см. docs/ADS_COSTS_DEDUP_FIX_2026-08-14.md).
 */
function wbAdsBqCreateQueryStatsCoverageView_(fq) {
  var sql =
    'CREATE OR REPLACE VIEW ' + fq('V_ADV_QUERY_STATS_COVERAGE') + ' AS\n' +
    'WITH days AS (\n' +
    "  SELECT DISTINCT FORMAT_DATE('%Y-%m-%d', DATE(`date`)) AS period_from\n" +
    '  FROM ' + fq('V_ADV_CAMPAIGN_STATS') + '\n' +
    '  WHERE `date` IS NOT NULL\n' +
    '),\n' +
    'agg AS (\n' +
    '  SELECT period_from,\n' +
    "         COUNTIF(status = 'OK')      AS ok_runs,\n" +
    "         COUNTIF(status = 'PARTIAL') AS partial_runs,\n" +
    "         COUNTIF(status = 'FAILED')  AS failed_runs,\n" +
    "         COUNTIF(status = 'EMPTY')   AS empty_runs,\n" +
    '         COUNT(*)                    AS total_attempts,\n' +
    '         MAX(SAFE_CAST(load_ts AS TIMESTAMP)) AS last_attempt_ts\n' +
    '  FROM ' + fq('RAW_WB_ADV_QUERY_STATS_RUNS') + '\n' +
    '  GROUP BY period_from\n' +
    '),\n' +
    'last_try AS (\n' +
    '  SELECT period_from, status AS last_status, error_message AS last_error,\n' +
    '         SAFE_CAST(scope_pairs AS INT64)          AS scope_pairs,\n' +
    '         SAFE_CAST(scope_spend_rub AS FLOAT64)    AS scope_spend_rub,\n' +
    '         SAFE_CAST(day_spend_costs_rub AS FLOAT64) AS day_spend_costs_rub\n' +
    '  FROM (\n' +
    '    SELECT *, ROW_NUMBER() OVER (\n' +
    '      PARTITION BY period_from\n' +
    '      ORDER BY SAFE_CAST(load_ts AS TIMESTAMP) DESC, run_id DESC\n' +
    '    ) AS _rn\n' +
    '    FROM ' + fq('RAW_WB_ADV_QUERY_STATS_RUNS') + '\n' +
    '  )\n' +
    '  WHERE _rn = 1\n' +
    ')\n' +
    'SELECT d.period_from,\n' +
    '       COALESCE(a.ok_runs, 0) > 0 AS is_published,\n' +
    '       COALESCE(a.ok_runs, 0)        AS ok_runs,\n' +
    '       COALESCE(a.partial_runs, 0)   AS partial_runs,\n' +
    '       COALESCE(a.failed_runs, 0)    AS failed_runs,\n' +
    '       COALESCE(a.empty_runs, 0)     AS empty_runs,\n' +
    '       COALESCE(a.total_attempts, 0) AS total_attempts,\n' +
    '       a.last_attempt_ts,\n' +
    '       l.last_status, l.last_error,\n' +
    '       l.scope_pairs, l.scope_spend_rub, l.day_spend_costs_rub,\n' +
    '       SAFE_DIVIDE(l.scope_spend_rub, l.day_spend_costs_rub) AS coverage_ratio\n' +
    'FROM days d\n' +
    'LEFT JOIN agg a      USING (period_from)\n' +
    'LEFT JOIN last_try l USING (period_from)';
  bqQuery_(sql);
}

/**
 * V_ADV_QUERY_BIDS — канонический СНИМОК ставок за день (Ads-3).
 * Не дедуп-вью: выбирается ОДИН снимок на дату, и берутся строки только его.
 * Источник выбора — RAW_WB_ADV_QUERY_BIDS_RUNS (run-log): полагаться на сами
 * строки данных нельзя, потому что у неуспешного прогона их может не быть вовсе.
 *
 * 🔴 ИДЕНТИЧНОСТЬ СНИМКА = (snapshot_date, snapshot_ts, run_id), НЕ один snapshot_ts.
 *    snapshot_ts имеет точность до секунды. Два execution, стартовавших в одну и ту
 *    же секунду (ручной запуск поверх триггера; параллельный retry; повтор после
 *    таймаута), получат РАЗНЫЕ run_id, но ОДИНАКОВЫЙ snapshot_ts — и JOIN только по
 *    ts снова склеил бы два независимых снимка в один гибрид, ровно тот дефект,
 *    ради которого канонизация и делалась. run_id уникален на execution, поэтому
 *    он и есть настоящий ключ снимка; ts остаётся в ключе как носитель времени.
 *    По той же причине run_id входит и во внутренний дедуп: схлопывать дубли строк
 *    можно ТОЛЬКО внутри одного прогона, иначе дедуп пересекает границу снимка.
 *    run_id DESC в ORDER BY канона — детерминированный тай-брейк при равных
 *    (status, snapshot_ts): без него выбор снимка был бы недетерминирован.
 */
function wbAdsBqCreateQueryBidsView_(fq) {
  var sql =
    'CREATE OR REPLACE VIEW ' + fq('V_ADV_QUERY_BIDS') + ' AS\n' +
    'WITH canon AS (\n' +
    '  SELECT snapshot_date, snapshot_ts, run_id, status AS snapshot_status\n' +
    '  FROM (\n' +
    '    SELECT snapshot_date, snapshot_ts, run_id, status,\n' +
    '           ROW_NUMBER() OVER (\n' +
    '             PARTITION BY snapshot_date\n' +
    "             ORDER BY CASE status WHEN 'OK' THEN 0 WHEN 'PARTIAL' THEN 1 ELSE 2 END,\n" +
    '                      SAFE_CAST(snapshot_ts AS TIMESTAMP) DESC,\n' +
    '                      run_id DESC\n' +
    '           ) AS _rn\n' +
    '    FROM ' + fq('RAW_WB_ADV_QUERY_BIDS_RUNS') + '\n' +
    "    WHERE http_success = 'TRUE' AND status IN ('OK', 'PARTIAL')\n" +
    '  )\n' +
    '  WHERE _rn = 1\n' +
    ')\n' +
    'SELECT * EXCEPT(_dup) FROM (\n' +
    '  SELECT b.*, c.snapshot_status,\n' +
    '         ROW_NUMBER() OVER (\n' +
    '           PARTITION BY b.run_id, b.snapshot_ts, b.advert_id, b.nm_id, b.norm_query\n' +
    '           ORDER BY b.load_ts DESC\n' +
    '         ) AS _dup\n' +
    '  FROM ' + fq('RAW_WB_ADV_QUERY_BIDS') + ' b\n' +
    '  JOIN canon c\n' +
    '    ON b.snapshot_date = c.snapshot_date\n' +
    '   AND b.snapshot_ts   = c.snapshot_ts\n' +
    '   AND b.run_id        = c.run_id\n' +
    "  WHERE b.processed_status = 'raw'\n" +
    ')\n' +
    'WHERE _dup = 1';
  bqQuery_(sql);
}

/** Сколько строк в каждой рекламной таблице BQ. */
function wbAdsBqStats() {
  var c = getBqConfig_();
  var tabs = ['RAW_WB_ADV_CAMPAIGNS', 'RAW_WB_ADV_CAMPAIGN_STATS', 'RAW_WB_ADV_BOOSTER_STATS',
    'RAW_WB_ADV_SEARCH_CLUSTERS', 'RAW_WB_ADV_COSTS',
    'RAW_WB_ADV_QUERY_BIDS', 'RAW_WB_ADV_QUERY_BIDS_RUNS',
    'RAW_WB_ADV_QUERY_STATS', 'RAW_WB_ADV_QUERY_STATS_RUNS'];
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
