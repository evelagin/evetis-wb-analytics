# UNITKA ENGINE v1 — Stage E2: LIVE SHADOW 12.09.2026 (без записи в книгу)

**Вердикт: `LIVE SHADOW = PASS` · `SAFE FOR CONTROLLED WRITE = NO (пока)`** — план записи
безопасен и объяснён до последней ячейки, все восемь production-гейтов пройдены, но
облачный контур (deployed `unitka-engine-shadow`, Sheets API под сервисным аккаунтом,
воронка по расписанию) ещё не проверен: у этой сессии нет доступа к GitHub Actions и GCP,
а Drive отказал в выдаче прав сервисным аккаунтам. Что сделано, что заблокировано и что
именно нужно от владельца — §7.

## 1. Как выполнен shadow

Реальный код Engine (`dist/loaders/unitka`: `UnitkaBq → buildPlan → evaluate{shadow}`),
запущенный через `cloud/scripts/unitka_offline_shadow.mjs` на **живых** данных того же момента:

* книга — экспорт `Юнитка_Evetis Cosmetics` в xlsx через Drive API **12.09 08:17 UTC**
  (значения и формулы строк 735–767, зеркала `WB736`/`WB737`, `ZZ_CONFIG!B2`);
* BigQuery — строки пяти вью `wb_mart.V_UNITKA_*` **12.09 08:17:46 UTC**.

Отличие от deployed-прогона: чтение книги шло через экспорт, а не через Sheets API под
`sa-loaders-shadow`; логика, план и QA — те же байты кода. Ни одной записи ни в книгу, ни в
BigQuery. Артефакты: `docs/unitka_engine_evidence/live_shadow_e2_2026-09-12_{diff.csv,report.json,profit.json}`.

## 2. Состояние источников на момент прогона

```
now MSK ............ 12.09.2026 11:17
funnel ............. 10.09  gating   (единственный прогон wb-funnel-prod: 11.09 06:50 UTC, Scheduler на паузе)
mart ............... 11.09  gating   (MART_RUNS COMPLETE 12.09 04:03 UTC)
orders/FACT_ORDERS . 11.09           stocks 12.09 · finance 11.09 · storage 09.09
LAST_CLOSED_DATE ... 10.09  = LCD книги (WB736 = ZZ_CONFIG!B2 = 10.09)   lag 1 день (норма ≤ 2)
```

⚠ Воронка стоит на 10.09, потому что Scheduler `wb-funnel-prod` на паузе. Отставание от D-1
сегодня 1 день, завтра 2 (ещё норма), **с 14.09 Engine честно падает `SOURCE_STALE`** — снять паузу
могу не я (§7).

## 3. Восемь production-гейтов

| гейт | результат |
|---|---|
| `BLOCKS = 24/24` | **PASS** — 24 nmID в строке 735, даты 01–30.09 во всех блоках и сводке |
| `BQ → SHEETS planned reconciliation` | **PASS** — контракт 3 603 ячейки; расхождений 668, каждое классифицировано (§4), после записи станет 0 |
| `FORMULA ERRORS = 0` | **PASS** — 0 ошибок в 735–767 × 589 колонок |
| `FUTURE LEAKAGE = 0` | **PASS** — фактов за датами > 10.09 нет; 455 формульных проекций остатка — KEEP (решение владельца), считаются отдельно `STOCK_PROJECTION_FUTURE`; **фактического хранения в будущих днях нет** (20 будущих формул хранения дают `""`) |
| `SUMMARY RECONCILIATION = PASS` | **PASS** — 10 закрытых дней × 8 колонок сводки = Σ блоков, `max|Δ| = 0`; `I767 = −7 544,57` = Σ дневных |
| `DIRECT LOGISTICS INVARIANT = PASS` | **PASS** — `597 = 545 + 52`, окно 12.08–10.09 |
| `NO DUPLICATE date×nmID` | **PASS** — 250 строк вью, дублей 0 |
| `LAST_CLOSED_DATE CONSISTENT` | **PASS** — имя = зеркало = вычисленное = 10.09 |

## 4. Diff plan — 668 ячеек, полный список в `live_shadow_e2_2026-09-12_diff.csv`

Колонки: `DATE · SKU · CELL · METRIC · OLD · NEW · CHANGE_TYPE · SOURCE · REASON`.

