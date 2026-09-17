# EXECUTIVE V2 — backend gaps (2026-09-16)

**Статус:** развёрнуто в production. Валидация: 16/16 проверок (`executive_v2_backend_validation.sql`) и 19/19 (`fin_contract_v2_validation.sql`).
**Контракт-основа:** `docs/FIN_CONTRACT_V2_2026-09-16.md` — не изменён.
**Metabase не менялся.** Executive V2 Phase B не начиналась.

| Артефакт | Путь |
|---|---|
| Миграция | `sql/dash/executive_v2_backend_2026-09-16.sql` |
| Валидация | `sql/dash/executive_v2_backend_validation.sql` |
| Откат | `sql/rollback/executive_v2_backend_2026-09-16/` (сначала два `R_*_BEFORE.sql`, затем `R_DROP_NEW_VIEWS.sql`) |

## 1. Объекты

| Объект | Тип изменения | Грейн |
|---|---|---|
| `wb_mart.V_WB_FINANCE_PRICE_COMPONENTS` | новый | строка финотчёта «Продажа/Возврат» (`finance_row_key`) |
| `wb_mart.V_DASH_EXECUTIVE_BREAKDOWN_DAILY` | новый | `day`, 1:1 к `V_DASH_KPI_DAILY` |
| `wb_mart.V_DASH_BUYOUT_COHORT_DAILY` | новый | `cohort_date` (дата заказа) |
| `wb_mart.V_DASH_SETTLEMENT_DAILY` | + колонки | `day` |
| `wb_mart.V_DASH_EXECUTIVE_ECONOMICS_DAILY` | + колонки | `day` |

**Регрессия:** 742 дня × 20 контрольных колонок (выручка, сбор, логистика, реклама, уровень счёта, результат до и после себестоимости, себестоимость, выплата и её мост) до и после миграции — **0 расхождений**.

---

## 2. GAP-01 — НДС на вознаграждение WB и вознаграждение ПВЗ

**Источник.** Сырые поля отчёта реализации WB (Finance API): `vwNds` и `ppvzReward`, парсинг в `V_WB_FINANCE_PRICE_COMPONENTS`.

**Тождество строки** (1 081 строка API-слоя, |остаток| ≤ 0,01 ₽ — округление `vw`, которое приходит с 18 знаками):
```
retailPriceWithDisc − forPay = СПП (retailPriceWithDisc − retailAmount) + vw + vwNds + acquiringFee + ppvzReward
```

- **Знак.** Сырой. В продаже поля положительны. У строки «Возврат» (1 шт., 21.07.2026) WB отдаёт те же поля с тем же знаком. Существующие `marketplace_fee_rub` и `settlement_goods_rub` суммируют их так же; направление не переворачивается (см. §9, KI-2).
- **Двойной счёт.** Компоненты раскладывают `marketplace_fee_rub` (управленческий слой) и `settlement_goods_rub` (settlement) и отдельным расходом не являются. Вычитать их поверх итога нельзя.
- **Покрытие.** LEGACY-импорт (до 13.07.2026) не содержит ключей `vwNds` и `ppvzReward`: 41 ключ JSON на 38 700 строках. Там компоненты = NULL, `fee_components_covered = FALSE`. Остатком они **не** вычисляются.

## 3. GAP-02 — разложение логистики

**Источник.** `V_WB_FINANCE_AMOUNTS_LONG_MAPPED` (`cost_category = 'logistics'`, SKU-строки внутри universe витрины — ровно популяция `logistics_rub`) × `bonusTypeName` из `V_WB_FINANCE_SEMANTIC`.

| Категория (взаимоисключающие) | Правило |
|---|---|
| `logistics_sold_rub` | «К клиенту при продаже» |
| `logistics_cancel_to_customer_rub` | «К клиенту при отмене» |
| `logistics_cancel_from_customer_rub` | «От клиента при отмене» |
| `logistics_cancellation_rub` | сумма двух предыдущих (агрегат для экрана) |
| `logistics_customer_return_rub` | «От клиента при возврате» |
| `logistics_defect_return_to_seller_rub` | «Возврат брака (К продавцу)» |
| `logistics_correction_rub` | операция «Коррекция логистики» |
| `logistics_unlabeled_rub` | метки нет — весь LEGACY-слой до 13.07.2026 |
| `logistics_other_label_rub` | новая метка WB. Ожидается 0, иначе валидация падает (EV2-B2). |

