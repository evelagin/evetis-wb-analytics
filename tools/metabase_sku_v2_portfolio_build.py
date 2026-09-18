#!/usr/bin/env python3
"""
EVETIS · WB SKU Portfolio V2 — Phase C1 · сборка нового Metabase dashboard.

Спецификация: docs/SKU_PERFORMANCE_V2_PHASE_C1_PORTFOLIO_2026-09-18.md
Backend:      docs/SKU_PERFORMANCE_V2_PHASE_B_BACKEND_2026-09-18.md (все числа — только отсюда)

Dashboard 3 и его карточки не трогаются. Карточки Portfolio создаются в коллекции 7 с префиксом
«SKU V2 · » и обновляются на месте по имени (идемпотентно).

Источник всех карточек — backend Phase B:
  • wb_mart.TVF_SKU_PERFORMANCE_V2_PERIOD(p_from, p_to) — суммы текущего и предыдущего окна по SKU;
  • wb_mart.V_DASH_SKU_PERFORMANCE_V2_DAILY — границы периода из фильтра, выбор товаров, дневной ряд.
Карточки только агрегируют суммы выбранных SKU и делят сумму на сумму (ratio-of-sums) — те же формулы, что в TVF.
Экономика, покрытие и надёжность не пересчитываются по-своему.

  tools/metabase_sku_v2_portfolio_build.py --check-sql [d1 d2]  # SQL карточек в BigQuery, без Metabase
  tools/metabase_sku_v2_portfolio_build.py --cards
  tools/metabase_sku_v2_portfolio_build.py --layout
  tools/metabase_sku_v2_portfolio_build.py --all

Профиль mb: $MB_PROFILE или evetis-dev. Секретов в скрипте нет.
"""
import json, os, subprocess, sys, tempfile, urllib.request, uuid

PROFILE = os.environ.get("MB_PROFILE", "evetis-dev")
COLL, DB = 7, 2
DASH_NAME = "EVETIS · WB SKU Portfolio V2"
PREFIX = "SKU V2 · "
DAY_FIELD, SKU_FIELD = 6672, 6675          # V_DASH_SKU_PERFORMANCE_V2_DAILY.day / .product_name_short
BQ_PROJECT = "project-fa311fc0-4d87-4781-986"
V = "`wb_mart.V_DASH_SKU_PERFORMANCE_V2_DAILY`"
TVF = "`wb_mart.TVF_SKU_PERFORMANCE_V2_PERIOD`"

# ── Пороги правил «Требуют внимания» (C1). Основание — распределения SKU за 20.04–30.08.2026 по
#    непересекающимся 14-дневным окнам, только окна с ≥ 10 единиц (docs Phase C1 §6). Это НЕ бизнес-нормы.
DRR_HIGH = 0.41            # p95 ДРР SKU-окна (n = 100; p90 = 0,342)
ORDERS_DROP = -0.50        # p5 Δ заказов между соседними окнами (n = 80; p10 = −0,44)
COHORT_DROP_PP = -14.0     # p5 Δ % выкупа, п.п. (n = 80; p10 = −12,5); только зрелые когорты обоих окон

# ── Общий префикс: период из фильтра → TVF → выбранные SKU ────────────────────
BASE = f"""WITH b AS (
  SELECT MIN(day) AS d1, MAX(day) AS d2
  FROM {V}
  WHERE {{{{day}}}}
),
t AS (SELECT * FROM {TVF}((SELECT d1 FROM b), (SELECT d2 FROM b))),
sel AS (
  SELECT DISTINCT nm_id FROM {V}
  WHERE TRUE [[AND {{{{sku}}}}]]
),
r AS (SELECT * FROM t WHERE NOT is_portfolio_total AND nm_id IN (SELECT nm_id FROM sel))"""

# Суммы выбранных SKU (для всего ассортимента = строке «Портфель» TVF; проверено регрессией).
SUM_COLS = ["orders_gross_units", "orders_gross_seller_price_rub", "orders_cancelled_units", "buyout_units",
            "cohort_buyout_orders", "cohort_resolved_orders", "cohort_unresolved_orders", "cohort_conflict_orders",
            "ads_attributed_rub", "buyout_seller_price_rub", "fin_seller_price_rub", "fin_sale_units", "fin_return_units",
            "contribution_after_cogs_rub", "contribution_before_cogs_rub", "credited_for_goods_rub"]
COV_COLS = ["orders_cov_days", "sales_cov_days", "ads_cov_days", "finance_cov_days", "storage_cov_days",
            "finance_provisional_days", "price_chain_missing_days", "current_days", "days_present"]
