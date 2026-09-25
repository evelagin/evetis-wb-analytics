#!/usr/bin/env python3
"""
EVETIS PLANNING — PR-PLAN-1 · экран планирования владельца в Metabase (коллекция 9 «00 · Owner Control Tower»).

Контракт: docs/plan/PR_PLAN_1_SALES_PLAN_TRAJECTORY_2026-09-25.md. Все числа — только из представлений
evetis_mart PR-PLAN-1 (и справочника названий товаров); карточки ничего не пересчитывают по-своему,
только выбирают, раскладывают по месяцам и подписывают.

Четыре смысла разведены везде одинаково:
  ФАКТ        — что есть и как продаётся сейчас (наблюдение);
  НУЖНО       — сколько нужно продавать, чтобы успеть до «срок годности − буфер» (требование к запасу);
  ПРЕДЛОЖЕНО  — план, который предлагает система; это ещё НЕ план, пока владелец его не утвердил;
  УТВЕРЖДЕНО  — план после решения владельца (ACK на хеш версии).

Вкладки: «Главное» · «Предложенный план» · «Крем для рук · УВЛ · АКНЕ» · «Детали».
Существующие дашборды коллекции 9 (5, 6, 8, 9) и их карточки не трогаются. Карточки создаются с
префиксом «PLAN · » и обновляются на месте по имени (идемпотентно).

  tools/metabase_plan1_build.py --check-sql     # SQL карточек в BigQuery, без Metabase
  tools/metabase_plan1_build.py --all           # карточки + раскладка
  tools/metabase_plan1_build.py --rollback      # архивировать дашборд и карточки «PLAN · »

Профиль mb: $MB_PROFILE или evetis-dev. Секретов в скрипте нет.
"""
import json, os, subprocess, sys, tempfile, urllib.request

PROFILE = os.environ.get("MB_PROFILE", "evetis-dev")
COLL, DB = 9, 2
DASH_NAME = "EVETIS PLANNING · план продаж и остатки по месяцам"
PREFIX = "PLAN · "
P = "project-fa311fc0-4d87-4781-986"


def v(name, ds="evetis_mart"):
    return f"`{P}.{ds}.{name}`"


NAMES = f"(SELECT internal_sku, product_name_short AS nm FROM {v('REF_PRODUCT_MASTER', 'evetis_ref')})"
# Последняя предложенная (не отозванная) версия — её владелец и видит, и утверждает.
PROPOSAL = f"""(SELECT plan_version FROM {v('V_PLAN_VERSION_STATUS')}
   WHERE plan_kind IN ('SYSTEM_PROPOSED', 'OWNER_AUTHORED') AND lifecycle_status NOT IN ('WITHDRAWN', 'INTEGRITY_BROKEN')
   ORDER BY created_at DESC LIMIT 1)"""
MODEL = f"""(SELECT plan_version FROM {v('V_PLAN_VERSION_STATUS')} WHERE plan_kind = 'MODEL_SCENARIO' ORDER BY created_at DESC LIMIT 1)"""
MONTH_RU = ("CONCAT(['янв','фев','мар','апр','май','июн','июл','авг','сен','окт','ноя','дек'][OFFSET(EXTRACT(MONTH FROM {x}) - 1)], "
            "' ', FORMAT_DATE('%y', {x}))")

RED, YELLOW, GREEN, GREY, BLUE = "#ED6E6E", "#F9CF48", "#84BB4C", "#DCDFE5", "#509EE3"
CARDS = {}


def fmt_neg(cols):
    """Отрицательное число — красным (нехватка без обрезки видна сразу)."""
    return [{"columns": cols, "type": "single", "operator": "<", "value": 0, "color": RED, "highlight_row": False}]


def fmt_text(col, pairs):
    return [{"columns": [col], "type": "single", "operator": "=", "value": val, "color": color, "highlight_row": False}
            for val, color in pairs]


def table(key, name, sql, titles, description, formatting=None, pivot=None, decimals=None):
    cs = {}
    for a, t in titles.items():
        s = {"column_title": t}
        if decimals and a in decimals:
            s["decimals"] = decimals[a]
        cs[json.dumps(["name", a], ensure_ascii=False)] = s
    viz = {"column_settings": cs, "table.column_formatting": formatting or []}
    if pivot:
        viz.update({"table.pivot": True, "table.pivot_column": pivot[0], "table.cell_column": pivot[1]})
    else:
        viz["table.pivot"] = False
    CARDS[key] = dict(name=PREFIX + name, display="table", sql=sql, description=description, viz=viz)


