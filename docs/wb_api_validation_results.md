# WB API Validation Results — EVETIS WB Analytics

> Результат фактической проверки WB API по `docs/wb_api_validation_checklist.md`. Финальная версия (ревизия 4).
> Метод проверки: официальная документация WB API (`dev.wildberries.ru`, Release Notes, разделы Reports / Financial Reports / Promotion) + перекрёстная сверка с рабочим кодом проекта.
> Тестовый контур T1–T11 выполнен; второй прогон проведён после contract-adjustment PR на контрольном периоде **2026-05-18 — 2026-05-24** (см. §8). Для протестированных блоков статус — «подтверждено тестовым запросом»; непротестированные детали остаются «подтверждено по документации / требует отдельного решения канона». Deprecated-методы помечены отдельно.

---

## 1. Краткий итог

Проект уже имеет рабочую архитектуру источников данных:

- **Orders / Sales / Returns / Stocks** — частично реализованы через Statistics API и рабочие листы.
- **Finance weekly detail** — сейчас фактически грузится через Excel/старый финансовый отчёт; новый API есть, но требует отдельного осторожного production-внедрения.
- **Advertising** — рабочий тестовый контур подтверждён через T7/T8/T9.
- **Storage / Acceptance / Deductions** — API-кандидаты есть; production-канон ещё не выбран.

Подтверждено тестовым запросом во втором прогоне после contract-adjustment PR:

- **T1 new Finance API** — `PASSED`, `rows=1472`.
- **T2 legacy finance** — `PASSED`, `rows=1472`.
- **T1/T2 finance totals matched**: `logistics=20697.22`, `storage=6099.35`, `acceptance=0`, `acquiring=6277.33`, `rebillLogistics=2122.38`.
- **T5 warehouse_remains** — `PASSED`, физический остаток `5269`; double count устранён через раздельные checksums.
- **T6 stocks-report/wb-warehouses** — `PARTIAL`, `rows=169`, `totalQuantity=5269`; фактический ответ `data.items` парсится.
- **T8 adv/v3/fullstats** — `PASSED`, `sumSpend=28276.64`.
- **T9 adv/v1/upd** — `PASSED`, `sumUpd=27342`.
- **T8/T9 ads reconciliation** — `deltaPercent=3.31%`, `status=OK`.

Открытые решения перед production-PR:

1. **Выбор production-канона по остаткам**: T5 как простой snapshot, T6 как детальный источник по складам/регионам/warehouseId/chrtId.
2. **Storage / Acceptance / Deductions** — канон и правила записи в production-листы требуют отдельного решения.
3. **Production-интеграция рекламы** — T8 принят как управленческий источник, T9 как контроль; внедрение в боевые листы должно идти отдельным PR.

---

## 2. Почему сейчас нужен именно validation map

Цель этого документа — не переписать код, а зафиксировать:

- какой endpoint реально подходит под каждый бизнес-блок;
- какие endpoint-ы уже deprecated или рискованные;
- где нельзя строить production-логику без тестового запроса;
- что именно должно быть проверено до изменения боевого кода.

Это особенно важно, потому что в проекте уже были сложные цепочки P&L, и новый API-слой нельзя подключать «на глаз».

---

## 3. API-блоки и статус проверки

### 3.1. Заказы

**Бизнес-блок:** заказы WB по SKU / дате.

**Текущий код:**

- `RAW_WB_ORDERS`
- `ORDERS_SALES_DAILY V2`
- `loadRawWbOrders()` / аналогичные функции

**Актуальный API-кандидат:**

- `GET /api/v1/supplier/orders`
- Host: `statistics-api.wildberries.ru`

**Документационный статус:** рабочий, не deprecated.

**Нужные поля:**

- `date`
- `lastChangeDate`
- `warehouseName`
- `countryName`
- `oblastOkrugName`
- `regionName`
- `supplierArticle`
- `nmId`
- `barcode`
- `totalPrice`
- `discountPercent`
- `spp`
- `finishedPrice`
- `priceWithDisc`
- `isCancel`
- `cancelDate`
- `orderType`
- `srid`

