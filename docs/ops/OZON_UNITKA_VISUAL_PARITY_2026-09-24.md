# OZON_Юнит_2025 → визуальный стандарт WB_Юнит_2025 — PRE_PRODUCTION_VISUAL_GATE

Дата: 2026-09-24. Ветка `feat/ozon-unitka-visual-parity` от `origin/main` `be32082`
(отдельный worktree, Gate 10 не затронут). Задача только про оформление: данные, формулы,
экономика, объединения, логика УФ и жизненный цикл не менялись.

Production-книга за время работы **не изменялась**: отпечатки листа Ozon до и после сессии
совпали по всем 14 показателям (значения, формулы, объединения, именованные диапазоны, УФ,
хвост владельца, высоты, ширины, скрытые колонки, группы). Все записи — только в тестовую
книгу `1HTgd__…` на новый лист `OZON_VISUAL_REHEARSAL` (sheetId 838700532).

## 1. Что обнаружено (живой Sheets API, а не скриншоты)

| свойство | WB (эталон) | Ozon сейчас | вывод |
|---|---|---|---|
| ширины колонок (84 / 104 «Доходность общая» / 100 «Реклама» / 56 день недели; сводка 73‥100) | измерено | **те же** | горизонтальной разницы нет |
| ширина блока SKU | 2 024 px (24 кол.) | 2 108 px (25 кол.) | +84 px — это колонка «Прочие прямые», у WB её нет |
| шрифты (Calibri 20 / 12 / 16 / 14 / 11, bold/italic, перенос, выравнивание) | измерено | **те же** | типографика совпадает |
| строка дня, API `pixelSize` | 18 (Sheets подгоняет по содержимому) | **25, приколочено** | |
| строка дня, отрисовка (скриншот владельца 65 %, калибровка по колонке 84 px) | **≈ 20,5 px** | 25,0 px | **Ozon выше на 22 %** — главная причина «рыхлости» |
| заголовок месяца / шапка / итог | Сентябрь: 18/76/18 (рисуется ≈34/96/27); Октябрь (текущее поколение): **40/108/33** | 40/108/33 | равно текущему поколению WB |
| цвет рамок | контур заголовка SKU и итога `#5f6368`, сетка шапки `#9aa0a6`, решётка дня чёрная | **всё чёрное** | Ozon тяжелее по линиям |

Высота секции месяца (30 дней): WB Сентябрь ≈ 790 px, WB Октябрь (31 день) ≈ 835 px;
Ozon Сентябрь **949 → 829 px** после изменения (−120 px, −12,6 %).

Причины:
1. `OZON_ROW_GEOMETRY.day = 25` было выведено в Gate 5D из PDF-экспорта (шаг WB 18,88 pt);
   в браузере это не подтвердилось.
2. `CANONICAL_CURRENT_WB_PRESENTATION_CONTRACT` снимал у рамок только **стиль**, без цвета,
   поэтому `toUserEnteredFormat` писал все рамки чёрными.

Серверный `autoResizeDimensions` для замера не годится: на репетиции он поставил 20 px всем
строкам, включая заголовок 20 пт — он не учитывает кегль.

## 2. Токены

```
WB_VISUAL_TOKENS
  widths: DATE..METRIC 84, TOTAL_PROFIT 104, INTERNAL_ADS 100, WEEKDAY 56; summary 73/73/92/102/82/85/85/85/104/100
  rows:   TITLE 40, HEADER 108, DAY ≈20.5 (render; API 18 + autofit), MTD 33, SPACER 18
  fonts:  Calibri; title 20 b/i; header 12 b wrap; day 12; mtd 16/12/14/11 b
  borders: day lattice SOLID #000000 (theme TEXT); SECTION/SKU contour SOLID_MEDIUM #5f6368; header lattice SOLID #9aa0a6
OZON_CURRENT_TOKENS: widths = WB; fonts = WB; rows 40/108/25/33/18; all borders #000000
PROPOSED_OZON_TOKENS: widths = WB; fonts = WB; rows 40/108/21/33/18; border colours = WB (162 сторон в контракте)
```

