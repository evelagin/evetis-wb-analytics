#!/usr/bin/env python3
"""
EVETIS · WB Executive V2 — Phase B · сборка Metabase dashboard 2.

Спецификация: docs/EXECUTIVE_V2_PHASE_B_IMPLEMENTATION_2026-09-16.md
Backend:      docs/EXECUTIVE_V2_BACKEND_2026-09-16.md (канонические поля; здесь только presentation)
Откат:        tools/metabase_exec_v2_rollback.py (восстанавливает раскладку из снимка «до»)

Идемпотентно. Карточки V2 ищутся по имени (префикс «Executive V2 · ») в коллекции 6
и обновляются на месте; отсутствующие создаются. Старые карточки НЕ удаляются и НЕ
меняются — только снимаются с раскладки dashboard 2.

Знак расходов — presentation convention: в SQL карточки выводится −SUM(...). Backend-
поля остаются положительными.

Использование:
  tools/metabase_exec_v2_build.py --check-sql     # только BigQuery: SQL карточек за контрольный период
  tools/metabase_exec_v2_build.py --cards         # создать/обновить карточки V2
  tools/metabase_exec_v2_build.py --layout        # переложить dashboard 2 (whole-array replace)
  tools/metabase_exec_v2_build.py --all           # cards + layout

Профиль mb: $MB_PROFILE или evetis-dev. Секретов в скрипте нет.
"""
import json, os, subprocess, sys, uuid, urllib.request

PROFILE = os.environ.get("MB_PROFILE", "evetis-dev")
COLL, DB, DASH = 6, 2, 2
PREFIX = "Executive V2 · "
KPI_DAY_FIELD = 909          # wb_mart.V_DASH_KPI_DAILY.day — дата всех карточек V2
BQ_PROJECT = "project-fa311fc0-4d87-4781-986"

KPI = "`wb_mart.V_DASH_KPI_DAILY`"
ECON = "`wb_mart.V_DASH_EXECUTIVE_ECONOMICS_DAILY`"
CORR = "`wb_mart.V_DASH_FINANCE_CORRECTED_DAILY`"
BRK = "`wb_mart.V_DASH_EXECUTIVE_BREAKDOWN_DAILY`"
SETL = "`wb_mart.V_DASH_SETTLEMENT_DAILY`"
COH = "`wb_mart.V_DASH_BUYOUT_COHORT_DAILY`"
FRESH = "`wb_mart.V_DASH_FRESHNESS_HEADER`"

PERIOD = f"d AS (SELECT day FROM {KPI} WHERE {{{{day}}}})"


# ── форматирование строк в SQL (presentation) ────────────────────────────────
def rub(x):
    """-12 345 ₽ — тот же вид, что у числовых карточек Metabase (дефис, пробел в группах)."""
    return (f"CONCAT(IF(ROUND({x}) < 0, '-', ''), "
            f"REPLACE(FORMAT(\"%'d\", ABS(CAST(ROUND({x}) AS INT64))), ',', ' '), ' ₽')")


def dec(x, n):
    return f"REPLACE(FORMAT('%.{n}f', {x}), '.', ',')"


def window_cte(cov_expr, value_expr, joins):
    """Окно выбранного периода и предыдущего окна той же длины (шаблон карточки 42)."""
    return f"""WITH b AS (
  SELECT MIN(day) AS d1, MAX(day) AS d2, COUNT(*) AS len
  FROM {KPI}
  WHERE {{{{day}}}}
),
agg AS (
  SELECT
    IF(k.day >= b.d1, 'cur', 'prv')                             AS w,
    IF(k.day >= b.d1, b.d1, DATE_SUB(b.d1, INTERVAL b.len DAY)) AS period_start,
    COUNTIF({cov_expr})                                         AS cov,
    {value_expr}                                                AS v
  FROM {KPI} k
  {joins}, b
  WHERE k.day BETWEEN DATE_SUB(b.d1, INTERVAL b.len DAY) AND b.d2
  GROUP BY w, period_start
)"""


def smart_sql(col, cov_expr, value_expr, joins, sign_guard=False):
    guard = ("\n  AND SIGN(v) = (SELECT SIGN(v) FROM agg WHERE w = 'cur')\n  AND v <> 0" if sign_guard else "")
    return (window_cte(cov_expr, value_expr, joins) +
            f"\nSELECT period_start AS period, v AS {col}\nFROM agg\nWHERE w = 'cur'\n"
            f"   OR (cov = (SELECT cov FROM agg WHERE w = 'cur'){guard})\nORDER BY period")


def money_cs(*cols, decimals=0):
    return {"column_settings": {json.dumps(["name", c], ensure_ascii=False): {
        "number_style": "decimal", "decimals": decimals, "suffix": " ₽"} for c in cols}}


def obj_viz(pairs):
    return {"table.columns": [{"name": c, "enabled": True} for c, _ in pairs],
            "column_settings": {json.dumps(["name", c], ensure_ascii=False): {"column_title": title} for c, title in pairs}}


def smart_viz(col, flip=False):
    v = {"scalar.field": col, "scalar.comparisons": [{"id": "prev", "type": "previousValue"}],
         "scalar.switch_positive_negative": flip}
    v.update(money_cs(col))
    return v


EL = "e.period_result_eligible"
J_E = f"JOIN {ECON} e USING (day)"
J_EC = f"JOIN {ECON} e USING (day)\n  JOIN {CORR} c USING (day)"
J_EX = f"JOIN {ECON} e USING (day)\n  JOIN {BRK} x USING (day)"

