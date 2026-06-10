WB API Validation Results — EVETIS WB Analytics

Результат фактической проверки WB API по docs/wb_api_validation_checklist.md. Финальная версия (ревизия 3).
Метод проверки: официальная документация WB API (dev.wildberries.ru, Release Notes, разделы Reports / Financial Reports / Promotion) + перекрёстная сверка с рабочим кодом проекта.
Тестовые API-запросы по кабинету EVETIS не выполнялись. Все рабочие блоки имеют статус «подтверждено по документации / требует тестового запроса». Deprecated-методы помечены отдельно.
Код не меняется, новые функции не пишутся, структура Google Sheets не меняется. Excel/Drive — только legacy / сверка / fallback. Себестоимость в WB API не ищется.

1. Краткий итог
Критические изменения архитектуры (главное):

Финансовый отчёт реализации. GET /api/v5/supplier/reportDetailByPeriod — deprecated, удаление 15 июля. Это legacy / текущий рабочий источник проекта, не целевой канон. Целевой источник API-only — POST https://finance-api.wildberries.ru/api/finance/v1/sales-reports/detailed (token Finance).
Остатки. GET /api/v1/supplier/stocks — deprecated, удаление 23 июня, не целевой источник. Целевые кандидаты — GET /api/v1/warehouse_remains и POST /api/analytics/v1/stocks-report/wb-warehouses (оба Analytics; канон ещё не выбран).
Заказы. GET /api/v1/supplier/orders (Main Reports) — остаётся операционным источником (воронка заказов). Явной пометки Deprecated на странице метода нет. Не финансовый факт. Мониторить Release Notes.
Продажи. GET /api/v1/supplier/sales (Main Reports) — остаётся операционным источником (операционные продажи/возвраты). Явной пометки Deprecated нет. Не финансовый факт — финансовый факт берётся из нового Finance API. Мониторить Release Notes.

Подтверждено по документации (требует тестового запроса):

Финотчёт реализации — новый sales-reports/detailed (Finance) как целевой; старый reportDetailByPeriod как legacy/сверка до 15 июля.
Логистика / хранение / удержания / приёмка — поля в отчёте реализации (новом или legacy) + отдельные task-based отчёты (paid_storage, acceptance_report).
Остатки — два целевых кандидата (warehouse_remains, stocks-report/wb-warehouses).
Реклама — GET /adv/v3/fullstats (текущий) и GET /adv/v1/upd (история списаний).
Связка date + nmId + advertId + spend — структурно есть в /adv/v3/fullstats (days[].date → apps[].nms[].{nmId, sum}), но SKU-фактом становится только после сверки sum с /adv/v1/upd и кабинетом.

Не из WB API (by design):

Себестоимость (COGS) — только из SKU_MASTER, COST_HISTORY, BUNDLES.

Нельзя кодить до уточнения/решения:

Целевой загрузчик финансов на новый Finance API — до подтверждения схемы запроса/ответа и таблицы соответствия полей.
Остатки — до выбора канона между двумя целевыми кандидатами (старый supplier/stocks нельзя закреплять).
Реклама как SKU-факт — до сверки Σ sum ↔ updSum ↔ кабинет.
Хранение / приёмка — до выбора единого канона (двойной учёт).

