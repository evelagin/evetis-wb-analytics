# Executive V2 · Phase C — аудит производительности и доказательство слоя

Скрипты только читают BigQuery и Metabase. Доказательство материализует слой во
временной таблице сессии BigQuery-скрипта (`CREATE TEMP TABLE`), которая удаляется сама.
Постоянных объектов не создаётся.

Рабочий каталог — любой временный. Скрипты ожидают подкаталог `pc/`:

```bash
mkdir -p pc/cards
mb dashboard get 2 --full --json --max-bytes=0 -p evetis-dev > pc/dashboard-2-before.json
# ids карточек dashboard 2 → pc/card_ids.txt (первая строка: id через запятую)
for i in $(head -1 pc/card_ids.txt | tr ',' ' '); do mb card get $i --full --json --max-bytes=0 -p evetis-dev > pc/cards/card-$i.json; done
python3 perf_audit.py 2026-08-31 2026-09-13 8   # 36 dashcard-запросов без кэша, 8 потоков → pc/perf_8.json
python3 proof_gen.py && python3 run_proof.py     # карточки на view vs на прототипе слоя → pc/proof_result.json
```

Прототип слоя: `sql/dash/proposals/executive_v2_daily_PROPOSAL.sql`.
Результаты от 2026-09-16: `docs/EXECUTIVE_V2_PHASE_C_PERFORMANCE_2026-09-16.md`.
