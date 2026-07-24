# Миграция Apps Script → Cloud Run — дизайн-документ (rev1)

**Проект:** EVETIS WB Analytics
**Дата:** 2026-07-24
**Статус:** ЧЕРНОВИК НА АУДИТ (ChatGPT). Кода/деплоя нет. Ни один ресурс GCP не
создаётся до визы владельца и прохождения аудита.

Решения владельца 24.07: **деплой = GitHub Actions CI/CD**; **старт = один пилот-
загрузчик** (strangler); **язык = TypeScript/Node**.

Правило проекта: перед кодом — что / зачем / риск / контрольные цифры.

---

## 0. TL;DR

Уносим 7 загрузчиков из Apps Script в контейнеризированные сервисы **Cloud Run**,
запуск по **Cloud Scheduler**, секреты в **Secret Manager**, деплой из git через
**GitHub Actions**. Логика (TS) живёт в репозитории → правки я/аудитор вносим в git,
деплой автоматический, ручная вставка в браузер исчезает. Начинаем с **одного
пилота — остатки (`runWbStocksSnapshot`)**, обкатываем шаблон на нём (strangler:
новое пишет в теневую таблицу, сверяем parity со старым N дней, потом cutover),
затем тиражируем на остальные 6.

**Бонус:** миграция попутно устраняет находку 24.07 — общий `ScriptLock` (из-за
которого REF столкнулся с финансами) исчезает: каждый загрузчик — отдельный сервис.

Что переносим (7): заказы, продажи, ночная пересверка продаж, реклама, остатки,
финансы daily/weekly, REF Sync. Что НЕ переносим: вся легаси Sheets-аналитика
(`Cleanwbdaily`, `MonthlyUnitReport`, дашборды, `onEditCostTracker` и пр.) —
остаётся в Apps Script как есть.

---

## 1. Текущее состояние (факты из репо)

- ~60 файлов `.gs`; активный BQ-конвейер — 7 функций-триггеров + общие утилиты
  (`WbBigQuery.gs`, `utils.gs`, `Config`, `Settings`).
- Секреты (токены WB: `WB_TOKEN_ANALYTICS`, статистики, рекламы, финансов) — в
  **Script Properties**. Флаги приёмников (напр. `WB_STOCKS_BQ_SINK`) — там же.
- Деплой = ручная вставка `.gs` в редактор Apps Script в браузере.
- Расписание = 10 time-триггеров, ВСЕ под одним project-wide `ScriptLock` →
  перекрытия дают тихие `SKIPPED_LOCKED` (находка 24.07: REF↔финансы).
- Пилот `WbStocksSnapshot.gs`: WB Analytics API (T6 `stocks-report/wb-warehouses`
  + T5 `warehouse_remains` контроль) → `RAW_WB_STOCKS` append + манифест
  `WB_STOCKS_SNAPSHOTS` (STARTED→COMPLETE/ERROR). Зависимости от Sheets: индекс SKU
  `buildSkuIndex_` (читает книгу) и best-effort лог `IMPORT_LOG_STOCKS`.

---

## 2. Целевая архитектура

```
GitHub (repo, TS)
  └─ push в main → GitHub Actions:
       build Docker → push в Artifact Registry → deploy Cloud Run (europe-west*)
Cloud Scheduler (cron, замена time-триггеров)
  └─ HTTP POST → Cloud Run сервис <loader> (OIDC-аутентификация)
       Cloud Run сервис:
         reads токен из Secret Manager
         WB API → нормализация → BigQuery (RAW + манифест/run-log)
         логи → Cloud Logging
Наблюдаемость: Cloud Logging + существующие run-log таблицы + сторож свежести
```

Компоненты:
- **Cloud Run** (регион `europe-west*` — рядом с датасетом EU): по одному сервису
  на загрузчик; или один сервис с маршрутами `/run/<loader>`. Для пилота — один
  сервис `wb-stocks`.
- **Cloud Scheduler**: cron-задание на загрузчик → HTTP-вызов сервиса с OIDC-
  токеном сервис-аккаунта (Cloud Run invoker). Расписания = текущие тайминги.
