"""Tenancy T4.1 — отдельная идентичность развёртывания SQL арендатора (sa-sql-deployer).

Модель прав BigQuery (PermBQ) выведена из документации: создание VIEW — tables.create на
целевом датасете и tables.getData на КАЖДОМ источнике; изменение — tables.update (с учётом
условия записи ACL по resource.name) и tables.get; список — tables.list; чтение — tables.get.
Права деплоера в модели берутся ТОЛЬКО из sql_identity (роли и матрица), поэтому модель
проверяет, что утверждённых прав достаточно для пути VIEW и недостаточно ни для чего больше.

18 состязательных случаев ACK T4.1 помечены A01…A18.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path

import pytest

from tools.autonomy import wif_domains as WD
from tools.tenancy import iam_proposals as IP
from tools.tenancy import plan_scan as PS
from tools.tenancy import platform as PL
from tools.tenancy import platform_roles as PR
from tools.tenancy import sql_deploy as SD
from tools.tenancy import sql_identity as SI
from tools.tenancy import sql_package as SP
from tools.tenancy import synthetic as SY
from tools.tenancy import tenant_bootstrap as TB
from tools.tests.test_tenancy_t32_remediation import _acl, _plan_for

REPO = Path(__file__).resolve().parents[2]
WF = (REPO / ".github" / "workflows" / "tenant-infra.yml").read_text(encoding="utf-8")
TENANT = "client_001"
C1 = SY.fixture_contract("client_001")
C2 = SY.fixture_contract("client_002")
P1, P2 = C1["project_id"], C2["project_id"]
DEP1, DEP2 = SI.deployer_email(P1), SI.deployer_email(P2)
OWNER_ENV = {"GITHUB_ACTOR_ID": PL.GITHUB_OWNER_ID, "GITHUB_RUN_ATTEMPT": "1"}


# ═══════════════════════════════════════ модель прав BigQuery для деплоера
def deployer_permissions(contract: dict, dataset: str, table: str | None) -> set[str]:
    """Эффективные права деплоера на датасете (table=None) или таблице — только из sql_identity."""
    by_id = {v: k for k, v in contract["datasets"].items()}
    key = by_id.get(dataset)
    if key is None:
        return set()
    got = set()
    for role_id, conditional in SI.GRANT_MATRIX[key]:
        perms = set(SI.SQL_ROLES[role_id]["permissions"])
        if conditional:
            # Условие проверяет resource.name таблицы: на уровне датасета оно ложно.
            if table is None or not table.startswith(SI.CONDITIONAL_VIEW_PREFIX):
                continue
        got |= perms
    return got


class PermBQ:
    """Tables API с проверкой прав деплоера. Ответ 403 — как у BigQuery, без записи."""

    def __init__(self, contract, fail_insert_at=None, principal_project=None):
        self.c, self.p = contract, contract["project_id"]
        self.store = {(contract["datasets"][t["dataset_key"]], t["table_id"]): {"type": "TABLE"}
                      for t in contract["tables"]}
        self.calls, self.inserts, self.fail_insert_at = [], 0, fail_insert_at

    def _can(self, perm, ds, table=None):
        return perm in deployer_permissions(self.c, ds, table)

    def get(self, ds, name):
        self.calls.append(("get", ds, name))
        if not self._can("bigquery.tables.get", ds, name):
            return 403, {}
        r = self.store.get((ds, name))
        return (200, copy.deepcopy(r)) if r else (404, {})

    def list(self, ds):
        self.calls.append(("list", ds))
        if not self._can("bigquery.tables.list", ds):
            SD._fail(f"tables.list {ds}: HTTP 403")
        names = [n for (d, n) in self.store if d == ds]
        return names, {n: self.store[(ds, n)]["type"] for n in names}

    def _sources_ok(self, query):
        import sqlglot
        from sqlglot import exp
        tree = sqlglot.parse_one(query, read="bigquery")
        ctes = {c.alias_or_name for c in tree.find_all(exp.CTE)}
        for tb in tree.find_all(exp.Table):
            if not tb.catalog and not tb.db and tb.name in ctes:
                continue
            if not tb.catalog:
                continue
            if tb.catalog != self.p or not self._can("bigquery.tables.getData", tb.db, tb.name):
                return False
        return True

    def insert(self, ds, res):
        self.calls.append(("insert", ds, res["tableReference"]["tableId"]))
        self.inserts += 1
        if self.fail_insert_at and self.inserts == self.fail_insert_at:
            return 503, {}                                  # обрыв посреди развёртывания
        if not self._can("bigquery.tables.create", ds) or not self._sources_ok(res["view"]["query"]):
            return 403, {}
        self.store[(ds, res["tableReference"]["tableId"])] = {
            "type": "VIEW", "description": res["description"], "view": dict(res["view"])}
        return 200, {}

    def update(self, ds, name, res):
        self.calls.append(("update", ds, name))
        if not self._can("bigquery.tables.update", ds, name) or not self._sources_ok(res["view"]["query"]):
            return 403, {}
        self.store[(ds, name)] = {"type": "VIEW", "description": res["description"], "view": dict(res["view"])}
        return 200, {}

    def test_permissions(self, ds, name, permissions):
        self.calls.append(("testIamPermissions", ds, name))
        return set(permissions) & deployer_permissions(self.c, ds, name)


@pytest.fixture
def rendered(tmp_path, monkeypatch):
    from tools.tenancy import registry as R
    monkeypatch.setattr(R, "terraform_inputs", lambda t: SY.fixture_contract(t))
    out = tmp_path / "sql"
    SP.render(TENANT, out)
    env = {"EXPECTED_MANIFEST_SHA256": hashlib.sha256((out / SD.MANIFEST).read_bytes()).hexdigest(),
           "EXPECTED_PACKAGE_SHA256": SD.rendered_package_sha256(out)}
    return out, env


@pytest.fixture
def specs(rendered):
    out, env = rendered
    return SD.load_package(TENANT, out, C1, env)


# ═══════════════════════════════════════ доверенная база: роли и матрица
def test_role_definitions_are_exactly_the_approved_ones():
    assert {r: set(v["permissions"]) for r, v in SI.SQL_ROLES.items()} == {
        "mpaSqlSourceRead": {"bigquery.tables.get", "bigquery.tables.getData"},
        "mpaSqlViewCreate": {"bigquery.tables.create", "bigquery.tables.get", "bigquery.tables.list"},
        "mpaSqlViewUpdate": {"bigquery.tables.update", "bigquery.tables.get"}}
    assert all(v["stage"] == "GA" for v in SI.SQL_ROLES.values())
    assert all(len(v["description"].encode()) <= 256 and len(v["title"].encode()) <= 100 for v in SI.SQL_ROLES.values())


def test_no_role_contains_a_never_permission_nor_datasets_get():
    every = set().union(*(v["permissions"] for v in SI.SQL_ROLES.values()))
    assert not every & SI.NEVER_FOR_DEPLOYER
    assert "bigquery.datasets.get" not in every          # решение владельца: не добавлять заранее


def test_grant_matrix_is_exactly_the_ten_entries():
    g = SI.dataset_grants(P1, C1["datasets"])
    assert len(g) == 10
    assert sorted((x["dataset_key"], x["role"].rsplit("/", 1)[1], x["condition"] is not None) for x in g) == sorted([
        ("analytics_share", "mpaSqlViewCreate", False), ("analytics_share", "mpaSqlViewUpdate", False),
        ("ozon_mart", "mpaSqlSourceRead", False), ("ozon_mart", "mpaSqlViewCreate", False),
        ("ozon_mart", "mpaSqlViewUpdate", False), ("ozon_raw", "mpaSqlSourceRead", False),
        ("ref", "mpaSqlSourceRead", False), ("tenant_ops", "mpaSqlSourceRead", False),
        ("tenant_ops", "mpaSqlViewCreate", False), ("tenant_ops", "mpaSqlViewUpdate", True)])
    assert all(x["role"].startswith(f"organizations/{PL.ORGANIZATION_ID}/roles/mpaSql") for x in g)


def test_condition_is_positive_and_names_type_service_and_view_prefix():
    e = SI.view_prefix_condition(P1, "tenant_ops")["expression"]
    assert e == ('resource.type == "bigquery.googleapis.com/Table" && resource.service == "bigquery.googleapis.com" '
                 '&& resource.name.startsWith("projects/mpa-t-client-001/datasets/tenant_ops/tables/V_")')
    assert "!=" not in e and "!" not in e.replace("!=", "")


def test_terraform_platform_constants_match_trusted_base():
    tf = (REPO / "infra" / "tenant" / "platform.tf").read_text(encoding="utf-8")
    assert f'organization_id   = "{PL.ORGANIZATION_ID}"' in tf
    assert f'sql_deployer_account_id = "{SI.DEPLOYER_ACCOUNT_ID}"' in tf
    assert "sql_roles               = [" + ", ".join(f'"{r}"' for r in sorted(SI.SQL_ROLES)) + "]" in tf
    assert f'sql_view_prefix         = "{SI.CONDITIONAL_VIEW_PREFIX}"' in tf


def test_terraform_grant_matrix_and_condition_text_match_trusted_base():
    tf = (REPO / "infra" / "tenant" / "platform.tf").read_text(encoding="utf-8")
    block = tf[tf.index("sql_grant_matrix = ["):]
    block = block[:block.index("]")]
    tf_matrix = set(re.findall(r'"([a-z_]+\|mpaSql[A-Za-z]+\|(?:true|false))"', block))
    py_matrix = {f"{k}|{r}|{str(c).lower()}" for k, roles in SI.GRANT_MATRIX.items() for r, c in roles}
    assert tf_matrix == py_matrix and len(py_matrix) == 10
    assert f'sql_condition_title     = "{SI.CONDITION_TITLE}"' in tf
    assert f'sql_condition_desc      = "{SI.CONDITION_DESCRIPTION}"' in tf


def test_contract_block_is_derived_not_configured():
    for c in (C1, C2):
        assert c["sql_deployer"] == SI.contract_block(c["project_id"], c["datasets"])
        assert c["sql_deployer"]["email"] == SI.deployer_email(c["project_id"])


# ═══════════════════════════════════════ инварианты контракта и пакета
def test_tenant_ops_package_views_all_carry_the_conditional_prefix():
    names = [p.stem for p in (SP.PACKAGE_DIR / "tenant_ops").glob("*.sql")]
    assert names and all(n.startswith(SI.CONDITIONAL_VIEW_PREFIX) for n in names)


def test_no_contract_table_anywhere_carries_the_conditional_prefix():
    for c in (C1, C2):
        assert not [t for t in c["tables"] if t["table_id"].startswith(SI.CONDITIONAL_VIEW_PREFIX)]


def test_unconditional_update_datasets_hold_no_contract_tables():
    """tables.update без условия (ozon_mart, analytics_share) безопасен, только пока там нет таблиц."""
    for c in (C1, C2):
        assert not [t for t in c["tables"] if t["dataset_key"] in ("ozon_mart", "analytics_share")]


def test_package_never_reads_the_client_layer():
    """analytics_share без mpaSqlSourceRead: самоссылка клиентского слоя дала бы 403 — запрещаем раньше."""
    for p in (SP.PACKAGE_DIR / "analytics_share").glob("*.sql"):
        body = "\n".join(l for l in p.read_text(encoding="utf-8").splitlines() if "CREATE OR REPLACE VIEW" not in l)
        assert "__tenant__.analytics_share." not in body, p.name


def test_load_package_rejects_a_tenant_ops_view_outside_the_prefix(rendered):
    out, env = rendered
    c = copy.deepcopy(C1)
    c["tables"].append(dict(c["tables"][0], dataset_key="tenant_ops", table_id="V_SHADOW"))
    with pytest.raises(SD.SqlDeployError, match="попадает под условие"):
        SD.load_package(TENANT, out, c, env)


# ═══════════════════════════════════════ A01–A18
def test_A01_provisioner_cannot_get_data_anywhere():
    assert "bigquery.tables.getData" not in IP.PROVISIONER_ROLE["permissions"]
    assert all(PL.PROVISIONER_SA not in repr(a) for a in PS.expected_dataset_access(C1).values())
    plan = _plan_for(C1)
    ds = next(r for r in plan["resource_changes"] if r["type"] == "google_bigquery_dataset")
    ds["change"]["after"]["access"].append(_acl(SI.role_name(SI.SOURCE_READ), user_by_email=PL.PROVISIONER_SA))
    assert any("провижионер" in f for f in PS.scan_plan(plan, C1))
    with pytest.raises(SD.SqlDeployError, match="ожидается деплоер"):
        SD.check_principal("t", P1, lookup=lambda _t: PL.PROVISIONER_SA)


def test_A02_deployer_rights_are_sufficient_for_the_whole_view_path(specs):
    bq = PermBQ(C1)
    SD.probe_permissions(specs, bq, C1)
    done = SD.deploy(specs, bq, P1)
    assert [op for op, _ in done] == ["insert"] * 32
    SD.verify_live(specs, bq, C1)
    SD.probe_permissions(specs, bq, C1)
    assert {op for op, _ in SD.deploy(specs, bq, P1)} == {"noop"}
    s = next(x for x in specs if x.dataset == "tenant_ops")
    bq.store[(s.dataset, s.name)]["view"]["query"] += "\n-- дрейф"
    assert [op for op, _ in SD.deploy(specs, bq, P1) if op != "noop"] == ["update"]  # условие V_* пропускает


def test_A03_deployer_has_no_path_to_evetis():
    assert deployer_permissions(C1, "wb_mart", None) == set()
    with pytest.raises(SD.SqlDeployError):
        SD._check_references(f"SELECT a FROM `{PL.EVETIS_PROJECT_ID}.wb_mart.MART_SKU_DAILY`", P1,
                             set(C1["datasets"].values()), "t")
    bq = PermBQ(C1)
    res = {"tableReference": {"tableId": "X"}, "description": "",
           "view": {"query": f"SELECT 1 FROM `{PL.EVETIS_PROJECT_ID}.wb_raw.RAW_WB_ORDERS`", "useLegacySql": False}}
    assert bq.insert("ozon_mart", res)[0] == 403
    assert TB.check_evetis_acls({"wb_mart": [{"role": "READER", "userByEmail": DEP1}]}, DEP1)


def test_A04_deployer_has_no_path_to_another_tenant():
    assert {g["role"] for g in SI.dataset_grants(P2, C2["datasets"])} == {g["role"] for g in SI.dataset_grants(P1, C1["datasets"])}
    assert SI.view_prefix_condition(P2, "tenant_ops")["expression"] != SI.view_prefix_condition(P1, "tenant_ops")["expression"]
    with pytest.raises(SD.SqlDeployError, match="ожидается деплоер"):
        SD.check_principal("t", P1, lookup=lambda _t: DEP2)
    plan = _plan_for(C1)
    ds = next(r for r in plan["resource_changes"] if r["type"] == "google_bigquery_dataset")
    ds["change"]["after"]["access"].append(_acl(SI.role_name(SI.SOURCE_READ), user_by_email=DEP2))
    assert any("mpa-t-client-002" in f for f in PS.scan_plan(plan, C1))
    res = {"tableReference": {"tableId": "X"}, "description": "",
           "view": {"query": f"SELECT 1 FROM `{P2}.ozon_raw.RAW_OZON_CATALOG`", "useLegacySql": False}}
    assert PermBQ(C1).insert("ozon_mart", res)[0] == 403


@pytest.mark.parametrize("perm", ["bigquery.tables.updateData", "bigquery.tables.delete"])
def test_A05_A06_deployer_cannot_change_or_delete_raw_rows_or_tables(specs, perm):
    for t in C1["tables"]:
        assert perm not in deployer_permissions(C1, C1["datasets"][t["dataset_key"]], t["table_id"])
    src = (REPO / "tools" / "tenancy" / "sql_deploy.py").read_text(encoding="utf-8")
    assert '"DELETE"' not in src and "insertAll" not in src and not re.search(r'/data["?/]', src)
    leaky = PermBQ(C1)
    leaky.test_permissions = lambda ds, n, p: set(p) & (deployer_permissions(C1, ds, n) | {perm})
    with pytest.raises(SD.SqlDeployError, match="таблице контракта"):
        SD.probe_permissions(specs, leaky, C1)


def test_A05b_update_on_platform_tables_is_denied_by_the_condition():
    for t in (x for x in C1["tables"] if x["dataset_key"] == "tenant_ops"):
        assert "bigquery.tables.update" not in deployer_permissions(C1, "tenant_ops", t["table_id"]), t["table_id"]
    assert "bigquery.tables.update" in deployer_permissions(C1, "tenant_ops", "V_COVERAGE_DAILY")
    assert "bigquery.tables.update" not in deployer_permissions(C1, "ozon_raw", "RAW_OZON_POSTINGS_FBO")


def test_A07_deployer_cannot_create_datasets():
    every = set().union(*(v["permissions"] for v in SI.SQL_ROLES.values()))
    assert not {p for p in every if p.startswith("bigquery.datasets.")}
    src = (REPO / "tools" / "tenancy" / "sql_deploy.py").read_text(encoding="utf-8")
    assert "/datasets\"" not in src and "datasets?" not in src


def test_A08_deployer_cannot_change_iam():
    every = set().union(*(v["permissions"] for v in SI.SQL_ROLES.values()))
    assert not {p for p in every if "IamPolicy" in p}
    plan = _plan_for(C1)
    plan["resource_changes"].append({"address": "google_project_iam_member.x", "mode": "managed",
                                     "type": "google_project_iam_member", "name": "x", "change": {
                                         "actions": ["create"], "before": None, "after_unknown": {},
                                         "after": {"project": P1, "role": "roles/bigquery.dataViewer",
                                                   "member": f"serviceAccount:{DEP1}"}}})
    assert any("не входит в разрешённые" in f for f in PS.scan_plan(plan, C1))
    assert TB.check_foreign_policy(f"projects/{P1}", {"bindings": [
        {"role": "roles/bigquery.dataViewer", "members": [f"serviceAccount:{DEP1}"]}]}, DEP1)


def test_A09_deployer_cannot_read_secrets():
    assert "secretmanager.versions.access" in SI.NEVER_FOR_DEPLOYER
    assert ("google_secret_manager_secret_iam_member", "seller-api-key", "roles/secretmanager.secretAccessor",
            f"serviceAccount:{DEP1}") not in PS.expected_iam(C1)


def test_A10_deployer_cannot_invoke_runtime():
    assert {"run.jobs.run", "cloudscheduler.jobs.run"} <= SI.NEVER_FOR_DEPLOYER
    assert not [t for t in PS.expected_iam(C1) if DEP1 in t[3]]


@pytest.mark.parametrize("cid", ["T11", "T12", "T13"])
def test_A11_A12_A13_bot_rerun_and_other_actor_get_no_deployer(cid):
    dom = next(d for d in WD.load_inventory()["domains"] if d["id"] == "mpa-tenant-infra")
    cfg = WD.desired_config(dom)
    case = next(c for c in WD.tenant_cases() if c.id == cid)
    assert cfg.obtainable(case.claims) == set()


def test_A11_legit_owner_run_obtains_the_deployer_only_through_the_same_strict_condition():
    dom = next(d for d in WD.load_inventory()["domains"] if d["id"] == "mpa-tenant-infra")
    rows = WD.verify_domain(WD.desired_config(dom), dom)
    assert all(r["status"] == "PASS" for r in rows)
    assert DEP1 in WD.privileged(dom)


@pytest.mark.parametrize("env", [{"GITHUB_ACTOR_ID": "12345678", "GITHUB_RUN_ATTEMPT": "1"},
                                 {"GITHUB_ACTOR_ID": PL.GITHUB_OWNER_ID, "GITHUB_RUN_ATTEMPT": "2"}])
def test_A12_A13_writer_gate_in_the_helper(env):
    with pytest.raises(SD.SqlDeployError):
        SD.check_writer(env)


def test_A14_no_arbitrary_sql_or_ddl_path():
    src = (REPO / "tools" / "tenancy" / "sql_deploy.py").read_text(encoding="utf-8")
    code = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
    for bad in ("/jobs", "/queries", "PATCH", ":setIamPolicy", ":getIamPolicy", "insertAll"):
        assert bad not in code, bad
    methods = set(re.findall(r'self\._call\("([A-Z]+)"', src))
    assert methods == {"GET", "POST", "PUT"}
    assert re.findall(r'self\._call\("POST", ([^\n]+)', src) == [
        "self._t(dataset), resource)", 'self._t(dataset, name) + ":testIamPermissions",']
    every = set().union(*(v["permissions"] for v in SI.SQL_ROLES.values()))
    assert "bigquery.jobs.create" not in every


def test_A15_hash_mismatch_fails_closed(rendered):
    out, env = rendered
    with pytest.raises(SD.SqlDeployError, match="хеш"):
        SD.load_package(TENANT, out, C1, dict(env, EXPECTED_PACKAGE_SHA256="0" * 64))


def test_A16_non_view_target_fails_closed(specs):
    bq = PermBQ(C1)
    s = next(x for x in specs if x.dataset == "tenant_ops")
    bq.store[(s.dataset, s.name)] = {"type": "TABLE"}
    with pytest.raises(SD.SqlDeployError, match="не VIEW"):
        SD.deploy(specs, bq, P1)
    assert not [c for c in bq.calls if c[0] in ("insert", "update")]


def test_A17_reference_outside_approved_datasets_fails_closed():
    for ref in (f"`{P1}.customer_share.T`", f"`{P1}.region-eu.INFORMATION_SCHEMA.JOBS`", "`other-proj-1.ozon_raw.T`"):
        with pytest.raises(SD.SqlDeployError):
            SD._check_references(f"SELECT a FROM {ref}", P1, set(C1["datasets"].values()), "t")


def test_A18_partial_deploy_is_detected_and_recovers_idempotently(specs):
    bq = PermBQ(C1, fail_insert_at=11)
    with pytest.raises(SD.SqlDeployError, match="HTTP 503"):
        SD.deploy(specs, bq, P1)
    with pytest.raises(SD.SqlDeployError, match="расходятся"):
        SD.verify_live(specs, bq, C1)
    bq.fail_insert_at = None
    done = SD.deploy(specs, bq, P1)
    assert [op for op, _ in done].count("noop") == 10 and [op for op, _ in done].count("insert") == 22
    SD.verify_live(specs, bq, C1)


# ═══════════════════════════════════════ sql_deploy: живые проверки
def test_verify_live_accepts_platform_tables_but_rejects_any_extra_object(specs):
    bq = PermBQ(C1)
    SD.deploy(specs, bq, P1)
    SD.verify_live(specs, bq, C1)                  # 7 таблиц tenant_ops — ожидаемы как TABLE
    bq.store[("tenant_ops", "V_EXTRA")] = {"type": "TABLE"}
    with pytest.raises(SD.SqlDeployError, match="расходятся"):
        SD.verify_live(specs, bq, C1)
    del bq.store[("tenant_ops", "V_EXTRA")]
    bq.store[("tenant_ops", "DATA_COVERAGE")] = {"type": "VIEW"}
    with pytest.raises(SD.SqlDeployError, match="неожиданный тип"):
        SD.verify_live(specs, bq, C1)


def test_expiration_on_a_package_view_is_drift(specs):
    bq = PermBQ(C1)
    SD.deploy(specs, bq, P1)
    s = specs[-1]
    bq.store[(s.dataset, s.name)]["expirationTime"] = "1790000000000"
    with pytest.raises(SD.SqlDeployError, match="расходится"):
        SD.verify_live(specs, bq, C1)


def test_probe_fails_closed_when_source_read_is_missing(specs):
    bq = PermBQ(C1)
    bq.test_permissions = lambda ds, n, p: set(p) & deployer_permissions(C1, ds, n) - {"bigquery.tables.getData"}
    with pytest.raises(SD.SqlDeployError, match="чтение строк"):
        SD.probe_permissions(specs, bq, C1)


@pytest.mark.parametrize("extra", ["bigquery.tables.setIamPolicy", "bigquery.tables.getIamPolicy",
                                   "bigquery.tables.export", "bigquery.tables.createSnapshot"])
def test_probe_fails_closed_on_table_iam_or_export(specs, extra):
    bq = PermBQ(C1)
    SD.deploy(specs, bq, P1)
    bq.test_permissions = lambda ds, n, p: set(p) & (deployer_permissions(C1, ds, n) | {extra})
    with pytest.raises(SD.SqlDeployError):
        SD.probe_permissions(specs, bq, C1)


def test_probe_fails_closed_when_client_layer_views_are_readable(specs):
    """Проектная dataViewer деплоеру дала бы getData на analytics_share — матрица это запрещает."""
    bq = PermBQ(C1)
    SD.deploy(specs, bq, P1)
    bq.test_permissions = lambda ds, n, p: set(p) & (deployer_permissions(C1, ds, n) | {"bigquery.tables.getData"})
    with pytest.raises(SD.SqlDeployError, match="analytics_share"):
        SD.probe_permissions(specs, bq, C1)


def test_probe_fails_closed_when_views_get_delete(specs):
    bq = PermBQ(C1)
    SD.deploy(specs, bq, P1)
    bq.test_permissions = lambda ds, n, p: set(p) & (deployer_permissions(C1, ds, n) | {"bigquery.tables.delete"})
    with pytest.raises(SD.SqlDeployError):
        SD.probe_permissions(specs, bq, C1)


def test_no_quota_project_header_and_principal_is_checked(monkeypatch):
    sent = []

    class Resp:
        status = 200

        def read(self):
            return b"{}"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    monkeypatch.setattr(SD.urllib.request, "urlopen", lambda req, timeout: sent.append(req) or Resp())
    SD.BigQueryTables("tok", P1).get("ozon_mart", "X")
    assert sent and {k.lower() for k in sent[0].headers} == {"authorization", "content-type"}
    assert SD.check_principal("t", P1, lookup=lambda _t: DEP1) == DEP1
    for bad in ("", DEP2, PL.PROVISIONER_SA):
        with pytest.raises(SD.SqlDeployError):
            SD.check_principal("t", P1, lookup=lambda _t, b=bad: b)


def test_main_checks_writer_before_any_network(monkeypatch, rendered):
    out, env = rendered
    monkeypatch.setattr(SD, "token_principal", lambda _t: (_ for _ in ()).throw(AssertionError("сеть")))
    monkeypatch.setenv("GCP_ACCESS_TOKEN", "x")
    for k, v in {**env, "GITHUB_ACTOR_ID": "1", "GITHUB_RUN_ATTEMPT": "1"}.items():
        monkeypatch.setenv(k, v)
    from tools.tenancy import tenant_infra as TI
    monkeypatch.setattr(TI, "terraform_inputs", lambda t: SY.fixture_contract(t))
    assert SD.main(["deploy", TENANT, str(out)]) == 1


# ═══════════════════════════════════════ workflow: идентичность job'а sql
def _job(name):
    m = re.search(rf"^  {name}:\n(.*?)(?=^  [a-z]+:\n|\Z)", WF, re.S | re.M)
    assert m, name
    return m.group(1)


def test_sql_job_authenticates_as_the_registry_deployer_not_the_provisioner():
    sql = _job("sql")
    assert "sa-tenant-provisioner" not in sql
    assert "service_account: ${{ steps.ident.outputs.email }}" in sql
    assert 'python tools/tenancy/sql_identity.py deployer-email "$TENANT_ID" >> "$GITHUB_OUTPUT"' in sql
    assert "https://www.googleapis.com/auth/userinfo.email" in sql
    assert sql.index("verify-package") < sql.index("google-github-actions/auth")


def test_plan_and_apply_jobs_keep_the_provisioner():
    for job in ("plan", "apply"):
        assert "service_account: sa-tenant-provisioner@mpa-platform.iam.gserviceaccount.com" in _job(job)
        assert "sa-sql-deployer" not in _job(job)


def test_deployer_email_cli(capsys, monkeypatch):
    from tools.tenancy import tenant_infra as TI
    monkeypatch.setattr(TI, "terraform_inputs", lambda t: SY.fixture_contract(t))
    assert SI.main(["deployer-email", "client_002"]) == 0
    assert capsys.readouterr().out.strip() == f"email={DEP2}"


# ═══════════════════════════════════════ сканер плана: правило D с условием и правило Q
def test_scanner_accepts_the_exact_deployer_acl():
    assert PS.scan_plan(_plan_for(C1), C1) == []
    assert PS.scan_plan(_plan_for(C2), C2) == []


def _tenant_ops_entries(plan):
    ds = next(r for r in plan["resource_changes"] if r["type"] == "google_bigquery_dataset"
              and r["change"]["after"]["dataset_id"] == "tenant_ops")
    return ds["change"]["after"]["access"]


@pytest.mark.parametrize("mutate,why", [
    (lambda a: [x for x in a if x["condition"]][0].update(condition=[]), "условие снято"),
    (lambda a: [x for x in a if x["condition"]][0]["condition"][0].update(
        expression=[x for x in a if x["condition"]][0]["condition"][0]["expression"].replace("/tables/V_", "/tables/")),
     "условие расширено"),
    (lambda a: [x for x in a if x["condition"]][0]["condition"][0].update(location="x.tf"), "location в условии"),
    (lambda a: [x for x in a if x["condition"]][0].update(role="roles/bigquery.dataEditor"), "предопределённая роль"),
    (lambda a: a.append(_acl(SI.role_name(SI.VIEW_UPDATE), user_by_email=DEP1)), "лишняя безусловная запись"),
    (lambda a: a.remove([x for x in a if x["role"].endswith("mpaSqlSourceRead")][0]), "нет обязательной записи"),
    (lambda a: a.append(_acl("organizations/1043233412973/roles/mpaTenantProvisioner", user_by_email=DEP1)),
     "роль провижионера деплоеру"),
    (lambda a: a.append(_acl(SI.role_name(SI.SOURCE_READ), iam_member=f"serviceAccount:{DEP1}")), "другая форма принципала"),
])
def test_scanner_rejects_any_deviation_of_the_deployer_acl(mutate, why):
    plan = _plan_for(C1)
    mutate(_tenant_ops_entries(plan))
    assert PS.scan_plan(plan, C1), why


def test_scanner_rejects_a_foreign_service_account_and_a_tampered_contract_block():
    plan = _plan_for(C1)
    sa = next(r for r in plan["resource_changes"] if r["address"] == "google_service_account.sql_deployer")
    sa["change"]["after"].update(account_id="sa-sql-admin", email=f"sa-sql-admin@{P1}.iam.gserviceaccount.com",
                                 member=f"serviceAccount:sa-sql-admin@{P1}.iam.gserviceaccount.com")
    assert any("не входит в контракт" in f for f in PS.scan_plan(plan, C1))
    c = copy.deepcopy(C1)
    c["sql_deployer"]["grants"][0]["role"] = "roles/bigquery.dataViewer"
    assert any("sql_deployer" in f for f in PS.scan_plan(_plan_for(c), c))


# ═══════════════════════════════════════ platform_roles verify
def _live_roles():
    return {r: {"name": SI.role_name(r), "title": v["title"], "description": v["description"], "stage": "GA",
                "includedPermissions": sorted(v["permissions"]), "etag": "BwZ="} for r, v in SI.SQL_ROLES.items()}


def test_platform_roles_accepts_the_live_shape():
    assert PR.check_roles(_live_roles(), [{"name": SI.role_name(r)} for r in SI.SQL_ROLES]) == []


@pytest.mark.parametrize("mutate,needle", [
    (lambda d: d.update(mpaSqlSourceRead=None), "роли нет"),
    (lambda d: d["mpaSqlViewCreate"].update(deleted=True), "удалена"),
    (lambda d: d["mpaSqlViewUpdate"].update(stage="BETA"), "стадия"),
    (lambda d: d["mpaSqlSourceRead"]["includedPermissions"].append("bigquery.datasets.get"), "лишние"),
    (lambda d: d["mpaSqlViewCreate"]["includedPermissions"].remove("bigquery.tables.list"), "не хватает"),
    (lambda d: d["mpaSqlViewUpdate"]["includedPermissions"].append("bigquery.tables.updateData"), "запрещённые"),
    (lambda d: d["mpaSqlViewUpdate"].update(title="x"), "title"),
])
def test_platform_roles_fails_closed(mutate, needle):
    d = _live_roles()
    mutate(d)
    assert any(needle in f for f in PR.check_roles(d, []))


def test_platform_roles_rejects_an_unknown_mpa_sql_role_and_unreadable_state():
    assert PR.check_roles(_live_roles(), [{"name": SI.ORG_ROLE_PREFIX + "mpaSqlAdmin"}])

    def run(*a):
        return (1, "ERROR: PERMISSION_DENIED iam.roles.get")
    assert PR.main(["verify"], run=run) == 1


# ═══════════════════════════════════════ tenant_bootstrap: ожидаемая привязка и её сверка
def _sa_ok():
    return {"email": DEP1, "disabled": False}, [{"keyType": "SYSTEM_MANAGED"}], {
        "bindings": SI.expected_sa_bindings(), "etag": "BwX="}


def test_bootstrap_expected_binding_is_the_provisioner_principal_set():
    snap = json.loads((REPO / "quality" / "autonomy" / "wif_tenant_pool_snapshot_2026-09-28.json").read_text())
    assert SI.expected_sa_bindings() == snap["service_account_bindings"]["sa-tenant-provisioner"]
    assert TB.check_sa(*_sa_ok()) == []


@pytest.mark.parametrize("mutate,needle", [
    (lambda s, k, p: (None, k, p), "не существует"),
    (lambda s, k, p: (dict(s, disabled=True), k, p), "выключен"),
    (lambda s, k, p: (s, [{"keyType": "USER_MANAGED"}], p), "ключи"),
    (lambda s, k, p: (s, k, {"bindings": []}), "bind не выполнен"),
    (lambda s, k, p: (s, k, {"bindings": [dict(SI.expected_sa_bindings()[0],
                                               members=[SI.WIF_MEMBER, "user:x@example.com"])]}), "≠ ожидаемой"),
    (lambda s, k, p: (s, k, {"bindings": SI.expected_sa_bindings() + [
        {"role": "roles/iam.serviceAccountTokenCreator", "members": ["user:x@example.com"]}]}), "≠ ожидаемой"),
    (lambda s, k, p: (s, k, {"bindings": [dict(SI.expected_sa_bindings()[0],
                                               condition={"expression": "true"})]}), "≠ ожидаемой"),
])
def test_bootstrap_verify_fails_closed_on_sa_drift(mutate, needle):
    assert any(needle in f for f in TB.check_sa(*mutate(*_sa_ok())))


def test_bootstrap_verify_detects_pool_bindings_on_other_sas_and_foreign_grants():
    other = {f"sa-ozon-runtime@{P1}.iam.gserviceaccount.com": {"bindings": SI.expected_sa_bindings()}}
    assert TB.check_tenant_sa_policies(other, DEP1)
    assert TB.check_tenant_sa_policies({DEP1: {"bindings": SI.expected_sa_bindings()}}, DEP1) == []
    assert TB.check_foreign_policy("folders/x", {"bindings": [{"role": "roles/iam.workloadIdentityUser",
                                                               "members": [SI.WIF_MEMBER]}]}, DEP1)


def _live_acls(project=P1, datasets=None):
    datasets = datasets or C1["datasets"]
    acls = {ds: [{"role": "OWNER", "specialGroup": "projectOwners"}] for ds in datasets.values()}
    for g in SI.dataset_grants(project, datasets):
        e = {"role": g["role"], "userByEmail": SI.deployer_email(project)}
        if g["condition"]:
            e["condition"] = dict(g["condition"])
        acls[datasets[g["dataset_key"]]].append(e)
    return acls


def test_bootstrap_verify_acl_matrix():
    assert TB.check_tenant_acls(P1, C1["datasets"], _live_acls()) == []
    bad = _live_acls()
    next(e for e in bad["tenant_ops"] if e.get("condition"))["condition"]["expression"] = "true"
    assert TB.check_tenant_acls(P1, C1["datasets"], bad)
    bad = _live_acls()
    bad["ozon_raw"].append({"role": "roles/bigquery.dataEditor", "userByEmail": PL.PROVISIONER_SA})
    assert any("провижионер" in f for f in TB.check_tenant_acls(P1, C1["datasets"], bad))
    bad = _live_acls()
    del bad["ref"]
    assert any("не прочитан" in f for f in TB.check_tenant_acls(P1, C1["datasets"], bad))


def test_bootstrap_same_code_verifies_client_002():
    assert TB.check_tenant_acls(P2, C2["datasets"], _live_acls(P2, C2["datasets"])) == []
    assert TB.check_tenant_acls(P1, C1["datasets"], _live_acls(P2, C2["datasets"]))


def test_bind_writes_only_into_an_empty_policy(monkeypatch):
    calls = []
    monkeypatch.setattr(TB, "_sa_state", lambda p, e: ({"email": e}, [], {"bindings": [
        {"role": "roles/iam.serviceAccountUser", "members": ["user:x@example.com"]}], "etag": "E"}))
    monkeypatch.setattr(TB, "_gcloud", lambda *a: calls.append(a))
    with pytest.raises(TB.BootstrapError, match="непустая"):
        TB.bind(C1, execute=True)
    assert not calls


def test_bind_dry_run_writes_nothing(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(TB, "_sa_state", lambda p, e: ({"email": e}, [], {"etag": "E"}))
    monkeypatch.setattr(TB, "_gcloud", lambda *a: calls.append(a))
    assert TB.bind(C1, execute=False) == 0
    assert not calls and "только показ" in capsys.readouterr().out


def test_bootstrap_verify_flags_pool_in_acl_table_iam_and_other_tenants():
    acls = _live_acls()
    acls["ozon_raw"].append({"role": "READER", "iamMember": SI.WIF_MEMBER})
    assert any("прямо в ACL" in f for f in TB.check_tenant_acls(P1, C1["datasets"], acls))
    pol = {"ozon_raw.RAW_OZON_SELLER_INFO": {"bindings": [
        {"role": "roles/bigquery.dataOwner", "members": [f"serviceAccount:{DEP1}"]}]}}
    assert TB.check_table_policies(pol, DEP1)
    assert TB.check_table_policies({"ozon_raw.X": {"bindings": [{"role": "r", "members": [SI.WIF_MEMBER]}]}}, DEP1)
    assert TB.check_table_policies({"ozon_raw.X": {}}, DEP1) == []
    assert TB.check_evetis_acls({"ozon_raw": [{"role": "READER", "userByEmail": DEP1}]}, DEP1, owner=P2)


# ═══════════════════════════════════════ wif_domains --live: непросмотренный проект — не чистый
def test_live_pool_scan_fails_closed_when_a_project_cannot_be_listed(monkeypatch):
    def j(*args):
        if args[:3] == ("iam", "service-accounts", "list") and args[3] == "--project=p-denied":
            raise RuntimeError("gcloud iam service-accounts list: PERMISSION_DENIED")
        if args[:3] == ("iam", "service-accounts", "list"):
            return []
        raise AssertionError(args)
    monkeypatch.setattr(WD, "_j", j)
    with pytest.raises(RuntimeError, match="PERMISSION_DENIED"):
        WD._pool_bindings([{"projectId": "p-ok"}, {"projectId": "p-denied"}], "p-ok", "1", "tenant-infra-pool")


def test_live_pool_scan_skips_only_projects_with_disabled_iam_api(monkeypatch):
    def j(*args):
        if args[3] == "--project=p-off":
            raise RuntimeError("gcloud iam: SERVICE_DISABLED iam.googleapis.com")
        if args[:3] == ("iam", "service-accounts", "list"):
            return [{"email": f"sa-sql-deployer@{P1}.iam.gserviceaccount.com"}]
        return {"bindings": SI.expected_sa_bindings()}
    monkeypatch.setattr(WD, "_j", j)
    got = WD._pool_bindings([{"projectId": "p-off"}, {"projectId": P1}], "mpa-platform",
                            PL.PLATFORM_PROJECT_NUMBER, "tenant-infra-pool")
    assert got == {DEP1: SI.expected_sa_bindings()}
