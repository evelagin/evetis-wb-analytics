"""Owner pilot coordinator contracts: all cloud functions mocked; no credentials."""
from __future__ import annotations

import copy
import json
from datetime import date, datetime, timezone

import pytest

from tools.tenancy import tenant_backfill as T, registry as R


def doc():
    return T.make_plan("client_001","fbo_postings","2026-09-17","2026-09-17","offline-pilot",
                       "2026-10-04T00:00:00Z",date(2026,10,4))


def test_plan_is_pure_and_scoped_to_validated_registry(monkeypatch):
    monkeypatch.setattr(T.TT,"_req",lambda *a:pytest.fail("offline plan touched cloud"))
    p=doc()
    assert p["runtime_plan"]["project"]=="mpa-t-client-001"
    assert p["mode"]=="BOUNDED_PILOT" and p["max_order_batches"]==1
    assert "None" not in json.dumps(p)


@pytest.mark.parametrize("mutate",[
    lambda p:p.update(image="mutable:latest"),
    lambda p:p["runtime_plan"].update(project="other-project"),
    lambda p:p["runtime_plan"].update(to="2026-10-03"),
    lambda p:p.update(max_units=1000),lambda p:p.update(mode="FULL_HISTORY")])
def test_changed_reviewed_scope_is_rejected_before_cloud(monkeypatch,mutate):
    p=doc();h=p["ack_hash"];mutate(p)
    monkeypatch.setattr(T.TT,"_req",lambda *a:pytest.fail("tampered plan touched cloud"))
    with pytest.raises(T.B.EvidenceError):T.validate_plan(p,h)


def test_full_history_not_implicitly_authorized():
    with pytest.raises(T.B.EvidenceError,match="31days"):
        T.make_plan("client_001","finance_accrual","2023-03-12","2026-09-30","full","2026-10-04T00:00:00Z",date(2026,10,4))


def test_checkpoint_uses_exact_existing_schema_and_disjoint_full_plan_namespace():
    p=doc();row=T.checkpoint(p,"synthetic", "RUNNING",1,datetime(2026,10,4,tzinfo=timezone.utc))
    schema=json.loads((T.REPO/"tools/tenancy/schema/tenant_ops/BACKFILL_CHECKPOINTS.json").read_text())
    assert set(row)=={x["name"] for x in schema["schema"]}
    assert row["plan_hash"]!=T.CK.plan_hash([T.CK.Chunk(p["runtime_plan"]["entity"],date(2026,9,17),date(2026,9,17))])
    assert T.CK.plan_versions([dict(row,status="PENDING")])=={}


def metadata(c,p):
    base,expected=T.resources(c)
    jobs=[]
    for name,cfg in expected.items():
        jobs.append({"name":base+"/jobs/"+name,"template":{"template":{
          "timeout":"3600s","maxRetries":0,
          "serviceAccount":f"{c['marketplaces']['ozon']['service_accounts']['runtime']}@{c['project_id']}.iam.gserviceaccount.com",
          "containers":[{"image":p["image"],"env":[{"name":k,"value":v} for k,v in cfg["env"].items()]}]}}})
    jobs.append({"name":base+"/jobs/tenant-control","template":{"template":{
        "timeout":"3600s","maxRetries":0,"serviceAccount":c["control"]["email"],"containers":[{"image":p["image"],"command":["python","lifecycle.py"],"args":["status"],
        "env":[{"name":k,"value":v} for k,v in c["control"]["job"]["env"].items()]}]}}})
    sched=[{"name":base+"/jobs/"+cfg["scheduler"],"state":"PAUSED"} for cfg in expected.values()]
    return jobs,sched


