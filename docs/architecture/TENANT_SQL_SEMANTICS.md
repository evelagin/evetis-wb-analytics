# Пакет SQL выделенного арендатора Ozon: семантика (T4-c, T4-d)

Дата: 2026-09-27. Пакет: `sql/tenant/ozon/` (`PACKAGE.json`, версия 1). Рендер и правила
P1–P8: `tools/tenancy/sql_package.py`.

## 1. Принцип

Витрины EVETIS **не переносятся**. Пакет собран заново из нормализованных фактов Ozon. Каждое
вычисление относится к одному из классов:

| Класс | Что это | Попадает в пакет |
|---|---|---|
| `MARKETPLACE_FACT` | то, что отдаёт Ozon (отправление, начисление, отчёт рекламы, снимок) | да |
| `GENERIC_DERIVED` | производное, одинаковое для любого продавца (сутки по Москве, суммы по классам, статусы полноты) | да |
| `TENANT_CONFIG` | допущение, которое задаёт продавец или оператор (себестоимость, налоги, фулфилмент) | только через `ref` арендатора |
| эмпирика EVETIS | константы и эвристики, откалиброванные на данных EVETIS | **нет** (валидатор P7/P8) |

Константа, которую нельзя обосновать ни правилом Ozon, ни конфигурацией арендатора, в пакет не
входит. Даты 19xx/20xx в коде пакета запрещены (P8): бизнес-даты — только конфигурация.

## 2. Объекты

Все объекты — представления. Сутки площадки — по времени Москвы (`Europe/Moscow`): так Ozon
группирует отчёты. RAW хранит дату отправления по UTC; пакет считает сутки заказа от
`created_at` по Москве.

