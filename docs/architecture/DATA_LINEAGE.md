# Происхождение данных (lineage)

<!-- СГЕНЕРИРОВАНО tools/render_architecture_docs.py из docs/architecture/system_inventory.json. Правки в этом файле будут затёрты: меняй генератор или снимок. -->

**Снимок:** 2026-09-22T04:35:09Z. Рёбра выведены из тел вью в `INFORMATION_SCHEMA.VIEWS`, а не из имён файлов: это то, что BigQuery исполняет.

Обозначения: `T` — таблица (носитель данных), `V` — вью (вычисление поверх других объектов). Стрелка читается «зависит от».

## Источники и потребление

Наиболее читаемые объекты платформы — те, чья поломка распространяется дальше всего.

| Объект | Тип | Прямых потребителей | Транзитивно |
|---|---|---:|---:|
| `wb_raw.REF_SKU_MASTER` | V | 19 | 37 |
| `evetis_ref.REF_SKU_CHANNEL_MAP` | T | 15 | 43 |
| `evetis_ref.REF_PRODUCT_MASTER` | T | 13 | 44 |
| `ozon_raw.RAW_OZON_POSTINGS_FBO` | T | 12 | 24 |
| `evetis_ref.V_PRODUCT_COGS_EFFECTIVE` | V | 11 | 27 |
| `wb_mart.MART_SKU_DAILY` | T | 10 | 28 |
| `evetis_ops.V_CT_STOCK_BALANCE` | V | 9 | 17 |
| `ozon_raw.RAW_OZON_FINANCE_ACCRUAL` | T | 9 | 16 |
| `wb_mart.V_CT_PLAN_ACTIVE` | V | 9 | 23 |
| `wb_mart.FACT_ORDERS` | T | 8 | 31 |
| `wb_mart.FACT_FINANCE` | T | 7 | 30 |
| `wb_mart.V_CT_ACTUAL_DAILY` | V | 7 | 23 |
| `wb_mart.V_CT_BOM_CURRENT` | V | 7 | 13 |
| `wb_mart.V_CT_INVENTORY_TRUTH` | V | 7 | 13 |
| `evetis_ops.V_OPS_BOM` | V | 6 | 19 |
| `wb_mart.V_UNITKA_LAST_CLOSED_DATE` | V | 6 | 9 |
| `wb_raw.V_WB_FINANCE_CANONICAL` | V | 6 | 30 |
| `wb_raw.V_WB_STORAGE_DAILY` | V | 6 | 13 |
| `evetis_ops.V_CT_OZON_ORDER_RECON` | V | 5 | 16 |
| `evetis_ref.CT_STOCK_SNAPSHOT` | T | 5 | 10 |
| `ozon_raw.RAW_OZON_ADS_SKU_DAILY` | T | 5 | 10 |
| `ozon_raw.RAW_OZON_STOCKS` | T | 5 | 20 |
| `wb_mart.FACT_STOCKS_SNAPSHOT` | T | 5 | 31 |
| `wb_raw.V_WB_FINANCE_SEMANTIC` | V | 5 | 16 |
| `wb_raw.V_WB_FUNNEL_DAILY` | V | 5 | 12 |

## Самые длинные цепочки вычисления

Глубина — число уровней вью между объектом и ближайшей таблицей. Длинная цепочка означает, что причина неверного числа на дашборде может лежать на много уровней ниже.

| Глубина | Объект |
|---:|---|
| 10 | `wb_mart.V_CT_DAILY_BRIEF` |
| 9 | `wb_mart.V_CT_DAILY_BRIEF_LINES` |
| 8 | `evetis_ops.V_OPS_SHEET_BOM` |
| 8 | `wb_mart.V_CT_OWNER_HOME` |
| 7 | `evetis_ops.V_OPS_BUNDLE_BOM_EXPANSION` |
| 7 | `evetis_ops.V_OPS_SHEET_BUNDLES` |
| 7 | `evetis_ops.V_OPS_SHEET_FF_TASK` |
| 7 | `wb_mart.V_CT_ACTION_CANDIDATES` |
| 7 | `wb_mart.V_CT_ATTENTION` |
| 7 | `wb_mart.V_DASH_EXECUTIVE_ECONOMICS_DAILY` |
| 7 | `wb_mart.V_UNITKA_RECON_INTEGRITY` |
| 6 | `evetis_ops.V_OPS_BUNDLE_PRODUCTION` |
| 6 | `evetis_ops.V_OPS_FF_TASK` |
| 6 | `evetis_ops.V_OPS_SHEET_FF_STOCK` |
| 6 | `evetis_ops.V_OPS_SHEET_PICK` |

