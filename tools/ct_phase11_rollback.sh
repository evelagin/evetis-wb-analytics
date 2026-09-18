#!/usr/bin/env bash
# =====================================================================================
# EVETIS OWNER CONTROL TOWER — PHASE 1.1 — ОТКАТ К СОСТОЯНИЮ PHASE 1 (коммит 075f45a)
# =====================================================================================
# Что делает:
#   BigQuery  — удаляет витрины Phase 1.1 (V_CT_REFRESH_STATUS, V_CT_SKU_CONTROL,
#               V_CT_DAILY_BRIEF_LINES, V_CT_DAILY_BRIEF) и возвращает Phase 1-определения
#               10 витрин ct_04b (`git show 075f45a:sql/control_tower/ct_04b_views_owner.sql`,
#               только CREATE OR REPLACE VIEW, blob-хэш закреплён ниже).
#               Процедуры sp_ct_* НЕ откатываются: sp_ct_generate_actions и sp_ct_action_update
#               в Phase 1 и сейчас побайтно совпадают, а текущая sp_ct_refresh_daily совместима
#               с витринами Phase 1. Историческая версия Phase 1 использовала TRUNCATE, которого
#               потабличный dataEditor у sa-ct-refresh может не хватить: планировщик ct-refresh-prod
#               (5 раз в сутки) падал бы до записи CT_INVENTORY_SNAPSHOT_DAILY — невосполнимые
#               пропуски истории.
#   Metabase  — печатает инструкцию: выполнить tools/metabase_ct_phase11_rollback.js в консоли
#               браузера на localhost:3000 (восстанавливает карточки 97–116 и раскладку
#               дашбордов 5/6, архивирует карточки и дашборды Phase 1.1).
#   Terraform — печатает инструкцию: удалить infra/terraform/ct_refresh.tf и ключ ct_refresh
#               в iam.tf, затем workflow `infra` action=apply (удалит SA, IAM и scheduler).
#
# 🔴 R2D-3b (2026-09-18): таблица evetis_ref.CT_CONFIG БОЛЬШЕ НЕ УДАЛЯЕТСЯ. В ней настройки
#   владельца (в т.ч. адрес web-app), которых нет в Git; текущая sp_ct_refresh_daily
#   продолжает её читать. Ни одна таблица CT_* этим скриптом не удаляется и не меняется.
#
# Порядок слоёв: Phase 1.1 нельзя откатывать под установленной Phase 1.2. Phase 1.2 не
#   создаёт новых объектов BigQuery, но добавляет в витрины V_CT_* колонки владельца
#   (why_owner, effect_line, effect_kind_ru, status_ru, channel_ru, kind_ru). Пока хотя бы
#   одна из них есть — отказ: сначала tools/ct_phase12_rollback.sh. Также отказ, если любой
#   объект вне отката ссылается на удаляемые витрины.
#
# Использование:
#   tools/ct_phase11_rollback.sh                       # dry-run: только план, bq не вызывается
#   tools/ct_phase11_rollback.sh --dry-run             # то же
#   tools/ct_phase11_rollback.sh --bigquery --confirm  # откат BigQuery (после проверок)
#   tools/ct_phase11_rollback.sh --all --confirm
#   --confirm без --bigquery/--metabase/--all — отказ. Запускать из корня репозитория.
# =====================================================================================
set -euo pipefail

PROJECT="${BQ_PROJECT:-project-fa311fc0-4d87-4781-986}"
BASE_COMMIT="075f45a"
# Закреплённые blob-хэши файлов Phase 1: скрипт применяет только проверенное содержимое.
EXPECTED_BLOB_04B="0ad3be32cfe1505fc4a5736221ec3a17f7cc5768"   # sql/control_tower/ct_04b_views_owner.sql

# Производные объекты Phase 1.1 — единственные цели удаления.
P11_VIEWS=(V_CT_DAILY_BRIEF V_CT_DAILY_BRIEF_LINES V_CT_SKU_CONTROL V_CT_REFRESH_STATUS)
# Переопределяются Phase 1-версиями (исключаются из проверки внешних ссылок).
REDEFINED_VIEWS=(V_CT_INVENTORY_TRUTH_LIVE V_CT_PLAN_VS_ACTUAL_DAILY V_CT_CASH_CONVERSION
          V_CT_BUNDLE_STATUS V_CT_SUPPLY_NEED V_CT_HAND_CREAM_CONTROL V_CT_ATTENTION
          V_CT_ACTION_CANDIDATES V_CT_ACTION_QUEUE V_CT_OWNER_HOME)
# Колонки, которые появляются только в Phase 1.2.
P12_MARKER_COLUMNS=(why_owner effect_line effect_kind_ru status_ru channel_ru kind_ru)
DEP_DATASETS=(wb_raw wb_mart wb_ops evetis_ref evetis_ops evetis_communications ozon_raw ozon_mart)

DRY_RUN=1; DO_BQ=0; DO_MB=0; SCOPE_GIVEN=0
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
join_by() { local IFS="$1"; shift; echo "$*"; }
in_list() { printf "'%s'," "$@" | sed 's/,$//'; }