# ════════════════════════════════════════ ВКЛАДКА 1 · ГЛАВНОЕ ════════════════════════════════════
table("signals", "Сигналы", f"""WITH ffa AS (
  SELECT MAX(ff_snapshot_date) AS ff FROM {v('CT_INVENTORY_SNAPSHOT_DAILY', 'evetis_ref')}
  WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM {v('CT_INVENTORY_SNAPSHOT_DAILY', 'evetis_ref')})
),
e AS (
  SELECT x.*, n.nm FROM {v('V_PLANNING_EXCEPTIONS')} x LEFT JOIN {NAMES} n USING (internal_sku)
  WHERE x.trajectory_basis IS NULL OR x.trajectory_basis = 'APPROVED_PLAN' OR x.plan_version = {PROPOSAL}
),
s AS (
  SELECT 1 AS o, '🔴' AS tone, 'Остаток на фулфилменте устарел' AS sig,
    CONCAT('Последний срез ФФ — ', FORMAT_DATE('%d.%m.%Y', (SELECT ff FROM ffa)), ', прошло ',
           CAST(DATE_DIFF(CURRENT_DATE('Europe/Moscow'), (SELECT ff FROM ffa), DAY) AS STRING),
           ' дн. (норма — 7). Остатки по месяцам ниже посчитаны от старого остатка: это расчёт, а не точная картина. Нужен новый срез ФФ.') AS what,
    COUNTIF(exception_code = 'INVENTORY_STALE') AS n
  FROM e WHERE exception_code = 'INVENTORY_STALE' HAVING COUNT(*) > 0
  UNION ALL
  SELECT 2, '🔴', 'Утверждённого плана нет',
    'Система предложила план (вкладка «Предложенный план»). Пока вы его не утвердите, колонка «Утверждено» пуста.', COUNT(*)
  FROM e WHERE exception_code = 'NO_APPROVED_PLAN' HAVING COUNT(*) > 0
  UNION ALL
  SELECT 3, '🔴', 'Не хватит товара по предложенному плану',
    STRING_AGG(CONCAT(nm, ' — с ', {MONTH_RU.format(x='month')}, ', до ', CAST(CAST(ROUND(units) AS INT64) AS STRING), ' шт.'), '; ' ORDER BY month, nm),
    COUNT(*)
  FROM e WHERE exception_code = 'COMPONENT_SHORTFALL' HAVING COUNT(*) > 0
  UNION ALL
  SELECT 4, '🟡', 'Поступление без подтверждённой даты',
    STRING_AGG(DISTINCT CONCAT(IFNULL(nm, internal_sku), ' — ', CAST(CAST(units AS INT64) AS STRING), ' шт.'), '; '), COUNT(DISTINCT inbound_id)
  FROM e WHERE exception_code IN ('INBOUND_ETA_UNKNOWN', 'INBOUND_BLOCKED', 'INBOUND_ETA_OVERDUE') HAVING COUNT(*) > 0
  UNION ALL
  SELECT 5, '🟡', 'Давление срока годности',
    STRING_AGG(CONCAT(nm, ' — к дате «продать до» останется ≈', CAST(CAST(ROUND(units) AS INT64) AS STRING), ' шт.'), '; ' ORDER BY nm),
    COUNT(*)
  FROM e WHERE exception_code = 'EXPIRY_PRESSURE' HAVING COUNT(*) > 0
  UNION ALL
  SELECT 6, '⚪', 'Возможное производство (не в расчёте)',
    STRING_AGG(CONCAT(IFNULL(nm, internal_sku), ' — ', CAST(CAST(units AS INT64) AS STRING), ' шт.'), '; '), COUNT(*)
  FROM e WHERE exception_code = 'INBOUND_HYPOTHETICAL' HAVING COUNT(*) > 0
)
SELECT tone, sig, what FROM s ORDER BY o""",
      {"tone": " ", "sig": "Сигнал", "what": "Что это значит"},
      "Главные сигналы для решения: несвежий остаток ФФ, нет утверждённого плана, нехватка товара, поставки без даты, давление срока.")