AGG = f""",
a AS (
  SELECT
    ANY_VALUE(period_from) AS period_from, ANY_VALUE(period_to) AS period_to, ANY_VALUE(period_days) AS len,
    ANY_VALUE(min_reliable_units) AS min_units, COUNT(*) AS skus,
    {", ".join(f"SUM(cur_{c}) AS c_{c}, SUM(prv_{c}) AS p_{c}" for c in SUM_COLS)},
    {", ".join(f"MAX(cur_{c}) AS c_{c}, MAX(prv_{c}) AS p_{c}" for c in COV_COLS)},
    SUM(cur_cogs_missing_sku_days) AS c_cogs_missing_sku_days
  FROM r
),
k AS (
  SELECT a.*,
    -- сопоставимость окон по метрике: одинаковое число покрытых суток, текущее окно покрыто целиком
    (c_orders_cov_days = p_orders_cov_days AND c_orders_cov_days = len)                              AS orders_cmp,
    (c_sales_cov_days = p_sales_cov_days AND c_sales_cov_days = len)                                 AS sales_cmp,
    (c_ads_cov_days = p_ads_cov_days AND c_ads_cov_days = len AND c_sales_cov_days = len AND p_sales_cov_days = len) AS drr_cmp,
    (c_finance_cov_days = p_finance_cov_days AND c_finance_cov_days = len
     AND c_ads_cov_days = len AND p_ads_cov_days = len)                                              AS econ_cmp,
    -- Зрелость когорты для ПОДАЧИ: нет заказов без исхода ИЛИ окно закончилось ≥ 21 дня назад
    -- (p99 «заказ → выкуп» = 18 дней, Phase A §F). Флаг Phase B cohort_mature остаётся строгим.
    (c_cohort_unresolved_orders + c_cohort_conflict_orders = 0
     OR DATE_DIFF(CURRENT_DATE('Europe/Moscow'), period_to, DAY) >= 21)                              AS c_cohort_mature,
    (p_cohort_unresolved_orders + p_cohort_conflict_orders = 0
     OR DATE_DIFF(CURRENT_DATE('Europe/Moscow'), DATE_SUB(period_from, INTERVAL 1 DAY), DAY) >= 21)  AS p_cohort_mature,
    -- Надёжность сравнения (порог Phase B min_reliable_units): база ≥ порога в ОБОИХ окнах. Это только пометка
    -- «мало данных» рядом с фактическим изменением; изменение не скрывается (display ≠ alert eligibility).
    (IFNULL(c_orders_gross_units, 0) >= min_units AND IFNULL(p_orders_gross_units, 0) >= min_units)   AS orders_rel,
    (IFNULL(c_buyout_units, 0) >= min_units AND IFNULL(p_buyout_units, 0) >= min_units)               AS buyouts_rel,
    (IFNULL(c_cohort_resolved_orders, 0) >= min_units AND IFNULL(p_cohort_resolved_orders, 0) >= min_units) AS cohort_rel,
    SAFE_DIVIDE(c_orders_gross_seller_price_rub, NULLIF(c_orders_gross_units, 0)) AS c_price,
    SAFE_DIVIDE(p_orders_gross_seller_price_rub, NULLIF(p_orders_gross_units, 0)) AS p_price,
    SAFE_DIVIDE(c_cohort_buyout_orders, NULLIF(c_cohort_resolved_orders, 0))      AS c_rate,
    SAFE_DIVIDE(p_cohort_buyout_orders, NULLIF(p_cohort_resolved_orders, 0))      AS p_rate,
    SAFE_DIVIDE(c_ads_attributed_rub, NULLIF(c_buyout_seller_price_rub, 0))       AS c_drr,
    SAFE_DIVIDE(p_ads_attributed_rub, NULLIF(p_buyout_seller_price_rub, 0))       AS p_drr,
    SAFE_DIVIDE(c_contribution_after_cogs_rub, NULLIF(c_fin_seller_price_rub, 0)) AS c_margin,
    SAFE_DIVIDE(p_contribution_after_cogs_rub, NULLIF(p_fin_seller_price_rub, 0)) AS p_margin,
    SAFE_DIVIDE(c_contribution_after_cogs_rub, NULLIF(c_fin_sale_units - c_fin_return_units, 0)) AS c_per_buyout
  FROM a
)"""


# ── форматирование (presentation) ─────────────────────────────────────────────
def num(x):
    return (f"CONCAT(IF(ROUND({x}) < 0, '−', ''), "
            f"REPLACE(FORMAT(\"%'d\", ABS(CAST(ROUND({x}) AS INT64))), ',', ' '))")


def rub(x):
    return f"CONCAT({num(x)}, ' ₽')"


def pct(x, n=1):
    return f"CONCAT(REPLACE(REPLACE(FORMAT('%.{n}f', ({x}) * 100), '.', ','), '-', '−'), ' %')"


LOW = "мало данных"


def low_mark(reliable):
    """Нейтральная пометка малой выборки рядом с фактическим изменением (без цвета, без подмены значения)."""
    return f"IF({reliable}, '', ' · {LOW}')"


def d_rub(c, p, cmp, reliable="TRUE"):
    """Δ в рублях — для величин, которые могут менять знак (процент от отрицательной базы бессмыслен)."""
    d = f"(({c}) - ({p}))"
    return (f"CASE WHEN NOT IFNULL({cmp}, FALSE) THEN 'н/с' WHEN ({c}) IS NULL OR ({p}) IS NULL THEN '—' "
            f"ELSE CONCAT({arrow(d, 0)}, {num('ABS(' + d + ')')}, ' ₽', {low_mark(reliable)}) END")


def arrow(d, digits=3):
    """Стрелка по ОКРУГЛЁННОМУ показанному значению: «↑ 0,0» не выводится."""
    return f"IF(ROUND({d}, {digits}) > 0, '↑ ', IF(ROUND({d}, {digits}) < 0, '↓ ', '→ '))"


