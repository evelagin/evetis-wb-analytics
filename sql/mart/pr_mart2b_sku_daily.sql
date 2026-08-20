-- ============================================================================
-- PR-Mart2b (PR#81) — EVETIS WB Analytics MART. Витрина #1: MART_SKU_DAILY (day × nm_id). REV2 (аудит).
-- Дата: 2026-07-30.  Контракты: docs/MART_MART2_CONTRACTS_2026-07-28.md (§4 spine, §KPI).
-- PR-нота: docs/MART_PR2B_SKU_DAILY_2026-07-30.md.  Зависит от PR-Mart2a (LONG_MAPPED в проде).
--
-- REV2 по замечаниям аудитора (REQUEST CHANGES, PR#81):
--   #1 Fail-closed guard: в V_WB_FINANCE_AMOUNTS_LONG_MAPPED (finance_date<=build_as_of) НЕТ строк
--      с cost_category IS NULL — неизвестные денежные пары не должны молча исчезать из витрины.
--   #2 Полнота universe: guard global_start<=build_as_of; BUILD rows>0;
--      (active REF nm_id) EXCEPT DISTINCT (BUILD nm_id) = 0 — ловит ПОЛНОСТЬЮ отсутствующий SKU.
--   #3 Все source-CTE и reconciliation ограничены source_date<=build_as_of (воспроизводимость на дату;
--      особенно finance/LONG_MAPPED — finance исключён из max_required_source_date).
--   #4 Убрана NULL-ловушка GREATEST/LEAST: max/min через MAX/MIN(d) FROM UNNEST(dates) WHERE d IS NOT NULL
--      + явный RAISE, если обязательные даты определить невозможно (пустые источники).
--   (+ добавлен KPI cpm = spend/views×1000 — был в списке KPI аудита.)
-- REV3 (re-audit): остаточный fail-closed blocker — NULL-safe UNNEST молча терпел пустой ОТДЕЛЬНЫЙ
--   обязательный источник (max_required считался по остальным). Теперь per-source max-date по
--   FACT_ORDERS/FACT_SALES/FACT_ADS_SKU_DAILY + явный RAISE на КАЖДЫЙ пустой; отдельная проверка непустоты
--   finance LONG_MAPPED (в max_required НЕ входит); только затем GREATEST(orders_max, sales_max, ads_max).
--   CPM входит в витрину (не deferred).
-- REV4 (re-audit): finance-guard считал ВСЕ строки LONG_MAPPED, а витрина потребляет только is_sku_row.
--   При сбое reference-мэппинга (все строки → ACCOUNT) guard прошёл бы, а fin оказался пуст → нулевая витрина
--   опубликовалась бы. Теперь считаем ТОЛЬКО is_sku_row + active universe (реальный источник витрины).
-- REV5 (re-audit): guard is_sku_row всё ещё шире, чем два выводимых показателя (commission_cost_positive,
--   logistics_cost_positive). Развёл на ДВА счётчика по cost_category='commission' и ='logistics' с RAISE каждый —
--   при сбое mapping конкретной категории общий is_sku_row-guard прошёл бы, а метрика занулилась.
--   (PR-B2: категория 'commission' переименована в 'wb_reward', счётчиков стало три — см. ниже.)
-- REV6 (PR-Mart3b-1): оркестрация. Дизайн: docs/MART_PR3B_PLAN_2026-08-03.md (REV3 APPROVED), PR-нота: docs/MART_PR3B1_LOADER_2026-08-03.md.
--   1) +параметр in_run_id STRING (сквозной id из Cloud Run; fallback GENERATE_UUID() при ''/NULL).
--   2) СНЯТ guard build_as_of < max_required_source_date (несовместим с D-1 и бэкфиллом; его роль берёт внешний
--      freshness-gate по V_INGEST_HEARTBEAT). v_max_required оставлен как sanity-величина, но НЕ гейтит.
--      Верхний guard build_as_of > сегодня(МСК) СОХРАНЁН. Источники витрины уже bounded <= build_as_of.
--   3) ADS coverage vs activity (аудит PR3b-1, блокеры #4/#3-REV3). Fail-closed ПОКРЫТИЕ рекламы за target_date
--      доказывает HEARTBEAT-гейт в loader (ads covers_target=COMPLETE), а НЕ MAX(FACT_ADS.date): успешный zero-row
--      день (ран загрузчика OK, рекламных строк нет) дал бы ложный stale по FACT. Процедура рекламу НЕ гейтит.
--   4) +колонки MART_SKU_DAILY: ads_activity_max_date DATE и ads_activity_lagged BOOL — ЧИСТО ДИАГНОСТИЧЕСКИЕ
--      поля АКТИВНОСТИ (build-level, одинаковы во всех строках). ⚠️ ЭТО НЕ индикатор покрытия/качества/полноты
--      данных: полнота гарантируется heartbeat-гейтом. ads_activity_max_date = MAX(FACT_ADS.date <= build_as_of)
--      (NULL/раньше build_as_of на дни без рекламы — НОРМА, не лаг данных). ads_activity_lagged =
--      (ads_activity_max_date IS NULL OR < build_as_of) — «в последний(е) день(дни) не было рекламной активности».
--      +ASSERT: ads_activity_max_date <= build_as_of, COUNT(DISTINCT ads_activity_max_date)<=1 (единый снимок).
-- PR-B2 (13.08.2026): честная семантика расхода на маркетплейс. Спека: docs/FINANCE_PR_B2_METRICS_2026-08-13.md.
--   Слово `commission` больше нигде не означает `vw`.
--   1) Колонка commission_cost_positive → wb_reward_cost_positive. Чистое переименование:
--      те же строки LONG_MAPPED, та же величина до копейки. Это vw — вознаграждение WB по операции.
--   2) +Колонка marketplace_fee_rub = SUM(FACT_FINANCE.marketplace_fee_gap_rub) по SKU-строкам —
--      authoritative сбор маркетплейса (retail_price_withdisc_rub − for_pay, V_WB_FINANCE_SEMANTIC §4.1).
--   3) hybrid_day_contribution_pre_cogs теперь вычитает marketplace_fee_rub вместо vw. Ранее контрибуция
--      была ЗАВЫШЕНА на (fee_gap − vw): валовая выручка выкупов уменьшалась на вознаграждение WB,
--      а не на реальный сбор. settlement_day_contribution_pre_cogs НЕ меняется (for_pay уже нетто).
--   4) wb_reward_cost_positive и acquiring_fee в формулы контрибуции НЕ входят — они внутри спреда;
--      вычитать их вторым разом нельзя. Это контракт, а не умолчание.
--   ⚠️ АТОМАРНО С sql/mart/pr_mart2a_finance_longform.sql (seed 'commission' → 'wb_reward') и с
--      sql/mart/pr_mart1_facts.sql (колонка marketplace_fee_gap_rub в FACT_FINANCE). Порядок в окне:
--      pr_mart1 → CALL sp_bootstrap_facts → pr_mart2a → pr_mart2b → CALL sp_build_mart_sku_daily.
--      Разнести по времени нельзя: между 2a и 2b guard ищет несуществующую категорию и штатный
--      прогон 07:00 упадёт с RAISE.
--   ⚠️ ЛОМАЮЩЕЕ переименование колонки витрины. Потребителей нет (дашборд не построен) — сделано сейчас.
--
-- Stage 3B (20.08.2026) — семантика рекламного расхода. Дизайн принят владельцем 20.08.
--   ПРАВИЛО 1 — миграция строго АДДИТИВНАЯ. Колонка `ad_spend` НЕ переименована и НЕ переопределена:
--     она была и остаётся АТРИБУТИРОВАННЫМ расходом (FACT_ADS_SKU_DAILY.stats_spend_rub). Статус —
--     legacy/deprecated: тот же смысл теперь доступен под явным именем ad_spend_attributed_rub.
--     Все существующие KPI (cpm, cpc, cpo_attributed, blended_cpo, drr_orders, drr_buyouts, roas, acos,
--     hybrid_/settlement_day_contribution_pre_cogs и все *_7d/_14d) считаются от `ad_spend` КАК РАНЬШЕ,
--     до последней копейки. Ни одна существующая величина этим PR не меняется.
--   ПРАВИЛО 2 — БИЛЛИНГ. +ad_spend_billed_rub: доля фактического списания WB, отнесённая на SKU
--     (FACT_ADS_SPEND_ALLOC_DAILY, аллокация внутри (date, advert_id) пропорционально stats).
--     🔴 ДЕНЕЖНОЙ колонки остатка в витрине НЕТ и быть не должно (ревью 20.08). Natural grain
--     остатка — (date, advert_id) и сутки, а не (day, nm_id): повторённая во всех строках дня
--     сумма молча завышалась бы в SUM() по SKU ровно в число SKU. Вместо суммы витрина несёт
--     БУЛЕВО наблюдение +has_unallocated_billing (в сутках есть биллинг, не отнесённый ни на
--     одну строку витрины). Сама сумма живёт ТОЛЬКО в reconciliation-объектах:
--     wb_mart.V_ADS_SPEND_RECONCILIATION (грейн date × advert_id) и ..._DAILY (грейн date),
--     где биллинг раскладывается на три взаимоисключающие части:
--         actual = billed_valid_sku_rub + billed_outside_sku_universe_rub + billed_no_allocation_basis_rub
--   ПРАВИЛО 3 — ЭКОНОМИКА на биллинге: +drr_orders_billed, +drr_buyouts_billed,
--     +hybrid_day_contribution_pre_cogs_billed, +settlement_day_contribution_pre_cogs_billed.
--   ПРАВИЛО 4 — PERFORMANCE на атрибуции: +roas_attributed, +acos_attributed (явные имена рядом с
--     legacy roas/acos) и отдельный +roas_billed. Однозначного поля `roas` без суффикса больше нет
--     в контракте: legacy-имя сохранено только как deprecated-совместимость.
--   ПОЛНОТА СУТОК — ДВА РАЗНЫХ ФЛАГА, их нельзя смешивать:
--     billed_complete              — ЗАКРЫТЫ ли сутки по биллингу. 🔴 Берётся ИСКЛЮЧИТЕЛЬНО из
--                                    wb_raw.V_ADV_COSTS_DAY_COVERAGE — объекта coverage-контракта
--                                    adv/v1/upd. Витрина этот флаг НЕ вычисляет и вычислять не может.
--                                    Правило «day <= MAX(FACT_ADS_COSTS_DAILY.date)» ОПРОВЕРГНУТО
--                                    данными 20.08 (docs/ADS_COSTS_COVERAGE_CONTRACT_2026-08-20.md):
--                                    оно не отличает успешные нулевые сутки от незагруженных, а
--                                    сам биллинг не финален на D+1 и не монотонен. Отсутствие
--                                    строки в coverage = FALSE (fail-closed): пока полнота не
--                                    доказана, billed-KPI не публикуются.
--     billing_allocation_complete  — удалось ли РАСПРЕДЕЛИТЬ весь опубликованный биллинг до SKU
--                                    (unallocated = 0). NULL, когда billed_complete = FALSE:
--                                    распределять ещё нечего, и «TRUE» было бы ложным успехом.
--   🔴 На сутках с billed_complete = FALSE все billed-KPI строго NULL, а не 0: ноль означал бы
--     «рекламы не было», и ДРР этих суток схлопнулся бы в ноль, занизив управленческую картину.
--     ad_spend_billed_rub при этом хранит фактически доступное значение (0, если списаний ещё нет) —
--     сама сумма не искажается, но потребитель видит по флагу, что сутки неполные.
--   ⚠️ ТРЕБУЕТ pr_mart1_facts.sql того же change-set (FACT_ADS_SPEND_ALLOC/UNALLOC_DAILY) и
--     ПЕРЕ-bootstrap FACT перед первым прогоном этой процедуры.
--
-- Грейн: day × nm_id, DENSE spine. Universe = REF_SKU_MASTER (WB, active, nm_id NOT NULL).
--   start_date(nm)=LEAST(MIN order/sale/ads/finance по nm, все <=build_as_of); fallback=mart_global_start_date.
--   GENERATE_DATE_ARRAY(start, build_as_of) → пропуски=0. Rolling: RANGE 6/13 PRECEDING по UNIX_DATE(day).
--   Заказы: orders_qty/rub ЧИСТЫЕ (is_cancel=FALSE) + canceled_*. Выкупы: is_return=FALSE + returns_*.
--   Вклад-до-COGS cross-base (НЕ P&L дня); основной управленческий = rolling. Маржа (−COGS) — MART v2.
--
-- Паттерн Mart1: BUILD → ASSERT → publish → ASSERT физики. BOOTSTRAP/MANUAL-ONLY, lock 'mart_sku_daily'.
-- ⚠️ Витрина создаётся ТОЛЬКО после merge PR#81. Оркестрация (runWbMartDaily) — PR-Mart3.
-- ============================================================================

