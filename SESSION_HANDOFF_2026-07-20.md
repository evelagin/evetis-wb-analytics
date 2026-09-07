# SESSION HANDOFF — 2026-07-20

Снимок состояния EVETIS WB Analytics на конец сессии 20.07.2026.
Заменяет `SESSION_HANDOFF_2026-07-12.md` как самый свежий.

**Главное за сессию:** закрыт контур продаж (ночная пересверка + общий rate-limit
guard), **остатки доведены до production** (Фаза E: RAW + manifest + VIEW + суточный
триггер), и — важнее всего — **проект переведён из набора загрузчиков в управляемую
BI-архитектуру**: зафиксирован документ `docs/ARCHITECTURE_EVETIS_ANALYTICS_v2.md`,
который теперь источник истины для всей дальнейшей разработки.

Всё перечисленное влито в `main` (PR #58–#63).

---

## 1. Что сделано в этой сессии

### PR #58/#59 — Sales Night Reconciliation (Фаза D2c, в проде)
- `WbSalesReconcile.gs`: `runWbSalesNightReconcile()` + ядро + ночной триггер
  `everyDays(1).atHour(4).nearMinute(20)` (~04:20 МСК).
- Конфиг `WB_SALES_RECONCILE_DAYS` (дефолт 7, границы 1..90); окно `dateFrom` =
  полночь МСК (сегодня − N).
- Новый BQ-хелпер `wbSalesBqStateKeysSince_(fromLcd)` — range-wide набор
  `sale_id|TO_HEX(MD5(raw_json))`. Внутрипакетно дедуп по `sale_id|state`
  (**НЕ** last-wins — латаем все отсутствующие состояния), append только пропусков.
- **watermark НЕ трогаем** (им владеет hourly); в логе `watermark_before ==
  watermark_after` — авто-доказательство.
- **Общий 65-сек API cooldown для ОБОИХ путей** (обязательное требование аудита):
  `wbSalesApiAcquireRequestSlot_()` + Script Property `WB_SALES_API_LAST_REQUEST_AT_MS`.
  Один `ScriptLock` защищает от одновременности, но НЕ от двух последовательных
  запросов в пределах минуты (runtime 429). Timestamp пишется **ДО** fetch.
  Новый статус `SKIPPED_RATE_LIMIT` (врезан и в hourly `WbSalesIncremental.gs`).
- Правка по ревью: `gaps_filled` фиксируется ДО append, `rows_written`
  подтверждается **только ПОСЛЕ** успешного `appendSalesRows_`.
  Контракт: падение append → `status=ERROR, gaps_filled=N, rows_written=0`;
  успех → `gaps_filled == rows_written`.
- **Приёмка пройдена:** 1-й прогон `OK` gaps_filled=1; повтор `OK_NO_GAPS`;
  частый повтор `SKIPPED_RATE_LIMIT` (HTTP не вызван); триггер идемпотентен;
  watermark не менялся.

### PR #60/#61 + 0c6cb1f — Probe остатков (диагностика)
- Старый probe блокировался **до** вызовов WB: `wbApiTestPrepare_` →
  `wbApiTestGetResultsFolder_` (DriveApp) → `BLOCKED`. Drive для диагностики не нужен.
- `probeWbStocksConsole()` — Drive-независимый, всё в `console.log`; сохранение
  JSON в Drive опционально (`{saveJson:true}`) и не блокирует. Harness
  `WbApiTestRunner/Utils/Config` не тронут (переиспользуются только Drive-free хелперы).
- Добавлена диагностика естественного ключа T6 + список unmatched `nm_id`.
- `probeWbStocksT6Periods()` (блокер B2) — три окна (май / сегодня / 7 дней);
  `same_()` сравнивает в т.ч. `distinctKey`/`duplicateKeys`/`uniqueNmId`.

### PR #62 — Загрузчик остатков (Фаза E, в проде)
- `WbStocksBigQuery.gs`: `RAW_WB_STOCKS` (append-only, партиция `_snapshot_date`,
  кластер `nm_id/warehouse_id`, **без** `processed_status/error_message`),
  manifest `WB_STOCKS_SNAPSHOTS` (STARTED/COMPLETE/ERROR + метрики),
  `V_WB_STOCKS_CURRENT` (строки только последнего **COMPLETE** снимка).
- `WbStocksSnapshot.gs`: `runWbStocksSnapshot()` + ядро, fetch T6/T5, валидация,
  нормализация, суточный триггер `everyDays(1).atHour(6).nearMinute(30)` (~06:30 МСК).
- Требования аудита, реализованные в коде:
  - **C1** — общий project-wide `ScriptLock` (отдельного в Apps Script не бывает);
    триггер разнесён по времени.
  - **C2** — manifest `STARTED` вставляется **ДО** fetch; sink OFF = runtime ERROR
    **без** manifest; финал `UPDATE ... WHERE status='STARTED'` + проверка
    `numDmlAffectedRows == 1`.
  - **C3** — детерминированные BQ job ID `STOCK_<snapshot_id>_BATCH_<n>`;
    при ошибке insert проверяем существование job (`Jobs.get`) → job есть: не
    дублируем; not found: повтор; проверить не смогли: fail closed.
  - Ревью-правки: DML ждёт `jobComplete=true` (`getQueryResults`) и проверяет
    `errorResult` **до** чтения `numDmlAffectedRows`; убран тавтологичный
    `nullKey`-чек.
- **Приёмка пройдена:** C0 ок; R1 — 155 строк, `control=OK`; R2 — новый
  `snapshot_id`, снова 155, VIEW остаётся 155 (не 310); installer идемпотентен.

### PR #63 — Архитектура EVETIS Analytics v2 (ревизия 3)
`docs/ARCHITECTURE_EVETIS_ANALYTICS_v2.md` — **источник истины**. Подробнее §4.

---

## 2. Доказанная эмпирика (не переоткрывать)

| Что | Числа |
|---|---|
| Ключ состояния продаж | RAW 3421 → distinct `sale_id` 3345 = distinct `sale_id\|md5(raw_json)` 3345 (76 точных повторов состояния) |
| Окно 7 дней (продажи) | 253 строки → 177 state-ключей |
| Ключ снимка остатков T6 | 155 строк = 155 distinct `nm_id\|chrt_id\|warehouse_id`, дублей 0 |
| Разбивка остатков | `quantity>0` 121 строка, `quantity=0` 34; агрегат `warehouse_id=0`/«Остальные» 17 строк, Σ=0 |
| Сверка остатков | Σ физ. T6 = Σ физ. T5 = **4565**; uniqueNmId 23; unmatched `nm_id` = **1083392113** |
| Семантика периода T6 | A(май)/B(сегодня)/C(7 дней) — **идентичны** → `currentPeriod` не влияет → прод берёт `today..today` МСК |
| Склад у продаж | `V_WB_SALES_RETURNS` 3382 строки, `warehouse_name` заполнен 100%, 23 имени, **`warehouseId` отсутствует** |

---

## 3. Что работает автоматически (production)

| Поток | Триггер | Статус |
|---|---|---|
| Заказы | `runWbOrdersIncremental`, hourly (watermark) | ✅ |
| Продажи + возвраты | `runWbSalesIncremental`, hourly (watermark) | ✅ |
| Ночная пересверка продаж | `runWbSalesNightReconcile`, ~04:20 МСК, окно 7 дней | ✅ |
| Остатки | `runWbStocksSnapshot`, ~06:30 МСК (снимок) | ✅ |
| **Реклама** | — | 🟡 **НЕ production**: BQ-слой и историческая загрузка частично готовы, регулярная автозагрузка не сделана |
| **Финансы** | — | 🟡 **НЕ production**: RAW/VIEW есть, история с 05.09.2024 загружена, daily-инкремент не принят |

---

## 4. Архитектура v2 — ключевые решения (полный текст в docs/)

- **Строим BI-систему**, а не набор загрузчиков. Три уровня: Data Platform →
  Data Marts → Ad Marts → Dashboard. Четыре домена: Продажи, Маркетинг, Финансы, Операции.
- **Товар — ДВА уровня ключей.** `DIM_LISTING` (`marketplace`+`nm_id`) и
  `DIM_PRODUCT` (`internal_sku`); **во всех conformed FACT хранятся ОБА**.
  Реклама/остатки/позиции живут на `nm_id`, прибыль/общая аналитика — на `internal_sku`.
  Варианты карточки (несколько `chrt_id`/баркодов) вынесены в `DIM_LISTING_VARIANT`.
- **`internal_sku` берётся ТОЛЬКО через `LEFT JOIN REF_SKU_MASTER`** (никогда INNER);
  неизвестный `nm_id` → `internal_sku IS NULL` + `sku_match_status='not_found'`,
  строка сохраняется; витрины обязаны показывать «не сопоставлено» — молчаливая
  потеря строк считается дефектом.
- **Наборы:** `sellable_sku` (на нём выручка) vs `component_sku` (раскладка cogs).
- **История остатков** строится из `RAW_WB_STOCKS` JOIN manifest
  `WHERE status='COMPLETE'` (`FACT_STOCKS_SNAPSHOT`), **не** из `V_WB_STOCKS_CURRENT`.
- **Атрибуция:** `attributed_ad_orders` / `non_attributed_orders`. Считать
  «общие − рекламные = органика» **нельзя** (разные окна атрибуции) — термин
  «органика» только после доказанного контракта.
- **Пять денежных баз не смешиваются:** `order_amount`, `sale_amount`, `for_pay`,
  `ad_attributed_revenue`, `finance_revenue`.
- **🔴 Формула прибыли/маржи — TBD.** `for_pay` уже НЕТТО после удержаний WB,
  поэтому `for_pay − cogs − комиссия − логистика − …` вычитала бы расходы дважды.
  Зафиксировать надо ровно один контракт: **A** (от валовой базы минус все
  удержания) либо **B** (от `for_pay` минус только то, что в него не входит).
  Решение — после reconciliation `V_WB_FINANCE` на контрольных суммах.
  До этого `MART_PNL_*` не строим и прибыль на дашборд не выводим.

**Утверждённый порядок работ:**
1. ✅ Закоммитить Architecture v2 → 2. **REF Sync** → 3. Реклама → production →
4. Базовые MART (`MART_SALES_DAILY`, `MART_SKU_DAILY`, `MART_STOCKS_CURRENT`,
`MART_ADS_OVERVIEW/CAMPAIGN/SKU_DAILY`) → 5. **Dashboard v0.5** (с базовой рекламной
воронкой) → 6. Финансы + reconciliation + PNL → 7. Запросы/ставки/позиции/drill-down.

---

## 5. СЛЕДУЮЩИЙ ШАГ — REF Sync (Этап 2)

Синхронизация справочников Google Sheets → BigQuery. Первым, потому что: объём
ограничен, нужен почти всем будущим витринам и закрывает единые ключи.

Цель — таблицы: `REF_SKU_MASTER` (`marketplace`+`nm_id` → `internal_sku`),
`REF_COST_HISTORY` (дата-эффективная себестоимость), `REF_BUNDLES`
(`sellable_sku` → `component_sku`), `REF_WAREHOUSES` (мост
`sales_name ↔ warehouse_id` + регион).

**До кода нужен discovery:** какие колонки реально есть в листах `SKU_MASTER` /
`COST_HISTORY` / `BUNDLES`, что из них нужно витринам, как обрабатывать
несколько `nm_id` на один `internal_sku`, и как наполнить `REF_WAREHOUSES`
(сверить 23 имени складов продаж с `warehouse_id` остатков).

---

## 6. Открытые вопросы и долги

- 🔴 **Reconciliation `for_pay`** — состав удержаний, выбор варианта A/B формулы PNL.
- 🟡 **Реклама → production** (регулярная загрузка + пересверка + триггер).
- 🟡 **Финансы → production** (daily incremental + скользящая пересверка).
- 🟡 **Probe рекламы по запросам/ставкам** — даёт ли API связку
  `запрос/ставка → расход → SKU`. Без связки витрины не строим.
- **Склад-мост** — сверить 23 имени продаж с `warehouse_id` остатков.
- **unmatched `nm_id` 1083392113** — проверить в кабинете и внести в `SKU_MASTER`.
- **Health Monitor** — отложен до после дашборда; потом сразу единый
  (заказы+продажи+пересверка+остатки), отдельный триггер, Telegram.
- **Ёмкость Google Sheets** — книга у лимита 10 млн ячеек; ветка
  `diag/workbook-cell-audit` (`WbWorkbookAudit.gs`) НЕ смерджена.

---

## 7. Опорные объекты

**BigQuery:** проект `project-fa311fc0-4d87-4781-986`, датасет `wb_raw`.
`RAW_WB_ORDERS`/`V_WB_ORDERS`, `RAW_WB_SALES_RETURNS`/`V_WB_SALES_RETURNS`,
`RAW_WB_STOCKS` + `WB_STOCKS_SNAPSHOTS`/`V_WB_STOCKS_CURRENT`,
`RAW_WB_FINANCE`/`V_WB_FINANCE`, `V_ADV_*`.

**Script Properties (ключевые):** `WB_SALES_LAST_CHANGE_WATERMARK`,
`WB_SALES_API_LAST_REQUEST_AT_MS`, `WB_SALES_RECONCILE_DAYS`, `WB_SALES_BQ_SINK`,
`WB_SALES_CONSUMER_SOURCE=BIGQUERY`, `WB_STOCKS_BQ_SINK`, `WB_ORDERS_LAST_CHANGE_WATERMARK`,
токены `WB_TOKEN_STATISTICS` / `WB_TOKEN_ANALYTICS` / `WB_TOKEN_FINANCE` / `WB_TOKEN_PROMOTION`.

---

## 8. Процесс (неизменен)

Не усложнять; минимально / обратимо / проверяемо. Перед кодом: что/зачем/риск/
контрольные цифры → потом код. Ключи дедупа доказываем эмпирически (probe/BQ).
`node --check` + `git diff --check`. **Ветку создаёт ассистент; commit/push,
вставку в Apps Script и установку триггеров делает владелец** после ручной приёмки.
План и PR прогоняются через аудитора (ChatGPT) — его замечания обязательны к
закрытию до мержа. Всё пользовательское — по-русски.
