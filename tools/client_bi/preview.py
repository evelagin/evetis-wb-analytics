"""Tenancy T6 — локальный предпросмотр клиентского отчёта на реальных данных арендатора (только чтение).

Строит один HTML-файл с 5 разделами спецификации tools/client_bi/report.SPEC. Источник — тела
вью analytics_share ИЗ ВЕТКИ (sql/tenant/ozon/analytics_share/*.sql), исполненные как обычный
SELECT: предпросмотр показывает ровно то, что вернут вью после развёртывания, и ничего не создаёт.

Данные клиента в Git не попадают: путь вывода внутри репозитория отклоняется (репозиторий
публичный). Файл — для владельца, до любых действий в аккаунте Google.

  python -m tools.client_bi.preview client_001 /путь/вне/репозитория/preview.html
"""
from __future__ import annotations

import html
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tools.client_bi import report as BI  # noqa: E402
from tools.tenancy import registry as R  # noqa: E402

SHARE_SQL = REPO / "sql" / "tenant" / "ozon" / "analytics_share"
MAX_BYTES = 500_000_000


def view_body(name: str, contract: dict) -> str:
    sql = (SHARE_SQL / f"{name}.sql").read_text(encoding="utf-8")
    sql = re.sub(r"`__tenant__\.([a-z_]+)\.", lambda m: f"`{contract['project_id']}.{contract['datasets'][m.group(1)]}.", sql)
    body = re.search(r"\bAS\s*\n(.*)$", sql, re.S).group(1)
    return "(" + re.sub(r";\s*$", "", body.strip()) + ")"