**Статус для проекта:** подтверждено по документации, рабочий источник.

**Решение:** можно использовать как источник заказов, но для P&L не считать продажей без сверки с Sales / Finance.

---

### 3.2. Продажи и возвраты

**Бизнес-блок:** фактические продажи, возвраты, `forPay`, связь с SKU.

**Текущий код:**

- `RAW_WB_SALES_RETURNS`
- `ORDERS_SALES_DAILY V2`

**Актуальный API-кандидат:**

- `GET /api/v1/supplier/sales`
- Host: `statistics-api.wildberries.ru`

**Документационный статус:** рабочий, не deprecated.

**Нужные поля:**

- `date`
- `lastChangeDate`
- `warehouseName`
- `supplierArticle`
- `nmId`
- `barcode`
- `totalPrice`
- `discountPercent`
- `spp`
- `forPay`
- `finishedPrice`
- `priceWithDisc`
- `saleID`
- `orderType`
- `srid`

**Особое правило:**

Возвраты часто определяются через `saleID`, где возвратные операции имеют специальный признак/тип. До теста нельзя предполагать универсальную схему только по одному полю.

**Статус для проекта:** подтверждено по документации, рабочий источник.

**Решение:** использовать для операционного блока продаж/возвратов, но финансовый итог сверять с Finance weekly detail.

---

### 3.3. Остатки WB — текущий Statistics API

**Бизнес-блок:** остатки по складам WB.

**Текущий код:**

- `STOCKS_WB`
- `loadRawWbStocks()` / аналоги

**Текущий API-кандидат:**

- `GET /api/v1/supplier/stocks`
- Host: `statistics-api.wildberries.ru`

**Документационный статус:** рабочий, но менее детализированный, чем новые warehouse report endpoints.

**Нужные поля:**

- `lastChangeDate`
- `warehouseName`
- `supplierArticle`
- `nmId`
- `barcode`
- `quantity`
- `inWayToClient`
- `inWayFromClient`
- `quantityFull`
- `subject`
- `category`
- `brand`
- `techSize`
- `Price`
- `Discount`
- `isSupply`
- `isRealization`
- `SCCode`

**Статус для проекта:** подтверждено по документации.

**Решение:** можно оставить как базовый источник остатков, но перед новым production-слоем сравнить с warehouse reports.

---

### 3.4. Остатки WB — новый warehouse reports API

**Бизнес-блок:** остатки WB по складам/регионам/детализации.

**API-кандидат A:**

- `GET /api/v1/warehouse_remains`
- Host: `seller-analytics-api.wildberries.ru`

**API-кандидат B:**

- `POST /api/v1/stocks-report/wb-warehouses`
- Host: `seller-analytics-api.wildberries.ru`

**Документационный статус:** актуальные аналитические endpoints.

**Важное ограничение:**

Документация WB по warehouse reports часто меняется, и структура ответа может отличаться по тарифу/кабинету/типу доступа.

**Что нужно проверить тестом:**

- есть ли разбивка по `nmID` / `nmId`;
- есть ли `barcode`;
- есть ли `warehouseName` / `warehouseId`;
- возвращаются ли нулевые остатки;
- есть ли разделение «на складе», «в пути к клиенту», «возврат в пути»;
- лимиты и задержка формирования отчёта.

**Статус для проекта:** требует тестового запроса.

**Решение:** не заменять текущий `STOCKS_WB` до теста. Сначала получить raw JSON по обоим кандидатам.

---

### 3.5. Финансовый отчёт WB — текущий Excel / legacy weekly detail

**Бизнес-блок:** финансовый P&L WB, комиссии, логистика, удержания, хранение, эквайринг.

**Текущий источник проекта:**

- Excel «Еженедельный детализированный отчёт»
- `RAW_WB_FINANCE`
- `CLEAN_WB_DAILY`
- `PNL_TOTAL`

**Текущий подтверждённый результат по прошлому тесту:**

- `rows=1472`
- продажи `313`
- реализация WB `158190.03`
- к перечислению `168174.71`
- логистика `20697.22`
- хранение `6099.35`
- удержания `28763.49`
- эквайринг `6277.33`
- после расходов WB `112614.65`