@pytest.mark.parametrize("fault",["active","wrong-image","no-binding","foreign-sa","scheduler-enabled","extra-job","continuation-token","extra-env","entrypoint","parallel","timeout","retry"])
def test_metadata_mutation_or_security_drift_stops_before_any_write(monkeypatch,fault):
    p=doc();c=R.terraform_inputs("client_001");jobs,sched=metadata(c,p)
    if fault=="wrong-image":jobs[0]["template"]["template"]["containers"][0]["image"]="bad"
    if fault=="no-binding":jobs[0]["template"]["template"]["containers"][0]["env"]=[]
    if fault=="foreign-sa":jobs[0]["template"]["template"]["serviceAccount"]="other@foreign"
    if fault=="scheduler-enabled":sched[0]["state"]="ENABLED"
    if fault=="extra-job":jobs.append({"name":jobs[0]["name"].rsplit("/",1)[0]+"/orphan"})
    if fault=="extra-env":jobs[0]["template"]["template"]["containers"][0]["env"].append({"name":"BACKFILL_WINDOW_DAYS","value":"30"})
    if fault=="entrypoint":jobs[0]["template"]["template"]["containers"][0]["command"]=["python","other.py"]
    if fault=="parallel":jobs[0]["template"]["taskCount"]=2
    if fault=="timeout":jobs[0]["template"]["template"]["timeout"]="7200s"
    if fault=="retry":jobs[0]["template"]["template"]["maxRetries"]=1
    monkeypatch.setattr(T.TL,"read_state",lambda *a:([],{},[],[],False))
    import lifecycle_core as L
    monkeypatch.setattr(L,"current_state",lambda _:L.VALIDATING)
    monkeypatch.setattr(T.TL,"operator_binding",lambda *a:({"seller":"BOUND"},{"seller":{"status":"PASS"}}))
    def read(method,url,body=None):
        assert method=="GET", "drift caused a write"
        if "cloudscheduler" in url:return {"jobs":sched}
        if "/executions?" in url:return {"executions":[{}]} if fault=="active" else {"executions":[]}
        return {"jobs":jobs,**({"nextPageToken":"x"} if fault=="continuation-token" else {})}
    monkeypatch.setattr(T.TT,"_req",read)
    with pytest.raises(T.B.EvidenceError):T.preflight(c,p,datetime(2026,10,4,tzinfo=timezone.utc))


@pytest.mark.parametrize("lifecycle",["BACKFILLING","READY","SUSPENDED","RECONCILING"])
def test_pilot_cannot_bypass_full_plan_or_ready_state(monkeypatch,lifecycle):
    p=doc();c=R.terraform_inputs("client_001")
    monkeypatch.setattr(T.TL,"read_state",lambda *a:([],{},[],[],False))
    import lifecycle_core as L
    monkeypatch.setattr(L,"current_state",lambda _:lifecycle)
    monkeypatch.setattr(T.TT,"_req",lambda *a:pytest.fail("denied lifecycle touched cloud"))
    with pytest.raises(T.B.EvidenceError):T.preflight(c,p,datetime(2026,10,4,tzinfo=timezone.utc))


def test_sql_dml_never_enters_query_api(monkeypatch):
    monkeypatch.setattr(T.TT,"_req",lambda *a:pytest.fail("DML touched API"))
    for sql in ["MERGE t USING s", "UPDATE t SET x=1", "SELECT 1; DELETE t", "CALL dangerous()"]:
        with pytest.raises(T.B.EvidenceError):T.select({"project_id":"offline"},sql,{})


def test_failed_or_active_operation_never_updates_checkpoint(monkeypatch):
    p=doc();c=R.terraform_inputs("client_001");base,_=T.resources(c)
    monkeypatch.setattr(T,"validate_plan",lambda *a:c)
    monkeypatch.setattr(T.TT,"_req",lambda method,url,body=None:{"done":False})
    with pytest.raises(T.B.EvidenceError,match="active or failed"):
        T.reconcile(p,p["ack_hash"],{"ack_hash":p["ack_hash"],"operation":base+"/operations/synthetic"})


def test_foreign_operation_rejected_before_api(monkeypatch):
    p=doc();c=R.terraform_inputs("client_001")
    monkeypatch.setattr(T,"validate_plan",lambda *a:c)
    monkeypatch.setattr(T.TT,"_req",lambda *a:pytest.fail("foreign operation read"))
    with pytest.raises(T.B.EvidenceError,match="foreign operation"):
        T.reconcile(p,p["ack_hash"],{"ack_hash":p["ack_hash"],"operation":"projects/foreign/locations/europe-west1/operations/x"})


def test_current_pre_engine_image_cannot_start_even_with_valid_owner_hash(monkeypatch):
    p=doc()
    monkeypatch.setattr(T.TT,"_req",lambda *a:pytest.fail("unqualified engine image touched cloud"))
    # Until the newly qualified release is registered, current v5 cannot consume WINDOW_V1.
    try:
        T.validate_plan(p,p["ack_hash"])
    except T.B.EvidenceError as error:
        assert "qualified WINDOW_V1" in str(error)



