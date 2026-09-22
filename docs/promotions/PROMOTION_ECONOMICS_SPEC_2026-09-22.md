# EVETIS — спецификация экономики акций
# Версия модели: `PROMO_ECON_V1`. Налоговый статус: `PRE_TAX`. 2026-09-22.

Ни одной новой формулы прибыли. Всё, что ниже, — это существующая экономика EVETIS,
вычисленная при **акционной цене вместо текущей**.

---

## 0. Инвариант, который нельзя нарушать

```
contribution_before_ads(P) = P · (1 − take_frac) − fixed_rub − COGS_rub
break_even_before_ads      = (fixed_rub + COGS_rub) / (1 − take_frac)
max_affordable_drr(P)      = contribution_before_ads(P) / P
```

Одна алгебра на обеих площадках — это не совпадение, а проверенный факт:

| | WB | Ozon |
|---|---|---|
| `take_frac` | `(effective_commission_pct + acquiring_p50_pct)/100` | `(commission_pct + acquiring_pct)/100` |
| `fixed_rub` | `logistics_expected_rub` | `last_mile_rub + logistics_expected_rub` |
| `COGS_rub` | `cogs_rub` | `current_management_cogs_rub` |
| Источник формулы | `sql/pricing/pr2_wb_forward_economics.sql:137-140` | `sql/current/ozon_mart/V_OZON_SKU_FORWARD_ECONOMICS_CURRENT.sql:33,83` |
| Проверка тождества | `for_pay = retail_price_withdisc × (1 − comm%) − acquiring`, 38 698 строк, 97,2 % ±0,02 ₽ | `break_even_price_expected = (fixed + COGS)/(1 − c_frac − a_frac)` — пересчитано вручную по 20 SKU, совпало |

**Правило реализации.** `V_PROMO_SKU_SCENARIO` **не переписывает** эти выражения. Она
берёт из `V_*_FORWARD_ECONOMICS_CURRENT` компоненты (`take_frac`, `fixed_rub`, `COGS_rub`)
и подставляет `P`. Статический тест PR-PROMO-3 сравнивает текст выражения вклада в
сценарной вью с текстом в авторитетном файле — расхождение роняет сборку. Причина: два
экземпляра одной формулы рано или поздно разойдутся, и разойдутся молча.

Почему не `wb_mart.TVF_WB_FORWARD_ECONOMICS`: эта TVF принимает **одну** `scenario_price`
на все SKU сразу, а в акции у каждого SKU своя требуемая цена. Переиспользуется не её
вызов, а её алгебра.

---

## 1. Авторитетный источник каждого входа

