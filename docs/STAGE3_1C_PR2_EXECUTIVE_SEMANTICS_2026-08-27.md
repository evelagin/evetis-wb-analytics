# STAGE 3.1C PR2 — Executive Financial Semantics Correction

**Дата:** 2026-08-27
**База:** HEAD `9ada18b8a7a85bf407450f2337d4e931644f65fc`
**Скрипты:** `sql/dash/pr_dash_finance_corrected_v1.sql`, `sql/dash/pr_dash_finance_corrected_validation.sql`
**Откат:** `tools/stage3_1c_pr2_finance_corrected_rollback.sh [--dry-run]`

---

## 1. Что было неправильно

Рекламный расход вычитался из результата **дважды**:

1. как **attributed advertising** внутри `contribution_pre_cogs_rub`
   (`MART_SKU_DAILY.ad_spend`, грейн день × nm_id);
2. повторно — внутри `account_level_total_rub`, потому что 92,5 % account-level
   операции WB «Удержание» является **рекламным биллингом** (доказано Stage 3.1C PR1).

Второй, независимый дефект: процент считался на **разных множествах суток** —
числитель `period_result_pre_cogs_rub` гейтится `contribution_covered ∧ finance_covered`
(25 суток на контрольном окне), а знаменатель карточка брала сырым
`sales_revenue_seller_base_rub` (26 суток). Процент занижался механически.

## 2. Что именно изменено

Один **overlay-view**, 1:1 к суточному грейну KPI-слоя. Исходные (BEFORE) величины
публикуются рядом с исправленными — ничего не переписывается «на месте».

```
account_level_total_corrected_rub    = account_level_total_rub - ad_billing_reconstructed_rub
period_result_pre_cogs_corrected_rub = contribution_pre_cogs_rub - account_level_total_corrected_rub
```

🔴 **OD-3.** Исключается **только** `AD_BILLING_RECONSTRUCTED`. `TRANSIT_DEDUCTION`,
`UNCLASSIFIED_DEDUCTION` и `CLASSIFICATION_CONFLICT` остаются расходом. uuid36-популяция
2025 года (794 732 ₽) не переклассифицирована и не исключена.

🔴 **D-1 (owner ACK).** Коррекция — **вычитание из предикатного итога**, а не пересборка
итога перечислением категорий: контракт `dashboard_contract_v2.sql` (CORRECTION-2)
намеренно определяет уровень счёта предикатом `NOT is_sku_row`, и перечисление молча
потеряло бы новую категорию WB. Эквивалентность двух форм проверяется ASSERT C-6 —
0 расхождений на всех 722 сутках.

🔴 **O-1 (owner ACK).** Ratio-полей в view **нет**. Хранятся согласованные
`revenue_base_period_result_rub` и `revenue_base_contribution_rub`; процент считает
потребитель как ratio-of-sums.

## 3. Затронутые production-объекты

| объект | действие |
| --- | --- |
| `wb_mart.V_DASH_FINANCE_CORRECTED_DAILY` | **создан** (VIEW, грейн `day`, 722 строки) |

Объектов `wb_mart`: **39 → 40**. `wb_raw` 56 и `evetis_ref` 4 — без изменений.

**Не изменено:** `V_DASH_KPI_DAILY`, `V_DASH_SKU_DAILY`, `V_DASH_COVERAGE_DAILY`,
`V_DASH_FRESHNESS_*`, `FACT_*`, `MART_SKU_DAILY`, `REF_COST_MAP`, `evetis_ref`,
`V_FACT_FINANCE_COGS`, `V_MART_SKU_DAILY_COGS`, `V_WB_DEDUCTIONS_CLASSIFIED`,
`V_ADVERTISING_RECONCILIATION_DAILY`, процедуры, Cloud Scheduler, Metabase.

## 4. BEFORE / AFTER, 01–26.08.2026

🔴 Снимок на 27.08.2026, а не вечный инвариант: `FACT_*` штатно обновляются.
В ASSERT эти числа **не зашиты** — валидация проверяет отношения, не значения.

