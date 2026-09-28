"""BigQuery history/analytics sink.

Two tables in the dedicated dataset `evetis_communications`:

* ``communications_current`` — one row per (channel, entity_type, source_id),
  kept up to date via MERGE (query jobs only, so it never hits the streaming
  buffer / DML conflict).
* ``communication_events`` — append-only journal. Rows are streamed with a
  deterministic ``event_id`` used as the insert row id, so a retried write does
  not create a duplicate event.

All writes are BEST-EFFORT: a BigQuery hiccup logs a warning and returns False,
it never blocks Telegram or a WB publish.
"""
from __future__ import annotations

from app.utils.audit_events import audited_store
from app.utils.logging import get_logger

logger = get_logger(__name__)


class ShadowSchemaError(Exception):
    """Raised when the shadow table schema is missing required fields or has the
    wrong type after an attempted migration — surfaced (never masked) so a
    pre-deploy step fails loudly instead of silently dropping shadow rows."""


# BigQuery reports some types under legacy names (INTEGER/BOOLEAN/FLOAT); normalize
# so an INT64 column declared here matches an INTEGER column reported by the API.
_BQ_TYPE_ALIASES = {
    "INTEGER": "INT64", "INT64": "INT64",
    "BOOL": "BOOL", "BOOLEAN": "BOOL",
    "FLOAT": "FLOAT64", "FLOAT64": "FLOAT64",
    "STRING": "STRING", "TIMESTAMP": "TIMESTAMP", "NUMERIC": "NUMERIC",
}


def _norm_bq_type(bqtype: str) -> str:
    return _BQ_TYPE_ALIASES.get(str(bqtype).upper(), str(bqtype).upper())

CURRENT_SCHEMA = [
    ("channel", "STRING"),
    ("entity_type", "STRING"),
    ("source_id", "STRING"),
    ("status", "STRING"),
    ("rating", "INT64"),
    ("product_name", "STRING"),
    ("supplier_article", "STRING"),
    ("nm_id", "STRING"),
    ("brand_name", "STRING"),
    ("buyer_name", "STRING"),
    ("has_photo", "BOOL"),
    ("has_video", "BOOL"),
    ("ai_answer", "STRING"),
    ("final_answer", "STRING"),
    ("openai_model", "STRING"),
    ("prompt_version", "STRING"),
    ("generation_number", "INT64"),
    ("first_seen_at", "TIMESTAMP"),
    ("published_at", "TIMESTAMP"),
    ("updated_at", "TIMESTAMP"),
]

# Shadow comparison journal for Communication Engine v2. Kept in its OWN table so
# shadow rows are never mixed with the production publication history above.
SHADOW_SCHEMA = [
    ("shadow_id", "STRING"),
    ("review_id", "STRING"),
    ("channel", "STRING"),
    ("shadow_at", "TIMESTAMP"),
    ("old_prompt_version", "STRING"),
    ("old_answer", "STRING"),
    ("v2_prompt_version", "STRING"),
    ("v2_answer", "STRING"),
    ("v2_model", "STRING"),
    ("classification_json", "STRING"),
    ("product_id", "STRING"),
    ("product_resolution_method", "STRING"),
    ("product_resolution_status", "STRING"),
    ("marketplace", "STRING"),
    ("marketplace_resolution_status", "STRING"),
    ("needs_manual_moderation", "BOOL"),
    ("validation_passed", "BOOL"),
    ("validation_issues_json", "STRING"),
    ("v2_usable", "BOOL"),
    ("v2_latency_ms", "INT64"),
    ("v2_token_input", "INT64"),
    ("v2_token_output", "INT64"),
    ("error_code", "STRING"),
    ("error_message", "STRING"),
]

