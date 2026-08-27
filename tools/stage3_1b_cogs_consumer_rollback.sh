#!/usr/bin/env bash
# ============================================================================
# STAGE 3.1B PR1 — ОТКАТ CONSUMER-СЛОЯ PRODUCT COGS
# Дата: 2026-08-27.  Сборка: sql/mart/pr_cogs_consumer_v1.sql
#
# 🔴 ОТКАТ STAGE-SCOPED. Скрипт удаляет РОВНО ДВА объекта, созданных этим PR,
#    и НИЧЕГО больше. Он сознательно НЕ трогает:
#      • FACT_FINANCE, FACT_SALES, FACT_ORDERS, FACT_* — не пересобираются;
#      • MART_SKU_DAILY и процедуры sp_bootstrap_facts / sp_build_mart_sku_daily;
#      • V_DASH_* — числа на живых экранах этим PR не менялись и не меняются;
#      • evetis_ref (Stage 3.1A) — ни одной строки;
#      • REF_COST_MAP, RAW_*, Google Sheets, Apps Script, Metabase.
#
# 🔴 STRUCTURAL BASELINE НЕ ВОЗВРАЩАЕТСЯ ЭТИМ СКРИПТОМ.
#    Константа wb_mart в sql/ref/pr_ref_cogs_validation.sql принадлежит
#    КОНКРЕТНОМУ commit/checkpoint, а не этому скрипту. Безусловное «37 -> 35»
#    затёрло бы последующие легитимные изменения структуры.
#    Откат репозитория выполняется только через git revert коммита Stage 3.1B PR1.
# ============================================================================
set -euo pipefail

PROJECT="project-fa311fc0-4d87-4781-986"
DRY_RUN=0
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=1

OBJECTS=(
  "${PROJECT}.wb_mart.V_MART_SKU_DAILY_COGS"
  "${PROJECT}.wb_mart.V_FACT_FINANCE_COGS"
)

echo "=== Stage 3.1B PR1 rollback ==="
echo "project : ${PROJECT}"
echo "mode    : $([[ ${DRY_RUN} -eq 1 ]] && echo 'DRY-RUN (ни одного DDL)' || echo 'EXECUTE')"
echo

# Порядок важен: V_MART_SKU_DAILY_COGS читает V_FACT_FINANCE_COGS.
for OBJ in "${OBJECTS[@]}"; do
  STMT="DROP VIEW IF EXISTS \`${OBJ}\`"
  if [[ ${DRY_RUN} -eq 1 ]]; then
    echo "[dry-run] ${STMT}"
  else
    echo "[execute] ${STMT}"
    bq --project_id="${PROJECT}" query --nouse_legacy_sql --format=none "${STMT}"
  fi
done

echo
echo "=== Проверка после отката ==="
CHECK="SELECT COUNT(*) AS wb_mart_objects,
       COUNTIF(table_name IN ('V_FACT_FINANCE_COGS','V_MART_SKU_DAILY_COGS')) AS stage_3_1b_objects
       FROM \`${PROJECT}.wb_mart.INFORMATION_SCHEMA.TABLES\`"
if [[ ${DRY_RUN} -eq 1 ]]; then
  echo "[dry-run] ${CHECK}"
  echo "[dry-run] ожидание после реального отката: stage_3_1b_objects = 0"
else
  bq --project_id="${PROJECT}" query --nouse_legacy_sql "${CHECK}"
fi

echo
echo "Откат репозитория (вручную, отдельно): git revert <commit Stage 3.1B PR1>."
echo "Он вернёт и structural baseline в состояние того checkpoint."
