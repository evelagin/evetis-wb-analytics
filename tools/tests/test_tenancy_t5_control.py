"""Tenancy T5 — control plane в доверенной базе, контракте, Terraform, сканере и пакете SQL.

Офлайн. Доказывает: матрица прав control одна и та же в control_identity.py, platform.tf и
выводе сканера; роли control не содержат запрещённых прав; блок control контракта подделать
нельзя; представления T5 исполняются и повторяют автомат lifecycle_core; схемы журналов
принимают ровно то, что пишет control; client_002 получает тот же control без правки кода.
"""
from __future__ import annotations

import ast
import copy
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "pipelines" / "ozon" / "runtime"))

from tools.tenancy import control_identity as CI  # noqa: E402
from tools.tenancy import ozon_contract as OC  # noqa: E402
from tools.tenancy import plan_scan as PS  # noqa: E402
from tools.tenancy import sql_identity as SI  # noqa: E402
from tools.tenancy import synthetic as SY  # noqa: E402
from tools.tests import tenancy_sql_harness as HS  # noqa: E402
from tools.tests.test_tenancy_t32_remediation import _plan_for  # noqa: E402

import lifecycle_core as L  # noqa: E402

PLATFORM_TF = (REPO / "infra" / "tenant" / "platform.tf").read_text(encoding="utf-8")
MODULE_TF = (REPO / "infra" / "tenant" / "modules" / "ozon_runtime" / "main.tf").read_text(encoding="utf-8")
ROOT_TF = (REPO / "infra" / "tenant" / "main.tf").read_text(encoding="utf-8")


def _tf_list(name):
    m = re.search(rf"{name}\s*=\s*\[(.*?)\]", PLATFORM_TF, re.S)
    return sorted(re.findall(r'"([^"]+)"', m.group(1)))


def _tf_str(name):
    return re.search(rf'{name}\s*=\s*"([^"]+)"', PLATFORM_TF).group(1)


# ═════════════════════════════════════════ доверенная база ↔ Terraform
def test_platform_tf_matches_control_identity():
    assert _tf_str("control_account_id") == CI.CONTROL_ACCOUNT_ID
    assert _tf_str("control_job_name") == CI.CONTROL_JOB_NAME
    assert _tf_list("control_roles") == sorted(CI.CONTROL_ROLES)
    want = sorted(f"{ds}|{r}|false" for ds, roles in CI.GRANT_MATRIX.items() for r in roles)
    assert _tf_list("control_grant_matrix") == want


def test_control_roles_are_minimal_and_disjoint_from_forbidden():
    for rid, spec in CI.CONTROL_ROLES.items():
        assert not (spec["permissions"] & CI.NEVER_FOR_CONTROL), rid
        assert all(p.startswith("bigquery.tables.") for p in spec["permissions"]), rid
    assert CI.CONTROL_ROLES[CI.APPEND]["permissions"] == {"bigquery.tables.get", "bigquery.tables.updateData"}
    assert CI.CONTROL_ROLES[CI.LEASE]["permissions"] == {"bigquery.tables.create", "bigquery.tables.get",
                                                         "bigquery.tables.list"}
    # getData (чтение строк) даёт только уже существующая роль mpaSqlSourceRead, и только там, где нужно.
    assert SI.SQL_ROLES["mpaSqlSourceRead"]["permissions"] == {"bigquery.tables.get", "bigquery.tables.getData"}


def test_control_never_writes_raw_ref_or_sees_client_layers():
    assert CI.GRANT_MATRIX["ozon_raw"] == (SI.SOURCE_READ,) and CI.GRANT_MATRIX["ref"] == (SI.SOURCE_READ,)
    assert CI.GRANT_MATRIX["ozon_mart"] == () and CI.GRANT_MATRIX["analytics_share"] == ()
    assert CI.APPEND in CI.GRANT_MATRIX["tenant_ops"] and CI.LEASE in CI.GRANT_MATRIX["tenant_locks"]


def test_control_has_no_project_level_roles_in_terraform():
    code = "\n".join(l for l in (ROOT_TF + MODULE_TF).splitlines() if not l.strip().startswith("#"))
    for block in re.findall(r'resource "google_project_iam_member" "[^"]+" \{.*?\n\}', code, re.S):
        assert "control" not in block
    assert code.count("local.control_member") == 1           # ровно одна привязка: secretAccessor
    assert 'role      = "roles/secretmanager.secretAccessor"\n  member    = local.control_member' in code