EVENTS_SCHEMA = [
    ("event_id", "STRING"),
    ("event_at", "TIMESTAMP"),
    ("channel", "STRING"),
    ("entity_type", "STRING"),
    ("source_id", "STRING"),
    ("event_type", "STRING"),
    ("status_before", "STRING"),
    ("status_after", "STRING"),
    ("telegram_user_id", "STRING"),
    ("openai_model", "STRING"),
    ("prompt_version", "STRING"),
    ("answer_version", "INT64"),
    ("latency_ms", "INT64"),
    ("token_input", "INT64"),
    ("token_output", "INT64"),
    ("error_code", "STRING"),
    ("error_message", "STRING"),
    ("payload_json", "STRING"),
]


@audited_store("bigquery")   # D-19b: every public method writes (inserts, upserts, schema) — attributed
class BigQueryRepository:
    def __init__(self, settings, client=None):
        self._s = settings
        self._client = client

    def _lazy(self):
        if self._client is None:
            from google.cloud import bigquery  # lazy

            self._client = bigquery.Client(
                project=self._s.gcp_project_id, location=self._s.bigquery_location
            )
        return self._client

    def _table(self, name: str) -> str:
        return f"{self._s.gcp_project_id}.{self._s.bigquery_dataset}.{name}"

    def insert_event(self, event: dict) -> bool:
        try:
            client = self._lazy()
            table = self._table(self._s.bigquery_events_table)
            errors = client.insert_rows_json(
                table, [event], row_ids=[event.get("event_id")]
            )
            if errors:
                logger.warning("bigquery insert_event errors: %s", errors)
                return False
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("bigquery insert_event failed: %s", type(exc).__name__)
            return False

    def insert_shadow(self, row: dict) -> bool:
        """Append one Communication Engine v2 shadow row. BEST-EFFORT: a BigQuery
        hiccup logs a warning and returns False — it never blocks or breaks the
        production reviews_v1 flow."""
        try:
            client = self._lazy()
            table = self._table(self._s.bigquery_shadow_table)
            # Use the unique shadow_id as the insert row id, NOT review_id — a
            # re-run of the same review on a different prompt version must produce
            # a distinct id so BigQuery does not dedup the two comparison rows.
            row_id = row.get("shadow_id")
            errors = client.insert_rows_json(
                table, [row], row_ids=[row_id] if row_id else None
            )
            if errors:
                logger.warning("bigquery insert_shadow errors: %s", errors)
                return False
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("bigquery insert_shadow failed: %s", type(exc).__name__)
            return False

    def upsert_current(self, row: dict) -> bool:
        try:
            from google.cloud import bigquery

            client = self._lazy()
            table = self._table(self._s.bigquery_current_table)
            cols = [c for c, _ in CURRENT_SCHEMA]
            params = []
            for col, bqtype in CURRENT_SCHEMA:
                params.append(bigquery.ScalarQueryParameter(col, bqtype, row.get(col)))
            set_clause = ", ".join(f"T.{c} = S.{c}" for c in cols if c not in ("channel", "entity_type", "source_id"))
            insert_cols = ", ".join(cols)
            insert_vals = ", ".join(f"S.{c}" for c in cols)
            select_src = ", ".join(f"@{c} AS {c}" for c in cols)
            sql = f"""
            MERGE `{table}` T
            USING (SELECT {select_src}) S
            ON T.channel = S.channel AND T.entity_type = S.entity_type AND T.source_id = S.source_id
            WHEN MATCHED THEN UPDATE SET {set_clause}
            WHEN NOT MATCHED THEN INSERT ({insert_cols}) VALUES ({insert_vals})
            """
            job = client.query(
                sql,
                job_config=bigquery.QueryJobConfig(query_parameters=params),
                location=self._s.bigquery_location,
            )
            job.result()
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("bigquery upsert_current failed: %s", type(exc).__name__)
            return False

    # --- schema-migration primitives (thin google-cloud-bigquery wrappers) ---
    # These are the only parts that touch the network; tests override them so the
    # migration ORCHESTRATION (ensure_shadow_schema) is verifiable offline.
    def _shadow_table_id(self) -> str:
        return self._table(self._s.bigquery_shadow_table)

    def _ensure_dataset(self) -> None:
        from google.cloud import bigquery

        client = self._lazy()
        dataset = bigquery.Dataset(f"{self._s.gcp_project_id}.{self._s.bigquery_dataset}")
        dataset.location = self._s.bigquery_location
        client.create_dataset(dataset, exists_ok=True)

    def _get_shadow_schema(self) -> dict | None:
        """Live {name: type} of the shadow table, or None if it does not exist."""
        from google.api_core.exceptions import NotFound

        client = self._lazy()
        try:
            table = client.get_table(self._shadow_table_id())
        except NotFound:
            return None
        return {f.name: f.field_type for f in table.schema}

    def _create_shadow_table(self) -> None:
        from google.cloud import bigquery

        client = self._lazy()
        table = bigquery.Table(
            self._shadow_table_id(),
            schema=[bigquery.SchemaField(c, t) for c, t in SHADOW_SCHEMA],
        )
        client.create_table(table)

    def _add_shadow_columns(self, columns: list) -> None:
        """ADD COLUMN IF NOT EXISTS for each (name, type) — idempotent."""
        if not columns:
            return
        client = self._lazy()
        adds = ", ".join(f"ADD COLUMN IF NOT EXISTS {c} {t}" for c, t in columns)
        sql = f"ALTER TABLE `{self._shadow_table_id()}` {adds}"
        client.query(sql, location=self._s.bigquery_location).result()

    def ensure_shadow_schema(self) -> dict:
        """Idempotent migration of the ``communication_engine_shadow`` table.

        1. create the table (full SHADOW_SCHEMA) if it does not exist;
        2. otherwise ``ADD COLUMN IF NOT EXISTS`` for any missing column (incl.
           ``shadow_id`` on a table created by an earlier version);
        3. re-read the live schema;
        4. FAIL-FAST: raise ``ShadowSchemaError`` if a required field is still
           missing or has the wrong type.

        Meant to be run as a SEPARATE pre-deploy step, never on every /poll.
        Returns ``{"table_created": bool, "columns_added": [...]}``.
        """
        self._ensure_dataset()
        before = self._get_shadow_schema()
        if before is None:
            self._create_shadow_table()
            table_created = True
            added_expected: list[str] = [c for c, _ in SHADOW_SCHEMA]
        else:
            table_created = False
            missing = [(c, t) for c, t in SHADOW_SCHEMA if c not in before]
            self._add_shadow_columns(missing)
            added_expected = [c for c, _ in missing]

        live = self._get_shadow_schema()
        if live is None:
            raise ShadowSchemaError("shadow table missing after migration")
        problems: list[str] = []
        for col, bqtype in SHADOW_SCHEMA:
            if col not in live:
                problems.append(f"missing column {col}")
            elif _norm_bq_type(live[col]) != _norm_bq_type(bqtype):
                problems.append(f"column {col} type {live[col]} != expected {bqtype}")
        if problems:
            raise ShadowSchemaError(
                "shadow table schema invalid after migration: " + "; ".join(problems)
            )
        return {"table_created": table_created, "columns_added": added_expected}

    # --- one-time setup used by scripts/DEPLOY ---
    def ensure_dataset_and_tables(self) -> None:
        from google.cloud import bigquery

        client = self._lazy()
        dataset_id = f"{self._s.gcp_project_id}.{self._s.bigquery_dataset}"
        dataset = bigquery.Dataset(dataset_id)
        dataset.location = self._s.bigquery_location
        client.create_dataset(dataset, exists_ok=True)
        for name, schema in (
            (self._s.bigquery_current_table, CURRENT_SCHEMA),
            (self._s.bigquery_events_table, EVENTS_SCHEMA),
            (self._s.bigquery_shadow_table, SHADOW_SCHEMA),
        ):
            table_id = self._table(name)
            table = bigquery.Table(
                table_id, schema=[bigquery.SchemaField(c, t) for c, t in schema]
            )
            client.create_table(table, exists_ok=True)
        logger.info("bigquery dataset and tables ensured")
