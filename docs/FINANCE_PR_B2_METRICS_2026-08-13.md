# PR-B2 · Финансовые метрики: честная бизнес-семантика

**Ред. 3 · 13.08.2026 · код написан, production НЕ тронут**
Ветка: `fix/finance-pr-b2-metrics-semantics`
Основание: REV2.4 (контракт v1, APPROVED) · PR #91 (ingestion) · PR #92 (semantic layer, merged)

Ред. 2 отличается от ред. 1 (той, на которую дан ACK) тремя измеренными уточнениями —
все перечислены в §9 «Отличия от ред. 1». Ни одно из них не меняет scope.

Ред. 3 закрывает два открытых вопроса ред. 2 и одну терминологическую правку —
§9.4–§9.6. Scope не меняется.

---

## 0. Scope

Ровно один долг: **слово `commission` нигде не должно означать `vw`.** Никаких новых
финансовых идей сверх согласованного.

| # | Изменение |
|---|---|
| B2-1 | `cost_category`: `commission` → `wb_reward` для трёх пар с `commission_amount` |
| B2-2 | Новая метрика `marketplace_fee_gap_rub` = `retail_price_withdisc_rub − for_pay` |
| B2-3 | `sp_build_mart_sku_daily`: переименование агрегата, новая статья расхода, формула hybrid-контрибуции, обновление guard и ASSERT |
| B2-4 | Фиксация контракта: `acquiring_rub` и `commission_native_rub` — декомпозиция спреда, не отдельные расходы |

**Вне PR-B2:** `REF_COGS`, сдвиг Scheduler, реклама, любые изменения ingestion,
`V_WB_FINANCE_CANONICAL`, инварианты PR-A, база `buyouts_rub` (см. §8).

---

## 1. Текущая семантика → целевая

### 1.1 Что означает `commission` сегодня

`REF_COST_MAP` содержит три пары с `cost_category = 'commission'`:

```
('Продажа',         'commission_amount', COST,       'commission', -1)
('Возврат',         'commission_amount', COST,       'commission', -1)
('Коррекция продаж','commission_amount', ADJUSTMENT, 'commission', -1)
```

`commission_amount` — это `ppvz_vw` ← поле API `vw` (`WbFinanceApiV1.gs:263`, карта
`Wbfinanceloader:137`), вознаграждение WB по операции. Комиссия маркетплейса отдельной
строкой отчёта **не приходит** — она внутри спреда `retail_price_withdisc_rub − for_pay`
вместе с эквайрингом.

### 1.2 Масштаб расхождения — измерено на production 13.08.2026

Срез витрины: `is_sku_row` × active universe × `finance_date <= 2026-08-12` —
ровно та база, на которой считает `sp_build_mart_sku_daily`.

```
месяц      wb_reward (vw)     marketplace_fee_gap      Δ = gap − vw
2024-09        17 924,61            61 232,55            +43 307,94
2024-10        66 760,51           235 482,57           +168 722,06
2024-11       114 296,18           414 186,42           +299 890,24
2024-12       140 935,55           603 891,60           +462 956,05
2025-01        52 863,71           283 853,45           +230 989,74
2025-02        72 585,94           299 451,55           +226 865,61
2025-03       134 589,63           518 690,20           +384 100,57
2025-04        55 088,24           216 984,68           +161 896,44
2025-05        50 380,23           169 020,93           +118 640,70
2025-06        26 857,50           144 675,85           +117 818,35
2025-07        14 346,83           151 301,59           +136 954,76
2025-08        38 982,19           286 025,74           +247 043,55
2025-09        60 888,11           295 442,26           +234 554,15
2025-10        48 390,61           302 831,76           +254 441,15
2025-11        52 420,56           352 538,34           +300 117,78
2025-12        85 755,04           815 424,14           +729 669,10
2026-01        26 995,74           328 860,58           +301 864,84
2026-02        26 735,10           376 575,77           +349 840,67
2026-03        61 106,98           641 090,86           +579 983,88
2026-04        44 162,52           427 964,05           +383 801,53
2026-05        61 003,25           404 159,65           +343 156,40
2026-06        55 138,47           280 193,28           +225 054,81
2026-07        28 691,62           231 439,10           +202 747,48
2026-08        27 244,73            69 842,90            +42 598,17
────────────────────────────────────────────────────────────────────
итого       1 364 143,84         7 911 159,82         +6 547 015,98
```

⚠️ Июль исправлен относительно ред. 1: было 231 146,54 / +202 454,92. См. §9.1.

### 1.3 Почему это не косметика

```
hybrid_day_contribution_pre_cogs = buyouts_rub − commission_cost_positive
                                 − logistics_cost_positive − ad_spend
```

