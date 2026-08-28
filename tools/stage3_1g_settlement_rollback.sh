#!/usr/bin/env bash
# ============================================================================
# STAGE 3.1G — ОТКАТ БЛОКА «РАСЧЁТЫ С WILDBERRIES»
# Дата: 2026-08-28.  Сборка: sql/dash/pr_dash_settlement_v1.sql
#                    плюс правка sql/finance/v_wb_finance_semantic.sql
#
# 🔴 ДВЕ РАЗНЫЕ ОПЕРАЦИИ. Stage 3.1G сделал два вида изменений:
#      1. СОЗДАЛ новый объект wb_mart.V_DASH_SETTLEMENT_DAILY -> его DROP;
#      2. ПЕРЕОПРЕДЕЛИЛ wb_raw.V_WB_FINANCE_SEMANTIC (добавлено поле
#         loyalty_points_rub) -> восстановление из снимка, а не DROP.
#
#    Снимок прежнего определения снят ДО изменений и лежит вне репозитория:
#      ~/.claude/evetis-rollback/stage3_1g/rollback_views_BEFORE.sql
#
# 🔴 ПОРЯДОК ИМЕЕТ ЗНАЧЕНИЕ. Если карточки Metabase уже читают новый слой,
#    СНАЧАЛА верните карточку 49 и удалите новые карточки блока «Расчёты
#    с Wildberries», и только потом откатывайте BigQuery. Иначе карточки
#    отдадут ошибку вместо чисел. Снимки карточек — в том же каталоге.
#
# 🔴 ЧЕГО ОТКАТ НЕ ТРОГАЕТ: V_DASH_KPI_DAILY, V_DASH_FINANCE_CORRECTED_DAILY,
#    V_DASH_EXECUTIVE_ECONOMICS_DAILY, классификатор удержаний Stage 3.1F,
#    объекты Product COGS, REF_COST_MAP, FACT_*, MART_SKU_DAILY,
#    SKU Performance. Управленческие числа Executive этим слоем не
#    затрагивались и от отката не изменятся.
#
# 🔴 ВЫПЛАТА WB — ЭТО ФАКТ ОТЧЁТОВ, А НЕ СЛЕДСТВИЕ СЛОЯ. Откат убирает её
#    с экрана, но не меняет ни одной цифры в источнике.
#
# --dry-run работает БЕЗ Google Cloud SDK: печатает план, проверяет наличие
#    снимка, не выполняет ни одного DDL, возвращает exit 0.
# ============================================================================
set -euo pipefail

PROJECT="${BQ_PROJECT:-project-fa311fc0-4d87-4781-986}"
SNAPSHOT="${STAGE31G_SNAPSHOT:-$HOME/.claude/evetis-rollback/stage3_1g/rollback_views_BEFORE.sql}"
DRY_RUN=0
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=1

NEW_OBJECT="V_DASH_SETTLEMENT_DAILY"

echo "=== Stage 3.1G rollback ==="
echo "project : ${PROJECT}"
echo "снимок  : ${SNAPSHOT}"
echo "mode    : $([[ ${DRY_RUN} -eq 1 ]] && echo 'DRY-RUN (ни одного DDL)' || echo 'EXECUTE')"
echo
echo "--- ПРЕДУСЛОВИЯ ---"
echo "  1. Карточки блока «Расчёты с Wildberries» сняты с дашборда 2,"
echo "     карточка 49 возвращена из снимка rollback_card_49_BEFORE.json."
echo "  2. wb_mart содержит 42 объекта (41 до Stage 3.1G + 1 новый)."
echo "  3. Снимок прежнего определения V_WB_FINANCE_SEMANTIC доступен."
echo

if [[ ! -f "${SNAPSHOT}" ]]; then
  echo "ОШИБКА: снимок прежнего определения не найден: ${SNAPSHOT}" >&2
  echo "Без него откат V_WB_FINANCE_SEMANTIC невозможен: этап переопределил" >&2
  echo "существующий view, и вернуть его можно только из снимка или git-истории." >&2
  exit 1
fi
echo "снимок найден: $(wc -c < "${SNAPSHOT}") байт"

STMT="DROP VIEW IF EXISTS \`${PROJECT}.wb_mart.${NEW_OBJECT}\`"
if [[ ${DRY_RUN} -eq 1 ]]; then
  echo
  echo "[dry-run] 1) ${STMT}"
  echo "[dry-run] 2) восстановление из снимка:"
  grep -n 'CREATE OR REPLACE VIEW' "${SNAPSHOT}" | sed 's/^/[dry-run]      /'
  echo
  echo "[dry-run] ожидание после реального отката:"
  echo "[dry-run]   wb_mart_objects = 41, ${NEW_OBJECT} отсутствует"
  echo "[dry-run]   V_WB_FINANCE_SEMANTIC без поля loyalty_points_rub"
  echo "[dry-run]   управленческие числа Executive не меняются"
  exit 0
fi

echo
echo "[execute] ${STMT}"
bq --project_id="${PROJECT}" query --nouse_legacy_sql --format=none "${STMT}"

echo "[execute] восстановление прежнего определения V_WB_FINANCE_SEMANTIC"
bq --project_id="${PROJECT}" query --nouse_legacy_sql --format=none < "${SNAPSHOT}"

echo
echo "--- ПОСТУСЛОВИЯ ---"
CHECK="SELECT COUNT(*) AS wb_mart_objects,
       COUNTIF(table_name = '${NEW_OBJECT}') AS stage_3_1g_objects,
       COUNTIF(table_name IN ('V_DASH_KPI_DAILY','V_DASH_FINANCE_CORRECTED_DAILY','V_DASH_EXECUTIVE_ECONOMICS_DAILY')) AS executive_layers
       FROM \`${PROJECT}.wb_mart.INFORMATION_SCHEMA.TABLES\`"
bq --project_id="${PROJECT}" query --nouse_legacy_sql "${CHECK}"

echo
echo "Откат репозитория (вручную, отдельно): git revert <commit Stage 3.1G>."
exit 0
