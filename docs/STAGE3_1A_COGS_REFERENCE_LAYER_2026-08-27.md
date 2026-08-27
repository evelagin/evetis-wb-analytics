# STAGE 3.1A — Product COGS Reference Layer (`evetis_ref`)

**Дата:** 2026-08-27
**База:** HEAD `7bf0472159089c48da622dd1bdcdadcea3e8cf60`
**Утверждение контракта:** Stage 3.0.3B, `COGS_CONTRACT_PREIMPLEMENTATION_VALIDATION = PASS`
**Скрипты:** `sql/ref/pr_ref_cogs_history.sql` (сборка + ASSERT), `sql/ref/pr_ref_cogs_validation.sql` (acceptance)
**Откат:** `tools/stage3_1a_cogs_rollback.sh [--dry-run]`

---

## 1. Что это

Независимый справочный слой исторической **Product COGS** бренда EVETIS.

**Product COGS** = закупка + расходы в Китае + таможня + доставка **до** фулфилмента + иные прямо относимые расходы до FF.

**НЕ входят:** хранение ФФ · сборка наборов · разборка · маркировка и стикеровка · отгрузка ФФ · доставка ФФ → склад маркетплейса · расходы кабинета WB (хранение, удержания, штрафы, приёмка) · зарплата, аренда, банк, IT · налог.

## 2. Что это НЕ

Слой **не подключён** к `MART_SKU_DAILY`, `V_DASH_*`, `FACT_*`, Metabase. Подключение к дашбордам — отдельный этап с отдельным ACK владельца, потому что оно меняет числа на живых экранах.

## 3. Marketplace-independence

Master key — **`internal_sku`**. Идентификаторов канала (`wb_nm_id`, `nm_id`, `channel_sku_id`, `marketplace`) в слое **нет**; их отсутствие проверяется ASSERT B-5.

`V_PRODUCT_COGS_EFFECTIVE` зависит **только** от объектов внутри `evetis_ref`. Канальные идентификаторы резолвятся снаружи, по `internal_sku`. При появлении Ozon, Золотого Яблока, Яндекс Маркета и офлайн-розницы создаётся отдельный `REF_SKU_CHANNEL_MAP` — таблицы себестоимости при этом не меняются.

Проверка покрытия событий WB-специфична (читает `wb_raw`) и потому живёт в `sql/ref/pr_ref_cogs_validation.sql`, а не вью внутри `evetis_ref`. Решение владельца, ACK GATE A.

### Зафиксированное ограничение (дословно)

> Stage 3.0.3 historical reconstruction is optimized for the WB analytics pipeline. Inventory movements through Ozon, L'Etoile and other channels are not reconstructed in this stage. Current Product COGS remains valid because it is product-level; historical effective-date boundaries remain management-grade estimates where cross-channel movements could matter.

Утверждение «WB был единственным каналом продаж» в контракте отсутствует и считается отозванным.

---

## 4. Объекты

| объект | тип | содержимое |
| --- | --- | --- |
| `evetis_ref.REF_SKU_COGS_HISTORY` | TABLE | 17 строк: 14 `PURCHASE_BATCH` + 2 `INVENTORY_TRANSFORMATION` + 1 `IMPORTED_FINISHED_SET` |
| `evetis_ref.REF_BUNDLE_COMPONENTS` | TABLE | 33 строки состава / 14 наборов, без стоимостных полей |
| `evetis_ref.V_BUNDLE_COGS_DERIVED` | VIEW | 21 производный интервал (interval intersection) |
| `evetis_ref.V_PRODUCT_COGS_EFFECTIVE` | VIEW | unified resolver, 38 интервалов = 17 + 21 |

Датасет: location `EU`, как у `wb_raw`, `wb_mart`, `evetis_communications`.

## 5. Logical row keys

BigQuery не обеспечивает `PRIMARY KEY` физически. Поэтому `cogs_history_id` (`COGS-001`…`COGS-017`) и `bundle_component_id` (`BC-01`…`BC-33`) названы **logical row keys**, а их непустота и уникальность гарантируются ASSERT A-1…A-6, а не декларацией. Идентификаторы присвоенные и неизменяемые; при корректировке границы меняются атрибуты строки, а не её личность.

Uniqueness invariants: `(internal_sku, effective_from)` и `(bundle_internal_sku, component_internal_sku, effective_from)`.

## 6. Boundary lineage

Каждая forensic-граница `T` — одно измеренное окно `[W_from, W_to]` и одна management-дата `B` внутри него. Раскладывается на два конца смежных интервалов:

```
поздняя строка:  effective_from       = B
                 effective_from_window = [W_from, W_to]
ранняя строка:   effective_to         = B − 1 день
                 effective_to_window  = [W_from − 1, W_to − 1]
```

Сдвиг на день — следствие того, что `effective_to` **включительная**. Связь двух концов держат `effective_from_transition_id` / `effective_to_transition_id`; согласованность проверяет ASSERT C-6.

Флаги `effective_from_is_reconstructed` и `effective_to_is_reconstructed` независимы; `is_reconstructed` — их OR (ASSERT C-5). Это разделение существует потому, что **величина** может быть подтверждена владельцем, а **дата** — нет: пример COGS-006 (429 ₽ owner-confirmed, дата окончания импортного режима реконструирована).

### Шесть forensic-границ

