# V_PROMO_OBSERVABILITY_CURRENT: статус STALE — план развёртывания (только по OWNER ACK)

Статус: **НЕ развёрнуто.** Ветка `fix/promo-observability-stale`.

## Проблема
С 14.10.2026 снимки акций Ozon не собираются (снятые методы v1), а вью показывало Ozon `OBSERVED` / `COMPLETE` по всем
8 способностям при возрасте последнего годного снимка 19 956+ мин. Потребители (планирование акций, 11.11) видели
состояние 14.10 как текущее.

## Изменение
1. Ветка `CASE`: возраст последнего годного снимка > 780 мин → `capability_status = 'STALE'` (после
   `NOT_PROVIDED_BY_SOURCE`, до всех статусов наблюдения). Порог = FAIL контракта DRO-1 для `ozon_promo` и `wb_promo`
   (тест сверяет).
2. Нет ни одного годного снимка площадки: каркас `UNNEST(['WB','OZON'])` + `LEFT JOIN` → 8 строк площадки со статусом
   `NO_VALID_OBSERVATION` (после `NOT_PROVIDED_BY_SOURCE`, до `STALE`), `latest_*` и возраст — NULL. Раньше площадка из вью
   **исчезала** (live-симуляция 28.10 без снимков Ozon: прежняя версия — 0 строк Ozon, новая — 8 × `NO_VALID_OBSERVATION`),
   а при NULL-возрасте статус проваливался бы в `NO_UNITS_IN_LATEST_OBSERVATION`. Проверка V-сценария
   `pr_promo2_canonical_validation.sql` (ровно 16 строк) теперь выполняется и в этом случае.

Колонки и типы не меняются; описание не меняется. Манифест: `canonical_body_sha256` d768db11…, `sync_state = pending_deploy`.

Фикстурная регрессия (`tools/promo_canonical_render.py`, сценарий FXO) поправлена в той же ветке: фикстуры датированы
01.01.2026, поэтому статус сопоставления Ozon в них теперь `STALE`; частичность проверяется по счётчикам. Прежняя проверка
`ozon_mapping_partial` на этой ветке давала FAIL (запуск блока FXO через `bq query`, 0 байт), после правки FXO 7/7 PASS.

## Live-проверка тела (SELECT, 28.10 ~16:30 UTC)
Ozon — 8/8 `STALE` (возраст 20 528 мин, снимок 14.10); WB — без изменений (возраст 75 мин: OBSERVED 4 /
PARTIALLY_OBSERVED 2 / NOT_PROVIDED_BY_SOURCE 2).

## Частичные снимки (PARTIAL) не дают ложного «нет товаров»
Все вью Ozon-промо получают наблюдения через `V_OZON_PROMO_OBSERVATION_HISTORY` (только `COMPLETE` / `REUSED`);
единственное прямое чтение RAW (`RAW_OZON_PROMO_PRODUCT_MARKETING` в `V_OZON_PROMO_SKU_STATE_HISTORY`) соединяется с уже
отфильтрованной историей по `observation_id`. Проверка после выпуска изоляции этапов (SELECT):
`SELECT COUNT(*) FROM ozon_mart.V_OZON_PROMO_SKU_STATE_HISTORY WHERE observation_id IN (SELECT observation_id FROM ozon_raw.OZON_PROMO_OBSERVATIONS WHERE status='PARTIAL')` = 0.

## Развёртывание
1. Снимок «до»: `SELECT view_definition FROM evetis_mart.INFORMATION_SCHEMA.VIEWS WHERE table_name='V_PROMO_OBSERVABILITY_CURRENT'`.
2. `bq query --dry_run` файла, затем выполнение `CREATE OR REPLACE VIEW` из `sql/current/evetis_mart/V_PROMO_OBSERVABILITY_CURRENT.sql`.
3. Readback: 16 строк; Ozon — `STALE` пока снимок старше 780 мин; WB — как до развёртывания; схема (INFORMATION_SCHEMA.COLUMNS) = манифест.
4. Затем снятие live (R2C) → `sync_state = captured_live` отдельным PR.

Откат: `CREATE OR REPLACE VIEW` из снимка шага 1.