| Объект | Класс | Строка (grain) | Основание |
|---|---|---|---|
| `ozon_mart.DIM_OZON_ACCRUAL_TYPE` | GENERIC_DERIVED | type_id | таксономия платформы v2: 33 типа Ozon → класс; официальное имя типа и основание (§5) |
| `ozon_mart.NORM_OZON_POSTING_LINE` | MARKETPLACE_FACT | отправление × SKU | текущий статус; `order_date_msk` |
| `ozon_mart.NORM_OZON_ACCRUAL` | MARKETPLACE_FACT | начисление × тип × SKU | ключ записи RAW, последняя выгрузка; знак как у Ozon (расход < 0); неизвестный тип → `UNCLASSIFIED`; `is_unresolved` |
| `ozon_mart.NORM_OZON_POSTING_SETTLEMENT` | GENERIC_DERIVED | отправление × SKU | экономический блок (число, полнота), комиссия, начисления по классам (сумма классов = все начисления) |
| `ozon_mart.NORM_OZON_ADS_SKU_DAILY` | MARKETPLACE_FACT | сутки × кампания × SKU | атрибуция отчёта Performance, не списанные деньги |
| `ozon_mart.NORM_OZON_ADS_CAMPAIGN_DAILY` | MARKETPLACE_FACT | сутки × кампания | траты по отчёту Performance |
| `ozon_mart.SNAP_OZON_STOCK` | MARKETPLACE_FACT | дата снимка × SKU × склад | история — только с первого снимка |
| `ozon_mart.SNAP_OZON_PRICE` | MARKETPLACE_FACT | дата снимка × offer_id | история — только с первого снимка |
| `ozon_mart.DIM_OZON_PRODUCT` | GENERIC_DERIVED | SKU | каталог + `ref.REF_SKU_CHANNEL_MAP` + `ref.REF_PRODUCT_MASTER`; нет соответствия — `UNMAPPED`, несколько — `AMBIGUOUS` |
| `ozon_mart.ECON_TENANT_COGS` | TENANT_CONFIG | артикул × интервал | себестоимость продавца из `ref.REF_COGS`; открытый интервал — до следующего |
| `ozon_mart.FACT_OZON_SALES_DAILY` | GENERIC_DERIVED | сутки заказа × SKU | заказы и их текущий исход; пустая цена или количество — сумма NULL |
| `ozon_mart.FACT_OZON_SKU_ECONOMICS_DAILY` | GENERIC_DERIVED | сутки заказа × SKU (доставленные) | выручка продавца, комиссия и расходы со знаком Ozon, себестоимость с покрытием; результат только при полных данных |
| `ozon_mart.FACT_OZON_STORE_COSTS_DAILY` | GENERIC_DERIVED | сутки начисления × класс × уровень × SKU | все начисления вне экономики доставленных: магазин, товар, недоставленные и незагруженные отправления |
| `tenant_ops.V_TENANT_STATE_CURRENT` | GENERIC_DERIVED | арендатор | последнее событие автомата |
| `tenant_ops.V_SELLER_BINDING_STATUS` | GENERIC_DERIVED | API | BOUND / UNBOUND / MISMATCH / NOT_OBSERVED / INVALID_BINDING |
| `tenant_ops.V_ENTITY_COVERAGE` | GENERIC_DERIVED | сущность | дни по статусам полноты |
| `tenant_ops.V_DQ_UNRESOLVED_ACCRUALS` | GENERIC_DERIVED | сутки × type_id | начисления вне таксономии или с пустой суммой; `blocking_accruals` |
| `tenant_ops.V_DQ_SETTLEMENT_ANOMALIES` | GENERIC_DERIVED | отправление × SKU | несколько или неполные экономические блоки |
| `tenant_ops.V_DQ_COGS_OVERLAPS` | GENERIC_DERIVED | пара интервалов | пересечения себестоимости продавца |
| `tenant_ops.V_COVERAGE_DAILY` | GENERIC_DERIVED | сущность × сутки | последняя оценка полноты из `DATA_COVERAGE` |
| `analytics_share.sales_daily` | GENERIC_DERIVED | сутки заказа | продажи магазина; нули только при `COMPLETE` |
| `analytics_share.orders` | MARKETPLACE_FACT | отправление × SKU | заказы FBO, без данных покупателя, кроме города |
| `analytics_share.advertising_daily` | MARKETPLACE_FACT | сутки × кампания | отчёт Performance (не биллинг) |
| `analytics_share.sku_daily` | GENERIC_DERIVED | сутки × SKU | заказы + атрибуция рекламы, ДРР; реклама без заказов не теряется |
| `analytics_share.inventory_current` | MARKETPLACE_FACT | SKU × склад | последний снимок остатков, возраст снимка |
| `analytics_share.price_history` | MARKETPLACE_FACT | дата снимка × offer_id | цены продавца со дня первого снимка |
| `analytics_share.profitability_daily` | GENERIC_DERIVED | сутки заказа × SKU | вклад до/после себестоимости со статусами |
| `analytics_share.store_costs_daily` | GENERIC_DERIVED | сутки × класс | начисления магазина вне отправлений |
| `analytics_share.data_coverage` | GENERIC_DERIVED | сущность × сутки | полнота: «ноль» против «нет данных» |

## 3. Экономика: что считается и чего не придумывается

Все суммы — **со знаком Ozon**: выручка > 0, комиссия и расходы < 0, результат — их сумма.
(Комиссия в RAW отрицательна: витрины EVETIS меняют её знак, пакет — нет.)

- **Выручка продавца** доставленного заказа = цена продавца за единицу из экономического блока
  расчёта Ozon × количество. Отправление рассчитано, если у него ровно один полный блок (цена
  продавца и комиссия). Иначе — цена из отправления, `revenue_basis` = `NOT_SETTLED` /
  `PARTIALLY_SETTLED`, и результат строки не публикуется.
- **Комиссия** — `commission_rub` блока (в `amount_rub` она не входит).
- **Расходы Ozon по отправлению** — логистика и последняя миля, эквайринг, обратная логистика,
  продвижение по отправлению, прочее; колонки классов покрывают все начисления отправления.
- **Результат до себестоимости** публикуется, только если все отправления строки рассчитаны,
  выручка известна, нет нераспознанных начислений (тип вне таксономии с ненулевой суммой или
  пустая сумма — `taxonomy_status = UNRESOLVED`) и нет признака возврата (расходы обратной
  логистики у доставленного отправления: сторно возврата платформа пока не загружает).
  Иначе NULL.
