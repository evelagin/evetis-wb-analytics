# Stage A / A4 — изоляция прав Ozon-загрузчика: незавершённый шаг

**Дата:** 2026-09-08 · **Находка:** F-04 (HIGH) · **Статус: PARTIAL — требуется один шаг владельца**

---

## Что было и почему это дефект

`sa-ozon-ingestion@project-fa311fc0-4d87-4781-986.iam.gserviceaccount.com` имел
на уровне **всего проекта**:

| Роль | Что даёт |
|---|---|
| `roles/bigquery.jobUser` | запускать job'ы — нужна |
| `roles/bigquery.dataEditor` | **запись во все датасеты**, включая `wb_raw`, `wb_mart`, `wb_ops`, `evetis_ref` |
| `roles/bigquery.dataViewer` | **чтение всех датасетов** |

Проектный `dataEditor` означает членство в `projectWriters`, то есть WRITER на
каждом датасете проекта. Ошибка в `pipelines/ozon` — например, неверный
`BQ_RAW_DATASET` — могла бы перезаписать данные Wildberries. Это нарушение
правила изоляции маркетплейсов: общим слоем должен оставаться только
`evetis_ref`, и только на чтение.

Для сравнения, WB-загрузчики так не настроены: у `sa-loaders-prod` на уровне
проекта только `jobUser`, а доступ к данным выдан поимённо на таблицы
(`infra/terraform/iam.tf`, `wb_tariffs_loader.tf`).

---

## Доказательство достаточного набора прав

Не предположение, а факт из `INFORMATION_SCHEMA.JOBS_BY_PROJECT` за 180 суток
по `user_email` этого service account:

| Датасет | Всего обращений | Write-job'ов | SELECT | Типы | Первое | Последнее |
|---|---|---|---|---|---|---|
| `ozon_raw` | 502 | 310 | 192 | SELECT, MERGE | 2026-09-03 | 2026-09-08 |

Разбивка по типам job'ов: 158 `LOAD` и 155 `MERGE` — все с назначением в
`ozon_raw`; 310 `SELECT` пишут в анонимный временный датасет.

**Ни одного обращения** к `wb_raw`, `wb_mart`, `wb_ops`, `evetis_ref`,
`ozon_mart`, `ozon_stg`. Упоминание `evetis_ref.REF_SKU_CHANNEL_MAP` в
`pipelines/ozon/runtime/common.py` — только в docstring: резолв идентификаторов
выполняется во вьюхах `ozon_mart`, а не в загрузчике.

Почему именно `dataEditor` на датасете, а не что-то слабее: на каждую сущность
загрузчик создаёт временную таблицу `_rt_<table>_<run>`, грузит в неё LOAD-job'ом,
делает MERGE и удаляет временную (`common.py:146-186`). Нужны `tables.create` и
`tables.delete` внутри датасета.

Доступ к GCS уже узкий и не трогается: `roles/storage.objectViewer` выдан
побакетно на `gs://evetis-ozon-staging-37074083763`.

---

## Что уже выполнено (2026-09-08)

✅ **Шаг 1 — выдан гранулярный доступ.** В ACL датасета `ozon_raw` добавлена
запись `roles/bigquery.dataEditor` → `sa-ozon-ingestion`
(BigQuery нормализует её как legacy `WRITER`):

```json
{"role": "WRITER", "userByEmail": "sa-ozon-ingestion@project-fa311fc0-4d87-4781-986.iam.gserviceaccount.com"}
```

Операция аддитивная: прав ни у кого не убыло, поведение пайплайна не изменилось.

✅ **Целевое состояние описано в IaC** — `infra/terraform/ozon_iam.tf`.

---

## ⛔ Что осталось — два шага, которые должен выполнить владелец

Снятие проектных ролей заблокировано политикой рабочей среды ассистента
(изменение project-level IAM). Команды готовы и проверены на синтаксис:

