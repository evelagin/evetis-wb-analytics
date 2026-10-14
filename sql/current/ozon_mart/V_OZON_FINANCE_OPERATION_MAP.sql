-- ============================================================================
-- CANONICAL CURRENT DEFINITION — ozon_mart.V_OZON_FINANCE_OPERATION_MAP (VIEW)
-- Git-first (sync_state pending_deploy): в production объекта ещё нет. Rules: sql/current/README.md.
-- D3 «Ozon Магазин P&L» (OWNER DIRECTIVE 2026-10-14 §6; проект docs/finance/OZON_STORE_PNL_PHASE_D3.md).
-- Таксономия 33 типов начислений Seller API + синтетические −1/−2 (D1, PR #339) как данные.
-- Аналог wb_mart.V_WB_FINANCE_OPERATION_MAP. Изменение карты — только PR с доказательством.
--   pnl_class   — DIRECT_SKU / ADS_MARKETING / ACCOUNT_LEVEL / PENDING_CLASSIFICATION;
--   pnl_column  — колонка V_OZON_STORE_PNL_MONTHLY для amount_rub строки (экономический блок
--                 seller_base/commission всегда идёт в B/D); 'L_OR_M' — тип 54: CPC или CPO по кампании;
--   date_basis  — ORDER_MONTH (месяц заказа отправления), BOOKING_MONTH (месяц начисления),
--                 SERVICE_DAY (сутки услуги = сутки начисления), CHARGE_DATE (дата списания CPO),
--                 SERVICE_PERIOD (Premium: период подписки из unit_number);
--   expected_sign — знак amount_rub: NEG, NEG_OR_ZERO, ZERO, POS, ANY (наблюдаемые корректировки).
-- valid_from / valid_to — период, на котором класс доказан; NULL — без ограничения.
-- ============================================================================
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_FINANCE_OPERATION_MAP`
OPTIONS (description = "Таксономия начислений Ozon как данные (D3), зерно = type_id. 33 типа Seller API /v1/finance/accrual + синтетические -1 (сторно реализации, D1) и -2 (реализация без услуг, PENDING до первого доказанного случая). pnl_class: DIRECT_SKU / ADS_MARKETING / ACCOUNT_LEVEL / PENDING_CLASSIFICATION. pnl_column - колонка V_OZON_STORE_PNL_MONTHLY для amount_rub; экономический блок (seller_base, commission) всегда в B/D. date_basis задаёт месяц P&L. Тип вне карты, смена названия или знак против expected_sign - V_OZON_FINANCE_NEW_OPERATIONS и блок публикации.")
AS
SELECT * FROM UNNEST([
  STRUCT(32 AS type_id, 'Логистика' AS operation_name, 'DIRECT_SKU' AS pnl_class, 'E' AS pnl_column, 'ORDER_MONTH' AS date_basis, 'ANY' AS expected_sign, CAST(NULL AS DATE) AS valid_from, CAST(NULL AS DATE) AS valid_to, 'D0 2026-10-11: экономический блок продажи + логистика; перерасчёт +1 509,50 (2026-05)' AS evidence_ref),
  (29, 'Доставка до места выдачи', 'DIRECT_SKU', 'F', 'ORDER_MONTH', 'NEG', NULL, NULL, 'D0: последняя миля; несёт экономический блок'),
  (28, 'Последняя миля', 'DIRECT_SKU', 'F', 'ORDER_MONTH', 'NEG', NULL, NULL, 'D0: 2025-04..05; несёт экономический блок'),
  (98, 'Доставка до места выдачи силами Ozon', 'DIRECT_SKU', 'F', 'ORDER_MONTH', 'NEG', NULL, NULL, 'D0: последняя миля Ozon'),
  (30, 'Выдача товара', 'DIRECT_SKU', 'F', 'ORDER_MONTH', 'NEG', NULL, NULL, 'D0: 2025-06'),
  (59, 'Обратная логистика', 'DIRECT_SKU', 'G', 'ORDER_MONTH', 'NEG', NULL, NULL, 'D0: возвраты с 2025-06-03'),
  (45, 'Обработка возвратов, отмен и невыкупов партнёрами', 'DIRECT_SKU', 'G', 'ORDER_MONTH', 'NEG', NULL, NULL, 'D0'),
  (9, 'Обработка возвратов', 'DIRECT_SKU', 'G', 'ORDER_MONTH', 'ZERO', NULL, NULL, 'D0: сумма 0,00'),
  (6, 'Обработка отменённых и невостребованных товаров', 'DIRECT_SKU', 'G', 'ORDER_MONTH', 'ZERO', NULL, NULL, 'Gate 5L: сумма 0,00'),
  (78, 'Краткосрочное размещение возврата FBS', 'DIRECT_SKU', 'G', 'BOOKING_MONTH', 'NEG', NULL, NULL, 'D0: строка только со sku'),
  (1, 'Эквайринг', 'DIRECT_SKU', 'H', 'ORDER_MONTH', 'ANY', NULL, NULL, 'D0: unit_number -> отправление или заказ+sku; положительные - возврат эквайринга'),
  (79, 'Временное размещение товара партнерами', 'DIRECT_SKU', 'I', 'BOOKING_MONTH', 'NEG', NULL, NULL, 'D0: хранение SKU'),
  (116, 'Сбор первых отзывов', 'DIRECT_SKU', 'J', 'ORDER_MONTH', 'NEG', NULL, NULL, 'D0: продвижение SKU, unit_number -> отправление'),
  (74, 'Звёздные товары', 'DIRECT_SKU', 'J', 'ORDER_MONTH', 'NEG', NULL, NULL, 'D0: продвижение SKU'),
  (48, 'Бонусы продавца', 'DIRECT_SKU', 'J', 'ORDER_MONTH', 'NEG', NULL, NULL, 'D0: продвижение SKU'),
  (39, 'Упаковка товара партнёрами', 'DIRECT_SKU', 'K', 'ORDER_MONTH', 'NEG', NULL, NULL, 'D0: прочие прямые'),
  (38, 'Обеспечение материалами для упаковки товара', 'DIRECT_SKU', 'K', 'ORDER_MONTH', 'NEG', NULL, NULL, 'D0: прочие прямые'),
  (10, 'Компенсация', 'DIRECT_SKU', 'K', 'ORDER_MONTH', 'NEG', NULL, NULL, 'D0: расход -250 по отправлению (2026-04), не доход'),
  (15, 'Утилизация', 'DIRECT_SKU', 'K', 'BOOKING_MONTH', 'NEG', NULL, NULL, 'D0: строка только со sku'),
  (71, 'Вывоз товара со склада силами Ozon', 'DIRECT_SKU', 'K', 'BOOKING_MONTH', 'NEG', NULL, NULL, 'D0: строка только со sku'),
  (41, 'Оплата за клик', 'ADS_MARKETING', 'L', 'SERVICE_DAY', 'ANY', NULL, NULL, 'D0: CPC-биллинг = Performance 0,00; возврат +167,74 (2026-09-08)'),
  (75, 'Трафареты', 'ADS_MARKETING', 'L', 'SERVICE_DAY', 'NEG', NULL, NULL, 'D0: CPC 2025-05..08'),
  (33, 'Рекламные услуги', 'ADS_MARKETING', 'L', 'SERVICE_DAY', 'NEG', NULL, NULL, 'D0: 2025-08'),
  (54, 'Продвижение товара', 'ADS_MARKETING', 'L_OR_M', 'CHARGE_DATE', 'NEG', NULL, NULL, 'Phase B: CPO-кампании (payment_type CPO) -> M; CPC-кампании 2025-05..08 -> L'),
  (47, 'Баллы за отзывы', 'ADS_MARKETING', 'N', 'BOOKING_MONTH', 'NEG', NULL, NULL, 'D0: реклама магазина без SKU'),
  (96, 'Ускоренный сбор отзывов', 'ADS_MARKETING', 'N', 'BOOKING_MONTH', 'NEG', NULL, NULL, 'D0: реклама магазина без SKU'),
  (12, 'Кросс-докинг', 'ACCOUNT_LEVEL', 'O', 'BOOKING_MONTH', 'NEG_OR_ZERO', NULL, NULL, 'D0: кабинет; 8 нулевых строк'),
  (77, 'Обработка товара', 'ACCOUNT_LEVEL', 'P', 'BOOKING_MONTH', 'NEG', NULL, NULL, 'D0: кабинет'),
  (46, 'Размещение товаров на складах Ozon', 'ACCOUNT_LEVEL', 'Q', 'BOOKING_MONTH', 'NEG', NULL, NULL, 'D0: кабинет'),
  (76, 'Страхование товара от массовых повреждений', 'ACCOUNT_LEVEL', 'R', 'BOOKING_MONTH', 'ANY', NULL, NULL, 'D0: кабинет; корректировка +459,47 (2026-10-05)'),
  (57, 'Корректировка стоимости услуг', 'ACCOUNT_LEVEL', 'S', 'BOOKING_MONTH', 'ANY', NULL, NULL, 'D0: 2025-10, нетто 0'),
  (52, 'Подписка Premium', 'ACCOUNT_LEVEL', 'T', 'SERVICE_PERIOD', 'NEG', NULL, NULL, 'D0: период подписки в unit_number dd.mm.yy-dd.mm.yy'),
  (25, 'Товарная компенсация', 'ACCOUNT_LEVEL', 'U', 'BOOKING_MONTH', 'POS', NULL, NULL, 'D0: доход кабинета'),
  (-1, 'Возврат покупателя: сторно реализации (экономический блок без услуг)', 'DIRECT_SKU', 'ECON_ONLY', 'ORDER_MONTH', 'ZERO', NULL, NULL, 'D1 PR #339: amount_rub = 0, деньги в seller_base/commission'),
  (-2, 'Реализация: экономический блок без услуг', 'PENDING_CLASSIFICATION', NULL, 'ORDER_MONTH', 'ZERO', NULL, NULL, 'D1 PR #339: случаев 0, PENDING до первого доказанного')
]);
