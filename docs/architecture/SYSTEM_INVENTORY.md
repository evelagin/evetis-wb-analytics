# Инвентарь платформы

<!-- СГЕНЕРИРОВАНО tools/render_architecture_docs.py из docs/architecture/system_inventory.json. Правки в этом файле будут затёрты: меняй генератор или снимок. -->

**Снимок:** 2026-09-21T05:56:56Z · **Проект:** `project-fa311fc0-4d87-4781-986` · **Регион:** EU · собран только чтением (5 запроса к BigQuery).

## Итого

| Величина | Значение |
|---|---|
| Датасетов | 10 |
| Объектов BigQuery | 283 |
| — таблиц | 133 |
| — вью | 150 |
| Процедур и функций | 22 |
| Рёбер зависимостей (из тел вью) | 486 |
| Вью с каноническим Git-определением (`sql/current`) | 14 |
| Вью **без** канонического определения | 136 |
| Ресурсов Terraform | 132 |
| Файлов Apps Script (production, вне CI) | 108 |

## Датасеты

| Датасет | Домен | Таблиц | Вью |
|---|---|---:|---:|
| `evetis_communications` | shared | 3 | 0 |
| `evetis_mart` | shared | 0 | 1 |
| `evetis_ops` | shared | 10 | 27 |
| `evetis_ref` | shared | 27 | 2 |
| `ozon_mart` | ozon | 0 | 13 |
| `ozon_raw` | ozon | 15 | 0 |
| `ozon_stg` | ozon | 0 | 0 |
| `wb_mart` | wb | 27 | 70 |
| `wb_ops` | wb | 9 | 2 |
| `wb_raw` | wb | 42 | 35 |

## Таблицы-носители данных

Только базовые таблицы; вью данных не хранят. `изменена` — момент последней записи в хранилище, он же простейший индикатор свежести.

### Домен `wb`

