-- ============================================================================
-- CONTROL TOWER PHASE 1 — REFERENCE / SEED TABLES (evetis_ref.CT_*)
-- Дата: 2026-09-10. База: HEAD 3755691e742b4058c7313f80b6a585be0413e2ab.
-- Контракт: docs/control_tower/CT_PHASE1_IMPLEMENTATION_2026-09-10.md
-- Откат:    tools/ct_phase1_rollback.sh [--dry-run]
--
-- ЧТО ЭТО. Owner-maintained справочники Control Tower: версионированный план
--   сезона, суточная кривая, OPEX, партии/сроки годности, срез остатков ФФ и
--   стартовый запас сезона, план сборки наборов, очередь действий владельца.
--   Всё — НОВЫЕ объекты с префиксом CT_. Ни один существующий объект
--   (FACT_*, MART_*, V_DASH_*, V_ADV_COSTS, Stage B, REF_*) не изменяется.
--
-- ИДЕМПОТЕНТНОСТЬ. CREATE TABLE IF NOT EXISTS: повторный прогон не переписывает
--   данные. Наполнение — отдельный файл 02_ct_seed_load.sql (MERGE / INSERT по
--   версии плана; старые версии никогда не удаляются).
--
-- ПОЧЕМУ evetis_ref, а не отдельный датасет. Здесь уже живут владельческие
--   справочники (REF_*), права Metabase-SA заданы на уровне проекта, новый
--   датасет потребовал бы проверки IAM (вне Phase 1). Вынос в evetis_ct —
--   кандидат Phase 2 по отдельному решению владельца.
-- ============================================================================

-- ── Версии плана ────────────────────────────────────────────────────────────
-- Grain: plan_version. Status: DRAFT | ACTIVE | SUPERSEDED. Ровно одна ACTIVE.
CREATE TABLE IF NOT EXISTS `project-fa311fc0-4d87-4781-986.evetis_ref.CT_PLAN_VERSION`
(
  plan_version            STRING    NOT NULL OPTIONS(description="Идентификатор версии, напр. SET_2026-09-09"),
  plan_name               STRING    NOT NULL OPTIONS(description="Человекочитаемое имя"),
  scenario_code           STRING    NOT NULL OPTIONS(description="SEASON_EXECUTION_TARGET | BALANCED | MARGIN_PROTECTION | AGGRESSIVE_CASH_RELEASE"),
  plan_status             STRING    NOT NULL OPTIONS(description="DRAFT | ACTIVE | SUPERSEDED — единственный авторитетный статус версии"),
  horizon_from            DATE      NOT NULL OPTIONS(description="Первый плановый день"),
  horizon_to              DATE      NOT NULL OPTIONS(description="Последний плановый день"),
  curve_version           STRING    NOT NULL OPTIONS(description="Версия суточной кривой (CT_DAILY_CURVE)"),
  assumption_version      STRING             OPTIONS(description="Версия допущений модели (assumptions_v2.json)"),
  source_artifact         STRING             OPTIONS(description="Файл-источник плана в репозитории"),
  source_commit           STRING             OPTIONS(description="Commit артефакта-источника"),
  target_cards_total      FLOAT64            OPTIONS(description="Контрольная сумма: карточки за горизонт"),
  target_units_total      FLOAT64            OPTIONS(description="Контрольная сумма: физические единицы за горизонт"),
  target_gmv_total        FLOAT64            OPTIONS(description="Контрольная сумма: GMV, ₽"),
  target_seller_cash_total FLOAT64           OPTIONS(description="Контрольная сумма: деньги продавца, ₽"),
  target_contribution_total FLOAT64          OPTIONS(description="Контрольная сумма: вклад до OPEX, ₽"),
  opening_inventory_as_of DATE               OPTIONS(description="Дата стартового запаса сезона"),
  opening_inventory_units INT64              OPTIONS(description="Стартовый продаваемый запас, флаконов"),
  opening_inventory_value_rub FLOAT64        OPTIONS(description="Стартовый запас по COGS, ₽"),
  created_at              TIMESTAMP NOT NULL OPTIONS(description="Момент загрузки"),
  created_by              STRING             OPTIONS(description="Кто загрузил"),
  superseded_at           TIMESTAMP          OPTIONS(description="Когда версия перестала быть ACTIVE"),
  notes                   STRING
)
OPTIONS(description="Control Tower: реестр версий плана сезона. Иммутабельно: новая корректировка = новая версия, старые не удаляются.");

