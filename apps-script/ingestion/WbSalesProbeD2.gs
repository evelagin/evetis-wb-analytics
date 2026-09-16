/**
 * D2 READ-ONLY PROBE — WB Sales/Returns
 * Endpoint: GET /api/v1/supplier/sales
 *
 * Временная диагностическая функция.
 *
 * НИЧЕГО НЕ ЗАПИСЫВАЕТ:
 * - не изменяет Google Sheets;
 * - не изменяет BigQuery;
 * - не изменяет Script Properties;
 * - не создаёт триггеры;
 * - не логирует токен.
 *
 * Выполняет ровно один HTTP-запрос без пагинации.
 * Все показатели считаются по сырому массиву ответа API до дедупликации.
 *
 * Запуск без параметров:
 * probeWbSalesReadonly()
 *
 * По умолчанию dateFrom = текущая дата минус 45 дней.
 *
 * Запуск с конкретной датой:
 * probeWbSalesReadonly('2026-06-22')
 */
function probeWbSalesReadonly(dateFromOpt) {
  var dateFrom = dateFromOpt || salesProbeDateDaysAgo_(45);

  if (!salesProbeValidDate_(dateFrom)) {
    var invalidDateResult = {
      probe: 'wb_sales_readonly',
      status: 'ERROR',
      error: 'Некорректный dateFrom. Ожидается YYYY-MM-DD.',
      dateFrom: String(dateFrom || '')
    };

    console.log(JSON.stringify(invalidDateResult, null, 2));
    return invalidDateResult;
  }

  var props = PropertiesService.getScriptProperties();

  var token =
    props.getProperty('WB_TOKEN_STATISTICS') ||
    props.getProperty('WB_TOKEN_ANALYTICS') ||
    '';

  if (!token) {
    var noTokenResult = {
      probe: 'wb_sales_readonly',
      status: 'ERROR',
      error: 'Нет WB_TOKEN_STATISTICS или WB_TOKEN_ANALYTICS в Script Properties.',
      dateFrom: dateFrom
    };

    console.log(JSON.stringify(noTokenResult, null, 2));
    return noTokenResult;
  }

  var url =
    'https://statistics-api.wildberries.ru/api/v1/supplier/sales' +
    '?dateFrom=' + encodeURIComponent(dateFrom) +
    '&flag=0';

  var response;

  try {
    response = UrlFetchApp.fetch(url, {
      method: 'get',
      headers: {
        Authorization: token
      },
      muteHttpExceptions: true
    });
  } catch (fetchError) {
    var fetchErrorResult = {
      probe: 'wb_sales_readonly',
      status: 'ERROR',
      dateFrom: dateFrom,
      error: 'Ошибка HTTP-запроса: ' +
        String((fetchError && fetchError.message) || fetchError)
    };

    console.log(JSON.stringify(fetchErrorResult, null, 2));
    return fetchErrorResult;
  }

  var httpStatus = response.getResponseCode();

  var result = {
    probe: 'wb_sales_readonly',
    dateFrom: dateFrom,
    http_status: httpStatus
  };

  if (httpStatus !== 200) {
    result.status = 'ERROR';
    result.error = String(response.getContentText() || '').substring(0, 500);

    if (httpStatus === 429) {
      result.hint =
        'Лимит Sales API: подождите не менее 65 секунд и повторите один раз.';
    }

    console.log(JSON.stringify(result, null, 2));
    return result;
  }

  var rawText = response.getContentText();
  var rows;

  try {
    rows = JSON.parse(rawText);
  } catch (jsonError) {
    result.status = 'ERROR';
    result.error =
      'Ответ WB содержит повреждённый JSON: ' +
      String((jsonError && jsonError.message) || jsonError);

    console.log(JSON.stringify(result, null, 2));
    return result;
  }

  if (!Array.isArray(rows)) {
    result.status = 'ERROR';
    result.error = 'Ответ WB не является массивом.';
    result.response_type = Object.prototype.toString.call(rows);

    console.log(JSON.stringify(result, null, 2));
    return result;
  }

  /*
   * Словари без прототипа.
   * Это защищает анализ от внешних идентификаторов вроде:
   * constructor, prototype, __proto__.
   */
  var keyPresent = Object.create(null);
  var keyNonEmpty = Object.create(null);

  var distinctSaleIds = Object.create(null);
  var distinctSrids = Object.create(null);

  var rowsBySaleId = Object.create(null);
  var rowsBySrid = Object.create(null);

  var saleIdFirstCharacterDistribution = Object.create(null);

  var emptySaleId = 0;
  var emptySrid = 0;
  var emptyLastChangeDate = 0;
  var unparseableLastChangeDate = 0;

  var minDate = null;
  var maxDate = null;
  var minLastChangeDate = null;
  var maxLastChangeDate = null;

  for (var i = 0; i < rows.length; i++) {
    var row = rows[i] || {};

    for (var key in row) {
      if (!Object.prototype.hasOwnProperty.call(row, key)) {
        continue;
      }

      keyPresent[key] = (keyPresent[key] || 0) + 1;

      var value = row[key];

      if (value !== '' && value !== null && value !== undefined) {
        keyNonEmpty[key] = (keyNonEmpty[key] || 0) + 1;
      }
    }

    var saleId = String(row.saleID || '').trim();
    var srid = String(row.srid || '').trim();
    var lastChangeDate = String(row.lastChangeDate || '').trim();
    var saleDate = String(row.date || '').trim();
    var nmId = String(row.nmId || '').trim();

    if (!saleId) {
      emptySaleId++;
    } else {
      distinctSaleIds[saleId] = true;

      if (!rowsBySaleId[saleId]) {
        rowsBySaleId[saleId] = [];
      }

      rowsBySaleId[saleId].push({
        srid: srid,
        nmId: nmId,
        date: saleDate,
        lastChangeDate: lastChangeDate
      });

      var firstCharacter = saleId.charAt(0).toUpperCase();

      saleIdFirstCharacterDistribution[firstCharacter] =
        (saleIdFirstCharacterDistribution[firstCharacter] || 0) + 1;
    }

    if (!srid) {
      emptySrid++;
    } else {
      distinctSrids[srid] = true;

      if (!rowsBySrid[srid]) {
        rowsBySrid[srid] = [];
      }

      rowsBySrid[srid].push({
        saleID: saleId,
        lastChangeDate: lastChangeDate
      });
    }

    if (!lastChangeDate) {
      emptyLastChangeDate++;
    } else if (!salesProbeValidLastChangeDate_(lastChangeDate)) {
      unparseableLastChangeDate++;
    }

    if (saleDate) {
      if (minDate === null || saleDate < minDate) {
        minDate = saleDate;
      }

      if (maxDate === null || saleDate > maxDate) {
        maxDate = saleDate;
      }
    }

    if (lastChangeDate) {
      if (
        minLastChangeDate === null ||
        lastChangeDate < minLastChangeDate
      ) {
        minLastChangeDate = lastChangeDate;
      }

      if (
        maxLastChangeDate === null ||
        lastChangeDate > maxLastChangeDate
      ) {
        maxLastChangeDate = lastChangeDate;
      }
    }
  }

  var saleIdWithMultipleRows = 0;
  var saleIdWithMultipleLastChangeDates = 0;
  var saleIdWithDifferentSrids = 0;
  var saleIdWithDifferentNmIds = 0;
  var saleIdWithDifferentDates = 0;

  var saleIdVersionExamples = [];

  for (var saleIdKey in rowsBySaleId) {
    if (!Object.prototype.hasOwnProperty.call(rowsBySaleId, saleIdKey)) {
      continue;
    }

    var saleGroup = rowsBySaleId[saleIdKey];

    if (saleGroup.length <= 1) {
      continue;
    }

    saleIdWithMultipleRows++;

    var lastChangeDates = Object.create(null);
    var srids = Object.create(null);
    var nmIds = Object.create(null);
    var dates = Object.create(null);

    for (var j = 0; j < saleGroup.length; j++) {
      var saleVersion = saleGroup[j];

      lastChangeDates[saleVersion.lastChangeDate] = true;
      srids[saleVersion.srid] = true;
      nmIds[saleVersion.nmId] = true;
      dates[saleVersion.date] = true;
    }

    var distinctLastChangeDates = Object.keys(lastChangeDates);
    var distinctGroupSrids = Object.keys(srids);
    var distinctGroupNmIds = Object.keys(nmIds);
    var distinctGroupDates = Object.keys(dates);

    if (distinctLastChangeDates.length > 1) {
      saleIdWithMultipleLastChangeDates++;

      if (saleIdVersionExamples.length < 20) {
        saleIdVersionExamples.push({
          saleID: salesProbeMaskId_(saleIdKey),
          rows: saleGroup.length,
          distinct_lastChangeDate: distinctLastChangeDates.length,
          lastChangeDates: distinctLastChangeDates
        });
      }
    }

    if (distinctGroupSrids.length > 1) {
      saleIdWithDifferentSrids++;
    }

    if (distinctGroupNmIds.length > 1) {
      saleIdWithDifferentNmIds++;
    }

    if (distinctGroupDates.length > 1) {
      saleIdWithDifferentDates++;
    }
  }

  var sridWithMultipleRows = 0;
  var sridWithMultipleSaleIds = 0;
  var sridWithSAndR = 0;

  var sridMultipleSaleIdExamples = [];

  for (var sridKey in rowsBySrid) {
    if (!Object.prototype.hasOwnProperty.call(rowsBySrid, sridKey)) {
      continue;
    }

    var sridGroup = rowsBySrid[sridKey];

    if (sridGroup.length > 1) {
      sridWithMultipleRows++;
    }

    var saleIdSet = Object.create(null);
    var hasSalePrefix = false;
    var hasReturnPrefix = false;

    for (var k = 0; k < sridGroup.length; k++) {
      var groupedSaleId = String(sridGroup[k].saleID || '');

      if (groupedSaleId) {
        saleIdSet[groupedSaleId] = true;
      }

      var prefix = groupedSaleId.charAt(0).toUpperCase();

      if (prefix === 'S') {
        hasSalePrefix = true;
      }

      if (prefix === 'R') {
        hasReturnPrefix = true;
      }
    }

    var groupedSaleIds = Object.keys(saleIdSet);

    if (groupedSaleIds.length > 1) {
      sridWithMultipleSaleIds++;

      if (sridMultipleSaleIdExamples.length < 20) {
        sridMultipleSaleIdExamples.push({
          srid: salesProbeMaskId_(sridKey),
          sale_ids: groupedSaleIds.map(salesProbeMaskId_)
        });
      }
    }

    if (hasSalePrefix && hasReturnPrefix) {
      sridWithSAndR++;
    }
  }

  var saleIdExamples = [];
  var allSaleIds = Object.keys(distinctSaleIds);

  for (var exampleIndex = 0; exampleIndex < allSaleIds.length; exampleIndex++) {
    if (saleIdExamples.length >= 20) {
      break;
    }

    saleIdExamples.push(
      salesProbeMaskId_(allSaleIds[exampleIndex])
    );
  }

  var fieldsToInspect = [
    'saleID',
    'srid',
    'date',
    'lastChangeDate',
    'warehouseType',
    'incomeID',
    'isSupply',
    'isRealization',
    'paymentSaleAmount',
    'priceWithDisc',
    'finishedPrice',
    'forPay',
    'gNumber',
    'regionName',
    'oblastOkrugName',
    'nmId',
    'barcode',
    'supplierArticle',
    'orderType'
  ];

  var fieldPresence = Object.create(null);

  for (
    var fieldIndex = 0;
    fieldIndex < fieldsToInspect.length;
    fieldIndex++
  ) {
    var fieldName = fieldsToInspect[fieldIndex];
    var presentCount = keyPresent[fieldName] || 0;
    var nonEmptyCount = keyNonEmpty[fieldName] || 0;

    fieldPresence[fieldName] = {
      present: presentCount,
      non_empty: nonEmptyCount,
      pct_present: rows.length
        ? salesProbeRoundOne_(presentCount / rows.length * 100)
        : 0,
      pct_non_empty: rows.length
        ? salesProbeRoundOne_(nonEmptyCount / rows.length * 100)
        : 0
    };
  }

  result.status = 'OK';
  result.raw_rows = rows.length;
  result.reached_cap_80000 = rows.length >= 80000;

  result.first_lastChangeDate = rows.length
    ? String((rows[0] || {}).lastChangeDate || '')
    : '';

  result.last_lastChangeDate = rows.length
    ? String((rows[rows.length - 1] || {}).lastChangeDate || '')
    : '';

  result.min_date = minDate;
  result.max_date = maxDate;
  result.min_lastChangeDate = minLastChangeDate;
  result.max_lastChangeDate = maxLastChangeDate;

  result.distinct_saleID = Object.keys(distinctSaleIds).length;
  result.distinct_srid = Object.keys(distinctSrids).length;

  result.empty_saleID = emptySaleId;
  result.empty_srid = emptySrid;
  result.empty_lastChangeDate = emptyLastChangeDate;
  result.unparseable_lastChangeDate = unparseableLastChangeDate;

  result.saleID_multi_row = saleIdWithMultipleRows;
  result.saleID_multi_distinct_lastChangeDate =
    saleIdWithMultipleLastChangeDates;

  result.saleID_diff_srid = saleIdWithDifferentSrids;
  result.saleID_diff_nmId = saleIdWithDifferentNmIds;
  result.saleID_diff_date = saleIdWithDifferentDates;

  result.srid_multi_row = sridWithMultipleRows;
  result.srid_multi_saleID = sridWithMultipleSaleIds;
  result.srid_with_S_and_R = sridWithSAndR;

  result.saleID_first_char_distribution =
    saleIdFirstCharacterDistribution;

  result.all_json_keys = Object.keys(keyPresent).sort();
  result.field_presence = fieldPresence;

  result.examples = {
    saleIDs: saleIdExamples,
    srid_with_multiple_saleID: sridMultipleSaleIdExamples,
    saleID_with_multiple_versions: saleIdVersionExamples
  };

  result.verdict_hint = {
    saleID_unique_per_raw_row:
      saleIdWithMultipleRows === 0,

    saleID_stable_across_srid_nmId_date:
      saleIdWithDifferentSrids === 0 &&
      saleIdWithDifferentNmIds === 0 &&
      saleIdWithDifferentDates === 0,

    saleID_has_versions:
      saleIdWithMultipleLastChangeDates > 0,

    empty_saleID_share_pct: rows.length
      ? salesProbeRoundOne_(emptySaleId / rows.length * 100)
      : 0,

    empty_srid_share_pct: rows.length
      ? salesProbeRoundOne_(emptySrid / rows.length * 100)
      : 0,

    need_resumable_pagination:
      rows.length >= 80000,

    saleID_candidate_event_key:
      rows.length > 0 &&
      emptySaleId === 0 &&
      saleIdWithDifferentSrids === 0 &&
      saleIdWithDifferentNmIds === 0 &&
      saleIdWithDifferentDates === 0
  };

  if (rows.length === 0) {
    result.note =
      'Пустой ответ. Проверьте наличие продаж за окно либо сдвиньте dateFrom раньше.';
  }

  console.log(JSON.stringify(result, null, 2));
  return result;
}


