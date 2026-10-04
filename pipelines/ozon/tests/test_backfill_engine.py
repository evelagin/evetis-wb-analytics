"""Adversarial, network-free WINDOW_V1 contracts; source fixtures contain no real identities."""
from __future__ import annotations

import copy
import json
from datetime import date

import pytest

import backfill_core as B
import backfill as F
import common as C


def env(entity="fbo_postings", **kw):
    return dict(BACKFILL_MODE=B.VERSION, TENANT_BINDING_REQUIRED="1", STRICT_PAGE_CAPS="1",
                BACKFILL_TARGET_PROJECT="mpa-t-example", SINCE="2026-09-17", UNTIL="2026-09-17",
                BACKFILL_GENERATION="pilot-v1", BACKFILL_ORIGIN="2026-10-04T00:00:00Z", **kw)


def plan(entity="fbo_postings", values=None):
    return B.plan(values or env(), entity, "mpa-t-example", "ozon_raw", "ref", date(2026, 10, 4))


@pytest.fixture
def harness(entities, monkeypatch):
    database, writes, proofs = {}, [], []
    monkeypatch.setattr(F.time, "sleep", lambda _: None)
    monkeypatch.setattr(F, "export_budget", lambda: 15)
    def merge(table, rows, keys, run_id, **kw):
        cols = set().union(*(r.keys() for r in rows)) if rows else set()
        C.validate_merge_batch(table, rows, keys, sorted(cols), kw.get("on_duplicate_key", "collapse_identical"))
        accepted = list({C.merge_key(r, keys): r for r in rows}.values())
        target = database.setdefault(table, {})
        inserted = 0
        for row in accepted:
            key = C.merge_key(row, keys)
            inserted += key not in target
            target[key] = copy.deepcopy(row)
        writes.append((table, copy.deepcopy(accepted)))
        return {"received": len(accepted), "inserted": inserted, "updated": len(accepted)-inserted}
    monkeypatch.setattr(C, "merge_rows", merge)
    def denied(*a, **kw):
        raise AssertionError("unmocked transport")
    monkeypatch.setattr(C, "seller_post", denied)
    monkeypatch.setattr(C, "perf_get", denied)
    monkeypatch.setattr(C, "perf_post", denied)
    def persist(result, evidence):
        proofs.append(copy.deepcopy(evidence))
    return database, writes, proofs, persist


def engine(p, state=None, **kw):
    return F.Engine(p, "rt-synthetic-unique", "2026-10-04T10:00:00+03:00", state or B.initial(p), **kw)


def posting(number, instant):
    return {"posting_number": number, "created_at": instant,
            "products": [{"sku": "synthetic-sku", "quantity": 1, "price": "100"}]}


@pytest.mark.parametrize("field,value", [("TENANT_BINDING_REQUIRED","0"), ("STRICT_PAGE_CAPS","0"),
    ("BACKFILL_TARGET_PROJECT","other-project"), ("BACKFILL_GENERATION",""),
    ("BACKFILL_ORIGIN","bad"), ("BACKFILL_FBO_PAGE_CAP","201"),
    ("BACKFILL_MIN_SECONDS","0"), ("BACKFILL_ORDER_BATCH","26"), ("BACKFILL_WINDOW_DAYS","31")])
def test_plan_security_bounds(field,value):
    values=env(); values[field]=value
    with pytest.raises(B.EvidenceError): plan(values=values)


@pytest.mark.parametrize("start,end", [("2026-10-04","2026-10-04"),("2026-09-19","2026-09-17"),
    ("2026-W38-1","2026-09-17"),("2026-09-17","2026-10-05")])
def test_explicit_finished_date_bounds(start,end):
    values=env(); values.update(SINCE=start,UNTIL=end)
    with pytest.raises(B.EvidenceError): plan(values=values)