| Объект | Слой | Строк | Размер | Изменена |
|---|---|---:|---:|---|
| `wb_mart.EXECUTIVE_V2_BUILD_LOG` | mart | 77 | 7.9 KB | 2026-09-21T05:11:30Z |
| `wb_mart.EXECUTIVE_V2_DAILY` | mart | 747 | 429.3 KB | 2026-09-21T05:11:26Z |
| `wb_mart.FACT_ADS_COSTS_DAILY` | mart | 1960 | 248.8 KB | 2026-09-21T04:02:55Z |
| `wb_mart.FACT_ADS_COSTS_DAILY__BUILD` | mart | 1960 | 248.8 KB | 2026-09-21T04:02:13Z |
| `wb_mart.FACT_ADS_SKU_DAILY` | mart | 6043 | 1.1 MB | 2026-09-21T04:02:50Z |
| `wb_mart.FACT_ADS_SKU_DAILY__BUILD` | mart | 6043 | 1.1 MB | 2026-09-21T04:02:03Z |
| `wb_mart.FACT_FINANCE` | mart | 206965 | 100.0 MB | 2026-09-21T04:02:44Z |
| `wb_mart.FACT_FINANCE__BUILD` | mart | 206965 | 100.0 MB | 2026-09-21T04:01:40Z |
| `wb_mart.FACT_ORDERS` | mart | 4786 | 1.6 MB | 2026-09-21T04:02:23Z |
| `wb_mart.FACT_ORDERS__BUILD` | mart | 4786 | 1.6 MB | 2026-09-21T04:01:01Z |
| `wb_mart.FACT_SALES` | mart | 4460 | 1.6 MB | 2026-09-21T04:02:28Z |
| `wb_mart.FACT_SALES__BUILD` | mart | 4460 | 1.6 MB | 2026-09-21T04:01:13Z |
| `wb_mart.FACT_STOCKS_SNAPSHOT` | mart | 6379 | 1.4 MB | 2026-09-21T04:02:33Z |
| `wb_mart.FACT_STOCKS_SNAPSHOT_BAK_20260817` | mart | 5591 | 1.2 MB | 2026-08-17T13:11:48Z |
| `wb_mart.FACT_STOCKS_SNAPSHOT__BUILD` | mart | 6379 | 1.4 MB | 2026-09-21T04:01:20Z |
| `wb_mart.MART_RUNS` | mart | 69 | 60.9 KB | 2026-09-21T04:04:16Z |
| `wb_mart.MART_SKU_DAILY` | mart | 8102 | 5.3 MB | 2026-09-21T04:04:05Z |
| `wb_mart.REF_COST_MAP` | reference | 21 | 5.4 KB | 2026-09-16T10:32:47Z |
| `wb_mart.SKU_PERFORMANCE_V2_BUILD_LOG` | mart | 57 | 5.8 KB | 2026-09-21T05:21:07Z |
| `wb_mart.SKU_PERFORMANCE_V2_CONFIG` | mart | 1 | 416 B | 2026-09-18T07:59:08Z |
| `wb_mart.SKU_PERFORMANCE_V2_DAILY` | mart | 8102 | 5.2 MB | 2026-09-21T05:21:04Z |
| `wb_mart.UNITKA_COGS_EFFECTIVE` | other | 38 | 10.2 KB | 2026-09-21T05:50:26Z |
| `wb_mart.UNITKA_COGS_PUBLISH_LOG` | operational | 41 | 5.9 KB | 2026-09-21T05:50:29Z |
| `wb_mart._EXECUTIVE_V2_BUILD_LOCK` | operational | 1 | 75 B | 2026-09-21T05:11:28Z |
| `wb_mart._MART_BOOTSTRAP_LOCK` | operational | 2 | 221 B | 2026-09-21T04:04:12Z |
| `wb_mart._SKU_PERFORMANCE_V2_BUILD_LOCK` | operational | 1 | 81 B | 2026-09-21T05:21:06Z |
| `wb_mart._UNITKA_COGS_PUBLISH_LOCK` | operational | 1 | 80 B | 2026-09-21T05:50:27Z |
| `wb_ops.OPS_ALERT_EVENT` | operational | 6 | 2.5 KB | 2026-09-21T03:01:08Z |
| `wb_ops.OPS_HEALTH_STATE` | operational | 840 | 285.6 KB | 2026-09-21T03:01:01Z |
| `wb_ops.OPS_INCIDENT` | operational | 3 | 1.3 KB | 2026-09-21T03:01:05Z |
| `wb_ops.OPS_METRIC_COVERAGE` | other | 8 | 2.0 KB | 2026-09-02T17:39:51Z |
| `wb_ops.OPS_METRIC_COVERAGE_GAPS` | other | 8 | 2.3 KB | 2026-09-02T17:39:52Z |
| `wb_ops.OPS_PIPELINE_REGISTRY` | other | 16 | 8.4 KB | 2026-09-08T10:31:50Z |
| `wb_ops.UNITKA_ENGINE_RUNS` | operational | 33 | 152.1 KB | 2026-09-20T18:23:36Z |
| `wb_ops.UNITKA_INTEGRITY_ISSUES` | other | 439 | 214.2 KB | 2026-09-20T18:23:34Z |
| `wb_ops.UNITKA_REPAIR_LEDGER` | other | 9 | 3.7 KB | 2026-09-20T13:18:20Z |
| `wb_raw.FINANCE_LOADER_RUNS` | operational | 183 | 17.1 KB | 2026-09-21T04:29:33Z |
| `wb_raw.FINANCE_REPORT_LOADS` | other | 122 | 66.4 KB | 2026-09-21T04:29:30Z |
| `wb_raw.FINANCE_WEEK_RECON` | other | 126 | 11.3 KB | 2026-09-14T09:27:31Z |
| `wb_raw.FINANCE_WEEK_STATUS` | other | 20 | 826 B | 2026-09-21T04:26:17Z |
| `wb_raw.INGEST_RUNS` | operational | 2380 | 273.7 KB | 2026-09-21T05:32:01Z |
| `wb_raw.LOADER_RUNS` | operational | 1200 | 455.0 KB | 2026-09-21T05:41:11Z |
| `wb_raw.RAW_WB_ADV_BOOSTER_STATS` | raw | 5025 | 931.2 KB | 2026-09-21T02:11:17Z |
| `wb_raw.RAW_WB_ADV_CAMPAIGNS` | raw | 29615 | 17.6 MB | 2026-09-21T02:07:45Z |
| `wb_raw.RAW_WB_ADV_CAMPAIGN_STATS` | raw | 54842 | 15.2 MB | 2026-09-21T02:11:22Z |
| `wb_raw.RAW_WB_ADV_COSTS` | raw | 12743 | 5.3 MB | 2026-09-21T02:08:03Z |
| `wb_raw.RAW_WB_ADV_COSTS_RUNS` | raw | 50 | 8.3 KB | 2026-09-21T02:08:06Z |
| `wb_raw.RAW_WB_ADV_QUERY_BIDS` | raw | 3745 | 1.3 MB | 2026-09-21T02:11:44Z |
| `wb_raw.RAW_WB_ADV_QUERY_BIDS_RUNS` | raw | 37 | 5.5 KB | 2026-09-21T02:11:48Z |
| `wb_raw.RAW_WB_ADV_QUERY_STATS` | raw | 55409 | 20.7 MB | 2026-09-21T02:12:11Z |
| `wb_raw.RAW_WB_ADV_QUERY_STATS_RUNS` | raw | 310 | 50.7 KB | 2026-09-21T02:12:14Z |
| `wb_raw.RAW_WB_ADV_SEARCH_CLUSTERS` | raw | 44 | 15.2 KB | 2026-07-11T20:14:07Z |
| `wb_raw.RAW_WB_FINANCE` | raw | 211904 | 334.7 MB | 2026-09-21T04:29:25Z |
| `wb_raw.RAW_WB_FUNNEL_DAILY` | raw | 1750 | 971.2 KB | 2026-09-20T06:31:38Z |
| `wb_raw.RAW_WB_FUNNEL_XLSX_BACKFILL` | raw | 65 | 29.8 KB | 2026-09-11T21:21:41Z |
| `wb_raw.RAW_WB_KIZ_SUPPLY` | raw | 3068 | 521.3 KB | 2026-08-24T15:59:34Z |
| `wb_raw.RAW_WB_ORDERS` | raw | 5543 | 2.6 MB | 2026-09-20T23:32:13Z |
| `wb_raw.RAW_WB_PAID_STORAGE` | raw | 6717 | 5.9 MB | 2026-09-20T08:45:57Z |
| `wb_raw.RAW_WB_PAID_STORAGE__STAGE` | raw | 2657 | 2.3 MB | 2026-09-20T08:45:49Z |
| `wb_raw.RAW_WB_PRICES` | raw | 24825 | 15.4 MB | 2026-09-21T05:41:06Z |
| `wb_raw.RAW_WB_SALES_RETURNS` | raw | 4819 | 6.5 MB | 2026-09-20T18:23:09Z |
| `wb_raw.RAW_WB_STOCKS` | raw | 6649 | 2.6 MB | 2026-09-21T03:23:42Z |
| `wb_raw.RAW_WB_STOCKS_T5` | raw | 7746 | 1.8 MB | 2026-09-21T03:23:39Z |
| `wb_raw.RAW_WB_STOCKS__CR` | raw | 4984 | 2.0 MB | 2026-09-21T03:31:23Z |
| `wb_raw.RAW_WB_SUPPLIES` | raw | 731 | 434.8 KB | 2026-09-20T23:31:36Z |
| `wb_raw.RAW_WB_SUPPLIES_GOODS` | raw | 910 | 400.6 KB | 2026-09-20T23:31:33Z |
| `wb_raw.RAW_WB_TARIFFS` | raw | 675431 | 366.2 MB | 2026-09-21T05:16:09Z |
| `wb_raw.REF_ACTIVE_VERSION` | reference | 1 | 49 B | 2026-09-21T05:22:50Z |
| `wb_raw.REF_SKU_MASTER_DATA` | reference | 250 | 72.9 KB | 2026-09-21T05:22:57Z |
| `wb_raw.REF_SYNC_RUNS` | reference | 62 | 7.7 KB | 2026-09-21T05:22:55Z |
| `wb_raw.REF_SYNC_TABLE_LOG` | reference | 62 | 5.8 KB | 2026-09-21T05:22:52Z |
| `wb_raw.WB_FUNNEL_OBSERVATIONS` | operational | 10 | 2.4 KB | 2026-09-20T06:31:40Z |
| `wb_raw.WB_PRICES_OBSERVATIONS` | operational | 993 | 245.8 KB | 2026-09-21T05:41:09Z |
| `wb_raw.WB_STOCKS_SNAPSHOTS` | operational | 72 | 17.7 KB | 2026-09-21T03:23:46Z |
| `wb_raw.WB_STOCKS_SNAPSHOTS_BAK_20260816` | backup | 6 | 1.4 KB | 2026-08-16T11:58:43Z |
| `wb_raw.WB_STOCKS_SNAPSHOTS__CR` | other | 57 | 10.7 KB | 2026-09-21T03:31:25Z |
| `wb_raw.WB_STORAGE_OBSERVATIONS` | operational | 0 | — | 2026-09-13T10:51:54Z |
| `wb_raw.WB_TARIFF_OBSERVATIONS` | operational | 15 | 3.5 KB | 2026-09-21T05:16:13Z |

