/**
 * ══════════════════════════════════════════════════════════════
 * EVETIS WB — WbSuppliesRawLoader.gs
 *
 * Загрузчик RAW-слоя поставок WB по проекту `docs/wb_supplies_raw_design.md`.
 * Источник — `supplies-api`, категория токена «Поставки» (`WB_TOKEN_SUPPLIES`).
 *
 * ЧТО ДЕЛАЕТ
 *   wbSuppliesEnsureTables()      — DDL: создаёт две таблицы. 🔴 DDL.
 *   runWbSuppliesBackfill()       — бэкфилл порциями с курсором. 🔴 DML.
 *   runWbSuppliesDaily()          — суточный инкремент. 🔴 DML.
 *   wbSuppliesCheckInvariants()   — И1…И10, только чтение.
 *   wbSuppliesShowState()         — состояние курсора, только чтение.
 *   wbSuppliesResetBackfill()     — сброс курсора, ничего не удаляет.
 *
 * 🔴 КОНТРАКТ ЗАПИСИ ОДНОГО ОБЪЕКТА (§7.2 проекта)
 *
 *     DELETE (run_id, entity_key) из обеих таблиц
 *       → LOAD goods
 *         → LOAD header   ← COMMIT объекта
 *           → сдвиг курсора
 *
 * Шапка пишется ПОСЛЕДНЕЙ и работает как commit-marker: канонический слой
 * читает позиции только того `run_id`, который выбран шапкой. Пока шапки нет,
 * позиции этого прогона вьюхами не видны. Ни один отказ не оставляет объект,
 * собранный из двух снимков, и ни один не создаёт дубль внутри `run_id`.
 *
 * 🔴 ОТЛИЧИЕ ОТ ПРОЕКТА, СОЗНАТЕЛЬНОЕ: курсор хранится НЕ как индекс в списке,
 * а как ключ сортировки последнего обработанного объекта `create_dt|entity_key`.
 * Индекс небезопасен: список перечитывается каждой порцией, и появление новой
 * заявки в середине бэкфилла сдвинуло бы индексы — часть объектов была бы
 * пропущена. Ключ сортировки устойчив к вставкам: новый объект имеет свежий
 * `createDate` и попадает в конец, то есть после курсора. См. §7.2.
 *
 * БЕЗОПАСНОСТЬ
 *   • токен в журнал не печатается — печатается имя свойства;
 *   • DDL и DML выполняются только явным запуском владельцем;
 *   • в листы книги не пишем — она у лимита ячеек.
 * ══════════════════════════════════════════════════════════════
 */

var WBS_HOST_        = 'https://supplies-api.wildberries.ru';
var WBS_TOKEN_KEYS_  = ['WB_TOKEN_SUPPLIES'];

var WBS_TBL_HEADER_  = 'RAW_WB_SUPPLIES';
var WBS_TBL_GOODS_   = 'RAW_WB_SUPPLIES_GOODS';

var WBS_PROP_RUN_    = 'WB_SUPPLIES_BACKFILL_RUN_ID';
var WBS_PROP_CURSOR_ = 'WB_SUPPLIES_BACKFILL_CURSOR';
var WBS_PROP_START_  = 'WB_SUPPLIES_BACKFILL_STARTED_AT';

var WBS_STATUS_DONE_ = 5;
var WBS_PAUSE_MS_    = 900;
var WBS_MAX_ATTEMPTS_= 3;
var WBS_TIME_BUDGET_ = 240000;   // 4 минуты на порцию, запас до лимита 6 мин
var WBS_OVERLAP_DAYS_= 14;       // §7.3 скользящее перекрытие
var WBS_TZ_          = 'Europe/Moscow';

// ── утилиты ───────────────────────────────────────────────────

function wbsLog_(s) { Logger.log(s); }

function wbsToken_() {
  var sp = PropertiesService.getScriptProperties();
  for (var i = 0; i < WBS_TOKEN_KEYS_.length; i++) {
    var v = String(sp.getProperty(WBS_TOKEN_KEYS_[i]) || '').trim();
    if (v) return { token: v, keyName: WBS_TOKEN_KEYS_[i] };
  }
  throw new Error('Нет токена: ' + WBS_TOKEN_KEYS_.join(' / '));
}

