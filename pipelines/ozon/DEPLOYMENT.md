# Ozon ingestion — происхождение и контракт развёртывания

Stage A / A3, 2026-09-08. Закрывает Ozon-часть находки **F-05**: до этого этапа
`pipelines/ozon/**` не входил ни в один workflow, образы не несли `GIT_SHA`, и
соответствие развёрнутого кода репозиторию считалось **UNKNOWN**.

Теперь оно **доказано**.

---

## 1. Что реально работает в production (проверено 2026-09-08)

| Cloud Run Job | Образ | ENTITIES | Расписание (MSK) |
|---|---|---|---|
| `ozon-runtime-fast` | `ozon-runtime-ingest@sha256:14f8a4c8…` | `stocks,fbo_postings` | `0 7,13,19 * * *` |
| `ozon-runtime-daily` | тот же digest | `catalog,prices,seller_info,finance_accrual,ads_campaigns,ads_expense_daily,ads_sku_daily,supplies` | `30 6 * * *` |
| `ozon-runtime-weekly` | тот же digest | `clusters` | `0 5 * * 1` |
| `ozon-runtime-ingest` | тот же digest | — | расписания нет |
| `ozon-bootstrap-load` | `ozon-bootstrap-load@sha256:2948b5cb…` | `ONLY_TABLES=…` | расписания нет (разовый) |

Все три рабочих джоба делят один образ и одну identity
(`sa-ozon-ingestion`); различается только `ENTITIES`.

---

## 2. Провенанс — доказан, UNKNOWN не осталось

### runtime (три рабочих джоба + `ozon-runtime-ingest`)

```
pipelines/ozon/runtime @ 4aa0070
        │  (diff -r: различий нет, 5 файлов)
        ▼
gs://project-fa311fc0-4d87-4781-986_cloudbuild/
    source/1788693831.486486-0fa18e0819294fd7bb2d58421c793d0f.tgz
        │
        ▼  Cloud Build 8b1a72dc-d9dc-4cdb-ba21-d085f429d1db
           europe-west1 · SUCCESS · 2026-09-06T11:23:54Z · тег stage3-4d3
        │
        ▼  @sha256:14f8a4c8d13fde7ae5d4e3364fe1ad5ce82c54d4b5c6d92c2313325d20b3f40c
        │
        ▼  ozon-runtime-fast / -daily / -weekly / -ingest
           digest закреплён в infra/terraform/ozon_ingestion.tf (local.ozon_runtime_image)
```

Сверка выполнена побайтово: `main.py`, `common.py`, `entities.py`,
`requirements.txt`, `Dockerfile` — идентичны текущему HEAD.

### bootstrap

```
pipelines/ozon/bootstrap @ 4aa0070
        │  (diff -r: различий нет, 4 файла)
        ▼
gs://run-sources-…-europe-west1/jobs/ozon-bootstrap-load/1788427420.934997-….zip
        │
        ▼  Cloud Build ff61b435-fd71-45bc-bd40-982ed18b4ad7 · 2026-09-03T09:23:43Z
        │
        ▼  @sha256:2948b5cb3571dd3491f6c1b1424608f66e4721a7d794bb8a61dad81ef109a11a
        ▼  ozon-bootstrap-load
```

---

## 3. 🔴 Тег `latest` НЕ является идентификатором production

Наблюдение Stage A в Artifact Registry
(`cloud-run-source-deploy/ozon-runtime-ingest`):

| digest | создан | теги |
|---|---|---|
| `14f8a4c8…` | 2026-09-06 11:24 | `stage3-4d3` ← **работает в production** |
| `43fb3a10…` | 2026-09-06 10:45 | `stage3-4d2` |
| `a7ce446a…` | 2026-09-03 14:56 | **`latest`** |

Тег `latest` указывает на образ, который **на трое суток старше** работающего.
Развёртывание «по `latest`» откатило бы Ozon ingestion назад и не оставило бы
следа в Git. Поэтому:

- **джобы обязаны ссылаться на digest**, а не на тег — это закреплено тестом
  `test_runtime_image_is_pinned_by_digest`;
- Terraform хранит digest в `local.ozon_runtime_image` и является источником
  истины о том, что развёрнуто.

---

## 4. Контракт будущего развёртывания

Тот же принцип, что у WB-загрузчиков (`.github/workflows/deploy-shadow.yml` →
`deploy-prod.yml`): **build once → promote exact digest**.

1. **Валидация** — `ci.yml`, job `ozon`: `compileall`, тесты контракта,
   `docker build` без push. Срабатывает на изменения `pipelines/ozon/**`.

   **Контракт совместимости runtime (Tenancy T2.2).** В эти тесты входит различающий
   прогон `tests/test_differential_compat.py`. Он сравнивает текущий runtime с
   конфигурацией EVETIS с замороженным эталоном до T2 (`tests/compat/baseline_pre_t2`) по
   всему наблюдаемому: HTTP, секретам, SQL, данным, журналу, stdout, stderr, коду выхода.
   Разрешены ровно две разницы: D1 — вырезание секретов, D2 — вывод сбоя журнала.
   Промоушен образа без зелёного прогона запрещён. Любая новая разница — осознанное
   изменение контракта: её добавляют в `ALLOWED_DIFFERENCES` с проверкой, а не
   ослаблением сравнения. Решение владельца 2026-09-24: EVETIS остаётся на
   `sha256:24e3c6d6…` до отдельных ворот выката runtime T2/T2.2.