### Домен `ozon`

| Объект | Слой | Строк | Размер | Изменена |
|---|---|---:|---:|---|
| `ozon_raw.OZON_INGESTION_RUNS` | operational | 306 | 53.1 KB | 2026-09-21T04:07:05Z |
| `ozon_raw.RAW_OZON_ADS_CAMPAIGNS` | raw | 1840 | 486.6 KB | 2026-09-21T03:32:09Z |
| `ozon_raw.RAW_OZON_ADS_EXPENSE_DAILY` | raw | 2261 | 536.3 KB | 2026-09-21T03:32:33Z |
| `ozon_raw.RAW_OZON_ADS_SKU_DAILY` | raw | 3870 | 992.6 KB | 2026-09-21T03:38:37Z |
| `ozon_raw.RAW_OZON_CATALOG` | raw | 400 | 192.4 KB | 2026-09-21T03:31:21Z |
| `ozon_raw.RAW_OZON_CLUSTERS` | raw | 5637 | 1.3 MB | 2026-09-21T02:01:11Z |
| `ozon_raw.RAW_OZON_FINANCE_ACCRUAL` | raw | 9642 | 2.3 MB | 2026-09-21T03:32:03Z |
| `ozon_raw.RAW_OZON_POSTINGS_FBO` | raw | 2347 | 1.6 MB | 2026-09-21T04:01:10Z |
| `ozon_raw.RAW_OZON_PRICES` | raw | 400 | 368.0 KB | 2026-09-21T03:31:27Z |
| `ozon_raw.RAW_OZON_PRICE_COMMISSIONS` | raw | 4800 | 1.0 MB | 2026-09-21T03:31:32Z |
| `ozon_raw.RAW_OZON_SELLER_INFO` | raw | 16 | 8.8 KB | 2026-09-21T03:31:38Z |
| `ozon_raw.RAW_OZON_STOCKS` | raw | 3899 | 1.0 MB | 2026-09-21T04:00:58Z |
| `ozon_raw.RAW_OZON_SUPPLIES` | raw | 170 | 62.8 KB | 2026-09-21T03:38:54Z |
| `ozon_raw.RAW_OZON_SUPPLY_BUNDLES` | raw | 648 | 263.6 KB | 2026-09-21T03:42:29Z |
| `ozon_raw.RAW_OZON_SUPPLY_ORDERS` | raw | 75 | 24.9 KB | 2026-09-21T03:38:48Z |