def d_pct(c, p, cmp, reliable="TRUE"):
    """Δ % для абсолютных величин; «н/с» — окна технически несопоставимы, «—» — нет базы (в прошлом окне 0).
    Малая выборка изменение НЕ скрывает: рядом ставится «мало данных»."""
    d = f"SAFE_DIVIDE(({c}) - ({p}), NULLIF(ABS({p}), 0))"
    return (f"CASE WHEN NOT IFNULL({cmp}, FALSE) THEN 'н/с' WHEN {d} IS NULL THEN '—' "
            f"ELSE CONCAT({arrow(d)}, REPLACE(FORMAT('%.1f', ABS({d}) * 100), '.', ','), ' %', {low_mark(reliable)}) END")


def d_pp(c, p, cmp, reliable="TRUE"):
    """Δ в процентных пунктах для долей."""
    d = f"(({c}) - ({p})) * 100"
    return (f"CASE WHEN NOT IFNULL({cmp}, FALSE) THEN 'н/с' WHEN ({c}) IS NULL OR ({p}) IS NULL THEN '—' "
            f"ELSE CONCAT({arrow(d, 1)}, REPLACE(FORMAT('%.1f', ABS({d})), '.', ','), ' п.п.', {low_mark(reliable)}) END")


CARDS = {}


def card(key, name, display, sql, viz, description, uses_sku=True):
    CARDS[key] = dict(name=PREFIX + name, display=display, sql=sql, viz=viz, description=description, uses_sku=uses_sku)



# ── СТАТУС ────────────────────────────────────────────────────────────────────
STATUS_SQL = BASE + AGG + """,
f AS (
  SELECT MAX(source_data_through) AS through,
         MAX(IF(finance_covered, day, NULL)) AS fin_through
  FROM """ + V + """
  WHERE day BETWEEN (SELECT d1 FROM b) AND (SELECT d2 FROM b)
),
s AS (
  SELECT k.*, f.through, f.fin_through,
         -- пропуски финансов ВНУТРИ периода (не хвост, который ещё не пришёл от WB)
         DATE_DIFF(f.fin_through, period_from, DAY) + 1 - c_finance_cov_days AS fin_gaps,
         DATE_DIFF(period_to, f.fin_through, DAY)                          AS fin_tail
  FROM k CROSS JOIN f
),
st AS (
  SELECT s.*,
    CASE
      WHEN c_orders_cov_days < len OR c_sales_cov_days < len OR c_ads_cov_days < len
        OR IFNULL(fin_gaps, len) > 0 OR fin_through IS NULL                         THEN 'INCOMPLETE'
      WHEN fin_tail > 0 OR c_finance_provisional_days > 0                          THEN 'PROVISIONAL'
      ELSE 'FINAL' END AS status_code
  FROM s
)
SELECT CONCAT(
  CASE status_code WHEN 'FINAL' THEN '🟢 Данные окончательные' WHEN 'PROVISIONAL' THEN '🟡 Предварительно'
                   ELSE '🔴 Неполные данные' END,
  ' · ', FORMAT_DATE('%d.%m', period_from), '–', FORMAT_DATE('%d.%m', period_to),
  IFNULL(CONCAT(' · ', NULLIF(ARRAY_TO_STRING(ARRAY(SELECT x FROM UNNEST([
    IF(c_orders_cov_days < len OR c_sales_cov_days < len, FORMAT('заказы %d/%d дн.', LEAST(c_orders_cov_days, c_sales_cov_days), len), NULL),
    IF(c_ads_cov_days < len, FORMAT('реклама %d/%d дн.', c_ads_cov_days, len), NULL),
    IF(IFNULL(fin_gaps, 0) > 0, FORMAT('нет финансов %d дн.', fin_gaps), NULL),
    IF(fin_tail > 0, CONCAT('финансы по ', FORMAT_DATE('%d.%m', fin_through)), NULL),
    IF(c_finance_provisional_days > 0, FORMAT('финансы предв. %d дн.', c_finance_provisional_days), NULL),
    IF(c_storage_cov_days < len, IF(c_storage_cov_days = 0, 'без хранения по товарам',
       FORMAT('хранение по товарам %d/%d дн.', c_storage_cov_days, len)), NULL),
    IF(c_price_chain_missing_days > 0, 'состав удержаний с 13.07', NULL),
    IF(c_cogs_missing_sku_days > 0, 'себестоимость частично', NULL),
    IF(NOT c_cohort_mature, FORMAT('выкуп: %d заказов без исхода',
       c_cohort_unresolved_orders + c_cohort_conflict_orders), NULL)
  ]) AS x WHERE x IS NOT NULL), ' · '), '')), '')
) AS status
FROM st"""
card("status", "Статус данных", "scalar", STATUS_SQL, {"scalar.field": "status"},
     "Качество выбранного периода. 🟢 — все источники покрыты, финансы WB окончательные. 🟡 — финансовый отчёт WB "
     "за последние дни ещё не пришёл («финансы по …»: вклад и маржа тогда без сравнения) или предварительный "
     "(«финансы предв.»). 🔴 — внутри периода нет данных. Оговорки: «хранение по товарам N/M дн.» — хранение по "
     "товару WB отдаёт с 01.09.2026, за более ранние дни вклад его не включает; «выкуп: N заказов без исхода» — "
     "процент выкупа предварительный; «состав удержаний с 13.07» — разложение удержаний WB из цены есть с 13.07.2026.")

# ── KPI ───────────────────────────────────────────────────────────────────────
KPI_TXT = {}


