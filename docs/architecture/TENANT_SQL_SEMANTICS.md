# Пакет SQL выделенного арендатора Ozon: семантика (T4-c)

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
| `ozon_mart.DIM_OZON_ACCRUAL_TYPE` | GENERIC_DERIVED | type_id | таксономия платформы v1: 33 типа начислений Ozon → класс и уровень атрибуции |
| `ozon_mart.NORM_OZON_POSTING_LINE` | MARKETPLACE_FACT | отправление × SKU | текущий статус; `order_date_msk` |
| `ozon_mart.NORM_OZON_ACCRUAL` | MARKETPLACE_FACT | начисление × тип × SKU | знак как у Ozon (расход < 0); неизвестный тип → `UNCLASSIFIED` |
| `ozon_mart.NORM_OZON_POSTING_SETTLEMENT` | GENERIC_DERIVED | отправление × SKU | цена продавца за единицу, комиссия, начисления по классам |
| `ozon_mart.NORM_OZON_ADS_SKU_DAILY` | MARKETPLACE_FACT | сутки × кампания × SKU | атрибуция отчёта Performance, не списанные деньги |
| `ozon_mart.NORM_OZON_ADS_CAMPAIGN_DAILY` | MARKETPLACE_FACT | сутки × кампания | траты по отчёту Performance |
| `ozon_mart.SNAP_OZON_STOCK` | MARKETPLACE_FACT | дата снимка × SKU × склад | история — только с первого снимка |
| `ozon_mart.SNAP_OZON_PRICE` | MARKETPLACE_FACT | дата снимка × offer_id | история — только с первого снимка |
| `ozon_mart.DIM_OZON_PRODUCT` | GENERIC_DERIVED | SKU | каталог + `ref.REF_SKU_CHANNEL_MAP` + `ref.REF_PRODUCT_MASTER`; нет соответствия — `UNMAPPED` |
| `ozon_mart.ECON_TENANT_COGS` | TENANT_CONFIG | артикул × интервал | себестоимость продавца из `ref.REF_COGS` |
| `ozon_mart.FACT_OZON_SALES_DAILY` | GENERIC_DERIVED | сутки заказа × SKU | заказы и их текущий исход |
| `ozon_mart.FACT_OZON_SKU_ECONOMICS_DAILY` | GENERIC_DERIVED | сутки заказа × SKU (доставленные) | выручка продавца, комиссия, расходы по отправлениям, себестоимость с покрытием |
| `ozon_mart.FACT_OZON_STORE_COSTS_DAILY` | GENERIC_DERIVED | сутки начисления × класс × SKU | начисления без отправления |
| `tenant_ops.V_TENANT_STATE_CURRENT` | GENERIC_DERIVED | арендатор | последнее событие автомата |
| `tenant_ops.V_SELLER_BINDING_STATUS` | GENERIC_DERIVED | API | BOUND / UNBOUND / MISMATCH / NOT_OBSERVED |
| `tenant_ops.V_ENTITY_COVERAGE` | GENERIC_DERIVED | сущность | дни по статусам полноты |
| `tenant_ops.V_DQ_UNCLASSIFIED_ACCRUALS` | GENERIC_DERIVED | сутки × type_id | начисления вне таксономии |

## 3. Экономика: что считается и чего не придумывается

- **Выручка продавца** доставленного заказа = цена продавца за единицу из расчётов Ozon ×
  количество. Если Ozon ещё не рассчитал отправление, используется цена отправления, а
  `revenue_basis` = `NOT_SETTLED` / `PARTIALLY_SETTLED`.
- **Комиссия** — из `commission_rub` (в `amount_rub` она не входит).
- **Расходы Ozon по отправлению** — логистика и последняя миля, эквайринг, обратная логистика,
  прочее, по классам таксономии, со знаком Ozon.
- **Результат до себестоимости** публикуется, только если в строке нет неклассифицированных
  начислений (`taxonomy_status`); иначе NULL. Правило таксономии: новый тип останавливает
  публикацию до классификации.
- **Себестоимость** — только из `ref.REF_COGS` продавца на дату заказа. `cogs_coverage`:
  `COMPLETE` / `PARTIAL` / `NOT_AVAILABLE`. Результат после себестоимости — только при
  `COMPLETE`, иначе NULL. Умолчаний нет.
- **Начисления без отправления** (подписка, биллинг продвижения, хранение, компенсации) — в
  `FACT_OZON_STORE_COSTS_DAILY` по дате начисления. На SKU-сутки заказа они не разносятся.
- **Реклама**: атрибуция отчёта (`NORM_OZON_ADS_*`) и биллинг (класс `PROMOTION_BILLING`)
  хранятся раздельно и не смешиваются.

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

## 5. Таксономия начислений — оговорка

Классы 33 типов — смысл операций Ozon (логистика, эквайринг, продвижение), а не калибровка.
Но набор типов собран по одному кабинету: у другого продавца могут встретиться новые типы.
Поэтому неизвестный тип никогда не относится в «прочее»: он остаётся `UNCLASSIFIED`, виден в
`tenant_ops.V_DQ_UNCLASSIFIED_ACCRUALS` и блокирует результат суток. Классификацию нового типа
вносят в `DIM_OZON_ACCRUAL_TYPE` новой версией пакета (PR).