# ── Карточки V2 ──────────────────────────────────────────────────────────────
CARDS = {}


def card(key, name, display, sql, viz, description):
    CARDS[key] = dict(name=PREFIX + name, display=display, sql=sql, viz=viz, description=description)


# ROW 0 · статус данных
card("status", "Статус данных", "scalar", f"""-- Статус данных Executive: V_DASH_EXECUTIVE_ECONOMICS_DAILY.executive_*_day
WITH {PERIOD}
SELECT CASE
  WHEN SUM(e.executive_incomplete_day) > 0  THEN '🔴 НЕПОЛНЫЕ ДАННЫЕ'
  WHEN SUM(e.executive_provisional_day) > 0 THEN '🟡 ПРЕДВАРИТЕЛЬНЫЕ ДАННЫЕ'
  ELSE '🟢 ОКОНЧАТЕЛЬНЫЕ ДАННЫЕ' END AS data_status
FROM d JOIN {ECON} e USING (day)""", {"scalar.field": "data_status"},
     "Окончательные — все источники прибыли за период закрыты. Предварительные — данные есть и уже в расчёте, "
     "но часть ещё может уточниться: рекламный биллинг WB стабилизируется до 15 дней, недельный финотчёт WB "
     "закрывается после окончания недели. Неполные — за часть дней нет продаж, финансов, рекламы или себестоимости.")

card("status_detail", "Статус данных: сутки", "object", f"""-- Счётчики суток: executive_provisional_day / executive_final_day / executive_incomplete_day
WITH {PERIOD}
SELECT
  SUM(e.executive_provisional_day)       AS provisional_days,
  SUM(e.executive_final_day)             AS final_days,
  SUM(e.executive_incomplete_day)        AS incomplete_days
FROM d JOIN {ECON} e USING (day)""", obj_viz([("provisional_days", "Предварительные сутки"), ("final_days", "Окончательные сутки"),
                                      ("incomplete_days", "Неполные сутки")]),
     "Сколько суток выбранного периода окончательные, предварительные и неполные. Предварительные сутки уже входят "
     "в прибыль, но их цифры ещё могут измениться: рекламный биллинг WB стабилизируется до 15 дней.")

card("freshness", "Свежесть данных", "object", f"""-- V_DASH_FRESHNESS_HEADER (не зависит от периода)
SELECT FORMAT_DATE('%d.%m.%Y', data_as_of_min) AS data_as_of,
       FORMAT_TIMESTAMP('%d.%m %H:%M МСК', mart_built_at, 'Europe/Moscow') AS mart_built
FROM {FRESH}""", obj_viz([("data_as_of", "Данные по"), ("mart_built", "Витрина собрана")]),
     "Дата, по которую загружены данные, и время сборки витрины. От выбранного периода не зависит.")

# ROW 1 · продажи
card("buyer_paid", "Оплачено покупателями", "smartscalar",
     smart_sql("buyer_paid_rub", "k.sales_covered", "SUM(k.sales_revenue_buyer_paid_rub)", ""),
     smart_viz("buyer_paid_rub"),
     "Сумма, которую заплатили покупатели за выкупленные товары: цена продавца минус СПП. Дата — дата выкупа. "
     "Сравнение — с предыдущим периодом той же длины.")

card("cohort_rate", "Выкуп по когортам", "scalar", f"""-- V_DASH_BUYOUT_COHORT_DAILY: SUM(buyout_orders) / SUM(resolved_orders), когорта = дата заказа
WITH {PERIOD}
SELECT CONCAT(IF(SUM(c.cohort_provisional_day) > 0 OR SUM(c.unresolved_orders) > 0, '🟡 ', '🟢 '),
              {dec("SAFE_DIVIDE(SUM(c.buyout_orders), SUM(c.resolved_orders)) * 100", 2)}, '%') AS buyout_rate
FROM d JOIN {COH} c ON c.cohort_date = d.day""",
     {"scalar.field": "buyout_rate"},
     "Доля выкупленных среди заказов периода с уже известным исходом (выкуп или отмена). Заказы берутся по дате "
     "заказа. Заказы без исхода в расчёт не входят и показаны ниже — пока они есть, процент предварительный (🟡); "
     "когда все заказы периода завершены — окончательный (🟢). "
     "Это не «выкупы ÷ заказы» за период: у них разные даты событий.")

# ROW 2 · от цены продавца до начисления WB (settlement chain, закрытые финотчёты)
CHAIN_COLS = ["seller_price_rub", "spp_rub", "wb_remuneration_rub", "wb_remuneration_vat_rub",
              "acquiring_rub", "pvz_reward_rub", "goods_payable_rub"]
