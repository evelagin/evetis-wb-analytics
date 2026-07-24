# SESSION HANDOFF — 2026-07-23

Снимок состояния EVETIS WB Analytics на конец сессии 22–23.07.2026.
Главное: **контур Finance ingestion (PR-Fin1) введён в production** — двухслойная
модель DAILY=PROVISIONAL / WEEKLY=FINAL, приёмка §8 пройдена, финальная виза аудитора
«APPROVED FOR PRODUCTION» получена. Осталось выполнить `wbFinInstallDailyTriggers()`.

---

## 1. Roadmap (решение владельца 22.07, пересмотр приоритетов)

**Финансы → MART → Dashboard v1 → Incomes → Supply Planning.**
Incomes (поставки): discovery завершён (эндпоинт `/api/v1/supplier/incomes`,
dateFrom по lastChangeDate, 1 req/мин, ключ-кандидат incomeId+barcode НЕ доказан),
код отложен до «после дашборда». Дашборд: рассмотреть Looker Studio поверх BQ.

## 2. Финансы: что построено (дизайн docs/FINANCE_DAILY_DESIGN_2026-07-22.md rev3-final)

**Открытие:** ежедневные отчёты реализации WB = параметр `period:'daily'` в
`POST /api/finance/v1/sales-reports/list` (default weekly — потому их «не было»).
reportId дневных — структурный 16-значный int64 (BigInt-риск: только строковый парсинг).
Probe A/B/C (WbFinanceDailyProbe.gs, логи 22.07): daily-слой ТОЧЕН (Δ=0 до копейки),
weekly содержит ТЕ ЖЕ rrdId под другим report_id → без замещения по (неделя×тип)
был бы двойной счёт ×2.

**Код:** `apps-script/WbFinanceDaily.gs` v1.3.1 (4 раунда code audit) +
`WbFinanceApiV1.gs` (глобальный персистентный пейсер 61с через ScriptProperty
`WB_FIN_API_LAST_REQ_MS` + lock; rrdId-курсор строкой). Ключевые механизмы:
manifest `FINANCE_REPORT_LOADS` grain=report_id (только MERGE, DISCOVERED→STARTED→
COMPLETE|ERROR, attempt только при STARTED, COMPLETE immutable, processing_run_id
скрывает частичные попытки), post-load SQL перед COMPLETE, PK-assert в каждом тике,
stale-recovery 120 мин, retry всегда с rrdId=0, C0-marker `WB_FIN_DAILY_C0_OK`
(без него не работают ни тик, ни триггеры), cutover `2026-07-13`, reportType строго
{1,2} (type 3 = fail-closed стоп до отдельного probe).

**BQ-объекты:** RAW_WB_FINANCE (+report_period/report_type INT64/run_id; raw_json был),
FINANCE_LOADER_RUNS, FINANCE_REPORT_LOADS, FINANCE_WEEK_STATUS, FINANCE_WEEK_RECON,
V_WB_FINANCE_COMPLETE (legacy-ветвь run_id IS NULL + manifest-confirmed),
V_WB_FINANCE_CANONICAL (cutover + замещение; поля source_layer/finance_status —
читать MART только отсюда), V_WB_FINANCE (пересоздан: старые 71 колонка, weekly-only
поверх COMPLETE — потребители без изменений).

## 3. Приёмка 23.07 (все инварианты сошлись)

C0 14:08: 201211/даты/Σ18563263.39/Σ18105658.87 идентичны на ДО/__TEST×3/ПОСЛЕ;
схема 71=71; preflight 0. Прогоны: PARTIAL(хвост 6) → PARTIAL(2) → OK(0) →
OK_NO_NEW — очередь пережила бюджет без потерь/дублей. Manifest 8=8 COMPLETE.
RAW +846 (246 daily 20–22.07 + 600 weekly 13–19.07). Canonical: неделя 13–19.07 =
WEEKLY/FINAL Σfor_pay 75536.55 (t1 72917.86 + t2 2618.69, НЕ ×2); дни 20–22.07 =
DAILY/PROVISIONAL 246 строк. V_WB_FINANCE 201811 (legacy 201211 + 600 weekly).
**Финансы догнаны: были по 12.07 → теперь по 22.07.**
RECON недели 13–19.07 = WARN (sum_daily=0) — задокументированный одноразовый артефакт
cutover-недели, НЕ дефект (виза аудитора это подтверждает).

## 4. СЛЕДУЮЩИЕ ШАГИ (владелец)

1. `wbFinInstallDailyTriggers()` → лог в чат (ожидаются 3 триггера 07:30/12:30/18:30 МСК).
2. 24.07 утром: первый автотик — FINANCE_LOADER_RUNS (AUTO), отчёты за 23.07, без дублей.
3. Пн 27.07: автофинализация недели 20–26.07 + первый настоящий RECON (ожидание Δ=0
   по всем метрикам либо документированное реальное расхождение WB).
4. Дальше по roadmap: **MART** (FACT_* + MART_SKU_DAILY поверх V_WB_FINANCE_CANONICAL
   и остальных прод-потоков) → Dashboard v1. Формула PNL — TBD до reconciliation
   for_pay (архитектура §8, вариант A/B).

## 5. Прочее состояние (без изменений за сессию)

Заказы/продажи/остатки — production (hourly/daily). Реклама: WbAdsDaily влит (#64),
триггер `wbAdsInstallDailyTrigger` ещё НЕ установлен (напоминание!). REF Sync PR1
в проде (#65), дальше PR2 cost/PR3 bundles/PR4 warehouses. Sheets на лимите ячеек.
Health Monitor отложен до «после дашборда» (внешний сторож свежести всё ещё нужен —
OAuth-инцидент 17–21.07 помним).

## 6. Процессные уроки сессии

- device_stage_files может отдать stale-снапшот при свежем mtime → перед diff'ами
  сверять md5 через device_bash (пути через mnt/<folder>, не /Users/...).
- Legacy wbFinanceBackfillAutoTick — НЕ production-entrypoint (несовместим с
  нереентерабельным pacer-lock), помечено в коде.
- Дефолты API коварны: недокументированный default `period=weekly` три недели прятал
  от нас ежедневные отчёты. Проверять параметры по OpenAPI-спеке (github
  eslazarev/wildberries-sdk — автогенерация из спеки WB).