-- ── Месячный план (seed из SET_DETAIL.csv) ───────────────────────────────────
-- Grain: plan_version × month × marketplace × internal_sku (карточка = SKU продажи).
CREATE TABLE IF NOT EXISTS `project-fa311fc0-4d87-4781-986.evetis_ref.CT_SEASON_PLAN_MONTHLY`
(
  plan_version            STRING  NOT NULL,
  month                   DATE    NOT NULL OPTIONS(description="Первое число месяца"),
  marketplace             STRING  NOT NULL OPTIONS(description="WB | OZON"),
  internal_sku            STRING  NOT NULL OPTIONS(description="SKU карточки (базовый или набор)"),
  sales_mode              STRING  NOT NULL OPTIONS(description="SOLO | BUNDLE"),
  target_cards            FLOAT64 NOT NULL,
  target_physical_units   FLOAT64 NOT NULL OPTIONS(description="cards × число компонентов по BOM"),
  target_price            FLOAT64,
  target_drr_pct          FLOAT64,
  target_gmv              FLOAT64,
  target_marketplace_costs FLOAT64,
  target_ad_spend         FLOAT64,
  target_seller_cash      FLOAT64 OPTIONS(description="GMV − расходы площадки − реклама"),
  target_cogs             FLOAT64,
  target_contribution     FLOAT64 OPTIONS(description="seller_cash − COGS; НЕ прибыль (до OPEX)"),
  source_artifact         STRING,
  source_row_hash         STRING,
  created_at              TIMESTAMP NOT NULL
)
OPTIONS(description="Control Tower: месячный план по SKU × канал (seed из docs/analysis/season_2026_2027/control_tower/SET_DETAIL.csv). Раскладывается по дням кривой CT_DAILY_CURVE в CT_SEASON_PLAN.");

-- ── Суточная кривая ─────────────────────────────────────────────────────────
-- Grain: curve_version × marketplace × plan_date. Вес = dow_factor × event_factor.
CREATE TABLE IF NOT EXISTS `project-fa311fc0-4d87-4781-986.evetis_ref.CT_DAILY_CURVE`
(
  curve_version  STRING  NOT NULL,
  marketplace    STRING  NOT NULL,
  plan_date      DATE    NOT NULL,
  dow_factor     FLOAT64 NOT NULL OPTIONS(description="Эффект дня недели (WB — замер FACT_ORDERS 13.04–06.09.2026)"),
  event_factor   FLOAT64 NOT NULL OPTIONS(description="Промо-окно: 11.11, Black Friday, пик декабря, НГ, 14/23 февраля, 8 марта"),
  event_label    STRING,
  raw_weight     FLOAT64 NOT NULL OPTIONS(description="dow_factor × event_factor; нормируется внутри месяца при раскладке"),
  created_at     TIMESTAMP NOT NULL
)
OPTIONS(description="Control Tower: суточная кривая распределения месячного плана. Дашборд читает date × target, а не делит месяц на дни.");

-- ── Суточный план (материализован из месячного × кривая) ────────────────────
-- Grain: plan_version × plan_date × marketplace × internal_sku × sales_mode.
CREATE TABLE IF NOT EXISTS `project-fa311fc0-4d87-4781-986.evetis_ref.CT_SEASON_PLAN`
(
  plan_version            STRING  NOT NULL,
  plan_status             STRING  NOT NULL OPTIONS(description="Статус на момент загрузки; авторитетный статус — CT_PLAN_VERSION.plan_status"),
  plan_date               DATE    NOT NULL,
  month                   DATE    NOT NULL,
  marketplace             STRING  NOT NULL,
  internal_sku            STRING  NOT NULL,
  product_name            STRING,
  sales_mode              STRING  NOT NULL,
  bundle_id               STRING           OPTIONS(description="= internal_sku набора для BUNDLE, NULL для SOLO"),
  component_count         INT64   NOT NULL OPTIONS(description="Число физических единиц в одной карточке"),
  target_cards            FLOAT64 NOT NULL,
  target_physical_units   FLOAT64 NOT NULL,
  target_gmv              FLOAT64,
  target_marketplace_costs FLOAT64,
  target_ad_spend         FLOAT64,
  target_seller_cash      FLOAT64,
  target_cogs             FLOAT64,
  target_contribution     FLOAT64,
  day_weight              FLOAT64 NOT NULL OPTIONS(description="Доля дня в месяце (сумма по месяцу × канал = 1)"),
  curve_version           STRING  NOT NULL,
  created_at              TIMESTAMP NOT NULL,
  source_artifact         STRING,
  assumption_version      STRING
)
PARTITION BY plan_date
CLUSTER BY plan_version, marketplace, internal_sku
OPTIONS(description="Control Tower: суточный план сезона (версионирован). Grain plan_version × date × marketplace × sku × sales_mode.");

