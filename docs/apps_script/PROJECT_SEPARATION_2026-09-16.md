# Apps Script — разделение проектов (Step 5B, 16.09.2026)

Репозиторий приведён к трём независимым деревьям исходников, по одному на живой
Apps Script проект. Production Apps Script, триггеры, Script Properties, BigQuery
и Google Sheets в рамках этой работы НЕ изменялись.

## Карта: дерево → production-проект

| Дерево | Проект | Script ID | Привязка |
|---|---|---|---|
| `apps-script/ingestion/` | evetis-wb-analitics | `10fr6u1YVdrTvZo7bkd88K4Vuv_-HiPerDbTw-9S9N34AUDQtfg6FtDkS` | container-bound → «Evetis аналитика 2.0» `1MLOCJEX0UR0Gj9gUcDbTBfPrtAhqQc2ctcsoaepSTiU` |
| `apps-script/unitka/` | (без названия) | `1DrcKy55LH5TrN3aPz6l18WpfPIOCUrRMEy95SP6TQXxaWWXtsPW3SBW3` | **standalone**, `openById` → «Юнитка_Evetis Cosmetics» `1E4L4JuwfEqr9owhsGkAjb8F24lRpWpWkEyVmSuRxaJg` |
| `apps-script/operations/` | EVETIS OPERATIONS · C2 | `1z4SVOytTs2F8VsHnRKhLrWb0Jw1nthFGyhifhMYEqrnKjKEjvhhrHy3W` | container-bound → «EVETIS OPERATIONS» `19J8EW-Xqz_twHk4ID7IvkumFOop4MTTZeX6U0uaNSAc` |

## Excluded legacy projects (OWNER DECISION 2 — HISTORICAL / FROZEN)

Эти проекты живые в Google, но **вне репозитория и вне clasp**. Их код не
переносится в git, не меняется и не смешивается с тремя актуальными деревьями.

| Проект | Script ID | Книга | Последнее изменение |
|---|---|---|---|
| Evetis Ozon | `1MemRXRCwnjwgXVsROYXMHHi3jDLmx5fyaVJxnnQaI4PhQrcDRS3jcqhq` | «EVETIS OZON» `1tzEB5QfeCKmMA4g-fh0liMKQXJfN3Xo6T-ghbKg9Tis` | 19.05.2026 |
| Evetis WB | `1k1gICszkM5vYLI0184OsEdytVJsY4DF_o7P6raMd4Xj3bcgl_XXwjeoz` | «EVETIS WB» `1Z2WDKNrLbaOipTRRpuewwIP4QhyHNyOfJuuOpCHh2U8` | 18.05.2026 |
| EVETIS OPERATIONS · C2 TEST | `12qeynQ7U_Ub-Pz4pvZn2N45epYM0uwHmjeSMeB_vFxfRTEMJMGBSrNCg` | тестовая копия `1tViXESeyAyfab96ZQyMKYq9UPrU9On7ZpN7IzElXyCU` | 14.09.2026 |
| (без названия, проба WB Advert API) | `19Pjunnep_pXESar9pbRwcTOr2cskoKMcgVaU_HBV82allQJHuJUYIHiA` | — | 04.05.2026 |
| (без названия ×2) | `1xkRQZdk…`, `1Okyjkfv…` | — | 05.2026 |

🔒 В проекте `19Pjunne…` в открытом виде лежит рабочий bearer-токен WB Advert API.
Отдельная задача безопасности, см. `docs/ops/SECURITY_BACKLOG.md`.

## Файлы, оставленные в корне `apps-script/` (не отнесены ни к одному дереву)

| Файл | Вердикт | Основание |
|---|---|---|
| `CtOwnerActions.gs` | DEAD-OR-OBSOLETE | `docs/control_tower/OPS_ARCHITECTURE_REVIEW_2026-09-11.md`: «never deployed», DEPRECATE; заменён Sheets-bound скриптом OPERATIONS C2 |
| `UnitkaFill.gs` | UNRESOLVED | самодостаточен (нет внешних зависимостей), отсутствует во всех production-проектах |
| `UnitkaR2.gs` | UNRESOLVED | то же |
| `UnitkaR4.gs` | UNRESOLVED | то же |

Они намеренно лежат вне трёх `rootDir`, поэтому `clasp push` их не видит.

## Известный production drift (в этой фазе НЕ исправляется)

| Файл | Проект | Состояние |
|---|---|---|
| `UnitkaR7` | INGESTION | пустая заглушка `function myFunction(){}` (3 строки) — остаток от создания файла. Канонический UnitkaR7 живёт в UNITKA. Удаление из INGESTION безопасно (доказано: ни один из 87 остальных файлов проекта не ссылается на её символы), но это production-изменение |
| `myFunction` | INGESTION | объявлена ДВАЖДЫ: в `UnitkaR7` (пустая) и в `UnitkaR3Ingest:118` (`{ r4FunnelProbe(); }`). В едином глобальном пространстве Apps Script побеждает загруженная позже — латентный дефект |
| Step 5A (5 файлов ADS/журнала) | INGESTION | репозиторий впереди прода: деплой Step 5A не выполнен |
| `UnitkaE6`, `UnitkaE6Run` | UNITKA | репозиторий впереди прода |
| `UnitkaR3Ingest`, `UnitkaR5`, `UnitkaR6`, `WbBigQuery`, `WbStocksBigQuery` | INGESTION | репозиторий впереди прода |
| 12 файлов PROD_ONLY | INGESTION | пробы и `*_TEMP`, которых нет в git |
