-- ============================================================================
-- PR-Mart2a — EVETIS WB Analytics MART. Finance long-form + REF_COST_MAP. REV2 (аудит PR#80).
-- Дата: 2026-07-30.  Контракты: docs/MART_MART2_CONTRACTS_2026-07-28.md (§3 REF, §KPI).
-- PR-нота: docs/MART_PR2A_FINANCE_LONGFORM_2026-07-30.md.
--
-- REV2 по замечаниям аудитора (REQUEST CHANGES, PR#80):
--   #1 FAIL-CLOSED: REF_COST_MAP__BUILD → ASSERT seed + live contracts → PUBLISH REF →
--      создать финальные VIEW. Ничего в целевые объекты не публикуется до прохождения ВСЕХ гейтов
--      (staging __BUILD + TEMP _long — не контрактные объекты; при падении ASSERT публикация не происходит).
--   #2 Conservation guard: явный spine всех 9 amount_field + LEFT JOIN + COALESCE(sum,0) —
--      поля без ненулевых LONG-строк (other_amount) больше НЕ выпадают из проверки.
--   #3 Domain guard NULL-safe (явные IS NULL для direction/sign/category) + точная проверка формулы
--      cost_amount_positive во ВСЕХ ТРЁХ режимах (COST/CREDIT/ADJUSTMENT), не только знаки.
--
-- Назначение: развернуть FACT_FINANCE 9 денежных полей в длинную форму (1 строка = 1 ненулевое
--   значение), приклеить нормализованное direction/category из REF_COST_MAP, дать cost_amount_positive
--   БЕЗ потери консервации (source_signed_amount авторитетен; cost_amount_positive — представление).
--
-- Что публикуется (3 объекта в wb_mart, read-model, обратимо DROP):
--   REF_COST_MAP (seed), V_WB_FINANCE_AMOUNTS_LONG (unpivot), V_WB_FINANCE_AMOUNTS_LONG_MAPPED (⨝REF).
-- Ничего существующего (FACT_*, загрузчики) не меняется.
--
-- Durable-контракты: op_key=COALESCE(operation_type_normalized,'__NULL__');
--   is_sku_row=COALESCE(nm_id>0 AND sku_match_status='matched', FALSE);
--   деньги FACT_FINANCE уже NUMERIC (без re-parse); 9 unpivot-полей (commission_amount, logistics_amount,
--   storage_fee, deduction, penalty, acceptance, acquiring_fee, additional_payment, other_amount);
--   compensation_amount НЕ разворачивается (отдельный guard);
--   field_normalization_sign: amount_field='commission_amount' → −1, прочие поля → +1
--     (речь о ПОЛЕ commission_amount, не о cost_category — с PR-B2 категория этих пар 'wb_reward');
--     🔴 ИСКЛЮЧЕНИЕ Stage 1.5 (26.08.2026): пара Продажа/commission_amount переведена
--     в ADJUSTMENT со знаком +1 — она знакопеременная, и +1 знак СОХРАНЯЕТ. Правило выше
--     остаётся верным для остальных пар этого поля;
--   cost_amount_positive: COST→+ABS; CREDIT→−ABS; ADJUSTMENT→source×field_sign.
--
-- ⚠️ Read-only dry-run исправленных гейтов пройден на проде 30.07 (все счётчики 0; 9/9 полей).
--    Объекты создаются ТОЛЬКО после merge PR#80 (production apply владельцем/оркестратором).
-- ============================================================================

CREATE SCHEMA IF NOT EXISTS `wb_mart` OPTIONS (location = 'EU');

-- ── 1. STAGING: REF_COST_MAP__BUILD (seed 21 пара; точные op-строки ИЗ ДАННЫХ) ──
--    Stage ADS-1A (06.09.2026): 19 → 21. Добавлены ДВА переименования WB, вступившие
--    в силу 01.09.2026: «Логистика» → «Доставка» и «Возмещение издержек по перевозке/по
--    складским операциям с товаром» → «Возмещение издержек по перемещению и операционной
--    обработке товара». Старые пары НЕ удалены — они несут историю до 31.08.2026
--    включительно и обязаны продолжать разноситься. Новые пары не имеют ни одной строки
--    до 01.09.2026 (проверено: rows_before_sep = 0 у обеих), поэтому историческая
--    семантика измениться не может по построению.
--    PR-B2 (13.08.2026): cost_category трёх пар с amount_field='commission_amount'
--    переименован 'commission' → 'wb_reward'. Причина: поле commission_amount
--    несёт ppvz_vw ← API `vw` — вознаграждение WB по операции, а НЕ комиссию
--    маркетплейса. Комиссия маркетплейса отдельной строкой отчёта не приходит:
--    она внутри спреда retail_price_withdisc_rub − for_pay вместе с эквайрингом
--    (V_WB_FINANCE_SEMANTIC §4.1 marketplace_fee_gap_rub). Слово `commission`
--    после PR-B2 не означает `vw` нигде в контуре.
--    ⚠️ ПЕРЕИМЕНОВАНИЕ АТОМАРНО С sql/mart/pr_mart2b_sku_daily.sql: guard
--    v_finance_wb_reward_rows и ASSERT консервации там ищут именно 'wb_reward'.
--    Выкатывать оба файла в одном окне; порядок между ними — 2a, затем 2b.
--    Величина расхода не меняется: правится только имя категории.
--
--    PR-B (13.08.2026): удалены пять аварийных пар op_key='__NULL__'.
--    Они ловили строки нового контура (op_key = COALESCE(operation_type_normalized,
--    '__NULL__')) и нейтрализовали fail-closed ASSERT §5.3 именно там, где он нужен;
--    попутно терялась семантика ADJUSTMENT — «Коррекция логистики» и «Корректировка
--    эквайринга» уходили в обычный COST с ABS(). После восстановления
--    operation_type_normalized в V_WB_FINANCE_SEMANTIC заглушки не нужны.
--    ⚠️ Порядок обязателен: сначала FACT_FINANCE пересобран из семантического слоя,
--    только потом этот скрипт. Иначе §5.3 упадёт — и это будет правильно.
CREATE OR REPLACE TABLE `wb_mart.REF_COST_MAP__BUILD` AS
SELECT op_key, amount_field, economic_direction, cost_category,
  field_normalization_sign, note, CURRENT_TIMESTAMP() AS seeded_at
FROM UNNEST([
  -- 🔴 Stage 1.5 (26.08.2026) BACK-PORT, выполнен в Stage ADS-1A (06.09.2026).
  --    Stage 1.5 правил production ПРЯМЫМ UPDATE четырёх строк REF_COST_MAP и в этот seed
  --    НЕ вернулся. Файл разошёлся с production ровно так же, как в Stage 1.6 разошлась
  --    sp_build_mart_sku_daily. Повторный прогон seed'а откатил бы знаковую нормализацию:
  --    Продажа/commission_amount 2 231 127,66 ₽ · Удержание/deduction 307 917,84 ₽ ·
  --    Пересчет платной приемки/acceptance 4 000,00 ₽ · Штраф/penalty 548,00 ₽.
  --    Проверено на практике 06.09.2026: прогон дофиксового seed'а был пойман guard'ом
  --    fix #5 в sp_build_mart_sku_daily ДО сборки витрины (§pre, до _MART_BOOTSTRAP_LOCK).
  --    Ветка ADJUSTMENT (x * field_normalization_sign) знак СОХРАНЯЕТ; COST/CREDIT
  --    применяют ABS() построчно и знакопеременную пару разрушают.
  STRUCT('Продажа' AS op_key,'commission_amount' AS amount_field,'ADJUSTMENT' AS economic_direction,
         'wb_reward' AS cost_category,1 AS field_normalization_sign,
         'Вознаграждение WB (vw) по продаже. НЕ сбор маркетплейса — см. marketplace_fee_gap_rub (PR-B2) | Stage 1.5 (2026-08-26): знакопеременное поле — знак СОХРАНЯЕТСЯ. Построчный ABS() отражал возвраты/кредиты как расход.' AS note),
  ('Возврат','commission_amount','COST','wb_reward',-1,'Вознаграждение WB (vw) по возврату (PR-B2)'),
  ('Возмещение за выдачу и возврат товаров на ПВЗ','commission_amount','CREDIT','reimbursement_pvz',-1,'Возмещение ПВЗ (кредит)'),
  ('Возмещение издержек по перевозке/по складским операциям с товаром','commission_amount','CREDIT','reimbursement_logistics',-1,'Возмещение издержек перевозки/склада (кредит)'),
  -- Stage ADS-1A (2026-09-06): WB ПЕРЕИМЕНОВАЛ операцию с 01.09.2026. Доказательство —
  --   чистая посуточная передача эстафеты без единого дня пересечения: старое имя присутствует
  --   92/92 суток 01.06–31.08 и исчезает 01.09; новое появляется 01.09 и присутствует 5/5 суток.
  --   Тождественны: amount_field (commission_amount), полярность знака (125/125 строк < 0,
  --   как 5829/5829 у старого), порядок величины (−13,4…−0,8 против −20,6…−0,04),
  --   привязка к SKU (125/125 matched), отношение к логистике (0,065…0,103 внутри
  --   исторического диапазона 0,014…0,199). Внешнее подтверждение: оферта WB от 01.09.2026
  --   переименовала услугу «организации доставки» в «доставку» (п. 13.1.4).
  -- 🔴 Категория НЕ 'wb_reward'. Отрицательная сумма в поле commission_amount здесь —
  --   ВОЗМЕЩЕНИЕ издержек продавца (кредит), а не вознаграждение маркетплейса. Классифицируем
  --   экономический смысл операции, а не техническое имя поля. Категория переиспользована
  --   существующая: downstream (dashboard_contract_v2 §fin_long_day) уже разносит
  --   reimbursement_logistics отдельной строкой; новое имя категории молча ушло бы
  --   в остаточные корзины other_sku_rub/other_account_rub и изменило бы «Прочие расходы WB».
  ('Возмещение издержек по перемещению и операционной обработке товара','commission_amount','CREDIT','reimbursement_logistics',-1,'Возмещение издержек перемещения/операционной обработки (кредит). Stage ADS-1A: переименование WB с 01.09.2026 операции «Возмещение издержек по перевозке/по складским операциям с товаром» — та же семантика, то же поле, тот же знак'),
  ('Коррекция продаж','commission_amount','ADJUSTMENT','wb_reward',-1,'Корректировка вознаграждения WB — знак сохраняется (PR-B2)'),
  ('Продажа','acquiring_fee','COST','acquiring',1,'Эквайринг по продаже (per-SKU COST)'),
  ('Возврат','acquiring_fee','COST','acquiring',1,'Эквайринг по возврату'),
  ('Коррекция продаж','acquiring_fee','ADJUSTMENT','acquiring',1,'Корректировка эквайринга (продажи)'),
  ('Корректировка эквайринга','acquiring_fee','ADJUSTMENT','acquiring',1,'Корректировка эквайринга'),
  ('Логистика','logistics_amount','COST','logistics',1,'Логистика WB'),
  -- Stage ADS-1A (2026-09-06): второе переименование того же дня. «Логистика» присутствует
  --   92/92 суток 01.06–31.08 и исчезает 01.09; «Доставка» появляется 01.09 (5/5 суток).
  --   Тождественны: amount_field (logistics_amount), знак (0 отрицательных строк из 85,
  --   как 0 из 2531 у старого), стоимость строки (53,01…70,39 ₽ против 52,41…66,50 ₽ —
  --   ступени нет). Категория 'logistics' переиспользована: только 'logistics' и 'wb_reward'
  --   попадают в logistics_cost_positive витрины (pr_mart2b_sku_daily.sql:273), поэтому
  --   любое другое имя вывело бы доставку из контрибуции SKU.
  ('Доставка','logistics_amount','COST','logistics',1,'Доставка WB. Stage ADS-1A: переименование WB с 01.09.2026 операции «Логистика» — то же поле, тот же знак, та же стоимость строки'),
  ('Коррекция логистики','logistics_amount','ADJUSTMENT','logistics',1,'Корректировка логистики'),
  ('Хранение','storage_fee','COST','storage',1,'Хранение WB'),
  ('Коррекция хранения','storage_fee','ADJUSTMENT','storage',1,'Корректировка хранения — знак сохраняется (PR-B)'),
  ('Удержание','deduction','ADJUSTMENT','deduction',1,'Прочие удержания | Stage 1.5 (2026-08-26): знакопеременное поле — знак СОХРАНЯЕТСЯ. Построчный ABS() отражал возвраты/кредиты как расход.'),
  ('Удержание','additional_payment','COST','deduction',1,'Удержание через доп. платёж'),
  ('Штраф','penalty','ADJUSTMENT','penalty',1,'Штрафы WB | Stage 1.5 (2026-08-26): знакопеременное поле — знак СОХРАНЯЕТСЯ. Построчный ABS() отражал возвраты/кредиты как расход.'),
  ('Платная приемка','acceptance','COST','acceptance',1,'Платная приёмка'),
  ('Пересчет платной приемки','acceptance','ADJUSTMENT','acceptance',1,'Пересчёт платной приёмки | Stage 1.5 (2026-08-26): знакопеременное поле — знак СОХРАНЯЕТСЯ. Построчный ABS() отражал возвраты/кредиты как расход.'),
  ('Стоимость участия в программе лояльности','additional_payment','COST','loyalty',1,'Программа лояльности WB')
]);

-- ── 2. SEED-контракты на __BUILD (NULL-safe) ─────────────────────────────────
ASSERT (SELECT COUNT(*) = COUNT(DISTINCT FORMAT('%t|%t', op_key, amount_field))
        FROM `wb_mart.REF_COST_MAP__BUILD`)
  AS 'PR-Mart2a seed: (op_key×amount_field) не уникален';
ASSERT (SELECT COUNTIF(op_key IS NULL OR TRIM(op_key)='' OR amount_field IS NULL OR TRIM(amount_field)='')
        FROM `wb_mart.REF_COST_MAP__BUILD`) = 0
  AS 'PR-Mart2a seed: NULL/empty op_key/amount_field';
ASSERT (SELECT COUNTIF(economic_direction IS NULL OR economic_direction NOT IN ('COST','CREDIT','ADJUSTMENT'))
        FROM `wb_mart.REF_COST_MAP__BUILD`) = 0
  AS 'PR-Mart2a seed: economic_direction NULL или вне домена';
ASSERT (SELECT COUNTIF(field_normalization_sign IS NULL OR field_normalization_sign NOT IN (-1, 1))
        FROM `wb_mart.REF_COST_MAP__BUILD`) = 0
  AS 'PR-Mart2a seed: field_normalization_sign NULL или вне {-1,1}';
ASSERT (SELECT COUNTIF(cost_category IS NULL OR TRIM(cost_category)='')
        FROM `wb_mart.REF_COST_MAP__BUILD`) = 0
  AS 'PR-Mart2a seed: cost_category NULL/empty';

-- ── 3. STAGING long (TEMP) для live-контрактов (целевые VIEW ещё НЕ созданы) ──
CREATE TEMP TABLE _long AS
SELECT
  f.finance_row_key, f.finance_date, f.nm_id,
  COALESCE(f.operation_type_normalized, '__NULL__')                     AS op_key,
  COALESCE(f.nm_id > 0 AND f.sku_match_status = 'matched', FALSE)       AS is_sku_row,
  u.amount_field, u.source_signed_amount
FROM `wb_mart.FACT_FINANCE` f,
UNNEST([
  STRUCT('commission_amount'  AS amount_field, f.commission_amount  AS source_signed_amount),
  ('logistics_amount',  f.logistics_amount), ('storage_fee', f.storage_fee), ('deduction', f.deduction),
  ('penalty', f.penalty), ('acceptance', f.acceptance), ('acquiring_fee', f.acquiring_fee),
  ('additional_payment', f.additional_payment), ('other_amount', f.other_amount)
]) u
WHERE u.source_signed_amount IS NOT NULL AND u.source_signed_amount <> 0;

CREATE TEMP TABLE _mapped AS
SELECT l.is_sku_row, l.op_key, l.amount_field, l.source_signed_amount AS s,
  r.economic_direction AS dir, r.field_normalization_sign AS sgn, r.cost_category AS cat,
  CASE r.economic_direction
    WHEN 'COST'       THEN ABS(l.source_signed_amount)
    WHEN 'CREDIT'     THEN -ABS(l.source_signed_amount)
    WHEN 'ADJUSTMENT' THEN l.source_signed_amount * r.field_normalization_sign
  END AS cp
FROM _long l LEFT JOIN `wb_mart.REF_COST_MAP__BUILD` r USING (op_key, amount_field);

-- ── 4. LIVE-контракты (fail-closed: до publish) ──────────────────────────────
-- §5.1 compensation guard.
ASSERT (SELECT COUNTIF(compensation_amount IS NOT NULL AND compensation_amount <> 0)
        FROM `wb_mart.FACT_FINANCE`) = 0
  AS 'PR-Mart2a §5.1: compensation_amount несёт ненулевые значения';

-- §5.2 лемма консервации по-полю: явный spine ВСЕХ 9 полей, LEFT JOIN, COALESCE (fix #2).
--   Поле без ненулевых LONG-строк (other_amount) остаётся в проверке: COALESCE(long,0)=COALESCE(fact,0).
ASSERT (
  WITH fields AS (SELECT f FROM UNNEST(['commission_amount','logistics_amount','storage_fee','deduction',
                    'penalty','acceptance','acquiring_fee','additional_payment','other_amount']) f),
  ls AS (SELECT amount_field, SUM(source_signed_amount) s FROM _long GROUP BY amount_field),
  fs AS (
    SELECT 'commission_amount' amount_field, COALESCE(SUM(commission_amount),0) fact_sum FROM `wb_mart.FACT_FINANCE`
    UNION ALL SELECT 'logistics_amount', COALESCE(SUM(logistics_amount),0) FROM `wb_mart.FACT_FINANCE`
    UNION ALL SELECT 'storage_fee', COALESCE(SUM(storage_fee),0) FROM `wb_mart.FACT_FINANCE`
    UNION ALL SELECT 'deduction', COALESCE(SUM(deduction),0) FROM `wb_mart.FACT_FINANCE`
    UNION ALL SELECT 'penalty', COALESCE(SUM(penalty),0) FROM `wb_mart.FACT_FINANCE`
    UNION ALL SELECT 'acceptance', COALESCE(SUM(acceptance),0) FROM `wb_mart.FACT_FINANCE`
    UNION ALL SELECT 'acquiring_fee', COALESCE(SUM(acquiring_fee),0) FROM `wb_mart.FACT_FINANCE`
    UNION ALL SELECT 'additional_payment', COALESCE(SUM(additional_payment),0) FROM `wb_mart.FACT_FINANCE`
    UNION ALL SELECT 'other_amount', COALESCE(SUM(other_amount),0) FROM `wb_mart.FACT_FINANCE`)
  SELECT COUNTIF(ABS(COALESCE(ls.s,0) - COALESCE(fs.fact_sum,0)) > 0.005)
  FROM fields fld
  LEFT JOIN ls ON ls.amount_field = fld.f
  LEFT JOIN fs ON fs.amount_field = fld.f
) = 0
  AS 'PR-Mart2a §5.2: лемма консервации по-полю (9/9) нарушена';

-- §5.3 unknown money-pairs = 0.
ASSERT (SELECT COUNT(*) FROM (SELECT op_key, amount_field FROM _mapped WHERE cat IS NULL GROUP BY op_key, amount_field)) = 0
  AS 'PR-Mart2a §5.3: денежные пары (op_key×field) вне REF_COST_MAP';

-- §5.4 нормализация: точная формула cost_amount_positive во ВСЕХ ТРЁХ режимах + нет NULL cp для money-строк (fix #3).
ASSERT (SELECT
      COUNTIF(dir = 'COST'       AND cp <> ABS(s))
    + COUNTIF(dir = 'CREDIT'     AND cp <> -ABS(s))
    + COUNTIF(dir = 'ADJUSTMENT' AND cp <> s * sgn)
    + COUNTIF(cat IS NOT NULL AND cp IS NULL)
    + COUNTIF(is_sku_row IS NULL)
    FROM `_mapped`) = 0
  AS 'PR-Mart2a §5.4a: формула cost_amount_positive неверна в каком-то из режимов (COST/CREDIT/ADJUSTMENT)';

-- §5.4b расщепление полное: total == SKU + ACCOUNT.
ASSERT (SELECT ABS(SUM(cp) - SUM(IF(is_sku_row, cp, 0)) - SUM(IF(NOT is_sku_row, cp, 0))) < 0.005
        FROM `_mapped` WHERE cp IS NOT NULL)
  AS 'PR-Mart2a §5.4b: SKU + ACCOUNT != total';

-- ── 4b. 🔴 SOURCE-OF-TRUTH PARITY GATE (Stage ADS-1A, 06.09.2026) ────────────
--   ПРИЧИНА. 06.09.2026 повторный прогон этого seed'а молча откатил четыре
--   знаковых исправления Stage 1.5, применённых 26.08.2026 прямым UPDATE по
--   production и не вернувшихся в файл. Ни один из гейтов §2/§5 этого не ловил:
--   все они проверяют seed на внутреннюю согласованность, но НИ ОДИН не
--   сравнивает seed с тем, что УЖЕ живёт в production. Инцидент остановил
--   только guard fix #5 в sp_build_mart_sku_daily — то есть на два слоя ниже
--   и уже после перезаписи REF.
--
--   ЧТО ДЕЛАЕТ. Перед публикацией сравнивает СЕМАНТИЧЕСКОЕ множество
--   (op_key × amount_field × direction × category × sign) действующего
--   production REF_COST_MAP с тем, что собирается опубликовать __BUILD.
--   Строка, которая есть в production и НЕ воспроизводится seed'ом, —
--   это либо hotfix, не вернувшийся в файл, либо осознанное удаление правила.
--   Отличить их автоматически нельзя, поэтому гейт fail-closed: он ОСТАНАВЛИВАЕТ
--   публикацию и требует явного решения человека.
--
--   КАК СНЯТЬ ЛОЖНОЕ СРАБАТЫВАНИЕ при намеренном удалении пары: сначала удалить
--   строку из production (UPDATE/DELETE с обоснованием в CHANGELOG), потом
--   прогонять seed. Обходить гейт правкой этого ASSERT запрещено.
--
--   ⚠️ Гейт предполагает, что REF_COST_MAP уже существует (верно с 30.07.2026).
--   Для bootstrap «с нуля» на пустом датасете его надо пропустить осознанно.
--   Логика проверена read-only на проде 06.09.2026: prod_only=0, seed_only=0.
ASSERT (
  SELECT COUNT(*) = 0 FROM (
    SELECT op_key, amount_field, economic_direction, cost_category, field_normalization_sign
    FROM `wb_mart.REF_COST_MAP`
    EXCEPT DISTINCT
    SELECT op_key, amount_field, economic_direction, cost_category, field_normalization_sign
    FROM `wb_mart.REF_COST_MAP__BUILD`))
  AS 'PR-Mart2a §4b DRIFT: в production REF_COST_MAP есть семантические строки, которых нет в seed. Публикация откатила бы их. Сначала back-port в этот файл — см. Stage ADS-1A (06.09.2026).';

-- ── 5. PUBLISH (только после прохождения ВСЕХ гейтов) ────────────────────────
CREATE OR REPLACE TABLE `wb_mart.REF_COST_MAP`
CLUSTER BY op_key, amount_field AS
SELECT op_key, amount_field, economic_direction, cost_category, field_normalization_sign, note, seeded_at
FROM `wb_mart.REF_COST_MAP__BUILD`;

CREATE OR REPLACE VIEW `wb_mart.V_WB_FINANCE_AMOUNTS_LONG` AS
SELECT
  f.finance_row_key, f.finance_date, f.nm_id, f.sku_match_status, f.operation_type_normalized,
  COALESCE(f.operation_type_normalized, '__NULL__')                     AS op_key,
  COALESCE(f.nm_id > 0 AND f.sku_match_status = 'matched', FALSE)       AS is_sku_row,
  u.amount_field, u.source_signed_amount
FROM `wb_mart.FACT_FINANCE` f,
UNNEST([
  STRUCT('commission_amount'  AS amount_field, f.commission_amount  AS source_signed_amount),
  ('logistics_amount',  f.logistics_amount), ('storage_fee', f.storage_fee), ('deduction', f.deduction),
  ('penalty', f.penalty), ('acceptance', f.acceptance), ('acquiring_fee', f.acquiring_fee),
  ('additional_payment', f.additional_payment), ('other_amount', f.other_amount)
]) u
WHERE u.source_signed_amount IS NOT NULL AND u.source_signed_amount <> 0;

CREATE OR REPLACE VIEW `wb_mart.V_WB_FINANCE_AMOUNTS_LONG_MAPPED` AS
SELECT
  l.finance_row_key, l.finance_date, l.nm_id, l.sku_match_status,
  l.operation_type_normalized, l.op_key, l.is_sku_row,
  l.amount_field, l.source_signed_amount,
  r.economic_direction, r.cost_category, r.field_normalization_sign,
  CASE r.economic_direction
    WHEN 'COST'       THEN ABS(l.source_signed_amount)
    WHEN 'CREDIT'     THEN -ABS(l.source_signed_amount)
    WHEN 'ADJUSTMENT' THEN l.source_signed_amount * r.field_normalization_sign
    ELSE NULL
  END AS cost_amount_positive
FROM `wb_mart.V_WB_FINANCE_AMOUNTS_LONG` l
LEFT JOIN `wb_mart.REF_COST_MAP` r USING (op_key, amount_field);

-- ── 6. cleanup staging ───────────────────────────────────────────────────────
DROP TABLE IF EXISTS `wb_mart.REF_COST_MAP__BUILD`;
