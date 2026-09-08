#!/usr/bin/env python
"""Pre-deploy: idempotently migrate the BigQuery ``communication_engine_shadow``
table schema.

Creates the table if it does not exist, and — critically — adds ``shadow_id`` to
a table created by an earlier version (v1.2.0) that predates that column, so
``insert_shadow`` rows are not rejected for an unknown field. Run this ONCE,
before enabling the shadow flag. It is safe to re-run (idempotent).

Exits non-zero on ANY failure so a deploy pipeline halts instead of proceeding
with a table that cannot accept shadow rows.

Usage:
    GCP_PROJECT_ID=... BIGQUERY_LOCATION=EU python -m scripts.migrate_shadow_table
"""
from __future__ import annotations

import sys

from app.config import get_settings
from app.services.bigquery_repository import BigQueryRepository, ShadowSchemaError


def main() -> int:
    settings = get_settings()
    repo = BigQueryRepository(settings)
    try:
        result = repo.ensure_shadow_schema()
    except ShadowSchemaError as exc:
        print(f"[shadow-migration] FAILED — schema invalid: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 — any failure must halt the deploy
        print(f"[shadow-migration] FAILED — {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(f"[shadow-migration] OK — {result}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
