#!/usr/bin/env bash
# ============================================================================
# FIN CONTRACT V2 (2026-09-16) — ОТКАТ
# Миграция: sql/dash/fin_contract_v2_2026-09-16.sql
# Снимки ДО изменений: sql/rollback/fin_contract_v2_2026-09-16/
#
# 🔴 ПОРЯДОК ОБЯЗАТЕЛЕН:
#   1. Metabase — карточки 46, 47, 51, 79, 83, 85 из metabase_cards_BEFORE/
#      (иначе 46/47/51/83 обратятся к исчезнувшей колонке ad_spend_financial_rub);
#   2. BigQuery — R1 (V_DASH_FINANCE_CORRECTED_DAILY), затем R2
#      (REF_COST_MAP → CREDIT, V_WB_FINANCE_AMOUNTS_LONG_MAPPED без MEMO).
#
# 🔴 ЧЕГО ОТКАТ НЕ ТРОГАЕТ: MART_SKU_DAILY, FACT_*, V_DASH_KPI_DAILY (он читает
#    LONG_MAPPED и вернётся к прежним числам сам), V_DASH_SETTLEMENT_DAILY,
#    V_DASH_EXECUTIVE_ECONOMICS_DAILY, себестоимость, SKU Performance.
#    Выплата WB от отката не меняется.
#
# 🔴 Seed sql/mart/pr_mart2a_finance_longform.sql содержит back-port MEMO.
#    После отката seed расходится с production — вернуть файл через git.
#
# --dry-run печатает план и проверяет наличие снимков, ничего не выполняет.
# ============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SNAP="$ROOT/sql/rollback/fin_contract_v2_2026-09-16"
PROFILE="${MB_PROFILE:-evetis-dev}"
PROJECT="${BQ_PROJECT:-project-fa311fc0-4d87-4781-986}"
DRY_RUN=0
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=1

for f in R1_V_DASH_FINANCE_CORRECTED_DAILY_BEFORE.sql R2_reimbursement_and_long_mapped_BEFORE.sql; do
  [[ -f "$SNAP/$f" ]] || { echo "ОШИБКА: нет снимка $SNAP/$f" >&2; exit 1; }
done
for id in 46 47 51 79 83 85; do
  [[ -f "$SNAP/metabase_cards_BEFORE/card-$id.json" ]] || { echo "ОШИБКА: нет снимка карточки $id" >&2; exit 1; }
done

echo "План отката FIN CONTRACT V2:"
echo "  1. mb card update 46 47 51 79 83 85 ← metabase_cards_BEFORE (description + dataset_query)"
echo "  2. BigQuery script R1_V_DASH_FINANCE_CORRECTED_DAILY_BEFORE.sql"
echo "  3. BigQuery script R2_reimbursement_and_long_mapped_BEFORE.sql"
[[ $DRY_RUN -eq 1 ]] && { echo "--dry-run: ничего не выполнено"; exit 0; }

command -v mb >/dev/null && command -v gcloud >/dev/null && command -v python3 >/dev/null \
  || { echo "ОШИБКА: нужны mb, gcloud, python3" >&2; exit 1; }

for id in 46 47 51 79 83 85; do
  python3 - "$SNAP/metabase_cards_BEFORE/card-$id.json" > "/tmp/fin_v2_rollback_card_$id.json" <<'PY'
import json, sys
c = json.load(open(sys.argv[1]))
dq = c["dataset_query"]
st = dq["stages"][0]
tt = st.get("template-tags")
if isinstance(tt, list):
    st["template-tags"] = {t["name"]: t for t in tt}
print(json.dumps({"description": c["description"], "dataset_query": dq}, ensure_ascii=False))
PY
  mb card update "$id" --file "/tmp/fin_v2_rollback_card_$id.json" -p "$PROFILE" --json >/dev/null
  echo "  карточка $id восстановлена"
done

run_script() {
  python3 - "$1" "$PROJECT" <<'PY'
import json, subprocess, sys, time, urllib.request
path, project = sys.argv[1], sys.argv[2]
tok = subprocess.check_output(["gcloud", "auth", "print-access-token"]).decode().strip()
h = {"Authorization": "Bearer " + tok, "Content-Type": "application/json"}
body = {"configuration": {"query": {"query": open(path).read(), "useLegacySql": False}},
        "jobReference": {"projectId": project, "location": "EU"}}
base = f"https://www.googleapis.com/bigquery/v2/projects/{project}/jobs"
job = json.load(urllib.request.urlopen(urllib.request.Request(base, json.dumps(body).encode(), h)))
jid = job["jobReference"]["jobId"]
while True:
    g = json.load(urllib.request.urlopen(urllib.request.Request(f"{base}/{jid}?location=EU", headers=h)))
    if g["status"]["state"] == "DONE":
        break
    time.sleep(3)
err = g["status"].get("errorResult")
print(("FAILED: " + json.dumps(err, ensure_ascii=False)) if err else f"OK {path}")
sys.exit(1 if err else 0)
PY
}
run_script "$SNAP/R1_V_DASH_FINANCE_CORRECTED_DAILY_BEFORE.sql"
run_script "$SNAP/R2_reimbursement_and_long_mapped_BEFORE.sql"
echo "Откат завершён. Проверка: результат 31.08–13.09 = −5 527,99 ₽, выплата 37 465,76 ₽."
