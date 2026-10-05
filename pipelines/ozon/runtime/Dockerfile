# Runtime ingestion домена Ozon (Cloud Run Job).
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
# promo.py обязателен: entities.py импортирует его на уровне модуля (сущность
# promo, PR-PROMO-1). Без этой строки образ собирается, но падает при старте
# с ImportError — то есть отказ проявился бы только в production.
COPY common.py entities.py main.py promo.py catalog_identity.py backfill.py backfill_core.py ./
# T5: identity/привязка (runtime и control), control plane арендатора и политика методов Seller.
COPY identity.py credentials.py lifecycle.py lifecycle_core.py checkpoints.py history.py quota.py dq.py control_store.py seller_policy.py seller_method_policy.json runtime_execution_contract.json ./
ENTRYPOINT ["python", "main.py"]
