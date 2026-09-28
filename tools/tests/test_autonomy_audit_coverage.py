"""Закрытие PR #202: доверенный аудит не может дать ложный PASS, ci-verify привязан к проверенному SHA.

Прежний аудит читал `region-eu.INFORMATION_SCHEMA.JOBS_BY_PROJECT` с `statement_type IS NOT NULL AND != 'SELECT'`:
LOAD/COPY/EXTRACT (statement_type NULL), SELECT с записью в таблицу (WRITE_TRUNCATE) и задания вне EU
были невидимы, а задержка INFORMATION_SCHEMA не ограничивалась. Теперь: jobs.list (все регионы) с зондом-отметкой,
Admin Activity и живая самопроверка IAM; всё недоказанное — BLOCKED, найденное — FAIL.
"""
from __future__ import annotations

import io
import urllib.error
from datetime import datetime, timedelta, timezone

import pytest

from tools.autonomy import audit as A
from tools.autonomy.audit import AUDIT_SOURCE, acl_problems, is_ae_job, job_mutation, run_audit, zero_mutations_proven

P = "project-fa311fc0-4d87-4781-986"
SA = f"sa-ae-reader@{P}.iam.gserviceaccount.com"
ANON = "_" + "0" * 40
NOW = datetime(2026, 9, 27, 13, 0, tzinfo=timezone.utc)
SINCE = "2026-09-27T12:12:35Z"
LOADER = f"sa-loaders-prod@{P}.iam.gserviceaccount.com"


def job(jt="QUERY", st="SELECT", dest=None, email=SA, state="DONE", err=None, purpose=None, location="EU"):
    cfg = {"jobType": jt}
    if jt == "QUERY":
        cfg["query"] = {"destinationTable": dest} if dest else {}
    if purpose:
        cfg["labels"] = {"purpose": purpose}
    j = {"user_email": email, "configuration": cfg, "jobReference": {"location": location},
         "status": {"state": state, **({"errorResult": err} if err else {})}, "statistics": {}}
    if st is not None:
        j["statistics"]["query"] = {"statementType": st}
    return j


def anon_dest():
    return {"projectId": P, "datasetId": ANON, "tableId": "anon1"}


def _wif_events():
    """Admin Activity, из которого восстанавливается ЖЕЛАЕМАЯ конфигурация WIF (Terraform = live, wif_check PASS)."""
    from tools.autonomy import wif_check
    cfg = wif_check.load_terraform()
    pool = "principalSet://iam.googleapis.com/projects/1/locations/global/workloadIdentityPools/github-pool/"
    wip = [{"timestamp": "2026-07-26T10:21:06Z", "protoPayload": {
        "methodName": "google.iam.admin.v1.WorkloadIdentityPools.CreateWorkloadIdentityPoolProvider",
        "request": {"workloadIdentityPoolProviderId": "github-provider",
                    "parent": f"projects/{P}/locations/global/workloadIdentityPools/github-pool",
                    "workloadIdentityPoolProvider": {
            "attributeCondition": cfg.condition, "attributeMapping": cfg.mapping,
            "oidc": {"issuerUri": "https://token.actions.githubusercontent.com"}}}}}]
    created, policies = [], []
    for i, (sa, members) in enumerate(sorted(cfg.bindings.items())):
        uid = str(100 + i)
        created.append({"protoPayload": {"response": {"email": f"{sa}@{P}.iam.gserviceaccount.com", "unique_id": uid}}})
        ms = [(f"{pool}{a}/{v}" if a != "google.subject" else
               f"principal://iam.googleapis.com/projects/1/locations/global/workloadIdentityPools/github-pool/subject/{v}")
              for a, v in members]
        policies.append({"timestamp": "2026-09-25T00:00:00Z", "protoPayload": {
            "serviceName": "iam.googleapis.com", "resourceName": f"projects/-/serviceAccounts/{uid}",
            "request": {"policy": {"bindings": [{"role": "roles/iam.workloadIdentityUser", "members": ms}]}}}})
    return wip, created, policies


WIP, SA_CREATED, SA_POLICIES = _wif_events()


def table_policy(rn, members=(f"serviceAccount:sa-loaders-prod@{P}.iam.gserviceaccount.com",)):
    return {"timestamp": "2026-08-01T00:00:00Z", "protoPayload": {
        "serviceName": "bigquery.googleapis.com", "resourceName": rn, "metadata": {"tableChange": {"table": {
            "policy": {"bindings": [{"role": "roles/bigquery.dataEditor", "members": list(members)}]}}}}}}


class FakeSource:
    def __init__(self, jobs=(), activity=(), access=(), granted=(), datasets=None, table_policies=(), writable=(),
                 created="2026-07-10T10:26:24Z", watermark_after=0, log_watermark_after=0, sa_events=None,
                 sa_granted=None, grants=None, routing=None, iam_events=None, wip=None, table_events=(),
                 f18=None, caller=SA, ancestors=(), ancestor_events=None, ancestor_policy=None, project_policy=None,
                 role_perms=None, logging_events=(), role_events=(), ds_events=(), move_events=(), secret_events=(),
                 now=None, ancestor_sinks=None):
        self.project = P
        self._now = now or NOW
        self._anc_sinks = dict(ancestor_sinks or {})
        self._wm_req, self._wm_ev = [], []          # отметка аудита — видна во ВСЕХ подходящих запросах журнала
        self._caller, self._ancestors = caller, list(ancestors)
        self._anc_events, self._anc_policy = dict(ancestor_events or {}), dict(ancestor_policy or {})
        self._project_policy, self._role_perms = project_policy, dict(role_perms or {})
        self._logging_events, self._role_events, self._ds_events = list(logging_events), list(role_events), list(ds_events)
        self._move_events, self._secret_events = list(move_events), list(secret_events)
        self._jobs, self._activity, self._access, self._granted = list(jobs), list(activity), list(access), list(granted)
        self._datasets = datasets if datasets is not None else {
            "wb_raw": {"location": "EU", "access": [{"role": "READER", "userByEmail": SA}]},
            ANON: {"location": "EU", "access": [{"role": "OWNER", "userByEmail": SA}]}}
        self._policies, self._writable, self._created = list(table_policies), dict(writable), created
        self._sa_events = sa_events if sa_events is not None else SA_CREATED + [
            {"protoPayload": {"response": {"email": LOADER, "unique_id": "900"}}}]
        self._iam_events = (list(iam_events) if iam_events is not None else list(SA_POLICIES)) + [
            table_policy(rn) for rn in table_policies]
        self._wip = wip if wip is not None else WIP
        self._table_events = list(table_events)
        self._f18 = f18 or {}
        self.tables_tested = []
        self._sa_granted, self._grants = dict(sa_granted or {}), dict(grants or {})
        self._routing = routing or {"sink": {"filter": A.DEFAULT_SINK_FILTER}, "exclusions": [],
                                    "bucket": {"retentionDays": 30}}
        self.watermark_after, self.log_watermark_after = watermark_after, log_watermark_after
        self.list_calls, self.log_probe_calls, self.sleeps, self.label = 0, 0, [], None
        self.filters, self.sa_checked, self.grant_locations = [], [], []

    def now(self):
        return self._now

    def sleep(self, s):
        self.sleeps.append(s)

    def jobs(self, since_ms):
        self.list_calls += 1
        extra = [job(purpose=self.label)] if self.label and self.list_calls > self.watermark_after else []
        return self._jobs + extra

    def logs(self, flt):
        self.filters.append(flt)
        if 'protoPayload.serviceName="logging.googleapis.com"' in flt:
            return self._logging_events
        if 'protoPayload.methodName:"CreateRole"' in flt:
            return self._role_events
        if "protoPayload.metadata.datasetChange:*" in flt:
            return self._ds_events
        if 'protoPayload.methodName:"MoveProject"' in flt:
            return self._move_events
        if "/secrets/EVETIS_TELEGRAM_BOT_TOKEN/versions/" in flt:
            return self._secret_events
        if self.label and self.label in flt:
            self.log_probe_calls += 1
            return [{"probe": 1}] if self.log_probe_calls > self.log_watermark_after else []
        if "CreateServiceAccount" in flt:
            return self._sa_events
        if "tableCreation" in flt:
            return self._table_events
        if "instrumentation_ready" in flt:
            rev = flt.split('revision_name="', 1)[1].split('"', 1)[0]
            return [{"resource": {"labels": _run_labels(rev)},
                     "jsonPayload": {"audit_event": "instrumentation_ready", "audit_schema": "wbc-audit/1",
                                     "revision": rev, "service": "evetis-wb-communications", "result": "ok",
                                     "trace_id": "", "request_id": ""}}] if rev in self._f18.get("ready", set()) else []
        reqs = self._f18.get("requests", []) + self._wm_req
        evs = self._f18.get("events", []) + self._wm_ev
        if 'trace="projects/' in flt:
            t = flt.split('trace="projects/', 1)[1].split('"', 1)[0]
            return [r for r in reqs if r.get("trace") == f"projects/{t}"]
        if 'audit_event="request_end" AND jsonPayload.trace_id=' in flt:
            t = flt.split('jsonPayload.trace_id="', 1)[1].split('"', 1)[0]
            return [e for e in evs if e["jsonPayload"].get("trace_id") == t and e["jsonPayload"]["audit_event"] == "request_end"]
        if "run.googleapis.com%2Frequests" in flt:
            return _ts_filter(reqs, flt)
        if "jsonPayload.audit_event:*" in flt:
            assert 'jsonPayload.logger="app.audit"' in flt and "run.googleapis.com%2Fstdout" in flt
            return _ts_filter(evs, flt)
        if 'resource.type="cloud_run_revision"' in flt and "logName=" not in flt:
            return _ts_filter(self._f18.get("service_entries", []) + reqs + evs, flt)
        if 'protoPayload.methodName:"Service"' in flt:
            return self._f18.get("svc_events", [])
        if "setIamPermissions" in flt:
            return self._iam_events
        if "WorkloadIdentityPool" in flt:
            return self._wip
        if "data_access" in flt:
            return self._access
        return self._activity

    def routing(self):
        return self._routing

    def granted(self, perms):
        assert set(self._granted) <= set(perms)
        return self._granted

    def sa_granted(self, email, perms):
        self.sa_checked.append(email)
        return self._sa_granted.get(email, [])

    def datasets(self):
        return self._datasets

    def table_can_write(self, ds, table):
        self.tables_tested.append((ds, table))
        return self._writable.get((ds, table), False)

    def project_created(self):
        return self._created

    def project_number(self):
        return "1"

    def secret_granted(self, name):
        if name in self._f18.get("missing_secrets", set()):
            return None
        return self._f18.get("secrets", {}).get(name, [])

    def probe_service(self, url, trace):
        self.probed = (url, trace)
        ts = self._now.strftime("%Y-%m-%dT%H:%M:%SZ")
        n_req = int(self._f18.get("service_watermark", 1))
        n_end = int(self._f18.get("service_watermark_end", n_req))
        self._wm_req += [req("/health", 200, trace, ts=ts) for _ in range(n_req)]
        for _ in range(n_end):
            e = end(trace, "/health", 200, 0)
            e["timestamp"] = ts
            self._wm_ev.append(e)
        return 200

    def bucket_granted(self, bucket, perms):
        if self._f18.get("bucket_missing"):
            return None
        return self._f18.get("buckets", {}).get(bucket, [])

    def project_number(self):
        return "1"

    def caller(self):
        return self._caller

    def ancestors(self):
        return self._ancestors

    def ancestor_logs(self, anc, flt):
        v = self._anc_events.get(anc, [])
        if isinstance(v, Exception):
            raise v
        return _ts_filter(v, flt)

    def ancestor_policy(self, anc):
        return self._anc_policy.get(anc)

    def project_policy(self):
        return self._project_policy

    def ancestor_sinks(self, anc):
        v = self._anc_sinks.get(anc, [])
        return None if v is None else v

    def role_permissions(self, role):
        return self._role_perms.get(role)

    def grant_jobs(self, location, since_iso):
        self.grant_locations.append(location)
        return self._grants.get(location, 0)