## Цепочки по доменам

### Wildberries

Конечных объектов (никто не читает изнутри BigQuery — это выход к Metabase, листам или агентам, либо мёртвый объект): **43**.

- `wb_mart.V_ADS_CAMPAIGN_DAILY` ← `wb_raw.V_ADV_CAMPAIGN_CONFIG_DAILY`, `wb_raw.V_ADV_CAMPAIGN_STATS`
- `wb_mart.V_ADS_SCREEN_QUERY` ← `wb_mart.V_ADS_FUNNEL_QUERY_28D`, `wb_mart.V_ADS_FUNNEL_QUERY_90D`, `wb_mart.V_ADS_FUNNEL_SIGNALS`, `wb_raw.REF_SKU_MASTER`
- `wb_mart.V_ADS_SCREEN_SKU` ← `wb_mart.V_ADS_FUNNEL_SKU_28D`, `wb_raw.REF_SKU_MASTER`
- `wb_mart.V_CT_ACTION_CANDIDATES` ← `evetis_ref.CT_PLAN_VERSION`, `wb_mart.V_ADS_SKU_ECONOMIC_LIMITS`, `wb_mart.V_CT_BUNDLE_STATUS`, `wb_mart.V_CT_FRESHNESS`, `wb_mart.V_CT_INVENTORY_TRUTH`, `wb_mart.V_CT_PLAN_VS_ACTUAL_DAILY`, `wb_mart.V_CT_SUPPLY_NEED`
- `wb_mart.V_CT_ACTUAL_DAILY_LIVE` ← `evetis_ref.REF_SKU_CHANNEL_MAP`, `evetis_ref.V_PRODUCT_COGS_EFFECTIVE`, `ozon_raw.RAW_OZON_ADS_SKU_DAILY`, `ozon_raw.RAW_OZON_FINANCE_ACCRUAL`, `ozon_raw.RAW_OZON_POSTINGS_FBO`, `wb_mart.V_CT_BOM_CURRENT`, `wb_mart.V_DASH_SKU_DAILY`, `wb_mart.V_MART_SKU_DAILY_COGS`
- `wb_mart.V_CT_DAILY_BRIEF` ← `wb_mart.V_CT_DAILY_BRIEF_LINES`
- `wb_mart.V_CT_INVENTORY_TRUTH_LIVE` ← `evetis_ref.CT_EXPIRY_BATCH`, `evetis_ref.CT_STOCK_SNAPSHOT`, `evetis_ref.REF_PRODUCT_MASTER`, `evetis_ref.REF_SKU_CHANNEL_MAP`, `evetis_ref.V_PRODUCT_COGS_EFFECTIVE`, `ozon_raw.RAW_OZON_STOCKS`, `wb_mart.V_CT_BOM_CURRENT`, `wb_mart.V_CT_PHYSICAL_DAILY`, `wb_mart.V_CT_PLAN_ACTIVE`, `wb_mart.V_CT_SUPPLY_FLOW`, `wb_raw.V_WB_STOCKS_T5_CURRENT`
- `wb_mart.V_CT_SKU_CONTROL` ← `evetis_ref.REF_PRODUCT_MASTER`, `wb_mart.V_CT_BUNDLE_STATUS`, `wb_mart.V_CT_PLAN_VS_ACTUAL_DAILY`, `wb_mart.V_CT_SUPPLY_NEED`
- `wb_mart.V_DASH_BUYOUT_COHORT_DAILY` ← `wb_mart.FACT_ORDERS`, `wb_mart.V_DASH_KPI_DAILY`, `wb_raw.REF_SKU_MASTER`, `wb_raw.V_WB_SALES_RETURNS`
- `wb_mart.V_DASH_EXECUTIVE_BREAKDOWN_DAILY` ← `wb_mart.V_DASH_KPI_DAILY`, `wb_mart.V_WB_FINANCE_AMOUNTS_LONG_MAPPED`, `wb_mart.V_WB_FINANCE_PRICE_COMPONENTS`, `wb_raw.REF_SKU_MASTER`, `wb_raw.V_WB_FINANCE_SEMANTIC`
- `wb_mart.V_DASH_EXECUTIVE_ECONOMICS_DAILY` ← `wb_mart.V_DASH_FINANCE_CORRECTED_DAILY`, `wb_mart.V_MART_SKU_DAILY_COGS`
- `wb_mart.V_DASH_FRESHNESS_BY_CONTRACT` ← `wb_mart.V_DATA_FRESHNESS`
- `wb_mart.V_DASH_FRESHNESS_HEADER` ← `wb_mart.MART_SKU_DAILY`, `wb_mart.V_DATA_FRESHNESS`
- `wb_mart.V_DASH_SETTLEMENT_DAILY` ← `wb_mart.V_DASH_KPI_DAILY`, `wb_mart.V_WB_DEDUCTIONS_CLASSIFIED`, `wb_mart.V_WB_FINANCE_PRICE_COMPONENTS`, `wb_raw.V_WB_FINANCE_SEMANTIC`
- `wb_mart.V_DASH_SKU_PERFORMANCE_V2_DAILY` ← `wb_mart.SKU_PERFORMANCE_V2_BUILD_LOG`, `wb_mart.SKU_PERFORMANCE_V2_DAILY`
- `wb_mart.V_MART_RUN_LOG` ← `wb_mart.MART_RUNS`, `wb_raw.LOADER_RUNS`
- `wb_mart.V_SKU_PERFORMANCE_V2_EXEC_BRIDGE_DAILY` ← `wb_mart.SKU_PERFORMANCE_V2_DAILY`, `wb_mart.V_DASH_EXECUTIVE_V2_DAILY`
- `wb_mart.V_UNITKA_COGS_CANONICAL` ← `wb_mart.UNITKA_COGS_EFFECTIVE`, `wb_mart.V_UNITKA_LAST_CLOSED_DATE`, `wb_raw.REF_SKU_MASTER`
- `wb_mart.V_UNITKA_COMMISSION_RATES` ← `wb_mart.V_UNITKA_LAST_CLOSED_DATE`, `wb_raw.V_WB_FINANCE_CANONICAL`
- `wb_mart.V_UNITKA_INTEGRITY` ← `wb_mart.FACT_ORDERS`, `wb_mart.V_UNITKA_DAILY_FACT`, `wb_mart.V_UNITKA_LAST_CLOSED_DATE`, `wb_raw.RAW_WB_FUNNEL_XLSX_BACKFILL`, `wb_raw.RAW_WB_PRICES`, `wb_raw.REF_SKU_MASTER`, `wb_raw.V_WB_FUNNEL_DAILY`, `wb_raw.V_WB_STORAGE_DAILY`
- `wb_mart.V_UNITKA_INTEGRITY_STATUS` ← `wb_ops.UNITKA_INTEGRITY_ISSUES`, `wb_ops.UNITKA_REPAIR_LEDGER`
- `wb_mart.V_UNITKA_LOGISTICS_RATES` ← `wb_mart.V_UNITKA_LAST_CLOSED_DATE`, `wb_raw.V_WB_FINANCE_CANONICAL`
- `wb_mart.V_UNITKA_RECON_COGS_CANONICAL` ← `wb_mart.UNITKA_COGS_EFFECTIVE`, `wb_mart.V_UNITKA_RECON_WINDOW`, `wb_raw.REF_SKU_MASTER`
- `wb_mart.V_UNITKA_RECON_INTEGRITY` ← `wb_mart.FACT_ORDERS`, `wb_mart.V_UNITKA_RECON_FACT`, `wb_mart.V_UNITKA_RECON_WINDOW`, `wb_raw.RAW_WB_PRICES`, `wb_raw.REF_SKU_MASTER`
- `wb_mart.V_WB_PRICING_ECONOMICS_HEALTH` ← `wb_mart.V_WB_SKU_FORWARD_ECONOMICS_CURRENT`, `wb_raw.V_WB_TARIFFS_CURRENT`, `wb_raw.WB_TARIFF_OBSERVATIONS`
- `wb_mart.V_WB_SUPPLIES_INTAKE_BY_SKU` ← `wb_mart.V_WB_SUPPLIES_DETAIL`
- `wb_ops.V_OPS_ACTIVE_INCIDENTS` ← `wb_ops.OPS_ALERT_EVENT`, `wb_ops.OPS_INCIDENT`
- `wb_ops.V_OPS_CURRENT_HEALTH` ← `wb_ops.OPS_HEALTH_STATE`, `wb_ops.OPS_INCIDENT`
- `wb_raw.V_ADV_BOOSTER_STATS` ← `wb_raw.RAW_WB_ADV_BOOSTER_STATS`
- `wb_raw.V_ADV_CAMPAIGNS` ← `wb_raw.RAW_WB_ADV_CAMPAIGNS`
- `wb_raw.V_ADV_COSTS_UNION_LEGACY` ← `wb_raw.RAW_WB_ADV_COSTS`
- `wb_raw.V_ADV_COSTS_UNION_PREBOOTSTRAP` ← `wb_raw.RAW_WB_ADV_COSTS`
- `wb_raw.V_ADV_QUERY_STATS_COVERAGE` ← `wb_raw.RAW_WB_ADV_QUERY_STATS_RUNS`, `wb_raw.V_ADV_CAMPAIGN_STATS`
- `wb_raw.V_ADV_SEARCH_CLUSTERS` ← `wb_raw.RAW_WB_ADV_SEARCH_CLUSTERS`
- `wb_raw.V_INGEST_HEARTBEAT` ← `wb_raw.INGEST_RUNS`, `wb_raw.LOADER_RUNS`
- `wb_raw.V_WB_FINANCE` ← `wb_raw.V_WB_FINANCE_COMPLETE`
- `wb_raw.V_WB_FUNNEL_COVERAGE` ← `wb_raw.RAW_WB_FUNNEL_DAILY`, `wb_raw.V_WB_FUNNEL_DAILY`
- `wb_raw.V_WB_ORDERS` ← `wb_raw.RAW_WB_ORDERS`
- `wb_raw.V_WB_PRICES_OBSERVED_CHANGES` ← `wb_raw.RAW_WB_PRICES`
- `wb_raw.V_WB_PRICES_OBSERVER_HEALTH` ← `wb_raw.WB_PRICES_OBSERVATIONS`
- `wb_raw.V_WB_STOCKS_CURRENT` ← `wb_raw.RAW_WB_STOCKS`, `wb_raw.WB_STOCKS_SNAPSHOTS`
- `wb_raw.V_WB_STORAGE_COVERAGE` ← `wb_raw.RAW_WB_PAID_STORAGE`, `wb_raw.V_WB_STORAGE_DAILY`
- `wb_raw.V_WB_STORAGE_RECONCILIATION` ← `wb_raw.V_WB_FINANCE_CANONICAL`, `wb_raw.V_WB_STORAGE_DAILY`