-- ── OPEX ────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS `project-fa311fc0-4d87-4781-986.evetis_ref.CT_OPEX`
(
  opex_version   STRING  NOT NULL,
  month          DATE    NOT NULL,
  cost_item      STRING  NOT NULL OPTIONS(description="FULFILLMENT | ACCOUNTING | DESIGNER | ..."),
  amount_rub     FLOAT64 NOT NULL,
  is_active      BOOL    NOT NULL,
  source         STRING,
  note           STRING,
  created_at     TIMESTAMP NOT NULL
)
OPTIONS(description="Control Tower: OPEX по месяцам (факт владельца 09.09.2026: ФФ 45 000 + бухгалтер 20 000 + дизайнер 12 000 до февраля 2027; далее 65 000). Вычитается из вклада → операционный результат.");

-- ── Партии и сроки годности ────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS `project-fa311fc0-4d87-4781-986.evetis_ref.CT_EXPIRY_BATCH`
(
  internal_sku        STRING NOT NULL,
  batch_id            STRING NOT NULL OPTIONS(description="REF_COST_BATCH.batch_id или BATCH-08 (inbound)"),
  batch_number        STRING,
  import_date         DATE,
  manufacture_date    DATE,
  shelf_life_months   INT64  NOT NULL,
  expiry_date         DATE   NOT NULL,
  expiry_source       STRING NOT NULL OPTIONS(description="OWNER_FACT_HARD | INFERENCE_IMPORT_MINUS_70D | OWNER_FACT_SHELF_LIFE"),
  expiry_confidence   STRING NOT NULL OPTIONS(description="HIGH | MEDIUM"),
  units_at_snapshot   INT64,
  snapshot_date       DATE,
  is_inbound          BOOL   NOT NULL,
  note                STRING,
  source_artifact     STRING,
  created_at          TIMESTAMP NOT NULL
)
OPTIONS(description="Control Tower: сроки годности по SKU/партии. Крем для рук — HARD 31.12.2026 (факт владельца); остальные партии — дата производства = импорт − 70 дн + 2 года (inference), партия 8 — +3 года.");

-- ── Срез остатков (ФФ — owner-authoritative) и стартовый запас сезона ──────
CREATE TABLE IF NOT EXISTS `project-fa311fc0-4d87-4781-986.evetis_ref.CT_STOCK_SNAPSHOT`
(
  snapshot_date            DATE   NOT NULL,
  internal_sku             STRING NOT NULL,
  ff_operational_units     INT64  NOT NULL OPTIONS(description="Оперативная полка фулфилмента (Usend)"),
  ff_pallet_units          INT64  NOT NULL OPTIONS(description="Длительное хранение (паллеты)"),
  in_transit_to_ozon_units INT64  NOT NULL OPTIONS(description="Отгружено с ФФ, ещё не принято Ozon"),
  wb_fbo_live_units        INT64           OPTIONS(description="Копия живого агрегата WB на дату среза (для baseline)"),
  ozon_fbo_units           INT64           OPTIONS(description="Копия FBO Ozon на дату среза (для baseline)"),
  fbs_units                INT64           OPTIONS(description="FBS-резерв (не запущен = 0)"),
  inbound_units            INT64  NOT NULL OPTIONS(description="В производстве/в пути из Китая — НЕ продаваемый запас"),
  inbound_status           STRING,
  inbound_eta              DATE,
  total_sellable_units     INT64  NOT NULL OPTIONS(description="ff_operational + ff_pallet + in_transit + wb_live + ozon + fbs"),
  unit_cogs_rub            FLOAT64,
  sellable_value_rub       FLOAT64,
  is_season_opening        BOOL   NOT NULL OPTIONS(description="TRUE — этот срез является стартовым запасом сезона"),
  source                   STRING NOT NULL OPTIONS(description="OWNER_FACT | USEND_LK | RECONSTRUCTED"),
  source_artifact          STRING,
  loaded_at                TIMESTAMP NOT NULL,
  note                     STRING
)
OPTIONS(description="Control Tower: срезы остатков по базовым SKU. Единственный источник остатков фулфилмента (production-источника ФФ нет). WB/Ozon в live-view берутся из production, здесь — копия на дату среза для baseline сезона.");