table("sku_chain", "По каждому товару", f"""SELECT
  n.nm AS name,
  o.inventory_position_units AS f_now,
  ROUND(o.observed_units_per_day_30d * 30) AS f_month,
  IF(o.sell_by_date IS NULL, '—', FORMAT_DATE('%d.%m.%Y', o.sell_by_date)) AS r_by,
  CASE
    WHEN o.sell_by_date IS NULL THEN '—'
    WHEN o.required_units_per_day_to_sell_by IS NOT NULL THEN CAST(CAST(ROUND(o.required_units_per_day_to_sell_by * 30) AS INT64) AS STRING)
    WHEN o.sell_by_date > o.inventory_as_of_date AND o.inventory_position_units > 0
      -- та же формула, что V_SKU_INVENTORY_TARGET_CURRENT (остаток ÷ дни до «продать до»), но на устаревшем остатке — с пометкой
      THEN CONCAT('≈', CAST(CAST(ROUND(o.inventory_position_units / DATE_DIFF(o.sell_by_date, o.inventory_as_of_date, DAY) * 30) AS INT64) AS STRING),
                  ' (по устаревшему остатку)')
    ELSE '—' END AS r_month,
  ROUND(o.proposed_units_horizon) AS p_total,
  ROUND(o.proposed_closing_units_at_horizon_end) AS p_end,
  IF(o.proposed_first_shortfall_month IS NULL, '—', CONCAT('🔴 с ', {MONTH_RU.format(x='o.proposed_first_shortfall_month')})) AS p_short,
  IF(o.proposed_units_left_at_sell_by > 0, ROUND(o.proposed_units_left_at_sell_by), NULL) AS p_left,
  CASE o.approved_trajectory_status WHEN 'COMPUTED' THEN 'утверждено' ELSE 'не утверждено' END AS a_status,
  CONCAT(
    IF(o.inventory_freshness_status != 'FRESH', '🔴 остаток ФФ устарел  ', ''),
    IF(o.proposed_first_shortfall_month IS NOT NULL, '🔴 нехватка  ', ''),
    IF(o.inbound_committed_not_in_trajectory_units > 0, '🟡 поставка без даты  ', ''),
    IF(o.proposed_units_left_at_sell_by > 0, '🟡 срок годности  ', ''),
    IF(o.inbound_hypothetical_units > 0, '⚪ возможное производство', '')) AS flags
FROM {v('V_PLANNING_SKU_OVERVIEW')} o LEFT JOIN {NAMES} n USING (internal_sku)
ORDER BY (o.proposed_first_shortfall_month IS NULL), o.proposed_first_shortfall_month, n.nm""",
      {"name": "Товар", "f_now": "ФАКТ · есть сейчас, шт.", "f_month": "ФАКТ · продаём, шт./мес.",
       "r_by": "НУЖНО · продать до", "r_month": "НУЖНО · шт./мес.", "p_total": "ПРЕДЛОЖЕНО · продать до 31.03, шт.",
       "p_end": "ПРЕДЛОЖЕНО · останется на 31.03, шт.", "p_short": "ПРЕДЛОЖЕНО · нехватка",
       "p_left": "ПРЕДЛОЖЕНО · останется к «продать до», шт.", "a_status": "УТВЕРЖДЕНО", "flags": "Сигналы"},
      "Одна строка — один физический товар (наборы разложены на составляющие). Слева направо: сколько есть → как продаём → "
      "сколько нужно → что предлагает система → утверждено ли → что останется. Продаём/нужно — в пересчёте на 30 дней.",
      formatting=fmt_neg(["p_end"]) + fmt_text("a_status", [("не утверждено", GREY), ("утверждено", GREEN)]))

table("closing_grid", "Остаток на конец месяца · по предложенному плану", f"""SELECT n.nm AS name,
  FORMAT_DATE('%Y-%m', t.month) AS m, ROUND(t.closing_units) AS val
FROM {v('V_PLAN_TRAJECTORY_MONTHLY')} t LEFT JOIN {NAMES} n USING (internal_sku)
WHERE t.trajectory_basis = 'VERSION_SCENARIO' AND t.plan_version = {PROPOSAL}
ORDER BY name, m""", {"name": "Товар", "m": "Месяц", "val": "Остаток, шт."},
      "Сколько штук останется на конец каждого месяца, если продавать по предложенному плану. Минус — нехватка "
      "(план не урезается). Подтверждённых поставок сейчас нет, поэтому остаток только убывает. Остаток ФФ устарел — это расчёт, не факт.",
      formatting=fmt_neg(["val"]), pivot=("m", "val"))

table("sales_grid", "Продажи по месяцам · предложено, физ. шт.", f"""SELECT n.nm AS name,
  FORMAT_DATE('%Y-%m', t.month) AS m, ROUND(t.planned_physical_units) AS val
FROM {v('V_PLAN_TRAJECTORY_MONTHLY')} t LEFT JOIN {NAMES} n USING (internal_sku)
WHERE t.trajectory_basis = 'VERSION_SCENARIO' AND t.plan_version = {PROPOSAL}
ORDER BY name, m""", {"name": "Товар", "m": "Месяц", "val": "Продать, шт."},
      "Сколько штук каждого товара предлагается продать в месяц (одиночные + в составе наборов). Текущий месяц — "
      "только остаток до конца месяца за вычетом уже заказанного.", pivot=("m", "val"))

table("approved_grid", "Утверждено · продажи по месяцам, карточки", f"""SELECT n.nm AS name, a.marketplace AS mp,
  FORMAT_DATE('%Y-%m', a.month) AS m, a.planned_cards AS val
FROM {v('V_SALES_PLAN_APPROVED')} a LEFT JOIN {NAMES} n USING (internal_sku)
UNION ALL
SELECT '— утверждённого плана пока нет —', '', FORMAT_DATE('%Y-%m', CURRENT_DATE()), CAST(NULL AS NUMERIC)
FROM (SELECT 1) WHERE NOT EXISTS (SELECT 1 FROM {v('V_SALES_PLAN_APPROVED')})
ORDER BY name, mp, m""", {"name": "Товар", "mp": "Площадка", "m": "Месяц", "val": "Карточек"},
      "Только то, что утвердил владелец. Пусто, пока план не утверждён.")