card("chain", "От цены продавца до начисления WB", "scalar", f"""-- V_DASH_SETTLEMENT_DAILY: цепочка settlement_* (GAP-01/05), закрытые сутки финотчётов WB.
-- Вычеты выводятся со знаком «−» (presentation). Backend-поля положительные.
WITH {PERIOD}
SELECT
   SUM(s.settlement_seller_price_rub)         AS seller_price_rub,
  -SUM(s.settlement_spp_rub)                  AS spp_rub,
  -SUM(s.settlement_wb_remuneration_rub)      AS wb_remuneration_rub,
  -SUM(s.settlement_wb_remuneration_vat_rub)  AS wb_remuneration_vat_rub,
  -SUM(s.settlement_acquiring_rub)            AS acquiring_rub,
  -SUM(s.settlement_pvz_reward_rub)           AS pvz_reward_rub,
   SUM(s.settlement_goods_rub)                AS goods_payable_rub
FROM d JOIN {SETL} s USING (day)""",
     dict({"scalar.field": "goods_payable_rub"}, **money_cs(*CHAIN_COLS)),
     "Цепочка по закрытым финансовым отчётам WB: цена продавца минус СПП, вознаграждение WB, НДС, эквайринг и "
     "вознаграждение ПВЗ даёт сумму к перечислению за товар. Слагаемые округлены до рубля, поэтому их сумма может "
     "отличаться от итога на 1–2 ₽.")

# ROW 3 · операционные расходы
card("ads", "Реклама", "smartscalar",
     smart_sql("ad_spend_rub", "c.ads_billing_covered", "-SUM(c.ad_spend_financial_rub)", f"JOIN {CORR} c USING (day)"),
     smart_viz("ad_spend_rub"),
     "Расходы на рекламу WB по дате оказания услуги (биллинг WB). Именно эта сумма уменьшает прибыль. Сумма "
     "рекламы в расчётах с WB может отличаться: там она учитывается по датам финансовых документов. "
     "Сравнение — с предыдущим периодом той же длины.")

card("logistics", "Логистика", "smartscalar",
     smart_sql("logistics_rub", EL, f"-SUM(IF({EL}, k.logistics_rub, NULL))", J_E),
     smart_viz("logistics_rub"),
     "Логистика WB по выкупленным товарам, отменам и возвратам. Вознаграждение ПВЗ и технические возмещения WB "
     "сюда не входят. Сравнение — с предыдущим периодом той же длины.")

card("storage", "Хранение", "smartscalar",
     smart_sql("storage_rub", EL, f"-SUM(IF({EL}, k.storage_rub, NULL))", J_E),
     smart_viz("storage_rub"),
     "Платное хранение товара на складах WB. Сравнение — с предыдущим периодом той же длины.")

card("tariff", "Тарифные опции WB", "scalar", f"""-- V_DASH_FINANCE_CORRECTED_DAILY.tariff_option_minimum_payment_rub
WITH {PERIOD}
SELECT -SUM(IF(c.period_result_eligible, c.tariff_option_minimum_payment_rub, NULL)) AS tariff_option_rub
FROM d JOIN {CORR} c USING (day)""", dict({"scalar.field": "tariff_option_rub"}, **money_cs("tariff_option_rub")),
     "Доплата до минимального платежа по подключённым опциям Конструктора тарифов WB. Удерживается WB раз в "
     "расчётный период, если комиссия по опциям меньше минимального платежа.")

card("other_wb", "Прочие расходы WB", "scalar", f"""-- account_level_total_corrected_rub − storage_rub − tariff_option_minimum_payment_rub
-- (утилизация, транзит, штрафы, платная приёмка, прочие удержания; возмещения WB = 0)
WITH {PERIOD}
SELECT -SUM(IF(c.period_result_eligible,
               c.account_level_total_corrected_rub - c.storage_rub - c.tariff_option_minimum_payment_rub, NULL)) AS other_wb_rub
FROM d JOIN {CORR} c USING (day)""", dict({"scalar.field": "other_wb_rub"}, **money_cs("other_wb_rub")),
     "Остальные расходы уровня кабинета WB: утилизация, транзит поставок, штрафы, платная приёмка и прочие "
     "удержания. Реклама, хранение и тарифные опции показаны отдельно; технические возмещения WB расходом не являются.")

# ROW 4 · финансовый результат
card("cogs", "Себестоимость проданных товаров", "smartscalar",
     smart_sql("product_cogs_rub", "e.after_product_cogs_eligible",
               "-SUM(IF(e.after_product_cogs_eligible, e.product_cogs_rub, NULL))", J_E),
     smart_viz("product_cogs_rub"),
     "Себестоимость выкупленных единиц по управленческой цене, действующей на дату продажи: закупка, доставка, "
     "таможня. Не FIFO. Сравнение — с предыдущим периодом той же длины.")

card("profit", "Прибыль после себестоимости", "scalar", f"""-- V_DASH_EXECUTIVE_ECONOMICS_DAILY.period_result_after_product_cogs_rub
WITH {PERIOD}
SELECT SUM(e.period_result_after_product_cogs_rub) AS profit_after_cogs_rub
FROM d JOIN {ECON} e USING (day)""",
     dict({"scalar.field": "profit_after_cogs_rub",
           "scalar.segments": [{"min": -1000000000, "max": 0, "color": "#ED6E6E", "label": "Убыток"},
                               {"min": 0, "max": 1000000000, "color": "#84BB4C", "label": "Прибыль"}]},
          **money_cs("profit_after_cogs_rub")),
     "Результат до себестоимости минус себестоимость проданных товаров. Налоги, OPEX и фулфилмент не вычтены — "
     "это не чистая прибыль.")