def kpi_text(key, name, value_sql, delta_sql, description):
    # Значение без базы (0 заказов / выкупов) — «—», а не пустая плитка: CONCAT с NULL обнулил бы всю строку.
    sql = BASE + AGG + f"\nSELECT CONCAT(IFNULL({value_sql}, '—'), '  ·  ', IFNULL({delta_sql}, '—')) AS kpi FROM k"
    card(key, name, "scalar", sql, {"scalar.field": "kpi"}, description)


kpi_text("orders", "Заказы", num("c_orders_gross_units"),
         d_pct("c_orders_gross_units", "p_orders_gross_units", "orders_cmp", "orders_rel"),
         "Все оформленные заказы за период, включая отменённые (дата заказа). Справа — изменение к предыдущему периоду "
         "такой же продолжительности; «н/с» — периоды несопоставимы по покрытию данных; «мало данных» — меньше 10 заказов в одном из периодов: изменение фактическое, но выводы делать рано; сигналов «Требуют внимания» по нему нет.")
kpi_text("buyouts", "Выкупы", num("c_buyout_units"),
         d_pct("c_buyout_units", "p_buyout_units", "sales_cmp", "buyouts_rel"),
         "Выкупленные товары по дате выкупа. Это события периода, а не судьба заказов периода: процент выкупа — "
         "в карточке «Выкуп». Справа — изменение к предыдущему периоду такой же продолжительности; «мало данных» — меньше 10 выкупов в одном из периодов: изменение фактическое, но выводы делать рано; сигналов «Требуют внимания» по нему нет.")
kpi_text("cohort_rate", "Выкуп",
         "CONCAT(IF(c_cohort_mature, '', '🟡 '), " + pct("c_rate") + ")",
         d_pp("c_rate", "p_rate", "orders_cmp AND p_cohort_mature", "cohort_rel"),
         "Доля завершённых заказов периода, которые были выкуплены: выкуплено / (выкуплено + отменено). Заказы берутся "
         "по дате заказа; незавершённые заказы не считаются отказами и в расчёт не входят. 🟡 — у части заказов "
         "исход ещё неизвестен, процент предварительный. Изменение — в процентных пунктах; «н/с», если прошлый период "
         "тоже ещё не завершён; «мало данных» — меньше 10 завершённых заказов в одном из периодов: изменение фактическое, но выводы делать рано; сигналов «Требуют внимания» по нему нет.")
kpi_text("price", "Ср. цена", rub("c_price"),
         d_pct("c_price", "p_price", "orders_cmp", "orders_rel"),
         "Средняя установленная продавцом цена до СПП, взвешенная по заказанным единицам: сумма цен всех заказанных "
         "единиц / число единиц (включая отменённые заказы). Изменение цены не оценивается как хорошее или плохое. "
         "На уровне всего ассортимента зависит и от набора проданных товаров. «мало данных» — меньше 10 заказов в одном из периодов: изменение фактическое, но выводы делать рано; сигналов «Требуют внимания» по нему нет.")
kpi_text("ads", "Реклама", rub("c_ads_attributed_rub"),
         d_pct("c_ads_attributed_rub", "p_ads_attributed_rub", "drr_cmp"),
         "Рекламные расходы, атрибутированные товарам по статистике кампаний WB (дата показа). Могут немного "
         "отличаться от рекламного биллинга WB в Executive — это разные контракты, распределение не выполняется.")
kpi_text("drr", "ДРР", pct("c_drr"),
         d_pp("c_drr", "p_drr", "drr_cmp", "buyouts_rel"),
         "Доля рекламных расходов в выручке: реклама / выкупы по цене продавца за период. Изменение — в процентных "
         "пунктах; «мало данных» — меньше 10 выкупов в одном из периодов: изменение фактическое, но выводы делать рано; сигналов «Требуют внимания» по нему нет.")
kpi_text("contribution", "Вклад SKU после себестоимости", rub("c_contribution_after_cogs_rub"),
         d_rub("c_contribution_after_cogs_rub", "p_contribution_after_cogs_rub", "econ_cmp", "buyouts_rel"),
         "Сколько товары заработали: начисление WB за товар минус прямая логистика, доступное хранение по товару, "
         "атрибутированная реклама и себестоимость. Общие расходы аккаунта (тарифная опция, штрафы, утилизация, "
         "прочие удержания) по товарам не распределяются, поэтому сумма не равна результату Executive. Это не прибыль. «мало данных» — меньше 10 выкупов в одном из периодов: изменение фактическое, но выводы делать рано; сигналов «Требуют внимания» по нему нет.")
kpi_text("margin", "Маржа вклада", pct("c_margin"),
         d_pp("c_margin", "p_margin", "econ_cmp", "buyouts_rel"),
         "Вклад SKU после себестоимости / выручка по цене продавца по финансовому отчёту WB (продажи минус возвраты). "
         "Изменение — в процентных пунктах; «мало данных» — меньше 10 выкупов в одном из периодов: изменение фактическое, но выводы делать рано; сигналов «Требуют внимания» по нему нет.")


