"""Tenancy T1: контракт арендатора, канонические имена, защита EVETIS, поиск учётных данных.

Всё офлайн. Документы для негативных случаев строятся копией настоящих
tenants/*/tenant.json с одной точечной порчей, чтобы каждый тест ломал ровно
одно правило.
"""
from __future__ import annotations

import ast
import copy
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from tools.tenancy import naming as N  # noqa: E402
from tools.tenancy import registry as R  # noqa: E402
from tools.tenancy import validation as V  # noqa: E402

TENANTS = REPO / "tenants"
# Документы арендаторов читаются только каноническим строгим загрузчиком (T2.1/L2).
EVETIS = V.load_tenant_document(TENANTS / "evetis" / "tenant.json")
CLIENT = V.load_tenant_document(TENANTS / "client_001" / "tenant.json")


def rules(doc, source="t.json"):
    return {f.rule for f in V.validate_tenant(doc, source)}


def mutate(doc, path, value):
    d = copy.deepcopy(doc)
    node = d
    keys = path.split(".")
    for k in keys[:-1]:
        node = node[k]
    if value is ...:
        del node[keys[-1]]
    else:
        node[keys[-1]] = value
    return d


# ─────────────────────────────────────────────── реестр в репозитории
def test_repository_registry_is_valid():
    assert R.validate_root() == []


def test_schema_uses_only_supported_keywords():
    V.load_schema()     # check_schema бросит SchemaError на неподдерживаемом ключевом слове


def test_schema_has_no_field_for_secret_values():
    """В контракте нет ни одного места, куда можно положить значение ключа.

    Единственные «секретные» поля — secret_refs, и каждое из них ограничено
    шаблоном ID секрета Secret Manager.
    """
    schema = V.load_schema()
    refs = schema["properties"]["marketplaces"]["properties"]["ozon"]["properties"]["secret_refs"]
    assert refs["additionalProperties"] is False
    for name, sub in refs["properties"].items():
        assert sub["pattern"] == "^[A-Za-z0-9_-]{1,255}$", name

    def walk(s, path):
        if isinstance(s, dict):
            if s.get("type") == "object" or "properties" in s:
                assert s.get("additionalProperties") is False, f"{path}: открытый объект"
            for k, v in s.get("properties", {}).items():
                if path.endswith("secret_refs"):
                    continue
                assert not V.SUSPICIOUS_KEY.search(k) or k == "secret_refs", f"{path}.{k}"
                walk(v, f"{path}.{k}")
            if isinstance(s.get("items"), dict):
                walk(s["items"], f"{path}[]")
    walk(schema, "$")


# ─────────────────────────────────────────────────────────── naming
def test_derivation_is_deterministic_and_normalized():
    assert N.derive_project_id("client_001") == "mpa-t-client-001"
    assert N.derive_project_id("client_001") == N.derive_project_id("client_001")
    # T2.1/L6: ревизия — на фиксированной позиции префикса, а не хвостом к slug
    assert N.derive_project_id("client_001", 7) == "mpa-t7-client-001"
    assert N.slug("client_001") == "client-001"
    assert N.terraform_state_prefix("client_001") == "tenants/client_001"
    assert N.resource_labels("client_001") == {"tenant": "client_001"}
    assert N.expected_ozon_secret_ids("client_001") == N.DEDICATED_OZON_SECRET_IDS
    assert N.expected_datasets("client_001") == {"ozon_raw": "ozon_raw", "ref": "ref", "ozon_mart": "ozon_mart",
                                                  "tenant_ops": "tenant_ops", "analytics_share": "analytics_share",
                                                  "tenant_locks": "tenant_locks"}


def test_slug_is_injective_so_distinct_ids_never_share_a_project():
    ids = ["abc", "abc_1", "abc1", "a_bc", "ab_c", "client_001", "client001", "client_0_01"]
    assert len({N.derive_project_id(i) for i in ids}) == len(ids)


@pytest.mark.parametrize("bad", [
    "", "ab", "1abc", "_abc", "Abc", "abc-def", "abc def", "abc.def", "abc__def", "abc_",
    "a" * 32, "клиент", None,
])
def test_invalid_tenant_ids(bad):
    with pytest.raises(N.NamingError):
        N.check_tenant_id(bad)


