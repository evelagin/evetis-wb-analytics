# Миграция Apps Script → Cloud Run — дизайн-документ (rev2, после аудита)

**Проект:** EVETIS WB Analytics
**Дата:** 2026-07-24 (rev2)
**Статус:** ЧЕРНОВИК. Аудит ChatGPT rev1: APPROVE WITH CHANGES. rev2 закрывает
3 блокера + замечания 4–10 + уточняет ответы §10. Кода/деплоя нет; `wb-stocks` не
пишем до визы на rev2.
**Предыдущая версия:** `docs/MIGRATION_CLOUDRUN_DESIGN_2026-07-24.md` (rev1).

Решения владельца: TypeScript/Node, GitHub Actions, strangler, пилот = остатки.

---

## 0. Что изменилось против rev1 (diff для аудитора)

| # | Замечание | Как закрыто в rev2 |
|---|---|---|
| Блокер 1 | Services → **Jobs** | Загрузчики = **Cloud Run Jobs** (запуск по расписанию, конечная задача, exit 0/1). Scheduler дёргает Run API, а не наш HTTP. Services — только для будущих API/webhook/агентов. |
| Блокер 2 | WIF базовый | **Workload Identity Federation с первого деплоя**, без JSON-ключа. GitHub OIDC → WI Pool/Provider → impersonation sa-deployer. |
| Блокер 3 | shadow/prod изоляция | Явные окружения `*-shadow`/`*-prod` + **разные runtime SA** (`sa-loaders-shadow`/`sa-loaders-prod`) с правами на РАЗНЫЕ таблицы. Неверный env физически не может писать в прод. |
| Замеч. 4 | «коллизии невозможны» — сильно | Смягчено: общий ScriptLock уходит, но нужен **persistent execution guard** (manifest `loader×period`, MERGE STARTED/COMPLETE/ERROR; повтор → OK_NO_NEW/ALREADY_RUNNING/recovery). |
| Замеч. 5 | parity допуск 0 неверен | Двухчастная приёмка: **structural parity строго** + **quantitative parity только для снимков ≤5 мин** + детерминированный **fixture/replay-тест** (payload → old normalizer vs TS normalizer 1:1). |
| Замеч. 6 | cutover слабый | Формальный 8-шаговый cutover без окна одновременной записи (см. §9). |
| Замеч. 7 | регион | **`europe-west1`** (входит в фактическую географию EU multi-region; `europe-west3` — нет). BQ job `location='EU'` явно; регион Run ≠ location BQ. |
| Замеч. 8 | не всё в Secret Manager | Разделены: SM (только токены), env/config (dataset/location/flags/timeout), **runtime state в BQ** (watermark/manifest/attempt). |
| Замеч. 9 | один workflow на всё | Разделены **ci.yml / deploy-shadow.yml / deploy-prod.yml / infra.yml**; prod — manual approval, immutable digest, без авто-IAM. |
| Замеч. 10 | IAM широкий | Права до таблиц, shadow/prod раздельно; **sa-deployer НЕ имеет Secret Accessor** к значениям токенов (только привязывает секрет к Job; читает runtime SA). |

---

## 1. Текущее состояние (факты)

~60 файлов `.gs`; активный BQ-конвейер — 7 функций-триггеров + утилиты
(`WbBigQuery.gs`, `utils.gs`, `Config`, `Settings`). Токены (`WB_TOKEN_ANALYTICS`,
рекламы, финансов) и флаги — в Script Properties. Деплой = ручная вставка в браузер.
10 триггеров под одним project-wide `ScriptLock` → тихие `SKIPPED_LOCKED` (находка
24.07). Пилот `WbStocksSnapshot.gs`: WB Analytics T6/T5 → `RAW_WB_STOCKS` + манифест
`WB_STOCKS_SNAPSHOTS`; зависимости от Sheets (индекс SKU, лог).

---

## 2. Целевая архитектура (rev2)

```
GitHub (repo, TS, монорепо /cloud)
  PR  → ci.yml: lint/typecheck/unit/fixture-parity/build (БЕЗ деплоя)
  merge → deploy-shadow.yml (WIF, impersonation) → Cloud Run Job shadow
  approval (GitHub Environment) → deploy-prod.yml → Cloud Run Job prod (immutable digest)
Cloud Scheduler (cron)
  └─ Run API: execute Cloud Run Job  (НЕ наш HTTP endpoint)
       Cloud Run Job (контейнер):
         node dist/cli.js <loader>
         reads токен из Secret Manager (runtime SA)
         execution-guard MERGE (loader×period) в BQ
         WB API → нормализация(SKU из REF_SKU_MASTER) → BigQuery (RAW + manifest)
         exit 0 (OK) / exit 1 (ERROR)
Наблюдаемость: Cloud Logging + run-manifest в BQ + сторож свежести
```

- **Вычислительная модель — Cloud Run Jobs** (не Services): по одному Job на
  логический загрузчик, **общий TS-кодбейс и один общий Docker-образ**; Job задаёт
  команду `node dist/cli.js stocks`. Плюсы: изоляция расписания/retries/timeout/
  прав, нативный статус execution, exit code = честный результат, нет HTTP-endpoint
  и Express, контейнер сам завершается. Services оставляем на будущее (API
  дашборда, webhook, интерфейс агента, Telegram-callback).
