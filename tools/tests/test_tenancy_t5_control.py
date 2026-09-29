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
              "_cap_row": "CAPABILITY_PROFILE", "_boundary_row": "HISTORY_BOUNDARIES", "dq_rows": "DQ_RESULTS"}
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


DEC_COLS = ["decision_id", "decision_type", "expect_state", "to_state", "expect_seq", "plan_hash",
            "secret_versions_json", "chunk_id", "reopens_run_id", "domain", "boundary_id", "reason", "actor",
            "decided_at"]


def _events(*rows, decisions=()):
    cols = ["tenant_id", "event_id", "occurred_at", "from_state", "to_state", "actor", "run_id", "reason_code",
            "reason_detail", "evidence_json", "seq", "decision_id"]
    return {"TENANT_STATE_EVENTS": (cols, [("t", eid, at, f, to, actor, "r", "x", None, None, seq, dec)
                                           for seq, eid, at, f, to, actor, dec in rows]),
            "OPERATOR_DECISIONS": (DEC_COLS, [(d, "TRANSITION", f, t, seq, None, None, None, None, None, None, None,
                                               "OPERATOR:o", "2026-09-01") for d, f, t, seq in decisions])}


GOOD = [(1, "e1", "2026-09-01 00:00:00", None, "CREDENTIALS_PENDING", "OPERATOR:o", "d1"),
        (2, "e2", "2026-09-01 00:01:00", "CREDENTIALS_PENDING", "VALIDATING", "OPERATOR:o", "d2")]
GOOD_DEC = [("d1", None, "CREDENTIALS_PENDING", 1), ("d2", "CREDENTIALS_PENDING", "VALIDATING", 2)]


def test_audit_view_accepts_valid_chain():
    rows = HS.run("tenant_ops", "V_TENANT_STATE_AUDIT", _events(*GOOD,
        (3, "e3", "2026-09-01 00:02:00", "VALIDATING", "CAPABILITY_DISCOVERY", "CONTROL:ctl-1", None),
        (4, "e4", "2026-09-01 00:03:00", "CAPABILITY_DISCOVERY", "SUSPENDED", "OPERATOR:o", None),
        decisions=GOOD_DEC))
    assert [r["audit_status"] for r in sorted(rows, key=lambda r: r["seq"])] == ["OK"] * 4
    assert all(r["first_violation_at"] is None for r in rows)


def test_audit_view_orders_by_seq_not_clock():
    """Часы ноутбука владельца отстают: время события 3 раньше события 2, но порядок — по seq."""
    rows = HS.run("tenant_ops", "V_TENANT_STATE_AUDIT", _events(
        GOOD[0], (2, "e2", "2026-09-01 00:05:00", "CREDENTIALS_PENDING", "VALIDATING", "OPERATOR:o", "d2"),
        (3, "e3", "2026-09-01 00:02:00", "VALIDATING", "CAPABILITY_DISCOVERY", "CONTROL:c", None), decisions=GOOD_DEC))
    assert all(r["audit_status"] == "OK" for r in rows)


@pytest.mark.parametrize("bad,violation", [
    ((3, "e3", "2026-09-01 00:02:00", "VALIDATING", "READY", "CONTROL:c", None), "EDGE"),
    ((3, "e3", "2026-09-01 00:02:00", "VALIDATING", "CAPABILITY_DISCOVERY", "OPERATOR:o", None), "ACTOR"),
    ((3, "e3", "2026-09-01 00:02:00", "RECONCILING", "READY", "CONTROL:c", None), "CHAIN"),
    ((3, "e3", "2026-09-01 00:02:00", "VALIDATING", "CAPABILITY_DISCOVERY", "RUNTIME:x", None), "ACTOR"),
    ((5, "e3", "2026-09-01 00:02:00", "VALIDATING", "CAPABILITY_DISCOVERY", "CONTROL:c", None), "SEQ"),
    ((3, "e3", "2026-09-01 00:02:00", "VALIDATING", "SUSPENDED", "CONTROL:c", None), None),
])
def test_audit_view_flags_violations(bad, violation):
    rows = HS.run("tenant_ops", "V_TENANT_STATE_AUDIT", _events(*GOOD, bad, decisions=GOOD_DEC))
    last = max(rows, key=lambda r: r["seq"])
    assert last["violation"] == violation and last["audit_status"] == ("OK" if violation is None else "VIOLATION")