# ── ТРЕБУЮТ ВНИМАНИЯ ──────────────────────────────────────────────────────────
#    Консервативные правила C1. Строка = SKU + факты. Причинность не утверждается.
#    Малая выборка (< min_reliable_units) не даёт сигналов, кроме «реклама без выкупов» (факт, не оценка).
ATTN_SQL = BASE + f""",
x AS (
  SELECT r.*,
    SAFE_DIVIDE(cur_ads_attributed_rub, NULLIF(cur_buyout_seller_price_rub, 0)) AS drr_c,
    (DATE_DIFF(CURRENT_DATE('Europe/Moscow'), period_to, DAY) >= 21 OR cur_cohort_unresolved_orders + cur_cohort_conflict_orders = 0) AS c_mature,
    (DATE_DIFF(CURRENT_DATE('Europe/Moscow'), prev_to, DAY) >= 21 OR prv_cohort_unresolved_orders + prv_cohort_conflict_orders = 0) AS p_mature
  FROM r
),
-- Один проход по SKU: все правила считаются массивом (BigQuery не материализует CTE, и UNION ALL из пяти
-- веток вычислял бы функцию периода пять раз).
rules AS (
  SELECT nm_id, product_name_short,
    ARRAY(SELECT AS STRUCT h.* FROM UNNEST([
      STRUCT(1 AS sev_rank, -cur_contribution_after_cogs_rub AS sev,
             IF(cur_contribution_after_cogs_rub < 0 AND cur_buyouts_reliable,
                CONCAT('Отрицательный вклад ', {rub('cur_contribution_after_cogs_rub')},
                       IFNULL(CONCAT(' · ', {rub('cur_contribution_after_cogs_per_buyout_rub')}, '/выкуп'), '')), NULL) AS what),
      STRUCT(2, cur_ads_attributed_rub,
             IF(cur_ads_attributed_rub > 0 AND cur_buyout_units = 0,
                CONCAT('Реклама ', {rub('cur_ads_attributed_rub')}, ' · выкупов нет'), NULL)),
      STRUCT(3, -orders_delta_pct,
             IF(orders_comparable AND cur_orders_reliable AND prv_orders_reliable AND period_days >= 14
                AND orders_delta_pct <= {ORDERS_DROP},
                CONCAT('Заказы ', {d_pct('cur_orders_gross_units', 'prv_orders_gross_units', 'TRUE')},
                       ' (', CAST(prv_orders_gross_units AS STRING), ' → ', CAST(cur_orders_gross_units AS STRING), ')',
                       IFNULL(CONCAT(' · цена ', {d_pct('cur_avg_seller_price_orders_rub', 'prv_avg_seller_price_orders_rub', 'TRUE')}), '')), NULL)),
      STRUCT(4, -cohort_buyout_rate_delta_pp,
             IF(orders_comparable AND cur_cohort_reliable AND prv_cohort_reliable AND c_mature AND p_mature
                AND cohort_buyout_rate_delta_pp <= {COHORT_DROP_PP},
                CONCAT('Выкуп ', {pct('cur_cohort_buyout_rate')}, ' · ',
                       {d_pp('cur_cohort_buyout_rate', 'prv_cohort_buyout_rate', 'TRUE')}), NULL)),
      STRUCT(5, drr_c,
             IF(cur_buyouts_reliable AND drr_c >= {DRR_HIGH},
                CONCAT('ДРР ', {pct('drr_c')},
                       IF(drr_comparable AND prv_buyouts_reliable, CONCAT(' · ', {d_pp('cur_drr', 'prv_drr', 'TRUE')}), '')), NULL))
    ]) AS h WHERE h.what IS NOT NULL) AS hits
  FROM x
),
top AS (
  SELECT product_name_short AS product, nm_id, hits[OFFSET(0)].sev_rank AS sev_rank, hits[OFFSET(0)].sev AS sev,
         ARRAY_TO_STRING(ARRAY(SELECT what FROM UNNEST(hits) ORDER BY sev_rank), ' · ') AS what
  FROM rules WHERE ARRAY_LENGTH(hits) > 0
  -- Порядок (детерминированный): 1) тип самого важного сигнала товара: отрицательный вклад → реклама без выкупов →
  -- падение заказов → падение выкупа → высокий ДРР; 2) внутри типа — величина (больший убыток / сумма рекламы /
  -- падение / п.п. / ДРР выше); 3) ничья — nm_id по возрастанию.
  ORDER BY sev_rank, sev DESC, nm_id LIMIT 6
)
SELECT
  IFNULL(top.product, '✅ Нет товаров, требующих внимания')                                                   AS sku,
  IFNULL(CAST(top.nm_id AS STRING), '')                                                                       AS nm,
  IFNULL(top.what, 'по правилам: отрицательный вклад, реклама без выкупов, падение заказов, падение выкупа, высокий ДРР') AS what
FROM (SELECT 1 AS one) LEFT JOIN top ON TRUE
ORDER BY top.sev_rank, top.sev DESC, top.nm_id"""
card("attention", "Требуют внимания", "table", ATTN_SQL,
     {"table.columns": [{"name": "sku", "enabled": True}, {"name": "nm", "enabled": True}, {"name": "what", "enabled": True}],
      "column_settings": {json.dumps(["name", "sku"]): {"column_title": "Товар"},
                          json.dumps(["name", "nm"]): {"column_title": "Артикул WB"},
                          json.dumps(["name", "what"]): {"column_title": "Что происходит"}},
      "table.row_index": False},
     "До 6 товаров. Порядок: сначала отрицательный вклад (больший убыток выше), затем реклама без выкупов (большая сумма "
     "выше), затем падение заказов, падение выкупа и высокий ДРР (большее отклонение выше); при равенстве — по артикулу."
     " Правила: (1) отрицательный вклад после себестоимости при ≥ 10 выкупах; "
     "(2) есть реклама, а выкупов нет; (3) заказы упали на 50 % и больше при ≥ 10 заказах в обоих периодах и периоде "
     "от 14 дней; (4) выкуп упал на 14 п.п. и больше — только по завершённым когортам и ≥ 10 завершённым заказам; "
     "(5) ДРР 41 % и выше при ≥ 10 выкупах. Пороги 3–5 — эмпирические пороги V1: 5 % самых крайних значений товаров "
     "EVETIS за апрель–август 2026 (14-дневные окна), а не универсальные бизнес-нормативы. Рядом с падением заказов показано изменение цены — это совпадение по времени, "
     "не причина.")

