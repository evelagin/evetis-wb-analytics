-- ============================================================================
-- UNITKA Stage E4 — ПРОВЕРКА СИНТАКСИСА транзакционного скрипта replaceWindow.
--
-- Что это. Точная копия procedural-скрипта из cloud/src/loaders/storage/bq.ts
-- (StorageBq.replaceWindow, коммит c7ecc3f) с реальными идентификаторами таблиц.
-- Единственное назначение файла — скормить скрипт валидатору GoogleSQL и убедиться,
-- что он разбирается. Файл НЕ предназначен для выполнения и выполнить замену не может.
--
-- Зачем понадобился. Первый production-прогон wb-paid-storage-prod-dxkt5 (14.09.2026)
-- упал на `Syntax error: Unexpected keyword ROWS at [1:20]`. SQL жил в шаблонной строке
-- TypeScript, поэтому ни tsc, ни eslint, ни vitest его не разбирают, а теневого контура
-- у загрузчика нет: он prodOnly и вне prod падает с STORAGE_PROD_ONLY. Первым, кто
-- разобрал запрос, оказался продовый BigQuery. Этот файл закрывает именно тот разрыв.
--
-- ТРИ НЕЗАВИСИМЫХ СЛОЯ ЗАЩИТЫ ОТ ЗАПИСИ (достаточно любого одного):
--   1. Первый оператор скрипта безусловно поднимает ошибку. Если файл запустить, а не
--      проверить, выполнение прекращается на нём, и до транзакции дело не доходит.
--   2. Литералы-«пустышки»: p_expected_rows / p_expected_days = -1, p_expected_rub = -1.0.
--      COUNT(*) и SUM никогда не равны -1, поэтому первый же gate внутри транзакции
--      поднял бы ошибку ДО DELETE, а обработчик сделал бы ROLLBACK.
--      Настоящие значения приходят из loader параметрами; здесь они недостижимы.
--   3. Оба DML-оператора обёрнуты в `IF FALSE THEN … END IF`. Парсер их разбирает
--      (ровно это нам и нужно), исполнение в них не заходит никогда.
--
-- ЧТО ЭТОТ ФАЙЛ ДОКАЗЫВАЕТ:
--   • корректность DECLARE с несколькими переменными и типами INT64 / FLOAT64 / STRING;
--   • допустимость BEGIN TRANSACTION … COMMIT TRANSACTION внутри блока BEGIN … END;
--   • корректность SET <var> = (SELECT …) внутри транзакции;
--   • корректность IF / RAISE USING MESSAGE = FORMAT(… %t …) как gate-ов;
--   • корректность обработчика EXCEPTION WHEN ERROR THEN ROLLBACK TRANSACTION; RAISE …;
--   • разрешимость всех имён столбцов в реальных таблицах;
--   • отсутствие зарезервированных слов GoogleSQL в роли алиасов.
--
-- ЧЕГО НЕ ДОКАЗЫВАЕТ:
--   • поведение транзакции в рантайме (изоляция, read-your-writes, откат) — это
--     проверяется только настоящим прогоном;
--   • типизацию query-параметров @start / @expectedRows / … — их объявляет клиент
--     в поле `types`, здесь они заменены на DECLARE … DEFAULT с теми же типами;
--   • что WB вернёт данные за запрошенное окно.
--
-- Как проверять — см. раздел «ИНСТРУКЦИЯ» в конце файла.
-- ============================================================================


-- ─────────────────────────────────────────────────────────────────────────────
-- ЧАСТЬ A — основной вариант. Требует, чтобы таблица RAW_WB_PAID_STORAGE__STAGE
-- уже существовала (её создаёт terraform apply по infra/terraform/wb_paid_storage_loader.tf).
-- На 14.09.2026 таблицы ЕЩЁ НЕТ: apply не выполнялся, и валидатор вернёт
-- «Not found: Table … RAW_WB_PAID_STORAGE__STAGE». Это ожидаемо — запускайте часть A
-- ПОСЛЕ terraform apply. До apply пользуйтесь частью B ниже.
-- ─────────────────────────────────────────────────────────────────────────────

