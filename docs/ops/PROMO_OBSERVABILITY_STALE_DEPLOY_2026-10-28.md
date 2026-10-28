# V_PROMO_OBSERVABILITY_CURRENT: статус STALE — план развёртывания (только по OWNER ACK)

Статус: **НЕ развёрнуто.** Ветка `fix/promo-observability-stale`.

## Проблема
С 14.10.2026 снимки акций Ozon не собираются (снятые методы v1), а вью показывало Ozon `OBSERVED` / `COMPLETE` по всем
8 способностям при возрасте последнего годного снимка 19 956+ мин. Потребители (планирование акций, 11.11) видели
состояние 14.10 как текущее.

## Изменение
Одна ветка `CASE`: возраст последнего годного снимка > 780 мин → `capability_status = 'STALE'` (после
`NOT_PROVIDED_BY_SOURCE`, до всех статусов наблюдения). Порог = FAIL контракта DRO-1 для `ozon_promo` и `wb_promo`
(тест сверяет). Колонки и типы не меняются; описание не меняется. Манифест: новый `canonical_body_sha256`,
`sync_state = pending_deploy`.

## Live-проверка тела (SELECT, 28.10 ~13:10 UTC)
Ozon — 8/8 `STALE` (возраст 20 378 мин, снимок 14.10); WB — без изменений (возраст 225 мин: OBSERVED / PARTIALLY_OBSERVED /
NOT_PROVIDED_BY_SOURCE как прежде).

## Частичные снимки (PARTIAL) не дают ложного «нет товаров»
Все вью Ozon-промо получают наблюдения через `V_OZON_PROMO_OBSERVATION_HISTORY` (только `COMPLETE` / `REUSED`);
единственное прямое чтение RAW (`RAW_OZON_PROMO_PRODUCT_MARKETING` в `V_OZON_PROMO_SKU_STATE_HISTORY`) соединяется с уже
отфильтрованной историей по `observation_id`. Проверка после выпуска изоляции этапов (SELECT):
`SELECT COUNT(*) FROM ozon_mart.V_OZON_PROMO_SKU_STATE_HISTORY WHERE observation_id IN (SELECT observation_id FROM ozon_raw.OZON_PROMO_OBSERVATIONS WHERE status='PARTIAL')` = 0.

## Развёртывание
1. Снимок «до»: `SELECT view_definition FROM evetis_mart.INFORMATION_SCHEMA.VIEWS WHERE table_name='V_PROMO_OBSERVABILITY_CURRENT'`.
2. `bq query --dry_run` файла, затем выполнение `CREATE OR REPLACE VIEW` из `sql/current/evetis_mart/V_PROMO_OBSERVABILITY_CURRENT.sql`.
3. Readback: Ozon — `STALE` пока снимок старше 780 мин; WB — как до развёртывания; схема (INFORMATION_SCHEMA.COLUMNS) = манифест.
4. Затем снятие live (R2C) → `sync_state = captured_live` отдельным PR.

Откат: `CREATE OR REPLACE VIEW` из снимка шага 1.
