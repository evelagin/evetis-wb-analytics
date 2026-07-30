# PR-Mart2a — Finance long-form + REF_COST_MAP (REV2, аудит PR#80)

**Дата:** 2026-07-30  **Статус:** REV2 по REQUEST CHANGES (PR#80). **Read-only dry-run прошёл на проде;
ожидает merge и production apply.** PR открыт — на проде объекты НЕ созданы.
**Контракты:** `docs/MART_MART2_CONTRACTS_2026-07-28.md` (§3 REF, §KPI). **SQL:** `sql/mart/pr_mart2a_finance_longform.sql`.
**Валидация:** `sql/mart/pr_mart2a_finance_longform_validation.sql`.

## Правки REV2 по замечаниям аудитора
1. **FAIL-CLOSED (#1).** Перестроено на `REF_COST_MAP__BUILD → ASSERT (seed + live) → PUBLISH REF → создать VIEW`.
   Целевые объекты (REF_COST_MAP, 2 VIEW) не создаются, пока не пройдут ВСЕ гейты. Промежуточные —
   staging-таблица `REF_COST_MAP__BUILD` и `TEMP _long/_mapped` (не контрактные; при падении ASSERT публикации нет).
2. **Conservation guard (#2).** Явный spine всех **9** `amount_field` + `LEFT JOIN` + `COALESCE(sum,0)`. Поле без
   ненулевых LONG-строк (`other_amount`) больше не выпадает: сверяется `COALESCE(long,0)` против `COALESCE(fact,0)`.
3. **Domain guard (#3).** Явные `IS NULL` для `economic_direction` / `field_normalization_sign` / `cost_category`
   (three-valued logic больше не пропускает NULL). Добавлена **точная проверка формулы** `cost_amount_positive`
   во ВСЕХ ТРЁХ режимах: `COST→ABS(s)`, `CREDIT→−ABS(s)`, `ADJUSTMENT→s×field_sign`, плюс запрет NULL cp для money-строк.
4. **Документация (#4).** Формулировка «применён на прод» убрана: PR открыт → «dry-run прошёл; ожидает merge и apply».
   (В прошлой сессии объекты были преждевременно созданы на проде — **откачены DROP-ом 30.07**, прод чист.)

## Что публикуется (после merge; 3 read-model объекта, обратимо DROP)
- **`REF_COST_MAP`** — seed, 22 пары `operation×field` → direction (COST/CREDIT/ADJUSTMENT), 10 категорий,
  `field_normalization_sign` (commission=−1, прочие=+1). Cluster op_key/amount_field.
- **`V_WB_FINANCE_AMOUNTS_LONG`** — UNPIVOT 9 полей; нули/NULL отброшены; op_key + is_sku_row. compensation — guard.
- **`V_WB_FINANCE_AMOUNTS_LONG_MAPPED`** — LONG ⨝ REF → direction/category/cost_amount_positive (UNKNOWN→NULL).

## Гейт (fail-closed)
Seed: (op_key×field) уникален; op_key/field/category NOT NULL/empty; direction ∈ домен (NULL-safe);
sign ∈ {−1,1} (NULL-safe). Live: §5.1 compensation=0; §5.2 консервация 9/9 полей (spine+COALESCE);
§5.3 unknown-pairs=0; §5.4a формула во всех 3 режимах + нет NULL cp; §5.4b SKU+ACCOUNT==total.

## Read-only dry-run (30.07, инлайн-реплика, ничего не создано)
`fields_checked=9`, `conservation_bad=0`, `domain_bad=0`, `norm_bad=0`, `unknown_rows=0`, `comp_nonzero=0`;
`Σ source_signed=6 989 474.30`; `total cost_positive=9 479 676.06`; **SKU 4 243 822.66 + ACCOUNT 5 235 853.40**.

## Заметки для аудита
- Точные op-строки взяты из данных: «Возмещение издержек по перевозке/по складским операциям с товаром» (CREDIT→reimbursement_logistics),
  «Возмещение за выдачу и возврат товаров на ПВЗ» (CREDIT→reimbursement_pvz).
- `acquiring_fee` на «Продажа» = 433 982 ₽ — per-SKU COST. Роутинг SKU vs ACCOUNT — по is_sku_row, не по категории.
- Суммы по-полю дрейфуют посуточно; инварианты (консервация 9/9, unknown=0, формула) держатся.

## Apply после merge
Прогнать `pr_mart2a_finance_longform.sql` целиком (fail-closed) → затем `..._validation.sql` §3-§6.