function wbsFetch_(method, url, token, payload) {
  for (var attempt = 0; attempt < WBS_MAX_ATTEMPTS_; attempt++) {
    var opt = { method: method, muteHttpExceptions: true, headers: { 'Authorization': token } };
    if (payload !== undefined && payload !== null) {
      opt.contentType = 'application/json';
      opt.payload = JSON.stringify(payload);
    }
    var code = -1, body = '';
    try {
      var resp = UrlFetchApp.fetch(url, opt);
      code = resp.getResponseCode();
      body = resp.getContentText();
    } catch (e) {
      if (attempt < WBS_MAX_ATTEMPTS_ - 1) { Utilities.sleep(4000 * (attempt + 1)); continue; }
      throw new Error('Сеть: ' + method + ' ' + url + ' → ' + e);
    }
    if ((code === 429 || code >= 500) && attempt < WBS_MAX_ATTEMPTS_ - 1) {
      Utilities.sleep(4000 * (attempt + 1));
      continue;
    }
    if (code !== 200 && code !== 204) {
      throw new Error('HTTP ' + code + ' ' + method + ' ' + url + ' :: ' +
                      String(body).substring(0, 200).replace(/\s+/g, ' '));
    }
    var json = null;
    try { json = JSON.parse(body); } catch (e2) { json = null; }
    return { code: code, json: json, body: body };
  }
  throw new Error('Не удалось выполнить ' + method + ' ' + url);
}

function wbsRows_(json) {
  if (json instanceof Array) return json;
  if (json && json.data instanceof Array) return json.data;
  return null;
}

function wbsInt_(v) {
  if (v === null || v === undefined || v === '') return null;
  var n = Number(v);
  return isNaN(n) ? null : Math.round(n);
}

function wbsNum_(v) {
  if (v === null || v === undefined || v === '') return null;
  var n = Number(v);
  return isNaN(n) ? null : n;
}

function wbsStr_(v) {
  if (v === null || v === undefined) return null;
  var s = String(v);
  return s === '' ? null : s;
}

function wbsBool_(v) { return (v === true || v === 'true'); }

/** WB отдаёт RFC3339 с офсетом. BigQuery TIMESTAMP это принимает как есть. */
function wbsTs_(v) {
  var s = wbsStr_(v);
  return s ? s : null;
}

function wbsMd5_(s) {
  var bytes = Utilities.computeDigest(Utilities.DigestAlgorithm.MD5, String(s), Utilities.Charset.UTF_8);
  var out = '';
  for (var i = 0; i < bytes.length; i++) {
    var b = (bytes[i] < 0 ? bytes[i] + 256 : bytes[i]).toString(16);
    out += (b.length === 1 ? '0' : '') + b;
  }
  return out;
}