def test_plan_identity_scope_and_refresh():
    p=plan(); values=env(); values["BACKFILL_GENERATION"]="refresh-v2"
    assert p["plan_id"] == plan()["plan_id"] != plan(values=values)["plan_id"]
    assert B.utc_ms("2026-09-17") == B.utc_ms("2026-09-18")-86400000
    assert B.stamp(B.utc_ms("2026-09-17")) == "2026-09-16T21:00:00.000Z"


@pytest.mark.parametrize("delta", [2,3,60000,86400000])
def test_half_open_split_has_no_gap_or_overlap(delta):
    left,right=B.split([100000,100000+delta],1)
    assert left[0]==100000 and left[1]==right[0] and right[1]==100000+delta


def test_checkpoint_gap_cannot_be_complete():
    p=plan(); s=B.initial(p); s["progress"]["pending"][0][0]+=1
    with pytest.raises(B.EvidenceError,match="gap"): B.validate(p,s)
    s=B.initial(p);s["complete"]=True
    with pytest.raises(B.EvidenceError,match="incomplete"):B.validate(p,s)


def test_fbo_recursive_cap_split_and_leaf_proofs(harness, monkeypatch):
    db,writes,proofs,persist=harness
    values=env(BACKFILL_FBO_PAGE_CAP="1",BACKFILL_MIN_SECONDS="60")
    p=plan(values=values)
    def source(path,body):
        a=datetime_ms(body["filter"]["since"]); b=datetime_ms(body["filter"]["to"])+1
        more=b-a>6*3600000
        return 200, {"postings":[posting(str(a),B.stamp(a))],"has_next":more,"cursor":"next" if more else ""}
    monkeypatch.setattr(C,"seller_post",source)
    out=engine(p,unit_budget=20).run(persist)
    assert out["evidence"]["complete"] and len(db["RAW_OZON_POSTINGS_FBO"])==4
    assert sum(d["detail"]["action"]=="SPLIT_PAGE_CAP" for d in proofs)==3
    assert all(d["detail"]["source_terminal"] for d in proofs if d["detail"]["action"]=="WINDOW_COMPLETE")
    assert len(writes)==4 # split parent never merged


def datetime_ms(value):
    from datetime import datetime
    return int(datetime.fromisoformat(value.replace("Z","+00:00")).timestamp()*1000)


def test_fbo_minimum_failure_never_merges_or_marks_done(harness,monkeypatch):
    db,writes,proofs,persist=harness
    p=plan(values=env(BACKFILL_FBO_PAGE_CAP="1",BACKFILL_MIN_SECONDS="86400"))
    monkeypatch.setattr(C,"seller_post",lambda path,body:(200,{"postings":[posting("x",body["filter"]["since"])],"has_next":True,"cursor":"c"}))
    with pytest.raises(B.EvidenceError,match="MINIMUM"):engine(p).run(persist)
    assert not db and not proofs


@pytest.mark.parametrize("payload", [{"postings":[],"has_next":True,"cursor":""},
    {"postings":[],"has_next":"false","cursor":""},{"postings":[],"cursor":""},
    {"postings":[],"has_next":True,"cursor":"x"}])
def test_fbo_malformed_terminal_fails_closed(harness,monkeypatch,payload):
    monkeypatch.setattr(C,"seller_post",lambda *a:(200,payload))
    with pytest.raises(B.EvidenceError):engine(plan()).run(harness[3])
    assert not harness[0] and not harness[2]


def test_fbo_repeated_cursor_no_partial_merge(harness,monkeypatch):
    monkeypatch.setattr(C,"seller_post",lambda path,body:(200,{"postings":[posting("x",body["filter"]["since"])],"has_next":True,"cursor":"same"}))
    with pytest.raises(B.EvidenceError,match="repeated cursor"):engine(plan()).run(harness[3])
    assert not harness[0]


