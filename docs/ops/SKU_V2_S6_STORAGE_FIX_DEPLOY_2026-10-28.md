# SKU Performance V2 · исправление S6 хранения — план развёртывания (только по OWNER ACK)

Статус: **НЕ развёрнуто.** Код — ветка `fix/sku-v2-s6-pair-universe`.

## Исходное состояние (live, 28.10.2026)

- `wb_mart.sp_build_sku_performance_v2_daily` = Git main (сверка ROUTINES 28.10, 38/38 процедур).
- Последняя успешная сборка 18.10 20:20 UTC, `source_data_through` = 15.10; с 19.10 04:20 UTC каждая сборка FAILED на S6.
- Отчёт платного хранения по SKU универсума в окне: 6 740,055267 ₽; на парах слоя 6 737,801067 ₽; до календаря SKU
  2,2542 ₽ (17.10, nm 909951444, событий нет); дыр внутри календаря 0.

## Что развёртывается

1. `CREATE OR REPLACE PROCEDURE wb_mart.sp_build_sku_performance_v2_daily` — §2 файла
   `sql/dash/sku_performance_v2_daily_v1.sql` (от строки `CREATE OR REPLACE PROCEDURE` до первой `END;`).
2. `CREATE OR REPLACE VIEW wb_mart.V_SKU_PERFORMANCE_V2_STORAGE_UNATTRIBUTED` — §5b того же файла.

**Не выполнять файл целиком:** §3 делает `DROP TABLE SKU_PERFORMANCE_V2_DAILY` + первую сборку — лишнее окно без
таблицы. Таблица и её схема не меняются.

## Порядок

1. Снимок «до»: `SELECT ddl FROM wb_mart.INFORMATION_SCHEMA.ROUTINES WHERE routine_name = 'sp_build_sku_performance_v2_daily'`
   → сохранить в манифест развёртывания (это и есть откат).
2. Dry-run §2 и §5b (`bq query --dry_run`), затем выполнение §2 и §5b.
3. Не трогать Scheduler `sku-performance-v2-layer-build`: следующая плановая сборка (`:20` каждый час 07–23 МСК)
   пересоберёт слой целиком. Ручной `CALL ... ('owner-ack-s6')` — только если ждать нельзя.
4. Приёмка (readback, не статус задания):
   - `SKU_PERFORMANCE_V2_BUILD_LOG`: последняя запись SUCCESS, `source_data_through` ≥ последней закрытой даты MART (≥ 27.10);
   - DRO-1 `sku_performance_v2` → HEALTHY, инциденты RUN_FAILED / FRESHNESS_LATE / FRESHNESS_STALE закрыты восстановлением;
   - `SELECT reason, COUNT(*), SUM(storage_rub) FROM wb_mart.V_SKU_PERFORMANCE_V2_STORAGE_UNATTRIBUTED GROUP BY 1`:
     `BEFORE_SKU_CALENDAR` = 1 строка / 2,2542 ₽, `MISSING_SKU_DAY` = 0;
   - мост `V_SKU_PERFORMANCE_V2_EXEC_BRIDGE_DAILY` за 16.10–последняя дата: `residual_pre_cogs_rub` в пределах, наблюдавшихся
     до 15.10 (сравнить распределение, не выдумывать порог);
   - `sql/dash/sku_performance_v2_daily_validation.sql` — все V-проверки PASS.
5. Повторный запуск сборки идемпотентен: слой пересобирается целиком из источников (TEMP → ASSERT → транзакция).

## Откат

`CREATE OR REPLACE PROCEDURE` из снимка шага 1; `DROP VIEW wb_mart.V_SKU_PERFORMANCE_V2_STORAGE_UNATTRIBUTED`.
Данные слоя не трогаются: при откате процедура снова падает на S6, таблица остаётся в последнем успешном состоянии.

## Решение владельца (отдельно, не блокирует)

SKU-день хранения до первого события (сейчас 2,2542 ₽) виден в Executive как хранение уровня счёта, но не в строках SKU.
Если нужно видеть его в экономике SKU, нужно расширить календарь `MART_SKU_DAILY` на первый день хранения. Это
изменение экономики витрины: только с контрольными суммами MART (CLAUDE.md) и отдельным PR.