@pytest.mark.parametrize("reserved", ["evetis", "evetis_shop", "platform", "admin", "prod",
                                      "mpa_client", "google_x", "wildberries", "tenants"])
def test_reserved_ids_are_unavailable_to_dedicated_tenants(reserved):
    with pytest.raises(N.NamingError):
        N.check_tenant_id(reserved)


def test_evetis_is_the_only_legacy_id():
    N.check_tenant_id("evetis", legacy=True)
    with pytest.raises(N.NamingError):
        N.check_tenant_id("client_001", legacy=True)


def test_maximum_lengths():
    ok = "a" * 24                                   # 6 + 24 = 30 символов проекта
    assert len(N.derive_project_id(ok)) == 30
    N.check_tenant_id("a" * 31)                     # сам ID допустим по контракту…
    with pytest.raises(N.NamingError, match="длиннее 30"):
        N.derive_project_id("a" * 25)               # …но для проекта слишком длинный
    assert len(N.derive_project_id("a" * 23, 9)) == 30     # ревизия занимает один символ
    with pytest.raises(N.NamingError, match="длиннее 30"):
        N.derive_project_id("a" * 24, 2)            # без обрезки: отказ


def test_project_ownership():
    """У каждого допустимого ID проекта ровно один владелец (обратное отображение)."""
    assert N.parse_project_id("mpa-t-client-001") == ("client_001", 1)
    assert N.parse_project_id("mpa-t2-client-001") == ("client_001", 2)
    # бывшая «суффиксная» форма принадлежит ДРУГОМУ арендатору, а не client_001
    assert N.parse_project_id("mpa-t-client-001-a1") == ("client_001_a1", 1)
    for bad in ("mpa-t1-client-001", "mpa-t-client-001-",
                "mpa-t--client-001", N.LEGACY_EVETIS["gcp_project_id"], "mpa-t-evetis_x"):
        with pytest.raises(N.NamingError):
            N.parse_project_id(bad)


# ───────────────────────────────────── EVETIS-факт сверен с production-контрактом
def _py_dict_literal(path: Path, name: str):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} не найден в {path}")


def test_legacy_evetis_pins_match_the_live_deployment_contract():
    common = REPO / "pipelines" / "ozon" / "runtime" / "common.py"
    defaults = _py_dict_literal(common, "LEGACY_EVETIS_SECRET_DEFAULTS")
    assert {N.OZON_SECRET_ENV_VARS[r]: v for r, v in N.LEGACY_EVETIS["ozon_secret_refs"].items()} == defaults
    assert _py_dict_literal(common, "LEGACY_EVETIS_REF_DATASET") == N.LEGACY_EVETIS["datasets"]["ref"]

    tfvars = (REPO / "infra" / "terraform" / "terraform.tfvars.example").read_text(encoding="utf-8")
    assert re.search(r'project_id\s*=\s*"' + re.escape(N.LEGACY_EVETIS["gcp_project_id"]) + '"', tfvars)
    tf = (REPO / "infra" / "terraform" / "ozon_ingestion.tf").read_text(encoding="utf-8")
    assert re.search(r'BQ_RAW_DATASET\s*=\s*"ozon_raw"', tf)
    variables = (REPO / "infra" / "terraform" / "variables.tf").read_text(encoding="utf-8")
    assert re.search(r'variable "bq_location".*?default\s*=\s*"EU"', variables, re.S)


def test_descriptor_and_pins_agree():
    assert EVETIS["data_boundary"]["gcp_project_id"] == N.LEGACY_EVETIS["gcp_project_id"]
    assert EVETIS["data_boundary"]["datasets"] == N.LEGACY_EVETIS["datasets"]
    assert EVETIS["marketplaces"]["ozon"]["secret_refs"] == N.LEGACY_EVETIS["ozon_secret_refs"]
    assert EVETIS["config_authority"] == "LEGACY_EXISTING_CONFIG"


