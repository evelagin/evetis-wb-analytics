/**
 * ══════════════════════════════════════════════════════════════
 * EVETIS WB — Code.gs  v1.1.0
 * Главный файл: точки входа, оркестрация сборки таблицы
 * ══════════════════════════════════════════════════════════════
 *
 * Изменения v1.1.0:
 *   • SpreadsheetApp.flush() после каждого шага setupWorkbook
 *   • Батчевый flush в createAllSheets_ (каждые 5 листов)
 *   • Таймаут-гард (4.5 мин) с возможностью продолжить
 *   • Тестовые функции для пошаговой отладки
 *   • Toast-уведомления о прогрессе вместо молчаливого ожидания
 *
 * setupWorkbook()   — полная сборка с нуля (листы + формат + валидации + защита)
 * createAllSheets() — только создание листов и заголовков
 * ══════════════════════════════════════════════════════════════
 */

// ───────────────────────────────────────
// КОНСТАНТЫ ВЫПОЛНЕНИЯ
// ───────────────────────────────────────

/** Максимальное время выполнения из меню — 6 мин, ставим порог 4.5 мин */
var MAX_EXECUTION_MS = 4.5 * 60 * 1000;

/** Сколько листов создавать до промежуточного flush */
var SHEETS_BATCH_SIZE = 5;


// ───────────────────────────────────────
// ТАЙМАУТ-ГАРД
// ───────────────────────────────────────

/**
 * Проверяет, не приближается ли тайм-аут.
 * @param {Date} startTime — время начала выполнения
 * @returns {boolean} true = пора останавливаться
 */
function isTimeLimitApproaching_(startTime) {
  return (new Date().getTime() - startTime.getTime()) > MAX_EXECUTION_MS;
}


// ───────────────────────────────────────
// ПОЛНАЯ НАСТРОЙКА ТАБЛИЦЫ
// ───────────────────────────────────────

/**
 * Главная функция: создаёт всю структуру таблицы с нуля.
 * Вызывается из меню «EVETIS WB → Полная настройка таблицы»
 * или вручную из редактора скриптов.
 *
 * Порядок:
 *   1. Создать все листы + заголовки          (тяжёлый)
 *   2. Создать лист «Настройки» (key-value)
 *   3. Создать лист «README»
 *   4. Применить форматирование               (тяжёлый)
 *   5. Применить выпадающие списки            (тяжёлый)
 *   6. Условное форматирование (OK/CHECK/ERROR)
 *   7. Защитить формульные/технические колонки
 *   8. Порядок вкладок + скрытие RAW + удаление «Лист1»
 */