2. Проверенные источники WB API
БлокРаздел WB APIEndpoint / методHTTPTokenСтатусЗаказыMain Reportsstatistics-api…/api/v1/supplier/ordersGETStatisticsоперационный источник / тест / мониторить Release NotesПродажиMain Reportsstatistics-api…/api/v1/supplier/salesGETStatisticsоперационный источник / тест / мониторить Release NotesВозвратыSales / Finance…/supplier/sales + новый Finance-отчётGET/POSTStatistics/Financeподтв. по докум. / тестОстатки (legacy)Main Reportsstatistics-api…/api/v1/supplier/stocksGETStatisticslegacy / deprecated (удаление 23 июня)Остатки (кандидат A)Warehouses Remains Reportseller-analytics-api…/api/v1/warehouse_remains (+ tasks/status, /download)GET (task)Analyticsцелевой кандидат / тестОстатки (кандидат B)Stocks Reportseller-analytics-api…/api/analytics/v1/stocks-report/wb-warehousesPOSTAnalyticsцелевой кандидат / тестФинотчёт (legacy)Financial Reportsstatistics-api…/api/v5/supplier/reportDetailByPeriodGETStatisticslegacy / deprecated (удаление 15 июля)Финотчёт (целевой)Financial Reportsfinance-api…/api/finance/v1/sales-reports/detailedPOSTFinanceподтв. по докум. / тестФинотчёт — списокFinancial Reportsfinance-api…/api/finance/v1/sales-reports/listPOSTFinanceподтв. по докум. / тестФинотчёт — по IDFinancial Reportsfinance-api…/api/finance/v1/sales-reports/detailed/{reportId}POSTFinanceподтв. по докум. / тестЛогистикаФинотчёт (поле)поле логистики в отчёте реализацииPOST/GETFinance/Statisticsподтв. по докум. / тестХранениеФинотчёт + Paid Storageотчёт реализации + seller-analytics-api…/api/v1/paid_storagePOST/GET (task)Finance/Analyticsподтв. по докум. / тестУдержанияФинотчёт (поля)поля удержаний/штрафов в отчёте реализацииPOST/GETFinance/StatisticsчастичноПлатная приёмкаФинотчёт + Paid Receptionотчёт реализации + seller-analytics-api…/api/v1/acceptance_reportPOST/GET (task)Finance/Analyticsподтв. по докум. / тестРекламаPromotion / Statisticsadvert-api…/adv/v3/fullstatsGETPromotionподтв. по докум. / тестРекламные расходыPromotion / Financeadvert-api…/adv/v1/updGETPromotionподтв. по докум. / тестРеклама date+nmId+advertId+spendPromotion/adv/v3/fullstats (nms[].sum)GETPromotionсвязка есть, сверка TBDSKU_MASTER/COST_HISTORY/BUNDLES— (не WB API)внутренние листы——вне API
3. Детальная проверка по блокам
Блок 1. Заказы
1. Назначение. Кол-во и сумма заказов для воронки заказов и операционного мониторинга.
2. API-источник.

Раздел: Statistics → Main Reports.
Endpoint: https://statistics-api.wildberries.ru/api/v1/supplier/orders, GET, token Statistics.
Статус: подтверждено по документации / операционный источник / требует тестового запроса / мониторить Release Notes. Явной пометки Deprecated на странице метода нет.

3. Параметры. Обязательные: dateFrom (RFC3339). Доп.: flag (0 — инкремент по lastChangeDate; 1 — все заказы за дату). Лимит ~80 000 строк/ответ, пагинация по lastChangeDate. История — не более 90 дней. Данные обновляются ~каждые 30 минут.
4. Поля ответа. Даты: date, lastChangeDate, cancelDate. Товар: supplierArticle, nmId, barcode, category, subject, brand, techSize. Финансы: totalPrice, discountPercent, spp, finishedPrice, priceWithDisc. Идентификаторы: srid, gNumber, sticker. Статус: isCancel.
5. RAW-слой. RAW_WB_ORDERS. Детализация: дата / nmId / srid. Дедуп: srid. Ключи: srid, nmId, barcode, supplierArticle → internal_sku.
6. SKU-привязка. Да, через nmId/barcode/supplierArticle. Риск: unmatched nmId.
7. P&L. Воронка / операционный показатель. Не финансовый факт (для финрасчётов — Finance API, блок 5). Суммы заказов ≠ реализации.
8. Риски. Предварительные данные; 90 дней истории; отменённые заказы (isCancel); не использовать как финансовый факт. Мониторить Release Notes на случай изменения статуса метода.
9. Решение. Операционный источник, подтверждён по документации. В API-only контуре — операционный слой (воронка), не финансовый факт. Следующий шаг: тестовый запрос за контрольный период, проверка srid/nmId и доли unmatched.
Блок 2. Продажи
1. Назначение. Кол-во продаж и операционная выручка; операционный источник возвратов.
2. API-источник.