/**
 * Возвращает дату N дней назад в формате YYYY-MM-DD.
 * Используется только для формирования dateFrom read-only probe.
 */
function salesProbeDateDaysAgo_(daysAgo) {
  var date = new Date();

  date.setDate(date.getDate() - Number(daysAgo || 0));

  return Utilities.formatDate(
    date,
    'Europe/Moscow',
    'yyyy-MM-dd'
  );
}


/**
 * Проверяет формат dateFrom.
 */
function salesProbeValidDate_(value) {
  var text = String(value || '');

  var match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(text);

  if (!match) {
    return false;
  }

  var year = Number(match[1]);
  var month = Number(match[2]);
  var day = Number(match[3]);

  if (year < 1 || month < 1 || month > 12) {
    return false;
  }

  var leapYear =
    (year % 4 === 0 && year % 100 !== 0) ||
    year % 400 === 0;

  var daysInMonth = [
    31,
    leapYear ? 29 : 28,
    31,
    30,
    31,
    30,
    31,
    31,
    30,
    31,
    30,
    31
  ][month - 1];

  return day >= 1 && day <= daysInMonth;
}


/**
 * Проверяет фактический формат lastChangeDate.
 *
 * Допускает разделитель T или пробел, чтобы probe мог показать
 * реальное поведение API, не изменяя данные.
 */
