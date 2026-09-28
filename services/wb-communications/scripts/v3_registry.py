"""Reviews & Q&A v3 knowledge registry CLI.

  python scripts/v3_registry.py validate [--external tests/v3/fixtures/external_ref_tables_2026-09-25.json]
      offline gate on the YAML seed (+ a pinned copy of the external REF tables)
  python scripts/v3_registry.py load-bq
      create (if missing) and FULLY RELOAD the v3-owned evetis_ref tables from the YAML seed.
      Never touches REF_SKU_CHANNEL_MAP / REF_PRODUCT_MASTER / REF_BUNDLE_COMPONENTS.
  python scripts/v3_registry.py build-snapshot [--activate]
      read ALL registry tables from BigQuery (owned + external), run the knowledge build gate,
      write app/v3/snapshots/<snapshot_id>.json + <snapshot_id>.report.json, log the snapshot
      in evetis_ref.KNOWLEDGE_SNAPSHOT; --activate also points snapshots/ACTIVE at it.
  python scripts/v3_registry.py verify-snapshot [<snapshot_id>]
      re-validate a committed snapshot (hash + gate status + seed agreement).

BigQuery is reached through the REST API on https://www.googleapis.com (bigquery.googleapis.com
is blocked on the owner's network) with the operator's `gcloud auth print-access-token`.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.v3.registry import (KNOWLEDGE_SNAPSHOT_SCHEMA, OWNED_TABLES, load_policy, registry_tables,  # noqa: E402
                             seed_sha256, table_schema)
from app.v3.snapshot import ACTIVE_FILE, SNAPSHOT_DIR, load_snapshot  # noqa: E402
from app.v3.snapshot_builder import SnapshotGateError, build_snapshot, validate, assemble  # noqa: E402

PROJECT = os.environ.get("GCP_PROJECT_ID", "project-fa311fc0-4d87-4781-986")
DATASET = "evetis_ref"
LOCATION = "EU"


class _Rest:
    """Minimal BigQuery REST client (operator's gcloud token; www.googleapis.com host)."""
    BASE = os.environ.get("BQ_API_ENDPOINT", "https://www.googleapis.com")

    def __init__(self):
        import subprocess
        self._tok = subprocess.check_output(["gcloud", "auth", "print-access-token"], text=True).strip()

    def _req(self, method, url, body=None, headers=None, raw=None):
        import urllib.request
        h = {"Authorization": "Bearer " + self._tok}
        h.update(headers or {})
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        if body is not None and raw is None:
            h["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=h, method=method)
        with urllib.request.urlopen(req, timeout=180) as r:
            return json.load(r)

    def query(self, sql: str) -> list[dict]:
        import time
        r = self._req("POST", f"{self.BASE}/bigquery/v2/projects/{PROJECT}/queries",
                      {"query": sql, "useLegacySql": False, "location": LOCATION, "timeoutMs": 120000,
                       "maxResults": 100000})
        job = r["jobReference"]["jobId"]
        while not r.get("jobComplete"):
            time.sleep(1)
            r = self._req("GET", f"{self.BASE}/bigquery/v2/projects/{PROJECT}/queries/{job}?location={LOCATION}")
        fields = r["schema"]["fields"]
        rows = []
        for row in r.get("rows", []):
            d = {}
            for f, c in zip(fields, row["f"]):
                v = c["v"]
                if v is not None and f["type"] in ("INTEGER", "INT64"):
                    v = int(v)
                elif v is not None and f["type"] in ("FLOAT", "FLOAT64"):
                    v = float(v)
                d[f["name"]] = v
            rows.append(d)
        if r.get("pageToken"):
            raise RuntimeError("result paging not implemented (too many rows)")
        return rows

    def load_json(self, table: str, schema: list[tuple[str, str]], rows: list[dict],
                  disposition: str = "WRITE_TRUNCATE") -> None:
        import time
        import uuid
        meta = {"configuration": {"load": {
            "destinationTable": {"projectId": PROJECT, "datasetId": DATASET, "tableId": table},
            "schema": {"fields": [{"name": c, "type": t, "mode": "NULLABLE"} for c, t in schema]},
            "sourceFormat": "NEWLINE_DELIMITED_JSON", "writeDisposition": disposition,
            "createDisposition": "CREATE_IF_NEEDED"}}, "jobReference": {"projectId": PROJECT, "location": LOCATION}}
        nd = "\n".join(json.dumps(r, ensure_ascii=False) for r in rows).encode()
        b = uuid.uuid4().hex
        body = (f"--{b}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{json.dumps(meta)}\r\n"
                f"--{b}\r\nContent-Type: application/octet-stream\r\n\r\n").encode() + nd + f"\r\n--{b}--\r\n".encode()
        r = self._req("POST", f"{self.BASE}/upload/bigquery/v2/projects/{PROJECT}/jobs?uploadType=multipart",
                      raw=body, headers={"Content-Type": f"multipart/related; boundary={b}"})
        jid = r["jobReference"]["jobId"]
        while True:
            st = self._req("GET", f"{self.BASE}/bigquery/v2/projects/{PROJECT}/jobs/{jid}?location={LOCATION}")["status"]
            if st["state"] == "DONE":
                if st.get("errorResult"):
                    raise RuntimeError(f"load {table} failed: {st['errorResult']} {st.get('errors', [])[:3]}")
                return
            time.sleep(1)


def _client():
    return _Rest()


def _read_table(client, name: str, where: str = "") -> list[dict]:
    return client.query(f"SELECT * FROM `{PROJECT}.{DATASET}.{name}` {where}")


def _external_from_bq(client) -> dict:
    return {
        "REF_SKU_CHANNEL_MAP": _read_table(client, "REF_SKU_CHANNEL_MAP", "WHERE marketplace = 'WB'"),
        "REF_PRODUCT_MASTER": _read_table(client, "REF_PRODUCT_MASTER"),
        "REF_BUNDLE_COMPONENTS": _read_table(client, "REF_BUNDLE_COMPONENTS"),
    }


def cmd_validate(args) -> int:
    tables = registry_tables()
    tables.update(json.loads(Path(args.external).read_text(encoding="utf-8")))
    snap = assemble(tables, load_policy())
    report = validate(snap, tables)
    print(json.dumps({k: report[k] for k in ("status", "checks", "errors", "warnings")}, ensure_ascii=False, indent=1))
    return 0 if report["status"] == "PASS" else 1


def cmd_load_bq(args) -> int:
    client = _client()
    tables = registry_tables()
    for name in OWNED_TABLES:
        rows = tables[name]
        client.load_json(name, table_schema(name, rows), rows, "WRITE_TRUNCATE")
        print(f"{name}: loaded {len(rows)} rows")
    client.load_json("KNOWLEDGE_SNAPSHOT", KNOWLEDGE_SNAPSHOT_SCHEMA, [], "WRITE_APPEND")
    print("KNOWLEDGE_SNAPSHOT: ensured (append-only)")
    return 0


def cmd_build(args) -> int:
    client = _client()
    tables = {name: _read_table(client, name) for name in OWNED_TABLES}
    tables.update(_external_from_bq(client))
    try:
        snap, report = build_snapshot(tables, load_policy(), seed_sha256=seed_sha256(),
                                      origin=f"bigquery:{PROJECT}.{DATASET}")
    except SnapshotGateError as exc:
        print(json.dumps(exc.report, ensure_ascii=False, indent=1))
        print("GATE FAILED — snapshot NOT written", file=sys.stderr)
        return 1
    sid = snap["snapshot_id"]
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    (SNAPSHOT_DIR / f"{sid}.json").write_text(json.dumps(snap, ensure_ascii=False, sort_keys=True, indent=1),
                                              encoding="utf-8")
    (SNAPSHOT_DIR / f"{sid}.report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1),
                                                     encoding="utf-8")
    if args.activate:
        ACTIVE_FILE.write_text(sid + "\n", encoding="utf-8")
    row = {"snapshot_id": sid, "generated_at": snap["generated_at"], "content_sha256": snap["content_sha256"],
           "schema_version": snap["schema_version"], "engine_version": snap["engine_version"],
           "policy_version": snap["policy"].get("policy_version"),
           "registry_seed_sha256": snap["source_manifest"]["registry_seed_sha256"],
           "origin": snap["source_manifest"]["origin"], "validation_status": report["status"],
           "manifest_json": json.dumps(snap["source_manifest"], ensure_ascii=False), "activated": bool(args.activate)}
    client.load_json("KNOWLEDGE_SNAPSHOT", KNOWLEDGE_SNAPSHOT_SCHEMA, [row], "WRITE_APPEND")
    print(json.dumps({"snapshot_id": sid, "content_sha256": snap["content_sha256"], "status": report["status"],
                      "warnings": report["warnings"], "logged": True}, ensure_ascii=False, indent=1))
    return 0


def cmd_verify(args) -> int:
    snap = load_snapshot(args.snapshot_id)  # raises on hash/gate/schema problems
    seed = registry_tables()
    mismatches = []
    for name in OWNED_TABLES:
        live = snap.data["source_manifest"]["tables"][name]
        from app.v3.registry import rows_sha256
        if live["sha256"] != rows_sha256(seed[name]):
            mismatches.append(name)
    print(json.dumps({"snapshot_id": snap.snapshot_id, "content_sha256": snap.data["content_sha256"],
                      "validation": snap.data["validation"]["status"],
                      "seed_tables_differ_from_snapshot": mismatches}, ensure_ascii=False, indent=1))
    return 0 if not mismatches else 2


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("validate")
    v.add_argument("--external", default="tests/v3/fixtures/external_ref_tables_2026-09-25.json")
    sub.add_parser("load-bq")
    b = sub.add_parser("build-snapshot")
    b.add_argument("--activate", action="store_true")
    vs = sub.add_parser("verify-snapshot")
    vs.add_argument("snapshot_id", nargs="?", default=None)
    args = ap.parse_args()
    return {"validate": cmd_validate, "load-bq": cmd_load_bq, "build-snapshot": cmd_build,
            "verify-snapshot": cmd_verify}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
