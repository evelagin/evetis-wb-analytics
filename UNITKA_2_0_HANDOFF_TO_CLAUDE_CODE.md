# UNITKA 2.0 — HANDOFF TO CLAUDE CODE

Собрано: 11.09.2026 · agent claude-opus-5 · сессия Cowork
Предыдущие стадии: R1 → R6. Подробности — `docs/UNITKA_2_0_R*.md`.

---

## 1. ТЕКУЩАЯ ЦЕЛЬ

Закрыть **последний** блокер сентябрьского шаблона: загрузить воронку продаж WB
в BigQuery и заполнить в юнитке колонки `P` (Переходы) и `R` (Положили в корзину).

Всё остальное по сентябрю закрыто и проверено. Архитектура не обсуждается:

```
BIGQUERY      = SOURCE OF TRUTH
GOOGLE SHEETS = OPERATING INTERFACE
APPS SCRIPT   = DETERMINISTIC WRITE LAYER
AI            = ANALYSIS / CONTROL / QA
```

---

## 2. ЧТО УЖЕ СДЕЛАНО

### Книга «Юнитка_Evetis Cosmetics» (`1E4L4JuwfEqr9owhsGkAjb8F24lRpWpWkEyVmSuRxaJg`)

Лист `WB_Юнит_2025`. Геометрия сентября:

```
735 шапка · 736 заголовки · 737–766 данные (30 дней) · 767 итог · 768 план
Блоки SKU: первый M(13), шаг 24, 24 блока. Крем для рук = блок M, nmID 252442517.
Колонки блока: M Дата · N Блогеры · O Показы · P Переходы(GAP) · Q Заказы(gross) ·
R Корзины(GAP) · S Отмены · T Остатки · U Оборачиваемость · V Доходность 1 шт ·
W Доходность общая · X Реклама внутр. · Y Внешняя реклама · Z ДРР · AA Цена ·
AB СПП % · AC Цена с СПП · AD Комиссия · AE Цена минус комиссия · AF Логистика ·
AG Хранение · AH Налог (резерв) · AI Доходность 1 шт · AJ День недели
$R$45 = 240 ₽ себестоимость. LAST_CLOSED_DATE зеркалится в WB736 (колонка 600),
потому что условное форматирование не умеет ссылаться на другой лист.
```

| стадия | итог |
|---|---|
| R1–R2 | календарь на серийных числах (timezone-trap), семантика заказов gross/cancels, остатки из BQ, цена из order-level, комиссия, логистика, платное хранение |
| R3 | Europe/Moscow, `LAST_CLOSED_DATE` из свежести источников (не `TODAY()-1`), хранение в BigQuery с идемпотентностью |
| R4 | будущие дни без фейковой экономики (313 формул в обёртке `IF(дата>LAST_CLOSED_DATE;"";…)`), аудит 23 MTD-колонок, декомпозиция прибыли |
| R5 | доказательство комиссии/эквайринга на 705 наблюдениях, аудит СПП, правовая база резерва |
| R6 | комиссия разделена на BASE + ACQUIRING, база резерва исправлена, финальный QA |

### Ключевые доказанные факты (не переоткрывать)

```
for_pay = retail_price_withdisc_rub − ROUND(srev × commission_percent/100; 2) − acquiring_fee
```
Проверено на 705 строках (01.08–09.09): MAE 0,0028 ₽, MAX 0,01 ₽, вне допуска 0.

* Эквайринг = **4,00 % от суммы, которую заплатил покупатель** (`retailAmount`),
  а не 1,66–4,88 % от цены продавца. Поле WB `acquiringPercent` — производное отношение.
  4,88 % — оплата частями, 0,4 % — кошелёк.
* **СПП финансирует WB и выручку продавца не уменьшает.** В формуле выплаты её нет.
* Разрыв «43,8 % против 45,32 %»: ставка выросла 34,5 % → 42,25 % в июле 2026,
  а СПП упала с ~43 % до 17–22 % — это подняло эквайринг в процентах от цены продавца.
* `MART_SKU_DAILY.orders_qty` — **NET**. Gross = `FACT_ORDERS: SUM(quantity)`,
  отмены = `SUM(IF(is_cancel,quantity,0))`. Окно перезабора отмен 21 день.