-- ── План сборки наборов ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS `project-fa311fc0-4d87-4781-986.evetis_ref.CT_BUNDLE_PLAN`
(
  plan_version         STRING NOT NULL,
  week_no              INT64  NOT NULL,
  week_start           DATE   NOT NULL,
  bundle_internal_sku  STRING NOT NULL,
  qty_assemble         INT64  NOT NULL,
  units_consumed       INT64  NOT NULL,
  sell_target_wb       INT64,
  sell_target_ozon     INT64,
  limiting_component   STRING,
  note                 STRING,
  source_artifact      STRING,
  created_at           TIMESTAMP NOT NULL
)
OPTIONS(description="Control Tower: план сборки наборов на ФФ по неделям (seed из BUNDLE_PRODUCTION_PLAN.csv; сборка раз в две недели = 2-недельный спрос × 1,2).");

-- ── Очередь действий владельца ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS `project-fa311fc0-4d87-4781-986.evetis_ref.CT_OWNER_ACTION_QUEUE`
(
  action_id             STRING    NOT NULL OPTIONS(description="CT-<seq>: стабильный ключ действия"),
  dedup_key             STRING    NOT NULL OPTIONS(description="action_type|marketplace|sku|bundle_id|reason_code — идемпотентность генератора"),
  generated_at          TIMESTAMP NOT NULL,
  generated_by          STRING    NOT NULL OPTIONS(description="SEED:<artifact> | RULE:<rule_code>"),
  action_date           DATE      NOT NULL,
  priority              STRING    NOT NULL OPTIONS(description="P0 TODAY | P1 THIS WEEK | P2 NEXT 2 WEEKS | P3 THIS MONTH | P4 WATCH"),
  deadline              DATE,
  action_type           STRING    NOT NULL OPTIONS(description="REPLENISH_WB | REPLENISH_OZON | PULL_FROM_PALLETS | ASSEMBLE_BUNDLES | LIQUIDATION | ADS_REVIEW | PRICE_ACTION | SALES_INVESTIGATION | DECISION | PROCUREMENT | FF_REQUEST | WATCH"),
  marketplace           STRING,
  sku                   STRING,
  bundle_id             STRING,
  qty                   FLOAT64,
  financial_effect_rub  FLOAT64,
  reason_code           STRING    NOT NULL,
  reason_text           STRING,
  source_metric         STRING,
  status                STRING    NOT NULL OPTIONS(description="OPEN | IN_PROGRESS | DONE | CANCELLED | SUPERSEDED"),
  status_updated_at     TIMESTAMP,
  completed_at          TIMESTAMP,
  owner_note            STRING,
  plan_version          STRING,
  first_seen_at         TIMESTAMP NOT NULL,
  last_seen_at          TIMESTAMP NOT NULL,
  times_seen            INT64     NOT NULL
)
OPTIONS(description="Control Tower: персистентная очередь действий владельца. Дедуп по dedup_key: повторный прогон генератора обновляет существующую OPEN/IN_PROGRESS строку, а не создаёт дубль.");