function setupWorkbook() {
  var ui = SpreadsheetApp.getUi();

  // Подтверждение от пользователя
  var response = ui.alert(
    'EVETIS WB — Полная настройка',
    'Будут созданы все листы системы, применено форматирование, ' +
    'валидации и защита.\n\n' +
    'Существующие листы НЕ удаляются — обновляются заголовки и настройки.\n\n' +
    'Продолжить?',
    ui.ButtonSet.YES_NO
  );

  if (response !== ui.Button.YES) {
    ui.alert('Операция отменена.');
    return;
  }

  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var startTime = new Date();
  log_('═══ Начало полной настройки EVETIS WB v' + SYSTEM_VERSION + ' ═══');

  // Массив шагов: [label, function, isCritical]
  // isCritical=true — при тайм-ауте всё равно считаем результат удовлетворительным
  var steps = [
    ['📋 Шаг 1/8: Создание листов и заголовков',     function() { createAllSheets_(ss, startTime); }],
    ['⚙️ Шаг 2/8: Настройка листа «Настройки»',      function() { createSettingsSheet_(ss); }],
    ['📖 Шаг 3/8: Создание README',                   function() { createReadmeSheet_(ss); }],
    ['🎨 Шаг 4/8: Применение форматирования',         function() { applyAllFormatting_(ss); }],
    ['📝 Шаг 5/8: Применение валидаций',              function() { applyAllValidations_(ss); }],
    ['🚦 Шаг 6/8: Условное форматирование',           function() { applyAllConditionalFormatting_(ss); }],
    ['🔒 Шаг 7/8: Защита колонок',                    function() { protectAllFormulaColumns_(ss); }],
    ['📑 Шаг 8/8: Финализация',                       function() {
      reorderSheets_(ss);
      hideRawSheets_(ss);
      removeDefaultSheet_(ss);
    }]
  ];

  var completedSteps = 0;
  var timedOut = false;

  try {
    for (var i = 0; i < steps.length; i++) {
      // Проверка тайм-аута перед каждым шагом
      if (isTimeLimitApproaching_(startTime)) {
        log_('⏱️ Тайм-аут приближается, остановка после шага ' + i + '/' + steps.length);
        timedOut = true;
        break;
      }

      var label = steps[i][0];
      var fn    = steps[i][1];

      log_(label + '...');
      ss.toast(label, 'EVETIS WB', 3);

      fn();

      // ★ Ключевое исправление: flush после КАЖДОГО шага
      SpreadsheetApp.flush();
      completedSteps++;

      var stepTime = ((new Date() - startTime) / 1000).toFixed(1);
      log_('  ✓ ' + label + ' — ' + stepTime + ' сек с начала');
    }

    // Итог
    var elapsed = ((new Date() - startTime) / 1000).toFixed(1);

    if (timedOut) {
      log_('═══ Частичная настройка: ' + completedSteps + '/' + steps.length + ' шагов за ' + elapsed + ' сек ═══');
      ui.alert(
        '⏱️ Частичная настройка',
        'Выполнено ' + completedSteps + ' из ' + steps.length + ' шагов за ' + elapsed + ' сек.\n\n' +
        'Достигнут лимит времени Google Apps Script (6 мин).\n' +
        'Запустите ещё раз — уже созданные листы не будут затронуты,\n' +
        'скрипт продолжит с оставшихся шагов.\n\n' +
        'Или запустите нужные шаги вручную из меню «Обслуживание».',
        ui.ButtonSet.OK
      );
    } else {
      log_('═══ Настройка завершена за ' + elapsed + ' сек ═══');
      ui.alert(
        '✅ Готово!',
        'Структура EVETIS WB создана успешно.\n\n' +
        '• Создано листов: ' + SHEET_ORDER.length + '\n' +
        '• Шагов выполнено: ' + completedSteps + '/' + steps.length + '\n' +
        '• Время: ' + elapsed + ' сек\n\n' +
        'Следующий шаг: заполните лист «Настройки» и SKU_MASTER.',
        ui.ButtonSet.OK
      );
    }

  } catch (e) {
    var elapsed = ((new Date() - startTime) / 1000).toFixed(1);
    log_('❌ ОШИБКА на шаге ' + (completedSteps + 1) + ' после ' + elapsed + ' сек: ' + e.message);
    log_('Stack: ' + e.stack);
    ui.alert(
      '❌ Ошибка на шаге ' + (completedSteps + 1),
      'Выполнено шагов до ошибки: ' + completedSteps + '/' + steps.length + '\n' +
      'Время: ' + elapsed + ' сек\n\n' +
      'Ошибка:\n' + e.message + '\n\n' +
      'Подробности: View → Logs.\n' +
      'Попробуйте запустить оставшиеся шаги вручную из меню «Обслуживание».',
      ui.ButtonSet.OK
    );
  }
}


// ───────────────────────────────────────
// СОЗДАНИЕ ВСЕХ ЛИСТОВ
// ───────────────────────────────────────

/**
 * Публичная обёртка для меню — создаёт все листы.
 */
function createAllSheets() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  createAllSheets_(ss);
  SpreadsheetApp.flush();
  SpreadsheetApp.getUi().alert('✅ Все листы созданы / обновлены.');
}

/**
 * Внутренняя функция: итерация по SHEET_ORDER,
 * создание листов и установка заголовков из SHEETS_SCHEMA.
 *
 * ★ v1.1.0: flush каждые SHEETS_BATCH_SIZE листов + таймаут-гард
 *
 * @param {Spreadsheet} ss
 * @param {Date} [startTime] — опциональный таймер для тайм-аут проверки
 */