| CHANGE_TYPE | ячеек | что это |
|---|---|---|
| `FACT_CHANGE` | **0** | нового закрытого дня нет: LCD книги 10.09 = LCD BigQuery |
| `LATE_SOURCE_CORRECTION` | **2** | `KU743` и `AG746` — §5 |
| `MODEL_PARAMETER_REFRESH` | **600** | комиссия: 20 блоков × 30 строк — §5 |
| `LCD_ADVANCE` | **0** | — |
| `NO_CHANGE` | **66** | формула-наследие → то же значение (54 `storage` = 0 в шести блоках за 01–09.09, 12 `stock` 10.09) |
| *(не в плане)* `NO_CHANGE` по значению | 2 935 | ячейки контракта, уже равные BigQuery |

Логистика: 720 ячеек — **0** изменений (ставки в книге уже от окна 12.08–10.09). `REVERSE_LEG_RATE` 32,5256 — без изменений.

## 5. Особые пункты

**Поздняя корректировка `KU743` (773170315 «Набор тоник+крем УВЛ», отмены 07.09: `0 → 1`).**
`FACT_ORDERS`: заказ `eBh.re8c…` от 07.09 на 1 267 ₽, `is_cancel = true`, `cancel_dt = 2026-09-11`,
витрина перестроена 12.09 04:00 UTC — уже после того, как `s82data` загрузил 07.09 в книгу (11.09).
Это ровно случай «WB пересчитал задним числом», ради которого окно факта — весь месяц.
Эффект по формуле Master: `profitAll = 1×289,89 − 1×(289,89 + 60,55 + 32,53) = −93,08` вместо
`+289,89` → **−382,97 ₽** (отменённый заказ теряет юнит-прибыль и платит оба плеча).

**Хранение 10.09 `AG746` (252442517 «Крем для рук»): `20,7 → пусто`.** В ячейке формула-наследие
`=IF($M746>LAST_CLOSED_DATE,"",T746*0.15)`; при загрузке 11.09 LCD был 09.09 и формула давала
`""` (= GAP, ячейку не перезаписали); после `s82lcd` она стала считать `остаток × 0,15 = 20,7`.
Источник хранения стоит на 09.09 → BigQuery за 10.09 даёт GAP → Engine очищает ячейку.
Эффект **+20,70 ₽**. Когда `RAW_WB_PAID_STORAGE` догрузит 10.09, Engine впишет факт.

**Комиссия: меняется 600 ячеек = 20 блоков × 30 строк** (4 блока не меняются). Магазинная
ставка `0,455916 → 0,45578`, своя у 252442517 `0,454322 → 0,453985` и т.д. Причина — только
сдвиг rolling-окна: книга от `s8rates()` при LCD 09.09 (11.08–09.09), Engine — 12.08–10.09;
формула `ROUND(cpw/base/100,6)+ROUND(acq/base,6)` побайтово та же (проверено прямым запросом).
Классифицировано `MODEL_PARAMETER_REFRESH` — допустимое обновление параметра модели.
Эффект **+20,75 ₽** (ставки в среднем чуть ниже).

**Прибыль магазина ДО и ПОСЛЕ предполагаемой записи** — пересчёт формул Master в LibreOffice
(headless, forced recalc) на копии экспорта; метод проверен: «до» воспроизводит `I767 = −7 544,57`
и `Σ W767 по 24 блокам` с точностью до копейки.

```
ДО      MTD прибыль магазина (Σ profitAll 24 блоков, 01–10.09) ...  −7 544,57 ₽
ПОСЛЕ   ....................................................  −7 886,26 ₽
Δ       ....................................................    −341,69 ₽

расшифровка (каждая группа применена отдельно):
  LATE_SOURCE_CORRECTION  KU743  отмена 07.09 .............  −382,97
  LATE_SOURCE_CORRECTION  AG746  хранение 10.09 → GAP ......   +20,70
  MODEL_PARAMETER_REFRESH комиссия, 600 ячеек .............   +20,75
  NO_CHANGE               66 формул → значение .............     0,00
  взаимодействие групп (округление/пересечение) ...........    −0,17
```

По дням: 01–06.09 и 08–09.09 — от +0,83 до +3,13 ₽ (комиссия); 07.09 **−381,62** (отмена);
10.09 +22,51 (хранение + комиссия). По блокам: 773170315 −382,80; 252442517 +32,37; остальные
в пределах ±2,6 ₽.

## 6. Формулы-наследие (инвентарь, решение KEEP выполнено в коде)

`stock` 487 формул (12 в закрытых днях, 475 в будущих — проекция `=вчера − заказы + отмены`),
`storage` 75 (55 в закрытых, 20 в будущих, все будущие дают `""`). Engine: закрытые дни → ACTUAL
из BigQuery (66 из них сегодня с тем же значением, `NO_CHANGE`), будущие дни → не трогает,
проекция остатка ≠ утечка; любое другое непустое значение в будущем дне (в т.ч. хранение) —
`FUTURE_LEAKAGE`. Тесты: `unitka_plan.test.ts` («KEEP только для остатка»).