**Статус для проекта:** рабочий и подтверждённый источник факта.

**Решение:** до полной проверки нового Finance API считать Excel/legacy weekly detail главным финансовым эталоном.

---

### 3.6. Финансовый отчёт WB — новый Finance API

**Бизнес-блок:** замена ручной загрузки weekly detail на API.

**API-кандидат:**

- `POST /api/v5/supplier/reportDetailByPeriod`
- Host: `seller-analytics-api.wildberries.ru` или актуальный host из документации WB на момент теста.

**Документационный статус:** актуальный финансовый endpoint, но требует проверки структуры ответа.

**Критичные поля:**

- `rrd_id`
- `gi_id`
- `subject_name`
- `nm_id`
- `brand_name`
- `sa_name`
- `ts_name`
- `barcode`
- `doc_type_name`
- `quantity`
- `retail_price`
- `retail_amount`
- `sale_percent`
- `commission_percent`
- `office_name`
- `supplier_oper_name`
- `order_dt`
- `sale_dt`
- `rr_dt`
- `shk_id`
- `retail_price_withdisc_rub`
- `delivery_amount`
- `return_amount`
- `delivery_rub`
- `gi_box_type_name`
- `product_discount_for_report`
- `supplier_promo`
- `rid`
- `ppvz_spp_prc`
- `ppvz_kvw_prc_base`
- `ppvz_kvw_prc`
- `sup_rating_prc_up`
- `is_kgvp_v2`
- `ppvz_sales_commission`
- `ppvz_for_pay`
- `ppvz_reward`
- `acquiring_fee`
- `acquiring_bank`
- `ppvz_vw`
- `ppvz_vw_nds`
- `ppvz_office_id`
- `ppvz_office_name`
- `ppvz_supplier_id`
- `ppvz_supplier_name`
- `ppvz_inn`
- `declaration_number`
- `bonus_type_name`
- `sticker_id`
- `site_country`
- `penalty`
- `additional_payment`
- `rebill_logistic_cost`
- `storage_fee`
- `deduction`
- `acceptance`
- `srid`

**Что нужно проверить тестом:**

- совпадает ли сумма `ppvz_for_pay` с Excel;
- совпадает ли логистика;
- совпадает ли хранение;
- совпадают ли удержания;
- есть ли `srid`;
- есть ли `rrd_id`;
- отличается ли структура от legacy Excel.

**Статус для проекта:** требует тестового запроса.

**Решение:** нельзя заменять `RAW_WB_FINANCE` на новый API без тестового запроса и сверки с уже загруженным Excel за тот же период.

---

### 3.7. Платное хранение

**Бизнес-блок:** детализация платного хранения по SKU/дате/складу.

**API-кандидат:**

- `GET /api/v1/paid_storage`
- Host: `seller-analytics-api.wildberries.ru`

**Документационный статус:** актуальный аналитический endpoint.

**Поля-кандидаты:**

- `date`
- `nmId`
- `vendorCode`
- `barcode`
- `warehouseName`
- `amount`
- `storageCost`
- `quantity`
- `volume`

**Что нужно проверить тестом:**

- совпадает ли сумма с weekly finance storage;
- есть ли детализация по SKU;
- можно ли разнести хранение по `nmId`;
- как API ведёт себя при отсутствии хранения.

**Статус для проекта:** требует тестового запроса.

**Решение:** не встраивать в P&L до сверки с weekly finance.

---

### 3.8. Приёмка / paid acceptance

**Бизнес-блок:** платная приёмка WB.

**API-кандидат:**

- endpoint из Analytics Reports / Acceptance report, актуальный путь проверять в документации WB.

**Документационный статус:** кандидат, требует уточнения.

**Что нужно проверить тестом:**

- есть ли сумма платной приёмки за период;
- есть ли детализация по поставке / SKU / складу;
- совпадает ли с Finance weekly detail;
- как отражается нулевая приёмка.

**Статус для проекта:** требует тестового запроса.

**Решение:** не кодить до подтверждения endpoint и структуры ответа.

---