function createAllSheets_(ss, startTime) {
  var created = 0;
  var skipped = 0;

  for (var i = 0; i < SHEET_ORDER.length; i++) {
    // Таймаут-гард (если передан startTime)
    if (startTime && isTimeLimitApproaching_(startTime)) {
      log_('⏱️ createAllSheets_: тайм-аут, создано ' + created + ' листов из ' + SHEET_ORDER.length);
      break;
    }

    var sheetName = SHEET_ORDER[i];
    var schema = SHEETS_SCHEMA[sheetName];

    // Пропускаем листы без схемы (README создаётся отдельно)
    if (!schema) {
      log_('⏭️ Пропуск (нет схемы): ' + sheetName);
      skipped++;
      continue;
    }

    var sheet = getOrCreateSheet_(ss, sheetName);

    // Для табличных листов — ставим заголовки
    if (schema.type === 'table' && schema.headers && schema.headers.length > 0) {
      var numCols = schema.headers.length;

      // Записываем заголовки в строку 1
      sheet.getRange(1, 1, 1, numCols).setValues([schema.headers]);

      // Убираем лишние пустые колонки справа (если лист новый)
      var maxCols = sheet.getMaxColumns();
      if (maxCols > numCols) {
        try {
          sheet.deleteColumns(numCols + 1, maxCols - numCols);
        } catch (e) {
          // Может быть ошибка если колонки содержат данные — не критично
          log_('⚠️ Не удалось удалить лишние колонки: ' + sheetName);
        }
      }

      log_('✅ Заголовки (' + numCols + ' кол.): ' + sheetName);
    }

    // Для дашбордов и особых листов — просто создаём
    if (schema.type === 'dashboard' || schema.type === 'existing') {
      log_('ℹ️ Лист создан (контент позже): ' + sheetName);
    }

    created++;

    // ★ Батчевый flush: сбрасываем буфер каждые N листов
    if (created % SHEETS_BATCH_SIZE === 0) {
      SpreadsheetApp.flush();
      log_('💾 Flush после ' + created + ' листов');
    }
  }

  // Финальный flush остатка
  SpreadsheetApp.flush();
  log_('📋 createAllSheets_: создано ' + created + ', пропущено ' + skipped);
}


// ───────────────────────────────────────
// README
// ───────────────────────────────────────

/**
 * Создаёт лист README с описанием системы и навигацией.
 */
