# Temporal semantics Unitka: PROVISIONAL → SETTLED (design, НЕ внедрять в 5A)

## 1. Текущее состояние (доказано 15.09)
`V_UNITKA_COMMISSION_RATES` и `V_UNITKA_LOGISTICS_RATES` — одно окно `[LCD−29, LCD]` по `_rr_date`,
одна ставка на nm_id, движок пишет её во ВСЕ строки месяца (включая будущие). Каждое продвижение
LCD переоценивает все закрытые дни (`MODEL_PARAMETER_REFRESH` 1 171–1 381 ячейка/прогон).
Масштаб на портфеле: комиссия ≤ 0,18 п.п., прямая логистика ≤ 0,35 ₽/ед (01–14.09).

## 2. Какие метрики provisional
| метрика | provisional источник (as-of) | actual источник |
|---|---|---|
| commission_rate (тариф + эквайринг) | окно `[d−29, d]` на момент закрытия d | `V_WB_FINANCE_CANONICAL` строки «Продажа»: `commission_percent`, `acquiring_fee`, `retail_price_withdisc_rub`, `quantity` (тождество комиссии: retail_withdisc − for_pay, см. commission-reconciliation) |
| direct logistics ₽/ед | то же окно | «Логистика»/«Доставка» `logistics_amount`, rn=1 по srid |
| reverse logistics ₽/отказ | то же окно | legs ≥ 2 по srid |
Не provisional (event-date факты, late correction легитимна): price, ads, storage, views/opens/carts/orders.

## 3. Ключ связи факт → SKU/day
`V_WB_FINANCE_CANONICAL.srid` = `FACT_ORDERS.order_srid` → (`nm_id`, `order_date`).
День атрибуции = `order_date` (строки Юнитки идут по дате заказа). Требует OD: order_date vs sale_dt
для комиссии (по sale_dt ставка «попадает» в другой день при лаге доставки).

## 4. Когда день settled
Доля некотменённых заказов дня d с финансовой строкой (продажа или отказ) — `coverage(d)`.
Факт 15.09: 18–28.08 = 100 %, 29.08–03.09 = 85–95 %, 09–10.09 = 62–64 %, 13–14.09 = 0 %.
Предложение: `SETTLED ⇔ coverage(d) ≥ 0.98 ИЛИ d ≤ today − 28` (принудительно; остаток
заказов без финансов — по as-of оценке с флагом `residual_provisional`). Проверить на 60 днях до выката.

## 5. Хранение и ревизии
```sql
CREATE TABLE wb_mart.UNITKA_DAY_PARAMETERS (
  nm_id INT64, day DATE, metric STRING,            -- commission_rate | direct_logistics | reverse_logistics
  value NUMERIC, state STRING,                     -- PROVISIONAL | SETTLED
  revision_no INT64, estimator_version STRING,
  window_from DATE, window_to DATE, as_of_lcd DATE,
  coverage_pct NUMERIC, residual_provisional BOOL,
  source_run_id STRING, reason_code STRING,        -- CLOSE | SETTLE | LATE_SOURCE_CORRECTION
  valid_from TIMESTAMP, superseded_at TIMESTAMP
) PARTITION BY day CLUSTER BY nm_id, metric;
```
Append-only; текущая версия = `superseded_at IS NULL`. PROVISIONAL пишется ОДИН раз при закрытии d
(as-of окно, заканчивающееся d) и больше не меняется от движения окна.

## 6. Late correction после settlement
Новая ревизия только если финансы по srid дня d изменились (новые/исправленные строки WB) и
|Δ| > допуска (предложение 0,5 ₽ на день×SKU); reason `LATE_SOURCE_CORRECTION`, в cell audit.
Движение rolling window → никогда не причина ревизии.

## 7. Что изменится при миграции
Сентябрь 01–14.09: закрытые дни с coverage ≥ 0,98 (сейчас ≈ 01.09) → actual; остальные →
as-of оценка. Порядок величины: ±1 ₽/день×SKU (как текущие MODEL_REFRESH). Будущие строки
месяца: остаются текущей оценкой (прогноз, не закрытые дни).

## 8. Backfill
Не нужен для истории до 01.09 (старый контур, значения заморожены). Сентябрь — одноразовый
пересчёт ТОЛЬКО после approval, через SHADOW diff по всем ячейкам + cell audit.

## 9. Открытые решения владельца
OD-T1 день атрибуции комиссии (order_date/sale_dt); OD-T2 порог settlement 0,98 / 28 дней;
OD-T3 допуск late correction; OD-T4 разрешение одноразового пересчёта сентября.