# ════════════════════════════════════ ВКЛАДКА 2 · ПРЕДЛОЖЕННЫЙ ПЛАН ═════════════════════════════
table("proposal_about", "Что предложено и как", f"""WITH h AS (SELECT * FROM {v('V_PLAN_VERSION_STATUS')} WHERE plan_version = {PROPOSAL})
SELECT k, val FROM h, UNNEST([
  STRUCT(1 AS o, 'Что это' AS k, 'Предложение системы. Это ещё не план: планом оно станет только после вашего утверждения.' AS val),
  STRUCT(2, 'Как посчитано', 'Сколько карточек заказали за последние 30 полных дней (без отмен) ÷ 30 × число дней в месяце. Отдельно WB и Ozon, отдельно каждая карточка и набор.'),
  STRUCT(3, 'Данные продаж по', FORMAT_DATE('%d.%m.%Y', h.basis_sales_as_of)),
  STRUCT(4, 'Период плана', CONCAT(FORMAT_DATE('%d.%m.%Y', h.horizon_from), ' — ', FORMAT_DATE('%d.%m.%Y', h.horizon_to))),
  STRUCT(5, 'Итого карточек', CAST(CAST(ROUND(h.planned_cards_total) AS INT64) AS STRING)),
  STRUCT(6, 'Чего в плане НЕТ', 'Сезонности, множителя Ozon ×1,25, множителей наборов, распродажи крема для рук к декабрю, будущих поставок. Всё это было в модели Control Tower и сюда не перенесено.'),
  STRUCT(7, 'Статус', CASE h.lifecycle_status WHEN 'PROPOSED' THEN 'предложено, ждёт вашего решения' WHEN 'APPROVED' THEN 'утверждено' ELSE h.lifecycle_status END),
  STRUCT(8, 'Номер версии', h.plan_version),
  STRUCT(9, 'Контрольная сумма для утверждения', h.content_sha256)
]) ORDER BY o""", {"k": " ", "val": " "}, "Метод, период, итог и что сознательно не заложено.")

table("assumptions", "Допущения предложения", f"""SELECT a.assumption_id AS id,
  CASE a.evidence_class WHEN 'FACT' THEN 'факт' WHEN 'ASSUMPTION' THEN 'допущение' WHEN 'INFERENCE' THEN 'вывод' ELSE a.evidence_class END AS cls,
  a.value_text AS txt
FROM {v('PLAN_ASSUMPTION', 'evetis_ref')} a
WHERE a.plan_version = {PROPOSAL} AND a.assumption_type != 'OBSERVED_RATE'
ORDER BY a.assumption_id""", {"id": "№", "cls": "Тип", "txt": "Содержание"},
      "Допущения, на которых построено предложение (темп по каждой карточке — во вкладке «Детали»).")

table("proposal_cards", "Предложено · карточки по SKU × WB/Ozon × месяц", f"""SELECT CONCAT(n.nm, IF(l.sales_mode = 'BUNDLE', ' (набор)', '')) AS name,
  IF(l.marketplace = 'OZON', 'Ozon', 'WB') AS mp, FORMAT_DATE('%Y-%m', l.month) AS m, l.planned_cards AS val
FROM {v('V_PLAN_LINE_MONTHLY_ALL')} l LEFT JOIN {NAMES} n USING (internal_sku)
WHERE l.plan_version = {PROPOSAL}
ORDER BY l.sales_mode DESC, name, mp, m""", {"name": "Карточка", "mp": "Площадка", "m": "Месяц", "val": "Карточек"},
      "Предложенный план в карточках продаж (как их видит покупатель: одиночный товар или набор), по площадкам и месяцам. "
      "Текущий месяц — целиком; уже проданное вычитается при расчёте остатков.", pivot=("m", "val"), decimals={"val": 1})

table("proposal_phys", "Предложено · физические шт. после разложения наборов", f"""WITH p AS (
  SELECT x.*, n.nm FROM {v('V_PLAN_PHYSICAL_MONTHLY')} x LEFT JOIN {NAMES} n ON n.internal_sku = x.component_sku
  WHERE x.plan_version = {PROPOSAL}
)
SELECT nm AS name, 'всего' AS part, FORMAT_DATE('%Y-%m', month) AS m, ROUND(planned_units_full_month) AS val FROM p
UNION ALL SELECT nm, '  в т.ч. в наборах', FORMAT_DATE('%Y-%m', month), ROUND(via_bundle_units_full_month) FROM p
ORDER BY name, part DESC, m""", {"name": "Товар", "part": " ", "m": "Месяц", "val": "Шт."},
      "Сколько физических единиц товара уйдёт за месяц: одиночные карточки + составляющие наборов по составу, "
      "зафиксированному вместе с версией плана.", pivot=("m", "val"))