## 7. Что сделано программно, что заблокировано, что нужно от владельца

| шаг E2 | статус | детали |
|---|---|---|
| 1 targeted plan | **инструмент готов, план не запущен** | `infra.yml` получил вход `targets` (`-target=…`); список адресов ниже. Нет GitHub-доступа из сессии |
| 2 снять паузу `wb-funnel-prod` | **не выполнено** | нужен `scheduler-control.yml` (loader `wb-funnel`, prod, resume) — GitHub |
| 3 свежий прогон воронки | **проверено: его нет** | `LOADER_RUNS`: один прогон 11.09; воронка на 10.09 |
| 4 infra apply | **не выполнено** | после targeted plan — GitHub/WIF |
| 5 доступ книги SA | **не выполнено, STOP** | Drive API из сессии: `The caller does not have permission` на оба share — нужен владелец |
| 6 deploy shadow | **не выполнено** | `deploy-shadow.yml` — GitHub |
| 7 LIVE SHADOW | **выполнено** реальным кодом на живых данных (см. §1) | deployed-прогон — после 4–6 |
| 8–10 | **выполнено** | §3–§5 |
| 11–13 | **соблюдено** | `UNITKA_WRITE_ENABLED` не включён, prod Job не запускался, октябрь не начат, в книгу не записано ничего |

### Пошагово для владельца

1. **Книга → «Настройки доступа»** (`1E4L4JuwfEqr9owhsGkAjb8F24lRpWpWkEyVmSuRxaJg`):
   `sa-loaders-shadow@project-fa311fc0-4d87-4781-986.iam.gserviceaccount.com` — **Читатель**;
   `sa-loaders-prod@project-fa311fc0-4d87-4781-986.iam.gserviceaccount.com` — **Редактор**;
   уведомление снять.
2. Запушить ветку `feat/unitka-2.0-september-master` (коммиты `30624d0`, E2) и слить в `main`
   (WIF apply работает только с `main`).
3. `infra.yml` → `action=plan`, `targets=` (одной строкой, через запятую):
   ```
   google_project_service.enabled["sheets.googleapis.com"],google_project_service.enabled["monitoring.googleapis.com"],google_bigquery_table.unitka_engine_runs,google_bigquery_table_iam_member.unitka_runs_write["shadow"],google_bigquery_table_iam_member.unitka_runs_write["prod"],google_bigquery_dataset_iam_member.unitka_shadow_view_raw,google_bigquery_dataset_iam_member.unitka_shadow_view_mart,google_cloud_run_v2_job.unitka_engine_shadow,google_cloud_run_v2_job.unitka_engine_prod,google_cloud_run_v2_job_iam_member.scheduler_unitka_shadow_invoke,google_cloud_run_v2_job_iam_member.scheduler_unitka_prod_invoke,google_cloud_scheduler_job.unitka_engine_shadow["morning"],google_cloud_scheduler_job.unitka_engine_shadow["reserve"],google_cloud_scheduler_job.unitka_engine_prod["morning"],google_cloud_scheduler_job.unitka_engine_prod["reserve"]
   ```
   Ожидание: **15 to add, 0 to change, 0 to destroy** (плюс 2 add, если задан
   `unitka_alert_email`: `google_monitoring_notification_channel.unitka_email[0]`,
   `google_monitoring_alert_policy.unitka_engine_failed[0]`). Любой `change/destroy` — стоп.
4. `infra.yml` → `action=apply`, те же `targets`.
5. `scheduler-control.yml` → `wb-funnel`, `prod`, `resume`; на следующее утро проверить
   `wb_raw.LOADER_RUNS` (`funnel`, `COMPLETE`) и `V_UNITKA_SOURCE_FRESHNESS.funnel = D-1`.
6. `bq query < sql/unitka/engine_v1_views.sql` — блок 6 (`V_UNITKA_ENGINE_STATUS`).
7. `deploy-shadow.yml` (main) → `scheduler-control.yml` → `unitka-engine`, `shadow`, `run-now`.
   Ожидание в `wb_ops.UNITKA_ENGINE_RUNS`: `mode=SHADOW`, `qa_status=SHADOW_DIFF`,
   `cells_planned` = этот же план (668 при неизменных источниках; после включения воронки —
   плюс `FACT_CHANGE` нового дня и `LCD_ADVANCE = 2`).
8. Совпал — `SAFE FOR CONTROLLED WRITE = YES`; дальше по runbook §2 (шаги 8–12).

Не делать: `UNITKA_WRITE_ENABLED`, запуск `unitka-engine-prod`, октябрь.
