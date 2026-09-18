#!/usr/bin/env bash
# =====================================================================================
# EVETIS OWNER CONTROL TOWER — PHASE 1 — ОТКАТ
# =====================================================================================
# Что делает: удаляет ТОЛЬКО производные объекты Control Tower Phase 1 — 3 процедуры
#             evetis_ref.sp_ct_* и 18 витрин wb_mart.V_CT_* (определения в
#             sql/control_tower/ct_04*.sql, ct_05_procedures.sql) — и архивирует
#             коллекцию Metabase «00 · Owner Control Tower».
#
# 🔴 R2D-3b (2026-09-18): ТАБЛИЦЫ evetis_ref.CT_* НЕ УДАЛЯЮТСЯ И НЕ МЕНЯЮТСЯ.
#   Прежняя версия удаляла 13 таблиц CT_*. Сегодня в них живут данные, которые
#   откат кода/витрин уничтожать не вправе:
#     • вводные владельца: CT_PLAN_VERSION, CT_OPEX, CT_STOCK_SNAPSHOT, CT_EXPIRY_BATCH
#       (живые значения отличаются от seed в Git);
#     • состояние действий: CT_OWNER_ACTION_QUEUE (генерация + решения владельца);
#     • история: CT_ACTION_STATUS_LOG, CT_REFRESH_LOG, CT_INVENTORY_SNAPSHOT_DAILY;
#     • настройки: CT_CONFIG (Phase 1.1);
#     • план: CT_SEASON_PLAN_MONTHLY, CT_BUNDLE_PLAN, CT_DAILY_CURVE, CT_SEASON_PLAN —
#       воспроизводимы, но удалять их при откате витрин незачем;
#     • CT_ACTUAL_DAILY — производная, но тоже сохраняется: без процедур её никто не
#       пишет, а удаление ничего не даёт.
#   Удалить таблицы можно только отдельным решением владельца, вне этого скрипта.
#
# Чего НЕ делает: не трогает FACT_*, MART_*, V_DASH_*, V_ADV_COSTS, Stage B, REF_*,
#             ozon_mart, evetis_ops, загрузчики Apps Script, дашборды Metabase 1–4.
#
# Порядок слоёв: Phase 1 откатывается ПОСЛЕДНЕЙ. Скрипт отказывается работать, пока в
#   BigQuery есть объекты Phase 1.1 (V_CT_REFRESH_STATUS, V_CT_SKU_CONTROL,
#   V_CT_DAILY_BRIEF_LINES, V_CT_DAILY_BRIEF) или любой объект вне отката ссылается
#   на удаляемые витрины/процедуры. Сначала tools/ct_phase12_rollback.sh, затем
#   tools/ct_phase11_rollback.sh (он же убирает планировщик ct_refresh).
#
# Использование:
#   tools/ct_phase1_rollback.sh                       # dry-run: только план, bq не вызывается
#   tools/ct_phase1_rollback.sh --dry-run             # то же
#   tools/ct_phase1_rollback.sh --bigquery --confirm  # откат BigQuery (после проверок)
#   tools/ct_phase1_rollback.sh --metabase --confirm  # откат Metabase
#   tools/ct_phase1_rollback.sh --all --confirm       # полный откат
#   --confirm без --bigquery/--metabase/--all — отказ.
#
# Metabase: требуется профиль mb (по умолчанию $MB_PROFILE или evetis-dev).
# =====================================================================================
set -euo pipefail

PROJECT="${BQ_PROJECT:-project-fa311fc0-4d87-4781-986}"
PROFILE="${MB_PROFILE:-evetis-dev}"
MB_COLLECTION_ID="${MB_COLLECTION_ID:-9}"
MB_DASHBOARDS=(5 6)
MB_CARDS=(97 98 99 100 101 102 103 104 105 106 107 108 109 110 111 112 113 114 115 116)