| Вход | WB | Ozon | Класс |
|---|---|---|---|
| Текущая цена продавца | `V_WB_SKU_FORWARD_ECONOMICS_CURRENT.seller_effective_price_rub` ← `wb_raw.V_WB_PRICES_CURRENT` (каденс 20 мин) | `V_OZON_SKU_FORWARD_ECONOMICS_CURRENT.seller_base_price` ← `RAW_OZON_PRICES` (суточный) | `CONFIRMED_EXISTING_CODE` |
| Требуемая акционная цена | **нет источника** (`nomenclatures` неприменим к автоакциям) | `V_OZON_PROMO_SKU_CURRENT.required_price_rub` | `CONFIRMED_API` |
| Максимально допустимая акционная цена | **нет источника** | `max_action_price` | `CONFIRMED_API` |
| Себестоимость | `cogs_rub`, `cogs_source`, `cogs_effective_from` ← `evetis_ref.V_PRODUCT_COGS_EFFECTIVE` | `current_management_cogs_rub`, `cogs_basis` ← та же вью | `CONFIRMED_EXISTING_CODE` |
| Комиссия | `effective_commission_pct` = base (`V_WB_TARIFFS_CURRENT`, `paidStorageKgvp` по предмету) + addon (`REF_MARKETPLACE_COMMISSION_COMPONENT`); сверка с медианой факта за 45 сут | `commission_pct` ← `V_OZON_SKU_CURRENT_TARIFF` | `CONFIRMED_EXISTING_CODE` |
| Эквайринг | `acquiring_p50_pct` (окно 45 сут; p75 — консервативный, p90 — стресс) | `acquiring_rub` = `acquiring_max_rub`, доля от цены | `CONFIRMED_EXISTING_CODE` |
| Логистика | `logistics_expected_rub` = Σлогистика / Σпродажи за 90 сут (нагрузка на проданную единицу, включает невыкуп); `logistics_p75_rub`, `logistics_p90_rub` | `logistics_expected_rub` + `last_mile_rub`; есть `logistics_min/max` | `CONFIRMED_EXISTING_CODE` |
| Возвраты и отмены | внутри `logistics_expected_rub` (нагрузка на проданную единицу) | отдельно: `cancellation_rate_pct`, `expected_return_cost_rub`, `cancel_cost_if_shipped_*` | `CONFIRMED_EXISTING_CODE` |
| Реклама | **не входит в вклад**; лимит = `max_affordable_drr_pre_tax_pct` | то же; `break_even_drr_expected_pct` | `CONFIRMED_EXISTING_CODE` |
| Налог | **не входит**, `tax_model_status = 'PRE_TAX'` в обеих моделях | то же | `CONFIRMED_EXISTING_CODE` |
| Хранение | **не входит** в маргинальный вклад (`docs/pricing/PR2_PHASE_A_DISCOVERY.md:231`) | не входит | `CONFIRMED_EXISTING_CODE` |
| Остатки и скорость | `wb_mart.V_CT_INVENTORY_TRUTH`: `sellable_units`, `marketplace_units`, `units_per_day_7d/30d`, `days_of_stock_*`, `inbound_eta`, `days_to_expiry` | та же вью — она кросс-маркетплейсная | `CONFIRMED_EXISTING_CODE` |
| Исторический факт | `evetis_mart.FACT_SKU_DAILY` (WB с 2024-09-05, OZON с 2025-04-26) | та же | `CONFIRMED_EXISTING_CODE` |

### 1.1 Что намеренно НЕ входит

Налог, хранение, приёмка, штрафы, удержания уровня кабинета, фулфилмент, OPEX, реклама.
Это ровно тот же периметр, что у `WB_FE_V1`. Менять его в движке акций нельзя: получилась
бы вторая экономика, не сходящаяся с Executive.

### 1.2 Fail closed

Строка сценария **не считается**, если отсутствует любой обязательный вход. Ноль не
подставляется никогда.

| Условие | Результат сценария |
|---|---|
| `cogs_rub IS NULL` | `BLOCKED`, `blocked_reason = 'COGS_MISSING'` |
| `logistics_expected_rub IS NULL` | `BLOCKED`, `'LOGISTICS_MISSING'` |
| комиссия NULL или `commission_reconciliation_status = 'MISMATCH'` | `BLOCKED`, `'COMMISSION_UNRELIABLE'` |
| `economics_status = 'BLOCKED'` в исходной вью | `BLOCKED`, `'UPSTREAM_BLOCKED'` |
| `required_price_rub IS NULL` при `scenario = 'ENTER'` | `BLOCKED`, `'PROMO_PRICE_UNAVAILABLE'` — **весь WB сегодня здесь** |
| `take_frac ≥ 1` | `BLOCKED`, `'TAKE_RATE_INVALID'` |
| возраст наблюдения цены > SLA | `economics_confidence = 'LOW'`, не блок |

---

## 2. Сценарии

Грейн: `marketplace × promotion_id × internal_sku × scenario`.

| Сценарий | Цена `P` | Когда строится |
|---|---|---|
| `BASELINE` | текущая цена продавца | всегда |
| `ENTER` | `required_price_rub` | `is_candidate` или `is_auto_add_scheduled` |
| `STAY` | `required_price_rub` участника | `is_participating` |
| `EXIT` | текущая цена продавца вне акции | `is_participating` |
| `MAX_ALLOWED` | `max_action_price_rub` | есть `max_action_price` |
| `AT_MAX_BOOST` | `price_at_max_boost_rub` | есть границы эластичности |