def _ts_filter(entries, flt):
    """Границы времени фильтра Logging (>=, <=, >, <) — как на сервере; для записей F-18."""
    import re as _re
    out = entries
    for op, val in _re.findall(r'timestamp(>=|<=|>|<)"([^"]+)"', flt):
        v = A._secs(val)
        cmp = {">=": lambda t: t >= v, "<=": lambda t: t <= v, ">": lambda t: t > v, "<": lambda t: t < v}[op]
        out = [e for e in out if cmp(A._secs(e.get("timestamp")))]
    return out


def audit(src, **kw):
    def watermark():
        src.label = "autonomy-audit-wm-abc123"
        return src.label
    return run_audit(src, SINCE, [SA], watermark, **kw)


# ------------------------------------------------------------ классы заданий ---
@pytest.mark.parametrize("j,expected", [
    (job(), None),                                                              # SELECT без записи
    (job(dest=anon_dest()), None),                                              # результат в анонимном кэше
    (job(jt="LOAD", st=None), "LOAD:None"),
    (job(jt="COPY", st=None), "COPY:None"),
    (job(jt="EXTRACT", st=None), "EXTRACT:None"),
    (job(dest={"projectId": P, "datasetId": "wb_mart", "tableId": "X"}), "QUERY:SELECT->wb_mart.X"),  # WRITE_TRUNCATE
    (job(dest={"projectId": "other", "datasetId": ANON, "tableId": "t"}), "QUERY:SELECT->" + ANON + ".t"),
    (job(st="SCRIPT"), "QUERY:SCRIPT"),
    (job(st="MERGE"), "QUERY:MERGE"),
    (job(st=None, state="RUNNING"), "QUERY:None"),                              # ещё не разобран — не доказано
    (job(st=None, state="DONE"), "QUERY:None"),
    (job(st=None, state="DONE", err={"reason": "invalidQuery"}), None),         # не разобран — не исполнялся
    (job(st="UPDATE", err={"reason": "accessDenied"}), "QUERY:UPDATE"),         # отказанная попытка — всё равно FAIL
    (job(jt=None, st=None), "None:None"),
])
def test_job_classes(j, expected):
    assert job_mutation(j, P) == expected


def test_ae_job_identity():
    assert is_ae_job(job(), [SA])
    assert is_ae_job(job(email="principal://iam.googleapis.com/projects/1/locations/global/workloadIdentityPools/p/subject/x"), [SA])
    assert is_ae_job(job(email="principalSet://iam.googleapis.com/x"), [SA])
    assert is_ae_job(job(email="someone@x", purpose="autonomy-test"), [SA])
    assert not is_ae_job(job(email=f"sa-loaders-prod@{P}.iam.gserviceaccount.com", st="MERGE"), [SA])


# ------------------------------------------------------------ решение аудита ---
def test_clean_is_pass_with_current_source():
    r = audit(FakeSource(jobs=[job(), job(dest=anon_dest())]))
    assert r["status"] == "PASS" and r["mutations"] == 0 and r["source"] == AUDIT_SOURCE and r["ae_jobs"] == 3


@pytest.mark.parametrize("bad", [job(jt="LOAD", st=None, location="US"), job(jt="COPY", st=None, location="europe-west1"),
                                 job(jt="EXTRACT", st=None),
                                 job(dest={"projectId": P, "datasetId": "wb_raw", "tableId": "RAW_WB_PRICES"}),
                                 job(email="principal://iam.googleapis.com/x", st="DELETE")])
def test_every_previously_invisible_mutation_class_is_fail(bad):
    r = audit(FakeSource(jobs=[job(), bad]))
    assert r["status"] == "FAIL" and r["mutations"] == 1 and not zero_mutations_proven(_run(r))[0]


def test_production_loaders_are_not_attributed_to_ae():
    loader = job(email=f"sa-loaders-prod@{P}.iam.gserviceaccount.com", st="MERGE")
    assert audit(FakeSource(jobs=[loader, job(jt="LOAD", st=None, email=loader["user_email"])]))["status"] == "PASS"


def test_admin_activity_of_ae_is_fail():
    r = audit(FakeSource(activity=[{"protoPayload": {"methodName": "google.cloud.bigquery.v2.TableService.DeleteTable",
                                                     "authenticationInfo": {"principalEmail": SA}}}]))
    assert r["status"] == "FAIL" and r["by_type"] == {"ADMIN:google.cloud.bigquery.v2.TableService.DeleteTable": 1}


def test_watermark_never_seen_is_blocked_with_bounded_wait():
    src = FakeSource(watermark_after=10 ** 6)
    r = audit(src)
    assert r["status"] == "BLOCKED" and r["mutations"] is None and "зонд" in r["error"]
    assert src.list_calls == A.WATERMARK_TRIES and sum(src.sleeps) == (A.WATERMARK_TRIES - 1) * A.WATERMARK_SLEEP
    src = FakeSource(log_watermark_after=10 ** 6)          # задания видны, а Data Access не догнал — тоже BLOCKED
    assert audit(src)["status"] == "BLOCKED" and src.log_probe_calls == A.WATERMARK_TRIES


def test_watermark_late_but_seen_is_pass():
    src = FakeSource(watermark_after=2)
    assert audit(src)["status"] == "PASS" and src.list_calls == 3


def test_trusted_audit_waits_until_the_closed_window_ends_and_is_bounded():
    from tools.tests.ae_fixtures import closed_run
    w = A.run_window(closed_run())                                  # конец окна 13:03:58, NOW = 13:00:00
    src = FakeSource()
    run_audit(src, w, [SA], lambda: setattr(src, "label", "autonomy-audit-wm-abc") or src.label, settle=True)
    assert src.sleeps[0] == 238                                     # до конца окна, детерминированно
    src = FakeSource()                                              # черновой: не ждёт, окно считается открытым
    r = run_audit(src, w, [SA], lambda: setattr(src, "label", "autonomy-audit-wm-abc") or src.label)
    assert src.sleeps == [] and r["window"]["closed"] is False and r["window"]["end"] is None
    far = {**w, "end": "2026-09-27T14:00:00Z"}                      # дальше допустимой выдержки — не спим часами
    r = run_audit(FakeSource(), far, [SA], lambda: "x", settle=True)
    assert r["status"] == "BLOCKED" and "выдержки" in r["error"]


@pytest.mark.parametrize("src", [
    FakeSource(granted=["bigquery.tables.updateData"]),
    FakeSource(datasets={"wb_raw": {"location": "EU", "access": [{"role": "WRITER", "userByEmail": SA}]}}),
    FakeSource(datasets={"wb_raw": {"location": "EU", "access": [{"role": "OWNER", "userByEmail": SA}]}}),
    FakeSource(datasets={"wb_raw": {"location": "EU", "access": [{"role": "roles/bigquery.dataEditor", "iamMember": f"serviceAccount:{SA}"}]}}),
    FakeSource(datasets={"wb_raw": {"location": "EU", "access": [{"role": "WRITER", "groupByEmail": "team@x"}]}}),
    FakeSource(datasets={"wb_raw": {"location": "EU", "access": [{"role": "WRITER", "domain": "x.com"}]}}),
    FakeSource(datasets={"wb_raw": {"location": "EU", "access": [{"role": "WRITER", "specialGroup": "allAuthenticatedUsers"}]}}),
    FakeSource(datasets={"wb_raw": {"location": "EU", "access": [{"role": "WRITER", "iamMember": "principalSet://iam.googleapis.com/x"}]}}),
    FakeSource(table_policies=[f"projects/{P}/datasets/wb_raw/tables/RAW_WB_PRICES"],
               writable={("wb_raw", "RAW_WB_PRICES"): True}),
    FakeSource(table_policies=[f"projects/{P}/datasets/wb_raw/connections/x"]),
    FakeSource(created="2025-01-01T00:00:00Z"),
])
def test_unproven_iam_invariant_is_blocked_not_zero(src):
    r = audit(src)
    assert r["status"] == "BLOCKED" and r["mutations"] is None and r["iam_invariant"]
    assert not zero_mutations_proven(_run(r))[0]


def test_acl_entries_that_cannot_reach_ae_are_accepted():
    access = [{"role": "READER", "userByEmail": SA}, {"role": "WRITER", "specialGroup": "projectWriters"},
              {"role": "OWNER", "specialGroup": "projectOwners"}, {"role": "WRITER", "userByEmail": "loader@x"},
              {"role": "WRITER", "iamMember": "serviceAccount:other@x"}, {"view": {"tableId": "v"}}]
    assert acl_problems("wb_raw", access, [SA]) == []
    assert acl_problems(ANON, [{"role": "OWNER", "userByEmail": SA}], [SA]) == []


def test_table_policies_only_on_other_principals_or_deleted_tables_pass():
    src = FakeSource(table_policies=[f"projects/{P}/datasets/wb_raw/tables/RAW_WB_PRICES",
                                     f"projects/{P}/datasets/wb_raw/tables/GONE",
                                     f"projects/{P}/datasets/wb_mart/routines/F"],
                     writable={("wb_raw", "GONE"): None})
    r = audit(src)
    assert r["status"] == "PASS" and r["table_policies"] == 2


def test_found_mutation_beats_unproven_iam():
    r = audit(FakeSource(jobs=[job(st="INSERT")], granted=["bigquery.tables.updateData"]))
    assert r["status"] == "FAIL" and r["mutations"] == 1


def _run(r):
    from tools.autonomy.orchestrator import Orchestrator
    from tools.tests.ae_fixtures import closed_run
    o = Orchestrator.__new__(Orchestrator)
    o.now = lambda: NOW
    return o._merge_audit(closed_run(), r)