`buyouts_rub` — валовая выручка выкупов, а вычитается из неё `vw`, а не реальный сбор WB.
Контрибуция завышена на величины столбца Δ. Соседняя метрика при этом уже корректна:
`settlement = finance_for_pay_accounting − ad_spend`, потому что `for_pay` уже нетто сбора.
Две метрики расходились ровно на `gap − vw`, и это расхождение ничем не помечено.

### 1.4 Целевая семантика

| Метрика | Смысл | Роль |
|---|---|---|
| `marketplace_fee_gap_rub` | `retail_price_withdisc_rub − for_pay` | **authoritative** сбор WB |
| `commission_native_rub` | `srev × commission_percent/100` | декомпозиция, справочно, со статусом |
| `acquiring_rub` | `acquiring_fee` | декомпозиция, **уже внутри gap** |
| `wb_reward_rub` | `vw` | вознаграждение WB по операции, отдельная сущность |

Тождество REV2.4 остаётся контрактом:
`marketplace_fee_gap_rub ≡ commission_native_rub + acquiring_rub + residual`.
На срезе `<= 2026-08-10`, «Продажа»: `7 896 898,01 = 7 297 697,73 + 444 751,92 + 154 448,36`.
С учётом одиннадцати строк «Возврат» тот же срез даёт `7 898 927,69` (+2 029,68) — см. §9.2.

---

## 2. Формулы и источники

```sql
-- B2-2, в V_WB_FINANCE_SEMANTIC §4.1 (добавление; существующие поля не тронуты)
IF(supplier_oper_name IN ('Продажа','Возврат'),
   ROUND(SAFE_CAST(REPLACE(retail_price_withdisc_rub,',','.') AS NUMERIC)
       - SAFE_CAST(REPLACE(for_pay,',','.') AS NUMERIC), 2),
   NULL) AS marketplace_fee_gap_rub
```

Источник — только источниковые поля WB, без справочников и без `commission_percent`,
поэтому метрика корректна и в окне аномалии 25.10–11.11.2024, где native-ставка недостоверна.

Проверено на всех 204 133 строках слоя: у 39 212 строк «Продажа»/«Возврат» обе части
непусты и парсятся (0 NULL), спред положителен на каждой строке.

---

## 3. Затрагиваемые объекты

| Объект | Что меняется |
|---|---|
| `sql/finance/v_wb_finance_semantic.sql` | +`marketplace_fee_gap_rub` (§4.1), перенумерация комментариев 4.x |
| `sql/mart/pr_mart1_facts.sql` | +колонка в `FACT_FINANCE`, +2 гейта (§4) |
| `sql/mart/pr_mart2a_finance_longform.sql` | seed: `cost_category` трёх пар `commission` → `wb_reward` |
| `sql/mart/pr_mart2b_sku_daily.sql` | пять точек, см. ниже |
| `sql/mart/pr_mart2b_sku_daily_validation.sql` | ред. 3: follow-up за переименованием, +сверка `marketplace_fee_rub` (§9.4) |
| `docs/FINANCE_PR_B_NORMALIZATION_2026-08-13.md` | восстановление документационного контракта PR-B (§9.5) |

Точки в `sp_build_mart_sku_daily`:

```
DECLARE           v_finance_commission_rows → v_finance_wb_reward_rows, +v_finance_fee_gap_rows
guard             cost_category='commission' → 'wb_reward', +третий guard на fee_gap
                  ⚠️ без правки guard процедура падает с RAISE на первом же прогоне
CTE fin           commission_cost_positive → wb_reward_cost_positive
CTE finpay        +marketplace_fee_rub = SUM(FACT_FINANCE.marketplace_fee_gap_rub)
SELECT            hybrid = buyouts_rub − marketplace_fee_rub − logistics_cost_positive − ad_spend
ASSERT            консервация 'commission' → 'wb_reward', +reconciliation marketplace_fee_rub
```

Колонки `MART_SKU_DAILY`: `commission_cost_positive` → `wb_reward_cost_positive`,
плюс новая `marketplace_fee_rub`. Переименование ломающее, но потребителей нет:
`grep` по `cloud/` даёт ноль вхождений, дашборд не построен.

---

## 4. Гейты, добавленные сверх ред. 1

Все четыре — применение уже действующих в контуре правил к новой метрике, не новая семантика.

| Гейт | Файл | Зачем |
|---|---|---|
| parse-QC на `retail_price_withdisc_rub` | `pr_mart1_facts.sql` | поле стало входом authoritative-метрики; `for_pay` уже был покрыт |
| ASSERT: `fee_gap IS NULL` на «Продажа»/«Возврат» = 0 | `pr_mart1_facts.sql` | `SUM` игнорирует NULL — молчаливый NULL занизил бы расход витрины |
| guard `v_finance_fee_gap_rows > 0` | `pr_mart2b_sku_daily.sql` | правило REV5: каждый вход контрибуции гейтится отдельно |
| reconciliation `marketplace_fee_rub` vs `FACT_FINANCE` | `pr_mart2b_sku_daily.sql` | метрика идёт мимо `REF_COST_MAP` и мимо леммы консервации PR-Mart2a §5.2 |