# ── ТАБЛИЦА ТОВАРОВ ───────────────────────────────────────────────────────────
TABLE_SQL = BASE + """,
x AS (
  SELECT r.*, (cur_orders_reliable AND prv_orders_reliable) AS ord_rel  -- надёжность Δ цены и Δ заказов
  FROM r
)
SELECT
  product_name_short                                                            AS sku,
  CAST(nm_id AS STRING)                                                         AS nm,
  cur_avg_seller_price_orders_rub                                               AS price,
  IF(orders_comparable, avg_seller_price_orders_delta_pct, NULL)                AS price_d,
  cur_orders_gross_units                                                        AS orders,
  IF(orders_comparable, orders_delta_pct, NULL)                                 AS orders_d,
  cur_buyout_units                                                              AS buyouts,
  cur_cohort_buyout_rate                                                        AS rate,
  cur_ads_attributed_rub                                                        AS ads,
  cur_drr                                                                       AS drr,
  cur_contribution_after_cogs_rub                                               AS contribution,
  cur_contribution_margin_after_cogs                                            AS margin,
  cur_contribution_after_cogs_per_buyout_rub                                    AS per_buyout,
  ARRAY_TO_STRING(ARRAY(SELECT x FROM UNNEST([
    -- малая выборка: мало данных сейчас ИЛИ показанное Δ посчитано на базе < порога (значения и Δ не скрываются)
    IF(low_sample OR (orders_comparable AND NOT ord_rel AND orders_delta_pct IS NOT NULL), 'малая выборка', NULL),
    IF(is_new_in_period, 'новый', NULL),
    IF(NOT orders_comparable AND NOT low_sample, 'нет сравнения', NULL)
  ]) AS x WHERE x IS NOT NULL), ' · ')                                          AS note
FROM x
WHERE IFNULL(cur_orders_gross_units, 0) + IFNULL(cur_buyout_units, 0) > 0
   OR IFNULL(cur_ads_attributed_rub, 0) <> 0 OR IFNULL(cur_contribution_after_cogs_rub, 0) <> 0
ORDER BY contribution DESC"""


def colfmt(title, **kw):
    return dict(column_title=title, **kw)


TABLE_VIZ = {
    "table.columns": [{"name": c, "enabled": True} for c in
                      ["sku", "nm", "price", "price_d", "orders", "orders_d", "buyouts", "rate", "ads", "drr",
                       "contribution", "margin", "per_buyout", "note"]],
    "table.row_index": False,
    # Ширины подобраны под 1440 px: 14 колонок без горизонтальной прокрутки при штатном шрифте.
    "table.column_widths": [150, 118, 93, 86, 87, 101, 93, 82, 94, 67, 82, 83, 97, 125],
    "column_settings": {json.dumps(["name", k]): v for k, v in {
        "sku": colfmt("Товар"),
        "nm": colfmt("Артикул WB"),
        "price": colfmt("Ср. цена", number_style="decimal", decimals=0, suffix=" ₽"),
        "price_d": colfmt("Δ цены", number_style="percent", decimals=1),
        "orders": colfmt("Заказы", number_style="decimal", decimals=0),
        "orders_d": colfmt("Δ заказов", number_style="percent", decimals=0),
        "buyouts": colfmt("Выкупы", number_style="decimal", decimals=0),
        "rate": colfmt("Выкуп", number_style="percent", decimals=1),
        "ads": colfmt("Реклама", number_style="decimal", decimals=0, suffix=" ₽"),
        "drr": colfmt("ДРР", number_style="percent", decimals=1),
        "contribution": colfmt("Вклад", number_style="decimal", decimals=0, suffix=" ₽"),
        "margin": colfmt("Маржа", number_style="percent", decimals=1),
        "per_buyout": colfmt("₽ / выкуп", number_style="decimal", decimals=0, suffix=" ₽"),
        "note": colfmt("Статус"),
    }.items()},
    "table.column_formatting": [
        # Цвет — только у главного результата: отрицательный вклад. Маржа и ₽/выкуп того же знака не дублируются.
        {"id": 0, "columns": ["contribution"], "type": "single", "operator": "<", "value": 0,
         "color": "#ED6E6E", "highlight_row": False},
    ],
}
card("table", "Товары", "table", TABLE_SQL, TABLE_VIZ,
     "Экономика каждого товара за период, по убыванию вклада. Ср. цена — средняя цена продавца до СПП по заказанным "
     "единицам; Δ цены и Δ заказов — к предыдущему периоду такой же длины (пусто, если периоды несопоставимы по "
     "покрытию данных или в прошлом периоде заказов не было). Выкупы — по дате выкупа. Выкуп — доля выкупленных среди завершённых заказов "
     "периода. Реклама — расходы, атрибутированные товару по статистике кампаний WB (не биллинг). ДРР — реклама / выкупы по цене продавца. Вклад — вклад SKU после себестоимости: "
     "начисление за товар минус логистика, хранение (с 01.09.2026), реклама и себестоимость; общие расходы аккаунта "
     "не распределяются. Маржа — вклад / выручка по цене продавца по финансовому отчёту WB. ₽ / выкуп — вклад на "
     "выкупленную единицу. «Малая выборка» — меньше 10 заказов и выкупов сейчас или меньше 10 заказов в одном из "
     "сравниваемых периодов: значения и изменения фактические, выводы делать рано, сигналов «Требуют внимания» по ним нет.")

