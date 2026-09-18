-- ═══════════════════════════════════════════════════════════════════════
-- Откат Stage 3.4D.2.
--
-- Откатывает только витрины. RAW-слой (RAW_OZON_PRICE_COMMISSIONS и
-- колонки тарифа в RAW_OZON_PRICES) сохраняется: блок commissions приходит
-- только в текущем ответе API, историю за прошлые дни Ozon не отдаёт.
-- Накопленные снимки пережили выкатку этапа и принадлежат жизненному циклу
-- данных загрузки, а не откату витрин.
--
-- ⛔ SUPERSEDED — DO NOT EXECUTE. Инструкция по образу ниже устарела:
-- с 2026-09-17 все Ozon job работают на образе R1, и её выполнение вернуло
-- бы production к коду до R1. Сохранена только как история.
-- Отдельно от SQL: образ Cloud Run. Все четыре job переведены на
-- sha256:43fb3a10601b99381571858210bb34e50cd792bca0c6fae72c550672db885422.
-- Возврат к прежнему образу:
--   IMG=europe-west1-docker.pkg.dev/project-fa311fc0-4d87-4781-986/\
--   cloud-run-source-deploy/ozon-runtime-ingest@sha256:a7ce446a661e612bab7f756f103ec8bcf41688429c1da9361096fae108639409
--   for J in ozon-runtime-daily ozon-runtime-fast ozon-runtime-weekly ozon-runtime-ingest; do
--     gcloud run jobs update $J --region=europe-west1 --image=$IMG
--   done
-- Прежний образ сохраняет прежнее поведение prices: одно поле
-- sales_percent_fbo, RAW_OZON_PRICE_COMMISSIONS не пишется.
-- ═══════════════════════════════════════════════════════════════════════

-- ── 1. Витрины Stage 3.4D.2 ───────────────────────────────────────────
-- Порядок важен: экономика ссылается на тариф и на здоровье источника,
-- сравнение схем ссылается на экономику.
DROP VIEW IF EXISTS `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_SKU_FBO_FBS_COMPARISON_CURRENT`;
DROP VIEW IF EXISTS `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_SKU_FORWARD_ECONOMICS_CURRENT`;
DROP VIEW IF EXISTS `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_SKU_CURRENT_TARIFF`;
DROP VIEW IF EXISTS `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_TARIFF_CHANGE_LOG`;
DROP VIEW IF EXISTS `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_TARIFF_SOURCE_HEALTH`;

-- Объекты Stage 3.4C (FCT_OZON_PNL_MONTHLY, FCT_OZON_SKU_PNL_MONTHLY,
-- V_OZON_LIFETIME_PNL, V_OZON_MART_FRESHNESS, V_OZON_SKU_UNIT_ECONOMICS_CURRENT)
-- этот откат НЕ трогает: Stage 3.4D.2 их не изменял.

-- ── 2. Длинная проекция компонент тарифа ──────────────────────────────
-- ozon_raw.RAW_OZON_PRICE_COMMISSIONS НЕ удаляется и не очищается:
-- ежедневные снимки тарифа невосстановимы (API отдаёт только текущее
-- состояние). Таблицу пишет суточная загрузка, её читают витрины ozon_mart.

-- ── 3. Колонки RAW_OZON_PRICES ────────────────────────────────────────
-- Колонки тарифа, добавленные Stage 3.4D.2, НЕ удаляются: в них хранится
-- ежедневная история тарифа, их пишет текущая загрузка prices() и читают
-- витрины ozon_mart. Схему назад не откатываем.

-- ── 4. Код runtime ────────────────────────────────────────────────────
-- ⛔ SUPERSEDED — DO NOT EXECUTE: код runtime с тех пор изменён (R1), revert
-- вернул бы production к поведению до R1. Сохранено только как история.
-- git revert коммита Stage 3.4D.2 по pipelines/ozon/runtime/entities.py
-- вернёт функцию prices() к записи в одну таблицу.