```bash
gcloud projects remove-iam-policy-binding project-fa311fc0-4d87-4781-986 \
  --member="serviceAccount:sa-ozon-ingestion@project-fa311fc0-4d87-4781-986.iam.gserviceaccount.com" \
  --role="roles/bigquery.dataEditor"
```

```bash
gcloud projects remove-iam-policy-binding project-fa311fc0-4d87-4781-986 \
  --member="serviceAccount:sa-ozon-ingestion@project-fa311fc0-4d87-4781-986.iam.gserviceaccount.com" \
  --role="roles/bigquery.dataViewer"
```

**Порядок и окно.** Выполнять между запусками Ozon: расписания —
`ozon-fast` 07:00/13:00/19:00 MSK, `ozon-daily` 06:30 MSK, `ozon-weekly` пн 05:00 MSK.
Шаг 1 уже выполнен, поэтому окна без прав не возникает: гранулярный доступ
действует до снятия проектного.

### Приёмка после снятия

1. Роли SA на уровне проекта — должна остаться ровно одна:
   ```bash
   gcloud projects get-iam-policy project-fa311fc0-4d87-4781-986 \
     --flatten="bindings[].members" --format="value(bindings.role)" \
     --filter="bindings.members:sa-ozon-ingestion@project-fa311fc0-4d87-4781-986.iam.gserviceaccount.com"
   # ожидается: roles/bigquery.jobUser
   ```
2. Дымовой прогон штатного пути (идемпотентный MERGE, тот же джоб идёт по
   расписанию 3 раза в сутки — лишний запуск данные не портит):
   ```bash
   gcloud run jobs execute ozon-runtime-fast \
     --project=project-fa311fc0-4d87-4781-986 --region=europe-west1 --wait
   ```
3. Журнал прогона без ошибок прав:
   ```sql
   SELECT ingestion_run_id, entity, rows_inserted, rows_updated, errors
   FROM `project-fa311fc0-4d87-4781-986.ozon_raw.OZON_INGESTION_RUNS`
   ORDER BY started_at DESC LIMIT 10
   ```
4. Негативная проверка изоляции — SA больше не должен видеть WB-датасеты:
   ```bash
   gcloud auth print-access-token \
     --impersonate-service-account=sa-ozon-ingestion@project-fa311fc0-4d87-4781-986.iam.gserviceaccount.com
   # затем этим токеном: GET .../datasets/wb_raw  -> ожидается 403
   ```
   Ничего не вставлять в WB-датасеты ради проверки: достаточно отказа на чтение
   метаданных.

### Откат

Если Ozon ingestion начнёт падать с `PERMISSION_DENIED` — вернуть роль и
разобраться, какое обращение не покрыто:

```bash
gcloud projects add-iam-policy-binding project-fa311fc0-4d87-4781-986 \
  --member="serviceAccount:sa-ozon-ingestion@project-fa311fc0-4d87-4781-986.iam.gserviceaccount.com" \
  --role="roles/bigquery.dataEditor"
```

Снимок политики проекта до изменения сохранён вне репозитория на машине, где
выполнялся Stage A (`iam_backup/project_policy.before.json`); текущее состояние
всегда можно получить `gcloud projects get-iam-policy`.

---

## Текущее фактическое состояние

| Уровень | Роль | Статус |
|---|---|---|
| проект | `roles/bigquery.jobUser` | остаётся — нужна |
| проект | `roles/bigquery.dataEditor` | ⛔ **подлежит снятию** |
| проект | `roles/bigquery.dataViewer` | ⛔ **подлежит снятию** |
| датасет `ozon_raw` | `roles/bigquery.dataEditor` | ✅ выдана 2026-09-08 |
| бакет staging | `roles/storage.objectViewer` | было и остаётся |

Пока два проектных гранта не сняты, **F-04 остаётся открытой**: изоляция
маркетплейсов не достигнута, хотя целевые права уже выданы и описаны в Terraform.
