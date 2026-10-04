-- PROPOSED / NOT DEPLOYED. Owner ACK required before execution.
-- New append-only operational journal; existing RAW/INGEST_RUNS schemas unchanged.
-- Contains bounded prepared RAW payloads: same access boundary as wb_raw, no credentials.
-- No expiry: deleting a prepared batch breaks resumability. Retention is a separate decision.
CREATE TABLE `project-fa311fc0-4d87-4781-986.wb_raw.ADS_FULLSTATS_RECOVERY` (
  recovery_id STRING NOT NULL,
  kind STRING NOT NULL,
  record_key STRING NOT NULL,
  revision INT64 NOT NULL,
  logical_date DATE NOT NULL,
  payload STRING NOT NULL,
  digest STRING NOT NULL
)
CLUSTER BY recovery_id, kind, record_key
OPTIONS(description='WB ads resume v1: immutable manifest revisions and prepared fullstats batches; no secrets');
