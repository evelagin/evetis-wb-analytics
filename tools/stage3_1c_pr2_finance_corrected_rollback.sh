#!/usr/bin/env bash
# ============================================================================
# STAGE 3.1C PR2 — ОТКАТ ИСПРАВЛЕННОЙ ФИНАНСОВОЙ СЕМАНТИКИ EXECUTIVE
# Дата: 2026-08-27.  Сборка: sql/dash/pr_dash_finance_corrected_v1.sql
#
# 🔴 ОТКАТ STAGE-SCOPED. Скрипт удаляет РОВНО ОДИН объект этого PR и ничего
#    больше. Он сознательно НЕ трогает:
#      • V_DASH_KPI_DAILY, V_DASH_SKU_DAILY, V_DASH_COVERAGE_DAILY — PR2 их
#        не менял ни одним символом, числа Executive на них и остались;
#      • объекты Stage 3.1C PR1 (V_WB_DEDUCTIONS_CLASSIFIED,
#        V_ADVERTISING_RECONCILIATION_DAILY) — это отдельный слой;
#      • объекты Stage 3.1B (V_FACT_FINANCE_COGS, V_MART_SKU_DAILY_COGS);
#      • FACT_*, MART_SKU_DAILY, REF_COST_MAP, процедуры, evetis_ref;
#      • RAW_*, Google Sheets, Apps Script, Metabase.
#
# 🔴 ЭКРАНЫ ОТ ЭТОГО ОТКАТА НЕ МЕНЯЮТСЯ. На момент PR2 Metabase к новому
#    слою не подключён (проверка C-17), поэтому удаление view возвращает
#    ровно то состояние, которое пользователь и видит.
#    Если карточки 51/53/54 уже переключены отдельным ACK — сначала верните
#    их на V_DASH_KPI_DAILY, потом запускайте этот скрипт.
#
# 🔴 STRUCTURAL BASELINE НЕ ВОЗВРАЩАЕТСЯ ЭТИМ СКРИПТОМ.
#    Константа wb_mart в sql/ref/pr_ref_cogs_validation.sql принадлежит
#    конкретному commit/checkpoint. Безусловное «40 -> 39» затёрло бы
#    последующие легитимные изменения структуры. Откат репозитория —
#    только git revert коммита Stage 3.1C PR2.
#
# --dry-run работает БЕЗ Google Cloud SDK: печатает план и точный SQL,
#    не выполняет ни одного DDL, возвращает exit 0.
# ============================================================================
set -euo pipefail

PROJECT="project-fa311fc0-4d87-4781-986"
DATASET="wb_mart"
DRY_RUN=0
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=1

OBJECTS=(
  "V_DASH_FINANCE_CORRECTED_DAILY"
)

echo "=== Stage 3.1C PR2 rollback ==="
echo "project : ${PROJECT}"
echo "dataset : ${DATASET}"
echo "mode    : $([[ ${DRY_RUN} -eq 1 ]] && echo 'DRY-RUN (ни одного DDL)' || echo 'EXECUTE')"
echo
echo "--- ПРЕДУСЛОВИЯ ---"
echo "  1. wb_mart содержит 40 объектов (39 до Stage 3.1C PR2 + 1 новый)."
echo "  2. Ни один другой объект wb_mart не ссылается на удаляемый view."
echo "  3. Карточки Metabase 51/53/54 читают V_DASH_KPI_DAILY, а не этот слой."
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
       COUNTIF(table_name = 'V_DASH_FINANCE_CORRECTED_DAILY') AS stage_3_1c_pr2_objects
       FROM \`${PROJECT}.${DATASET}.INFORMATION_SCHEMA.TABLES\`"
if [[ ${DRY_RUN} -eq 1 ]]; then
  echo "[dry-run] ${CHECK}"
  echo "[dry-run] ожидание после реального отката: wb_mart_objects = 39, stage_3_1c_pr2_objects = 0"
else
  bq --project_id="${PROJECT}" query --nouse_legacy_sql "${CHECK}"
fi

echo
echo "Откат репозитория (вручную, отдельно): git revert <commit Stage 3.1C PR2>."
echo "Он вернёт и structural baseline в состояние того checkpoint."
exit 0