# ROW 5 · расчёты с WB
PSD = [("post_sale_wb_promotion_documents_rub", "Реклама по документам WB"),
       ("post_sale_logistics_rub", "Логистика"),
       ("post_sale_storage_rub", "Хранение"),
       ("post_sale_tariff_option_minimum_payment_rub", "Тарифные опции"),
       ("post_sale_utilization_rub", "Утилизация"),
       ("post_sale_transit_rub", "Транзит поставок"),
       ("post_sale_penalty_rub", "Штрафы"),
       ("post_sale_acceptance_rub", "Платная приёмка"),
       ("post_sale_force_majeure_rub", "Компенсация WB (форс-мажор)"),
       ("post_sale_review_points_refund_rub", "Возврат аванса «Баллы за отзывы»"),
       ("post_sale_unclassified_deductions_rub", "Прочие удержания WB"),
       ("post_sale_loyalty_program_rub", "Программа лояльности"),
       ("post_sale_loyalty_points_rub", "Баллы лояльности"),
       ("post_sale_other_operations_rub", "Прочие операции")]
psd_rows = ",\n  ".join(f"STRUCT({i} AS ord, '{label}' AS item, -SUM(s.{col}) AS amount_rub)"
                         for i, (col, label) in enumerate(PSD, 1))
card("psd_breakdown", "Удержания после реализации: состав", "table", f"""-- V_DASH_SETTLEMENT_DAILY.post_sale_* (GAP-04). Реклама — документы WB, не биллинг P&L.
-- «−» уменьшает выплату, «+» — кредит WB. Нулевые статьи не выводятся.
WITH {PERIOD},
x AS (
  SELECT [
  {psd_rows}
  ] AS items
  FROM d JOIN {SETL} s USING (day)
)
SELECT it.item AS item, it.amount_rub AS amount_rub
FROM x, UNNEST(x.items) it
WHERE ROUND(it.amount_rub, 2) <> 0
ORDER BY it.ord""",
     {"table.columns": [{"name": "item", "enabled": True}, {"name": "amount_rub", "enabled": True}],
      "column_settings": {json.dumps(["name", "item"]): {"column_title": "Статья"},
                          json.dumps(["name", "amount_rub"]): {"column_title": "Сумма", "number_style": "decimal",
                                                               "decimals": 0, "suffix": " ₽"}}},
     "Из чего состоят удержания WB после реализации за закрытые финансовые отчёты. «Реклама по документам WB» — "
     "сумма рекламных услуг, удержанная WB по закрытым финансовым документам. Может отличаться от рекламы периода "
     "в прибыли, которая учитывается по дате оказания услуги.")




card("result_status", "Статус результата", "scalar", f"""-- Статус результата: V_DASH_EXECUTIVE_ECONOMICS_DAILY.executive_*_day (тот же контракт, что «Статус данных»)
WITH {PERIOD}
SELECT CASE
  WHEN SUM(e.executive_incomplete_day) > 0  THEN '🔴 Предварительный результат · есть неполные данные'
  WHEN SUM(e.executive_provisional_day) > 0 THEN '🟡 Предварительный результат'
  ELSE '🟢 Окончательный результат' END AS result_status
FROM d JOIN {ECON} e USING (day)""", {"scalar.field": "result_status"},
     "Статус финансового результата за период. Предварительный — значения уже в расчёте, но часть источников ещё "
     "может уточниться (рекламный биллинг WB, незакрытый недельный финотчёт). Есть неполные данные — за часть суток "
     "нет полной экономики, и они в результат не вошли. Значения не скрываются.")

# ── Вторичные карточки (object: «подпись — значение», без обрезки текста) ─────
card("cohort_detail", "Выкуп по когортам: состав", "object", f"""-- V_DASH_BUYOUT_COHORT_DAILY: resolved_orders, buyout_orders, unresolved_orders, cohort_provisional_day
WITH {PERIOD}
SELECT
  SUM(c.resolved_orders)   AS resolved_orders,
  SUM(c.buyout_orders)     AS buyout_orders,
  SUM(c.unresolved_orders) AS unresolved_orders
FROM d JOIN {COH} c ON c.cohort_date = d.day""",
     obj_viz([("resolved_orders", "Завершено"), ("buyout_orders", "Выкуплено"), ("unresolved_orders", "Ждут исхода")]),
     "Заказы периода по дате заказа: сколько уже завершены (выкуп или отмена), сколько из них выкуплено и сколько "
     "ещё ожидают исхода. Пока есть заказы без исхода, процент выкупа предварительный.")

card("chain_total", "От цены продавца до начисления WB: итог", "object", f"""-- settlement_seller_price_rub − settlement_goods_rub; settlement_price_chain_covered
WITH {PERIOD}
SELECT
  {rub("-(SUM(s.settlement_seller_price_rub) - SUM(s.settlement_goods_rub))")} AS withheld_from_price,
  CONCAT(CAST(COUNTIF(s.settlement_price_chain_covered) AS STRING), ' из ', CAST(COUNT(*) AS STRING), ' сут.') AS closed_days
FROM d LEFT JOIN {SETL} s USING (day)""",
     obj_viz([("withheld_from_price", "Удержано из цены"), ("closed_days", "Закрытые отчёты")]),
     "Всего удержано из цены продавца = СПП + вознаграждение WB + НДС + эквайринг + вознаграждение ПВЗ. СПП не является "
     "комиссией WB. Блок считается только по суткам, закрытым недельным финансовым отчётом WB.")