`OZON_VISUAL_EXCEPTIONS` — нет новых. Уже существующие и обоснованные ранее:
- `role=OTHER_DIRECT WB_width=— OZON_width=84 reason=25-я колонка Ozon, оформление от STORAGE (Gate 7)`;
- семантические расхождения фона дня (`OZON_VISUAL_DIVERGENCES`: цена, СПП, комиссия, логистика —
  факт; корзина, остаток — расчёт; внешняя реклама — ручная);
- «короб» прочих прямых в строке итога (правая стена переезжает с «Хранения», Gate 7).

## 3. Изменения кода (только визуальные свойства)

| файл | изменение |
|---|---|
| `cloud/src/loaders/unitka/ozon/presentation.ts` | `OZON_ROW_GEOMETRY.day` 25 → **21**; комментарий с новым замером |
| `cloud/src/loaders/unitka/ozon/wbcontract.ts` | `CellFormatSpec.borderColors`; повторный замер WB Сентябрь добавил цвета 162 сторонам (только вставки, стили не изменились; скрипт `gen_colours.mjs` падает, если сериализация не воспроизводит файл) |
| `cloud/src/loaders/unitka/ozon/structure.ts` | `toUserEnteredFormat` пишет `colorStyle` стороны, если цвет задан; чёрные стороны — как раньше |
| `cloud/test/unitka_ozon.test.ts` | высоты 40/108/21/33/18; палитра рамок ровно `#5f6368`,`#9aa0a6`; цвет только у стороны со стилем; «Прочие прямые» наследуют цвет «Хранения» |

`FORMULA_CHANGES=0 VALUE_CHANGES=0 FINANCIAL_LOGIC_CHANGES=0 STRUCTURAL_DATA_CHANGES=0`.
Окно перезаписи, LCD, порядок фаз, УФ, объединения — не тронуты. `tsc` чистый,
vitest 1381 passed / 19 skipped (как до изменения).

Кто контролирует что:
- **высоты строк** — писатель ставит каждый прогон для ВСЕХ секций (`rowHeightRequests(allSections)`);
- **статический формат (рамки, шрифты, фоны)** — писатель ставит только секциям окна
  (45 суток; 1-го числа — 120 суток);
- **только в книге**: легаси-оформление секций май 2025 – апрель 2026, свёрнутые группы и
  скрытые колонки (состояние просмотра владельца), пер-SKU цвета заголовков WB.

## 4. Репетиция

1. Точная копия: `sheets.copyTo` листа production в тестовую книгу; `copyTo` превратил 32 967 ссылок
   на `OZON_LAST_CLOSED_DATE` в `#REF!` — восстановлены `updateCells(userEnteredValue)` (без сброса
   формата). После: формулы = production (0 расхождений), значения = production,
   `userEnteredFormat` строк 570–650 = production (0 из 44 409), УФ = production (с точностью до sheetId).
2. Миграция каноническим форматтером (harness `visual_parity_2026-09-24/harness.mjs`):
   `rowHeightRequests` для 17 секций + свойство `borders` из `staticFormatRequests` для
   канонических секций май–сентябрь 2026 (`fields=userEnteredFormat.borders`, остальное не трогается).
   Секции 2025 – апрель 2026 оформлены по-старому и не перекрашиваются.
3. Результат по всему листу: изменились только рамки (14 062 ячейки, строки 466–637),
   **стиль — 0 изменений**, цвет: чёрный → `#5f6368` 16 546 сторон, чёрный → `#9aa0a6` 8 100
   (включая общие рёбра соседей); высоты — только строки дня 25 → 21 (518 строк).
4. Readback: 332 002 стороны рамок и 586 высот строк = плану, расхождений 0.
5. Паритет по ролям с WB Сентябрь: было 76 ролей с видимым отличием, стало 28 — все известны
   (раздел 2, плюс пер-SKU цвет заголовка блока у WB и знак прибыли через УФ у Ozon).
6. Писатель Ozon (новый код, реальный BigQuery, запись только в TEST): окно 10.08–23.09,
   32 538 ячеек, PRE_COMMIT PASS, `LCD_NOT_ADVANCED`. После него формат изменился в **0** ячейках,
   значения 0, формулы 0, высоты те же.