### Ozon

Конечных объектов (никто не читает изнутри BigQuery — это выход к Metabase, листам или агентам, либо мёртвый объект): **7**.

- `ozon_mart.V_OZON_AGENT_DECISION_INPUT` ← `ozon_mart.FCT_OZON_SKU_PNL_MONTHLY`, `ozon_mart.V_OZON_SKU_FORWARD_ECONOMICS_CURRENT`, `ozon_mart.V_OZON_TARIFF_SOURCE_HEALTH`
- `ozon_mart.V_OZON_COMMISSION_RECOVERY` ← _нет зависимостей_
- `ozon_mart.V_OZON_LIFETIME_PNL` ← `ozon_mart.FCT_OZON_PNL_MONTHLY`, `ozon_mart.FCT_OZON_SKU_PNL_MONTHLY`, `ozon_mart.V_OZON_MART_FRESHNESS`, `ozon_raw.RAW_OZON_ADS_EXPENSE_DAILY`
- `ozon_mart.V_OZON_SKU_FBO_FBS_COMPARISON_CURRENT` ← `ozon_mart.V_OZON_SKU_CURRENT_TARIFF`, `ozon_mart.V_OZON_SKU_FORWARD_ECONOMICS_CURRENT`
- `ozon_mart.V_OZON_SKU_PNL_DAILY_OPERATIONAL` ← `evetis_ref.REF_SKU_CHANNEL_MAP`, `evetis_ref.V_PRODUCT_COGS_EFFECTIVE`, `ozon_mart.FCT_OZON_SKU_PNL_DAILY`, `ozon_mart.V_OZON_CIS_BUYOUT`, `ozon_mart.V_OZON_COMMISSION_POLICY`, `ozon_mart.V_OZON_LOGISTICS_ESTIMATOR`, `ozon_raw.RAW_OZON_FINANCE_ACCRUAL`, `ozon_raw.RAW_OZON_POSTINGS_FBO`
- `ozon_mart.V_OZON_SKU_UNIT_ECONOMICS_CURRENT` ← `ozon_mart.FCT_OZON_SKU_PNL_MONTHLY`
- `ozon_mart.V_OZON_TARIFF_CHANGE_LOG` ← `ozon_raw.RAW_OZON_PRICE_COMMISSIONS`

