# EVETIS Management Dashboard — спецификация (сезон 2026/27)

Принцип: каждая страница отвечает на управленческий вопрос, а не показывает «все колонки». Источники — только валидированные MART/views (`wb_mart`, `ozon_raw` → будущий `ozon_mart`, `evetis_ref`); RAW в Looker не тянуть. Все деньги — в семантике проекта (WB: `V_WB_FINANCE_CANONICAL` / `V_ADS_SKU_ECONOMIC_LIMITS`; Ozon: полная цена = выручка + баллы + программы партнёров). Цветовые статусы: 🔴 RED overstock/liquidation/expiry · 🟡 YELLOW requires attention · 🟢 GREEN healthy · 🔵 BLUE replenish/growth.

Предлагаемый consumption-слой (не деплоить без отдельного разрешения): `wb_mart.V_DASH_SEASON_*` поверх существующих `V_DASH_*`, плюс три новые витрины: `V_INVENTORY_TRUTH_DAILY` (ФФ + WB + Ozon + транзит по базовым SKU, наборы разложены), `V_SEASON_PLAN` (план по SKU × месяц × канал — из `SKU_FORECAST_SEP_MAR.csv` как seed-таблицы), `V_SUPPLY_PLANNER` (цель FBO, отгрузка, дата stock-out).

---

## PAGE 1 — EXECUTIVE OVERVIEW

Вопрос: *как идёт сезон и что требует моего решения сегодня?*

**KPI-плитки (период: 7 дн / MTD / сезон с 01.09, сравнение с планом Balanced):**
Revenue (GMV) · Units sold (карточки и флаконы) · Contribution после рекламы · Contribution margin % · Advertising ₽ · DRR % · Inventory units (продаваемые, без LOST_CLAIMED) · Inventory value @COGS · Days of stock (портфель, по темпу 30 дн) · Cash converted this season (seller cash после площадок и рекламы, нарастающим) · Projected March residual (модель runout по текущему темпу).

**Блок ATTENTION REQUIRED (5–10 строк, генерируются правилами):**
1. SKU с днями запаса на площадке < 14 при спросе > 1/день → «поставить N».
2. SKU, у которых ДРР за 7 дн > экономический потолок (`max_ad_drr_breakeven`) → «снизить ставку/выключить».
3. Реализованная цена < 95 % прайса 7 дн подряд (акции) → «проверить автоакции».
4. Лимитирующий компонент наборов < 60 дней → «заказать производство» (сейчас: крем АКНЕ).
5. Крем для рук: темп < целевого (12/день) → «ускорить через Ozon/наборы».
6. Фулфилмент: наряд не исполнен / дефицит отгрузки / баланс < 0.
7. Партия 8: статус (в Китае / в пути / принята).
8. Приёмка WB FBO не подтверждена → «слот».

Источники: `V_DASH_KPI_DAILY`, `V_DASH_EXECUTIVE_ECONOMICS_DAILY`, `V_ADS_SKU_ECONOMIC_LIMITS`, `V_INVENTORY_TRUTH_DAILY`, `V_SUPPLY_PLANNER`.

## PAGE 2 — INVENTORY CONTROL TOWER

Вопрос: *где лежит товар и что с каждым SKU делать?*

Таблица по базовым SKU (наборы разложены, отдельный переключатель «показать наборы как физические позиции на площадках»):
`total_stock · ff_operational · ff_pallets · wb_fbo · ozon_fbo · in_transit · fbs · days_cover (площадка) · days_cover (всего) · forecast_30d · march_remaining (модель) · recommended_action`.

Статус по правилам: RED — покрытие > 180 дн или флаг expiry/liquidation (Cherry, Amber, тоники, пудра, крем для рук); YELLOW — 60–180 дн или дефицит на площадке при запасе на ФФ (сыворотка УВЛ сейчас); GREEN — 30–60; BLUE — < 30 дн и нет товара на ФФ (крем УВЛ до партии 8, крем АКНЕ с декабря).

Drill-down: SKU → точки хранения → история снимков (T5 / Ozon stocks / ФФ). Источники: `V_WB_STOCKS_T5_CURRENT` (живой агрегат отдельно от `LOST_CLAIMED`), `RAW_OZON_STOCKS` (последний snapshot), ФФ — пока seed-таблица из `INVENTORY_TRUTH.csv`, потом `usend_raw`.

## PAGE 3 — SALES & ECONOMICS

Вопрос: *что зарабатывает, а что съедает деньги?*

SKU-уровень, период 7/28/90 дн, разрез WB/Ozon: units · revenue (GMV) · seller cash · contribution до рекламы · реклама · contribution после рекламы · margin % · ASP факт vs прайс · marketplace expenses (комиссия, логистика, хранение, эквайринг) · тренд (спарклайн 12 недель).