def test_fbo_interruption_resume_and_completed_replay(harness,monkeypatch):
    p=plan();db,writes,proofs,persist=harness
    monkeypatch.setattr(C,"seller_post",lambda path,body:(200,{"postings":[posting("x",body["filter"]["since"])],"has_next":False,"cursor":""}))
    def lost_ack(*a):raise RuntimeError("journal unavailable")
    with pytest.raises(RuntimeError,match="journal"):engine(p).run(lost_ack)
    out=engine(p).run(persist)
    assert out["inserted"]==0 and len(db["RAW_OZON_POSTINGS_FBO"])==1
    replay=engine(p,out["evidence"]["state"]).run(persist)
    assert replay["evidence"]["replay"] and replay["evidence"]["execution_requests"]==0


def test_fbo_duplicate_key_conflict_visible_before_merge(harness,monkeypatch):
    def source(path,body):
        a=posting("x",body["filter"]["since"]);b=copy.deepcopy(a);b["products"][0]["quantity"]=2
        return 200,{"postings":[a,b],"has_next":False,"cursor":""}
    monkeypatch.setattr(C,"seller_post",source)
    with pytest.raises(C.MergeKeyConflictError):engine(plan()).run(harness[3])
    assert not harness[0]


def finance_fixture(type_id=84,amount="12",cursor=""):
    return {"last_id":cursor,"accruals":[{"accrual_id":"synthetic-a","accrued_category":"SERVICE",
       "non_item_fee":{"type_id":type_id,"accrued":{"amount":amount}}}]}


@pytest.mark.parametrize("tid",[84,999999])
def test_unknown_finance_is_preserved_and_exposed(harness,monkeypatch,tid):
    monkeypatch.setattr(C,"seller_post",lambda path,body:(200,{"accrual_types":[]} if path.endswith("/types") else finance_fixture(tid)))
    out=engine(plan("finance_accrual")).run(harness[3])
    row=next(iter(harness[0]["RAW_OZON_FINANCE_ACCRUAL"].values()))
    assert row["type_id"]==tid and row["operation_name"]=="UNKNOWN"
    assert harness[2][-1]["detail"]["unknown_type_ids"]==[tid]
    assert out["evidence"]["complete"]


def test_finance_resume_then_explicit_refresh_updates_late_value(harness,monkeypatch):
    values=env();values["UNTIL"]="2026-09-18";p=plan("finance_accrual",values)
    amount=["12"]
    def source(path,body):
        if path.endswith("/types"):return 200,{"accrual_types":[{"id":84,"description":"SOURCE_LABEL"}]}
        x=finance_fixture(amount=amount[0]);x["accruals"][0]["accrual_id"]=body["date"]
        return 200,x
    monkeypatch.setattr(C,"seller_post",source)
    first=engine(p,unit_budget=1).run(harness[3]);assert not first["evidence"]["complete"]
    second=engine(p,first["evidence"]["state"]).run(harness[3]);assert second["evidence"]["complete"]
    amount[0]="14";values["BACKFILL_GENERATION"]="refresh-v2"
    refreshed=engine(plan("finance_accrual",values)).run(harness[3])
    assert refreshed["inserted"]==0 and refreshed["updated"]==2
    assert {r["amount_rub"] for r in harness[0]["RAW_OZON_FINANCE_ACCRUAL"].values()}=={14.0}
    assert harness[2][-1]["detail"]["economic_finality"]=="UNPROVEN"


def test_finance_repeated_cursor_fails_before_merge(harness,monkeypatch):
    monkeypatch.setattr(C,"seller_post",lambda path,body:(200,{"accrual_types":[]} if path.endswith("/types") else finance_fixture(cursor="same")))
    with pytest.raises(B.EvidenceError,match="repeated cursor"):engine(plan("finance_accrual")).run(harness[3])
    assert not harness[0]


def test_finance_duplicate_real_accrual_never_collapsed(harness,monkeypatch):
    def source(path,body):
        if path.endswith("/types"):return 200,{"accrual_types":[]}
        d=finance_fixture();d["accruals"]*=2;return 200,d
    monkeypatch.setattr(C,"seller_post",source)
    with pytest.raises(C.MergeKeyConflictError):engine(plan("finance_accrual")).run(harness[3])


