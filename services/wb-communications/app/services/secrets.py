"""Secret loading.

Priority for each secret:
  1. An environment variable of the same name (used for LOCAL DEV only).
  2. Google Secret Manager `projects/<project>/secrets/<name>/versions/latest`.

In Cloud Run you normally do NOT set the env var — the service account reads the
secret at startup. Locally you export the env var (or use a .env you never
commit). Secret *values* are never logged.
"""
from __future__ import annotations

import os
from functools import lru_cache

from app.domain.exceptions import ConfigError
from app.utils.logging import get_logger

logger = get_logger(__name__)


@lru_cache(maxsize=None)
def _client():
    # Imported lazily so unit tests and local runs that rely on env vars never
    # need the google-cloud-secret-manager credentials.
    from google.cloud import secretmanager  # type: ignore

    return secretmanager.SecretManagerServiceClient()


def get_secret(name: str, project_id: str, *, required: bool = True) -> str:
    """Return a secret value. Env var wins over Secret Manager for local dev."""
    env_value = os.environ.get(name)
    if env_value:
        logger.info("secret %s loaded from environment", name)
        return env_value

    if not project_id:
        if required:
            raise ConfigError(f"secret {name}: no env var and GCP_PROJECT_ID is empty")
        return ""

    resource = f"projects/{project_id}/secrets/{name}/versions/latest"
    try:
        response = _client().access_secret_version(request={"name": resource})
        logger.info("secret %s loaded from Secret Manager", name)
        return response.payload.data.decode("utf-8").strip()
    except Exception as exc:  # noqa: BLE001
        if required:
            # message intentionally excludes the resource payload
            raise ConfigError(f"secret {name}: could not read from Secret Manager") from exc
        logger.warning("optional secret %s not available", name)
        return ""