# Производные объекты Phase 1 — единственные цели удаления.
CT_PROCS=(sp_ct_refresh_daily sp_ct_generate_actions sp_ct_action_update)
# Порядок: сверху вниз по зависимостям.
CT_VIEWS=(V_CT_OWNER_HOME V_CT_ACTION_QUEUE V_CT_ACTION_CANDIDATES V_CT_ATTENTION
          V_CT_HAND_CREAM_CONTROL V_CT_SUPPLY_NEED V_CT_BUNDLE_STATUS V_CT_CASH_CONVERSION
          V_CT_PLAN_VS_ACTUAL_DAILY V_CT_FRESHNESS V_CT_INVENTORY_TRUTH V_CT_INVENTORY_TRUTH_LIVE
          V_CT_SUPPLY_FLOW V_CT_PHYSICAL_DAILY V_CT_ACTUAL_DAILY V_CT_ACTUAL_DAILY_LIVE
          V_CT_PLAN_ACTIVE V_CT_BOM_CURRENT)
# Объекты Phase 1.1: пока они есть, откат Phase 1 запрещён.
LATER_LAYER_VIEWS=(V_CT_REFRESH_STATUS V_CT_SKU_CONTROL V_CT_DAILY_BRIEF_LINES V_CT_DAILY_BRIEF)
# Таблицы, которые откат сохраняет (перечень для отчёта; скрипт к ним не обращается).
CT_TABLES_RETAINED=(CT_PLAN_VERSION CT_OPEX CT_STOCK_SNAPSHOT CT_EXPIRY_BATCH CT_CONFIG
          CT_OWNER_ACTION_QUEUE CT_ACTION_STATUS_LOG CT_REFRESH_LOG CT_INVENTORY_SNAPSHOT_DAILY
          CT_SEASON_PLAN_MONTHLY CT_BUNDLE_PLAN CT_DAILY_CURVE CT_SEASON_PLAN CT_ACTUAL_DAILY)
# Датасеты, в которых ищутся внешние ссылки на удаляемые объекты.
DEP_DATASETS=(wb_raw wb_mart wb_ops evetis_ref evetis_ops evetis_communications ozon_raw ozon_mart)

DRY_RUN=1
DO_BQ=0
DO_MB=0
SCOPE_GIVEN=0

for arg in "$@"; do
  case "$arg" in
    --dry-run)  DRY_RUN=1 ;;
    --confirm)  DRY_RUN=0 ;;
    --bigquery) DO_BQ=1; SCOPE_GIVEN=1 ;;
    --metabase) DO_MB=1; SCOPE_GIVEN=1 ;;
    --all)      DO_BQ=1; DO_MB=1; SCOPE_GIVEN=1 ;;
    *) echo "Неизвестный аргумент: $arg" >&2; exit 2 ;;
  esac
done
if [[ $DRY_RUN -eq 0 && $SCOPE_GIVEN -eq 0 ]]; then
  echo "ОТКАЗ: --confirm требует явного --bigquery, --metabase или --all." >&2
  exit 2
fi
if [[ $DO_BQ -eq 0 && $DO_MB -eq 0 ]]; then DO_BQ=1; DO_MB=1; fi

refuse() { echo "ОТКАЗ: $*" >&2; echo "Ничего не изменено." >&2; exit 4; }

run() {
  if [[ $DRY_RUN -eq 1 ]]; then echo "  [dry-run] $*"; else echo "  → $*"; "$@"; fi
}
bq_run() {
  if [[ $DRY_RUN -eq 1 ]]; then echo "  [dry-run] bq query: ${1:0:110}..."; else bq query --use_legacy_sql=false --project_id="$PROJECT" "$1"; fi
}
# Read-only проверка: запрос обязан вернуть одно целое число. Любая ошибка — отказ.
bq_scalar() {
  local out val
  out=$(bq query --use_legacy_sql=false --format=csv --quiet --project_id="$PROJECT" "$1") \
    || refuse "проверка не выполнилась: ${2}"
  val=$(printf '%s\n' "$out" | tail -n 1 | tr -d '[:space:]')
  [[ "$val" =~ ^[0-9]+$ ]] || refuse "проверка вернула не число (${2}): ${val}"
  printf '%s' "$val"
}
join_by() { local IFS="$1"; shift; echo "$*"; }

