-- ═══════════════════════════════════════════════════════════════
-- EVETIS WB — слой потребления поставок, v1 (четыре вью)
-- Проект: docs/wb_supplies_raw_design.md §5
-- Источник: wb_raw.RAW_WB_SUPPLIES / RAW_WB_SUPPLIES_GOODS
--           (бэкфилл BF_20260819_000452 принят как baseline)
--
-- Дата: 19.08.2026. Статус: НА РЕВЬЮ. Выкат — после APPROVE.
-- Заменяет черновик docs/wb_supplies_views.sql.
--
-- РАЗМЕЩЕНИЕ по конвенции проекта:
--   wb_raw   — канонический слой над RAW (как V_WB_ORDERS, V_WB_STOCKS_CURRENT)
--   wb_mart  — витрины для Looker (как V_ADS_*, V_MART_*)
--
-- 🔴 ТРИ ПРАВИЛА, КОТОРЫЕ НЕЛЬЗЯ НАРУШАТЬ
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
-- 3. Фильтр is_completed живёт РОВНО В ОДНОМ МЕСТЕ — в V_WB_SUPPLIES_DETAIL.
--    Агрегат строится поверх детали, поэтому определение «привезено»
--    физически не может разъехаться между слоями.
--
-- В конце файла: П1–П3 — baseline acceptance на 19.08.2026 (разовая),
-- П4–П7 — структурные гейты (ожидание 0 при любом объёме данных).
-- ═══════════════════════════════════════════════════════════════


-- ───────────────────────────────────────────────────────────────
-- 1. wb_raw.V_WB_SUPPLIES_CURRENT
--    Грейн: entity_key. По одной шапке на объект — из ПОСЛЕДНЕГО
--    прогона, который этот объект записал.
--
--    ⚠️ Вью НЕ фильтрует по is_completed: она отдаёт все 131 объект,
--    включая незавершённые заявки. Отбор состоявшихся — правило 3,
--    он живёт в V_WB_SUPPLIES_DETAIL. Здесь «current» означает
--    «актуальная версия объекта», а не «состоявшаяся поставка».
--
--    🔑 Шапка — commit-marker объекта: загрузчик пишет goods, и только
--    потом header (wbsWriteObject_). Поэтому «последняя шапка» всегда
--    указывает на прогон, чьи позиции уже лежат в таблице.
--
--    ⚠️ Тай-брейк run_id DESC работает только при равном snapshot_ts.
--    Сегодня префиксы упорядочены удачно (DL_ > BF_), но повторный
--    бэкфилл в ту же секунду, что суточный прогон, дал бы неверный
--    выбор. 🔴 Ни один гейт этого файла такого случая НЕ ловит —
--    почему именно, разобрано в примечании к П4.
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
--    Грейн: (entity_key, barcode). Позиции ТОГО ЖЕ прогона, что и
--    current-шапка. Своего выбора по времени или barcode не делает —
--    см. правило 1 в шапке файла.
--
--    JOIN намеренно INNER: объект без шапки не существует.
--    Обратный случай — шапка без позиций — это дыра в данных, а не
--    норма; её ловит гейт П7, а не молчаливый LEFT JOIN.
-- ───────────────────────────────────────────────────────────────
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_SUPPLIES_GOODS_CURRENT` AS
SELECT g.*
FROM `project-fa311fc0-4d87-4781-986.wb_raw.RAW_WB_SUPPLIES_GOODS` g
JOIN `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_SUPPLIES_CURRENT` s
  ON  g.entity_key = s.entity_key
 AND  g.run_id     = s.run_id;


-- ───────────────────────────────────────────────────────────────
-- 3. wb_mart.V_WB_SUPPLIES_DETAIL
--    Рабочая детальная витрина. ТОЛЬКО состоявшиеся поставки.
--
--    🔑 Грейн по построению — (entity_key, barcode), а не
--    (entity_key, nm_id). На истории 19.08.2026 они совпадают
--    (0 nm_id с двумя штрихкодами), но это свойство ДАННЫХ, а не
--    конструкции: набор и его компонент теоретически могут прийти
--    под разными баркодами с одним nm_id. Потребителю, которому
--    нужен ровно «поставка × SKU», агрегировать самому либо брать
--    V_WB_SUPPLIES_INTAKE_BY_SKU. Гейт П5 держит это утверждение
--    под наблюдением.
--
--    Незавершённые заявки смотреть в wb_raw.V_WB_SUPPLIES_CURRENT.
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
--
--    То же возражение относится и к vendor_code, поэтому рядом с
--    ANY_VALUE(vendor_code) стоит vendor_codes_cnt. Сегодня он равен
--    единице у всех 24 SKU, то есть ANY_VALUE ничего не выбирает;
--    как только он станет >1, значение перестанет быть каноническим,
--    и это будет видно в самой витрине, а не в чужой голове.
--
--    🔴 NULL-семантика счётчиков. Голый COUNT(DISTINCT x) игнорирует
--    NULL, поэтому набор {NULL, 'EVT-…'} дал бы ровно 1 и выглядел бы
--    однозначным — то есть счётчик молчал бы в единственном случае,
--    ради которого он и поставлен. Оба счётчика считаются через
--    FORMAT('%t', x), где NULL становится отдельным различимым
--    значением 'NULL'. Тот же приём и по той же причине уже применён
--    в ASSERT-ах FACT_STOCKS_SNAPSHOT (CHANGELOG 17.08, ред. 2).
--    Рядом — сырые COUNTIF(... IS NULL): счётчик отвечает «однозначно
--    ли значение», диагностика отвечает «есть ли вообще пропуски».
--    Факт 19.08: 497 строк детали, NULL и пустых строк нет ни в
--    vendor_code, ни в barcode; NULL-safe счётчики совпали с наивными.
--
--    🔴 qty_delta_diag = SUM(quantity) − SUM(accepted_quantity) корректна
--    только потому, что accepted_quantity не содержит NULL (проверено,
--    гейт П6). SUM игнорирует NULL, и при их появлении разность начала
--    бы считаться по разным множествам строк.
-- ───────────────────────────────────────────────────────────────
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_SUPPLIES_INTAKE_BY_SKU` AS
SELECT
  nm_id,
  ANY_VALUE(vendor_code)                    AS vendor_code,
  COUNT(DISTINCT FORMAT('%t', vendor_code)) AS vendor_codes_cnt,   -- 🔴 NULL-safe контроль ANY_VALUE выше
  COUNTIF(vendor_code IS NULL)              AS vendor_code_nulls,  -- диагностика пропусков
  COUNT(DISTINCT FORMAT('%t', barcode))     AS barcodes_cnt,       -- 🔴 NULL-safe контроль, не значение
  COUNTIF(barcode IS NULL)                  AS barcode_nulls,      -- диагностика пропусков
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
-- ПРИЁМКА. Прогнана read-only на телах вью (inline-CTE, без создания
-- объектов) 19.08.2026 — результат в комментариях «факт».
-- После выката прогнать повторно на объектах.
--
-- 🔴 ДВА РАЗНЫХ КЛАССА ПРОВЕРОК, НЕ ПУТАТЬ:
--
--   П1–П3 — BASELINE ACCEPTANCE на состояние 19.08.2026, срез
--   бэкфилла `BF_20260819_000452`. Числа 24 / 44 788 / 131 / 108
--   доказывают, что вью воспроизводят принятый baseline, и годны
--   ровно один раз — до первого успешного `runWbSuppliesDaily()`.
--   Дальше они ЗАКОНОМЕРНО изменятся: приедут новые поставки,
--   изменятся статусы, вырастут суммы. Ставить их постоянными
--   runtime-гейтами нельзя — такой гейт краснел бы ровно тогда,
--   когда контур работает правильно. Для сравнения после суточных
--   прогонов нужен либо гейт вида «не убывает», либо сверка с
--   отдельно зафиксированным baseline-снимком; ни того, ни другого
--   этот файл не вводит.
--
--   П4–П7 — СТРУКТУРНЫЕ ГЕЙТЫ. Ожидаемое значение 0 не зависит от
--   объёма данных и остаётся верным после любого суточного прогона.
--   Их и только их имеет смысл гонять регулярно.
-- ═══════════════════════════════════════════════════════════════

