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
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
# A directly invoked tenancy CLI exposes tools/tenancy/platform.py on sys.path.
# Keep project facts qualified; otherwise uuid can import that file as stdlib
# platform on Linux. This changes only this process's module search boundary.
sys.path[:] = [p for p in sys.path if Path(p or '.').resolve() != Path(__file__).resolve().parent]
import uuid  # noqa: E402
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
from tools.tenancy import registry as R, tenant_tables as TT, tenant_lifecycle as TL
from tools.tenancy.validation import parse_tenant_json
import backfill_core as B
import checkpoints as CK
import qualification as QF

RUN_API = "https://run.googleapis.com/v2"
SCHED_API = "https://cloudscheduler.googleapis.com/v1"
GLOBAL_JOB_FIELDS = "items(metadata(name,namespace,labels)),metadata(continue),unreachable"


def global_job_url(project):
    # Documented v1 global endpoint used by gcloud run jobs list. Projected
    # metadata only: unknown Job environments/secret values are never read.
    return (f"https://run.googleapis.com/apis/run.googleapis.com/v1/namespaces/{project}/jobs"
            f"?limit=1000&fields={GLOBAL_JOB_FIELDS}")


def verify_global_jobs(response, names, region):
    if not isinstance(response, dict) or response.get("unreachable") or (response.get("metadata") or {}).get("continue"):
        raise B.EvidenceError("all-region tenant inventory incomplete")
    items=response.get("items", [])
    if not isinstance(items,list):
        raise B.EvidenceError("all-region tenant inventory malformed")
    found=[]
    for item in items:
        meta=item.get("metadata",{}) if isinstance(item,dict) else {}
        name=meta.get("name");location=(meta.get("labels") or {}).get("cloud.googleapis.com/location")
        if not isinstance(name,str) or location!=region:
            raise B.EvidenceError("unexpected tenant job in all-region inventory")
        found.append(name)
    if len(found)!=len(set(found)) or set(found)!=set(names):
        raise B.EvidenceError("unexpected tenant job in all-region inventory")


def target(tenant_id):
    c = R.terraform_inputs(tenant_id)
    if c["datasets"]["ozon_raw"] != "ozon_raw" or c["datasets"]["ref"] != "ref":
        raise B.EvidenceError("noncanonical pilot datasets")
    return c


def make_plan(tenant_id, entity, frm, to, generation, origin, today=None, *, max_units=20):
    if type(max_units) is not int or not 1 <= max_units <= 20:
        raise B.EvidenceError("bounded unit budget must be integer 1..20")
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
           "max_requests": 400, "max_units": max_units, "max_order_batches": 1}
    out["ack_hash"] = B.digest(out)
    return out


def validate_plan(doc, ack_hash):
    if not isinstance(doc, dict) or doc.get("ack_hash") != ack_hash:
        raise B.EvidenceError("exact reviewed pilot hash required")
    unsigned = {k: v for k, v in doc.items() if k != "ack_hash"}
    if B.digest(unsigned) != ack_hash or doc.get("mode") != "BOUNDED_PILOT":
        raise B.EvidenceError("modified or unsupported pilot plan")
    p = doc["runtime_plan"]
    observation = date.fromisoformat(p["observation_date"]) if "observation_date" in p else None
    expected = make_plan(doc["tenant_id"], p["entity"], p["from"], p["to"], p["generation"], p["origin"], today=observation, max_units=doc["max_units"])
    compatible = QF.matches(doc)
    if expected != doc and not compatible:
        raise B.EvidenceError("pilot plan/config/image differs from current reviewed contract")
    c = target(doc["tenant_id"])
    from tools.tenancy import platform as PL
    matches = [parse_tenant_json(f.read_text()) for f in (REPO / PL.RUNTIME_RELEASES_DIR / "ozon").glob("*.json")]
    matches = [r for r in matches if r.get("image") == c["marketplaces"]["ozon"]["runtime_image"]]
    if len(matches) != 1:
        raise B.EvidenceError("backfill image release evidence missing/ambiguous")
    facts = matches[0].get("verification", {}).get("built_artifact", {})
    if facts.get("backfill_window_v1") != "PASS" or facts.get("backfill_implementation_hash") != B.implementation_hash():
        raise B.EvidenceError("exact image has no matching qualified WINDOW_V1 capability")
    if compatible and facts.get("qualification_resume_root") != QF.ROOT:
        raise B.EvidenceError("current artifact has no exact historical qualification compatibility")
    return c