table("proposal_vs_fact", "Предложено против фактического темпа", f"""WITH t AS (
  SELECT t.*, n.nm FROM {v('V_PLAN_TRAJECTORY_MONTHLY')} t LEFT JOIN {NAMES} n USING (internal_sku)
  WHERE t.trajectory_basis = 'VERSION_SCENARIO' AND t.plan_version IN ({PROPOSAL}, {MODEL})
),
a AS (
  SELECT nm, internal_sku,
    ANY_VALUE(observed_units_per_day_30d) AS fact_day,
    ANY_VALUE(observed_velocity_quality_30d) AS q,
    SUM(IF(plan_version = {PROPOSAL} AND NOT is_current_month, planned_physical_units, 0))
      / NULLIF(SUM(IF(plan_version = {PROPOSAL} AND NOT is_current_month, period_days, 0)), 0) AS prop_day,
    SUM(IF(plan_version = {MODEL} AND NOT is_current_month, planned_physical_units, 0))
      / NULLIF(SUM(IF(plan_version = {MODEL} AND NOT is_current_month, period_days, 0)), 0) AS model_day,
    ARRAY_AGG(IF(plan_version = {PROPOSAL}, closing_units, NULL) IGNORE NULLS ORDER BY month DESC LIMIT 1)[SAFE_OFFSET(0)] AS end_units,
    MIN(IF(plan_version = {PROPOSAL} AND shortfall_units > 0, month, NULL)) AS short_m,
    MAX(IF(plan_version = {PROPOSAL}, projected_units_at_sell_by, NULL)) AS left_sb,
    ANY_VALUE(sell_by_date) AS sell_by
  FROM t GROUP BY nm, internal_sku
)
SELECT nm AS name,
  ROUND(fact_day * 30) AS fact_m,
  ROUND(prop_day * 30) AS prop_m,
  IF(fact_day > 0, CAST(ROUND((prop_day / fact_day - 1) * 100) AS INT64) + 0, NULL) AS diff_pct,
  ROUND(model_day * 30) AS model_m,
  ROUND(end_units) AS end_units,
  IF(prop_day > 0 AND end_units > 0, ROUND(end_units / prop_day / 30, 1), NULL) AS cover_m,
  CASE
    WHEN short_m IS NOT NULL THEN CONCAT('🔴 дефицит с ', {MONTH_RU.format(x='short_m')})
    WHEN left_sb > 0 THEN CONCAT('🟡 к ', FORMAT_DATE('%d.%m.%Y', sell_by), ' останется ≈', CAST(CAST(ROUND(left_sb) AS INT64) AS STRING), ' шт.')
    WHEN prop_day > 0 AND end_units / prop_day / 30 > 12 THEN '🟡 запаса больше чем на год'
    ELSE '—' END AS verdict,
  CASE q WHEN 'NORMAL' THEN 'надёжный' WHEN 'STOCKOUT_CONSTRAINED' THEN 'занижен: были дни без товара'
    WHEN 'AVAILABILITY_NOT_FULLY_OBSERVED' THEN 'наличие видно не за все дни' WHEN 'INSUFFICIENT_HISTORY' THEN 'мало истории'
    WHEN 'NO_SALES_WITH_STOCK' THEN 'продаж нет' ELSE IFNULL(q, '—') END AS quality
FROM a ORDER BY (short_m IS NULL), short_m, name""",
      {"name": "Товар", "fact_m": "ФАКТ · продаём, шт./мес.", "prop_m": "ПРЕДЛОЖЕНО · шт./мес.", "diff_pct": "Разница с фактом, %",
       "model_m": "Модель CT · шт./мес.", "end_units": "ПРЕДЛОЖЕНО · останется на 31.03", "cover_m": "Хватит ещё на, мес.",
       "verdict": "Дефицит / избыток", "quality": "Насколько верен факт"},
      "Где предложение расходится с фактическим темпом и к чему приводит: дефицит, остаток к дате «продать до», "
      "избыток. Темп — в пересчёте на 30 дней, по полным месяцам плана. Модель CT — для сравнения, не план.",
      formatting=fmt_neg(["end_units"]))

# ═══════════════════════════════ ВКЛАДКА 3 · КРЕМ ДЛЯ РУК · УВЛ · АКНЕ ═══════════════════════════
def focus(key, sku, title, note):
    table(key, title, f"""WITH t AS (
  SELECT * FROM {v('V_PLAN_TRAJECTORY_MONTHLY')}
  WHERE internal_sku = '{sku}' AND trajectory_basis = 'VERSION_SCENARIO' AND plan_version IN ({PROPOSAL}, {MODEL})
)
SELECT {MONTH_RU.format(x='month')} AS m,
  ROUND(MAX(IF(plan_version = {PROPOSAL}, opening_units, NULL))) AS op,
  ROUND(MAX(IF(plan_version = {PROPOSAL}, eligible_inbound_units, NULL))) AS inb,
  ROUND(MAX(IF(plan_version = {PROPOSAL}, planned_physical_units, NULL))) AS p_sales,
  ROUND(MAX(IF(plan_version = {PROPOSAL}, planned_via_bundle_units, NULL))) AS p_bundle,
  ROUND(MAX(IF(plan_version = {PROPOSAL}, closing_units, NULL))) AS p_end,
  ROUND(MAX(observed_run_rate_units)) AS f_sales,
  ROUND(MAX(required_units_to_sell_by)) AS r_sales,
  ROUND(MAX(IF(plan_version = {MODEL}, planned_physical_units, NULL))) AS m_sales,
  ROUND(MAX(IF(plan_version = {MODEL}, closing_units, NULL))) AS m_end,
  IF(MAX(projected_units_at_sell_by) IS NULL, '',
     CONCAT('к «продать до» ', FORMAT_DATE('%d.%m', ANY_VALUE(sell_by_date)), ': ≈',
            CAST(CAST(ROUND(MAX(IF(plan_version = {PROPOSAL}, projected_units_at_sell_by, NULL))) AS INT64) AS STRING), ' шт. по предложению')) AS note
FROM t GROUP BY month ORDER BY month""",
          {"m": "Месяц", "op": "Начало, шт.", "inb": "+ Подтв. поставки", "p_sales": "− ПРЕДЛОЖЕНО продать",
           "p_bundle": "  в т.ч. в наборах", "p_end": "= Останется", "f_sales": "ФАКТ-темп за период",
           "r_sales": "НУЖНО к «продать до»", "m_sales": "Модель CT · продать", "m_end": "Модель CT · останется", "note": " "},
          note, formatting=fmt_neg(["p_end", "m_end"]))