---

## 5. Инварианты

1. `marketplace_fee_gap_rub ≡ commission_native_rub + acquiring_rub + residual`, residual задокументирован REV2.4.
2. `SUM(wb_reward_cost_positive)` после = `SUM(commission_cost_positive)` до, помесячно, до копейки.
   Проверено на источнике: обе стороны дают `1 364 143,84 ₽`. Переименование не меняет величину.
3. `settlement_day_contribution_pre_cogs` не меняется вообще — формула не трогается.
4. `hybrid_day_contribution_pre_cogs` меняется ровно на `−(gap − vw)` помесячно, значения §1.2.
5. Эквайринг не вычитается вторым разом: `acquiring_rub` не входит ни в одну формулу контрибуции.
6. Ни одна цифра REV2.4 не сдвигается: регрессия читает канон, а не витрину.
7. **Сходимость двух баз.** В месяцах, где `FACT_SALES` полон (2026-05 … 2026-08),
   после PR-B2 выполняется `hybrid ≡ settlement − logistics` с точностью до рубля:

```
месяц    hybrid после   settlement − logistics    остаток   buyouts − srev
2026-05    461 032,13          461 542,42         −510,29      −510,29
2026-06    297 072,42          297 072,10           +0,32        +0,32
2026-07    200 792,79          201 632,60         −839,81      −839,81
2026-08     41 984,10           41 984,04           +0,06        +0,06
```

Остаток объясняется ровно расхождением баз `buyouts_rub` (Sales API) и `srev` (финотчёт),
до копейки. До PR-B2 две метрики расходились на необъяснённое `gap − vw`. Это и есть
содержательное доказательство корректности: две независимо построенные величины сошлись.

## 6. Regression gates

| Гейт | Ожидание |
|---|---|
| `pr_a_regression_check.sql` | 20/20 PASS |
| `pr_a_integrity_checks.sql` + A5d | 7/7 нулей |
| `pr_b_unknown_pairs_check.sql` | 0 |
| Legacy equivalence, 3 поля | 0 расхождений |
| `wb_reward` = прежний `commission`, помесячно | delta 0,00 ₽ |
| `hybrid` delta | ровно значения §1.2, ни рублём больше |
| `settlement` delta | 0,00 ₽ |
| `hybrid ≡ settlement − logistics` на 2026-05…08 | остаток = `buyouts − srev`, §5.7 |
| прочие метрики `MART_SKU_DAILY` | без изменений |

## 7. Rollout

1. `V_WB_FINANCE_SEMANTIC` + `marketplace_fee_gap_rub`.
2. Пере-деплой `pr_mart1_facts.sql` → `CALL sp_bootstrap_facts('')`.
3. Пере-деплой `pr_mart2a_finance_longform.sql` (seed с `wb_reward`).
4. Пере-деплой `pr_mart2b_sku_daily.sql` — **обязательно вместе с шагом 3**.
5. `CALL sp_build_mart_sku_daily(...)`.
6. Гейты §6.

Шаги 3 и 4 нельзя разнести по времени: между ними guard ищет несуществующую категорию,
и штатный прогон 07:00 упадёт с RAISE. Шаг 2 тоже нельзя отделить от 4 более чем на сутки:
до него `FACT_FINANCE` не имеет колонки `marketplace_fee_gap_rub`.

## 8. Найдено при реализации, НЕ чинится в PR-B2

**База `buyouts_rub` короче финансовой истории.** `FACT_SALES` начинается фактически
с 2026-04 (в марте — 741 ₽ на весь месяц), тогда как финансовый контур покрывает
с 2024-09. С 2026-05 `buyouts_rub ≈ srev` до рубля, раньше — ноль против полного расхода.

Следствие: `hybrid_day_contribution_pre_cogs` интерпретируем только с 2026-05.
До этой даты он и раньше был структурно бессмыслен, но PR-B2 увеличивает искажение
в этой зоне в ~5,8 раза (вычитаемое растёт с 1,36 млн до 7,91 млн ₽ на всей истории).

Это **не регрессия PR-B2** — метрика была нерабочей и до него, — но замалчивать нельзя.
В бэклог: либо ограничить `hybrid` окном, где `FACT_SALES` полон, либо строить его
на `srev` из финотчёта. Диагностические правила рекламы на `hybrid` до 2026-05 строить
нельзя. Решение — отдельным PR, вместе с `REF_COGS`.

## 9. Отличия от ред. 1

