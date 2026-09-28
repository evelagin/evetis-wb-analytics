# SPP-3 — колонка `AB` Юнитки WB из `wb_mart.V_WB_SPP_DAILY` (2026-09-28)

Этап 3 задачи «СПП WB в Юнитку». Этап 1 — ценовые поля заказов (`docs/UNITKA_SPP_1_ORDERS_PROBE_2026-09-28.md`),
этап 2 — вью `wb_mart.V_WB_SPP_DAILY` (`docs/UNITKA_SPP_2_DAILY_VIEW_2026-09-28.md`, развёрнута 28.09).

Статус: код в Engine 2.2.0, по умолчанию `off`. Production — `observe` (план и манифест отката в журнал,
**AB не пишется**). `write` включает только владелец, отдельным решением.

## 1. Что меняется

| Что | Как |
|---|---|
| Где | тот же суточный цикл Gate 10 (`unitka-engine-prod`), отдельного расписания нет |
| Режим | `UNITKA_SPP_MODE = off \| observe \| write`, неверное значение → `off` + предупреждение |
| Источник | `wb_mart.V_WB_SPP_DAILY` (грейн `date_msk × nm_id`) |
| Окно | `max(01.09.2026, кандидат − 34 дня) … кандидат LCD` — то же окно сверки 35 дней |
| Колонка | только `AB` каждого блока SKU (`(col − 13 − 15) mod 24 = 0`) |

**Не меняется:** формулы `AC`, `Z`, `K`, `AH`; `AA`, `Q`, `N`; финансовая модель WB; архитектура LCD;
жизненный цикл месяца; Ozon; PROMO / Gate 11; воронка; загрузка заказов; `V_WB_SPP_DAILY`.

## 2. Контракт записи

```
date < 2026-09-01                         → не трогается никогда
date > кандидата LCD (в т.ч. будущие дни) → не трогается никогда
есть строка V_WB_SPP_DAILY                → AB = ROUND(MAX(effective_spp_pct, 0), 1)   (п.п., 20 = 20 %)
строки нет                                → AB пусто
```

Отрицательная эффективная СПП (рассрочка с наценкой) во вью остаётся отрицательной, в `AB` пишется 0.
Сравнение с листом — допуск 0,0005; пусто = пусто.

Класс каждой изменяемой ячейки:

| Класс | Когда |
|---|---|
| `ACTUAL_FILL` | в листе пусто, во вью есть строка |
| `ACTUAL_REPLACE` | в листе ручное число ≠ факту |
| `NEGATIVE_MARKUP_TO_ZERO` | эффективная СПП < 0 → 0 |
| `MANUAL_CLEAR_NO_ORDER` | ручное число, строки вью нет, `Q = 0` |
| `MANUAL_CLEAR_FUNNEL_ONLY` | ручное число, строки вью нет, `Q > 0` (заказ есть в воронке, нет в Orders API) |
| `PRICE_DATA_MISSING_CLEAR` | строка вью есть, но `effective_spp_pct` пусто |

Не изменяется: `ALREADY_CORRECT` (лист = план), `NO_CHANGE` (пусто и пусто).
`SPP_MISSING` = `Q > 0` без строки вью (`missing_with_q_gt_0`); `Q = 0` без СПП — норма.

Выход за границы (дата < старта, > кандидата, будущее) — отказ `SPP_PLAN_OUT_OF_BOUNDS` до записи.

## 3. Место в цикле (атомарность с LCD)

```
кандидат → готовность → план фактов → план AB (снимки секций окна ДО записи)
  → журнал: unitka_spp_plan + unitka_spp_undo_manifest (снимок отката, ДО записи)
  → ОДНА запись values.batchUpdate RAW: факты + числа AB
  → values.batchClear: очистки AB (формат числа сохраняется)
  → перечитывание → reconciliation + SPP_READBACK (значение и формат каждой ячейки AB)
  → integrity → compare-before-commit → коммит LCD → проверка после коммита
```

- `SPP_READBACK` не пройден → `SPP_READBACK_FAILED`, коммит LCD не начинается (`LCD_NOT_ADVANCED`);
  следующий прогон повторяет тот же кандидат.
- `observe`: план строится, в `AB` ничего не пишется. Сбой плана (вью, права, геометрия) —
  `unitka_spp_plan_failed` (warn), факты и LCD идут как без SPP.
- `write`: сбой плана — отказ **до любой записи** (факты без AB не пишутся).
- Поздние заказы: WB пересчитал прошлый день окна → переписывается ровно эта ячейка.
- Идемпотентность: повтор при неизменном источнике — 0 записей (`SPP_VALUE_MUTATIONS = 0`).

### Почему очистка — `batchClear`, а не запись пустой строки

Измерено на тестовой книге: запись `''` через `values.batchUpdate` **стирает формат числа** ячейки;
`values.batchClear` формат сохраняет. Первая репетиция поймала это на `SPP_READBACK` (260 ячеек) —
LCD не был закоммичен, откат вернул всё. Запись числа RAW формат `#,##0` сохраняет.