preflight_bigquery() {
  command -v bq >/dev/null 2>&1 || refuse "bq не найден — проверки выполнить нельзя."
  echo "--- Предпроверки (только чтение) ---"

  local later n
  later="'$(join_by , "${LATER_LAYER_VIEWS[@]}" | sed "s/,/','/g")'"
  n=$(bq_scalar "SELECT COUNT(*) FROM \`${PROJECT}.wb_mart.INFORMATION_SCHEMA.TABLES\` WHERE table_name IN (${later})" "объекты Phase 1.1")
  [[ "$n" == "0" ]] || refuse "в wb_mart есть ${n} объект(ов) Phase 1.1. Сначала tools/ct_phase12_rollback.sh и tools/ct_phase11_rollback.sh."
  echo "  OK: объектов Phase 1.1 нет"

  local views_re procs_re excl_views excl_procs parts=() d
  views_re="wb_mart\\.($(join_by '|' "${CT_VIEWS[@]}"))\\b"
  procs_re="evetis_ref\\.($(join_by '|' "${CT_PROCS[@]}"))\\b"
  excl_views="'$(join_by , "${CT_VIEWS[@]}" | sed "s/,/','/g")'"
  excl_procs="'$(join_by , "${CT_PROCS[@]}" | sed "s/,/','/g")'"
  for d in "${DEP_DATASETS[@]}"; do
    parts+=("SELECT '${d}' AS ds, table_name AS obj, view_definition AS body FROM \`${PROJECT}.${d}.INFORMATION_SCHEMA.VIEWS\`")
    parts+=("SELECT '${d}', routine_name, routine_definition FROM \`${PROJECT}.${d}.INFORMATION_SCHEMA.ROUTINES\`")
  done
  n=$(bq_scalar "WITH o AS ($(join_by ' ' "${parts[@]/%/ UNION ALL}" | sed 's/ UNION ALL$//'))
SELECT COUNT(*) FROM o
WHERE (REGEXP_CONTAINS(body, r'${views_re}') OR REGEXP_CONTAINS(body, r'${procs_re}'))
  AND NOT ((ds = 'wb_mart' AND obj IN (${excl_views})) OR (ds = 'evetis_ref' AND obj IN (${excl_procs})))" "внешние зависимости")
  [[ "$n" == "0" ]] || refuse "${n} объект(ов) вне отката ссылаются на удаляемые витрины/процедуры."
  echo "  OK: внешних ссылок на удаляемые объекты нет"
}

echo "=== CONTROL TOWER PHASE 1 ROLLBACK ==="
[[ $DRY_RUN -eq 1 ]] && echo "РЕЖИМ: dry-run (ничего не удаляется, bq не вызывается). Для реального отката: --bigquery|--metabase|--all --confirm."
echo

# ── BigQuery ─────────────────────────────────────────────────────────────────────────
if [[ $DO_BQ -eq 1 ]]; then
  if [[ $DRY_RUN -eq 1 ]]; then
    echo "--- Предпроверки (при --confirm, только чтение) ---"
    echo "  [dry-run] нет объектов Phase 1.1: $(join_by ' ' "${LATER_LAYER_VIEWS[@]}")"
    echo "  [dry-run] нет внешних ссылок на удаляемые объекты в: $(join_by ' ' "${DEP_DATASETS[@]}")"
  else
    preflight_bigquery
  fi

  echo "--- BigQuery: процедуры ---"
  for p in "${CT_PROCS[@]}"; do
    bq_run "DROP PROCEDURE IF EXISTS \`${PROJECT}.evetis_ref.${p}\`"
  done

  echo "--- BigQuery: витрины wb_mart.V_CT_* ---"
  for v in "${CT_VIEWS[@]}"; do
    bq_run "DROP VIEW IF EXISTS \`${PROJECT}.wb_mart.${v}\`"
  done

  echo "--- BigQuery: таблицы evetis_ref.CT_* СОХРАНЯЮТСЯ (не удаляются, не меняются) ---"
  echo "  $(join_by ' ' "${CT_TABLES_RETAINED[@]}")"

  echo "--- BigQuery: проверка, что production цел (только чтение) ---"
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