def test_catalog_all_and_archived_preserve_prior_snapshot(harness,monkeypatch):
    requests=[]
    def source(path,body):
        requests.append((path,body))
        if path.endswith("/list") and path=="/v3/product/list":
            x=1 if body["filter"]["visibility"]=="ALL" else 2
            return 200,{"result":{"total_items":1,"items":[{"product_id":x}],"last_id":"last"}}
        x=body["product_id"][0]
        return 200,{"items":[{"id":x,"sku":str(x),"offer_id":str(x),"is_archived":x==2}]}
    monkeypatch.setattr(C,"seller_post",source)
    out=engine(plan("catalog")).run(harness[3])
    assert out["evidence"]["complete"]
    assert [b["filter"]["visibility"] for path,b in requests if path=="/v3/product/list"]==["ALL","ARCHIVED"]
    rows=list(harness[0]["RAW_OZON_CATALOG"].values());assert len(rows)==2 and any(r["is_archived"] for r in rows)
    assert all(r["snapshot_date"]!= "2026-09-17" for r in rows) # no fabricated historical snapshot


def supply_source(total, *, bundle_items=1, corrupt=False):
    def source(path,body):
        if path.endswith("/list"):
            pos=int(body["last_id"] or 0);n=body["limit"];ids=list(range(pos,min(pos+n,total)))
            return 200,{"order_ids":ids,"last_id":str(pos+n) if pos+n<total else ""}
        if path.endswith("/get"):
            return 200,{"orders":[{"order_id":i,"supplies":[{"supply_id":i+10000,"bundle_id":i+20000}]} for i in body["order_ids"]]}
        pos=int(body["last_id"] or 0);end=min(pos+100,bundle_items)
        return 200,{"items":[{"sku":str(i+1),"quantity":1} for i in range(pos,end)],
                    "total_count":bundle_items+(1 if corrupt else 0),"has_next":end<bundle_items,
                    "last_id":str(end) if end<bundle_items else ""}
    return source


def test_thousands_supplies_continue_across_bounded_executions(harness,monkeypatch):
    monkeypatch.setattr(C,"seller_post",supply_source(3009))
    p=plan("supplies",env(BACKFILL_ORDER_BATCH="25"));s=B.initial(p);runs=0
    # Drop accumulated proof copies here: production journal is durable, not memory-only.
    def persist(result,evidence):harness[2][:]=[copy.deepcopy(evidence)]
    while not s["complete"]:
        out=engine(p,s,unit_budget=100,request_budget=100).run(persist)
        assert out["evidence"]["state"]["sequence"]>s["sequence"]
        s=out["evidence"]["state"];runs+=1
        assert runs<200
    assert runs>1 and s["orders"]==s["supplies"]==s["bundles"]==3009
    assert len(harness[0]["RAW_OZON_SUPPLY_BUNDLES"])==3009


def test_bundle_interruption_preserves_page_and_never_claims_complete(harness,monkeypatch):
    monkeypatch.setattr(C,"seller_post",supply_source(1,bundle_items=201))
    p=plan("supplies");out=engine(p,unit_budget=3).run(harness[3])
    assert not out["evidence"]["complete"] and len(harness[0]["RAW_OZON_SUPPLY_BUNDLES"])==100
    final=engine(p,out["evidence"]["state"]).run(harness[3])
    assert final["evidence"]["complete"] and len(harness[0]["RAW_OZON_SUPPLY_BUNDLES"])==201


def test_bundle_contradictory_total_visible_partial_not_complete(harness,monkeypatch):
    monkeypatch.setattr(C,"seller_post",supply_source(1,bundle_items=101,corrupt=True))
    with pytest.raises(B.EvidenceError,match="contradiction"):engine(plan("supplies")).run(harness[3])
    assert len(harness[0]["RAW_OZON_SUPPLY_BUNDLES"])==100
    assert not harness[2][-1]["state"]["complete"]