-- П1. [BASELINE 19.08.2026] Витрина по SKU против принятого бэкфилла.
SELECT COUNT(*) AS sku_cnt, SUM(qty_sent) AS qty_total
FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_SUPPLIES_INTAKE_BY_SKU`;
-- ожидается на срезе BF_20260819_000452: sku_cnt = 24, qty_total = 44788
-- факт 19.08: 24 / 44788 ✅
-- ⚠️ после первого daily значения изменятся — это норма, а не регресс

-- П2. [BASELINE 19.08.2026] Детальный слой: та же сумма, 108 поставок.
SELECT COUNT(DISTINCT entity_key) AS supplies, SUM(quantity) AS qty_total
FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_SUPPLIES_DETAIL`;
-- ожидается на срезе BF_20260819_000452: supplies = 108, qty_total = 44788
-- факт 19.08: 108 / 44788 ✅
-- ⚠️ после первого daily значения изменятся — это норма, а не регресс

-- П3. [BASELINE 19.08.2026] Канонический слой шапок.
SELECT COUNT(*) AS objects, COUNTIF(is_completed) AS completed
FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_SUPPLIES_CURRENT`;
-- ожидается на срезе BF_20260819_000452: objects = 131, completed = 108
-- факт 19.08: 131 / 108 ✅
-- ⚠️ после первого daily значения изменятся — это норма, а не регресс

-- П4. [СТРУКТУРНЫЙ] Один объект — один run_id в текущем срезе позиций.
--
-- 🔴 ЧТО ЭТА ПРОВЕРКА ДОКАЗЫВАЕТ, А ЧТО НЕТ. Ноль здесь НЕ является
-- доказательством того, что тай-брейк выбрал правильный прогон.
-- V_WB_SUPPLIES_GOODS_CURRENT конструктивно ограничен тем самым
-- run_id, который выбрала шапка, поэтому при действующем определении
-- вью результат не может быть иным: проверка подтверждает следствие
-- собственной конструкции. Её ценность — регрессионная: она покраснеет,
-- если будущая правка ослабит join (например, оставит только
-- entity_key) и слой начнёт склеивать позиции из разных снимков.
--
-- Правильность ВЫБОРА прогона этим запросом не проверяется и в этом
-- файле не проверяется вообще. Ошибочный выбор возможен только при
-- равном snapshot_ts у двух прогонов одного объекта, когда решает
-- лексикографика run_id; такой случай наблюдался бы на RAW, до вью,
-- и требует отдельной проверки. Замер 19.08 на RAW: объектов с двумя
-- run_id при равном snapshot_ts — 0, то есть тай-брейк сегодня ни разу
-- не применялся.
SELECT COUNT(*) AS mixed_runs
FROM (
  SELECT g.entity_key
  FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_SUPPLIES_GOODS_CURRENT` g
  GROUP BY g.entity_key
  HAVING COUNT(DISTINCT g.run_id) > 1
);
-- ожидается: 0 при любом объёме данных. факт 19.08: 0 ✅

