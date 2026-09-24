#!/usr/bin/env bash
# Откат WIF hardening M1/M2 (2026-09-24) к состоянию pre_*.json. Исполняет ВЛАДЕЛЕЦ.
# Возвращает широкие привязки — то есть возвращает риск S1. Только при провале проверки.
set -euo pipefail
P=project-fa311fc0-4d87-4781-986
POOL=principalSet://iam.googleapis.com/projects/37074083763/locations/global/workloadIdentityPools/github-pool
WF=evelagin/evetis-wb-analytics/.github/workflows
R=roles/iam.workloadIdentityUser
# --- откат M2: вернуть старые привязки, затем убрать новые -----------------------------
gcloud iam service-accounts add-iam-policy-binding sa-deployer@$P.iam.gserviceaccount.com --project=$P --role=$R \
  --member="$POOL/attribute.repository/evelagin/evetis-wb-analytics" --condition=None
gcloud iam service-accounts add-iam-policy-binding sa-terraform-plan@$P.iam.gserviceaccount.com --project=$P --role=$R \
  --member="$POOL/attribute.repository/evelagin/evetis-wb-analytics" --condition=None
gcloud iam service-accounts add-iam-policy-binding sa-terraform-apply@$P.iam.gserviceaccount.com --project=$P --role=$R \
  --member="$POOL/attribute.repo_ref/evelagin/evetis-wb-analytics@refs/heads/main" --condition=None
for f in deploy-prod.yml deploy-shadow.yml; do
  gcloud iam service-accounts remove-iam-policy-binding sa-deployer@$P.iam.gserviceaccount.com --project=$P --role=$R \
    --member="$POOL/attribute.workflow_ref/$WF/$f@refs/heads/main" --condition=None || true
done
for f in infra.yml scheduler-control.yml; do
  gcloud iam service-accounts remove-iam-policy-binding sa-terraform-apply@$P.iam.gserviceaccount.com --project=$P --role=$R \
    --member="$POOL/attribute.workflow_ref/$WF/$f@refs/heads/main" --condition=None || true
done
gcloud iam service-accounts remove-iam-policy-binding sa-terraform-plan@$P.iam.gserviceaccount.com --project=$P --role=$R \
  --member="$POOL/attribute.tf_plan_workflow/infra.yml" --condition=None || true
# --- откат M1: исходный маппинг (4 атрибута); условие провайдера не менялось ------------
gcloud iam workload-identity-pools providers update-oidc github-provider --workload-identity-pool=github-pool \
  --location=global --project=$P \
  --attribute-mapping='google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.ref=assertion.ref,attribute.repo_ref=assertion.repository + "@" + assertion.ref'
# После отката: Terraform state расходится с живым состоянием для 4 ресурсов — `terraform apply
# -refresh-only -target=…` или повторить M1/M2 после исправления причины.