CREATE SCHEMA IF NOT EXISTS `wb_mart` OPTIONS (location = 'EU');

CREATE TABLE IF NOT EXISTS `wb_mart._MART_BOOTSTRAP_LOCK` (
  lock_id STRING NOT NULL, is_running BOOL, run_id STRING, last_run_id STRING,
  acquired_at TIMESTAMP, released_at TIMESTAMP
);
MERGE `wb_mart._MART_BOOTSTRAP_LOCK` T
USING (SELECT 'mart_sku_daily' AS lock_id) S ON T.lock_id = S.lock_id
WHEN NOT MATCHED THEN INSERT (lock_id, is_running) VALUES ('mart_sku_daily', FALSE);

CREATE OR REPLACE PROCEDURE `wb_mart.sp_build_mart_sku_daily`(
  IN in_build_as_of_date DATE,
  IN in_global_start_date DATE,
  IN in_run_id STRING            -- REV6: сквозной id из Cloud Run; '' / NULL → GENERATE_UUID()
)
BEGIN
  DECLARE v_run_id       STRING;
  DECLARE v_built_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP();
  DECLARE v_build_as_of  DATE      DEFAULT in_build_as_of_date;
  DECLARE v_global_start DATE;
  DECLARE v_max_required DATE;
  DECLARE v_orders_max   DATE;
  DECLARE v_sales_max    DATE;
  DECLARE v_ads_max                 DATE;
  DECLARE v_ads_activity_max_date DATE;   -- REV6: MAX(FACT_ADS.date <= build_as_of) — диагностика активности (НЕ покрытие)
  DECLARE v_ads_activity_lagged   BOOL;   -- REV6: диагностический build-level флаг активности (НЕ качество/лаг данных)
  DECLARE v_finance_wb_reward_rows  INT64;   -- PR-B2: бывш. v_finance_commission_rows
  DECLARE v_finance_logistics_rows  INT64;
  DECLARE v_finance_fee_gap_rows    INT64;   -- PR-B2: authoritative сбор WB — третий обязательный вход

  -- REV6: сквозной run_id (fallback на UUID, чтобы ручной CALL без id тоже работал).
  SET v_run_id = COALESCE(NULLIF(in_run_id, ''), GENERATE_UUID());

  -- build_as_of обязателен раньше всего (используется в проверке непустоты finance ниже).
  IF v_build_as_of IS NULL THEN
    RAISE USING MESSAGE = 'sp_build_mart_sku_daily: build_as_of_date IS NULL (нужен явный DATE)'; END IF;

  -- REV3 (re-audit): ОТДЕЛЬНАЯ max-date по каждому обязательному источнику + явный RAISE на пустой.
  --   NULL-safe UNNEST раньше молча терпел пустой отдельный источник (max_required считался по остальным).
  SET v_orders_max = (SELECT MAX(order_date) FROM `wb_mart.FACT_ORDERS`);
  SET v_sales_max  = (SELECT MAX(sale_date)  FROM `wb_mart.FACT_SALES`);
  SET v_ads_max    = (SELECT MAX(`date`)     FROM `wb_mart.FACT_ADS_SKU_DAILY`);
  IF v_orders_max IS NULL THEN RAISE USING MESSAGE = 'sp_build_mart_sku_daily: FACT_ORDERS пуст'; END IF;
  IF v_sales_max  IS NULL THEN RAISE USING MESSAGE = 'sp_build_mart_sku_daily: FACT_SALES пуст'; END IF;
  IF v_ads_max    IS NULL THEN RAISE USING MESSAGE = 'sp_build_mart_sku_daily: FACT_ADS_SKU_DAILY пуст'; END IF;


  -- finance НЕ входит в max_required (лагает недельно), но обязательные финансовые входы витрины
  --   обязаны быть непусты. REV5 (аудит): счётчики ПО КАЖДОМУ входу отдельно — иначе при сбое mapping
  --   конкретной категории (соответствующие строки исчезли, прочие SKU-строки остались) общий
  --   guard прошёл бы, а соответствующая метрика витрины занулилась, и reconciliation сравнил бы ноль с нулём.
  -- PR-B2: входов стало ТРИ.
  --   1) SKU wb_reward   — бывш. 'commission'; это vw, вознаграждение WB, из LONG_MAPPED;
  --   2) SKU logistics   — без изменений, из LONG_MAPPED;
  --   3) SKU fee_gap     — authoritative сбор маркетплейса, ПРЯМО из FACT_FINANCE (не из LONG_MAPPED:
  --      marketplace_fee_gap_rub не является одним из 9 unpivot-полей и в REF_COST_MAP не участвует).
  --      Он вошёл в hybrid_day_contribution_pre_cogs, поэтому подчиняется тому же правилу REV5:
  --      молчаливый ноль здесь завысил бы контрибуцию ровно так же, как раньше это делал vw.
  SET v_finance_wb_reward_rows = (
    SELECT COUNT(*) FROM `wb_mart.V_WB_FINANCE_AMOUNTS_LONG_MAPPED`
    WHERE finance_date <= v_build_as_of AND is_sku_row
      AND cost_category = 'wb_reward' AND cost_amount_positive IS NOT NULL
      AND nm_id IN (SELECT nm_id FROM `wb_raw.REF_SKU_MASTER`
                    WHERE marketplace='WB' AND active=TRUE AND nm_id IS NOT NULL));
  SET v_finance_logistics_rows = (
    SELECT COUNT(*) FROM `wb_mart.V_WB_FINANCE_AMOUNTS_LONG_MAPPED`
    WHERE finance_date <= v_build_as_of AND is_sku_row
      AND cost_category = 'logistics' AND cost_amount_positive IS NOT NULL
      AND nm_id IN (SELECT nm_id FROM `wb_raw.REF_SKU_MASTER`
                    WHERE marketplace='WB' AND active=TRUE AND nm_id IS NOT NULL));
  SET v_finance_fee_gap_rows = (
    SELECT COUNT(*) FROM `wb_mart.FACT_FINANCE`
    WHERE finance_date <= v_build_as_of
      AND COALESCE(nm_id > 0 AND sku_match_status = 'matched', FALSE)
      AND marketplace_fee_gap_rub IS NOT NULL
      AND nm_id IN (SELECT nm_id FROM `wb_raw.REF_SKU_MASTER`
                    WHERE marketplace='WB' AND active=TRUE AND nm_id IS NOT NULL));
  IF v_finance_wb_reward_rows = 0 THEN
    RAISE USING MESSAGE = 'sp_build_mart_sku_daily: SKU wb_reward finance пуст (is_sku_row, active universe, <=build_as_of)'; END IF;
  IF v_finance_logistics_rows = 0 THEN
    RAISE USING MESSAGE = 'sp_build_mart_sku_daily: SKU logistics finance пуст (is_sku_row, active universe, <=build_as_of)'; END IF;
  IF v_finance_fee_gap_rows = 0 THEN
    RAISE USING MESSAGE = 'sp_build_mart_sku_daily: SKU marketplace_fee_gap пуст (is_sku_row, active universe, <=build_as_of)'; END IF;

  -- REV6: v_max_required оставлен как sanity-величина (в лог/отладку), но БОЛЬШЕ НЕ ГЕЙТИТ build.
  --   Внешний freshness-gate (loader mart по V_INGEST_HEARTBEAT) доказывает факт успешной загрузки за D-1.
  SET v_max_required = GREATEST(v_orders_max, v_sales_max, v_ads_max);

  -- global_start: fallback для «мёртвых» SKU (обязательные источники непусты → MIN определён; finance тоже проверен).
  SET v_global_start = IFNULL(in_global_start_date, (
    SELECT MIN(d) FROM UNNEST([
      (SELECT MIN(order_date)   FROM `wb_mart.FACT_ORDERS`),
      (SELECT MIN(sale_date)    FROM `wb_mart.FACT_SALES`),
      (SELECT MIN(`date`)       FROM `wb_mart.FACT_ADS_SKU_DAILY`),
      (SELECT MIN(finance_date) FROM `wb_mart.FACT_FINANCE`)
    ]) AS d WHERE d IS NOT NULL));

  -- --- остальные guards (fail-closed) ---
  IF v_build_as_of > CURRENT_DATE('Europe/Moscow') THEN
    RAISE USING MESSAGE = 'sp_build_mart_sku_daily: build_as_of_date в будущем (> сегодня МСК)'; END IF;
  -- REV6: guard build_as_of < max_required СНЯТ (см. заголовок REV6 #2) — несовместим с D-1/бэкфиллом.

  -- REV6 (блокеры #4/#3-REV3): ДИАГНОСТИЧЕСКИЕ ads-метаданные АКТИВНОСТИ (НЕ гейт, НЕ индикатор полноты).
  --   Покрытие рекламы за target_date доказывает heartbeat-гейт loader'а; здесь — лишь МАКС дата рекламной
  --   АКТИВНОСТИ в FACT (NULL/раньше build_as_of на дни без рекламы — НОРМА). BI НЕ должен трактовать эти поля
  --   как «данные лагают/неполны» — они говорят лишь «в этот день не было рекламных строк».
  SET v_ads_activity_max_date = (SELECT MAX(`date`) FROM `wb_mart.FACT_ADS_SKU_DAILY` WHERE `date` <= v_build_as_of);
  SET v_ads_activity_lagged = (v_ads_activity_max_date IS NULL) OR (v_ads_activity_max_date < v_build_as_of);

  IF v_global_start IS NULL THEN
    RAISE USING MESSAGE = 'sp_build_mart_sku_daily: mart_global_start_date не определён'; END IF;
  IF v_global_start > v_build_as_of THEN                                            -- fix #2
    RAISE USING MESSAGE = 'sp_build_mart_sku_daily: global_start > build_as_of (пустой spine)'; END IF;

  -- fix #1: неизвестные денежные пары (cost_category IS NULL) в окне <=build_as_of ЗАПРЕЩЕНЫ.
  --   Иначе fin-CTE (cost_amount_positive IS NOT NULL) молча уронил бы их из витрины.
  ASSERT (SELECT COUNTIF(cost_category IS NULL)
          FROM `wb_mart.V_WB_FINANCE_AMOUNTS_LONG_MAPPED`
          WHERE finance_date <= v_build_as_of) = 0
    AS 'SKU_DAILY §pre: unknown money-pair (cost_category IS NULL) в LONG_MAPPED — расширить REF_COST_MAP';

  -- --- concurrency guard ---
  UPDATE `wb_mart._MART_BOOTSTRAP_LOCK`
     SET is_running = TRUE, run_id = v_run_id, acquired_at = CURRENT_TIMESTAMP(), released_at = NULL
   WHERE lock_id = 'mart_sku_daily' AND is_running = FALSE;
  IF @@row_count = 0 THEN
    RAISE USING MESSAGE = 'sp_build_mart_sku_daily: lock занят (manual-only). Снять _MART_BOOTSTRAP_LOCK при зависшем ране.'; END IF;

  BEGIN
    -- fix #3: ВСЕ source-CTE ограничены <= @build_as_of (воспроизводимость на дату).
    EXECUTE IMMEDIATE """
      CREATE OR REPLACE TABLE `wb_mart.MART_SKU_DAILY__BUILD`
      PARTITION BY day CLUSTER BY nm_id AS
      WITH universe AS (
        SELECT nm_id FROM `wb_raw.REF_SKU_MASTER`
        WHERE marketplace='WB' AND active=TRUE AND nm_id IS NOT NULL GROUP BY nm_id),
      ads AS (
        SELECT nm_id, `date` d, SUM(stats_spend_rub) ad_spend, SUM(views) views, SUM(clicks) clicks,
          SUM(ad_orders_raw) ad_orders_raw, SUM(ads_revenue_raw_rub) ads_revenue_raw_rub,
          SUM(ads_revenue_dedup_estimate_rub) ads_revenue_dedup_estimate_rub,
          SUM(ad_orders_dedup_estimate) ad_orders_dedup_estimate
        FROM `wb_mart.FACT_ADS_SKU_DAILY` WHERE `date` <= @build_as_of GROUP BY nm_id, `date`),
      ads_billed AS (
        -- Stage 3B: доля фактического списания WB, отнесённая на SKU (грейн SKU × сутки).
        SELECT nm_id, `date` d, SUM(billed_alloc_rub) ad_spend_billed_rub
        FROM `wb_mart.FACT_ADS_SPEND_ALLOC_DAILY` WHERE `date` <= @build_as_of GROUP BY nm_id, `date`),
      cov AS (
        -- 🔴 ЕДИНСТВЕННЫЙ источник правды о закрытости суток. Витрина его не выводит.
        SELECT `date` d, billed_complete
        FROM `wb_raw.V_ADV_COSTS_DAY_COVERAGE`
        WHERE `date` <= @build_as_of),
      bill_day AS (
        -- Stage 3B: всё списание суток целиком (грейн СУТКИ) — знаменатель сходимости.
        SELECT `date` d, SUM(actual_spend_rub) actual_spend_day_rub
        FROM `wb_mart.FACT_ADS_COSTS_DAILY` WHERE `date` <= @build_as_of GROUP BY `date`),
      bill_day_universe AS (
        -- Stage 3B: та часть списания суток, которую реально несут строки витрины.
        --   Ограничение по universe обязательно: аллокация на nm_id вне справочника в витрину
        --   не попадёт, и без этого вычета остаток суток оказался бы занижен.
        SELECT `date` d, SUM(billed_alloc_rub) billed_universe_day_rub
        FROM `wb_mart.FACT_ADS_SPEND_ALLOC_DAILY`
        WHERE `date` <= @build_as_of AND nm_id IN (SELECT nm_id FROM universe)
        GROUP BY `date`),
      ord AS (
        SELECT nm_id, order_date d, COUNTIF(NOT is_cancel) orders_qty,
          SUM(IF(NOT is_cancel, price_with_disc, 0)) orders_rub,
          COUNTIF(is_cancel) canceled_qty, SUM(IF(is_cancel, price_with_disc, 0)) canceled_rub
        FROM `wb_mart.FACT_ORDERS` WHERE order_date <= @build_as_of GROUP BY nm_id, order_date),
      sal AS (
        SELECT nm_id, sale_date d, COUNTIF(NOT is_return) buyouts_qty,
          SUM(IF(NOT is_return, price_with_disc, 0)) buyouts_rub,
          SUM(IF(NOT is_return, sales_for_pay_operational, 0)) sales_for_pay_operational,
          COUNTIF(is_return) returns_qty, SUM(IF(is_return, price_with_disc, 0)) returns_rub
        FROM `wb_mart.FACT_SALES` WHERE sale_date <= @build_as_of GROUP BY nm_id, sale_date),
      fin AS (
        -- PR-B2: wb_reward_cost_positive — бывш. commission_cost_positive. Переименование, не пересчёт:
        --   те же строки, та же величина. Это vw (вознаграждение WB), и в формулу контрибуции он больше НЕ входит.
        SELECT nm_id, finance_date d,
          SUM(IF(cost_category='wb_reward', cost_amount_positive, 0)) wb_reward_cost_positive,
          SUM(IF(cost_category='logistics', cost_amount_positive, 0)) logistics_cost_positive
        FROM `wb_mart.V_WB_FINANCE_AMOUNTS_LONG_MAPPED`
        WHERE is_sku_row AND cost_amount_positive IS NOT NULL AND finance_date <= @build_as_of
        GROUP BY nm_id, finance_date),
      finpay AS (
        -- PR-B2: marketplace_fee_rub — authoritative сбор WB, retail_price_withdisc_rub − for_pay.
        --   Берётся из FACT_FINANCE, а не из LONG_MAPPED: это не одно из 9 unpivot-полей.
        --   Гейт SKU тот же (is_sku_row), окно то же. NULL вне «Продажа»/«Возврат» — SUM их игнорирует,
        --   и это корректно: у прочих операций пары «выручка × выплата» не существует.
        SELECT nm_id, finance_date d,
          SUM(finance_for_pay_accounting) finance_for_pay_accounting,
          SUM(marketplace_fee_gap_rub)    marketplace_fee_rub
        FROM `wb_mart.FACT_FINANCE`
        WHERE COALESCE(nm_id > 0 AND sku_match_status='matched', FALSE) AND finance_date <= @build_as_of
        GROUP BY nm_id, finance_date),
      findt AS (SELECT nm_id, MIN(finance_date) d FROM `wb_mart.FACT_FINANCE`
                WHERE nm_id > 0 AND finance_date <= @build_as_of GROUP BY nm_id),
      firstev AS (
        SELECT nm_id, MIN(d) first_d FROM (
          SELECT nm_id, d FROM ads UNION ALL SELECT nm_id, d FROM ord
          UNION ALL SELECT nm_id, d FROM sal UNION ALL SELECT nm_id, d FROM findt
        ) GROUP BY nm_id),
      spine AS (
        SELECT u.nm_id, day
        FROM universe u LEFT JOIN firstev f USING (nm_id),
        UNNEST(GENERATE_DATE_ARRAY(IFNULL(f.first_d, @global_start), @build_as_of)) day),
      joined AS (
        SELECT s.nm_id, s.day,
          IFNULL(a.ad_spend,0) ad_spend, IFNULL(a.views,0) views, IFNULL(a.clicks,0) clicks,
          IFNULL(a.ad_orders_raw,0) ad_orders_raw, IFNULL(a.ads_revenue_raw_rub,0) ads_revenue_raw_rub,
          IFNULL(a.ads_revenue_dedup_estimate_rub,0) ads_revenue_dedup_estimate_rub,
          IFNULL(a.ad_orders_dedup_estimate,0) ad_orders_dedup_estimate,
          IFNULL(ab.ad_spend_billed_rub,0) ad_spend_billed_rub,
          -- ВНУТРЕННЯЯ величина: в витрину как деньги НЕ выходит, нужна только для булева флага.
          IFNULL(bd.actual_spend_day_rub,0) - IFNULL(bu.billed_universe_day_rub,0) AS unallocated_day_rub_internal,
          IFNULL(cv.billed_complete, FALSE) AS billed_complete,   -- fail-closed: нет строки покрытия => не закрыто
          IFNULL(o.orders_qty,0) orders_qty, IFNULL(o.orders_rub,0) orders_rub,
          IFNULL(o.canceled_qty,0) canceled_qty, IFNULL(o.canceled_rub,0) canceled_rub,
          IFNULL(sl.buyouts_qty,0) buyouts_qty, IFNULL(sl.buyouts_rub,0) buyouts_rub,
          IFNULL(sl.sales_for_pay_operational,0) sales_for_pay_operational,
          IFNULL(sl.returns_qty,0) returns_qty, IFNULL(sl.returns_rub,0) returns_rub,
          IFNULL(fn.wb_reward_cost_positive,0) wb_reward_cost_positive,
          IFNULL(fn.logistics_cost_positive,0) logistics_cost_positive,
          IFNULL(fp.marketplace_fee_rub,0) marketplace_fee_rub,
          IFNULL(fp.finance_for_pay_accounting,0) finance_for_pay_accounting
        FROM spine s
        LEFT JOIN ads a  ON a.nm_id=s.nm_id  AND a.d=s.day
        LEFT JOIN ads_billed ab ON ab.nm_id=s.nm_id AND ab.d=s.day
        LEFT JOIN bill_day bd            ON bd.d=s.day
        LEFT JOIN bill_day_universe bu   ON bu.d=s.day
        LEFT JOIN cov cv                 ON cv.d=s.day
        LEFT JOIN ord o  ON o.nm_id=s.nm_id  AND o.d=s.day
        LEFT JOIN sal sl ON sl.nm_id=s.nm_id AND sl.d=s.day
        LEFT JOIN fin fn ON fn.nm_id=s.nm_id AND fn.d=s.day
        LEFT JOIN finpay fp ON fp.nm_id=s.nm_id AND fp.d=s.day),
      rolled AS (
        SELECT *,
          SUM(ad_spend) OVER w7 ad_spend_7d, SUM(ad_spend) OVER w14 ad_spend_14d,
          SUM(ads_revenue_raw_rub) OVER w7 ads_revenue_raw_7d, SUM(ads_revenue_raw_rub) OVER w14 ads_revenue_raw_14d,
          SUM(ads_revenue_dedup_estimate_rub) OVER w7 ads_revenue_dedup_estimate_7d,
          SUM(ads_revenue_dedup_estimate_rub) OVER w14 ads_revenue_dedup_estimate_14d,
          SUM(ad_orders_raw) OVER w7 ad_orders_raw_7d, SUM(ad_orders_raw) OVER w14 ad_orders_raw_14d,
          SUM(ad_orders_dedup_estimate) OVER w7 ad_orders_dedup_estimate_7d,
          SUM(ad_orders_dedup_estimate) OVER w14 ad_orders_dedup_estimate_14d,
          SUM(buyouts_rub) OVER w7 buyouts_rub_7d, SUM(buyouts_rub) OVER w14 buyouts_rub_14d,
          SUM(orders_rub) OVER w7 orders_rub_7d, SUM(orders_rub) OVER w14 orders_rub_14d,
          SUM(orders_qty) OVER w7 orders_qty_7d, SUM(orders_qty) OVER w14 orders_qty_14d
        FROM joined
        WINDOW
          w7  AS (PARTITION BY nm_id ORDER BY UNIX_DATE(day) RANGE BETWEEN 6  PRECEDING AND CURRENT ROW),
          w14 AS (PARTITION BY nm_id ORDER BY UNIX_DATE(day) RANGE BETWEEN 13 PRECEDING AND CURRENT ROW))
      SELECT
        day, nm_id,
        ad_spend, views, clicks, ad_orders_raw, ads_revenue_raw_rub, ads_revenue_dedup_estimate_rub, ad_orders_dedup_estimate,
        -- Stage 3B. ad_spend выше — legacy (deprecated), тождественно attributed; см. ПРАВИЛО 1 в шапке.
        ad_spend AS ad_spend_attributed_rub,
        ad_spend_billed_rub,
        billed_complete,
        -- булево наблюдение вместо денежной суммы: сама сумма — в reconciliation-объектах.
        (unallocated_day_rub_internal > 0)                      AS has_unallocated_billing,
        IF(billed_complete, NOT (unallocated_day_rub_internal > 0), NULL) AS billing_allocation_complete,
        orders_qty, orders_rub, canceled_qty, canceled_rub,
        buyouts_qty, buyouts_rub, sales_for_pay_operational, returns_qty, returns_rub,
        wb_reward_cost_positive, logistics_cost_positive, marketplace_fee_rub, finance_for_pay_accounting,
        SAFE_DIVIDE(clicks, views)                       AS ctr,
        SAFE_DIVIDE(ad_spend, views) * 1000              AS cpm,
        SAFE_DIVIDE(ad_spend, clicks)                    AS cpc,
        SAFE_DIVIDE(ad_spend, ad_orders_raw)             AS cpo_attributed,
        SAFE_DIVIDE(ad_spend, orders_qty)                AS blended_cpo,
        SAFE_DIVIDE(ad_spend, orders_rub)                AS drr_orders,
        SAFE_DIVIDE(ad_spend, buyouts_rub)               AS drr_buyouts,
        SAFE_DIVIDE(ads_revenue_raw_rub, ad_spend)       AS roas,     -- legacy = attributed (deprecated)
        SAFE_DIVIDE(ad_spend, ads_revenue_raw_rub)       AS acos,     -- legacy = attributed (deprecated)
        -- Stage 3B, ПРАВИЛО 4: performance-метрики остаются на АТРИБУЦИИ — только под явными именами.
        SAFE_DIVIDE(ads_revenue_raw_rub, ad_spend)       AS roas_attributed,
        SAFE_DIVIDE(ad_spend, ads_revenue_raw_rub)       AS acos_attributed,
        -- Stage 3B, ПРАВИЛО 3: экономика — на БИЛЛИНГЕ. NULL на неполных сутках, не 0.
        IF(billed_complete, SAFE_DIVIDE(ad_spend_billed_rub, orders_rub),  NULL) AS drr_orders_billed,
        IF(billed_complete, SAFE_DIVIDE(ad_spend_billed_rub, buyouts_rub), NULL) AS drr_buyouts_billed,
        IF(billed_complete, SAFE_DIVIDE(ads_revenue_raw_rub, ad_spend_billed_rub), NULL) AS roas_billed,
        -- PR-B2: из валовой выручки выкупов вычитается РЕАЛЬНЫЙ сбор маркетплейса
        --   (marketplace_fee_rub), а не vw. wb_reward_cost_positive из формулы ИСКЛЮЧЁН:
        --   иначе вознаграждение WB вычиталось бы вторым разом поверх спреда, внутри
        --   которого оно уже сидит. acquiring_rub по той же причине не участвует.
        buyouts_rub - marketplace_fee_rub - logistics_cost_positive - ad_spend
                                                         AS hybrid_day_contribution_pre_cogs,
        -- settlement НЕ трогается: for_pay уже нетто сбора WB.
        finance_for_pay_accounting - ad_spend            AS settlement_day_contribution_pre_cogs,
        -- Stage 3B: те же две формулы, но с фактическим списанием вместо атрибуции.
        --   На сутках без опубликованного биллинга — NULL: подстановка нуля означала бы
        --   «реклама была бесплатной» и завысила бы вклад этих суток.
        IF(billed_complete,
           buyouts_rub - marketplace_fee_rub - logistics_cost_positive - ad_spend_billed_rub,
           NULL)                                         AS hybrid_day_contribution_pre_cogs_billed,
        IF(billed_complete,
           finance_for_pay_accounting - ad_spend_billed_rub,
           NULL)                                         AS settlement_day_contribution_pre_cogs_billed,
        ad_spend_7d, ad_spend_14d, ads_revenue_raw_7d, ads_revenue_raw_14d,
        ads_revenue_dedup_estimate_7d, ads_revenue_dedup_estimate_14d,
        ad_orders_raw_7d, ad_orders_raw_14d, ad_orders_dedup_estimate_7d, ad_orders_dedup_estimate_14d,
        buyouts_rub_7d, buyouts_rub_14d, orders_rub_7d, orders_rub_14d, orders_qty_7d, orders_qty_14d,
        SAFE_DIVIDE(ad_spend_7d,  buyouts_rub_7d)        AS drr_buyouts_7d,
        SAFE_DIVIDE(ad_spend_14d, buyouts_rub_14d)       AS drr_buyouts_14d,
        SAFE_DIVIDE(ads_revenue_raw_7d,  ad_spend_7d)    AS roas_7d,
        SAFE_DIVIDE(ads_revenue_raw_14d, ad_spend_14d)   AS roas_14d,
        SAFE_DIVIDE(ad_spend_7d,  orders_qty_7d)         AS blended_cpo_7d,
        SAFE_DIVIDE(ad_spend_14d, orders_qty_14d)        AS blended_cpo_14d,
        @build_as_of AS build_as_of_date,
        @ads_activity_max_date AS ads_activity_max_date,   -- REV6: build-level, одинаков во всех строках
        @ads_activity_lagged AS ads_activity_lagged,                          -- REV6: build-level, одинаков во всех строках
        @run_id AS mart_run_id, @built_at AS built_at
      FROM rolled
    """ USING v_build_as_of AS build_as_of, v_global_start AS global_start, v_run_id AS run_id, v_built_at AS built_at,
             v_ads_activity_max_date AS ads_activity_max_date, v_ads_activity_lagged AS ads_activity_lagged;

    -- --- ASSERT-гейт на __BUILD ---
    -- fix #2: непустой BUILD.
    ASSERT (SELECT COUNT(*) FROM `wb_mart.MART_SKU_DAILY__BUILD`) > 0
      AS 'SKU_DAILY: пустой BUILD (fail-closed)';
    -- грейн (day, nm_id) уникален + not-null.
    ASSERT (SELECT COUNTIF(day IS NULL OR nm_id IS NULL) FROM `wb_mart.MART_SKU_DAILY__BUILD`) = 0
      AS 'SKU_DAILY: NULL day/nm_id';
    ASSERT (SELECT COUNT(*) = COUNT(DISTINCT FORMAT('%t|%t', day, nm_id)) FROM `wb_mart.MART_SKU_DAILY__BUILD`)
      AS 'SKU_DAILY: грейн (day,nm_id) не уникален';
    -- fix #2: полнота universe — КАЖДЫЙ активный SKU обязан присутствовать в BUILD (ловит отсутствующий nm).
    ASSERT (SELECT COUNT(*) FROM (
              SELECT nm_id FROM `wb_raw.REF_SKU_MASTER`
              WHERE marketplace='WB' AND active=TRUE AND nm_id IS NOT NULL
              EXCEPT DISTINCT
              SELECT nm_id FROM `wb_mart.MART_SKU_DAILY__BUILD`)) = 0
      AS 'SKU_DAILY: активный SKU отсутствует в BUILD (universe неполон)';
    -- плотность spine: у каждого nm число дней == (MAX-MIN+1), пропусков нет.
    ASSERT (SELECT COUNTIF(cnt <> span) FROM (
              SELECT nm_id, COUNT(*) cnt, DATE_DIFF(MAX(day), MIN(day), DAY) + 1 span
              FROM `wb_mart.MART_SKU_DAILY__BUILD` GROUP BY nm_id)) = 0
      AS 'SKU_DAILY: разрывы в dense-spine';
    -- граница: MAX(day) == build_as_of.
    ASSERT (SELECT MAX(day) FROM `wb_mart.MART_SKU_DAILY__BUILD`) = v_build_as_of
      AS 'SKU_DAILY: MAX(day) != build_as_of_date';
    -- REV6: снимок АКТИВНОСТИ рекламы не в будущем относительно build_as_of.
    ASSERT (SELECT COUNTIF(ads_activity_max_date > build_as_of_date) FROM `wb_mart.MART_SKU_DAILY__BUILD`) = 0
      AS 'SKU_DAILY: ads_activity_max_date > build_as_of_date';
    -- REV6: ads_* — build-level МЕТАДАННЫЕ, обязаны быть ЕДИНЫ на всю публикацию (не per-day).
    --   NULL-safe (аудит REV4): COUNT(DISTINCT col) игнорирует NULL и пропустил бы смесь NULL+дата;
    --   TO_JSON_STRING(STRUCT(...)) считает NULL отдельным значением.
    ASSERT (SELECT COUNT(DISTINCT TO_JSON_STRING(STRUCT(ads_activity_max_date AS value)))
            FROM `wb_mart.MART_SKU_DAILY__BUILD`) <= 1
      AS 'SKU_DAILY: ads_activity_max_date не единый снимок (ожидается build-level, NULL-safe)';
    ASSERT (SELECT COUNT(DISTINCT TO_JSON_STRING(STRUCT(ads_activity_lagged AS value)))
            FROM `wb_mart.MART_SKU_DAILY__BUILD`) <= 1
      AS 'SKU_DAILY: ads_activity_lagged не единый снимок (ожидается build-level, NULL-safe)';

    -- fix #3: reconciliation ограничен <= build_as_of (совпадает с bounding source-CTE).
    ASSERT (SELECT ABS((SELECT SUM(ad_spend) FROM `wb_mart.MART_SKU_DAILY__BUILD`)
                     - (SELECT SUM(stats_spend_rub) FROM `wb_mart.FACT_ADS_SKU_DAILY`
                        WHERE `date` <= v_build_as_of
                          AND nm_id IN (SELECT nm_id FROM `wb_raw.REF_SKU_MASTER`
                                        WHERE marketplace='WB' AND active=TRUE AND nm_id IS NOT NULL))) < 0.01)
      AS 'SKU_DAILY: ad_spend != FACT (universe, <=build_as_of)';
    ASSERT (SELECT (SELECT SUM(orders_qty) FROM `wb_mart.MART_SKU_DAILY__BUILD`)
                 = (SELECT COUNTIF(NOT is_cancel) FROM `wb_mart.FACT_ORDERS`
                    WHERE order_date <= v_build_as_of
                      AND nm_id IN (SELECT nm_id FROM `wb_raw.REF_SKU_MASTER`
                                    WHERE marketplace='WB' AND active=TRUE AND nm_id IS NOT NULL)))
      AS 'SKU_DAILY: orders_qty != FACT (universe, <=build_as_of)';
    ASSERT (SELECT ABS((SELECT SUM(buyouts_rub) FROM `wb_mart.MART_SKU_DAILY__BUILD`)
                     - (SELECT SUM(IF(NOT is_return, price_with_disc, 0)) FROM `wb_mart.FACT_SALES`
                        WHERE sale_date <= v_build_as_of
                          AND nm_id IN (SELECT nm_id FROM `wb_raw.REF_SKU_MASTER`
                                        WHERE marketplace='WB' AND active=TRUE AND nm_id IS NOT NULL))) < 0.01)
      AS 'SKU_DAILY: buyouts_rub != FACT (universe, <=build_as_of)';
    ASSERT (SELECT ABS(
              (SELECT SUM(wb_reward_cost_positive + logistics_cost_positive) FROM `wb_mart.MART_SKU_DAILY__BUILD`)
            - (SELECT SUM(cost_amount_positive) FROM `wb_mart.V_WB_FINANCE_AMOUNTS_LONG_MAPPED`
               WHERE is_sku_row AND cost_category IN ('wb_reward','logistics') AND finance_date <= v_build_as_of
                 AND nm_id IN (SELECT nm_id FROM `wb_raw.REF_SKU_MASTER`
                               WHERE marketplace='WB' AND active=TRUE AND nm_id IS NOT NULL))) < 0.01)
      AS 'SKU_DAILY: finance wb_reward+logistics != LONG_MAPPED (universe SKU, <=build_as_of)';
    -- PR-B2: reconciliation для authoritative-сбора. marketplace_fee_rub идёт мимо REF_COST_MAP
    --   и мимо леммы консервации PR-Mart2a §5.2, поэтому нуждается в собственной сверке с источником —
    --   иначе единственная метрика, реально формирующая hybrid-контрибуцию, осталась бы без гейта.
    ASSERT (SELECT ABS(
              (SELECT SUM(marketplace_fee_rub) FROM `wb_mart.MART_SKU_DAILY__BUILD`)
            - (SELECT SUM(marketplace_fee_gap_rub) FROM `wb_mart.FACT_FINANCE`
               WHERE COALESCE(nm_id > 0 AND sku_match_status='matched', FALSE)
                 AND finance_date <= v_build_as_of
                 AND nm_id IN (SELECT nm_id FROM `wb_raw.REF_SKU_MASTER`
                               WHERE marketplace='WB' AND active=TRUE AND nm_id IS NOT NULL))) < 0.01)
      AS 'SKU_DAILY: marketplace_fee_rub != FACT_FINANCE (universe SKU, <=build_as_of)';


    -- ── Stage 3B: гейты рекламного расхода ────────────────────────────────
    -- S1. attributed — ТОЧНЫЙ синоним legacy ad_spend. Проверка построчная, а не по сумме:
    --     совпадение итогов не исключало бы перестановку значений между строками.
    ASSERT (SELECT COUNTIF(ad_spend_attributed_rub <> ad_spend) FROM `wb_mart.MART_SKU_DAILY__BUILD`) = 0
      AS 'SKU_DAILY: ad_spend_attributed_rub != ad_spend (построчно)';
    -- S2. billed — pass-through из FACT_ADS_SPEND_ALLOC_DAILY в тех же границах (universe, <= build_as_of).
    ASSERT (SELECT ABS((SELECT SUM(ad_spend_billed_rub) FROM `wb_mart.MART_SKU_DAILY__BUILD`)
                     - (SELECT IFNULL(SUM(billed_alloc_rub), 0) FROM `wb_mart.FACT_ADS_SPEND_ALLOC_DAILY`
                        WHERE `date` <= v_build_as_of
                          AND nm_id IN (SELECT nm_id FROM `wb_raw.REF_SKU_MASTER`
                                        WHERE marketplace='WB' AND active=TRUE AND nm_id IS NOT NULL))) < 0.01)
      AS 'SKU_DAILY: ad_spend_billed_rub != FACT_ADS_SPEND_ALLOC_DAILY (universe, <=build_as_of)';
    -- S3. Сутки с биллингом ОБЯЗАНЫ присутствовать в витрине — иначе их unallocated просто исчез бы
    --     из всякой агрегации, и недостача не была бы видна ни в одной строке.
    ASSERT (SELECT COUNT(*) FROM (
              SELECT `date` FROM `wb_mart.FACT_ADS_COSTS_DAILY` WHERE `date` <= v_build_as_of
              EXCEPT DISTINCT
              SELECT day FROM `wb_mart.MART_SKU_DAILY__BUILD`)) = 0
      AS 'SKU_DAILY: сутки с биллингом отсутствуют в витрине (unallocated был бы потерян)';
    -- S4. Витрина несёт РОВНО ту часть биллинга, что распределена на SKU её universe —
    --     ни рублём больше, ни рублём меньше. Посуточно, а не только в итоге.
    ASSERT (SELECT COUNT(*) FROM (
              SELECT day, SUM(ad_spend_billed_rub) billed_sum
              FROM `wb_mart.MART_SKU_DAILY__BUILD` GROUP BY day) x
            LEFT JOIN (
              SELECT `date` d, SUM(billed_alloc_rub) alloc
              FROM `wb_mart.FACT_ADS_SPEND_ALLOC_DAILY`
              WHERE `date` <= v_build_as_of
                AND nm_id IN (SELECT nm_id FROM `wb_raw.REF_SKU_MASTER`
                              WHERE marketplace='WB' AND active=TRUE AND nm_id IS NOT NULL)
              GROUP BY `date`) a
              ON a.d = x.day
            WHERE ABS(x.billed_sum - IFNULL(a.alloc, 0)) > 0.005) = 0
      AS 'SKU_DAILY: посуточная SUM(billed) != FACT alloc (universe)';
    -- S5. Три day-level колонки обязаны быть ЕДИНЫ внутри суток (иначе ANY_VALUE в S4 лгал бы).
    --     NULL-safe через TO_JSON_STRING(STRUCT(...)) — COUNT(DISTINCT) сам по себе игнорирует NULL.
    ASSERT (SELECT COUNT(*) FROM (
              SELECT day FROM `wb_mart.MART_SKU_DAILY__BUILD` GROUP BY day
              HAVING COUNT(DISTINCT TO_JSON_STRING(STRUCT(has_unallocated_billing AS v))) > 1
                  OR COUNT(DISTINCT TO_JSON_STRING(STRUCT(billed_complete AS v))) > 1
                  OR COUNT(DISTINCT TO_JSON_STRING(STRUCT(billing_allocation_complete AS v))) > 1)) = 0
      AS 'SKU_DAILY: day-level колонки биллинга не едины внутри суток';
    -- S6. billed_complete — ТОЧНАЯ копия coverage-контракта, без собственной логики витрины.
    --     Отсутствие строки покрытия обязано читаться как FALSE, а не как «наверное, да».
    ASSERT (SELECT COUNT(*) FROM (
              SELECT day, ANY_VALUE(billed_complete) flag
              FROM `wb_mart.MART_SKU_DAILY__BUILD` GROUP BY day) x
            LEFT JOIN `wb_raw.V_ADV_COSTS_DAY_COVERAGE` c ON c.`date` = x.day
            WHERE x.flag <> IFNULL(c.billed_complete, FALSE)) = 0
      AS 'SKU_DAILY: billed_complete != V_ADV_COSTS_DAY_COVERAGE (fail-closed)';
    -- S7. billing_allocation_complete — ДРУГОЙ вопрос: весь ли опубликованный биллинг дошёл до SKU.
    --     Определён только там, где биллинг вообще опубликован.
    ASSERT (SELECT COUNTIF(billed_complete AND (billing_allocation_complete IS NULL
                     OR billing_allocation_complete <> (NOT has_unallocated_billing)))
            FROM `wb_mart.MART_SKU_DAILY__BUILD`) = 0
      AS 'SKU_DAILY: billing_allocation_complete != NOT has_unallocated_billing на полных сутках';
    -- S8. На неполных сутках billed-KPI строго NULL (ноль означал бы «рекламы не было»).
    ASSERT (SELECT COUNTIF(NOT billed_complete AND (
                     drr_orders_billed  IS NOT NULL
                  OR drr_buyouts_billed IS NOT NULL
                  OR roas_billed        IS NOT NULL
                  OR hybrid_day_contribution_pre_cogs_billed     IS NOT NULL
                  OR settlement_day_contribution_pre_cogs_billed IS NOT NULL
                  OR billing_allocation_complete IS NOT NULL))
            FROM `wb_mart.MART_SKU_DAILY__BUILD`) = 0
      AS 'SKU_DAILY: billed-KPI не NULL на сутках с неопубликованным биллингом';
    -- S9. Витрина не может нести БОЛЬШЕ, чем WB списал за сутки — это были бы выдуманные деньги.
    ASSERT (SELECT COUNT(*) FROM (
              SELECT day, SUM(ad_spend_billed_rub) billed_sum
              FROM `wb_mart.MART_SKU_DAILY__BUILD` GROUP BY day) x
            LEFT JOIN (
              SELECT `date` d, SUM(actual_spend_rub) act
              FROM `wb_mart.FACT_ADS_COSTS_DAILY` WHERE `date` <= v_build_as_of GROUP BY `date`) c
              ON c.d = x.day
            WHERE x.billed_sum - IFNULL(c.act, 0) > 0.005) = 0
      AS 'SKU_DAILY: SUM(billed) по суткам превышает списание WB';
    -- S10. Булев флаг обязан соответствовать факту: остаток суток = списание − несомое витриной.
    --     Без этого гейта флаг мог бы разойтись с reconciliation, и «всё распределено» стало бы
    --     утверждением ни на чём не основанным.
    ASSERT (SELECT COUNT(*) FROM (
              SELECT day, SUM(ad_spend_billed_rub) billed_sum,
                     ANY_VALUE(has_unallocated_billing) flag
              FROM `wb_mart.MART_SKU_DAILY__BUILD` GROUP BY day) x
            LEFT JOIN (
              SELECT `date` d, SUM(actual_spend_rub) act
              FROM `wb_mart.FACT_ADS_COSTS_DAILY` WHERE `date` <= v_build_as_of GROUP BY `date`) c
              ON c.d = x.day
            WHERE x.flag <> (IFNULL(c.act, 0) - x.billed_sum > 0)) = 0
      AS 'SKU_DAILY: has_unallocated_billing не соответствует остатку суток';

    -- --- publish ---
    CREATE OR REPLACE TABLE `wb_mart.MART_SKU_DAILY`
      PARTITION BY day CLUSTER BY nm_id AS
      SELECT * FROM `wb_mart.MART_SKU_DAILY__BUILD`;

    ASSERT (SELECT COUNTIF(is_partitioning_column='YES') FROM `wb_mart.INFORMATION_SCHEMA.COLUMNS`
            WHERE table_name='MART_SKU_DAILY') = 1 AS 'SKU_DAILY: partition != 1';
    ASSERT (SELECT COUNTIF(clustering_ordinal_position IS NOT NULL) FROM `wb_mart.INFORMATION_SCHEMA.COLUMNS`
            WHERE table_name='MART_SKU_DAILY') = 1 AS 'SKU_DAILY: cluster != 1';

    DROP TABLE IF EXISTS `wb_mart.MART_SKU_DAILY__BUILD`;

    UPDATE `wb_mart._MART_BOOTSTRAP_LOCK`
       SET is_running=FALSE, run_id=NULL, last_run_id=v_run_id, released_at=CURRENT_TIMESTAMP()
     WHERE lock_id='mart_sku_daily';

  EXCEPTION WHEN ERROR THEN
    UPDATE `wb_mart._MART_BOOTSTRAP_LOCK`
       SET is_running=FALSE, run_id=NULL, last_run_id=v_run_id, released_at=CURRENT_TIMESTAMP()
     WHERE lock_id='mart_sku_daily';
    RAISE USING MESSAGE = FORMAT('sp_build_mart_sku_daily FAILED: %s', @@error.message);
  END;
END;

-- Ручной прогон (владелец, после APPROVE + merge). REV6: третий аргумент — in_run_id ('' → UUID):
--   CALL `wb_mart.sp_build_mart_sku_daily`(DATE_SUB(CURRENT_DATE('Europe/Moscow'), INTERVAL 1 DAY), NULL, '');
-- В проде loader mart зовёт: CALL sp_bootstrap_facts(@run_id); CALL sp_build_mart_sku_daily(@target_date, NULL, @run_id);