focus("hand", "EVT-HC-HAND-300", "Крем для рук · по месяцам",
      "Срок годности партии 31.12.2026, «продать до» 01.12.2026. При текущем темпе к этой дате останется большая часть запаса. "
      "Модель CT закладывала распродажу к декабрю — в предложение это не перенесено: решение о распродаже за владельцем.")
focus("moist", "EVT-FC-MOIST-50", "Крем увлажняющий (УВЛ) · по месяцам",
      "Сейчас 2 шт. Партия 5 000 шт. произведена, находится в Китае и ждёт оплаты; подтверждённой даты нет — в расчёт не входит "
      "(ни в октябрь, ни позже). Модель CT считала её пришедшей 20.10.2026 — отсюда разница в остатках.")
focus("acne", "EVT-FC-ACNE-50", "Крем АКНЕ · по месяцам",
      "3 000 шт. — только возможное производство (упаковка у фабрики есть, крем не заказан): в расчёт не входит.")

table("focus_lots", "Поставки по трём кремам", f"""SELECT n.nm AS name, l.quantity AS q,
  CASE l.lot_state WHEN 'PRODUCED' THEN 'произведено' WHEN 'PLANNED' THEN 'в планах, не заказано' WHEN 'HYPOTHETICAL' THEN 'гипотеза'
    WHEN 'ORDER_CONFIRMED' THEN 'заказ подтверждён' WHEN 'IN_PRODUCTION' THEN 'в производстве' WHEN 'IN_TRANSIT' THEN 'в пути'
    WHEN 'RECEIVED' THEN 'принято' WHEN 'CANCELLED' THEN 'отменено' ELSE l.lot_state END AS st,
  CASE l.blocker WHEN 'AWAITING_PAYMENT' THEN 'ждёт оплаты' ELSE IFNULL(l.blocker, '—') END AS bl,
  IF(l.eta_date IS NULL, 'нет подтверждённой даты', FORMAT_DATE('%d.%m.%Y', l.eta_date)) AS eta,
  IF(l.in_base_trajectory, 'да', 'нет') AS in_t,
  IF(l.ct_model_inbound_eta IS NULL, '—', CONCAT(FORMAT_DATE('%d.%m.%Y', l.ct_model_inbound_eta), ' (допущение модели)')) AS ct
FROM {v('V_INBOUND_LOT_CURRENT')} l LEFT JOIN {NAMES} n USING (internal_sku)
WHERE l.internal_sku IN ('EVT-HC-HAND-300', 'EVT-FC-MOIST-50', 'EVT-FC-ACNE-50') ORDER BY name""",
      {"name": "Товар", "q": "Шт.", "st": "Состояние", "bl": "Препятствие", "eta": "Дата поступления", "in_t": "Входит в расчёт",
       "ct": "Дата в модели CT"},
      "Поступление входит в расчёт только с подтверждённой датой и без препятствий.",
      formatting=fmt_text("in_t", [("нет", GREY), ("да", GREEN)]))

# ═══════════════════════════════════════ ВКЛАДКА 4 · ДЕТАЛИ ══════════════════════════════════════
table("exceptions", "Все исключения", f"""SELECT
  CASE severity WHEN 'BLOCKER' THEN '🔴' WHEN 'WARNING' THEN '🟡' ELSE '⚪' END AS sev, exception_code AS code,
  IFNULL(trajectory_basis, '') AS basis, IFNULL(plan_version, '') AS ver, IFNULL(internal_sku, '') AS sku,
  IFNULL(inbound_id, '') AS lot, month AS m, ROUND(units, 1) AS u, detail, evidence
FROM {v('V_PLANNING_EXCEPTIONS')} ORDER BY severity_rank, code, basis, ver, sku""",
      {"sev": " ", "code": "Код", "basis": "Основа", "ver": "Версия", "sku": "SKU", "lot": "Партия", "m": "Месяц", "u": "Ед.",
       "detail": "Подробно", "evidence": "Источник"},
      "Служебная таблица: все исключения всех версий с происхождением.")