-- П5. [СТРУКТУРНЫЙ] Грейн детали. Первое число — контракт вью (обязан быть 0).
--     Второе — наблюдение о данных: сегодня «поставка × SKU» совпадает
--     с «поставка × позиция». Рост второго числа не ошибка, а сигнал,
--     что появился SKU с несколькими штрихкодами в одной поставке.
SELECT
  (SELECT COUNT(*) FROM (SELECT entity_key, barcode
     FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_SUPPLIES_DETAIL`
     GROUP BY 1,2 HAVING COUNT(*) > 1)) AS dup_entity_barcode,
  (SELECT COUNT(*) FROM (SELECT entity_key, nm_id
     FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_SUPPLIES_DETAIL`
     GROUP BY 1,2 HAVING COUNT(*) > 1)) AS multi_barcode_per_sku;
-- ожидается: dup_entity_barcode = 0 (жёстко), multi_barcode_per_sku = 0 (наблюдение)
-- факт 19.08: 0 / 0 ✅

-- П6. [СТРУКТУРНЫЙ] NULL-профиль полей, на которых стоят суммы и даты.
SELECT
  COUNTIF(nm_id IS NULL)             AS null_nm_id,
  COUNTIF(quantity IS NULL)          AS null_quantity,
  COUNTIF(accepted_quantity IS NULL) AS null_accepted,
  COUNTIF(fact_date IS NULL)         AS null_fact_date
FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_SUPPLIES_DETAIL`;
-- ожидается: все четыре = 0. факт 19.08: 0/0/0/0 ✅

-- П7. [СТРУКТУРНЫЙ] 🔴 СТОРОЖ СУТОЧНОГО ПРОГОНА. Состоявшаяся поставка без позиций
--     в текущем срезе. Сценарий: суточный прогон перечитал поставку,
--     WB отдал пустой массив goods (не ошибку), шапка закоммитилась
--     с новым run_id — и INNER JOIN тихо вычел её единицы из 44 788.
--     Ноль сегодня НЕ доказывает, что так будет завтра: гейт нужен
--     именно после установки ежедневного триггера.
SELECT COUNT(*) AS completed_without_goods
FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_SUPPLIES_CURRENT` s
WHERE s.is_completed
  AND NOT EXISTS (
    SELECT 1 FROM `project-fa311fc0-4d87-4781-986.wb_raw.V_WB_SUPPLIES_GOODS_CURRENT` g
    WHERE g.entity_key = s.entity_key
  );
-- ожидается: 0. факт 19.08: 0 ✅

-- П8 (справочно, не гейт). Поведение actual на всей выборке.
SELECT actual_matches, COUNT(DISTINCT entity_key) AS supplies
FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_SUPPLIES_DETAIL`
GROUP BY actual_matches;
-- факт 19.08: DESTINATION 72 · TRANSIT 27 · NONE 9 = 108, OTHER отсутствует ✅

-- П9 (справочно, не гейт). Сверка шести июльских поставок с памятью владельца.
SELECT supply_id, fact_date, dest_warehouse_name, transit_warehouse_name,
       COUNT(*) AS positions, SUM(quantity) AS qty, SUM(accepted_quantity) AS accepted
FROM `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_SUPPLIES_DETAIL`
WHERE fact_date BETWEEN '2026-07-01' AND '2026-07-31'
GROUP BY supply_id, fact_date, dest_warehouse_name, transit_warehouse_name
ORDER BY fact_date, supply_id;
-- факт 19.08, все шесть сошлись ✅
--   40321392 06.07 Тула 40/40 · 41037541 24.07 Волгоград 165/165
--   41039407 24.07 Владимир Воршинское 165/164 · 41039644 24.07 Сарапул 165/165
--   41047776 28.07 Екатеринбург-Перспективная 170/169 · 41047801 28.07 Новосемейкино 165/165