card("drr", "ДРР", "object", window_cte("c.ads_billing_covered",
     "SUM(IF(c.ads_billing_covered, c.ad_spend_financial_rub, NULL)) AS ads, "
     "SUM(IF(c.ads_billing_covered, c.sales_revenue_seller_base_rub, NULL))", f"JOIN {CORR} c USING (day)")
     .replace("AS v\n", "AS rev\n") + f"""
, r AS (
  SELECT MAX(IF(w = 'cur', SAFE_DIVIDE(ads, rev), NULL)) AS cur,
         MAX(IF(w = 'prv', SAFE_DIVIDE(ads, rev), NULL)) AS prv,
         MAX(IF(w = 'cur', cov, NULL)) = MAX(IF(w = 'prv', cov, NULL)) AS comparable
  FROM agg
)
SELECT
  CONCAT({dec("cur * 100", 1)}, '%') AS drr,
  CASE
    WHEN NOT comparable OR prv IS NULL OR cur IS NULL THEN 'нет сопоставимого периода'
    WHEN ROUND((cur - prv) * 100, 1) = 0 THEN 'без изменений'
    WHEN cur > prv THEN CONCAT('▲ ', {dec("(cur - prv) * 100", 1)}, ' п.п. (хуже)')
    ELSE CONCAT('▼ ', {dec("(prv - cur) * 100", 1)}, ' п.п. (лучше)')
  END AS drr_change
FROM r""", obj_viz([("drr", "ДРР"), ("drr_change", "К прошлому периоду")]),
     "ДРР = реклама по биллингу WB ÷ выручка по цене продавца. Изменение — в процентных пунктах к предыдущему периоду "
     "той же длины; снижение ДРР лучше.")

card("log_breakdown", "Логистика: состав", "object", f"""-- V_DASH_EXECUTIVE_BREAKDOWN_DAILY: logistics_sold_rub, logistics_cancellation_rub
--   (= logistics_cancel_to_customer_rub + logistics_cancel_from_customer_rub); logistics_rub / buyouts_qty
WITH {PERIOD},
s AS (
  SELECT
    SUM(IF(e.period_result_eligible, x.logistics_sold_rub, NULL))                 AS sold,
    SUM(IF(e.period_result_eligible, x.logistics_cancellation_rub, NULL))         AS cancel,
    SUM(IF(e.period_result_eligible, x.logistics_cancel_to_customer_rub, NULL))   AS to_c,
    SUM(IF(e.period_result_eligible, x.logistics_cancel_from_customer_rub, NULL)) AS from_c,
    SUM(IF(e.period_result_eligible, k.logistics_rub, NULL))                      AS total,
    SUM(IF(e.period_result_eligible, k.buyouts_qty, NULL))                        AS buyouts
  FROM d JOIN {ECON} e USING (day) JOIN {BRK} x USING (day) JOIN {KPI} k USING (day)
)
SELECT
  {rub("-sold")}   AS sold,
  {rub("-cancel")} AS cancellation,
  {rub("-to_c")}   AS cancel_to_customer,
  {rub("-from_c")} AS cancel_from_customer,
  CONCAT({dec("SAFE_DIVIDE(total, buyouts)", 2)}, ' ₽') AS per_buyout
FROM s""", obj_viz([("sold", "Выкупленные товары"), ("cancellation", "Отмены и обратная логистика"),
                   ("cancel_to_customer", "  к клиенту при отмене"), ("cancel_from_customer", "  от клиента при отмене"),
                   ("per_buyout", "Логистика на один выкуп")]),
     "Выкупленные товары — доставка к покупателю выкупленных товаров. Отмены и обратная логистика — доставка к клиенту "
     "при отмене и обратно от клиента. На один выкуп — вся логистика периода ÷ число выкупов.")

card("storage_per_day", "Хранение в день", "object", f"""-- SUM(storage_rub) / COUNTIF(period_result_eligible)
WITH {PERIOD}
SELECT CONCAT('≈', {dec("SAFE_DIVIDE(SUM(IF(e.period_result_eligible, k.storage_rub, NULL)), COUNTIF(e.period_result_eligible))", 2)},
              ' ₽') AS storage_per_day,
       CAST(COUNTIF(e.period_result_eligible) AS STRING) AS days
FROM d JOIN {ECON} e USING (day) JOIN {KPI} k USING (day)""",
     obj_viz([("storage_per_day", "В среднем в день"), ("days", "Учтено суток")]),
     "Хранение за период ÷ число учтённых суток.")