- Сумма категорий = `logistics_rub` на всей истории (EV2-B1).
- Возмещения (MEMO) и `ppvzReward` не входят: у них другая категория и другое поле.
- **Почему LEGACY не размечается.** В LEGACY нет ни `bonusTypeName`, ни `delivery_amount/return_amount`. Эвристика «две строки логистики на srid = отмена» из аудита UNITKA — вывод, а не метка; в канон она не взята.
- `logistics_legs_covered = FALSE` на сутках с неразмеченной логистикой.

## 4. GAP-03 — процент выкупа (когортный) — CLOSED

**Доказательство связи** (2026-09-16):
- 4 714 заказов, `order_srid` уникален, `quantity = 1` у всех;
- исход связывается по srid с продажей из `V_WB_SALES_RETURNS`;
- когорты 04–07.2026: 3 863 заказа, неразрешённых 0, конфликтов (отмена и продажа одновременно) 0, мульти-продаж 0;
- продаж без заказа внутри покрытия заказов — 2 (0,04 %) плюс продажи текущих суток. 173 апрельские продажи без заказа относятся к заказам до начала покрытия (13.04.2026) — это доказано по `order_dt` финотчёта.

**Контракт:**

| Элемент | Определение |
|---|---|
| Дата когорты | `FACT_ORDERS.order_date` (сутки МСК) |
| Числитель | `buyout_orders` — у заказа есть продажа (не возврат) с тем же srid |
| Знаменатель | `resolved_orders` = `buyout_orders + cancelled_orders` |
| Процент | `SUM(buyout_orders) / SUM(resolved_orders)` по выбранным когортам (ratio-of-sums) |
| Отмена | `FACT_ORDERS.is_cancel` и продажи нет → `cancelled_orders`, в знаменателе |
| Возврат | выкуп остаётся выкупом (gross); `returned_after_buyout_orders` — отдельно. Net-вариант не канонизирован: окно возврата само цензурировано справа. |
| Исход неизвестен | `unresolved_orders`, в знаменатель **не** входит |
| Конфликт | `conflict_orders`, в знаменатель не входит, когорта не финальна (на истории 0) |
| Зрелость | `cohort_is_final` = 0 неразрешённых и 0 конфликтов, сутки не текущие. Порог в днях **не вводится**. Период финален, если все его когорты финальны; иначе PROVISIONAL. |

Наблюдаемые лаги исхода: продажа — медиана 3–5 дней, максимум 33; отмена — максимум 46 дней. Для отображения это не порог, а справка.

**Оговорка.** Процент по известным исходам на свежих когортах смещён: быстрые исходы известны раньше медленных. Поэтому статус PROVISIONAL обязателен к показу, а `unresolved_orders` — к раскрытию.

## 5. GAP-04 — удержания после реализации (settlement)

Колонки `post_sale_*_rub` в `V_DASH_SETTLEMENT_DAILY`. Знак: «+» уменьшает выплату, «−» — кредит WB. Опубликованы только на закрытых сутках (`settlement_eligible`).

| Колонка | Источник |
|---|---|
| `post_sale_logistics_rub` | `logistics_amount` всех строк |
| `post_sale_storage_rub` | `storage_fee` (включая «Коррекция хранения») |
| `post_sale_acceptance_rub` | `acceptance` |
| `post_sale_penalty_rub` | `penalty` |
| `post_sale_wb_promotion_documents_rub` | «Удержание» класса AD_BILLING — **документы** WB Продвижение, не биллинг P&L |
| `post_sale_tariff_option_minimum_payment_rub` | MINIMUM_PAYMENT_ADJUSTMENT |
| `post_sale_utilization_rub` | UTILIZATION |
| `post_sale_transit_rub` | TRANSIT_DEDUCTION |
| `post_sale_force_majeure_rub` | FORCE_MAJEURE_PAYMENT (кредит, «−») |
| `post_sale_review_points_refund_rub` | REVIEW_POINTS_ADVANCE_REFUND (кредит, «−») |
| `post_sale_unclassified_deductions_rub` | прочие классы «Удержание» (UNCLASSIFIED/UNKNOWN/CONFLICT) |
| `post_sale_loyalty_program_rub` | `additional_payment` «Стоимость участия в программе лояльности» |
| `post_sale_loyalty_points_rub` | `loyalty_points_rub` (cashbackAmount) |
| `post_sale_other_operations_rub` | `deduction + additional_payment` прочих операций (ожидается 0) |
| `post_sale_deductions_rub` | итог = −`post_realization_deductions_rub` |

