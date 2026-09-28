"""Tenancy T3.2 — универсальный модуль арендатора и контракт провижининга.

Офлайн: ни облака, ни сети, ни state. Живые части T3.2 (эксперимент backend, паритет
схем с EVETIS) выполнены отдельно и описаны в PR; здесь — то, что CI обязан держать.
"""
from __future__ import annotations

import copy
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from tools.tenancy import iam_proposals as I  # noqa: E402
from tools.tenancy import naming as N  # noqa: E402
from tools.tenancy import ozon_contract as OC  # noqa: E402
from tools.tenancy import ozon_schema_parity as SP  # noqa: E402
from tools.tenancy import platform as PL  # noqa: E402
from tools.tenancy import registry as R  # noqa: E402
from tools.tenancy import synthetic as SY  # noqa: E402
from tools.tenancy import tenant_infra as TI  # noqa: E402

TENANT_ROOT = REPO / "infra" / "tenant"
WORKFLOW = REPO / PL.TENANT_INFRA_WORKFLOW
TF_FILES = sorted(p for p in TENANT_ROOT.rglob("*.tf") if ".terraform" not in p.parts)


def cli(*args):
    return subprocess.run([sys.executable, str(REPO / "tools" / "tenancy" / "registry.py"), *args],
                          capture_output=True, text=True)