Endpoint: https://statistics-api.wildberries.ru/api/v1/supplier/sales, GET, token Statistics.
Статус: подтверждено по документации / операционный источник / требует тестового запроса / мониторить Release Notes. Явной пометки Deprecated на странице метода нет.
Денежный факт продаж — из нового Finance-отчёта (блок 5).

3. Параметры. dateFrom (обязат.), flag (0/1). Лимит ~80 000 строк, пагинация по lastChangeDate. История — 90 дней.
4. Поля ответа. Как в заказах + saleID, forPay, paymentSaleAmount.
5. RAW-слой. RAW_WB_SALES_RETURNS. Дедуп: srid/saleID. Ключи: srid, nmId, barcode → internal_sku.
6. SKU-привязка. Да, через nmId/barcode.
7. P&L. Операционная выручка/возвраты. Не финансовый факт — финансовый факт из Finance API. Риск двойного учёта выручки, если подавать в P&L и sales, и финотчёт.
8. Риски. forPay здесь предварительный, не равен факту из отчёта реализации; возвраты в общем потоке (блок 3). Мониторить Release Notes.
9. Решение. Операционный источник, подтверждён по документации. Следующий шаг: тестовый запрос + сверка операционной выручки sales vs Finance-отчёт.
Блок 3. Возвраты
1. Назначение. Кол-во и сумма возвратов как вычет из выручки.
2. API-источник.

Операционно: …/supplier/sales (возврат маркируется saleID).
Финансово (целевой): новый Finance-отчёт реализации (sales-reports/detailed) — тип документа «Возврат», суммы со знаком.
Доп.: seller-analytics-api…/api/v1/analytics/goods-return (Analytics, вспомогательный).

3. Параметры. Наследует sales / Finance-отчёт соответственно.
4. Поля. saleID, srid, nmId, суммы со знаком; в Finance-отчёте — тип операции/документа.
5. RAW-слой. RAW_WB_SALES_RETURNS (is_return) для счёта; RAW_WB_FINANCE для денежного факта.
6. SKU-привязка. Да, через nmId/srid.
7. P&L. Вычет из выручки. Канон — Finance-отчёт реализации.
8. Риски. Двойной учёт (sales + финотчёт); сдвиг периода.
9. Решение. Денежный факт — только из Finance-отчёта. Следующий шаг: тестовая сверка.
Блок 4. Остатки
1. Назначение. Остатки на складах для оборачиваемости и плана поставок (не P&L).
2. API-источник.

Legacy: https://statistics-api.wildberries.ru/api/v1/supplier/stocks (GET, Statistics) — legacy / deprecated, удаление 23 июня. Не целевой источник.
Целевой кандидат A: GET https://seller-analytics-api.wildberries.ru/api/v1/warehouse_remains — task-based сводный отчёт остатков (token Analytics; create task → /tasks/{task_id}/status → /tasks/{task_id}/download; отчёт хранится 2 часа).
Целевой кандидат B: POST https://seller-analytics-api.wildberries.ru/api/analytics/v1/stocks-report/wb-warehouses — новый метод текущих остатков на складах WB, добавлен как замена supplier/stocks (token Analytics; параметры и поля — требуют отдельной проверки).
Статус: оба целевые кандидаты / требуется тестовый запрос / требуется выбрать канон. Заранее не утверждаем, какой станет финальным.

3. Параметры.

Кандидат A (warehouse_remains). Группировки (любая комбинация): groupByBrand, groupBySubject, groupBySa, groupByNm (при true появляется volume), groupByBarcode, groupBySize; фильтры filterPics, filterVolume; locale. История — текущий снимок (ретро-история — через Stocks Report).
Кандидат B (stocks-report/wb-warehouses). POST; параметры тела/фильтры и доступность истории — TBD / требует проверки.

4. Поля ответа.