### Домен `shared`

| Объект | Слой | Строк | Размер | Изменена |
|---|---|---:|---:|---|
| `evetis_communications.communication_engine_shadow` | other | 2 | 2.6 KB | 2026-07-23T07:50:46Z |
| `evetis_communications.communication_events` | other | 398 | 68.6 KB | 2026-09-21T05:44:08Z |
| `evetis_communications.communications_current` | other | 95 | 117.8 KB | 2026-09-21T05:43:01Z |
| `evetis_ops.CT_BUNDLE_BUILD` | reference | 0 | — | 2026-09-11T15:13:18Z |
| `evetis_ops.CT_OPENING_FF_SNAPSHOT` | reference | 12 | 1.9 KB | 2026-09-11T15:13:24Z |
| `evetis_ops.CT_OPENING_OZON_EVIDENCE` | reference | 9 | 926 B | 2026-09-11T15:13:27Z |
| `evetis_ops.CT_OPS_REQUEST_LOG` | reference | 1 | 138 B | 2026-09-11T16:28:44Z |
| `evetis_ops.CT_SHIPMENT` | reference | 9 | 2.6 KB | 2026-09-11T16:28:44Z |
| `evetis_ops.CT_SHIPMENT_LINE` | reference | 88 | 9.5 KB | 2026-09-11T16:28:44Z |
| `evetis_ops.CT_STOCK_MOVEMENT` | reference | 88 | 22.6 KB | 2026-09-11T16:28:44Z |
| `evetis_ops.OPS_CONFIG` | reference | 6 | 956 B | 2026-09-12T09:08:28Z |
| `evetis_ops.REF_CHANNEL_SHIPPING` | reference | 3 | 2.1 KB | 2026-09-11T21:29:34Z |
| `evetis_ops.REF_SKU_LOGISTICS` | reference | 45 | 62.2 KB | 2026-09-11T21:29:38Z |
| `evetis_ref.BAK_20260904B_REF_SKU_COGS_HISTORY` | backup | 17 | 15.2 KB | 2026-09-04T10:19:22Z |
| `evetis_ref.BAK_20260904_REF_BUNDLE_COMPONENTS` | backup | 33 | 4.5 KB | 2026-09-04T09:29:02Z |
| `evetis_ref.BAK_20260904_REF_SKU_CHANNEL_MAP` | backup | 45 | 8.4 KB | 2026-09-04T07:54:31Z |
| `evetis_ref.BAK_20260904_REF_SKU_COGS_HISTORY` | backup | 17 | 11.2 KB | 2026-09-04T07:54:07Z |
| `evetis_ref.CT_ACTION_STATUS_LOG` | reference | 2 | 233 B | 2026-09-21T04:41:09Z |
| `evetis_ref.CT_ACTUAL_DAILY` | reference | 11826 | 3.1 MB | 2026-09-21T04:40:37Z |
| `evetis_ref.CT_BUNDLE_PLAN` | reference | 83 | 15.7 KB | 2026-09-10T06:31:17Z |
| `evetis_ref.CT_CONFIG` | reference | 5 | 1.0 KB | 2026-09-11T12:21:55Z |
| `evetis_ref.CT_DAILY_CURVE` | reference | 424 | 28.9 KB | 2026-09-10T06:27:10Z |
| `evetis_ref.CT_EXPIRY_BATCH` | reference | 11 | 2.5 KB | 2026-09-10T06:28:40Z |
| `evetis_ref.CT_INVENTORY_SNAPSHOT_DAILY` | reference | 132 | 79.6 KB | 2026-09-21T04:40:51Z |
| `evetis_ref.CT_OPEX` | reference | 20 | 1.5 KB | 2026-09-10T06:28:42Z |
| `evetis_ref.CT_OWNER_ACTION_QUEUE` | reference | 35 | 21.2 KB | 2026-09-21T04:41:11Z |
| `evetis_ref.CT_PLAN_VERSION` | reference | 1 | 746 B | 2026-09-10T12:21:24Z |
| `evetis_ref.CT_REFRESH_LOG` | reference | 211 | 37.3 KB | 2026-09-21T04:41:14Z |
| `evetis_ref.CT_SEASON_PLAN` | reference | 7710 | 2.4 MB | 2026-09-10T06:27:14Z |
| `evetis_ref.CT_SEASON_PLAN_MONTHLY` | reference | 264 | 57.7 KB | 2026-09-10T06:25:47Z |
| `evetis_ref.CT_STOCK_SNAPSHOT` | reference | 11 | 4.5 KB | 2026-09-10T06:28:44Z |
| `evetis_ref.REF_BUNDLE_COMPONENTS` | reference | 33 | 7.8 KB | 2026-09-04T09:29:16Z |
| `evetis_ref.REF_COST_ADDITIONAL_LANDED` | reference | 16 | 6.3 KB | 2026-09-04T10:20:42Z |
| `evetis_ref.REF_COST_BATCH` | reference | 7 | 2.6 KB | 2026-09-04T09:05:41Z |
| `evetis_ref.REF_COST_BATCH_SKU` | reference | 15 | 3.1 KB | 2026-09-04T09:07:13Z |
| `evetis_ref.REF_MARKETPLACE_COMMISSION_COMPONENT` | reference | 1 | 1.1 KB | 2026-09-07T11:41:35Z |
| `evetis_ref.REF_PRODUCT_MASTER` | reference | 25 | 6.7 KB | 2026-09-01T11:48:26Z |
| `evetis_ref.REF_SKU_CHANNEL_MAP` | reference | 47 | 9.0 KB | 2026-09-04T09:13:09Z |
| `evetis_ref.REF_SKU_COGS_HISTORY` | reference | 17 | 16.8 KB | 2026-09-04T10:21:00Z |
| `evetis_ref.REF_WB_COMMISSION_HISTORY` | reference | 1 | 801 B | 2026-09-10T19:55:34Z |