function createReadmeSheet_(ss) {
  var sheet = getOrCreateSheet_(ss, SHEET_NAMES.README);

  // Очищаем если уже был контент
  sheet.clear();

  var content = [
    ['EVETIS WB — Управленческая аналитика Wildberries'],
    [''],
    ['Версия системы: ' + SYSTEM_VERSION],
    ['Дата создания: ' + Utilities.formatDate(new Date(), 'Europe/Moscow', 'dd.MM.yyyy HH:mm')],
    [''],
    ['═══ СТРУКТУРА СИСТЕМЫ ═══'],
    [''],
    ['СЛОЙ 1 — НАСТРОЙКИ И СПРАВОЧНИКИ'],
    ['  • Настройки — ключевые параметры системы (API-ключи в Script Properties)'],
    ['  • SKU_MASTER — справочник товаров (артикулы, себестоимость, цены)'],
    ['  • BUNDLES — справочник наборов (комплекты → состав)'],
    ['  • COST_HISTORY — журнал изменений себестоимости'],
    [''],
    ['СЛОЙ 2 — СЫРЫЕ ДАННЫЕ API (скрытые листы)'],
    ['  • RAW_WB_ORDERS — заказы из WB API'],
    ['  • RAW_WB_SALES_RETURNS — продажи и возвраты'],
    ['  • RAW_WB_FINANCE — финансовые отчёты WB'],
    ['  • RAW_WB_ADS — рекламная статистика'],
    ['  • RAW_WB_STOCKS — остатки на складах WB'],
    [''],
    ['СЛОЙ 3 — ОЧИЩЕННЫЕ ДАННЫЕ'],
    ['  • CLEAN_WB_DAILY — нормализованные дневные данные'],
    ['  • ERRORS_CONTROL — журнал ошибок и расхождений'],
    [''],
    ['СЛОЙ 4 — АНАЛИТИКА И ОТЧЁТЫ'],
    ['  • DASHBOARD_WB — главный дашборд'],
    ['  • Воронка WB — воронка продаж'],
    ['  • ADS_WB — аналитика рекламы'],
    ['  • STOCKS_WB — управление остатками'],
    ['  • SUPPLY_PLAN — план поставок'],
    ['  • WAREHOUSE_ANALYTICS — аналитика по складам'],
    ['  • UNIT_SKU_DAILY — юнит-экономика по SKU'],
    ['  • PNL_TOTAL — общий P&L'],
    ['  • ABC_XYZ — ABC/XYZ анализ'],
    [''],
    ['ОПЕРАЦИОННЫЕ ЛИСТЫ'],
    ['  • FULFILLMENT — параметры фулфилмента'],
    ['  • BANK_EXPENSES — расходы ИП (банк, зарплаты, аренда)'],
    ['  • TAX_USN — расчёт УСН 15%'],
    [''],
    ['═══ ВАЖНЫЕ ПРАВИЛА ═══'],
    [''],
    ['1. RAW-листы — неизменяемые. Данные из API записываются как пришли.'],
    ['2. API-токены хранятся в Script Properties (не в ячейках).'],
    ['3. P&L, cashflow, УСН и выплаты WB — разные сущности, не смешивать.'],
    ['4. Двойной учёт расходов запрещён.'],
    ['5. Серые колонки — формулы/API. Белые — ручной ввод.'],
    [''],
    ['═══ БЫСТРЫЙ СТАРТ ═══'],
    [''],
    ['1. Заполните лист «Настройки» — имена API-ключей'],
    ['2. Сохраните реальные ключи: меню EVETIS WB → О системе'],
    ['3. Заполните SKU_MASTER — все активные товары'],
    ['4. Заполните COST_HISTORY — текущая себестоимость'],
    ['5. Запустите загрузку данных из API (когда будет готов модуль)']
  ];

  // Записываем контент одним вызовом
  sheet.getRange(1, 1, content.length, 1).setValues(content);

  // Форматирование заголовка
  sheet.getRange(1, 1)
    .setFontSize(16)
    .setFontWeight('bold')
    .setFontColor(COLORS.ANALYTICS_HEADER_BG);

  // Форматирование заголовков разделов
  var sectionRows = [6, 8, 14, 21, 25, 36, 41, 48];
  for (var i = 0; i < sectionRows.length; i++) {
    sheet.getRange(sectionRows[i], 1)
      .setFontWeight('bold')
      .setFontSize(11);
  }

  // Версия и дата
  sheet.getRange(3, 1, 2, 1)
    .setFontColor('#6B7280')
    .setFontStyle('italic');

  // Ширина колонки
  sheet.setColumnWidth(1, 650);

  // Заморозка заголовка
  sheet.setFrozenRows(1);

  log_('✅ README создан');
}


// ───────────────────────────────────────
// ОТДЕЛЬНЫЕ КОМАНДЫ (вызываются из Menu.gs)
// ───────────────────────────────────────

// createAllSheets — публичная функция определена выше

// refreshFormatting()   — определена в Menu.gs как обёртка
// refreshValidations()  — определена в Menu.gs как обёртка
// refreshProtections()  — определена в Menu.gs как обёртка


// ───────────────────────────────────────
// ДИАГНОСТИКА
// ───────────────────────────────────────

/**
 * Проверка здоровья системы — все ли листы на месте,
 * правильные ли заголовки, есть ли обязательные настройки.
 */