Тождество `settlement_goods_rub − post_sale_deductions_rub = wb_payout_rub` выполняется на всех закрытых сутках истории (EV2-C1). Документы рекламы равны `ad_billing_reconstructed_rub` corrected-слоя (EV2-C3). Возмещения WB (MEMO) денежного потока не несут, поэтому строк в разложении нет.

**Цепочка цены (GAP-01, settlement-популяция)** — колонки `settlement_*`:
```
settlement_seller_price_rub − settlement_spp_rub − settlement_wb_remuneration_rub − settlement_wb_remuneration_vat_rub
 − settlement_acquiring_rub − settlement_pvz_reward_rub − settlement_price_chain_rounding_rub
 + settlement_goods_other_operations_rub = settlement_goods_rub          (EV2-C2)
settlement_seller_price_rub − settlement_spp_rub = settlement_buyer_paid_rub
```

## 6. GAP-05 — СПП

| Поле | Слой | База даты | Контроль 31.08–13.09 |
|---|---|---|---|
| **`spp_rub`** (канон Executive) | `V_DASH_EXECUTIVE_BREAKDOWN_DAILY` | дата продажи, Statistics API | **27 074,54** = 139 439,61 − 112 365,07 |
| `fee_spp_rub` | там же, внутри разложения сбора | дата финотчёта, SKU universe | 27 074,52 |
| `settlement_spp_rub` | `V_DASH_SETTLEMENT_DAILY` | дата финотчёта, все строки, закрытые сутки | 27 074,52 |

- **Разница 0,02 ₽** — копеечное округление источников: на 10 из 195 продаж Statistics API отдаёт `priceWithDisc`/`finishedPrice` на 0,01 выше, чем `retailPriceWithDisc`/`retailAmount` финотчёта (по srid).
- **Знак.** СПП построчно бывает отрицательной (68 строк истории, покупатель заплатил больше цены продавца), сумма нетто.
- **Когда какое поле.** На мосту «Выручка → Оплачено покупателями» использовать `spp_rub`, в разложении «Удержано WB из цены» — `fee_spp_rub`.

## 7. Статус данных Executive

Колонки в `V_DASH_EXECUTIVE_ECONOMICS_DAILY`.

| Колонка | Смысл (1 = да) |
|---|---|
| `exec_missing_sales_day`, `exec_missing_ads_attribution_day`, `exec_missing_finance_day`, `exec_missing_ads_billing_day`, `exec_missing_cogs_day` | нет покрытия источника |
| `exec_current_day` | текущие сутки |
| `exec_provisional_finance_day` | финансы суток из DAILY-слоя (не закрыты неделей WB) |
| `exec_provisional_ads_billing_day` | биллинг загружен, но не прошёл SLA стабильности (15 сут × 3 чтения — в SQL) |
| `executive_incomplete_day` / `executive_provisional_day` / `executive_final_day` | ровно один = 1 на сутки |
| `executive_data_status` | `INCOMPLETE` / `PROVISIONAL` / `FINAL` |

**Статус периода для Metabase:** `INCOMPLETE`, если `SUM(executive_incomplete_day) > 0`; иначе `PROVISIONAL`, если `SUM(executive_provisional_day) > 0`; иначе `FINAL`.

Это статус управленческого P&L. Для блока «Расчёты с WB» действует свой гейт — `settlement_eligible_day` / `settlement_open_day`. Для процента выкупа — `cohort_is_final`.

## 8. Контрольные значения

Полная история (разложения доступны только на API-слое): покрытие сбора — 68 суток; неразмеченная логистика LEGACY — 2 666 414,53 ₽; статус по суткам — 142 FINAL / 13 PROVISIONAL / 587 INCOMPLETE; выплата 11 009 085,98 и результат 327 296,48 без изменений.

