# Lossless reconciliation — Git как полный source of truth (16.09.2026)

Состояние ПЕРЕД первым `clasp push`. Production Apps Script не изменялся.

## Итог симуляции

| проект | prod файлов | clasp tracked | added | modified | deleted |
|---|---|---|---|---|---|
| ingestion | 88 | 88 | 0 | 11 | **0** |
| unitka | 9 | 10 | 1 | 2 | **0** |
| operations | 2 | 2 | 0 | 0 | **0** |

Проверено двумя независимыми способами: `tools/clasp_prepush_verify.js` (симуляция против
снимка production) и `clasp show-file-status` (список файлов глазами самого clasp).

## Что пришлось починить, чтобы получить deleted = 0

**1. Имена файлов.** clasp отправляет файл под именем «локальное имя минус одно расширение».
В production 51 файл назывался иначе, чем в репозитории:
- 34 файла в репозитории вообще не имели расширения (`AuditAndNotes`, `Menu v2`, `Wbfinance`,
  `loadCostHistory`, …). clasp такие файлы не отправляет, а их production-двойников удаляет;
- 17 файлов отличались регистром (`WbAdsDaily.gs` против production `Wbadsdaily`). Для clasp
  это разные файлы: push создал бы новый и удалил старый;
- `WbSalesReturnsLoader.gs` — в production имя файла включает `.gs`, поэтому локально он
  называется `WbSalesReturnsLoader.gs.gs`.

**2. Регистр на диске.** APFS на этом Mac регистронезависима: `git mv A a` меняет индекс, но
имя на диске остаётся старым. clasp читает диск, а не индекс. Регистр 18 файлов приведён в
соответствие двухшаговым переименованием; верификатор теперь сверяет диск с индексом.

**3. Импорт production-only.** 13 файлов ingestion и `Код.gs` в unitka скопированы побайтово.

**4. Выравнивание по байтам.** 35 файлов различались только отступами и пустыми строками —
взяты байты production, чтобы push не трогал их без нужды. Сравнение построчное с обрезкой
краевых пробелов; содержимое строк не менялось.

## Объяснение каждого modified

| проект | файл | почему отличается |
|---|---|---|
| ingestion | `IngestRunLog.gs`, `Wbadsdaily.gs`, `WbAdsRawLoader.gs`, `WbAdsQueryBids.gs`, `Wbadsquerystats.gs` | Step 5A: reaper зависших STARTED, гейт стены прогона, provenance триггеров. Готово к выкату, не выкачено |
| ingestion | `UnitkaR3Ingest.gs`, `UnitkaR5.gs`, `UnitkaR6.gs` | репозиторий впереди production |
| ingestion | `WbBigQuery.gs`, `WbStocksBigQuery.gs`, `Wbsuppliesinventoryprobe4.gs` | репозиторий впереди production |
| unitka | `UnitkaE6.gs`, `UnitkaE6Run.gs` | E6-A write-фаза: репозиторий впереди production |
| unitka | `UnitkaE6State.gs` (**added**) | read-only диагностика E6; принадлежность к UNITKA доказана зависимостью от `canon_`/`loadJson_`/`saveJson_` |

Ни одно изменение не является побочным эффектом реорганизации — это ровно тот набор, который
осознанно предназначен к выкату.

## OPERATIONS: монолит остаётся монолитом

В production проект — один файл `Код.gs`. Источник правды в Git — модули `Ops*.gs`;
`tools/build_ops_monolith.js` собирает из них `Код.gs` и воспроизводит байты production
**точно**: sha256 `0280a3c3f5ee…`, 120 094 байта. Модули исключены из push через
`.claspignore`, поэтому первый push — полный no-op (0/0/0).

Заголовок сборки намеренно сохраняет старый путь `apps-script/evetis_operations` — именно так
он выглядит в production. Менять его — отдельный controlled deployment, иначе push перестанет
быть no-op. Миграция монолит → модули требует отдельного OWNER APPROVAL (решение 4).

`node tools/build_ops_monolith.js --check` падает, если `Код.gs` разошёлся с модулями.

## Карантин

`apps-script/_unassigned/` — вне всех трёх `rootDir`, clasp их не видит:
`CtOwnerActions.gs` (never deployed, DEPRECATE по OPS_ARCHITECTURE_REVIEW),
`UnitkaFill.gs`, `UnitkaR2.gs`, `UnitkaR4.gs` (принадлежность не доказана).

## Временные production-артефакты в Git

Импортированы только ради lossless первого push, помечены в `tools/clasp_projects.json`:
`UnitkaR7.gs` (пустая заглушка `function myFunction(){}`, канонический файл принадлежит UNITKA),
`FindFinanceReturns_TEMP.gs`, `WbApiGateSalesReturns_TEMP.gs`. Все — CANDIDATE_REMOVE,
из production по OWNER DECISION 2 пока не удаляются.

## Legacy

`Evetis Ozon`, `Evetis WB` и проба WB Advert API не входят ни в один `.clasp.json`, их файлов
в репозитории нет. Проверено поиском по scriptId и по именам файлов.

🔒 OWNER ACTION (вне этого этапа): ротация bearer-токена WB Advert API, лежащего в открытом
виде в старом проекте-пробе. В Git он не переносился.