* Логистика 30D: «Логистика» 187 строк + «Доставка» 57 строк = 244 / 15 522,43 ₽
  на 212 выкупленных единиц = **73,2190 ₽/ед**.
* `LAST_CLOSED_DATE = 2026-09-09` (MART build_as_of, FACT_ORDERS, PAID_STORAGE все на 09.09;
  финансы дотянулись до 10.09, но ограничитель — заказы и остатки).

### Параметры R6 в `ZZ_CONFIG`

| ключ | значение |
|---|---|
| `BASE_COMMISSION_RATE` | 0,422506 |
| `ROLLING_EFFECTIVE_ACQUIRING_30D` | 0,031822 |
| `TOTAL_OPERATIONAL_RATE` | 0,454328 ← стоит в `AD737:AD766` |
| `ACQUIRING_WINDOW` | 2026-08-11..2026-09-09 (D-30..D-1, D = LAST_CLOSED_DATE+1) |
| `ACQUIRING_SAMPLE_SIZE` | 212 |
| `ACQUIRING_CONFIDENCE` | HIGH (порог 30) |
| `REALIZED_SPP_ROLLING_30D` | 0,1702 — **диагностика**, в формулы не входит |
| `TAX_RESERVE_BASE` | AA (цена продавца) |

`AH = IF($M>LAST_CLOSED_DATE;"";AA*2%)` — управленческий резерв, **не расчёт УСН**.

---

## 3. ИЗМЕНЁННЫЕ ФАЙЛЫ

### Modified

| файл | что |
|---|---|
| `infra/terraform/secrets.tf` | `data` на существующий `WB_TOKEN_REPORTS` + пор**есурсный** `secretAccessor` |
| `.github/workflows/deploy-prod.yml` | отдельный шаг деплоя `wb-funnel-prod` с `WB_ANALYTICS_SECRET=WB_TOKEN_REPORTS` |
| `cloud/src/config.ts` | `funnelLookbackDays` (14), `funnelRawTable`, `funnelObservationsTable` |
| `cloud/src/loaders/registry.ts` | регистрация `funnel` с `logicalPeriod: d1Moscow` |
| `CHANGELOG.md` | записи R5 и R6 |

### Untracked (новые, ещё не в git)

```
infra/terraform/wb_funnel_loader.tf     Cloud Run Job + Scheduler (paused) + IAM
cloud/src/loaders/funnel/               wbApi.ts · normalize.ts · bq.ts · index.ts
cloud/test/funnel_normalize.test.ts     юнит-тесты (в этой сессии не запускались: node_modules под macOS-arm64)
sql/ddl/wb_raw_funnel.sql               RAW_WB_FUNNEL_DAILY · WB_FUNNEL_OBSERVATIONS · V_WB_FUNNEL_DAILY · V_WB_FUNNEL_COVERAGE
sql/ddl/wb_raw_paid_storage.sql         RAW_WB_PAID_STORAGE (создана) + views (созданы)
apps-script/UnitkaFill.gs · UnitkaR2.gs · UnitkaR3Ingest.gs · UnitkaR4.gs · UnitkaR5.gs · UnitkaR6.gs
docs/UNITKA_2_0_*.md                    11 документов R1–R6
```

⚠️ `docs/ozon/…` в untracked — **не из этой работы**, не коммитить вместе с юниткой.

---

## 4. GIT STATUS / BRANCH

```
branch : main
HEAD   : 3c29231 (2026-09-10) feat(control-tower): Phase 1.2 — owner UX simplification

 M .github/workflows/deploy-prod.yml
 M CHANGELOG.md
 M cloud/src/config.ts
 M cloud/src/loaders/registry.ts
 M infra/terraform/secrets.tf
?? apps-script/Unitka{Fill,R2,R3Ingest,R4,R5,R6}.gs
?? cloud/src/loaders/funnel/
?? cloud/test/funnel_normalize.test.ts
?? docs/UNITKA_2_0_*.md
?? infra/terraform/wb_funnel_loader.tf
?? sql/ddl/
?? docs/ozon/…            ← чужая работа, не трогать
```