| id | SKU | переход | окно | management boundary |
| --- | --- | --- | --- | --- |
| T1 | HAND | 219 → 214,50 | 2025-01-21 … 2025-01-30 | 2025-01-26 |
| T2 | HAND | 214,50 → 240 | 2025-03-03 … 2025-03-21 | 2025-03-10 |
| T3 | BODY | 219 → 214,50 | 2025-09-28 … 2025-11-27 | 2025-11-01 |
| T4 | SET-HAND-BODY | IMPORTED → FF_ASSEMBLED | 2025-04-07 … 2025-06-05 | 2025-05-01 |
| T5 | FS-MOIST | 165 → 133 | 2026-02-01 … 2026-04-01 | 2026-03-01 |
| T6 | FS-ACNE | 165 → 138 | 2026-04-01 … 2026-09-01 | 2026-08-01 |

🔴 **T6 — `MANAGEMENT_RECONSTRUCTED_BOUNDARY`, а не физическая дата перехода партии.** Forensic доказал одновременное существование старой и новой партии после 2026-08-01. Модель v1 сознательно аппроксимирует mixed-batch consumption. Полный текст — в поле `notes` строк COGS-011 и COGS-012.

## 7. Правило вычисления набора

```
bundle_product_cogs(bundle, d) =
    Σ  component_qty × product_cogs_rub(component, d)
    по строкам REF_BUNDLE_COMPONENTS, действующим на дату d

если хотя бы один компонент не разрешается ровно в один интервал на дату d
    → результат NULL (fail-closed), никогда 0, частичная сумма запрещена
```

Assembly cost в это выражение **не входит** и относится к будущему слою фулфилмента.

Исключение — `EVT-SET-HAND-BODY` в период 2024-09-10 … 2025-04-30: импортный готовый набор является самостоятельной неделимой товарной единицей (429 ₽) и суммой компонентов не заменяется. Взаимоисключение режимов проверяет ASSERT D-3.

## 8. Инварианты

| блок | что проверяет |
| --- | --- |
| A-1…A-8 | logical row keys: непустота, уникальность, uniqueness invariants, число строк 17 / 33 / 14 |
| B-1…B-6 | значения > 0, допустимый `cogs_origin_type`, порядок дат, отсутствие канальных и стоимостных колонок |
| R4a…R4c | сентинел `9999-12-31` не попадает в данные и не протекает через resolver |
| C-1…C-7 | boundary lineage: окна содержат свои значения, отсутствуют где не нужны, согласованы между смежными строками |
| D-1…D-4 | отсутствие пересечений интервалов и определений, взаимоисключение режимов, существование компонентов |
| E-4, R5a…R5c | resolver без NULL-стоимостей, 21 derived, 14 наборов, 38 интервалов |
| E-1…E-3 | event coverage (в файле валидации): ровно одно разрешение на событие |

🔴 **Инвариант непрерывного календарного покрытия НЕ вводится.** Даты без событий вне интервалов допустимы. `UNKNOWN COGS ≠ 0`.

### Спроектированный разрыв покрытия — один

`EVT-HC-BODY-300` не имеет интервала после 2026-04-26: SKU выбыл, остаток на WB = 0, в остатках фулфилмента позиция отсутствует, событий после этой даты нет. Следствием того же правила `EVT-SET-HAND-BODY` теряет производный интервал после 2026-04-26 — сработал fail-closed по построению. Будущая продажа любого из двух получит NULL, а не подставленную цену несуществующей партии.

## 9. Fail-closed семантика потребителя

| правило | формулировка |
| --- | --- |
| F-1 | 0 или >1 совпадений → `cogs_rub = NULL` и все производные after-COGS метрики этого грейна = NULL. Никогда 0, никогда «последняя известная» |
| F-2 | Не разрешается хотя бы один компонент набора → `bundle_product_cogs = NULL` целиком |
| F-3 | `ff_cost_rub` никогда не складывается с `product_cogs_rub` неявно |
| F-4 | `legacy_cogs_rub` не участвует ни в одном выражении расчёта — только audit lineage |

## 10. Известные дефекты справочников Google Sheets (НЕ исправляются)

| # | дефект |
| --- | --- |
| DQ-1 | `909951444`: `BUNDLES` = 474, `COST_HISTORY`/`SKU_MASTER` = 497 |
| DQ-2 | `910330849`: `BUNDLES` = 355, `COST_HISTORY` = 352 |
| DQ-3 | `1083392113` отсутствует в `BUNDLES`; состав взят из `SKU_MASTER` |
| DQ-4 | `bundle_build_cost` расходится между `BUNDLES` и `SKU_MASTER` |
| DQ-5 | `cogs_valid_from` в `SKU_MASTER` пуст у всех 25 SKU |
| DQ-6 | форматы `valid_from` в `COST_HISTORY` неоднородны |
| DQ-7 | `909951444` активен, но ни разу не отгружался на WB |

Ни один дефект на контракт не влияет: `bundle_total_cost` и `bundle_build_cost` source of truth не являются.

## 11. Runbook

```bash
# сборка (идемпотентна)
bq --project_id=project-fa311fc0-4d87-4781-986 query --nouse_legacy_sql < sql/ref/pr_ref_cogs_history.sql

# acceptance (read-only)
bq --project_id=project-fa311fc0-4d87-4781-986 query --nouse_legacy_sql < sql/ref/pr_ref_cogs_validation.sql

# откат
bash tools/stage3_1a_cogs_rollback.sh --dry-run
bash tools/stage3_1a_cogs_rollback.sh
```

## 12. Финансовая чувствительность (справочно, Stage 3.0.3)

Полный разброс исторической Product COGS между краями всех реконструированных окон — **57 822 … 70 122 ₽** на **8 839 646 ₽**, то есть **0,65–0,79 %**; в процентных пунктах валовой величины — 0,30–0,37 п.п. Вклад: FS-MOIST 27 168 ₽, FS-ACNE 21 924 ₽, HAND 17 100 ₽, SET 2 130 ₽, BODY 1 800 ₽, остальные SKU — ноль.