Кандидат A: brand, subjectName, vendorCode, nmId, barcode, techSize, volume, quantity, массив warehouses[] (разбивка количества по складам).
Кандидат B: TBD / требует проверки (ожидаемо — текущие остатки по складам WB с nmId/складом; подтвердить тестом).

5. RAW-слой. RAW_WB_STOCKS. Детализация: snapshot-дата / nmId / склад. Дедуп: snapshot-дата + nmId + склад (row_hash). Ключи: nmId, barcode, vendorCode, склад → internal_sku.

Формирование snapshot: при каждой регулярной загрузке проставлять единую snapshot_date и разворачивать остатки в строки snapshot_date × nmId × склад. Оба кандидата отдают текущее состояние, поэтому историю фиксирует проект (регулярный запуск).

6. SKU-привязка. Да, через nmId/barcode/vendorCode. Риск: один nmId на нескольких складах — корректно суммировать.
7. P&L. Не P&L (оборачиваемость, supply planning).
8. Риски. Старый supplier/stocks отключается 23 июня — нельзя закреплять как источник новой архитектуры; у кандидата B параметры/поля ещё не проверены; разные механики (task-based vs прямой POST) → разный код загрузчика.
9. Решение. Перейти с supplier/stocks на один из двух целевых кандидатов. Следующий шаг: тестовый запрос обоих методов (A и B), сравнение полей/детализации/истории и выбор канона до разработки загрузчика.
Блок 5. Финансовый отчёт реализации
1. Назначение. Главный финансовый факт WB: реализация, комиссия, к перечислению, логистика, хранение, удержания, приёмка, эквайринг.
2. API-источник.
2a. Legacy / текущий рабочий источник проекта.

Endpoint: GET https://statistics-api.wildberries.ru/api/v5/supplier/reportDetailByPeriod, token Statistics.
Статус: legacy / deprecated / временно используется в текущем коде. Удаление — 15 июля.
Роль: допустимо для сверки и временной поддержки текущего загрузчика (importWbFinanceFromApi), но нельзя закреплять как целевой API-only production endpoint.
Подтверждённая механика (совпадает с кодом проекта): dateFrom (RFC3339), dateTo, limit (≤100 000), rrdid (пагинация с 0 по rrd_id); история с 29.01.2024; лимит 1 запрос/мин; при отсутствии данных теперь возвращает 204.

2b. Целевой источник для API-only архитектуры.

Endpoint: POST https://finance-api.wildberries.ru/api/finance/v1/sales-reports/detailed («Details for the Sales Reports by Period»).
HTTP method: POST.
Token category: Finance (персональный или сервисный токен).
Доступность данных: с 29 января 2024.
Companion-методы: POST /api/finance/v1/sales-reports/list (список отчётов → reportId); POST /api/finance/v1/sales-reports/detailed/{reportId} (детализация по отчёту).
Параметры запроса (точный body — требует подтверждения по живой документации / тестовому запросу): период (dateFrom/dateTo) для …/detailed либо reportId; fields — массив для кастомизации набора полей; пагинация — TBD.
Ключевые отличия от legacy (важно для загрузчика): имена полей в camelCase; имена стандартизованы под другие методы; денежные поля — строки (string), а не числа; выбор полей через fields; маппинг строить строго по официальной таблице соответствия имён полей (Release Notes, пометка S = string).

3. Поля ответа. Семантически — те же сущности отчёта реализации (идентификатор строки, nmId, srid/аналог, vendorCode, barcode, даты, кол-во, сумма реализации, комиссия, к перечислению, логистика, хранение, удержания/штрафы, приёмка, эквайринг). Точные camelCase-имена не приводятся — требуют подтверждения по официальной таблице. Себестоимости нет (by design).
4. RAW-слой. RAW_WB_FINANCE. Детализация: операция / дата / nmId / строка отчёта. Дедуп: идентификатор строки отчёта (аналог rrd_id) + row_hash.
5. Совместимость с текущим RAW_WB_FINANCE.