def test_campaign_real_pagination(harness,monkeypatch):
    requests=[]
    def source(path,**kw):
        requests.append(path);page=int(path.split("page=")[1].split("&")[0])
        return 200,json.dumps({"total":"101","list":[{"id":i} for i in (range(100) if page==1 else [100])]})
    monkeypatch.setattr(C,"perf_get",source)
    out=engine(plan("ads_campaigns")).run(harness[3])
    assert out["evidence"]["complete"] and len(requests)==2
    assert len(harness[0]["RAW_OZON_ADS_CAMPAIGNS"])==101


@pytest.mark.parametrize("payload",[{"list":[]},{"total":"10","list":[]},{"total":"-1","list":[]}])
def test_campaign_unknown_or_incomplete_source_never_passes(harness,monkeypatch,payload):
    monkeypatch.setattr(C,"perf_get",lambda *a,**kw:(200,json.dumps(payload)))
    with pytest.raises(B.EvidenceError):engine(plan("ads_campaigns")).run(harness[3])
    assert not harness[0]


@pytest.mark.parametrize("path",["/api/client/campaign/all_sku_promo/activate",
    "/api/client/campaign?page=1&pageSize=100&state=activate", "/api/client/campaign?page=0&pageSize=100",
    "/api/client/campaign?page=1&pageSize=1000", "/api/client/campaign/1/activate"])
def test_performance_write_and_extra_query_paths_remain_denied(path):
    assert not any(r.fullmatch(path) for r in C.PERF_ALLOWED_GET)


def test_snapshot_historical_mode_not_fabricated(harness):
    with pytest.raises(B.EvidenceError,match="no source backfill"):engine(plan("prices")).run(harness[3])
    assert not harness[0]


def test_sku_report_uuid_continues_without_new_submission(harness,monkeypatch):
    submits=[]
    def get(path,**kw):
        if path.startswith("/api/client/campaign?"):return 200,json.dumps({"total":1,"list":[{"id":1,"advObjectType":"SKU"}]})
        if "/expense?" in path:return 200,"ID;Расход\n1;10\n"
        if "/report?" in path:return 200,"synthetic title\nsku;День;Расход, ₽, с НДС\n77;17.09.2026;10\n"
        return 200,{"state":"OK"}
    monkeypatch.setattr(C,"perf_get",get)
    monkeypatch.setattr(C,"perf_post",lambda path,body:(submits.append(body) or 200,{"UUID":"synthetic-uuid"}))
    p=plan("ads_sku_daily");out=engine(p,unit_budget=3).run(harness[3])
    assert len(submits)==1 and not out["evidence"]["complete"]
    final=engine(p,out["evidence"]["state"]).run(harness[3])
    assert final["evidence"]["complete"] and len(submits)==1
    assert len(harness[0]["RAW_OZON_ADS_SKU_DAILY"])==1


def test_async_submission_crash_blocks_duplicate_post(harness,monkeypatch):
    p=plan("ads_sku_daily");s=B.initial(p)
    s["progress"].update(pending=["1"], report={"phase":"INTENT","batch":["1"],"execution":"another-run"})
    with pytest.raises(B.EvidenceError,match="AMBIGUOUS"):engine(p,s).run(harness[3])


@pytest.mark.parametrize("payload",[{"has_next":False},{"has_next":False,"cursor":"last"}])
def test_authoritative_terminal_does_not_require_next_cursor(payload):
    assert B.source_terminal(payload,"cursor",set(),require_flag=True)[0]


def test_journal_write_rejection_is_fatal(monkeypatch):
    class Client:
        def insert_rows_json(self,*a,**k):return [{"index":0,"errors":[{"reason":"synthetic"}]}]
    monkeypatch.setattr(C,"bq",lambda:Client())
    from datetime import datetime,timezone
    with pytest.raises(RuntimeError,match="journal rejected"):
        C.record_run("synthetic","catalog",datetime.now(timezone.utc),"a","b",{},"OK")


