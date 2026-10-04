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