2. **Сборка** — из коммита `main`, с меткой коммита:
   ```bash
   gcloud builds submit pipelines/ozon/runtime \
     --project=project-fa311fc0-4d87-4781-986 --region=europe-west1 \
     --tag europe-west1-docker.pkg.dev/project-fa311fc0-4d87-4781-986/\
   cloud-run-source-deploy/ozon-runtime-ingest:$(git rev-parse --short HEAD)
   ```
   Тег — короткий SHA коммита, не `latest`.
3. **Фиксация digest** — взять `results.images[].digest` из сборки и записать
   его в `local.ozon_runtime_image`. Коммит этой строки и есть запись о том, что
   развёрнуто.
4. **Раскатка** — НЕ `terraform apply`. Поле `image` у `google_cloud_run_v2_job.ozon_runtime`
   стоит под `ignore_changes` (см. `lifecycle` в `ozon_ingestion.tf`), поэтому целевой план
   на этот ресурс даёт «No changes» даже при новом digest в файле — проверено 2026-09-22.
   Образ продвигается так же, как у WB-загрузчиков, — обновлением самих Job'ов:

   ```bash
   for JOB in ozon-runtime-daily ozon-runtime-fast ozon-runtime-weekly ozon-runtime-promo; do
     gcloud run jobs update "$JOB" --region europe-west1 \
       --image europe-west1-docker.pkg.dev/project-fa311fc0-4d87-4781-986/\
   cloud-run-source-deploy/ozon-runtime-ingest@sha256:…
   done
   ```

   Digest в `local.ozon_runtime_image` — запись о том, что развёрнуто, а не источник
   раскатки. Все **четыре** джоба обязаны получить ОДИН digest: разъехаться они не могут
   только потому, что команда одна на всех, а не потому, что это гарантирует Terraform.
   Четвёртый — `ozon-runtime-promo` (PR-PROMO-1, наблюдатель акций, каденция 4 раза в
   сутки). Состав образа проверяется тестом `tests/test_image_contents.py`: всё, что
   импортируется от `main.py`, обязано входить в `COPY` Dockerfile.

**Пока не сделано (осознанно, вне Stage A):** образ не несёт `GIT_SHA` в env,
как это устроено у WB-загрузчиков. Добавление переменной означает новую ревизию
всех трёх джобов, то есть production-изменение, которое Stage A не выполняет.
Провенанс до этого момента обеспечивается парой «тег коммита + digest в
Terraform».

---

## 4a. 🔴 НЕ РАЗВЁРНУТО: окно загрузки начислений 30 суток (Gate 9)

Gate 9 изменил окно `finance_accrual` с 14 на 30 суток
(`pipelines/ozon/runtime/entities.py`, `REGISTRY`). Изменение **в `main`, но не в
production**: три рабочих джоба стоят на образе `@sha256:b00380d6…`, собранном до
Gate 9, а `pipelines/ozon/**` не входит ни в один деплойный workflow — образ
собирается вручную по §4.

Замер 2026-09-22: суточный прогон 06:30 МСК взял окно `2026-09-08..2026-09-22`,
то есть 14 суток + сегодня. Это ещё старый код.

**Данные при этом не потеряны.** Перезагрузка за 113 суток (`2026-06-01..2026-09-21`)
получила 2300 строк и вставила **0**: ни одного начисления с `event_date` старше
14 суток за всю историю не появлялось. 30 суток — запас на случай, когда Ozon
опубликует начисление задним числом, а не устранение известной потери.

Развернуть: собрать образ из `main` по §4, записать digest в
`local.ozon_runtime_image` (`infra/terraform/ozon_ingestion.tf`), `terraform apply`.
До этого момента считать production-окно равным **14**, а не 30.

---

## 5. `ozon-bootstrap-load` — разовый артефакт, решение владельца открыто

Джоб выполнял однократную загрузку исторических данных из
`gs://evetis-ozon-staging-…/bootstrap_v1`:
`ONLY_TABLES=clusters,supply_orders,supplies,supply_bundles,stocks_20260903,orders_fbo`.
Расписания нет, последний запуск — 2026-09-03.

Классификация: **migration/bootstrap artifact**, не production-пайплайн.

Stage A намеренно **не** заводит его в Terraform: постоянный ресурс в IaC читался
бы как часть регулярного контура и рано или поздно получил бы расписание.
Удалять его Stage A тоже не может — это разрушающая операция.

Решение владельца (одно из двух):
- **оставить как есть** — тогда зафиксировать в `wb_ops` как `MANUAL`-пайплайн,
  чтобы он не выглядел неучтённым ресурсом;
- **удалить** после подтверждения, что повторный bootstrap не понадобится;
  исходники и архив сборки останутся в Git и GCS.

---

## 6. Что Stage A НЕ делал

- бизнес-логика ingestion не менялась;
- ни одна сущность не переносилась между каденциями;
- новые вызовы Ozon API не добавлялись, записи в Ozon нет и не появилось;
- джобы не пересобирались и не переразвёртывались;
- `ozon_raw` и `ozon_mart` не изменялись.
