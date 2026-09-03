# Runtime ingestion домена Ozon (Cloud Run Job).
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY common.py entities.py main.py ./
ENTRYPOINT ["python", "main.py"]