Коммитов не делал. Работа ведётся прямо на `main` — перед коммитом завести ветку.

---

## 5. SECRETS

GCP project: **`project-fa311fc0-4d87-4781-986`**

| секрет | кем создан | назначение | статус |
|---|---|---|---|
| `WB_TOKEN_ANALYTICS` | Terraform (`local.wb_secrets`) | остатки, платное хранение | 🔴 **НЕ ТРОГАТЬ И НЕ ПЕРЕВЫПУСКАТЬ** |
| `WB_TOKEN_ADS` | Terraform | реклама | — |
| `WB_TOKEN_FINANCE` | Terraform | финансы | — |
| `WB_TOKEN_REPORTS` | **владелец вручную** | токен «Аналитика» с правом на отчёты; нужен воронке | подключается этим хендофом |

Второй секрет для воронки не заводить. Значение токена в Script Properties не копировать.

Отдельно: в Apps Script проекте `evetis-wb-analitics`
(`10fr6u1YVdrTvZo7bkd88K4Vuv_-HiPerDbTw-9S9N34AUDQtfg6FtDkS`) в Script Properties лежат
8 токенов WB (ANALYTICS, CONTENT, DOCUMENTS, FINANCE, MARKETPLACE, PROMOTION,
STATISTICS, SUPPLIES). `WB_TOKEN_REPORTS` там **отсутствует** и добавлять его туда не нужно —
облачный загрузчик читает Secret Manager.

---

## 6. SERVICE ACCOUNT, КОТОРЫЙ ДОЛЖЕН ЧИТАТЬ `WB_TOKEN_REPORTS`

```
sa-loaders-prod@project-fa311fc0-4d87-4781-986.iam.gserviceaccount.com
```

Объявлен в `infra/terraform/service_accounts.tf` как `google_service_account.loaders_prod`
(`account_id = "sa-loaders-prod"`, «WB loaders runtime (PROD)»). Это `service_account`
у Cloud Run Job `wb-funnel-prod`.

Роль — **только** `roles/secretmanager.secretAccessor`, **только** на этот секрет.
Project-wide не выдавать.

---

## 7. ПОДГОТОВЛЕННЫЕ TERRAFORM-ФАЙЛЫ

### `infra/terraform/secrets.tf` (добавлено)

```hcl
data "google_secret_manager_secret" "wb_token_reports" {
  secret_id = "WB_TOKEN_REPORTS"
}

resource "google_secret_manager_secret_iam_member" "prod_access_wb_token_reports" {
  secret_id = data.google_secret_manager_secret.wb_token_reports.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.loaders_prod.email}"
}
```

Секрет подключён как `data`, а не `resource`: он уже существует, и `apply` на `resource`
упал бы «already exists». Terraform значение токена не хранит.

### `infra/terraform/wb_funnel_loader.tf` (новый)

* `google_cloud_run_v2_job.wb_funnel_prod` — образ из `var.container_image`, `args = ["funnel"]`,
  `max_retries = 0`, `timeout = 900s`, `service_account = loaders_prod`
* `local.funnel_env`: `WB_ANALYTICS_SECRET = "WB_TOKEN_REPORTS"`, `FUNNEL_LOOKBACK_DAYS = "14"`,
  `FUNNEL_RAW_TABLE`, `FUNNEL_OBSERVATIONS_TABLE`, хост `seller-analytics-api.wildberries.ru`
* `google_cloud_run_v2_job_iam_member` — `roles/run.invoker` для `sa-scheduler-prod`, пор**есурсно**
* два `google_bigquery_table_iam_member` — `dataEditor` на уровне **таблиц**, не датасета
* `google_cloud_scheduler_job.wb_funnel_prod` — `30 6 * * *` UTC (09:30 МСК), **`paused = true`**

🔴 `lifecycle.ignore_changes` включает `containers[0].env` — это действует **на UPDATE**.
Значит живому Job'у имя секрета выставляет не `apply`, а деплой (см. ниже).

### `.github/workflows/deploy-prod.yml` (добавлен шаг)