Структура RAW-листа не меняется; целевые показатели те же.
Меняется маппинг источника: текущий FINANCE_API_FIELD_MAP_ (snake_case rrd_id, ppvz_for_pay, delivery_rub, storage_fee, deduction, penalty, acceptance, acquiring_fee, …) → новые camelCase-имена + парсинг строковых денежных полей.
load_id-префикс (FIN_API_) и идемпотентная дедупликация сохраняются; ключ дедупа переключается на новый идентификатор строки.

6. Какие изменения потребуются в будущем загрузчике (без кода сейчас):

сменить host/метод: statistics-api … GET reportDetailByPeriod → finance-api … POST sales-reports/detailed;
сменить токен на категорию Finance;
построить новую таблицу соответствия полей (camelCase) по официальной таблице WB;
добавить приведение строковых денежных полей к числу;
при необходимости задать fields;
реализовать пагинацию нового метода (вместо rrdid);
оставить legacy-загрузчик как fallback/сверку до 15 июля, затем вывести из регулярного потока.

7. P&L. Главный финансовый факт API-only: выручка/реализация, комиссия, к перечислению; несёт логистику/хранение/удержания/приёмку (блоки 6–9).
8. Риски. Срок: legacy удаляется 15 июля; смена типа денежных полей (string) — риск парсинга; новая пагинация; лимиты Finance-категории требуют подтверждения.
9. Решение. Целевой канон — POST /api/finance/v1/sales-reports/detailed (Finance); legacy reportDetailByPeriod — временно/сверка. Следующий шаг: подтвердить body/пагинацию и таблицу полей, тестовый запрос, сверка с legacy-данными.
Блок 6. Логистика
1. Назначение. Расходы на доставку покупателю.
2. API-источник. Поле логистики в отчёте реализации (целевой — Finance sales-reports/detailed; legacy — delivery_rub в reportDetailByPeriod). Статус: подтв. по докум. / тест.
3. Параметры. Наследует блок 5.
4. Поля. Логистическая сумма на строке операции (+ nmId, дата, идентификатор строки). В новой версии имя поля — camelCase, тип — string.
5. RAW-слой. RAW_WB_FINANCE. Детализация: операция / дата / nmId.
6. SKU-привязка. Да — строки логистики обычно несут nmId. Риск: часть корректировок без nmId.
7. P&L. Расход: логистика WB. SKU-факт по большинству строк.
8. Риски. Двойной учёт логистики, если суммировать поле логистики и тянуть её из другого места.
9. Решение. Брать только из отчёта реализации. Следующий шаг: на тесте измерить долю строк логистики без nmId.
Блок 7. Хранение
1. Назначение. Плата за хранение.
2. API-источник (два варианта).

(A) Поле хранения в отчёте реализации (Finance целевой / legacy storage_fee) — агрегатно/операционно.
(B) GET https://seller-analytics-api.wildberries.ru/api/v1/paid_storage (token Analytics, task-based: create → status → download).
Статус: подтв. по докум. / тест.

3. Параметры (paid_storage). Обязательные: dateFrom, dateTo. Максимальный период — 8 дней. Отчёт хранится 2 часа. История — TBD.
4. Поля (paid_storage). date, nmId, barcode, vendorCode, chrtId, warehouse, warehousePrice (сумма хранения), volume, calcType, тарифные даты.
5. RAW-слой. Для SKU-детализации — RAW_WB_STORAGE; агрегат — RAW_WB_FINANCE. Детализация: дата / nmId / склад. Дедуп: дата + nmId + склад.
6. SKU-привязка. Да (paid_storage даёт nmId + warehousePrice). В отчёте реализации хранение часто строками без nmId → агрегатно.
7. P&L. Расход: хранение WB. SKU-факт — через paid_storage; агрегат — через финотчёт.
8. Риски. Двойной учёт хранения (поле хранения в финотчёте + warehousePrice). Период paid_storage 8 дней → много задач.
9. Решение. Выбрать один канон (рекомендация: paid_storage для SKU-детализации, финотчёт — сверка). Следующий шаг: решение о каноне + тест и сверка сумм.
Блок 8. Удержания
1. Назначение. Прочие удержания и штрафы.
2. API-источник. Поля удержаний/штрафов в отчёте реализации (Finance целевой / legacy deduction, penalty, bonus_type_name). Статус: частично.
3. Параметры. Наследует блок 5.
4. Поля. Удержание/штраф + тип операции; nmId — не всегда заполнен.
5. RAW-слой. RAW_WB_FINANCE. Детализация: операция / дата; nmId — при наличии.
6. SKU-привязка. Частично. Часть — по nmId, часть — кабинетные (без nmId).
7. P&L. Расход: удержания. Строки без nmId — кабинетный расход, не SKU-факт.
8. Риски. Смешение удержаний и платной приёмки (общие сервисные строки); распределение кабинетной части на SKU без правила исказит юнит.
9. Решение. Частично; кабинетную часть — отдельным блоком, не распределять без правила. Следующий шаг: на тесте классифицировать типы и долю строк без nmId.
Блок 9. Платная приёмка
1. Назначение. Стоимость платной приёмки поставок.
2. API-источник (два варианта).