Проверены все блоки (1, средние, 22-й), сводка, строка итога, хвост владельца (отпечаток
неизменен), сентябрь и август; октябрь у Ozon ещё не создан — его создаст писатель 01.10 уже с
новыми токенами.

## 5. PRE_PRODUCTION_VISUAL_GATE

```
REFERENCE_WB_SHEET=WB_Юнит_2025 (739487431)
TARGET_OZON_SHEET=OZON_Юнит_2025 (1867941682)

WB_BLOCK_PIXEL_WIDTH=2024
OZON_BLOCK_PIXEL_WIDTH_BEFORE=2108
OZON_BLOCK_PIXEL_WIDTH_AFTER=2108          (+84 = «Прочие прямые»)

WB_NORMAL_ROW_HEIGHT=18 API / ≈20.5 rendered
OZON_NORMAL_ROW_HEIGHT_BEFORE=25
OZON_NORMAL_ROW_HEIGHT_AFTER=21

WB_HEADER_HEIGHT=108 (Oct, current generation; Sep 76 API / ≈96 rendered)
OZON_HEADER_HEIGHT_BEFORE=108
OZON_HEADER_HEIGHT_AFTER=108

WB_MONTH_SECTION_PIXEL_HEIGHT≈790 (Sep, 30 d) / ≈835 (Oct, 31 d)
OZON_MONTH_SECTION_PIXEL_HEIGHT=949 → 829 (Sep, 30 d)

COLUMN_WIDTH_CHANGES=0
ROW_HEIGHT_CHANGES=518 day rows 25→21 (17 sections)
TYPOGRAPHY_CHANGES=0
BORDER_CHANGES=colour only, 5 canonical sections (May–Sep 2026): black→#5f6368 / #9aa0a6
ALIGNMENT_CHANGES=0
FILL_CHANGES=0

OZON_VISUAL_EXCEPTIONS=none new (OTHER_DIRECT from Gate 7; semantic fills; MTD box)

REHEARSAL_VALUE_MUTATIONS=0
REHEARSAL_FORMULA_MUTATIONS=0
REHEARSAL_MERGE_MUTATIONS=0
REHEARSAL_NAMED_RANGE_MUTATIONS=0
REHEARSAL_CF_LOGIC_MUTATIONS=0
REHEARSAL_OWNER_DATA_MUTATIONS=0

VISUAL_READBACK=PASS (332002 border sides + 586 row heights, 0 mismatches)
VISUAL_DRIFT_AFTER_WRITER=0

FORMATTER_CODE_CHANGES=presentation.ts (day 21), wbcontract.ts (+borderColors), structure.ts (colorStyle), tests

PROPOSED_PRODUCTION_FORMAT_MUTATIONS=10284 batchUpdate requests, sha256 of plan 57d23e48…220b
  = 85 updateDimensionProperties (ROWS) + 10199 repeatCell fields=userEnteredFormat.borders
  (byte-identical to the rehearsed plan, sheetId-normalised)

PRODUCTION_VISUAL_READY=YES
```

## 6. Решения владельца (2026-09-24)

1. Приняты: строка дня 25 → 21 px; цвета рамок из канонического контракта WB. Ширины,
   типографика, выравнивание, заливки, семантика УФ, формулы, значения, имена, объединения,
   хвост владельца, жизненный цикл, финансы и логика Gate 10 — без изменений.
2. Пер-SKU цвета заголовков блоков WB (`#dce8f2`, `#f3e2e6`, `#f7ead9`, `#e2eee4`, `#e6e3f2`,
   `#efeae1`, `#eaeaee`) **не переносятся**: у Ozon остаётся единый голубой `#dce8f2`. Это
   отдельное возможное дизайн-решение, не часть этого PR.
3. **Production 24.09 не меняется.** 25.09 в 10:00 МСК идёт первый штатный `LCD_COMMITTED`
   Gate 10 для WB и Ozon — косметический вывод с ним не смешивается.

## 7. Порядок вывода (после приёмки Gate 10)

Условие старта: приёмка Gate 10 после прогона 25.09 10:00 МСК — PASS
(WB `B2` → 24.09, Ozon `B30` → 24.09 независимо, финансовая и структурная регрессия = 0).

