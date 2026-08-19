-- ═══════════════════════════════════════════════════════════════
-- EVETIS WB — вьюхи слоя поставок
-- Проект: docs/wb_supplies_raw_design.md, §5
-- Дата: 19.08.2026. Статус: на ревью, не выполнялось.
--
-- РАЗМЕЩЕНИЕ по конвенции проекта:
--   wb_raw   — канонический слой над RAW (как V_WB_ORDERS, V_WB_STOCKS_CURRENT)
--   wb_mart  — витрины для Looker (как V_ADS_*, V_MART_*)
--
-- 🔴 ДВА ПРАВИЛА, КОТОРЫЕ НЕЛЬЗЯ НАРУШАТЬ
--
-- 1. Позиции читаются ТОЛЬКО того run_id, который выбрала шапка.
--    Самостоятельный ROW_NUMBER() по (entity_key, barcode) в goods-вью
--    запрещён: при удалении позиции из состава новой строки, которая
--    «перебила» бы старую, не существует, и удалённая позиция осталась бы
--    в текущем слое навсегда, завышая SUM(quantity). См. §5.2 проекта.
--
-- 2. Исторический ввоз считается по goods.quantity.
--    accepted_quantity — диагностика. Ноль в нём не означает «не принято»:
--    до 20.08.2025 поле массово не заполнялось, а две поставки февраля 2026
--    документально приняты как «неопознанный товар». См. §13.1
--    docs/wb_incomes_api_retirement.md.
--
-- КОНТРОЛЬНЫЕ ЦИФРЫ ПОСЛЕ СОЗДАНИЯ — в конце файла.
-- ═══════════════════════════════════════════════════════════════


-- ───────────────────────────────────────────────────────────────
-- 1. wb_raw.V_WB_SUPPLIES_CURRENT
--    Одна актуальная committed-шапка на entity_key.
--    Она же определяет, ИЗ КАКОГО run_id читать позиции.
-- ───────────────────────────────────────────────────────────────
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_SUPPLIES_CURRENT` AS
SELECT * EXCEPT(rn)
FROM (
  SELECT
    *,
    ROW_NUMBER() OVER (
      PARTITION BY entity_key
      ORDER BY snapshot_ts DESC, run_id DESC
    ) AS rn
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_SUPPLIES`
)
WHERE rn = 1;


-- ───────────────────────────────────────────────────────────────
-- 2. wb_raw.V_WB_SUPPLIES_GOODS_CURRENT
--    Позиции ТОГО ЖЕ прогона, что и current-шапка. Своего выбора
--    по времени или barcode не делает — см. правило 1 в шапке файла.
-- ───────────────────────────────────────────────────────────────
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_SUPPLIES_GOODS_CURRENT` AS
SELECT g.*
FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_SUPPLIES_GOODS` g
JOIN `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_SUPPLIES_CURRENT` s
  ON  g.entity_key = s.entity_key
 AND  g.run_id     = s.run_id;


-- ───────────────────────────────────────────────────────────────
-- 3. wb_mart.V_WB_SUPPLIES_DETAIL
--    Грейн: поставка × SKU. ТОЛЬКО состоявшиеся поставки.
--
--    🔑 Фильтр `is_completed` живёт ЗДЕСЬ и только здесь. Витрина
--    V_WB_SUPPLIES_INTAKE_BY_SKU строится поверх этой вью, поэтому
--    определение «привезено» физически не может разъехаться между
--    детальным и агрегатным слоями.
--
--    Незавершённые заявки при необходимости смотреть в
--    wb_raw.V_WB_SUPPLIES_CURRENT — там они есть.
-- ───────────────────────────────────────────────────────────────
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_SUPPLIES_DETAIL` AS
SELECT
  s.entity_key,
  s.supply_id,
  s.preorder_id,
  DATE(s.fact_dt)                 AS fact_date,
  s.fact_dt,
  s.supply_dt,
  s.create_dt,
  s.status_id,

  -- Склады: три РАЗНЫХ поля, не взаимозаменяемые.
  s.warehouse_id                  AS dest_warehouse_id,
  s.warehouse_name                AS dest_warehouse_name,   -- 🔑 куда шла поставка
  s.transit_warehouse_id,
  s.transit_warehouse_name,                                 -- сортировочный центр
  s.actual_warehouse_id,
  s.actual_warehouse_name,                                  -- 🔴 точка сдачи коробов
  (s.transit_warehouse_id IS NOT NULL)                      AS via_transit,
  CASE
    WHEN s.actual_warehouse_id IS NULL THEN 'NONE'
    WHEN s.actual_warehouse_id = s.transit_warehouse_id THEN 'TRANSIT'
    WHEN s.actual_warehouse_id = s.warehouse_id         THEN 'DESTINATION'
    ELSE 'OTHER'
  END                                                       AS actual_matches,

  -- Экономика поставки
  s.acceptance_cost,
  s.paid_acceptance_coefficient,
  s.storage_coef,
  s.delivery_coef,
  s.box_type_id,
  s.is_box_on_pallet,

  -- Позиция
  g.nm_id,
  g.barcode,
  g.vendor_code,
  g.tech_size,
  g.quantity,                                               -- 🔑 привезено на WB
  g.accepted_quantity,                                      -- диагностика
  g.ready_for_sale_quantity,
  g.unloading_quantity,
  g.accepted_quantity_zero,
  g.acceptance_resolution,

  -- Качество источника
  s.card_read_ok,
  s.run_id,
  s.snapshot_ts
FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_SUPPLIES_GOODS_CURRENT` g
JOIN `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_SUPPLIES_CURRENT`       s
  ON  g.entity_key = s.entity_key
 AND  g.run_id     = s.run_id
