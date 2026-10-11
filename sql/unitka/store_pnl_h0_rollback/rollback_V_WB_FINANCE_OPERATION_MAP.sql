-- ROLLBACK (H0): тело production wb_mart.V_WB_FINANCE_OPERATION_MAP ДО изменения H0, снято tables.get 2026-10-11 (Phase C, main 8753337).
CREATE OR REPLACE VIEW `project-fa311fc0-4d87-4781-986.wb_mart.V_WB_FINANCE_OPERATION_MAP` AS
SELECT * FROM UNNEST([
  STRUCT('Продажа' AS supplier_oper_name, 'DIRECT_SKU' AS treatment, 'REVENUE_COMMISSION' AS category, 'цена продавца × кол-во; удержание WB = база − for_pay (комиссия + эквайринг)' AS note),
  ('Возврат', 'DIRECT_SKU', 'RETURNS', 'сторно продажи по srid'),
  ('Коррекция продаж', 'PENDING_CLASSIFICATION', 'REVENUE_COMMISSION', 'историческая (до 2025-07); модель её не читает — появление = разбор'),
  ('Корректировка эквайринга', 'PENDING_CLASSIFICATION', 'REVENUE_COMMISSION', 'историческая (до 2025-01); модель её не читает — появление = разбор'),
  ('Логистика', 'DIRECT_SKU', 'LOGISTICS', 'прямые, обратные плечи и отказы по srid (до 31.08.2026)'),
  ('Доставка', 'DIRECT_SKU', 'LOGISTICS', 'то же с 01.09.2026 (переименование WB)'),
  ('Коррекция логистики', 'DIRECT_SKU', 'LOGISTICS', 'историческая (до 2026-04)'),
  ('Хранение', 'DIRECT_SKU', 'STORAGE', 'факт по nm; Юнитка с 01.09 — тот же факт'),
  ('Коррекция хранения', 'DIRECT_SKU', 'STORAGE', 'сторно хранения'),
  ('Платная приемка', 'ACCOUNT_LEVEL', 'ACCEPTANCE', 'приёмка поставки'),
  ('Пересчет платной приемки', 'ACCOUNT_LEVEL', 'ACCEPTANCE', 'историческая'),
  ('Штраф', 'ACCOUNT_LEVEL', 'PENALTY', 'без nm и заказа (08.2026: «srid» 6144364 — не заказ)'),
  ('Удержание', 'BY_DEDUCTION_CLASS', 'DEDUCTION', 'класс — V_WB_DEDUCTIONS_CLASSIFIED'),
  ('Возмещение издержек по перевозке/по складским операциям с товаром', 'MEMO_NON_PNL', 'REIMBURSEMENT', 'Р2: погашено вознаграждением WB, forPay = 0'),
  ('Возмещение издержек по перемещению и операционной обработке товара', 'MEMO_NON_PNL', 'REIMBURSEMENT', 'Р2: то же с 01.09.2026'),
  ('Возмещение за выдачу и возврат товаров на ПВЗ', 'MEMO_NON_PNL', 'REIMBURSEMENT', 'Р2: ppvzReward погашен вознаграждением WB'),
  ('Компенсация скидки по программе лояльности', 'MEMO_ZERO', 'LOYALTY', 'денежных полей нет (raw_json)'),
  ('Сумма баллов, удержанных в рамках акции "Баллы за отзывы"', 'MEMO_ZERO', 'REVIEW_POINTS', 'денежных полей нет (raw_json)'),
  ('Добровольная компенсация при возврате', 'PENDING_CLASSIFICATION', 'COMPENSATION', 'историческая; поле денег не доказано — появление = разбор, не молчаливый доход'),
  ('Компенсация ущерба', 'PENDING_CLASSIFICATION', 'COMPENSATION', 'историческая; поле денег не доказано — появление = разбор, не молчаливый доход'),
  ('Стоимость участия в программе лояльности', 'PENDING_CLASSIFICATION', 'LOYALTY', '2025-08/09; не встречалась в месяцах Phase C'),
  ('Сумма удержанная за начисленные баллы программы лояльности', 'PENDING_CLASSIFICATION', 'LOYALTY', '2025-08/09; не встречалась в месяцах Phase C')
]);