def test_control_job_has_no_scheduler_and_fixed_entrypoint():
    assert 'resource "google_cloud_run_v2_job" "control"' in MODULE_TF
    job = re.search(r'resource "google_cloud_run_v2_job" "control" \{.*?\n\}', MODULE_TF, re.S).group(0)
    assert 'command = ["python", "lifecycle.py"]' in job and 'args    = ["status"]' in job
    assert "service_account = var.control.email" in job
    for sched in re.findall(r'resource "google_cloud_scheduler_job" "[^"]+" \{.*?\n\}', MODULE_TF, re.S):
        assert "for_each = var.ozon.jobs" in sched
    assert tuple(CI.CONTROL_COMMAND) == ("python", "lifecycle.py") and CI.CONTROL_DEFAULT_ARGS == ("status",)


def test_lifecycle_status_is_read_only():
    """Аргумент job'а по умолчанию — status: ни записи, ни обращения к Ozon."""
    tree = ast.parse((REPO / "pipelines/ozon/runtime/lifecycle.py").read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "cmd_status")
    src = ast.unparse(fn)
    assert "append" not in src and "seller_post" not in src and "perf_" not in src and "create_lease" not in src


# ═════════════════════════════════════════ контракт и сканер
def test_contract_control_block_is_derived_for_every_tenant():
    for tid in SY.SYNTHETIC_TENANTS:
        c = SY.fixture_contract(tid)
        assert c["control"] == PS.expected_control(c)
        assert c["control"]["email"] == f"sa-tenant-control@{c['project_id']}.iam.gserviceaccount.com"
        assert c["control"]["job"]["env"]["GCP_PROJECT_ID"] == c["project_id"]
        assert all(j["env"]["TENANT_BINDING_REQUIRED"] == "1" for j in c["marketplaces"]["ozon"]["jobs"].values())
        assert PS.scan_plan(_plan_for(c), c) == []


def test_client_002_differs_from_client_001_only_in_identity():
    a, b = SY.fixture_contract("client_001"), SY.fixture_contract("client_002")
    norm = lambda c: json.loads(json.dumps(c["control"]).replace(c["project_id"], "P").replace(c["tenant_id"], "T"))
    assert norm(a) == norm(b)


CONTRACT_TAMPER = {
    "control email other project": lambda c: c["control"].update(email="sa-tenant-control@mpa-t-client-002.iam.gserviceaccount.com"),
    "control grant added": lambda c: c["control"]["grants"].append(
        {"dataset_key": "ref", "role": SI.role_name(CI.APPEND), "condition": None}),
    "control env binding flag": lambda c: c["control"]["job"]["env"].update(TENANT_BINDING_REQUIRED="1"),
    "control env other project": lambda c: c["control"]["job"]["env"].update(GCP_PROJECT_ID="mpa-t-client-002"),
    "runtime without binding flag": lambda c: c["marketplaces"]["ozon"]["jobs"]["ozon-runtime-daily"]["env"].pop(
        "TENANT_BINDING_REQUIRED"),
    "runtime job named tenant-control": lambda c: c["marketplaces"]["ozon"]["jobs"].update(
        {"tenant-control": copy.deepcopy(c["marketplaces"]["ozon"]["jobs"]["ozon-runtime-daily"])}),
    "control removed": lambda c: c.update(control=None),
}


@pytest.mark.parametrize("name", sorted(CONTRACT_TAMPER))
def test_scanner_rejects_tampered_control_contract(name):
    c = SY.fixture_contract("client_001")
    plan = _plan_for(c)
    bad = copy.deepcopy(c)
    CONTRACT_TAMPER[name](bad)
    assert PS.control_findings(bad), name


def test_scanner_expected_acl_has_control_only_where_matrix_says():
    c = SY.fixture_contract("client_001")
    acl = PS.expected_dataset_access(c)
    ctl = CI.control_email(c["project_id"])
    got = {ds: sorted(r.rsplit("/", 1)[-1] for r, _k, who, _c in entries if who == ctl) for ds, entries in acl.items()}
    assert got == {"ozon_raw": ["mpaSqlSourceRead"], "ref": ["mpaSqlSourceRead"], "ozon_mart": [],
                   "analytics_share": [], "tenant_ops": ["mpaSqlSourceRead", "mpaTenantControlAppend"],
                   "tenant_locks": ["mpaTenantControlLease"]}