def test_audit_view_flags_operator_edge_without_owner_decision():
    """control записал «OPERATOR» сам: решения владельца на этот номер в ref нет — PROOF; решение на
    другой номер (повтор) не подходит."""
    forged = [(1, "e1", "2026-09-01 00:00:00", None, "CREDENTIALS_PENDING", "OPERATOR:forged", "d1"),
              (2, "e2", "2026-09-01 00:01:00", "CREDENTIALS_PENDING", "VALIDATING", "OPERATOR:forged", "dx")]
    rows = HS.run("tenant_ops", "V_TENANT_STATE_AUDIT", _events(*forged, decisions=[GOOD_DEC[0]]))
    by = {r["seq"]: r for r in rows}
    assert by[1]["audit_status"] == "OK" and by[2]["violation"] == "PROOF"
    mism = [(1, "e1", "2026-09-01 00:00:00", None, "CREDENTIALS_PENDING", "OPERATOR:o", "d1"),
            (2, "e2", "2026-09-01 00:01:00", "CREDENTIALS_PENDING", "VALIDATING", "OPERATOR:o", "d1")]
    rows = HS.run("tenant_ops", "V_TENANT_STATE_AUDIT", _events(*mism, decisions=[GOOD_DEC[0]]))
    assert max(rows, key=lambda r: r["seq"])["violation"] == "PROOF"       # решение от другого ребра
    replay = [(1, "e1", "2026-09-01 00:00:00", None, "CREDENTIALS_PENDING", "OPERATOR:o", "d1"),
              (2, "e2", "2026-09-01 00:01:00", "CREDENTIALS_PENDING", "VALIDATING", "OPERATOR:o", "d2")]
    rows = HS.run("tenant_ops", "V_TENANT_STATE_AUDIT", _events(*replay, decisions=[
        GOOD_DEC[0], ("d2", "CREDENTIALS_PENDING", "VALIDATING", 7)]))                # решение на номер 7
    assert max(rows, key=lambda r: r["seq"])["violation"] == "PROOF"


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
        # Аудит читает решения владельца (ref.OPERATOR_DECISIONS); клиентского слоя и витрины — нет.
        assert refs - {("ref", "OPERATOR_DECISIONS"), ("tenant_ops", n)} <= \
            {("tenant_ops", t) for t in OC.PLATFORM_TABLES["tenant_ops"]}, n


# ═════════════════════════════════════════ политика методов Seller (ревью PR #226, находка 14)
def test_policy_generator_takes_the_strictest_class_per_path(tmp_path):
    from tools.tenancy import ozon_method_policy as P
    spec = {"paths": {
        "/v1/x/list": {"post": {"summary": "Получить список"}, "delete": {"summary": "Получить список"}},
        "/v1/y/info": {"get": {"summary": "Получить информацию"}, "post": {"summary": "Удалить товар"}},
        "/v1/z/info": {"get": {"summary": "Получить информацию"}},
    }}
    f = tmp_path / "spec.json"
    f.write_text(json.dumps(spec), encoding="utf-8")
    m = P.build(f, "2026-09-28")["methods"]
    assert m["/v1/x/list"]["class"] == "MUTATION" and m["/v1/x/list"]["http"] == "DELETE"
    assert m["/v1/y/info"]["class"] == "MUTATION"
    assert m["/v1/z/info"]["class"] == "READ"


def test_policy_file_has_one_method_per_path_and_no_approved_mutations():
    from tools.tenancy import ozon_method_policy as P
    pol = P.load_policy()
    assert pol["approved_mutation_methods"] == [] and len(pol["methods"]) == 481
    assert {v["http"] for v in pol["methods"].values()} <= {"GET", "POST"}


# ═════════════════════════════════════════ V_TENANT_STATE_CURRENT: текущее — по seq, не по часам (T5)
CUR_COLS = ["tenant_id", "event_id", "occurred_at", "from_state", "to_state", "actor", "run_id", "reason_code",
            "reason_detail", "evidence_json", "seq", "decision_id"]
STATES = ["CREDENTIALS_PENDING", "VALIDATING", "CAPABILITY_DISCOVERY", "READY_FOR_BACKFILL", "BACKFILLING",
          "RECONCILING", "READY", "SUSPENDED"]


def _cur(rows, package_dir=None):
    """rows: [(event_id, occurred_at, to_state, seq)] → строки представления по арендатору."""
    tables = {"TENANT_STATE_EVENTS": (CUR_COLS, [("t", eid, at, None, to, "CONTROL:c", "r", "x", None, None, seq, None)
                                                 for eid, at, to, seq in rows])}
    if package_dir is None:
        return HS.run("tenant_ops", "V_TENANT_STATE_CURRENT", tables)
    old = HS.PACKAGE_DIR
    HS.PACKAGE_DIR = package_dir
    try:
        return HS.run("tenant_ops", "V_TENANT_STATE_CURRENT", tables)
    finally:
        HS.PACKAGE_DIR = old