## Оркестрация

### Cloud Scheduler

| Задание | Состояние | Расписание | Зона | Цель |
|---|---|---|---|---|
| `ct-refresh-prod` | ENABLED | `40 7,9,12,16,19 * * *` | Europe/Moscow | bigquery_job · `CALL `evetis_ref.sp_ct_refresh_daily`()` |
| `evetis-wb-poll` | ENABLED | `0 8,11,14,17,20 * * *` | Europe/Moscow | http_service |
| `executive-v2-layer-build` | ENABLED | `10 7-23 * * *` | Europe/Moscow | bigquery_job · `CALL `wb_mart.sp_build_executive_v2_daily`('scheduler')` |
| `ozon-daily` | ENABLED | `30 6 * * *` | Europe/Moscow | cloud_run_job |
| `ozon-fast` | ENABLED | `0 7,13,19 * * *` | Europe/Moscow | cloud_run_job |
| `ozon-weekly` | ENABLED | `0 5 * * 1` | Europe/Moscow | cloud_run_job |
| `sku-performance-v2-layer-build` | ENABLED | `20 7-23 * * *` | Europe/Moscow | bigquery_job · `CALL `wb_mart.sp_build_sku_performance_v2_daily`('scheduler')` |
| `unitka-cogs-publication` | ENABLED | `50 7-23 * * *` | Europe/Moscow | bigquery_job · `CALL `wb_mart.sp_publish_unitka_cogs`('scheduler')` |
| `unitka-engine-prod-morning` | ENABLED | `0 7 * * *` | Etc/UTC | cloud_run_job |
| `unitka-engine-prod-reserve` | ENABLED | `30 9 * * *` | Etc/UTC | cloud_run_job |
| `unitka-engine-shadow-morning` | PAUSED | `0 7 * * *` | Etc/UTC | cloud_run_job |
| `unitka-engine-shadow-reserve` | PAUSED | `30 9 * * *` | Etc/UTC | cloud_run_job |
| `wb-funnel-prod` | ENABLED | `30 6 * * *` | Etc/UTC | cloud_run_job |
| `wb-mart-prod` | ENABLED | `0 7,9,12,16 * * *` | Europe/Moscow | cloud_run_job |
| `wb-ops-health-prod` | ENABLED | `0 */3 * * *` | Europe/Moscow | bigquery_job · `CALL `wb_ops.sp_evaluate_pipeline_health`()` |
| `wb-paid-storage-prod` | ENABLED | `45 8 * * *` | Etc/UTC | cloud_run_job |
| `wb-prices-prod` | ENABLED | `*/20 * * * *` | Etc/UTC | cloud_run_job |
| `wb-stocks-prod` | PAUSED | `30 6 * * *` | Europe/Moscow | cloud_run_job |
| `wb-stocks-shadow` | ENABLED | `30 6 * * *` | Europe/Moscow | cloud_run_job |
| `wb-tariffs-prod` | ENABLED | `15 5 * * *` | Etc/UTC | cloud_run_job |

### Cloud Run Jobs

| Job | Аргументы | Образ закреплён по digest |
|---|---|---|
| `ozon-bootstrap-load` | `—` | да |
| `ozon-runtime-daily` | `—` | да |
| `ozon-runtime-fast` | `—` | да |
| `ozon-runtime-ingest` | `—` | да |
| `ozon-runtime-weekly` | `—` | да |
| `unitka-engine-prod` | `unitka` | да |
| `unitka-engine-shadow` | `unitka` | да |
| `wb-funnel-prod` | `funnel` | да |
| `wb-mart-prod` | `mart` | да |
| `wb-paid-storage-prod` | `storage` | да |
| `wb-prices-prod` | `prices` | да |
| `wb-stocks-prod` | `noop` | да |
| `wb-stocks-shadow` | `stocks` | да |
| `wb-tariffs-prod` | `tariffs` | да |

### Cloud Run Services

| Сервис | Регион | Ingress |
|---|---|---|
| `evetis-wb-communications` | europe-west1 | all |

## Процедуры BigQuery