def execution_image(doc, c):
    return c["marketplaces"]["ozon"]["runtime_image"] if QF.matches(doc) else doc["image"]


def continuation_overrides(doc):
    return {"BACKFILL_RESUME_PLAN_ID":doc["runtime_plan"]["plan_id"]} if QF.matches(doc) else {}


def resources(c):
    base = f"projects/{c['project_id']}/locations/{c['region']}"
    return base, c["marketplaces"]["ozon"]["jobs"]


def canonical_template(job, expected_env, image, account, control=False):
    # Fixed deployment contract: infra/tenant/modules/ozon_runtime/main.tf.
    outer=job["template"];template=outer["template"];containers=template["containers"]
    if outer.get("taskCount",1)!=1 or outer.get("parallelism",0) not in (0,1):
        raise B.EvidenceError("canonical single-task concurrency differs")
    if template.get("timeout")!="3600s" or template.get("maxRetries",0)!=0:
        raise B.EvidenceError("canonical timeout/retry contract differs")
    if len(containers)!=1 or containers[0]["image"]!=image or template.get("serviceAccount")!=account:
        raise B.EvidenceError("canonical image/identity parity failed")
    container=containers[0]
    if (container.get("command") or [])!=(['python','lifecycle.py'] if control else []) or (container.get("args") or [])!=(['status'] if control else []):
        raise B.EvidenceError("canonical entrypoint/args drift")
    env=container.get("env",[]);actual={e["name"]:e.get("value") for e in env}
    if len(actual)!=len(env) or actual!=expected_env:
        raise B.EvidenceError("canonical env differs (unknown/duplicate/changed settings)")