### 3.9. Рекламные кампании — список кампаний

**Бизнес-блок:** список рекламных кампаний, статусы, типы.

**API:**

- `GET /adv/v1/promotion/count`
- `POST /adv/v1/promotion/adverts`
- Host: `advert-api.wildberries.ru`

**Документационный статус:** актуальные Promotion API endpoints.

**Нужные поля:**

- `advertId`
- `type`
- `status`
- `changeTime`
- `createTime`
- `startTime`
- `endTime`
- `name`

**Статус для проекта:** подтверждено по документации.

**Решение:** можно использовать для справочника кампаний, но не для расхода.

---

### 3.10. Рекламные расходы — financial upd

**Бизнес-блок:** финансовые списания по рекламе.

**API:**

- `GET /adv/v1/upd`
- Host: `advert-api.wildberries.ru`

**Документационный статус:** актуальный endpoint.

**Нужные поля:**

- `updNum`
- `updTime`
- `advertId`
- `campName`
- `updSum`
- `paymentType`
- `advertStatus`

**Статус для проекта:** подтверждено по документации.

**Решение:** использовать как финансовый контроль рекламных списаний по кабинету.

**Ограничение:** не даёт `nmId`, поэтому сам по себе не подходит для SKU-воронки.

---

### 3.11. Рекламная статистика fullstats

**Бизнес-блок:** показы, клики, CTR, CPC, CR, расходы по `advertId` и потенциально `nmId`.

**API:**

- `POST /adv/v3/fullstats`
- Host: `advert-api.wildberries.ru`

**Документационный статус:** актуальный endpoint.

**Нужные поля:**

- `advertId`
- `date`
- `views`
- `clicks`
- `ctr`
- `cpc`
- `sum`
- `atbs`
- `orders`
- `cr`
- `shks`
- `sum_price`
- `days`
- `apps`
- `nms`
- `nmId`

**Критичное правило:**

Фактический расход по SKU можно брать из `nms[].sum` только после тестовой сверки:

```text
sum(nms[].sum) по всем кампаниям за период
≈
sum(updSum) за тот же период
```

Если суммы не сходятся, то fullstats можно использовать для воронки, но финансовый P&L брать из `/adv/v1/upd`.

**Статус для проекта:** требует тестового запроса.

**Решение:** не подключать в `ADS_WB` как финансовый факт без сверки.

---

### 3.12. Справочники карточек / SKU

**Бизнес-блок:** сопоставление `nmId`, barcode, vendorCode, product name.

**Текущие источники:**

- `SKU_MASTER`
- WB Content API
- ручные справочники

**API-кандидаты:**

- Content API карточек WB.

**Статус:** в проекте уже есть `SKU_MASTER`, его нельзя ломать.

**Решение:** новый API не должен перезаписывать `SKU_MASTER`; только дополнять после отдельного mapping-audit.

---

## 4. Сводная таблица решений

| Блок | API / источник | Статус | Решение |
|---|---|---|---|
| Заказы | `/api/v1/supplier/orders` | рабочий | оставить как источник заказов |
| Продажи / возвраты | `/api/v1/supplier/sales` | рабочий | источник операционных продаж |
| Остатки текущие | `/api/v1/supplier/stocks` | рабочий | оставить как fallback |
| Остатки новые | `warehouse_remains` / `stocks-report/wb-warehouses` | A PASSED; B PARTIAL; оба подтверждены тестом, канон не выбран | выбрать канон отдельным PR |
| Финотчёт Excel / legacy | weekly detail Excel | подтверждён | текущий эталон |
| Финотчёт Finance POST | `/api/v5/supplier/reportDetailByPeriod` | подтверждён тестовым запросом / совпал с legacy | можно готовить production PR |
| Финотчёт legacy v5 | старый financial report | PASSED / legacy-сверка до удаления | оставить как контроль на переходный период |
| Хранение | `paid_storage` | кандидат | сверить с finance |
| Приёмка | acceptance report | кандидат | уточнить endpoint и сверить |
| Рекл. кампании | `promotion/count`, `promotion/adverts` | рабочий | справочник кампаний |
| Рекл. расходы | `fullstats` + `upd` | T8 — источник P&L/SKU-воронки; T9 — контроль; delta 3.31% OK | использовать T8 как управленческий источник, T9 как контроль |
| Рекл. воронка | `fullstats` | подтверждена тестом; источник T8; контроль T9 | использовать для SKU-воронки |
| SKU mapping | `SKU_MASTER` + Content API | частично есть | не перезаписывать без audit |

