#!/usr/bin/env bash
# =====================================================================================
# EVETIS OWNER CONTROL TOWER — PHASE 1 — ОТКАТ
# =====================================================================================
# Что делает: удаляет ТОЛЬКО объекты Control Tower (CT_* / V_CT_* / sp_ct_*) в BigQuery
#             и архивирует коллекцию Metabase «00 · Owner Control Tower».
# Чего НЕ делает: не трогает ни один существующий объект — FACT_*, MART_*, V_DASH_*,
#             V_ADV_COSTS, Stage B, REF_*, ozon_mart, загрузчики Apps Script,
#             дашборды Metabase 1–4 и их карточки.
# После отката вся существующая аналитика EVETIS работает без изменений.
#
# Использование:
#   tools/ct_phase1_rollback.sh --dry-run          # показать план, ничего не делать
#   tools/ct_phase1_rollback.sh --bigquery         # откатить только BigQuery
#   tools/ct_phase1_rollback.sh --metabase         # откатить только Metabase
#   tools/ct_phase1_rollback.sh --all --confirm    # полный откат
#
# Metabase: требуется профиль mb (по умолчанию $MB_PROFILE или evetis-dev).
# =====================================================================================
set -euo pipefail

PROJECT="${BQ_PROJECT:-project-fa311fc0-4d87-4781-986}"
PROFILE="${MB_PROFILE:-evetis-dev}"
MB_COLLECTION_ID="${MB_COLLECTION_ID:-9}"
MB_DASHBOARDS=(5 6)
MB_CARDS=(97 98 99 100 101 102 103 104 105 106 107 108 109 110 111 112 113 114 115 116)

DRY_RUN=1
DO_BQ=0
DO_MB=0

for arg in "$@"; do
  case "$arg" in
    --dry-run)  DRY_RUN=1 ;;
    --confirm)  DRY_RUN=0 ;;
    --bigquery) DO_BQ=1 ;;
    --metabase) DO_MB=1 ;;
    --all)      DO_BQ=1; DO_MB=1 ;;
    *) echo "Неизвестный аргумент: $arg" >&2; exit 2 ;;
  esac
done
if [[ $DO_BQ -eq 0 && $DO_MB -eq 0 ]]; then DO_BQ=1; DO_MB=1; fi

run() {
  if [[ $DRY_RUN -eq 1 ]]; then echo "  [dry-run] $*"; else echo "  → $*"; "$@"; fi
}
bq_run() {
  if [[ $DRY_RUN -eq 1 ]]; then echo "  [dry-run] bq query: ${1:0:110}..."; else bq query --use_legacy_sql=false --project_id="$PROJECT" "$1"; fi
}

echo "=== CONTROL TOWER PHASE 1 ROLLBACK ==="
[[ $DRY_RUN -eq 1 ]] && echo "РЕЖИМ: dry-run (ничего не удаляется). Для реального отката добавьте --confirm."
echo

# ── BigQuery ─────────────────────────────────────────────────────────────────────────
if [[ $DO_BQ -eq 1 ]]; then
  echo "--- BigQuery: процедуры ---"
  for p in sp_ct_refresh_daily sp_ct_generate_actions sp_ct_action_update; do
    bq_run "DROP PROCEDURE IF EXISTS \`${PROJECT}.evetis_ref.${p}\`"
  done

  echo "--- BigQuery: витрины wb_mart.V_CT_* ---"
  # Порядок: сверху вниз по зависимостям.
  for v in V_CT_OWNER_HOME V_CT_ACTION_QUEUE V_CT_ACTION_CANDIDATES V_CT_ATTENTION \
           V_CT_HAND_CREAM_CONTROL V_CT_SUPPLY_NEED V_CT_BUNDLE_STATUS V_CT_CASH_CONVERSION \
           V_CT_PLAN_VS_ACTUAL_DAILY V_CT_FRESHNESS V_CT_INVENTORY_TRUTH V_CT_INVENTORY_TRUTH_LIVE \
           V_CT_SUPPLY_FLOW V_CT_PHYSICAL_DAILY V_CT_ACTUAL_DAILY V_CT_ACTUAL_DAILY_LIVE \
           V_CT_PLAN_ACTIVE V_CT_BOM_CURRENT; do
    bq_run "DROP VIEW IF EXISTS \`${PROJECT}.wb_mart.${v}\`"
  done

  echo "--- BigQuery: таблицы evetis_ref.CT_* ---"
  for t in CT_INVENTORY_SNAPSHOT_DAILY CT_ACTUAL_DAILY CT_REFRESH_LOG CT_ACTION_STATUS_LOG \
           CT_OWNER_ACTION_QUEUE CT_BUNDLE_PLAN CT_STOCK_SNAPSHOT CT_EXPIRY_BATCH CT_OPEX \
           CT_SEASON_PLAN CT_DAILY_CURVE CT_SEASON_PLAN_MONTHLY CT_PLAN_VERSION; do
    bq_run "DROP TABLE IF EXISTS \`${PROJECT}.evetis_ref.${t}\`"
  done

  echo "--- BigQuery: проверка, что production цел ---"
  bq_run "SELECT COUNT(*) AS wb_dash_rows FROM \`${PROJECT}.wb_mart.V_DASH_SKU_DAILY\`"
  bq_run "SELECT COUNT(*) AS adv_costs_rows FROM \`${PROJECT}.wb_raw.V_ADV_COSTS\`"
fi

# ── Metabase ─────────────────────────────────────────────────────────────────────────
if [[ $DO_MB -eq 1 ]]; then
  echo
  echo "--- Metabase: дашборды Control Tower ---"
  for d in "${MB_DASHBOARDS[@]}"; do run mb --profile "$PROFILE" dashboard archive "$d"; done

  echo "--- Metabase: карточки Control Tower ---"
  for c in "${MB_CARDS[@]}"; do run mb --profile "$PROFILE" card archive "$c"; done

  echo "--- Metabase: коллекция ---"
  run mb --profile "$PROFILE" collection archive "$MB_COLLECTION_ID"

  echo "--- Metabase: проверка, что существующие дашборды целы ---"
  for d in 2 3; do run mb --profile "$PROFILE" dashboard get "$d" --format json --max-bytes 0; done
fi

echo
echo "=== ГОТОВО ==="
[[ $DRY_RUN -eq 1 ]] && echo "Это был dry-run. Реальный откат: tools/ct_phase1_rollback.sh --all --confirm"
exit 0