table("versions", "Версии плана", f"""SELECT plan_version AS ver, plan_kind AS kind, lifecycle_status AS st, method_id AS method,
  horizon_from AS hf, horizon_to AS ht, line_rows_actual AS lines, ROUND(planned_cards_total, 1) AS cards,
  content_sha256 AS sha, integrity_ok AS ok, events_total AS ev, IFNULL(ignored_events, '') AS ign,
  approved_by AS by_, approved_at AS at_, effective_from_month AS ef, effective_to_month_exclusive AS et
FROM {v('V_PLAN_VERSION_STATUS')} ORDER BY created_at DESC""",
      {"ver": "Версия", "kind": "Вид", "st": "Статус", "method": "Метод", "hf": "С", "ht": "По", "lines": "Строк",
       "cards": "Карточек", "sha": "content_sha256", "ok": "Целостность", "ev": "Событий", "ign": "Недействующие события",
       "by_": "Утвердил", "at_": "Когда", "ef": "Действует с", "et": "До (искл.)"},
      "Служебная таблица: реестр версий, хеши, события. Модельный сценарий CT (MODEL_SCENARIO) утвердить нельзя.")

table("rates", "Темп по каждой карточке (основа предложения)", f"""SELECT n.nm AS name, a.scope_marketplace AS mp, a.scope_sku AS sku,
  ROUND(a.value_numeric, 3) AS rate, ROUND(a.value_numeric * 30, 1) AS rate_m, a.value_text AS txt
FROM {v('PLAN_ASSUMPTION', 'evetis_ref')} a LEFT JOIN {NAMES} n ON n.internal_sku = a.scope_sku
WHERE a.plan_version = {PROPOSAL} AND a.assumption_type = 'OBSERVED_RATE' ORDER BY name, mp""",
      {"name": "Карточка", "mp": "Площадка", "sku": "SKU", "rate": "Карточек в день", "rate_m": "≈ в 30 дней", "txt": "Основание"},
      "Наблюдаемый темп заказов без отмен за 30 полных дней по каждой карточке и площадке — из него построено предложение.")

table("inbound_all", "Все партии поступления", f"""SELECT inbound_id AS id, internal_sku AS sku, quantity AS q, lot_state AS st,
  IFNULL(blocker, '') AS bl, eta_date AS eta, eta_status AS eta_st, inclusion_status AS inc, in_base_trajectory AS in_t,
  ct_model_inbound_eta AS ct, CONCAT(evidence_class, ': ', evidence_ref) AS ev, state_recorded_at AS rec
FROM {v('V_INBOUND_LOT_CURRENT')} ORDER BY sku, id""",
      {"id": "Партия", "sku": "SKU", "q": "Шт.", "st": "Состояние", "bl": "Блокер", "eta": "Дата", "eta_st": "Статус даты",
       "inc": "Правило", "in_t": "В расчёте", "ct": "Дата модели CT", "ev": "Основание", "rec": "Записано"},
      "Служебная таблица: партии с основанием и правилом включения.")


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


def items():
    it = mb("collection", "items", str(COLL))
    return it.get("data", it) if isinstance(it, dict) else it


def existing():
    return {r["name"]: r["id"] for r in items() if r.get("model") == "card" and r.get("name", "").startswith(PREFIX)}


def build_cards():
    have, ids = existing(), {}
    for key, c in CARDS.items():
        body = {"name": c["name"], "display": c["display"], "description": c["description"],
                "visualization_settings": c["viz"], "collection_id": COLL, "type": "question",
                "dataset_query": {"database": DB, "lib/type": "mbql/query",
                                  "stages": [{"lib/type": "mbql.stage/native", "native": c["sql"], "template-tags": {}}]}}
        if c["name"] in have:
            mb("card", "update", str(have[c["name"]]), body=body); ids[key] = have[c["name"]]
            print(f"updated {ids[key]:>4}  {c['name']}")
        else:
            ids[key] = mb("card", "create", body=body)["id"]
            print(f"created {ids[key]:>4}  {c['name']}")
    return ids


def text_card(md, h=1, align="middle"):
    return {"virtual_card": {"name": None, "display": "text", "visualization_settings": {}, "dataset_query": {}, "archived": False},
            "text": md, "text.align_vertical": align, "dashcard.background": False}


def heading(text):
    return {"virtual_card": {"name": None, "display": "heading", "visualization_settings": {}, "dataset_query": {}, "archived": False},
            "text": text, "dashcard.background": False}


LEGEND = ("**ФАКТ** — что есть и как продаётся сейчас · **НУЖНО** — сколько продавать, чтобы успеть до «срок годности − 30 дн.» · "
          "**ПРЕДЛОЖЕНО** — план, который предлагает система (ещё не план) · **УТВЕРЖДЕНО** — план после вашего решения")