def preflight(c, doc, now, backend=None, *, allow_active=False):
    if backend is not None:
        base_contract={k:v for k,v in backend.c.items() if k!='orchestration'}
        if base_contract!={k:v for k,v in c.items() if k!='orchestration'}:
            raise B.EvidenceError("cloud controller base registry contract drift")
        c=backend.c
    tables = backend.tables if backend is not None else TT.Tables(c["project_id"])
    request = backend.request if backend is not None else TT._req
    chain, decisions, ledger, rows, hold = TL.read_state(c, tables)
    import lifecycle_core as L
    state = L.current_state(chain)
    if hold or state not in {L.VALIDATING, L.CAPABILITY_DISCOVERY}:
        raise B.EvidenceError("pilot lifecycle/hold gate denied")
    binding, creds = TL.operator_binding(c, tables, now, [doc["runtime_plan"]["entity"]])
    if any(v != "BOUND" for v in binding.values()) or any(v["status"] != "PASS" for v in creds.values()):
        raise B.EvidenceError("pilot requires verified binding/credential evidence")
    base, expected = resources(c)
    orchestration = c.get("orchestration")
    expected_names = set(expected) | {"tenant-control"}
    if orchestration:
        expected_names.add(orchestration["job"]["name"])
    verify_global_jobs(request("GET",global_job_url(c["project_id"])),expected_names,c["region"])
    live = request("GET", f"{RUN_API}/{base}/jobs")
    items=live.get("jobs",[])
    jobs = {j["name"].rsplit("/",1)[-1]: j for j in items}
    if set(jobs) != expected_names or len(items)!=len(jobs) or live.get("nextPageToken") or any(j['name']!=base+'/jobs/'+n for n,j in jobs.items()):
        raise B.EvidenceError("unexpected tenant job inventory")
    for name, cfg in expected.items():
        job = jobs[name]
        canonical_template(job,cfg["env"],execution_image(doc,c),
                           f"{c['marketplaces']['ozon']['service_accounts']['runtime']}@{c['project_id']}.iam.gserviceaccount.com")
        if job.get("runningCount", 0) and not allow_active:
            raise B.EvidenceError("tenant ingestion execution already active")
        executions = request("GET", f"{RUN_API}/{job['name']}/executions?pageSize=1000")
        active=[e for e in executions.get('executions',[]) if not e.get('completionTime')]
        if executions.get('nextPageToken') or (active and not allow_active) or job.get('runningCount',0)>len(active):
            raise B.EvidenceError('active/unproven tenant execution')
        for execution in active:
            t=execution.get('template') or {};containers=t.get('containers') or []
            envs=containers[0].get('env',[]) if len(containers)==1 else []
            env={v['name']:v.get('value') for v in envs}
            if len(containers)!=1 or containers[0].get('image')!=execution_image(doc,c) or t.get('serviceAccount')!=f"{c['marketplaces']['ozon']['service_accounts']['runtime']}@{c['project_id']}.iam.gserviceaccount.com" or len(env)!=len(envs) or env.get('TENANT_BINDING_REQUIRED')!='1' or env.get('STRICT_PAGE_CAPS')!='1':
                raise B.EvidenceError('active runtime execution security provenance drift')
        if backend is not None and active:
            backend.active=True
    control = jobs["tenant-control"]
    canonical_template(control,c["control"]["job"]["env"],execution_image(doc,c),c["control"]["email"],control=True)
    executions = request("GET", f"{RUN_API}/{control['name']}/executions?pageSize=1000")
    active_control=[e for e in executions.get('executions',[]) if not e.get('completionTime')]
    if executions.get('nextPageToken') or (active_control and not allow_active) or control.get('runningCount',0)>len(active_control):
        raise B.EvidenceError('control execution active/unproven')
    for execution in active_control:
        task=execution.get('template') or {}; containers=task.get('containers') or []
        if len(containers)!=1 or containers[0].get('image')!=execution_image(doc,c) or task.get('serviceAccount')!=c['control']['email']:
            raise B.EvidenceError('active control execution security provenance drift')
    if backend is not None and active_control:
        backend.active=True
    if orchestration:
        from tools.tenancy import orchestration_contract as OC
        controller=jobs[orchestration['job']['name']]
        OC.verify_job(controller,orchestration)
        executions=request("GET",f"{RUN_API}/{controller['name']}/executions?pageSize=1000")
        own=getattr(backend,'current_execution',None) if backend is not None else None
        if not isinstance(own,str) or not own.startswith(controller['name']+'/executions/'):
            raise B.EvidenceError('own registered controller execution identity missing')
        if executions.get('nextPageToken') or any(not e.get('completionTime') and e.get('name')!=own for e in executions.get('executions',[])):
            raise B.EvidenceError('another controller execution active/unproven')
        if not any(e.get('name')==own for e in executions.get('executions',[])):
            raise B.EvidenceError('own controller execution not visible')
    schedules = request("GET", f"{SCHED_API}/{base}/jobs?pageSize=500")
    names = {j["name"].rsplit("/",1)[-1]: j for j in schedules.get("jobs", [])}
    ordinary_names={x["scheduler"] for x in expected.values()}
    scheduler_names=ordinary_names|({orchestration["scheduler"]["name"]} if orchestration else set())
    if set(names) != scheduler_names or schedules.get("nextPageToken"):
        raise B.EvidenceError("Scheduler inventory differs")
    if any(names[n].get("state") != "PAUSED" for n in ordinary_names):
        raise B.EvidenceError("pilot requires every Scheduler PAUSED")
    if orchestration:
        schedule=names[orchestration['scheduler']['name']]
        target=schedule.get('httpTarget') or {}
        token=target.get('oauthToken') or {}
        if schedule.get('schedule')!=orchestration['scheduler']['schedule'] or schedule.get('timeZone')!=orchestration['scheduler']['time_zone'] or target.get('body') not in (None,'','e30=') or schedule.get('state')!=orchestration['scheduler']['state'] or target.get('uri')!=orchestration['scheduler']['uri'] or target.get('httpMethod')!='POST' or token.get('serviceAccountEmail')!=orchestration['accounts']['wake']['email'] or token.get('scope')!='https://www.googleapis.com/auth/cloud-platform':
            raise B.EvidenceError('historical Scheduler target/identity/state drift')
    return tables, ledger


