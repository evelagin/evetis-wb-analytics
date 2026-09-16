# Step 5A — offline-харнесс для Apps Script

Node-`vm` харнесс, который грузит **реальные** `.gs` из `apps-script/ingestion/` и прогоняет
15 регрессий на режимы отказа, вскрытые инцидентами 11.09 и 15.09.2026.
Внешние зависимости не нужны: BigQuery, Sheets, UrlFetch и часы подменены в `harness.js`.

```bash
node tools/step5a_appsscript_tests/run.js            # исходники репозитория
node tools/step5a_appsscript_tests/run.js <каталог>  # другой каталог (напр. выгрузка production)
```

Что покрыто:

| # | Проверка |
|---|---|
| 01–05 | state-machine reaper: stale STARTED → ERROR/STALE_RUN_TIMEOUT с provenance; COMPLETE и ERROR неизменяемы; свежий STARTED не трогается; повтор идемпотентен |
| 05b | порог < 15 мин отвергается; без activation-свойства reaper выключен |
| 05c | исторические STARTED до activation не закрываются |
| 05d | `source='cloud_run'` (LOADER_RUNS) не затрагивается |
| 06+07 | hard kill → гейт закрыт; reaper → гейт ВСЁ ЕЩЁ закрыт (не fail-open); retry → COMPLETE → гейт открыт |
| 06b | решение catch-up: COMPLETE→skip, свежий STARTED→skip, ERROR/старый→run |
| 08 | окно costs = D−14 … D−1 |
| 08b | trigger provenance: ручной запуск → MANUAL, триггер → SCHEDULED |
| 10 | нормальный WB (3 c/запрос, 431 id): все 9 вызовов, без остановки |
| 11 | медленный WB (25 c/запрос): критичный путь оставляет место под финализацию (сверяется с `WB_ADS_FINALIZE_RESERVE_MS_`, не с константой в тесте) |
| 11b | хвост (query_stats) не стартует у стены прогона |

`probe_extract.gs` — минимальная выжимка `wbAdsLast7Range_`/`wbAdsCostsRangeBack_` из
`WbAdsProbe.gs`, чтобы не тянуть в песочницу весь probe-файл.
