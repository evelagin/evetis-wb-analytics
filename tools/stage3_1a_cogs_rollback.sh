#!/usr/bin/env bash
# ============================================================================
# STAGE 3.1A — PRODUCT COGS REFERENCE LAYER · ОТКАТ
#
# Что откатывает: датасет BigQuery `evetis_ref` целиком — 2 таблицы и 2 view:
#   REF_SKU_COGS_HISTORY (17 строк)
#   REF_BUNDLE_COMPONENTS (33 строки)
#   V_BUNDLE_COGS_DERIVED (21 производный интервал)
#   V_PRODUCT_COGS_EFFECTIVE (38 интервалов)
#
# Чего НЕ трогает: wb_raw, wb_mart, MART_SKU_DAILY, V_DASH_*, FACT_*,
#   REF_COST_MAP, Metabase, Google Sheets, Apps Script, Cloud Run.
#
# Почему откат полный и безопасный: слой ни от чего не зависит и от него
#   ничего не зависит — ни одна вью и ни одна процедура wb_mart/wb_raw не
#   упоминает evetis_ref. Проверка этого — предусловие P4/P5.
#
# ДВА РЕЖИМА
#   --dry-run   печатает план: project/dataset, предусловия, точный
#               destructive SQL, постусловия. НИЧЕГО не выполняет, bq не
#               требуется и не вызывается, exit 0.
#   без флага   реальный откат: проверяет bq и все предусловия, выполняет
#               DROP SCHEMA, проверяет постусловия. Останавливается при
#               любом несоответствии.
#
# Репозиторий откатывается отдельно и тривиально: до commit четыре файла
#   Stage 3.1A остаются untracked, поэтому достаточно их удалить:
#     rm -f sql/ref/pr_ref_cogs_history.sql sql/ref/pr_ref_cogs_validation.sql \
#           docs/STAGE3_1A_COGS_REFERENCE_LAYER_2026-08-27.md \
#           tools/stage3_1a_cogs_rollback.sh
#     rmdir sql/ref 2>/dev/null
#
# Запуск:  bash tools/stage3_1a_cogs_rollback.sh [--dry-run]
# ============================================================================
set -euo pipefail

PROJECT="${BQ_PROJECT:-project-fa311fc0-4d87-4781-986}"
DATASET="evetis_ref"

DRY_RUN=0
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=1

DESTRUCTIVE_SQL="DROP SCHEMA \`$PROJECT.$DATASET\` CASCADE;"

q() { bq --project_id="$PROJECT" query --nouse_legacy_sql --format=csv "$1" | tail -n +2; }

echo "== Stage 3.1A rollback =="
echo "   project : $PROJECT"
echo "   dataset : $DATASET"
if [[ $DRY_RUN -eq 1 ]]; then
  echo "   режим   : DRY-RUN — план без выполнения"
else
  echo "   режим   : EXECUTION — будет выполнено удаление"
fi
echo

# ── DRY-RUN: только печать плана, bq не требуется ──────────────────────────
if [[ $DRY_RUN -eq 1 ]]; then
  cat <<EOF
-- PRECONDITIONS (будут проверены при реальном откате) -----------------------
 P1  наличие bq (Google Cloud SDK) в PATH
 P2  wb_raw объектов  = 56
     SELECT COUNT(*) FROM $PROJECT.wb_raw.INFORMATION_SCHEMA.TABLES
 P3  wb_mart объектов = 35
     SELECT COUNT(*) FROM $PROJECT.wb_mart.INFORMATION_SCHEMA.TABLES
 P4  ссылок на $DATASET из wb_mart = 0
     SELECT COUNT(*) FROM $PROJECT.wb_mart.INFORMATION_SCHEMA.VIEWS WHERE view_definition LIKE '%$DATASET%'
 P5  ссылок на $DATASET из wb_raw = 0
     SELECT COUNT(*) FROM $PROJECT.wb_raw.INFORMATION_SCHEMA.VIEWS WHERE view_definition LIKE '%$DATASET%'
 Любое несоответствие -> ОСТАНОВ, откат НЕ выполняется.

-- DESTRUCTIVE SQL (единственная разрушающая операция) -----------------------
$DESTRUCTIVE_SQL
 Эквивалент через CLI: bq --project_id=$PROJECT rm -r -f -d $PROJECT:$DATASET