# ═════════════════════════════════════════ схемы журналов ↔ то, что пишет control
def _schema_fields(ds, table):
    return {f["name"] for f in OC.load_table_spec(ds, table)["schema"]}


def test_checkpoint_rows_fit_schema():
    """_cp_row собирает ровно колонки BACKFILL_CHECKPOINTS (без импорта runtime: разбор AST)."""
    tree = ast.parse((REPO / "pipelines/ozon/runtime/lifecycle.py").read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_cp_row")
    d = next(x for x in ast.walk(fn) if isinstance(x, ast.Dict))
    assert {k.value for k in d.keys} == _schema_fields("tenant_ops", "BACKFILL_CHECKPOINTS")


def test_every_control_row_fits_its_schema():
    """Ключи строк, которые собирает lifecycle.py, — подмножество схемы таблицы (insertAll строгий)."""
    src = (REPO / "pipelines/ozon/runtime/lifecycle.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    expect = {"observe_seller": "SELLER_IDENTITY_OBSERVATIONS", "observe_performance": "SELLER_IDENTITY_OBSERVATIONS",
              "_cap_row": "CAPABILITY_PROFILE", "_boundary_row": "HISTORY_BOUNDARIES", "cmd_dq": "DQ_RESULTS"}
    for fn in (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name in expect):
        dicts = [d for d in ast.walk(fn) if isinstance(d, ast.Dict) and len(d.keys) > 6]
        assert dicts, fn.name
        keys = {k.value for k in dicts[0].keys if isinstance(k, ast.Constant)}
        assert keys <= _schema_fields("tenant_ops", expect[fn.name]), (fn.name, keys - _schema_fields("tenant_ops", expect[fn.name]))


def test_new_columns_are_nullable_appends_only():
    for table, new in (("BACKFILL_CHECKPOINTS", {"plan_hash", "lease_owner", "lease_until", "lease_generation",
                                                 "started_at", "completed_at", "evidence_json"}),
                       ("DQ_RESULTS", {"severity"})):
        fields = OC.load_table_spec("tenant_ops", table)["schema"]
        tail = fields[-len(new):]
        assert {f["name"] for f in tail} == new and all(f["mode"] == "NULLABLE" for f in tail), table


# ═════════════════════════════════════════ пакет SQL: два представления T5
def _edges_from_sql():
    sql = (HS.PACKAGE_DIR / "tenant_ops" / "V_TENANT_STATE_AUDIT.sql").read_text(encoding="utf-8")
    body = sql.split("WITH edges AS (", 1)[1].split("),", 1)[0]
    out = set()
    for f, t, a in re.findall(r"SELECT (CAST\(NULL AS STRING\)|'\w+')(?: AS from_state)?, '(\w+)'(?: AS to_state)?, '(\w+)'", body):
        out.add((None if f.startswith("CAST") else f.strip("'"), t, a))
    return out


def test_audit_view_edges_equal_lifecycle_core():
    want = {(f, t, a) for (f, t), (actors, _v) in L.EDGES.items() for a in actors}
    assert _edges_from_sql() == want


def _events(*rows):
    cols = ["tenant_id", "event_id", "occurred_at", "from_state", "to_state", "actor", "run_id", "reason_code",
            "reason_detail", "evidence_json"]
    return {"TENANT_STATE_EVENTS": (cols, [("t", eid, at, f, to, actor, "r", "x", None, None)
                                           for eid, at, f, to, actor in rows])}


def test_audit_view_accepts_valid_chain():
    rows = HS.run("tenant_ops", "V_TENANT_STATE_AUDIT", _events(
        ("e1", "2026-09-01 00:00:00", None, "CREDENTIALS_PENDING", "OPERATOR:o"),
        ("e2", "2026-09-01 00:01:00", "CREDENTIALS_PENDING", "VALIDATING", "OPERATOR:o"),
        ("e3", "2026-09-01 00:02:00", "VALIDATING", "CAPABILITY_DISCOVERY", "CONTROL:ctl-1"),
        ("e4", "2026-09-01 00:03:00", "CAPABILITY_DISCOVERY", "SUSPENDED", "CONTROL:ctl-2")))
    assert [r["audit_status"] for r in sorted(rows, key=lambda r: r["seq"])] == ["OK"] * 4
    assert all(r["first_violation_at"] is None for r in rows)


@pytest.mark.parametrize("bad,violation", [
    (("e3", "2026-09-01 00:02:00", "VALIDATING", "READY", "CONTROL:c"), "EDGE"),
    (("e3", "2026-09-01 00:02:00", "VALIDATING", "CAPABILITY_DISCOVERY", "OPERATOR:o"), "ACTOR"),
    (("e3", "2026-09-01 00:02:00", "RECONCILING", "READY", "CONTROL:c"), "CHAIN"),
    (("e3", "2026-09-01 00:02:00", "VALIDATING", "CAPABILITY_DISCOVERY", "RUNTIME:x"), "ACTOR"),
])
def test_audit_view_flags_violations(bad, violation):
    rows = HS.run("tenant_ops", "V_TENANT_STATE_AUDIT", _events(
        ("e1", "2026-09-01 00:00:00", None, "CREDENTIALS_PENDING", "OPERATOR:o"),
        ("e2", "2026-09-01 00:01:00", "CREDENTIALS_PENDING", "VALIDATING", "OPERATOR:o"), bad))
    last = max(rows, key=lambda r: r["seq"])
    assert last["audit_status"] == "VIOLATION" and last["violation"] == violation
    assert all(r["first_violation_at"] == bad[1] for r in rows)


def test_capability_view_takes_latest_per_pair():
    cols = ["profile_id", "run_id", "discovered_at", "api", "capability", "status", "evidence_kind", "http_status",
            "evidence_json", "notes"]
    rows = HS.run("tenant_ops", "V_CAPABILITY_CURRENT", {"CAPABILITY_PROFILE": (cols, [
        ("p1", "r1", "2026-09-01 00:00:00", "seller", "catalog", "DENIED", "LIVE_CALL", 403, None, None),
        ("p2", "r2", "2026-09-02 00:00:00", "seller", "catalog", "AVAILABLE", "LIVE_CALL", 200, None, None),
        ("p3", "r2", "2026-09-02 00:00:00", "performance", "async_report", "NOT_APPLICABLE", "DOCUMENTED", None, None, None),
        ("p4", "r3", "2026-09-03 00:00:00", "performance", "expense", "UNAVAILABLE", "LIVE_CALL", 500, None, None),
    ])})
    got = {(r["api"], r["capability"]): (r["status"], bool(r["is_ok"])) for r in rows}
    assert got == {("seller", "catalog"): ("AVAILABLE", True), ("performance", "async_report"): ("NOT_APPLICABLE", True),
                   ("performance", "expense"): ("UNAVAILABLE", False)}
    import lifecycle_core as LCORE
    assert LCORE.OK_CAPABILITY == ("AVAILABLE", "NOT_APPLICABLE")
    sql = (HS.PACKAGE_DIR / "tenant_ops" / "V_CAPABILITY_CURRENT.sql").read_text(encoding="utf-8")
    assert "p.status IN ('AVAILABLE', 'NOT_APPLICABLE') AS is_ok" in sql


def test_t5_views_are_tenant_ops_only_and_share_is_not_extended():
    pkg = json.loads((HS.PACKAGE_DIR / "PACKAGE.json").read_text(encoding="utf-8"))
    names = [(o["dataset"], o["name"]) for o in pkg["objects"]]
    assert ("tenant_ops", "V_CAPABILITY_CURRENT") in names and ("tenant_ops", "V_TENANT_STATE_AUDIT") in names
    counts = {ds: sum(1 for d, _n in names if d == ds) for ds in ("ozon_mart", "tenant_ops", "analytics_share")}
    assert counts == {"ozon_mart": 13, "tenant_ops": 10, "analytics_share": 9} and len(names) == 32
    for n in ("V_CAPABILITY_CURRENT", "V_TENANT_STATE_AUDIT"):
        sql = (HS.PACKAGE_DIR / "tenant_ops" / f"{n}.sql").read_text(encoding="utf-8")
        refs = set(re.findall(r"`__tenant__\.(\w+)\.(\w+)`", sql))
        assert {ds for ds, _t in refs} == {"tenant_ops"}, n