`EXIT` численно равен `BASELINE`, но это **разные решения**: `EXIT` теряет бустинг, а
`BASELINE` его и не имел. Разница фиксируется в `boost_delta_pp`, а не в цене.

### 2.1 Поля сценария

Для каждой строки:

| Поле | Формула |
|---|---|
| `scenario_price_rub` | `P` |
| `customer_price_rub` | **Ozon:** NULL, с `customer_price_status = 'NOT_OBSERVABLE_PRE_SALE'` (разрыв до −29 %, PR-0 §5.2). **WB:** NULL, `'SPP_NOT_OBSERVABLE'` |
| `revenue_per_unit_rub` | `P` — база выручки везде цена ПРОДАВЦА; СПП и субсидии площадки выплату не увеличивают |
| `marketplace_cost_per_unit_rub` | `P · take_frac` |
| `logistics_per_unit_rub` | `fixed_rub` |
| `cogs_per_unit_rub` | `COGS_rub` |
| `tax_per_unit_rub` | NULL, `tax_model_status='PRE_TAX'` |
| `contribution_before_ads_rub` | `P·(1−take_frac) − fixed − COGS` |
| `contribution_before_ads_pct` | `contribution_before_ads_rub / P · 100` |
| `break_even_price_rub` | `(fixed + COGS)/(1 − take_frac)` |
| `margin_of_safety_rub` | `P − break_even_price_rub` |
| `max_ad_spend_per_unit_rub` | `contribution_before_ads_rub` |
| `break_even_drr_pct` | `contribution_before_ads_pct` |
| `contribution_after_ads_rub(d)` | `P·(1 − take_frac − d/100) − fixed − COGS`, `d` = фактический ДРР SKU за 28 сут |
| `boost_pct` | `boost_current_pct` для `STAY`, `boost_max_pct` для `AT_MAX_BOOST`, 0 для `BASELINE`/`EXIT` |
| `case_basis` | `EXPECTED`; вторая строка с `WORST` (WB — `stress`, Ozon — `worst_case`) |

Сценарии считаются **и в expected, и в worst**. Решение ENTER/STAY принимается по
expected, но **запрет** — по worst: политика «не входить, если худший случай уводит ниже
точки безубыточности» дешевле, чем разбор по факту.

---

## 3. Ключевая метрика: требуемый рост продаж

Это ответ на вопрос «стоит ли акция того», не сводящийся к процентам маржи.

```
baseline_contribution_per_day = baseline_units_per_day × contribution_BASELINE_per_unit
required_units_per_day        = baseline_contribution_per_day / contribution_PROMO_per_unit
required_sales_uplift_pct     = required_units_per_day / baseline_units_per_day − 1
                              = contribution_BASELINE_per_unit / contribution_PROMO_per_unit − 1
```

`baseline_units_per_day` — `V_CT_INVENTORY_TRUTH.units_per_day_30d`, с запасным
`units_per_day_7d`; при обоих NULL → `INSUFFICIENT_DATA`, метрика не выдаётся.

Граничные случаи, которые обязаны обрабатываться явно:

| Случай | Поведение |
|---|---|
| `contribution_PROMO_per_unit ≤ 0` | `required_sales_uplift_pct = NULL`, `uplift_status = 'IMPOSSIBLE_NEGATIVE_CONTRIBUTION'` — рост продаж не спасает убыток |
| `contribution_BASELINE_per_unit ≤ 0` | `uplift_status = 'BASELINE_ALREADY_LOSS'` — сравнивать не с чем |
| `contribution_PROMO ≥ contribution_BASELINE` | `required_sales_uplift_pct ≤ 0`, `uplift_status = 'NO_UPLIFT_REQUIRED'` — именно этот случай у всех 18 SKU в «Эластичном бустинге» Ozon сегодня |
| `baseline_units_per_day = 0` | `uplift_status = 'NO_BASELINE_SALES'` |

