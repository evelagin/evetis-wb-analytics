# Миграция Cloud Run — rev2-final diff (2 обязательные правки + контракты PR-Mig0)

**Проект:** EVETIS WB Analytics · **Дата:** 2026-07-24
**Статус:** финальный аудит rev2 = APPROVE WITH TWO REQUIRED FIXES. Ниже — короткий
diff (как просил аудитор). После него: **APPROVED FOR PR-Mig0**. Кода/деплоя нет.
**Патчит:** `docs/MIGRATION_CLOUDRUN_DESIGN_2026-07-24_rev2.md` (§3 и §9).

---

## ✅ REQUIRED FIX 1 — execution-guard: добавить окружение в ключ (патч §3)

**Было:** ключ `loader_name × logical_period` (напр. `wb-stocks × 2026-07-24`).
**Проблема:** shadow и prod делят один ключ → shadow, записав COMPLETE за 24.07,
заставит prod-smoke-test вернуть `OK_NO_NEW` и НЕ записать прод. Shadow блокирует prod.

**Стало — единый контракт с составным ключом:**
```
PRIMARY LOGICAL KEY = environment × loader_name × logical_period
  shadow × wb-stocks × 2026-07-24
  prod   × wb-stocks × 2026-07-24
```
Таблица состояния — общая (`LOADER_RUNS`), но уникальность по этому составному ключу
(физическое разделение `LOADER_RUNS_SHADOW`/`_PROD` — допустимая альтернатива).

**Поля манифеста (контракт):** `environment`, `loader_name`, `logical_period`,
`run_id`, `execution_id`, **`image_digest`**, `git_sha`, `started_at`,
`completed_at`, `status`, `attempt_count`, `error_code`, `error_message`,
`rows_fetched`, `rows_loaded`. `image_digest` — чтобы доказать, какой именно
контейнер сформировал снимок (нужно для parity/cutover/расследований).

---

## ✅ REQUIRED FIX 2 — порядок cutover: сначала OFF, потом ON (патч §9)

**Было:** шаг 5 «включаем prod Scheduler» → шаг 6 «выключаем Apps Script» — между
ними оба писателя активны, при срабатывании cron получаем двойную запись в прод
(противоречит инварианту «никакой одновременной записи»).

**Стало (безопасный порядок):**
1. Проверить prod-запись Cloud Run (ручной execution прошёл).
2. Выбрать короткое окно cutover вне расписания загрузки.
3. **Отключить Apps Script trigger.**
4. **Убедиться, что старый execution сейчас не выполняется.**
5. **Включить Cloud Scheduler.**
6. При необходимости — вручную догрузить пропущенный logical_period (безопасно
   благодаря манифесту + идемпотентности).
7. Проверить следующий авто-execution.
8. Хранить Apps Script-код + инструкцию отката 7–14 дней.

Принцип: несколько минут вообще без активного расписания лучше, чем два писателя
одновременно. Пропуск догружается вручную.

---

## Принятые уточнения 3–7 (вносятся как контракты PR-Mig0, без rev3)

**3. Отдельная identity для Scheduler.** Помимо runtime SA — отдельные
`sa-scheduler-shadow` / `sa-scheduler-prod` (или один `sa-cloud-scheduler` с правом
запускать только конкретные Jobs). Роль на нужном Job: `roles/run.invoker` или уже
`roles/run.jobsExecutor` (нужен `run.jobs.run`). Разделяем роли: **Scheduler SA —
право запустить Job; Runtime SA — права контейнера во время выполнения.**

**4. Promotion одного digest.** CI собирает образ ОДИН раз → `sha256:ABC` → deploy
shadow `ABC` → parity → approve → deploy prod **тот же** `sha256:ABC`. Prod не
пересобирается. В манифест писать `image_digest` + `git_sha`.

**5. Иерархия retries (v1).** Три разных уровня с ограниченной ответственностью:
- HTTP (`wbHttp`): только 429 / временные 5xx / network timeout, exponential
  backoff + верхний лимит;
- Cloud Run task `maxRetries = 0..1`;
- Cloud Scheduler `retryCount = 1..2`.
Не складывать бесконтрольно (иначе один сбой cron → лавина WB-запросов и rate-limit).

**6. Точные BigQuery-права (контракт wb-stocks-shadow runtime SA):**
- Чтение `REF_SKU_MASTER`: `tables.get`, `tables.getData`.
- Запись `RAW_WB_STOCKS__CR`, `WB_STOCKS_SNAPSHOTS__CR`: `tables.get`,
  `tables.updateData`.
- Запуск задач: `jobs.create` (через `roles/bigquery.jobUser`, project-level,
  `location=EU`).
- Таблицы создаёт **Terraform** заранее → runtime SA НЕ нужен `tables.create`
  (только читает/пишет данные). Соответствует принципу «Terraform создаёт
  структуру, Job только читает/пишет данные».

**7. Управление Scheduler.** `infra.yml` (Terraform) владеет существованием и
конфигом Scheduler (cron, timezone, target, Scheduler SA, retry). Включение/
выключение при cutover — отдельный ручной workflow **`scheduler-control.yml`**
(параметры `environment`, `loader`, `action=pause|resume|run-now`) вместо похода в
Console (сохраняет аудит). `deploy-prod.yml` НЕ меняет cron/IAM/Scheduler — только
обновляет Job на утверждённый digest.

---

## Статус

Оба required fix внесены; уточнения 3–7 приняты в контракты PR-Mig0.
Вердикт аудита: **APPROVED FOR PR-Mig0**. Следующий шаг — foundation-PR (скелет
`/cloud` + Terraform + workflows + контракты Jobs/WIF/execution-guard[env-ключ]/
promotion/retry/logging), **без бизнес-логики**, + чек-лист разовой настройки GCP.
