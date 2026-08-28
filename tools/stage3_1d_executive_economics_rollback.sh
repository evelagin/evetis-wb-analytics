#!/usr/bin/env bash
# ============================================================================
# STAGE 3.1D — ОТКАТ СЛОЯ ЭКОНОМИКИ EXECUTIVE ПОСЛЕ PRODUCT COGS
# Дата: 2026-08-28.  Сборка: sql/dash/pr_dash_executive_economics_v1.sql
#
# 🔴 ОТКАТ STAGE-SCOPED. Скрипт удаляет РОВНО ОДИН объект этого этапа и ничего
#    больше. Он сознательно НЕ трогает:
#      • V_DASH_FINANCE_CORRECTED_DAILY (Stage 3.1C PR2) — Executive до
#        себестоимости живёт на нём и после отката продолжит работать;
#      • V_FACT_FINANCE_COGS и V_MART_SKU_DAILY_COGS (Stage 3.1B);
#      • объекты Stage 3.1C PR1 (V_WB_DEDUCTIONS_CLASSIFIED,
#        V_ADVERTISING_RECONCILIATION_DAILY);
#      • evetis_ref (Stage 3.1A: REF_SKU_COGS_HISTORY, REF_BUNDLE_COMPONENTS,
#        V_BUNDLE_COGS_DERIVED, V_PRODUCT_COGS_EFFECTIVE);
#      • V_DASH_KPI_DAILY, V_DASH_SKU_DAILY, V_DASH_COVERAGE_DAILY,
#        FACT_*, MART_SKU_DAILY, REF_COST_MAP, процедуры;
#      • RAW_*, Google Sheets, Apps Script, Metabase.
#
# 🔴 ПОРЯДОК ИМЕЕТ ЗНАЧЕНИЕ. Если карточки Metabase уже переключены на этот
#    слой отдельным ACK — СНАЧАЛА верните их на V_DASH_FINANCE_CORRECTED_DAILY,
#    и только потом запускайте этот скрипт. Иначе карточки после отката
#    отдадут ошибку вместо чисел.
#    На момент создания слоя Metabase к нему не подключён, поэтому откат
#    в этом состоянии экраны не меняет.
#
# 🔴 STRUCTURAL BASELINE НЕ ВОЗВРАЩАЕТСЯ ЭТИМ СКРИПТОМ. Константа wb_mart = 41
#    в sql/dash/pr_dash_executive_economics_validation.sql принадлежит
#    конкретному checkpoint; безусловное «41 -> 40» затёрло бы последующие
#    легитимные изменения структуры. Откат репозитория — только git revert.
#
# --dry-run работает БЕЗ Google Cloud SDK: печатает план и точный SQL,
#    не выполняет ни одного DDL, возвращает exit 0.
# ============================================================================
set -euo pipefail

PROJECT="${BQ_PROJECT:-project-fa311fc0-4d87-4781-986}"
DATASET="wb_mart"
DRY_RUN=0
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=1

OBJECTS=(
  "V_DASH_EXECUTIVE_ECONOMICS_DAILY"
)

echo "=== Stage 3.1D rollback ==="
echo "project : ${PROJECT}"
echo "dataset : ${DATASET}"
echo "mode    : $([[ ${DRY_RUN} -eq 1 ]] && echo 'DRY-RUN (ни одного DDL)' || echo 'EXECUTE')"
echo
echo "--- ПРЕДУСЛОВИЯ ---"
echo "  1. wb_mart содержит 41 объект (40 до Stage 3.1D + 1 новый)."
echo "  2. Ни один другой объект wb_mart не ссылается на удаляемый view."
echo "  3. Карточки Metabase не читают V_DASH_EXECUTIVE_ECONOMICS_DAILY."
echo

for OBJ in "${OBJECTS[@]}"; do
  STMT="DROP VIEW IF EXISTS \`${PROJECT}.${DATASET}.${OBJ}\`"
  if [[ ${DRY_RUN} -eq 1 ]]; then
    echo "[dry-run] ${STMT}"
  else
    echo "[execute] ${STMT}"
    bq --project_id="${PROJECT}" query --nouse_legacy_sql --format=none "${STMT}"
  fi
done

echo
echo "--- ПОСТУСЛОВИЯ ---"
CHECK="SELECT COUNT(*) AS wb_mart_objects,
       COUNTIF(table_name = 'V_DASH_EXECUTIVE_ECONOMICS_DAILY') AS stage_3_1d_objects,
       COUNTIF(table_name = 'V_DASH_FINANCE_CORRECTED_DAILY')   AS stage_3_1c_pr2_objects,
       COUNTIF(table_name IN ('V_FACT_FINANCE_COGS','V_MART_SKU_DAILY_COGS')) AS stage_3_1b_objects
       FROM \`${PROJECT}.${DATASET}.INFORMATION_SCHEMA.TABLES\`"
if [[ ${DRY_RUN} -eq 1 ]]; then
  echo "[dry-run] ${CHECK}"
  echo "[dry-run] ожидание после реального отката: wb_mart_objects = 40,"
  echo "[dry-run]   stage_3_1d_objects = 0, stage_3_1c_pr2_objects = 1, stage_3_1b_objects = 2"
else
  bq --project_id="${PROJECT}" query --nouse_legacy_sql "${CHECK}"
fi

echo
echo "Откат репозитория (вручную, отдельно): git revert <commit Stage 3.1D>."
exit 0