(A) Поле приёмки в отчёте реализации (Finance целевой / legacy acceptance).
(B) GET https://seller-analytics-api.wildberries.ru/api/v1/acceptance_report (token Analytics, task-based: create → status → download).
Статус: подтв. по докум. / тест.

3. Параметры (acceptance_report). Обязательные: dateFrom, dateTo (YYYY-MM-DD). Максимальный период — 31 день. Отчёт хранится 2 часа.
4. Поля (acceptance_report). count, giCreateDate, incomeId, nmID, shkCreateDate, subjectName, total.
5. RAW-слой. RAW_WB_FINANCE (агрегат) или отдельный RAW после решения. Детализация: дата / nmID / поставка (incomeId). Дедуп: incomeId + nmID + дата.
6. SKU-привязка. Да на уровне поставки (nmID + total). В финотчёте приёмка чаще партийная/без nmId.
7. P&L. Расход: приёмка. SKU/партия-факт через acceptance_report; агрегат — через финотчёт.
8. Риски. Двойной учёт приёмки (финотчёт + acceptance_report); приёмка относится к поставке (incomeId), а не к продаже.
9. Решение. Выбрать один канон. Следующий шаг: тест обоих источников и сверка.
Блок 10. Реклама
1. Назначение. Статистика кампаний: показы, клики, заказы, расход — по дням, advertId, nmId.
2. API-источник.

Список кампаний: advert-api…/adv/v1/promotion/count, /adv/v1/promotion/adverts.
Статистика: GET https://advert-api.wildberries.ru/adv/v3/fullstats — текущий метод (v2 — deprecated).
Token: Promotion. Статус: подтв. по докум. / тест.

3. Параметры (/adv/v3/fullstats).

Обязательные: ids (массив advertId, максимум 50), beginDate, endDate.
Максимальный период — 31 день.
Лимит — 3 запроса в минуту, интервал 20 секунд (программное превышение → временная блокировка).
Статистика — для кампаний в статусах 7, 9, 11. Синхронизация данных ~раз в 3 минуты.

4. Поля ответа. Иерархия: advertId → days[].date → apps[].appType → nms[]. На уровне nms[]: nmId, name, views, clicks, ctr, cpc, atbs, orders, shks, sum (расход), sum_price.
5. RAW-слой. RAW_WB_ADS. Детализация: дата / advertId / nmId. Дедуп: stat_date + advert_id + nmId (+ appType, если хранить разбивку).
6. SKU-привязка. Да — через nmId. Риск: nmId вне SKU_MASTER.
7. P&L. Рекламная аналитика; денежная роль — блоки 11–12.
8. Риски. Лимит 3 запроса/мин × ≤50 кампаний → планировать батчи; нужен предварительный список advertId; активные статусы могут не попадать в выборку 7/9/11.
9. Решение. Подтверждено по документации. Следующий шаг: тест promotion/adverts + fullstats с учётом лимитов.
Блок 11. Рекламные расходы
1. Назначение. Денежный расход на рекламу для P&L.
2. API-источник (два уровня).