### Общие и нейтральные

Конечных объектов (никто не читает изнутри BigQuery — это выход к Metabase, листам или агентам, либо мёртвый объект): **13**.

- `evetis_mart.FACT_SKU_DAILY` ← `evetis_ref.REF_PRODUCT_MASTER`, `evetis_ref.REF_SKU_CHANNEL_MAP`, `ozon_mart.FCT_OZON_SKU_PNL_DAILY`, `wb_mart.SKU_PERFORMANCE_V2_DAILY`
- `evetis_ops.V_CT_SEED_RECON` ← `evetis_ops.CT_OPENING_FF_SNAPSHOT`, `evetis_ops.CT_OPENING_OZON_EVIDENCE`, `evetis_ops.V_OPS_OZON_ORDER_UNITS`, `evetis_ref.CT_STOCK_SNAPSHOT`
- `evetis_ops.V_OPS_SHEET_BOM` ← `evetis_ops.V_OPS_BUNDLE_BOM_EXPANSION`
- `evetis_ops.V_OPS_SHEET_BUNDLES` ← `evetis_ops.V_OPS_BUNDLE_PRODUCTION`
- `evetis_ops.V_OPS_SHEET_CHANNELS` ← `evetis_ops.REF_CHANNEL_SHIPPING`
- `evetis_ops.V_OPS_SHEET_CONFIG` ← `evetis_ops.OPS_CONFIG`
- `evetis_ops.V_OPS_SHEET_FF_STOCK` ← `evetis_ops.V_OPS_FF_STOCK_SHEET`
- `evetis_ops.V_OPS_SHEET_FF_TASK` ← `evetis_ops.V_OPS_FF_TASK`
- `evetis_ops.V_OPS_SHEET_LOGISTICS` ← `evetis_ops.REF_SKU_LOGISTICS`
- `evetis_ops.V_OPS_SHEET_META` ← `evetis_ops.CT_STOCK_MOVEMENT`, `evetis_ops.OPS_CONFIG`, `evetis_ops.V_OPS_SUPPLY_CALENDAR`, `ozon_raw.RAW_OZON_STOCKS`, `ozon_raw.RAW_OZON_SUPPLY_ORDERS`, `wb_mart.V_CT_PLAN_ACTIVE`, `wb_raw.V_WB_STOCKS_T5_CURRENT`
- `evetis_ops.V_OPS_SHEET_PICK` ← `evetis_ops.V_OPS_PICK_FROM_STORAGE`
- `evetis_ops.V_OPS_SHEET_PLAN` ← `evetis_ops.V_OPS_SUPPLY_CALENDAR`, `evetis_ops.V_OPS_SUPPLY_PLAN`
- `evetis_ops.V_OPS_SHEET_SHIPMENTS` ← `evetis_ops.V_OPS_SHIPMENTS_SHEET`

## Полный граф

Машиночитаемая форма — `system_inventory.json`, поля `depends_on` и `consumed_by` у каждого объекта. Здесь — только разрезы, которые читает человек.