# ── ДИНАМИКА ──────────────────────────────────────────────────────────────────
DYN_SQL = f"""SELECT
  day,
  SUM(orders_gross_units)                                                                AS orders,
  SAFE_DIVIDE(SUM(orders_gross_seller_price_rub), NULLIF(SUM(orders_gross_units), 0))   AS price
FROM {V}
WHERE {{{{day}}}}
  [[AND {{{{sku}}}}]]
GROUP BY day
ORDER BY day"""
card("dynamics", "Заказы и средняя цена по дням", "combo", DYN_SQL,
     {"graph.dimensions": ["day"], "graph.metrics": ["orders", "price"],
      "series_settings": {"orders": {"display": "bar", "title": "Заказы", "axis": "left", "color": "#509EE3"},
                          "price": {"display": "line", "title": "Ср. цена, ₽", "axis": "right", "color": "#7172AD"}},
      "graph.x_axis.title_text": "", "graph.y_axis.title_text": "", "graph.show_values": False,
      "column_settings": {json.dumps(["name", "price"]): {"number_style": "decimal", "decimals": 0, "suffix": " ₽"}}},
     "Столбцы — заказы по дате заказа, линия — средняя цена продавца до СПП в заказах этого дня (сумма цен / число "
     "единиц). Для всего ассортимента цена зависит и от набора заказанных товаров; для одного товара выберите его "
     "в фильтре «Товар». Совпадение изменений цены и заказов не доказывает причину.")


# ── mb / BigQuery ─────────────────────────────────────────────────────────────
def mb(*args, body=None):
    cmd = ["mb", *args, "-p", PROFILE, "--json", "--max-bytes=0"]
    tmp = None
    if body is not None:
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(body, tmp, ensure_ascii=False); tmp.close()
        cmd += ["--file", tmp.name]
    out = subprocess.run(cmd, capture_output=True, text=True)
    if tmp:
        os.unlink(tmp.name)
    if out.returncode != 0:
        raise SystemExit(f"mb {' '.join(args)} failed: {out.stdout[:800]} {out.stderr[:800]}")
    return json.loads(out.stdout) if out.stdout.strip() else None


def dataset_query(sql, uses_sku, keep=None):
    """template-tags: при обновлении карточки id тегов сохраняются (keep), иначе — новые UUID v4."""
    keep = keep or {}
    tags = {}
    for name, field, disp, widget in (("day", DAY_FIELD, "Период", "date/all-options"), ("sku", SKU_FIELD, "Товар", "string/=")):
        if "{{" + name + "}}" in sql:
            old = keep.get(name, {})
            dim_uuid = (old.get("dimension") or [None, {}])[1].get("lib/uuid") if old.get("dimension") else None
            tags[name] = {"id": old.get("id") or str(uuid.uuid4()), "name": name, "display-name": disp, "type": "dimension",
                          "dimension": ["field", {"lib/uuid": dim_uuid or str(uuid.uuid4())}, field], "widget-type": widget}
    return {"database": DB, "lib/type": "mbql/query",
            "stages": [{"lib/type": "mbql.stage/native", "native": sql, "template-tags": tags}]}


def existing():
    items = mb("collection", "items", str(COLL))
    rows = items.get("data", items) if isinstance(items, dict) else items
    return {r["name"]: r["id"] for r in rows if r.get("model") == "card" and r.get("name", "").startswith(PREFIX)}


def build_cards():
    have, ids = existing(), {}
    for key, c in CARDS.items():
        keep = {}
        if c["name"] in have:
            cur = mb("card", "get", str(have[c["name"]]), "--full")
            tt = cur["dataset_query"]["stages"][0].get("template-tags") or []
            keep = {t["name"]: t for t in (tt if isinstance(tt, list) else tt.values())}
        body = {"name": c["name"], "display": c["display"], "description": c["description"],
                "visualization_settings": c["viz"], "dataset_query": dataset_query(c["sql"], c["uses_sku"], keep),
                "collection_id": COLL, "type": "question"}
        if c["name"] in have:
            mb("card", "update", str(have[c["name"]]), body=body); ids[key] = have[c["name"]]
            print(f"updated {ids[key]:>4}  {c['name']}")
        else:
            ids[key] = mb("card", "create", body=body)["id"]
            print(f"created {ids[key]:>4}  {c['name']}")
    return ids