Проверочный пример (метод, воспроизведённый на живых данных 2026-09-22, Ozon,
акция 4253043, кандидат offer_id 930334396):
вклад сейчас 160 ₽/шт, вклад по акционной цене 1130 ₽ — 9 ₽/шт →
`required_sales_uplift_pct = 160/9 − 1 = +1592 %`. Такой рост нереален; движок обязан
сказать это числом, а не ощущением.

---

## 4. Ограничение остатками

Из `wb_mart.V_CT_INVENTORY_TRUTH`, без второй истины:

| Поле | Формула |
|---|---|
| `available_units` | `marketplace_units` — то, что реально на площадке |
| `baseline_days_of_cover` | `available_units / units_per_day_30d` |
| `promo_days_of_cover` | `available_units / (units_per_day_30d × (1 + required_sales_uplift_pct))` |
| `promo_days_remaining` | `DATE_DIFF(ends_at, CURRENT_DATE)` |
| `stock_out_before_promo_end` | `promo_days_of_cover < promo_days_remaining` |
| `expected_stock_out_date` | `CURRENT_DATE + promo_days_of_cover` |
| `inbound_covers_gap` | `inbound_eta ≤ expected_stock_out_date AND inbound_units > 0` |
| `expiry_conflict` | `days_to_expiry < promo_days_of_cover` |
| `meets_min_stock` | `available_units ≥ min_stock_required` |

`meets_min_stock = FALSE` → `NOT_ELIGIBLE`, а не «плохая экономика»: площадка просто
не примет товар.

Отдельно фиксируется обратный случай: `baseline_days_of_cover` заметно выше горизонта
сезона при `days_to_expiry` близком — тогда акция может быть оправдана **даже при
отрицательном приросте вклада на единицу**, потому что альтернатива — списание. Это не
исключение из пола, а отдельный reason code `EXPIRY_PRESSURE`, и решение остаётся за
владельцем.

---

## 5. Историческая эффективность: `FACT_PROMO_SKU_WINDOW`

### 5.1 Окна

| Окно | Определение |
|---|---|
| `PRE` | `[starts_at − 14 сут, starts_at)` |
| `DURING` | `[starts_at, LEAST(ends_at, CURRENT_DATE)]` |
| `POST` | `(ends_at, ends_at + 14 сут]` |

14 суток — не догма, а следствие наблюдаемой частоты акций: на WB акции идут внахлёст
каждые 3–7 дней (см. календарь в `PROMOTION_DISCOVERY_2026-09-22.md` §3.1), поэтому более
длинное окно гарантированно захватит соседнюю акцию. Длина окна хранится в строке
(`window_days`), чтобы её можно было пересмотреть без переписывания модели.

### 5.2 Метрики окна

Источник — `evetis_mart.FACT_SKU_DAILY` (нейтральный кросс-МП факт):
`units_per_day`, `gmv_per_day_rub`, `contribution_per_day_rub`,
`ad_spend_per_day_rub`, `drr_pct`, `return_rate_pct`, `cancel_rate_pct`.
Из `wb_raw.V_WB_FUNNEL_DAILY` (только WB, только с 2026-09-04):
`open_card_count`, `add_to_cart_conversion`, `cart_to_order_conversion`, `buyout_percent`.
Из `RAW_*_PRICES`: `avg_seller_price_rub`, `price_changes_n`.
Из снимков остатков: `days_out_of_stock`.

### 5.3 Конфаундеры — проверяются, а не игнорируются

| Флаг | Условие |
|---|---|
| `confounder_stockout` | `days_out_of_stock > 0` в любом из окон |
| `confounder_ad_change` | `|ad_spend_per_day(DURING) − ad_spend_per_day(PRE)| / PRE > 30 %` |
| `confounder_price_change` | в окне `PRE` или `POST` была смена цены продавца |
| `confounder_overlapping_promo` | в окне активна другая акция того же SKU — **сегодня на WB это норма, а не исключение** |
| `confounder_seasonality` | окно пересекает известное событие площадки |
| `confounder_short_window` | `window_days < 7` |
| `confounder_content_change` | смена названия/характеристик карточки в окне |

