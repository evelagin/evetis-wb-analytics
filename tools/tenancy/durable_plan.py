"""Durable immutable orchestration records in existing tenant_ops / tenant_locks.

No marketplace HTTP, SQL DML, credential reads, temporary files or new datastore.
A reader issues SELECT; a separately authenticated append-only writer issues insertAll.
Metadata commit markers avoid treating streaming visibility delay as absent evidence.
This module alone does not authorize dispatch or prove a deployed controller.
"""
from __future__ import annotations
import json
import re
from datetime import datetime, timezone
from tools.tenancy import tenant_backfill as BF
from tools.tenancy.validation import parse_tenant_json

VERSION = "CLOUD_BACKFILL_V1"
HASH = re.compile(r"^[0-9a-f]{64}$")
KINDS = frozenset({"MANIFEST", "DISPATCH_INTENT", "DISPATCH_RECEIPT", "RECONCILED", "WAITING", "STOPPED", "COMPLETE", "DEPENDENCY_PLAN", "SNAPSHOT_CERT", "FAILED_PRE_SOURCE", "FULL_MANIFEST", "CHUNK_PLAN", "CHUNK_COMPLETE", "FULL_COMPLETE", "T5_PARENT_COMPLETE", "CHUNK_SUPERSEDED"})
MAX_RECORD_BYTES = 900000


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def digest(value):
    return BF.B.digest(value)


def check_hash(value):
    if not isinstance(value, str) or not HASH.fullmatch(value):
        raise BF.B.EvidenceError("invalid immutable record hash")
    return value


def root_manifest(tenant, source_sha, image, created_at, purpose, plans):
    c = BF.target(tenant)
    if not re.fullmatch(r"[0-9a-f]{40}", source_sha):
        raise BF.B.EvidenceError("exact source SHA required")
    if purpose not in {"QUALIFICATION", "FULL_HISTORY"} or not plans:
        raise BF.B.EvidenceError("explicit nonempty frozen scope required")
    dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    if dt.utcoffset() is None or dt.utcoffset().total_seconds() != 0:
        raise BF.B.EvidenceError("manifest UTC timestamp required")
    if image != c["marketplaces"]["ozon"]["runtime_image"]:
        raise BF.B.EvidenceError("manifest image differs from canonical release")
    from tools.tenancy import platform as PL
    releases = [parse_tenant_json(f.read_text()) for f in (BF.REPO / PL.RUNTIME_RELEASES_DIR / "ozon").glob("*.json")]
    releases = [r for r in releases if r.get("image") == image]
    if len(releases) != 1 or releases[0].get("source", {}).get("commit") != source_sha:
        raise BF.B.EvidenceError("runtime source/image provenance mismatch")
    seen = set()
    for doc in plans:
        BF.validate_plan(doc, doc.get("ack_hash"))
        if doc["tenant_id"] != tenant or doc["image"] != image or doc["ack_hash"] in seen:
            raise BF.B.EvidenceError("foreign, duplicate or mixed-image plan")
        seen.add(doc["ack_hash"])
    out = {"version": VERSION, "tenant": tenant, "project": c["project_id"],
           "source_sha": source_sha, "image": image, "created_at": created_at,
           "purpose": purpose, "plans": plans}
    out["hash"] = digest(out)
    if len(encoded(out).encode()) > MAX_RECORD_BYTES:
        raise BF.B.EvidenceError("manifest exceeds bounded record size; shard before freeze")
    return out


def validate_manifest(doc):
    if not isinstance(doc, dict):
        raise BF.B.EvidenceError("manifest must be an object")
    check_hash(doc.get("hash"))
    if digest({k: v for k, v in doc.items() if k != "hash"}) != doc["hash"]:
        raise BF.B.EvidenceError("immutable manifest hash mismatch")
    if doc == BF.QF.manifest():
        for p in doc["plans"]:
            BF.validate_plan(p, p["ack_hash"])
        return BF.target(doc["tenant"])
    expected = root_manifest(doc["tenant"], doc["source_sha"], doc["image"],
                             doc["created_at"], doc["purpose"], doc["plans"])
    if expected != doc:
        raise BF.B.EvidenceError("manifest schema/config/source scope mismatch")
    return BF.target(doc["tenant"])