| Процедура | Тип | Изменена | Читает/пишет |
|---|---|---|---|
| `evetis_ops.fn_ops_round_decision` | FUNCTION | 2026-09-12T09:08:31Z | — |
| `evetis_ops.sp_ops_bundle_build` | PROCEDURE | 2026-09-11T15:14:01Z | `evetis_ops.CT_BUNDLE_BUILD`, `evetis_ops.CT_OPS_REQUEST_LOG`, `evetis_ops.CT_SHIPMENT_LINE`, `evetis_ops.CT_STOCK_MOVEMENT`, `evetis_ops.OPS_CONFIG`, `evetis_ops.V_CT_OZON_ORDER_RECON` … (+7) |
| `evetis_ops.sp_ops_guard_fresh` | PROCEDURE | 2026-09-11T15:13:49Z | `evetis_ops.OPS_CONFIG`, `ozon_raw.RAW_OZON_SUPPLY_ORDERS`, `wb_mart.V_CT_INVENTORY_TRUTH` |
| `evetis_ops.sp_ops_guard_writeback` | PROCEDURE | 2026-09-11T15:13:45Z | `evetis_ops.OPS_CONFIG` |
| `evetis_ops.sp_ops_post_movements` | PROCEDURE | 2026-09-11T15:13:39Z | `evetis_ops.CT_STOCK_MOVEMENT`, `evetis_ops.V_CT_STOCK_BALANCE`, `evetis_ops.V_OPS_BOM`, `evetis_ref.REF_PRODUCT_MASTER` |
| `evetis_ops.sp_ops_request_reverse` | PROCEDURE | 2026-09-11T15:14:04Z | `evetis_ops.CT_BUNDLE_BUILD`, `evetis_ops.CT_OPS_REQUEST_LOG`, `evetis_ops.CT_SHIPMENT`, `evetis_ops.CT_STOCK_MOVEMENT`, `evetis_ops.OPS_CONFIG`, `evetis_ops.V_CT_SHIPMENT_CURRENT` … (+2) |
| `evetis_ops.sp_ops_reverse_core` | PROCEDURE | 2026-09-11T15:13:42Z | `evetis_ops.CT_STOCK_MOVEMENT`, `evetis_ops.sp_ops_post_movements` |
| `evetis_ops.sp_ops_seed_opening` | PROCEDURE | 2026-09-11T15:13:51Z | `evetis_ops.CT_OPENING_FF_SNAPSHOT`, `evetis_ops.CT_OPENING_OZON_EVIDENCE`, `evetis_ops.CT_OPS_REQUEST_LOG`, `evetis_ops.CT_SHIPMENT`, `evetis_ops.CT_SHIPMENT_LINE`, `evetis_ops.CT_STOCK_MOVEMENT` … (+3) |
| `evetis_ops.sp_ops_seed_reverse` | PROCEDURE | 2026-09-11T15:13:54Z | `evetis_ops.CT_OPS_REQUEST_LOG`, `evetis_ops.CT_SHIPMENT`, `evetis_ops.CT_STOCK_MOVEMENT`, `evetis_ops.OPS_CONFIG`, `evetis_ops.V_CT_SHIPMENT_CURRENT`, `evetis_ops.sp_ops_reverse_core` |
| `evetis_ops.sp_ops_shipment_reserve` | PROCEDURE | 2026-09-11T15:13:58Z | `evetis_ops.CT_OPS_REQUEST_LOG`, `evetis_ops.CT_SHIPMENT`, `evetis_ops.CT_SHIPMENT_LINE`, `evetis_ops.CT_STOCK_MOVEMENT`, `evetis_ops.OPS_CONFIG`, `evetis_ops.V_CT_OZON_ORDER_RECON` … (+7) |
| `evetis_ref.sp_ct_action_update` | PROCEDURE | 2026-09-10T11:30:06Z | `evetis_ref.CT_ACTION_STATUS_LOG`, `evetis_ref.CT_OWNER_ACTION_QUEUE` |
| `evetis_ref.sp_ct_generate_actions` | PROCEDURE | 2026-09-10T11:31:00Z | `evetis_ref.CT_ACTION_STATUS_LOG`, `evetis_ref.CT_OWNER_ACTION_QUEUE`, `evetis_ref.CT_REFRESH_LOG`, `wb_mart.V_CT_ACTION_CANDIDATES` |
| `evetis_ref.sp_ct_refresh_daily` | PROCEDURE | 2026-09-10T14:16:45Z | `evetis_ref.CT_ACTUAL_DAILY`, `evetis_ref.CT_CONFIG`, `evetis_ref.CT_INVENTORY_SNAPSHOT_DAILY`, `evetis_ref.CT_OWNER_ACTION_QUEUE`, `evetis_ref.CT_PLAN_VERSION`, `evetis_ref.CT_REFRESH_LOG` … (+10) |
| `wb_mart.TVF_SKU_PERFORMANCE_V2_PERIOD` | TABLE FUNCTION | 2026-09-18T07:59:56Z | `wb_mart.SKU_PERFORMANCE_V2_CONFIG`, `wb_mart.V_DASH_SKU_PERFORMANCE_V2_DAILY` |
| `wb_mart.TVF_WB_FORWARD_ECONOMICS` | TABLE FUNCTION | 2026-09-07T11:54:43Z | `wb_mart.V_WB_SKU_FORWARD_ECONOMICS_CURRENT` |
| `wb_mart.sp_bootstrap_facts` | PROCEDURE | 2026-08-17T12:41:52Z | `wb_mart.FACT_ADS_COSTS_DAILY`, `wb_mart.FACT_ADS_COSTS_DAILY__BUILD`, `wb_mart.FACT_ADS_SKU_DAILY`, `wb_mart.FACT_ADS_SKU_DAILY__BUILD`, `wb_mart.FACT_FINANCE`, `wb_mart.FACT_FINANCE__BUILD` … (+16) |
| `wb_mart.sp_build_executive_v2_daily` | PROCEDURE | 2026-09-17T06:42:22Z | `wb_mart.EXECUTIVE_V2_BUILD_LOG`, `wb_mart.EXECUTIVE_V2_DAILY`, `wb_mart.V_DASH_BUYOUT_COHORT_DAILY`, `wb_mart.V_DASH_EXECUTIVE_BREAKDOWN_DAILY`, `wb_mart.V_DASH_EXECUTIVE_ECONOMICS_DAILY`, `wb_mart.V_DASH_FINANCE_CORRECTED_DAILY` … (+5) |
| `wb_mart.sp_build_mart_sku_daily` | PROCEDURE | 2026-08-26T19:33:10Z | `wb_mart.FACT_ADS_SKU_DAILY`, `wb_mart.FACT_FINANCE`, `wb_mart.FACT_ORDERS`, `wb_mart.FACT_SALES`, `wb_mart.INFORMATION_SCHEMA`, `wb_mart.MART_SKU_DAILY` … (+5) |
| `wb_mart.sp_build_sku_performance_v2_daily` | PROCEDURE | 2026-09-18T07:59:09Z | `wb_mart.FACT_ORDERS`, `wb_mart.FACT_SALES`, `wb_mart.INFORMATION_SCHEMA`, `wb_mart.MART_SKU_DAILY`, `wb_mart.SKU_PERFORMANCE_V2_BUILD_LOG`, `wb_mart.SKU_PERFORMANCE_V2_DAILY` … (+12) |
| `wb_mart.sp_publish_unitka_cogs` | PROCEDURE | 2026-09-18T16:58:34Z | `evetis_ref.V_PRODUCT_COGS_EFFECTIVE`, `wb_mart.UNITKA_COGS_EFFECTIVE`, `wb_mart.UNITKA_COGS_PUBLISH_LOG`, `wb_mart._UNITKA_COGS_PUBLISH_LOCK` |
| `wb_ops.sp_evaluate_pipeline_health` | PROCEDURE | 2026-09-06T16:42:19Z | `wb_mart.MART_RUNS`, `wb_mart.MART_SKU_DAILY`, `wb_mart.V_WB_FINANCE_AMOUNTS_LONG_MAPPED`, `wb_ops.OPS_PIPELINE_REGISTRY`, `wb_ops.sp_ops_apply_health`, `wb_raw.RAW_WB_ADV_QUERY_BIDS_RUNS` … (+2) |
| `wb_ops.sp_ops_apply_health` | PROCEDURE | 2026-09-06T16:44:22Z | `wb_ops.OPS_ALERT_EVENT`, `wb_ops.OPS_HEALTH_STATE`, `wb_ops.OPS_INCIDENT` |