def test_lost_journal_ack_does_not_confuse_retry_telemetry_with_source_conflict():
    detail={"action":"DAY_COMPLETE","day":"2026-09-17","expected_unique_rows":5,
            "source_terminal":True,"transport_requests":1,"transport_retries":0}
    retry=dict(detail,transport_requests=3,transport_retries=2)
    assert T.source_detail(detail)==T.source_detail(retry)
    assert T.source_detail(detail)!=T.source_detail(dict(retry,expected_unique_rows=4))



def test_pilot_lease_generation_survives_expired_tables_and_ignores_old_release_marker():
    now=datetime(2026,10,4,tzinfo=timezone.utc);cid="0"*16
    ledger=[{"lease_generation":22,"evidence_json":json.dumps({"mode":"BOUNDED_PILOT"})}]
    assert T.pilot_lease_generation(ledger,[],cid,now,lambda name:({"owner":"old"},"{}"))==23
    labels={"until":str(int(now.timestamp()+3600)),"owner":"new-owner"}
    with pytest.raises(T.B.EvidenceError,match="held"):
        T.pilot_lease_generation(ledger,[(T.CK.lease_name(cid,23),labels)],cid,now,
                                lambda name:({"owner":"old-owner"},"{}"))
    assert T.pilot_lease_generation(ledger,[(T.CK.lease_name(cid,23),labels)],cid,now,
                                  lambda name:({"owner":"new-owner"},"{}"))==24


def test_pilot_namespace_exhaustion_never_reuses_a_generation():
    ledger=[{"lease_generation":9999,"evidence_json":json.dumps({"mode":"BOUNDED_PILOT"})}]
    with pytest.raises(T.B.EvidenceError,match="namespace exhausted"):
        T.pilot_lease_generation(ledger,[],"0"*16,datetime(2026,10,4,tzinfo=timezone.utc),lambda _:None)