class DurableRecords:
    """Reader and writer are deliberately distinct authority boundaries.

    write_record accepts exactly one existing checkpoint-table append. reader is
    the existing SELECT-only coordinator verifier. No writer query capability is
    needed. Neither credential needs marketplace secrets or RAW mutation rights.
    """
    def __init__(self, contract, metadata, select, write_record):
        self.c, self.metadata, self.select, self.write_record = contract, metadata, select, write_record

    def _marker(self, record_hash):
        return "BFR_" + check_hash(record_hash)

    def commit(self, root_hash, kind, sequence, payload, now):
        check_hash(root_hash)
        if kind not in KINDS or type(sequence) is not int or sequence < 0:
            raise BF.B.EvidenceError("unsupported orchestration record")
        if kind == "MANIFEST":
            validate_manifest(payload)
            if payload["hash"] != root_hash or sequence != 0:
                raise BF.B.EvidenceError("manifest/root identity mismatch")
            # Full-history activation is a separate gate; this storage foundation
            # must not accidentally publish an authorized full plan on code existence.
            if payload["purpose"] != "QUALIFICATION":
                raise BF.B.EvidenceError("FULL_HISTORY_GO_UNPROVEN: qualified gate adapter required")
        if kind == "FULL_MANIFEST":
            from tools.tenancy import full_history as F
            F.validate_manifest(payload)
            if payload["hash"] != root_hash or sequence != 0:
                raise BF.B.EvidenceError("full manifest/root identity differs")
        record = {"version": VERSION, "root_hash": root_hash, "kind": kind,
                  "sequence": sequence, "payload": payload}
        record_hash = digest(record)
        data = encoded(record)
        if len(data.encode()) > MAX_RECORD_BYTES:
            raise BF.B.EvidenceError("record size guard; never truncate durable evidence")
        # Record-addressed markers alone do not elect a single dispatch: two
        # controllers can produce different run IDs for the same sequence.
        # A consistent, non-expiring per-sequence CAS fence elects exactly one
        # immutable intent BEFORE any journal append or Cloud Run POST.
        if kind in {"DISPATCH_INTENT", "DISPATCH_RECEIPT", "RECONCILED"}:
            if not 1 <= sequence <= 9999999999:
                raise BF.B.EvidenceError("invalid durable dispatch sequence")
            fence = f"BFQ_{root_hash}_{sequence:010d}_{kind}"
            labels = {"root": root_hash[:16], "kind": kind.lower()}
            description = encoded({"record_hash": record_hash})
            locks = self.c["datasets"]["tenant_locks"]
            existing = self.metadata.get_table(locks, fence)
            if existing is None:
                if not self.metadata.create_marker(locks, fence, labels, description):
                    existing = self.metadata.get_table(locks, fence)
                else:
                    existing = (labels, description)
            if existing != (labels, description):
                raise BF.B.EvidenceError("concurrent durable sequence conflict; never dispatch twice")
        marker = self._marker(record_hash)
        locks = self.c["datasets"]["tenant_locks"]
        description = encoded({"root_hash": root_hash, "record_hash": record_hash,
                               "kind": kind, "sequence": sequence})
        existing = self.metadata.get_table(locks, marker)
        if existing:
            if existing != ({"root": root_hash[:16], "kind": kind.lower()}, description):
                raise BF.B.EvidenceError("durable commit marker conflict")
            self.read(root_hash, record_hash)
            return record_hash
        # Existing schema, separate namespace. This is not a T5 date-plan PENDING row.
        row = {f["name"]: None for f in parse_tenant_json((BF.REPO / "tools/tenancy/schema/tenant_ops/BACKFILL_CHECKPOINTS.json").read_text())["schema"]}
        day = now.astimezone(BF.B.MSK).date().isoformat()
        row.update(backfill_id=record_hash[:16], entity="orchestration", window_from=day,
                   window_to=day, status="FAILED" if kind=="FAILED_PRE_SOURCE" else "RUNNING", attempts=sequence, updated_at=now.isoformat(),
                   plan_hash=root_hash, evidence_json=data)
        self.write_record(row)
        self._read_rows(root_hash, record_hash)  # SELECT sees streaming rows; Tables API may lag.
        labels = {"root": root_hash[:16], "kind": kind.lower()}
        if not self.metadata.create_marker(locks, marker, labels, description):
            got = self.metadata.get_table(locks, marker)
            if got != (labels, description):
                raise BF.B.EvidenceError("concurrent durable marker conflict")
        return record_hash

    def history(self, root_hash, max_records=10000):
        """Discover committed records via consistent metadata, never local state.

        The full root in marker description disambiguates the short label.
        A fence without a committed row is deliberately not a runnable intent;
        only the elected identical record can repair publication, never a new
        run ID. Retention/configuration is an activation preflight requirement.
        """
        check_hash(root_hash)
        if type(max_records) is not int or not 1 <= max_records <= 10000:
            raise BF.B.EvidenceError("bounded history inventory required")
        markers = {}
        for name, labels, created in self.metadata.list_tables(self.c["datasets"]["tenant_locks"]):
            if not name.startswith("BFR_") or labels.get("root") != root_hash[:16]:
                continue
            marker = self.metadata.get_table(self.c["datasets"]["tenant_locks"], name)
            if marker is None:
                raise BF.B.EvidenceError("committed marker disappeared")
            description = parse_tenant_json(marker[1] or "{}")
            if description.get("root_hash") != root_hash:
                continue
            if name != self._marker(description.get("record_hash")):
                raise BF.B.EvidenceError("durable marker identity mismatch")
            if len(markers) >= max_records:
                raise BF.B.EvidenceError("durable history exceeds bounded inventory; shard before continuation")
            markers[description['record_hash']] = marker
        if not markers:
            return []
        # One bounded SELECT rather than one query job per committed record.
        # Metadata is the commit authority; uncommitted streaming rows remain
        # excluded even when visible in this query.
        values=self.select(self.c, f"SELECT DISTINCT evidence_json FROM `{self.c['project_id']}.{self.c['datasets']['tenant_ops']}.BACKFILL_CHECKPOINTS` WHERE plan_hash = @root LIMIT {max_records+1}", {'root':('STRING',root_hash)})
        if len(values)>max_records:
            raise BF.B.EvidenceError('durable history query guard; shard before continuation')
        rows={}
        for value in values:
            record=parse_tenant_json(value['evidence_json']);h=digest(record)
            if h not in markers:continue
            if record.get('version')!=VERSION or record.get('root_hash')!=root_hash:
                raise BF.B.EvidenceError('durable record corruption/scope mismatch')
            expected=({'root':root_hash[:16],'kind':record['kind'].lower()},encoded({'root_hash':root_hash,'record_hash':h,'kind':record['kind'],'sequence':record['sequence']}))
            if markers[h]!=expected:
                raise BF.B.EvidenceError('commit marker/record mismatch')
            rows[h]=record
        if set(rows)!=set(markers):
            raise BF.B.EvidenceError('committed durable record missing/corrupt; never reset')
        return sorted(rows.values(), key=lambda r: (r["sequence"], r["kind"], digest(r)))

    def _read_rows(self, root_hash, record_hash):
        check_hash(root_hash); check_hash(record_hash)
        rows = self.select(self.c, f"SELECT DISTINCT evidence_json FROM `{self.c['project_id']}.{self.c['datasets']['tenant_ops']}.BACKFILL_CHECKPOINTS` WHERE plan_hash = @root AND backfill_id = @record LIMIT 2",
                           {"root": ("STRING", root_hash), "record": ("STRING", record_hash[:16])})
        if len(rows) != 1:
            raise BF.B.EvidenceError("durable record missing/ambiguous; do not retry dispatch")
        record = parse_tenant_json(rows[0]["evidence_json"])
        if digest(record) != record_hash or record.get("root_hash") != root_hash or record.get("version") != VERSION:
            raise BF.B.EvidenceError("durable record corruption/scope mismatch")
        return record

    def read(self, root_hash, record_hash):
        check_hash(root_hash); check_hash(record_hash)
        marker = self.metadata.get_table(self.c["datasets"]["tenant_locks"], self._marker(record_hash))
        if not marker:
            raise BF.B.EvidenceError("uncommitted durable record; no execution permitted")
        record = self._read_rows(root_hash, record_hash)
        expected = ({"root": root_hash[:16], "kind": record["kind"].lower()},
                    encoded({"root_hash": root_hash, "record_hash": record_hash,
                             "kind": record["kind"], "sequence": record["sequence"]}))
        if marker != expected:
            raise BF.B.EvidenceError("commit marker/record mismatch")
        return record