-- Слой защиты 1: безусловная остановка до любой транзакции.
BEGIN
  RAISE USING MESSAGE =
    'E4 SYNTAX CHECK ONLY. Этот файл предназначен для валидации синтаксиса, а не для запуска. '
    'Настоящую замену окна выполняет Job wb-paid-storage-prod. Ничего не записано.';
END;

BEGIN
  DECLARE stage_rows, stage_days, stage_outside, target_rows, target_days INT64;
  DECLARE stage_rub, target_rub FLOAT64;

  -- Слой защиты 2: вместо @-параметров — заведомо недостижимые литералы.
  DECLARE p_start          STRING  DEFAULT '2026-09-10';
  DECLARE p_end            STRING  DEFAULT '2026-09-13';
  DECLARE p_expected_rows  INT64   DEFAULT -1;
  DECLARE p_expected_days  INT64   DEFAULT -1;
  DECLARE p_expected_rub   FLOAT64 DEFAULT -1.0;

  BEGIN TRANSACTION;

  -- Gate 1: stage обязан быть ровно тем, что мы собираемся вставить. Проверяется ДО DELETE,
  -- поэтому на этом этапе target не тронут вообще.
  SET stage_rows = (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_PAID_STORAGE__STAGE`);
  SET stage_days = (SELECT COUNT(DISTINCT date_msk) FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_PAID_STORAGE__STAGE`);
  SET stage_outside = (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_PAID_STORAGE__STAGE` WHERE NOT (date_msk BETWEEN DATE(p_start) AND DATE(p_end)));
  SET stage_rub = (SELECT CAST(ROUND(IFNULL(SUM(warehouse_price), 0), 2) AS FLOAT64) FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_PAID_STORAGE__STAGE`);
  IF stage_rows != p_expected_rows THEN
    RAISE USING MESSAGE = FORMAT('WB_STORAGE_STAGE_ROWS: ожидалось %t, в stage %t', p_expected_rows, stage_rows);
  END IF;
  IF stage_outside != 0 THEN
    RAISE USING MESSAGE = FORMAT('WB_STORAGE_STAGE_OUT_OF_WINDOW: %t строк вне окна %t..%t', stage_outside, p_start, p_end);
  END IF;
  IF stage_days != p_expected_days THEN
    RAISE USING MESSAGE = FORMAT('WB_STORAGE_STAGE_DAYS: ожидалось %t, в stage %t', p_expected_days, stage_days);
  END IF;
  IF ABS(stage_rub - p_expected_rub) > 0.02 THEN
    RAISE USING MESSAGE = FORMAT('WB_STORAGE_STAGE_AMOUNT: источник %t, stage %t', p_expected_rub, stage_rub);
  END IF;

  -- Атомарная замена окна. Гранулярность — окно дат, всё вне [start, end] не затрагивается.
  -- Слой защиты 3: `IF FALSE` — парсер разбирает оба оператора, исполнение сюда не заходит.
  IF FALSE THEN
    DELETE FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_PAID_STORAGE` WHERE date_msk BETWEEN DATE(p_start) AND DATE(p_end);
    INSERT INTO `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_PAID_STORAGE` SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_PAID_STORAGE__STAGE`;
  END IF;

  -- Gate 2: тот же post-load QA, что раньше выполнялся ПОСЛЕ коммита. Транзакция видит
  -- собственные изменения, поэтому проверка честная — но провал ещё откатывается.
  SET target_rows = (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_PAID_STORAGE` WHERE date_msk BETWEEN DATE(p_start) AND DATE(p_end));
  SET target_days = (SELECT COUNT(DISTINCT date_msk) FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_PAID_STORAGE` WHERE date_msk BETWEEN DATE(p_start) AND DATE(p_end));
  SET target_rub = (SELECT CAST(ROUND(IFNULL(SUM(warehouse_price), 0), 2) AS FLOAT64) FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_PAID_STORAGE` WHERE date_msk BETWEEN DATE(p_start) AND DATE(p_end));
  IF target_rows != p_expected_rows THEN
    RAISE USING MESSAGE = FORMAT('WB_STORAGE_TARGET_ROWS: ожидалось %t, в target %t', p_expected_rows, target_rows);
  END IF;
  IF target_days != p_expected_days THEN
    RAISE USING MESSAGE = FORMAT('WB_STORAGE_TARGET_DAYS: ожидалось %t, в target %t', p_expected_days, target_days);
  END IF;
  IF ABS(target_rub - p_expected_rub) > 0.02 THEN
    RAISE USING MESSAGE = FORMAT('WB_STORAGE_TARGET_AMOUNT: источник %t, target %t', p_expected_rub, target_rub);
  END IF;

  COMMIT TRANSACTION;
EXCEPTION WHEN ERROR THEN
  ROLLBACK TRANSACTION;
  RAISE USING MESSAGE = @@error.message;
END;


-- ─────────────────────────────────────────────────────────────────────────────
-- ЧАСТЬ B — вариант «до terraform apply». Отличается от части A РОВНО ОДНИМ:
-- вместо несуществующей пока RAW_WB_PAID_STORAGE__STAGE подставлена существующая
-- RAW_WB_PAID_STORAGE (схемы идентичны — см. infra/terraform/wb_paid_storage_loader.tf,
-- local.storage_schema, и sql/ddl/wb_raw_paid_storage.sql). Все рискованные конструкции
-- — DECLARE, BEGIN/COMMIT/ROLLBACK TRANSACTION, SET из подзапроса, IF/RAISE/FORMAT,
-- EXCEPTION-обработчик, @@error.message — проверяются полностью; непроверенным
-- остаётся только литеральное имя stage-таблицы.
--
-- Чтобы прогнать часть B отдельно, закомментируйте часть A (или скопируйте в Console
-- только блок ниже вместе с предшествующим ему блоком-остановкой).
-- ─────────────────────────────────────────────────────────────────────────────

BEGIN
  RAISE USING MESSAGE =
    'E4 SYNTAX CHECK ONLY (часть B). Валидация синтаксиса, не запуск. Ничего не записано.';
END;

BEGIN
  DECLARE stage_rows, stage_days, stage_outside, target_rows, target_days INT64;
  DECLARE stage_rub, target_rub FLOAT64;

  DECLARE p_start          STRING  DEFAULT '2026-09-10';
  DECLARE p_end            STRING  DEFAULT '2026-09-13';
  DECLARE p_expected_rows  INT64   DEFAULT -1;
  DECLARE p_expected_days  INT64   DEFAULT -1;
  DECLARE p_expected_rub   FLOAT64 DEFAULT -1.0;

  BEGIN TRANSACTION;

  SET stage_rows = (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_PAID_STORAGE`);
  SET stage_days = (SELECT COUNT(DISTINCT date_msk) FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_PAID_STORAGE`);
  SET stage_outside = (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_PAID_STORAGE` WHERE NOT (date_msk BETWEEN DATE(p_start) AND DATE(p_end)));
  SET stage_rub = (SELECT CAST(ROUND(IFNULL(SUM(warehouse_price), 0), 2) AS FLOAT64) FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_PAID_STORAGE`);
  IF stage_rows != p_expected_rows THEN
    RAISE USING MESSAGE = FORMAT('WB_STORAGE_STAGE_ROWS: ожидалось %t, в stage %t', p_expected_rows, stage_rows);
  END IF;
  IF stage_outside != 0 THEN
    RAISE USING MESSAGE = FORMAT('WB_STORAGE_STAGE_OUT_OF_WINDOW: %t строк вне окна %t..%t', stage_outside, p_start, p_end);
  END IF;
  IF stage_days != p_expected_days THEN
    RAISE USING MESSAGE = FORMAT('WB_STORAGE_STAGE_DAYS: ожидалось %t, в stage %t', p_expected_days, stage_days);
  END IF;
  IF ABS(stage_rub - p_expected_rub) > 0.02 THEN
    RAISE USING MESSAGE = FORMAT('WB_STORAGE_STAGE_AMOUNT: источник %t, stage %t', p_expected_rub, stage_rub);
  END IF;

  IF FALSE THEN
    DELETE FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_PAID_STORAGE` WHERE date_msk BETWEEN DATE(p_start) AND DATE(p_end);
    INSERT INTO `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_PAID_STORAGE` SELECT * FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_PAID_STORAGE`;
  END IF;

  SET target_rows = (SELECT COUNT(*) FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_PAID_STORAGE` WHERE date_msk BETWEEN DATE(p_start) AND DATE(p_end));
  SET target_days = (SELECT COUNT(DISTINCT date_msk) FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_PAID_STORAGE` WHERE date_msk BETWEEN DATE(p_start) AND DATE(p_end));
  SET target_rub = (SELECT CAST(ROUND(IFNULL(SUM(warehouse_price), 0), 2) AS FLOAT64) FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_PAID_STORAGE` WHERE date_msk BETWEEN DATE(p_start) AND DATE(p_end));
  IF target_rows != p_expected_rows THEN
    RAISE USING MESSAGE = FORMAT('WB_STORAGE_TARGET_ROWS: ожидалось %t, в target %t', p_expected_rows, target_rows);
  END IF;
  IF target_days != p_expected_days THEN
    RAISE USING MESSAGE = FORMAT('WB_STORAGE_TARGET_DAYS: ожидалось %t, в target %t', p_expected_days, target_days);
  END IF;
  IF ABS(target_rub - p_expected_rub) > 0.02 THEN
    RAISE USING MESSAGE = FORMAT('WB_STORAGE_TARGET_AMOUNT: источник %t, target %t', p_expected_rub, target_rub);
  END IF;

  COMMIT TRANSACTION;