function salesProbeValidLastChangeDate_(value) {
  var text = String(value || '');

  var match =
    /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(\.\d+)?(?:Z|[+-]\d{2}:\d{2})?$/
      .exec(text);

  if (!match) {
    return false;
  }

  var year = Number(match[1]);
  var month = Number(match[2]);
  var day = Number(match[3]);
  var hour = Number(match[4]);
  var minute = Number(match[5]);
  var second = Number(match[6]);

  if (year < 1) {
    return false;
  }

  if (month < 1 || month > 12) {
    return false;
  }

  if (
    hour < 0 ||
    hour > 23 ||
    minute < 0 ||
    minute > 59 ||
    second < 0 ||
    second > 59
  ) {
    return false;
  }

  var leapYear =
    (year % 4 === 0 && year % 100 !== 0) ||
    year % 400 === 0;

  var daysInMonth = [
    31,
    leapYear ? 29 : 28,
    31,
    30,
    31,
    30,
    31,
    31,
    30,
    31,
    30,
    31
  ][month - 1];

  return day >= 1 && day <= daysInMonth;
}


/**
 * Маскирует внешние идентификаторы в диагностических примерах.
 *
 * Пример:
 * S123456789 → S…89#10
 */
function salesProbeMaskId_(value) {
  var text = String(value || '');

  if (!text) {
    return '';
  }

  if (text.length <= 4) {
    return text.charAt(0) + '***';
  }

  return (
    text.charAt(0) +
    '…' +
    text.slice(-2) +
    '#' +
    text.length
  );
}


/**
 * Округление до одного знака после запятой.
 */
function salesProbeRoundOne_(value) {
  return Math.round(Number(value || 0) * 10) / 10;
}


function probeWbSalesReadonly90Days() {
  return probeWbSalesReadonly('2026-04-13');
}