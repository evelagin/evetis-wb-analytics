#!/usr/bin/env bash
# ============================================================================
# STAGE 3.1F — ОТКАТ КЛАССИФИКАЦИИ УДЕРЖАНИЙ ПО ПРЯМЫМ МЕТКАМ WB
# Дата: 2026-08-28.  Сборка: sql/mart/pr_deductions_direct_labels_v1.sql
#                    плюс правка sql/finance/v_wb_finance_semantic.sql
#
# 🔴 ЭТОТ ОТКАТ НЕ УДАЛЯЕТ ОБЪЕКТЫ. Stage 3.1F не создал ни одного нового
#    объекта: он переопределил четыре существующих view. Поэтому откат —
#    это ВОССТАНОВЛЕНИЕ прежних определений из снимка, а не DROP.
#
#    Снимок снят ДО изменений и лежит вне репозитория:
#      ~/.claude/evetis-rollback/stage3_1f/rollback_views_BEFORE.sql
#    Он содержит CREATE OR REPLACE VIEW для:
#      wb_raw.V_WB_FINANCE_SEMANTIC
#      wb_mart.V_WB_DEDUCTIONS_CLASSIFIED
#      wb_mart.V_ADVERTISING_RECONCILIATION_DAILY
#      wb_mart.V_DASH_FINANCE_CORRECTED_DAILY
#
# 🔴 ПОРЯДОК ИМЕЕТ ЗНАЧЕНИЕ. Если карточки Metabase уже переключены на новые
#    поля (utilization_rub, exceptional_compensation_rub и прочие), СНАЧАЛА
#    верните карточку 51 из снимка ~/.claude/evetis-rollback/stage3_1f/,
#    и только потом откатывайте BigQuery. Иначе карточка отдаст ошибку.
#
# 🔴 ЧЕГО ОТКАТ НЕ ТРОГАЕТ: evetis_ref, V_FACT_FINANCE_COGS,
#    V_MART_SKU_DAILY_COGS, V_DASH_EXECUTIVE_ECONOMICS_DAILY (он читает
#    corrected-слой и вернётся к прежним числам сам), FACT_*, MART_SKU_DAILY,
#    REF_COST_MAP, SKU Performance, сырой settlement.
#
# 🔴 ВЫПЛАТА WB ОТ ОТКАТА НЕ МЕНЯЕТСЯ. Она считается из сырых полей и от
#    классификации не зависит — ни до, ни после Stage 3.1F.
#
# --dry-run работает БЕЗ Google Cloud SDK: печатает план и проверяет наличие
#    снимка, не выполняет ни одного DDL, возвращает exit 0.
# ============================================================================
set -euo pipefail

PROJECT="${BQ_PROJECT:-project-fa311fc0-4d87-4781-986}"
SNAPSHOT="${STAGE31F_SNAPSHOT:-$HOME/.claude/evetis-rollback/stage3_1f/rollback_views_BEFORE.sql}"
DRY_RUN=0
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=1

echo "=== Stage 3.1F rollback ==="
echo "project : ${PROJECT}"
echo "снимок  : ${SNAPSHOT}"
echo "mode    : $([[ ${DRY_RUN} -eq 1 ]] && echo 'DRY-RUN (ни одного DDL)' || echo 'EXECUTE')"
echo
echo "--- ПРЕДУСЛОВИЯ ---"
echo "  1. Карточка Metabase 51 возвращена на прежний SQL (если была переключена)."
echo "  2. wb_mart содержит 41 объект, wb_raw 56, evetis_ref 4 — новых объектов нет."
echo "  3. Снимок прежних определений доступен и читаем."
echo

if [[ ! -f "${SNAPSHOT}" ]]; then
  echo "ОШИБКА: снимок прежних определений не найден: ${SNAPSHOT}" >&2
  echo "Без него откат невозможен: Stage 3.1F переопределил существующие view," >&2
  echo "и восстановить их можно только из сохранённого DDL или git-истории." >&2
  exit 1
fi
echo "снимок найден: $(wc -c < "${SNAPSHOT}") байт"

if [[ ${DRY_RUN} -eq 1 ]]; then
  echo
  echo "[dry-run] был бы выполнен файл целиком:"
  grep -n 'CREATE OR REPLACE VIEW' "${SNAPSHOT}" | sed 's/^/[dry-run]   /'
  echo
  echo "[dry-run] ожидание после реального отката:"
  echo "[dry-run]   V_WB_DEDUCTIONS_CLASSIFIED  — классы AD_BILLING_RECONSTRUCTED / TRANSIT / UNCLASSIFIED"
  echo "[dry-run]   billed advertising 01-26.08 — снова 52 875,00 (с ошибочными 38 руб. утилизации)"
  echo "[dry-run]   operating result 01-26.08   — снова 90 344,47"
  echo "[dry-run]   выплата WB 27.07-23.08      — 110 101,96 (не меняется ни при каком исходе)"
  exit 0
fi

echo
echo "[execute] восстановление прежних определений из снимка"
bq --project_id="${PROJECT}" query --nouse_legacy_sql --format=none < "${SNAPSHOT}"

echo
echo "--- ПОСТУСЛОВИЯ ---"
CHECK="SELECT COUNT(*) AS wb_mart_objects,
       COUNTIF(table_name IN ('V_WB_DEDUCTIONS_CLASSIFIED','V_ADVERTISING_RECONCILIATION_DAILY','V_DASH_FINANCE_CORRECTED_DAILY')) AS stage_3_1f_views,
       COUNTIF(table_name IN ('V_FACT_FINANCE_COGS','V_MART_SKU_DAILY_COGS')) AS stage_3_1b_views,
       COUNTIF(table_name = 'V_DASH_EXECUTIVE_ECONOMICS_DAILY') AS stage_3_1d_views
       FROM \`${PROJECT}.wb_mart.INFORMATION_SCHEMA.TABLES\`"
bq --project_id="${PROJECT}" query --nouse_legacy_sql "${CHECK}"

echo
echo "Откат репозитория (вручную, отдельно): git revert <commit Stage 3.1F>."
exit 0
