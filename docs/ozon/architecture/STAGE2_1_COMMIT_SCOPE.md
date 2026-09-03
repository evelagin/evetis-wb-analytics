# Предлагаемый объём коммита после Stage 2.1

Коммит **не выполнен**. Это предложение.

## Что коммитить

**Документация и архитектура — `docs/ozon/`**
- `architecture/MARKETPLACE_ISOLATION.md`
- `architecture/ISOLATION_COMPLIANCE_CHECK_2026-09-01.md`
- `architecture/PRODUCT_REFERENCE_FIELD_CLASSIFICATION.csv`
- `architecture/STAGE2_1_COMMIT_SCOPE.md`
- `OZON_DATA_CONTRACT_V1.md`
- `OZON_BIGQUERY_DESIGN_V1.md`
- `audit_2026-08-30/LOG.md`
- `audit_2026-08-30/reports/*.md`

**Производные датасеты — `audit_2026-08-30/data/*.csv`** (~1,1 МБ, 36 файлов).
Это результат аудита, воспроизводимый скриптами, но полезный как зафиксированный
снимок доказательств. Крупнейший — `finance_accrual_fact.csv` 404 КБ.

**Скрипты, годные в production — предлагается перенести в `tools/ozon/`**
- `raw/backfill_v1/_bf_common.py` — секреты, retry, throttling, журнал ошибок
- `raw/backfill_v1/_bf_finance.py`, `_bf_seller.py`, `_bf_performance.py`, `_bf_sku_spend.py`
- `raw/_manifest_add.py` — ведение манифеста
- `build_facts.py`, `build_economics.py`, `qa_backfill.py`

## Что не коммитить

**Сырой корпус `audit_2026-08-30/raw/` — 9,6 МБ, 321 файл.** Предлагаю исключить целиком
и добавить в `.gitignore`, потому что:

- он полностью воспроизводим загрузчиками из API;
- целостность и происхождение уже зафиксированы в `raw/MANIFEST.csv` (SHA-256 на каждый файл);
- `raw/api/ozon_seller_swagger_2026-08-31.json` (3,8 МБ) и `ozon_perf_swagger_2026-08-31.json`
  (300 КБ) — чужие публичные спецификации, им не место в нашей истории;
- 6 скриншотов (1,4 МБ) — доказательство UI-состояния, а не источник цифр.

**Исключение: `raw/MANIFEST.csv` коммитить.** Это и есть доказательная база —
298 записей с хешами, источниками и временами извлечения. Без сырых файлов он остаётся
проверяемым описанием того, что и когда было получено.

**Одноразовые артефакты аудита, не production-код** — оставить в `docs/ozon/`, но не
переносить в `tools/ozon/`: все `raw/api/_probe*.sh`, `_smoke_test.sh`, `_perf_*.sh`,
`_s17_*.sh`, `_s21_*.sh`, `_s17_finance_accruals.py`, `_bf_sku_spend_boundary.py`.
Это разведочные пробы конкретных гипотез, у них нет будущего использования.

**`docs/ozon/prompt_ozon_audit.md`** — исходное задание, решение владельца.

## Стратегия по raw

Предлагаемое дополнение к `.gitignore`:

```
docs/ozon/audit_*/raw/**
!docs/ozon/audit_*/raw/MANIFEST.csv
```

Если владелец хочет сохранить сырые данные — их место в GCS, а не в git,
с тем же манифестом в качестве индекса.

## Проверка на секреты

Выполнена перед подготовкой этого документа. Искались **значения**, а не имена полей:
Api-Key, Performance `client_secret`, JWT-подобные строки `eyJ…`.
Результат — чисто. Строки вида `access_token` в исходниках загрузчиков утечкой
не считаются: это имена полей в коде.

`Client-Id` из скана исключён сознательно: Ozon сам возвращает его как `company_id`
в ответах `/v1/returns/list`. Это идентификатор компании, а не секрет.

## Предлагаемое сообщение коммита

```
feat(ozon): audit v1, data contract and BigQuery design

Аудит кабинета Ozon за 2026-06-01..2026-08-31, только чтение.

- каталог 20/20, себестоимость из evetis_ref, покрытие полное
- сверка CPC: Performance API против начислений, 0,00 руб на 90 днях
- SKU-атрибуция рекламы 99,38% через асинхронный отчёт Performance API
- найдена неучтённая статья маркетинга: тип 116, Сбор первых отзывов
- зафиксировано правило изоляции доменов WB и Ozon
- спроектированы REF_PRODUCT_MASTER и REF_SKU_CHANNEL_MAP, устраняющие D024

DDL не выполнялся. В Ozon ничего не менялось. IAM не менялся.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
```