- **Cloud Scheduler** → execute Job через Run API (управляющий Google API).
- **Secret Manager** — только секреты (токены WB и т.п.).
- **Artifact Registry** — образы (immutable digest).
- **GitHub Actions + WIF** — деплой без ключа.
- **Runtime SA (least privilege, раздельно):** `sa-loaders-shadow` (пишет ТОЛЬКО
  `*__CR`-таблицы) и `sa-loaders-prod` (пишет ТОЛЬКО прод), + `Job User`,
  `Secret Accessor` на нужные секреты, `BigQuery Job User` (location EU).
- **Deployer SA (`sa-deployer`)** через WIF-impersonation: `Run Admin`,
  `Artifact Registry Writer`, `Service Account User` (на runtime SA); **без
  Secret Accessor к значениям**; `Scheduler Admin` — только в infra-workflow, не в
  app-деплое.

---

## 3. Execution guard (замечание 4) — обязателен

Отдельные Jobs убирают общий ScriptLock, но НЕ гарантируют идемпотентность.
Остаются: два execution одного loader, дубль-доставка Scheduler, ручной+авто,
retry поверх нового cron, параллельные MERGE/DDL. Поэтому — общий framework
(обобщаем лучшее из Finance-манифеста):

Ключ `loader_name × logical_period` (напр. `wb-stocks × 2026-07-24`). Перед работой
Job делает guarded MERGE в BQ-таблицу состояния: STARTED → COMPLETE/ERROR.
Повторный запуск: COMPLETE → `OK_NO_NEW`; свежий STARTED → `ALREADY_RUNNING`;
устаревший STARTED → recovery по правилам loader.

**Правильная формулировка:** project-wide ScriptLock исчезает; независимые
загрузчики не блокируют друг друга; защита от повторного/параллельного запуска
КАЖДОГО loader — через persistent run-manifest и идемпотентные ключи.

---

## 4. Структура репозитория

```
/cloud/
  package.json  tsconfig.json  .eslintrc  (test framework)
  /src/lib/     bqClient(location=EU), secretClient, wbHttp(429), skuIndex(REF_SKU_MASTER),
                runManifest(execution-guard), config(env), logging(structured), errors(exit-code)
  /src/cli.ts   node dist/cli.js <loader>
  /src/loaders/ stocks.ts (пилот), …
  Dockerfile    (один общий образ)
/infra/terraform/   APIs, Artifact Registry, SA, WI Pool/Provider, IAM, secrets(без значений),
                    Cloud Run Jobs, Scheduler, monitoring
/.github/workflows/ ci.yml  deploy-shadow.yml  deploy-prod.yml  infra.yml
```
`apps-script/` не трогаем до cutover каждого потока.

---

## 5. Секреты / конфиг / состояние (замечание 8)

- **Secret Manager (секреты):** `WB_TOKEN_ANALYTICS`, `WB_TOKEN_ADS`,
  `WB_TOKEN_FINANCE`, (позже `TELEGRAM_BOT_TOKEN`, `AI_API_KEY`).
- **Env/config (несекретное):** `GCP_PROJECT_ID`, `BQ_LOCATION=EU`, `BQ_RAW_DATASET`,
  `SINK_MODE`, `LOOKBACK_DAYS`, `WB_HTTP_TIMEOUT_MS`, `LOADER_NAME`, `ENVIRONMENT`,
  `LOG_LEVEL`.
- **Runtime state (BQ state/run-таблицы, НЕ SM/env):** watermark, last successful
  period, manifest status, attempt count.

---

## 6. CI/CD — раздельные workflow (замечание 9)

- **ci.yml** (на PR): install, lint, typecheck, unit, **fixture-parity**, build
  Docker, security/deps. **Без деплоя.**
- **deploy-shadow.yml** (после merge): деплой Job shadow (WIF).
- **deploy-prod.yml**: **manual approval** через GitHub Environment; деплой
  конкретного **immutable image digest**; без авто-изменения IAM; prod-Scheduler
  включается отдельной операцией.
- **infra.yml**: Terraform `plan` на PR, `apply` только с approval; отдельно от кода.

Аутентификация — **WIF** (замечание/блокер 2): `permissions: id-token: write`,
GitHub OIDC → WI Pool → Provider → impersonation `sa-deployer`. JSON-ключ — только
аварийный временный вариант, не основной контракт.

---

## 7. Пилот: остатки (`wb-stocks` Job)

**Почему остатки:** дневной, самодостаточный, малый blast-radius, снимок = полное
состояние (parity показательна). Порт-детали: индекс SKU из **`REF_SKU_MASTER`**
(уходит зависимость от Sheets); токен из Secret Manager; логика T6/валидация/
нормализация/T5-контроль/манифест — 1:1; запись через `@google-cloud/bigquery`
(load job, `location='EU'`). Runtime SA — `sa-loaders-shadow`.

---

## 8. Приёмка пилота (замечание 5) — две части

