# OZON_INCREMENTAL_CONTRACT_V1

Версия 1, 2026-09-03. Домен Ozon. Подчиняется `architecture/MARKETPLACE_ISOLATION.md`.
Runtime не читает и не пишет `wb_raw` и `wb_mart`. Идентификаторы товаров резолвятся
только через `evetis_ref.REF_SKU_CHANNEL_MAP`. Равенство `offer_id` и WB `nm_id`
контрактом идентичности **не является**.

## Компоненты

| Компонент | Назначение | Где |
| --- | --- | --- |
| `ozon-bootstrap-load` | исторический bootstrap и восстановление из подготовленных файлов GCS | `pipelines/ozon/bootstrap/` |
| `ozon-runtime-ingest` | регулярный сбор из Ozon API | `pipelines/ozon/runtime/` |

Bootstrap **не** становится ежедневным загрузчиком. Общими остаются только приёмы:
MERGE по логическому ключу, retry, структурный лог, `ingestion_run_id`, хеши.

## Контракт по сущностям

| Сущность | Natural key | Изменяемые поля | Lookback | Late arrival | Пагинация | Частота |
| --- | --- | --- | --- | --- | --- | --- |
| catalog | snapshot_date + sku | весь снимок | 0 | н/п | `last_id` | daily |
| prices | snapshot_date + offer_id | весь снимок | 0 | н/п | `cursor` | daily |
| stocks | snapshot_date + sku + warehouse_id | остатки суток | 0 | н/п | нет | 3×/сутки |
| fbo_postings | posting_number + sku | `status`, `substatus`, `payout_rub`, `commission_amount_rub` | **30 дней** | статус меняется после создания | `cursor` + `has_next`, limit ≤ 100 | 3×/сутки |
| finance_accrual | accrual_id + type_id + sku | суммы уточняются | **14 дней** | **доказано на практике** | `last_id`, 1 день на запрос | daily |
| ads_campaigns | snapshot_date + campaign_id | состояние, бюджеты | 0 | н/п | нет | daily |
| ads_expense_daily | date + campaign_id | расход уточняется | **7 дней** | да | нет | daily |
| ads_sku_daily | date + campaign_id + sku | расход по SKU | **7 дней** | да | ZIP-отчёт, ≤62 дн и ≤10 кампаний | daily |
| clusters | snapshot_date + warehouse_id | состав кластеров | 0 | н/п | нет | weekly |
| supply_orders | order_id | `state`, `state_updated_at` | полный список | да | нет | daily |
| supplies | order_id + supply_id | `state`, `arrival_date` | полный список | да | нет | daily |
| supply_bundles | bundle_id + sku | `quantity_planned` | полный список | да | limit ≤ 100 | daily |

### Почему lookback именно такой

**Постинги 30 дней.** Статус меняется после создания: наблюдались переходы
`awaiting_packaging` → `delivering` → `delivered` на горизонте нескольких недель.

**Финансы 14 дней — подтверждено фактом.** Первый runtime-прогон добавил начисления
за **2026-08-31 на 1 726,67 ₽**, которых не было в backfill от 01.09: они просто ещё не
были проведены. Без lookback эта сумма потерялась бы навсегда.

**Реклама 7 дней.** Расход уточняется несколько дней; асинхронный отчёт лимитирован
62 днями и 10 кампаниями на запрос.

## Идемпотентность

Все сущности пишутся одинаково: `load job` во временную staging-таблицу →
`MERGE` по натуральному ключу → удаление staging. Внутри staging применяется
дедупликация `ROW_NUMBER() OVER (PARTITION BY ключ ORDER BY extracted_at DESC)`,
поэтому повтор в пределах одного прогона тоже безопасен.

Снимки (`catalog`, `prices`, `stocks`, `ads_campaigns`, `clusters`) имеют логическим
ключом **дату**, а не момент времени. Повторный прогон в тот же день обновляет строки,
а не создаёт вторую копию снимка. Новая строка появляется только на новую дату.

Проверено: два последовательных прогона дали **0 дублей** по всем натуральным ключам.

## Ретраи и лимиты

Экспоненциальный backoff `3 / 6 / 12 / 24 / 48` секунд, до 5 попыток, на коды
`429, 500, 502, 503, 504`, а также на сетевые обрывы и SSL-ошибки.
Паузы между днями и страницами 1–1,5 с. Счётчики запросов и ретраев пишутся в реестр прогонов.

Первый прогон: 249 запросов, 6 ретраев. Второй: 10 сущностей из 10, статус `OK`.

## Изоляция отказов

Каждая сущность выполняется независимо в своём `try`. Падение одной **не останавливает**
остальные: в первом прогоне `fbo_postings` упал, а девять других завершились штатно
и записали свои данные. Статус прогона: `OK`, `PARTIAL` или `FAILED`.
Каждая сущность пишет отдельную строку в `ozon_raw.OZON_INGESTION_RUNS`.

## Семантика маркетинга

Хранится раздельно и не смешивается: `CPC / type 41`, `SEARCH_PROMO / type 54`,
`FirstCustomerReview / type 116`. Историю по типу 116 сохраняем как факт; при
отключённой программе прогнозный статус — `NOT_APPLICABLE`, в forward-ДРР она не входит.

Периметр рекламы исторический: в `ads_sku_daily` попадают все кампании с расходом
в окне, включая `INACTIVE` и `ARCHIVED`. Нераспределённый остаток расхода
искусственно не разносится.

## Расписания

| Расписание | Cron (МСК) | Сущности |
| --- | --- | --- |
| `ozon-fast` | `0 7,13,19 * * *` | stocks, fbo_postings |
| `ozon-daily` | `30 6 * * *` | catalog, prices, finance_accrual, ads_campaigns, ads_expense_daily, ads_sku_daily, supplies |
| `ozon-weekly` | `0 5 * * 1` | clusters |

Идентичности: runtime — `sa-ozon-ingestion`, запуск — `sa-ozon-scheduler`.
Планировщик WB владельцем пайплайна Ozon не является.

## Историческая догрузка

Runtime принимает `SINCE` и `UNTIL`. Отдельного кода для расширения истории не нужно:
`ENTITIES=fbo_postings SINCE=2025-04-01 UNTIL=2026-05-31` закроет пробел по продажам.