(A) GET https://advert-api.wildberries.ru/adv/v1/upd — история списаний: updSum по advertId + updTime (уровень кампании/кабинета, без nmId).
(B) /adv/v3/fullstats → nms[].sum — расход на уровне nmId (блок 12).
Token: Promotion. Статус: подтв. по докум. / тест.

3. Параметры (/adv/v1/upd). Обязательные: from, to; период 1..31 день. Лимит 1 запрос/сек (burst 5).
4. Поля (/adv/v1/upd). updNum, updTime, updSum, advertId, campName, advertType, paymentType, advertStatus.
5. RAW-слой. RAW_WB_ADS (детализация по nmId из fullstats) и/или отдельный учёт кабинетного итога из upd.
6. SKU-привязка. upd — нет (без nmId). fullstats.sum — да (по nmId).
7. P&L. Расход: реклама. upd = кабинетный/кампанийный итог (контроль); fullstats.sum = разнесение по nmId.
8. Риски. Σ fullstats.sum по кампании может не совпадать с updSum (округление, тип кампании, разнесение WB). Зафиксирован реальный кейс, когда fullstats возвращал sum = 0 при наличии заказов — поле может быть нестабильным.
9. Решение. До сверки рекламный расход по SKU — оценка, кабинетный итог — из upd. Следующий шаг: сверка Σ sum ↔ updSum ↔ кабинет.
Блок 12. Детализация рекламы date + nmId + advertId + spend
1. Назначение. Ключевая проверка: можно ли считать рекламу SKU-фактом.
2. API-источник. /adv/v3/fullstats (GET, Promotion). Статус: связка структурно есть, числовая сверка — TBD.
3. Параметры. Как блок 10: ids (≤50), beginDate, endDate; период ≤31 день; 3 запроса/мин, интервал 20 с.
4. Поля связки.

date — days[].date;
advertId — корень объекта кампании;
nmId — days[].apps[].nms[].nmId;
spend — days[].apps[].nms[].sum (суммировать по appType в рамках дня).
→ Полная связка date + nmId + advertId + spend присутствует структурно.

5. RAW-слой. RAW_WB_ADS, гранулярность date + nmId + advertId (агрегация sum по appType).
6. SKU-привязка. Да, по nmId. Риск: nmId вне SKU_MASTER; sum — разнесение WB, не «чистый» прямой расход.
7. P&L. Условие SKU-факта рекламы. Выполнимо только после сверки (блок 11).
8. Риски. Расхождение fullstats.sum ↔ upd.updSum; известны случаи sum = 0; типы кампаний (аукцион/авто/АРК) разносят расход по-разному.
9. Решение (обязательная оговорка).

nms[].sum можно использовать как spend на уровне nmId только после тестовой сверки с /adv/v1/upd и кабинетом WB. До сверки это не финальный SKU-факт, а предварительная детализация / оценка.