# ───────────────────────────────────────────── валидация: отказные случаи
@pytest.mark.parametrize("path,value,rule", [
    ("tenant_id", "Client-1", "schema"),
    ("status", "LIVE", "schema"),
    ("data_boundary.gcp_project_id", "Bad_Project", "schema"),
    ("data_boundary.gcp_project_id", "mpa-t-client-002", "naming"),
    ("marketplaces.ozon.secret_refs.seller_api_key", "tenant/ozon/api_key", "schema"),
    ("marketplaces.ozon.secret_refs.seller_api_key", "custom-name", "naming"),
    ("unexpected_field", "x", "schema"),
    ("marketplaces.ozon.api_key", "ozon-seller-api-key", "schema"),
    ("modules.unitka", True, "module_unsupported"),
    ("marketplaces.ozon.fulfillment_schemes", ["FBO", "FBS"], "fulfillment_unsupported"),
    ("marketplaces.ozon.entities", ["catalog", "no_such_entity"], "entities"),
    ("marketplaces.ozon.entities", ["catalog", "promo"], "entities"),
    ("cogs_policy", "LEGACY_EVETIS", "legacy_only"),
    ("scheduler_state", "NOT_MANAGED", "legacy_only"),
    ("bi_access", {"mode": "LEGACY_EVETIS_METABASE"}, "legacy_only"),
    ("bi_access", {"mode": "ANALYTICS_SHARE_VIEWER"}, "bi"),
    ("config_authority", "LEGACY_EXISTING_CONFIG", "authority"),
    ("marketplaces.ozon.secret_refs.performance_client_secret", ..., "secret_ref_missing"),
    ("status", "SUSPENDED", "lifecycle"),
    ("lifecycle.created_at", "2026-09-24", "schema"),
])
def test_dedicated_tenant_violations(path, value, rule):
    assert rule in rules(mutate(CLIENT, path, value))


@pytest.mark.parametrize("path,value", [
    ("data_boundary.gcp_project_id", "mpa-t-evetis-new"),
    ("data_boundary.datasets", {"ozon_raw": "ozon_raw_v2", "ref": "evetis_ref"}),
    ("marketplaces.ozon.secret_refs.seller_api_key", "EVETIS_OZON_API_KEY_V2"),
    ("scheduler_state", "PAUSED"),
    ("status", "SUSPENDED"),
    ("marketplaces.ozon.entities", ["catalog"]),
])
def test_evetis_descriptor_cannot_be_used_to_mutate_production(path, value):
    """Любая правка дескриптора EVETIS против зафиксированного факта — ошибка CI."""
    found = rules(mutate(EVETIS, path, value))
    assert "evetis_protected" in found or "lifecycle" in found


def test_dedicated_tenant_cannot_point_at_evetis_resources():
    d = mutate(CLIENT, "data_boundary.gcp_project_id", N.LEGACY_EVETIS["gcp_project_id"])
    assert "evetis_protected" in rules(d)
    d = mutate(CLIENT, "marketplaces.ozon.secret_refs.seller_api_key", "EVETIS_OZON_API_KEY")
    assert "evetis_protected" in rules(d)
    d = mutate(CLIENT, "tenant_id", "evetis")
    assert "naming" in rules(d)


# ───────────────────────────────────────────── поиск учётных данных
SYNTHETIC_CREDENTIALS = {
    "uuid_api_key": "0b7c0f3e-5a41-4c0e-9d1a-2f6b8e4c1a77",
    "ozon_performance_client_id": "12345678-1712345678901@advertising.performance.ozon.ru",
    "jwt": "eyJhbGciOiJFUzI1NiJ9.eyJhY2MiOjEsImVudCI6MX0.c2lnbmF0dXJl",
    "pem_block": "-----BEGIN PRIVATE KEY-----",
    "long_random_token": "Zk3q9XwP2vL8rT5mN1bY7cH4jG6dS0aE",
    "bare_number": "5550199",
}


@pytest.mark.parametrize("kind,value", sorted(SYNTHETIC_CREDENTIALS.items()))
def test_credential_material_is_detected_and_never_echoed(kind, value):
    for path in ("legal_name", "notes", "marketplaces.ozon.secret_refs.seller_api_key"):
        findings = V.validate_tenant(mutate(CLIENT, path, value), "t.json")
        assert any(f.rule.startswith("credential") for f in findings), (kind, path)
        assert all(value not in str(f) for f in findings), "значение попало в сообщение"