card("result_changes", "Изменение результата к прошлому периоду", "object", f"""-- Δ ₽ прибыли и Δ п.п. маржи к предыдущему окну той же длины; сравнение только при равном покрытии
WITH b AS (
  SELECT MIN(day) AS d1, MAX(day) AS d2, COUNT(*) AS len FROM {KPI} WHERE {{{{day}}}}
),
agg AS (
  SELECT
    IF(k.day >= b.d1, 'cur', 'prv') AS w,
    COUNTIF(e.period_result_eligible) AS cov1,
    COUNTIF(e.after_product_cogs_eligible) AS cov2,
    SUM(IF(e.period_result_eligible, e.period_result_pre_cogs_corrected_rub, NULL)) AS pre,
    SUM(IF(e.period_result_eligible, e.revenue_base_period_result_rub, NULL)) AS rev1,
    SUM(IF(e.after_product_cogs_eligible, e.period_result_after_product_cogs_rub, NULL)) AS post,
    SUM(IF(e.after_product_cogs_eligible, e.revenue_base_after_product_cogs_rub, NULL)) AS rev2
  FROM {KPI} k JOIN {ECON} e USING (day), b
  WHERE k.day BETWEEN DATE_SUB(b.d1, INTERVAL b.len DAY) AND b.d2
  GROUP BY w
),
r AS (
  SELECT
    MAX(IF(w = 'cur', post, NULL)) AS p_cur, MAX(IF(w = 'prv', post, NULL)) AS p_prv,
    MAX(IF(w = 'cur', SAFE_DIVIDE(pre, rev1), NULL)) AS m1_cur, MAX(IF(w = 'prv', SAFE_DIVIDE(pre, rev1), NULL)) AS m1_prv,
    MAX(IF(w = 'cur', SAFE_DIVIDE(post, rev2), NULL)) AS m2_cur, MAX(IF(w = 'prv', SAFE_DIVIDE(post, rev2), NULL)) AS m2_prv,
    MAX(IF(w = 'cur', cov1, NULL)) = MAX(IF(w = 'prv', cov1, NULL)) AS ok1,
    MAX(IF(w = 'cur', cov2, NULL)) = MAX(IF(w = 'prv', cov2, NULL)) AS ok2
  FROM agg
)
SELECT
  CASE WHEN NOT ok2 OR p_prv IS NULL OR p_cur IS NULL THEN 'нет сопоставимого периода'
       WHEN ROUND(p_cur - p_prv) = 0 THEN 'как в прошлом периоде'
       WHEN p_cur > p_prv THEN CONCAT('▲ на ', {rub("p_cur - p_prv")}, ' лучше')
       ELSE CONCAT('▼ на ', {rub("p_prv - p_cur")}, ' хуже') END AS profit_change,
  CASE WHEN NOT ok1 OR m1_prv IS NULL OR m1_cur IS NULL THEN 'нет сопоставимого периода'
       WHEN ROUND((m1_cur - m1_prv) * 100, 1) = 0 THEN 'без изменений'
       WHEN m1_cur > m1_prv THEN CONCAT('▲ ', {dec("(m1_cur - m1_prv) * 100", 1)}, ' п.п. (лучше)')
       ELSE CONCAT('▼ ', {dec("(m1_prv - m1_cur) * 100", 1)}, ' п.п. (хуже)') END AS margin_pre_change,
  CASE WHEN NOT ok2 OR m2_prv IS NULL OR m2_cur IS NULL THEN 'нет сопоставимого периода'
       WHEN ROUND((m2_cur - m2_prv) * 100, 1) = 0 THEN 'без изменений'
       WHEN m2_cur > m2_prv THEN CONCAT('▲ ', {dec("(m2_cur - m2_prv) * 100", 1)}, ' п.п. (лучше)')
       ELSE CONCAT('▼ ', {dec("(m2_prv - m2_cur) * 100", 1)}, ' п.п. (хуже)') END AS margin_post_change
FROM r""", obj_viz([("profit_change", "Прибыль после себестоимости"), ("margin_pre_change", "Маржа до себестоимости"),
                   ("margin_post_change", "Маржа после себестоимости")]),
     "Изменение к предыдущему периоду той же длины: прибыль — в рублях (проценты при смене знака теряют смысл), "
     "маржа — в процентных пунктах. Сравнение показывается, только если в обоих периодах одинаковое число учтённых суток.")

# Карточки V2, поглощённые объединением (архивируются, если были созданы ранее)
OBSOLETE = [PREFIX + n for n in ("Выкуп по когортам: статус", "Всего удержано из цены", "Закрытые финотчёты",
                                  "Логистика: выкупленные товары", "Логистика: отмены и обратная логистика",
                                  "Логистика: плечи и на выкуп", "Прибыль: изменение к прошлому периоду",
                                  "Маржа до себестоимости: изменение", "Маржа после себестоимости: изменение")]

# ── Раскладка dashboard 2 ────────────────────────────────────────────────────
def heading(text):
    return {"virtual_card": {"name": None, "display": "heading", "visualization_settings": {},
                             "dataset_query": {}, "archived": False},
            "text": text, "dashcard.background": False}


def op_text(text):
    return {"virtual_card": {"name": None, "display": "text", "visualization_settings": {},
                             "dataset_query": {}, "archived": False},
            "text": f"# {text}", "text.align_vertical": "middle", "text.align_horizontal": "center",
            "dashcard.background": False}