def bq(sql):
    tok = subprocess.check_output(["gcloud", "auth", "print-access-token"]).decode().strip()
    body = json.dumps({"query": sql, "useLegacySql": False, "location": "EU", "timeoutMs": 200000,
                       "defaultDataset": {"projectId": BQ_PROJECT, "datasetId": "wb_mart"}}).encode()
    r = urllib.request.Request(f"https://www.googleapis.com/bigquery/v2/projects/{BQ_PROJECT}/queries", data=body,
                               headers={"Authorization": "Bearer " + tok, "Content-Type": "application/json"})
    try:
        d = json.load(urllib.request.urlopen(r))
    except urllib.error.HTTPError as e:
        return {"error": e.read().decode()[:600]}
    f = [x["name"] for x in d["schema"]["fields"]]
    return {"rows": [dict(zip(f, [c["v"] for c in row["f"]])) for row in d.get("rows", [])]}


def render(sql, d1, d2, sku=None):
    q = sql.replace("{{day}}", f"day BETWEEN DATE '{d1}' AND DATE '{d2}'")
    q = q.replace("[[AND {{sku}}]]", f"AND product_name_short IN ({', '.join(repr(s) for s in sku)})" if sku else "")
    return q.replace("[[AND {{sku}} ]]", "")


def check_sql(d1="2026-09-01", d2="2026-09-14", sku=None):
    for key, c in CARDS.items():
        res = bq(render(c["sql"], d1, d2, sku))
        print(f"== {key}: {json.dumps(res, ensure_ascii=False)[:600]}")


PARAMS = [
    # По умолчанию — 30 дней, заканчивающиеся позавчера: финансовый отчёт WB приходит с задержкой 1–2 дня,
    # и так вклад и маржа сравнимы с предыдущим периодом с первого открытия.
    {"id": "date", "type": "date/all-options", "name": "Период", "slug": "date", "sectionId": "date", "default": "past30days-from-1days"},
    {"id": "sku", "type": "string/=", "name": "Товар", "slug": "sku", "sectionId": "string", "isMultiSelect": True},
]


def heading(text):
    return {"virtual_card": {"name": None, "display": "heading", "visualization_settings": {},
                             "dataset_query": {}, "archived": False},
            "text": text, "dashcard.background": False}


def layout(ids):
    L, nid = [], [0]

    def put(key, col, row, w, h, title, viz=None):
        nid[0] -= 1
        cid = ids[key]
        vs = dict(viz or {}); vs["card.title"] = title
        pm = [{"parameter_id": "date", "card_id": cid, "target": ["dimension", ["template-tag", "day"]]}]
        if "{{sku}}" in CARDS[key]["sql"]:
            pm.append({"parameter_id": "sku", "card_id": cid, "target": ["dimension", ["template-tag", "sku"]]})
        L.append({"id": nid[0], "card_id": cid, "col": col, "row": row, "size_x": w, "size_y": h,
                  "visualization_settings": vs, "series": [], "dashboard_tab_id": None, "parameter_mappings": pm})

    def virt(vs, col, row, w, h):
        nid[0] -= 1
        L.append({"id": nid[0], "card_id": None, "col": col, "row": row, "size_x": w, "size_y": h,
                  "visualization_settings": vs, "series": [], "dashboard_tab_id": None, "parameter_mappings": []})

    row = 0
    put("status", 0, row, 24, 2, "Статус данных"); row += 2
    for i, (k, t) in enumerate([("orders", "Заказы"), ("buyouts", "Выкупы"), ("cohort_rate", "Выкуп"), ("price", "Ср. цена")]):
        put(k, i * 6, row, 6, KPI_H, t)
    row += KPI_H
    for i, (k, t) in enumerate([("ads", "Реклама"), ("drr", "ДРР"), ("contribution", "Вклад SKU после себестоимости"), ("margin", "Маржа вклада")]):
        put(k, i * 6, row, 6, KPI_H, t)
    row += KPI_H
    for key, (col, w, h, title) in EXTRA_LAYOUT.items():
        if key.startswith("#"):
            virt(heading(title), 0, row, 24, 1); row += 1
        elif key in ids:
            put(key, col, row, w, h, title); row += h if col + w >= 24 else 0
    return L


KPI_H = 2
EXTRA_LAYOUT = {
    "attention": (0, 24, 6, "Требуют внимания"),
    "table": (0, 24, 17, "Товары"),
    "dynamics": (0, 24, 7, "Заказы и средняя цена по дням"),
}


def dashboard_id():
    items = mb("collection", "items", str(COLL))
    rows = items.get("data", items) if isinstance(items, dict) else items
    hit = [r for r in rows if r.get("name") == DASH_NAME and r.get("model") == "dashboard"]
    if hit:
        return hit[0]["id"]
    d = mb("dashboard", "create", body={"name": DASH_NAME, "collection_id": COLL, "parameters": PARAMS,
            "description": "Управленческий экран ассортимента: заказы, выкуп, цена, реклама и вклад товаров за период "
                           "в сравнении с предыдущим периодом той же длины. Данные — backend SKU Performance V2."})
    return d["id"]


def build_layout(ids):
    did = dashboard_id()
    mb("dashboard", "update", str(did), body={"width": "full", "parameters": PARAMS, "dashcards": layout(ids)})
    print(f"dashboard {did}: {len(layout(ids))} dashcards")
    return did


if __name__ == "__main__":
    a = sys.argv[1:]
    if "--check-sql" in a:
        rest = [x for x in a if not x.startswith("--")]
        check_sql(*(rest[:2] or []))
    elif "--cards" in a:
        print(build_cards())
    elif "--all" in a:
        build_layout(build_cards())
    elif "--layout" in a:
        have = existing()
        build_layout({key: have[c["name"]] for key, c in CARDS.items() if c["name"] in have})
    else:
        print(__doc__)
