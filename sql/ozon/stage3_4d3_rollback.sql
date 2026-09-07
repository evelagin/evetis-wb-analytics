-- ═══════════════════════════════════════════════════════════════════════
-- Откат Stage 3.4D.3.
--
-- Порядок обратный установке. Три объекта возвращаются к определениям
-- Stage 3.4D.2, один удаляется, одна RAW-таблица удаляется.
--
-- ⚠️ Возврат витрин НЕ выполняется этим файлом автоматически: чтобы
-- вернуть прежние определения, нужно повторно применить
-- sql/ozon/stage3_4d2_ozon_mart_forward.sql. Здесь только удаление
-- добавленного и явное указание, что переприменить.
--
-- ⚠️ Удаление RAW_OZON_SELLER_INFO необратимо теряет историю статуса
-- подписки: API отдаёт только текущее состояние, восстановить снимки за
-- прошлые дни неоткуда.
-- ═══════════════════════════════════════════════════════════════════════

-- ── 1. Новый объект стадии ────────────────────────────────────────────
DROP VIEW IF EXISTS `project-fa311fc0-4d87-4781-986.ozon_mart.V_OZON_AGENT_DECISION_INPUT`;

-- ── 2. Возврат трёх изменённых витрин ─────────────────────────────────
-- Выполнить: sql/ozon/stage3_4d2_ozon_mart_forward.sql, разделы 1, 3, 4
--   V_OZON_TARIFF_SOURCE_HEALTH            (без seller_info и ворот агента)
--   V_OZON_SKU_CURRENT_TARIFF              (Premium константой FALSE)
--   V_OZON_SKU_FORWARD_ECONOMICS_CURRENT   (поле safe_ad_capacity_15pct_worst_case_rub)
--
-- ⚠️ После возврата V_OZON_SKU_FORWARD_ECONOMICS_CURRENT потребители,
-- обращавшиеся к safe_ad_spend_15pct_worst_case_rub, сломаются: в версии
-- 3.4D.2 поле называется safe_ad_capacity_15pct_worst_case_rub.
--
-- ⚠️ Прежние определения содержат скалярные подзапросы (SELECT x FROM health),
-- из-за которых витрина считалась недопустимо долго и падала по таймауту.
-- Откат вернёт и эту проблему. Если цель — только убрать Premium из
-- ingestion, дешевле поправить точечно, а не откатывать целиком.

-- ── 3. Состояние продавца ─────────────────────────────────────────────
DROP TABLE IF EXISTS `project-fa311fc0-4d87-4781-986.ozon_raw.RAW_OZON_SELLER_INFO`;

-- ── 4. Конфигурация runtime ───────────────────────────────────────────
-- Убрать seller_info из набора сущностей суточного job:
--   gcloud run jobs update ozon-runtime-daily --region=europe-west1 \
--     --update-env-vars="^@^ENTITIES=catalog,prices,finance_accrual,ads_campaigns,ads_expense_daily,ads_sku_daily,supplies"
-- и вернуть прежний образ:
--   IMG=europe-west1-docker.pkg.dev/project-fa311fc0-4d87-4781-986/\
--   cloud-run-source-deploy/ozon-runtime-ingest@sha256:43fb3a10601b99381571858210bb34e50cd792bca0c6fae72c550672db885422
--   for J in ozon-runtime-daily ozon-runtime-fast ozon-runtime-weekly ozon-runtime-ingest; do
--     gcloud run jobs update $J --region=europe-west1 --image=$IMG
--   done
-- Соответствующая правка в infra/terraform/ozon_ingestion.tf: вернуть
-- прежний список entities и digest.

-- ── 5. Код runtime ────────────────────────────────────────────────────
-- git revert коммита Stage 3.4D.3 по pipelines/ozon/runtime/entities.py
-- удалит функцию seller_info() и запись в REGISTRY.

-- ── 6. Документы ──────────────────────────────────────────────────────
-- docs/ozon/OZON_FORWARD_ECONOMICS_CONTRACT_V1.md — контракт заморозки;
-- удалять его при откате не следует: он описывает состояние, к которому
-- слой возвращается, и историю переименований.