def test_clean_documents_raise_no_credential_findings():
    for doc in (EVETIS, CLIENT):
        assert V.detect_credential_material(doc, "t.json") == []


def test_suspicious_key_outside_secret_refs_is_flagged():
    d = copy.deepcopy(CLIENT)
    d["marketplaces"]["ozon"]["api_key"] = "ozon-seller-api-key"
    assert any(f.rule == "suspicious_key" for f in V.detect_credential_material(d, "t.json"))


# ───────────────────────────────────────────── реестр целиком
def _copy_registry(tmp_path):
    root = tmp_path / "tenants"
    shutil.copytree(TENANTS, root)
    return root


def _write(root, tid, doc):
    (root / tid).mkdir(exist_ok=True)
    (root / tid / "tenant.json").write_text(json.dumps(doc), encoding="utf-8")


def test_l6_suffix_collision_is_structurally_impossible(tmp_path):
    """T2.1/L6: прежде client + суффикс «001» давал ID проекта client_001. Теперь
    такой ID не выводится из tenant 'client' ни при какой ревизии."""
    root = _copy_registry(tmp_path)
    other = mutate(CLIENT, "tenant_id", "client")
    other["data_boundary"]["gcp_project_id"] = "mpa-t-client-001"
    _write(root, "client", other)
    found = R.validate_root(root)
    assert any(f.rule == "naming" and "client.tenant.json" not in f.source
               and f.path == "$.data_boundary.gcp_project_id" for f in found)


def test_registry_detects_duplicate_identity(tmp_path):
    """Защита в глубину: тот же документ во втором каталоге — коллизия проекта и ID."""
    root = _copy_registry(tmp_path)
    _write(root, "client_002", CLIENT)
    assert any(f.rule == "collision" for f in R.validate_root(root))


def test_registry_requires_directory_name_to_match(tmp_path):
    root = _copy_registry(tmp_path)
    _write(root, "client_002", CLIENT)
    assert any(f.rule == "registry" and "каталога" in f.message for f in R.validate_root(root))


def test_registry_rejects_stray_files_and_bad_json(tmp_path):
    root = _copy_registry(tmp_path)
    (root / "client_001" / "keys.txt").write_text("x", encoding="utf-8")
    (root / "loose.json").write_text("{}", encoding="utf-8")
    (root / "broken").mkdir()
    (root / "broken" / "tenant.json").write_text("{not json", encoding="utf-8")
    msgs = [str(f) for f in R.validate_root(root)]
    assert any("keys.txt" in m for m in msgs)
    assert any("loose.json" in m for m in msgs)
    assert any("[json]" in m for m in msgs)


def test_registry_requires_evetis_descriptor(tmp_path):
    root = _copy_registry(tmp_path)
    shutil.rmtree(root / "evetis")
    assert any("нет дескриптора EVETIS" in f.message for f in R.validate_root(root))


def test_second_legacy_tenant_is_rejected(tmp_path):
    root = _copy_registry(tmp_path)
    twin = mutate(EVETIS, "tenant_id", "evetis_two")
    _write(root, "evetis_two", twin)
    assert R.validate_root(root) != []


# ───────────────────────────────────────────── CLI (ворота CI)
def test_cli_exit_codes():
    ok = subprocess.run([sys.executable, str(REPO / "tools" / "tenancy" / "registry.py"), "validate"],
                        capture_output=True, text=True, timeout=60)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    bad = subprocess.run([sys.executable, str(REPO / "tools" / "tenancy" / "registry.py"), "nope"],
                         capture_output=True, text=True, timeout=60)
    assert bad.returncode == 3


def test_runtime_env_contains_names_only():
    env = R.ozon_runtime_env(CLIENT)
    assert env["GCP_PROJECT_ID"] == "mpa-t-client-001"
    assert env["OZON_SECRET_SELLER_API_KEY"] == "ozon-seller-api-key"
    # имена переменных OZON_SECRET_* законны; проверяются значения — только ИМЕНА секретов
    assert V.detect_credential_material({"values": list(env.values())}, "env") == []