def test_current_state_is_highest_seq_not_latest_clock():
    """Регрессия: seq=10 с более поздним occurred_at и seq=11 с более ранним — текущее обязано быть seq=11."""
    rows = _cur([("e10", "2026-09-29 12:00:00", "BACKFILLING", 10), ("e11", "2026-09-29 09:00:00", "RECONCILING", 11)])
    assert len(rows) == 1 and rows[0]["seq"] == 11 and rows[0]["state"] == "RECONCILING"
    assert rows[0]["journal_status"] == "GAP"                     # номера 1..9 отсутствуют
    full = [(f"e{i:02d}", f"2026-09-29 {23 - i:02d}:00:00", STATES[i % len(STATES)], i) for i in range(1, 12)]
    rows = _cur(full)                                             # часы идут назад, номера — вперёд
    assert rows[0]["seq"] == 11 and rows[0]["journal_status"] == "OK"


@pytest.mark.parametrize("rows,status,seq", [
    ([("a", "2026-09-29 01:00:00", "VALIDATING", 1), ("b", "2026-09-29 02:00:00", "CAPABILITY_DISCOVERY", 2),
      ("b", "2026-09-29 02:00:00", "CAPABILITY_DISCOVERY", 2)], "OK", 2),          # повтор insertAll — схлопнут
    ([("a", "2026-09-29 01:00:00", "VALIDATING", 1), ("b", "2026-09-29 02:00:00", "CAPABILITY_DISCOVERY", 2),
      ("c", "2026-09-29 03:00:00", "SUSPENDED", 2)], "DUPLICATE", 2),              # разные события на номер 2
    ([("a", "2026-09-29 01:00:00", "VALIDATING", 1), ("c", "2026-09-29 03:00:00", "SUSPENDED", 3)], "GAP", 3),
    ([("b", "2026-09-29 02:00:00", "VALIDATING", 2), ("c", "2026-09-29 03:00:00", "SUSPENDED", 3)], "GAP", 3),
    ([("x", "2026-09-29 05:00:00", "VALIDATING", None), ("a", "2026-09-29 01:00:00", "CREDENTIALS_PENDING", 1)],
     "NO_SEQ", 1),                                                                  # нумерованное важнее
    ([("x", "2026-09-29 05:00:00", "VALIDATING", None), ("y", "2026-09-29 04:00:00", "CREDENTIALS_PENDING", None)],
     "NO_SEQ", None),                                                               # только формат до T5
])
def test_current_state_duplicate_gap_and_legacy_semantics(rows, status, seq):
    got = _cur(rows)
    assert len(got) == 1 and got[0]["journal_status"] == status and got[0]["seq"] == seq
    if seq is None:
        assert got[0]["state"] == "VALIDATING"                    # без номеров — последнее по времени


def test_current_state_view_matches_lifecycle_core_on_random_chains():
    """Паритет SQL и кода: на перемешанных часах текущее по представлению = lifecycle_core.current_state."""
    import random
    rnd = random.Random(20260929)
    for _ in range(150):
        n = rnd.randint(1, 12)
        rows = []
        for i in range(1, n + 1):
            seq = i if rnd.random() > 0.1 else None
            rows.append((f"e{i:02d}", f"2026-09-{rnd.randint(1, 28):02d} {rnd.randint(0, 23):02d}:00:00",
                         rnd.choice(STATES), seq))
        want = L.current_state([{"event_id": eid, "occurred_at": at.replace(" ", "T") + "+00:00", "to_state": to,
                                 "seq": seq} for eid, at, to, seq in rows])
        assert _cur(rows)[0]["state"] == want, rows


def test_current_state_ordering_mutation_is_killed(tmp_path):
    """Мутация: вернуть «последнее по occurred_at» — регрессионный тест обязан её поймать."""
    import shutil
    pkg = tmp_path / "ozon"
    shutil.copytree(HS.PACKAGE_DIR, pkg)
    view = pkg / "tenant_ops" / "V_TENANT_STATE_CURRENT.sql"
    sql = view.read_text(encoding="utf-8")
    good = "ORDER BY IF(v.seq IS NULL, 0, 1) DESC, v.seq DESC, v.occurred_at DESC, v.event_id DESC"
    assert good in sql
    view.write_text(sql.replace(good, "ORDER BY v.occurred_at DESC, v.event_id DESC"), encoding="utf-8")
    rows = [("e10", "2026-09-29 12:00:00", "BACKFILLING", 10), ("e11", "2026-09-29 09:00:00", "RECONCILING", 11)]
    assert _cur(rows, pkg)[0]["seq"] == 10                        # мутант выбирает не то событие
    assert _cur(rows)[0]["seq"] == 11                             # настоящее представление — верное
