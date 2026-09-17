/**
 * ══════════════════════════════════════════════════════════════
 *  WbBigQuery.gs — слой доступа к BigQuery (Фаза A миграции)
 * ══════════════════════════════════════════════════════════════
 *  Назначение: вынести хранилище RAW из Google Sheets в BigQuery.
 *  Этот файл НИЧЕГО не меняет в книге и в существующих загрузчиках.
 *  Он только: читает конфиг, создаёт датасет/таблицу, грузит строки
 *  пакетным load-job (NEWLINE_DELIMITED_JSON — бесплатно) и умеет
 *  выполнять SELECT.
 *
 *  Требуется: включённый advanced-сервис "BigQuery API" в редакторе
 *  (Services → + → BigQuery API) и связанный GCP-проект с биллингом.
 *
 *  Порядок настройки:
 *    1) один раз запустить bqSaveConfig_ONE_TIME() (вписать точный ID);
 *    2) запустить bqShowConfig() — проверить, что всё считалось;
 *    3) запустить bqSelfTest() — полная проверка round-trip.
 * ══════════════════════════════════════════════════════════════
 */

// Ключи Script Properties (по аналогии с WB_TOKEN_FINANCE)
var BQ_PROP_PROJECT_ = 'BQ_PROJECT_ID';
var BQ_PROP_DATASET_ = 'BQ_DATASET';
var BQ_PROP_LOCATION_ = 'BQ_LOCATION';

// Значения по умолчанию
var BQ_DEFAULT_DATASET_ = 'wb_raw';
var BQ_DEFAULT_LOCATION_ = 'EU';

// Имя таблицы сырого финансового слоя в BigQuery
var BQ_TABLE_FINANCE_ = 'RAW_WB_FINANCE';


// ═══════════════════════════════════════
//  КОНФИГ
// ═══════════════════════════════════════

/**
 * ОДНОРАЗОВО. Впишите ТОЧНЫЙ Project ID (скопируйте из консоли GCP,
 * Settings → Project ID) между кавычками и запустите эту функцию.
 * В репозитории оставлен плейсхолдер. Перед запуском в Apps Script
 * замените его на точный Project ID из GCP Console.
 */
function bqSaveConfig_ONE_TIME() {
  var PROJECT_ID = 'PASTE_EXACT_GCP_PROJECT_ID_HERE'; // ← вставьте точный Project ID из GCP перед запуском в Apps Script!
  if (!PROJECT_ID || PROJECT_ID === 'PASTE_EXACT_GCP_PROJECT_ID_HERE') {
    throw new Error('Замените PASTE_EXACT_GCP_PROJECT_ID_HERE на точный GCP Project ID перед запуском bqSaveConfig_ONE_TIME().');
  }
  var props = PropertiesService.getScriptProperties();
  props.setProperty(BQ_PROP_PROJECT_, String(PROJECT_ID).trim());
  props.setProperty(BQ_PROP_DATASET_, BQ_DEFAULT_DATASET_);
  props.setProperty(BQ_PROP_LOCATION_, BQ_DEFAULT_LOCATION_);
  console.log('✅ Конфиг сохранён. Проверьте bqShowConfig().');
  bqShowConfig();
}

/** Показать текущий конфиг BigQuery из Script Properties. */
function bqShowConfig() {
  var c = getBqConfig_();
  console.log('projectId : ' + c.projectId);
  console.log('dataset   : ' + c.datasetId);
  console.log('location  : ' + c.location);
}

/** Читает конфиг; кидает понятную ошибку, если проект не задан. */
function getBqConfig_() {
  var props = PropertiesService.getScriptProperties();
  var projectId = String(props.getProperty(BQ_PROP_PROJECT_) || '').trim();
  if (!projectId || projectId === 'PASTE_EXACT_GCP_PROJECT_ID_HERE') {
    throw new Error('BQ_PROJECT_ID не задан или содержит плейсхолдер. Запустите bqSaveConfig_ONE_TIME() с точным Project ID.');
  }
  return {
    projectId: projectId,
    datasetId: String(props.getProperty(BQ_PROP_DATASET_) || BQ_DEFAULT_DATASET_).trim(),
    location: String(props.getProperty(BQ_PROP_LOCATION_) || BQ_DEFAULT_LOCATION_).trim()
  };
}


