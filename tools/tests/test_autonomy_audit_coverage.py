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


class FakeSource:
    def __init__(self, jobs=(), activity=(), granted=(), datasets=None, table_policies=(), writable=(),
                 created="2026-07-10T10:26:24Z", watermark_after=0):
        self.project = P
        self._jobs, self._activity, self._granted = list(jobs), list(activity), list(granted)
        self._datasets = datasets if datasets is not None else {"wb_raw": [{"role": "READER", "userByEmail": SA}],
                                                                ANON: [{"role": "OWNER", "userByEmail": SA}]}
        self._policies, self._writable, self._created = list(table_policies), dict(writable), created
        self.watermark_after, self.list_calls, self.sleeps, self.label = watermark_after, 0, [], None

    def now(self):
        return NOW

    def sleep(self, s):
        self.sleeps.append(s)

    def jobs(self, since_ms):
        self.list_calls += 1
        extra = [job(purpose=self.label)] if self.label and self.list_calls > self.watermark_after else []
        return self._jobs + extra

    def activity(self, flt):
        if 'methodName:"SetIamPolicy"' in flt:
            return [{"protoPayload": {"resourceName": rn}} for rn in self._policies]
        return self._activity

    def granted(self, perms):
        assert set(self._granted) <= set(perms)
        return self._granted

    def datasets(self):
        return self._datasets

    def table_can_write(self, ds, table):
        return self._writable.get((ds, table), False)

    def project_created(self):
        return self._created


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
    r = audit(FakeSource(activity=[{"protoPayload": {"methodName": "google.cloud.bigquery.v2.TableService.DeleteTable"}}]))
    assert r["status"] == "FAIL" and r["by_type"] == {"ADMIN:google.cloud.bigquery.v2.TableService.DeleteTable": 1}


def test_watermark_never_seen_is_blocked_with_bounded_wait():
    src = FakeSource(watermark_after=10 ** 6)
    r = audit(src)
    assert r["status"] == "BLOCKED" and r["mutations"] is None and "зонд" in r["error"]
    assert src.list_calls == A.WATERMARK_TRIES and sum(src.sleeps) == (A.WATERMARK_TRIES - 1) * A.WATERMARK_SLEEP


def test_watermark_late_but_seen_is_pass():
    src = FakeSource(watermark_after=2)
    assert audit(src)["status"] == "PASS" and src.list_calls == 3


def test_settle_delay_is_deterministic_and_bounded():
    src = FakeSource()
    audit(src, settle_from=(NOW - timedelta(seconds=60)).strftime("%Y-%m-%dT%H:%M:%SZ"))
    assert src.sleeps[0] == A.SETTLE_SECONDS - 60
    src = FakeSource()
    audit(src, settle_from=(NOW - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"))
    assert src.sleeps == []


@pytest.mark.parametrize("src", [
    FakeSource(granted=["bigquery.tables.updateData"]),
    FakeSource(datasets={"wb_raw": [{"role": "WRITER", "userByEmail": SA}]}),
    FakeSource(datasets={"wb_raw": [{"role": "OWNER", "userByEmail": SA}]}),
    FakeSource(datasets={"wb_raw": [{"role": "roles/bigquery.dataEditor", "iamMember": f"serviceAccount:{SA}"}]}),
    FakeSource(datasets={"wb_raw": [{"role": "WRITER", "groupByEmail": "team@x"}]}),
    FakeSource(datasets={"wb_raw": [{"role": "WRITER", "domain": "x.com"}]}),
    FakeSource(datasets={"wb_raw": [{"role": "WRITER", "specialGroup": "allAuthenticatedUsers"}]}),
    FakeSource(datasets={"wb_raw": [{"role": "WRITER", "iamMember": "principalSet://iam.googleapis.com/x"}]}),
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
    assert acl_problems("wb_raw", access, SA) == []
    assert acl_problems(ANON, [{"role": "OWNER", "userByEmail": SA}], SA) == []


def test_table_policies_only_on_other_principals_or_deleted_tables_pass():
    src = FakeSource(table_policies=[f"projects/{P}/datasets/wb_raw/tables/RAW_WB_PRICES",
                                     f"projects/{P}/datasets/wb_raw/tables/GONE",
                                     f"projects/{P}/datasets/wb_mart/routines/F"],
                     writable={("wb_raw", "GONE"): None})
    r = audit(src)
    assert r["status"] == "PASS" and r["table_policies_checked"] == 2


def test_found_mutation_beats_unproven_iam():
    r = audit(FakeSource(jobs=[job(st="INSERT")], granted=["bigquery.tables.updateData"]))
    assert r["status"] == "FAIL" and r["mutations"] == 1


def _run(r):
    from tools.autonomy.orchestrator import Orchestrator
    o = Orchestrator.__new__(Orchestrator)
    o.now = lambda: NOW
    base = {"run_id": "run-20260927T121235Z-ab2e1c53", "created_at": SINCE, "production_mutations": 0, "usage": []}
    return o._merge_audit(base, r)


# ------------------------------------------------------------ источник и сбои ---
def test_legacy_or_unlabelled_evidence_is_not_zero():
    run = _run({"status": "PASS", "mutations": 0, "source": AUDIT_SOURCE})
    assert zero_mutations_proven(run)[0]
    for legacy in (_run(0), _run({"status": "PASS", "mutations": 0}),
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


def test_settle_is_applied_only_in_the_trusted_audit_command(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from tools.autonomy import cli
    seen = []
    monkeypatch.setattr(A, "count_mutations", lambda *a, **k: seen.append(k.get("settle_from")) or {"status": "PASS"})
    run = {"created_at": SINCE, "updated_at": "2026-09-27T12:50:00Z"}
    for cmd, expected in (("audit", "2026-09-27T12:50:00Z"), ("agent-run", None)):
        a = SimpleNamespace(cmd=cmd, project=P, token_command="true", audit_identity=[SA], engineer="none",
                            reviewer="none", sandbox_root=str(tmp_path), dry_run=False, pending_dir=None)
        orch = cli._orchestrator(a, cli.StateStore(tmp_path / "s"))
        orch.audit(run)
        assert seen[-1] == expected


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
