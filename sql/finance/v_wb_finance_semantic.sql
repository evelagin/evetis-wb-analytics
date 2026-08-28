-- ============================================================================
-- PR-B · V_WB_FINANCE_SEMANTIC — семантический слой финансового контура
--
-- Назначение: привести поколения WB API (legacy до 12.07.2026 и новый контур
--   с 13.07.2026) к ОДНОМУ финансовому контракту, вычисляя производные поля
--   в SQL, а не при загрузке. RAW остаётся тем, что реально прислал WB.
--
-- Почему в SQL, а не в маппере: RAW append-only, поэтому правка
--   finDailyMapRow_ заполнила бы поля только для будущих строк, а 2 922 уже
--   загруженные остались бы с NULL навсегда. Здесь история восстанавливается
--   ретроактивно и лечится сама при очередной полной сборке wb-mart-prod.
--
-- Источник восстановления для нового контура — колонка raw_json: маппер
--   сохраняет весь detailed-объект WB строкой (finDailyMapRow_: raw_json).
--   Проверено: заполнена у 2 922 из 2 922 строк нового контура.
--
-- PR-B2 (13.08.2026): добавлено ОДНО поле — marketplace_fee_gap_rub,
--   authoritative сбор WB. Изменение аддитивное: ни одно существующее поле
--   слоя PR-B не тронуто, типы и содержимое прежние. Переименование
--   cost_category 'commission' → 'wb_reward' выполняется в
--   sql/mart/pr_mart2a_finance_longform.sql, перевод hybrid-контрибуции
--   на fee_gap — в sql/mart/pr_mart2b_sku_daily.sql.
--
-- РЕЖИМ СОЗДАНИЯ: CREATE OR REPLACE VIEW — данные не пишутся.
-- Спека: docs/FINANCE_PR_B_NORMALIZATION_2026-08-13.md (ред. 3)
-- Эталон семантики: артефакт REV2.4 от 11.08.2026 (контракт v1, APPROVED)
-- ============================================================================

CREATE OR REPLACE VIEW `wb_raw.V_WB_FINANCE_SEMANTIC` AS
WITH
-- ── Справочник SKU: три пути разрешения, как в legacy buildSkuIndex_ ───────
--    (byNm / byBarcode / byVendor, apps-script/Wbfinanceimportfromdrive)
ref AS (
  SELECT nm_id, internal_sku,
         TRIM(IFNULL(barcode, ''))                  AS bc,
         UPPER(TRIM(IFNULL(wb_vendor_code, '')))    AS vc
  FROM `wb_raw.REF_SKU_MASTER`
),
by_nm AS (SELECT nm_id, ANY_VALUE(internal_sku) sku FROM ref GROUP BY nm_id),
by_bc AS (SELECT bc,    ANY_VALUE(internal_sku) sku FROM ref WHERE bc <> '' GROUP BY bc),
by_vc AS (SELECT vc,    ANY_VALUE(internal_sku) sku FROM ref WHERE vc <> '' GROUP BY vc),

src AS (
  SELECT c.*,
    -- ⚠️ КОНТРАКТ РАЗРЕШЕНИЯ SKU. Порядок: nm_id, НАЙДЕННЫЙ В СПРАВОЧНИКЕ →
    --    barcode → vendor code. Нельзя заменять простым COALESCE по nm_id:
    --    WB проставляет на строках возмещений служебный wb_nm_id (например
    --    99866376), которого в справочнике нет, а реальный товар опознаётся
    --    по barcode — один и тот же такой nm_id в legacy разрешён в три
    --    разных SKU. При обратном порядке расходятся 184 строки.
    --    Эмпирическое доказательство: 201 211 / 201 211 совпадений с legacy.
    COALESCE(n.sku, b.sku, v.sku) AS resolved_internal_sku,
    SAFE_CAST(REPLACE(c.commission_percent, ',', '.') AS NUMERIC) AS pct_num
  FROM `wb_raw.V_WB_FINANCE_CANONICAL` c
  LEFT JOIN by_nm n ON n.nm_id = SAFE_CAST(c.wb_nm_id AS INT64)
  LEFT JOIN by_bc b ON b.bc    = TRIM(IFNULL(c.barcode, ''))
  LEFT JOIN by_vc v ON v.vc    = UPPER(TRIM(COALESCE(NULLIF(c.sa_name, ''), c.wb_vendor_code, '')))
)