```yaml
- name: Deploy wb-funnel-prod (отдельным шагом — у него свой секрет)
  run: |
    gcloud run jobs update wb-funnel-prod \
      --region ${{ vars.GCP_REGION }} \
      --image "${{ inputs.image_digest }}" \
      --update-env-vars IMAGE_DIGEST=…,GIT_SHA=…,WB_ANALYTICS_SECRET=WB_TOKEN_REPORTS
```

Отдельный шаг, а не общий цикл `for JOB in …`: иначе `WB_ANALYTICS_SECRET` попал бы
на `wb-stocks-prod` / `wb-mart-prod` / `wb-prices-prod` / `wb-tariffs-prod`, у которых секрет свой.

---

## 8. КОМАНДЫ

### Предварительно

```bash
git checkout -b feat/unitka-2.0-funnel-reports-token
# закоммитить: apps-script/Unitka*.gs, cloud/src/loaders/funnel/, cloud/test/, sql/ddl/,
#              docs/UNITKA_2_0_*.md, infra/terraform/wb_funnel_loader.tf, secrets.tf,
#              deploy-prod.yml, config.ts, registry.ts, CHANGELOG.md
# НЕ коммитить docs/ozon/** — чужая работа
cd cloud && npx tsc --noEmit && npx vitest run test/funnel_normalize.test.ts
cd infra/terraform && terraform fmt -check && terraform validate
```

### Порядок включения воронки (менять нельзя)

```
1) GitHub Actions → infra → action: plan     # убедиться, что план = только secret IAM + job env
2) GitHub Actions → infra → action: apply    # sa-loaders-prod получает secretAccessor
3) BigQuery: создать таблицы воронки, если их ещё нет
     bq query --use_legacy_sql=false < sql/ddl/wb_raw_funnel.sql
4) GitHub Actions → deploy-prod              # image_digest + source_git_sha из deploy-shadow
5) gcloud run jobs execute wb-funnel-prod --region europe-west1 --wait
6) Проверить QA (раздел 9). ТОЛЬКО ПОСЛЕ ЭТОГО:
   gcloud scheduler jobs resume wb-funnel-prod --location europe-west1
```

### Заполнить `P` и `R` в книге

После успешной загрузки — тем же способом, что R6: новый файл в Apps Script проекте
`evetis-wb-analitics`, **ровно одна публичная функция** (остальные с суффиксом `_`),
запись блоком через `setValues()`, обёртка закрытого дня сохраняется.

⚠️ Ловушки, проверенные кровью:
* выпадающий список функций в редакторе Apps Script **переключается ненадёжно** —
  делайте единственную публичную функцию;
* дубликат имени функции между файлами молча перекрывает одну из версий;
* лимит выполнения 6 минут: только блочные `getValues()`/`setValues()`, никаких
  поцелльных циклов на этой книге (315k формул);
* `insertAll` в BigQuery держит строки в streaming buffer ~90 минут — `DELETE` по ним
  падает; идемпотентность должна ловить это и **пропускать**, а не грузить повторно.

---

## 9. ЧТО СЧИТАТЬ PASS

### Загрузка воронки

| проверка | PASS |
|---|---|
| `POST /api/v2/nm-report/downloads` | **200** (403 «Report not available» = токен всё ещё без права на отчёт) |
| grain `RAW_WB_FUNNEL_DAILY` | `date_msk × nm_id`, минимум `openCardCount` и `addToCartCount` |
| дубли | один `run_id` на окно, повторный запуск не удваивает строки |
| покрытие 01–09.09 | 9 дней из 9 по nmID 252442517 |
| источник | **воронка карточки целиком, включая органику.** Клики рекламы — НЕ переходы (01.08.2026: в книге 103 перехода, кликов в BQ 52) |

### Сверка книга ↔ BigQuery

```
DATE | BQ_CARD_OPENS | SHEETS_CARD_OPENS | BQ_ADD_TO_CART | SHEETS_ADD_TO_CART | STATUS
```
PASS = **STATUS = OK на всех 9 днях**, MISMATCH = 0.

### Финальный QA сентября (эталон после R6, должен воспроизводиться)