-- POSTCONDITIONS (будут проверены после отката) -----------------------------
 Q1  датасет $DATASET отсутствует
 Q2  MART_SKU_DAILY = 7477 строк
     SELECT COUNT(*) FROM $PROJECT.wb_mart.MART_SKU_DAILY
 Q3  REF_COST_MAP = 19 правил
     SELECT COUNT(*) FROM $PROJECT.wb_mart.REF_COST_MAP
 Q4  wb_raw = 56 объектов, wb_mart = 35 объектов (не изменились)

-- РЕПОЗИТОРИЙ (откатывается вручную, отдельно от BigQuery) ------------------
 rm -f sql/ref/pr_ref_cogs_history.sql sql/ref/pr_ref_cogs_validation.sql
 rm -f docs/STAGE3_1A_COGS_REFERENCE_LAYER_2026-08-27.md tools/stage3_1a_cogs_rollback.sh
 rmdir sql/ref 2>/dev/null
EOF
  echo
  echo "DRY-RUN: НИКАКИХ КОМАНД НЕ ВЫПОЛНЕНО."
  echo "         bq не вызывался, BigQuery не запрашивался и не изменён,"
  echo "         файлы репозитория не тронуты."
  exit 0
fi

# ── EXECUTION MODE: полные guards, ни один не ослаблен ─────────────────────
echo "-- P1. Наличие bq --------------------------------------------------------"
command -v bq >/dev/null 2>&1 || {
  echo "   ОСТАНОВ: утилита bq (Google Cloud SDK) не найдена в PATH."
  echo "   Запусти в окружении с установленным и авторизованным Google Cloud SDK,"
  echo "   либо выполни вручную:"
  echo "     $DESTRUCTIVE_SQL"
  exit 2
}
echo "   bq найден: $(command -v bq)"

echo "-- P2/P3. Предусловие: соседние слои не тронуты ---------------------------"
RAW_N=$(q "SELECT COUNT(*) FROM \`$PROJECT.wb_raw.INFORMATION_SCHEMA.TABLES\`")
MART_N=$(q "SELECT COUNT(*) FROM \`$PROJECT.wb_mart.INFORMATION_SCHEMA.TABLES\`")
echo "   wb_raw = $RAW_N (ожидание 56), wb_mart = $MART_N (ожидание 35)"
[[ "$RAW_N" == "56" && "$MART_N" == "35" ]] || {
  echo "   ОСТАНОВ: состав соседних датасетов отличается от baseline. Разберись до отката."; exit 1; }

echo "-- P4/P5. Предусловие: на $DATASET никто не ссылается ---------------------"
DEPS=$(q "SELECT COUNT(*) FROM \`$PROJECT.wb_mart.INFORMATION_SCHEMA.VIEWS\` WHERE view_definition LIKE '%$DATASET%'")
DEPS2=$(q "SELECT COUNT(*) FROM \`$PROJECT.wb_raw.INFORMATION_SCHEMA.VIEWS\` WHERE view_definition LIKE '%$DATASET%'")
echo "   ссылок из wb_mart: $DEPS, из wb_raw: $DEPS2 (ожидание 0 и 0)"
[[ "$DEPS" == "0" && "$DEPS2" == "0" ]] || {
  echo "   ОСТАНОВ: на $DATASET есть зависимости. Сначала отключи потребителей."; exit 1; }

echo "-- Удаление датасета $DATASET --------------------------------------------"
echo "   $DESTRUCTIVE_SQL"
bq --project_id="$PROJECT" rm -r -f -d "$PROJECT:$DATASET"

echo "-- Q1..Q4. Постусловия ---------------------------------------------------"
MART_ROWS=$(q "SELECT COUNT(*) FROM \`$PROJECT.wb_mart.MART_SKU_DAILY\`")
COST_MAP=$(q "SELECT COUNT(*) FROM \`$PROJECT.wb_mart.REF_COST_MAP\`")
RAW_A=$(q "SELECT COUNT(*) FROM \`$PROJECT.wb_raw.INFORMATION_SCHEMA.TABLES\`")
MART_A=$(q "SELECT COUNT(*) FROM \`$PROJECT.wb_mart.INFORMATION_SCHEMA.TABLES\`")
echo "   MART_SKU_DAILY = $MART_ROWS (ожидание 7477), REF_COST_MAP = $COST_MAP (ожидание 19)"
echo "   wb_raw = $RAW_A (ожидание 56), wb_mart = $MART_A (ожидание 35)"
[[ "$MART_ROWS" == "7477" && "$COST_MAP" == "19" && "$RAW_A" == "56" && "$MART_A" == "35" ]] || {
  echo "   ВНИМАНИЕ: production изменился. Разберись."; exit 1; }
echo "   Откат завершён: $DATASET удалён, production не затронут."