- **Поздние начисления** по отправлению относятся к суткам заказа и меняют прошлые сутки при
  поступлении.
- **Себестоимость** — только из `ref.REF_COGS` продавца на дату заказа, ровно один действующий
  интервал. Нет или пересечение — себестоимости нет (`V_DQ_COGS_OVERLAPS`). `cogs_coverage`:
  `COMPLETE` / `PARTIAL` / `NOT_AVAILABLE`. Результат после себестоимости — только при
  `COMPLETE`. Умолчаний нет.
- **Начисления вне экономики доставленных** — в `FACT_OZON_STORE_COSTS_DAILY` по дате
  начисления, с уровнем `cost_scope`: `STORE`, `SKU`, `POSTING_NOT_DELIVERED` (отмены,
  невыкупы), `POSTING_NOT_LOADED` (отправление вне загруженной истории),
  `POSTING_SKU_UNMATCHED`. Экономика + эти начисления = все начисления, без пропусков и
  повторов (тест сохранения).
- **Реклама**: атрибуция отчёта (`NORM_OZON_ADS_*`) и биллинг (классы `PROMOTION_*` в
  начислениях) хранятся раздельно и не смешиваются.

## 4. Витрины EVETIS: что извлечено, что нет

| Витрина EVETIS | Извлечено в пакет | Не перенесено (почему) |
|---|---|---|
| `FCT_OZON_SKU_PNL_DAILY` | выручка по цене продавца × количество (GENERIC_DERIVED); классы type_id (таксономия) | присоединение и эвристика выкупа СНГ («доставлено, выплата 0») — эмпирика EVETIS; себестоимость EVETIS |
| `FCT_OZON_SKU_PNL_MONTHLY` | — (месячные агрегаты строятся из суточных фактов) | product master и себестоимость EVETIS, выкуп СНГ |
| `FCT_OZON_PNL_MONTHLY` | — | границы комиссии 0,18/0,52 (калибровка по категориям EVETIS), выкуп СНГ |
| `V_OZON_LIFETIME_PNL` | — | статусы и зависимости витрин EVETIS |
| `V_OZON_SKU_PNL_DAILY_OPERATIONAL` | — | эвристика выкупа, порог 36 дней, себестоимость EVETIS |
| `V_OZON_SKU_CURRENT_TARIFF` | — | даты тарифа, ×1,20, 15 ₽, 2,50 ₽, 99 дней, 9 990 ₽ Premium — тарифные допущения на дату; для арендатора — будущая `TENANT_CONFIG` или данные цен/комиссий |
| `V_OZON_TARIFF_SOURCE_HEALTH` | — | зависит от текущего тарифа EVETIS |
| `V_OZON_SKU_FORWARD_ECONOMICS_CURRENT` | — | доля отгрузок после отмены 0,558 (портфель EVETIS), сетки целевой маржи и ДРР |
| `V_OZON_SKU_FBO_FBS_COMPARISON_CURRENT` | — | сетка стоимости фулфилмента; FBS у арендатора не используется |
| `V_OZON_SKU_UNIT_ECONOMICS_CURRENT` | — | целевые пороги 0,10–0,25 |
| `V_OZON_AGENT_DECISION_INPUT` | — | вход агента EVETIS |
| `V_OZON_PROMO_ECONOMICS_BASIS_HISTORY` | — | снимок экономики акций EVETIS; модуль акций у арендатора выключен |
| `V_OZON_PROMO_ECONOMICS_SCENARIO_HISTORY` | — | то же |

Четыре generic-представления EVETIS (политика комиссии, журнал тарифов, оценка логистики,
свежесть) в v1 пакета не включены: для первых — нужна история цен/комиссий арендатора (копится с
запуска), свежесть заменена покрытием `tenant_ops` (T5 пишет `DATA_COVERAGE`).

## 5. Таксономия начислений: провенанс