def select(c, sql, parameters, request=None):
    request = request or TT._req
    if not sql.lstrip().startswith("SELECT ") or ";" in sql:
        raise B.EvidenceError("coordinator SQL must be a single SELECT")
    body = {"query": sql, "useLegacySql": False, "location": "EU", "timeoutMs": 10000,
            "maximumBytesBilled": "1073741824", "parameterMode": "NAMED",
            "queryParameters": [{"name":k,"parameterType":{"type":t},"parameterValue":{"value":str(v)}}
                                for k,(t,v) in parameters.items()]}
    result = request("POST", f"{TT.BQ}/projects/{c['project_id']}/queries", body)
    ref = result["jobReference"]
    for _ in range(30):
        if result.get("jobComplete"):
            break
        result = request("GET", f"{TT.BQ}/projects/{c['project_id']}/queries/{ref['jobId']}?location=EU&timeoutMs=10000")
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


def pilot_lease_generation(ledger, leases, cid, now, done_reader, failed_reader=None):
    """Monotonic CAS generation survives lease-table expiry; old LD never releases a new owner."""
    recorded=[]
    for row in ledger:
        if parse_tenant_json(row.get("evidence_json") or "{}").get("mode") != "BOUNDED_PILOT":
            continue
        generation=row.get("lease_generation")
        if type(generation) is not int or not 1 <= generation <= 9999:
            raise B.EvidenceError("pilot lease ledger generation invalid")
        recorded.append(generation)
    generation=max(recorded,default=0)+1
    relevant=[(name,labels) for name,labels in leases if CK.LEASE_RE.match(name) and CK.LEASE_RE.match(name).group(1)==cid]
    if relevant:
        name,labels=max(relevant,key=lambda pair:int(CK.LEASE_RE.match(pair[0]).group(2)))
        last=int(CK.LEASE_RE.match(name).group(2))
        generation=max(generation,last+1)
        done=done_reader(f"LD_{cid}_{last:04d}")
        released=done is not None and bool(labels.get("owner")) and done[0].get("owner")==labels["owner"]
        until=labels.get("until")
        expired=until and until.isdigit() and datetime.fromtimestamp(int(until),timezone.utc)+CK.VISIBILITY_GRACE<=now
        failed=bool(failed_reader and failed_reader(last,labels))
        if not released and not expired and not failed:
            raise B.EvidenceError("pilot lease held; reconcile terminal execution before continuation")
    if generation>9999:
        raise B.EvidenceError("pilot lease generation namespace exhausted; explicit migration required")
    return generation