def layout(ids):
    """ids: key → card_id для V2; существующие карточки — числовые id."""
    L = []
    nid = [0]

    def put(card_ref, col, row, w, h, title=None, desc=None, viz=None, mapped=True):
        nid[0] -= 1
        cid = ids[card_ref] if isinstance(card_ref, str) else card_ref
        vs = dict(viz or {})
        if title is not None:
            vs["card.title"] = title
        if desc is not None:
            vs["card.description"] = desc
        L.append({"id": nid[0], "card_id": cid, "col": col, "row": row, "size_x": w, "size_y": h,
                  "visualization_settings": vs, "series": [], "dashboard_tab_id": None,
                  "parameter_mappings": ([{"parameter_id": "date", "card_id": cid,
                                           "target": ["dimension", ["template-tag", "day"]]}] if mapped else [])})

    def virt(vs, col, row, w, h):
        nid[0] -= 1
        L.append({"id": nid[0], "card_id": None, "col": col, "row": row, "size_x": w, "size_y": h,
                  "visualization_settings": vs, "series": [], "dashboard_tab_id": None, "parameter_mappings": []})

    # ROW 0 · статус данных (h3)
    put("status", 0, 0, 6, 3, title="Статус данных")
    put("status_detail", 6, 0, 9, 3, title="Сутки периода")
    put("freshness", 15, 0, 9, 3, title="Свежесть данных", mapped=False)

    # ROW 1 · продажи (h5)
    virt(heading("Продажи"), 0, 3, 24, 1)
    put(42, 0, 4, 4, 5, title="Заказы",
        desc="Все оформленные заказы за период, включая отменённые. Дата — дата заказа. Сравнение — с предыдущим "
             "периодом той же длины.")
    put(43, 4, 4, 4, 5, title="Выкупы",
        desc="Товары, которые покупатели выкупили. Дата — дата выкупа, поэтому выкупы нельзя делить на заказы того же "
             "периода: процент выкупа — в карточке «Выкуп по когортам».")
    put(44, 8, 4, 5, 5, title="Выручка",
        desc='Выкупленные товары по цене продавца (после скидки продавца, до СПП). Дата — дата выкупа. База для маржи и ДРР.')
    put("buyer_paid", 13, 4, 5, 5, title="Оплачено покупателями")
    put("cohort_rate", 18, 4, 6, 2, title="Выкуп по когортам")
    put("cohort_detail", 18, 6, 6, 3, title="Когорты заказов периода")

    # ROW 2 · от цены продавца до начисления WB (2 ряда плиток по 6 колонок, h3)
    virt(heading("От цены продавца до начисления WB"), 0, 9, 24, 1)
    chain = [("seller_price_rub", "Цена продавца", 0, 10,
              "Цена продавца по товарам, реализованным в закрытых финансовых отчётах WB. С выручкой может расходиться на копейки округления."),
             ("spp_rub", "СПП", 6, 10,
              "Разница между ценой продавца и суммой, оплаченной покупателем с учётом скидки WB."),
             ("wb_remuneration_rub", "Вознаграждение WB", 12, 10,
              "Вознаграждение Wildberries за реализацию товара, без НДС."),
             ("wb_remuneration_vat_rub", "НДС на вознаграждение WB", 18, 10,
              "НДС, начисленный на вознаграждение Wildberries."),
             ("acquiring_rub", "Эквайринг", 0, 13,
              "Оплата платёжных услуг: приём оплаты от покупателя."),
             ("pvz_reward_rub", "Вознаграждение ПВЗ", 6, 13,
              "Вознаграждение партнёрскому пункту выдачи, учтённое WB в расчёте начисления за товар."),
             ("goods_payable_rub", "К перечислению за товар", 12, 13,
              "Начисление WB продавцу за реализованные товары до последующих удержаний: цена продавца минус СПП, вознаграждение WB, НДС, эквайринг и ПВЗ. Слагаемые округлены до рубля, поэтому их сумма может отличаться от итога на 1–2 ₽.")]
    for field, title, col, row, desc in chain:
        put("chain", col, row, 6, 3, title=title, desc=desc, viz={"scalar.field": field})
    put("chain_total", 18, 13, 6, 3, title="Итог блока")

    # ROW 3 · операционные расходы
    virt(heading("Операционные расходы периода"), 0, 16, 24, 1)
    put("ads", 0, 17, 6, 4, title="Реклама")
    put("logistics", 6, 17, 6, 4, title="Логистика")
    put("storage", 12, 17, 6, 4, title="Хранение")
    put("tariff", 18, 17, 6, 2, title="Тарифные опции WB")
    put("other_wb", 18, 19, 6, 2, title="Прочие расходы WB")
    put("drr", 0, 21, 6, 5, title="Доля рекламы в выручке")
    put("log_breakdown", 6, 21, 12, 5, title="Состав логистики")
    put("storage_per_day", 18, 21, 6, 5, title="Хранение в день")

    # ROW 4 · финансовый результат
    virt(heading("Финансовый результат"), 0, 26, 24, 1)
    put("result_status", 0, 27, 24, 2, title="Статус результата")
    put(53, 0, 29, 6, 4, title="Результат до себестоимости",
        desc='Выручка минус удержания WB из цены, реклама, логистика, хранение, тарифные опции и прочие расходы WB. Себестоимость, налоги и OPEX не вычтены.')
    put("cogs", 6, 29, 7, 4, title="Себестоимость проданных товаров")
    put("profit", 13, 29, 11, 4, title="Прибыль после себестоимости")
    put(54, 0, 33, 6, 3, title="Маржа до себестоимости", desc="Результат до себестоимости ÷ выручка.")
    put(74, 6, 33, 7, 3, title="Маржа после себестоимости", desc="Прибыль после себестоимости ÷ выручка.")
    put("result_changes", 13, 33, 11, 3, title="Изменение к прошлому периоду")

    # ROW 5 · расчёты с Wildberries
    virt(heading("Расчёты с Wildberries — по закрытым финансовым отчётам WB, не прибыль"), 0, 36, 24, 1)
    put(49, 0, 37, 7, 3, title="К перечислению за товар",
        desc='Начисление WB продавцу за реализованные товары до последующих удержаний. По закрытым финансовым отчётам WB.')
    virt(op_text("−"), 7, 37, 1, 3)
    put(75, 8, 37, 7, 3, title="Удержания после реализации",
        desc="Всё, что WB удержал из начисления за товар: реклама по документам WB, логистика, хранение, тарифные "
             "опции и прочие удержания. Состав — в таблице ниже.")
    virt(op_text("="), 15, 37, 1, 3)
    put(76, 16, 37, 8, 3, title="К выплате от WB",
        desc='К перечислению за товар минус удержания после реализации. Сумма расчёта с WB по закрытым финансовым отчётам, не прибыль.')
    put("psd_breakdown", 8, 40, 7, 6, title="Состав удержаний")

    # ROW 6 · динамика
    virt(heading("Динамика по дням"), 0, 46, 24, 1)
    put(50, 0, 47, 24, 6, title="Выручка и прибыль после себестоимости по дням",
        desc="Столбцы — выручка по цене продавца за сутки; линия — прибыль после себестоимости тех же суток. "
             "Разрыв линии — сутки без полной экономики.",
        viz={"series_settings": {
            "revenue_rub": {"display": "bar", "title": "Выручка", "color": "#F2C94C"},
            "result_after_cogs_rub": {"display": "line", "title": "Прибыль после себестоимости", "color": "#1B7FBD"}}})
    return L