```
#REF! = 0 · #VALUE! = 0 · #NAME? = 0 · #DIV/0! = 0 · #N/A = 0 · #ERROR! = 0
MISMATCH = 0
будущих дней 21, непустых экономических ячеек в них 0
AD737 = 45,4 %   AD767 = 45,4 %
AH737 = IF($M737>LAST_CLOSED_DATE;"";AA737*2%) → 10 ₽
AE737 = 272 ₽    AI737 = −51 ₽    AI767 = −43 ₽
AG767 хранение = 264 ₽    AF737 логистика = 73 ₽
W767 доходность общая = −8 728 ₽    Q767 заказы = 59
```

### Тождество выплаты (регресс-тест на любом окне)

`цена продавца − базовая комиссия − эквайринг = for_pay`
PASS = **MAE ≤ 0,01 ₽, MAX ≤ 0,02 ₽, строк вне допуска 0**, sample ≥ 100.

---

## 10. ЧТО НЕЛЬЗЯ ТРОГАТЬ

* 🔴 **Август и более ранние периоды.** Ни одной ячейки, ни автоматически, ни «заодно».
  Строки 700–734 на листе `WB_Юнит_2025`.
* 🔴 **Другие SKU.** Работаем только с блоком `M` (крем для рук, nmID 252442517).
  Остальные 23 блока — после отдельного подтверждения владельца.
* 🔴 **Визуальный стандарт.** Текущее оформление APPROVED владельцем в R5.
  Никаких редизайнов, перекрашиваний, перестановок колонок и переименований листов.
* 🔴 `WB_TOKEN_ANALYTICS` — не перевыпускать и не перезаписывать.
* 🔴 Не копировать значения августа в сентябрь. Не выдавать оценку за факт.
  Нет данных — ставить **GAP**, а не выдумывать.
* 🔴 Не создавать второй загрузчик воронки: пайплайн уже есть в `cloud/src/loaders/funnel/`.
* 🔴 Не включать iterative calculations, чтобы спрятать ошибку.
* 🔴 Автономная запись в книгу — только по ручному запуску.

---

## 11. ТЕКУЩИЙ BLOCKER

**Funnel loader не подключён к `WB_TOKEN_REPORTS`.**

Состояние API на 10–11.09.2026, установлено живым зондированием
(`dev.wildberries.ru` отдаёт 498 автоматическим запросам, документацию прочитать нельзя):

| ручка | ответ с `WB_TOKEN_ANALYTICS` |
|---|---|
| `GET /api/v2/nm-report/downloads` | 200 |
| `POST /api/v2/nm-report/downloads` (`DETAIL_HISTORY_REPORT`) | **403** `{"title":"Authorization error","detail":"Report not available"}` |
| `POST …` (`GROUPED_HISTORY_REPORT`) | 403, то же |
| legacy `POST /api/v1/nm-report/detail`, `/content/v2/analytics/nm-report/detail` | 404 — сняты WB |
| `GET /api/v1/paid_storage` (контроль) | 200 |

Диагноз: **право токена на отчёт**, не код и не деплой.

Почему не доделано в сессии Cowork: ни одна доступная среда не имеет сетевого пути
к Google Cloud и к WB одновременно —

| среда | `secretmanager` / `run.googleapis.com` | `seller-analytics-api.wildberries.ru` |
|---|---|---|
| облачный контейнер | 403 от egress-шлюза (policy denial) | 403 |
| локальная VM рабочего стола | сети нет (`000`) | сети нет |
| Apps Script | нужен scope `cloud-platform`, правка манифеста заблокирована | работает, но токен в Secret Manager |

Поэтому подключение оформлено кодом (раздел 7) и требует прогона из CI (раздел 8).

---

## 12. КРИТЕРИЙ ЗАВЕРШЕНИЯ

```
✔ P (Переходы)          заполнены из WB API за 01–09.09.2026, источник openCardCount
✔ R (Положили в корзину) заполнены из WB API за тот же период, источник addToCartCount
✔ QA сверка книга ↔ BigQuery: MISMATCH = 0 на всех днях
✔ ошибочных значений в блоке: 0
✔ будущие дни остаются пустыми
✔ SEPTEMBER MASTER TEMPLATE READY: YES
```

После этого — **остановиться и ждать одобрения владельца**. Ни август, ни масштабирование
на остальные 23 SKU без отдельного подтверждения не начинать.