Порядок обязателен: **новый форматтер в runtime раньше записи формата.** Прежний писатель
(`31b2d480…`) в следующий прогон вернёт строки дня к 25 px во всех секциях и перекрасит рамки
окна (август–сентябрь) в чёрный.

1. Слияние PR в `main`.
2. `deploy-shadow` → digest; `deploy-prod` с этим digest (общий образ всех prod-job).
3. Убедиться, что новый форматтер в production runtime: образ `ozon-unitka-prod` = новый digest,
   `GIT_SHA` = merge-коммит; содержимое образа — `docker create` + `docker cp` файла
   `dist/loaders/unitka/ozon/presentation.js` (`day: 21`) и `wbcontract.js` (`borderColors`),
   без `docker run`.
4. План и снимок отката с живого листа, только чтение, тем же кодом, что на репетиции
   (`docs/ops/visual_parity_2026-09-24/harness.mjs`, `dist` из merge-коммита):
   `plan` → сверить с отрепетированным (10 284 запроса, побайтно равны с точностью до sheetId) →
   `snapshot` → `rollback_plan.json` (259 запросов) сохранить вне scratchpad.
5. Отпечатки до записи (`fp.py prodN prod`): значения, формулы, имена, объединения, УФ, хвост
   владельца, ширины, скрытие, группы.
6. Запись `apply` в production. Harness намеренно пишет только в TEST: для production нужен
   отдельный явный допуск владельца (снять проверку `TEST` в режиме `apply` одним коммитом или
   запустить с его разрешения). Ручное форматирование в Google Sheets не используется.
7. Независимое перечитывание: `readback.py` (рамки и высоты = плану), `fp.py` (дельты), затем
   плановый прогон писателя и повторное сравнение формата (дрейф = 0).

Приёмка после записи:

```
VALUES_MUTATIONS=0  FORMULA_MUTATIONS=0  NAMED_RANGE_MUTATIONS=0  MERGE_MUTATIONS=0
CF_LOGIC_MUTATIONS=0  OWNER_TAIL_MUTATIONS=0  COLUMN_WIDTH_MUTATIONS=0  TYPOGRAPHY_MUTATIONS=0
ALIGNMENT_MUTATIONS=0  FILL_MUTATIONS=0
NORMAL_DAY_ROW_HEIGHT=21  BORDER_COLOR_MIGRATION=PASS  VISUAL_DRIFT_AFTER_WRITER=0
```

## 8. Откат — только оформление

Данные и runtime Gate 10 не затрагиваются.

- **Снимок до миграции.** `harness.mjs snapshot` читает с живого листа текущие высоты всех строк,
  которых касается план (586), и текущие рамки прямоугольника плана с запасом в одну строку и
  колонку (строки 465–638, колонки 1–561; хвост владельца с колонки 562 не входит никогда) и
  пишет `rollback_plan.json`: 259 запросов `updateDimensionProperties` + `updateCells
  fields=userEnteredFormat.borders`. Значения, формулы, УФ, объединения он не читает и не пишет.
- **Доказано на второй точной копии** `OZON_VISUAL_ROLLBACK_TEST` (sheetId 597023705):
  снимок → миграция (высоты `f77c9bb2…` → `efbda319…`) → откат → все 14 отпечатков равны
  исходным, формат строк 450–650 — 0 отличающихся ячеек.
- **Чтобы откат не перезаписал писатель,** нужен форматтер со старыми токенами: откатить
  runtime на прежний digest `sha256:31b2d480394e88d988598ca83632c79ff1973ccacdef1f0b5d347dc9ec3ccaa6`
  (он и есть Gate 10 без этого PR — пока в `main` между ними ничего не слито) или revert-коммит
  этого PR + деплой. Затем `apply rollback_plan.json`. Прежний писатель и сам вернёт 25 px всем
  секциям и чёрные рамки окну; снимок нужен для мая–июля и для немедленного отката.

## 9. Не делалось

- Легаси-секции Ozon май 2025 – апрель 2026 оформлены не каноническим форматтером: их рамки и
  заливки не трогаются, высоты строк им писатель задаёт и так.
- Блоки Ozon раскрыты по-разному (видимая ширина 1 352 – 2 108 px) — свёрнутые группы,
  состояние просмотра, не менялось.
- Gate 11 не начат.
