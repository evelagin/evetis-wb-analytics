# T6 — клиентский доступ к `analytics_share` (client_001 / Heldi)

Статус: код, Terraform и тесты готовы; **реальные права не выданы** (`bi_access.mode = NONE`
в реестре). Активация — отдельное решение владельца после получения клиентской идентичности.

## Что получает клиент

| Объект | Право | Зачем |
|---|---|---|
| датасет `analytics_share` проекта арендатора | `READER` (ACL датасета) | единственный интерфейс данных (ADR-08) |
| проект арендатора `mpa-t-client-001` | `roles/bigquery.jobUser` | запускать собственные запросы из BI/консоли; прав на данные не даёт |

Ничего больше: ни `ozon_raw`, `ref`, `ozon_mart`, `tenant_ops`, `tenant_locks`, ни Secret Manager,
Cloud Run, Cloud Scheduler, ни проекта платформы или EVETIS. Principals — только `user:`/`group:`
(сервисные аккаунты, домены и `allUsers` отвергаются реестром и Terraform).

## Почему нужны авторизованные датасеты

Вью `analytics_share` читают `ozon_mart` и `tenant_ops`; вью `ozon_mart` — `ozon_raw` и `ref`;
вью `tenant_ops` — `ref` и `ozon_mart`. У клиента прав на источники нет, поэтому источники
авторизуют вью (`access.dataset`, `target_types = ["VIEWS"]`) ровно по этим рёбрам:

```
analytics_share → ozon_mart, tenant_ops
ozon_mart       → ozon_raw, ref
tenant_ops      → ref, ozon_mart
```

Рёбра не выдуманы: `tools/tenancy/client_access.AUTHORIZED_VIEW_EDGES` сверяется тестом с
фактическими ссылками в `sql/tenant/ozon/**/*.sql` (`test_tenancy_t6_client_access.py`).
Авторизация датасета — не доступ клиента к источнику: он видит только вью `analytics_share`.

## Как включить (после решения владельца)

1. Реестр: `tenants/client_001/tenant.json` → `bi_access = {"mode": "ANALYTICS_SHARE_VIEWER",
   "principals": ["user:<email>"]}` (или `group:`), PR в main.
2. `python tools/tenancy/registry.py terraform-inputs client_001` даёт блок `client_access`
   (READER, jobUser, 6 рёбер авторизации); `terraform plan` — только добавления: 2 записи ACL
   `analytics_share` на principal, 6 записей `access.dataset` на источниках, 1 `google_project_iam_member`.
3. `terraform apply` — только по ACK владельца с показом плана.
4. Проверка изоляции в бою: от имени клиентской идентичности `SELECT` из каждой из 9 вью
   проходит; `SELECT` из `ozon_raw.*`, `ref.*`, `ozon_mart.*`, `tenant_ops.*` — `Access Denied`.

## Минимальные дашборды (на существующих вью, без новых объектов)

| Страница | Вью | Показатели | Фильтры |
|---|---|---|---|
| Продажи и заказы | `sales_daily`, `orders` | заказы, единицы, выручка по дате заказа; доставлено/отменено/в пути; `data_status` | период, SKU, склад |
| Остатки и покрытие | `inventory_current` + `sales_daily` | остаток по складам, дни без продаж, покрытие (остаток / средние продажи 14 дн.) | склад, SKU |
| Реклама и ДРР | `advertising_daily`, `sku_daily` | расход, показы, клики, заказы, ACOS; ДРР по SKU = `ad_spend_attributed_rub / ordered_value_rub` | период, кампания, SKU |
| Доходность SKU | `profitability_daily`, `store_costs_daily` | выручка продавца, комиссия, логистика, эквайринг, продвижение; `contribution_pre_cogs_rub` | период, SKU |
| Актуальность данных | `data_coverage` | статус каждой сущности по суткам | — |

Правила отображения: прибыль не показывается, пока `cogs_policy = NONE_PRE_COGS` — только
вклад до себестоимости (`PRE_COGS`), подпись «предварительно» при `finance_data_status != COMPLETE`;
дни с `data_status != COMPLETE` помечаются, не маскируются нулями. Реализация страниц — Looker
Studio под клиентской идентичностью (учётные данные просматривающего), без копирования витрин EVETIS.

## Что не входит

Себестоимость и сопоставление SKU клиента (ввод владельцем в `ref.REF_COGS` /
`ref.REF_SKU_CHANNEL_MAP` по отдельному регламенту), портал самообслуживания, биллинг.