// ═══════════════════════════════════════
//  СХЕМА RAW_WB_FINANCE (69 колонок 1:1 со схемой листа)
//  RAW = верное сырьё → все поля STRING (без потерь при загрузке
//  из-за запятых/пустых значений). Приведение типов — в SQL-витринах.
//  Плюс одна служебная типизированная колонка _rr_date (DATE) для
//  партиционирования.
// ═══════════════════════════════════════

var BQ_FINANCE_STRING_FIELDS_ = [
  // Служебные
  'load_id', 'loaded_at', 'source_api', 'request_date_from', 'request_date_to',
  'report_id', 'report_period_from', 'report_period_to', 'row_hash', 'raw_row_number',
  // Идентификаторы операции WB
  'wb_operation_id', 'rrd_id', 'srid', 'rid', 'shk_id',
  'sticker_id', 'doc_type_name', 'supplier_oper_name', 'operation_type_normalized',
  // Даты
  'order_dt', 'sale_dt', 'rr_dt', 'create_dt', 'period_month', 'last_change_date',
  // Товар
  'wb_nm_id', 'wb_vendor_code', 'barcode', 'sa_name', 'ts_name',
  'brand_name', 'subject_name', 'internal_sku', 'sku_match_status',
  // География
  'office_name', 'warehouse_name', 'country_name', 'region_name',
  // Продажи/цены
  'retail_price', 'retail_amount', 'retail_price_withdisc_rub',
  'sale_percent', 'commission_percent', 'product_discount_for_report',
  'supplier_promo', 'spp_percent', 'quantity',
  // Деньги WB
  'sale_amount', 'return_amount_rub', 'for_pay',
  'commission_amount', 'sales_commission', 'logistics_amount',
  'storage_fee', 'deduction', 'penalty', 'acceptance',
  'additional_payment', 'acquiring_fee', 'compensation_amount',
  'other_amount', 'currency',
  // Технические
  'money_parse_status', 'money_parse_comment',
  'is_duplicate', 'processed_status', 'error_message',
  // v1.0.2
  'raw_json', 'error_code', 'rebill_logistics'
];

/** Строит объект schema для BigQuery.Tables.insert. */
function bqFinanceSchema_() {
  var fields = BQ_FINANCE_STRING_FIELDS_.map(function (n) {
    return { name: n, type: 'STRING', mode: 'NULLABLE' };
  });
  // Служебная типизированная колонка для партиции
  fields.push({ name: '_rr_date', type: 'DATE', mode: 'NULLABLE' });
  return { fields: fields };
}


// ═══════════════════════════════════════
//  ДАТАСЕТ / ТАБЛИЦА
// ═══════════════════════════════════════

/** Создаёт датасет, если его ещё нет. */
function bqEnsureDataset_() {
  var c = getBqConfig_();
  try {
    BigQuery.Datasets.get(c.projectId, c.datasetId);
    return false; // уже есть
  } catch (e) {
    BigQuery.Datasets.insert(
      { datasetReference: { projectId: c.projectId, datasetId: c.datasetId }, location: c.location },
      c.projectId
    );
    console.log('✅ Датасет создан: ' + c.datasetId + ' (' + c.location + ')');
    return true;
  }
}

/** Создаёт таблицу RAW_WB_FINANCE (партиция по _rr_date, кластер по nm_id/rrd_id). */
function bqEnsureFinanceTable_() {
  var c = getBqConfig_();
  try {
    BigQuery.Tables.get(c.projectId, c.datasetId, BQ_TABLE_FINANCE_);
    return false; // уже есть
  } catch (e) {
    BigQuery.Tables.insert(
      {
        tableReference: { projectId: c.projectId, datasetId: c.datasetId, tableId: BQ_TABLE_FINANCE_ },
        schema: bqFinanceSchema_(),
        timePartitioning: { type: 'DAY', field: '_rr_date' },
        clustering: { fields: ['wb_nm_id', 'rrd_id'] }
      },
      c.projectId, c.datasetId
    );
    console.log('✅ Таблица создана: ' + c.datasetId + '.' + BQ_TABLE_FINANCE_);
    return true;
  }
}


// ═══════════════════════════════════════
//  ЗАГРУЗКА (пакетный load-job, бесплатно)
// ═══════════════════════════════════════

/**
 * Грузит массив объектов {colName: value,...} в таблицу через load-job.
 * writeDisposition = WRITE_APPEND. Возвращает число загруженных строк.
 * rows — массив плоских объектов (значения строкой/числом).
 */
