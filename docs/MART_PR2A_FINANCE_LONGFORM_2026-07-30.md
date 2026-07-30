# PR-Mart2a — Finance long-form + REF_COST_MAP

**Дата:** 2026-07-30  **Статус:** APPROVE (ChatGPT). Применён на прод, все гейты пройдены, live-сверка ОК.
**Контракты:** `docs/MART_MART2_CONTRACTS_2026-07-28.md` (§3 REF, §KPI). **SQL:** `sql/mart/pr_mart2a_finance_longform.sql`.
**Валидация:** `sql/mart/pr_mart2a_finance_longform_validation.sql`.

## Зачем
Витрины Mart2 (SKU_DAILY / ACCOUNT_DAILY) должны видеть расходы по операциям в нормализованном виде.
`FACT_FINANCE` хранит 9 денежных полей «широко» и со знаком, зависящим от операции (комиссия COST на «Продажа»,
CREDIT на «Возмещение*»). PR-Mart2a разворачивает это в длинную форму и приклеивает direction/category, сохраняя
консервацию (`source_signed_amount` авторитетен; `cost_amount_positive` — нормализованное представление).

## Что создаётся (3 объекта в `wb_mart`, read-model, обратимо DROP)
1. **`REF_COST_MAP`** — seed-таблица, 22 пары `operation_type_normalized × amount_field` → `economic_direction`
   (COST/CREDIT/ADJUSTMENT), `cost_category` (10 категорий), `field_normalization_sign` (commission=−1, прочие=+1),
   `note`, `seeded_at`. Cluster `op_key, amount_field`. Точные op-строки взяты ИЗ ДАННЫХ.
2. **`V_WB_FINANCE_AMOUNTS_LONG`** — source-faithful UNPIVOT 9 полей (commission_amount, logistics_amount, storage_fee,
   deduction, penalty, acceptance, acquiring_fee, additional_payment, other_amount). Нули/NULL отброшены.
   Несёт `op_key=COALESCE(operation_type_normalized,'__NULL__')` и `is_sku_row=COALESCE(nm_id>0 AND status='matched', FALSE)`.
   `compensation_amount` НЕ разворачивается (денег не несёт — §5.1 guard).
3. **`V_WB_FINANCE_AMOUNTS_LONG_MAPPED`** — LONG ⨝ REF → `economic_direction`, `cost_category`, `cost_amount_positive`
   (COST→+ABS; CREDIT→−ABS; ADJUSTMENT→source×field_sign; UNKNOWN→NULL, в суммы не входит).

## Что НЕ трогается
`FACT_*`, загрузчики, триггеры — без изменений. Потребители подключаются с Mart2b (SKU_DAILY) и далее.

## Гейт (fail-closed, в DDL)
- §5.1 compensation guard: `COUNTIF(compensation_amount<>0)=0` (NULL-safe).
- §5.2 лемма консервации по-полю: `Σ source_signed` (LONG) == `Σ поля` (FACT), порог 0.005.
- §5.3 unknown money-pairs = 0 (каждая денежная пара обязана быть в REF).
- §5.4 нормализация: COST≥0, CREDIT≤0; и `SKU + ACCOUNT == total`.
- §5.5 REF уникален по (op_key×field); домены direction/sign.

## Приёмка на проде (30.07)
`ref_pairs=22`, `categories=10`, `long_rows=240 292`, грейн (finance_row_key×field) уникален;
`unknown_pairs=0`; `Σ source_signed=6 989 474.30`; `Σ cost_positive=9 479 676.06`;
**`SKU 4 243 822.66 + ACCOUNT 5 235 853.40 = 9 479 676.06`** (потерь нет); `comp_nonzero=0`.
Все 7 ASSERT-гейтов прошли при применении.

## Заметки для аудита
- Точные op-строки: «Возмещение издержек по перевозке/по складским операциям с товаром» (CREDIT→reimbursement_logistics),
  «Возмещение за выдачу и возврат товаров на ПВЗ» (CREDIT→reimbursement_pvz).
- `acquiring_fee` на «Продажа» = 433 982 ₽ — per-SKU COST (в SKU-costs).
- Роутинг SKU vs ACCOUNT — по `is_sku_row` (nm-match), НЕ по категории.
- Суммы по-полю дрейфуют посуточно (finance растёт); инварианты (консервация, unknown=0) держатся.