### 5.4 Классы доверия — детерминированные определения

| Класс | Условие |
|---|---|
| `HIGH_EVIDENCE` | ≥3 завершённых акции по этому SKU; ни одного флага конфаундера ни в одной; в каждом окне ≥7 суток и ≥10 проданных единиц |
| `MEDIUM_EVIDENCE` | ≥2 акции; ≤1 флаг конфаундера суммарно; ≥7 суток и ≥5 единиц в окне |
| `LOW_EVIDENCE` | ≥1 акция с данными во всех трёх окнах, при любых конфаундерах |
| `INSUFFICIENT_DATA` | нет ни одной акции с полными окнами, либо окно раньше 2026-09-07 (WB) / 2026-08-31 (Ozon) — до этих дат нет истории цены |

**Сегодня все SKU обеих площадок получают `INSUFFICIENT_DATA`.** Это не дефект модели, а
её честный ответ: наблюдение акций начинается сейчас. Первый `LOW_EVIDENCE` появится после
первой завершённой акции с накопленными окнами — ориентировочно через 4–6 недель после
запуска PR-PROMO-1.

### 5.5 Разграничение, которое нельзя стирать

| Термин | Что это |
|---|---|
| `observed_difference_pct` | `(units_per_day(DURING) − units_per_day(PRE)) / units_per_day(PRE)` — **арифметика, не причинность** |
| `estimated_incremental_effect_pct` | появится только тогда, когда будет контроль (сопоставимые SKU без акции в тот же период) и конфаундеры сняты. **В V1 не вычисляется и колонка не заводится** |

Колонка `estimated_incremental_effect_pct` не создаётся пустой намеренно: пустое поле с
таким именем рано или поздно кто-нибудь заполнит разницей до/после.

### 5.6 Как историческое сравнивается с требуемым

```
uplift_plausibility = CASE
  WHEN evidence_class = 'INSUFFICIENT_DATA'                                  THEN 'UNKNOWN'
  WHEN required_sales_uplift_pct <= 0                                        THEN 'NOT_REQUIRED'
  WHEN required_sales_uplift_pct <= p50_observed_difference_pct              THEN 'PLAUSIBLE'
  WHEN required_sales_uplift_pct <= max_observed_difference_pct              THEN 'OPTIMISTIC'
  ELSE 'IMPLAUSIBLE' END
```

где `p50_observed_difference_pct` и `max_observed_difference_pct` берутся по завершённым
акциям **того же `action_type`/`promotion_type`** этого SKU, а при их нехватке — по
портфелю той же площадки, с понижением класса доверия на ступень.

---

## 6. Контрольные цифры для приёмки экономического слоя

| Проверка | Ожидание |
|---|---|
| `V_PROMO_SKU_SCENARIO` при `scenario='BASELINE'`, Ozon | `contribution_before_ads_rub` совпадает с `V_OZON_SKU_FORWARD_ECONOMICS_CURRENT.contribution_expected` до копейки по всем 20 SKU |
| То же, WB | совпадает с `contribution_before_ads_pre_tax_rub` по всем SKU |
| `break_even_price_rub` при `BASELINE`, Ozon | совпадает с `break_even_price_expected` |
| Число строк `scenario='ENTER'` для WB | **0**, у всех `blocked_reason = 'PROMO_PRICE_UNAVAILABLE'` |
| `required_sales_uplift_pct` для 18 участников «Эластичного бустинга» Ozon | ≤ 0, `uplift_status = 'NO_UPLIFT_REQUIRED'` (т. к. `action_price = price`) |
| `evidence_class` по всем SKU на дату развёртывания | `INSUFFICIENT_DATA` |
| Любая строка с `cogs_per_unit_rub IS NULL` | `economics_status = 'BLOCKED'`, ни одной рекомендации ENTER/STAY |
| Сумма `contribution_before_ads_rub` по `BASELINE` × `units_per_day_30d` | сходится с дневным вкладом `FACT_SKU_DAILY` в пределах, объяснимых разницей «форвард по текущей цене» и «факт по проданному» |