---

## 5. Блоки, которые нельзя кодить без отдельного production-PR

После второго тестового прогона API-контракты T1/T2/T5/T6/T8/T9 подтверждены, но production-код всё равно нельзя менять без отдельного PR и явного выбора канона.

### 5.1. Finance API

Контракт T1/T2 подтверждён тестовым запросом и совпал по ключевым суммам. Следующий шаг — отдельный production PR для аккуратного внедрения finance mapping в существующую CLEAN/PNL-логику.

### 5.2. Остатки

T5 подтверждён как простой snapshot, T6 подтверждён как candidate B с детализацией `warehouseId/chrtId/region`. Перед production-внедрением нужно выбрать канон A/B и правила записи в `STOCKS_WB`.

### 5.3. Хранение / приёмка

Storage и acceptance требуют отдельного решения канона и сверки с weekly finance. Не подключать к P&L без отдельного production PR.

### 5.4. Реклама

T8 принят как управленческий источник расхода для P&L и SKU-воронки; T9 — контроль списаний WB. Production-интеграция рекламы должна идти отдельным PR без коэффициента T9/T8.

---

## 6. Главные риски для P&L

| Риск | Почему опасно | Как закрывать |
|---|---|---|
| Подмена Excel finance новым API без сверки | можно сломать P&L | сначала parallel-run |
| Двойной учёт логистики | логистика может быть в finance и отдельном report | один канон расхода |
| Двойной учёт хранения | storage есть в finance и paid_storage | paid_storage только детализация, finance — итог |
| Реклама как SKU-факт без политики T8/T9 | можно смешать статистический расход и финансовые списания | T8 fullstats.sum используется для управленческого P&L и SKU-воронки; T9 updSum — контроль. `deltaPercent <= 5%` = OK, `> 5%` = WARNING + ручная сверка кабинета WB |
| Остатки из разных endpoints | разные срезы времени | один источник snapshot |
| SKU mapping по названию | риск неверного SKU | только `nmId` / barcode / vendorCode |
| Удержания WB | могут быть строками без SKU | нужен отдельный кабинетный bucket |

---

## 7. Рекомендация по следующему PR

Этап тестирования API закрыт. Следующие PR должны быть production-интеграцией по блокам, без смешивания всех источников в один большой релиз.

Рекомендуемый порядок:

1. **Finance production PR** — внедрить подтверждённый T1/T2 mapping в production finance / CLEAN / P&L logic.
2. **Ads production PR** — внедрить T8 в рекламную воронку и SKU allocation; T9 оставить как контроль списаний.
3. **Stocks production PR** — внедрить T5/T6 в `STOCKS_WB` после выбора канона A/B.
4. **Storage / Acceptance PR** — подключать только после отдельного решения канона и сверки с finance.

Запрещено в следующем production PR:

- массово переписывать P&L;
- удалять legacy finance до parallel-run;
- смешивать хранение из двух источников;
- считать рекламу по SKU без явного правила T8/T9;
- перезаписывать `SKU_MASTER`.

---

## 8. Результаты второго прогона после contract-adjustment PR

Контрольный период: **2026-05-18 — 2026-05-24**.

Цель второго прогона — проверить исправления test harness после первого фактического запуска T1–T11:

- finance checksum mapping для T1/T2;
- parsing `data.items` для T6;
- раздельные stock checksums для T5 без двойного счёта;
- новую рекламную политику T8-first / T9-control.

### 8.1. Finance T1/T2

| Тест | Статус | rows | Ключевой результат |
|---|---:|---:|---|
| T1 new Finance API | PASSED | 1472 | mapping исправлен |
| T2 legacy finance | PASSED | 1472 | совпал с T1 по ключевым суммам |