После успешной сверки (расхождение в допуске) — допустимо помечать как SKU-факт; иначе — оценка.
Блок 13. Внутренние справочники (SKU_MASTER, COST_HISTORY, BUNDLES)
1. Назначение. Маппинг товаров и себестоимость (COGS). Не из WB API.
2. Источник. Внутренние листы Google Sheets. Себестоимость в WB API отсутствует by design.
3–4. Поля. SKU_MASTER: internal_sku ↔ nmId/barcode/vendorCode. COST_HISTORY: internal_sku + дата + COGS. BUNDLES: набор → компоненты.
5. RAW-слой. Сами справочники; подмешиваются на уровне ЮНИТ / P&L.
6. SKU-привязка. Это источник привязки. Риск: неполный SKU_MASTER → unmatched в RAW.
7. P&L. COGS (только отсюда), себестоимость наборов (только из BUNDLES).
8. Риски. Нет записи COGS в COST_HISTORY; нет состава набора в BUNDLES; устаревший маппинг nmId.
9. Решение. Вне API-аудита. Следующий шаг: внутренняя проверка полноты справочников.
4. Сводная таблица решений
БлокAPI найденДетализация достаточнаSKU-фактP&L рольСтатусСледующий шагЗаказыдадаn/aворонкаоперац. источник / тест / мониторить RNтест-запросПродажидадаоперац.выручка (не фин. факт)операц. источник / тест / мониторить RNсверка с FinanceВозвратыдадада (финотчёт)вычет из выручкиподтв. по докум. / тестсверкаОстатки (legacy)дадаn/aне P&Llegacy / deprecated (23 июня)не использоватьОстатки (кандидаты A/B)даA — да; B — TBDn/aне P&Lоба кандидаты / тест / выбрать канонтест A и B, выбор канонаФинотчёт (legacy v5)дададафин. факт (временно)legacy / deprecated (15 июля)fallback/сверка до 15 июляФинотчёт (Finance POST)дададаглавный фин. фактподтв. по докум. / тестподтвердить body/поля + тестЛогистикададада (б.ч.)логистикаподтв. по докум. / тестдоля без nmIdХранениеда (финотчёт + paid_storage)дада (paid_storage)хранениеподтв. по докум. / тествыбор канонаУдержаниядачастичночастичноудержания (часть кабинет)частичноклассификацияПлатная приёмкада (финотчёт + acceptance_report)дада (партия)приёмкаподтв. по докум. / тествыбор канонаРекламада (/adv/v3/fullstats)дачерез nmIdаналитикаподтв. по докум. / тестсписок кампаний + fullstatsРекл. расходыда (upd + fullstats)дада (fullstats) / нет (upd)рекламаподтв. по докум. / тестсверка суммРеклама date+nmId+advertId+spendда (nms[].sum)датолько после сверкиусловие SKU-фактасвязка есть, сверка TBDсверка Σsum↔updSumСправочникиn/a (не API)зависит от заполненияда, если заполненыCOGS/маппингвне APIвнутренняя проверка
5. Блоки, которые нельзя кодить до уточнения

Финансы (целевой) — POST /api/finance/v1/sales-reports/detailed: до подтверждения body-параметров, пагинации и официальной таблицы соответствия camelCase-полей (и парсинга строковых денежных полей).
Финансы (legacy) — reportDetailByPeriod: не закреплять как целевой; держать как fallback/сверку до 15 июля.
Остатки — до выбора канона между warehouse_remains (A) и stocks-report/wb-warehouses (B); старый supplier/stocks (23 июня) как целевой не кодить.
Хранение / приёмка — до выбора единого канона (иначе двойной учёт).
Удержания на уровне SKU — до классификации типов и решения по кабинетной части.
Реклама как SKU-факт — до сверки Σ fullstats.sum ↔ updSum ↔ кабинет.
Любой блок без тестового запроса — остаётся «подтверждено по документации / требует тестового запроса».


Заказы/продажи (supplier/orders, supplier/sales) разрешено использовать как операционные источники (воронка, операционные продажи/возвраты), но не как финансовый факт, и при условии мониторинга Release Notes.

6. Риски для P&L

Срок жизни legacy-методов: финотчёт v5 (15 июля), supplier/stocks (23 июня) — без миграции регулярный контур пострадает.
Смена контракта Finance API: денежные поля стали строками, имена — camelCase → риск ошибок маппинга/парсинга при переходе.
Два кандидата по остаткам: разные механики (task-based A vs POST B) → разный код; нужен выбор канона до разработки.
Двойной учёт логистики — только из отчёта реализации.
Двойной учёт хранения — поле хранения в финотчёте + warehousePrice; один канон.
Смешение удержаний и платной приёмки — общие сервисные строки финотчёта; разделять по типу операции.
Неверное распределение рекламы — fullstats.sum ≠ updSum, бывают sum = 0; без сверки реклама по SKU — только оценка.
Excel/Drive как ложный эталон — только legacy / сверка / fallback; эталон фиксируется на API-only периоде.
Отсутствие COGS в COST_HISTORY — себестоимость не из WB API; без записи юнит некорректен.
Себестоимость наборов без BUNDLES — наборы без состава дают неверный COGS.