def bq(sql: str, project: str) -> list[dict]:
    if not re.match(r"(?is)^\s*(WITH|SELECT)\b", sql):
        raise ValueError("только SELECT")
    dry = subprocess.run(["bq", f"--project_id={project}", "query", "--dry_run", "--format=json", "--use_legacy_sql=false", sql],
                         capture_output=True, text=True)
    if dry.returncode:
        raise RuntimeError(dry.stdout + dry.stderr)
    stats = json.loads(dry.stdout).get("statistics", {})
    if stats.get("query", {}).get("statementType", "SELECT") != "SELECT" or int(stats.get("totalBytesProcessed", 0)) > MAX_BYTES:
        raise RuntimeError(f"запрос не SELECT или больше {MAX_BYTES} байт")
    r = subprocess.run(["bq", f"--project_id={project}", "query", "--format=json", "--max_rows=2000", "--use_legacy_sql=false", sql],
                       capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(r.stdout + r.stderr)
    return json.loads(r.stdout or "[]")


def collect(tenant_id: str) -> dict:
    c = R.terraform_inputs(tenant_id)
    p = c["project_id"]
    v = {n: view_body(n, c) for n in BI.CA.client_facing_views()}
    q = lambda sql: bq(sql, p)  # noqa: E731
    d: dict = {"project": p, "tenant": tenant_id}
    d["freshness"] = q(f"SELECT * FROM {v['data_freshness']} ORDER BY dashboard_section, entity")
    end = next(r["last_complete_date"] for r in d["freshness"] if r["entity"] == "fbo_postings")
    d["sales_end"] = end
    d["sales"] = q(f"SELECT * FROM {v['sales_daily']} ORDER BY sales_date")
    win = f"BETWEEN DATE_SUB(DATE '{end}', INTERVAL 27 DAY) AND DATE '{end}'" if end else "IS NULL"
    d["sales_window"] = q(f"""SELECT COUNT(*) AS days, COUNTIF(data_status = 'COMPLETE') AS complete_days, SUM(orders) AS orders,
        SUM(ordered_units) AS ordered_units, SUM(ordered_value_rub) AS ordered_value_rub, SUM(cancelled_units) AS cancelled_units,
        SUM(delivered_units) AS delivered_units, SUM(in_progress_units) AS in_progress_units, SUM(delivered_value_rub) AS delivered_value_rub
        FROM {v['sales_daily']} WHERE sales_date {win}""")[0]
    d["sku"] = q(f"""SELECT offer_id, ANY_VALUE(ozon_name) AS ozon_name, SUM(ordered_units) AS ordered_units, SUM(ordered_value_rub) AS ordered_value_rub,
        SUM(cancelled_units) AS cancelled_units, SUM(delivered_units) AS delivered_units
        FROM {v['sku_daily']} WHERE data_status = 'COMPLETE' AND sales_date {win} GROUP BY offer_id ORDER BY ordered_value_rub DESC LIMIT 15""")
    d["warehouses"] = q(f"""SELECT warehouse_name, COUNT(DISTINCT posting_number) AS postings, SUM(quantity) AS units,
        SUM(IF(is_cancelled, quantity, 0)) AS cancelled, SUM(IF(is_delivered, quantity, 0)) AS delivered
        FROM {v['orders']} WHERE order_date {win} GROUP BY warehouse_name ORDER BY units DESC LIMIT 10""")
    d["cover"] = q(f"SELECT * FROM {v['inventory_cover']} ORDER BY days_of_cover IS NULL, days_of_cover, available_units DESC")
    d["clusters"] = q(f"""SELECT cluster_name, SUM(available_stock_count) AS available, SUM(transit_stock_count) AS transit
        FROM {v['inventory_current']} GROUP BY cluster_name ORDER BY available DESC LIMIT 12""")
    aend = next(r["last_complete_date"] for r in d["freshness"] if r["entity"] == "ads_expense_daily")
    d["ads_end"] = aend
    awin = f"BETWEEN DATE_SUB(DATE '{aend}', INTERVAL 27 DAY) AND DATE '{aend}'" if aend else "IS NULL"
    d["ads_daily"] = q(f"""SELECT stat_date, SUM(expense_rub) AS expense_rub, SUM(revenue_rub) AS revenue_rub, ANY_VALUE(data_status) AS data_status
        FROM {v['advertising_daily']} GROUP BY stat_date ORDER BY stat_date""")
    d["ads_window"] = q(f"""SELECT a.*, s.ordered_value_rub, s.complete_days AS both_complete_days FROM
        (SELECT SUM(expense_rub) AS expense_rub, SUM(impressions) AS impressions, SUM(clicks) AS clicks, SUM(orders) AS orders,
           SUM(revenue_rub) AS revenue_rub FROM {v['advertising_daily']} WHERE data_status = 'COMPLETE' AND stat_date {awin}) a
        CROSS JOIN (SELECT SUM(x.ordered_value_rub) AS ordered_value_rub, COUNT(*) AS complete_days FROM {v['sales_daily']} x
           JOIN (SELECT DISTINCT stat_date FROM {v['advertising_daily']} WHERE data_status = 'COMPLETE' AND stat_date {awin}) y
           ON y.stat_date = x.sales_date WHERE x.data_status = 'COMPLETE') s""")[0]
    d["ads_drr_spend"] = q(f"""SELECT SUM(a.expense_rub) AS expense_rub FROM {v['advertising_daily']} a
        JOIN {v['sales_daily']} s ON s.sales_date = a.stat_date
        WHERE a.data_status = 'COMPLETE' AND s.data_status = 'COMPLETE' AND a.stat_date {awin}""")[0]
    d["campaigns"] = q(f"""SELECT campaign_title, SUM(expense_rub) AS expense_rub, SUM(impressions) AS impressions, SUM(clicks) AS clicks,
        SUM(orders) AS orders, SUM(revenue_rub) AS revenue_rub FROM {v['advertising_daily']}
        WHERE data_status = 'COMPLETE' AND stat_date {awin} GROUP BY campaign_title ORDER BY expense_rub DESC LIMIT 10""")
    d["billing"] = q(f"""SELECT SUM(amount_rub) AS amount_rub FROM {v['store_costs_daily']}
        WHERE accrual_class IN ('PROMOTION_BILLING', 'PROMOTION_SERVICES') AND event_date {awin}""")[0]
    d["sku_ads"] = q(f"""SELECT ads_data_status, COUNT(*) AS rows_, SUM(ad_spend_attributed_rub) AS spend
        FROM {v['sku_daily']} WHERE sales_date {awin} GROUP BY ads_data_status""")
    ewin = f"BETWEEN DATE_SUB(DATE '{end}', INTERVAL 59 DAY) AND DATE '{end}'" if end else "IS NULL"
    d["econ_basis"] = q(f"""SELECT revenue_basis, finance_data_status, ANY_VALUE(finance_status_reason) AS reason, SUM(delivered_units) AS units,
        SUM(seller_revenue_rub) AS revenue, SUM(commission_rub) AS commission, SUM(logistics_rub) AS logistics,
        SUM(acquiring_rub) AS acquiring, SUM(return_logistics_rub) AS return_logistics, SUM(contribution_pre_cogs_rub) AS pre_cogs,
        COUNTIF(contribution_pre_cogs_rub IS NULL) AS pre_cogs_null, COUNT(*) AS rows_, ANY_VALUE(cogs_coverage) AS cogs
        FROM {v['profitability_daily']} WHERE sales_date {ewin} GROUP BY 1, 2 ORDER BY 1""")
    d["econ_sku"] = q(f"""SELECT offer_id, ANY_VALUE(ozon_name) AS ozon_name, SUM(delivered_units) AS units, SUM(seller_revenue_rub) AS revenue,
        SUM(commission_rub) AS commission, SUM(logistics_rub) AS logistics, SUM(acquiring_rub) AS acquiring
        FROM {v['profitability_daily']} WHERE sales_date {ewin} AND revenue_basis = 'SETTLED' GROUP BY offer_id ORDER BY revenue DESC LIMIT 12""")
    d["costs"] = q(f"""SELECT accrual_class, SUM(amount_rub) AS amount_rub, SUM(unresolved_accruals) AS unresolved
        FROM {v['store_costs_daily']} WHERE event_date {ewin} GROUP BY accrual_class ORDER BY amount_rub""")
    d["coverage"] = q(f"""SELECT entity, coverage_date, status FROM {v['data_coverage']}
        WHERE coverage_date > DATE_SUB(CURRENT_DATE('Europe/Moscow'), INTERVAL 45 DAY)""")
    d["generated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return d


# ───────────────────────────────────────────── рендер
def num(x, digits=0):
    if x is None or x == "":
        return "—"
    v = float(x)
    s = f"{v:,.{digits}f}".replace(",", " ").replace(".", ",")
    return s.replace("-", "−")


def pct(a, b):
    try:
        return num(100 * float(a) / float(b), 1) + " %" if a is not None and b not in (None, 0, "0") and float(b) else "—"
    except (TypeError, ValueError):
        return "—"


STATUS_RU = {k: v[0] for k, v in BI.STATUS_LABELS.items()}


def chip(status):
    s = status or "UNKNOWN"
    return f'<span class="chip s-{s.lower()}">{html.escape(STATUS_RU.get(s, s))}</span>'


def table(headers, rows, num_cols=()):
    th = "".join(f'<th class="{"n" if i in num_cols else ""}">{html.escape(h)}</th>' for i, h in enumerate(headers))
    body = "".join("<tr>" + "".join(f'<td class="{"n" if i in num_cols else ""}">{c}</td>' for i, c in enumerate(r)) + "</tr>" for r in rows)
    return f'<div class="tw"><table><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table></div>'


def kpis(items):
    return '<div class="kpis">' + "".join(
        f'<div class="kpi"><div class="kl">{html.escape(l)}</div><div class="kv">{v}</div>{f"<div class=ks>{html.escape(s)}</div>" if s else ""}</div>'
        for l, v, s in items) + "</div>"


def bars_svg(points, fields, colors, labels, status_key="data_status", height=220, until=None):
    """Столбцы по суткам; сутки не COMPLETE — штриховка, отсутствующие сутки — пустой серый слот."""
    if not points:
        return "<p class=muted>Нет строк.</p>"
    from datetime import date, timedelta
    days = [date.fromisoformat(p["sales_date"] if "sales_date" in p else p["stat_date"]) for p in points]
    byday = {dd: p for dd, p in zip(days, points)}
    last = max(days[-1], date.fromisoformat(until)) if until else days[-1]
    span = [days[0] + timedelta(i) for i in range((last - days[0]).days + 1)]
    vmax = max((float(p.get(f) or 0) for p in points for f in fields), default=1) or 1
    w = max(620, len(span) * 9)
    bw = (w - 60) / len(span)
    out = [f'<svg viewBox="0 0 {w} {height + 30}" class="chart" role="img">']
    for t in range(0, 5):
        y = 10 + (height - 20) * t / 4
        out.append(f'<line x1="56" x2="{w}" y1="{y:.1f}" y2="{y:.1f}" class="grid"/><text x="52" y="{y + 4:.1f}" class="ax" text-anchor="end">{num(vmax * (1 - t / 4) / 1000)}k</text>')
    for i, dd in enumerate(span):
        x = 58 + i * bw
        p = byday.get(dd)
        if p is None:
            out.append(f'<rect x="{x:.1f}" y="10" width="{bw - 1:.1f}" height="{height - 20}" class="gap"><title>{dd}: нет данных</title></rect>')
            continue
        incomplete = (p.get(status_key) or "UNKNOWN") != "COMPLETE"
        sub = bw / len(fields)
        for j, f in enumerate(fields):
            v = float(p.get(f) or 0)
            h = (height - 20) * v / vmax
            out.append(f'<rect x="{x + j * sub:.1f}" y="{10 + height - 20 - h:.1f}" width="{max(sub - 1, 1):.1f}" height="{h:.1f}" '
                       f'fill="{colors[j]}" {"class=inc" if incomplete else ""}><title>{dd} · {labels[j]}: {num(v)} ₽ · {STATUS_RU.get(p.get(status_key) or "UNKNOWN")}</title></rect>')
        if dd.day == 1 or i == 0:
            out.append(f'<text x="{x:.1f}" y="{height + 22}" class="ax">{dd.strftime("%d.%m")}</text>')
    run = []
    for i, dd in enumerate(span + [None]):
        if dd is not None and dd not in byday:
            run.append(i)
            continue
        if len(run) >= 3:
            x0, x1 = 58 + run[0] * bw, 58 + (run[-1] + 1) * bw
            out.append(f'<text x="{(x0 + x1) / 2:.1f}" y="{height / 2:.1f}" class="gapt" text-anchor="middle">нет данных</text>'
                       f'<text x="{(x0 + x1) / 2:.1f}" y="{height / 2 + 14:.1f}" class="gapt" text-anchor="middle">{span[run[0]].strftime("%d.%m")}–{span[run[-1]].strftime("%d.%m")}</text>')
        run = []
    out.append("</svg>")
    legend = "".join(f'<span class="lg"><i style="background:{c}"></i>{html.escape(l)}</span>' for c, l in zip(colors, labels))
    legend += '<span class="lg"><i class="lgap"></i>сутки без данных</span><span class="lg"><i class="linc"></i>неполные сутки</span>'
    return "".join(out) + f'<div class="legend">{legend}</div>'


def heatmap(cov, freshness, today):
    from datetime import date, timedelta
    t = date.fromisoformat(today)
    days = [t - timedelta(i) for i in range(44, -1, -1)]
    m = {(r["entity"], r["coverage_date"]): r["status"] for r in cov}
    rows = []
    for f in freshness:
        cells = "".join(f'<td class="hm s-{(m.get((f["entity"], dd.isoformat())) or "unknown").lower()}" title="{dd} · {STATUS_RU.get(m.get((f["entity"], dd.isoformat())) or "UNKNOWN")}"></td>' for dd in days)
        rows.append(f'<tr><th class="hmh">{html.escape(f["entity_title"])}</th>{cells}</tr>')
    head = "".join(f'<td class="hmd">{dd.strftime("%d") if dd.day in (1, 10, 20) or dd == t else ""}</td>' for dd in days)
    return f'<div class="tw"><table class="hmt"><tbody>{"".join(rows)}<tr><th></th>{head}</tr></tbody></table></div>'


CSS = """
:root{--bg:#f7f7f5;--card:#fff;--ink:#1d1f23;--mut:#6b7078;--line:#e4e4e0;--c1:#2a6fdb;--c2:#13a37f;--c3:#c4572e;
--ok:#1f9d55;--okb:#e3f5ea;--pa:#a86b00;--pab:#fdf1d8;--un:#6b7078;--unb:#ececea;--fa:#b42318;--fab:#fde7e5}
@media (prefers-color-scheme:dark){:root{--bg:#141518;--card:#1c1e22;--ink:#e8e9eb;--mut:#9aa0a8;--line:#2c2f35;
--okb:#173826;--pab:#3a2e12;--unb:#34373e;--fab:#3d1a17}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
header{padding:20px 16px 8px;max-width:1180px;margin:auto}h1{font-size:22px;margin:0 0 4px}
.sub{color:var(--mut)}nav{position:sticky;top:0;background:var(--bg);border-bottom:1px solid var(--line);z-index:2}
nav div{max-width:1180px;margin:auto;padding:8px 16px;display:flex;gap:6px;flex-wrap:wrap}
nav a{color:var(--ink);text-decoration:none;padding:5px 10px;border-radius:6px;border:1px solid var(--line);background:var(--card);font-size:13px}
main{max-width:1180px;margin:auto;padding:8px 16px 40px}section{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:18px;margin:16px 0}
h2{font-size:18px;margin:0 0 10px}h3{font-size:14px;margin:18px 0 8px;color:var(--mut);font-weight:600}
.banner{border-left:3px solid var(--pa);background:var(--pab);padding:9px 12px;border-radius:6px;margin:0 0 12px}
.banner.ok{border-color:var(--ok);background:var(--okb)}.banner.un{border-color:var(--un);background:var(--unb)}
.kpis{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:10px;margin:8px 0}
.kpi{border:1px solid var(--line);border-radius:8px;padding:10px}.kl{color:var(--mut);font-size:12px}.kv{font-size:19px;font-weight:600;font-variant-numeric:tabular-nums}.ks{color:var(--mut);font-size:11px}
.tw{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:13px}th,td{padding:6px 8px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}
th{color:var(--mut);font-weight:600;font-size:12px}td.n,th.n{text-align:right;font-variant-numeric:tabular-nums}
.chip{display:inline-block;padding:1px 8px;border-radius:10px;font-size:12px}
.s-complete{background:var(--okb);color:var(--ok)}.s-partial{background:var(--pab);color:var(--pa)}
.s-unknown,.s-not_available,.s-not_applicable{background:var(--unb);color:var(--un)}.s-failed{background:var(--fab);color:var(--fa)}
.chart{width:100%;height:auto}.grid{stroke:var(--line)}.ax{fill:var(--mut);font-size:10px}.gap{fill:var(--unb);stroke:var(--line);stroke-dasharray:2 2}.gapt{fill:var(--mut);font-size:11px}.inc{opacity:.45}
.legend{display:flex;gap:14px;flex-wrap:wrap;color:var(--mut);font-size:12px;margin-top:4px}.lg i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:5px;vertical-align:-1px}
.lgap{background:var(--unb)}.linc{background:var(--c1);opacity:.45}
.hmt td.hm{width:14px;min-width:14px;height:16px;padding:0;border:1px solid var(--card)}.hmh{font-weight:500;color:var(--ink);font-size:12px}.hmd{font-size:10px;color:var(--mut);padding:0}
.muted{color:var(--mut)}.foot{color:var(--mut);font-size:12px}
@media (max-width:600px){.kv{font-size:16px}}
"""


def render(d: dict) -> str:
    fr = {r["entity"]: r for r in d["freshness"]}
    today = d["freshness"][0]["as_of_date"]
    sw = d["sales_window"]
    end = d["sales_end"]
    P = []
    # 1
    fp = fr["fbo_postings"]
    sales_banner = (f'<div class="banner">Заказы полны по <b>{end}</b> (отставание {fp["days_since_last_complete"]} сут.). '
                    f'Сутки позже — «Нет данных (не загружено)», а не ноль. Сегодня ({today}): {chip(fp["as_of_status"])}</div>')
    P.append(f'''<section id="sales"><h2>1. Продажи и заказы</h2>{sales_banner}
<h3>28 суток по {end} (только полные сутки: {sw["complete_days"]} из {sw["days"]})</h3>
{kpis([("Заказано, ₽", num(sw["ordered_value_rub"]), "по цене отправления"), ("Заказано, шт.", num(sw["ordered_units"]), ""),
       ("Отменено, шт.", num(sw["cancelled_units"]), pct(sw["cancelled_units"], sw["ordered_units"]) + " заказанного"),
       ("Доставлено, шт.", num(sw["delivered_units"]), pct(sw["delivered_units"], sw["ordered_units"]) + " заказанного"),
       ("В пути, шт.", num(sw["in_progress_units"]), ""), ("Доставлено, ₽", num(sw["delivered_value_rub"]), "")])}
<h3>Заказано и доставлено по суткам заказа, ₽</h3>
{bars_svg(d["sales"], ["ordered_value_rub", "delivered_value_rub"], ["var(--c1)", "var(--c2)"], ["Заказано", "Доставлено"], until=today)}
<h3>Товары — 15 по стоимости заказанного (28 суток)</h3>
{table(["Артикул", "Товар", "Заказано, шт.", "Заказано, ₽", "Отменено", "Доля отмен", "Доставлено"],
       [[html.escape(r["offer_id"] or "—"), html.escape((r["ozon_name"] or "—")[:60]), num(r["ordered_units"]), num(r["ordered_value_rub"]),
         num(r["cancelled_units"]), pct(r["cancelled_units"], r["ordered_units"]), num(r["delivered_units"])] for r in d["sku"]], (2, 3, 4, 5, 6))}
<h3>Склады отгрузки — 10 крупнейших (28 суток)</h3>
{table(["Склад", "Отправлений", "Единиц", "Отменено", "Доставлено"],
       [[html.escape(r["warehouse_name"] or "—"), num(r["postings"]), num(r["units"]), num(r["cancelled"]), num(r["delivered"])] for r in d["warehouses"]], (1, 2, 3, 4))}
</section>''')
    # 2
    cv = d["cover"]
    c0 = cv[0] if cv else {}
    vst = c0.get("velocity_data_status")
    P.append(f'''<section id="inventory"><h2>2. Остатки</h2>
<div class="banner {"ok" if vst == "COMPLETE" else ""}">Остатки FBO на <b>{c0.get("snapshot_date")}</b> {chip(c0.get("stock_data_status"))}. Скорость — чистые заказы
(заказано − отменено) за <b>{c0.get("velocity_basis_from")} – {c0.get("velocity_basis_to")}</b>: база старше снимка на {c0.get("velocity_basis_lag_days")} сут. {chip(vst)}
{"— запас в днях оценён по устаревшей базе." if vst != "COMPLETE" else ""}</div>
{kpis([("Доступно, шт.", num(sum(float(r["available_units"] or 0) for r in cv)), "все склады FBO"),
       ("В пути, шт.", num(sum(float(r["transit_units"] or 0) for r in cv)), ""),
       ("Товаров < 14 дн. запаса", num(sum(1 for r in cv if r["days_of_cover"] is not None and float(r["days_of_cover"]) < 14)), ""),
       ("Товаров без остатка", num(sum(1 for r in cv if r["available_units"] is not None and float(r["available_units"]) == 0)), "но с заказами в базе")])}
<h3>Запас в днях — 20 товаров с наименьшим запасом</h3>
{table(["Артикул", "Товар", "Доступно", "В пути", "Кластеров", "Заказов/сут.", "Запас, дн.", "Скорость"],
       [[html.escape(r["offer_id"] or str(r["sku"])), html.escape((r["ozon_name"] or "—")[:55]), num(r["available_units"]), num(r["transit_units"]),
         num(r["clusters_with_stock"]), num(r["avg_daily_units"], 1), num(r["days_of_cover"]), chip(r["velocity_data_status"])] for r in cv[:20]], (2, 3, 4, 5, 6))}
<h3>Остаток по кластерам (12 крупнейших)</h3>
{table(["Кластер", "Доступно", "В пути"], [[html.escape(r["cluster_name"] or "—"), num(r["available"]), num(r["transit"])] for r in d["clusters"]], (1, 2))}
</section>''')
    # 3
    aw = d["ads_window"]
    ae = fr["ads_expense_daily"]
    asku = fr["ads_sku_daily"]
    drr_spend = d["ads_drr_spend"]["expense_rub"]
    P.append(f'''<section id="ads"><h2>3. Реклама</h2>
<div class="banner">Расходы кампаний полны по <b>{ae["last_complete_date"]}</b> (отставание {ae["days_since_last_complete"]} сут.). Расход по товарам: {chip(asku["last_status"])}
— ДРР по товарам не показывается до «Полные данные». Отчёт кампаний — атрибуция; списано Ozon — биллинг, их не складывают.</div>
<h3>28 суток по {d["ads_end"]}</h3>
{kpis([("Расход кампаний, ₽", num(aw["expense_rub"]), "отчёт Performance"), ("Показы", num(aw["impressions"]), ""), ("Клики", num(aw["clicks"]), "CTR " + pct(aw["clicks"], aw["impressions"])),
       ("CPC, ₽", num(float(aw["expense_rub"] or 0) / float(aw["clicks"]) if aw["clicks"] else None, 2), ""), ("Заказы по отчёту", num(aw["orders"]), ""),
       ("ACOS", pct(aw["expense_rub"], aw["revenue_rub"]), "расход / выручка по отчёту"),
       ("ДРР магазина", pct(drr_spend, aw["ordered_value_rub"]), f'по {aw["both_complete_days"]} суткам, где полны и заказы, и реклама'),
       ("Списано Ozon за продвижение, ₽", num(-float(d["billing"]["amount_rub"] or 0)), "биллинг из начислений, не атрибуция")])}
<h3>Расход кампаний по суткам, ₽</h3>
{bars_svg(d["ads_daily"], ["expense_rub"], ["var(--c3)"], ["Расход"], status_key="data_status", until=today)}
<h3>Кампании — 10 по расходу (28 суток)</h3>
{table(["Кампания", "Расход, ₽", "Показы", "Клики", "CTR", "CPC, ₽", "Заказы", "ACOS"],
       [[html.escape((r["campaign_title"] or "—")[:50]), num(r["expense_rub"]), num(r["impressions"]), num(r["clicks"]), pct(r["clicks"], r["impressions"]),
         num(float(r["expense_rub"] or 0) / float(r["clicks"]) if r["clicks"] and float(r["clicks"]) else None, 2), num(r["orders"]), pct(r["expense_rub"], r["revenue_rub"])]
        for r in d["campaigns"]], (1, 2, 3, 4, 5, 6, 7))}
<h3>Товары: атрибуция рекламы</h3>
{table(["Статус расхода по товарам", "Строк товар×сутки", "Атрибутировано, ₽"], [[chip(r["ads_data_status"]), num(r["rows_"]), num(r["spend"])] for r in d["sku_ads"]], (1, 2))}
</section>''')
    # 4
    fa = fr["finance_accrual"]
    basis_ru = {"SETTLED": "Рассчитано Ozon", "PARTIALLY_SETTLED": "Частично рассчитано", "NOT_SETTLED": "Не рассчитано"}
    P.append(f'''<section id="economics"><h2>4. Экономика SKU</h2>
<div class="banner">Предварительно. Начисления полны по <b>{fa["last_complete_date"]}</b>; за 30 суток без оценки {fa["unknown_days_30"]} сут.
Вклад до себестоимости (<b>PRE_COGS</b>, не прибыль) вычисляется только для суток с созревшими полными начислениями — сейчас таких нет, поэтому «—» = «не вычисляется», а не ноль.
Себестоимость клиентом не передана — вклад после себестоимости не показывается.</div>
<h3>Доставленные заказы за 60 суток по {end}, по степени расчёта Ozon</h3>
{table(["Расчёт Ozon", "Статус финансов", "Доставлено, шт.", "Выручка продавца, ₽", "Комиссия, ₽", "Логистика, ₽", "Эквайринг, ₽", "Вклад PRE_COGS, ₽"],
       [[basis_ru.get(r["revenue_basis"], r["revenue_basis"]), chip(r["finance_data_status"]), num(r["units"]), num(r["revenue"]), num(r["commission"]),
         num(r["logistics"]), num(r["acquiring"]), num(r["pre_cogs"]) if int(r["pre_cogs_null"]) == 0 else "— не вычисляется"] for r in d["econ_basis"]], (2, 3, 4, 5, 6, 7))}
<h3>Товары — рассчитанные Ozon отправления (60 суток), 12 по выручке</h3>
{table(["Артикул", "Товар", "Доставлено", "Выручка, ₽", "Комиссия, ₽", "Комиссия, %", "Логистика, ₽", "Логистика, %"],
       [[html.escape(r["offer_id"] or "—"), html.escape((r["ozon_name"] or "—")[:50]), num(r["units"]), num(r["revenue"]), num(r["commission"]),
         pct(-float(r["commission"] or 0), r["revenue"]), num(r["logistics"]), pct(-float(r["logistics"] or 0), r["revenue"])] for r in d["econ_sku"]], (2, 3, 4, 5, 6, 7))}
<h3>Начисления вне доставленных заказов (60 суток), по классу</h3>
{table(["Класс начисления", "Сумма, ₽", "Нераспознанных"], [[html.escape(r["accrual_class"]), num(r["amount_rub"]), num(r["unresolved"])] for r in d["costs"]], (1, 2))}
</section>''')
    # 5
    P.append(f'''<section id="coverage"><h2>5. Покрытие данных</h2>
<div class="banner un">Сегодня {today}: сутки, которые ещё не загружены (например, при остановленной загрузке), показаны как «Нет данных (не загружено)», а не нулём и не вчерашним статусом.</div>
{table(["Сущность", "Раздел", "Сегодня", "Последние оценённые", "Последние полные", "Отставание, сут.", "Устарело", "Полных за 30", "Неполных", "Без оценки"],
       [[html.escape(r["entity_title"]), html.escape(r["dashboard_section"]), chip(r["as_of_status"]), chip(r["last_status"]), r["last_complete_date"] or "—",
         num(r["days_since_last_complete"]), "да" if r["is_stale"] else "нет", num(r["complete_days_30"]), num(r["partial_days_30"]), num(r["unknown_days_30"])]
        for r in d["freshness"]], (5, 7, 8, 9))}
<h3>Статус по суткам, 45 суток (серый — нет оценки)</h3>
{heatmap(d["coverage"], d["freshness"], today)}
<div class="legend"><span class="lg"><i class="s-complete" style="background:var(--okb)"></i>Полные данные</span><span class="lg"><i style="background:var(--pab)"></i>Неполные</span><span class="lg"><i style="background:var(--unb)"></i>Нет данных</span><span class="lg"><i style="background:var(--fab)"></i>Ошибка</span></div>
</section>''')
    nav = "".join(f'<a href="#{p["id"]}">{html.escape(p["title"])}</a>' for p in BI.SPEC["pages"])
    return f'''<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Аналитика Ozon — предпросмотр</title><style>{CSS}</style></head><body>
<header><h1>Аналитика Ozon — предпросмотр клиентского отчёта</h1>
<div class="sub">Арендатор {html.escape(d["tenant"])} · источник: вью analytics_share из ветки (исполнены как SELECT, ничего не создано) · {d["generated_at"]}.
Внутренний файл владельца — не публиковать: содержит данные клиента.</div></header>
<nav><div>{nav}</div></nav><main>{"".join(P)}
<p class="foot">Правила: {" ".join(html.escape(r) for r in BI.SPEC["rules"])}</p></main></body></html>'''


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    out = Path(argv[1]).expanduser().resolve()
    if REPO in out.parents:
        print("Отказ: предпросмотр с данными клиента нельзя класть в репозиторий (он публичный).")
        return 2
    d = collect(argv[0])
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(d), encoding="utf-8")
    print(f"записан {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