@pytest.mark.parametrize("project", ["mpa-t-synthetic-a", "mpa-t-synthetic-b"])
def test_terminal_zero_effect_failure_expires_to_independent_retry_preserving_predecessor(monkeypatch, project):
    """No failed reconcile/false DONE; reclaim uses the existing grace and CAS generation."""
    from datetime import timedelta
    p = doc()
    c = copy.deepcopy(R.terraform_inputs("client_001"))
    c["project_id"] = project
    p["runtime_plan"]["project"] = project
    now = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)
    old_run = "bf-synthetic-failed"
    cid = T.B.digest(["BOUNDED_PILOT_EXCLUSIVE", project])[:16]
    until = now - T.CK.VISIBILITY_GRACE
    labels = {"owner": old_run, "until": str(int(until.timestamp()))}
    old_checkpoint = T.checkpoint(p, old_run, "RUNNING", 1, now - timedelta(hours=5))
    old_journal = {"ingestion_run_id": old_run, "status": "FAILED", "rows_inserted": 0,
                   "rows_updated": 0, "evidence_json": None}
    ledger = [copy.deepcopy(old_checkpoint)]
    leases = {T.CK.lease_name(cid, 1): (labels, json.dumps({"ack_hash": p["ack_hash"]}))}
    before = copy.deepcopy((ledger, old_journal, leases))
    monkeypatch.setattr(T, "validate_plan", lambda *a: c)
    base, jobs = T.resources(c)
    failed_receipt = {"operation": base + "/operations/failed", "ack_hash": p["ack_hash"],
                      "run_id": old_run, "lease_generation": 1}
    monkeypatch.setattr(T.TT, "_req", lambda *a: {"done": True, "error": {"code": 10}})
    with pytest.raises(T.B.EvidenceError, match="active or failed"):
        T.reconcile(p, p["ack_hash"], failed_receipt)
    assert (ledger, old_journal, leases) == before
    with pytest.raises(T.B.EvidenceError, match="held"):
        T.pilot_lease_generation(ledger, list((n, v[0]) for n, v in leases.items()), cid,
                                now - timedelta(microseconds=1), lambda n: leases.get(n))
    assert T.pilot_lease_generation(ledger, list((n, v[0]) for n, v in leases.items()), cid,
                                    now, lambda n: leases.get(n)) == 2
    # The new source/image attempt has its own immutable plan/receipt, not a reused failure.
    retry = copy.deepcopy(p)
    retry["runtime_plan"]["generation"] = "synthetic-independent-retry"
    retry["runtime_plan"]["plan_id"] = T.B.digest([project, "new-qualified-source"])
    retry["image"] = "synthetic-fixed-image@sha256:" + "1" * 64
    retry["ack_hash"] = T.B.digest({k:v for k,v in retry.items() if k != "ack_hash"})
    class Clock(datetime):
        @staticmethod
        def now(tz): return now
    monkeypatch.setattr(T, "datetime", Clock)
    writes = []
    class Tables:
        def list_tables(self, dataset): return [(n, v[0], None) for n,v in leases.items()]
        def get_table(self, dataset, name): return leases.get(name)
        def append(self, dataset, table, rows):
            assert table == "BACKFILL_CHECKPOINTS"
            writes.extend(copy.deepcopy(rows)); ledger.extend(copy.deepcopy(rows))
        def create_marker(self, dataset, name, labels, description):
            assert name.endswith("_0002")
            leases[name] = (labels, description); return True
    tables = Tables()
    monkeypatch.setattr(T, "preflight", lambda *a: (tables, ledger))
    def start_api(method, url, body):
        assert method == "POST"
        if url.endswith("/tables"):
            name = body["tableReference"]["tableId"]
            assert name == T.CK.lease_name(cid, 2) and name not in leases
            leases[name] = (body["labels"], body["description"])
            return body
        assert url.endswith(":run")
        return {"name": base + "/operations/retry"}
    monkeypatch.setattr(T.TT, "_req", start_api)
    receipt = T.start(retry, retry["ack_hash"])
    assert receipt["run_id"] != old_run and receipt["lease_generation"] == 2
    assert writes[-1]["status"] == "RUNNING"
    entity = retry["runtime_plan"]["entity"]
    job = next(n for n, cfg in jobs.items() if entity in cfg["entities"])
    env = dict(jobs[job]["env"], INGESTION_RUN_ID=receipt["run_id"], ENTITIES=entity,
               BACKFILL_TARGET_PROJECT=project, BACKFILL_MODE=T.B.VERSION,
               SINCE=retry["runtime_plan"]["from"], UNTIL=retry["runtime_plan"]["to"],
               BACKFILL_GENERATION=retry["runtime_plan"]["generation"],
               BACKFILL_ORIGIN=retry["runtime_plan"]["origin"],
               BACKFILL_MAX_REQUESTS=str(retry["max_requests"]), BACKFILL_MAX_UNITS=str(retry["max_units"]))
    execution = {"name":base + "/jobs/" + job + "/executions/retry", "completionTime":now.isoformat(),
                 "succeededCount":1, "template":{"serviceAccount":
                 c['marketplaces']['ozon']['service_accounts']['runtime'] + "@" + project + ".iam.gserviceaccount.com",
                 "containers":[{"image":retry["image"], "env":[{"name":k,"value":v} for k,v in env.items()]}]}}
    monkeypatch.setattr(T.TT, "_req", lambda *a:{"done":True, "response":execution})
    state = T.B.initial(retry["runtime_plan"])
    state["complete"] = True
    monkeypatch.setattr(T, "read_proof", lambda *a:{"plan":retry["runtime_plan"], "state":state})
    assert T.reconcile(retry, retry["ack_hash"], receipt)["checkpoint"] == "DONE"
    assert ledger[0] == old_checkpoint and old_journal == before[1]
    assert leases[T.CK.lease_name(cid, 1)] == before[2][T.CK.lease_name(cid, 1)]
    assert all(r["run_id"] != old_run for r in writes)
    assert T.CK.fold(ledger)[old_checkpoint["backfill_id"]]["status"] == "RUNNING"


@pytest.mark.parametrize("budget", [0, 21, 1000, True, "1", None])
def test_bounded_unit_budget_cannot_expand_or_change_type(budget):
    with pytest.raises(T.B.EvidenceError, match="unit budget"):
        T.make_plan("client_001", "catalog", "2026-09-17", "2026-09-17", "offline",
                    "2026-10-04T00:00:00Z", date(2026,10,4), max_units=budget)

def test_smaller_unit_budget_is_frozen_in_reviewed_plan(monkeypatch):
    p = T.make_plan("client_001", "catalog", "2026-09-17", "2026-09-17", "offline",
                    "2026-10-04T00:00:00Z", date(2026,10,4), max_units=1)
    assert p["max_units"] == 1 and p["ack_hash"] == T.B.digest({k:v for k,v in p.items() if k != "ack_hash"})
    p["max_units"] = 20
    monkeypatch.setattr(T.TT, "_req", lambda *a: pytest.fail("modified budget touched cloud"))
    with pytest.raises(T.B.EvidenceError, match="modified"):
        T.validate_plan(p,p["ack_hash"])


