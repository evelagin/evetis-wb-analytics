# Runtime ingestion домена Ozon (Cloud Run Job).
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
# promo.py обязателен: entities.py импортирует его на уровне модуля (сущность
# promo, PR-PROMO-1). Без этой строки образ собирается, но падает при старте
# с ImportError — то есть отказ проявился бы только в production.
COPY common.py entities.py main.py promo.py ./
ENTRYPOINT ["python", "main.py"]