function systemHealthCheck() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var ui = SpreadsheetApp.getUi();
  var issues = [];
  var ok = 0;

  // Проверяем наличие всех листов
  for (var i = 0; i < SHEET_ORDER.length; i++) {
    var sheetName = SHEET_ORDER[i];
    var sheet = ss.getSheetByName(sheetName);
    if (!sheet) {
      issues.push('❌ Лист отсутствует: ' + sheetName);
    } else {
      ok++;
      // Проверяем заголовки для табличных листов
      var schema = SHEETS_SCHEMA[sheetName];
      if (schema && schema.type === 'table' && schema.headers) {
        var firstHeader = sheet.getRange(1, 1).getValue();
        if (firstHeader !== schema.headers[0]) {
          issues.push('⚠️ Неверный первый заголовок: ' + sheetName +
                       ' (ожидалось «' + schema.headers[0] + '», найдено «' + firstHeader + '»)');
        }
      }
    }
  }

  // Проверяем наличие API-ключей в Script Properties
  var requiredProps = ['WB_TOKEN_STANDARD', 'WB_TOKEN_STATISTICS'];
  for (var j = 0; j < requiredProps.length; j++) {
    var val = getScriptProperty_(requiredProps[j]);
    if (!val) {
      issues.push('⚠️ Не задан Script Property: ' + requiredProps[j]);
    }
  }

  // Результат
  var message = '✅ Листов найдено: ' + ok + ' из ' + SHEET_ORDER.length + '\n\n';
  if (issues.length === 0) {
    message += 'Проблем не обнаружено!';
  } else {
    message += 'Обнаружено проблем: ' + issues.length + '\n\n' + issues.join('\n');
  }

  ui.alert('🩺 Диагностика системы', message, ui.ButtonSet.OK);
}


// ═══════════════════════════════════════════════════════════════
// ПОШАГОВЫЕ ТЕСТОВЫЕ ФУНКЦИИ
// Запускать из редактора скриптов для отладки зависаний.
// Каждая функция выполняет один шаг и выводит время в Logger.
// ═══════════════════════════════════════════════════════════════

function testStep1_CreateSheets() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var t = new Date();
  createAllSheets_(ss);
  SpreadsheetApp.flush();
  Logger.log('Шаг 1 (листы): ' + ((new Date() - t) / 1000).toFixed(1) + ' сек');
}

function testStep2_Settings() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var t = new Date();
  createSettingsSheet_(ss);
  SpreadsheetApp.flush();
  Logger.log('Шаг 2 (настройки): ' + ((new Date() - t) / 1000).toFixed(1) + ' сек');
}

function testStep3_Readme() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var t = new Date();
  createReadmeSheet_(ss);
  SpreadsheetApp.flush();
  Logger.log('Шаг 3 (README): ' + ((new Date() - t) / 1000).toFixed(1) + ' сек');
}

function testStep4_Formatting() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var t = new Date();
  applyAllFormatting_(ss);
  SpreadsheetApp.flush();
  Logger.log('Шаг 4 (форматирование): ' + ((new Date() - t) / 1000).toFixed(1) + ' сек');
}

function testStep5_Validations() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var t = new Date();
  applyAllValidations_(ss);
  SpreadsheetApp.flush();
  Logger.log('Шаг 5 (валидации): ' + ((new Date() - t) / 1000).toFixed(1) + ' сек');
}

function testStep6_ConditionalFormatting() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var t = new Date();
  applyAllConditionalFormatting_(ss);
  SpreadsheetApp.flush();
  Logger.log('Шаг 6 (условное форматирование): ' + ((new Date() - t) / 1000).toFixed(1) + ' сек');
}

function testStep7_Protections() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var t = new Date();
  protectAllFormulaColumns_(ss);
  SpreadsheetApp.flush();
  Logger.log('Шаг 7 (защита): ' + ((new Date() - t) / 1000).toFixed(1) + ' сек');
}

function testStep8_Finalize() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var t = new Date();
  reorderSheets_(ss);
  hideRawSheets_(ss);
  removeDefaultSheet_(ss);
  SpreadsheetApp.flush();
  Logger.log('Шаг 8 (финализация): ' + ((new Date() - t) / 1000).toFixed(1) + ' сек');
}