- **Secret Manager**: токены WB и флаги; сервис читает при старте (не в образе).
- **Artifact Registry**: Docker-образы.
- **GitHub Actions**: на push в main — build+push+deploy. Аутентификация в GCP —
  **сервис-аккаунт-ключ в GitHub Secrets** (по решению владельца); в §9 отмечен
  более безопасный вариант Workload Identity Federation (без долгоживущего ключа)
  как опция усиления.
- **Сервис-аккаунты (least privilege):**
  - `sa-loaders@…` (runtime Cloud Run): роли `BigQuery Data Editor` на датасете
    `wb_raw`, `BigQuery Job User`, `Secret Manager Secret Accessor`.
  - `sa-deployer@…` (GitHub Actions): `Cloud Run Admin`, `Artifact Registry Writer`,
    `Service Account User` (для sa-loaders), `Cloud Scheduler Admin`. Ключ — в
    GitHub Secrets.

---

## 3. Структура репозитория (монорепо в существующем)

```
/cloud/                         ← новый корень облачного конвейера
  package.json  tsconfig.json
  /lib/                         ← общее: bqClient, secretClient, wbHttp(429-retry),
                                   skuIndex(из REF_SKU_MASTER), runLog, config
  /services/
    /wb-stocks/                 ← ПИЛОТ
      index.ts  (HTTP-обработчик /run)
      loader.ts (портированная логика WbStocksSnapshot)
      Dockerfile
  /infra/                       ← IaC: Cloud Run + Scheduler + IAM (gcloud-скрипты
                                   или Terraform — решить в §10)
/.github/workflows/deploy.yml   ← build+push+deploy по push в main
```
`apps-script/` остаётся нетронутым до cutover каждого загрузчика.

---

## 4. Пилот: остатки (`wb-stocks`)

**Почему остатки:** дневной (легко наблюдать, не почасовой), самодостаточный,
малый blast-radius (остатки не входят в критичный PNL-cutover), и главное — снимок
= полное состояние дня, значит **parity проверяется идеально** (nm×склад×qty новый
vs старый). Богатый существующий манифест `WB_STOCKS_SNAPSHOTS` упрощает сверку.

**Что меняется при порте (осознанно):**
- Индекс SKU — из **`REF_SKU_MASTER` (BQ)**, а не из Sheets → уходит последняя
  зависимость от книги.
- Токен `WB_TOKEN_ANALYTICS` — из Secret Manager.
- Sheet-лог `IMPORT_LOG_STOCKS` — не переносим (манифест BQ и так источник истины).
- Логика T6-fetch/валидация/нормализация/T5-контроль/манифест STARTED→COMPLETE —
  **портируется 1:1** (те же инварианты: пустой ответ=ERROR, дедуп ключа
  nmId|chrtId|warehouseId, пост-COUNT, допуск T5 ±2).
- Запись в BQ — через `@google-cloud/bigquery` (load job с детерминированным
  jobId — тот же паттерн идемпотентности).

**Strangler (без риска для прода):**
1. Новый сервис пишет в **теневую таблицу** `RAW_WB_STOCKS__CR` (и манифест
   `WB_STOCKS_SNAPSHOTS__CR`), НЕ в прод. Старый триггер Apps Script продолжает
   работать в прод как обычно.
2. N дней (предлагаю ≥5) сверяем parity: строки/ключи/суммы qty нового снимка ==
   старого за тот же день (§8).
3. При зелёном parity — **cutover:** отключаем триггер `runWbStocksSnapshot` в
   Apps Script, новый сервис начинает писать в прод `RAW_WB_STOCKS`. Откат =
   включить триггер обратно (старый код не удаляем до стабилизации).

---

## 5. Деплой (GitHub Actions)

`deploy.yml`: на push в `main` с изменениями в `/cloud/**` →
`google-github-actions/auth` (ключ sa-deployer из secrets) → build+push образа в
Artifact Registry → `gcloud run deploy wb-stocks` → применить Scheduler/IAM из
`/infra`. Правки логики = обычный git-flow (я кладу файл → владелец
review+commit+push в GitHub Desktop → Actions деплоит). Ручной вставки в браузер нет.