Обязательные колонки-сигналы: `asp_vs_list_pct` (акции), `drr_vs_ceiling_pp`, `economic_state` (ABOVE_BREAKEVEN / BELOW / NEGATIVE_BEFORE_ADS / NO_AD_SPEND).

Источники: `V_ADS_SKU_ECONOMIC_LIMITS` (WB), `V_WB_SKU_FORWARD_ECONOMICS_CURRENT` (прайс-экономика), Ozon — постинги + начисления + `RAW_OZON_ADS_SKU_DAILY` (пометить «attributed only»).

## PAGE 4 — SEASON PLAN

Вопрос: *идём ли по плану?*

График 1: Actual vs Forecast (Balanced) vs Target (Aggressive) — карточки и GMV по неделям, сентябрь–март; вертикальные метки событий (11.11, BF 27.11, NY, 14.02, 23.02, 08.03).
График 2: Opening inventory → Sales → Remaining (водопад по месяцам; флаконы и ₽ @COGS), отдельно линия «партия 8».
Таблица: план/факт по SKU × месяц из `SKU_FORECAST_SEP_MAR.csv` (seed) против `FACT_ORDERS` + постинги Ozon.

## PAGE 5 — BUNDLES

Вопрос: *какие наборы масштабировать, какие выводят сток, какие убыточны?*

По каждому набору: продажи (WB/Ozon), ASP, contribution ₽/%, реклама, **underlying units consumed** (по BOM), **limiting component** и сколько наборов ещё можно собрать, **inventory release effect** (сколько флаконов Cherry/Amber/крема для рук ушло через наборы за период). Классы: HERO / INVENTORY-RELEASE / BLOCKED (нет компонента) / BAD (contribution < компонентов поштучно). Источники: `REF_BUNDLE_COMPONENTS`, `V_ADS_SKU_ECONOMIC_LIMITS`, `BUNDLE_PLAN.csv` (seed).

## PAGE 6 — MARKETPLACE COMPARISON

Вопрос: *где выгоднее продавать каждый SKU?*

WB vs Ozon по SKU: units · revenue · margin до/после рекламы · DRR · ASP · stock · contribution. **Не унифицировать цены**: у WB `price_with_disc` (до СПП), у Ozon полная цена с баллами; комиссии 42,25 % vs 52 %; показывать обе семантики рядом с пометкой. Сводный индикатор «предпочтительный канал» = где contribution ₽ на флакон выше при текущем ДРР (сейчас Ozon у тоников, пудры, Cherry/Amber, крема для рук; WB у крема УВЛ до повышения цены на Ozon).

## PAGE 7 — ADVERTISING ACTION CENTER

Вопрос: *что делать со ставками сегодня?*

Четыре группы по правилу `drr_7d` vs `economic ceiling` (`max_ad_drr_breakeven` из `V_ADS_SKU_ECONOMIC_LIMITS`, для цели 20 % — `max_drr_for_20pct`):
- **SCALE** — ДРР ≤ 60 % потолка и продажи растут;
- **KEEP** — 60–100 % потолка;
- **REDUCE** — 100–150 % потолка;
- **STOP / WASTE** — > 150 % потолка или NEGATIVE_BEFORE_ADS (сейчас: крем для рук WB, Amber, пудра, тоник УВЛ, набор тоник+сыв УВЛ).

Для кампании/запроса только: spend · attributed sales · DRR · economic DRR ceiling · gap to ceiling (п.п.) · action. Полная таблица запросов (`V_ADS_FUNNEL_QUERY_28D`) — только по клику. Ozon — отдельная вкладка с пометкой attributed_spend.

## PAGE 8 — SUPPLY PLANNER

Вопрос: *что отправить на этой неделе?*

По SKU × канал: current FBO · forecast demand 30 дн · target cover (45 дн) · planned shipment · FF reserve · expected stock-out date · статус слота (WB: NOT_PROVEN пока не подтверждён). Плюс блок «снять с паллет» и «собрать наборов» на неделю. Источник: `V_SUPPLY_PLANNER` (seed из `SUPPLY_PLAN_8W.csv` + `FBO_FBS_ALLOCATION.csv`), обновление ежедневно из остатков и темпа.

---

## AI-сводка (ежедневно, шаблон)

Каждое предложение строится из одной строки данных; порядок фиксирован: (1) продажи 7 дн vs 30 дн, (2) главный драйвер объёма и его экономика, (3) SKU с ближайшим stock-out на площадке и рекомендуемая поставка, (4) лимитирующий компонент наборов, (5) статус partий/инбаунда, (6) крем для рук: темп vs цель ликвидации, (7) реклама: объекты выше потолка с суммой потерь, (8) фулфилмент: исполнение наряда / дефицит / баланс. Прототип на данных 09.09.2026 — в `EXECUTIVE_SUMMARY.md`.