def start(doc, ack_hash, *, backend=None, on_prepared=None, on_receipt=None):
    now = datetime.now(timezone.utc); c=validate_plan(doc, ack_hash)
    observation=doc['runtime_plan'].get('observation_date')
    if observation and observation!=str(now.astimezone(B.MSK).date()):
        raise B.EvidenceError('snapshot observation day stale; freeze a fresh dated plan')
    tables, ledger = preflight(c,doc,now,backend=backend) if backend is not None else preflight(c,doc,now)
    request = backend.append_request if backend is not None else TT._req
    p=doc["runtime_plan"]; cid=B.digest(["BOUNDED_PILOT_EXCLUSIVE",c["project_id"]])[:16]
    leases=[(n,lb) for n,lb,created in tables.list_tables(c["datasets"]["tenant_locks"])]
    generation=pilot_lease_generation(ledger,leases,cid,now,
                lambda name:tables.get_table(c["datasets"]["tenant_locks"],name),
                failed_reader=(lambda generation,labels:any(p['lease_generation']==generation and p['run_id']==labels.get('owner') for p in getattr(backend,'verified_pre_source_failures',[]))) if backend is not None else None)
    run_id=f"bf-{uuid.uuid4()}"
    lease=CK.lease_name(cid,generation)
    body={"tableReference":{"projectId":c["project_id"],"datasetId":c["datasets"]["tenant_locks"],"tableId":lease},
          "schema":{"fields":[{"name":"marker","type":"STRING"}]},
          "labels":{"until":str(int((now+CK.LEASE_TTL).timestamp())),"owner":run_id},
          "description":json.dumps({"mode":"BOUNDED_PILOT","ack_hash":ack_hash}),
          "expirationTime":str(int((now+CK.LEASE_TTL+CK.LEASE_TABLE_KEEP).timestamp()*1000))}
    request("POST",f"{TT.BQ}/projects/{c['project_id']}/datasets/{c['datasets']['tenant_locks']}/tables",body)
    tables.append(c["datasets"]["tenant_ops"],"BACKFILL_CHECKPOINTS",[checkpoint(doc,run_id,"RUNNING",generation,now)])
    base,jobs=resources(c)
    name=next(n for n,cfg in jobs.items() if p["entity"] in cfg["entities"])
    overrides={"ENTITIES":p["entity"],"SINCE":p["from"],"UNTIL":p["to"],"INGESTION_RUN_ID":run_id,
               "BACKFILL_MODE":B.VERSION,"BACKFILL_TARGET_PROJECT":c["project_id"],
               "BACKFILL_GENERATION":p["generation"],"BACKFILL_ORIGIN":p["origin"],
               "BACKFILL_MAX_REQUESTS":str(doc["max_requests"]),"BACKFILL_MAX_UNITS":str(doc["max_units"])}
    if p["window_days"] != 1:
        overrides["BACKFILL_WINDOW_DAYS"] = str(p["window_days"])
    overrides.update(continuation_overrides(doc))
    body={"overrides":{"containerOverrides":[{"env":[{"name":k,"value":v} for k,v in overrides.items()]}]}}
    prepared={"run_id":run_id,"lease_generation":generation,"ack_hash":ack_hash,
              "job":base+"/jobs/"+name,"overrides":body}
    # Durable intent publication is synchronous and happens before the only POST.
    # A publication/response/receipt failure never retries that POST in this call.
    if on_prepared is not None:
        on_prepared(prepared)
    out=backend.dispatch(doc,name,body) if backend is not None else TT._req("POST",f"{RUN_API}/{base}/jobs/{name}:run",body)
    operation=out.get("name", "")
    if not operation.startswith(base+"/operations/"):
        raise B.EvidenceError("dispatch operation receipt missing/foreign; never repeat POST")
    receipt={"operation":operation,"run_id":run_id,"lease_generation":generation,"ack_hash":ack_hash}
    if on_receipt is not None:
        on_receipt(receipt)
    return receipt


