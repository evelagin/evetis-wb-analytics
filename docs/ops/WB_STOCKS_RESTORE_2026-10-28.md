# WB остатки · восстановление снимка 28.10.2026 из shadow — пакет выпуска (только по OWNER ACK)

Статус: **НЕ выполнено.** Скрипты: `sql/recovery/wb_stocks_restore_from_shadow_v1.sql`, откат —
`sql/recovery/wb_stocks_restore_from_shadow_v1_rollback.sql`. Оба проходят `bq query --dry_run`.

## Потребители и совместимость (live, 28.10.2026, только чтение)

Поиск по `INFORMATION_SCHEMA.VIEWS/ROUTINES` всех датасетов и по коду репозитория. Ни один потребитель не фильтрует
`snapshot_id`/`load_id` по шаблону (`STOCK_SNAP_2…`, `STOCK_LOAD_…`), ни один не читает `error_message` манифеста остатков.

| Потребитель | Как выбирает снимок | Увидит 28.10 после восстановления |
|---|---|---|
| `wb_mart.sp_bootstrap_facts` → `FACT_STOCKS_SNAPSHOT` (MART, полная пересборка, `wb-mart-prod` 07/09/12/16 МСК) | COMPLETE, последний по `started_at` на `period_to` | да, `STOCK_SNAP_shadow_20261028`, 22 строки / 1 106 шт. |
| `wb_raw.V_WB_STOCKS_CURRENT`, `V_WB_STOCKS_T5_CURRENT` (CT, `ops_08_supply_plan`) | COMPLETE, последний по `completed_at` | да, пока не придёт 29.10 |
| Unitka (поле «остаток») | `FACT_STOCKS_SNAPSHOT` за дату | да, после ближайшей сборки MART |
| `store_inventory_loss_v1` / `V_WB_INVENTORY_LOSS_MONTHLY` | T5, `MAX(snapshot_id)` на дату | да (на дату один снимок — предусловие) |
| DRO-1 `TVF_PIPELINE_PROBE` | наличие RAW за `_snapshot_date` при `snapshot_ts ≤ as_of`; полнота — последний COMPLETE по `completed_at` | да: 28.10 → present, `control_status` OK |
| Apps Script `availability_gap_prev` | последний COMPLETE с `availability_gap` | да: у shadow 28.10 `availability_gap = 0` |

Симуляция (SELECT над боевыми таблицами ∪ строки, которые запишет скрипт) дала для 28.10 одинаковый результат во всех
трёх способах выбора: `STOCK_SNAP_shadow_20261028`, 22 / 1 106.

Порядок снимков сохраняется: `completed_at` и `snapshot_ts` копируются из shadow (28.10 03:30 UTC), поэтому восстановление,
выполненное даже после снимка 29.10, не сделает 28.10 «текущим». Манифест shadow совпадает с боевым по всем контрольным
полям за 26–27.10 (control/availability, суммы T5/T6, число строк T5); отличие только `error_message` `''` против NULL.

Единственный производитель остаётся один: Cloud Run `wb-stocks-prod` в Scheduler — PAUSED; восстановление — разовая запись.

## DRO-1

Сейчас (14:30 UTC): `stocks_snapshot` — MISSING / DATA_LOSS_IMMINENT, `loss_deadline` 29.10 00:00 МСК.
После восстановления 28.10 становится COMPLETE в `V_DATA_PERIOD_STATE` (наличие проверяется по RAW, не по сроку).
**Лучше выполнить до 29.10 00:00 МСК (21:00 UTC 28.10):** после срока DRO-1 повысит инцидент до потери данных.

## 03.09 не затрагивается

- В shadow снимок 03.09 **есть** (22 строки), боевой манифест 03.09 — ERROR, RAW пуст: предусловия скрипта 03.09 не отсекают.
  Поэтому оба скрипта начинаются с `ASSERT p_date IN (DATE '2026-10-28')`. Новая дата — только отдельный ACK и правка списка.
- Readback внутри транзакции: общее число строк RAW / RAW_T5 = до + n, манифест = до + 1. Другие партиции изменить нельзя.
- Тест `tools/tests/test_wb_stocks_restore_sql.py` (список дат, запись только трёх таблиц в одной транзакции, источник только
  `__CR`, откат только строк восстановления) — 5/5 на новой версии, 2 падения на версии без списка дат.

## Откат

`wb_stocks_restore_from_shadow_v1_rollback.sql`: одна транзакция, удаляет строки с `load_id = RESTORE_FROM_SHADOW_20261028`
в партиции 28.10 и манифест `STOCK_SNAP_shadow_20261028` с отметкой `RESTORED_FROM_SHADOW`; readback по общим числам строк.
Строки вставлены DML (не потоковой вставкой), поэтому DELETE доступен сразу. После отката ближайшая сборка MART снова
уберёт 28.10 из `FACT_STOCKS_SNAPSHOT`.

## Приёмка

1. Вывод скрипта без ошибки; `SELECT COUNT(*), SUM(quantity) FROM RAW_WB_STOCKS WHERE _snapshot_date='2026-10-28'` = 22 / 1 106;
   RAW_T5 за 28.10 = 204; COMPLETE-манифест за 28.10 ровно один.
2. Сразу: `V_WB_STOCKS_CURRENT` → `STOCK_SNAP_shadow_20261028`.
3. После ближайшего `wb-mart-prod`: `FACT_STOCKS_SNAPSHOT` за 28.10 = 22 / 1 106.
4. Ближайший `dro-health-eval` (каждые 30 мин): `V_DATA_PERIOD_STATE` 28.10 → COMPLETE, инцидент закрыт.