bq_run() {
  if [[ $DRY_RUN -eq 1 ]]; then echo "  [dry-run] bq query: ${1:0:110}..."; else bq query --use_legacy_sql=false --project_id="$PROJECT" "$1"; fi
}
bq_file() {
  # $1 = git path, $2 = описание. Применяет Phase 1 версию файла целиком (CREATE OR REPLACE идемпотентен).
  if [[ $DRY_RUN -eq 1 ]]; then echo "  [dry-run] bq query < git show ${BASE_COMMIT}:$1   # $2"; else git show "${BASE_COMMIT}:$1" | bq query --use_legacy_sql=false --project_id="$PROJECT"; fi
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

check_phase1_sources() {
  local b04
  b04=$(git rev-parse --verify --quiet "${BASE_COMMIT}:sql/control_tower/ct_04b_views_owner.sql" || true)
  [[ "$b04" == "$EXPECTED_BLOB_04B" ]] \
    || refuse "файлы Phase 1 в ${BASE_COMMIT} не совпадают с закреплёнными (запускайте из корня репозитория EVETIS)."
}

preflight_bigquery() {
  command -v bq >/dev/null 2>&1 || refuse "bq не найден — проверки выполнить нельзя."
  check_phase1_sources
  echo "--- Предпроверки (только чтение) ---"

  local n
  n=$(bq_scalar "SELECT COUNT(*) FROM \`${PROJECT}.wb_mart.INFORMATION_SCHEMA.COLUMNS\`
WHERE STARTS_WITH(table_name, 'V_CT_') AND column_name IN ($(in_list "${P12_MARKER_COLUMNS[@]}"))" "контракт Phase 1.2")
  [[ "$n" == "0" ]] || refuse "Phase 1.2 установлена (${n} колонок-маркеров в V_CT_*). Сначала tools/ct_phase12_rollback.sh."
  echo "  OK: колонок Phase 1.2 нет"

  local views_re parts=() d
  views_re="wb_mart\\.($(join_by '|' "${P11_VIEWS[@]}"))\\b"
  for d in "${DEP_DATASETS[@]}"; do
    parts+=("SELECT '${d}' AS ds, table_name AS obj, view_definition AS body FROM \`${PROJECT}.${d}.INFORMATION_SCHEMA.VIEWS\`")
    parts+=("SELECT '${d}', routine_name, routine_definition FROM \`${PROJECT}.${d}.INFORMATION_SCHEMA.ROUTINES\`")
  done
  n=$(bq_scalar "WITH o AS ($(join_by ' ' "${parts[@]/%/ UNION ALL}" | sed 's/ UNION ALL$//'))
SELECT COUNT(*) FROM o
WHERE REGEXP_CONTAINS(body, r'${views_re}')
  AND NOT (ds = 'wb_mart' AND obj IN ($(in_list "${P11_VIEWS[@]}" "${REDEFINED_VIEWS[@]}")))" "внешние зависимости")
  [[ "$n" == "0" ]] || refuse "${n} объект(ов) вне отката ссылаются на удаляемые витрины Phase 1.1."
  echo "  OK: внешних ссылок на удаляемые витрины нет"
}

echo "=== CONTROL TOWER PHASE 1.1 ROLLBACK (→ Phase 1, ${BASE_COMMIT}) ==="
[[ $DRY_RUN -eq 1 ]] && echo "РЕЖИМ: dry-run (ничего не удаляется, bq не вызывается). Для реального отката: --bigquery|--metabase|--all --confirm."
echo

if [[ $DO_BQ -eq 1 ]]; then
  if [[ $DRY_RUN -eq 1 ]]; then
    echo "--- Предпроверки (при --confirm, только чтение) ---"
    echo "  [dry-run] blob-хэши файлов Phase 1 в ${BASE_COMMIT} совпадают с закреплёнными"
    echo "  [dry-run] в V_CT_* нет колонок Phase 1.2: $(join_by ' ' "${P12_MARKER_COLUMNS[@]}")"
    echo "  [dry-run] нет внешних ссылок на удаляемые витрины в: $(join_by ' ' "${DEP_DATASETS[@]}")"
  else
    preflight_bigquery
  fi
  echo "--- BigQuery: удаление витрин Phase 1.1 ---"
  for v in V_CT_DAILY_BRIEF V_CT_DAILY_BRIEF_LINES V_CT_SKU_CONTROL; do
    bq_run "DROP VIEW IF EXISTS \`${PROJECT}.wb_mart.${v}\`"
  done
  echo "--- BigQuery: возврат Phase 1-определений 10 витрин ct_04b (процедуры sp_ct_* не меняются) ---"
  bq_file sql/control_tower/ct_04b_views_owner.sql "владельческие витрины Phase 1 (Owner Home без ссылки на V_CT_REFRESH_STATUS)"
  echo "--- BigQuery: удаление оставшейся витрины Phase 1.1 ---"
  bq_run "DROP VIEW IF EXISTS \`${PROJECT}.wb_mart.V_CT_REFRESH_STATUS\`"
  echo "--- BigQuery: evetis_ref.CT_CONFIG и все таблицы CT_* СОХРАНЯЮТСЯ ---"
  echo "--- BigQuery: проверка, что Phase 1 жива (только чтение) ---"
  bq_run "SELECT COUNT(*) AS owner_home_rows FROM \`${PROJECT}.wb_mart.V_CT_OWNER_HOME\`"
  echo
fi

if [[ $DO_MB -eq 1 ]]; then
  echo "--- Metabase (ручной шаг, same-origin) ---"
  echo "  1. Открыть http://localhost:3000 под владельцем → DevTools → Console"
  echo "  2. Вставить содержимое tools/metabase_ct_phase11_rollback.js, затем: await ctRollback.all()"
  echo "     Восстанавливает карточки 97–116 и раскладку дашбордов 5/6 (width=fixed), архивирует"
  echo "     карточки Phase 1.1 (по описанию «EVETIS Control Tower Phase 1.1») и дашборды «ДЕТАЛИ», «DAILY BRIEF»."
  echo
fi

echo "--- Terraform / Cloud Scheduler (штатный путь) ---"
echo "  git rm infra/terraform/ct_refresh.tf; убрать ключ ct_refresh из local.terraform_actas_targets (iam.tf);"
echo "  workflow infra → action=plan (ожидается 14 to destroy) → action=apply с approval."
echo
echo "Готово."
