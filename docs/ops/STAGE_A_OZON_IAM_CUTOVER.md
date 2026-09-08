# Stage A / A4 — изоляция прав Ozon: поправка к находке F-04

**Дата:** 2026-09-08 (Stage A Closeout) · **Статус: CLOSED — изоляция подтверждена, ничего снимать не требовалось**

Предыдущая редакция этого документа описывала «незавершённый шаг» и содержала
команды снятия двух проектных ролей. **Эти команды выполнять НЕ НУЖНО, и они
удалены.** Ниже — что было установлено на самом деле.

---

## 1. В чём была ошибка диагностики

Аудит 2026-09-08 и первая редакция A4 утверждали, что
`sa-ozon-ingestion@project-fa311fc0-4d87-4781-986.iam.gserviceaccount.com`
имеет `roles/bigquery.dataEditor` и `roles/bigquery.dataViewer` **на уровне
всего проекта**, и потому может писать в `wb_raw`, `wb_mart`, `wb_ops` и
`evetis_ref`.

Это неверно. Обе привязки — **условные (IAM Conditions)**:

| Роль | Условие | Выражение |
|---|---|---|
| `roles/bigquery.dataEditor` | `ozon_raw_only` | `resource.name.startsWith(".../datasets/ozon_raw")` |
| `roles/bigquery.dataViewer` | `evetis_ref_only` | `resource.name.startsWith(".../datasets/evetis_ref")` |
| `roles/bigquery.jobUser` | — (безусловная) | право запускать job'ы, доступа к данным не даёт |

### Как ошибка возникла

```bash
# ❌ ТАК ПРОВЕРЯТЬ НЕЛЬЗЯ — condition молча отбрасывается,
#    и условная привязка выглядит как проектная
gcloud projects get-iam-policy PROJECT \
  --flatten="bindings[].members" --format="value(bindings.role)" \
  --filter="bindings.members:SA_EMAIL"
```

```bash
# ✅ ТАК ПРАВИЛЬНО — читать bindings[].condition
gcloud projects get-iam-policy PROJECT --format=json \
  | python3 -c "import json,sys
d=json.load(sys.stdin)
sa='serviceAccount:SA_EMAIL'
for b in d['bindings']:
    if sa in b.get('members',[]):
        print(b['role'], '| condition:', json.dumps(b.get('condition'), ensure_ascii=False))"
```

Во всём проекте условных привязок ровно две, и обе — на этом service account.

**Вывод: изоляция маркетплейсов была реализована до Stage A и реализована
правильно.** Ozon пишет только в свой RAW-слой и читает только общий справочный
слой `evetis_ref` — в точности правило проекта.

---

## 2. Что Stage A сделал и откатил

08.09.2026 в ACL датасета `ozon_raw` была добавлена запись `WRITER` для этого SA
как «шаг 1 сужения прав». На фоне уже существующего условного `dataEditor` она
была избыточной, а её обоснование — ошибочным.

**В Closeout запись удалена:** ACL `ozon_raw` вернулся ровно в то состояние, в
котором был до Stage A (`projectWriters` / `projectOwners` / владелец /
`projectReaders`, без персональных записей).

Эффективные права `sa-ozon-ingestion` за весь этап **не изменились ни разу**.

---

## 3. Доказательства изоляции

Прямая проверка через impersonation невозможна: у пользователя нет
`roles/iam.serviceAccountTokenCreator` на этом SA, а выдавать эту роль ради
проверки означало бы расширить права — Stage A такого не делает. Вместо одной
пробы используются три независимых доказательства.

**1. Определение.** IAM-условие вычисляется самой системой IAM. Роль физически
не применяется к ресурсу, чьё имя не удовлетворяет выражению условия. Ресурс
`.../datasets/wb_mart/tables/MART_SKU_DAILY` не начинается с
`.../datasets/ozon_raw`, поэтому `dataEditor` к нему не применяется.

**2. Других путей доступа нет.** Проверены ACL всех семи датасетов проекта:

| Датасет | Запись для `sa-ozon-ingestion` |
|---|---|
| `wb_raw` | нет |
| `wb_mart` | нет |
| `wb_ops` | нет |
| `evetis_ref` | нет |
| `ozon_raw` | нет |
| `ozon_mart` | нет |
| `ozon_stg` | нет |

Прочие проектные роли — только безусловный `bigquery.jobUser`.

**3. Эмпирика.** `INFORMATION_SCHEMA.JOBS_BY_PROJECT` за 180 суток по
`user_email` этого SA: 502 обращения к таблицам, **все в `ozon_raw`**;
158 `LOAD` и 155 `MERGE`, назначение — `ozon_raw`; ни одного обращения к
WB-датасетам.

**4. Дымовой прогон.** 2026-09-08 12:11 UTC, уже **без** избыточной записи ACL:

```
gcloud run jobs execute ozon-runtime-fast --wait
→ Execution [ozon-runtime-fast-v6khz] has successfully completed.
```

Журнал `ozon_raw.OZON_INGESTION_RUNS`:

| entity | rows_received | rows_updated | requests | errors |
|---|---|---|---|---|
| `stocks` | 197 | 197 | 2 | 0 |
| `fbo_postings` | 175 | 175 | 2 | 0 |

Загрузчик читает Ozon API, создаёт и удаляет временные таблицы `_rt_*`, делает
MERGE — всё внутри `ozon_raw`, с одними лишь условными привязками.

---

## 4. Текущее целевое состояние

| Уровень | Роль | Условие | Статус |
|---|---|---|---|
| проект | `roles/bigquery.jobUser` | — | оставлена |
| проект | `roles/bigquery.dataEditor` | `ozon_raw_only` | оставлена — это и есть изоляция |
| проект | `roles/bigquery.dataViewer` | `evetis_ref_only` | оставлена — разрешённое чтение общего слоя |
| бакет `evetis-ozon-staging-…` | `roles/storage.objectViewer` | — | оставлена, нужна bootstrap-джобу |
| датасет `ozon_raw` | ACL-запись `WRITER` | — | **удалена как избыточная** |

Описано в IaC: `infra/terraform/ozon_iam.tf`, включая оба условия.
Принято Terraform-ом 2026-09-08 (`terraform apply`: 12 imported, 0 destroyed);
повторный `plan` — `No changes`.

---

## 5. Что из этого следует для будущих проверок

1. Права service account проверять только через `--format=json` с чтением
   `bindings[].condition`. Плоский `value(bindings.role)` скрывает условия и
   превращает корректную настройку в ложную находку HIGH.
2. Не «сужать» права до того, как доказано, что они действительно широкие.
3. `roles/bigquery.dataViewer` на `evetis_ref` **оставить**: это объявленная
   зависимость от общего справочного слоя, разрешённая правилом изоляции.
   Отсутствие обращений за 180 суток — не основание её снимать.
