#!/usr/bin/env bash
# ============================================================================
# STAGE 3.1C PR1 — ОТКАТ СЛОЯ КЛАССИФИКАЦИИ РЕКЛАМНОГО БИЛЛИНГА
# Дата: 2026-08-27.  Сборка: sql/mart/pr_ad_billing_classification_v1.sql
#
# 🔴 ОТКАТ STAGE-SCOPED. Скрипт удаляет РОВНО ДВА объекта этого PR и ничего
#    больше. Он сознательно НЕ трогает:
#      • V_DASH_* — числа Executive и SKU Performance этим PR не менялись;
#      • FACT_*, MART_SKU_DAILY, REF_COST_MAP, процедуры — не пересобираются;
#      • evetis_ref и объекты Stage 3.1B (V_FACT_FINANCE_COGS,
#        V_MART_SKU_DAILY_COGS) — не затрагиваются;
#      • RAW_*, Google Sheets, Apps Script, Metabase.
#
# 🔴 STRUCTURAL BASELINE НЕ ВОЗВРАЩАЕТСЯ ЭТИМ СКРИПТОМ.
#    Константа wb_mart в sql/ref/pr_ref_cogs_validation.sql принадлежит
#    конкретному commit/checkpoint. Безусловное «39 -> 37» затёрло бы
#    последующие легитимные изменения структуры. Откат репозитория —
#    только git revert коммита Stage 3.1C PR1.
#
# --dry-run работает БЕЗ Google Cloud SDK: печатает план и точный SQL,
#    не выполняет ни одного DDL, возвращает exit 0.
# ============================================================================
set -euo pipefail

PROJECT="project-fa311fc0-4d87-4781-986"
DATASET="wb_mart"
DRY_RUN=0
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=1

# Порядок важен: reconciliation читает classified.
OBJECTS=(
  "V_ADVERTISING_RECONCILIATION_DAILY"
  "V_WB_DEDUCTIONS_CLASSIFIED"
)

echo "=== Stage 3.1C PR1 rollback ==="
echo "project : ${PROJECT}"
echo "dataset : ${DATASET}"
echo "mode    : $([[ ${DRY_RUN} -eq 1 ]] && echo 'DRY-RUN (ни одного DDL)' || echo 'EXECUTE')"
echo
echo "--- ПРЕДУСЛОВИЯ ---"
echo "  1. wb_mart содержит 39 объектов (37 до Stage 3.1C PR1 + 2 новых)."
echo "  2. Ни один объект V_DASH_* не ссылается на удаляемые вью."
echo "  3. Metabase к ним не подключён: карточки 40-71 читают только V_DASH_*."
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
       COUNTIF(table_name IN ('V_WB_DEDUCTIONS_CLASSIFIED','V_ADVERTISING_RECONCILIATION_DAILY')) AS stage_3_1c_objects
       FROM \`${PROJECT}.${DATASET}.INFORMATION_SCHEMA.TABLES\`"
if [[ ${DRY_RUN} -eq 1 ]]; then
  echo "[dry-run] ${CHECK}"
  echo "[dry-run] ожидание после реального отката: wb_mart_objects = 37, stage_3_1c_objects = 0"
else
  bq --project_id="${PROJECT}" query --nouse_legacy_sql "${CHECK}"
fi

echo
echo "Откат репозитория (вручную, отдельно): git revert <commit Stage 3.1C PR1>."
echo "Он вернёт и structural baseline в состояние того checkpoint."
exit 0