## Вью без канонического Git-определения

Для этих объектов авторитетным определением остаётся production, а не репозиторий: изменение в BigQuery мимо Git не будет замечено. Покрытие расширяется по `sql/current/README.md`.

- **`evetis_ops`** (27): `V_CT_BUNDLE_CAPACITY`, `V_CT_OZON_ORDER_RECON`, `V_CT_SEED_RECON`, `V_CT_SHIPMENT_CURRENT`, `V_CT_STOCK_BALANCE`, `V_CT_STOCK_STATE`, `V_OPS_BOM`, `V_OPS_BUNDLE_BOM_EXPANSION`, `V_OPS_BUNDLE_PRODUCTION`, `V_OPS_FF_STOCK_SHEET`, `V_OPS_FF_TASK`, `V_OPS_OZON_ORDER_UNITS`, `V_OPS_PICK_FROM_STORAGE`, `V_OPS_SHEET_BOM`, `V_OPS_SHEET_BUNDLES`, `V_OPS_SHEET_CHANNELS`, `V_OPS_SHEET_CONFIG`, `V_OPS_SHEET_FF_STOCK`, `V_OPS_SHEET_FF_TASK`, `V_OPS_SHEET_LOGISTICS`, `V_OPS_SHEET_META`, `V_OPS_SHEET_PICK`, `V_OPS_SHEET_PLAN`, `V_OPS_SHEET_SHIPMENTS`, `V_OPS_SHIPMENTS_SHEET`, `V_OPS_SUPPLY_CALENDAR`, `V_OPS_SUPPLY_PLAN`
- **`evetis_ref`** (2): `V_BUNDLE_COGS_DERIVED`, `V_PRODUCT_COGS_EFFECTIVE`
- **`wb_mart`** (70): `V_ADS_CAMPAIGN_DAILY`, `V_ADS_FUNNEL_QUERY_28D`, `V_ADS_FUNNEL_QUERY_90D`, `V_ADS_FUNNEL_QUERY_DAILY`, `V_ADS_FUNNEL_SIGNALS`, `V_ADS_FUNNEL_SKU_28D`, `V_ADS_SCREEN_QUERY`, `V_ADS_SCREEN_SKU`, `V_ADS_SKU_ECONOMIC_LIMITS`, `V_ADVERTISING_RECONCILIATION_DAILY`, `V_CT_ACTION_CANDIDATES`, `V_CT_ACTION_QUEUE`, `V_CT_ACTUAL_DAILY`, `V_CT_ACTUAL_DAILY_LIVE`, `V_CT_ATTENTION`, `V_CT_BOM_CURRENT`, `V_CT_BUNDLE_STATUS`, `V_CT_CASH_CONVERSION`, `V_CT_DAILY_BRIEF`, `V_CT_DAILY_BRIEF_LINES`, `V_CT_FRESHNESS`, `V_CT_HAND_CREAM_CONTROL`, `V_CT_INVENTORY_TRUTH`, `V_CT_INVENTORY_TRUTH_LIVE`, `V_CT_OWNER_HOME`, `V_CT_PHYSICAL_DAILY`, `V_CT_PLAN_ACTIVE`, `V_CT_PLAN_VS_ACTUAL_DAILY`, `V_CT_REFRESH_STATUS`, `V_CT_SKU_CONTROL`, `V_CT_SUPPLY_FLOW`, `V_CT_SUPPLY_NEED`, `V_DASH_BUYOUT_COHORT_DAILY`, `V_DASH_COVERAGE_DAILY`, `V_DASH_EXECUTIVE_BREAKDOWN_DAILY`, `V_DASH_EXECUTIVE_ECONOMICS_DAILY`, `V_DASH_EXECUTIVE_V2_DAILY`, `V_DASH_FINANCE_CORRECTED_DAILY`, `V_DASH_FRESHNESS_BY_CONTRACT`, `V_DASH_FRESHNESS_HEADER`, `V_DASH_KPI_DAILY`, `V_DASH_SETTLEMENT_DAILY`, `V_DASH_SKU_DAILY`, `V_DASH_SKU_PERFORMANCE_V2_DAILY`, `V_DATA_FRESHNESS`, `V_FACT_FINANCE_COGS`, `V_MART_RUN_LOG`, `V_MART_SKU_DAILY_COGS`, `V_SKU_PERFORMANCE_V2_EXEC_BRIDGE_DAILY`, `V_UNITKA_COGS_CANONICAL`, `V_UNITKA_COMMISSION_RATES`, `V_UNITKA_DAILY_FACT`, `V_UNITKA_INTEGRITY`, `V_UNITKA_INTEGRITY_STATUS`, `V_UNITKA_LAST_CLOSED_DATE`, `V_UNITKA_LOGISTICS_RATES`, `V_UNITKA_RECON_COGS_CANONICAL`, `V_UNITKA_RECON_FACT`, `V_UNITKA_RECON_INTEGRITY`, `V_UNITKA_RECON_WINDOW`, `V_UNITKA_SOURCE_FRESHNESS`, `V_WB_DEDUCTIONS_CLASSIFIED`, `V_WB_FINANCE_AMOUNTS_LONG`, `V_WB_FINANCE_AMOUNTS_LONG_MAPPED`, `V_WB_FINANCE_PRICE_COMPONENTS`, `V_WB_PRICING_ECONOMICS_HEALTH`, `V_WB_SKU_COST_INPUTS`, `V_WB_SKU_FORWARD_ECONOMICS_CURRENT`, `V_WB_SUPPLIES_DETAIL`, `V_WB_SUPPLIES_INTAKE_BY_SKU`
- **`wb_ops`** (2): `V_OPS_ACTIVE_INCIDENTS`, `V_OPS_CURRENT_HEALTH`
- **`wb_raw`** (35): `REF_SKU_MASTER`, `V_ADV_BOOSTER_STATS`, `V_ADV_CAMPAIGNS`, `V_ADV_CAMPAIGN_CONFIG_DAILY`, `V_ADV_CAMPAIGN_CONFIG_SNAPSHOT`, `V_ADV_CAMPAIGN_STATS`, `V_ADV_COSTS`, `V_ADV_COSTS_DAY_COVERAGE`, `V_ADV_COSTS_SNAPSHOT`, `V_ADV_COSTS_UNION_LEGACY`, `V_ADV_COSTS_UNION_PREBOOTSTRAP`, `V_ADV_QUERY_BIDS`, `V_ADV_QUERY_STATS`, `V_ADV_QUERY_STATS_COVERAGE`, `V_ADV_SEARCH_CLUSTERS`, `V_INGEST_HEARTBEAT`, `V_WB_FINANCE`, `V_WB_FINANCE_CANONICAL`, `V_WB_FINANCE_COMPLETE`, `V_WB_FINANCE_SEMANTIC`, `V_WB_FUNNEL_COVERAGE`, `V_WB_FUNNEL_DAILY`, `V_WB_ORDERS`, `V_WB_PRICES_CURRENT`, `V_WB_PRICES_OBSERVED_CHANGES`, `V_WB_PRICES_OBSERVER_HEALTH`, `V_WB_SALES_RETURNS`, `V_WB_STOCKS_CURRENT`, `V_WB_STOCKS_T5_CURRENT`, `V_WB_STORAGE_COVERAGE`, `V_WB_STORAGE_DAILY`, `V_WB_STORAGE_RECONCILIATION`, `V_WB_SUPPLIES_CURRENT`, `V_WB_SUPPLIES_GOODS_CURRENT`, `V_WB_TARIFFS_CURRENT`