TABS = [
    ("Главное", [
        ("text", LEGEND, 1),
        ("signals", "Сигналы", 5),
        ("#", "По каждому товару: есть → продаём → нужно → предложено → утверждено → останется"),
        ("sku_chain", "По каждому товару", 7),
        ("#", "По месяцам"),
        ("closing_grid", "ПРЕДЛОЖЕНО · сколько останется на конец месяца, шт.", 7),
        ("sales_grid", "ПРЕДЛОЖЕНО · сколько продать за месяц, шт.", 7),
        ("approved_grid", "УТВЕРЖДЕНО · продажи по месяцам", 3),
    ]),
    ("Предложенный план", [
        ("text", "Это предложение системы. Оно **не утверждено** и ничего не меняет, пока вы его не утвердите. "
                 "Ниже — как оно посчитано, что в нём по товарам, площадкам и месяцам и где оно расходится с фактом.", 1),
        ("proposal_about", "Что предложено и как", 6),
        ("assumptions", "Допущения", 4),
        ("proposal_vs_fact", "Предложено против факта: где дефицит, где избыток", 7),
        ("proposal_cards", "Карточки продаж · SKU × площадка × месяц", 12),
        ("proposal_phys", "Физические шт. после разложения наборов", 9),
    ]),
    ("Крем для рук · УВЛ · АКНЕ", [
        ("hand", "Крем для рук", 5),
        ("moist", "Крем увлажняющий (УВЛ)", 5),
        ("acne", "Крем АКНЕ", 5),
        ("focus_lots", "Поставки по трём кремам", 3),
    ]),
    ("Детали", [
        ("text", "Служебные таблицы: происхождение чисел, версии и контрольные суммы, правила включения поставок.", 1),
        ("exceptions", "Все исключения", 7),
        ("versions", "Версии плана", 4),
        ("rates", "Темп по каждой карточке", 8),
        ("inbound_all", "Все партии поступления", 3),
    ]),
]


def layout(ids):
    tabs, cards, nid = [], [], 0
    for ti, (tname, blocks) in enumerate(TABS):
        tid = -(ti + 1)
        tabs.append({"id": tid, "name": tname})
        row = 0
        for b in blocks:
            nid -= 1
            if b[0] == "#":
                cards.append({"id": nid, "card_id": None, "col": 0, "row": row, "size_x": 24, "size_y": 1, "dashboard_tab_id": tid,
                              "visualization_settings": heading(b[1]), "series": [], "parameter_mappings": []})
                row += 1
            elif b[0] == "text":
                cards.append({"id": nid, "card_id": None, "col": 0, "row": row, "size_x": 24, "size_y": b[2], "dashboard_tab_id": tid,
                              "visualization_settings": text_card(b[1]), "series": [], "parameter_mappings": []})
                row += b[2]
            else:
                key, title, h = b
                cards.append({"id": nid, "card_id": ids[key], "col": 0, "row": row, "size_x": 24, "size_y": h, "dashboard_tab_id": tid,
                              "visualization_settings": {"card.title": title}, "series": [], "parameter_mappings": []})
                row += h
    return tabs, cards


def dashboard_id():
    hit = [r for r in items() if r.get("name") == DASH_NAME and r.get("model") == "dashboard"]
    if hit:
        return hit[0]["id"]
    return mb("dashboard", "create", body={
        "name": DASH_NAME, "collection_id": COLL, "parameters": [],
        "description": "PR-PLAN-1. Сколько товара есть, как продаётся, сколько нужно продавать, что предлагает система, что утверждено "
                       "и что останется по месяцам. План утверждает только владелец."})["id"]


def build_layout(ids):
    did = dashboard_id()
    tabs, cards = layout(ids)
    mb("dashboard", "update", str(did), body={"width": "full", "parameters": [], "tabs": tabs, "dashcards": cards})
    print(f"dashboard {did}: {len(tabs)} tabs, {len(cards)} dashcards")
    return did


def bq(sql):
    tok = subprocess.check_output(["gcloud", "auth", "print-access-token"]).decode().strip()
    body = json.dumps({"query": sql, "useLegacySql": False, "location": "EU", "timeoutMs": 200000, "maxResults": 4}).encode()
    r = urllib.request.Request(f"https://www.googleapis.com/bigquery/v2/projects/{P}/queries", data=body,
                               headers={"Authorization": "Bearer " + tok, "Content-Type": "application/json"})
    try:
        d = json.load(urllib.request.urlopen(r, timeout=300))
    except urllib.error.HTTPError as e:
        return {"error": e.read().decode()[:700]}
    return {"rows": d.get("totalRows"), "first": [[c["v"] for c in row["f"]] for row in d.get("rows", [])[:3]]}


def rollback():
    for r in items():
        if (r.get("model") == "dashboard" and r.get("name") == DASH_NAME) or \
           (r.get("model") == "card" and r.get("name", "").startswith(PREFIX)):
            mb(r["model"], "update", str(r["id"]), body={"archived": True})
            print(f"archived {r['model']} {r['id']} {r['name']}")


if __name__ == "__main__":
    a = sys.argv[1:]
    if "--check-sql" in a:
        only = [x for x in a if not x.startswith("--")]
        for key, c in CARDS.items():
            if not only or key in only:
                print(f"== {key}: {json.dumps(bq(c['sql']), ensure_ascii=False)[:900]}")
    elif "--all" in a:
        build_layout(build_cards())
    elif "--rollback" in a:
        rollback()
    else:
        print(__doc__)