def quota_decision(reservations, unknown_exports, now, report_phase=None, floor=15, *, calibration=False):
    """Canonical rolling intents, not an assertion about Ozon's actual quota.

    Poll/download may finish a known submitted report with zero remaining intent
    allowance. An ambiguous INTENT never triggers a repeat POST.
    """
    if type(unknown_exports) is not int or unknown_exports < 0 or unknown_exports:
        raise BF.B.EvidenceError("unknown ordinary exports; quota accounting unproven")
    if (not calibration and floor != 15) or type(floor) is not int or not 1 <= floor <= 90:
        raise BF.B.EvidenceError("reviewed conservative floor must not be changed")
    if report_phase not in (None, "POLL", "INTENT"):
        raise BF.B.EvidenceError("corrupt async phase")
    if report_phase == "INTENT":
        return {"status": "STOPPED", "reason": "REPORT_SUBMISSION_AMBIGUOUS", "allowance": 0}
    by = {}
    for r in reservations:
        key = (check_hash(r["plan_id"]), r["sequence"])
        if type(r["sequence"]) is not int or r["sequence"] <= 0 or type(r["exports"]) is not int or not 1 <= r["exports"] <= 10:
            raise BF.B.EvidenceError("invalid durable reservation")
        at = datetime.fromisoformat(r["at"].replace("Z", "+00:00"))
        if at.utcoffset() is None or at > now:
            raise BF.B.EvidenceError("invalid reservation timestamp")
        # Identical insert retries collapse. A contradictory count fails closed;
        # repeated acknowledgements conservatively extend the expiry.
        if key in by and by[key][0] != r["exports"]:
            raise BF.B.EvidenceError("reservation conflict")
        by[key] = (r["exports"], max(at, by.get(key, (0, at))[1]))
    from datetime import timedelta
    active = [(n, at + timedelta(hours=24)) for n, at in by.values() if at + timedelta(hours=24) > now]
    used = sum(n for n, expiry in active)
    allowance = max(floor - used, 0)
    if report_phase == "POLL":
        return {"status": "POLL_ONLY", "allowance": allowance}
    if allowance:
        return {"status": "ELIGIBLE", "allowance": allowance}
    return {"status": "WAITING", "allowance": 0, "eligible_at": min(expiry for n, expiry in active).isoformat()}