# ═══════════════════════════════════════ реестр → Terraform: детерминизм и авторитет
def test_terraform_inputs_is_deterministic_and_sorted():
    a, b = cli("terraform-inputs", "client_001"), cli("terraform-inputs", "client_001")
    assert a.returncode == 0, a.stderr
    assert a.stdout == b.stdout
    doc = json.loads(a.stdout)
    assert a.stdout == json.dumps(doc, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def test_terraform_inputs_contract_shape_for_client_001():
    c = R.terraform_inputs("client_001")
    assert c["contract_version"] == 1 and c["tenant_id"] == "client_001"
    assert c["project_id"] == N.derive_project_id("client_001") == "mpa-t-client-001"
    assert c["project_id_revision"] == 1
    assert c["parent_folder"] == PL.TENANTS_FOLDER
    assert c["state"] == {"bucket": PL.STATE_BUCKET, "prefix": "tenants/client_001"}
    assert c["datasets"] == {"ozon_raw": "ozon_raw", "ref": "ref", "ozon_mart": "ozon_mart",
                             "tenant_ops": "tenant_ops", "analytics_share": "analytics_share",
                                                  "tenant_locks": "tenant_locks"}
    assert c["scheduler_state"] == "PAUSED"
    assert c["labels"] == {"tenant": "client_001", "managed_by": "vts-tenant-infra"}
    ozon = c["marketplaces"]["ozon"]
    assert set(ozon["jobs"]) == {"ozon-runtime-fast", "ozon-runtime-daily", "ozon-runtime-weekly"}
    assert ozon["service_accounts"] == {"runtime": "sa-ozon-runtime", "scheduler": "sa-ozon-scheduler"}
    assert ozon["runtime_image"] == PL.load_runtime_release(REPO)["ozon"]   # выпуск T3.2b, по digest
    assert set(c["marketplaces"]) == {"ozon"}   # WB не включён


@pytest.mark.parametrize("arg", ["mpa-t-client-001", "project-fa311fc0-4d87-4781-986",
                                 "evetis", "client_001 ", "CLIENT_001", "../client_001", ""])
def test_operator_cannot_pass_a_project_id_or_anything_but_a_tenant_id(arg):
    r = cli("terraform-inputs", arg)
    assert r.returncode == 1 and r.stdout == "", (arg, r.stdout)


def test_cli_has_no_project_or_prefix_or_image_argument():
    assert cli("terraform-inputs", "client_001", "--project", "x").returncode == 3
    src = (REPO / "tools" / "tenancy" / "tenant_infra.py").read_text(encoding="utf-8")
    assert "argparse" not in src and "project_id=" not in src.split("def main")[1]


def test_backend_prefix_is_derived_only_from_registry(tmp_path):
    TI.render("client_001", tmp_path)
    backend = json.loads((tmp_path / "backend.json").read_text())
    assert backend == {"bucket": PL.STATE_BUCKET, "prefix": N.terraform_state_prefix("client_001")}
    contract = json.loads((tmp_path / TI.CONTRACT_FILE).read_text())["contract"]
    assert contract == R.terraform_inputs("client_001")


def test_legacy_evetis_cannot_be_exported_to_tenant_terraform():
    with pytest.raises(N.NamingError):
        R.terraform_inputs("evetis")


def test_scheduler_state_other_than_paused_is_refused():
    doc = copy.deepcopy(R.load_tenant("client_001"))
    doc["scheduler_state"] = "ENABLED"
    with pytest.raises(N.NamingError):
        R._terraform_contract(doc)


# ═══════════════════════════════════════ каденция: единый контракт = EVETIS
def _evetis_ozon_jobs():
    tf = (REPO / "infra" / "terraform" / "ozon_ingestion.tf").read_text(encoding="utf-8")
    block = tf.split("ozon_jobs = {", 1)[1].split("\n  }\n", 1)[0]
    jobs = {}
    for name, entities, schedule in re.findall(
            r'"(ozon-runtime-\w+)" = \{\s*entities = "([^"]+)"\s*schedule = "([^"]+)"', block):
        jobs[name] = (tuple(entities.split(",")), schedule)
    names = dict(re.findall(r'"(ozon-runtime-\w+)"\s*=\s*"(ozon-\w+)"',
                            tf.split("ozon_scheduler_names = {", 1)[1].split("}", 1)[0]))
    return jobs, names


def test_ozon_jobs_match_evetis_terraform():
    jobs, names = _evetis_ozon_jobs()
    assert set(jobs) == set(OC.OZON_JOBS) and len(jobs) == 4
    for job, (entities, schedule) in jobs.items():
        assert OC.OZON_JOBS[job]["entities"] == entities
        assert OC.OZON_JOBS[job]["schedule"] == schedule
        assert OC.OZON_JOBS[job]["scheduler"] == names[job]


def test_every_runtime_entity_has_exactly_one_job_and_tables():
    from tools.tenancy.validation import load_ozon_entities
    runtime = load_ozon_entities()
    owners = [e for spec in OC.OZON_JOBS.values() for e in spec["entities"]]
    assert sorted(owners) == sorted(runtime) and len(owners) == len(set(owners))
    assert set(OC.ENTITY_TABLES) == set(runtime)


def test_jobs_for_rejects_an_entity_without_a_job():
    with pytest.raises(OC.ContractError):
        OC.jobs_for(["stocks", "made_up_entity"])


# ═══════════════════════════════════════ таблицы: полнота DDL и паритет
def _runtime_tables_by_entity():
    """Статический разбор runtime: какие таблицы пишет каждая сущность."""
    ent = (REPO / "pipelines/ozon/runtime/entities.py").read_text(encoding="utf-8")
    promo = (REPO / "pipelines/ozon/runtime/promo.py").read_text(encoding="utf-8")
    out = {}
    for fn in re.split(r"\n(?=def )", ent):
        m = re.match(r"def (\w+)", fn)
        if m and m.group(1) in OC.ENTITY_TABLES:
            out[m.group(1)] = set(re.findall(r'"(RAW_OZON_[A-Z_0-9]+)"', fn))
    out["promo"] = set(re.findall(r'TBL_\w+ = "([A-Z_0-9]+)"', promo))
    return out


def test_entity_table_contract_matches_runtime_source():
    scanned = _runtime_tables_by_entity()
    for entity, tables in OC.ENTITY_TABLES.items():
        assert scanned[entity] <= set(tables), (entity, scanned[entity] - set(tables))
    assert '"OZON_INGESTION_RUNS"' in (REPO / "pipelines/ozon/runtime/common.py").read_text()


def test_every_table_any_entity_needs_has_a_git_schema():
    tables = OC.tables_for(OC.ENTITY_TABLES)
    assert len(tables["ozon_raw"]) == 21 and len(tables["ref"]) == 6 and len(tables["tenant_ops"]) == 7   # T5: + OPERATOR_DECISIONS
    assert OC.tables_for(OC.ENTITY_TABLES, include_platform=False)["ref"] == ["REF_SKU_CHANNEL_MAP"]
    for ds, names in tables.items():
        for t in names:
            spec = OC.load_table_spec(ds, t)
            assert spec["schema"], t


@pytest.mark.parametrize("table", ["OZON_INGESTION_RUNS", "RAW_OZON_CLUSTERS", "RAW_OZON_PRICE_COMMISSIONS",
                                   "RAW_OZON_SELLER_INFO", "RAW_OZON_SUPPLIES", "RAW_OZON_SUPPLY_BUNDLES",
                                   "RAW_OZON_SUPPLY_ORDERS"])
def test_seven_previously_uncaptured_tables_now_have_schemas(table):
    spec = OC.load_table_spec("ozon_raw", table)
    names = [f["name"] for f in spec["schema"]]
    assert "ingestion_run_id" in names or table == "OZON_INGESTION_RUNS"
    assert (REPO / "pipelines/ozon/schema/ozon_raw" / f"{table}.json").is_file()


def test_client_001_bootstrap_covers_every_table_its_entities_write():
    c = R.terraform_inputs("client_001")
    have = {t["table_id"] for t in c["tables"]}
    entities = R.load_tenant("client_001")["marketplaces"]["ozon"]["entities"]
    need = {"OZON_INGESTION_RUNS"} | {t for e in entities for t in OC.ENTITY_TABLES[e]}
    need |= {t for names in OC.PLATFORM_TABLES.values() for t in names}          # T4: таблицы платформы
    assert have == need and len(have) == 28           # T5: + ref.OPERATOR_DECISIONS


def test_schema_snapshots_are_project_neutral():
    for p in (REPO / "pipelines" / "ozon" / "schema").rglob("*.json"):
        text = p.read_text(encoding="utf-8")
        for m in PL.EVETIS_FORBIDDEN_MARKERS:
            assert m not in text, (p.name, m)


def test_tenant_tables_carry_no_evetis_descriptions():
    c = R.terraform_inputs("client_001")
    assert "EVETIS" not in json.dumps(c, ensure_ascii=False)
    assert all('"description"' not in t["schema_json"] for t in c["tables"])


def test_schema_parity_offline_holds():
    assert SP.offline_findings() == []


def test_schema_parity_catches_a_drifted_snapshot(monkeypatch):
    real = OC.load_table_spec

    def drifted(ds, t):
        spec = copy.deepcopy(real(ds, t))
        if t == "RAW_OZON_STOCKS":
            spec["schema"] = [dict(f, type="STRING") if f["name"] == "idc" else f for f in spec["schema"]]
        return spec
    monkeypatch.setattr(OC, "load_table_spec", drifted)
    assert any("RAW_OZON_STOCKS.idc" in f for f in SP.offline_findings())


# ═══════════════════════════════════════ образ: только неизменяемый digest платформы
@pytest.mark.parametrize("ref", [
    "", "latest", f"{PL.RUNTIME_REGISTRY}/ozon-runtime:latest", f"{PL.RUNTIME_REGISTRY}/ozon-runtime:v1",
    f"{PL.RUNTIME_REGISTRY}/ozon-runtime@sha256:abc", f"{PL.RUNTIME_REGISTRY}/ozon-runtime@sha256:" + "F" * 64,
    SY.EVETIS_IMAGE, "docker.io/library/python@sha256:" + "a" * 64,
    f"{PL.RUNTIME_REGISTRY}/ozon-runtime@sha256:{'a' * 64}\n"])
def test_mutable_or_foreign_image_is_rejected(ref):
    with pytest.raises(PL.RuntimeReleaseError):
        PL.check_runtime_image(ref)


def test_immutable_platform_digest_is_accepted_and_release_file_is_valid():
    assert PL.check_runtime_image(SY.FIXTURE_IMAGE) == SY.FIXTURE_IMAGE
    release = PL.load_runtime_release(REPO)
    assert set(release) == {"ozon"} and PL.check_runtime_image(release["ozon"]) == release["ozon"]


def test_plan_refuses_without_an_approved_image(tmp_path, monkeypatch):
    # Отказ при null в дескрипторе остаётся (откат выпуска = PR с null или прежним digest).
    monkeypatch.setattr(PL, "load_runtime_release", lambda _repo: {"ozon": None})
    with pytest.raises(TI.TenantInfraError, match="нет утверждённого образа"):
        TI.plan("client_001", tmp_path)


# ═══════════════════════════════════════ корень Terraform: статические гарантии
def _tf_text():
    return "\n".join(p.read_text(encoding="utf-8") for p in TF_FILES)


def test_platform_tf_matches_platform_py():
    tf = (TENANT_ROOT / "platform.tf").read_text(encoding="utf-8")
    for key, value in {"tenants_folder_id": PL.TENANTS_FOLDER_ID, "project_id": PL.PLATFORM_PROJECT_ID,
                       "project_number": PL.PLATFORM_PROJECT_NUMBER, "state_bucket": PL.STATE_BUCKET,
                       "region": PL.RUNTIME_REGION, "runtime_registry": PL.RUNTIME_REGISTRY}.items():
        assert re.search(rf'{key}\s*=\s*"{re.escape(value)}"', tf), key
    markers = re.findall(r'"([^"]+)",', tf.split("evetis_forbidden_markers = [", 1)[1].split("]", 1)[0])
    assert tuple(markers) == PL.EVETIS_FORBIDDEN_MARKERS


def test_tenant_root_never_manages_the_project_billing_folder_or_keys():
    text = _tf_text()
    for forbidden in (r'resource\s+"google_project"\s', r'"google_billing_', r'"google_folder',
                      r'"google_organization', r'"google_project_iam_(policy|binding)"',
                      r'"google_service_account_key"', r'"google_secret_manager_secret_version"',
                      r'"google_storage_', r'"google_artifact_registry_', r'"google_iam_workload_identity',
                      r'"google_cloud_run_v2_job_iam_'):
        assert not re.search(forbidden, text), forbidden


def test_tenant_root_has_no_evetis_and_no_tenant_specific_code():
    """EVETIS в корне арендатора — только как список запретов в platform.tf."""
    text = _tf_text()
    outside = "\n".join(p.read_text(encoding="utf-8") for p in TF_FILES if p.name != "platform.tf")
    code = "\n".join(l for l in outside.splitlines() if not l.strip().startswith("#"))
    for m in PL.EVETIS_FORBIDDEN_MARKERS:
        assert m not in code, m
    assert "client_00" not in text and not re.search(r"tenant_id\s*==\s*\"", text)


def test_guards_cover_parent_billing_activity_and_workspace():
    g = (TENANT_ROOT / "guards.tf").read_text(encoding="utf-8")
    assert "local.platform.tenants_folder_id" in g and "billing_account" in g
    assert "lifecycleState:ACTIVE" in g and 'terraform.workspace == "default"' in g
    for res in ("google_project_service", "google_bigquery_dataset", "module \"ozon\""):
        assert res in (TENANT_ROOT / "main.tf").read_text(encoding="utf-8")
    assert "depends_on = [terraform_data.guard]" in (TENANT_ROOT / "main.tf").read_text(encoding="utf-8")


def test_schedulers_are_paused_as_second_layer_and_destructive_protection_is_on():
    mod = (TENANT_ROOT / "modules" / "ozon_runtime" / "main.tf").read_text(encoding="utf-8")
    assert re.search(r"\n\s*paused\s*=\s*true\n", mod)
    assert "deletion_protection = true" in mod
    assert "deletion_protection      = true" in (TENANT_ROOT / "main.tf").read_text(encoding="utf-8")


def test_provider_quota_goes_to_tenant_project_not_platform():
    v = (TENANT_ROOT / "versions.tf").read_text(encoding="utf-8")
    assert "user_project_override = true" in v and "billing_project       = var.contract.project_id" in v
    assert 'backend "gcs" {}' in v


def test_terraform_tests_cover_both_synthetic_tenants_and_all_negative_fixtures():
    t = (TENANT_ROOT / "tests" / "tenant.tftest.hcl").read_text(encoding="utf-8")
    assert "client_001.contract.json" in t and "client_002.contract.json" in t
    for name in SY.negative_cases():
        assert f'"contract_rejects_{name}"' in t, name
    assert t.count("expect_failures = [terraform_data.guard]") >= 4


def test_fixtures_are_current_registry_output():
    for tid in SY.SYNTHETIC_TENANTS:
        assert (SY.FIXTURE_DIR / f"{tid}.contract.json").read_text(encoding="utf-8") == SY.fixture_text(tid)
    for name, doc in SY.negative_cases().items():
        got = json.loads((SY.FIXTURE_DIR / "negative" / f"{name}.contract.json").read_text(encoding="utf-8"))
        assert got == doc, name


# ═══════════════════════════════════════ переносимость client_001 → client_002
def _shape(c):
    o = c["marketplaces"]["ozon"]
    return ([(t["dataset_key"], t["table_id"], t["schema_json"]) for t in c["tables"]],
            sorted(o["jobs"]), sorted(c["datasets"]), c["apis"], sorted(o["secret_ids"].values()),
            o["service_accounts"])


def test_client_002_same_code_same_shape_different_identity():
    a, b = SY.fixture_contract("client_001"), SY.fixture_contract("client_002")
    assert a["project_id"] != b["project_id"] and a["state"]["prefix"] != b["state"]["prefix"]
    assert _shape(a) == _shape(b)
    for job in a["marketplaces"]["ozon"]["jobs"]:
        ea, eb = a["marketplaces"]["ozon"]["jobs"][job]["env"], b["marketplaces"]["ozon"]["jobs"][job]["env"]
        assert {k for k in ea if ea[k] != eb[k]} == {"GCP_PROJECT_ID"}
    assert not (REPO / "tenants" / "client_002").exists()     # эфемерная фикстура, не арендатор


# ═══════════════════════════════════════ секреты: только имена
def test_rendered_artifacts_contain_secret_names_only(tmp_path):
    from tools.tenancy.validation import detect_credential_material
    TI.render("client_001", tmp_path)
    names = set(N.DEDICATED_OZON_SECRET_IDS.values())
    for f in tmp_path.iterdir():
        doc = json.loads(f.read_text())
        for finding in detect_credential_material(doc, f.name):
            # Ключ OZON_SECRET_* детектор помечает по имени поля — допустимо, только если
            # значение — ИМЯ секрета из naming, а не что-то похожее на значение.
            value = doc
            for part in re.findall(r"\.([^.\[]+)((?:\[\d+\])*)", finding.path):
                value = value[part[0]]
                for idx in re.findall(r"\[(\d+)\]", part[1]):
                    value = value[int(idx)]
            values = set(value.values()) if isinstance(value, dict) else {value}
            if finding.rule == "suspicious_key":
                assert values <= names, finding
            else:   # детектор длинных токенов: допустимы только точные факты платформы
                # (бакет state и утверждённый digest образа из runtime_release.json, T3.2b)
                # T4.1: имена ролей организации mpaSql* и условие V_* — факты доверенной базы.
                from tools.tenancy import sql_identity as SI
                c = json.loads((tmp_path / TI.CONTRACT_FILE).read_text())["contract"]
                facts = {PL.STATE_BUCKET, PL.load_runtime_release(REPO)["ozon"]}
                facts |= {SI.role_name(r) for r in SI.SQL_ROLES}
                from tools.tenancy import control_identity as CI     # T5: роли control plane
                facts |= {SI.role_name(r) for r in CI.CONTROL_ROLES}
                facts.add(SI.view_prefix_condition(c["project_id"], c["datasets"]["tenant_ops"])["expression"])
                assert values <= facts, finding
    contract = json.loads((tmp_path / TI.CONTRACT_FILE).read_text())["contract"]
    assert contract["marketplaces"]["ozon"]["secret_ids"] == N.DEDICATED_OZON_SECRET_IDS
    env = contract["marketplaces"]["ozon"]["jobs"]["ozon-runtime-daily"]["env"]
    assert {k for k in env if k.startswith("OZON_SECRET_")} == set(N.OZON_SECRET_ENV_VARS.values())


# Сканер плана: tools/tests/test_tenancy_t32_remediation.py (правила H1/M1/M2/H2).


# ═══════════════════════════════════════ workflow и WIF
def _wf():
    return WORKFLOW.read_text(encoding="utf-8")


WORKFLOW_INPUTS = ["tenant_id", "operation", "source_run_id", "expected_source_commit", "expected_tfplan_sha256",
                   "expected_contract_sha256", "expected_delta", "expected_manifest_sha256", "expected_package_sha256"]


def _job(wf: str, name: str) -> str:
    """Текст одного job'а (от `  <name>:` до следующего job'а верхнего уровня)."""
    body = wf.split("\njobs:\n", 1)[1]
    parts = re.split(r"(?m)^  ([a-z_]+):\n", body)
    jobs = dict(zip(parts[1::2], parts[2::2]))
    return jobs[name]


def test_workflow_is_dispatch_only_with_the_declared_inputs():
    wf = _wf()
    on_block = wf.split("\non:\n", 1)[1].split("\npermissions:", 1)[0]
    assert re.findall(r"^  (\w+):", on_block, re.M) == ["workflow_dispatch"]
    assert re.findall(r"^      (\w+):", on_block, re.M) == WORKFLOW_INPUTS
    assert "options: [plan, apply, sql-preview, sql-deploy, sql-verify]" in on_block
    for job in ("plan", "verify", "apply", "sql"):
        j = _job(wf, job)
        assert "github.ref == 'refs/heads/main'" in j and "github.event_name == 'workflow_dispatch'" in j, job
        assert 'test "$GITHUB_REF" = "refs/heads/main"' in j, job


def test_workflow_jobs_are_separated_by_operation_and_plan_job_cannot_apply():
    wf = _wf()
    runs = {job: "\n".join(re.findall(r"run: (.+)", _job(wf, job))) + "\n".join(
            re.findall(r"run: \|\n((?:          .+\n)+)", _job(wf, job))) for job in ("plan", "verify", "apply", "sql")}
    assert "inputs.operation == 'plan'" in _job(wf, "plan") and "inputs.operation == 'apply'" in _job(wf, "apply")
    assert "apply" not in runs["plan"] and "tenant_infra.py plan" in runs["plan"]
    assert "tenant_infra.py render" in runs["plan"] and "tenant_deploy.py package-plan" in runs["plan"]
    assert "tenant_deploy.py fetch" in runs["verify"] and "tenant_deploy.py apply" not in runs["verify"]
    assert "tenant_deploy.py apply" in runs["apply"] and "tenant_deploy.py fetch" not in runs["apply"]
    assert "tenant_infra.py plan" not in runs["apply"] + runs["verify"] and "package-plan" not in runs["apply"]
    assert "tenant_deploy" not in runs["sql"] and "sql_deploy.py" in runs["sql"]
    for job, r in runs.items():
        assert "terraform " not in r, job                       # Terraform — только через помощники


def test_workflow_takes_no_command_prefix_or_image_and_never_calls_terraform_directly():
    wf = _wf()
    runs = "\n".join(re.findall(r"run: (.+)", wf))
    assert "terraform " not in runs
    for bad in ("inputs.project", "inputs.prefix", "inputs.image", "inputs.command", "backend-config"):
        assert bad not in wf, bad
    assert "${{ inputs." not in runs and "${{ inputs." not in "\n".join(
        re.findall(r"run: \|\n((?:          .+\n)+)", wf))           # входы — только через env, не в шелл
    assert re.search(r"permissions:\n  contents: read\n  id-token: write\n", wf)


def test_workflow_auth_matches_platform_and_live_wif_condition():
    wf = _wf()
    assert f"workload_identity_provider: {PL.WIF_PROVIDER}" in wf
    assert f"service_account: {PL.PROVISIONER_SA}" in wf
    assert f"{PL.GITHUB_REPOSITORY}/{PL.TENANT_INFRA_WORKFLOW}@refs/heads/main" in PL.WIF_ATTRIBUTE_CONDITION
    assert "assertion.event_name == 'workflow_dispatch'" in PL.WIF_ATTRIBUTE_CONDITION
    assert WORKFLOW.name == "tenant-infra.yml"


def test_tenant_infra_module_has_no_apply_entrypoint():
    src = (REPO / "tools" / "tenancy" / "tenant_infra.py").read_text(encoding="utf-8")
    assert '"apply"' not in src and "'apply'" not in src
    assert 'argv[0] not in ("render", "plan")' in src


# ═══════════════════════════════════════ предложения IAM (не применены)
def test_secret_container_role_excludes_payload_access():
    perms = I.SECRET_CONTAINER_ROLE["permissions"]
    assert not any(p.startswith("secretmanager.versions.") for p in perms)
    assert "secretmanager.secrets.create" in perms and "secretmanager.secrets.delete" not in perms


def test_provisioner_role_contains_nothing_from_the_never_list():
    for p in I.PROVISIONER_ROLE["permissions"]:
        assert not any(p.startswith(n) for n in I.NEVER_FOR_PROVISIONER), p
    assert "bigquery.tables.getData" not in I.PROVISIONER_ROLE["permissions"]
    assert "cloudscheduler.jobs.enable" not in I.PROVISIONER_ROLE["permissions"]


def test_artifact_registry_plan_has_no_evetis_principal_and_no_writer():
    blob = json.dumps(I.ARTIFACT_REGISTRY_BINDINGS)
    for m in PL.EVETIS_FORBIDDEN_MARKERS:
        assert m not in blob
    assert {b["role"] for b in I.ARTIFACT_REGISTRY_BINDINGS["bindings"]} == {"roles/artifactregistry.reader"}