def test_resume_uses_typed_plan_sequence_then_exact_clustered_proof(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(F.bigquery,"ScalarQueryParameter",lambda n,t,v:(n,t,v),raising=False)
    monkeypatch.setattr(F.bigquery,"QueryJobConfig",lambda **kw:SimpleNamespace(**kw),raising=False)
    p=plan();state=B.initial(p);state["sequence"]=1
    state["progress"]["pending"]=[B.split(state["progress"]["pending"][0],60000)[0],B.split(state["progress"]["pending"][0],60000)[1]]
    doc={"plan":p,"state":state,"detail":{"action":"SPLIT_PAGE_CAP"}}
    calls=[]
    class Client:
        def query(self,sql,**kw):
            calls.append((sql,kw))
            values=[{"sequence":1}] if len(calls)==1 else [{"evidence_json":json.dumps(doc)}]
            return SimpleNamespace(result=lambda:values)
    monkeypatch.setattr(C,"bq",lambda:Client())
    assert F.resume(p)==state
    assert "MAX(backfill_sequence)" in calls[0][0] and "backfill_sequence = @seq" in calls[1][0]
    assert all(sql.strip().startswith("SELECT") and kw["job_config"].maximum_bytes_billed==1073741824 for sql,kw in calls)


def test_binding_denial_precedes_backfill_or_any_raw_write(monkeypatch):
    import main as M
    from datetime import datetime,timezone
    monkeypatch.setattr(C,"PROJECT","mpa-t-example");monkeypatch.setattr(C,"DATASET","ozon_raw");monkeypatch.setattr(C,"REF_DATASET","ref")
    monkeypatch.setattr(C,"STRICT_PAGE_CAPS",True)
    monkeypatch.setattr(C,"now_msk",lambda:datetime(2026,10,4,tzinfo=timezone.utc))
    for k,v in env().items():monkeypatch.setenv(k,v)
    monkeypatch.setenv("ENTITIES","fbo_postings")
    monkeypatch.setattr(M,"binding_gate",lambda *a:("seller:NOT_CONFIRMED","synthetic"))
    monkeypatch.setattr(F,"run_backfill",lambda *a:pytest.fail("binding bypass"))
    with pytest.raises(SystemExit) as ex:M.main()
    assert ex.value.code==3


def test_implementation_hash_changes_when_source_changes(monkeypatch):
    original=B.implementation_hash()
    read=B.Path.read_bytes
    def changed(p):
        data=read(p)
        return data+b"\n# synthetic reviewed code change" if p.name=="backfill.py" else data
    monkeypatch.setattr(B.Path,"read_bytes",changed)
    assert B.implementation_hash()!=original


def test_future_journal_origin_cannot_hide_prior_progress():
    values=env();values["BACKFILL_ORIGIN"]="2099-01-01T00:00:00Z"
    with pytest.raises(B.EvidenceError,match="future"):plan(values=values)


def test_sku_quota_deferred_without_report_intent_or_submission(harness,monkeypatch):
    monkeypatch.setattr(F,"export_budget",lambda:0)
    p=plan("ads_sku_daily");s=B.initial(p)
    s["progress"].update(pending=["1"],report=None)
    out=engine(p,s).run(harness[3])
    assert not out["evidence"]["complete"] and not harness[2]
    assert out["evidence"]["state"]["progress"]["report"] is None


def test_exact_image_qualification_driver_exercises_installed_engine():
    import runpy
    from pathlib import Path
    facts=runpy.run_path(str(Path(__file__).with_name('_backfill_image_driver.py')))['qualify']()
    assert facts['backfill_window_v1']=='PASS' and facts['backfill_implementation_hash']==B.implementation_hash()


@pytest.mark.parametrize('foreign_target',[False,True])
def test_real_entrypoint_denies_unbound_or_foreign_backfill_before_resume(harness,monkeypatch,foreign_target):
    import main as M
    values=env();values['ENTITIES']='fbo_postings'
    values['BACKFILL_TARGET_PROJECT']='foreign' if foreign_target else C.PROJECT
    for key,value in values.items():monkeypatch.setenv(key,value)
    monkeypatch.setattr(C,'REF_DATASET','ref')
    monkeypatch.setattr(C,'_seller_execution_scope',C._seller_execution_scope) # restore after main's mutation
    monkeypatch.setattr(F,'resume',lambda *a:pytest.fail('unbound/foreign source progress read'))
    def binding(*a):
        assert not foreign_target, 'foreign config reached binding/cloud gate'
        return {'seller':'UNBOUND'},'no owner binding'
    monkeypatch.setattr(M,'binding_gate',binding)
    with pytest.raises(SystemExit) as result:M.main()
    assert result.value.code==(2 if foreign_target else 3)
    assert not harness[0]



@pytest.mark.parametrize("changed",[False,True])
def test_trial_natural_key_convergence_preserves_unrelated_business_rows(harness,monkeypatch,changed):
    import entities as E
    trial_id="trial-c001-20261001-01" # fixture evidence, never a runtime tenant branch
    original=E._finance_rows(finance_fixture(amount="12")["accruals"],{},"2026-09-17",trial_id,"2026-10-01T00:00:00Z")[0]
    untouched=dict(original,accrual_id=999,event_date="2026-09-18")
    keys=["accrual_id","type_id","sku"]
    db=harness[0].setdefault("RAW_OZON_FINANCE_ACCRUAL",{})
    db[C.merge_key(original,keys)]=copy.deepcopy(original)
    db[C.merge_key(untouched,keys)]=copy.deepcopy(untouched)
    source=finance_fixture(amount="14" if changed else "12")
    monkeypatch.setattr(C,"seller_post",lambda path,body:(200,{"accrual_types":[]} if path.endswith("/types") else source))
    out=engine(plan("finance_accrual")).run(harness[3])
    assert out["inserted"]==0 and out["updated"]==1 and len(db)==2
    assert db[C.merge_key(untouched,keys)]==untouched
    observed=db[C.merge_key(original,keys)]
    assert observed["sku"] is None and observed["amount_rub"]==(14.0 if changed else 12.0)
    ignored=set(E._meta("","",""))|{"source_payload_hash"}
    delta={key for key in original if key not in ignored and original[key]!=observed[key]}
    assert delta==({"amount_rub"} if changed else set())
    assert observed["ingestion_run_id"]!=trial_id # allowed upsert provenance, no broad cleanup


@pytest.mark.parametrize("domain", ["catalog", "supplies", "bundle"])
def test_oversized_source_continuation_denied_before_any_raw_write(harness,monkeypatch,domain):
    huge="x"*900001
    p=plan("supplies" if domain=="bundle" else domain)
    state=B.initial(p)
    if domain=="catalog":
        def source(path,body):
            if path=="/v3/product/list":
                return 200,{"result":{"total_items":2,"items":[{"product_id":1}],"last_id":huge}}
            return 200,{"items":[{"id":1,"sku":"1"}]}
    elif domain=="supplies":
        def source(path,body):
            if path.endswith("/list"):return 200,{"order_ids":[1],"last_id":""}
            return 200,{"orders":[{"order_id":1,"supplies":[{"supply_id":2,"bundle_id":huge}]}]}
    else:
        state["progress"]["bundle"]={"link":[1,2,3],"cursor":"","cursors":[],"seen":{},"count":0,"pages":0}
        def source(path,body):
            return 200,{"items":[{"sku":huge,"quantity":1}],"total_count":2,"has_next":True,"last_id":"next"}
    monkeypatch.setattr(C,"seller_post",source)
    with pytest.raises(B.EvidenceError,match="continuation exceeds"):
        engine(p,state).run(harness[3])
    assert not harness[1] and not harness[2]  # No RAW MERGE and no checkpoint ACK.


def test_pre_merge_reserves_scalar_accounting_growth():
    p=plan();s=B.initial(p);s['padding']=''
    s['padding']='x'*(899900-len(json.dumps(s).encode()))
    assert len(json.dumps(s).encode())==899900
    assert B.validate(p,s) is s
    with pytest.raises(B.EvidenceError,match='continuation exceeds'):
        B.validate(p,s,reserve=128)