# ------------------------------------------------------------ источник и сбои ---
def test_legacy_or_unlabelled_evidence_is_not_zero():
    from tools.tests.ae_fixtures import clean_audit, closed_run
    run = _run(clean_audit(closed_run()))
    assert zero_mutations_proven(run)[0], zero_mutations_proven(run)
    for legacy in (_run(0), _run({"status": "PASS", "mutations": 0}),
                   _run({**clean_audit(closed_run()), "source": "jobs_list+audit_logs+iam_selftest+iam_history+ingress_attribution/v5"}),
                   _run({"status": "PASS", "mutations": 0, "source": "information_schema_region_eu/v1"})):
        ok, why = zero_mutations_proven(legacy)
        assert not ok and "источник аудита" in why


@pytest.mark.parametrize("exc", [RuntimeError("socket timeout"), KeyError("jobs"), ValueError("bad json"),
                                 urllib.error.URLError("reset")])
def test_any_source_failure_is_blocked(monkeypatch, exc):
    monkeypatch.setattr(A, "resolve_token", lambda *a: "t")

    def boom(*a, **k):
        raise exc
    monkeypatch.setattr(A, "run_audit", boom)
    r = A.count_mutations(P, "true", SINCE, [SA])
    assert r["status"] == "BLOCKED" and r["mutations"] is None and r["source"] == AUDIT_SOURCE


def test_no_identity_is_blocked():
    assert A.count_mutations(P, "true", SINCE, [])["status"] == "BLOCKED"


def test_jobs_list_is_global_and_fully_paginated():
    calls = []

    def http(method, url, body=None):
        calls.append(url)
        return {"jobs": [job()], "nextPageToken": "n"} if len(calls) < 3 else {"jobs": [job()]}
    src = A.GcpAuditSource(P, "t", http=http)
    assert len(src.jobs(1)) == 3
    assert all("location=" not in u and "allUsers=true" in u and "projection=full" in u for u in calls)
    endless = A.GcpAuditSource(P, "t", http=lambda *a, **k: {"jobs": [], "nextPageToken": "n"})
    with pytest.raises(RuntimeError, match="лимит страниц"):
        endless.jobs(1)


def test_table_self_check_http_semantics():
    def http_code(code):
        def http(method, url, body=None):
            raise urllib.error.HTTPError(url, code, "x", {}, io.BytesIO(b""))
        return http
    assert A.GcpAuditSource(P, "t", http=http_code(404)).table_can_write("d", "t") is None
    with pytest.raises(urllib.error.HTTPError):
        A.GcpAuditSource(P, "t", http=http_code(403)).table_can_write("d", "t")
    yes = A.GcpAuditSource(P, "t", http=lambda *a, **k: {"permissions": ["bigquery.tables.updateData"]})
    assert yes.table_can_write("d", "t") is True


