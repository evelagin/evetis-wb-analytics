# PR-Mart2b — Finance long-form + MART_ACCOUNT_DAILY + MART_ADS_RECON_DAILY (дизайн rev5, после аудита #4)

**Дата:** 2026-07-28  **Статус:** ЧЕРНОВИК на аудит #5. Все пробы — read-only live 28.07.
Аудит #4 закрыт: unattributed_ads вынесен в MART_ADS_RECON_DAILY (ACCOUNT — только finance); other_unmapped/UNKNOWN
порог строго 0; compensation guard = SUM(ABS)=0.

## 0. Правки аудита #3 (закрыты)
- #1 глобального field_cost_sign мало → направление по **(operation_type_normalized × amount_field)**, operation-specific > field-default (§3).
- #3 разделить reconciliation: finance-консервация и ads-консервация раздельно; synthetic unattributed_ads ВНЕ finance (§6).
- economic_direction — детерминированный mapping + в grain (§3, §5).
- compensation_amount — **эмпирически = 0 во всех строках** → исключён из cost-полей + guard Σ=0 (§2).

## 1. Принцип
Финансовые деньги — в long-form (одна строка = одно денежное поле одной finance-строки). Направление и категория —
из mapping по (операция × поле). `source_signed_amount` авторитетен для консервации; `cost_amount_positive` —
нормализованное представление.

## 2. `V_WB_FINANCE_AMOUNTS_LONG` (foundation SKU+ACCOUNT)
UNPIVOT денежных cost-полей `FACT_FINANCE` → `(finance_row_key, finance_date, nm_id, sku_match_status,
operation_type_normalized, amount_field, source_signed_amount)`. Список cost-полей (БЕЗ revenue/settlement
for_pay/sale_amount/retail_*): `commission_amount, logistics_amount, storage_fee, deduction, penalty, acceptance,
acquiring_fee, additional_payment, other_amount`. **`compensation_amount` ИСКЛЮЧЁН** (эмпирически 0 строк с ненулевым значением) —
+ guard `ASSERT SUM(ABS(compensation_amount)) = 0` (fail-closed; ABS чтобы знаки не сокращались). Строки с полем=0/NULL не берём.
**Лемма консервации:** ∀ field `Σ source_signed(long) == Σ field(FACT_FINANCE)` — ASSERT на BUILD.

## 3. Направление по (operation × amount_field) → economic_direction + normalization_rule
Эмпирика (live 28.07): `commission_amount` = **COST** на «Продажа/Коррекция продаж/Возврат» (Σ −888 653; отриц.=затрата),
но **CREDIT** на «Возмещение*» (Σ −309 009 / −127 892; отриц.=возмещение WB). Один и тот же знак источника → разный
экономический смысл → направление задаётся ОПЕРАЦИЕЙ, не полем. `logistics_amount` — COST (полож.), `storage_fee/
deduction/penalty/acceptance/acquiring_fee/additional_payment/other_amount` — COST (полож.).

`REF_COST_MAP(operation_type_normalized, amount_field) → (economic_direction, cost_category)`:
- direction ∈ {COST, CREDIT} (**operation-specific имеет приоритет над field-default**);
- нормализация: **COST → +ABS(source), CREDIT → −ABS(source)** → `cost_amount_positive`;
- `source_signed_amount` — сырой WB-знак (для консервации). Обе величины хранятся.
Неизвестная (operation×field) → direction=`UNKNOWN`, cost_category=`other_unmapped` + fail-closed guard (§6).

| operation × commission_amount | direction | cost_category (SKU/ACCOUNT) |
|---|---|---|
| Продажа / Коррекция продаж / Возврат | COST | SKU commission |
| Возмещение перевозки/склад | CREDIT | ACCOUNT reimbursement_logistics |
| Возмещение ПВЗ | CREDIT | ACCOUNT reimbursement_pvz |
| storage_fee: Хранение | COST | ACCOUNT storage |
| deduction: Удержание | COST | ACCOUNT deduction |
| penalty: Штраф | COST | ACCOUNT penalty |
| acceptance: Платная приемка/Пересчет | COST | ACCOUNT acceptance |
| logistics_amount: Логистика | COST | ACCOUNT logistics_account |

## 4. Категория и версионирование
`cost_category` — стабильная укрупнённая, отдельно от сырой `operation_type_normalized` (храним оба). Map сидируется SQL
в `wb_mart.REF_COST_MAP`; позже переносится/версионируется через REF Sync.

## 5. MART_ACCOUNT_DAILY (ТОЛЬКО finance) и отдельный MART_ADS_RECON_DAILY (аудит #4.5)
**MART_ACCOUNT_DAILY** — только финансовые нетоварные суммы. Грейн: **day × cost_category × operation_type_normalized
× amount_field × economic_direction** (полный след; economic_direction в grain, детерминированно из map). Источник —
`V_WB_FINANCE_AMOUNTS_LONG` где non-SKU (`nm_id=0 OR sku_match_status<>'matched'`). Колонки: + `source_signed_amount` (Σ),
`cost_amount_positive` (Σ), rows_cnt, mart_run_id, built_at. **unattributed_ads СЮДА НЕ входит** — смешение провенансов запрещено.

**MART_ADS_RECON_DAILY** (отдельная витрина, day-грейн): `actual_spend` (=Σ FACT_ADS_COSTS.actual_spend),
`stats_spend_attributed` (=Σ FACT_ADS_SKU.stats_spend), `unattributed_ads = actual_spend − stats_spend_attributed`
(signed), + rolling-7d. Здесь же ads-консервация (§6.2). Никакого finance.

## 6. Reconciliation — РАЗДЕЛЁН (аудит #3.3)
1. **Finance-консервация:** `Σ SKU_DAILY finance-cost + Σ MART_ACCOUNT_DAILY == Σ cost-полей FACT_FINANCE`
   (по `source_signed_amount`, единый список полей §2). Long-form лемма гарантирует поблочно. (ACCOUNT — только finance.)
2. **Ads-консервация (в MART_ADS_RECON_DAILY, ОТДЕЛЬНО):** `stats_spend_attributed + unattributed_ads == actual_spend`
   по дню (= `Σ FACT_ADS_SKU.stats_spend + (actual−stats) == Σ FACT_ADS_COSTS.actual_spend`). Никакого finance.
3. **other_unmapped / UNKNOWN guard:** для v1 порог **строго 0** (аудит #4) — любая неизвестная (operation×field) с деньгами
   → FAIL (нельзя молча терять/сваливать). Появилась новая операция → сначала расширить REF_COST_MAP.
4. **compensation guard:** `SUM(ABS(compensation_amount)) = 0` (не Σ, чтобы знаки не сокращались). 5. Стандартные: BUILD>0, дедуп, not-null, IS.

## 7. Открытые вопросы аудитору
- Q1: подтвердить REF_COST_MAP по (operation×field) §3 (особенно commission COST vs CREDIT).
- Q2: глубина грейна ACCOUNT до amount_field (полный след) — ок?
- Q3: «Удержание» (4,58 млн) под-разрез по supplier_oper_name — в v1 или позже?
- Q4: порог other_unmapped-guard; момент переноса map в REF.