Сверка T1/T2:

| Метрика | Значение |
|---|---:|
| `logistics` | 20697.22 |
| `storage` | 6099.35 |
| `acceptance` | 0 |
| `acquiring` | 6277.33 |
| `rebillLogistics` | 2122.38 |

Вывод:

- `deliveryService` корректно маппится в `logistics`.
- `paidStorage` корректно маппится в `storage`.
- `paidAcceptance` корректно маппится в `acceptance`.
- `rebillLogisticCost` / `rebill_logistic_cost` вынесен в отдельный checksum `rebillLogistics` и не смешивается с `acquiring`.
- T1 и T2 дают одну финансовую картину за период.

### 8.2. Остатки T5/T6

| Тест | Статус | rows | Ключевой результат |
|---|---:|---:|---|
| T5 warehouse_remains | PASSED | 21 | double count устранён |
| T6 stocks-report/wb-warehouses | PARTIAL | 169 | `data.items` распарсен |

T5 checksums:

| Поле | Значение |
|---|---:|
| `stockTotalQtyFromTotalRows` | 5269 |
| `stockPhysicalQtyByWarehouses` | 5269 |
| `inWayToClientQty` | 145 |
| `inWayFromClientQty` | 23 |
| `warehouseRows` | 128 |
| `totalRowsPresent` | true |

T6 checksums:

| Поле | Значение |
|---|---:|
| `totalQuantity` | 5269 |
| `inWayToClient` | 146 |
| `inWayFromClient` | 23 |
| `uniqueNmId` | 21 |
| `unmatchedNmId` | 0 |
| `hasWarehouse` | true |
| `hasRegion` | true |
| `hasWarehouseId` | true |
| `hasChrtId` | true |

Вывод:

- T5 больше не использует старый double-count `totalQty`.
- Физический остаток T5 и T6 совпал: `5269 = 5269`.
- T6 больше не возвращает `rows=0`; фактическая схема `rawResponse.data.items` подтверждена.
- T6 оставлен как `PARTIAL`, потому что это candidate B и production-канон ещё не выбран.

### 8.3. Реклама T7/T8/T9

| Тест | Статус | rows | Метрика |
|---|---:|---:|---:|
| T7 campaigns | PASSED | 386 | campaigns loaded |
| T8 fullstats | PASSED | 578 | `sumSpend=28276.64` |
| T9 upd | PASSED | 93 | `sumUpd=27342` |

Сверка:

| Метрика | Значение |
|---|---:|
| T8 total spend | 28276.64 |
| T9 total updSum | 27342 |
| Delta amount | 934.64 |
| `deltaPercent` | 3.31% |
| Status | OK |

Решение:

- T8 `adv/v3/fullstats` — источник управленческого рекламного расхода для P&L, SKU-аналитики и рекламной воронки.
- T9 `adv/v1/upd` — контрольный источник списаний WB.
- Коэффициент T9/T8 не применяется.
- SKU-расходы не масштабируются к T9.
- При `deltaPercent <= 5%` статус `OK`.
- При `deltaPercent > 5%` статус `WARNING` и требуется ручная сверка кабинета WB.

### 8.4. Итог второго прогона

| Блок | Итог |
|---|---|
| Finance T1/T2 | подтверждён тестовым запросом, суммы совпали |
| Stocks T5 | подтверждён, double count устранён |
| Stocks T6 | подтверждён как candidate B, `data.items` парсится |
| Ads T8/T9 | подтверждён T8-first / T9-control, delta `3.31% OK` |

Итоговое решение:

- Test harness после contract-adjustment PR валидирован.
- Этап тестирования API считается закрытым для T1/T2/T5/T6/T8/T9.
- Следующий этап — отдельные production PR по блокам: Finance → Ads → Stocks → Storage/Acceptance.

---

_Итог: тестовый контур T1–T11 выполнен; протестированные блоки подтверждены тестовым запросом, непротестированные детали остаются в статусе «требует отдельного решения канона». Следующий шаг — не расширять test harness, а переводить подтверждённые источники в production-логику отдельными малым PR._