**Официальное Ozon** — только `type_id`, имя и описание типа из справочника
`POST /v1/finance/accrual/types` (124 типа; снимок 31.08.2026 в архиве аудита
`ozon/audit_2026-08-30/raw/backfill_v1/finance/accrual_types.json`, sha256
`1bf25e8b887ecf45e1c6cf0950723b9db4318ef3a94560d1c9a7cfb84c2313c2`, совпадает с
`docs/ozon/audit_2026-08-30/raw/MANIFEST.csv`). **Класс — решение платформы**: Ozon классов не
публикует. Основание класса у каждого типа:

- `OZON_TYPE_NAME` — класс прямо следует из официального имени и описания (Logistic →
  LOGISTICS, Acquiring → ACQUIRING, PremiumSubscription → SUBSCRIPTION…);
- `PLATFORM_INTERPRETATION` — группировка платформы: платные программы продавца собраны в
  PROMOTION_SERVICES, остаточные услуги — в OTHER_MARKETPLACE_COST, обработка возвратов
  партнёрами и размещение возвратов — в RETURN_LOGISTICS.

**Эмпирика EVETIS и что с ней сделано.** Набор из 33 типов — это типы, которые встретились в
одном кабинете (EVETIS). Поэтому он не считается полным: 91 официальный тип не классифицирован
и остаётся `UNCLASSIFIED`. Уровень атрибуции (прежний `attribution_scope` в таксономии v1)
был наблюдением по тому же кабинету — в v2 он убран и берётся из самой строки начисления
(есть отправление / есть SKU / магазин). Разнесение классов по статьям EVETIS (списки type_id в
витринах EVETIS) в пакет не переносилось.

**Неизвестный тип** никогда не относится в «прочее»: он остаётся `UNCLASSIFIED`, виден в
`tenant_ops.V_DQ_UNRESOLVED_ACCRUALS`, а при ненулевой или пустой сумме блокирует результат
затронутых строк. Новый тип классифицируется одной строкой в `DIM_OZON_ACCRUAL_TYPE` новой
версией пакета (PR) — сразу для всех арендаторов; кода конкретного арендатора нет, и type_id
нигде, кроме таксономии, не упоминается (тест).