WHERE s.is_completed;


-- ───────────────────────────────────────────────────────────────
-- 4. wb_mart.V_WB_SUPPLIES_INTAKE_BY_SKU
--    Грейн: nm_id. Ответ на вопрос «сколько каждого товара завезено
--    на WB с сентября 2024».
--
--    🔴 barcode как ЗНАЧЕНИЕ здесь отсутствует намеренно: при GROUP BY
--    nm_id конструкция ANY_VALUE(barcode) выдала бы случайный
--    исторический штрихкод, выглядящий каноническим. Вместо него —
--    счётчик barcodes_cnt: значение больше единицы это сигнал
--    разобраться, а не молча подставленное число.
-- ───────────────────────────────────────────────────────────────
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_SUPPLIES_INTAKE_BY_SKU` AS
SELECT
  nm_id,
  ANY_VALUE(vendor_code)          AS vendor_code,
  COUNT(DISTINCT barcode)         AS barcodes_cnt,          -- контроль, не значение
  COUNT(DISTINCT entity_key)      AS supplies_cnt,
  MIN(fact_date)                  AS first_supply_date,
  MAX(fact_date)                  AS last_supply_date,
  SUM(quantity)                   AS qty_sent,              -- 🔑 привезено на WB
  SUM(accepted_quantity)          AS qty_accepted_diag,     -- 🔴 НЕ исторический ввоз
  SUM(quantity) - SUM(accepted_quantity) AS qty_delta_diag,
  COUNT(DISTINCT dest_warehouse_name)     AS warehouses_cnt
FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_SUPPLIES_DETAIL`
GROUP BY nm_id;


-- ═══════════════════════════════════════════════════════════════
-- ПРОВЕРКА ПОСЛЕ СОЗДАНИЯ. Ожидаемые значения — в комментариях.
-- ═══════════════════════════════════════════════════════════════

-- П1. Витрина по SKU: 24 строки, 44 788 единиц.
SELECT COUNT(*) AS sku_cnt, SUM(qty_sent) AS qty_total
FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_SUPPLIES_INTAKE_BY_SKU`;
-- ожидается: sku_cnt = 24, qty_total = 44788

-- П2. Детальный слой: 108 состоявшихся поставок, та же сумма.
SELECT COUNT(DISTINCT entity_key) AS supplies, SUM(quantity) AS qty_total
FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_SUPPLIES_DETAIL`;
-- ожидается: supplies = 108, qty_total = 44788

-- П3. Канонический слой шапок: 131 объект, из них 108 состоявшихся.
SELECT COUNT(*) AS objects, COUNTIF(is_completed) AS completed
FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_SUPPLIES_CURRENT`;
-- ожидается: objects = 131, completed = 108

-- П4. 🔴 Один объект — один run_id. Если больше нуля, вью склеивает снимки.
SELECT COUNT(*) AS mixed_runs
FROM (
  SELECT g.entity_key
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_SUPPLIES_GOODS_CURRENT` g
  GROUP BY g.entity_key
  HAVING COUNT(DISTINCT g.run_id) > 1
);
-- ожидается: 0

-- П5. Поведение actual на всей выборке.
SELECT actual_matches, COUNT(DISTINCT entity_key) AS supplies
FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_SUPPLIES_DETAIL`
GROUP BY actual_matches;
-- ожидается: DESTINATION, TRANSIT и NONE (пустой actual у части состоявшихся
-- поставок — такие встречались, см. §12.3 проекта).
-- 🔴 Значения OTHER быть НЕ должно: это означало бы, что actual указывает на
-- склад, не совпадающий ни с транзитом, ни с назначением — правило, которое за
-- всю историю не нарушалось ни разу.

-- П6. Сверка шести июльских поставок с памятью владельца.
SELECT supply_id, fact_date, dest_warehouse_name, transit_warehouse_name,
       COUNT(*) AS positions, SUM(quantity) AS qty, SUM(accepted_quantity) AS accepted
FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_SUPPLIES_DETAIL`
WHERE fact_date BETWEEN '2026-07-01' AND '2026-07-31'
GROUP BY supply_id, fact_date, dest_warehouse_name, transit_warehouse_name
ORDER BY fact_date, supply_id;
-- ожидается 6 строк: Тула 40, Волгоград 165, Владимир 165 (accepted 164),
-- Сарапул 165, Екатеринбург 170 (accepted 169), Новосемейкино 165