-- ── Журнал смены статусов (append-only) ─────────────────────────────────────
CREATE TABLE IF NOT EXISTS `project-fa311fc0-4d87-4781-986.evetis_ref.CT_ACTION_STATUS_LOG`
(
  action_id     STRING    NOT NULL,
  changed_at    TIMESTAMP NOT NULL,
  old_status    STRING,
  new_status    STRING    NOT NULL,
  owner_note    STRING,
  changed_by    STRING    NOT NULL
)
OPTIONS(description="Control Tower: журнал изменений статусов действий (append-only). Пишется только процедурой evetis_ref.sp_ct_action_update.");

-- ── Материализованный факт продаж ───────────────────────────────────────────────────
-- Purpose : снять с дашбордов пересчёт тяжёлой LIVE-логики (WB + Ozon за всю историю).
-- Grain   : date × marketplace × internal_sku × sales_mode.
-- Source  : wb_mart.V_CT_ACTUAL_DAILY_LIVE (production-семантика без изменений).
-- Owner   : Control Tower. Update: полный перестрой в sp_ct_refresh_daily() (TRUNCATE+INSERT).
-- Rollback: DROP TABLE — существующая аналитика не затрагивается.
CREATE TABLE IF NOT EXISTS `project-fa311fc0-4d87-4781-986.evetis_ref.CT_ACTUAL_DAILY`
(
  d DATE NOT NULL, marketplace STRING NOT NULL, internal_sku STRING NOT NULL, sales_mode STRING NOT NULL, component_count INT64,
  cards_ordered INT64, cards_cancelled INT64, cards_sold INT64, cards_returned INT64, units_ordered INT64, units_sold INT64,
  gmv_ordered NUMERIC, revenue_seller_base NUMERIC, marketplace_costs NUMERIC, ad_spend NUMERIC, seller_cash NUMERIC, cogs NUMERIC, contribution NUMERIC,
  contribution_covered BOOL, cogs_covered BOOL, include_in_pnl BOOL, economics_basis STRING,
  refreshed_at TIMESTAMP NOT NULL
)
PARTITION BY d
CLUSTER BY marketplace, internal_sku
OPTIONS (description = 'Control Tower: materialised daily actuals (WB + Ozon, card + physical-unit grain, production economics semantics). Full rebuild by evetis_ref.sp_ct_refresh_daily() from wb_mart.V_CT_ACTUAL_DAILY_LIVE. Additive CT object; rollback = DROP TABLE.');

-- ── Дневной срез правды о запасе ────────────────────────────────────────────────────
-- Purpose : зафиксировать остаток на каждый день (история запаса + стабильность дашборда).
-- Grain   : snapshot_date × internal_sku (компонентные SKU).
-- Source  : wb_mart.V_CT_INVENTORY_TRUTH_LIVE (ФФ-срез владельца + живые остатки площадок).
-- Owner   : Control Tower. Update: замена партиции текущего дня в sp_ct_refresh_daily().
-- Rollback: DROP TABLE.
-- Схема наследуется от LIVE-витрины, поэтому таблица создаётся через CTAS в первом прогоне:
--   CREATE TABLE IF NOT EXISTS ... PARTITION BY snapshot_date CLUSTER BY internal_sku AS
--   SELECT CURRENT_DATE() AS snapshot_date, CURRENT_TIMESTAMP() AS snapshot_ts, v.*
--   FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_CT_INVENTORY_TRUTH_LIVE` v;

-- ── Журнал обновлений Control Tower ─────────────────────────────────────────────────
-- Purpose : наблюдаемость ежедневного прогона; источник домена CT_* в витрине свежести.
-- Grain   : run_id × step.
-- Owner   : Control Tower. Update: пишут sp_ct_refresh_daily и sp_ct_generate_actions.
-- Rollback: DROP TABLE.
CREATE TABLE IF NOT EXISTS `project-fa311fc0-4d87-4781-986.evetis_ref.CT_REFRESH_LOG`
(
  run_id STRING NOT NULL, run_ts TIMESTAMP NOT NULL, step STRING NOT NULL, status STRING NOT NULL,
  rows_affected INT64, message STRING, duration_ms INT64
)
PARTITION BY DATE(run_ts)
OPTIONS (description = 'Control Tower: refresh/run log written by sp_ct_refresh_daily / sp_ct_generate_actions. Source of the CT_REFRESH freshness domain. Additive CT object; rollback = DROP TABLE.');