def decide_tick(records, plan_hash, quota, active_execution=False, verified_failures=()):
    """Pure fail-closed dispatch protocol; network adapter must obey this verdict.

    Never dispatch after an intent lacking a receipt. Reconcile terminal receipts
    before any new intent; a waiting state is not completion or a runtime failure.
    """
    check_hash(plan_hash)
    current = [r for r in records if r.get("root_hash") == plan_hash]
    if any(r.get("version") != VERSION or r.get("kind") not in KINDS for r in current):
        raise BF.B.EvidenceError("corrupt durable orchestration history")
    if any(not any(r['kind']=='FAILED_PRE_SOURCE' and r['payload']==p and r['sequence']==p['dispatch_sequence'] for r in current) for p in verified_failures):
        raise BF.B.EvidenceError('verified failure lacks its immutable recovery record')
    recovered={p['dispatch_sequence'] for p in verified_failures}
    approved_stops={h for p in verified_failures for h in p['stop_hashes']}
    if any(r['root_hash']!=p['root_hash'] for p in verified_failures for r in current):
        raise BF.B.EvidenceError('foreign recovery root')
    if any(r["kind"] == "STOPPED" and digest(r) not in approved_stops for r in current):
        return {"action": "STOPPED"}
    intents = {r["sequence"]: r for r in current if r["kind"] == "DISPATCH_INTENT"}
    receipts = {r["sequence"]: r for r in current if r["kind"] == "DISPATCH_RECEIPT"}
    reconciled = {r["sequence"] for r in current if r["kind"] == "RECONCILED"}
    for kind in ("DISPATCH_INTENT", "DISPATCH_RECEIPT", "RECONCILED"):
        versions = {}
        for r in current:
            if r["kind"] == kind:
                seq = r["sequence"]
                if type(seq) is not int or seq < 1:
                    raise BF.B.EvidenceError("invalid dispatch sequence")
                if seq in versions and versions[seq] != r:
                    raise BF.B.EvidenceError("dispatch sequence conflict")
                versions[seq] = r
    if set(receipts) - set(intents) or reconciled - set(receipts):
        raise BF.B.EvidenceError("orphan receipt/reconciliation")
    if intents and sorted(intents) != list(range(1, max(intents) + 1)):
        raise BF.B.EvidenceError("dispatch sequence gap")
    if active_execution:
        return {"action": "MONITOR"}
    for seq in sorted(intents):
        if seq not in receipts:
            return {"action": "RECOVER_RECEIPT_OR_STOP", "sequence": seq}
        if seq not in reconciled and seq not in recovered:
            return {"action": "RECONCILE", "sequence": seq}
    if quota["status"] == "STOPPED":
        return {"action": "STOPPED", "reason": quota["reason"]}
    if quota["status"] == "WAITING":
        return {"action": "WAITING", "eligible_at": quota["eligible_at"]}
    if quota["status"] not in {"ELIGIBLE", "POLL_ONLY"}:
        raise BF.B.EvidenceError("invalid quota eligibility")
    return {"action": "PREPARE_NEXT", "sequence": max(intents, default=0) + 1,
            "poll_only": quota["status"] == "POLL_ONLY"}