def test_window_wait_is_applied_only_in_the_trusted_audit_command(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from tools.autonomy import cli
    from tools.tests.ae_fixtures import closed_run
    seen = []
    monkeypatch.setattr(A, "count_mutations", lambda *a, **k: seen.append((a[2], k.get("settle"))) or {"status": "PASS"})
    run = closed_run()
    for cmd, expected in (("audit", True), ("agent-run", False)):
        a = SimpleNamespace(cmd=cmd, project=P, token_command="true", audit_identity=[SA], engineer="none",
                            reviewer="none", sandbox_root=str(tmp_path), dry_run=False, pending_dir=None)
        orch = cli._orchestrator(a, cli.StateStore(tmp_path / "s"))
        orch.audit(run)
        assert seen[-1] == (run, expected)                          # окно выводится из состояния, не created→now


# ------------------------------------------------------------ ci-verify: привязка PR ---
from tools.autonomy.verification import pr_binding  # noqa: E402

SHA, BR, URL = "b" * 40, "ae/objective-x-1234abcd", "https://github.com/o/r/pull/9"
GOOD_PR = {"url": URL, "state": "OPEN", "isDraft": True, "baseRefName": "main", "headRefName": BR, "headRefOid": SHA,
           "isCrossRepository": False}


def test_bound_pr_passes():
    assert pr_binding(GOOD_PR, URL, SHA, BR) == []


@pytest.mark.parametrize("change", [{"isDraft": False}, {"baseRefName": "release"}, {"isCrossRepository": True},
                                    {"isCrossRepository": None}, {"headRefOid": "c" * 40}, {"headRefName": "ae/other"},
                                    {"state": "CLOSED"}, {"state": "MERGED"}, {"url": "https://github.com/o/r/pull/10"}])
def test_any_binding_violation_fails(change):
    assert pr_binding({**GOOD_PR, **change}, URL, SHA, BR)


def test_missing_pr_or_url_fails():
    assert pr_binding(None, URL, SHA, BR) and pr_binding(GOOD_PR, None, SHA, BR)


def test_green_ci_with_unbound_pr_blocks_instead_of_ready(tmp_path):
    """Все обязательные workflows зелёные на SHA, но head PR подменён → BLOCKED (UNSAFE), не READY_FOR_HUMAN_REVIEW."""
    from tools.autonomy.verification import evaluate
    from tools.tests.test_autonomy_ci_trust import Pipeline
    from tools.tests.test_autonomy_publication_audit import FakeGhPublisher, _ready_for_pr, _with_remote
    p = Pipeline(tmp_path)
    _with_remote(p)
    run_id = _ready_for_pr(p)
    from tools.tests import ae_fixtures as F

    def verifier(run):
        ver = run["verification"]
        return evaluate(ver["workflows"], F.ci_runs(run), ver["head_sha"], run["branch"], ver["dispatched_at"],
                        pr={**GOOD_PR, "url": run["pr_url"], "headRefName": run["branch"], "headRefOid": "d" * 40},
                        pr_url=run["pr_url"])
    run = p.orch(p.branch, publisher=FakeGhPublisher(p.repo), verifier=verifier,
                 audit=F.clean_audit).advance(run_id)
    assert run["state"] == "BLOCKED" and "не привязан" in run["transitions"][-1]["reason"]
    assert run["last_gate"]["verdict"] == "UNSAFE"


def test_verifier_without_binding_evidence_is_not_ready(tmp_path):
    from tools.tests.test_autonomy_ci_trust import Pipeline
    from tools.tests.test_autonomy_publication_audit import FakeGhPublisher, _ready_for_pr, _with_remote
    from tools.tests import ae_fixtures as F
    p = Pipeline(tmp_path)
    _with_remote(p)
    run_id = _ready_for_pr(p)
    legacy = lambda run: {"status": "PASS", "runs": [], "head_sha": run["verification"]["head_sha"]}  # noqa: E731
    run = p.orch(p.branch, publisher=FakeGhPublisher(p.repo), verifier=legacy, audit=F.clean_audit).advance(run_id)
    assert run["state"] == "BLOCKED"


def test_github_verifier_reads_the_run_pr(monkeypatch):
    from tools.autonomy import verification as V
    got = {}
    monkeypatch.setattr(V, "fetch_runs", lambda *a: [])
    monkeypatch.setattr(V, "fetch_pr", lambda repo, url: got.setdefault("url", url) and GOOD_PR)
    run = {"branch": BR, "pr_url": URL, "verification": {"head_sha": SHA, "dispatched_at": "2026-09-27T13:00:00Z"}}
    r = V.GitHubVerifier("o/r", ["ci.yml"])(run)
    assert got["url"] == URL and r["pr_binding"]["status"] == "PASS" and r["status"] == "PENDING"
    assert V.GitHubVerifier("o/r", ["ci.yml"])({**run, "pr_url": None})["status"] == "FAIL"


def test_ci_verify_job_reads_prs_but_cannot_write_them():
    import re
    from pathlib import Path
    text = (Path(__file__).resolve().parents[2] / ".github/workflows/autonomy-run.yml").read_text()
    block = text.split("\n  ci-verify:\n", 1)[1].split("\n  persist:\n", 1)[0]
    perms = block.split("permissions:", 1)[1].split("    env:", 1)[0]
    assert re.search(r"^\s+pull-requests: read\b", perms, re.M) and re.search(r"^\s+actions: read\b", perms, re.M)
    assert "write" not in perms.replace("contents: write", "")


# ------------------------------------------------------------ v3: делегирование, Data Access, SA, DCL ---
from tools.autonomy.audit import data_access_violation, delegated_from_ae, routing_problems  # noqa: E402

GH = "principal://iam.googleapis.com/projects/1/locations/global/workloadIdentityPools/github-pool/subject/repo:o/r"


def entry(svc="bigquery.googleapis.com", method="google.cloud.bigquery.v2.JobService.Query", eff=SA, chain=None,
          meta=None, request=None, authz=None):
    ai = {"principalEmail": eff} if "@" in eff else {"principalSubject": eff}
    if chain:
        ai["serviceAccountDelegationInfo"] = chain
    pp = {"serviceName": svc, "methodName": method, "authenticationInfo": ai}
    if meta is not None:
        pp["metadata"] = {"@type": "x", **meta}
    if request is not None:
        pp["request"] = request
    if authz is not None:
        pp["authorizationInfo"] = authz
    return {"protoPayload": pp}


@pytest.mark.parametrize("e,expected", [
    (entry(meta={"tableDataRead": {}}, chain=[{"principalSubject": GH}]), None),       # сам AE через WIF, чтение
    (entry(method="jobservice.jobcompleted"), None),
    (entry(method="google.cloud.bigquery.v2.JobService.InsertJob", meta={"jobInsertion": {}}), None),
    (entry(method="google.cloud.bigquery.v2.JobService.InsertJob", meta={"tableDataChange": {}}),
     "DATA:google.cloud.bigquery.v2.JobService.InsertJob:['tableDataChange']"),
    (entry(method="tabledata.insertAll"), "DATA:tabledata.insertAll:[]"),
    (entry(method="google.cloud.bigquery.v2.TableDataService.InsertAll"), "DATA:google.cloud.bigquery.v2.TableDataService.InsertAll:[]"),
    (entry(method="google.cloud.bigquery.storage.v1.BigQueryWrite.AppendRows"),
     "DATA:google.cloud.bigquery.storage.v1.BigQueryWrite.AppendRows:[]"),
    (entry(method="google.cloud.bigquery.v2.TableService.TestIamPermissions"), None),
    (entry(svc="iamcredentials.googleapis.com", method="GenerateAccessToken",
           request={"name": f"projects/-/serviceAccounts/{SA}"}), None),
    (entry(svc="iamcredentials.googleapis.com", method="GenerateAccessToken",
           request={"name": f"projects/-/serviceAccounts/{LOADER}"}), f"IMPERSONATION:{LOADER}"),
    (entry(svc="iamcredentials.googleapis.com", method="SignJwt", request={}), "IMPERSONATION:?"),
    (entry(eff=LOADER, chain=[{"firstPartyPrincipal": {"principalEmail": SA}}], meta={"tableDataChange": {}}),
     f"DELEGATED:bigquery.googleapis.com:{LOADER}"),
    (entry(eff=LOADER, chain=[{"principalSubject": GH}]), None),        # чужой WIF-workflow — не FAIL, см. ниже
    (entry(eff=LOADER, chain=[{"firstPartyPrincipal": {"principalEmail": "service-1@gcp-sa-cloudscheduler.iam.gserviceaccount.com"}}],
           meta={"tableDataChange": {}}), None),                                        # production, не AE
    (entry(eff=LOADER, meta={"tableDataChange": {}}), None),
    (entry(svc="storage.googleapis.com", method="storage.objects.create",
           authz=[{"permission": "storage.objects.create", "permissionType": "DATA_WRITE", "granted": True}]),
     "DATA:storage.googleapis.com:storage.objects.create"),
    (entry(svc="logging.googleapis.com", method="ReadLogEntries",
           authz=[{"permission": "logging.logEntries.list", "permissionType": "DATA_READ"}]), None),
    (entry(svc="pubsub.googleapis.com", method="Publish"), "DATA:pubsub.googleapis.com:Publish"),   # без authz — не чтение
])
def test_data_access_classes(e, expected):
    assert data_access_violation(e, [SA]) == expected


def test_impersonated_loader_dml_is_fail_although_jobs_list_shows_the_loader():
    """M1: AE имперсонирует загрузчик; jobs.list видит только загрузчик, но цепочка делегирования выдаёт AE."""
    dml = job(email=LOADER, st="MERGE")
    used = entry(eff=LOADER, chain=[{"firstPartyPrincipal": {"principalEmail": SA}}],
                 method="google.cloud.bigquery.v2.JobService.InsertJob", meta={"tableDataChange": {}})
    r = audit(FakeSource(jobs=[dml], access=[used]))
    assert r["status"] == "FAIL" and r["by_type"] == {f"DELEGATED:bigquery.googleapis.com:{LOADER}": 1}


def test_audit_log_filters_cover_delegation_and_federation():
    src = FakeSource()
    audit(src)
    window = [f for f in src.filters if f'timestamp>="{SINCE}"' in f and "authenticationInfo" in f]
    assert {("activity" in f, "data_access" in f) for f in window} >= {(True, False), (False, True)}
    for f in window:
        assert f'serviceAccountDelegationInfo.firstPartyPrincipal.principalEmail="{SA}"' in f
        assert 'serviceAccountDelegationInfo.principalSubject:"principal"' in f
        assert 'authenticationInfo.principalSubject:"principal"' in f
    assert not delegated_from_ae(entry(chain=[{"principalSubject": GH}]), [SA])      # сам AE — не «чужой»


OK_SINK = {"filter": A.DEFAULT_SINK_FILTER}


@pytest.mark.parametrize("routing,bad", [
    ({"sink": OK_SINK, "exclusions": [], "bucket": {"retentionDays": 30}}, False),
    ({"sink": {**OK_SINK, "exclusions": [{"name": "e", "disabled": True}]}, "exclusions": [], "bucket": {"retentionDays": 30}}, False),
    ({"sink": {**OK_SINK, "disabled": True}, "exclusions": [], "bucket": {"retentionDays": 30}}, True),
    ({"sink": {"filter": A.DEFAULT_SINK_FILTER + ' AND NOT protoPayload.methodName:"InsertAll"'}, "exclusions": [],
      "bucket": {"retentionDays": 30}}, True),                                     # сужение фильтра
    ({"sink": {"filter": 'NOT LOG_ID("cloudaudit.googleapis.com/data_access")'}, "exclusions": [],
      "bucket": {"retentionDays": 30}}, True),
    ({"sink": {**OK_SINK, "exclusions": [{"name": "bq"}]}, "exclusions": [], "bucket": {"retentionDays": 30}}, True),
    ({"sink": OK_SINK, "exclusions": [{"name": "legacy"}], "bucket": {"retentionDays": 30}}, True),
    ({"sink": {**OK_SINK, "destination": "logging.googleapis.com/projects/p/locations/global/buckets/other"},
      "exclusions": [], "bucket": {"retentionDays": 30}}, True),
    ({"sink": OK_SINK, "exclusions": [], "bucket": {"retentionDays": 1}}, True),  # окно прогона ~48 мин + сутки
    ({"sink": OK_SINK, "exclusions": [], "bucket": {}}, True),
])
def test_log_routing_must_deliver_data_access(routing, bad):
    assert bool(routing_problems(routing, timedelta(minutes=48))) is bad
    assert audit(FakeSource(routing=routing))["status"] == ("BLOCKED" if bad else "PASS")


def test_other_wif_workflow_in_window_is_blocked_not_a_sticky_fail():
    deploy = entry(eff=f"sa-deployer@{P}.iam.gserviceaccount.com", chain=[{"principalSubject": GH}],
                   svc="run.googleapis.com", method="google.cloud.run.v1.Jobs.ReplaceJob")
    r = audit(FakeSource(activity=[deploy], access=[entry(eff=LOADER, chain=[{"principalSubject": GH}],
                                                          meta={"tableDataChange": {}})]))
    assert r["status"] == "BLOCKED" and r["mutations"] is None and any("атрибуция" in x for x in r["iam_invariant"])
    assert _run(r)["production_mutations"] == 0
    ae = entry(eff=LOADER, chain=[{"firstPartyPrincipal": {"principalEmail": SA}}], svc="run.googleapis.com",
               method="google.cloud.run.v1.Jobs.RunJob")
    assert audit(FakeSource(activity=[ae]))["status"] == "FAIL"                     # от AE — FAIL, как прежде


def test_foreign_project_sa_with_roles_here_is_self_checked():
    other = "robot@other-project.iam.gserviceaccount.com"
    ev = list(SA_POLICIES) + [pol("run.googleapis.com", RUN_SVC, [{"role": "roles/run.invoker",
                                                                  "members": [f"serviceAccount:{other}"]}])]
    src = FakeSource(iam_events=ev, sa_granted={other: ["iam.serviceAccounts.getAccessToken"]})
    r = audit(src)
    assert other in src.sa_checked and r["status"] == "BLOCKED"


def test_every_project_sa_is_self_checked_and_any_impersonation_right_blocks():
    src = FakeSource()
    assert audit(src)["status"] == "PASS" and LOADER in src.sa_checked and SA in src.sa_checked
    r = audit(FakeSource(sa_granted={LOADER: ["iam.serviceAccounts.getAccessToken"]}))
    assert r["status"] == "BLOCKED" and any(LOADER in x for x in r["iam_invariant"])


def test_sa_inventory_must_be_complete():
    events = SA_CREATED + [{"protoPayload": {}}]
    assert audit(FakeSource(sa_events=events))["status"] == "BLOCKED"
    failed = SA_CREATED + [{"protoPayload": {"status": {"code": 6}}}]
    assert audit(FakeSource(sa_events=failed))["status"] == "PASS"            # неудачное создание — SA нет


def tbl(ds, name, kind, ts):
    md = {"tableCreation": {"table": {"tableName": f"projects/{P}/datasets/{ds}/tables/{name}"}}} if kind == "create" else \
        {"tableDeletion": {"table": {"tableName": f"projects/{P}/datasets/{ds}/tables/{name}"}}}
    return {"timestamp": ts, "protoPayload": {"serviceName": "bigquery.googleapis.com",
                                              "methodName": "google.cloud.bigquery.v2.JobService.InsertJob",
                                              "resourceName": f"projects/{P}/datasets/{ds}/tables/{name}", "metadata": md}}


def test_every_existing_table_is_self_checked_whatever_granted_the_right():
    """Право updateData, выданное как угодно (DCL GRANT из чужого проекта, незаписанная политика), видно на
    самопроверке каждой существующей таблицы — даже в датасете, невидимом AE."""
    events = [tbl("evetis_communications", "REPLIES", "create", "2026-08-01T00:00:00Z"),
              tbl("wb_mart", "TMP", "create", "2026-08-01T00:00:00Z"), tbl("wb_mart", "TMP", "delete", "2026-08-02T00:00:00Z"),
              tbl("wb_mart", "BACK", "delete", "2026-08-01T00:00:00Z"), tbl("wb_mart", "BACK", "create", "2026-08-03T00:00:00Z"),
              tbl(ANON, "anon", "create", "2026-08-01T00:00:00Z"),
              tbl("_script" + "a" * 40, "_res", "create", "2026-08-01T00:00:00Z")]
    src = FakeSource(table_events=list(reversed(events)))       # журнал не обязан отдавать по времени
    assert audit(src)["status"] == "PASS"
    assert set(src.tables_tested) == {("evetis_communications", "REPLIES"), ("wb_mart", "BACK")}
    r = audit(FakeSource(table_events=events, writable={("evetis_communications", "REPLIES"): True}))
    assert r["status"] == "BLOCKED" and any("REPLIES" in x for x in r["iam_invariant"])


def test_unreachable_regions_are_not_silently_skipped():
    src = A.GcpAuditSource(P, "t", http=lambda *a, **k: {"jobs": [], "unreachable": ["asia-south2"]})
    with pytest.raises(RuntimeError, match="недоступны регионы"):
        src.jobs(1)
    ds = A.GcpAuditSource(P, "t", http=lambda *a, **k: {"datasets": [], "unreachable": ["asia-south2"]})
    with pytest.raises(RuntimeError, match="недоступны регионы"):
        ds.datasets()


def test_failed_parent_with_children_is_not_read_only():
    j = job(st=None, state="DONE", err={"reason": "invalidQuery"})
    j["statistics"]["numChildJobs"] = "3"
    assert job_mutation(j, P) == "QUERY:None"


def test_acl_checked_for_every_ae_identity():
    other = f"sa-ae-second@{P}.iam.gserviceaccount.com"
    assert acl_problems("wb_raw", [{"role": "WRITER", "userByEmail": other}], [SA, other])


def test_foreign_repo_pr_url_is_refused_and_never_fetched(monkeypatch):
    from tools.autonomy import verification as V
    fetched = []
    monkeypatch.setattr(V, "fetch_runs", lambda *a: [])
    monkeypatch.setattr(V, "fetch_pr", lambda repo, url: fetched.append(url) or GOOD_PR)
    run = {"branch": BR, "pr_url": "https://github.com/attacker/r/pull/9",
           "verification": {"head_sha": SHA, "dispatched_at": "2026-09-27T13:00:00Z"}}
    r = V.GitHubVerifier("o/r", ["ci.yml"])(run)
    assert r["status"] == "FAIL" and fetched == [] and "не PR репозитория" in r["pr_binding"]["problems"][0]
    assert pr_binding({**GOOD_PR, "url": "https://github.com/o/r/pull/9x"}, "https://github.com/o/r/pull/9x",
                      SHA, BR, repo="o/r")


@pytest.mark.parametrize("v,expected", [(0.25, 0.25), (1, 1.0), (float("nan"), None), (float("inf"), None),
                                        (True, None), ("0.3", None), (None, None)])
def test_cost_must_be_finite(v, expected):
    assert A.finite_cost(v) == expected


def test_report_ignores_non_finite_cost(tmp_path):
    import json as _json
    from tools.autonomy.report import _usage_cost
    (tmp_path / "diagnostics").mkdir()
    (tmp_path / "diagnostics" / "r.json").write_text(_json.dumps({"invocations": [
        {"role": "reviewer", "total_cost_usd": float("nan")}, {"role": "reviewer", "total_cost_usd": 0.1}]}))
    assert _usage_cost({"role": "reviewer", "diagnostics": {"file": "diagnostics/r.json"}}, tmp_path) == 0.1
    assert _usage_cost({"role": "reviewer", "total_cost_usd": float("nan"),
                        "diagnostics": {"file": "diagnostics/r.json"}}, tmp_path) == 0.1


# ------------------------------------------------------------ v4: IAM всех ресурсов и WIF по истории ---
def pol(svc, rn, bindings, ts="2026-09-01T00:00:00Z", **pp):
    return {"timestamp": ts, "protoPayload": {"serviceName": svc, "resourceName": rn,
                                              "request": {"policy": {"bindings": bindings}}, **pp}}


def delta(rn, action, role, member, ts):
    return {"timestamp": ts, "protoPayload": {"serviceName": "storage.googleapis.com", "resourceName": rn,
                                              "methodName": "storage.setIamPermissions", "serviceData": {
                                                  "policyDelta": {"bindingDeltas": [{"action": action, "role": role,
                                                                                     "member": member}]}}}}


RUN_SVC = f"projects/{P}/locations/europe-west1/services/evetis-wb-communications"


@pytest.mark.parametrize("extra,blocked", [
    ([pol("run.googleapis.com", RUN_SVC, [{"role": "roles/run.invoker", "members": ["allUsers"]}])], True),  # F-18
    ([pol("storage.googleapis.com", "projects/_/buckets/b", [{"role": "roles/storage.objectViewer",
                                                              "members": ["allUsers"]}])], True),         # чтение объектов:
    # storage.objects.get/list запрещены AE с F-18 (путь к Terraform state) — «читающая» роль раскрывается до прав
    ([delta("projects/_/buckets/b", "ADD", "roles/storage.objectAdmin", "group:team@x", "2026-09-01T00:00:00Z")], True),
    ([delta("projects/_/buckets/b", "ADD", "roles/storage.objectAdmin", "group:team@x", "2026-09-01T00:00:00Z"),
      delta("projects/_/buckets/b", "REMOVE", "roles/storage.objectAdmin", "group:team@x", "2026-09-02T00:00:00Z")], False),
    ([pol("pubsub.googleapis.com", f"projects/{P}/topics/t", [{"role": "roles/pubsub.publisher",
                                                               "members": [f"serviceAccount:{SA}"]}])], True),
    ([pol("cloudresourcemanager.googleapis.com", f"projects/{P}", [{"role": "roles/logging.viewer",
                                                                    "members": [f"serviceAccount:{SA}"]}])], False),
    ([pol("run.googleapis.com", RUN_SVC, [{"role": "roles/run.invoker", "members": [
        "principalSet://iam.googleapis.com/projects/1/locations/global/workloadIdentityPools/github-pool/attribute.repository/o/r"]}])], True),
    ([{"timestamp": "2026-09-01T00:00:00Z", "protoPayload": {"serviceName": "pubsub.googleapis.com",
                                                             "resourceName": "t", "methodName": "SetIamPolicy"}}], True),
    ([{"timestamp": "2026-09-01T00:00:00Z", "protoPayload": {"serviceName": "pubsub.googleapis.com",
                                                             "resourceName": "t", "status": {"code": 7}}}], False),
    ([pol("secretmanager.googleapis.com", "s", [{"role": "roles/secretmanager.secretAccessor",
                                                 "members": [LOADER], "condition": {"expression": "true"}}])], True),
])
def test_iam_state_of_every_resource(extra, blocked):
    r = audit(FakeSource(iam_events=list(SA_POLICIES) + extra))
    assert r["status"] == ("BLOCKED" if blocked else "PASS"), r["iam_invariant"]


def test_latest_full_policy_wins():
    grant = pol("run.googleapis.com", RUN_SVC, [{"role": "roles/run.invoker", "members": ["allUsers"]}], ts="2026-09-01T00:00:00Z")
    revoke = pol("run.googleapis.com", RUN_SVC, [{"role": "roles/run.invoker", "members": [f"serviceAccount:{LOADER}"]}],
                 ts="2026-09-02T00:00:00Z")
    assert audit(FakeSource(iam_events=list(SA_POLICIES) + [revoke, grant]))["status"] == "PASS"


def _deployer_uid():
    return next(e["protoPayload"]["response"]["unique_id"] for e in SA_CREATED
                if e["protoPayload"]["response"]["email"].startswith("sa-deployer@"))


def test_wif_reconstruction_passes_the_s1_evaluator_and_detects_a_leak():
    assert audit(FakeSource())["status"] == "PASS"
    leak = pol("iam.googleapis.com", f"projects/-/serviceAccounts/{_deployer_uid()}", [{
        "role": "roles/iam.workloadIdentityUser", "members": [
            "principal://iam.googleapis.com/projects/1/locations/global/workloadIdentityPools/github-pool/subject/"
            "repo:evelagin/evetis-wb-analytics:ref:refs/heads/main"]}], ts="2026-09-26T00:00:00Z")
    r = audit(FakeSource(iam_events=list(SA_POLICIES) + [leak]))
    assert r["status"] == "BLOCKED" and any(x.startswith("WIF: инвариант S1") for x in r["iam_invariant"])


def test_wif_provider_update_mask_is_applied():
    upd = {"timestamp": "2026-09-26T00:00:00Z", "protoPayload": {
        "methodName": "google.iam.admin.v1.WorkloadIdentityPools.UpdateWorkloadIdentityPoolProvider",
        "request": {"updateMask": "attributeCondition", "workloadIdentityPoolProvider": {
            "name": f"projects/{P}/locations/global/workloadIdentityPools/github-pool/providers/github-provider",
            "attributeCondition": "assertion.repository_owner == \"someone-else\""}}}}
    r = audit(FakeSource(wip=WIP + [upd]))
    assert r["status"] == "BLOCKED" and any("WIF" in x for x in r["iam_invariant"])


@pytest.mark.parametrize("wip_extra,iam_extra", [
    ([{"timestamp": "2026-09-26T00:00:00Z", "protoPayload": {
        "methodName": "google.iam.admin.v1.WorkloadIdentityPools.DeleteWorkloadIdentityPoolProvider", "request": {"name": "x"}}}], []),
    ([], [pol("iam.googleapis.com", "projects/-/serviceAccounts/999999", [{"role": "roles/iam.workloadIdentityUser", "members": [
        "principalSet://iam.googleapis.com/projects/1/locations/global/workloadIdentityPools/github-pool/attribute.repository/x"]}])]),
])
def test_unmodelled_wif_state_is_blocked(wip_extra, iam_extra):
    r = audit(FakeSource(wip=WIP + wip_extra, iam_events=list(SA_POLICIES) + iam_extra))
    assert r["status"] == "BLOCKED", r["iam_invariant"]


def test_member_of_another_pool_is_not_modelled_as_github():
    """principalSet другого пула оценщик GitHub-провайдера не моделирует — отказ, даже если S1 проходит."""
    uid = _deployer_uid()
    base = next(e for e in SA_POLICIES if e["protoPayload"]["resourceName"].endswith(f"/{uid}"))
    members = base["protoPayload"]["request"]["policy"]["bindings"][0]["members"] + [
        "principalSet://iam.googleapis.com/projects/2/locations/global/workloadIdentityPools/tenant-pool/attribute.repository/x"]
    ev = [e for e in SA_POLICIES if e is not base] + [pol("iam.googleapis.com", f"projects/-/serviceAccounts/{uid}", [
        {"role": "roles/iam.workloadIdentityUser", "members": members}], ts="2026-09-26T00:00:00Z")]
    r = audit(FakeSource(iam_events=ev))
    assert r["status"] == "BLOCKED" and any("другого пула" in x for x in r["iam_invariant"])
    assert not any(x.startswith("WIF: инвариант S1") for x in r["iam_invariant"])


def test_missing_provider_history_is_blocked():
    r = audit(FakeSource(wip=[]))
    assert r["status"] == "BLOCKED" and any("провайдер github-provider" in x for x in r["iam_invariant"])


# ------------------------------------------------------------ финальное ревью: модель WIF и атрибуция ---
def _deployer_policy_with(members_extra=(), role="roles/iam.workloadIdentityUser"):
    uid = _deployer_uid()
    base = next(e for e in SA_POLICIES if e["protoPayload"]["resourceName"].endswith(f"/{uid}"))
    binds = [dict(b) for b in base["protoPayload"]["request"]["policy"]["bindings"]]
    if role == "roles/iam.workloadIdentityUser":
        binds[0] = {**binds[0], "members": binds[0]["members"] + list(members_extra)}
    else:
        binds.append({"role": role, "members": list(members_extra)})
    return [e for e in SA_POLICIES if e is not base] + [pol("iam.googleapis.com", f"projects/-/serviceAccounts/{uid}",
                                                           binds, ts="2026-09-26T00:00:00Z")]


POOL = "principalSet://iam.googleapis.com/projects/1/locations/global/workloadIdentityPools/github-pool"


@pytest.mark.parametrize("ev,needle", [
    (_deployer_policy_with([f"{POOL}/*"]), "вне модели S1"),                       # весь пул
    (_deployer_policy_with([f"{POOL}/attribute.repository/evelagin/evetis-wb-analytics"],
                           role="projects/p/roles/tokenMinter"), "оценщик S1 её не учитывает"),
])
def test_federated_members_outside_the_s1_model_are_blocked(ev, needle):
    r = audit(FakeSource(iam_events=ev))
    assert r["status"] == "BLOCKED" and any(needle in x for x in r["iam_invariant"]), r["iam_invariant"]


def test_wif_check_itself_no_longer_drops_unmodelled_bindings():
    from tools.autonomy import wif_check
    doc = {"provider": {"attributeCondition": "true", "attributeMapping": {}},
           "service_account_bindings": {"sa-deployer": [{"role": "roles/iam.workloadIdentityUser",
                                                         "members": [f"{POOL}/*"]}]}}
    with pytest.raises(ValueError, match="вне модели"):
        wif_check.from_snapshot(doc, "t")
    doc["service_account_bindings"]["sa-deployer"] = [{"role": "roles/owner", "members": [f"{POOL}/attribute.x/y"]}]
    with pytest.raises(ValueError, match="не моделирует"):
        wif_check.from_snapshot(doc, "t")


@pytest.mark.parametrize("upd,needle", [
    ({"updateMask": "attribute_condition", "workloadIdentityPoolProvider": {"attribute_condition": "true"}}, "updateMask"),
    ({"updateMask": "oidc", "workloadIdentityPoolProvider": {"oidc": {"issuerUri": "https://evil.example"}}}, "issuer"),
    ({"updateMask": "disabled", "workloadIdentityPoolProvider": {"disabled": True}}, "updateMask"),
])
def test_unmodelled_provider_updates_are_blocked(upd, needle):
    upd["workloadIdentityPoolProvider"]["name"] = (f"projects/{P}/locations/global/workloadIdentityPools/github-pool/"
                                                   "providers/github-provider")
    e = {"timestamp": "2026-09-26T00:00:00Z", "protoPayload": {
        "methodName": "google.iam.admin.v1.WorkloadIdentityPools.UpdateWorkloadIdentityPoolProvider", "request": upd}}
    r = audit(FakeSource(wip=WIP + [e]))
    assert r["status"] == "BLOCKED" and any(needle in x for x in r["iam_invariant"]), r["iam_invariant"]


def test_federated_subject_logged_under_sa_name_is_not_dropped():
    e = {"protoPayload": {"serviceName": "run.googleapis.com", "methodName": "x", "authenticationInfo": {
        "principalEmail": f"sa-deployer@{P}.iam.gserviceaccount.com", "principalSubject": GH}}}
    assert A.attribution(e, [SA]) == "federated_other"
    r = audit(FakeSource(activity=[e]))
    assert r["status"] == "BLOCKED" and any("атрибуция" in x for x in r["iam_invariant"])


def test_matched_but_unattributed_entry_is_blocked_not_ignored():
    e = {"protoPayload": {"serviceName": "bigquery.googleapis.com", "methodName": "x",
                          "authenticationInfo": {"principalEmail": LOADER}}}
    assert A.attribution(e, [SA]) is None
    assert audit(FakeSource(access=[e]))["status"] == "BLOCKED"
    assert audit(FakeSource(activity=[e]))["status"] == "BLOCKED"


def test_custom_role_named_viewer_is_not_trusted_as_read():
    ev = list(SA_POLICIES) + [pol("storage.googleapis.com", "projects/_/buckets/b", [
        {"role": f"projects/{P}/roles/dataViewer", "members": ["allAuthenticatedUsers"]}])]
    assert audit(FakeSource(iam_events=ev))["status"] == "BLOCKED"


def test_event_order_uses_numeric_fractions():
    early = pol("run.googleapis.com", RUN_SVC, [{"role": "roles/run.invoker", "members": ["allUsers"]}],
                ts="2026-09-01T00:00:00Z")
    late = pol("run.googleapis.com", RUN_SVC, [{"role": "roles/run.invoker", "members": [f"serviceAccount:{LOADER}"]}],
               ts="2026-09-01T00:00:00.5Z")
    # строкой "…00Z" > "…00.5Z" ('Z' > '.'), но по времени публичная политика раньше и уже заменена
    assert audit(FakeSource(iam_events=list(SA_POLICIES) + [late, early]))["status"] == "PASS"
    assert A._order({"timestamp": "2026-09-01T00:00:00.9Z"}) > A._order({"timestamp": "2026-09-01T00:00:00.123456789Z"})


def test_wif_condition_binding_is_blocked():
    ev = _deployer_policy_with()
    ev[-1]["protoPayload"]["request"]["policy"]["bindings"][0]["condition"] = {"expression": "true"}
    r = audit(FakeSource(iam_events=ev))
    assert r["status"] == "BLOCKED" and any("IAM-условия" in x for x in r["iam_invariant"])


def test_wif_evaluator_refusal_is_blocked_not_an_exception(monkeypatch):
    """Страховка: если оценщик S1 откажется моделировать конфигурацию, аудит — BLOCKED, а не исключение."""
    from tools.autonomy import wif_check

    def refuse(doc, source):
        raise ValueError("вне модели")
    monkeypatch.setattr(wif_check, "from_snapshot", refuse)
    r = audit(FakeSource())
    assert r["status"] == "BLOCKED" and any(x.startswith("WIF: инвариант S1") for x in r["iam_invariant"])


def test_iam_check_requires_readable_clean_org_policy():
    from tools.tests.test_autonomy_iam import SNAP
    from tools.autonomy import iam_check
    base = iam_check.evaluate(SNAP)
    bad = iam_check.evaluate({**SNAP, "org_risky_members": ["roles/iam.workloadIdentityUser principalSet://x"]})
    unread = iam_check.evaluate({**SNAP, "org_policy_readable": False})
    assert bad["status"] == "FAIL" and unread["status"] == "FAIL"
    assert any("организации" in f for f in bad["findings"]) and any("организации" in f for f in unread["findings"])
    assert not any("организации" in f for f in base["findings"])        # старые снимки без полей — без находки


def test_wif_resources_are_matched_by_full_path_not_short_name():
    """Одноимённые пул/провайдер в другом проекте или втором пуле — не наши: отказ, а не подмена модели."""
    other = {"timestamp": "2026-09-26T00:00:00Z", "protoPayload": {
        "methodName": "google.iam.admin.v1.WorkloadIdentityPools.CreateWorkloadIdentityPoolProvider",
        "request": {"workloadIdentityPoolProviderId": "github-provider",
                    "parent": "projects/other/locations/global/workloadIdentityPools/github-pool",
                    "workloadIdentityPoolProvider": {"attributeCondition": "true", "attributeMapping": {},
                                                     "oidc": {"issuerUri": A.GITHUB_ISSUER}}}}}
    r = audit(FakeSource(wip=WIP + [other]))
    assert r["status"] == "BLOCKED" and any("не моделирует: CreateWorkloadIdentityPoolProvider" in x for x in r["iam_invariant"])
    assert not any(x.startswith("WIF: инвариант S1") for x in r["iam_invariant"])   # модель не подменена
    pool2 = {"timestamp": "2026-09-26T00:00:00Z", "protoPayload": {
        "methodName": "google.iam.admin.v1.WorkloadIdentityPools.CreateWorkloadIdentityPool",
        "request": {"workloadIdentityPoolId": "github-pool-2", "parent": f"projects/{P}/locations/global"}}}
    assert audit(FakeSource(wip=WIP + [pool2]))["status"] == "BLOCKED"
    same_name_other_project = _deployer_policy_with([
        "principalSet://iam.googleapis.com/projects/2/locations/global/workloadIdentityPools/github-pool/attribute.repository/x"])
    r = audit(FakeSource(iam_events=same_name_other_project))
    assert r["status"] == "BLOCKED" and any("другого пула" in x for x in r["iam_invariant"])


def test_pool_prefix_must_lead_the_member_not_hide_inside_it():
    smuggled = ("principalSet://iam.googleapis.com/projects/2/locations/global/workloadIdentityPools/x/attribute.a/"
                "://iam.googleapis.com/projects/1/locations/global/workloadIdentityPools/github-pool/")
    r = audit(FakeSource(iam_events=_deployer_policy_with([smuggled])))
    assert r["status"] == "BLOCKED" and any("другого пула" in x for x in r["iam_invariant"])


def test_expired_token_is_refreshed_once_then_fails_closed(monkeypatch):
    import urllib.request
    calls, tokens = [], iter(["t2", "t3"])

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        calls.append(req.get_header("Authorization"))
        if req.get_header("Authorization") == "Bearer t1":
            raise urllib.error.HTTPError(req.full_url, 401, "x", {}, io.BytesIO(b""))
        return Resp(b'{"createTime": "2026-07-10T00:00:00Z"}')
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    src = A.GcpAuditSource(P, "t1", refresh=lambda: next(tokens))
    assert src.project_created() == "2026-07-10T00:00:00Z" and calls == ["Bearer t1", "Bearer t2"]
    stale = A.GcpAuditSource(P, "t1", refresh=lambda: "t1")            # обновление не помогло — исключение (BLOCKED)
    with pytest.raises(urllib.error.HTTPError):
        stale.project_created()



# ------------------------------------------------------------ F-18: атрибуция публичного входа (D-19b) ---
F18_RN = f"projects/{P}/locations/europe-west1/services/evetis-wb-communications"
F18_SA = f"evetis-wb-comms@{P}.iam.gserviceaccount.com"
REV = "evetis-wb-communications-00031-abc"
TR1, TR2 = "a" * 32, "b" * 32


def f18_iam(extra_members=(), sa_extra=()):
    ev = [pol("run.googleapis.com", F18_RN, [{"role": "roles/run.invoker", "members": ["allUsers", *extra_members]}]),
          pol("cloudresourcemanager.googleapis.com", f"projects/{P}", [
              {"role": "roles/bigquery.jobUser", "members": [f"serviceAccount:{F18_SA}"]},
              {"role": "roles/datastore.user", "members": [f"serviceAccount:{F18_SA}"]},
              *[{"role": r, "members": [f"serviceAccount:{F18_SA}"]} for r in sa_extra]])]
    for n in ("EVETIS_ADMIN_TOKEN", "EVETIS_OPENAI_API_KEY", "EVETIS_SCHEDULER_SECRET", "EVETIS_TELEGRAM_BOT_TOKEN",
              "EVETIS_TELEGRAM_WEBHOOK_SECRET", "EVETIS_WB_API_TOKEN"):
        ev.append(pol("secretmanager.googleapis.com", f"projects/1/secrets/{n}",
                      [{"role": "roles/secretmanager.secretAccessor", "members": [f"serviceAccount:{F18_SA}"]}]))
    return list(SA_POLICIES) + ev


def svc_event(sa=F18_SA, ingress="all", method="google.cloud.run.v1.Services.ReplaceService", timeout=300,
              ts="2026-09-28T08:30:00Z", name="evetis-wb-communications", location="europe-west1", project=P,
              generation=30, image="img@sha256:" + "a" * 64, invoker=None, dry_run=None, status=None, prev=None):
    ann = {"run.googleapis.com/ingress": ingress}
    if invoker is not None:
        ann["run.googleapis.com/invoker-iam-disabled"] = invoker
    request = {"service": {
        "metadata": {"annotations": ann, "generation": generation},
        "spec": {"template": {"spec": {"serviceAccountName": sa, "timeoutSeconds": timeout,
                                        "containers": [{"image": image}]}}},
        "status": {"latestCreatedRevisionName": prev} if prev else {}}}
    if dry_run is not None:
        request["dryRun"] = dry_run
    pp = {"methodName": method, "resourceName": f"namespaces/{project}/services/{name}", "request": request}
    if status:
        pp["status"] = {"code": status}
    return {"timestamp": ts, "resource": {"labels": {"location": location, "project_id": project, "service_name": name}},
            "protoPayload": pp}


def _run_labels(rev):
    return {"revision_name": rev, "project_id": P, "location": "europe-west1", "service_name": "evetis-wb-communications"}


def req(route, status, trace, rev=REV, ts="2026-09-28T12:00:00Z"):
    return {"timestamp": ts, "trace": f"projects/{P}/traces/{trace}" if trace else "",
            "resource": {"labels": _run_labels(rev)},
            "httpRequest": {"requestUrl": f"https://svc.run.app{route}", "status": status, "latency": "1.2s",
                            "requestMethod": "GET" if route == "/health" else "POST"}}


def ev(kind, trace, rev=REV, **fields):
    fields.setdefault("result", "ok")
    jp = {"audit_event": kind, "audit_schema": "wbc-audit/1", "service": "evetis-wb-communications",
          "revision": rev, "trace_id": trace, "request_id": fields.pop("request_id", "rid-" + (trace or "none")[:6]), **fields}
    return {"timestamp": "2026-09-28T12:00:00.5Z", "trace": f"projects/{P}/traces/{trace}" if trace else "",
            "resource": {"labels": _run_labels(rev)}, "jsonPayload": jp}


def end(trace, route, status, attempts, **kw):
    return ev("request_end", trace, route=route, http_status=status, mutation_attempts=attempts, **kw)


def good_chain():
    return {
        "requests": [req("/telegram-webhook", 200, TR1), req("/poll", 200, TR2), req("/health", 200, "c" * 32)],
        "events": [
            ev("auth_ok", TR1, route="/telegram-webhook", mechanism="telegram_secret_token"),
            ev("auth_ok", TR1, route="/telegram-webhook", mechanism="telegram_allowlist"),
            ev("mutation_attempt", TR1, mutation_class="wb_write:PATCH:/api/v1/questions", target_system="wildberries", target_ref="r1"),
            ev("mutation_success", TR1, mutation_class="wb_write:PATCH:/api/v1/questions", target_system="wildberries", target_ref="r1"),
            ev("mutation_attempt", TR1, mutation_class="telegram:editMessageText", target_system="telegram"),
            ev("mutation_success", TR1, mutation_class="telegram:editMessageText", target_system="telegram"),
            ev("auth_ok", TR2, route="/poll", mechanism="scheduler_shared_secret"),
            ev("mutation_attempt", TR2, mutation_class="telegram:sendMessage", target_system="telegram"),
            ev("mutation_failure", TR2, mutation_class="telegram:sendMessage", target_system="telegram"),
            end(TR1, "/telegram-webhook", 200, 2), end(TR2, "/poll", 200, 1), end("c" * 32, "/health", 200, 0),
        ],
        "ready": {REV}, "svc_events": [svc_event()]}


def f18_audit(chain=None, **iam_kw):
    return audit(FakeSource(iam_events=f18_iam(**iam_kw), f18=chain if chain is not None else good_chain()))


def test_f18_full_attribution_chain_exempts_only_this_binding():
    r = f18_audit()
    assert r["status"] == "PASS", r["iam_invariant"]


def mutate(chain, **changes):
    c = {k: (list(v) if isinstance(v, list) else v) for k, v in chain.items()}
    c.update(changes)
    return c


@pytest.mark.parametrize("change,status", [
    ({"ready": set()}, "BLOCKED"),                                            # инструментирования не было
    ({"svc_events": [svc_event(sa="other@x.iam.gserviceaccount.com")]}, "BLOCKED"),   # сменён runtime SA
    ({"svc_events": [svc_event(ingress="internal")]}, "BLOCKED"),
    ({"svc_events": []}, "BLOCKED"),                                          # спецификация не восстановлена
    ({"svc_events": [svc_event(method="google.cloud.run.v2.Services.UpdateService")]}, "BLOCKED"),
])
def test_f18_configuration_and_instrumentation_must_be_proven(change, status):
    assert f18_audit(mutate(good_chain(), **change))["status"] == status


def test_f18_expanded_sa_permissions_block():
    assert f18_audit(sa_extra=["roles/bigquery.dataEditor"])["status"] == "BLOCKED"


def test_f18_additional_public_member_on_service_blocks():
    assert f18_audit(extra_members=["allAuthenticatedUsers"])["status"] == "BLOCKED"


def test_other_public_cloud_run_service_is_not_exempt():
    other = f"projects/{P}/locations/europe-west1/services/some-other-service"
    ev_ = f18_iam() + [pol("run.googleapis.com", other, [{"role": "roles/run.invoker", "members": ["allUsers"]}])]
    r = audit(FakeSource(iam_events=ev_, f18=good_chain()))
    assert r["status"] == "BLOCKED" and any("some-other-service" in x for x in r["iam_invariant"])


@pytest.mark.parametrize("route", ["/poll", "/telegram-webhook"])
def test_f18_successful_protected_request_without_auth_ok_is_never_pass(route):
    c = good_chain()
    c["requests"] = c["requests"] + [req(route, 200, "d" * 32)]            # IP Telegram не важен: нет auth_ok
    r = f18_audit(c)                                   # без request_end приложения — доказательство неполно: BLOCKED
    assert r["status"] == "BLOCKED" and r["mutations"] is None
    c["events"] = c["events"] + [end("d" * 32, route, 200, 0)]            # приложение подтвердило маршрут — FAIL
    r = f18_audit(c)
    assert r["status"] == "FAIL" and any("без auth_ok" in k for k in r["by_type"])


def test_f18_telegram_ip_or_http_200_is_not_authentication():
    c = good_chain()
    c["events"] = [e for e in c["events"] if not (e["jsonPayload"]["audit_event"] == "auth_ok"
                                                  and e["jsonPayload"]["mechanism"] == "telegram_secret_token")]
    assert f18_audit(c)["status"] == "FAIL"


@pytest.mark.parametrize("bad", [
    ev("mutation_attempt", "", mutation_class="telegram:sendMessage", target_system="telegram"),        # вне запроса
    ev("mutation_attempt", "c" * 32, mutation_class="telegram:sendMessage", target_system="telegram"),  # на /health
    ev("mutation_attempt", "e" * 32, mutation_class="telegram:sendMessage", target_system="telegram"),  # нет запроса
])
def test_f18_unexplained_downstream_mutation_is_never_pass(bad):
    c = good_chain()
    c["events"] = c["events"] + [bad]
    assert f18_audit(c)["status"] in ("FAIL", "BLOCKED")


def test_f18_wb_mutation_requires_allowlist_as_well():
    c = good_chain()
    c["events"] = [e for e in c["events"] if e["jsonPayload"].get("mechanism") != "telegram_allowlist"]
    r = f18_audit(c)
    assert r["status"] == "FAIL" and any("telegram_allowlist" in k for k in r["by_type"])


def test_f18_wb_mutation_from_poll_is_outside_policy():
    c = good_chain()
    c["events"] = c["events"] + [ev("mutation_attempt", TR2, mutation_class="wb_write:POST:/x", target_system="wildberries"),
                                 ev("mutation_success", TR2, mutation_class="wb_write:POST:/x", target_system="wildberries")]
    assert f18_audit(c)["status"] == "FAIL"


def test_f18_unknown_result_duplicate_trace_revision_mismatch_block():
    c = good_chain()
    c["events"] = [e for e in c["events"] if not (e["jsonPayload"]["audit_event"] == "mutation_success"
                                                  and e["jsonPayload"]["target_system"] == "wildberries")]
    assert f18_audit(c)["status"] == "BLOCKED"
    c = good_chain(); c["requests"] = c["requests"] + [req("/health", 200, TR1)]
    assert f18_audit(c)["status"] == "BLOCKED"
    c = good_chain(); c["events"] = c["events"] + [ev("auth_ok", TR2, rev="evetis-wb-communications-00001-old",
                                                      route="/poll", mechanism="scheduler_shared_secret")]
    assert f18_audit(c)["status"] == "BLOCKED"


def test_f18_events_leaking_secrets_or_unknown_fields_fail():
    c = good_chain()
    c["events"] = c["events"] + [ev("auth_ok", TR2, route="/poll", mechanism="scheduler_shared_secret",
                                    result="token 123456789:AAFAKE_fake-token-for-tests_0123456789abcdef")]
    assert f18_audit(c)["status"] == "FAIL"
    c = good_chain(); c["events"][0]["jsonPayload"]["telegram_user_id"] = "42"
    assert f18_audit(c)["status"] == "FAIL"


def test_f18_successful_protected_request_without_trace_blocks():
    c = good_chain(); c["requests"] = c["requests"] + [req("/poll", 200, "")]
    assert f18_audit(c)["status"] == "BLOCKED"


def test_f18_historical_uninstrumented_window_is_not_proven():
    """Окно 27.09: ревизии без instrumentation_ready — аудит не притворяется, что оно доказано."""
    c = good_chain()
    c["requests"] = c["requests"] + [req("/poll", 200, "f" * 32, rev="evetis-wb-communications-00025-rq8")]
    r = f18_audit(c)
    assert r["status"] == "BLOCKED" and r["mutations"] is None and any("00025-rq8" in x for x in r["iam_invariant"])


def test_f18_denied_requests_are_fine():
    c = good_chain(); c["requests"] = c["requests"] + [req("/poll", 403, "9" * 32), req("/telegram-webhook", 403, "8" * 32)]
    c["events"] = c["events"] + [ev("auth_denied", "9" * 32, route="/poll", mechanism="scheduler_shared_secret"),
                                 end("9" * 32, "/poll", 403, 0), end("8" * 32, "/telegram-webhook", 403, 0)]
    assert f18_audit(c)["status"] == "PASS"


# ------------------------------------------------------------ повтор временных ошибок источника ---
def _flaky(codes):
    import urllib.request
    seq = list(codes)

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake(req_, timeout=None):
        c = seq.pop(0)
        if c == "net":
            raise urllib.error.URLError("reset")
        if c != 200:
            raise urllib.error.HTTPError(req_.full_url, c, "x", {}, io.BytesIO(b""))
        return Resp(b'{"createTime": "2026-07-10T00:00:00Z"}')
    return fake


@pytest.mark.parametrize("codes,ok,sleeps", [([503, 500, 200], True, [2, 4]), (["net", 200], True, [2]),
                                              ([429, 502, 504, 503], False, [2, 4, 8]), ([400], False, []),
                                              ([403, 200], False, [])])
def test_transient_errors_retried_bounded_then_fail_closed(monkeypatch, codes, ok, sleeps):
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", _flaky(codes))
    slept = []
    src = A.GcpAuditSource(P, "t", sleep=slept.append)
    if ok:
        assert src.project_created() == "2026-07-10T00:00:00Z"
    else:
        with pytest.raises((urllib.error.HTTPError, urllib.error.URLError)):
            src.project_created()
    assert slept == sleeps



def test_f18_open_allowlist_or_denied_allowlist_does_not_authorise_wb():
    c = good_chain()
    for e in c["events"]:
        if e["jsonPayload"].get("mechanism") == "telegram_allowlist":
            e["jsonPayload"]["result"] = "open_no_allowlist"
    assert f18_audit(c)["status"] == "FAIL"
    c = good_chain()
    c["events"] = c["events"] + [ev("auth_denied", TR1, route="/telegram-webhook", mechanism="telegram_allowlist",
                                    result="not_allowlisted")]
    assert f18_audit(c)["status"] == "FAIL"


def test_f18_non_allowlisted_callback_answer_is_fine_but_no_wb():
    c = good_chain()
    t3 = "7" * 32
    c["requests"] = c["requests"] + [req("/telegram-webhook", 200, t3)]
    c["events"] = c["events"] + [
        ev("auth_ok", t3, route="/telegram-webhook", mechanism="telegram_secret_token"),
        ev("auth_denied", t3, route="/telegram-webhook", mechanism="telegram_allowlist", result="not_allowlisted"),
        ev("mutation_attempt", t3, mutation_class="telegram:answerCallbackQuery", target_system="telegram"),
        ev("mutation_success", t3, mutation_class="telegram:answerCallbackQuery", target_system="telegram"),
        end(t3, "/telegram-webhook", 200, 1)]
    assert f18_audit(c)["status"] == "PASS"


def test_f18_two_request_ids_under_one_trace_block():
    c = good_chain()
    c["events"] = c["events"] + [ev("auth_ok", TR2, route="/poll", mechanism="scheduler_shared_secret",
                                    request_id="another-request")]
    assert f18_audit(c)["status"] == "BLOCKED"


def test_f18_mutation_before_auth_or_outside_request_window():
    c = good_chain()
    for e in c["events"]:
        if e["jsonPayload"]["audit_event"] == "auth_ok" and e["jsonPayload"]["mechanism"] == "scheduler_shared_secret":
            e["timestamp"] = "2026-09-28T12:00:01Z"             # позже изменения (12:00:00.5)
    assert f18_audit(c)["status"] == "FAIL"
    c = good_chain()
    for e in c["events"]:     # терминальное событие Telegram через час после запроса — вне интервала, порядок цел
        if e["jsonPayload"]["audit_event"] == "mutation_success" and e["jsonPayload"]["target_system"] == "telegram":
            e["timestamp"] = "2026-09-28T13:00:00Z"
    r = f18_audit(c)
    assert r["status"] == "BLOCKED" and any("вне интервала" in x for x in r["iam_invariant"])


def test_f18_instance_mismatch_blocks():
    c = good_chain()
    c["requests"][0]["labels"] = {"instanceId": "i-1"}
    c["events"][0]["labels"] = {"instanceId": "i-2"}
    assert f18_audit(c)["status"] == "BLOCKED"


def test_f18_outcome_unknown_needs_read_back():
    c = good_chain()
    for e in c["events"]:
        if e["jsonPayload"]["audit_event"] == "mutation_success" and e["jsonPayload"]["target_system"] == "wildberries":
            e["jsonPayload"]["audit_event"] = "mutation_failure"; e["jsonPayload"]["result"] = "outcome_unknown"
    assert f18_audit(c)["status"] == "BLOCKED"


def test_f18_duplicate_trace_without_events_blocks():
    c = good_chain(); c["requests"] = c["requests"] + [req("/health", 200, "5" * 32), req("/health", 200, "5" * 32)]
    r = f18_audit(c)
    assert r["status"] == "BLOCKED" and any("у 2 запросов" in x for x in r["iam_invariant"])


def test_f18_unknown_event_schema_blocks():
    c = good_chain(); c["events"][0]["jsonPayload"]["audit_schema"] = "wbc-audit/0"
    r = f18_audit(c)
    assert r["status"] in ("BLOCKED", "FAIL") and any("неизвестной схемы" in x for x in r["iam_invariant"])


def test_f18_success_on_unknown_route_blocks_not_sticky_fail():
    c = good_chain(); c["requests"] = c["requests"] + [req("/debug/run", 200, "4" * 32)]
    r = f18_audit(c)
    assert r["status"] == "BLOCKED" and any("неизвестному маршруту" in x for x in r["iam_invariant"]) and not r["by_type"]


def test_f18_other_public_members_message():
    r = f18_audit(extra_members=["allAuthenticatedUsers"])
    assert any("ещё публичные" in x for x in r["iam_invariant"])


def test_f18_unmodelled_later_service_change_blocks():
    c = good_chain()
    later = svc_event(method="google.cloud.run.v2.Services.UpdateService"); later["timestamp"] = "2026-09-28T09:00:00Z"
    c["svc_events"] = [svc_event(), later]
    r = f18_audit(c)
    assert r["status"] == "BLOCKED" and any("не моделирует" in x for x in r["iam_invariant"])



# ------------------------------------------------------------ ревью v5: HIGH-1, MEDIUM-2…5 ---
def test_f18_ae_able_to_read_an_auth_secret_blocks():
    c = good_chain(); c["secrets"] = {"EVETIS_TELEGRAM_WEBHOOK_SECRET": ["secretmanager.versions.access"]}
    r = f18_audit(c)
    assert r["status"] == "BLOCKED" and any("EVETIS_TELEGRAM_WEBHOOK_SECRET" in x for x in r["iam_invariant"])


def test_secret_and_log_forging_permissions_are_forbidden_project_wide():
    for p_ in ("secretmanager.versions.access", "cloudscheduler.jobs.get", "run.services.get", "logging.logEntries.create"):
        assert p_ in A.FORBIDDEN_PROJECT_PERMISSIONS
    assert f18_audit(mutate(good_chain()))["status"] == "PASS"
    r = audit(FakeSource(iam_events=f18_iam(), f18=good_chain(), granted=["secretmanager.versions.access"]))
    assert r["status"] == "BLOCKED"


def test_ae_reading_a_secret_is_a_data_access_violation():
    e = entry(svc="secretmanager.googleapis.com", method="google.cloud.secretmanager.v1.SecretManagerService.AccessSecretVersion",
              authz=[{"permission": "secretmanager.versions.access", "permissionType": "DATA_READ"}])
    e["protoPayload"]["resourceName"] = f"projects/1/secrets/EVETIS_SCHEDULER_SECRET/versions/latest"
    assert data_access_violation(e, [SA]).startswith("SECRET_READ:EVETIS_SCHEDULER_SECRET")


@pytest.mark.parametrize("drop", ["request_end_missing", "mutation_event_lost", "status_mismatch"])
def test_f18_event_completeness_via_request_end(drop):
    c = good_chain()
    if drop == "request_end_missing":
        c["events"] = [e for e in c["events"] if not (e["jsonPayload"]["audit_event"] == "request_end"
                                                      and e["jsonPayload"]["trace_id"] == TR2)]
    elif drop == "mutation_event_lost":        # attempt и результат потерялись вместе — счётчик request_end выдаёт
        c["events"] = [e for e in c["events"] if not (e["jsonPayload"].get("mutation_class") == "telegram:sendMessage")]
    else:
        for e in c["events"]:
            if e["jsonPayload"]["audit_event"] == "request_end" and e["jsonPayload"]["trace_id"] == TR2:
                e["jsonPayload"]["http_status"] = 500
    r = f18_audit(c)
    assert r["status"] == "BLOCKED", r["iam_invariant"]


def test_f18_failed_request_with_complete_evidence_passes():
    c = good_chain(); t5 = "3" * 32
    c["requests"] = c["requests"] + [req("/telegram-webhook", 500, t5)]
    c["events"] = c["events"] + [ev("auth_ok", t5, route="/telegram-webhook", mechanism="telegram_secret_token"),
                                 end(t5, "/telegram-webhook", 500, 0)]
    assert f18_audit(c)["status"] == "PASS"


def test_f18_service_log_watermark_required_even_for_empty_window():
    empty = {"requests": [], "events": [], "ready": {REV}, "svc_events": [svc_event()]}
    assert f18_audit(empty)["status"] == "PASS"
    assert f18_audit({**empty, "service_watermark": False})["status"] == "BLOCKED"


def test_f18_forged_or_foreign_marker_is_not_instrumentation():
    class Src(FakeSource):
        def logs(self, flt):
            if "instrumentation_ready" in flt:
                return [{"jsonPayload": {"audit_event": "instrumentation_ready", "audit_schema": "wbc-audit/1",
                                         "revision": REV, "injected": "x"}}]
            return super().logs(flt)
    r = audit(Src(iam_events=f18_iam(), f18=good_chain()))
    assert r["status"] == "BLOCKED"


@pytest.mark.parametrize("path", ["/docs", "/openapi.json", "/redoc"])
def test_f18_read_only_framework_routes_do_not_fail(path):
    c = good_chain(); t6 = "2" * 32
    c["requests"] = c["requests"] + [req(path, 200, t6)]
    c["events"] = c["events"] + [end(t6, path, 200, 0)]
    assert f18_audit(c)["status"] == "PASS"


def test_f18_percent_encoded_route_is_routed_like_starlette():
    c = good_chain(); t7 = "1" * 32
    c["requests"] = c["requests"] + [req("/telegram%2Dwebhook", 200, t7)]
    c["events"] = c["events"] + [end(t7, "/telegram-webhook", 200, 0)]
    r = f18_audit(c)
    assert r["status"] == "FAIL" and any("без auth_ok" in k for k in r["by_type"])   # это webhook без auth_ok


def test_f18_success_without_trace_on_unknown_route_blocks():
    c = good_chain(); c["requests"] = c["requests"] + [req("/weird", 200, "")]
    assert f18_audit(c)["status"] == "BLOCKED"


@pytest.mark.parametrize("svc_events", [
    [svc_event(ts="2026-09-28T08:00:00Z"), svc_event(sa="other@x.iam.gserviceaccount.com", ts="2026-09-28T12:00:00Z"),
     svc_event(ts="2026-09-28T12:05:00Z")],                                    # переключили SA в окне и вернули
    [svc_event(timeout=900)],                                                  # запрос может пережить выдержку
    [svc_event(timeout=None)],
])
def test_f18_spec_changes_inside_window_and_timeout(svc_events):
    assert f18_audit(mutate(good_chain(), svc_events=svc_events))["status"] == "BLOCKED"



def test_f18_scheduler_secret_read_paths_are_closed():
    for p_ in ("cloudscheduler.jobs.list", "run.services.list", "run.revisions.get", "run.revisions.list",
               "storage.objects.get", "storage.objects.list"):
        assert p_ in A.FORBIDDEN_PROJECT_PERMISSIONS
    c = good_chain(); c["buckets"] = {"evetis-wb-tfstate-1": ["storage.objects.get"]}
    r = f18_audit(c)
    assert r["status"] == "BLOCKED" and any("Terraform state" in x for x in r["iam_invariant"])



def test_f18_missing_state_bucket_or_auth_secret_blocks():
    r = f18_audit(mutate(good_chain(), bucket_missing=True))
    assert r["status"] == "BLOCKED" and any("не найден" in x for x in r["iam_invariant"])
    r = f18_audit(mutate(good_chain(), missing_secrets={"EVETIS_SCHEDULER_SECRET"}))
    assert r["status"] == "BLOCKED" and any("EVETIS_SCHEDULER_SECRET" in x for x in r["iam_invariant"])