SELECT
  s.* EXCEPT (operation_type_normalized, internal_sku, sku_match_status,
              commission_amount, resolved_internal_sku, pct_num),

  -- ── 0. Основание платежа от WB (Stage 3.1F) ─────────────────────────────
  --    WB присылает человекочитаемое описание операции в bonusTypeName.
  --    Поле выводится здесь ОДИН раз, чтобы потребители не дублировали разбор
  --    JSON. Нормализация сознательно консервативная: TRIM, схлопывание
  --    пробельных последовательностей и приведение пустой строки к NULL.
  --    Смысл текста не меняется: ни перевода, ни обрезки, ни склейки категорий.
  --    🔴 Поле заполнено НЕ везде: WB начал его передавать 2026-07-19,
  --    до этой даты оно пусто на 100 % строк «Удержание». NULL здесь означает
  --    «WB не сообщил основание», а не «оснований нет».
  NULLIF(TRIM(REGEXP_REPLACE(IFNULL(JSON_VALUE(s.raw_json, '$.bonusTypeName'), ''),
                             r'\s+', ' ')), '')                AS bonus_type_name,

  -- ── 1. Тип операции ─────────────────────────────────────────────────────
  --    На legacy это НЕ нормализация, а копия supplier_oper_name:
  --    18 различных значений, каждое отображается само в себя, 201 211/201 211.
  COALESCE(s.operation_type_normalized, s.supplier_oper_name)
    AS operation_type_normalized,

  -- ── 2. Привязка к товару ────────────────────────────────────────────────
  --    Ретроспективная эквивалентность на legacy: 201 211/201 211 по обоим полям.
  COALESCE(s.internal_sku, s.resolved_internal_sku) AS internal_sku,
  COALESCE(s.sku_match_status,
           IF(s.resolved_internal_sku IS NULL, 'not_found', 'matched')) AS sku_match_status,

  -- ── 3. commission_amount — ТОЛЬКО compatibility-поле ────────────────────
  --    Историческое содержимое: ppvz_vw ← поле API `vw` (WbFinanceApiV1.gs:263,
  --    карта Wbfinanceloader:137). Это вознаграждение WB по операции,
  --    а НЕ комиссия маркетплейса. Знак не инвертируется: в нормализаторе нет
  --    ни одного умножения на −1, отрицательные значения пришли от WB.
  --    Тип STRING и сырое содержимое сохраняются буквально — без промежуточной
  --    конверсии через FLOAT64, чтобы не вносить floating-point артефакты
  --    в raw-compatible поле. НЕ показывать пользователю как «комиссию».
  COALESCE(s.commission_amount, JSON_VALUE(s.raw_json, '$.vw')) AS commission_amount,

  -- ── 4. Честные семантические метрики ────────────────────────────────────

  --    4.1 ⭐ AUTHORITATIVE сбор маркетплейса (PR-B2).
  --        Спред между retail_price_withdisc_rub (ценой продавца / базой
  --        расчёта WB) и for_pay (суммой к перечислению продавцу).
  --        ⚠️ Терминология REV2.4: retail_price_withdisc_rub — это НЕ сумма,
  --        фактически уплаченная покупателем. Разницу закрывает СПП, которую
  --        платит WB, поэтому покупатель платит меньше базы расчёта. Называть
  --        эту базу «ценой покупателя» нельзя — именно такая подмена понятий
  --        и породила PR-B2 (docs/FINANCE_PR_B2_METRICS_2026-08-13.md §9.6).
  --        Именно эта величина — реальный расход на маркетплейс, и именно она
  --        вычитается из hybrid-контрибуции в MART_SKU_DAILY
  --        (см. pr_mart2b_sku_daily.sql).
  --        Считается ТОЛЬКО из источниковых полей WB — без справочников и без
  --        commission_percent, — поэтому остаётся корректной и в окне аномалии
  --        масштаба ставки 25.10–11.11.2024, где 4.3 недостоверна.
  --        Тождество REV2.4: fee_gap ≡ commission_native_rub + acquiring_rub + residual.
  --        ⚠️ Определена только для «Продажа»/«Возврат» — операций, у которых
  --        существует пара (база реализации продавца × выплата продавцу). Для прочих
  --        операций спреда не существует, поэтому NULL, а не 0.
  --        Измерено 13.08.2026: 39 212 строк «Продажа»/«Возврат», у всех обе
  --        части непусты и парсятся (0 NULL), спред положителен у всех строк.
  --        «Возврат» — 11 строк за всю историю, +2 029,68 ₽: WB отдаёт по ним
  --        srev и for_pay положительными, спред тоже положителен и трактуется
  --        как расход. Это согласовано с уже действующим правилом REF_COST_MAP
  --        («Возврат» × commission_amount → COST) и материальности не имеет.
  IF(s.supplier_oper_name IN ('Продажа', 'Возврат'),
     ROUND(SAFE_CAST(REPLACE(s.retail_price_withdisc_rub, ',', '.') AS NUMERIC)
           - SAFE_CAST(REPLACE(s.for_pay, ',', '.') AS NUMERIC), 2),
     NULL) AS marketplace_fee_gap_rub,

  --    4.2 Вознаграждение WB под своим именем.
  --        Отдельная сущность, НЕ сбор маркетплейса: с PR-B2 попадает
  --        в MART_SKU_DAILY как wb_reward_cost_positive и в формулу
  --        контрибуции больше не входит.
  COALESCE(SAFE_CAST(REPLACE(s.commission_amount, ',', '.') AS NUMERIC),
           SAFE_CAST(JSON_VALUE(s.raw_json, '$.vw') AS NUMERIC))
    AS wb_reward_rub,

  --    4.3 Native-комиссия по модели REV2.4 — СПРАВОЧНАЯ метрика,
  --        ДЕКОМПОЗИЦИЯ спреда 4.1, а не отдельный расход.
  --        Тождество: srev − for_pay ≡ srev × commission_percent/100 + acquiring_fee.
  --        ⚠️ НЕ использовать как authoritative expense в P&L: в окне
  --        25.10–11.11.2024 WB отдавал commission_percent долей (0.22–0.23),
  --        а не процентом, и native-ставка там недостоверна (REV2.4).
  --        Authoritative-расход — marketplace_fee_gap_rub (4.1).
  IF(s.supplier_oper_name IN ('Продажа', 'Возврат'),
     ROUND(SAFE_CAST(REPLACE(s.retail_price_withdisc_rub, ',', '.') AS NUMERIC)
           * s.pct_num / 100, 2),
     NULL) AS commission_native_rub,

  --    4.4 Quality-контракт для 4.3. Детектор размерности сделан по данным,
  --        а не по датам: срабатывает сам, если WB повторит смену масштаба.
  --        Проверено на всей истории (39 212 строк «Продажа»/«Возврат»):
  --        дробных ставок 1 082, ровно 2024-10-25 … 2024-11-11,
  --        вне окна — 0, процентных внутри окна — 0. То есть предикат
  --        и окно REV2.4 совпадают в точности.
  CASE
    WHEN s.supplier_oper_name NOT IN ('Продажа', 'Возврат') THEN NULL
    WHEN s.pct_num IS NULL OR s.pct_num = 0                 THEN 'NATIVE_RATE_MISSING'
    WHEN s.pct_num > 0 AND s.pct_num < 1                    THEN 'NATIVE_RATE_SCALE_FRACTION'
    ELSE 'OK'
  END AS commission_native_status,

  --    4.5 Эквайринг — отдельная метрика для аналитики.
  --        ⚠️ УЖЕ ВХОДИТ в спред 4.1 вместе с комиссией. Это компонент
  --        декомпозиции, а не дополнительный расход: вычитать его из маржи
  --        вторым разом нельзя. PR-B2 фиксирует это контрактом — acquiring_rub
  --        не участвует ни в одной формуле контрибуции MART_SKU_DAILY.
  SAFE_CAST(REPLACE(s.acquiring_fee, ',', '.') AS NUMERIC) AS acquiring_rub,

  -- ── 5. Происхождение значения — для диагностики и QC ────────────────────
  IF(s.operation_type_normalized IS NULL, 'DERIVED_SQL', 'STORED') AS semantic_origin

FROM src s;