def read_proof(c,doc,run_id,request=None):
    reader = (lambda c, sql, params: select(c,sql,params,request=request)) if request is not None else select
    p=doc["runtime_plan"]; table=f"{c['project_id']}.ozon_raw.OZON_INGESTION_RUNS"
    rows=reader(c,f"SELECT DISTINCT status, evidence_json FROM `{table}` WHERE started_at >= @origin AND ingestion_run_id = @run AND entity = @entity LIMIT 2",
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


def reconcile(doc,ack_hash,receipt, *, backend=None):
    request = backend.request if backend is not None else TT._req
    c=validate_plan(doc,ack_hash)
    if receipt.get("ack_hash")!=ack_hash:raise B.EvidenceError("receipt ACK mismatch")
    base,jobs=resources(c)
    if not receipt["operation"].startswith(base+"/operations/"):
        raise B.EvidenceError("foreign operation receipt")
    operation=request("GET",f"{RUN_API}/{receipt['operation']}")
    if not operation.get("done") or operation.get("error"):
        raise B.EvidenceError("execution active or failed; operator review required")
    execution=operation.get("response") or {}
    if not execution.get("completionTime") or execution.get("failedCount",0) or execution.get("cancelledCount",0) or execution.get("succeededCount")!=1 or execution.get("taskCount",1)!=1:
        raise B.EvidenceError("execution not proven successfully terminal")
    p=doc["runtime_plan"]
    job=next(n for n,cfg in jobs.items() if p["entity"] in cfg["entities"])
    template=execution["template"];containers=template["containers"]
    prefix=base+"/jobs/"+job+"/executions/"
    if not execution.get("name","").startswith(prefix) or len(containers)!=1 or containers[0]["image"]!=execution_image(doc,c) or template.get("serviceAccount")!=f"{c['marketplaces']['ozon']['service_accounts']['runtime']}@{c['project_id']}.iam.gserviceaccount.com":
        raise B.EvidenceError("execution job/image/identity provenance mismatch")
    env={v["name"]:v.get("value") for v in containers[0].get("env",[])}
    expected={"INGESTION_RUN_ID":receipt["run_id"],"BACKFILL_TARGET_PROJECT":c["project_id"],
        "ENTITIES":p["entity"],"SINCE":p["from"],"UNTIL":p["to"],"BACKFILL_MODE":B.VERSION,
        "BACKFILL_GENERATION":p["generation"],"BACKFILL_ORIGIN":p["origin"],
        "TENANT_BINDING_REQUIRED":"1","STRICT_PAGE_CAPS":"1",
        "BACKFILL_MAX_REQUESTS":str(doc["max_requests"]),"BACKFILL_MAX_UNITS":str(doc["max_units"])}
    if p["window_days"] != 1:
        expected["BACKFILL_WINDOW_DAYS"] = str(p["window_days"])
    expected.update(continuation_overrides(doc))
    if any(env.get(k)!=v for k,v in expected.items()):
        raise B.EvidenceError("execution provenance mismatch")
    now=datetime.now(timezone.utc);tables,ledger=preflight(c,doc,now,backend=backend) if backend is not None else preflight(c,doc,now)
    cid=B.digest(["BOUNDED_PILOT_EXCLUSIVE",c["project_id"]])[:16]
    lease=tables.get_table(c["datasets"]["tenant_locks"],CK.lease_name(cid,receipt["lease_generation"]))
    if not lease or lease[0].get("owner")!=receipt["run_id"] or parse_tenant_json(lease[1] or "{}").get("ack_hash")!=ack_hash:
        raise B.EvidenceError("terminal execution lease/ACK provenance mismatch")
    proof=read_proof(c,doc,receipt["run_id"],request=request) if backend is not None else read_proof(c,doc,receipt["run_id"])
    # Persist traversal checkpoint. DQ/coverage remain separate; no blanket COMPLETE coverage.
    status="DONE" if proof["state"]["complete"] else "RUNNING"
    tables.append(c["datasets"]["tenant_ops"],"BACKFILL_CHECKPOINTS",
                  [checkpoint(doc,receipt["run_id"],status,receipt["lease_generation"],now,proof)])
    cid=B.digest(["BOUNDED_PILOT_EXCLUSIVE",c["project_id"]])[:16]
    # Terminal completion sign permits only the next CAS generation in this pilot namespace.
    marker=f"LD_{cid}_{receipt['lease_generation']:04d}"
    created=tables.create_marker(c["datasets"]["tenant_locks"],marker,
                         {"owner":receipt["run_id"]},json.dumps({"operation":receipt["operation"],"ack_hash":ack_hash}))
    if not created:
        existing=tables.get_table(c["datasets"]["tenant_locks"],marker)
        if not existing or existing[0].get("owner")!=receipt["run_id"] or parse_tenant_json(existing[1] or "{}").get("ack_hash")!=ack_hash:
            raise B.EvidenceError("pilot release marker belongs to a different lease owner")
    return {"checkpoint":status,"sequence":proof["state"]["sequence"],"rows_observed":proof["state"]["rows"],
            "orders":proof["state"]["orders"],"supplies":proof["state"]["supplies"],"bundles":proof["state"]["bundles"],
            "coverage":"UNPROVEN_PENDING_DQ", "lifecycle_changed":False,"scheduler_changed":False}



def source_detail(detail):
    # A lost insert acknowledgement may repeat a durable sequence after transport
    # retries. Source/window/business proof must agree; attempt telemetry may differ.
    return {k:v for k,v in detail.items() if k not in {"transport_requests","transport_retries"}}


def verify_coverage(doc,ack_hash, *, backend=None):
    """Read-back business grain plus source accounting; then append existing scoped evidence.

    API traversal completion is separate from economic DQ/finality. A partial pilot cannot
    become a full-history boundary or a READY decision.
    """
    c=validate_plan(doc,ack_hash);now=datetime.now(timezone.utc)
    tables,ledger=preflight(c,doc,now,backend=backend) if backend is not None else preflight(c,doc,now)
    reader = (lambda c, sql, params: select(c,sql,params,request=backend.request)) if backend is not None else select
    p=doc["runtime_plan"];journal=f"{c['project_id']}.ozon_raw.OZON_INGESTION_RUNS"
    params={"origin":("TIMESTAMP",p["origin"]),"pid":("STRING",p["plan_id"]),"entity":("STRING",p["entity"])}
    units=reader(c,f"SELECT DISTINCT backfill_sequence, backfill_detail_json FROM `{journal}` WHERE started_at >= @origin AND backfill_plan_id = @pid AND entity = @entity AND status = 'OK' AND backfill_sequence IS NOT NULL ORDER BY backfill_sequence",params)
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
    latest=reader(c,f"SELECT DISTINCT evidence_json FROM `{journal}` WHERE started_at >= @origin AND backfill_plan_id = @pid AND entity = @entity AND backfill_sequence = @seq",dict(params,seq=("INT64",max(details))))
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
                out=reader(c,f"SELECT COUNT(*) AS rows_n, COUNT(DISTINCT TO_JSON_STRING(STRUCT({keys}))) AS keys_n FROM `{c['project_id']}.ozon_raw.{table}` WHERE {predicate}",{"day":("DATE",day)})[0]
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
                    link=reader(c,f"SELECT COUNTIF(r.sku IS NOT NULL) AS sku_rows, COUNTIF(r.sku IS NOT NULL AND NOT EXISTS (SELECT 1 FROM `{c['project_id']}.ozon_raw.RAW_OZON_CATALOG` c WHERE c.snapshot_date = @snapshot AND c.sku = r.sku)) AS unresolved_sku_rows FROM `{c['project_id']}.ozon_raw.{table}` r WHERE {predicate}",
                                {"day":("DATE",day),"snapshot":("DATE",str(now.astimezone(B.MSK).date()))})[0]
                    ambiguous=reader(c,f"SELECT COUNT(*) AS n FROM (SELECT sku FROM `{c['project_id']}.ozon_raw.RAW_OZON_CATALOG` WHERE snapshot_date = @snapshot AND sku IS NOT NULL GROUP BY sku HAVING COUNT(DISTINCT product_id) > 1)", {"snapshot":("DATE",str(now.astimezone(B.MSK).date()))})[0]["n"]
                    if ambiguous:raise B.EvidenceError("PILOT_CATALOG_SKU_JOIN_AMBIGUOUS")
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
            counts=reader(c,f"SELECT COUNT(*) AS rows_n, COUNT(DISTINCT TO_JSON_STRING(STRUCT({keys}))) AS keys_n FROM `{c['project_id']}.ozon_raw.{table}` WHERE CAST({filterkey} AS STRING) IN UNNEST(JSON_VALUE_ARRAY(@ids))",{"ids":("STRING",json.dumps(values))})[0]
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
            table,keys="RAW_OZON_CATALOG","snapshot_date,product_id"
            final=next(d for d in reversed(list(details.values())) if "archived_count" in d)
            expected_count=final["all_count"]+final["archived_count"]
        else:
            table,keys="RAW_OZON_ADS_CAMPAIGNS","snapshot_date,campaign_id"
            expected_count=next(d["campaigns"] for d in details.values() if d.get("action")=="CAMPAIGNS_COMPLETE")
        counts=reader(c,f"SELECT COUNT(*) AS rows_n, COUNT(DISTINCT TO_JSON_STRING(STRUCT({keys}))) AS keys_n FROM `{c['project_id']}.ozon_raw.{table}` WHERE snapshot_date = @day",{"day":("DATE",p["observation_date"])})[0]
        if counts["rows_n"]!=counts["keys_n"] or counts["rows_n"]!=expected_count:
            raise B.EvidenceError("PILOT_SNAPSHOT_KEY_ACCOUNTING_FAILED")
        if p["entity"]=="catalog":
            identity=reader(c,f"SELECT COUNTIF(product_id IS NULL OR SAFE_CAST(product_id AS INT64) IS NULL OR SAFE_CAST(product_id AS INT64) <= 0) AS invalid_products,COUNTIF(sku IS NOT NULL AND (SAFE_CAST(sku AS INT64) IS NULL OR SAFE_CAST(sku AS INT64) <= 0)) AS invalid_skus,COUNTIF(sku IS NULL) AS sku_absent,COUNT(DISTINCT sku) AS valid_skus,COUNTIF(sku IS NOT NULL) AS sku_rows FROM `{c['project_id']}.ozon_raw.{table}` WHERE snapshot_date = @day", {"day":("DATE",p["observation_date"])})[0]
            if identity["invalid_products"] or identity["invalid_skus"] or identity["valid_skus"]!=identity["sku_rows"]:
                raise B.EvidenceError("PILOT_CATALOG_IDENTITY_AMBIGUOUS")
            readback.append({"product_identity":identity})
        readback.append({"table":table,"rows":counts["rows_n"],"keys":counts["keys_n"],"historical_status":"UNPROVEN"})
        coverage.append({"entity":p["entity"],"coverage_date":p["observation_date"],"status":"COMPLETE",
            "reason":"CURRENT_SNAPSHOT_ONLY_NOT_HISTORICAL_STATUS", "rows_loaded":counts["rows_n"],
            "source_run_id":p["plan_id"],"evaluated_at":now.isoformat()})
    if coverage:tables.append(c["datasets"]["tenant_ops"],"DATA_COVERAGE",coverage)
    if p["entity"]=="finance_accrual":
        import dq as DQ
        unresolved=reader(c,f"SELECT COUNTIF(operation_name IS NULL OR operation_name = 'UNKNOWN') AS n FROM `{c['project_id']}.ozon_raw.RAW_OZON_FINANCE_ACCRUAL` WHERE event_date BETWEEN @frm AND @to",
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
    p.add_argument("--max-units",type=int,default=20)
    for cmd in ("start","reconcile","verify"):
        p=sub.add_parser(cmd);p.add_argument("--plan",type=Path,required=True);p.add_argument("--ack-hash",required=True)
        if cmd=="reconcile":p.add_argument("--receipt",type=Path,required=True)
    args=parser.parse_args(argv)
    try:
        if args.command=="plan":out=make_plan(args.tenant,args.entity,args.since,args.until,args.generation,args.origin,max_units=args.max_units)
        else:
            doc=parse_tenant_json(args.plan.read_text())
            out=start(doc,args.ack_hash) if args.command=="start" else (verify_coverage(doc,args.ack_hash) if args.command=="verify" else reconcile(doc,args.ack_hash,parse_tenant_json(args.receipt.read_text())))
        print(json.dumps(out,sort_keys=True))
        return 0
    except (B.EvidenceError,TT.TableError,KeyError,ValueError) as error:
        # API helpers never carry secret response bodies; contract failures reveal no payload.
        print("BACKFILL_BLOCKED: "+str(error),file=sys.stderr);return 2


if __name__=="__main__":sys.exit(main())
