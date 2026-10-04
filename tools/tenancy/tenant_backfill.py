#!/usr/bin/env python3
"""Owner-gated bounded pilot coordinator; never activates lifecycle or Scheduler.

plan is offline. start writes a lease/checkpoint and executes one canonical job.
reconcile reads a terminal execution/journal, validates persisted key counts and appends
existing checkpoint/coverage evidence. No SQL DML, credential reads or new Cloud Run jobs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
from tools.tenancy import registry as R, tenant_tables as TT, tenant_lifecycle as TL
from tools.tenancy.validation import parse_tenant_json
import backfill_core as B
import checkpoints as CK

RUN_API = "https://run.googleapis.com/v2"
SCHED_API = "https://cloudscheduler.googleapis.com/v1"


def target(tenant_id):
    c = R.terraform_inputs(tenant_id)
    if c["datasets"]["ozon_raw"] != "ozon_raw" or c["datasets"]["ref"] != "ref":
        raise B.EvidenceError("noncanonical pilot datasets")
    return c


def make_plan(tenant_id, entity, frm, to, generation, origin, today=None):
    c = target(tenant_id)
    env = {"BACKFILL_MODE": B.VERSION, "TENANT_BINDING_REQUIRED": "1", "STRICT_PAGE_CAPS": "1",
           "BACKFILL_TARGET_PROJECT": c["project_id"], "SINCE": frm, "UNTIL": to,
           "BACKFILL_GENERATION": generation, "BACKFILL_ORIGIN": origin}
    p = B.plan(env, entity, c["project_id"], "ozon_raw", "ref", today or datetime.now(B.MSK).date())
    if entity not in {"catalog", "fbo_postings", "finance_accrual", "supplies", "ads_campaigns",
                       "ads_expense_daily", "ads_sku_daily"}:
        raise B.EvidenceError("no historical source execution for snapshot-only domain")
    if (date.fromisoformat(to) - date.fromisoformat(frm)).days >= 31:
        raise B.EvidenceError("bounded pilot scope exceeds31days; full-history plan separately approved")
    if entity not in set(R.load_tenant(tenant_id)["marketplaces"]["ozon"]["entities"]):
        raise B.EvidenceError("entity not enabled in canonical registry")
    out = {"mode": "BOUNDED_PILOT", "tenant_id": tenant_id,
           "image": c["marketplaces"]["ozon"]["runtime_image"], "runtime_plan": p,
           "max_requests": 400, "max_units": 20, "max_order_batches": 1}
    out["ack_hash"] = B.digest(out)
    return out


def validate_plan(doc, ack_hash):
    if not isinstance(doc, dict) or doc.get("ack_hash") != ack_hash:
        raise B.EvidenceError("exact reviewed pilot hash required")
    unsigned = {k: v for k, v in doc.items() if k != "ack_hash"}
    if B.digest(unsigned) != ack_hash or doc.get("mode") != "BOUNDED_PILOT":
        raise B.EvidenceError("modified or unsupported pilot plan")
    p = doc["runtime_plan"]
    expected = make_plan(doc["tenant_id"], p["entity"], p["from"], p["to"], p["generation"], p["origin"])
    if expected != doc:
        raise B.EvidenceError("pilot plan/config/image differs from current reviewed contract")
    c = target(doc["tenant_id"])
    from tools.tenancy import platform as PL
    matches = [parse_tenant_json(f.read_text()) for f in (REPO / PL.RUNTIME_RELEASES_DIR / "ozon").glob("*.json")]
    matches = [r for r in matches if r.get("image") == doc["image"]]
    if len(matches) != 1:
        raise B.EvidenceError("backfill image release evidence missing/ambiguous")
    facts = matches[0].get("verification", {}).get("built_artifact", {})
    if facts.get("backfill_window_v1") != "PASS" or facts.get("backfill_implementation_hash") != B.implementation_hash():
        raise B.EvidenceError("exact image has no matching qualified WINDOW_V1 capability")
    return c


def resources(c):
    base = f"projects/{c['project_id']}/locations/{c['region']}"
    return base, c["marketplaces"]["ozon"]["jobs"]


def preflight(c, doc, now):
    tables = TT.Tables(c["project_id"])
    chain, decisions, ledger, rows, hold = TL.read_state(c, tables)
    import lifecycle_core as L
    state = L.current_state(chain)
    if hold or state not in {L.VALIDATING, L.CAPABILITY_DISCOVERY}:
        raise B.EvidenceError("pilot lifecycle/hold gate denied")
    binding, creds = TL.operator_binding(c, tables, now, [doc["runtime_plan"]["entity"]])
    if any(v != "BOUND" for v in binding.values()) or any(v["status"] != "PASS" for v in creds.values()):
        raise B.EvidenceError("pilot requires verified binding/credential evidence")
    base, expected = resources(c)
    live = TT._req("GET", f"{RUN_API}/{base}/jobs")
    jobs = {j["name"].rsplit("/",1)[-1]: j for j in live.get("jobs", [])}
    expected_names = set(expected) | {"tenant-control"}
    if set(jobs) != expected_names or live.get("nextPageToken"):
        raise B.EvidenceError("unexpected tenant job inventory")
    for name, cfg in expected.items():
        job = jobs[name]
        template = job["template"]["template"]
        containers = template["containers"]
        if len(containers) != 1 or containers[0]["image"] != doc["image"]:
            raise B.EvidenceError("pilot exact-image parity failed")
        actual = {e["name"]: e.get("value") for e in containers[0].get("env", [])}
        if any(actual.get(k) != v for k,v in cfg["env"].items()):
            raise B.EvidenceError("canonical runtime env parity failed")
        if template.get("serviceAccount") != f"{c['marketplaces']['ozon']['service_accounts']['runtime']}@{c['project_id']}.iam.gserviceaccount.com":
            raise B.EvidenceError("runtime identity mismatch")
        if job.get("runningCount", 0):
            raise B.EvidenceError("tenant ingestion execution already active")
        executions = TT._req("GET", f"{RUN_API}/{job['name']}/executions?pageSize=1000")
        if executions.get("nextPageToken") or any(not e.get("completionTime") for e in executions.get("executions", [])):
            raise B.EvidenceError("active/unproven tenant execution")
    control = jobs["tenant-control"]
    template = control["template"]["template"]
    containers = template["containers"]
    expected_control = c["control"]["job"]
    if len(containers) != 1 or containers[0]["image"] != doc["image"] or template.get("serviceAccount") != c["control"]["email"]:
        raise B.EvidenceError("control image/identity parity failed")
    actual = {e["name"]: e.get("value") for e in containers[0].get("env", [])}
    if any(actual.get(k) != v for k,v in expected_control["env"].items()):
        raise B.EvidenceError("control env parity failed")
    executions = TT._req("GET", f"{RUN_API}/{control['name']}/executions?pageSize=1000")
    if executions.get("nextPageToken") or any(not e.get("completionTime") for e in executions.get("executions", [])):
        raise B.EvidenceError("control execution active/unproven")
    schedules = TT._req("GET", f"{SCHED_API}/{base}/jobs?pageSize=500")
    names = {j["name"].rsplit("/",1)[-1]: j for j in schedules.get("jobs", [])}
    if set(names) != {x["scheduler"] for x in expected.values()} or schedules.get("nextPageToken"):
        raise B.EvidenceError("Scheduler inventory differs")
    if any(j.get("state") != "PAUSED" for j in names.values()):
        raise B.EvidenceError("pilot requires every Scheduler PAUSED")
    return tables, ledger


def select(c, sql, parameters):
    if not sql.lstrip().startswith("SELECT ") or ";" in sql:
        raise B.EvidenceError("coordinator SQL must be a single SELECT")
    body = {"query": sql, "useLegacySql": False, "location": "EU", "timeoutMs": 10000,
            "maximumBytesBilled": "1073741824", "parameterMode": "NAMED",
            "queryParameters": [{"name":k,"parameterType":{"type":t},"parameterValue":{"value":str(v)}}
                                for k,(t,v) in parameters.items()]}
    result = TT._req("POST", f"{TT.BQ}/projects/{c['project_id']}/queries", body)
    ref = result["jobReference"]
    for _ in range(30):
        if result.get("jobComplete"):
            break
        result = TT._req("GET", f"{TT.BQ}/projects/{c['project_id']}/queries/{ref['jobId']}?location=EU&timeoutMs=10000")
    if not result.get("jobComplete") or result.get("errors") or result.get("pageToken"):
        raise B.EvidenceError("bounded verification query incomplete/failed")
    fields = result.get("schema",{}).get("fields",[])
    return [{f["name"]: TT._cast(f,col.get("v")) for f,col in zip(fields,row["f"])} for row in result.get("rows",[])]


def checkpoint(doc, run_id, status, generation, now, evidence=None):
    p = doc["runtime_plan"]
    return {"backfill_id": B.digest(["BOUNDED_PILOT",p["plan_id"]])[:16], "entity": p["entity"],
            "window_from": p["from"], "window_to": p["to"], "status": status, "attempts": generation,
            "rows_written": (evidence or {}).get("state",{}).get("rows"), "run_id": run_id,
            "error_code": "PILOT_EXECUTION_FAILED" if status=="FAILED" else None,
            "error_detail": None, "updated_at": now.isoformat(), "plan_hash": doc["ack_hash"],
            "lease_owner": run_id, "lease_until": (now+CK.LEASE_TTL).isoformat(),
            "lease_generation": generation, "started_at": now.isoformat(),
            "completed_at": now.isoformat() if status=="DONE" else None,
            "evidence_json": json.dumps({"mode":"BOUNDED_PILOT", "proof":evidence},sort_keys=True)}


def start(doc, ack_hash):
    now = datetime.now(timezone.utc); c=validate_plan(doc, ack_hash)
    tables, ledger = preflight(c,doc,now)
    p=doc["runtime_plan"]; cid=B.digest(["BOUNDED_PILOT_EXCLUSIVE",c["project_id"]])[:16]
    leases=[(n,lb) for n,lb,created in tables.list_tables(c["datasets"]["tenant_locks"]) if CK.LEASE_RE.match(n) and CK.LEASE_RE.match(n).group(1)==cid]
    generation=1
    if leases:
        n,lb=max(leases,key=lambda pair:pair[0]); generation=int(CK.LEASE_RE.match(n).group(2))+1
        done=tables.get_table(c["datasets"]["tenant_locks"],f"LD_{cid}_{generation-1:04d}")
        until=lb.get("until")
        expired=until and until.isdigit() and datetime.fromtimestamp(int(until),timezone.utc)+CK.VISIBILITY_GRACE<=now
        if done is None and not expired:
            raise B.EvidenceError("pilot lease held; reconcile terminal execution before continuation")
    run_id=f"bf-{uuid.uuid4()}"
    lease=CK.lease_name(cid,generation)
    body={"tableReference":{"projectId":c["project_id"],"datasetId":c["datasets"]["tenant_locks"],"tableId":lease},
          "schema":{"fields":[{"name":"marker","type":"STRING"}]},
          "labels":{"until":str(int((now+CK.LEASE_TTL).timestamp())),"owner":run_id},
          "description":json.dumps({"mode":"BOUNDED_PILOT","ack_hash":ack_hash}),
          "expirationTime":str(int((now+CK.LEASE_TTL+CK.LEASE_TABLE_KEEP).timestamp()*1000))}
    TT._req("POST",f"{TT.BQ}/projects/{c['project_id']}/datasets/{c['datasets']['tenant_locks']}/tables",body)
    tables.append(c["datasets"]["tenant_ops"],"BACKFILL_CHECKPOINTS",[checkpoint(doc,run_id,"RUNNING",generation,now)])
    base,jobs=resources(c)
    name=next(n for n,cfg in jobs.items() if p["entity"] in cfg["entities"])
    overrides={"ENTITIES":p["entity"],"SINCE":p["from"],"UNTIL":p["to"],"INGESTION_RUN_ID":run_id,
               "BACKFILL_MODE":B.VERSION,"BACKFILL_TARGET_PROJECT":c["project_id"],
               "BACKFILL_GENERATION":p["generation"],"BACKFILL_ORIGIN":p["origin"],
               "BACKFILL_MAX_REQUESTS":str(doc["max_requests"]),"BACKFILL_MAX_UNITS":str(doc["max_units"])}
    out=TT._req("POST",f"{RUN_API}/{base}/jobs/{name}:run",{"overrides":{"containerOverrides":[{"env":[{"name":k,"value":v} for k,v in overrides.items()]}]}})
    return {"operation":out["name"],"run_id":run_id,"lease_generation":generation,"ack_hash":ack_hash}


def read_proof(c,doc,run_id):
    p=doc["runtime_plan"]; table=f"{c['project_id']}.ozon_raw.OZON_INGESTION_RUNS"
    rows=select(c,f"SELECT DISTINCT status, evidence_json FROM `{table}` WHERE started_at >= @origin AND ingestion_run_id = @run AND entity = @entity LIMIT 2",
                {"origin":("TIMESTAMP",p["origin"]),"run":("STRING",run_id),"entity":("STRING",p["entity"])})
    if len(rows)!=1 or not rows[0].get("evidence_json"):
        raise B.EvidenceError("terminal execution lacks unambiguous aggregate runtime proof")
    proof=parse_tenant_json(rows[0]["evidence_json"])
    if proof.get("plan")!=p:raise B.EvidenceError("runtime proof scope mismatch")
    B.validate(p,proof["state"])
    if rows[0]["status"] not in {"OK","IN_PROGRESS"}:
        raise B.EvidenceError("runtime failed; no completion proof")
    if (rows[0]["status"]=="OK") != proof["state"]["complete"]:
        raise B.EvidenceError("runtime status/completion mismatch")
    return proof


def reconcile(doc,ack_hash,receipt):
    c=validate_plan(doc,ack_hash)
    if receipt.get("ack_hash")!=ack_hash:raise B.EvidenceError("receipt ACK mismatch")
    base,jobs=resources(c)
    if not receipt["operation"].startswith(base+"/operations/"):
        raise B.EvidenceError("foreign operation receipt")
    operation=TT._req("GET",f"{RUN_API}/{receipt['operation']}")
    if not operation.get("done") or operation.get("error"):
        raise B.EvidenceError("execution active or failed; operator review required")
    execution=operation.get("response") or {}
    if not execution.get("completionTime") or execution.get("failedCount",0) or execution.get("succeededCount")!=1:
        raise B.EvidenceError("execution not proven successfully terminal")
    p=doc["runtime_plan"]
    job=next(n for n,cfg in jobs.items() if p["entity"] in cfg["entities"])
    template=execution["template"];containers=template["containers"]
    prefix=base+"/jobs/"+job+"/executions/"
    if not execution.get("name","").startswith(prefix) or len(containers)!=1 or containers[0]["image"]!=doc["image"] or template.get("serviceAccount")!=f"{c['marketplaces']['ozon']['service_accounts']['runtime']}@{c['project_id']}.iam.gserviceaccount.com":
        raise B.EvidenceError("execution job/image/identity provenance mismatch")
    env={v["name"]:v.get("value") for v in containers[0].get("env",[])}
    expected={"INGESTION_RUN_ID":receipt["run_id"],"BACKFILL_TARGET_PROJECT":c["project_id"],
        "ENTITIES":p["entity"],"SINCE":p["from"],"UNTIL":p["to"],"BACKFILL_MODE":B.VERSION,
        "BACKFILL_GENERATION":p["generation"],"BACKFILL_ORIGIN":p["origin"],
        "TENANT_BINDING_REQUIRED":"1","STRICT_PAGE_CAPS":"1"}
    if any(env.get(k)!=v for k,v in expected.items()):
        raise B.EvidenceError("execution provenance mismatch")
    now=datetime.now(timezone.utc);tables,ledger=preflight(c,doc,now)
    cid=B.digest(["BOUNDED_PILOT_EXCLUSIVE",c["project_id"]])[:16]
    lease=tables.get_table(c["datasets"]["tenant_locks"],CK.lease_name(cid,receipt["lease_generation"]))
    if not lease or lease[0].get("owner")!=receipt["run_id"] or parse_tenant_json(lease[1] or "{}").get("ack_hash")!=ack_hash:
        raise B.EvidenceError("terminal execution lease/ACK provenance mismatch")
    proof=read_proof(c,doc,receipt["run_id"])
    # Persist traversal checkpoint. DQ/coverage remain separate; no blanket COMPLETE coverage.
    status="DONE" if proof["state"]["complete"] else "RUNNING"
    tables.append(c["datasets"]["tenant_ops"],"BACKFILL_CHECKPOINTS",
                  [checkpoint(doc,receipt["run_id"],status,receipt["lease_generation"],now,proof)])
    cid=B.digest(["BOUNDED_PILOT_EXCLUSIVE",c["project_id"]])[:16]
    # Terminal completion sign permits only the next CAS generation in this pilot namespace.
    tables.create_marker(c["datasets"]["tenant_locks"],f"LD_{cid}_{receipt['lease_generation']:04d}",
                         {"owner":receipt["run_id"]},json.dumps({"operation":receipt["operation"],"ack_hash":ack_hash}))
    return {"checkpoint":status,"sequence":proof["state"]["sequence"],"rows_observed":proof["state"]["rows"],
            "orders":proof["state"]["orders"],"supplies":proof["state"]["supplies"],"bundles":proof["state"]["bundles"],
            "coverage":"UNPROVEN_PENDING_DQ", "lifecycle_changed":False,"scheduler_changed":False}



def source_detail(detail):
    # A lost insert acknowledgement may repeat a durable sequence after transport
    # retries. Source/window/business proof must agree; attempt telemetry may differ.
    return {k:v for k,v in detail.items() if k not in {"transport_requests","transport_retries"}}


def verify_coverage(doc,ack_hash):
    """Read-back business grain plus source accounting; then append existing scoped evidence.

    API traversal completion is separate from economic DQ/finality. A partial pilot cannot
    become a full-history boundary or a READY decision.
    """
    c=validate_plan(doc,ack_hash);now=datetime.now(timezone.utc)
    tables,ledger=preflight(c,doc,now)
    p=doc["runtime_plan"];journal=f"{c['project_id']}.ozon_raw.OZON_INGESTION_RUNS"
    params={"origin":("TIMESTAMP",p["origin"]),"pid":("STRING",p["plan_id"]),"entity":("STRING",p["entity"])}
    units=select(c,f"SELECT DISTINCT backfill_sequence, backfill_detail_json FROM `{journal}` WHERE started_at >= @origin AND backfill_plan_id = @pid AND entity = @entity AND status = 'OK' AND backfill_sequence IS NOT NULL ORDER BY backfill_sequence",params)
    expected={};details={};unknown=set();limits=set()
    for u in units:
        seq=u["backfill_sequence"];d=source_detail(parse_tenant_json(u["backfill_detail_json"] or "{}"))
        if seq in details and details[seq]!=d:raise B.EvidenceError("historical checkpoint sequence conflict")
        details[seq]=d
    if not details or sorted(details)!=list(range(1,max(details)+1)):
        raise B.EvidenceError("source proof sequence has a gap")
    for d in details.values():
        if d.get("action")=="WINDOW_COMPLETE":
            for day,count in d["by_moscow_day"].items():expected[day]=expected.get(day,0)+count
        elif d.get("action") in {"DAY_COMPLETE","SKU_BATCH_COMPLETE"}:
            day=d["day"];expected[day]=expected.get(day,0)+d.get("expected_unique_rows",0)
            unknown.update(d.get("unknown_type_ids",[]))
        if d.get("retention_completeness")=="UNPROVEN_EMPTY":limits.add(d["day"])
    latest=select(c,f"SELECT DISTINCT evidence_json FROM `{journal}` WHERE started_at >= @origin AND backfill_plan_id = @pid AND entity = @entity AND backfill_sequence = @seq",dict(params,seq=("INT64",max(details))))
    if len(latest)!=1:raise B.EvidenceError("latest source state ambiguous")
    state=parse_tenant_json(latest[0]["evidence_json"])["state"];B.validate(p,state)
    mapping={"fbo_postings":("RAW_OZON_POSTINGS_FBO","posting_number,sku","order_date BETWEEN DATE_SUB(@day,INTERVAL 1 DAY) AND @day AND DATE(TIMESTAMP(created_at),'Europe/Moscow') = @day"),
        "finance_accrual":("RAW_OZON_FINANCE_ACCRUAL","accrual_id,type_id,sku","event_date = @day"),
        "ads_expense_daily":("RAW_OZON_ADS_EXPENSE_DAILY","date,campaign_id","date = @day"),
        "ads_sku_daily":("RAW_OZON_ADS_SKU_DAILY","date,campaign_id,sku","date = @day")}
    coverage=[];readback=[]
    if p["entity"] in mapping:
        table,keys,predicate=mapping[p["entity"]]
        cur=date.fromisoformat(p["from"])
        while cur<=date.fromisoformat(p["to"]):
            day=str(cur)
            if p["entity"]=="fbo_postings":
                complete=state["progress"]["completed_to"]>=B.utc_ms(str(cur+timedelta(days=1)))
            else:complete=state["progress"]["next_day"]>day
            if complete:
                out=select(c,f"SELECT COUNT(*) AS rows_n, COUNT(DISTINCT TO_JSON_STRING(STRUCT({keys}))) AS keys_n FROM `{c['project_id']}.ozon_raw.{table}` WHERE {predicate}",{"day":("DATE",day)})[0]
                n=out["rows_n"];k=out["keys_n"]
                if n!=k or n!=expected.get(day,0):
                    raise B.EvidenceError("PILOT_DATA_RECONCILIATION_FAILED: source/key/persisted counts differ")
                status="UNKNOWN" if day in limits else "COMPLETE"
                reason="EMPTY_SOURCE_RETENTION_UNPROVEN" if day in limits else None
                coverage.append({"entity":p["entity"],"coverage_date":day,"status":status,"reason":reason,
                    "rows_loaded":n,"source_run_id":p["plan_id"],"evaluated_at":now.isoformat()})
                item={"day":day,"rows":n,"keys":k,"coverage":status}
                if p["entity"] in {"fbo_postings","finance_accrual","ads_sku_daily"}:
                    # Current retained ALL+ARCHIVED identity only, never fabricate historic status.
                    link=select(c,f"SELECT COUNTIF(r.sku IS NOT NULL) AS sku_rows, COUNTIF(r.sku IS NOT NULL AND NOT EXISTS (SELECT 1 FROM `{c['project_id']}.ozon_raw.RAW_OZON_CATALOG` c WHERE c.snapshot_date = @snapshot AND c.sku = r.sku)) AS unresolved_sku_rows FROM `{c['project_id']}.ozon_raw.{table}` r WHERE {predicate}",
                                {"day":("DATE",day),"snapshot":("DATE",str(now.astimezone(B.MSK).date()))})[0]
                    item["sku_linkage"]=link
                    if link["unresolved_sku_rows"]:
                        raise B.EvidenceError("PILOT_SKU_LINKAGE_UNPROVEN: retained catalog does not resolve every source SKU")
                readback.append(item)
            cur+=timedelta(days=1)
    # Snapshot/source enumeration pilots use their actual observation date, not SINCE.
    if p["entity"]=="supplies":
        progress=state["progress"]
        ids=[str(x) for x in progress["seen_orders"]]
        pending={str(link[0]) for link in progress["pending"]}
        if progress["bundle"] is not None:pending.add(str(progress["bundle"]["link"][0]))
        bids=[key for key in progress["bundle_links"] if key not in pending]
        expected_bundles=sum(d["source_items"] for d in details.values() if d.get("bundle_complete"))
        checks=[("RAW_OZON_SUPPLY_ORDERS","order_id", "order_id",ids,state["orders"]),
                ("RAW_OZON_SUPPLIES","order_id,supply_id","order_id",ids,state["supplies"]),
                ("RAW_OZON_SUPPLY_BUNDLES","bundle_id,sku","bundle_id",bids,expected_bundles)]
        for table,keys,filterkey,values,expected_count in checks:
            counts=select(c,f"SELECT COUNT(*) AS rows_n, COUNT(DISTINCT TO_JSON_STRING(STRUCT({keys}))) AS keys_n FROM `{c['project_id']}.ozon_raw.{table}` WHERE CAST({filterkey} AS STRING) IN UNNEST(JSON_VALUE_ARRAY(@ids))",{"ids":("STRING",json.dumps(values))})[0]
            if counts["rows_n"]!=counts["keys_n"] or counts["rows_n"]!=expected_count:
                raise B.EvidenceError("PILOT_SUPPLY_KEY_ACCOUNTING_FAILED")
            readback.append({"table":table,"rows":counts["rows_n"],"keys":counts["keys_n"]})
        if len(bids)!=state["bundles"]:raise B.EvidenceError("bundle completion accounting mismatch")
        readback.append({"source_traversal_complete":state["complete"],"orders":state["orders"],
                         "supplies":state["supplies"],"bundles":state["bundles"],
                         "unfinished_bundles":len(pending),"content_unavailable_supplies":progress.get("missing_bundle_links",0),
                         "coverage":"PARTIAL_SOURCE_LIMITATION"})
        coverage.append({"entity":p["entity"],"coverage_date":str(now.astimezone(B.MSK).date()),
            "status":"PARTIAL","reason":"BOUNDED_RETAINED_ORDER_PREFIX_NOT_DATED_HISTORY",
            "rows_loaded":state["rows"],"source_run_id":p["plan_id"],"evaluated_at":now.isoformat()})
    elif p["entity"] in {"catalog","ads_campaigns"}:
        if not state["complete"]:raise B.EvidenceError("snapshot traversal incomplete")
        if p["entity"]=="catalog":
            table,keys="RAW_OZON_CATALOG","snapshot_date,sku"
            final=next(d for d in reversed(list(details.values())) if "archived_count" in d)
            expected_count=final["all_count"]+final["archived_count"]
        else:
            table,keys="RAW_OZON_ADS_CAMPAIGNS","snapshot_date,campaign_id"
            expected_count=next(d["campaigns"] for d in details.values() if d.get("action")=="CAMPAIGNS_COMPLETE")
        counts=select(c,f"SELECT COUNT(*) AS rows_n, COUNT(DISTINCT TO_JSON_STRING(STRUCT({keys}))) AS keys_n FROM `{c['project_id']}.ozon_raw.{table}` WHERE snapshot_date = @day",{"day":("DATE",p["observation_date"])})[0]
        if counts["rows_n"]!=counts["keys_n"] or counts["rows_n"]!=expected_count:
            raise B.EvidenceError("PILOT_SNAPSHOT_KEY_ACCOUNTING_FAILED")
        readback.append({"table":table,"rows":counts["rows_n"],"keys":counts["keys_n"],"historical_status":"UNPROVEN"})
        coverage.append({"entity":p["entity"],"coverage_date":p["observation_date"],"status":"COMPLETE",
            "reason":"CURRENT_SNAPSHOT_ONLY_NOT_HISTORICAL_STATUS", "rows_loaded":counts["rows_n"],
            "source_run_id":p["plan_id"],"evaluated_at":now.isoformat()})
    if coverage:tables.append(c["datasets"]["tenant_ops"],"DATA_COVERAGE",coverage)
    if p["entity"]=="finance_accrual":
        import dq as DQ
        unresolved=select(c,f"SELECT COUNTIF(operation_name IS NULL OR operation_name = 'UNKNOWN') AS n FROM `{c['project_id']}.ozon_raw.RAW_OZON_FINANCE_ACCRUAL` WHERE event_date BETWEEN @frm AND @to",
                          {"frm":("DATE",p["from"]),"to":("DATE",p["to"])})[0]["n"]
        check=DQ.evaluate({"finance_unresolved_rows":unresolved})["FIN_CLASSIFICATION"]
        dq={"result_id":B.digest([p["plan_id"],"FIN_CLASSIFICATION",now.isoformat()])[:32],
            "check_id":"FIN_CLASSIFICATION","period_from":p["from"],"period_to":p["to"],
            "metric":check["metric"],"source_a":"RAW_OZON_FINANCE_ACCRUAL","source_b":None,
            "value_a":unresolved,"value_b":None,"difference":None,"difference_pct":None,
            "tolerance_abs":None,"tolerance_pct":None,"status":check["status"],"severity":check["severity"],
            "explanation":json.dumps({"scope":"BOUNDED_PILOT","unknown_type_ids":sorted(unknown),"economic_finality":"UNPROVEN"}),
            "evaluated_at":now.isoformat(),"run_id":p["plan_id"]}
        tables.append(c["datasets"]["tenant_ops"],"DQ_RESULTS",[dq])
    # HISTORY_BOUNDARIES must represent source-boundary discovery, not pilot progress.
    # Existing lifecycle selects latest per entity; publishing a pilot pseudo-boundary would
    # silently shadow authoritative discovery. Pilot date evidence stays in checkpoint proof.
    return {"scope":"BOUNDED_PILOT","source_sequence":max(details),"readback":readback,
            "unknown_finance_type_ids":sorted(unknown),"history_boundary_published":False,"full_history":"UNPROVEN","economic_finality":"UNPROVEN"}

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest="command",required=True)
    p=sub.add_parser("plan")
    for key in ("tenant","entity","since","until","generation","origin"):p.add_argument("--"+key,required=True)
    for cmd in ("start","reconcile","verify"):
        p=sub.add_parser(cmd);p.add_argument("--plan",type=Path,required=True);p.add_argument("--ack-hash",required=True)
        if cmd=="reconcile":p.add_argument("--receipt",type=Path,required=True)
    args=parser.parse_args(argv)
    try:
        if args.command=="plan":out=make_plan(args.tenant,args.entity,args.since,args.until,args.generation,args.origin)
        else:
            doc=parse_tenant_json(args.plan.read_text())
            out=start(doc,args.ack_hash) if args.command=="start" else (verify_coverage(doc,args.ack_hash) if args.command=="verify" else reconcile(doc,args.ack_hash,parse_tenant_json(args.receipt.read_text())))
        print(json.dumps(out,sort_keys=True))
        return 0
    except (B.EvidenceError,TT.TableError,KeyError,ValueError) as error:
        # API helpers never carry secret response bodies; contract failures reveal no payload.
        print("BACKFILL_BLOCKED: "+str(error),file=sys.stderr);return 2


if __name__=="__main__":sys.exit(main())