## 4. Снимок отката (manifest)

Каждый прогон с изменениями пишет в журнал `unitka_spp_undo_manifest`:
`{ digest, cells, manifest_b64 }`, `manifest_b64 = "gz." + base64url(gzip(canonicalJson))`.

Манифест `unitka-spp/ab-migration` v1: spreadsheetId, sheetId, sheetName, окно, кандидат и по каждой
ячейке — `a1, row, col, month, date, nmId, block, class, previousValue, previousFormula, numberFormat,
newValue`. `digest = SHA-256(canonicalJson)`. Разбор проверяет отпечаток, дату ≥ 01.09 и то, что колонка —
`AB`.

Практика доказательств: при первой записи манифест сохраняется в
`gs://evetis-audit-evidence-37074083763/unitka-spp/<дата>/` вместе с `.sha256`.

## 5. Откат — загрузчик `unitka-spp-rollback`

- По умолчанию ПЛАН (шлюз Sheets readonly): состояние каждой ячейки `AT_NEW / AT_PREVIOUS / DRIFT`.
- `DRIFT` (ячейку после миграции правил кто-то ещё) → `SPP_ROLLBACK_DRIFT`, ничего не пишется.
- Исполнение — только `ENVIRONMENT=prod` + `UNITKA_SPP_ROLLBACK_WRITE=1` +
  `UNITKA_SPP_ROLLBACK_DIGEST` = отпечаток манифеста + без `DRY_RUN`.
- Восстанавливает: числа (RAW), формулы (USER_ENTERED), пустые (batchClear), затем по перечитыванию —
  формат числа (`repeatCell`, `fields: userEnteredFormat.numberFormat`). Проверка после:
  `SPP_ROLLBACK_VERIFY_FAILED` при любом расхождении.
- Трогает только ячейки манифеста. Факты, другие колонки, LCD, Ozon, BigQuery — нет.
- После отката — `UNITKA_SPP_MODE=off`, иначе следующий прогон снова запишет факт.

```bash
gcloud run jobs execute unitka-engine-prod --region europe-west1 --wait \
  --args=unitka-spp-rollback \
  --update-env-vars=UNITKA_SPP_ROLLBACK_MANIFEST=<manifest_b64>,UNITKA_SPP_ROLLBACK_DIGEST=<digest>,UNITKA_SPP_ROLLBACK_WRITE=1
```

Без `UNITKA_SPP_ROLLBACK_WRITE=1` та же команда — только план.

## 6. Репетиция на тестовой книге (28.09, настоящий Sheets API, production BigQuery на чтение)

Книга — копия production (не оригинал). Код — собранный `dist` ветки, записи в BigQuery перехвачены.

| Прогон | Результат |
|---|---|
| 1 (с дефектами) | `SPP_READBACK` FAIL: `''` стёр формат 260 ячеек; LCD **не** закоммичен (`LCD_CANDIDATE`) |
| откат по манифесту `42468a18…` | 260 / 260: значения, формулы, форматы восстановлены; `drift 0`, `verify_failed 0`. Вне AB изменились только пересчитанные значения формул (AC 113, Z 67, итог K 18), текст ни одной формулы не изменился — **ROLLBACK_PROVEN** |
| 2 (исправленный код) | 183 SKU-дня: `ACTUAL_REPLACE 84`, `ACTUAL_FILL 83`, `NEGATIVE_MARKUP_TO_ZERO 1`, `MANUAL_CLEAR_NO_ORDER 158`, `MANUAL_CLEAR_FUNNEL_ONLY 2`, `already_correct 15`, `missing_with_q_gt_0 6`; все счётчики границ 0; `SPP_READBACK` PASS; LCD закоммичен; манифест `0d83eca7…` |
| 3 (повтор) | 0 ячеек, `already_correct 183`, `LCD_NOT_ADVANCED`; отпечатки значений, формул и форматов AB идентичны второму прогону — **идемпотентность** |

Формат `AB` после записи и после повтора — `NUMBER #,##0` (как в production).

## 7. Тесты

`cloud/test/unitka_spp.test.ts` (план, границы, классы, идемпотентность, поздние заказы, readback,
манифест, «доставка не задаёт режим»), `cloud/test/unitka_loader.test.ts` (observe / off / write,
мягкий observe и строгий write при сбое вью, одна запись, атомарность с LCD, batchClear,
`SPP_CLEAR_UNSUPPORTED`), `cloud/test/unitka_spp_rollback.test.ts` (план, восстановление, повтор, drift,
отпечаток / чужая книга / не prod).

## 8. Включение режима

Режим — runtime-значение Job'а, которое ставит оператор (как `UNITKA_RECONCILE_MODE`): ни workflow,
ни Terraform его не задают (статический тест), env Job'а в Terraform — `ignore_changes`, deploy-prod
обновляет только `IMAGE_DIGEST`/`GIT_SHA`.

```bash
gcloud run jobs update unitka-engine-prod --region europe-west1 --update-env-vars UNITKA_SPP_MODE=observe
```

`write` — только отдельным решением владельца, по плану из отчёта observe.