EXCEPTION WHEN ERROR THEN
  ROLLBACK TRANSACTION;
  RAISE USING MESSAGE = @@error.message;
END;


-- ============================================================================
-- ИНСТРУКЦИЯ — как проверить, НИЧЕГО не выполняя
--
-- Вариант 1. BigQuery Console (рекомендую, кнопку «Запустить» жать не нужно).
--   1. console.cloud.google.com/bigquery, проект project-fa311fc0-4d87-4781-986.
--   2. «Создать запрос» → вставить нужную часть (A или B) целиком.
--   3. Обработку региона задать EU: «Ещё» → «Настройки запроса» → Location = EU.
--   4. Смотреть на индикатор валидации в правом нижнем углу редактора — Console
--      проверяет текст на лету:
--        • зелёная галочка = СИНТАКСИС ВАЛИДЕН. Для multi-statement скриптов оценка
--          объёма данных может отсутствовать или быть «unknown» — это нормально
--          и ошибкой не является; значим сам факт отсутствия красной ошибки;
--        • красный значок = ошибка, с текстом и позицией [строка:колонка].
--      Индикатор — это и есть dry run: BigQuery разбирает скрипт и НЕ выполняет его.
--   5. Ничего больше не делать. Кнопку «Запустить» не нажимать.
--
-- Вариант 2. bq CLI, явный dry run (тоже ничего не выполняет):
--   bq query --use_legacy_sql=false --dry_run --location=EU \
--     < docs/unitka_engine_evidence/storage/e4_transaction_syntax_check.sql
--   Успех: «Query successfully validated. Assuming the tables are not modified,
--   running this query will process N bytes of data.» Ошибка: текст и позиция [строка:колонка].
--
-- Как читать результат:
--   • «Query successfully validated» / зелёная галочка → скрипт разбирается,
--     класс ошибок вроде `Unexpected keyword ROWS at [1:20]` исключён.
--   • «Not found: Table … RAW_WB_PAID_STORAGE__STAGE» в части A до terraform apply —
--     ожидаемо и не является синтаксической ошибкой; проверяйте частью B.
--   • Любая иная ошибка → прислать текст с позицией, поправлю bq.ts до деплоя.
--
-- Что делать, если файл всё-таки запустили: ничего. Первый же оператор поднимает
-- ошибку, скрипт прекращается, DML недостижим по трём независимым причинам.
-- Ни одна строка RAW_WB_PAID_STORAGE не изменится.
--
-- Источник истины — cloud/src/loaders/storage/bq.ts (StorageBq.replaceWindow).
-- При правке скрипта в коде обновить и этот файл: он доказательство, а не копия «на память».
-- ============================================================================
