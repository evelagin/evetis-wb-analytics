#!/usr/bin/env bash
# Откат ТОЛЬКО M3 (sa-ae-reader). M1/M2 не трогает. Исполняет владелец.
# Предпочтительно — targeted destroy из каталога Terraform main:
#   terraform destroy -target=google_service_account_iam_member.ae_reader_wif \
#     -target=google_bigquery_dataset_iam_member.ae_reader_read -target=google_project_iam_member.ae_reader \
#     -target=google_service_account.ae_reader
# Эквивалент на gcloud/bq (если state недоступен):
set -euo pipefail
P=project-fa311fc0-4d87-4781-986
SA=sa-ae-reader@$P.iam.gserviceaccount.com
for r in roles/bigquery.jobUser roles/bigquery.resourceViewer roles/logging.viewer; do
  gcloud projects remove-iam-policy-binding $P --member="serviceAccount:$SA" --role=$r --condition=None || true
done
# Датасетные READER-записи: удаление SA делает их «deleted:serviceAccount:…»; убрать явно через bq:
for ds in wb_raw wb_mart wb_ops evetis_ref evetis_ops evetis_mart ozon_raw ozon_mart; do
  echo "проверить/убрать READER $SA в $P:$ds (bq show --format=prettyjson / bq update --source)"
done
gcloud iam service-accounts delete $SA --project=$P --quiet   # уносит и 4 WIF-привязки на самом SA