| type_id | Имя Ozon | Описание Ozon | Класс | Основание |
|---|---|---|---|---|
| 75 | Stencil | Трафареты | PROMOTION_BILLING | OZON_TYPE_NAME |
| 41 | PayPerClick | Оплата за клик | PROMOTION_BILLING | OZON_TYPE_NAME |
| 33 | Marketing | Рекламные услуги | PROMOTION_BILLING | OZON_TYPE_NAME |
| 54 | Promotion | Продвижение товара | PROMOTION_BILLING | OZON_TYPE_NAME |
| 47 | PointsForReviews | Баллы за отзывы | PROMOTION_SERVICES | PLATFORM_INTERPRETATION |
| 96 | AcceleratedReviewCollection | Ускоренный сбор отзывов | PROMOTION_SERVICES | PLATFORM_INTERPRETATION |
| 116 | FirstCustomerReview | Сбор первых отзывов | PROMOTION_SERVICES | PLATFORM_INTERPRETATION |
| 74 | StarsMembership | Звёздные товары | PROMOTION_SERVICES | PLATFORM_INTERPRETATION |
| 48 | PremiumCashbackIndividualPoints | Бонусы продавца | PROMOTION_SERVICES | PLATFORM_INTERPRETATION |
| 52 | PremiumSubscription | Подписка Premium | SUBSCRIPTION | OZON_TYPE_NAME |
| 32 | Logistic | Логистика | LOGISTICS | OZON_TYPE_NAME |
| 12 | CrossDock | Кросс-докинг | LOGISTICS | OZON_TYPE_NAME |
| 29 | LastMileCourier | Доставка до места выдачи | LAST_MILE | OZON_TYPE_NAME |
| 28 | LastMile | Последняя миля | LAST_MILE | OZON_TYPE_NAME |
| 98 | DeliveryToHandoverPlaceByOzon | Доставка до места выдачи силами Ozon | LAST_MILE | OZON_TYPE_NAME |
| 30 | LastMilePickUpPoint | Выдача товара | LAST_MILE | OZON_TYPE_NAME |
| 79 | TemporaryPlacementsAgent | Временное размещение товара партнерами | STORAGE | OZON_TYPE_NAME |
| 46 | Placements | Размещение товаров на складах Ozon | STORAGE | OZON_TYPE_NAME |
| 1 | Acquiring | Эквайринг | ACQUIRING | OZON_TYPE_NAME |
| 59 | ReturnFlowLogistic | Обратная логистика | RETURN_LOGISTICS | OZON_TYPE_NAME |
| 9 | ClientReturn | Обработка возвратов | RETURN_LOGISTICS | OZON_TYPE_NAME |
| 45 | PickUpPointReturnAcceptance | Обработка возвратов, отмен и невыкупов партнёрами | RETURN_LOGISTICS | PLATFORM_INTERPRETATION |
| 78 | TemporaryPlacement | Краткосрочное размещение возврата FBS | RETURN_LOGISTICS | PLATFORM_INTERPRETATION |
| 6 | Cancellation | Обработка отменённых и невостребованных товаров | CANCELLATION_COST | OZON_TYPE_NAME |
| 15 | Disposal | Утилизация | OTHER_MARKETPLACE_COST | PLATFORM_INTERPRETATION |
| 71 | SellerReturns | Вывоз товара со склада силами Ozon | OTHER_MARKETPLACE_COST | PLATFORM_INTERPRETATION |
| 39 | PackingFee | Упаковка товара партнёрами | OTHER_MARKETPLACE_COST | PLATFORM_INTERPRETATION |
| 38 | PackageCost | Обеспечение материалами для упаковки товара | OTHER_MARKETPLACE_COST | PLATFORM_INTERPRETATION |
| 77 | SupplyInbound | Обработка товара | OTHER_MARKETPLACE_COST | PLATFORM_INTERPRETATION |
| 76 | StockInsurance | Страхование товара от массовых повреждений | OTHER_MARKETPLACE_COST | PLATFORM_INTERPRETATION |
| 57 | RealizationReportCorrection | Корректировка стоимости услуг | OTHER_MARKETPLACE_COST | PLATFORM_INTERPRETATION |
| 25 | ItemCompensation | Товарная компенсация | COMPENSATION | OZON_TYPE_NAME |
| 10 | Compensation | Компенсация | COMPENSATION | OZON_TYPE_NAME |

## 6. `analytics_share` — семантический слой клиента (T4-d)

Машиночитаемый контракт: `sql/tenant/ozon/analytics_share/CONTRACT.json` — смысл, строка
(grain), измерения, метрики (единица, аддитивность), колонки статуса полноты, lineage (прямые
объекты и исходные таблицы), свежесть, полнота, ограничения. Тесты сверяют контракт с SQL: каждая
колонка представления описана ровно один раз, lineage совпадает с разбором SQL.

Правила слоя:

- читает только `ozon_mart` и `tenant_ops` (P4), RAW и `ref` — никогда напрямую;
- **нет данных ≠ ноль**: статус полноты берётся из `tenant_ops.DATA_COVERAGE`, отсутствие оценки —
  `UNKNOWN`; нулевые метрики за сутки без строк выдаются только при `COMPLETE`, иначе NULL;
  `COALESCE(метрика, 0)` в слое запрещён тестом;
- результат до/после себестоимости — вклад, а не прибыль: налоги, OPEX и фулфилмент продавца не
  входят;
- реклама в двух смыслах, не смешиваются: атрибуция отчёта (`advertising_daily`, `sku_daily`) и
  биллинг (`store_costs_daily`, класс `PROMOTION_BILLING`);
- снимки (остатки, цены) не восстанавливают прошлое: история начинается с первого снимка.

Доступ: в T4 никому, кроме владельцев проекта (ACL `projectOwners`). Механизм доступа клиента —
T6 (см. `TENANCY_DESIGN.md` §4c).