| показатель | BEFORE | AFTER |
| --- | ---: | ---: |
| `contribution_pre_cogs_rub` | 104 989,07 | 104 989,07 |
| в т. ч. attributed advertising (уже вычтена) | 38 861,24 | 38 861,24 |
| `deduction_rub` (операция WB целиком) | 57 186,19 | 57 186,19 |
| — `ad_billing_reconstructed_rub` | внутри расхода | **исключён из расчёта**, 52 875,00 как контроль |
| — `transit_deduction_rub` | 2 596,45 | 2 596,45 расход |
| — `unclassified_deduction_rub` | 1 714,74 | 1 714,74 расход |
| — `classification_conflict_rub` | 0,00 | 0,00 |
| `other_wb_deductions_rub` | — | 4 311,19 |
| `storage_rub` / `penalty_account_rub` / `acceptance_rub` | 7 634,80 / 6 320,00 / 0,00 | без изменений |
| `reimbursement_account_rub` (кредит, знак сохранён) | −748,17 | −748,17 |
| `account_level_total_rub` | 70 392,82 | **17 517,82** |
| **`period_result_pre_cogs_rub`** | **34 596,25** | **87 471,25** |
| знаменатель маржи | 335 526,88 (26 сут.) | **324 257,88 (25 сут.)** |
| `period_result_pre_cogs_margin_pct` | 10,31 % (рассогласован) | **26,98 %** |
| `contribution_pre_cogs_margin_pct` | 31,29 % (рассогласован) | **32,38 %** |
| ad billed / attributed / unallocated | — | 52 875,00 / 38 861,24 / **+14 013,76** |

Lifetime-эффект для справки: `period_result` по 135 покрытым суткам
775 325,24 → 1 226 020,66 ₽.

## 5. Acceptance

| # | критерий | итог |
| --- | --- | --- |
| A1 | BEFORE воспроизводится | **PASS** (C-3: 0 расхождений с `V_DASH_KPI_DAILY` на 722 сутках) |
| A2 | ad billing 01–26.08 = 52 875,00 ₽ | **PASS** |
| A3 | AFTER `period_result` ≈ 87 471,25 ₽ | **PASS** 87 471,2505 ₽ |
| A4 | transit остаётся расходом | **PASS** 2 596,45 ₽ внутри corrected (C-12) |
| A5 | unclassified остаётся расходом | **PASS** 1 714,74 ₽ внутри corrected (C-12) |
| A6 | unknown/conflict не исчезают и не обнуляются | **PASS** (C-4, C-5, C-11) |
| A7 | реклама не вычитается дважды | **PASS** (C-7: дельта AFTER−BEFORE = ровно биллинг, 0 исключений) |
| A8 | Product COGS не участвует | **PASS** (C-13) |
| A9 | `FACT_*`, `MART_SKU_DAILY`, `evetis_ref`, `REF_COST_MAP` не изменены | **PASS** (C-14..C-16; MART 7 477, REF_COST_MAP 19, evetis_ref 4, wb_raw 56) |
| A10 | прочие KPI не изменились | **PASS** (`V_DASH_KPI_DAILY` 722 строки, `V_DASH_SKU_DAILY` 7 477 — как до PR2) |
| A11 | numerator/denominator alignment | **PASS** (C-8, C-9) |
| A12 | Metabase не переключён | **PASS** (C-17: ни один объект не читает новый слой) |

## 6. Что остаётся нерешённым

* **uuid36-популяция 2025 года, 794 732 ₽** — по OD-4 остаётся `UNCLASSIFIED_DEDUCTION`
  и остаётся расходом. Природа устанавливается только детализацией кабинета WB.
* **175 суток с удержаниями на 4 165 983,50 ₽ лежат вне eligible-окна** — в
  `period_result` они не входили и не входят. Это состояние покрытия истории, а не
  эффект PR2; PR2 его не создаёт и не маскирует.
* **Карточка 63 «Доходность до себестоимости, % (SKU)»** имеет однотипный
  coverage-перекос на слое SKU Performance. В PR2 не трогается: SKU Performance вне
  области этого PR.
* **Product COGS к Executive не подключён** — следующий этап.

## 7. Metabase

Требуется **отдельный ACK**. PR2 отдаёт только semantic layer; переключение карточек
**51** «Структура затрат за период», **53** «Результат периода до себестоимости»,
**54** «Рентабельность до себестоимости» на `V_DASH_FINANCE_CORRECTED_DAILY` — отдельный шаг.
Проценты в карточках считаются ratio-of-sums по выровненному знаменателю:

```sql
SAFE_DIVIDE(SUM(period_result_pre_cogs_corrected_rub), SUM(revenue_base_period_result_rub))
```

## 8. Deviations

* **D-1** — предикатная форма коррекции вместо перечислительной (owner ACK, C-6).
* **D-2** — `ad_billing_reconstructed_rub` (маска `finance_covered`, статья расчёта) и
  `ad_spend_billed_rub` (маска рекламной сверки) публикуются как **два отдельных поля**
  при одинаковом значении внутри финансового покрытия. Причина: блоки живут по разным
  гейтам, и склейка их в одно поле сделала бы покрытие невидимым.