| Метрика | 31.08–13.09 | 27.07–23.08 |
|---|---:|---:|
| `wb_remuneration_vat_rub` | 5 222,23 | 12 628,01 |
| `pvz_reward_rub` | 2 878,19 | 7 361,74 |
| `wb_remuneration_rub` | 23 737,46 | 57 400,11 |
| `acquiring_rub` | 4 322,46 | 11 248,77 |
| `fee_spp_rub` / `spp_rub` | 27 074,52 / 27 074,54 | 76 044,96 / 75 974,89 (разные базы дат: финотчёт и продажа) |
| `marketplace_fee_rub` (без изменений) | 63 234,88 | 164 683,60 |
| `logistics_sold_rub` | 11 863,33 | 30 912,94 |
| `logistics_cancellation_rub` (к клиенту 1 214,10 + от клиента 606,33) | 1 820,43 | 6 616,36 |
| `logistics_rub` (без изменений) | 13 683,76 | 37 529,30 |
| `post_sale_deductions_rub` | 38 738,89 = 13 683,76 + 1 817,13 + 20 818 + 2 382 + 38 | 88 373,44 = официальная сверка WB |
| `wb_payout_rub` (без изменений) | 37 465,76 | 110 101,96 |
| Процент выкупа (когорты) | 91,82 % (146 / 159), PROVISIONAL: 40 заказов без исхода, финальны 2 из 14 когорт | 89,19 % (528 / 592), 2 без исхода, финальны 26 из 28 |
| Статус Executive | PROVISIONAL (2 FINAL + 12 PROVISIONAL по биллингу) | FINAL (28 из 28) |
| Результат после себестоимости (без изменений) | −5 483,68 | −22 870,41 |

## 9. Известные проблемы (не исправлены: итоги не меняются по условию задачи)

- **KI-2026-09-16-1** (из FIN CONTRACT V2): продажа 09.07.2026 с `sku_match_status = not_found`, +638,72 ₽ к результату суток.
- **KI-2026-09-16-2.** Строка «Возврат» 21.07.2026 (srid `eAL.r0beccb16df984929944be5399989c01b.0.0`): WB отдаёт `forPay` 547,44 положительным.
  - `settlement_goods_rub` суммирует её как поступление, `marketplace_fee_rub` — как сбор (+292,56).
  - Если это сторно продажи, выплата 21.07 завышена на 2 × 547,44 ₽.
  - Официальная сверка выплаты этот день не покрывала. Нужна сверка с отчётом WB за неделю 20–26.07.
- **KI-2026-09-16-3.** Логистика SKU вне активного universe (88 строк LEGACY, 14 960 ₽, 08.04–07.05.2026) не входит ни в `logistics_rub`, ни в уровень счёта.
  - В результат периода из них попадают только одни сутки на 1 360 ₽ (остальные — до начала покрытия 13.04).
  - Величина видна в `V_DASH_KPI_DAILY.sku_costs_outside_universe_rub`.

---

## 10. EXECUTIVE V2 HANDOFF TO COWORK

Общие правила:
- все суммы — `SUM` по суткам выбранного периода;
- проценты — ratio-of-sums;
- NULL = неизвестно (не рисовать как 0);
- фильтр периода — по `day` (для когорт — по `cohort_date`).

