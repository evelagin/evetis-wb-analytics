-- ============================================================================
-- PR-PROMO-4 · справочники владельца для планирования (evetis_ref)
-- Контракт: docs/promotions/PR_PROMO_4_INVENTORY_SELL_THROUGH_CONTEXT_2026-09-24.md
--
-- ЧТО ЭТО. Два пустых справочника, которые заполняет ТОЛЬКО владелец явной операцией.
-- Ни одна процедура, расписание или сервисный аккаунт в них не пишет.
--
--   REF_SALES_PLAN_APPROVAL   — утверждение версии плана Control Tower как бизнес-плана
--                               продаж. План хранится там же, где и раньше
--                               (CT_PLAN_VERSION / CT_SEASON_PLAN_MONTHLY), второго
--                               хранилища плана нет. Без строки APPROVED план — модельный
--                               сценарий, и метрики плана (V_SALES_PLAN_MONTHLY_CURRENT,
--                               траектория по плану) не считаются.
--   REF_SKU_INVENTORY_TARGET  — целевой остаток на дату, заданный владельцем
--                               (SEASON_END | MANUAL_DATE). Цели по сроку годности сюда
--                               не пишутся: они выводятся из CT_EXPIRY_BATCH.
--
-- ИСТОРИЯ. Только добавление: новое решение = новая строка, действует последняя по
-- recorded_at. Отзыв утверждения — строка REVOKED, отмена цели — строка CANCELLED.
-- CREATE TABLE IF NOT EXISTS: повторный прогон ничего не меняет. Откат —
-- sql/promotions/pr_promo4_rollback.sql (таблицы удаляются, только если пусты).
-- ============================================================================

CREATE TABLE IF NOT EXISTS `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SALES_PLAN_APPROVAL`
(
  plan_version        STRING    NOT NULL OPTIONS(description="evetis_ref.CT_PLAN_VERSION.plan_version"),
  approval_status     STRING    NOT NULL OPTIONS(description="APPROVED | REVOKED"),
  approved_scope      STRING    NOT NULL OPTIONS(description="MONTHLY_SKU_CHANNEL_UNITS — утверждены карточки по месяцу, каналу и SKU"),
  approved_by         STRING    NOT NULL OPTIONS(description="Кто утвердил (владелец)"),
  approved_at         TIMESTAMP NOT NULL OPTIONS(description="Когда утверждено"),
  approval_reference  STRING             OPTIONS(description="Ссылка на решение: письмо, протокол, сообщение"),
  note                STRING,
  recorded_at         TIMESTAMP NOT NULL OPTIONS(description="Когда строка записана"),
  recorded_by         STRING    NOT NULL OPTIONS(description="Кто записал строку")
)
OPTIONS(description="PR-PROMO-4: утверждение версии плана Control Tower владельцем. Пишет только владелец. Действует последняя строка по recorded_at. Без строки APPROVED план — модельный сценарий.");

CREATE TABLE IF NOT EXISTS `project-fa311fc0-4d87-4781-986.evetis_ref.REF_SKU_INVENTORY_TARGET`
(
  target_id              STRING    NOT NULL OPTIONS(description="Стабильный ключ цели; новая версия цели = та же target_id, новая строка"),
  internal_sku           STRING    NOT NULL OPTIONS(description="Канонический физический SKU (не набор)"),
  target_type            STRING    NOT NULL OPTIONS(description="SEASON_END | MANUAL_DATE"),
  target_date            DATE      NOT NULL OPTIONS(description="Дата, к началу которой остаток должен быть не выше целевого"),
  target_ending_units    INT64     NOT NULL OPTIONS(description="Целевой остаток позиции запаса на target_date, ≥ 0"),
  target_status          STRING    NOT NULL OPTIONS(description="ACTIVE | CANCELLED"),
  approved_by            STRING    NOT NULL OPTIONS(description="Кто задал цель (владелец)"),
  approved_at            TIMESTAMP NOT NULL,
  approval_reference     STRING,
  note                   STRING,
  recorded_at            TIMESTAMP NOT NULL,
  recorded_by            STRING    NOT NULL
)
OPTIONS(description="PR-PROMO-4: целевой остаток запаса на дату, заданный владельцем. Пишет только владелец. Действует последняя строка target_id по recorded_at.");
