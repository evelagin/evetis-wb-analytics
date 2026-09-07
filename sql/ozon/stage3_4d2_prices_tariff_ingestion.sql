-- ═══════════════════════════════════════════════════════════════════════
-- Stage 3.4D.2 — миграция RAW-слоя цен под текущий тарифный контракт.
--
-- ЗАЧЕМ. До этой миграции RAW_OZON_PRICES сохранял из блока `commissions`
-- ровно одно поле — sales_percent_fbo — и acquiring. Остальные девять
-- компонент тарифа (логистика FBO/FBS min/max, последняя миля, обратная
-- логистика, первая миля FBS, ставки FBS/rFBS/FBP) приходили в ответе
-- /v5/product/info/prices и молча выбрасывались. Поэтому форвардная
-- экономика Stage 3.4D жила на разовом снимке 2026-09-04 и не могла
-- обновляться сама.
--
-- ЧТО ДЕЛАЕМ. Только аддитивно:
--   1. RAW_OZON_PRICES — новые NULLABLE-колонки. Историю не трогаем:
--      строки до миграции остаются с NULL в новых полях, и это честно —
--      значений в источнике для них у нас нет.
--   2. RAW_OZON_PRICE_COMMISSIONS — длинная проекция блока commissions,
--      одна строка на компоненту. Нужна ровно затем, чтобы НОВЫЙ тариф
--      Ozon не потребовал миграции схемы: незнакомый ключ ляжет строкой
--      со схемой UNKNOWN и is_known_component = FALSE, а витрина
--      форвардной экономики по этому флагу закроется.
--
-- ⚠️ Записи в маркетплейс здесь нет и быть не может: /v5/product/info/prices
--    — READ-метод. Цены, ставки, кампании, остатки не меняются.
--
-- Откат: sql/ozon/stage3_4d2_rollback.sql
-- ═══════════════════════════════════════════════════════════════════════

-- ── 1. Расширение снимка цен ──────────────────────────────────────────
-- Порядок колонок повторяет порядок полей в ответе API: сначала price,
-- потом commissions по схемам, потом индексы, потом сырой JSON и счётчики
-- для детектора изменений.
ALTER TABLE `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_PRICES`
  ADD COLUMN IF NOT EXISTS currency_code STRING
      OPTIONS(description="price.currency_code"),
  ADD COLUMN IF NOT EXISTS retail_price_rub NUMERIC
      OPTIONS(description="price.retail_price"),
  ADD COLUMN IF NOT EXISTS vat_rate NUMERIC
      OPTIONS(description="price.vat, доля (0 = без НДС)"),
  ADD COLUMN IF NOT EXISTS auto_action_enabled BOOL
      OPTIONS(description="price.auto_action_enabled"),
  ADD COLUMN IF NOT EXISTS auto_add_to_ozon_actions_enabled BOOL
      OPTIONS(description="price.auto_add_to_ozon_actions_list_enabled"),
  ADD COLUMN IF NOT EXISTS volume_weight_l NUMERIC
      OPTIONS(description="volume_weight — объёмный вес, литры"),
  ADD COLUMN IF NOT EXISTS sales_percent_fbs NUMERIC
      OPTIONS(description="commissions.sales_percent_fbs, проценты"),
  ADD COLUMN IF NOT EXISTS sales_percent_rfbs NUMERIC
      OPTIONS(description="commissions.sales_percent_rfbs, проценты"),
  ADD COLUMN IF NOT EXISTS sales_percent_fbp NUMERIC
      OPTIONS(description="commissions.sales_percent_fbp, проценты"),
  ADD COLUMN IF NOT EXISTS fbo_direct_flow_trans_min_rub NUMERIC
      OPTIONS(description="Логистика FBO, нижняя граница. Официально = универсальный тариф по объёму (лист «Тарифы по умолчанию», logistika-fbo-fbs-28082026.xlsx). Сверено 20/20 на 2026-09-06"),
  ADD COLUMN IF NOT EXISTS fbo_direct_flow_trans_max_rub NUMERIC
      OPTIONS(description="Логистика FBO, верхняя граница = максимум по 848 маршрутам матрицы кластер→кластер. Сверено 20/20"),
  ADD COLUMN IF NOT EXISTS fbo_deliv_to_customer_rub NUMERIC
      OPTIONS(description="Доставка до места выдачи FBO, п. 2.7: партнёры ≤ 25 ₽, силами Ozon 25 ₽"),
  ADD COLUMN IF NOT EXISTS fbo_return_flow_rub NUMERIC
      OPTIONS(description="Обратная логистика FBO, п. 2.1.2: тариф равен тарифу логистики"),
  ADD COLUMN IF NOT EXISTS fbs_first_mile_min_rub NUMERIC
      OPTIONS(description="Обработка отправления FBS, нижняя граница = 0 ₽ при отгрузке в СЦ (п. 2.3.2 с 25.08.2026)"),
  ADD COLUMN IF NOT EXISTS fbs_first_mile_max_rub NUMERIC
      OPTIONS(description="Обработка отправления FBS, верхняя граница = 10 ₽ ПВЗ/ППЗ (п. 2.3.2). Курьерская отгрузка тарифицируется партнёром и в API не приходит"),
  ADD COLUMN IF NOT EXISTS fbs_direct_flow_trans_min_rub NUMERIC
      OPTIONS(description="Логистика FBS, нижняя граница"),
  ADD COLUMN IF NOT EXISTS fbs_direct_flow_trans_max_rub NUMERIC
      OPTIONS(description="Логистика FBS, верхняя граница"),
  ADD COLUMN IF NOT EXISTS fbs_deliv_to_customer_rub NUMERIC
      OPTIONS(description="Доставка до места выдачи FBS"),
  ADD COLUMN IF NOT EXISTS fbs_return_flow_rub NUMERIC
      OPTIONS(description="Обратная логистика FBS, п. 2.3.4"),
  ADD COLUMN IF NOT EXISTS ozon_index_min_price_rub NUMERIC
      OPTIONS(description="price_indexes.ozon_index_data.min_price"),
  ADD COLUMN IF NOT EXISTS ozon_index_value NUMERIC
      OPTIONS(description="price_indexes.ozon_index_data.price_index_value"),
  ADD COLUMN IF NOT EXISTS self_marketplaces_index_min_price_rub NUMERIC
      OPTIONS(description="price_indexes.self_marketplaces_index_data.min_price"),
  ADD COLUMN IF NOT EXISTS self_marketplaces_index_value NUMERIC
      OPTIONS(description="price_indexes.self_marketplaces_index_data.price_index_value"),
  ADD COLUMN IF NOT EXISTS commissions_json STRING
      OPTIONS(description="Сырой блок commissions, ключи отсортированы. Аудиторский след: по нему воспроизводится любая производная колонка"),
  ADD COLUMN IF NOT EXISTS commissions_field_count INT64
      OPTIONS(description="Число ключей в блоке commissions. Рост числа = сигнал детектора изменений"),
  ADD COLUMN IF NOT EXISTS commissions_unknown_fields STRING
      OPTIONS(description="Ключи commissions, которых нет в COMMISSION_MAP runtime. NULL = все компоненты классифицированы. Непусто ⇒ forward_economics_ready = FALSE");