| Metric | Canonical source | Field | Definition | Sign | Control 31.08–13.09 | Finality / coverage | Notes |
|---|---|---|---|---|---:|---|---|
| СПП | `V_DASH_EXECUTIVE_BREAKDOWN_DAILY` | `spp_rub` | цена продавца − оплачено покупателями, дата продажи | + скидка, построчно бывает − | 27 074,54 | `sales_covered` | точно = карточка 44 − карточка 77 |
| Вознаграждение WB | `V_DASH_EXECUTIVE_BREAKDOWN_DAILY` | `wb_remuneration_rub` | `vw` отчёта WB, без НДС | + | 23 737,46 | `fee_components_covered` | часть «Удержано WB из цены» |
| НДС на вознаграждение WB | `V_DASH_EXECUTIVE_BREAKDOWN_DAILY` | `wb_remuneration_vat_rub` | `vwNds` отчёта WB | + | 5 222,23 | `fee_components_covered` | NULL до 13.07.2026 |
| Эквайринг | `V_DASH_EXECUTIVE_BREAKDOWN_DAILY` | `acquiring_rub` | `acquiringFee` | + | 4 322,46 | `fee_components_covered` | |
| Вознаграждение ПВЗ | `V_DASH_EXECUTIVE_BREAKDOWN_DAILY` | `pvz_reward_rub` | `ppvzReward` | + | 2 878,19 | `fee_components_covered` | NULL до 13.07.2026 |
| СПП внутри сбора | `V_DASH_EXECUTIVE_BREAKDOWN_DAILY` | `fee_spp_rub` | `retailPriceWithDisc − retailAmount`, дата финотчёта | ± | 27 074,52 | `fee_components_covered` | для разложения сбора; сумма 5 компонентов + `fee_components_rounding_rub` = `marketplace_fee_rub` |
| Логистика — выкупы | `V_DASH_EXECUTIVE_BREAKDOWN_DAILY` | `logistics_sold_rub` | «К клиенту при продаже» | + | 11 863,33 | `logistics_legs_covered` | |
| Логистика — отмены | `V_DASH_EXECUTIVE_BREAKDOWN_DAILY` | `logistics_cancellation_rub` | к клиенту + от клиента при отмене | + | 1 820,43 | `logistics_legs_covered` | детали: `logistics_cancel_to_customer_rub` 1 214,10, `logistics_cancel_from_customer_rub` 606,33 |
| Логистика — прочие плечи | `V_DASH_EXECUTIVE_BREAKDOWN_DAILY` | `logistics_customer_return_rub`, `logistics_defect_return_to_seller_rub`, `logistics_correction_rub`, `logistics_unlabeled_rub` | см. §3 | + | 0 | `logistics_legs_covered` | сумма всех плеч = `logistics_rub` 13 683,76 |
| Процент выкупа | `V_DASH_BUYOUT_COHORT_DAILY` | `SUM(buyout_orders) / SUM(resolved_orders)` | когорта даты заказа, только известные исходы | — | 91,82 % (146 / 159) | `cohort_is_final`; период финален, если `SUM(cohort_provisional_day) = 0` | показывать `SUM(unresolved_orders)` (40) и статус |
| Удержания после реализации — итог | `V_DASH_SETTLEMENT_DAILY` | `post_sale_deductions_rub` | всё, что WB удержал из «к перечислению за товар» | + удержание | 38 738,89 | `settlement_eligible_day` | = −`post_realization_deductions_rub` |
| — реклама (документы WB) | `V_DASH_SETTLEMENT_DAILY` | `post_sale_wb_promotion_documents_rub` | «Оказание услуг WB Продвижение» | + | 20 818,00 | `settlement_eligible_day` | НЕ смешивать с P&L-рекламой 20 750 |
| — логистика | `V_DASH_SETTLEMENT_DAILY` | `post_sale_logistics_rub` | все строки логистики | + | 13 683,76 | `settlement_eligible_day` | |
| — хранение | `V_DASH_SETTLEMENT_DAILY` | `post_sale_storage_rub` | | + | 1 817,13 | `settlement_eligible_day` | |
| — минимальный платёж по тарифной опции | `V_DASH_SETTLEMENT_DAILY` | `post_sale_tariff_option_minimum_payment_rub` | «Остаток по минимальному платежу» | + | 2 382,00 | `settlement_eligible_day` | |
| — утилизация | `V_DASH_SETTLEMENT_DAILY` | `post_sale_utilization_rub` | | + | 38,00 | `settlement_eligible_day` | |
| — прочие статьи | `V_DASH_SETTLEMENT_DAILY` | `post_sale_acceptance_rub`, `post_sale_penalty_rub`, `post_sale_transit_rub`, `post_sale_force_majeure_rub`, `post_sale_review_points_refund_rub`, `post_sale_unclassified_deductions_rub`, `post_sale_loyalty_program_rub`, `post_sale_loyalty_points_rub`, `post_sale_other_operations_rub` | см. §5 | − у кредитов (форс-мажор, баллы) | 0 | `settlement_eligible_day` | не скрывать отрицательные значения |
| Цена продавца (settlement) | `V_DASH_SETTLEMENT_DAILY` | `settlement_seller_price_rub` | Σ `retailPriceWithDisc` | + | 139 439,53 | `settlement_price_chain_covered` | цепочка до `settlement_goods_rub` 76 204,65 |
| Статус данных Executive | `V_DASH_EXECUTIVE_ECONOMICS_DAILY` | `executive_incomplete_day`, `executive_provisional_day`, `executive_final_day` | INCOMPLETE если Σincomplete > 0; иначе PROVISIONAL если Σprovisional > 0; иначе FINAL | — | PROVISIONAL (2 / 12 / 0) | причины: `exec_missing_*_day`, `exec_provisional_finance_day`, `exec_provisional_ads_billing_day` | заменяет логику карточки 41 «Качество периода» (там только финансы) |