**9.1 Июльская цифра исправлена.** В ред. 1 `fee_gap` за 2026-07 указан как 231 146,54 ₽.
Измерение делалось только по «Продажа», тогда как согласованная формула покрывает
«Продажа» и «Возврат». Разница — одна строка возврата на 292,56 ₽. Верно: **231 439,10 ₽**,
дельта hybrid за июль **+202 747,48 ₽**. Остальные пять месяцев ред. 1 совпали до копейки:
в них SKU-строк «Возврат» нет.

**9.2 Поведение «Возврат» задокументировано.** 11 строк за всю историю, +2 029,68 ₽.
WB отдаёт по ним и `retail_price_withdisc_rub`, и `for_pay` положительными, спред тоже
положителен и трактуется как расход. Это согласовано с уже действующим правилом
`REF_COST_MAP` («Возврат» × `commission_amount` → COST) и материальности не имеет.

**9.3 Таблица §1.2 расширена** с шести месяцев до всех двадцати четырёх.

---

## 9А. Отличия ред. 3 от ред. 2 (решения аудитора)

**9.4 `pr_mart2b_sku_daily_validation.sql` обновлён до ред. 3, а не заморожен.**
Файл жёстко проверял `cost_category='commission'` и колонку `commission_cost_positive`.
После PR-B2 он стал бы заведомо красным при корректном production — плохое состояние
репозитория. Решение: обновить существующий файл, параллельный «замороженный» не заводить,
прежнюю ревизию хранит git. Изменения — четыре, все follow-up за `pr_mart2b_sku_daily.sql`:
`commission → wb_reward`, `commission_cost_positive → wb_reward_cost_positive`,
+счётчик `finance_fee_gap_rows` в §1, +дельта `d_marketplace_fee` в §5
(сверка `marketplace_fee_rub` ↔ `FACT_FINANCE.marketplace_fee_gap_rub`, зеркалит
одноимённый ASSERT процедуры). Новых проверок бизнес-смысла файл не приобретает.

**9.5 Спека PR-B остаётся в ветке.** `docs/FINANCE_PR_B_NORMALIZATION_2026-08-13.md`
при merge PR #92 в репозиторий не попала, при том что `v_wb_finance_semantic.sql`
на неё ссылается (`Спека: docs/FINANCE_PR_B_NORMALIZATION_2026-08-13.md`). Это dangling
documentation reference. Docs-only добавление её в PR-B2 — восстановление
документационного контракта, а не расширение бизнес-scope.

**9.6 Терминологическая правка в §4.1 `v_wb_finance_semantic.sql`.** Комментарий описывал
спред как «между тем, что заплатил покупатель, и тем, что WB отдал продавцу».
Это неверно относительно REV2.4: `retail_price_withdisc_rub` — цена продавца, база
расчёта WB, а не сумма, фактически уплаченная покупателем (разницу закрывает СПП,
которую платит WB). Формула не менялась. Новая формулировка: «спред между
`retail_price_withdisc_rub` (ценой продавца / базой расчёта WB) и `for_pay`
(суммой к перечислению продавцу)»; ниже «(выручка покупателя × выплата продавцу)» →
«(база реализации продавца × выплата продавцу)». Правка не косметическая: PR-B2 и делается
ради того, чтобы терминология перестала вводить в заблуждение.

---

## 10. Rollback

| Объект | Откат |
|---|---|
| `V_WB_FINANCE_SEMANTIC` | версия из merge-коммита PR #92 |
| `sp_bootstrap_facts` | `pr_mart1_facts.sql` из merge-коммита PR #92 |
| `REF_COST_MAP` + LONG-views | `pr_mart2a_finance_longform.sql` из merge-коммита PR #92 |
| `sp_build_mart_sku_daily` | `pr_mart2b_sku_daily.sql` из коммита `60e9fe9` |
| `FACT_*`, `MART_SKU_DAILY` | пересборка через `CALL` после отката процедур |

Порядок отката обратный: `pr_mart2b` → `pr_mart2a` → `pr_mart1` → вью → пересборка.
Контрольные значения после отката: `MART_SKU_DAILY` 7 084 строки,
`finance_for_pay_accounting` 18 818 598,41 ₽, `commission_cost_positive` 1 364 143,84 ₽,
`hybrid` итого −1 245 618,61 ₽, `settlement` итого 18 334 559,62 ₽.

## 11. Что PR-B2 сознательно НЕ делает

* Не трогает `commission_amount` как compatibility-поле — оно остаётся, с прежним содержимым.
* Не меняет `commission_native_status` и логику детектора размерности.
* Не вводит COGS и не трогает `REF_COST_MAP` вне трёх пар.
* Не касается ingestion, канона и инвариантов PR-A.
* Не чинит базу `buyouts_rub` (§8).