function wbsSqlLit_(s) {
  return "'" + String(s).replace(/\\/g, '\\\\').replace(/'/g, "\\'") + "'";
}

/** 🔴 Ключ объекта. Префикс обязателен: без него supply_id и preorder_id столкнутся. */
function wbsEntityKey_(o) {
  var pre = wbsInt_(o.preorderID), sup = wbsInt_(o.supplyID);
  if (pre !== null && pre !== 0) return 'P:' + pre;
  if (sup !== null && sup !== 0) return 'S:' + sup;
  return null;   // объект без обоих идентификаторов — пропускаем и логируем
}

/** Ключ сортировки, устойчивый к вставкам новых объектов между порциями. */
function wbsSortKey_(o, entityKey) {
  return String(o.createDate || '') + '|' + entityKey;
}

function wbsIsCompleted_(o) {
  return Number(o.statusID) === WBS_STATUS_DONE_ && !!wbsStr_(o.factDate);
}

// ── DDL ───────────────────────────────────────────────────────

function wbsHeaderSchema_() {
  return [
    { name: 'snapshot_ts', type: 'TIMESTAMP', mode: 'REQUIRED' },
    { name: 'run_id',      type: 'STRING',    mode: 'REQUIRED' },
    { name: 'entity_key',  type: 'STRING',    mode: 'REQUIRED' },
    { name: 'supply_id',   type: 'INTEGER' },
    { name: 'preorder_id', type: 'INTEGER' },
    { name: 'status_id',   type: 'INTEGER' },
    { name: 'create_dt',   type: 'TIMESTAMP' },
    { name: 'supply_dt',   type: 'TIMESTAMP' },
    { name: 'fact_dt',     type: 'TIMESTAMP' },
    { name: 'updated_dt',  type: 'TIMESTAMP' },
    { name: 'box_type_id', type: 'INTEGER' },
    { name: 'is_box_on_pallet', type: 'BOOLEAN' },
    { name: 'warehouse_id',   type: 'INTEGER' },
    { name: 'warehouse_name', type: 'STRING' },
    { name: 'transit_warehouse_id',   type: 'INTEGER' },
    { name: 'transit_warehouse_name', type: 'STRING' },
    { name: 'actual_warehouse_id',    type: 'INTEGER' },
    { name: 'actual_warehouse_name',  type: 'STRING' },
    { name: 'acceptance_cost', type: 'NUMERIC' },
    { name: 'paid_acceptance_coefficient', type: 'NUMERIC' },
    { name: 'storage_coef',  type: 'NUMERIC' },
    { name: 'delivery_coef', type: 'NUMERIC' },
    { name: 'reject_reason', type: 'STRING' },
    { name: 'supplier_assign_name', type: 'STRING' },
    { name: 'card_quantity',           type: 'INTEGER' },
    { name: 'card_accepted_quantity',  type: 'INTEGER' },
    { name: 'card_ready_quantity',     type: 'INTEGER' },
    { name: 'card_unloading_quantity', type: 'INTEGER' },
    { name: 'depersonalized_quantity', type: 'INTEGER' },
    { name: 'can_show_quantity', type: 'BOOLEAN' },
    { name: 'is_completed',  type: 'BOOLEAN' },
    { name: 'card_read_ok',  type: 'BOOLEAN' },
    { name: 'raw_json',      type: 'STRING' },
    { name: 'row_hash',      type: 'STRING' }
  ];
}

function wbsGoodsSchema_() {
  return [
    { name: 'snapshot_ts', type: 'TIMESTAMP', mode: 'REQUIRED' },
    { name: 'run_id',      type: 'STRING',    mode: 'REQUIRED' },
    { name: 'entity_key',  type: 'STRING',    mode: 'REQUIRED' },
    { name: 'supply_id',   type: 'INTEGER' },
    { name: 'barcode',     type: 'STRING' },
    { name: 'vendor_code', type: 'STRING' },
    { name: 'nm_id',       type: 'INTEGER' },
    { name: 'tech_size',   type: 'STRING' },
    { name: 'color',       type: 'STRING' },
    { name: 'tnved',       type: 'STRING' },
    { name: 'need_kiz',    type: 'BOOLEAN' },
    { name: 'supplier_box_amount', type: 'INTEGER' },
    { name: 'quantity',                type: 'INTEGER' },
    { name: 'accepted_quantity',       type: 'INTEGER' },
    { name: 'ready_for_sale_quantity', type: 'INTEGER' },
    { name: 'unloading_quantity',      type: 'INTEGER' },
    { name: 'accepted_quantity_zero',  type: 'BOOLEAN' },
    { name: 'acceptance_resolution',   type: 'STRING' },
    { name: 'raw_json', type: 'STRING' },
    { name: 'row_hash', type: 'STRING' }
  ];
}

/** 🔴 DDL. Создаёт таблицы, если их нет. Существующие не трогает. */
function wbSuppliesEnsureTables() {
  var c = getBqConfig_();
  bqEnsureDataset_();
  var defs = [
    { id: WBS_TBL_HEADER_, schema: wbsHeaderSchema_(), part: 'snapshot_ts' },
    { id: WBS_TBL_GOODS_,  schema: wbsGoodsSchema_(),  part: 'snapshot_ts' }
  ];
  for (var i = 0; i < defs.length; i++) {
    var d = defs[i], exists = true;
    try { BigQuery.Tables.get(c.projectId, c.datasetId, d.id); }
    catch (e) { exists = false; }
    if (exists) { wbsLog_('таблица уже есть: ' + d.id); continue; }
    BigQuery.Tables.insert({
      tableReference: { projectId: c.projectId, datasetId: c.datasetId, tableId: d.id },
      schema: { fields: d.schema },
      timePartitioning: { type: 'DAY', field: d.part }
    }, c.projectId, c.datasetId);
    wbsLog_('создана таблица: ' + d.id);
  }
  wbsLog_('DDL готов. Вьюхи создаются отдельно по §5 проекта.');
}

// ── чтение API ────────────────────────────────────────────────

function wbsList_(token) {
  var limit = 1000;
  var r = wbsFetch_('post', WBS_HOST_ + '/api/v1/supplies?limit=' + limit + '&offset=0', token, {});
  var rows = wbsRows_(r.json);
  if (!rows) throw new Error('Список поставок не разобрался');
  if (rows.length >= limit) {
    throw new Error('🔴 Выборка усечена на ' + limit + ' — нужен проход по offset, загрузка остановлена');
  }
  return rows;
}

function wbsCard_(token, supplyId) {
  var r = wbsFetch_('get', WBS_HOST_ + '/api/v1/supplies/' + supplyId, token);
  return r.json || null;
}

function wbsGoods_(token, supplyId) {
  var limit = 1000;
  var r = wbsFetch_('get', WBS_HOST_ + '/api/v1/supplies/' + supplyId + '/goods?limit=' + limit + '&offset=0', token);
  var rows = wbsRows_(r.json);
  // 🔴 Неразобранный ответ НЕ превращаем в пустой состав. Иначе дрейф обёртки
  // JSON при HTTP 200 дал бы «0 позиций», commit-шапку и исчезновение прежних
  // позиций объекта из канонического слоя — без единой ошибки в журнале.
  // Настоящий пустой состав приходит как [] и сюда не попадает: [] — truthy.
  if (!rows) {
    throw new Error('🔴 Состав поставки ' + supplyId + ' не разобрался как массив — ' +
                    'структура ответа изменилась, загрузка остановлена');
  }
  if (rows.length >= limit) {
    throw new Error('🔴 Состав поставки ' + supplyId + ' усечён на ' + limit + ' — загрузка остановлена');
  }
  return rows;
}

// ── построение строк ──────────────────────────────────────────

function wbsHeaderRow_(o, card, entityKey, runId, snapshotTs) {
  var row = {
    snapshot_ts: snapshotTs,
    run_id: runId,
    entity_key: entityKey,
    supply_id:   wbsInt_(o.supplyID),
    preorder_id: wbsInt_(o.preorderID),
    status_id:   wbsInt_(o.statusID),
    create_dt:   wbsTs_(o.createDate),
    supply_dt:   wbsTs_(o.supplyDate),
    fact_dt:     wbsTs_(o.factDate),
    updated_dt:  wbsTs_(o.updatedDate),
    box_type_id: wbsInt_(o.boxTypeID),
    is_box_on_pallet: wbsBool_(o.isBoxOnPallet),
    warehouse_id: null, warehouse_name: null,
    transit_warehouse_id: null, transit_warehouse_name: null,
    actual_warehouse_id: null,  actual_warehouse_name: null,
    acceptance_cost: null, paid_acceptance_coefficient: null,
    storage_coef: null, delivery_coef: null,
    reject_reason: null, supplier_assign_name: null,
    card_quantity: null, card_accepted_quantity: null,
    card_ready_quantity: null, card_unloading_quantity: null,
    depersonalized_quantity: null, can_show_quantity: null,
    is_completed: wbsIsCompleted_(o),
    card_read_ok: !!card,
    raw_json: JSON.stringify({ list: o, card: card || null })
  };

  if (card) {
    row.warehouse_id   = wbsInt_(card.warehouseID);
    row.warehouse_name = wbsStr_(card.warehouseName);
    row.transit_warehouse_id   = wbsInt_(card.transitWarehouseID);
    row.transit_warehouse_name = wbsStr_(card.transitWarehouseName);
    row.actual_warehouse_id    = wbsInt_(card.actualWarehouseID);
    row.actual_warehouse_name  = wbsStr_(card.actualWarehouseName);
    row.acceptance_cost = wbsNum_(card.acceptanceCost);
    row.paid_acceptance_coefficient = wbsNum_(card.paidAcceptanceCoefficient);
    row.storage_coef  = wbsNum_(card.storageCoef);    // приходит строкой
    row.delivery_coef = wbsNum_(card.deliveryCoef);
    row.reject_reason = wbsStr_(card.rejectReason);
    row.supplier_assign_name = wbsStr_(card.supplierAssignName);
    row.card_quantity           = wbsInt_(card.quantity);
    row.card_accepted_quantity  = wbsInt_(card.acceptedQuantity);
    row.card_ready_quantity     = wbsInt_(card.readyForSaleQuantity);
    row.card_unloading_quantity = wbsInt_(card.unloadingQuantity);
    row.depersonalized_quantity = wbsInt_(card.depersonalizedQuantity);
    row.can_show_quantity = wbsBool_(card.canShowQuantity);
  }

  row.row_hash = wbsMd5_([
    row.entity_key, row.supply_id, row.preorder_id, row.status_id,
    row.create_dt, row.supply_dt, row.fact_dt, row.updated_dt,
    row.warehouse_id, row.transit_warehouse_id, row.actual_warehouse_id,
    row.card_quantity, row.card_accepted_quantity, row.card_ready_quantity,
    row.card_unloading_quantity, row.depersonalized_quantity
  ].join('|'));

  return row;
}

function wbsGoodsRow_(g, o, entityKey, runId, snapshotTs) {
  var qty = wbsInt_(g.quantity), acc = wbsInt_(g.acceptedQuantity);
  var row = {
    snapshot_ts: snapshotTs,
    run_id: runId,
    entity_key: entityKey,
    supply_id: wbsInt_(o.supplyID),
    barcode:     wbsStr_(g.barcode),
    vendor_code: wbsStr_(g.vendorCode),
    nm_id:       wbsInt_(g.nmID),
    tech_size:   wbsStr_(g.techSize),
    color:       wbsStr_(g.color),
    tnved:       wbsStr_(g.tnved),
    need_kiz:    wbsBool_(g.needKiz),
    supplier_box_amount: wbsInt_(g.supplierBoxAmount),
    quantity: qty,
    accepted_quantity: acc,
    ready_for_sale_quantity: wbsInt_(g.readyForSaleQuantity),
    unloading_quantity: wbsInt_(g.unloadingQuantity),
    // 🔴 Наблюдение, не интерпретация: ноль НЕ означает «не принято». §13.1.
    accepted_quantity_zero: (acc === 0 && qty !== null && qty > 0),
    acceptance_resolution: null,   // заполняется справочником, не из API
    raw_json: JSON.stringify(g)
  };
  row.row_hash = wbsMd5_([
    row.entity_key, row.barcode, row.nm_id, row.quantity,
    row.accepted_quantity, row.ready_for_sale_quantity, row.unloading_quantity
  ].join('|'));
  return row;
}

// ── запись одного объекта: DELETE → goods → header → (курсор снаружи) ──

/**
 * 🔴 DML с ГАРАНТИРОВАННЫМ ожиданием завершения job.
 *
 * Штатный `bqQuery_()` для этого не годится: он вызывает `BigQuery.Jobs.query`
 * и сразу возвращает строки, не проверяя `jobComplete`. Для чтения это
 * безобидно, а для нашего протокола — нет: если `DELETE` ещё выполняется, когда
 * начался `LOAD goods`, поздно завершившийся `DELETE` снесёт только что
 * загруженные строки того же `(run_id, entity_key)`. Тихая потеря данных.
 *
 * Поэтому здесь свой helper: Jobs.insert + опрос до DONE. Штатный `bqQuery_()`
 * не трогаем, чтобы не задеть остальной production.
 */
function wbsExecDml_(sql) {
  var c = getBqConfig_();
  var ins = BigQuery.Jobs.insert({
    jobReference: { projectId: c.projectId, location: c.location },
    configuration: { query: { query: sql, useLegacySql: false } }
  }, c.projectId);

  var jobId = ins.jobReference.jobId;
  var loc = ins.jobReference.location || c.location;

  var state = '', tries = 0;
  do {
    Utilities.sleep(1000);
    var j = BigQuery.Jobs.get(c.projectId, jobId, { location: loc });
    state = j.status.state;
    if (j.status.errorResult) {
      throw new Error('BQ DML error: ' + JSON.stringify(j.status.errorResult) + ' :: ' + sql);
    }
    tries++;
  } while (state !== 'DONE' && tries < 120);

  if (state !== 'DONE') throw new Error('BQ DML не завершился за отведённое время :: ' + sql);
  return true;
}

function wbsDeleteObject_(runId, entityKey) {
  var c = getBqConfig_();
  var where = " WHERE run_id = " + wbsSqlLit_(runId) + " AND entity_key = " + wbsSqlLit_(entityKey);
  // 🔴 Оба DELETE ждут DONE. Без этого LOAD может начаться раньше их завершения.
  wbsExecDml_('DELETE FROM `' + c.projectId + '.' + c.datasetId + '.' + WBS_TBL_GOODS_ + '`' + where);
  wbsExecDml_('DELETE FROM `' + c.projectId + '.' + c.datasetId + '.' + WBS_TBL_HEADER_ + '`' + where);
}

/**
 * Пишет объект целиком. Возвращает {goods: n, hasCard: bool}.
 * 🔴 Порядок обязателен: goods, затем header. Header — commit-marker объекта.
 */
function wbsWriteObject_(token, o, entityKey, runId, snapshotTs) {
  var supplyId = wbsInt_(o.supplyID);
  var card = null, goods = [];

  if (supplyId !== null && supplyId !== 0) {
    card  = wbsCard_(token, supplyId);   Utilities.sleep(WBS_PAUSE_MS_);
    goods = wbsGoods_(token, supplyId);  Utilities.sleep(WBS_PAUSE_MS_);
  }

  wbsDeleteObject_(runId, entityKey);

  if (goods.length) {
    var grows = new Array(goods.length);
    for (var i = 0; i < goods.length; i++) {
      grows[i] = wbsGoodsRow_(goods[i], o, entityKey, runId, snapshotTs);
    }
    bqLoadRows_(WBS_TBL_GOODS_, grows);
  }

  // COMMIT объекта
  bqLoadRows_(WBS_TBL_HEADER_, [wbsHeaderRow_(o, card, entityKey, runId, snapshotTs)]);

  return { goods: goods.length, hasCard: !!card };
}

// ── бэкфилл ───────────────────────────────────────────────────

function wbSuppliesResetBackfill() {
  var sp = PropertiesService.getScriptProperties();
  sp.deleteProperty(WBS_PROP_RUN_);
  sp.deleteProperty(WBS_PROP_CURSOR_);
  sp.deleteProperty(WBS_PROP_START_);
  wbsLog_('Курсор бэкфилла сброшен. Данные в RAW НЕ тронуты.');
}

function wbSuppliesShowState() {
  var sp = PropertiesService.getScriptProperties();
  wbsLog_('run_id : ' + (sp.getProperty(WBS_PROP_RUN_) || '(нет)'));
  wbsLog_('курсор : ' + (sp.getProperty(WBS_PROP_CURSOR_) || '(нет)'));
  wbsLog_('начат  : ' + (sp.getProperty(WBS_PROP_START_) || '(нет)'));
}

/**
 * 🔴 DML. Бэкфилл порциями. Запускать повторно, пока не напечатает COMPLETE.
 */
function runWbSuppliesBackfill() {
  var t0 = new Date().getTime();
  var sp = PropertiesService.getScriptProperties();
  var lock = LockService.getScriptLock();
  if (!lock.tryLock(5000)) { wbsLog_('SKIPPED_LOCKED: другой прогон уже идёт'); return; }

  try {
    var tk = wbsToken_();
    var runId = sp.getProperty(WBS_PROP_RUN_);
    var startedAt = sp.getProperty(WBS_PROP_START_);
    if (!runId) {
      runId = 'BF_' + Utilities.formatDate(new Date(), WBS_TZ_, 'yyyyMMdd_HHmmss');
      startedAt = new Date().toISOString();
      sp.setProperty(WBS_PROP_RUN_, runId);
      sp.setProperty(WBS_PROP_START_, startedAt);
      sp.deleteProperty(WBS_PROP_CURSOR_);
      wbsLog_('НОВЫЙ бэкфилл, run_id = ' + runId);
    } else {
      wbsLog_('ПРОДОЛЖЕНИЕ бэкфилла, run_id = ' + runId);
    }
    wbsLog_('токен из свойства: ' + tk.keyName);

    // Один снимок на весь бэкфилл — так все объекты одного run_id имеют общий ts.
    var snapshotTs = startedAt;
    var cursor = sp.getProperty(WBS_PROP_CURSOR_) || '';

    var list = wbsList_(tk.token);
    wbsLog_('объектов в списке: ' + list.length);

    // Детерминированная сортировка, устойчивая к вставкам.
    var items = [];
    var noKey = 0;
    for (var i = 0; i < list.length; i++) {
      var ek = wbsEntityKey_(list[i]);
      if (!ek) { noKey++; continue; }
      items.push({ o: list[i], ek: ek, sk: wbsSortKey_(list[i], ek) });
    }
    if (noKey) wbsLog_('⚠️ пропущено объектов без обоих идентификаторов: ' + noKey);
    items.sort(function (a, b) { return a.sk < b.sk ? -1 : (a.sk > b.sk ? 1 : 0); });

    var done = 0, goodsRows = 0, noCard = 0, stopped = false;
    for (var j = 0; j < items.length; j++) {
      if (cursor && items[j].sk <= cursor) continue;
      if ((new Date().getTime() - t0) > WBS_TIME_BUDGET_) { stopped = true; break; }

      var res = wbsWriteObject_(tk.token, items[j].o, items[j].ek, runId, snapshotTs);
      sp.setProperty(WBS_PROP_CURSOR_, items[j].sk);   // сдвиг ТОЛЬКО после записи
      cursor = items[j].sk;
      done++; goodsRows += res.goods;
      if (!res.hasCard) noCard++;
      wbsLog_(done + ') ' + items[j].ek + ' · позиций ' + res.goods + (res.hasCard ? '' : ' · без карточки'));
    }

    wbsLog_('');
    wbsLog_('записано объектов за прогон: ' + done + ' · строк позиций: ' + goodsRows +
            (noCard ? ' · без карточки: ' + noCard : ''));

    if (stopped) {
      wbsLog_('PARTIAL — упёрлись в бюджет времени. Запустить функцию ещё раз.');
    } else {
      wbsLog_('COMPLETE — все объекты обработаны, run_id ' + runId);
      wbsLog_('Курсор оставлен на месте намеренно: повторный запуск ничего не перезапишет.');
      wbsLog_('Для нового снимка вызвать wbSuppliesResetBackfill().');
    }
    wbsLog_('Дальше: wbSuppliesCheckInvariants()');
  } finally {
    lock.releaseLock();
  }
}

// ── суточный инкремент ────────────────────────────────────────

/**
 * 🔴 DML. Перечитывает: незавершённые объекты, объекты с новым updated_dt и
 * завершённые с fact_dt за последние WBS_OVERLAP_DAYS_ дней (§7.3).
 */
function runWbSuppliesDaily() {
  var t0 = new Date().getTime();
  var lock = LockService.getScriptLock();
  if (!lock.tryLock(5000)) { wbsLog_('SKIPPED_LOCKED'); return; }

  try {
    var c = getBqConfig_();
    var tk = wbsToken_();
    var runId = 'DL_' + Utilities.formatDate(new Date(), WBS_TZ_, 'yyyyMMdd_HHmmss');
    var snapshotTs = new Date().toISOString();
    wbsLog_('суточный прогон, run_id = ' + runId);

    var list = wbsList_(tk.token);
    wbsLog_('объектов в списке: ' + list.length);

    // Последнее известное состояние: updated_dt по entity_key.
    // 🔴 Сравниваем время как epoch ms, а не строки. WB отдаёт RFC3339
    // (2026-08-04T08:51:38+03:00), BigQuery сериализует TIMESTAMP иначе —
    // лексикографическое `>` сравнивало бы форматы, а не моменты. Побочный
    // эффект был бы каскадным: daily считал бы «обновлённым» почти всё,
    // упирался в бюджет времени и на следующем запуске строил тот же список.
    var known = {};
    var q = bqQuery_(
      'SELECT entity_key, MAX(UNIX_MILLIS(updated_dt)) AS upd_ms ' +
      'FROM `' + c.projectId + '.' + c.datasetId + '.' + WBS_TBL_HEADER_ + '` ' +
      'GROUP BY entity_key');
    var rows = (q && q.rows) || [];
    for (var i = 0; i < rows.length; i++) {
      known[rows[i].f[0].v] = Number(rows[i].f[1].v || 0);
    }
    wbsLog_('известных объектов в RAW: ' + rows.length);

    var overlapFrom = new Date(new Date().getTime() - WBS_OVERLAP_DAYS_ * 24 * 3600 * 1000);
    var overlapDay = Utilities.formatDate(overlapFrom, WBS_TZ_, 'yyyy-MM-dd');

    var todo = [];
    for (var j = 0; j < list.length; j++) {
      var o = list[j], ek = wbsEntityKey_(o);
      if (!ek) continue;

      var reason = '';
      var apiUpdMs = Date.parse(String(o.updatedDate || ''));
      if (!(ek in known))                              reason = 'новый';
      else if (Number(o.statusID) !== WBS_STATUS_DONE_) reason = 'не завершён';
      else if (!isNaN(apiUpdMs) && apiUpdMs > Number(known[ek] || 0)) reason = 'updated_dt новее';
      else if (String(o.factDate || '').substring(0, 10) >= overlapDay) reason = 'окно перекрытия';

      if (reason) todo.push({ o: o, ek: ek, why: reason });
    }
    wbsLog_('к перечитыванию: ' + todo.length + ' из ' + list.length);

    var done = 0, goodsRows = 0;
    for (var k = 0; k < todo.length; k++) {
      if ((new Date().getTime() - t0) > WBS_TIME_BUDGET_) {
        wbsLog_('PARTIAL: бюджет времени исчерпан на ' + done + ' из ' + todo.length +
                '. Запустить функцию ещё раз — оставшиеся попадут в новый run_id.');
        break;
      }
      var r = wbsWriteObject_(tk.token, todo[k].o, todo[k].ek, runId, snapshotTs);
      done++; goodsRows += r.goods;
      wbsLog_(done + ') ' + todo[k].ek + ' · ' + todo[k].why + ' · позиций ' + r.goods);
    }

    wbsLog_('');
    wbsLog_('обновлено объектов: ' + done + ' · строк позиций: ' + goodsRows);
    wbsLog_('Дальше: wbSuppliesCheckInvariants()');
  } finally {
    lock.releaseLock();
  }
}

// ── инварианты И1…И10 ─────────────────────────────────────────

function wbsCount_(sql) {
  var q = bqQuery_(sql);
  var rows = (q && q.rows) || [];
  if (!rows.length) return 0;
  return Number(rows[0].f[0].v || 0);
}

/** Только чтение. Печатает отчёт по И1…И10. */
function wbSuppliesCheckInvariants() {
  var c = getBqConfig_();
  var H = '`' + c.projectId + '.' + c.datasetId + '.' + WBS_TBL_HEADER_ + '`';
  var G = '`' + c.projectId + '.' + c.datasetId + '.' + WBS_TBL_GOODS_ + '`';
  var fails = 0, warns = 0;

  function check(code, label, sql, isError) {
    var n = wbsCount_(sql);
    var mark = n === 0 ? '✅' : (isError ? '🔴 ОШИБКА' : '⚠️ предупреждение');
    wbsLog_(code + ' ' + mark + ' · нарушений ' + n + ' · ' + label);
    if (n > 0) { if (isError) fails++; else warns++; }
    return n;
  }

  wbsLog_('=== ИНВАРИАНТЫ RAW-слоя поставок ===');

  check('И1 ', 'entity_key уникален внутри run_id',
    'SELECT COUNT(*) FROM (SELECT run_id, entity_key FROM ' + H +
    ' GROUP BY run_id, entity_key HAVING COUNT(*) > 1)', true);

  check('И2 ', 'позиции без шапки того же (run_id, entity_key) — ожидаемы у прерванных прогонов',
    'SELECT COUNT(*) FROM (SELECT DISTINCT g.run_id, g.entity_key FROM ' + G + ' g ' +
    'LEFT JOIN ' + H + ' h USING (run_id, entity_key) WHERE h.entity_key IS NULL)', false);

  check('И3 ', 'is_completed при пустом fact_dt',
    'SELECT COUNT(*) FROM ' + H + ' WHERE is_completed AND fact_dt IS NULL', true);

  check('И4 ', 'SUM(quantity) позиций ≠ card_quantity',
    'SELECT COUNT(*) FROM (SELECT h.run_id, h.entity_key FROM ' + H + ' h JOIN ' + G + ' g ' +
    'USING (run_id, entity_key) WHERE h.card_quantity IS NOT NULL ' +
    'GROUP BY h.run_id, h.entity_key, h.card_quantity ' +
    'HAVING SUM(g.quantity) != ANY_VALUE(h.card_quantity))', false);

  check('И5 ', 'SUM(accepted) позиций ≠ card_accepted_quantity — известны 2 случая из 108',
    'SELECT COUNT(*) FROM (SELECT h.run_id, h.entity_key FROM ' + H + ' h JOIN ' + G + ' g ' +
    'USING (run_id, entity_key) WHERE h.card_accepted_quantity IS NOT NULL ' +
    'GROUP BY h.run_id, h.entity_key, h.card_accepted_quantity ' +
    'HAVING SUM(g.accepted_quantity) != ANY_VALUE(h.card_accepted_quantity))', false);

  check('И6 ', 'actual не равен ни транзиту, ни назначению',
    'SELECT COUNT(*) FROM ' + H + ' WHERE actual_warehouse_id IS NOT NULL ' +
    'AND actual_warehouse_id != IFNULL(transit_warehouse_id, -1) ' +
    'AND actual_warehouse_id != IFNULL(warehouse_id, -1)', false);

  check('И7 ', 'один preorder_id → несколько supply_id',
    'SELECT COUNT(*) FROM (SELECT preorder_id FROM ' + H +
    ' WHERE preorder_id IS NOT NULL AND supply_id IS NOT NULL ' +
    'GROUP BY preorder_id HAVING COUNT(DISTINCT supply_id) > 1)', true);

  check('И8 ', 'один supply_id → несколько preorder_id',
    'SELECT COUNT(*) FROM (SELECT supply_id FROM ' + H +
    ' WHERE supply_id IS NOT NULL AND preorder_id IS NOT NULL ' +
    'GROUP BY supply_id HAVING COUNT(DISTINCT preorder_id) > 1)', true);

  check('И9 ', 'у одного supply_id preorder_id был и NULL, и непустой → entity_key сменился S:→P:',
    'SELECT COUNT(*) FROM (SELECT supply_id FROM ' + H + ' WHERE supply_id IS NOT NULL ' +
    'GROUP BY supply_id HAVING COUNTIF(preorder_id IS NULL) > 0 ' +
    'AND COUNTIF(preorder_id IS NOT NULL) > 0)', true);

  check('И10', 'barcode не уникален внутри (run_id, entity_key) → SUM(quantity) удвоится',
    'SELECT COUNT(*) FROM (SELECT run_id, entity_key, barcode FROM ' + G +
    ' GROUP BY run_id, entity_key, barcode HAVING COUNT(*) > 1)', true);

  wbsLog_('');
  wbsLog_(fails === 0 ? '✅ критических нарушений нет' : '🔴 критических нарушений: ' + fails);
  if (warns) wbsLog_('⚠️ предупреждений: ' + warns + ' — разобрать, но загрузку не блокируют');

  // Контрольные цифры §8 проекта
  wbsLog_('');
  wbsLog_('=== КОНТРОЛЬНЫЕ ЦИФРЫ (эталон из проб) ===');
  var last = bqQuery_(
    'WITH cur AS (SELECT * EXCEPT(rn) FROM (SELECT *, ROW_NUMBER() OVER ' +
    '(PARTITION BY entity_key ORDER BY snapshot_ts DESC, run_id DESC) rn FROM ' + H + ') WHERE rn = 1) ' +
    'SELECT COUNT(*) AS objects, COUNTIF(is_completed) AS completed FROM cur');
  if (last && last.rows && last.rows.length) {
    wbsLog_('объектов: ' + last.rows[0].f[0].v + ' (эталон 131) · состоявшихся: ' +
            last.rows[0].f[1].v + ' (эталон 108)');
  }
  var sums = bqQuery_(
    'WITH cur AS (SELECT * EXCEPT(rn) FROM (SELECT *, ROW_NUMBER() OVER ' +
    '(PARTITION BY entity_key ORDER BY snapshot_ts DESC, run_id DESC) rn FROM ' + H + ') WHERE rn = 1) ' +
    'SELECT SUM(g.quantity), SUM(g.accepted_quantity), SUM(g.ready_for_sale_quantity) ' +
    'FROM ' + G + ' g JOIN cur s ON g.entity_key = s.entity_key AND g.run_id = s.run_id ' +
    'WHERE s.is_completed');
  if (sums && sums.rows && sums.rows.length) {
    var f = sums.rows[0].f;
    wbsLog_('quantity: ' + f[0].v + ' (эталон 44788) · accepted: ' + f[1].v +
            ' (эталон 20948) · ready: ' + f[2].v + ' (эталон 20854)');
  }
  wbsLog_('🔴 Расхождение с эталоном означает дефект загрузчика, а не источника.');
}