# ── транспорт ────────────────────────────────────────────────────────────────
def mb(*args, body=None):
    cmd = ["mb", *args, "-p", PROFILE, "--json", "--max-bytes=0"]
    if body is not None:
        path = "/tmp/exec_v2_body.json"
        with open(path, "w") as f:
            json.dump(body, f, ensure_ascii=False)
        cmd += ["--file", path]
    out = subprocess.run(cmd, capture_output=True, text=True)
    if out.returncode != 0:
        raise SystemExit(f"mb {' '.join(args)} failed: {out.stdout[:800]} {out.stderr[:800]}")
    return json.loads(out.stdout) if out.stdout.strip() else None


def dataset_query(sql, with_period=True):
    stage = {"lib/type": "mbql.stage/native", "native": sql, "template-tags": {}}
    if "{{day}}" in sql:
        stage["template-tags"]["day"] = {
            "id": str(uuid.uuid4()), "name": "day", "display-name": "Период", "type": "dimension",
            "dimension": ["field", {"lib/uuid": str(uuid.uuid4())}, KPI_DAY_FIELD], "widget-type": "date/all-options"}
    return {"database": DB, "lib/type": "mbql/query", "stages": [stage]}


def existing_v2():
    items = mb("collection", "items", str(COLL))
    rows = items.get("data", items) if isinstance(items, dict) else items
    return {r["name"]: r["id"] for r in rows if r.get("model") == "card" and r.get("name", "").startswith(PREFIX)}


def build_cards():
    have = existing_v2()
    ids = {}
    for key, c in CARDS.items():
        body = {"name": c["name"], "display": c["display"], "description": c["description"],
                "visualization_settings": c["viz"], "dataset_query": dataset_query(c["sql"]),
                "collection_id": COLL, "type": "question"}
        if c["name"] in have:
            cur = mb("card", "get", str(have[c["name"]]), "--full")
            tags = cur["dataset_query"]["stages"][0].get("template-tags") or []
            tags = tags if isinstance(tags, list) else list(tags.values())
            if tags and "day" in body["dataset_query"]["stages"][0]["template-tags"]:
                body["dataset_query"]["stages"][0]["template-tags"]["day"] = [t for t in tags if t["name"] == "day"][0]
            mb("card", "update", str(have[c["name"]]), body=body)
            ids[key] = have[c["name"]]
            print(f"updated {ids[key]:>4}  {c['name']}")
        else:
            res = mb("card", "create", body=body)
            ids[key] = res["id"]
            print(f"created {ids[key]:>4}  {c['name']}")
    for name, cid in have.items():
        if name in OBSOLETE:
            mb("card", "update", str(cid), body={"archived": True})
            print(f"archived {cid:>4}  {name}")
    return ids


def build_layout(ids):
    dash = mb("dashboard", "get", str(DASH), "--full")
    body = {"width": "full", "parameters": dash["parameters"], "dashcards": layout(ids)}
    mb("dashboard", "update", str(DASH), body=body)
    print(f"dashboard {DASH}: {len(body['dashcards'])} dashcards, width=full")


def check_sql(d1="2026-08-31", d2="2026-09-13"):
    tok = subprocess.check_output(["gcloud", "auth", "print-access-token"]).decode().strip()
    for key, c in CARDS.items():
        q = c["sql"].replace("{{day}}", f"day BETWEEN '{d1}' AND '{d2}'")
        req = urllib.request.Request(
            f"https://www.googleapis.com/bigquery/v2/projects/{BQ_PROJECT}/queries",
            data=json.dumps({"query": q, "useLegacySql": False, "location": "EU", "timeoutMs": 120000}).encode(),
            headers={"Authorization": "Bearer " + tok, "Content-Type": "application/json"})
        try:
            r = json.load(urllib.request.urlopen(req))
            names = [f["name"] for f in r["schema"]["fields"]]
            rows = [[v["v"] for v in row["f"]] for row in r.get("rows", [])]
            print(f"{key:18} {names} {rows}")
        except urllib.error.HTTPError as e:
            print(f"{key:18} ERROR {e.read().decode()[:400]}")


if __name__ == "__main__":
    a = set(sys.argv[1:])
    if "--check-sql" in a:
        check_sql(*[x for x in sys.argv[1:] if x[0].isdigit()][:2])
    if "--cards" in a or "--all" in a:
        ids = build_cards()
        json.dump(ids, open("/tmp/exec_v2_ids.json", "w"))
    if "--layout" in a or "--all" in a:
        ids = json.load(open("/tmp/exec_v2_ids.json")) if "--all" not in a else ids
        build_layout(ids)