@pytest.mark.parametrize("window", [0, 31, True, "14", None])
def test_multiday_fbo_window_is_bounded_and_typed(window):
    with pytest.raises(T.B.EvidenceError, match="FBO window"):
        T.make_plan("client_001", "fbo_postings", "2026-09-17", "2026-09-30",
                    "split-live", "2026-10-04T00:00:00Z", window_days=window)


def test_multiday_window_is_fbo_only_and_keeps_strict_cap(monkeypatch):
    monkeypatch.setattr(T.TT, "_req", lambda *a: pytest.fail("offline plan touched cloud"))
    p = T.make_plan("client_001", "fbo_postings", "2026-09-17", "2026-09-30",
                    "split-live", "2026-10-04T00:00:00Z", window_days=14)
    assert p["runtime_plan"]["window_days"] == 14
    assert p["runtime_plan"]["fbo_page_cap"] == 200
    assert p["max_requests"] == 400
    T.validate_plan(p, p["ack_hash"])
    p["runtime_plan"]["window_days"] = 30
    with pytest.raises(T.B.EvidenceError, match="modified"):
        T.validate_plan(p, p["ack_hash"])
    with pytest.raises(T.B.EvidenceError, match="only for FBO"):
        T.make_plan("client_001", "finance_accrual", "2026-09-17", "2026-09-30",
                    "split-live", "2026-10-04T00:00:00Z", window_days=14)


def test_default_plan_and_existing_supplies_continuation_identity_unchanged():
    small = T.make_plan("client_001", "supplies", "2026-09-17", "2026-09-17",
                        "retained", "2026-10-04T00:00:00Z", max_units=1)
    drain = T.make_plan("client_001", "supplies", "2026-09-17", "2026-09-17",
                        "retained", "2026-10-04T00:00:00Z", max_units=20)
    assert small["runtime_plan"] == drain["runtime_plan"]
    assert small["ack_hash"] != drain["ack_hash"]


def test_multiday_override_is_sent_and_wrong_readback_rejected(monkeypatch):
    p = T.make_plan("client_001", "fbo_postings", "2026-09-17", "2026-09-30",
                    "split-live", "2026-10-04T00:00:00Z", window_days=14)
    c = T.target("client_001")
    writes = []
    class Tables:
        def list_tables(self, _): return []
        def append(self, *args): writes.append(args)
    monkeypatch.setattr(T, "preflight", lambda *a: (Tables(), []))
    requests = []
    def api(method, url, body):
        requests.append((method, url, body))
        return {"name": T.resources(c)[0] + "/operations/synthetic"}
    monkeypatch.setattr(T.TT, "_req", api)
    receipt = T.start(p, p["ack_hash"])
    overrides = {e["name"]: e["value"] for e in requests[-1][2]["overrides"]["containerOverrides"][0]["env"]}
    assert overrides["BACKFILL_WINDOW_DAYS"] == "14"
    base, jobs = T.resources(c)
    name = next(n for n, cfg in jobs.items() if "fbo_postings" in cfg["entities"])
    env = dict(jobs[name]["env"], **overrides)
    env["BACKFILL_WINDOW_DAYS"] = "1"
    execution = {"name": base + "/jobs/" + name + "/executions/synthetic",
        "completionTime": "2026-10-05T00:00:00Z", "succeededCount": 1,
        "template": {"serviceAccount": c["marketplaces"]["ozon"]["service_accounts"]["runtime"] + "@" + c["project_id"] + ".iam.gserviceaccount.com",
                     "containers": [{"image": p["image"], "env": [{"name":k,"value":v} for k,v in env.items()]}]}}
    monkeypatch.setattr(T.TT, "_req", lambda *a: {"done": True, "response": execution})
    monkeypatch.setattr(T, "preflight", lambda *a: pytest.fail("wrong override reached checkpoint mutation"))
    before = copy.deepcopy(writes)
    with pytest.raises(T.B.EvidenceError, match="provenance mismatch"):
        T.reconcile(p, p["ack_hash"], receipt)
    assert writes == before