## 6. Секреты

`WB_TOKEN_ANALYTICS` и остальные токены/флаги: владелец разово создаёт секреты в
Secret Manager (значения — у него). Сервис читает при старте, права —
`Secret Accessor` у `sa-loaders`. В Script Properties для пилота больше не нужны.

## 7. Расписание и устранение ScriptLock

Cloud Scheduler cron на остатки ~06:30 МСК (`30 6 * * *` в TZ Europe/Moscow).
Так как каждый загрузчик — отдельный сервис, **общего замка нет** → находка 24.07
(REF↔финансы) на новом контуре невозможна by design. Для оставшихся загрузчиков
при миграции разносить расписания уже не обязательно.

## 8. Контрольные цифры (parity пилота)

За каждый общий день N:
1. `rows(RAW_WB_STOCKS__CR) == rows(RAW_WB_STOCKS)` для снимка дня.
2. `distinct(nm|chrt|wh)` совпадает; дублей 0 в обоих.
3. `Σ quantity` и `Σ physical` совпадают (допуск 0 — источник тот же T6).
4. Манифест `__CR` = COMPLETE, control_status OK, delta ≤ 2 (как у прод).
5. SKU-match (matched/not_found) совпадает с прод (после перевода индекса на BQ).
Зелёные ≥5 дней → cutover.

## 9. Риски

1. **Секреты в CI-ключе** — минимизировать роли sa-deployer; вариант усиления —
   Workload Identity Federation (без ключа). Рекомендую WIF, но принимаю выбор
   ключа как быстрый старт.
2. **Parity-разрыв** — держим старый триггер до подписи parity; откат тривиален.
3. **Состояние/watermark** — пилот (снимок) без состояния, безопасен. Для
   заказов/продаж watermark сейчас в Script Properties → при их миграции нужен
   стор состояния (BQ-таблица состояния или Firestore). Отметка на будущие PR.
4. **Стоимость** — Cloud Run + Scheduler при 1 запуске/день ≈ копейки.
5. **T5 long-poll до ~180с** — уложиться в таймаут Cloud Run (до 3600с; ок).

## 10. Открытые вопросы к владельцу/аудитору

1. IaC: `gcloud`-скрипты в `/infra` (проще) или **Terraform** (воспроизводимее)?
2. CI-аутентификация: SA-ключ (выбран) или сразу **Workload Identity Federation**
   (безопаснее, без ключа)?
3. Cloud Run на пилот: отдельный сервис `wb-stocks` (рекомендую) или общий сервис
   с маршрутами на все загрузчики?
4. Регион Cloud Run: `europe-west1`/`europe-west3` — подтвердить.
5. Порог parity: 5 дней достаточно или хочешь дольше/строже?

## 11. Что делает владелец разово (я подготовлю точные команды)

Включить API (Run, Scheduler, Secret Manager, Artifact Registry, Cloud Build);
создать 2 сервис-аккаунта + роли; создать секреты WB в Secret Manager; создать
ключ sa-deployer и положить в GitHub Secrets. Всё остальное (код, Dockerfile,
workflow, infra-скрипты) — я в git.

## 12. План PR

- **PR-Mig0:** скелет `/cloud` (lib, tsconfig, Dockerfile-шаблон, `deploy.yml`,
  `/infra`) + чек-лист разовой настройки GCP для владельца. Без бизнес-логики.
- **PR-Mig1:** пилот `wb-stocks` (порт логики, индекс SKU из BQ, теневые таблицы)
  + parity-харнесс. Деплой в тень, сверка ≥5 дней.
- **Cutover пилота:** отключить триггер остатков в Apps Script, переключить сервис
  на прод-таблицу. Подпись parity.
- **PR-Mig2…N:** тиражирование на остальные 6 (реклама → финансы → заказы/продажи
  с миграцией watermark → REF → ночная пересверка), по одному, тем же паттерном.

_Код и деплой — после визы владельца на rev1 и аудита ChatGPT._