**A. Replay/fixture parity (строго 1:1).** Один сохранённый API-payload →
Apps Script normalizer И TS normalizer → полное равенство строк (схема, ID-
нормализация, типы, дедуп, null-policy, warehouse mapping, SKU-match, переходы
манифеста, post-load валидация). Доказывает корректность порта независимо от
живых остатков.

**B. Live shadow parity.** Новый Job пишет в **теневые** `RAW_WB_STOCKS__CR` /
`WB_STOCKS_SNAPSHOTS__CR` (SA shadow, прод недоступен). ≥5 последовательных
executions, из них ≥1 день с реальным изменением остатков; все COMPLETE, дублей 0;
**количественную** parity считаем ТОЛЬКО для снимков с близким временем
(`ABS(old.snapshot_at − new.snapshot_at) ≤ 5 мин`), отчёт: matched / old-only /
new-only / changed_qty / abs_delta / rel_delta; динамические расхождения объяснены.

Без replay-теста live-окно поднять до 7 дней. Приёмка = A ✓ и B ✓.

---

## 9. Формальный cutover (замечание 6)

1. Shadow parity подписана (A+B).
2. Production Job создан, Scheduler ВЫКЛЮЧЕН.
3. Один **ручной** production execution.
4. Проверка: manifest COMPLETE, RAW, row count, distinct key, snapshot-view,
   текущие потребители.
5. Включаем production Scheduler.
6. Отключаем Apps Script trigger `runWbStocksSnapshot`.
7. На следующий день проверяем авто-execution.
8. Старый код храним 7–14 дней (откат).

На момент переключения — **никакой одновременной записи** Apps Script и Cloud Run
в один прод-снимок.

---

## 10. Регион (замечание 7)

Cloud Run — **`europe-west1`** (входит в фактическую географию BigQuery EU
multi-region; `europe-west3` — нет). Во всех BQ-задачах явно `location='EU'`
(location job обязан совпадать с location датасета). Регион Run и location BQ —
разные настройки.

---

## 11. IAM (замечание 10)

- Runtime loader: доступ на конкретные датасеты/таблицы, не blanket Data Editor;
  не создаёт/не удаляет произвольные объекты; shadow и prod разделены.
- `sa-deployer`: impersonate runtime SA, но **без чтения значений WB-секретов**
  (Secret Accessor — только у runtime SA); `Scheduler Admin` — в infra-workflow,
  не в app-деплое.
- На первых PR допустимо временно шире, но целевой least-privilege — как выше.

---

## 12. Ответы на §10 (уточнены аудитом)

1. **IaC — Terraform** (не gcloud-скрипты): APIs, Artifact Registry, SA, WI
   Pool/Provider, IAM, secrets без значений, Cloud Run Jobs, Scheduler, monitoring,
   datasets/tables при нужде. Значения токенов Terraform НЕ хранит.
2. **CI-аутентификация — WIF сразу**, не этапами.
3. **Структура — ни service, ни «общий сервис»:** один общий TS-кодбейс + один
   общий Docker-образ + **отдельный Cloud Run Job на loader** (`wb-orders`,
   `wb-sales`, `wb-sales-reconcile`, `wb-ads`, `wb-stocks`, `wb-finance`,
   `wb-ref-sync`), команда `node dist/cli.js <loader>`.
4. **Регион — `europe-west1`; BQ location = EU.**
5. **5 дней parity — достаточно ПРИ наличии replay-теста**; иначе 7. Полная
   приёмка — §8.

---

## 13. План PR (скорректирован)

- **PR-Mig0 — Foundation** (без бизнес-логики): `/cloud` (package/tsconfig/eslint/
  test, `/src/lib`, `/src/cli`, Dockerfile), `/infra/terraform`, `.github/workflows`.
  Заложить: модель **Jobs**, **WIF**, **Terraform**, CI без прод-деплоя,
  окружения shadow/prod, контракт run-manifest, structured logging, exit-code.
- **PR-Mig1 — Stocks shadow:** порт stocks, fixture capture, replay-тест, теневые
  таблицы, `sa-loaders-shadow`, Job, Scheduler-shadow, parity-отчёт, ≥5 дней.
- **PR-Mig1b — Stocks production cutover** (отдельный релиз): prod Job, prod-права,
  ручной smoke-test, Scheduler, отключение Apps Script, инструкции отката.
- **Далее по одному, порядок:** stocks → **REF → ads → orders → sales →
  nightly reconciliation → finance** (финансы последними — самый сложный:
  очередь, недельная финализация, pacing, reconciliation, immutable manifest,
  legacy cutover).

## 14. Разовая настройка владельцем (дам точные Terraform + шаги)

Включить API (Run, Scheduler, Secret Manager, Artifact Registry, Cloud Build);
Terraform создаст SA, WI Pool/Provider, IAM, Artifact Registry, пустые секреты,
Jobs, Scheduler. Владелец руками: положить ЗНАЧЕНИЯ токенов WB в Secret Manager;
настроить GitHub Environment с approval для prod. Ключей в GitHub нет (WIF).

_Код `wb-stocks` — только после визы владельца на rev2. Далее PR-Mig0._