-- ── 2. Длинная проекция компонент тарифа ──────────────────────────────
CREATE TABLE IF NOT EXISTS
  `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_PRICE_COMMISSIONS`
(
  snapshot_ts TIMESTAMP NOT NULL,
  snapshot_date DATE NOT NULL,
  offer_id STRING NOT NULL,
  product_id STRING,
  sale_scheme STRING NOT NULL
    OPTIONS(description="FBO | FBS | RFBS | FBP | COMMON | LEGACY | UNKNOWN. UNKNOWN — новый неклассифицированный компонент"),
  commission_component STRING NOT NULL
    OPTIONS(description="SALES_PERCENT | DIRECT_FLOW_TRANS_MIN | DIRECT_FLOW_TRANS_MAX | DELIV_TO_CUSTOMER | RETURN_FLOW | FIRST_MILE_MIN | FIRST_MILE_MAX | ACQUIRING. Для неизвестной компоненты — сырое имя поля API"),
  api_field STRING NOT NULL
    OPTIONS(description="Имя поля в ответе API как есть. Именно оно сравнивается между снимками детектором изменений"),
  value_num NUMERIC,
  unit STRING OPTIONS(description="PERCENT | RUB | UNKNOWN"),
  is_known_component BOOL NOT NULL
    OPTIONS(description="FALSE ⇒ компонента отсутствует в COMMISSION_MAP runtime и НЕ учтена ни в одной формуле. Закрывает витрину форвардной экономики"),
  extracted_at TIMESTAMP NOT NULL,
  source_endpoint STRING NOT NULL,
  ingestion_run_id STRING NOT NULL,
  source_payload_hash STRING
)
PARTITION BY snapshot_date
CLUSTER BY offer_id, sale_scheme
OPTIONS(description="Компоненты тарифа Ozon из блока commissions /v5/product/info/prices, длинный формат. PK (snapshot_date, offer_id, sale_scheme, commission_component). Grain выбран так, чтобы новый тариф не требовал ALTER TABLE: неизвестный ключ приходит строкой с is_known_component=FALSE");
