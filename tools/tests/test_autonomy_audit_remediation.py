"""PR #202 — финальная доработка доверенного аудита (ACK 2026-09-28): ограниченное окно прогона, история
журналов/IAM/Cloud Run, dry-run, возможности AE, учёт отметки, привязка инструментирования, F-19 отдельно,
точная идентичность ресурса. Каждый тест — регрессия конкретного пробела; всё недоказанное — BLOCKED.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from tools.autonomy import audit as A
from tools.autonomy.audit import run_audit, run_window, zero_mutations_proven
from tools.tests.ae_fixtures import clean_audit, closed_run, evidence_for
from tools.tests.test_autonomy_audit_coverage import (F18_RN, F18_SA, P, SA, FakeSource, end, ev, f18_iam, pol,
                                                      req, svc_event)

AFTER = datetime(2026, 9, 28, 13, 30, tzinfo=timezone.utc)       # аудит спустя сутки после прогона
OLD_REV = "evetis-wb-communications-00025-rq8"
NEW_REV = "evetis-wb-communications-00032-22j"
TA, TB, TC = "a1" * 16, "b2" * 16, "c3" * 16
OWNER = "owner@example.com"


def hist_chain(**over):
    """27.09: ревизия 00025-rq8 без инструментирования; ежечасный /poll в 11:00:06 и 14:00:04 — вне окна."""
    c = {"requests": [req("/poll", 200, TA, rev=OLD_REV, ts="2026-09-27T11:00:06Z"),
                      req("/poll", 200, TB, rev=OLD_REV, ts="2026-09-27T14:00:04Z")],
         "events": [], "ready": set(),
         "svc_events": [svc_event(ts="2026-07-29T15:08:58Z", generation=24),
                        svc_event(ts="2026-09-28T06:03:58Z", generation=25, prev=OLD_REV)]}
    c.update(over)
    return c


def haudit(chain=None, run=None, iam_events=None, **kw):
    src = FakeSource(iam_events=iam_events if iam_events is not None else f18_iam(), f18=chain or hist_chain(),
                     now=kw.pop("now", AFTER), **kw)

    def wm():
        src.label = "autonomy-audit-wm-hist01"
        return src.label
    return run_audit(src, run_window(run or closed_run()), [SA], wm, settle=True, run_id=(run or closed_run())["run_id"]), src


# ─────────────────────────────────────────────────────────── A: ограниченное окно ───
def test_window_is_derived_from_trusted_state_not_now():
    w = run_window(closed_run())
    assert (w["start"], w["activity_end"], w["end"], w["closed"]) == (
        "2026-09-27T12:07:35Z", "2026-09-27T12:53:58Z", "2026-09-27T13:03:58Z", True)


@pytest.mark.parametrize("mutate,closed", [
    (lambda r: r, True),
    (lambda r: {**r, "state": "REVIEWING", "transitions": r["transitions"][:-1]}, False),        # ещё в работе агента
    (lambda r: {**r, "created_at": "2026-09-27T12:00:00Z"}, False),                              # не совпал первый переход
    (lambda r: {**r, "state": "FAILED"}, False),                                                  # состояние ≠ переходу
    (lambda r: {**r, "transitions": r["transitions"][:3] + r["transitions"][4:]}, False),        # разрыв цепочки
    (lambda r: {**r, "transitions": [*r["transitions"][:-1], {**r["transitions"][-1], "at": "2026-09-27T12:00:00Z"}]}, False),
])
def test_window_closes_only_on_a_consistent_exit_from_agent_work(mutate, closed):
    assert run_window(mutate(closed_run()))["closed"] is closed


def test_resumed_run_window_ends_at_the_last_exit_from_agent_work():
    r = closed_run(tail=[("READY_FOR_PR", "FAILED", "12:55:00")])
    assert run_window(r)["end"] == "2026-09-27T13:03:58Z"
    blocked = closed_run()
    blocked["transitions"][-1] = {**blocked["transitions"][-1], "to": "BLOCKED"}
    blocked["state"] = "BLOCKED"
    resumed = {**blocked, "transitions": blocked["transitions"] + [
        {"from": "BLOCKED", "to": "PLANNING", "at": "2026-09-27T15:00:00Z"},
        {"from": "PLANNING", "to": "BLOCKED", "at": "2026-09-27T15:20:00Z"}], "state": "BLOCKED"}
    assert run_window(resumed)["end"] == "2026-09-27T15:30:00Z"


def test_historical_run_recomputes_no_requests_in_commissioning_window():
    """Критический приёмочный тест: 27.09 — PASS по NO_REQUESTS_IN_COMMISSIONING_WINDOW самим кодом."""
    r, _ = haudit()
    f = r["f18"]
    assert r["status"] == "PASS", r["iam_invariant"]
    assert (f["result"], f["reason"]) == ("PASS", A.NO_REQUESTS)
    assert f["window"] == {"start": "2026-09-27T12:07:35Z", "end": "2026-09-27T13:03:58Z"}
    assert f["counts"]["REMAINING_EVENTS"] == 0 and f["counts"]["service_entries_any_log"] == 0
    assert f["negative_controls"] == {"before": 1, "after": 1, "after_source": "natural", "span_seconds": A.CONTROL_SPAN}
    assert f["serving_at_window_start"] == OLD_REV and f["spec_changes_in_window"] == 0
    assert f["public_in_window"]["iam"] == ["allUsers"]


def test_late_production_traffic_does_not_affect_a_completed_run():
    c = hist_chain()
    c["requests"] += [req("/poll", 200, TC, rev=OLD_REV, ts="2026-09-27T13:04:30Z"),     # после конца окна
                      req("/poll", 200, "d4" * 16, rev=NEW_REV, ts="2026-09-28T12:00:00Z")]
    r, _ = haudit(c)
    assert r["status"] == "PASS" and r["f18"]["reason"] == A.NO_REQUESTS


def test_request_inside_window_on_uninstrumented_revision_is_blocked():
    c = hist_chain()
    c["requests"].append(req("/poll", 200, TC, rev=OLD_REV, ts="2026-09-27T12:30:00Z"))
    r, _ = haudit(c)
    assert r["status"] == "BLOCKED" and r["f18"]["result"] == "BLOCKED"
    assert any("00025-rq8" in x for x in r["iam_invariant"])


@pytest.mark.parametrize("drop", ["before", "after"])
def test_missing_negative_control_blocks(drop):
    c = hist_chain()
    c["requests"] = [x for x in c["requests"] if (x["timestamp"] < "2026-09-27T12") != (drop == "before")]
    r, _ = haudit(c)
    assert r["status"] == "BLOCKED" and any("отрицательный контроль" in x for x in r["iam_invariant"])


def test_non_request_service_entry_inside_window_blocks():
    r, _ = haudit(hist_chain(service_entries=[{"timestamp": "2026-09-27T12:40:00Z", "textPayload": "worker tick",
                                                "resource": {"labels": req("/x", 0, "")["resource"]["labels"]}}]))
    assert r["status"] == "BLOCKED" and any("без запросов" in x for x in r["iam_invariant"])


def test_closed_window_evidence_is_required_for_zero_mutations():
    run = closed_run(audit_status="PASS")
    run["audit_evidence"] = evidence_for(run)
    assert zero_mutations_proven(run)[0]
    for bad in ({"window_end": None}, {"window_end": "2026-09-27T20:00:00Z"}, {"window_start": "2026-09-27T12:12:35Z"},
                {"capability_mode": A.CAPABILITY_STATIC}, {"f18_result": "BLOCKED"}, {"f18_result": None}):
        r2 = {**run, "audit_evidence": {**run["audit_evidence"], **bad}}
        assert not zero_mutations_proven(r2)[0], bad


# ─────────────────────────────────────────────────────────── B: история журналов ───
def lg(method, status=None, ts="2026-09-27T12:30:00Z"):
    pp = {"serviceName": "logging.googleapis.com", "methodName": f"google.logging.v2.ConfigServiceV2.{method}",
          "resourceName": f"projects/{P}/sinks/_Default"}
    if status:
        pp["status"] = {"code": status}
    return {"timestamp": ts, "protoPayload": pp}


@pytest.mark.parametrize("method", ["UpdateSink", "CreateExclusion", "DeleteExclusion", "UpdateBucket", "DeleteBucket",
                                    "CreateView", "UpdateSettings", "DeleteLog"])
def test_any_applied_logging_config_change_since_window_start_blocks(method):
    r, _ = haudit(logging_events=[lg(method)])
    assert r["status"] == "BLOCKED" and any(method in x for x in r["iam_invariant"])


def test_denied_logging_change_is_not_a_change():
    r, _ = haudit(logging_events=[lg("UpdateSink", status=7)])
    assert r["status"] == "PASS"


# ───────────────────────────────────── C/L: публичность в любой момент окна, invoker, идентичность ───
def delta(rn, action, role, member, ts, svc="run.googleapis.com"):
    return {"timestamp": ts, "protoPayload": {"serviceName": svc, "resourceName": rn, "methodName": "SetIamPolicy",
                                              "serviceData": {"policyDelta": {"bindingDeltas": [
                                                  {"action": action, "role": role, "member": member}]}}}}


def test_public_binding_added_and_removed_inside_window_is_seen():
    other = f"projects/{P}/locations/europe-west1/services/some-other-service"
    ev_ = f18_iam() + [delta(other, "ADD", "roles/run.invoker", "allUsers", "2026-09-27T12:20:00Z"),
                       delta(other, "REMOVE", "roles/run.invoker", "allUsers", "2026-09-27T12:40:00Z")]
    r, _ = haudit(iam_events=ev_)
    assert r["status"] == "BLOCKED" and any("в окне" in x and "some-other-service" in x for x in r["iam_invariant"])


def test_f18_service_public_only_inside_window_is_evaluated_not_ignored():
    base = [e for e in f18_iam() if e["protoPayload"].get("resourceName") != F18_RN]
    ev_ = base + [delta(F18_RN, "ADD", "roles/run.invoker", "allUsers", "2026-09-27T12:20:00Z"),
                  delta(F18_RN, "REMOVE", "roles/run.invoker", "allUsers", "2026-09-27T12:40:00Z")]
    r, _ = haudit(iam_events=ev_)
    assert r["f18"]["public_in_window"]["iam"] == ["allUsers"] and r["f18"]["public_now"]["iam"] == []
    assert r["f18"]["result"] == "PASS" and r["f18"]["reason"] == A.NO_REQUESTS       # окно доказывает отсутствие запросов
    assert any("изменение IAM после начала окна" in x for x in r["iam_invariant"])    # но текущее ≠ окну → BLOCKED
    assert r["status"] == "BLOCKED"


def test_invoker_iam_disabled_on_f18_service_is_public_and_needs_the_window_proof():
    c = hist_chain(svc_events=[svc_event(ts="2026-07-29T15:08:58Z", generation=24, invoker="true"),
                               svc_event(ts="2026-09-28T06:03:58Z", generation=25, prev=OLD_REV)])
    base = [e for e in f18_iam() if e["protoPayload"].get("resourceName") != F18_RN]
    r, _ = haudit(c, iam_events=base)
    assert r["f18"]["public_in_window"]["invoker_iam_disabled"] is True and r["f18"]["result"] == "PASS"
    c["requests"].append(req("/poll", 200, TC, rev=OLD_REV, ts="2026-09-27T12:30:00Z"))
    r, _ = haudit(c, iam_events=base)
    assert r["f18"]["result"] == "BLOCKED" and r["status"] == "BLOCKED"


@pytest.mark.parametrize("kw", [{"name": "evetis-wb-communications-shadow"}, {"location": "us-central1"},
                                {"project": "other-project"}])
def test_invoker_disabled_elsewhere_blocks_and_is_not_confused_with_f18(kw):
    c = hist_chain()
    c["svc_events"] = c["svc_events"] + [svc_event(ts="2026-09-27T12:30:00Z", invoker="true",
                                                   sa="x@y.iam.gserviceaccount.com", **kw)]
    r, _ = haudit(c)
    assert r["status"] == "BLOCKED" and any("invoker-iam-disabled" in x for x in r["iam_invariant"])
    assert r["f18"]["spec_changes_in_window"] == 0 and r["f18"]["result"] == "PASS"      # F-18 — только точный ресурс


def test_unknown_invoker_value_blocks():
    c = hist_chain(svc_events=[svc_event(ts="2026-07-29T15:08:58Z", generation=24, invoker="maybe")])
    r, _ = haudit(c)
    assert r["status"] == "BLOCKED" and any("invoker-iam-disabled" in x for x in r["iam_invariant"])


def test_foreign_log_entries_are_excluded_by_platform_labels():
    c = hist_chain()
    foreign = req("/poll", 200, TC, rev=OLD_REV, ts="2026-09-27T12:30:00Z")
    foreign["resource"]["labels"] = {**foreign["resource"]["labels"], "location": "us-central1"}
    c["requests"].append(foreign)
    assert haudit(c)[0]["f18"]["reason"] == A.NO_REQUESTS


def test_resource_id_parsing_is_exact():
    assert A.run_service_id(svc_event()) == (P, "europe-west1", "evetis-wb-communications")
    assert A.run_service_id(svc_event(name="evetis-wb-communications-shadow"))[2] == "evetis-wb-communications-shadow"
    e = svc_event(); e["resource"]["labels"].pop("location")
    assert A.run_service_id(e) is None


# ─────────────────────────────────────────────────────────── D: dry-run ───
def test_good_dry_run_does_not_hide_a_bad_applied_spec():
    c = hist_chain(svc_events=[svc_event(ts="2026-07-29T15:08:58Z", generation=24, sa="evil@x.iam.gserviceaccount.com"),
                               svc_event(ts="2026-07-29T15:09:30Z", generation=24, dry_run="all")])
    r, _ = haudit(c)
    assert r["status"] == "BLOCKED" and any("SA/ingress/timeout" in x for x in r["iam_invariant"])


@pytest.mark.parametrize("unapplied", [{"dry_run": "all"}, {"dry_run": True}, {"status": 9}])
def test_bad_unapplied_change_is_ignored(unapplied):
    c = hist_chain()
    c["svc_events"] = c["svc_events"] + [svc_event(ts="2026-09-27T12:30:00Z", sa="evil@x.iam.gserviceaccount.com",
                                                   invoker="true", **unapplied)]
    r, _ = haudit(c)
    assert r["status"] == "PASS" and r["f18"]["spec_changes_in_window"] == 0


def test_validate_only_and_failed_operation_do_not_change_iam():
    other = f"projects/{P}/locations/europe-west1/services/x"
    vo = delta(other, "ADD", "roles/run.invoker", "allUsers", "2026-09-27T12:20:00Z")
    vo["protoPayload"]["request"] = {"validateOnly": True}
    first = delta(other, "ADD", "roles/run.invoker", "allUsers", "2026-09-27T12:21:00Z")
    first["operation"] = {"id": "op1", "first": True}
    last = {"timestamp": "2026-09-27T12:21:05Z", "operation": {"id": "op1", "last": True},
            "protoPayload": {"serviceName": "run.googleapis.com", "resourceName": other, "status": {"code": 13}}}
    assert A.iam_policy_state([vo, first, last])[0] == {}


# ─────────────────────────────────────────────────────────── F: возможности AE ───
@pytest.mark.parametrize("perm", ["logging.sinks.update", "logging.exclusions.create", "logging.buckets.delete",
                                  "logging.logEntries.create", "logging.views.setIamPolicy", "iam.roles.update",
                                  "run.services.setIamPolicy", "cloudscheduler.jobs.update", "secretmanager.versions.add"])
def test_evidence_surface_permissions_are_forbidden_and_block(perm):
    assert perm in A.FORBIDDEN_PROJECT_PERMISSIONS
    r, _ = haudit(granted=[perm])
    assert r["status"] == "BLOCKED" and any(perm in x for x in r["iam_invariant"])


def _static(**kw):
    proj = pol("cloudresourcemanager.googleapis.com", f"projects/{P}", [
        {"role": "roles/logging.viewer", "members": [f"serviceAccount:{SA}"]},
        {"role": "roles/bigquery.jobUser", "members": [f"serviceAccount:{F18_SA}"]},
        {"role": "roles/datastore.user", "members": [f"serviceAccount:{F18_SA}"]}])
    ev_ = [e for e in f18_iam() if e["protoPayload"].get("resourceName") != f"projects/{P}"] + [proj]
    kw.setdefault("project_policy", {"bindings": [{"role": "roles/logging.viewer", "members": [f"serviceAccount:{SA}"]}]})
    kw.setdefault("role_perms", {"roles/logging.viewer": ["logging.logEntries.list"]})
    return haudit(iam_events=ev_, caller=OWNER, **kw)[0]


def test_owner_run_uses_static_ae_capability_not_owner_permissions():
    """Аудит не от имени AE: права владельца не подменяют AE, раскрытие ролей AE выполняется, но итог — BLOCKED
    (доказательство 0 мутаций требует самопроверки самой AE); F-18 при этом вычисляется полностью."""
    r = _static(granted=["resourcemanager.projects.setIamPolicy"])
    assert r["status"] == "BLOCKED" and len(r["iam_invariant"]) == 1 and "не самопроверкой AE" in r["iam_invariant"][0]
    assert r["capability_mode"] == A.CAPABILITY_STATIC and r["caller_is_ae"] is False
    assert r["f18"]["result"] == "PASS" and r["f18"]["reason"] == A.NO_REQUESTS


@pytest.mark.parametrize("kw,needle", [
    ({"role_perms": {"roles/logging.viewer": ["logging.logEntries.list", "logging.sinks.update"]}}, "по ролям"),
    ({"role_perms": {}}, "не прочитано"),
    ({"project_policy": None}, "не читается"),
    ({"project_policy": {"bindings": [{"role": "roles/owner", "members": [f"serviceAccount:{SA}"]}]}}, "≠ живой"),
])
def test_static_capability_fails_closed(kw, needle):
    r = _static(**kw)
    assert r["status"] == "BLOCKED" and any(needle in x for x in r["iam_invariant"])


def test_static_mode_is_never_publication_evidence():
    run = closed_run(audit_status="PASS")
    run["audit_evidence"] = evidence_for(run, capability_mode=A.CAPABILITY_STATIC)
    ok, why = zero_mutations_proven(run)
    assert not ok and "самопроверкой самой AE" in why


# ─────────────────────────────────────────────────────────── G: история IAM ───
def test_ae_role_granted_after_window_start_blocks_even_if_removed():
    proj = f"projects/{P}"
    ev_ = f18_iam() + [delta(proj, "ADD", "roles/editor", f"serviceAccount:{SA}", "2026-09-27T12:30:00Z",
                             svc="cloudresourcemanager.googleapis.com"),
                       delta(proj, "REMOVE", "roles/editor", f"serviceAccount:{SA}", "2026-09-27T12:50:00Z",
                             svc="cloudresourcemanager.googleapis.com")]
    r, _ = haudit(iam_events=ev_)
    assert r["status"] == "BLOCKED" and any("roles/editor" in x for x in r["iam_invariant"])


def test_ae_change_after_window_end_still_blocks_current_selftest_representativeness():
    add = delta(f"projects/{P}", "ADD", "roles/logging.viewer", f"serviceAccount:{SA}", "2026-09-26T10:00:00Z",
                svc="cloudresourcemanager.googleapis.com")
    assert haudit(iam_events=f18_iam() + [add])[0]["status"] == "PASS"            # до окна — не изменение в окне
    rm = delta(f"projects/{P}", "REMOVE", "roles/logging.viewer", f"serviceAccount:{SA}", "2026-09-28T10:00:00Z",
               svc="cloudresourcemanager.googleapis.com")
    r, _ = haudit(iam_events=f18_iam() + [add, rm])                               # снято уже после окна
    assert r["status"] == "BLOCKED" and any("после начала окна" in x for x in r["iam_invariant"])


def test_non_sensitive_iam_change_is_fine():
    ev_ = f18_iam() + [delta(f"projects/{P}", "ADD", "roles/viewer", "user:someone@example.com", "2026-09-27T12:30:00Z",
                             svc="cloudresourcemanager.googleapis.com")]
    assert haudit(iam_events=ev_)[0]["status"] == "PASS"


def role_ev(role, method="UpdateRole", ts="2026-09-27T12:30:00Z"):
    return {"timestamp": ts, "protoPayload": {"serviceName": "iam.googleapis.com", "resourceName": role,
                                              "methodName": f"google.iam.admin.v1.{method}"}}


def test_custom_role_bound_to_ae_changed_after_start_blocks():
    role = f"projects/{P}/roles/aeBigQueryJobRunner"
    ev_ = f18_iam() + [pol("bigquery.googleapis.com", f"projects/{P}/datasets/wb_raw", [
        {"role": role, "members": [f"serviceAccount:{SA}"]}])]
    r, _ = haudit(iam_events=ev_, role_events=[role_ev(role)])
    assert any("роль AE" in x for x in r["iam_invariant"]) and r["status"] == "BLOCKED"
    r, _ = haudit(role_events=[role_ev(f"projects/{P}/roles/unrelated")])
    assert r["status"] == "PASS"


@pytest.mark.parametrize("change,blocked", [
    ({"datasetChange": {"bindingDeltas": [{"action": "ADD", "role": "roles/bigquery.dataEditor", "member": f"serviceAccount:{SA}"}]}}, True),
    ({"datasetChange": {"dataset": {"acl": {"policy": {"bindings": [{"role": "roles/bigquery.dataViewer", "members": ["allUsers"]}]}}}}}, True),
    ({"datasetChange": {"reason": "UPDATE"}}, True),                                 # ACL не восстановить
    ({"datasetChange": {"bindingDeltas": [{"action": "ADD", "role": "roles/bigquery.dataViewer", "member": "user:a@b.c"}]}}, False),
])
def test_dataset_acl_history(change, blocked):
    e = {"timestamp": "2026-09-27T12:30:00Z", "protoPayload": {"serviceName": "bigquery.googleapis.com",
         "methodName": "google.cloud.bigquery.v2.DatasetService.PatchDataset",
         "resourceName": f"projects/{P}/datasets/wb_raw", "metadata": change}}
    assert (haudit(ds_events=[e])[0]["status"] == "BLOCKED") is blocked


ORG = "organizations/1043233412973"


def test_ancestor_iam_history_is_required_and_checked():
    r, _ = haudit(ancestors=[ORG], ancestor_events={ORG: []}, ancestor_policy={ORG: {"bindings": []}})
    assert r["status"] == "PASS"
    r, _ = haudit(ancestors=[ORG], ancestor_events={ORG: PermissionError("403")})
    assert r["status"] == "BLOCKED" and any("недоступна" in x for x in r["iam_invariant"])
    grant = delta(ORG, "ADD", "roles/owner", f"serviceAccount:{SA}", "2026-09-27T12:30:00Z",
                  svc="cloudresourcemanager.googleapis.com")
    r, _ = haudit(ancestors=[ORG], ancestor_events={ORG: [grant]}, ancestor_policy={ORG: {"bindings": []}})
    assert r["status"] == "BLOCKED" and any("изменён с участием" in x for x in r["iam_invariant"])
    owner_only = delta(ORG, "ADD", "roles/iam.organizationRoleAdmin", "user:owner@example.com", "2026-09-28T08:47:39Z",
                       svc="cloudresourcemanager.googleapis.com")
    assert haudit(ancestors=[ORG], ancestor_events={ORG: [owner_only]},
                  ancestor_policy={ORG: {"bindings": []}})[0]["status"] == "PASS"
    assert haudit(ancestors=[ORG], ancestor_events={ORG: []})[0]["status"] == "BLOCKED"        # политика предка не читается


def test_project_move_and_wif_change_after_start_block():
    mv = {"timestamp": "2026-09-27T12:30:00Z", "protoPayload": {"methodName": "MoveProject", "resourceName": f"projects/{P}"}}
    assert haudit(move_events=[mv])[0]["status"] == "BLOCKED"
    from tools.tests.test_autonomy_audit_coverage import WIP
    upd = {"timestamp": "2026-09-27T12:30:00Z", "protoPayload": {
        "methodName": "google.iam.admin.v1.WorkloadIdentityPools.UpdateWorkloadIdentityPoolProvider",
        "request": {"workloadIdentityPoolProvider": {"name": "x"}, "updateMask": "attributeCondition"}}}
    r, _ = haudit(wip=list(WIP) + [upd])
    assert r["status"] == "BLOCKED" and any("WIF менялась" in x for x in r["iam_invariant"])


# ─────────────────────────────────────────────────────────── H: учёт отметки ───
@pytest.mark.parametrize("n_req,n_end", [(0, 1), (2, 1), (1, 0), (1, 2)])
def test_watermark_must_be_exactly_one_request_and_one_request_end(n_req, n_end):
    r, _ = haudit(hist_chain(service_watermark=n_req, service_watermark_end=n_end))
    assert r["status"] == "BLOCKED" and any("контрольная отметка" in x for x in r["iam_invariant"])


def test_watermark_counts_are_reported_and_open_window_excludes_only_correlated_entries():
    c = {"requests": [req("/health", 200, "e5" * 16, rev=NEW_REV)], "events": [end("e5" * 16, "/health", 200, 0, rev=NEW_REV)],
         "ready": {NEW_REV}, "svc_events": [svc_event(generation=31, image="img@sha256:" + "b" * 64)]}
    src = FakeSource(iam_events=f18_iam(), f18=c)
    src.label = None
    r = run_audit(src, "2026-09-27T12:12:35Z", [SA], lambda: setattr(src, "label", "autonomy-audit-wm-x") or src.label)
    assert set(r["f18"]["counts"]) >= {"TOTAL_EVENTS", "WATERMARK_EVENTS", "EXCLUDED_WATERMARK_EVENTS", "REMAINING_EVENTS"}
    assert r["f18"]["counts"]["WATERMARK_EVENTS"] == r["f18"]["counts"]["EXCLUDED_WATERMARK_EVENTS"]


# ─────────────────────────────────────────────────────────── I: привязка инструментирования ───
def post_chain(**over):
    t = "f6" * 16
    c = {"requests": [req("/poll", 200, t, rev=NEW_REV, ts="2026-09-28T12:30:04Z"),
                      req("/poll", 200, "09" * 16, rev=NEW_REV, ts="2026-09-28T11:00:04Z"),    # контроль до окна
                      req("/poll", 200, "08" * 16, rev=NEW_REV, ts="2026-09-28T14:00:04Z")],   # и после
         "events": [ev("auth_ok", t, rev=NEW_REV, route="/poll", mechanism="scheduler_shared_secret"),
                    end(t, "/poll", 200, 0, rev=NEW_REV)],
         "ready": {NEW_REV},
         "svc_events": [svc_event(ts="2026-09-28T11:26:17Z", generation=31, image="img@sha256:" + "c" * 64)]}
    for e in c["events"]:
        e["timestamp"] = "2026-09-28T12:30:04.5Z"
    c.update(over)
    return c


def post_run():
    return closed_run(created="2026-09-28T12:12:35Z")


def test_post_d19b_request_chain_on_bound_instrumented_revision_passes():
    r, _ = haudit(post_chain(), run=post_run(), now=datetime(2026, 9, 28, 16, 0, tzinfo=timezone.utc))
    assert r["f18"]["result"] == "PASS" and r["f18"]["reason"] == "ATTRIBUTED_REQUEST_CHAIN", r["iam_invariant"]
    assert r["f18"]["revisions"][NEW_REV] == {"image": "sha256:" + "c" * 64, "instrumented": True}


@pytest.mark.parametrize("over", [
    {"svc_events": [svc_event(ts="2026-09-28T11:26:17Z", generation=31, image="img:latest")]},      # не digest
    {"svc_events": [svc_event(ts="2026-09-28T11:26:17Z", generation=30)]},                          # ревизия не та
    {"svc_events": [svc_event(ts="2026-09-28T11:20:00Z", generation=31),                            # две спецификации
                    svc_event(ts="2026-09-28T11:26:17Z", generation=31)]},
    {"ready": set()},                                                                               # маркера нет
])
def test_instrumentation_marker_must_bind_to_revision_and_digest(over):
    r, _ = haudit(post_chain(**over), run=post_run(), now=datetime(2026, 9, 28, 16, 0, tzinfo=timezone.utc))
    assert r["f18"]["result"] == "BLOCKED" and r["status"] == "BLOCKED"


def test_marker_of_another_service_or_region_is_not_accepted():
    class Src(FakeSource):
        def logs(self, flt):
            out = super().logs(flt)
            if "instrumentation_ready" in flt:
                for m in out:
                    m["resource"]["labels"] = {**m["resource"]["labels"], "service_name": "evetis-wb-communications-shadow"}
            return out
    src = Src(iam_events=f18_iam(), f18=post_chain(), now=datetime(2026, 9, 28, 16, 0, tzinfo=timezone.utc))
    r = run_audit(src, run_window(post_run()), [SA], lambda: setattr(src, "label", "autonomy-audit-wm-y") or src.label,
                  settle=True)
    assert r["f18"]["result"] == "BLOCKED"


def test_named_revision_is_bound_through_the_next_event():
    tl = [("t1", {"generation": 28, "template_name": None, "prev_created": "x-00028-a", "image": "i@sha256:1"}),
          ("t2", {"generation": 29, "template_name": None, "prev_created": "x-tg-rot1126", "image": "i@sha256:2"})]
    assert A.revision_origin(tl, "x-tg-rot1126", "x")["image"] == "i@sha256:1"
    assert A.revision_origin(tl, "x-00030-zz", "x")["image"] == "i@sha256:2"


# ─────────────────────────────────────────────────────────── J: F-19 отдельно ───
def test_accepted_historical_risk_is_shown_but_never_changes_a_result():
    r, _ = haudit()
    assert [x["id"] for x in r["accepted_risks"]] == ["F-19"] and r["accepted_risks"][0]["affects_machine_result"] is False
    c = hist_chain()
    c["requests"].append(req("/poll", 200, TC, rev=OLD_REV, ts="2026-09-27T12:30:00Z"))
    r, _ = haudit(c)
    assert r["status"] == "BLOCKED" and r["accepted_risks"]                          # риск принят, но BLOCKED остаётся
    run = closed_run(audit_status="BLOCKED")
    run["audit_evidence"] = evidence_for(run, status="BLOCKED", mutations=None, accepted_risks=["F-19"])
    assert not zero_mutations_proven(run)[0]


def test_f19_facts_are_machine_read_from_admin_activity():
    sec = [{"timestamp": "2026-09-28T07:42:19Z", "protoPayload": {"methodName": "AddSecretVersion",
            "resourceName": "projects/1/secrets/EVETIS_TELEGRAM_BOT_TOKEN/versions/2"}},
           {"timestamp": "2026-09-28T08:26:48Z", "protoPayload": {"methodName": "DisableSecretVersion",
            "resourceName": "projects/1/secrets/EVETIS_TELEGRAM_BOT_TOKEN/versions/1"}}]
    r, _ = haudit(secret_events=sec)
    assert r["f19"] == {"secret": "EVETIS_TELEGRAM_BOT_TOKEN", "v1_disabled_at": "2026-09-28T08:26:48",
                        "new_versions": ["2"], "v1_disabled_after_window": True}
    assert r["status"] == "PASS"


# ─────────────────────────────────────────── MEDIUM #6: словарь маршрутов на стороне аудита ───
@pytest.mark.parametrize("route", ["/bearer abcdefghijklmnop", "/x/123456789:AAFAKE_fake-token-for-tests_0123456789abcdef",
                                   "/wp-admin", "/ünï"])
def test_raw_route_in_event_is_blocked_not_an_attacker_triggered_fail(route):
    c = post_chain()
    t = "a7" * 16
    c["requests"].append(req("/wp", 404, t, rev=NEW_REV, ts="2026-09-28T12:40:10Z"))
    e = end(t, route, 404, 0, rev=NEW_REV)
    e["timestamp"] = "2026-09-28T12:40:10.5Z"
    c["events"].append(e)
    r, _ = haudit(c, run=post_run(), now=datetime(2026, 9, 28, 16, 0, tzinfo=timezone.utc))
    assert r["status"] == "BLOCKED" and r["mutations"] is None and not r["by_type"]
    text = str(r)
    assert "bearer abc" not in text and "AAFAKE" not in text and "ünï" not in text


def test_unknown_request_path_never_reaches_audit_output_raw():
    c = post_chain()
    t = "a8" * 16
    c["requests"].append(req("/debug/secret-path-xyz", 200, t, rev=NEW_REV, ts="2026-09-28T12:40:10Z"))
    e = end(t, "UNKNOWN", 200, 0, rev=NEW_REV)
    e["timestamp"] = "2026-09-28T12:40:10.5Z"
    c["events"].append(e)
    r, _ = haudit(c, run=post_run(), now=datetime(2026, 9, 28, 16, 0, tzinfo=timezone.utc))
    assert r["status"] == "BLOCKED" and not r["by_type"] and "secret-path-xyz" not in str(r)


# ─────────────────────────────────────────────────────────── in-process publication ───
def test_clean_audit_fixture_matches_the_closed_window_contract():
    run = closed_run()
    a = clean_audit(run)
    assert a["window"]["closed"] and a["capability_mode"] == A.CAPABILITY_LIVE


# ─────────────────────────────────────────── добивание мутантов (история, отметка, маркер) ───
def test_federated_binding_change_after_window_start_blocks():
    fed = "principalSet://iam.googleapis.com/projects/1/locations/global/workloadIdentityPools/github-pool/attribute.repository/x/y"
    ev_ = f18_iam() + [delta("projects/-/serviceAccounts/100", "ADD", "roles/iam.workloadIdentityUser", fed,
                             "2026-09-27T12:30:00Z", svc="iam.googleapis.com")]
    r, _ = haudit(iam_events=ev_)
    assert r["status"] == "BLOCKED" and any("после начала окна" in x and "principalSet" in x for x in r["iam_invariant"])


def test_watermark_entry_inside_a_closed_window_blocks(monkeypatch):
    import secrets
    monkeypatch.setattr(secrets, "token_hex", lambda n=16: "ab" * n)
    c = hist_chain()
    c["requests"].append(req("/health", 200, "ab" * 16, rev=OLD_REV, ts="2026-09-27T12:30:00Z"))
    r, _ = haudit(c)
    assert r["status"] == "BLOCKED" and any("внутри закрытого окна" in x for x in r["iam_invariant"])


def test_excluded_watermark_must_be_correlated_by_route_and_method(monkeypatch):
    import secrets
    monkeypatch.setattr(secrets, "token_hex", lambda n=16: "cd" * n)
    c = {"requests": [req("/poll", 200, "cd" * 16, rev=NEW_REV)], "events": [], "ready": {NEW_REV},
         "svc_events": [svc_event(generation=31, image="img@sha256:" + "b" * 64)]}
    src = FakeSource(iam_events=f18_iam(), f18=c)
    r = run_audit(src, "2026-09-27T12:12:35Z", [SA], lambda: setattr(src, "label", "autonomy-audit-wm-z") or src.label)
    assert r["status"] == "BLOCKED" and any("не опознана как отметка" in x for x in r["iam_invariant"])
    c_ = r["f18"]["counts"]                  # отметка (запрос + request_end) исключена, чужой /poll с её trace — нет
    assert c_["WATERMARK_EVENTS"] - c_["EXCLUDED_WATERMARK_EVENTS"] == 1


@pytest.mark.parametrize("field,value", [("service", "evetis-wb-communications-shadow"), ("revision", "other-rev"),
                                         ("audit_schema", "wbc-audit/0")])
def test_marker_payload_must_name_this_service_revision_and_schema(field, value):
    class Src(FakeSource):
        def logs(self, flt):
            out = super().logs(flt)
            if "instrumentation_ready" in flt:
                for m in out:
                    m["jsonPayload"][field] = value
            return out
    src = Src(iam_events=f18_iam(), f18=post_chain(), now=datetime(2026, 9, 28, 16, 0, tzinfo=timezone.utc))
    r = run_audit(src, run_window(post_run()), [SA], lambda: setattr(src, "label", "autonomy-audit-wm-m") or src.label,
                  settle=True)
    assert r["f18"]["result"] == "BLOCKED" and r["f18"]["revisions"][NEW_REV]["instrumented"] is False


@pytest.mark.parametrize("url,expected", [("https://s/admin/test-wb", "/admin"), ("https://s/admin", "/admin"),
                                          ("https://s/administrator", "/administrator"), ("https://s/poll%3Fx=1", "/poll?x=1"),
                                          ("https://s/poll?x=1", "/poll")])
def test_consumer_route_mapping_matches_producer_routing(url, expected):
    assert A._route(url) == expected


def test_ancestor_logging_config_change_since_window_start_blocks():
    """Агрегирующий sink организации с intercept мог увести журналы проекта — история журналов предка обязательна."""
    sink = {"timestamp": "2026-09-27T12:30:00Z", "protoPayload": {
        "serviceName": "logging.googleapis.com", "methodName": "google.logging.v2.ConfigServiceV2.CreateSink",
        "resourceName": f"{ORG}/sinks/intercept-all"}}
    r, _ = haudit(ancestors=[ORG], ancestor_events={ORG: [sink]}, ancestor_policy={ORG: {"bindings": []}})
    assert r["status"] == "BLOCKED" and any(ORG in x and "CreateSink" in x for x in r["iam_invariant"])


# ─────────────────────────────────── второй раунд ревью: M1–M4 и LOW ───
def test_negative_control_after_window_is_natural_traffic_not_the_audit_watermark():
    c = hist_chain()
    c["requests"] = [x for x in c["requests"] if x["timestamp"] < "2026-09-27T12"]        # только контроль до окна
    r, _ = haudit(c)                                         # аудит через сутки: естественный трафик после окна обязан быть
    assert r["status"] == "BLOCKED" and r["f18"]["negative_controls"]["after_source"] is None
    fresh = datetime(2026, 9, 27, 13, 10, tzinfo=timezone.utc)          # сразу после окна — отметка с пометкой
    r, _ = haudit(c, now=fresh)
    assert r["f18"]["negative_controls"]["after_source"] == "watermark" and r["f18"]["result"] == "PASS"


def test_intercepting_ancestor_sink_or_unreadable_sinks_block():
    base = dict(ancestors=[ORG], ancestor_events={ORG: []}, ancestor_policy={ORG: {"bindings": []}})
    assert haudit(**base)[0]["status"] == "PASS"
    r, _ = haudit(**base, ancestor_sinks={ORG: [{"name": f"{ORG}/sinks/x", "includeChildren": True, "interceptChildren": True}]})
    assert r["status"] == "BLOCKED" and any("перехватывает" in x for x in r["iam_invariant"])
    assert haudit(**base, ancestor_sinks={ORG: None})[0]["status"] == "BLOCKED"              # и истории нет
    born = {"timestamp": "2026-07-10T10:26:25Z", "protoPayload": {"methodName": "CreateOrganization", "resourceName": ORG}}
    sink = {"timestamp": "2026-08-01T00:00:00Z", "protoPayload": {"serviceName": "logging.googleapis.com",
            "methodName": "google.logging.v2.ConfigServiceV2.CreateSink", "resourceName": f"{ORG}/sinks/x"}}
    hist = dict(base, ancestor_sinks={ORG: None})
    assert haudit(**{**hist, "ancestor_events": {ORG: [born]}})[0]["status"] == "PASS"         # история с создания пуста
    assert haudit(**{**hist, "ancestor_events": {ORG: [born, sink]}})[0]["status"] == "BLOCKED"
    copy_only = [{"name": f"{ORG}/sinks/y", "includeChildren": True}]                   # копия, не перехват
    assert haudit(**base, ancestor_sinks={ORG: copy_only})[0]["status"] == "PASS"


def test_full_policy_without_delta_cannot_hide_a_removal():
    full = {"timestamp": "2026-09-27T12:30:00Z", "protoPayload": {"serviceName": "cloudresourcemanager.googleapis.com",
            "resourceName": ORG, "methodName": "SetIamPolicy", "request": {"policy": {"bindings": []}}}}
    r, _ = haudit(ancestors=[ORG], ancestor_events={ORG: [full]}, ancestor_policy={ORG: {"bindings": []}})
    assert r["status"] == "BLOCKED" and any("без дельты" in x for x in r["iam_invariant"])
    rm = {"timestamp": "2026-09-27T12:30:00Z", "protoPayload": {"serviceName": "bigquery.googleapis.com",
          "methodName": "google.cloud.bigquery.v2.DatasetService.PatchDataset", "resourceName": f"projects/{P}/datasets/wb_raw",
          "metadata": {"datasetChange": {"bindingDeltas": [{"action": "REMOVE", "role": "roles/bigquery.dataEditor",
                                                            "member": f"serviceAccount:{SA}"}]}}}}
    assert haudit(ds_events=[rm])[0]["status"] == "BLOCKED"                              # снятие AE тоже видно
    full_ds = {**rm, "protoPayload": {**rm["protoPayload"], "metadata": {"datasetChange": {"dataset": {"acl": {
        "policy": {"bindings": []}}}}}}}
    assert haudit(ds_events=[full_ds])[0]["status"] == "BLOCKED"
    gone = {**rm, "protoPayload": {**rm["protoPayload"], "metadata": {"datasetDeletion": {}}}}
    assert haudit(ds_events=[gone])[0]["status"] == "BLOCKED"


def test_group_bound_read_role_with_forbidden_permission_blocks():
    ev_ = f18_iam() + [pol("cloudresourcemanager.googleapis.com", f"projects/{P}/x", [
        {"role": "roles/cloudscheduler.viewer", "members": ["group:ops@example.com"]}], ts="2026-09-01T00:00:00Z")]
    r, _ = haudit(iam_events=ev_, role_perms={"roles/cloudscheduler.viewer": ["cloudscheduler.jobs.get"]})
    assert r["status"] == "BLOCKED" and any("cloudscheduler.jobs.get" in x for x in r["iam_invariant"])
    r, _ = haudit(iam_events=ev_, role_perms={"roles/cloudscheduler.viewer": ["cloudscheduler.locations.list"]})
    assert r["status"] == "PASS"
    grp = {ORG: {"bindings": [{"role": "roles/owner", "members": ["group:ops@example.com"]}]}}
    r, _ = haudit(ancestors=[ORG], ancestor_events={ORG: []}, ancestor_policy=grp)
    assert r["status"] == "BLOCKED" and any(ORG in x and "group:ops" in x for x in r["iam_invariant"])


def test_ae_identity_actions_are_audited_until_window_end_plus_token_ttl():
    def act(ts):
        return {"timestamp": ts, "protoPayload": {"methodName": "google.cloud.bigquery.v2.TableService.DeleteTable",
                                                  "authenticationInfo": {"principalEmail": SA}}}
    r, src = haudit(activity=[act("2026-09-27T13:30:00Z")])         # после окна, но в пределах жизни токена AE
    assert r["status"] == "FAIL" and "ADMIN:google.cloud.bigquery.v2.TableService.DeleteTable" in r["by_type"]
    ae_filters = [f for f in src.filters if "authenticationInfo" in f and "cloudaudit.googleapis.com%2Factivity" in f]
    assert ae_filters and all('timestamp<="2026-09-27T14:03:58Z"' in f for f in ae_filters)      # конец окна + TTL
    live = datetime(2026, 9, 27, 13, 10, tzinfo=timezone.utc)       # аудит сразу после окна: горизонт = момент чтения
    _r, src = haudit(now=live)
    assert all('timestamp<="2026-09-27T13:10:00Z"' in f for f in src.filters
               if "authenticationInfo" in f and "cloudaudit.googleapis.com%2Factivity" in f)


def test_audit_config_change_since_window_start_blocks_and_mask_keeps_bindings():
    ac = {"timestamp": "2026-09-27T12:30:00Z", "protoPayload": {"serviceName": "cloudresourcemanager.googleapis.com",
          "resourceName": f"projects/{P}", "methodName": "SetIamPolicy",
          "request": {"policy": {"auditConfigs": [{"service": "allServices"}]}, "updateMask": "auditConfigs"},
          "serviceData": {"policyDelta": {"auditConfigDeltas": [{"action": "REMOVE", "service": "secretmanager.googleapis.com"}]}}}}
    r, src = haudit(iam_events=f18_iam() + [ac])
    assert r["status"] == "BLOCKED" and any("auditConfigs" in x for x in r["iam_invariant"])
    state, _p = A.iam_policy_state(f18_iam() + [ac])
    assert state[("cloudresourcemanager.googleapis.com", f"projects/{P}")]           # привязки не обнулились


def test_stray_entries_of_uninstrumented_revision_block_in_attribution_branch():
    c = post_chain(service_entries=[{"timestamp": "2026-09-28T12:35:00Z", "textPayload": "startup",
                                     "resource": {"labels": {**req("/x", 0, "")["resource"]["labels"],
                                                             "revision_name": "evetis-wb-communications-00031-qz6"}}}])
    r, _ = haudit(c, run=post_run(), now=datetime(2026, 9, 28, 16, 0, tzinfo=timezone.utc))
    assert r["f18"]["result"] == "BLOCKED" and any("без привязанного инструментирования" in x for x in r["iam_invariant"])


# ─────────────────────────────────── финальное ревью: MEDIUM-1 (/admin/../health) ───
def test_dot_segment_path_cannot_force_a_sticky_false_fail():
    """Фронт Google мог нормализовать `/admin/../health` в `/health` (200), а журнал хранит исходный URL."""
    c = post_chain()
    t = "a9" * 16
    c["requests"].append(req("/admin/../health", 200, t, rev=NEW_REV, ts="2026-09-28T12:40:10Z"))
    e = end(t, "/health", 200, 0, rev=NEW_REV)
    e["timestamp"] = "2026-09-28T12:40:10.5Z"
    c["events"].append(e)
    r, _ = haudit(c, run=post_run(), now=datetime(2026, 9, 28, 16, 0, tzinfo=timezone.utc))
    assert r["status"] == "BLOCKED" and not r["by_type"] and r["mutations"] is None


def test_protected_route_disagreement_between_platform_and_app_is_blocked_not_fail():
    c = post_chain()
    t = "aa" * 16
    c["requests"].append(req("/admin/x", 200, t, rev=NEW_REV, ts="2026-09-28T12:40:10Z"))
    e = end(t, "/health", 200, 0, rev=NEW_REV)
    e["timestamp"] = "2026-09-28T12:40:10.5Z"
    c["events"].append(e)
    r, _ = haudit(c, run=post_run(), now=datetime(2026, 9, 28, 16, 0, tzinfo=timezone.utc))
    assert r["status"] == "BLOCKED" and not r["by_type"]
    e["jsonPayload"]["route"] = "/admin"                          # согласны и нет auth_ok — это FAIL
    r, _ = haudit(c, run=post_run(), now=datetime(2026, 9, 28, 16, 0, tzinfo=timezone.utc))
    assert r["status"] == "FAIL" and any("без auth_ok" in k for k in r["by_type"])


@pytest.mark.parametrize("url,expected", [("https://s/poll;x", "/poll;x"), ("https://s/admin/../health", "UNKNOWN:dot-segments")])
def test_consumer_route_keeps_params_and_rejects_dot_segments(url, expected):
    assert A._route(url) == expected
