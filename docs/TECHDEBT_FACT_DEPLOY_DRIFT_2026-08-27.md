# TECHNICAL DEBT — расхождение `pr_mart1_facts.sql` ↔ production

**Обнаружено:** 2026-08-27, gate Stage 3.1B PR1
**Статус:** зарегистрировано, НЕ исправляется в Stage 3.1B
**Владелец решения:** owner ACK 27.08 — «ремонт этого drift не входит в текущий scope»

---

## Что расходится

`sql/mart/pr_mart1_facts.sql` в репозитории содержит §1.7 — построение
`wb_mart.FACT_ADS_SPEND_ALLOC_DAILY` и `wb_mart.FACT_ADS_SPEND_UNALLOC_DAILY`
(отложенный change-set Stage 3B, commit `e30f668`). В развёрнутой процедуре
`wb_mart.sp_bootstrap_facts` этих объектов **нет**.

Доказательство (`wb_mart.INFORMATION_SCHEMA.ROUTINES`, замер 27.08.2026):

| маркер в `routine_definition` `sp_bootstrap_facts` | production |
| --- | :-: |
| `FACT_ADS_SPEND_ALLOC_DAILY` | отсутствует |
| `FACT_ADS_SPEND_UNALLOC_DAILY` | отсутствует |
| `marketplace_fee_gap_rub` (PR-B2) | присутствует |

Аналогичное расхождение по `pr_mart2b_sku_daily.sql` было закрыто ранее
возвратом файла на production-совместимую линию (Stage 1.6, шапка файла).
Для `pr_mart1_facts.sql` возврат не выполнялся.

## Почему это важно

Прогон `sql/mart/pr_mart1_facts.sql` с текущего HEAD **создаст два объекта
Stage 3B** и изменит `wb_mart` с 37 до 39 объектов. Любая будущая правка
FACT-слоя (например аддитивный пронос `srid` — маршрут R2 Stage 3.1B)
не может быть выполнена публикацией этого файла как есть.

## Что заблокировано этим долгом

* **Пронос `srid` в `FACT_FINANCE` / `FACT_SALES`** (deferred prerequisite
  Stage 3.1B). Пока его нет, связка «возврат → исходная продажа» выполняется
  на consumer-границе из canonical-слоя (`V_WB_FINANCE_SEMANTIC`,
  `V_WB_SALES_RETURNS`) — маршрут R3. Контракт потребителя от этого не зависит:
  переход на `FACT.srid` — замена источника в одном CTE.
* Любая другая аддитивная колонка FACT-слоя.

## Условие снятия

Отдельный PR синхронизации FACT deployment, в котором решается судьба §1.7:
либо Stage 3B выкатывается штатно, либо §1.7 выносится из
`pr_mart1_facts.sql` на production-совместимую линию — как это уже
сделано для `pr_mart2b_sku_daily.sql`.

**Stage 3B в рамках Stage 3.1B не начинается.**

## Смежные точки восстановления Stage 3B

* `git show e30f668 -- sql/mart/pr_mart2b_sku_daily.sql`
* `docs/ADS_SPEND_STAGE3B_2026-08-20.md`
* `docs/ADS_COSTS_SNAPSHOT_CONTRACT_2026-08-20.md`
* `sql/mart/ads_spend_stage3b_validation.sql`