function bqLoadRows_(tableId, rows) {
  if (!rows || !rows.length) return 0;
  var c = getBqConfig_();

  // NDJSON: по одному JSON-объекту на строку
  var lines = new Array(rows.length);
  for (var i = 0; i < rows.length; i++) lines[i] = JSON.stringify(rows[i]);
  var blob = Utilities.newBlob(lines.join('\n'), 'application/octet-stream');

  var job = {
    jobReference: {
      projectId: c.projectId,
      location: c.location
    },
    configuration: {
      load: {
        destinationTable: { projectId: c.projectId, datasetId: c.datasetId, tableId: tableId },
        sourceFormat: 'NEWLINE_DELIMITED_JSON',
        writeDisposition: 'WRITE_APPEND',
        ignoreUnknownValues: true,
        maxBadRecords: 0
      }
    }
  };

  var ins = BigQuery.Jobs.insert(job, c.projectId, blob);
  var jobId = ins.jobReference.jobId;
  var jobLocation = ins.jobReference.location || c.location;

  // Ждём завершения (load обычно секунды)
  var state = '', tries = 0;
  do {
    Utilities.sleep(1500);
    var j = BigQuery.Jobs.get(c.projectId, jobId, { location: jobLocation });
    state = j.status.state;
    if (j.status.errorResult) {
      throw new Error('BQ load error: ' + JSON.stringify(j.status.errorResult));
    }
    tries++;
  } while (state !== 'DONE' && tries < 120);

  if (state !== 'DONE') throw new Error('BQ load: job не завершился за отведённое время');
  return rows.length;
}

/** Выполняет SELECT и возвращает {rows, totalRows}. */
function bqQuery_(sql) {
  var c = getBqConfig_();
  var res = BigQuery.Jobs.query(
    { query: sql, useLegacySql: false, location: c.location }, c.projectId
  );
  return { rows: res.rows || [], totalRows: res.totalRows };
}


// ═══════════════════════════════════════
//  САМОПРОВЕРКА (round-trip)
// ═══════════════════════════════════════

/**
 * Полная проверка доступа БЕЗ касания реальных данных:
 *  1) создаёт датасет (если нет);
 *  2) создаёт временную таблицу _selftest;
 *  3) грузит 2 строки;
 *  4) читает COUNT(*);
 *  5) удаляет _selftest.
 */
function bqSelfTest() {
  var c = getBqConfig_();
  console.log('Проект: ' + c.projectId + ' | датасет: ' + c.datasetId + ' | регион: ' + c.location);

  bqEnsureDataset_();

  var testTable = '_selftest';
  // Пересоздаём тестовую таблицу с нуля
  try { BigQuery.Tables.remove(c.projectId, c.datasetId, testTable); } catch (e) {}
  BigQuery.Tables.insert(
    {
      tableReference: { projectId: c.projectId, datasetId: c.datasetId, tableId: testTable },
      schema: { fields: [
        { name: 'id', type: 'STRING' },
        { name: 'note', type: 'STRING' },
        { name: 'ts', type: 'TIMESTAMP' }
      ] }
    },
    c.projectId, c.datasetId
  );

  var now = new Date().toISOString();
  var n = bqLoadRows_(testTable, [
    { id: '1', note: 'evetis bq selftest', ts: now },
    { id: '2', note: 'row two', ts: now }
  ]);
  console.log('Загружено строк: ' + n);

  var q = bqQuery_('SELECT COUNT(*) AS c FROM `' + c.projectId + '.' + c.datasetId + '.' + testTable + '`');
  var cnt = q.rows.length ? q.rows[0].f[0].v : '?';
  console.log('COUNT(*) из BigQuery: ' + cnt);

  try { BigQuery.Tables.remove(c.projectId, c.datasetId, testTable); } catch (e) {}

  if (String(cnt) === '2') {
    console.log('✅ SELF-TEST OK: доступ, создание, загрузка и чтение работают.');
  } else {
    console.log('⚠️ SELF-TEST: ожидали 2, получили ' + cnt + '. Проверьте логи.');
  }
}

/**
 * Создать боевую таблицу RAW_WB_FINANCE (после успешного bqSelfTest).
 * Отдельно, чтобы не создавать её раньше времени.
 */
function bqCreateFinanceTable() {
  bqEnsureDataset_();
  var created = bqEnsureFinanceTable_();
  console.log(created ? '✅ RAW_WB_FINANCE создана.' : 'ℹ️ RAW_WB_FINANCE уже существовала.');
}
