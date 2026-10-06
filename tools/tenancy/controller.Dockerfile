# Build from a clean exact source commit at repository root. Never deploy by tag.
FROM python:3.12-slim
WORKDIR /app
COPY tools/tenancy/controller-requirements.txt /app/controller-requirements.txt
RUN pip install --no-cache-dir --only-binary=:all: --require-hashes -r controller-requirements.txt
COPY tools /app/tools
COPY pipelines/ozon/runtime /app/pipelines/ozon/runtime
COPY tenants /app/tenants
COPY infra/tenant/runtime_release.json /app/infra/tenant/runtime_release.json
COPY infra/tenant/releases /app/infra/tenant/releases
ARG CONTROLLER_SOURCE_SHA
LABEL org.opencontainers.image.revision=$CONTROLLER_SOURCE_SHA
RUN python -c 'import os,re,pathlib; s=os.environ.get("CONTROLLER_SOURCE_SHA", ""); assert re.fullmatch("[0-9a-f]{40}",s); pathlib.Path("CONTROLLER_SOURCE_SHA").write_text(s+"\n")'
ENV PYTHONDONTWRITEBYTECODE=1
CMD ["python", "-m", "tools.tenancy.cloud_controller"]
